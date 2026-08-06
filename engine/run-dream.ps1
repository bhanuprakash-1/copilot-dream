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
  [int]$TimeoutMinutes = 60,          # kill + verify-by-artifact if the model run exceeds this
  [ValidateRange(60, 3500)]
  [int]$BackgroundTaskWaitSeconds = 3300,
  [string]$ReplayPlan,
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
$completionDir = Join-Path $engine 'completion'
$pendingDir = Join-Path $engine 'pending'
$replayWorkDir = Join-Path $pendingDir '.work'
$receiptRoot = Join-Path $completionDir 'receipts'
$stamp    = Get-Date -Format 'yyyyMMdd-HHmmss'
$today    = Get-Date -Format 'yyyy-MM-dd'
New-Item -ItemType Directory -Force -Path $logsDir,$completionDir,$pendingDir,$replayWorkDir,$receiptRoot | Out-Null

function Log($m){ $line = "{0}  {1}" -f (Get-Date -Format o), $m; Write-Host $line; Add-Content -Path (Join-Path $logsDir "run-$today.log") -Value $line }

if ($BackgroundTaskWaitSeconds -ge ($TimeoutMinutes * 60)) {
  throw "BackgroundTaskWaitSeconds must be lower than TimeoutMinutes (currently $TimeoutMinutes minutes)."
}

# retention: keep recent harvest snapshots / run outputs / logs (journals are kept forever - tiny + audit)
try {
  Get-ChildItem $harvest -Filter 'harvest-*.*' -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -Skip 40 | Remove-Item -Force -EA SilentlyContinue
  Get-ChildItem $logsDir -Filter 'dream-*.out.txt' -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -Skip 40 | Remove-Item -Force -EA SilentlyContinue
  Get-ChildItem $logsDir -Filter 'run-*.log' -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -Skip 30 | Remove-Item -Force -EA SilentlyContinue
  Get-ChildItem $completionDir -Filter '*.json' -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -Skip 40 | Remove-Item -Force -EA SilentlyContinue
  Get-ChildItem $replayWorkDir -Filter '*.json' -EA SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Force -EA SilentlyContinue
  # map-reduce scratch: keep the last 10 nightly shard dirs (each holds shards + claims + candidates + apply-plan)
  Get-ChildItem (Join-Path $harvest 'shards') -Directory -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -Skip 10 | Remove-Item -Recurse -Force -EA SilentlyContinue
} catch {}

$replayMode = -not [string]::IsNullOrWhiteSpace($ReplayPlan)
$replayPlanPath = $null
$replaySourcePlanPath = $null
if ($replayMode) {
  $replaySourcePlanPath = (Resolve-Path -LiteralPath $ReplayPlan -ErrorAction Stop).Path
  if ([IO.Path]::GetExtension($replaySourcePlanPath) -ne '.json') {
    throw "ReplayPlan must point to a .json apply-plan file."
  }
  if ([IO.Path]::GetFileName($replaySourcePlanPath) -like 'replay-*') {
    throw "ReplayPlan must point to a preserved apply-plan, not a derived replay work file."
  }
}
if ($replayMode -and $ProposeOnly) {
  throw "ReplayPlan cannot be combined with ProposeOnly. Re-run the original propose-only window instead."
}

Log "DREAM start model=$Model dryrun=$DryRun replay=$replayMode backgroundWait=${BackgroundTaskWaitSeconds}s host=$env:COMPUTERNAME"

# ---- locate copilot ----
$copilot = (Get-Command copilot -ErrorAction SilentlyContinue).Source
if (-not $copilot) { Log "FATAL copilot not on PATH"; exit 3 }

# ---- Phase A: harvest ----
if (-not $SkipHarvest -and -not $replayMode) {
  $hArgs = @((Join-Path $engine 'harvest.py'),'--config',$config)
  if ($Hours -gt 0) { $hArgs += @('--hours', "$Hours") }
  Log "harvest: python $($hArgs -join ' ')"
  $oldErrorActionPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $hout = & python @hArgs 2>&1 | Out-String
    $harvestExitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $oldErrorActionPreference
  }
  Add-Content -Path (Join-Path $logsDir "run-$today.log") -Value $hout
  Write-Host $hout
  if ($harvestExitCode -ne 0) { Log "FATAL harvest exit=$harvestExitCode"; exit 4 }
} elseif ($replayMode) {
  Log "harvest skipped (replay source: $replaySourcePlanPath)"
} else {
  Log "harvest skipped"
}

