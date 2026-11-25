"""Installed-apps-only RAM inspector & closer window for Vimona (Fedora/Linux)."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import signal
import threading
import time
from typing import Callable

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk, Pango

from .icons import SvgIcon
from .slide_commit import SlideCommitWidget
from .tokens import COLORS


_PROTECTED_MODEL_PIDS_LOCK = threading.Lock()
_PROTECTED_MODEL_PIDS: dict[int, tuple[str, int | None]] = {}


def _read_pid_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="ignore")
        rp = raw.rfind(")")
        if rp < 0:
            return None
        fields = raw[rp + 2 :].split()
        return int(fields[19]) if len(fields) > 19 else None
    except Exception:
        return None


def register_protected_pid(pid: int, reason: str = "Model engine (locked)") -> None:
    """Protect a model-running PID (bound to its kernel starttime) until verified exit (`E10`, `K1`)."""
    if pid and pid > 2:
        st_ticks = _read_pid_start_ticks(int(pid))
        with _PROTECTED_MODEL_PIDS_LOCK:
            _PROTECTED_MODEL_PIDS[int(pid)] = (reason, st_ticks)


def unregister_protected_pid(pid: int | None) -> None:
    if pid:
        with _PROTECTED_MODEL_PIDS_LOCK:
            _PROTECTED_MODEL_PIDS.pop(int(pid), None)


def _get_protected_pid_map() -> dict[int, str]:
    """Return currently valid protected PIDs, purging exited/reused entries (`E10`, `K1`)."""
    out: dict[int, str] = {}
    stale: list[int] = []
    with _PROTECTED_MODEL_PIDS_LOCK:
        items = list(_PROTECTED_MODEL_PIDS.items())
    for pid, (reason, rec_ticks) in items:
        cur_ticks = _read_pid_start_ticks(pid)
        if cur_ticks is None:
            stale.append(pid)
            continue
        if rec_ticks is not None and cur_ticks != rec_ticks:
            stale.append(pid)
            continue
        out[pid] = reason
    if stale:
        with _PROTECTED_MODEL_PIDS_LOCK:
            for spid in stale:
                _PROTECTED_MODEL_PIDS.pop(spid, None)
    return out


@dataclass
class InstalledAppUsage:
    app_id: str
    name: str
    binary_name: str
    rss_mb: float
    pids: tuple[int, ...]
    category: str = "Installed App"
    anon_rss_mb: float = 0.0
    shared_mb: float = 0.0
    pid_identities: tuple[tuple[int, int | None], ...] = ()


@dataclass
class ProcessRamUsage:
    pid: int
    name: str
    cmd_preview: str
    rss_mb: float
    uid: int
    protected: bool
    protection_reason: str = ""
    anon_rss_mb: float = 0.0
    shared_mb: float = 0.0
    start_ticks: int | None = None
    exe_path: str = ""


@dataclass
class _DesktopAppMeta:
    app_id: str
    name: str
    exec_names: tuple[str, ...]
    category: str


_APP_DESKTOP_DIRS: tuple[Path, ...] = (
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path("~/.local/share/applications").expanduser(),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("~/.local/share/flatpak/exports/share/applications").expanduser(),
    Path("/var/lib/snapd/desktop/applications"),
)

# Explicit defense-in-depth denylist so kernel, desktop session, audio, or system
# daemons NEVER appear even if a .desktop or binary name partially overlaps.
_SYSTEM_PROC_EXACT: frozenset[str] = frozenset(
    {
        "systemd",
        "init",
        "dbus-daemon",
        "dbus-broker",
        "dbus-broker-launch",
        "pipewire",
        "pipewire-pulse",
        "pipewire-media-session",
        "wireplumber",
        "pulseaudio",
        "gnome-shell",
        "gnome-session-binary",
        "gnome-session-ctl",
        "mutter",
        "mutter-x11-frames",
        "kwin_wayland",
        "kwin_x11",
        "plasmashell",
        "xwayland",
        "sway",
        "wayfire",
        "hyprland",
        "gdm",
        "gdm-wayland-session",
        "gdm-x-session",
        "sddm",
        "dconf-service",
        "gnome-keyring-daemon",
        "ssh-agent",
        "gpg-agent",
        "polkit-gnome-authentication-agent-1",
        "polkitd",
        "upowerd",
        "udisksd",
        "rtkit-daemon",
        "accounts-daemon",
        "networkmanager",
        "wpa_supplicant",
        "bluetoothd",
        "power-profiles-daemon",
        "thermald",
        "boltd",
        "colord",
        "goa-daemon",
        "goa-identity-service",
        "orca",
        "abrt-applet",
        "gjs",
        "sh",
        "bash",
        "zsh",
        "fish",
        "dash",
        "sudo",
        "su",
        "pkexec",
        "python",
        "python3",
        "node",
        "llama-server",
    }
)

_SYSTEM_PROC_PREFIXES: tuple[str, ...] = (
    "kworker",
    "ksoftirqd",
    "kswapd",
    "migration",
    "systemd-",
    "gvfs",
    "xdg-",
    "at-spi",
    "gsd-",
    "evolution-",
    "tracker-",
    "localsearch",
    "tinysparql",
    "ibus",
    "fcitx",
    "gnome-initial-setup",
    "abrt-",
    "NetworkManager",
)

_SYSTEM_CGROUP_HINTS: tuple[str, ...] = (
    "/system.slice/",
    "/init.scope",
    "xdg-desktop-portal",
    "gvfs",
    "at-spi",
    "pipewire",
    "wireplumber",
    "dbus",
    "gsd-",
    "gnome-session",
    "gnome-shell",
    "evolution-",
    "tracker-",
    "ibus",
)

_SYSTEM_DESKTOP_INFRA_NAMES: frozenset[str] = frozenset(
    {
        "gnome-control-center",
        "org.gnome.settings",
        "org.gnome.shell",
        "org.gnome.nautiluspreviewer",
        "vimona",
        "dev.vimona.desktop",
    }
)

_EXEC_WRAPPERS: frozenset[str] = frozenset(
    {
        "env",
        "flatpak",
        "run",
        "snap",
        "nohup",
        "setsid",
        "bash",
        "sh",
        "dbus-launch",
        "gamemoderun",
        "prime-run",
    }
)

_DESKTOP_CACHE: tuple[float, dict[str, _DesktopAppMeta], dict[str, _DesktopAppMeta]] | None = None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _extract_exec_candidates(exec_line: str) -> tuple[str, ...]:
    """Parse XDG Desktop Entry `Exec=` strings via Freedesktop unescaping & shlex (`K8`)."""
    # Unescape Desktop Entry backslash sequences (`\s`, `\n`, `\"`, `\\`) before tokenization
    unescaped = (
        exec_line.replace("\\\\", "\\")
        .replace("\\s", " ")
        .replace("\\t", " ")
    )
    cleaned = re.sub(r"%[fFuUdDnNickvm]", "", unescaped).strip()
    try:
        parts = shlex.split(cleaned, posix=True)
    except ValueError:
        parts = [p.strip("\"'") for p in cleaned.split() if p.strip()]
    out: list[str] = []
    for tok in parts:
        if not tok:
            continue
        if "=" in tok and not tok.startswith("/"):
            continue
        if tok.startswith("-"):
            continue
        base = Path(tok).name.lower()
        if base in _EXEC_WRAPPERS:
            continue
        if base in ("bash", "sh", "-c"):
            continue
        if base and base not in out:
            out.append(base)
            stem = re.sub(r"-\d+(\.\d+)*$", "", base)
            if stem and stem != base and stem not in out:
                out.append(stem)
    return tuple(out)


def _load_installed_desktop_catalog() -> tuple[dict[str, _DesktopAppMeta], dict[str, _DesktopAppMeta]]:
    """Return `(by_app_id, by_binary_name)` for installed user-facing `.desktop` apps."""
    global _DESKTOP_CACHE
    now = time.monotonic()
    if _DESKTOP_CACHE is not None and (now - _DESKTOP_CACHE[0]) < 30.0:
        return _DESKTOP_CACHE[1], _DESKTOP_CACHE[2]

    by_id: dict[str, _DesktopAppMeta] = {}
    by_bin: dict[str, _DesktopAppMeta] = {}

    for root in _APP_DESKTOP_DIRS:
        if not root.is_dir():
            continue
        for desktop_file in root.glob("*.desktop"):
            raw = _read_text(desktop_file)
            if not raw:
                continue
            in_entry = False
            fields: dict[str, str] = {}
            for line in raw.splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("["):
                    in_entry = s == "[Desktop Entry]"
                    continue
                if not in_entry or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                if k not in fields:
                    fields[k.strip()] = v.strip()

            if fields.get("Type") != "Application":
                continue
            if fields.get("NoDisplay", "").lower() == "true":
                continue
            if fields.get("Hidden", "").lower() == "true":
                continue
            if not fields.get("Name") or not fields.get("Exec"):
                continue

            app_id = desktop_file.stem.lower()
            if app_id in _SYSTEM_DESKTOP_INFRA_NAMES:
                continue

            cats = fields.get("Categories", "")
            # Exclude core GNOME/KDE background Settings/Daemon components
            only_in = fields.get("OnlyShowIn", "")
            if "System;" in cats and "Emulator" not in cats and "TerminalEmulator" not in cats and "Settings" in cats:
                continue
            if only_in and ("X-GNOME" in fields.get("X-GNOME-Autostart-Phase", "")):
                continue

            exec_bins = _extract_exec_candidates(fields["Exec"])
            # Filter out entries whose only binary is a system binary or generic shell
            safe_bins = tuple(
                b for b in exec_bins if b not in _SYSTEM_PROC_EXACT and not b.startswith(_SYSTEM_PROC_PREFIXES)
            )
            if not safe_bins and "flatpak" not in fields["Exec"].lower():
                continue

            source_tag = "Flatpak App" if "flatpak" in str(root) else "Installed App"
            meta = _DesktopAppMeta(
                app_id=app_id,
                name=fields["Name"],
                exec_names=safe_bins,
                category=source_tag,
            )
            by_id.setdefault(app_id, meta)
            for b in safe_bins:
                # Prefer user/flatpak or exact-name entries over generic helpers
                by_bin.setdefault(b, meta)

    _DESKTOP_CACHE = (now, by_id, by_bin)
    return by_id, by_bin


def _is_system_process(
    comm: str,
    exe_path: str,
    cmdline_args: list[str],
    cgroup_text: str,
) -> bool:
    low_comm = comm.lower()
    if low_comm in _SYSTEM_PROC_EXACT or low_comm.startswith(_SYSTEM_PROC_PREFIXES):
        return True
    if exe_path:
        low_exe = exe_path.lower()
        exe_base = Path(low_exe).name
        if any(seg in low_exe for seg in ("/usr/libexec/", "/usr/lib/systemd/", "/lib/systemd/", "/usr/sbin/", "/sbin/")):
            # Exceptions for legitimate user apps shipped inside lib/ (like Firefox / Chrome / Electron / Code / LibreOffice)
            if not any(ok in low_exe for ok in ("firefox", "chrome", "chromium", "brave", "code", "libreoffice", "obsidian", "discord", "slack", "spotify", "thunderbird")):
                return True
        if exe_base in _SYSTEM_PROC_EXACT or exe_base.startswith(_SYSTEM_PROC_PREFIXES):
            return True
    if any(hint in cgroup_text for hint in _SYSTEM_CGROUP_HINTS):
        return True
    # Exclude Linux background GApplication daemons, Vimona itself, and internal inference runtimes
    joined = " ".join(cmdline_args).lower()
    if "--gapplication-service" in joined or "vimona" in joined or "comfyui" in joined or "llama-server" in joined:
        return True
    return False


def _match_installed_app(
    comm: str,
    exe_path: str,
    cmdline_args: list[str],
    cgroup_text: str,
    by_id: dict[str, _DesktopAppMeta],
    by_bin: dict[str, _DesktopAppMeta],
) -> _DesktopAppMeta | None:
    # 1. Flatpak / Snap / Desktop Unit match in cgroup (`app-flatpak-com.discordapp.Discord-1234.scope`)
    m_fp = re.search(r"app-flatpak-([A-Za-z0-9._-]+?)-\d+\.scope", cgroup_text)
    if m_fp:
        fp_id = m_fp.group(1).lower()
        if fp_id in by_id:
            return by_id[fp_id]
        short = fp_id.split(".")[-1].title()
        return _DesktopAppMeta(app_id=fp_id, name=short, exec_names=(comm.lower(),), category="Flatpak App")

    m_app = re.search(r"app-(?:gnome|kde|org\.freedesktop\.[^\s-]+)-([A-Za-z0-9._-]+?)-\d+\.scope", cgroup_text)
    if m_app:
        unit_id = m_app.group(1).replace("\\x2d", "-").lower()
        if unit_id in by_id:
            return by_id[unit_id]

    # 2. Match executable basename or argv[0] basename against `.desktop` binary catalog
    candidates: list[str] = []
    if exe_path:
        b = Path(exe_path).name.lower()
        if b:
            candidates.append(b)
    if cmdline_args:
        a0 = Path(cmdline_args[0]).name.lower()
        if a0 and a0 not in candidates:
            candidates.append(a0)
    if comm:
        c = comm.lower()
        if c not in candidates:
            candidates.append(c)

    for cand in candidates:
        if cand in _SYSTEM_PROC_EXACT or cand.startswith(_SYSTEM_PROC_PREFIXES):
            continue
        if cand in by_bin:
            return by_bin[cand]
        stem = re.sub(r"-\d+(\.\d+)*$", "", cand)
        if stem in by_bin:
            return by_bin[stem]

    return None


def _current_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _parse_proc_status_mem(status_txt: str) -> tuple[int, str, float, float, float]:
    """Return `(uid, comm, rss_mb, anon_rss_mb, shared_mb)` accurately from `/proc/<pid>/status` (`K10`)."""
    proc_uid = -1
    rss_kb = 0
    anon_kb = 0
    file_kb = 0
    shmem_kb = 0
    comm = ""
    for ln in status_txt.splitlines():
        if ln.startswith("Uid:"):
            parts = ln.split()
            if len(parts) >= 2 and parts[1].isdigit():
                proc_uid = int(parts[1])
        elif ln.startswith("VmRSS:"):
            parts = ln.split()
            if len(parts) >= 2 and parts[1].isdigit():
                rss_kb = int(parts[1])
        elif ln.startswith("RssAnon:"):
            parts = ln.split()
            if len(parts) >= 2 and parts[1].isdigit():
                anon_kb = int(parts[1])
        elif ln.startswith("RssFile:"):
            parts = ln.split()
            if len(parts) >= 2 and parts[1].isdigit():
                file_kb = int(parts[1])
        elif ln.startswith("RssShmem:"):
            parts = ln.split()
            if len(parts) >= 2 and parts[1].isdigit():
                shmem_kb = int(parts[1])
        elif ln.startswith("Name:"):
            comm = ln.split(":", 1)[1].strip()
    rss_mb = rss_kb / 1024.0
    anon_mb = (anon_kb / 1024.0) if anon_kb > 0 else rss_mb
    shared_mb = (file_kb + shmem_kb) / 1024.0
    return (proc_uid, comm, rss_mb, anon_mb, shared_mb)


def _send_sigterm_verified(
    pid: int,
    *,
    expected_uid: int,
    expected_start_ticks: int | None = None,
    expected_name: str = "",
) -> tuple[bool, str]:
    """Verify UID, starttime & identity and send SIGTERM atomically via pidfd when available (`K1`–`K3`)."""
    entry = Path(f"/proc/{pid}")
    if not entry.is_dir():
        return False, f"Process {pid} already exited."
    status_txt = _read_text(entry / "status")
    if not status_txt:
        return False, f"Process {pid} vanished during check."
    cur_uid, cur_comm, _r, _a, _s = _parse_proc_status_mem(status_txt)
    if cur_uid != expected_uid:
        return False, f"UID mismatch on PID {pid}; refusing signal."
    cur_ticks = _read_pid_start_ticks(pid)
    if expected_start_ticks is not None and (cur_ticks is None or cur_ticks != expected_start_ticks):
        return False, f"PID {pid} was reused after scan; aborted safely."
    if expected_name and cur_comm and expected_name.lower() != cur_comm.lower():
        # Verify exe basename wasn't completely swapped
        try:
            exe_b = Path(os.readlink(entry / "exe")).name.lower()
        except OSError:
            exe_b = ""
        if expected_name.lower() not in (cur_comm.lower(), exe_b):
            return False, f"PID {pid} identity changed ({cur_comm}); aborted safely."

    live_prot, live_reason = _is_pid_protected_live(pid)
    if live_prot:
        return False, f"Protected process ({live_reason}) cannot be killed."

    # Prefer Linux pidfd_open + pidfd_send_signal to prevent post-verification PID-reuse race (`K3`)
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        pfd = -1
        try:
            pfd = os.pidfd_open(pid, 0)
            # Re-check starttime while holding open pidfd to close race window completely
            if expected_start_ticks is not None and _read_pid_start_ticks(pid) != expected_start_ticks:
                return False, f"PID {pid} exited/reused before signal; aborted safely."
            signal.pidfd_send_signal(pfd, signal.SIGTERM, None, 0)
            return True, f"Sent graceful terminate signal via pidfd to {cur_comm or pid} (PID {pid})"
        except ProcessLookupError:
            return False, f"Process {pid} already exited."
        except PermissionError:
            return False, f"Permission denied for PID {pid}."
        except OSError:
            pass
        finally:
            if pfd >= 0:
                try:
                    os.close(pfd)
                except OSError:
                    pass

    try:
        os.kill(pid, signal.SIGTERM)
        return True, f"Sent terminate signal to {cur_comm or pid} (PID {pid})"
    except ProcessLookupError:
        return False, f"Process {pid} already exited."
    except PermissionError:
        return False, f"Permission denied for PID {pid}."


def scan_installed_running_apps(min_rss_mb: float = 8.0) -> list[InstalledAppUsage]:
    """Scan `/proc` for currently running user-installed desktop apps ONLY.

    Strictly excludes root processes, kernel threads, systemd slices, display servers,
    audio/dbus/gvfs portals, shells, and Vimona itself. Groups multi-process apps
    (browsers, Electron apps) into one entry with summed RSS MB.
    """
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    uid = _current_uid()
    my_pid = os.getpid()
    by_id, by_bin = _load_installed_desktop_catalog()
    grouped: dict[str, list[tuple[int, float, float, float, int | None, _DesktopAppMeta, str]]] = {}

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue

        status_txt = _read_text(entry / "status")
        if not status_txt:
            continue

        proc_uid, comm, rss_mb, anon_mb, shared_mb = _parse_proc_status_mem(status_txt)
        if proc_uid != uid or rss_mb <= 0:
            continue

        raw_cmd = ""
        try:
            raw_cmd = (entry / "cmdline").read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        if not raw_cmd:
            continue
        cmd_args = [a for a in raw_cmd.split("\x00") if a]
        if not cmd_args:
            continue

        try:
            exe_path = os.readlink(entry / "exe")
        except OSError:
            exe_path = ""

        cgroup_text = _read_text(entry / "cgroup")
        if _is_system_process(comm, exe_path, cmd_args, cgroup_text):
            continue
        exe_base = Path(exe_path).name.lower() if exe_path else ""
        cmd_joined = " ".join(cmd_args)
        is_model_proc, _reason = _classify_model_engine_process(
            pid=pid,
            comm=comm,
            exe_base=exe_base,
            cmd_str=cmd_joined,
        )
        if is_model_proc:
            continue

        meta = _match_installed_app(comm, exe_path, cmd_args, cgroup_text, by_id, by_bin)
        if meta is None:
            continue

        st_ticks = _read_pid_start_ticks(pid)
        grouped.setdefault(meta.app_id, []).append(
            (pid, rss_mb, anon_mb, shared_mb, st_ticks, meta, comm)
        )

    apps: list[InstalledAppUsage] = []
    for app_id, proc_items in grouped.items():
        total_mb = sum(item[1] for item in proc_items)
        if total_mb < min_rss_mb:
            continue
        total_anon = sum(item[2] for item in proc_items)
        total_shared = sum(item[3] for item in proc_items)
        meta = proc_items[0][5]
        pids = tuple(sorted(item[0] for item in proc_items))
        identities = tuple((item[0], item[4]) for item in proc_items)
        main_bin = meta.exec_names[0] if meta.exec_names else proc_items[0][6]
        apps.append(
            InstalledAppUsage(
                app_id=app_id,
                name=meta.name,
                binary_name=main_bin,
                rss_mb=total_mb,
                pids=pids,
                category=meta.category,
                anon_rss_mb=total_anon,
                shared_mb=total_shared,
                pid_identities=identities,
            )
        )

    apps.sort(key=lambda a: a.rss_mb, reverse=True)
    return apps


def close_installed_app(app: InstalledAppUsage) -> tuple[bool, int]:
    """Send verified graceful SIGTERM via pidfd/starttime validation to an installed app (`K1`–`K3`)."""
    signaled = 0
    uid = _current_uid()
    id_map = dict(app.pid_identities) if app.pid_identities else {}
    for pid in app.pids:
        ok, _msg = _send_sigterm_verified(
            pid,
            expected_uid=uid,
            expected_start_ticks=id_map.get(pid),
        )
        if ok:
            signaled += 1
    return (signaled > 0), signaled


_MODEL_ENGINE_EXACT: frozenset[str] = frozenset(
    {
        "llama-server",
        "llama-cli",
        "llama-bench",
        "llama-completion",
        "sd-cli",
        "sd",
        "stable-diffusion",
        "ollama",
        "ollama_runner",
        "vllm",
        "koboldcpp",
        "exllamav2",
        "tabbyapi",
        "comfyui",
        "glslc",
        "glslang",
        "glslangvalidator",
        "rocminfo",
        "rocm-smi",
        "ffmpeg",
        "ffprobe",
    }
)

_MODEL_ENGINE_CMD_HINTS: tuple[str, ...] = (
    "llama-server",
    "llama.cpp",
    "stable-diffusion.cpp",
    "sd-cli",
    "/comfyui/",
    "comfyui/main.py",
    "comfyui",
    "vimona_media_engine",
    "fast_media_engine",
    "light-uncensored.gguf",
    "pony-diffusion",
    "wan2.1",
    "umt5_xxl",
    ".gguf",
    ".safetensors",
    "models/llm",
    "models/unet",
    "models/diffusion_models",
    "models/loras",
    "vimona_kf_",
    "vimona_win_",
    "vimona_clip_",
    "rocm",
    "miopen",
    "amdgpu",
    "vulkan",
    "glslc",
    "triton",
    "inductor",
    "torch.compile",
    "pytorch",
    "ggml",
    "onnxruntime",
    "causvid",
    "dmd2_sdxl",
)


_CRASH_CRITICAL_EXACT: frozenset[str] = frozenset(
    {
        "systemd",
        "init",
        "kthreadd",
        "dbus-daemon",
        "dbus-broker",
        "dbus-broker-launch",
        "gnome-shell",
        "mutter",
        "mutter-x11-frames",
        "kwin_wayland",
        "kwin_x11",
        "plasmashell",
        "xorg",
        "xwayland",
        "sway",
        "wayfire",
        "hyprland",
        "gdm",
        "gdm-wayland-session",
        "gdm-x-session",
        "sddm",
        "lightdm",
        "login",
        "gnome-session-binary",
        "gnome-session-ctl",
        "pipewire",
        "wireplumber",
        "pulseaudio",
        "gnome-keyring-daemon",
        "polkitd",
        "udevd",
        "systemd-udevd",
        "networkmanager",
        "upowerd",
        "udisksd",
        "dconf-service",
    }
)


def _get_ancestor_pids() -> set[int]:
    anc: set[int] = set()
    cur = os.getpid()
    for _ in range(16):
        anc.add(cur)
        stat_txt = _read_text(Path(f"/proc/{cur}/stat"))
        if not stat_txt:
            break
        rparen = stat_txt.rfind(")")
        if rparen < 0:
            break
        rest = stat_txt[rparen + 2 :].split()
        if len(rest) >= 2 and rest[1].isdigit():
            ppid = int(rest[1])
            if ppid <= 1 or ppid in anc:
                break
            cur = ppid
        else:
            break
    return anc


def _get_ppid_map() -> dict[int, int]:
    ppids: dict[int, int] = {}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return ppids
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat_txt = _read_text(entry / "stat")
        if not stat_txt:
            continue
        rparen = stat_txt.rfind(")")
        if rparen < 0:
            continue
        rest = stat_txt[rparen + 2 :].split()
        if len(rest) >= 2 and rest[1].isdigit():
            ppids[pid] = int(rest[1])
    return ppids


def _classify_model_engine_process(
    pid: int,
    comm: str,
    exe_base: str,
    cmd_str: str,
    protected_pids: dict[int, str] | None = None,
    ppid_map: dict[int, int] | None = None,
) -> tuple[bool, str]:
    reg = protected_pids if protected_pids is not None else _get_protected_pid_map()
    if pid in reg:
        return True, reg[pid]

    # Walk parents to also protect child workers forked by model runners
    if reg:
        pmap = ppid_map if ppid_map is not None else _get_ppid_map()
        cur = pid
        seen: set[int] = set()
        for _ in range(12):
            if cur in reg:
                return True, reg[cur]
            parent = pmap.get(cur, 0)
            if parent <= 1 or parent in seen:
                break
            seen.add(cur)
            cur = parent

    low = (comm or "").lower()
    exe_low = (exe_base or "").lower()
    cmd_low = (cmd_str or "").lower()
    if low in _MODEL_ENGINE_EXACT or exe_low in _MODEL_ENGINE_EXACT:
        return True, "Model engine (locked)"
    if any(hint in cmd_low for hint in _MODEL_ENGINE_CMD_HINTS):
        return True, "Model engine (locked)"
    if (low == "ffmpeg" or exe_low == "ffmpeg") and ("vimona" in cmd_low or "comfyui" in cmd_low):
        return True, "Media pipeline (locked)"
    return False, ""


def _is_pid_protected_live(pid: int) -> tuple[bool, str]:
    if pid <= 2:
        return True, "Kernel thread"
    if pid in _get_ancestor_pids():
        return True, "Vimona / active session"
    entry = Path(f"/proc/{pid}")
    if not entry.is_dir():
        return False, ""
    status_txt = _read_text(entry / "status")
    proc_uid = -1
    comm = ""
    for ln in status_txt.splitlines():
        if ln.startswith("Uid:"):
            parts = ln.split()
            if len(parts) >= 2 and parts[1].isdigit():
                proc_uid = int(parts[1])
        elif ln.startswith("Name:"):
            comm = ln.split(":", 1)[1].strip()
    if proc_uid != -1 and proc_uid != _current_uid():
        return True, "Root / OS service"
    raw_cmd = ""
    try:
        raw_cmd = (entry / "cmdline").read_bytes().decode("utf-8", errors="ignore")
    except OSError:
        pass
    cmd_args = [a for a in raw_cmd.split("\x00") if a] if raw_cmd else []
    if not cmd_args:
        return True, "Kernel thread"
    cmd_str = " ".join(cmd_args)
    try:
        exe_base = Path(os.readlink(entry / "exe")).name.lower()
    except OSError:
        exe_base = ""
    is_model, reason = _classify_model_engine_process(pid, comm, exe_base, cmd_str)
    if is_model:
        return True, reason
    cgroup_text = _read_text(entry / "cgroup")
    low = comm.lower()
    if (
        low in _CRASH_CRITICAL_EXACT
        or exe_base in _CRASH_CRITICAL_EXACT
        or low.startswith(("kworker", "kswapd", "systemd-", "xdg-desktop-portal", "at-spi", "gsd-", "gnome-session"))
        or "/system.slice/" in cgroup_text
        or "/init.scope" in cgroup_text
    ):
        return True, "Desktop / OS core"
    if "vimona" in cmd_str.lower():
        return True, "Vimona UI"
    return False, ""


def scan_all_processes_by_ram(min_rss_mb: float = 4.0) -> list[ProcessRamUsage]:
    """Return ALL processes sorted highest RAM first, flagging OS & model processes as protected."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    uid = _current_uid()
    ancestors = _get_ancestor_pids()
    protected_pids = _get_protected_pid_map()
    ppid_map = _get_ppid_map()
    out: list[ProcessRamUsage] = []

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        status_txt = _read_text(entry / "status")
        if not status_txt:
            continue

        proc_uid, comm, rss_mb, anon_mb, shared_mb = _parse_proc_status_mem(status_txt)
        if rss_mb <= 0 or rss_mb < min_rss_mb:
            continue

        raw_cmd = ""
        try:
            raw_cmd = (entry / "cmdline").read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            pass
        cmd_args = [a for a in raw_cmd.split("\x00") if a] if raw_cmd else []
        cmd_full = " ".join(cmd_args) if cmd_args else f"[{comm or pid}]"
        cmd_str = cmd_full[:87] + "…" if len(cmd_full) > 90 else cmd_full

        try:
            exe_path = os.readlink(entry / "exe")
        except OSError:
            exe_path = ""
        cgroup_text = _read_text(entry / "cgroup")

        low = (comm or "").lower()
        exe_base = Path(exe_path).name.lower() if exe_path else ""
        protected = False
        reason = ""

        is_model, model_reason = _classify_model_engine_process(
            pid=pid,
            comm=comm,
            exe_base=exe_base,
            cmd_str=cmd_full,
            protected_pids=protected_pids,
            ppid_map=ppid_map,
        )

        if pid <= 2 or not cmd_args or not comm:
            protected, reason = True, "Kernel / vanished thread"
        elif is_model:
            protected, reason = True, model_reason
        elif pid in ancestors:
            protected, reason = True, "Vimona / active session"
        elif proc_uid != uid:
            protected, reason = True, "Root / OS service"
        elif (
            low in _CRASH_CRITICAL_EXACT
            or exe_base in _CRASH_CRITICAL_EXACT
            or low.startswith(("kworker", "kswapd", "systemd-", "xdg-desktop-portal", "at-spi", "gsd-", "gnome-session"))
            or "/system.slice/" in cgroup_text
            or "/init.scope" in cgroup_text
        ):
            protected, reason = True, "Desktop / OS core"
        elif "vimona" in cmd_full.lower():
            protected, reason = True, "Vimona UI"
        elif not exe_base and low in _EXEC_WRAPPERS:
            protected, reason = True, "Uncertain launcher process"

        st_ticks = _read_pid_start_ticks(pid)
        out.append(
            ProcessRamUsage(
                pid=pid,
                name=comm or exe_base or str(pid),
                cmd_preview=cmd_str,
                rss_mb=rss_mb,
                uid=proc_uid,
                protected=protected,
                protection_reason=reason,
                anon_rss_mb=anon_mb,
                shared_mb=shared_mb,
                start_ticks=st_ticks,
                exe_path=exe_base,
            )
        )

    out.sort(key=lambda p: p.rss_mb, reverse=True)
    return out


