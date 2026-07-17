#Requires -Version 5.1
<#
.SYNOPSIS
  Approve a Dream review-queue proposal: record it as applied and remove the proposal file.

.DESCRIPTION
  A review-queue proposal is a *suggested* skill edit awaiting your approval. Approving it has two
  halves:
    1. The semantic half - the proposal's "After" edit is applied to the target skill. The nightly
       Dream applies high-confidence ones automatically; for a queued (medium/low) one, either let the
       Dream apply it, apply it yourself, or let the Scout "Dream review actions" agent apply it for you
       from natural language.
    2. The bookkeeping half - THIS script: mark the ledger item status = applied and delete the
       proposal file so it is not re-proposed.

  Run this AFTER the edit is in the skill. To DISCARD a proposal instead, use dream-reject.ps1.

.PARAMETER Slug
  Approve the proposal(s) whose filename contains this text. Case-insensitive substring match.
.PARAMETER File
  Approve one specific proposal by full path.
.PARAMETER All
  Approve every pending proposal.
.PARAMETER List
  Just list the pending proposals with their target skill (no changes).
.PARAMETER DryRun
  Show what would be approved without changing anything.

.EXAMPLE
  .\dream-approve.ps1 -List
.EXAMPLE
  .\dream-approve.ps1 -Slug aca-dataplane-cilium
.EXAMPLE
  .\dream-approve.ps1 -All
#>
[CmdletBinding()]
param(
  [string]$Slug,
  [string]$File,
  [switch]$All,
  [switch]$List,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$engine = Join-Path $env:USERPROFILE '.copilot\dream'
$config = Join-Path $engine 'config.json'
$ledger = Join-Path $engine 'ledger.py'
$rqDir  = Join-Path $engine 'review-queue'

if (-not (Test-Path $rqDir)) { Write-Host "No review-queue dir; nothing to approve."; exit 0 }

# resolve python
$py = $null
foreach ($cand in @('python','python3','py')) { $c = Get-Command $cand -EA SilentlyContinue; if ($c) { $py = $c.Source; break } }
if (-not $py) { Write-Host "Python not found on PATH."; exit 3 }

function Get-Field($path, $name) {
  # read a 'name: value' line from the proposal's YAML frontmatter
  $pat = '^\s*' + [regex]::Escape($name) + ':\s*(.+?)\s*$'
  $m = Select-String -Path $path -Pattern $pat -EA SilentlyContinue | Select-Object -First 1
  if ($m) { return $m.Matches[0].Groups[1].Value } else { return $null }
}

$proposals = Get-ChildItem $rqDir -Filter '*.md' -EA SilentlyContinue | Sort-Object Name

if ($List -or (-not $Slug -and -not $File -and -not $All)) {
  if (-not $proposals) { Write-Host "Review-queue is empty."; exit 0 }
  Write-Host "Pending review-queue proposals:`n"
  foreach ($f in $proposals) {
    $fp = Get-Field $f.FullName 'fingerprint'
    $tg = Get-Field $f.FullName 'target'
    $title = (Select-String -Path $f.FullName -Pattern '^#\s+(.*)$' -EA SilentlyContinue | Select-Object -First 1)
    $t = if ($title) { $title.Matches[0].Groups[1].Value } else { '(no title)' }
    Write-Host ("  {0}`n      target={1}  fp={2}`n      {3}" -f $f.Name, $(if($tg){$tg}else{'?'}), $(if($fp){$fp}else{'?'}), $t)
  }
  Write-Host "`nApprove with:  .\dream-approve.ps1 -Slug <name>   |   -All"
  Write-Host "(approval records items as APPLIED and deletes the files - make sure each edit is already in its target skill)"
  exit 0
}

# select targets
$targets = @()
if ($All)  { $targets = $proposals }
elseif ($File) { if (Test-Path $File) { $targets = @(Get-Item $File) } else { Write-Host "No such file: $File"; exit 1 } }
elseif ($Slug) { $targets = $proposals | Where-Object { $_.Name -like "*$Slug*" } }

if (-not $targets -or $targets.Count -eq 0) { Write-Host "No matching proposals for the given selector."; exit 1 }

Write-Host ("About to approve {0} proposal(s):" -f $targets.Count)
$targets | ForEach-Object {
  $tg = Get-Field $_.FullName 'target'
  Write-Host ("  - {0}   (target: {1})" -f $_.Name, $(if($tg){$tg}else{'?'}))
}
Write-Host "Reminder: this records them APPLIED and deletes the files. Ensure each edit is already in its target skill."
if ($DryRun) { Write-Host "`n-DryRun: no changes made."; exit 0 }

$approved = 0; $deleted = 0
foreach ($f in $targets) {
  $fp = Get-Field $f.FullName 'fingerprint'
  if ($fp) {
    & $py $ledger --config $config set-status --fingerprint $fp --status applied | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host ("  [ok] ledger {0} -> applied" -f $fp); $approved++ }
    else { Write-Host ("  [warn] could not set-status for {0} (deleting file anyway)" -f $fp) }
  } else {
    Write-Host ("  [warn] no fingerprint in {0}; deleting file only" -f $f.Name)
  }
  Remove-Item $f.FullName -Force
  $deleted++
}
Write-Host ("`nDone. approved(ledger)={0}  files_deleted={1}" -f $approved, $deleted)
Write-Host "Approved knowledge now lives in its target skill; these fingerprints won't be re-proposed."
