#!/usr/bin/env bash
# Build standalone Linux application artifacts for Vimona:
#   1. dist/Vimona.AppDir/          (portable AppDir + native ELF stub launcher)
#   2. dist/Vimona-Linux-x86_64.run (single-file executable standalone app)
#   3. dist/Vimona-Linux-x86_64.tar.gz (portable archive)
#   4. dist/Vimona-x86_64.AppImage  (if `appimagetool` is installed)
#   5. dist/Vimona                  (if `--pyinstaller` is passed and PyInstaller is installed)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${REPO_DIR}/dist"
APP_DIR="${DIST_DIR}/Vimona.AppDir"
APP_VERSION="1.0.0"
USE_PYINSTALLER=0

for arg in "$@"; do
    case "$arg" in
        --pyinstaller) USE_PYINSTALLER=1 ;;
        -h|--help)
            cat <<'EOF'
Usage: ./installers/build_linux_standalone.sh [--pyinstaller]

Builds standalone Linux application bundles in ./dist/:
  - dist/Vimona.AppDir/             Portable AppDir with native ELF stub + Python bundle
  - dist/Vimona-Linux-x86_64.run    Single-file executable standalone Linux app
  - dist/Vimona-Linux-x86_64.tar.gz Portable tarball of Vimona.AppDir
  - dist/Vimona-x86_64.AppImage     Built automatically if 'appimagetool' is installed

Options:
  --pyinstaller   Also compile a frozen single-file ELF binary via PyInstaller
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 1
            ;;
    esac
done

log() { printf "\033[1;34m[linux-build]\033[0m %s\n" "$*"; }

cd "${REPO_DIR}"
mkdir -p "${DIST_DIR}"
rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}/usr/bin" "${APP_DIR}/usr/share/vimona"

log "Staging Vimona runtime into ${APP_DIR}..."
cp -a "${REPO_DIR}/vimona" "${APP_DIR}/usr/share/vimona/vimona"
rm -rf "${APP_DIR}/usr/share/vimona/vimona/__pycache__" \
       "${APP_DIR}/usr/share/vimona/vimona"/*/__pycache__ 2>/dev/null || true
cp -a "${REPO_DIR}/run_vimona.py" "${APP_DIR}/usr/share/vimona/run_vimona.py"
cp -a "${REPO_DIR}/dev.vimona.Desktop.svg" "${APP_DIR}/dev.vimona.Desktop.svg"
cp -a "${REPO_DIR}/dev.vimona.Desktop.svg" "${APP_DIR}/.DirIcon"
if [[ -f "${REPO_DIR}/assets/brand/vimona-icon-256.png" ]]; then
    mkdir -p "${APP_DIR}/usr/share/icons/hicolor/256x256/apps"
    cp -a "${REPO_DIR}/assets/brand/vimona-icon-256.png" "${APP_DIR}/usr/share/icons/hicolor/256x256/apps/dev.vimona.Desktop.png"
    cp -a "${REPO_DIR}/assets/brand/vimona-icon-256.png" "${APP_DIR}/dev.vimona.Desktop.png"
fi
cp -a "${REPO_DIR}/LICENSE" "${APP_DIR}/LICENSE"

# Clean desktop entry inside AppDir
cat > "${APP_DIR}/dev.vimona.Desktop.desktop" <<'EOF'
[Desktop Entry]
Version=1.0
Type=Application
Name=Vimona
Comment=Local AI Chat, /dev/shm Research, & Air-Gapped Media Studio
Exec=vimona %U
Icon=dev.vimona.Desktop
Terminal=false
Categories=Graphics;
StartupNotify=true
StartupWMClass=dev.vimona.Desktop
EOF

# Native C ELF launcher inside AppDir/usr/bin/vimona for reliable $0 / argv[0] handling
if command -v gcc >/dev/null 2>&1; then
    log "Compiling native Linux ELF stub launcher..."
    cat > "${DIST_DIR}/_vimona_stub.c" <<'CEOF'
#define _POSIX_C_SOURCE 200809L
#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char exe_buf[PATH_MAX];
    ssize_t len = readlink("/proc/self/exe", exe_buf, sizeof(exe_buf) - 1);
    if (len <= 0) {
        fprintf(stderr, "vimona: failed to resolve executable path\n");
        return 1;
    }
    exe_buf[len] = '\0';

    /* exe is <AppDir>/usr/bin/vimona -> jump to <AppDir>/usr/share/vimona */
    char *dir1 = dirname(exe_buf);          /* .../usr/bin */
    char *dir2 = dirname(dir1);             /* .../usr */
    char app_share[PATH_MAX];
    snprintf(app_share, sizeof(app_share), "%s/share/vimona", dir2);

    char py_entry[PATH_MAX + 32];
    snprintf(py_entry, sizeof(py_entry), "%s/run_vimona.py", app_share);

    const char *prev_pp = getenv("PYTHONPATH");
    if (prev_pp && prev_pp[0] != '\0') {
        size_t sz = strlen(app_share) + strlen(prev_pp) + 2;
        char *pp = malloc(sz);
        if (pp) {
            snprintf(pp, sz, "%s:%s", app_share, prev_pp);
            setenv("PYTHONPATH", pp, 1);
            free(pp);
        }
    } else {
        setenv("PYTHONPATH", app_share, 1);
    }
    setenv("VIMONA_STANDALONE", "1", 1);

    const char *py = "/usr/bin/python3";
    if (access(py, X_OK) != 0) {
        py = "python3";
    }

    char **py_argv = calloc((size_t)(argc + 2), sizeof(char *));
    if (!py_argv) return 1;
    py_argv[0] = (char *)py;
    py_argv[1] = py_entry;
    for (int i = 1; i < argc; i++) {
        py_argv[i + 1] = argv[i];
    }
    py_argv[argc + 1] = NULL;

    execvp(py, py_argv);
    perror("execvp(python3)");
    return 127;
}
CEOF
    gcc -O2 -Wall -Wextra "${DIST_DIR}/_vimona_stub.c" -o "${APP_DIR}/usr/bin/vimona"
    command -v strip >/dev/null 2>&1 && strip "${APP_DIR}/usr/bin/vimona" || true
    rm -f "${DIST_DIR}/_vimona_stub.c"