# ---- ensure ledger exists ----
$oldErrorActionPreference = $ErrorActionPreference
try {
  $ErrorActionPreference = 'Continue'
  $ledgerInitOut = & python (Join-Path $engine 'ledger.py') --config $config init 2>&1 | Out-String
  $ledgerInitExitCode = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $oldErrorActionPreference
}
if ($ledgerInitExitCode -ne 0) {
  Add-Content -Path (Join-Path $logsDir "run-$today.log") -Value $ledgerInitOut
  Write-Host $ledgerInitOut
  Log "FATAL ledger init exit=$ledgerInitExitCode"
  exit 5
}

# ---- Phase B: headless consolidation ----
$sessionId = [guid]::NewGuid().ToString()
$receiptDir = Join-Path $receiptRoot $sessionId
New-Item -ItemType Directory -Force -Path $receiptDir | Out-Null
$ephemeralReplayReceipt = $false
if ($replayMode) {
  $replayReceiptDir = $null
  $replayMetaPath = $replaySourcePlanPath -replace '\.json$', '.meta.json'
  if ($replayMetaPath -eq $replaySourcePlanPath) {
    throw "Could not derive a safe replay metadata path."
  }
  $replayMeta = $null
  if (Test-Path -LiteralPath $replayMetaPath) {
    $replayMeta = Get-Content -LiteralPath $replayMetaPath -Raw | ConvertFrom-Json
    if ($replayMeta.mode -eq 'propose-only') {
      Remove-Item -LiteralPath $receiptDir -Recurse -Force -EA SilentlyContinue
      throw "This pending plan came from ProposeOnly and cannot be replayed as an applying run."
    }
    if ($replayMeta.receipt_dir -and (Test-Path -LiteralPath $replayMeta.receipt_dir)) {
      $replayReceiptDir = [string]$replayMeta.receipt_dir
    }
  }
  if ($replayReceiptDir) {
    Remove-Item -LiteralPath $receiptDir -Recurse -Force -EA SilentlyContinue
    $receiptDir = $replayReceiptDir
  } elseif ($DryRun) {
    $replayReceiptDir = $receiptDir
    $ephemeralReplayReceipt = $true
  } else {
    if (-not $replayMeta) {
      $replayMeta = [pscustomobject]@{
        status = 'pending'
        source_plan = $replaySourcePlanPath
      }
    }
    $replayMeta | Add-Member -NotePropertyName receipt_dir -NotePropertyValue $receiptDir -Force
    $replayMeta | ConvertTo-Json -Depth 6 | Set-Content -Path $replayMetaPath -Encoding utf8
    $replayReceiptDir = $receiptDir
  }
  $replayPlanRoot = if ($DryRun) { $env:TEMP } else { $replayWorkDir }
  $replayPlanPath = Join-Path $replayPlanRoot "replay-$today-$stamp-$($sessionId.Substring(0,8)).json"
  $replayArgs = @((Join-Path $engine 'reduce.py'),'--config',$config,'replay','--in',$replaySourcePlanPath,'--out',$replayPlanPath)
  if ($replayReceiptDir) { $replayArgs += @('--receipts',$replayReceiptDir) }
  $oldErrorActionPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $replayOut = & python @replayArgs 2>&1 | Out-String
    $replayExitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $oldErrorActionPreference
  }
  Add-Content -Path (Join-Path $logsDir "run-$today.log") -Value $replayOut
  Write-Host $replayOut
  if ($replayExitCode -ne 0) {
    Log "FATAL replay-plan filtering failed exit=$replayExitCode"
    exit 2
  }
  Log "prepared idempotent replay plan -> $replayPlanPath"
}
$completionFile = Join-Path $completionDir "run-$today-$sessionId.json"
$journalTarget = if ($replayMode) {
  Join-Path $journalDir "$today-replay-$($sessionId.Substring(0,8)).md"
} else {
  Join-Path $journalDir "$today.md"
}
$safeMode = ""
if ($ProposeOnly) {
  $safeMode = @"

SAFE MODE (propose-only): Do NOT edit any reference skill or dream-active-work in place. In the APPLY
phase, SKIP the per-skill and active-work editor sub-agents; instead route EVERY change - including
short-term/active-work updates and promotions - through the review-queue sub-agent as proposal files.
Still shard, still run the MAP classifiers, still merge + upsert to the ledger, and still write the
journal. This is a review-only run.
"@
}
$recoveryMode = ""
if ($replayMode) {
  $recoveryMode = @"

RECOVERY MODE: Resume the unfinished work order at:
  $replayPlanPath
Skip Phases 0-2 completely: do NOT harvest, shard, classify, merge, upsert, or regenerate the plan.
Begin at Phase 3 using that exact apply-plan, then complete Phases 4-5. Journal this as a recovered
APPLY run. Do not advance or reinterpret its claims.
"@
}
$bootstrap = @"
You are running the nightly DREAM consolidation (unattended, autonomous, no questions).
Run as a LEAN MAP-REDUCE ORCHESTRATOR: shard the harvest, fan out parallel classifier sub-agents (MAP),
merge their compact JSON (REDUCE), fan out parallel per-skill editor sub-agents (APPLY), then journal.
Do NOT read raw session transcripts or full skill bodies into your own context - keep it lean and push
all heavy reading/editing into sub-agents. Follow the instructions in this file EXACTLY, every phase:
  $promptFp
