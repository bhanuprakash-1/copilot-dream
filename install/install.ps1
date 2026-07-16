#requires -Version 5.1
<#
.SYNOPSIS
    Bootstrap installer for Copilot Dream (Windows / PowerShell).

.DESCRIPTION
    Copies the Dream engine and the two Dream skills into your Copilot home
    (%USERPROFILE%\.copilot\), seeds config.json and inbox.md from the shipped
    templates, and initializes the SQLite ledger. The script is idempotent and
    never deletes or overwrites your data: config.json, ledger.db, state.json,
    inbox.md, journal/, review-queue/, harvest/ and logs/ are always preserved.

.PARAMETER Force
    Overwrite the dream / dream-active-work skill folders if they already exist.
    Your runtime state and config.json are still preserved.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install\install.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install\install.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step($msg)  { Write-Host ('==> ' + $msg) -ForegroundColor Cyan }
function Write-Info($msg)  { Write-Host ('    ' + $msg) }
function Write-Ok($msg)    { Write-Host ('    [ok]   ' + $msg) -ForegroundColor Green }
function Write-Skip2($msg) { Write-Host ('    [skip] ' + $msg) -ForegroundColor DarkGray }
function Write-Warn2($msg) { Write-Host ('    [warn] ' + $msg) -ForegroundColor Yellow }

# --- 1. Resolve repo root (this script lives in <repo>\install\) --------------
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$RepoRoot = Split-Path -Parent $ScriptDir

$EngineSrc = Join-Path $RepoRoot 'engine'
$SkillsSrc = Join-Path $RepoRoot 'skills'

$CopilotHome = Join-Path $env:USERPROFILE '.copilot'
$DreamDst    = Join-Path $CopilotHome 'dream'
$SkillsDst   = Join-Path $CopilotHome 'skills'

Write-Step 'Copilot Dream installer'
Write-Info ('repo root  : ' + $RepoRoot)
Write-Info ('dream dir  : ' + $DreamDst)
Write-Info ('skills dir : ' + $SkillsDst)
if ($Force) { Write-Info 'mode       : -Force (existing skills will be overwritten)' }

# Data we must never overwrite or delete.
$ProtectedFiles = @('config.json','ledger.db','ledger.db-wal','ledger.db-shm','ledger.db-journal','state.json','inbox.md')
$ProtectedDirs  = @('journal','review-queue','harvest','logs')

# --- 2. Copy engine\* -> ~\.copilot\dream\ -----------------------------------
Write-Step 'Installing engine'
New-Item -ItemType Directory -Force -Path $DreamDst | Out-Null
foreach ($d in $ProtectedDirs) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DreamDst $d) | Out-Null
}

if (-not (Test-Path $EngineSrc)) {
    Write-Warn2 ('engine source not found: ' + $EngineSrc + ' (nothing to copy)')
} else {
    $engineFiles = Get-ChildItem -Path $EngineSrc -Recurse -File -Force
    foreach ($f in $engineFiles) {
        $rel = $f.FullName.Substring($EngineSrc.Length).TrimStart('\', '/')
        $top = ($rel -split '[\\/]')[0]
        if ($ProtectedDirs -contains $top) { continue }   # never ship runtime state
        $dst  = Join-Path $DreamDst $rel
        $name = Split-Path $dst -Leaf
        if (($ProtectedFiles -contains $name) -and (Test-Path $dst)) {
            Write-Skip2 ('kept your existing ' + $rel)
            continue
        }
        $dstDir = Split-Path -Parent $dst
        New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
        Copy-Item -Path $f.FullName -Destination $dst -Force
        Write-Ok ('engine\' + $rel)
    }
}

# --- 3. Copy skills -> ~\.copilot\skills\ ------------------------------------
Write-Step 'Installing skills'
New-Item -ItemType Directory -Force -Path $SkillsDst | Out-Null
foreach ($skill in @('dream', 'dream-active-work')) {
    $src = Join-Path $SkillsSrc $skill
    $dst = Join-Path $SkillsDst $skill
    if (-not (Test-Path $src)) { Write-Warn2 ('skill source missing: ' + $src); continue }
    if ((Test-Path $dst) -and (-not $Force)) {
        Write-Skip2 ($skill + ' already installed (use -Force to overwrite)')
        continue
    }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force
    Write-Ok ('skill ' + $skill)
}

# --- 4. Seed config.json from the template if absent -------------------------
Write-Step 'Seeding config'
$cfg        = Join-Path $DreamDst 'config.json'
$cfgExample = Join-Path $DreamDst 'config.example.json'
if (Test-Path $cfg) {
    Write-Skip2 'config.json already exists (kept)'
} elseif (Test-Path $cfgExample) {
    Copy-Item -Path $cfgExample -Destination $cfg -Force
    Write-Ok 'created config.json from config.example.json'
    Write-Warn2 'edit config.json: set your git email(s) and repo roots before the first run'
} else {
    Write-Warn2 'config.example.json not found in engine; create config.json manually (see examples\example-config.json)'
}

# --- 5. Initialize the ledger -------------------------------------------------
Write-Step 'Initializing ledger'
$ledger = Join-Path $DreamDst 'ledger.py'
if (Test-Path $ledger) {
    $py = $null
    foreach ($cand in @('python', 'python3', 'py')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) { $py = $cmd.Source; break }
    }
    if (-not $py) {
        Write-Warn2 'Python not found on PATH; run this yourself once Python is installed:'
        Write-Info  ('  python "' + $ledger + '" init')
    } else {
        & $py $ledger init
        if ($LASTEXITCODE -eq 0) { Write-Ok 'ledger initialized' }
        else { Write-Warn2 ('ledger init exited with code ' + $LASTEXITCODE) }
    }
} else {
    Write-Warn2 'ledger.py not found in engine; skipping ledger init'
}

# --- 6. Seed inbox.md from the template if absent ----------------------------
Write-Step 'Seeding inbox'
$inbox        = Join-Path $DreamDst 'inbox.md'
$inboxExample = Join-Path $DreamDst 'inbox.example.md'
if (Test-Path $inbox) {
    Write-Skip2 'inbox.md already exists (kept)'
} elseif (Test-Path $inboxExample) {
    Copy-Item -Path $inboxExample -Destination $inbox -Force
    Write-Ok 'created inbox.md from inbox.example.md'
} else {
    Write-Warn2 'inbox.example.md not found in engine; the Dream will create inbox.md on the first note'
}

# --- 7. Next steps ------------------------------------------------------------
Write-Step 'Done. Next steps:'
Write-Host ''
Write-Host '  1. Edit your config:'
Write-Host '       ~/.copilot/dream/config.json   (start from examples/example-config.json)'
Write-Host ''
Write-Host '  2. Dry run (no AI spend) to preview what it would harvest:'
Write-Host '       powershell -NoProfile -ExecutionPolicy Bypass -File ~/.copilot/dream/run-dream.ps1 -DryRun'
Write-Host ''
Write-Host '  3. Schedule the nightly run (Windows Task Scheduler):'
Write-Host '       powershell -NoProfile -ExecutionPolicy Bypass -File ~/.copilot/dream/triggers/install-scheduled-task.ps1'
Write-Host ''
Write-Host '  4. Check health any time:'
Write-Host '       powershell -NoProfile -ExecutionPolicy Bypass -File ~/.copilot/dream/dream-status.ps1'
Write-Host ''
