<#
.SYNOPSIS
    Vimona Windows 10/11 Native Installer (MSYS2 UCRT64 GTK4 + Libadwaita + Shortcuts)
.DESCRIPTION
    Bootstraps MSYS2 + GTK4 + Libadwaita + Python runtime dependencies on Windows,
    installs Vimona into %LOCALAPPDATA%\Vimona, and creates Start Menu + Desktop
    shortcuts with the official Vimona icon.
#>
[CmdletBinding()]
param(
    [switch]$SkipDeps,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$InstallDir = Join-Path $env:LOCALAPPDATA "Vimona"
$StartMenuShortcut = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\Vimona.lnk"
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Vimona.lnk"

function Write-Info([string]$msg) { Write-Host "[vimona] $msg" -ForegroundColor Cyan }
function Write-Warn([string]$msg) { Write-Host "[warn]   $msg" -ForegroundColor Yellow }

if ($Uninstall) {
    Write-Info "Removing Vimona from $InstallDir..."
    if (Test-Path $StartMenuShortcut) { Remove-Item $StartMenuShortcut -Force }
    if (Test-Path $DesktopShortcut) { Remove-Item $DesktopShortcut -Force }
    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
    Write-Info "Vimona uninstalled."
    exit 0
}

function Find-MsysBash {
    $candidates = @(
        "C:\msys64\usr\bin\bash.exe",
        (Join-Path $env:LOCALAPPDATA "msys64\usr\bin\bash.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

$msysBash = Find-MsysBash
if (-not $SkipDeps) {
    if (-not $msysBash) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Info "Installing MSYS2 via winget (required for native GTK4 & Libadwaita on Windows)..."
            winget install --id MSYS2.MSYS2 -e --silent --accept-package-agreements --accept-source-agreements
            $msysBash = Find-MsysBash
        }
    }
    if (-not $msysBash) {
        throw "MSYS2 was not found at C:\msys64. Install MSYS2 (https://www.msys2.org/) or run with pre-installed dist\Vimona-Setup.exe."
    }

    Write-Info "Installing GTK4, Libadwaita, PyGObject, OpenCV, Pillow, and FFmpeg via MSYS2 UCRT64..."
    & $msysBash -lc "pacman -Sy --needed --noconfirm mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-python-numpy mingw-w64-ucrt-x86_64-python-pillow mingw-w64-ucrt-x86_64-python-opencv mingw-w64-ucrt-x86_64-ffmpeg mingw-w64-ucrt-x86_64-python-pip"
}

Write-Info "Installing Vimona files into $InstallDir..."
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-Item -Path (Join-Path $RepoDir "vimona") -Destination $InstallDir -Recurse -Force
Copy-Item -Path (Join-Path $RepoDir "run_vimona.py") -Destination $InstallDir -Force
New-Item -ItemType Directory -Path (Join-Path $InstallDir "assets") -Force | Out-Null
Copy-Item -Path (Join-Path $RepoDir "assets\vimona.ico") -Destination (Join-Path $InstallDir "assets\vimona.ico") -Force

$LauncherCmd = Join-Path $InstallDir "Vimona.cmd"
$UcrtPy = "C:\msys64\ucrt64\bin\python3.exe"
if (-not (Test-Path $UcrtPy)) { $UcrtPy = "python" }
$LauncherBody = @"
@echo off
setlocal
set "PATH=C:\msys64\ucrt64\bin;%PATH%"
set "PYTHONPATH=$InstallDir"
cd /d "$InstallDir"
start "" "$UcrtPy" "$InstallDir\run_vimona.py" %*
"@
Set-Content -Path $LauncherCmd -Value $LauncherBody -Encoding ASCII

$WshShell = New-Object -ComObject WScript.Shell
foreach ($lnkPath in @($StartMenuShortcut, $DesktopShortcut)) {
    $sc = $WshShell.CreateShortcut($lnkPath)
    $sc.TargetPath = $UcrtPy
    $sc.Arguments = "`"$InstallDir\run_vimona.py`""
    $sc.WorkingDirectory = $InstallDir
    $sc.IconLocation = (Join-Path $InstallDir "assets\vimona.ico")
    $sc.Description = "Vimona — Local Air-Gapped AI Workstation"
    $sc.Save()
}

Write-Info "Installation complete! Launch Vimona from the Start Menu or Desktop shortcut."