Inputs:
  - config:  $config
  - harvest: $harvest\latest.json  (the sharder reads it; you read the shard manifest, not the bodies)
  - shard:   python $engine\shard.py --config $config
  - reduce:  python $engine\reduce.py --config $config <merge|plan> ...
  - ledger:  python $engine\ledger.py <subcommand>
Model policy: you must be on $Model at long_context/max, and spawn every sub-agent on $Model too.
HEADLESS LIFECYCLE: NEVER end your turn while any background sub-agent is running. Keep the same turn
active and wait with read_agent(wait:true, timeout up to 180 seconds), repeating until every launched
agent has completed. Do not rely on a later completion notification to wake a new turn.
Every successful APPLY bucket must write its receipt under:
  $receiptDir
Write the journal to this exact path and record the run:
  $journalTarget
After both succeed, write the final completion marker:
  $completionFile
The marker must be valid JSON with session_id="$sessionId", date="$today", status="complete", and a
completed_utc timestamp. It is the FINAL filesystem action of the run.$safeMode$recoveryMode
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
  if ($replayMode) { Remove-Item -LiteralPath $replayPlanPath -Force -EA SilentlyContinue }
  if (-not $replayMode -or $ephemeralReplayReceipt) {
    Remove-Item -LiteralPath $receiptDir -Recurse -Force -EA SilentlyContinue
  }
  Log "DRYRUN done"
  exit 0
}

$outFile = Join-Path $logsDir "dream-$today-$stamp.out.txt"
$runStart = Get-Date
Log "invoking copilot (session $sessionId) -> $outFile (timeout ${TimeoutMinutes}m)"
Log "copilot background-task drain timeout=${BackgroundTaskWaitSeconds}s"

# Run copilot in a background job (the call operator preserves multi-line arg fidelity), redirecting all
# streams to the out file. We wait with a timeout: a stuck subagent/MCP teardown must never hang the run.
# Success is judged only by fresh journal + final completion marker. Process exit 0 is not task completion.
$job = Start-Job -Name "dream-copilot" -ScriptBlock {
  param($cp, $a, $of, $taskWaitSeconds)
  $env:COPILOT_TASK_WAIT_TIMEOUT_SECONDS = [string]$taskWaitSeconds
  & $cp @a *> $of
  $LASTEXITCODE
} -ArgumentList $copilot, $cliArgs, $outFile, $BackgroundTaskWaitSeconds

