#Requires -Version 5.1
<#
.SYNOPSIS
  Nightly "Dream" runner: harvest the day's material, then run the headless consolidation on
  claude-opus-4.8 or gpt-5.6-sol at 1M context / max reasoning.

.DESCRIPTION
  Enforces the two-model policy, stages a deterministic harvest, invokes `copilot -p` with the
  Dream consolidation prompt, and advances the run watermark only on success.

.EXAMPLE
  .\run-dream.ps1                       # default model (claude-opus-4.8), full run
  .\run-dream.ps1 -Model gpt-5.6-sol    # use GPT-5.6 Sol instead
  .\run-dream.ps1 -DryRun               # harvest + print the command, do NOT call the model
#>
[CmdletBinding()]
param(
  [ValidateSet('claude-opus-4.8','gpt-5.6-sol')]
  [string]$Model = 'claude-opus-4.8',
  [double]$Hours = 0,                 # 0 = auto (watermark-based)
  [int]$TimeoutMinutes = 45,          # kill + verify-by-artifact if the model run exceeds this
  [switch]$SkipHarvest,
  [switch]$ProposeOnly,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$engine   = Join-Path $env:USERPROFILE '.copilot\dream'
$config   = Join-Path $engine 'config.json'
$promptFp = Join-Path $engine 'dream-consolidation.prompt.md'
$stateFp  = Join-Path $engine 'state.json'
$logsDir  = Join-Path $engine 'logs'
$harvest  = Join-Path $engine 'harvest'
$journalDir = Join-Path $engine 'journal'
$stamp    = Get-Date -Format 'yyyyMMdd-HHmmss'
$today    = Get-Date -Format 'yyyy-MM-dd'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Log($m){ $line = "{0}  {1}" -f (Get-Date -Format o), $m; Write-Host $line; Add-Content -Path (Join-Path $logsDir "run-$today.log") -Value $line }

# retention: keep recent harvest snapshots / run outputs / logs (journals are kept forever - tiny + audit)
try {
  Get-ChildItem $harvest -Filter 'harvest-*.*' -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -Skip 40 | Remove-Item -Force -EA SilentlyContinue
  Get-ChildItem $logsDir -Filter 'dream-*.out.txt' -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -Skip 40 | Remove-Item -Force -EA SilentlyContinue
  Get-ChildItem $logsDir -Filter 'run-*.log' -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -Skip 30 | Remove-Item -Force -EA SilentlyContinue
} catch {}

Log "DREAM start model=$Model dryrun=$DryRun host=$env:COMPUTERNAME"

# ---- locate copilot ----
$copilot = (Get-Command copilot -ErrorAction SilentlyContinue).Source
if (-not $copilot) { Log "FATAL copilot not on PATH"; exit 3 }

# ---- Phase A: harvest ----
if (-not $SkipHarvest) {
  $hArgs = @((Join-Path $engine 'harvest.py'),'--config',$config)
  if ($Hours -gt 0) { $hArgs += @('--hours', "$Hours") }
  Log "harvest: python $($hArgs -join ' ')"
  $hout = & python @hArgs 2>&1 | Out-String
  Add-Content -Path (Join-Path $logsDir "run-$today.log") -Value $hout
  Write-Host $hout
  if ($LASTEXITCODE -ne 0) { Log "WARN harvest exit=$LASTEXITCODE (continuing)"; }
} else { Log "harvest skipped" }

# ---- ensure ledger exists ----
& python (Join-Path $engine 'ledger.py') --config $config init 2>&1 | Out-Null

# ---- Phase B: headless consolidation ----
$sessionId = [guid]::NewGuid().ToString()
$safeMode = ""
if ($ProposeOnly) {
  $safeMode = @"

SAFE MODE (propose-only): Do NOT edit any reference skill or dream-active-work in place. Route EVERY
proposed change - including short-term/active-work updates - to the review-queue as proposal files.
Still register items in the ledger and still write the journal. This is a review-only run.
"@
}
$bootstrap = @"
You are running the nightly DREAM consolidation (unattended, autonomous, no questions).
Follow the instructions in this file EXACTLY and execute every phase:
  $promptFp
Inputs:
  - config:  $config
  - harvest: $harvest\latest.json  (read the JSON it points to)
  - ledger:  python $engine\ledger.py <subcommand>
Model policy: you must be on $Model at long_context/max. Write the journal for $today and record the run.$safeMode
"@

$cliArgs = @(
  '-p', $bootstrap,
  '--model', $Model,
  '--context', 'long_context',
  '--effort', 'max',
  '--allow-all-tools',
  '--allow-all-paths',
  '--no-ask-user',
  '--add-dir', $env:USERPROFILE,
  '--log-dir', $logsDir,
  '--log-level', 'info',
  '--name', "dream-$today",
  '--session-id', $sessionId,
  '-C', $engine
)

if ($DryRun) {
  Log "DRYRUN would run: copilot -p <bootstrap> --model $Model --context long_context --effort max --allow-all-tools --allow-all-paths --no-ask-user -C $engine"
  Write-Host "`n--- bootstrap prompt ---`n$bootstrap`n------------------------"
  Log "DRYRUN done"
  exit 0
}

$outFile = Join-Path $logsDir "dream-$today-$stamp.out.txt"
$runStart = Get-Date
$journalToday = Join-Path $journalDir "$today.md"
Log "invoking copilot (session $sessionId) -> $outFile (timeout ${TimeoutMinutes}m)"

# Run copilot in a background job (the call operator preserves multi-line arg fidelity), redirecting all
# streams to the out file. We wait with a timeout: a stuck subagent/MCP teardown must never hang the run.
# Success is judged by artifact (today's journal written fresh + run recorded) OR a clean exit 0.
$job = Start-Job -Name "dream-copilot" -ScriptBlock {
  param($cp, $a, $of)
  & $cp @a *> $of
  $LASTEXITCODE
} -ArgumentList $copilot, $cliArgs, $outFile

$deadline = $runStart.AddMinutes($TimeoutMinutes)
$graceDeadline = $null
$code = $null
$stuckTeardown = $false
while ($true) {
  if ($job.State -ne 'Running') { $code = (Receive-Job $job); Log "copilot job state=$($job.State) exit=$code"; break }
  $journalFresh = (Test-Path $journalToday) -and ((Get-Item $journalToday).LastWriteTime -gt $runStart)
  if ($journalFresh -and -not $graceDeadline) {
    $graceDeadline = (Get-Date).AddSeconds(120)
    Log "success artifact detected (journal $today.md written); allowing up to 120s grace for record-run/teardown"
  }
  if ($graceDeadline -and (Get-Date) -gt $graceDeadline) { Log "grace elapsed; proceeding (teardown still running)"; $stuckTeardown=$true; break }
  if ((Get-Date) -gt $deadline) { Log "TIMEOUT after ${TimeoutMinutes}m; will verify by artifact"; $stuckTeardown=$true; break }
  Start-Sleep -Seconds 10
}

# Stop the job. Only force-kill spawned copilot/node processes when teardown was actually stuck (so a
# clean run never risks a concurrently-launched copilot session).
Stop-Job $job -EA SilentlyContinue; Remove-Job $job -Force -EA SilentlyContinue
if ($stuckTeardown) {
  Get-Process copilot,node -EA SilentlyContinue | Where-Object { $_.StartTime -gt $runStart } |
    ForEach-Object { try { Stop-Process -Id $_.Id -Force -EA SilentlyContinue; Log "killed stuck $($_.ProcessName) $($_.Id)" } catch {} }
}

# ---- success determination: clean exit 0, OR today's journal was written fresh this run ----
$journalOk = (Test-Path $journalToday) -and ((Get-Item $journalToday).LastWriteTime -gt $runStart)
$success = ($code -eq 0) -or $journalOk
Log "success=$success (exitcode=$code journalFresh=$journalOk)"

# ---- Phase C: watermark (advance only on a successful APPLYING run) ----
if ($success -and $ProposeOnly) {
  Log "propose-only success: watermark intentionally NOT advanced (a future applying run reconsiders this window)"
  Log "DREAM ok (propose-only)"
  exit 0
}
elseif ($success) {
  $state = @{ last_run_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); last_model = $Model; last_session = $sessionId }
  ($state | ConvertTo-Json) | Set-Content -Path $stateFp -Encoding utf8
  Log "watermark advanced -> $($state.last_run_utc)"
  Log "DREAM ok"
  exit 0
} else {
  Log "DREAM failed (no journal written, non-zero exit; watermark NOT advanced; next run reconsiders window)"
  exit 1
}
