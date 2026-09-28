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
$modelSettings = $null
try {
  $modelSettings = & (Join-Path $engine 'resolve-model-policy.ps1') -Config (Join-Path $engine 'config.json')
} catch {
  $issues.Add("invalid Dream model policy: $($_.Exception.Message)")
}

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

# --- outcome of the most recent real run (newest run log; the last attempt decides) ---
$lastOutcome = $null
$lastFailureReason = $null
$newestRunLog = Get-ChildItem $logsDir -Filter 'run-*.log' -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -First 1
if ($newestRunLog) {
  $runLines = @(Get-Content -LiteralPath $newestRunLog.FullName -EA SilentlyContinue)
  $startIdx = -1
  for ($i = $runLines.Count - 1; $i -ge 0; $i--) {
    if ($runLines[$i] -match 'DREAM start' -and $runLines[$i] -notmatch 'dryrun=True') { $startIdx = $i; break }
  }
  if ($startIdx -ge 0) {
    $endIdx = $runLines.Count - 1
    for ($i = $startIdx + 1; $i -lt $runLines.Count; $i++) { if ($runLines[$i] -match 'DREAM start') { $endIdx = $i - 1; break } }
    $block = @($runLines[$startIdx..$endIdx])
    if ($block -match 'DREAM ok') { $lastOutcome = 'ok' }
    elseif ($block -match 'DREAM failed') {
      $lastOutcome = 'failed'
      $reasonLine = @($block -match 'failure-reason:') | Select-Object -Last 1
      $lastFailureReason = if ($reasonLine) { ($reasonLine -split 'failure-reason:\s*', 2)[1] } else { "see $($newestRunLog.Name)" }
    }
    elseif ($block -match '\sFATAL\s') {
      $lastOutcome = 'failed'
      $lastFailureReason = (@($block -match '\sFATAL\s') | Select-Object -Last 1) -replace '^\S+\s+', ''
    }
    elseif (($now - $newestRunLog.LastWriteTime).TotalHours -gt 3.5) { $lastOutcome = 'no-outcome' }
    else { $lastOutcome = 'running' }
  }
}
if ($lastOutcome -eq 'failed') { $issues.Add("last run failed: $lastFailureReason") }
if ($lastOutcome -eq 'no-outcome') { $warns.Add("last run in $($newestRunLog.Name) recorded no outcome (killed or crashed?)") }
$lastLogTail = if ($newestRunLog) { (Get-Content -LiteralPath $newestRunLog.FullName -Tail 3) -join ' | ' } else { '' }

