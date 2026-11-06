<#
  One-time setup for the supercon10 MicroPython project.

    1. creates .venv and installs requirements.txt (mpremote, esptool)
    2. reads board info from the ESP32
    3. pulls every file off the board into backups\<time>\ and device\
    4. optionally saves a full flash image with esptool
    5. starts a git repo with the untouched device code as the first commit

  Usage (from this folder):   .\setup.cmd            or   .\setup.ps1 -Port COM6
  Options:  -Port COMx   -SkipFlashBackup   -FlashBackup (no prompt)
  Close the MicroPython Tools plugin / any serial monitor before running.
#>
param(
    [string]$Port = "COM6",
    [switch]$SkipFlashBackup,
    [switch]$FlashBackup
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host ""; Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# 1. virtual environment ------------------------------------------------------
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Step "Creating virtual environment in .venv"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        Fail "Python 3 wasn't found. Install it from python.org (tick 'Add python.exe to PATH') and rerun."
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail "Couldn't create .venv" }
}

Step "Installing host tools (mpremote, esptool)"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }

# 2. talk to the board --------------------------------------------------------
$env:MPY_PORT = $Port
Step "Reading board info on $Port"
& $venvPy tools\dev.py info
if ($LASTEXITCODE -ne 0) { Fail "Couldn't talk to the board on $Port. Check the cable/port and that nothing else has it open." }

# 3. pull the code ------------------------------------------------------------
Step "Copying every file off the board"
& $venvPy tools\dev.py pull
if ($LASTEXITCODE -ne 0) { Write-Warning "Some files couldn't be read; see the list above." }

# 4. optional full flash image -------------------------------------------------
$doFlash = $FlashBackup
if (-not $FlashBackup -and -not $SkipFlashBackup) {
    $ans = Read-Host "Also save a full flash image (MicroPython firmware + filesystem) with esptool? Takes a few minutes. [Y/n]"
    $doFlash = ($ans -eq "" -or $ans -match '^[Yy]')
}
if ($doFlash) {
    Step "Saving full flash image"
    & $venvPy tools\dev.py flash-backup
    if ($LASTEXITCODE -ne 0) { Write-Warning "Flash backup didn't complete. You can rerun it later: .venv\Scripts\python tools\dev.py flash-backup" }
}

# 5. git ----------------------------------------------------------------------
if ((Get-Command git -ErrorAction SilentlyContinue) -and -not (Test-Path .git)) {
    Step "Starting a git repo with the original device code as the first commit"
    git init --quiet
    git add -A
    git commit --quiet -m "Original code pulled from the badge on $Port"
    if ($LASTEXITCODE -ne 0) { Write-Warning "git commit failed (set user.name / user.email?). The repo is initialised and files are staged." }
}

Step "Done"
Write-Host "Open this folder in PyCharm. Device code is in device\, run configs are under 'Device' in the Run menu."
