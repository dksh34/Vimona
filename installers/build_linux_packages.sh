#!/usr/bin/env bash
# Build distro-native Linux packages from dist/Vimona.AppDir:
#   - dist/vimona_1.0.0_amd64.deb              (Debian / Ubuntu / Mint / Pop!_OS)
#   - dist/vimona-1.0.0-1.x86_64.rpm           (Fedora / RHEL / Nobara / openSUSE, when rpmbuild present)
#   - dist/vimona-1.0.0-1-x86_64.pkg.tar.zst   (Arch / Manjaro / CachyOS / EndeavourOS)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${REPO_DIR}/dist"
APP_DIR="${DIST_DIR}/Vimona.AppDir"
PKG_VER="1.0.0"
PKG_REL="1"

log() { printf "\033[1;34m[linux-pkgs]\033[0m %s\n" "$*"; }

cd "${REPO_DIR}"
if [[ ! -d "${APP_DIR}/usr/share/vimona" ]]; then
    "${SCRIPT_DIR}/build_linux_standalone.sh"
fi

STAGE_ROOT="${DIST_DIR}/_pkg_rootfs"
rm -rf "${STAGE_ROOT}"
mkdir -p \
    "${STAGE_ROOT}/usr/bin" \
    "${STAGE_ROOT}/usr/share/vimona" \
    "${STAGE_ROOT}/usr/share/applications" \
    "${STAGE_ROOT}/usr/share/icons/hicolor/scalable/apps" \
    "${STAGE_ROOT}/usr/share/icons/hicolor/256x256/apps" \
    "${STAGE_ROOT}/usr/share/licenses/vimona"

cp -a "${APP_DIR}/usr/bin/vimona" "${STAGE_ROOT}/usr/bin/vimona"
cp -a "${APP_DIR}/usr/share/vimona/." "${STAGE_ROOT}/usr/share/vimona/"
cp -a "${APP_DIR}/dev.vimona.Desktop.svg" "${STAGE_ROOT}/usr/share/icons/hicolor/scalable/apps/dev.vimona.Desktop.svg"
if [[ -f "${APP_DIR}/dev.vimona.Desktop.png" ]]; then
    cp -a "${APP_DIR}/dev.vimona.Desktop.png" "${STAGE_ROOT}/usr/share/icons/hicolor/256x256/apps/dev.vimona.Desktop.png"
fi
cp -a "${REPO_DIR}/LICENSE" "${STAGE_ROOT}/usr/share/licenses/vimona/LICENSE"

sed "s|^Exec=.*|Exec=/usr/bin/vimona %U|; s|^Icon=.*|Icon=dev.vimona.Desktop|" \
    "${APP_DIR}/dev.vimona.Desktop.desktop" > "${STAGE_ROOT}/usr/share/applications/dev.vimona.Desktop.desktop"

# 1. Debian / Ubuntu (.deb) using dpkg-deb OR standard ar + tar fallback
DEB_STAGE="${DIST_DIR}/_deb_build"
DEB_OUT="${DIST_DIR}/vimona_${PKG_VER}_amd64.deb"
rm -rf "${DEB_STAGE}" "${DEB_OUT}"
mkdir -p "${DEB_STAGE}/DEBIAN"
cp -a "${STAGE_ROOT}/." "${DEB_STAGE}/"

INSTALLED_KB="$(du -sk "${STAGE_ROOT}/usr" | awk '{print $1}')"
cat > "${DEB_STAGE}/DEBIAN/control" <<EOF
Package: vimona
Version: ${PKG_VER}-${PKG_REL}
Section: graphics
Priority: optional
Architecture: amd64
Installed-Size: ${INSTALLED_KB}
Maintainer: Vimona Project <dksh3413@gmail.com>
Homepage: https://github.com/dksh34/Vimona
Depends: python3 (>= 3.11), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1, python3-numpy, python3-pil, ffmpeg, util-linux
Recommends: firefox
Description: Local AI orchestration harness with hardware-tuned Hugging Face models
 Vimona pairs GTK4/Libadwaita UI, ephemeral /dev/shm Firefox research, and
 kernel-isolated media engines orchestrated with sequential zero-overlap RAM relay.
EOF

cat > "${DEB_STAGE}/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database /usr/share/applications || true
command -v gtk4-update-icon-cache >/dev/null 2>&1 && gtk4-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
exit 0
EOF
chmod 0755 "${DEB_STAGE}/DEBIAN/postinst"

if command -v dpkg-deb >/dev/null 2>&1; then
    log "Building Debian/Ubuntu package via dpkg-deb: ${DEB_OUT}..."
    dpkg-deb --root-owner-group --build "${DEB_STAGE}" "${DEB_OUT}"
