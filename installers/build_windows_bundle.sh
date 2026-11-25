#!/usr/bin/env bash
# Build direct downloadable Windows 10/11 portable + one-click installer bundle:
#   - dist/Vimona-Windows-x86_64/
#   - dist/Vimona-Windows-x86_64.zip
#   - downloads/Vimona-Windows-x86_64.zip (optional via --sync-downloads)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${REPO_DIR}/dist"
WIN_DIR="${DIST_DIR}/Vimona-Windows-x86_64"
ZIP_OUT="${DIST_DIR}/Vimona-Windows-x86_64.zip"
SYNC_DOWNLOADS=0

for arg in "$@"; do
    case "$arg" in
        --sync-downloads) SYNC_DOWNLOADS=1 ;;
        -h|--help)
            echo "Usage: ./installers/build_windows_bundle.sh [--sync-downloads]"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 1
            ;;
    esac
done

log() { printf "\033[1;34m[windows-bundle]\033[0m %s\n" "$*"; }

cd "${REPO_DIR}"
rm -rf "${WIN_DIR}"
mkdir -p "${WIN_DIR}/assets" "${WIN_DIR}/installers"

log "Staging Vimona Windows runtime into ${WIN_DIR}..."
cp -a "${REPO_DIR}/vimona" "${WIN_DIR}/vimona"
rm -rf "${WIN_DIR}/vimona/__pycache__" "${WIN_DIR}/vimona"/*/__pycache__ 2>/dev/null || true
cp -a "${REPO_DIR}/run_vimona.py" "${WIN_DIR}/run_vimona.py"
cp -a "${REPO_DIR}/LICENSE" "${WIN_DIR}/LICENSE"
cp -a "${REPO_DIR}/assets/vimona.ico" "${WIN_DIR}/assets/vimona.ico"
cp -a "${REPO_DIR}/dev.vimona.Desktop.svg" "${WIN_DIR}/dev.vimona.Desktop.svg"
cp -a "${REPO_DIR}/installers/install-windows.ps1" "${WIN_DIR}/installers/install-windows.ps1"
cp -a "${REPO_DIR}/installers/build_windows_exe.ps1" "${WIN_DIR}/installers/build_windows_exe.ps1"
cp -a "${REPO_DIR}/installers/vimona-setup.iss" "${WIN_DIR}/installers/vimona-setup.iss"

# Double-click Windows one-click installer launcher (handles ExecutionPolicy automatically)
python3 - <<'PY' "${WIN_DIR}"
import sys
from pathlib import Path

win_dir = Path(sys.argv[1])

install_bat = """@echo off
setlocal
title Vimona Windows Setup
cd /d "%~dp0"
echo [Vimona] Installing Windows desktop app, GTK4 runtime, and shortcuts...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installers\\install-windows.ps1" %*
if errorlevel 1 (
    echo.
    echo [Vimona] Setup encountered an error. Check messages above.
    pause
    exit /b 1
)
echo.
echo [Vimona] Setup finished cleanly. Launching Vimona...
call "%~dp0Launch-Vimona.bat"
"""

launch_bat = """@echo off
setlocal
cd /d "%~dp0"
set "VIMONA_HOME=%LOCALAPPDATA%\\Vimona"
set "APP_ROOT=%~dp0"
if exist "%VIMONA_HOME%\\run_vimona.py" set "APP_ROOT=%VIMONA_HOME%\\"
set "PATH=C:\\msys64\\ucrt64\\bin;%LOCALAPPDATA%\\msys64\\ucrt64\\bin;%PATH%"
set "PY_BIN=C:\\msys64\\ucrt64\\bin\\python3.exe"
if not exist "%PY_BIN%" set "PY_BIN=%LOCALAPPDATA%\\msys64\\ucrt64\\bin\\python3.exe"
if not exist "%PY_BIN%" (
    echo [Vimona] First-run setup required to install GTK4 + Python runtime...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installers\\install-windows.ps1"
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
if not exist "%PY_BIN%" set "PY_BIN=python"
set "PYTHONPATH=%APP_ROOT%;%PYTHONPATH%"
start "" "%PY_BIN%" "%APP_ROOT%run_vimona.py" %*
"""

(win_dir / "Install-Vimona.bat").write_bytes(install_bat.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))
(win_dir / "Launch-Vimona.bat").write_bytes(launch_bat.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))
PY

rm -f "${ZIP_OUT}"
log "Creating ${ZIP_OUT}..."
(cd "${DIST_DIR}" && zip -r -q -9 "${ZIP_OUT}" "Vimona-Windows-x86_64")

if [[ "${SYNC_DOWNLOADS}" -eq 1 ]]; then
    mkdir -p "${REPO_DIR}/downloads"
    cp -f "${ZIP_OUT}" "${REPO_DIR}/downloads/Vimona-Windows-x86_64.zip"
    log "Synced ${REPO_DIR}/downloads/Vimona-Windows-x86_64.zip"
fi

ls -lh "${ZIP_OUT}"
