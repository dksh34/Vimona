<#
.SYNOPSIS
    Build standalone Vimona.exe and optional Vimona-Setup.exe Windows installer.
.DESCRIPTION
    Uses MSYS2 UCRT64 Python + PyInstaller inside a Windows environment to package
    Vimona (including GTK4/Libadwaita typelibs, SafeBlur ML weights, and assets\vimona.ico)
    into dist\Vimona\Vimona.exe, and compiles dist\Vimona-Setup.exe if Inno Setup is present.
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "C:\msys64\ucrt64\bin\python3.exe",
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path
Set-Location $RepoDir

if (-not (Test-Path $PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $PythonExe = $cmd.Source }
    else { throw "Python executable not found at $PythonExe. Run installers\install-windows.ps1 first or pass -PythonExe." }
}

Write-Host "[build] Ensuring PyInstaller is available..." -ForegroundColor Cyan
& $PythonExe -m pip install --upgrade pyinstaller | Out-Null

$modeFlag = if ($OneFile) { "--onefile" } else { "--onedir" }

$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    $modeFlag,
    "--name", "Vimona",
    "--icon", "assets\vimona.ico",
    "--add-data", "vimona\skills;vimona\skills",
    "--add-data", "vimona\safety_ml_weights.npz;vimona",
    "--add-data", "dev.vimona.Desktop.svg;.",
    "--add-data", "assets\vimona.ico;assets",
    "--collect-all", "gi",
    "--hidden-import", "numpy",
    "--hidden-import", "cv2",
    "--hidden-import", "PIL",
    "run_vimona.py"
)

Write-Host "[build] Running PyInstaller ($modeFlag)..." -ForegroundColor Cyan
& $PythonExe @pyiArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exeOut = if ($OneFile) { "dist\Vimona.exe" } else { "dist\Vimona\Vimona.exe" }
Write-Host "[build] Built executable: $exeOut" -ForegroundColor Green

$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc -and (Test-Path "C:\Program Files (x86)\Inno Setup 6\ISCC.exe")) {
    $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
}

if ($iscc -and -not $OneFile -and (Test-Path "installers\vimona-setup.iss")) {
    Write-Host "[build] Compiling Windows Setup Wizard (Vimona-Setup.exe) via Inno Setup..." -ForegroundColor Cyan
    & $iscc "installers\vimona-setup.iss"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[build] Built Windows installer: dist\Vimona-Setup.exe" -ForegroundColor Green
    }
}