def safe_kill_process(proc: ProcessRamUsage) -> tuple[bool, str]:
    """Terminate a verified non-critical user process via starttime/pidfd safety (`K1`–`K3`)."""
    if proc.protected or proc.uid != _current_uid() or proc.pid in _get_ancestor_pids() or proc.pid <= 2:
        return False, f"Protected process ({proc.protection_reason or 'OS / Model critical'}) cannot be killed."
    return _send_sigterm_verified(
        proc.pid,
        expected_uid=proc.uid,
        expected_start_ticks=proc.start_ticks,
        expected_name=proc.name,
    )


def find_unnecessary_processes(min_rss_mb: float = 4.0) -> list[ProcessRamUsage]:
    """Return non-OS, non-Vimona, non-model processes safe to close to reclaim RAM."""
    return [p for p in scan_all_processes_by_ram(min_rss_mb=min_rss_mb) if not p.protected]


def terminate_unnecessary_processes(
    procs: list[ProcessRamUsage] | None = None,
) -> tuple[int, float]:
    """Close only non-protected unnecessary processes, keeping Vimona & models running."""
    targets = procs if procs is not None else find_unnecessary_processes()
    killed = 0
    freed_mb = 0.0
    for proc in targets:
        ok, _msg = safe_kill_process(proc)
        if ok:
            killed += 1
            freed_mb += proc.rss_mb
    return killed, freed_mb


