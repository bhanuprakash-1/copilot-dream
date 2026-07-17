#Requires -Version 5.1
<#
.SYNOPSIS
  Discard (reject) Dream review-queue proposals so they are never incorporated - now or on any
  future night.

.DESCRIPTION
  A review-queue proposal is a *suggested* skill edit awaiting your approval. To discard one
  properly it is not enough to delete the .md file: because the nightly consolidation re-classifies
  the raw sessions/commits each run, a deleted proposal can resurface while its source is still in
  the harvest window. This script records a permanent veto:

    1. Reads the proposal's `fingerprint:` from its YAML frontmatter.
    2. Marks that ledger item `status = rejected` (reduce.py force-drops rejected fingerprints, so
       the claim is never proposed, applied, or promoted again).
    3. Deletes the proposal file.

  Approve a proposal instead by leaving it in place (a normal run applies high-confidence ones) or
  applying it by hand - this script is only for the ones you do NOT want.

.PARAMETER Slug
  Reject the proposal(s) whose filename contains this text (e.g. 'spa-static-js-cache' or a date
  like '2026-07-17'). Case-insensitive substring match.

.PARAMETER File
  Reject one specific proposal by full path.

.PARAMETER All
  Reject every pending proposal in the review-queue.

.PARAMETER List
  Just list the pending proposals (no changes).

.PARAMETER DryRun
  Show what would be rejected without changing anything.

.EXAMPLE
  .\dream-reject.ps1 -List
.EXAMPLE
  .\dream-reject.ps1 -Slug spa-static-js-cache
.EXAMPLE
  .\dream-reject.ps1 -Slug 2026-07-17          # reject all of that day's proposals
.EXAMPLE
  .\dream-reject.ps1 -All
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

if (-not (Test-Path $rqDir)) { Write-Host "No review-queue dir; nothing to reject."; exit 0 }

# resolve python
$py = $null
foreach ($cand in @('python','python3','py')) { $c = Get-Command $cand -EA SilentlyContinue; if ($c) { $py = $c.Source; break } }
if (-not $py) { Write-Host "Python not found on PATH."; exit 3 }

function Get-Fingerprint($path) {
  # fingerprint lives in the YAML frontmatter: 'fingerprint: <hex>'
  $m = Select-String -Path $path -Pattern '^\s*fingerprint:\s*([0-9a-fA-F]+)\s*$' -EA SilentlyContinue | Select-Object -First 1
  if ($m) { return $m.Matches[0].Groups[1].Value } else { return $null }
}

$proposals = Get-ChildItem $rqDir -Filter '*.md' -EA SilentlyContinue | Sort-Object Name

if ($List -or (-not $Slug -and -not $File -and -not $All)) {
  if (-not $proposals) { Write-Host "Review-queue is empty."; exit 0 }
  Write-Host "Pending review-queue proposals:`n"
  foreach ($f in $proposals) {
    $fp = Get-Fingerprint $f.FullName
    $title = (Select-String -Path $f.FullName -Pattern '^#\s+(.*)$' -EA SilentlyContinue | Select-Object -First 1)
    $t = if ($title) { $title.Matches[0].Groups[1].Value } else { '(no title)' }
    $fpShown = if ($fp) { $fp } else { '?' }
    Write-Host ("  {0}`n      fp={1}  {2}" -f $f.Name, $fpShown, $t)
  }
  Write-Host "`nReject with:  .\dream-reject.ps1 -Slug <name>   |   -All"
  if (-not $List) { Write-Host "(showing list because no target was given)" }
  exit 0
}

# select targets
$targets = @()
if ($All)  { $targets = $proposals }
elseif ($File) { if (Test-Path $File) { $targets = @(Get-Item $File) } else { Write-Host "No such file: $File"; exit 1 } }
elseif ($Slug) { $targets = $proposals | Where-Object { $_.Name -like "*$Slug*" } }

if (-not $targets -or $targets.Count -eq 0) { Write-Host "No matching proposals for the given selector."; exit 1 }

Write-Host ("About to reject {0} proposal(s):" -f $targets.Count)
$targets | ForEach-Object { Write-Host ("  - {0}" -f $_.Name) }
if ($DryRun) { Write-Host "`n-DryRun: no changes made."; exit 0 }

$rejected = 0; $deleted = 0
foreach ($f in $targets) {
  $fp = Get-Fingerprint $f.FullName
  if ($fp) {
    & $py $ledger --config $config set-status --fingerprint $fp --status rejected | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host ("  [veto] ledger {0} -> rejected" -f $fp); $rejected++ }
    else { Write-Host ("  [warn] could not set-status for {0} (deleting file anyway)" -f $fp) }
  } else {
    Write-Host ("  [warn] no fingerprint in {0}; deleting file, but it may resurface" -f $f.Name)
  }
  Remove-Item $f.FullName -Force
  $deleted++
}
Write-Host ("`nDone. rejected(ledger)={0}  files_deleted={1}" -f $rejected, $deleted)
Write-Host "These claims will now be force-dropped on every future run (no re-proposal)."
