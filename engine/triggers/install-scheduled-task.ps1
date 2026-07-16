#Requires -Version 5.1
<#
.SYNOPSIS
  Register the nightly Dream as a Windows Scheduled Task (alternative to a desktop-scheduler automation).

.DESCRIPTION
  Runs run-dream.ps1 daily at 04:15 as the current user, "run only when logged on" so it shares your
  interactive session (any mapped network drives + Copilot auth are available). If your machine stays
  logged on/idle overnight, this fires reliably.

  Use -RunWhenLoggedOff to instead store credentials and run in session 0 (note: mapped network drives
  may be unavailable there; the core sessions->skills path still works because those live under your
  user profile on the system drive).

.EXAMPLE
  .\install-scheduled-task.ps1                 # 04:15 daily, logged-on
  .\install-scheduled-task.ps1 -Time 04:15 -Model gpt-5.6-sol
  .\install-scheduled-task.ps1 -Unregister     # remove the task
#>
[CmdletBinding()]
param(
  [string]$Time = '04:15',
  [ValidateSet('claude-opus-4.8','gpt-5.6-sol')][string]$Model = 'claude-opus-4.8',
  [switch]$RunWhenLoggedOff,
  [switch]$Unregister
)
$ErrorActionPreference = 'Stop'
$TaskName = 'CopilotDream'
$script   = Join-Path $env:USERPROFILE '.copilot\dream\run-dream.ps1'

if ($Unregister) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "Removed scheduled task '$TaskName'."
  return
}
if (-not (Test-Path $script)) { throw "run-dream.ps1 not found at $script" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -Model $Model"
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
  -MultipleInstances IgnoreNew

if ($RunWhenLoggedOff) {
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Password -RunLevel Limited
  Write-Host "NOTE: -RunWhenLoggedOff registers with LogonType Password; you'll be prompted to store your password. Mapped network drives may be unavailable in that context."
} else {
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force `
  -Description "Nightly Copilot 'Dream' - harvest sessions+git and refine personal skills ($Model @ 1M/max)." | Out-Null

Write-Host "Registered '$TaskName' daily at $Time (Model=$Model, LoggedOn=$(-not $RunWhenLoggedOff))."
Write-Host "Test now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Inspect:   Get-ScheduledTaskInfo -TaskName $TaskName"