else
    log "Building Debian/Ubuntu package via ar+tar: ${DEB_OUT}..."
    printf "2.0\n" > "${DEB_STAGE}/debian-binary"
    (cd "${DEB_STAGE}/DEBIAN" && tar --numeric-owner --owner=0 --group=0 -czf "${DEB_STAGE}/control.tar.gz" .)
    (cd "${DEB_STAGE}" && tar --numeric-owner --owner=0 --group=0 --exclude='./DEBIAN' --exclude='./debian-binary' --exclude='./control.tar.gz' -czf "${DEB_STAGE}/data.tar.gz" ./usr)
    ar rcs "${DEB_OUT}" "${DEB_STAGE}/debian-binary" "${DEB_STAGE}/control.tar.gz" "${DEB_STAGE}/data.tar.gz"
fi
rm -rf "${DEB_STAGE}"

# 2. Arch Linux / Manjaro / CachyOS (.pkg.tar.zst)
if command -v zstd >/dev/null 2>&1; then
    ARCH_STAGE="${DIST_DIR}/_arch_build"
    ARCH_OUT="${DIST_DIR}/vimona-${PKG_VER}-${PKG_REL}-x86_64.pkg.tar.zst"
    rm -rf "${ARCH_STAGE}" "${ARCH_OUT}"
    mkdir -p "${ARCH_STAGE}"
    cp -a "${STAGE_ROOT}/." "${ARCH_STAGE}/"
    INSTALLED_BYTES="$(du -sb "${STAGE_ROOT}/usr" | awk '{print $1}')"
    BUILD_DATE="$(date +%s)"
    cat > "${ARCH_STAGE}/.PKGINFO" <<EOF
pkgname = vimona
pkgbase = vimona
pkgver = ${PKG_VER}-${PKG_REL}
pkgdesc = Local AI orchestration harness with hardware-tuned Hugging Face models
url = https://github.com/dksh34/Vimona
builddate = ${BUILD_DATE}
packager = Vimona Project <dksh3413@gmail.com>
size = ${INSTALLED_BYTES}
arch = x86_64
license = MIT
depend = python
depend = python-gobject
depend = gtk4
depend = libadwaita
depend = python-numpy
depend = python-pillow
depend = ffmpeg
depend = util-linux
EOF
    log "Building Arch Linux package: ${ARCH_OUT}..."
    (cd "${ARCH_STAGE}" && tar --numeric-owner --owner=0 --group=0 -cf - .PKGINFO usr | zstd -q -19 -o "${ARCH_OUT}")
    rm -rf "${ARCH_STAGE}"
fi

# 3. Fedora / RHEL / openSUSE (.rpm) via rpmbuild
if command -v rpmbuild >/dev/null 2>&1; then
    RPM_TOP="${DIST_DIR}/_rpmbuild"
    rm -rf "${RPM_TOP}"
    mkdir -p "${RPM_TOP}"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
    tar -C "${STAGE_ROOT}" -czf "${RPM_TOP}/SOURCES/vimona-rootfs.tar.gz" usr
    cat > "${RPM_TOP}/SPECS/vimona.spec" <<EOF
%global debug_package %{nil}
%define _build_id_links none

Name:           vimona
Version:        ${PKG_VER}
Release:        ${PKG_REL}%{?dist}
Summary:        Local AI orchestration harness with hardware-tuned Hugging Face models
License:        MIT
URL:            https://github.com/dksh34/Vimona
BuildArch:      x86_64
Source0:        vimona-rootfs.tar.gz
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       python3-numpy
Requires:       python3-pillow
Requires:       ffmpeg-free
Requires:       util-linux

%description
Vimona is a GTK4 + Libadwaita local AI orchestration harness powered by
Hugging Face hardware-tuned model picking, ephemeral /dev/shm research,
and kernel-isolated offline image/video diffusion relays.

%prep
%setup -c -T

%install
tar -C %{buildroot} -xzf %{SOURCE0}

%files
/usr/bin/vimona
/usr/share/vimona
/usr/share/applications/dev.vimona.Desktop.desktop
/usr/share/icons/hicolor/scalable/apps/dev.vimona.Desktop.svg
/usr/share/icons/hicolor/256x256/apps/dev.vimona.Desktop.png
/usr/share/licenses/vimona/LICENSE
EOF
    log "Building Fedora/RHEL/openSUSE RPM via rpmbuild..."
    rpmbuild --define "_topdir ${RPM_TOP}" -bb "${RPM_TOP}/SPECS/vimona.spec"
    find "${RPM_TOP}/RPMS" -name '*.rpm' -exec cp -f {} "${DIST_DIR}/vimona-${PKG_VER}-${PKG_REL}.x86_64.rpm" \;
    rm -rf "${RPM_TOP}"
else
    log "Note: rpmbuild not installed locally; .rpm will be built in GitHub Actions Linux CI."
fi

rm -rf "${STAGE_ROOT}"
log "Built Linux distro packages in ${DIST_DIR}:"
ls -lh "${DIST_DIR}"/*.deb "${DIST_DIR}"/*.rpm "${DIST_DIR}"/*.pkg.tar.zst 2>/dev/null || ls -lh "${DIST_DIR}"