else
    cat > "${APP_DIR}/usr/bin/vimona" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
APP_SHARE="${HERE%/usr/bin}/usr/share/vimona"
export PYTHONPATH="${APP_SHARE}${PYTHONPATH:+:${PYTHONPATH}}"
export VIMONA_STANDALONE=1
exec python3 "${APP_SHARE}/run_vimona.py" "$@"
EOF
    chmod +x "${APP_DIR}/usr/bin/vimona"
fi

cat > "${APP_DIR}/AppRun" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
APPDIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
export VIMONA_APPDIR="${APPDIR}"
exec "${APPDIR}/usr/bin/vimona" "$@"
EOF
chmod +x "${APP_DIR}/AppRun" "${APP_DIR}/usr/bin/vimona" "${APP_DIR}/usr/share/vimona/run_vimona.py"

# Build portable tarball
TAR_OUT="${DIST_DIR}/Vimona-Linux-x86_64.tar.gz"
log "Creating portable archive ${TAR_OUT}..."
tar -C "${DIST_DIR}" -czf "${TAR_OUT}" "Vimona.AppDir"

# Build single-file self-extracting standalone Linux executable (.run)
PAYLOAD_HASH="$(sha256sum "${TAR_OUT}" | awk '{print substr($1,1,12)}')"
RUN_OUT="${DIST_DIR}/Vimona-Linux-x86_64.run"
log "Building single-file standalone app ${RUN_OUT} (tag ${PAYLOAD_HASH})..."

cat > "${RUN_OUT}" <<STUB_EOF
#!/usr/bin/env bash
# Vimona Standalone Linux Application (${APP_VERSION} / build ${PAYLOAD_HASH})
set -euo pipefail
BUILD_TAG="${PAYLOAD_HASH}"
CACHE_ROOT="\${XDG_CACHE_HOME:-\$HOME/.cache}/vimona-standalone-\${BUILD_TAG}"
APP_DIR="\${CACHE_ROOT}/Vimona.AppDir"

usage() {
    cat <<'EOH'
Vimona Standalone Linux App (${APP_VERSION})

Usage:
  ./Vimona-Linux-x86_64.run                   Launch Vimona desktop app
  ./Vimona-Linux-x86_64.run --install-desktop Install icon into app menu (~/.local/share/applications)
  ./Vimona-Linux-x86_64.run --extract [DIR]   Extract portable Vimona.AppDir into DIR
  ./Vimona-Linux-x86_64.run --check           Verify bundled payload and GTK4 runtime
EOH
}

extract_self() {
    local target="\$1"
    mkdir -p "\${target}"
    local line
    line=\$(awk '/^__VIMONA_TAR_GZ_PAYLOAD__\$/ {print NR + 1; exit 0}' "\$0")
    tail -n +"\${line}" "\$0" | tar -xz -C "\${target}"
}