class InstalledAppsRamWindow(Adw.Window):
    """In-app Fedora RAM Manager with Installed Apps & All Processes (crash-safe kill)."""

    def __init__(
        self,
        parent: Gtk.Window,
        get_ram_summary: Callable[[], tuple[float, float]],
        on_app_closed: Callable[[str, float], None] | None = None,
        initial_tab: str = "apps",
        get_override: Callable[[], bool] | None = None,
        on_toggle_override: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(title="RAM & Process Manager")
        self.set_transient_for(parent)
        self.set_modal(False)
        self.set_default_size(600, 540)
        self.add_css_class("vimona-window")

        self._get_ram_summary = get_ram_summary
        self._on_app_closed = on_app_closed
        self._get_override = get_override
        self._on_toggle_override = on_toggle_override
        self._active_tab = "all" if initial_tab == "all" else "apps"
        self._refresh_timer: int | None = None
        self._hovering_list: bool = False

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.add_css_class("vimona-header")

        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._apps_tab_btn = Gtk.Button(label="Installed Apps")
        self._apps_tab_btn.set_has_frame(False)
        self._apps_tab_btn.add_css_class("grid-layout-pill")
        self._apps_tab_btn.set_cursor_from_name("pointer")
        self._apps_tab_btn.connect("clicked", lambda _b: self.set_tab("apps"))

        self._all_tab_btn = Gtk.Button(label="All Processes")
        self._all_tab_btn.set_has_frame(False)
        self._all_tab_btn.add_css_class("grid-layout-pill")
        self._all_tab_btn.set_cursor_from_name("pointer")
        self._all_tab_btn.connect("clicked", lambda _b: self.set_tab("all"))

        tab_box.append(self._apps_tab_btn)
        tab_box.append(self._all_tab_btn)
        header.pack_start(tab_box)

        refresh_btn = Gtk.Button()
        refresh_btn.set_has_frame(False)
        refresh_btn.add_css_class("mini-action-btn")
        refresh_btn.set_cursor_from_name("pointer")
        refresh_btn.set_label("Refresh")
        refresh_btn.connect("clicked", lambda _b: self.refresh())
        header.pack_end(refresh_btn)

        toolbar_view.add_top_bar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(14)
        root.set_margin_bottom(16)
        root.set_margin_start(18)
        root.set_margin_end(18)

        # RAM overview meter card
        summary_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        summary_card.add_css_class("gallery-notice")

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_row.append(SvgIcon("chip", size_px=15, color=COLORS.accent_primary))
        self._ram_title = Gtk.Label(label="RAM Usage")
        self._ram_title.add_css_class("gallery-card-title")
        self._ram_title.set_xalign(0.0)
        self._ram_title.set_hexpand(True)
        top_row.append(self._ram_title)

        self._ram_value = Gtk.Label(label="0.0 / 0 GB RAM")
        self._ram_value.add_css_class("handoff-mono")
        top_row.append(self._ram_value)
        summary_card.append(top_row)

        self._ram_bar = Gtk.ProgressBar()
        self._ram_bar.add_css_class("handoff-progress")
        summary_card.append(self._ram_bar)

        self._hint_lbl = Gtk.Label()
        self._hint_lbl.add_css_class("gallery-card-meta")
        self._hint_lbl.set_xalign(0.0)
        self._hint_lbl.set_wrap(True)
        summary_card.append(self._hint_lbl)

        if self._on_toggle_override is not None:
            ov_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            ov_row.set_margin_top(4)
            ov_lbl = Gtk.Label(label="Override high-RAM guard (continue generation even near limit)")
            ov_lbl.add_css_class("gallery-card-meta")
            ov_lbl.set_xalign(0.0)
            ov_lbl.set_hexpand(True)
            ov_lbl.set_wrap(True)
            ov_row.append(ov_lbl)

            self._override_btn = Gtk.Button()
            self._override_btn.set_has_frame(False)
            self._override_btn.add_css_class("safe-blur-toggle")
            self._override_btn.set_cursor_from_name("pointer")
            self._override_btn.connect("clicked", self._on_override_clicked)
            ov_row.append(self._override_btn)
            summary_card.append(ov_row)
            self._sync_override_btn()

        clean_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        clean_row.set_margin_top(4)
        clean_lbl = Gtk.Label(
            label="Free maximum RAM (keeps only OS, Vimona & AI model engines)"
        )
        clean_lbl.add_css_class("gallery-card-meta")
        clean_lbl.set_xalign(0.0)
        clean_lbl.set_hexpand(True)
        clean_lbl.set_wrap(True)
        clean_row.append(clean_lbl)

        self._clean_btn = Gtk.Button(label="Close Unnecessary…")
        self._clean_btn.set_has_frame(False)
        self._clean_btn.add_css_class("ram-cut-btn")
        self._clean_btn.set_cursor_from_name("pointer")
        self._clean_btn.connect("clicked", lambda _b: self._prompt_clean_unnecessary())
        clean_row.append(self._clean_btn)
        summary_card.append(clean_row)

        self._clean_revealer = Gtk.Revealer()
        self._clean_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._clean_revealer.set_transition_duration(180)
        self._clean_revealer.set_reveal_child(False)
        self._clean_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._clean_box.set_margin_top(6)
        self._clean_revealer.set_child(self._clean_box)
        summary_card.append(self._clean_revealer)

        root.append(summary_card)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self._list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: setattr(self, "_hovering_list", True))
        motion.connect("leave", lambda *_a: setattr(self, "_hovering_list", False))
        scroll.add_controller(motion)
        scroll.set_child(self._list_box)
        root.append(scroll)

        toolbar_view.set_content(root)
        self.set_content(toolbar_view)

        self.set_tab(self._active_tab)
        self._refresh_timer = GLib.timeout_add(3200, self._on_timer_refresh)
        self.connect("close-request", self._on_close)

    def _sync_override_btn(self) -> None:
        if not hasattr(self, "_override_btn") or self._get_override is None:
            return
        active = self._get_override()
        self._override_btn.set_label("Override: On" if active else "Override: Off")
        if active:
            self._override_btn.add_css_class("active")
        else:
            self._override_btn.remove_css_class("active")

    def _on_override_clicked(self, _b: Gtk.Button) -> None:
        if self._on_toggle_override is None or self._get_override is None:
            return
        self._on_toggle_override(not self._get_override())
        self._sync_override_btn()

    def set_tab(self, tab: str) -> None:
        self._active_tab = "all" if tab == "all" else "apps"
        for btn, key in ((self._apps_tab_btn, "apps"), (self._all_tab_btn, "all")):
            if key == self._active_tab:
                btn.add_css_class("active")
            else:
                btn.remove_css_class("active")
        if self._active_tab == "apps":
            self._hint_lbl.set_text(
                "Installed running desktop apps only — Linux system & background processes are hidden."
            )
        else:
            self._hint_lbl.set_text(
                "All processes ranked highest RAM first. Core OS, desktop & model processes are locked."
            )
        self.refresh()

    def _on_timer_refresh(self) -> bool:
        if not self.get_visible():
            return False
        # Never tear down rows on background timer ticks (stops scroll/hover/click glitches)
        used_gb, total_gb = self._get_ram_summary()
        frac = min(1.0, used_gb / max(0.1, total_gb))
        self._ram_value.set_text(f"RAM {used_gb:.1f} / {total_gb:.0f} GB ({int(round(frac * 100))}%)")
        self._ram_bar.set_fraction(frac)
        self._sync_override_btn()
        return True

    def _prompt_clean_unnecessary(self) -> None:
        targets = find_unnecessary_processes()
        c = self._clean_box.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._clean_box.remove(c)
            c = nxt

        if not targets:
            toast_lbl = Gtk.Label(label="Only OS, Vimona & model processes are active.")
            toast_lbl.add_css_class("gallery-card-meta")
            self._clean_box.append(toast_lbl)
            self._clean_revealer.set_reveal_child(True)
            GLib.timeout_add(2200, lambda: (self._clean_revealer.set_reveal_child(False), False)[1])
            return

        total_mb = sum(p.rss_mb for p in targets)
        mb_txt = f"{total_mb / 1024.0:.2f} GB" if total_mb >= 1024.0 else f"{total_mb:.0f} MB"
        slider = SlideCommitWidget(
            label=f"Slide to allow closing {len(targets)} (~{mb_txt})",
            done_label="Cleaned!",
            width=360,
            height=36,
            radius=18,
            track_color="#24272E",
            handle_color="#EBCB8B",
            success_color="#B44848",
            on_confirm=lambda items=targets: self._execute_clean_unnecessary(items),
        )
        slider.set_hexpand(True)
        self._clean_box.append(slider)

        cancel_b = Gtk.Button(label="Cancel")
        cancel_b.set_has_frame(False)
        cancel_b.add_css_class("mini-action-btn")
        cancel_b.set_cursor_from_name("pointer")
        cancel_b.set_valign(Gtk.Align.CENTER)
        cancel_b.connect("clicked", lambda _b: self._clean_revealer.set_reveal_child(False))
        self._clean_box.append(cancel_b)
        self._clean_revealer.set_reveal_child(True)

    def _execute_clean_unnecessary(self, targets: list[ProcessRamUsage]) -> None:
        killed, freed_mb = terminate_unnecessary_processes(targets)
        self._clean_revealer.set_reveal_child(False)
        if killed > 0 and self._on_app_closed is not None:
            self._on_app_closed(f"{killed} unnecessary process(es)", freed_mb)
        GLib.timeout_add(450, lambda: (self.refresh(), False)[1])

    def _on_close(self, _w: Gtk.Window) -> bool:
        if self._refresh_timer is not None:
            GLib.source_remove(self._refresh_timer)
            self._refresh_timer = None
        return False

    def refresh(self) -> None:
        used_gb, total_gb = self._get_ram_summary()
        frac = min(1.0, used_gb / max(0.1, total_gb))
        self._ram_value.set_text(f"RAM {used_gb:.1f} / {total_gb:.0f} GB ({int(round(frac * 100))}%)")
        self._ram_bar.set_fraction(frac)
        self._sync_override_btn()

        c = self._list_box.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._list_box.remove(c)
            c = nxt

        if self._active_tab == "all":
            self._render_all_processes()
        else:
            self._render_installed_apps()

    def _render_installed_apps(self) -> None:
        apps = scan_installed_running_apps()
        if not apps:
            empty = Gtk.Label(label="No heavy user-installed applications are currently running.")
            empty.add_css_class("gallery-card-meta")
            empty.set_margin_top(28)
            empty.set_justify(Gtk.Justification.CENTER)
            self._list_box.append(empty)
            return

        for app in apps:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add_css_class("ram-app-row")

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            info.set_hexpand(True)

            name_lbl = Gtk.Label(label=app.name)
            name_lbl.add_css_class("gallery-card-title")
            name_lbl.set_xalign(0.0)
            name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            info.append(name_lbl)

            ram_str = f"{app.rss_mb / 1024.0:.2f} GB" if app.rss_mb >= 1024.0 else f"{app.rss_mb:.0f} MB"
            priv_str = (
                f"{app.anon_rss_mb:.0f} MB priv · {app.shared_mb:.0f} MB shared"
                if app.shared_mb > 1.0
                else "private RSS"
            )
            proc_txt = "1 window/process" if len(app.pids) == 1 else f"{len(app.pids)} processes"
            meta_lbl = Gtk.Label(label=f"{ram_str} RSS ({priv_str}) · {proc_txt} · {app.category}")
            meta_lbl.add_css_class("gallery-card-meta")
            meta_lbl.set_xalign(0.0)
            info.append(meta_lbl)
            row.append(info)

            close_btn = Gtk.Button()
            close_btn.set_has_frame(False)
            close_btn.add_css_class("ram-cut-btn")
            close_btn.set_cursor_from_name("pointer")
            close_btn.set_valign(Gtk.Align.CENTER)
            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            btn_box.append(SvgIcon("close", size_px=12, color="#FFFFFF"))
            btn_box.append(Gtk.Label(label="End App"))
            close_btn.set_child(btn_box)
            close_btn.connect("clicked", lambda _b, item=app: self._on_close_app_clicked(item))
            row.append(close_btn)

            self._list_box.append(row)

    def _render_all_processes(self) -> None:
        procs = scan_all_processes_by_ram(min_rss_mb=4.0)
        if not procs:
            empty = Gtk.Label(label="No processes above 4 MB found.")
            empty.add_css_class("gallery-card-meta")
            empty.set_margin_top(28)
            self._list_box.append(empty)
            return

        for idx, proc in enumerate(procs[:45]):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add_css_class("ram-app-row")

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            info.set_hexpand(True)

            rank_prefix = "#1 Most RAM · " if idx == 0 else ""
            title_lbl = Gtk.Label(label=f"{rank_prefix}{proc.name} (PID {proc.pid})")
            title_lbl.add_css_class("gallery-card-title")
            title_lbl.set_xalign(0.0)
            title_lbl.set_max_width_chars(38)
            title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            info.append(title_lbl)

            ram_str = f"{proc.rss_mb / 1024.0:.2f} GB" if proc.rss_mb >= 1024.0 else f"{proc.rss_mb:.0f} MB"
            priv_tag = (
                f" ({proc.anon_rss_mb:.0f}M priv / {proc.shared_mb:.0f}M shm)"
                if proc.shared_mb > 1.0
                else ""
            )
            sub = f"{ram_str} RSS{priv_tag} · {proc.cmd_preview}"
            meta_lbl = Gtk.Label(label=sub)
            meta_lbl.add_css_class("gallery-card-meta")
            meta_lbl.set_xalign(0.0)
            meta_lbl.set_max_width_chars(46)
            meta_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            info.append(meta_lbl)
            row.append(info)

            if proc.protected:
                prot_lbl = Gtk.Label(label=proc.protection_reason or "Protected")
                prot_lbl.add_css_class("ram-protected-tag")
                prot_lbl.set_valign(Gtk.Align.CENTER)
                row.append(prot_lbl)
            else:
                kill_btn = Gtk.Button()
                kill_btn.set_has_frame(False)
                kill_btn.add_css_class("ram-cut-btn")
                kill_btn.set_cursor_from_name("pointer")
                kill_btn.set_valign(Gtk.Align.CENTER)
                kb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
                kb.append(SvgIcon("close", size_px=12, color="#FFFFFF"))
                kb.append(Gtk.Label(label="Kill Process"))
                kill_btn.set_child(kb)
                kill_btn.connect("clicked", lambda _b, item=proc: self._on_kill_process_clicked(item))
                row.append(kill_btn)

            self._list_box.append(row)

    def _on_close_app_clicked(self, app: InstalledAppUsage) -> None:
        ok, count = close_installed_app(app)
        if ok:
            self._hint_lbl.set_text(
                f"Sent graceful terminate signal to {count} process(es) of {app.name}; verifying reclaim…"
            )
            if self._on_app_closed is not None:
                self._on_app_closed(app.name, app.rss_mb)
            GLib.timeout_add(550, lambda: (self.refresh(), False)[1])
        else:
            self._hint_lbl.set_text(
                f"Skipped {app.name}: protected, already exited, or PID changed since scan."
            )
            GLib.timeout_add(350, lambda: (self.refresh(), False)[1])

    def _on_kill_process_clicked(self, proc: ProcessRamUsage) -> None:
        ok, msg = safe_kill_process(proc)
        self._hint_lbl.set_text(msg)
        if ok:
            if self._on_app_closed is not None:
                self._on_app_closed(f"{proc.name} (PID {proc.pid})", proc.rss_mb)
            GLib.timeout_add(450, lambda: (self.refresh(), False)[1])
        else:
            GLib.timeout_add(350, lambda: (self.refresh(), False)[1])
