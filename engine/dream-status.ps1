#Requires -Version 5.1
<#
.SYNOPSIS
  Dream health check - is the nightly consolidation alive, when did it last run, and what needs my review?

.DESCRIPTION
  Run this any morning (or wire it into a scheduler/chat digest). Reports GREEN / YELLOW / RED with the reasons,
  covering the failure modes that would silently stop the Dream: missing trigger, failed/last run, stale
  journal (didn't run/hung), missing copilot/python, expired auth.

.EXAMPLE
  powershell -File ~\.copilot\dream\dream-status.ps1
  powershell -File ~\.copilot\dream\dream-status.ps1 -Json     # one-line JSON for a scheduler/chat digest
#>
[CmdletBinding()]
param(
  [int]$StaleWarnHours = 28,   # newest journal older than this -> YELLOW
  [int]$StaleFailHours = 50,   # ... older than this -> RED
  [switch]$Json
)
$ErrorActionPreference = 'SilentlyContinue'
$engine  = Join-Path $env:USERPROFILE '.copilot\dream'
$ledger  = Join-Path $engine 'ledger.py'
$journalDir = Join-Path $engine 'journal'
$rqDir   = Join-Path $engine 'review-queue'
$logsDir = Join-Path $engine 'logs'
$stateFp = Join-Path $engine 'state.json'
$now = Get-Date
$issues = New-Object System.Collections.Generic.List[string]
$warns  = New-Object System.Collections.Generic.List[string]

# --- prerequisites ---
$copilot = (Get-Command copilot -EA SilentlyContinue).Source
$python  = (Get-Command python  -EA SilentlyContinue).Source
if (-not $copilot) { $issues.Add("copilot not on PATH") }
if (-not $python)  { $issues.Add("python not on PATH") }

# --- trigger: Task Scheduler and/or an optional scheduler automation ---
$task = Get-ScheduledTask -TaskName 'CopilotDream' -EA SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName 'CopilotDream' -EA SilentlyContinue } else { $null }
$autoApp = $null
$autoFile = Join-Path $env:USERPROFILE '.copilot\m-automations\automations.json'
if (Test-Path $autoFile) {
  try {
    $autoApp = (Get-Content $autoFile -Raw | ConvertFrom-Json) | Where-Object { $_.name -match 'Dream' }
  } catch {}
}
$taskState = if ($task) { "$($task.State)" } else { $null }
$autoState = if ($autoApp) { if ($autoApp.enabled) { 'enabled' } else { 'disabled' } } else { $null }
if (-not $task -and -not $autoApp) { $issues.Add("no nightly trigger found (neither Task Scheduler 'CopilotDream' nor a scheduler 'Dream' automation)") }
if ($task -and $taskState -eq 'Disabled') { $warns.Add("Task Scheduler 'CopilotDream' is Disabled") }
if ($taskInfo -and $taskInfo.LastTaskResult -ne 0 -and $taskInfo.LastTaskResult -ne 267011) { $warns.Add("last Task Scheduler result = $($taskInfo.LastTaskResult) (non-zero)") }

# --- last run (ledger runs table) ---
$lastRun = $null
if ($python) {
  try {
    $statsJson = & python $ledger stats 2>$null | Out-String
    if ($statsJson) { $lastRun = ($statsJson | ConvertFrom-Json) }
  } catch {}
}
$lastRunTuple = $lastRun.last_run   # [run_id, finished, model, status]
$lastRunStatus = if ($lastRunTuple) { $lastRunTuple[3] } else { $null }
if ($lastRunStatus -and $lastRunStatus -notmatch 'success|ok') { $warns.Add("last ledger run status = '$lastRunStatus'") }

# --- newest journal (did it actually run?) ---
$newestJournal = Get-ChildItem $journalDir -Filter '*.md' -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -First 1
$journalAgeH = if ($newestJournal) { [math]::Round(($now - $newestJournal.LastWriteTime).TotalHours,1) } else { $null }
if (-not $newestJournal) { $issues.Add("no journal has ever been written") }
elseif ($journalAgeH -gt $StaleFailHours) { $issues.Add("newest journal is ${journalAgeH}h old (> ${StaleFailHours}h) - Dream likely stopped") }
elseif ($journalAgeH -gt $StaleWarnHours) { $warns.Add("newest journal is ${journalAgeH}h old (> ${StaleWarnHours}h) - did last night run?") }

