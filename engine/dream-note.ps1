#Requires -Version 5.1
<#
.SYNOPSIS
  Quick note capture for the nightly Dream - appends a timestamped bullet to inbox.md.
.EXAMPLE
  dream-note track the checkout-refactor work as an active thread
  dream-note "stop tracking the search-indexing spike; it shipped"
#>
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Note)
$inbox = Join-Path $env:USERPROFILE '.copilot\dream\inbox.md'
$text = ($Note -join ' ').Trim().Trim('"')
if (-not $text) { Write-Host "usage: dream-note <your note text>"; exit 1 }
if (-not (Test-Path $inbox)) { "# Dream Inbox`n`n<!-- Add notes below this line -->`n" | Set-Content $inbox -Encoding utf8 }
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
Add-Content -Path $inbox -Value ("- ({0}) {1}" -f $stamp, $text) -Encoding utf8
Write-Host "noted -> $inbox"
Write-Host ("  - ({0}) {1}" -f $stamp, $text)