$deadline = $runStart.AddMinutes($TimeoutMinutes)
$graceDeadline = $null
$code = $null
$stuckTeardown = $false
while ($true) {
  if ($job.State -ne 'Running') { $code = (Receive-Job $job); Log "copilot job state=$($job.State) exit=$code"; break }
  $completionFresh = (Test-Path $completionFile) -and ((Get-Item $completionFile).LastWriteTime -gt $runStart)
  if ($completionFresh -and -not $graceDeadline) {
    $graceDeadline = (Get-Date).AddSeconds(180)
    Log "completion marker detected; allowing up to 180s grace for teardown"
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

# ---- success determination: both task-completion artifacts must be fresh ----
$journalOk = (Test-Path $journalTarget) -and ((Get-Item $journalTarget).LastWriteTime -gt $runStart)
$completionOk = (Test-Path $completionFile) -and ((Get-Item $completionFile).LastWriteTime -gt $runStart)
$success = $journalOk -and $completionOk
Log "success=$success (exitcode=$code journalFresh=$journalOk completionFresh=$completionOk)"

function Preserve-PendingApplyPlan {
  $plan = Get-ChildItem (Join-Path $harvest 'shards') -Filter 'apply-plan.json' -Recurse -EA SilentlyContinue |
    Where-Object { $_.LastWriteTime -gt $runStart } |
    Sort-Object LastWriteTime -Desc |
    Select-Object -First 1
  if (-not $plan) {
    Log "no fresh apply-plan found to preserve"
    return
  }

  $base = "apply-plan-$today-$stamp-$($sessionId.Substring(0,8))"
  $pendingPlan = Join-Path $pendingDir "$base.json"
  $pendingMeta = Join-Path $pendingDir "$base.meta.json"
  Copy-Item -LiteralPath $plan.FullName -Destination $pendingPlan -Force
  $meta = [ordered]@{
    status = 'pending'
    preserved_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    run_date = $today
    session_id = $sessionId
    model = $Model
    source_plan = $plan.FullName
    pending_plan = $pendingPlan
    output_file = $outFile
    exit_code = $code
    journal_fresh = $journalOk
    completion_fresh = $completionOk
    mode = $(if ($ProposeOnly) { 'propose-only' } else { 'apply' })
    receipt_dir = $receiptDir
    replay_command = $(if ($ProposeOnly) { $null } else { "powershell -File `"$PSCommandPath`" -ReplayPlan `"$pendingPlan`"" })
  }
  ($meta | ConvertTo-Json -Depth 4) | Set-Content -Path $pendingMeta -Encoding utf8
  Log "preserved unfinished apply-plan -> $pendingPlan"
}

# ---- Phase C: watermark (advance only on a successful APPLYING run) ----
if ($success -and $replayMode) {
  $replayDone = "$replaySourcePlanPath.completed.json"
  ([ordered]@{
    status = 'complete'
    completed_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    session_id = $sessionId
    source_plan = $replaySourcePlanPath
    filtered_plan = $replayPlanPath
    receipt_dir = $receiptDir
    completion_marker = $completionFile
  } | ConvertTo-Json) | Set-Content -Path $replayDone -Encoding utf8
  Remove-Item -LiteralPath $replayPlanPath -Force -EA SilentlyContinue
  Log "replay success: watermark intentionally NOT advanced; completion receipt -> $replayDone"
  Log "DREAM ok (replay)"
  exit 0
}
elseif ($success -and $ProposeOnly) {
  Log "propose-only success: watermark intentionally NOT advanced (a future applying run reconsiders this window)"
  Log "DREAM ok (propose-only)"
  exit 0
}
elseif ($success) {
  $state = @{ last_run_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); last_model = $Model; last_session = $sessionId }
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [IO.File]::WriteAllText($stateFp, ($state | ConvertTo-Json), $utf8NoBom)
  Log "watermark advanced -> $($state.last_run_utc)"
  Log "DREAM ok"
  exit 0
} else {
  if ($replayMode) {
    Remove-Item -LiteralPath $replayPlanPath -Force -EA SilentlyContinue
    Log "replay failed; source plan remains at $replaySourcePlanPath"
  } else {
    Preserve-PendingApplyPlan
  }
  Log "DREAM failed (completion artifacts missing; watermark NOT advanced; next run reconsiders window)"
  exit 1
}
