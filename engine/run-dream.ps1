#Requires -Version 5.1
<#
.SYNOPSIS
  Nightly "Dream" runner: harvest the day's material, then run the headless consolidation on
  the cost-conscious model pinned in config.json, with long_context and xhigh reasoning.

.DESCRIPTION
  Enforces config.model_policy, stages a deterministic harvest, invokes `copilot -p` with the
  Dream consolidation prompt, and advances the run watermark only on success.

  The headless run starts with every MCP server disabled (config.runner.disable_mcp_servers): the
  Dream only needs local tools, and unattended OAuth-backed servers can make Copilot abort at startup
  while settling managed MCP policy. A failed attempt that applied nothing (no apply-plan, no receipts)
  is retried with a fresh session, up to config.runner.max_attempts.

.EXAMPLE
  .\run-dream.ps1                       # configured model, full run
  .\run-dream.ps1 -Model gpt-5.6-sol     # explicit allowed model
  .\run-dream.ps1 -DryRun               # harvest + print the command, do NOT call the model
#>
[CmdletBinding()]
param(
  [string]$Model,
  [double]$Hours = 0,                 # 0 = auto (watermark-based)
  [int]$TimeoutMinutes = 60,          # kill + verify-by-artifact if the model run exceeds this
  [ValidateRange(60, 3500)]
  [int]$BackgroundTaskWaitSeconds = 3300,
  [string]$ReplayPlan,
  [switch]$SkipHarvest,
  [switch]$ProposeOnly,
  [switch]$DryRun,
  # Internal: set by the runner when it re-invokes itself to retry a failed attempt.
  [int]$Attempt = 1,
  [string]$RunStartedUtc
)

$ErrorActionPreference = 'Stop'
$engine   = Join-Path $env:USERPROFILE '.copilot\dream'
$config   = Join-Path $engine 'config.json'
$modelSettings = & (Join-Path $engine 'resolve-model-policy.ps1') -Config $config -Model $Model
$Model = $modelSettings.Model
$Context = $modelSettings.Context
$Effort = $modelSettings.Effort
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

$cfgAll = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
$runnerCfg = $cfgAll.runner
# The active-work applier appends compacted text here with file tools that need an existing parent dir.
$archiveDir = if ($cfgAll.paths.archive_dir) { $cfgAll.paths.archive_dir -replace '^~', $env:USERPROFILE } else { Join-Path $engine 'archive' }
New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null
function Get-RunnerSetting([string]$Name, $Default) {
  if ($runnerCfg -and $null -ne $runnerCfg.$Name) { return $runnerCfg.$Name }
  return $Default
}
$maxAttempts = [Math]::Max(1, [int](Get-RunnerSetting 'max_attempts' 2))
$retryBackoffSeconds = [Math]::Max(0, [int](Get-RunnerSetting 'retry_backoff_seconds' 120))
$totalBudgetMinutes = [int](Get-RunnerSetting 'total_budget_minutes' 170)
$disableMcp = [bool](Get-RunnerSetting 'disable_mcp_servers' $true)
$keepMcp = @(Get-RunnerSetting 'keep_mcp_servers' @())
$firstStartUtc = if ($RunStartedUtc) {
  [datetime]::Parse($RunStartedUtc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
} else { (Get-Date).ToUniversalTime() }

# Every MCP server visible from the engine directory (user, workspace, plugin, builtin), so each can be
# disabled for the headless run. Falls back to the user mcp-config.json if `copilot mcp list` fails.
function Get-McpServerNames([string]$CopilotPath, [string]$WorkDir) {
  $names = New-Object System.Collections.Generic.List[string]
  $oldPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    Push-Location -LiteralPath $WorkDir
    try { $raw = & $CopilotPath mcp list --json 2>$null | Out-String } finally { Pop-Location }
    $brace = $raw.IndexOf('{')
    if ($LASTEXITCODE -eq 0 -and $brace -ge 0) {
      foreach ($p in ($raw.Substring($brace) | ConvertFrom-Json).mcpServers.PSObject.Properties) { $names.Add($p.Name) }
    }
  } catch {
  } finally {
    $ErrorActionPreference = $oldPreference
  }
  if ($names.Count -eq 0) {
    try {
      $userMcp = Get-Content -LiteralPath (Join-Path $env:USERPROFILE '.copilot\mcp-config.json') -Raw | ConvertFrom-Json
      foreach ($p in $userMcp.mcpServers.PSObject.Properties) { $names.Add($p.Name) }
    } catch {}
  }
  return @($names | Select-Object -Unique)
}