case "\${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    --extract)
        OUT_DIR="\${2:-./Vimona-Extracted}"
        extract_self "\${OUT_DIR}"
        echo "Extracted portable app to: \${OUT_DIR}/Vimona.AppDir"
        exit 0
        ;;
esac

if [[ ! -x "\${APP_DIR}/AppRun" ]]; then
    TMP_STAGE="\${CACHE_ROOT}.staging.\$\$"
    rm -rf "\${TMP_STAGE}"
    extract_self "\${TMP_STAGE}"
    rm -rf "\${CACHE_ROOT}"
    mv "\${TMP_STAGE}" "\${CACHE_ROOT}"
fi

if [[ "\${1:-}" == "--check" ]]; then
    python3 -c '
import sys
sys.path.insert(0, "'"\${APP_DIR}/usr/share/vimona"'")
import vimona, vimona.safety_ml
from vimona.preflight import run_preflight_checks
rep = run_preflight_checks(require_display=False)
print("Standalone OK:", vimona.__app_id__, "at", "'"\${APP_DIR}"'")
print(rep.format_text())
raise SystemExit(0 if rep.can_start_gui else 1)
'
    exit \$?
fi

if [[ "\${1:-}" == "--install-desktop" ]]; then
    SELF_DEST="\${HOME}/.local/bin/Vimona-Linux-x86_64.run"
    APPS_DIR="\${XDG_DATA_HOME:-\$HOME/.local/share}/applications"
    ICONS_DIR="\${XDG_DATA_HOME:-\$HOME/.local/share}/icons/hicolor/scalable/apps"
    mkdir -p "\${HOME}/.local/bin" "\${APPS_DIR}" "\${ICONS_DIR}"
    cp -f "\$(readlink -f "\$0")" "\${SELF_DEST}"
    chmod +x "\${SELF_DEST}"
    cp -f "\${APP_DIR}/dev.vimona.Desktop.svg" "\${ICONS_DIR}/dev.vimona.Desktop.svg"
    PNG_ICONS_DIR="\${XDG_DATA_HOME:-\$HOME/.local/share}/icons/hicolor/256x256/apps"
    mkdir -p "\${PNG_ICONS_DIR}"
    if [[ -f "\${APP_DIR}/dev.vimona.Desktop.png" ]]; then
        cp -f "\${APP_DIR}/dev.vimona.Desktop.png" "\${PNG_ICONS_DIR}/dev.vimona.Desktop.png"
    fi
    sed "s|^Exec=.*|Exec=\${SELF_DEST}|; s|^Icon=.*|Icon=dev.vimona.Desktop|" \
        "\${APP_DIR}/dev.vimona.Desktop.desktop" > "\${APPS_DIR}/dev.vimona.Desktop.desktop"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "\${APPS_DIR}" || true
    command -v gtk4-update-icon-cache >/dev/null 2>&1 && gtk4-update-icon-cache -f -t "\${XDG_DATA_HOME:-\$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
    echo "Installed standalone app launcher: \${SELF_DEST}"
    exit 0
fi

exec "\${APP_DIR}/AppRun" "\$@"
__VIMONA_TAR_GZ_PAYLOAD__
STUB_EOF

cat "${TAR_OUT}" >> "${RUN_OUT}"
chmod +x "${RUN_OUT}"

if command -v appimagetool >/dev/null 2>&1; then
    AI_OUT="${DIST_DIR}/Vimona-x86_64.AppImage"
    log "Building AppImage ${AI_OUT}..."
    ARCH=x86_64 appimagetool "${APP_DIR}" "${AI_OUT}"
fi

if [[ "${USE_PYINSTALLER}" -eq 1 ]]; then
    if python3 -c "import PyInstaller" >/dev/null 2>&1; then
        log "Building PyInstaller single-file ELF dist/Vimona-Linux-x86_64..."
        python3 -m PyInstaller \
            --noconfirm \
            --clean \
            --onefile \
            --name "Vimona-Linux-x86_64" \
            --add-data "vimona/skills:vimona/skills" \
            --add-data "vimona/safety_ml_weights.npz:vimona" \
            --add-data "dev.vimona.Desktop.svg:." \
            --collect-all gi \
            --hidden-import numpy \
            --hidden-import cv2 \
            --hidden-import PIL \
            run_vimona.py
    else
        echo "PyInstaller is not installed in the active Python environment." >&2
        exit 1
    fi
fi

log "Standalone Linux artifacts built in ${DIST_DIR}:"
ls -lh "${APP_DIR}/usr/bin/vimona" "${TAR_OUT}" "${RUN_OUT}"