# --- skill changes made by the newest finished run (skillaudit.py change records) ---
$lastChanges = $null
$auditedRuns = @{}
foreach ($cf in @(Get-ChildItem (Join-Path $engine 'changes') -Filter '*.json' -EA SilentlyContinue)) {
  try { $rec = Get-Content -LiteralPath $cf.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
  if ($rec.post) { $auditedRuns[[string]$rec.run] = $true }
  if ($rec.post -and (-not $lastChanges -or [string]$rec.started_utc -gt [string]$lastChanges.started_utc)) { $lastChanges = $rec }
}
$historyEnabled = $true
try { $historyCfg = (Get-Content (Join-Path $engine 'config.json') -Raw | ConvertFrom-Json).history; if ($historyCfg -and $historyCfg.enabled -eq $false) { $historyEnabled = $false } } catch {}
if ($historyEnabled -and $lastRunTuple -and -not $auditedRuns.ContainsKey([string]$lastRunTuple[0])) {
  $issues.Add(("no skill-change record for the last run {0}: its skill edits were not snapshotted (see the 'history:' lines in the run log)" -f ([string]$lastRunTuple[0]).Substring(0, [Math]::Min(8, ([string]$lastRunTuple[0]).Length))))
}
$changesSummary = $null
if ($lastChanges) {
  $firstFew = { param($list) @(@($list) | Select-Object -First 12) }
  $changesSummary = [ordered]@{
    run = $lastChanges.run8; date = $lastChanges.date; status = $lastChanges.status; report = $lastChanges.report
    skills = @(@($lastChanges.skills) | ForEach-Object {
      [ordered]@{ name = $_.name; watched = [bool]$_.watched; chars_before = $_.chars_before; chars_after = $_.chars_after
                  lines_added = $_.added; lines_removed = $_.removed; lines_rewritten = $_.rewritten; lines_rewritten_into = $_.rewritten_into
                  sections_rewritten = & $firstFew $_.sections_rewritten; sections_removed_count = @($_.sections_removed).Count
                  sections_removed = & $firstFew $_.sections_removed; sections_added = & $firstFew $_.sections_added
                  applier_summary = $_.applier_summary }
    })
    attribution = $lastChanges.attribution; changed_by_others = @($lastChanges.others | Where-Object { $_ }).Count
    revert_command = "python ~/.copilot/dream/skillaudit.py revert --run $($lastChanges.run8) --skill <name> [--check]"
  }
  $touchedWatched = @(@($lastChanges.skills) | Where-Object { $_.watched -and ($_.removed -gt 0 -or $_.rewritten -gt 0 -or @($_.sections_removed).Count -gt 0) })
  if ($touchedWatched.Count) {
    $warns.Add(("watched skill text rewritten or removed by run {0} ({1}): {2} - review {3}" -f $lastChanges.run8, $lastChanges.date,
      (($touchedWatched | ForEach-Object { "{0} (~{1} rewritten, -{2} removed)" -f $_.name, $_.rewritten, $_.removed }) -join ', '), $lastChanges.report))
  }
}

# --- verdict ---
$verdict = if ($issues.Count -gt 0) { 'RED' } elseif ($warns.Count -gt 0) { 'YELLOW' } else { 'GREEN' }

if ($Json) {
  $o = [ordered]@{
    verdict=$verdict; journal_age_h=$journalAgeH; newest_journal=$(if($newestJournal){$newestJournal.Name});
    last_run_status=$lastRunStatus; last_run_model=$(if($lastRunTuple){$lastRunTuple[2]});
    last_outcome=$lastOutcome; last_failure_reason=$lastFailureReason;
    configured_model=$modelSettings.Model; configured_context=$modelSettings.Context; configured_effort=$modelSettings.Effort;
    ledger_items=$lastRun.items; watermark=$watermark;
    trigger_task=$taskState; trigger_scheduler=$autoState; next_run=$(if($taskInfo){"$($taskInfo.NextRunTime)"});
    pending_review=$pendingCount; last_changes=$changesSummary; issues=@($issues); warnings=@($warns)
  }
  ($o | ConvertTo-Json -Compress -Depth 6); return
}

# --- human report ---
$mark = @{ GREEN='[ OK ]'; YELLOW='[WARN]'; RED='[FAIL]' }[$verdict]
Write-Host ""
Write-Host "===== DREAM STATUS: $mark $verdict =====" -ForegroundColor $(@{GREEN='Green';YELLOW='Yellow';RED='Red'}[$verdict])
Write-Host ("  Newest journal : {0}  ({1}h ago)" -f $(if($newestJournal){$newestJournal.Name}else{'<none>'}), $journalAgeH)
Write-Host ("  Last run       : {0}  model={1}  finished={2}" -f $lastRunStatus, $(if($lastRunTuple){$lastRunTuple[2]}), $(if($lastRunTuple){$lastRunTuple[1]}))
if ($lastOutcome) { Write-Host ("  Last outcome   : {0}{1}" -f $lastOutcome, $(if($lastFailureReason){" - $lastFailureReason"}else{''})) }
Write-Host ("  Ledger items   : {0}" -f $lastRun.items)
Write-Host ("  Watermark      : {0}" -f $(if($watermark){$watermark}else{'<not advanced yet>'}))
Write-Host ("  Trigger        : TaskScheduler={0}  Scheduler={1}  next={2}" -f $(if($taskState){$taskState}else{'-'}), $(if($autoState){$autoState}else{'-'}), $(if($taskInfo){$taskInfo.NextRunTime}else{'-'}))
Write-Host ("  Review pending : {0}" -f $pendingCount)
if ($pendingCount -gt 0) { $pending | ForEach-Object { Write-Host ("      - {0}" -f $_.Name) } }
if ($lastChanges) {
  $skillLines = @(@($lastChanges.skills) | ForEach-Object { "{0}{1} +{2}/-{3}/~{4}->{5}" -f $_.name, $(if ($_.watched) { '*' } else { '' }), $_.added, $_.removed, $_.rewritten, $_.rewritten_into })
  Write-Host ("  Skill changes  : run {0} ({1}): {2}" -f $lastChanges.run8, $lastChanges.date, $(if ($skillLines.Count) { $skillLines -join ', ' } else { 'none' }))
  if ($skillLines.Count) { Write-Host ("                   report: {0}   (* watched; +added/-removed/~rewritten->into lines)" -f $lastChanges.report) }
}
if ($issues.Count) { Write-Host "  ISSUES:" -ForegroundColor Red; $issues | ForEach-Object { Write-Host "      * $_" -ForegroundColor Red } }
if ($warns.Count)  { Write-Host "  WARNINGS:" -ForegroundColor Yellow; $warns | ForEach-Object { Write-Host "      * $_" -ForegroundColor Yellow } }
if ($lastLogTail) { Write-Host "  Newest run log tail:"; Write-Host "      $lastLogTail" }
Write-Host ""
Write-Host "  Review:  Get-Content $journalDir\$(if($newestJournal){$newestJournal.Name}else{'<date>.md'})"
Write-Host "  Approve: powershell -File $engine\dream-approve.ps1 -List   (after the edit is in the skill; then -Slug <name>)"
Write-Host "  Discard: powershell -File $engine\dream-reject.ps1 -List    (then -Slug <name> | -All)"
Write-Host "  Undo a skill edit: python $engine\skillaudit.py revert --run <run8|date|last> --skill <name> --check  (then without --check)"
Write-Host "  Or just reply in the Scout 'Dream digest + actions' thread in plain English (approve / reject / track)."
Write-Host "  Policy:  $($modelSettings.Model) / $($modelSettings.Context) / $($modelSettings.Effort) (all agents)"
Write-Host "  Run now: powershell -File $engine\run-dream.ps1"
Write-Host ""