# One line explaining why an attempt produced no completion artifacts, for the run log and digest.
function Get-FailureReason([string]$OutFile, $ExitCode, [bool]$TimedOut) {
  if ($TimedOut) { return "run exceeded ${TimeoutMinutes}m before writing the journal + completion marker" }
  if (-not (Test-Path -LiteralPath $OutFile)) { return "copilot exit=$ExitCode and produced no output" }
  $fatal = Select-String -LiteralPath $OutFile -Pattern 'Error executing prompt|^\s*Error:|FATAL' -Context 0,1 | Select-Object -First 1
  if ($fatal) {
    $text = ($fatal.Line -replace '^\s*copilot(\.exe)?\s*:\s*', '').Trim()
    # PowerShell wraps native stderr at console width; rejoin the continuation line.
    $next = @($fatal.Context.PostContext)[0]
    if ($fatal.Line.Length -ge 100 -and $next -and $next -notmatch '^\s*(At line|\+|$)') { $text = "$text $($next.Trim())" }
    return $text.Substring(0, [Math]::Min(300, $text.Length))
  }
  if (Select-String -LiteralPath $OutFile -SimpleMatch 'to=functions.' -Quiet) {
    return "copilot exit=${ExitCode}: the orchestrator wrote tool calls as plain text instead of executing them"
  }
  return "copilot exit=$ExitCode without writing the journal + completion marker"
}

function Get-FreshApplyPlan([datetime]$Since) {
  Get-ChildItem (Join-Path $harvest 'shards') -Filter 'apply-plan.json' -Recurse -EA SilentlyContinue |
    Where-Object { $_.LastWriteTime -gt $Since } |
    Sort-Object LastWriteTime -Desc |
    Select-Object -First 1
}

# Skill history (skillaudit.py): snapshot around the run so every skill edit is itemized and revertible.
# Never fatal - a history problem is logged and the Dream carries on.
function Invoke-SkillHistory([string[]]$HistoryArgs) {
  $oldPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $out = & python (Join-Path $engine 'skillaudit.py') --config $config @HistoryArgs 2>&1 | Out-String
    $code = $LASTEXITCODE
  } catch {
    $out = "skillaudit failed to start: $($_.Exception.Message)"; $code = 1
  } finally {
    $ErrorActionPreference = $oldPreference
  }
  foreach ($line in ($out -split "`r?`n" | Where-Object { $_.Trim() })) { Log "history: $line" }
  if ($code -ne 0) { Log "history: skillaudit exit=$code (continuing)" }
}

# Kill every process descended from $RootPid that started after $Since, children before parents.
# Parent ids survive a parent's exit on Windows, so the walk still finds orphaned grandchildren.
function Stop-DreamProcessTree([int]$RootPid, [datetime]$Since) {
  $children = @{}
  foreach ($p in @(Get-CimInstance Win32_Process -EA SilentlyContinue)) {
    $parent = [int]$p.ParentProcessId
    if (-not $children.ContainsKey($parent)) { $children[$parent] = New-Object System.Collections.Generic.List[object] }
    $children[$parent].Add($p)
  }
  $victims = New-Object System.Collections.Generic.List[object]
  $queue = New-Object System.Collections.Generic.Queue[int]
  $queue.Enqueue($RootPid)
  while ($queue.Count -gt 0) {
    $id = $queue.Dequeue()
    if (-not $children.ContainsKey($id)) { continue }
    foreach ($child in $children[$id]) {
      if ($child.CreationDate -ge $Since -and -not ($victims | Where-Object { $_.ProcessId -eq $child.ProcessId })) {
        $victims.Add($child); $queue.Enqueue([int]$child.ProcessId)
      }
    }
  }
  for ($i = $victims.Count - 1; $i -ge 0; $i--) {
    try { Stop-Process -Id $victims[$i].ProcessId -Force -EA Stop; Log "killed stuck $($victims[$i].Name) $($victims[$i].ProcessId)" } catch {}
  }
}

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

