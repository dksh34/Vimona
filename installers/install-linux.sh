#!/usr/bin/env bash
# Vimona Universal Linux Installer (Fedora / Ubuntu / Debian / Arch)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/vimona/app"
BIN_DIR="${HOME}/.local/bin"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

SKIP_DEPS=0
UNINSTALL=0

for arg in "$@"; do
    case "$arg" in
        --skip-deps) SKIP_DEPS=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            cat <<'EOF'
Usage: ./installers/install-linux.sh [--skip-deps] [--uninstall]

Options:
  --skip-deps   Skip OS package installation and only wire desktop launcher/symlink
  --uninstall   Remove Vimona application files, launcher, and desktop entry
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 1
            ;;
    esac
done

log()  { printf "\033[1;34m[vimona]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m   %s\n" "$*"; }
err()  { printf "\033[1;31m[error]\033[0m  %s\n" "$*" >&2; }

if [[ "$UNINSTALL" -eq 1 ]]; then
    log "Removing Vimona installation..."
    rm -rf "$APP_HOME"
    rm -f "${BIN_DIR}/vimona"
    rm -f "${APPS_DIR}/dev.vimona.Desktop.desktop"
    rm -f "${ICONS_DIR}/dev.vimona.Desktop.svg"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPS_DIR" || true
    fi
    log "Uninstalled Vimona cleanly."
    exit 0
fi

install_system_deps() {
    if [[ "$SKIP_DEPS" -eq 1 ]]; then
        log "Skipping system package installation (--skip-deps)."
        return
    fi

    local distro=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release
        distro="${ID:-}"
    fi

    local sudo_cmd=""
    if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
        if command -v sudo >/dev/null 2>&1; then
            sudo_cmd="sudo"
        else
            warn "Not running as root and 'sudo' not found; skipping system package install."
            return
        fi
    fi

    case "$distro" in
        fedora|rhel|centos|rocky|almalinux)
            log "Detected Fedora/RHEL ($distro). Installing GTK4, Libadwaita, Python & media dependencies..."
            $sudo_cmd dnf install -y \
                python3 python3-gobject gtk4 libadwaita \
                python3-numpy python3-pillow python3-opencv \
                ffmpeg firefox util-linux
            ;;
        ubuntu|debian|pop|linuxmint)
            log "Detected Debian/Ubuntu ($distro). Installing GTK4, Libadwaita, Python & media dependencies..."
            $sudo_cmd apt-get update
            $sudo_cmd apt-get install -y \
                python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
                python3-numpy python3-pil python3-opencv \
                ffmpeg firefox util-linux
            ;;
        arch|manjaro|endeavouros|cachyos)
            log "Detected Arch Linux ($distro). Installing GTK4, Libadwaita, Python & media dependencies..."
            $sudo_cmd pacman -Sy --needed --noconfirm \
                python python-gobject gtk4 libadwaita \
                python-numpy python-pillow python-opencv \
                ffmpeg firefox util-linux
            ;;
        *)
            warn "Unsupported/custom distro '${distro:-unknown}'. Ensure Python 3.11+, GTK4, Libadwaita, PyGObject, NumPy, Pillow, OpenCV, FFmpeg, and Firefox are installed."
            ;;
    esac

    if python3 -m pip --version >/dev/null 2>&1; then
        log "Ensuring playwright is available for ephemeral headless browser extraction..."
        python3 -m pip install --user --quiet playwright || warn "Could not pip install playwright; continuing with fallback browser runner."
    fi
}

install_app_files() {
    log "Installing Vimona files to ${APP_HOME}..."
    mkdir -p "$APP_HOME" "$BIN_DIR" "$APPS_DIR" "$ICONS_DIR"

    # Copy core application tree cleanly (excluding dev scratch caches)
    rm -rf "${APP_HOME}/vimona" "${APP_HOME}/assets"
    cp -a "${REPO_DIR}/vimona" "${APP_HOME}/vimona"
    cp -a "${REPO_DIR}/run_vimona.py" "${APP_HOME}/run_vimona.py"
    cp -a "${REPO_DIR}/launch-vimona.sh" "${APP_HOME}/launch-vimona.sh"
    cp -a "${REPO_DIR}/dev.vimona.Desktop.svg" "${APP_HOME}/dev.vimona.Desktop.svg"
    cp -a "${REPO_DIR}/dev.vimona.Desktop.desktop" "${APP_HOME}/dev.vimona.Desktop.desktop"
    if [[ -d "${REPO_DIR}/assets" ]]; then
        mkdir -p "${APP_HOME}/assets"
        cp -a "${REPO_DIR}/assets/vimona.ico" "${APP_HOME}/assets/vimona.ico" || true
    fi
    chmod +x "${APP_HOME}/launch-vimona.sh" "${APP_HOME}/run_vimona.py"

    ln -sf "${APP_HOME}/launch-vimona.sh" "${BIN_DIR}/vimona"

    cp -a "${REPO_DIR}/dev.vimona.Desktop.svg" "${ICONS_DIR}/dev.vimona.Desktop.svg"
    PNG_ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
    mkdir -p "${PNG_ICON_DIR}"
    if [[ -f "${REPO_DIR}/assets/brand/vimona-icon-256.png" ]]; then
        cp -a "${REPO_DIR}/assets/brand/vimona-icon-256.png" "${PNG_ICON_DIR}/dev.vimona.Desktop.png"
    fi
    sed "s|^Exec=.*|Exec=${BIN_DIR}/vimona|; s|^Icon=.*|Icon=dev.vimona.Desktop|" \
        "${REPO_DIR}/dev.vimona.Desktop.desktop" > "${APPS_DIR}/dev.vimona.Desktop.desktop"

    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPS_DIR" || true
    fi
    if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
        gtk4-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
    fi
}

verify_install() {
    log "Verifying Python imports (PyGObject + GTK4 + Vimona)..."
    python3 -c '
import sys
sys.path.insert(0, "'"${APP_HOME}"'")
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import vimona.safety_ml
print("Verification OK: GTK4 + Adw + SafeBlurMLEngine ready.")
' || warn "Runtime import check reported a missing dependency."

    log "Done! Launch from your app menu ('Vimona') or run: ${BIN_DIR}/vimona"
    if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
        warn "Add '${BIN_DIR}' to your PATH to invoke 'vimona' directly from any shell."
    fi
}

install_system_deps
install_app_files
verify_install