# --- watermark (last successful applying run) ---
$watermark = $null
if (Test-Path $stateFp) { try { $watermark = (Get-Content $stateFp -Raw | ConvertFrom-Json).last_run_utc } catch {} }

# --- pending review queue ---
$pending = Get-ChildItem $rqDir -Filter '*.md' -EA SilentlyContinue
$pendingCount = ($pending | Measure-Object).Count
if ($pendingCount -gt 0) { $warns.Add("$pendingCount review-queue item(s) awaiting your approval") }

# --- last run log error scan ---
$today = Get-Date -Format 'yyyy-MM-dd'
$runLog = Join-Path $logsDir "run-$today.log"
$lastLogTail = if (Test-Path $runLog) { (Get-Content $runLog -Tail 3) -join ' | ' } else { '' }

# --- verdict ---
$verdict = if ($issues.Count -gt 0) { 'RED' } elseif ($warns.Count -gt 0) { 'YELLOW' } else { 'GREEN' }

if ($Json) {
  $o = [ordered]@{
    verdict=$verdict; journal_age_h=$journalAgeH; newest_journal=$(if($newestJournal){$newestJournal.Name});
    last_run_status=$lastRunStatus; last_run_model=$(if($lastRunTuple){$lastRunTuple[2]});
    ledger_items=$lastRun.items; watermark=$watermark;
    trigger_task=$taskState; trigger_scheduler=$autoState; next_run=$(if($taskInfo){"$($taskInfo.NextRunTime)"});
    pending_review=$pendingCount; issues=@($issues); warnings=@($warns)
  }
  ($o | ConvertTo-Json -Compress); return
}

# --- human report ---
$mark = @{ GREEN='[ OK ]'; YELLOW='[WARN]'; RED='[FAIL]' }[$verdict]
Write-Host ""
Write-Host "===== DREAM STATUS: $mark $verdict =====" -ForegroundColor $(@{GREEN='Green';YELLOW='Yellow';RED='Red'}[$verdict])
Write-Host ("  Newest journal : {0}  ({1}h ago)" -f $(if($newestJournal){$newestJournal.Name}else{'<none>'}), $journalAgeH)
Write-Host ("  Last run       : {0}  model={1}  finished={2}" -f $lastRunStatus, $(if($lastRunTuple){$lastRunTuple[2]}), $(if($lastRunTuple){$lastRunTuple[1]}))
Write-Host ("  Ledger items   : {0}" -f $lastRun.items)
Write-Host ("  Watermark      : {0}" -f $(if($watermark){$watermark}else{'<not advanced yet>'}))
Write-Host ("  Trigger        : TaskScheduler={0}  Scheduler={1}  next={2}" -f $(if($taskState){$taskState}else{'-'}), $(if($autoState){$autoState}else{'-'}), $(if($taskInfo){$taskInfo.NextRunTime}else{'-'}))
Write-Host ("  Review pending : {0}" -f $pendingCount)
if ($pendingCount -gt 0) { $pending | ForEach-Object { Write-Host ("      - {0}" -f $_.Name) } }
if ($issues.Count) { Write-Host "  ISSUES:" -ForegroundColor Red; $issues | ForEach-Object { Write-Host "      * $_" -ForegroundColor Red } }
if ($warns.Count)  { Write-Host "  WARNINGS:" -ForegroundColor Yellow; $warns | ForEach-Object { Write-Host "      * $_" -ForegroundColor Yellow } }
if ($lastLogTail) { Write-Host "  Today's run log tail:"; Write-Host "      $lastLogTail" }
Write-Host ""
Write-Host "  Review:  Get-Content $journalDir\$(if($newestJournal){$newestJournal.Name}else{'<date>.md'})"
Write-Host "  Approve: inspect $rqDir\*.md then apply/adjust"
Write-Host "  Discard: powershell -File $engine\dream-reject.ps1 -List   (then -Slug <name> | -All)"
Write-Host "  Run now: powershell -File $engine\run-dream.ps1 -Model claude-opus-4.8"
Write-Host ""