# One Dream run at a time. skillaudit.py refuses reverts/restores while this lock names a live process;
# a lock left by a killed run expires with its process. Retries run inside the lock their parent holds.
$lockFile = Join-Path $engine 'run.lock'
function Remove-DreamLock { if ($Attempt -eq 1 -and -not $DryRun) { Remove-Item -LiteralPath $lockFile -Force -EA SilentlyContinue } }
if ($Attempt -eq 1 -and -not $DryRun) {
  $holder = $null
  try { $holder = Get-Content -LiteralPath $lockFile -Raw -EA Stop | ConvertFrom-Json } catch {}
  if ($holder) {
    $startedAt = if ($holder.started_utc -is [datetime]) { $holder.started_utc.ToUniversalTime() } else {
      try { [datetime]::Parse([string]$holder.started_utc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime() } catch { [datetime]::MinValue } }
    $proc = Get-Process -Id ([int]$holder.pid) -EA SilentlyContinue
    $live = $proc -and $proc.StartTime.ToUniversalTime() -le $startedAt.AddMinutes(1) -and ((Get-Date).ToUniversalTime() - $startedAt).TotalHours -lt 4
    if ($live) { Log "DREAM skipped: another Dream run is in progress (pid $($holder.pid), started $($holder.started_utc))"; exit 6 }
  }
  ([ordered]@{ pid = $PID; started_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') } | ConvertTo-Json -Compress) |
    Set-Content -LiteralPath $lockFile -Encoding ascii
}

Log "DREAM start model=$Model attempt=$Attempt/$maxAttempts dryrun=$DryRun replay=$replayMode backgroundWait=${BackgroundTaskWaitSeconds}s host=$env:COMPUTERNAME"

# ---- locate copilot ----
$copilot = (Get-Command copilot -ErrorAction SilentlyContinue).Source
if (-not $copilot) { Log "FATAL copilot not on PATH"; Remove-DreamLock; exit 3 }

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
  if ($harvestExitCode -ne 0) { Log "FATAL harvest exit=$harvestExitCode"; Remove-DreamLock; exit 4 }
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
  Remove-DreamLock
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
    Remove-DreamLock
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
Model policy: you must be on $Model at $Context/$Effort. For EVERY sub-agent, parallel worker and retry,
explicitly set model="$Model", reasoning_effort="$Effort", context_tier="$Context" in the launch tool.
Never inherit tool defaults or use a weaker model, lower effort or default context. If the requested
configuration is unavailable, stop and report the policy failure; do not downgrade.
HEADLESS LIFECYCLE: NEVER end your turn while any background sub-agent is running. Keep the same turn
active and wait with read_agent(wait:true, timeout up to 180 seconds), repeating until every launched
agent has completed. Do not rely on a later completion notification to wake a new turn.
Execute every step through real tool calls. Never write a tool call or its output as plain text, and
never report an artifact as written until a tool result shows it exists on disk.
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
  '--context', $Context,
  '--effort', $Effort,
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

$mcpArgs = @()
if ($disableMcp) {
  $mcpNames = @(Get-McpServerNames -CopilotPath $copilot -WorkDir $engine | Where-Object { $keepMcp -notcontains $_ })
  foreach ($name in $mcpNames) { $mcpArgs += @('--disable-mcp-server', $name) }
  if ($keepMcp -notcontains 'github-mcp-server') { $mcpArgs += '--disable-builtin-mcps' }
  Log ("mcp: disabled {0} server(s) for the headless run: {1}" -f $mcpNames.Count, ($mcpNames -join ', '))
}
$cliArgs += $mcpArgs

if ($DryRun) {
  Log "DRYRUN would run: copilot -p <bootstrap> --model $Model --context $Context --effort $Effort --allow-all-tools --allow-all-paths --no-ask-user -C $engine $($mcpArgs -join ' ')"
  Write-Host "`n--- bootstrap prompt ---`n$bootstrap`n------------------------"
  if ($replayMode) { Remove-Item -LiteralPath $replayPlanPath -Force -EA SilentlyContinue }
  if (-not $replayMode -or $ephemeralReplayReceipt) {
    Remove-Item -LiteralPath $receiptDir -Recurse -Force -EA SilentlyContinue
  }
  Log "DRYRUN done"
  exit 0
}

$outFile = Join-Path $logsDir "dream-$today-$stamp.out.txt"
$jobPidFile = Join-Path $logsDir "dream-$today-$stamp.job.pid"
Invoke-SkillHistory @('run-begin', '--run', $sessionId, '--date', $today, '--receipts', $receiptDir)
$runStart = Get-Date
Log "invoking copilot (session $sessionId) -> $outFile (timeout ${TimeoutMinutes}m)"
Log "copilot background-task drain timeout=${BackgroundTaskWaitSeconds}s"

# Run copilot in a background job (the call operator preserves multi-line arg fidelity), redirecting all
# streams to the out file. We wait with a timeout: a stuck subagent/MCP teardown must never hang the run.
# Success is judged only by fresh journal + final completion marker. Process exit 0 is not task completion.
$job = Start-Job -Name "dream-copilot" -ScriptBlock {
  param($cp, $a, $of, $taskWaitSeconds, $pidFile)
  Set-Content -LiteralPath $pidFile -Value $PID
  $env:COPILOT_TASK_WAIT_TIMEOUT_SECONDS = [string]$taskWaitSeconds
  & $cp @a *> $of
  $LASTEXITCODE
} -ArgumentList $copilot, $cliArgs, $outFile, $BackgroundTaskWaitSeconds, $jobPidFile

$deadline = $runStart.AddMinutes($TimeoutMinutes)
$graceDeadline = $null
$code = $null
$stuckTeardown = $false
$timedOut = $false
while ($true) {
  if ($job.State -ne 'Running') { $code = (Receive-Job $job); Log "copilot job state=$($job.State) exit=$code"; break }
  $completionFresh = (Test-Path $completionFile) -and ((Get-Item $completionFile).LastWriteTime -gt $runStart)
  if ($completionFresh -and -not $graceDeadline) {
    $graceDeadline = (Get-Date).AddSeconds(180)
    Log "completion marker detected; allowing up to 180s grace for teardown"
  }
  if ($graceDeadline -and (Get-Date) -gt $graceDeadline) { Log "grace elapsed; proceeding (teardown still running)"; $stuckTeardown=$true; break }
  if ((Get-Date) -gt $deadline) { Log "TIMEOUT after ${TimeoutMinutes}m; will verify by artifact"; $stuckTeardown=$true; $timedOut=$true; break }
  Start-Sleep -Seconds 10
}

# Stop the job. Only when teardown was actually stuck, kill the processes this run spawned: the job
# host's descendants, never other copilot/node processes (interactive sessions, editors) on the machine.
if ($stuckTeardown) {
  $jobPid = $null
  try { $jobPid = [int](Get-Content -LiteralPath $jobPidFile -EA Stop | Select-Object -First 1) } catch {}
  if ($jobPid) { Stop-DreamProcessTree -RootPid $jobPid -Since $runStart } else { Log "job pid unknown; not killing any processes" }
}
Stop-Job $job -EA SilentlyContinue; Remove-Job $job -Force -EA SilentlyContinue
Remove-Item -LiteralPath $jobPidFile -Force -EA SilentlyContinue

# ---- success determination: both task-completion artifacts must be fresh ----
$journalOk = (Test-Path $journalTarget) -and ((Get-Item $journalTarget).LastWriteTime -gt $runStart)
$completionOk = (Test-Path $completionFile) -and ((Get-Item $completionFile).LastWriteTime -gt $runStart)
$success = $journalOk -and $completionOk
Log "success=$success (exitcode=$code journalFresh=$journalOk completionFresh=$completionOk)"

# Runs even when the run failed: a failed run can still have edited skills.
$historyArgs = @('run-end', '--run', $sessionId, '--date', $today, '--receipts', $receiptDir,
                 '--status', $(if ($success) { 'ok' } else { 'failed' }))
if ($journalOk) { $historyArgs += @('--journal', $journalTarget) }
Invoke-SkillHistory $historyArgs

function Preserve-PendingApplyPlan($plan) {
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
  Remove-DreamLock
  exit 0
}
elseif ($success -and $ProposeOnly) {
  Log "propose-only success: watermark intentionally NOT advanced (a future applying run reconsiders this window)"
  Log "DREAM ok (propose-only)"
  Remove-DreamLock
  exit 0
}
elseif ($success) {
  # Stamp the watermark with the harvest cutoff, not the completion time: sessions updated while this
  # run (and any retries) were in flight must fall inside the next window.
  $watermarkUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  try {
    $latestHarvest = Get-Content -LiteralPath (Join-Path $harvest 'latest.json') -Raw | ConvertFrom-Json
    $harvestedAt = if ($latestHarvest.cutoff_utc) { $latestHarvest.cutoff_utc } else { $latestHarvest.generated_utc }
    if ($harvestedAt -is [datetime]) { $watermarkUtc = $harvestedAt.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
    elseif ($harvestedAt) { $watermarkUtc = [string]$harvestedAt }
  } catch {}
  $state = @{ last_run_utc = $watermarkUtc; last_model = $Model; last_session = $sessionId }
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [IO.File]::WriteAllText($stateFp, ($state | ConvertTo-Json), $utf8NoBom)
  Log "watermark advanced -> $($state.last_run_utc)"
  Log "DREAM ok"
  Remove-DreamLock
  exit 0
} else {
  Log "failure-reason: $(Get-FailureReason -OutFile $outFile -ExitCode $code -TimedOut $timedOut)"
  if ($replayMode) {
    Remove-Item -LiteralPath $replayPlanPath -Force -EA SilentlyContinue
    Log "replay failed; source plan remains at $replaySourcePlanPath"
  } else {
    $freshPlan = Get-FreshApplyPlan -Since $runStart
    $receiptCount = @(Get-ChildItem -LiteralPath $receiptDir -Filter '*.json' -EA SilentlyContinue).Count
    $elapsedMinutes = ((Get-Date).ToUniversalTime() - $firstStartUtc).TotalMinutes
    $budgetLeft = ($elapsedMinutes + ($retryBackoffSeconds / 60.0) + $TimeoutMinutes) -le $totalBudgetMinutes
    # Retrying is safe only when this attempt applied nothing; otherwise its plan is preserved for replay.
    if (-not $freshPlan -and $receiptCount -eq 0 -and $Attempt -lt $maxAttempts -and $budgetLeft) {
      Remove-Item -LiteralPath $receiptDir -Recurse -Force -EA SilentlyContinue
      Log "retrying in ${retryBackoffSeconds}s with a fresh session (attempt $($Attempt + 1)/$maxAttempts; nothing was applied)"
      Start-Sleep -Seconds $retryBackoffSeconds
      $retryArgs = @{
        Model = $Model; TimeoutMinutes = $TimeoutMinutes; BackgroundTaskWaitSeconds = $BackgroundTaskWaitSeconds
        SkipHarvest = $true; Attempt = $Attempt + 1; RunStartedUtc = $firstStartUtc.ToString('o')
      }
      if ($ProposeOnly) { $retryArgs.ProposeOnly = $true }
      & $PSCommandPath @retryArgs
      $retryExit = $LASTEXITCODE
      Remove-DreamLock
      exit $retryExit
    }
    Preserve-PendingApplyPlan $freshPlan
  }
  Log "DREAM failed (completion artifacts missing; watermark NOT advanced; next run reconsiders window)"
  Remove-DreamLock
  exit 1
}
