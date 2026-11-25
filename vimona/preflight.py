"""Startup platform & dependency preflight checks with graceful degradation (`M1`–`M10`)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any, Literal

from .config import VimonaRuntimeConfig, get_config


CheckStatus = Literal["ok", "warn", "error"]


@dataclass
class PreflightCheckItem:
    name: str
    status: CheckStatus
    detail: str
    required: bool = False


@dataclass
class PreflightReport:
    os_platform: str
    os_supported: bool
    display_backend: str
    checks: list[PreflightCheckItem] = field(default_factory=list)
    capabilities: dict[str, bool] = field(default_factory=dict)
    can_start_gui: bool = True
    degraded_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "os_platform": self.os_platform,
            "os_supported": self.os_supported,
            "display_backend": self.display_backend,
            "checks": [asdict(c) for c in self.checks],
            "capabilities": dict(self.capabilities),
            "can_start_gui": self.can_start_gui,
            "degraded_reasons": list(self.degraded_reasons),
        }

    def format_text(self) -> str:
        lines = [
            f"Vimona Preflight Report ({self.os_platform}, display={self.display_backend})",
            f"GUI Ready: {'YES' if self.can_start_gui else 'NO'}",
            "Capabilities: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in sorted(self.capabilities.items())),
        ]
        for chk in self.checks:
            tag = chk.status.upper()
            req = "required" if chk.required else "optional"
            lines.append(f"  [{tag:5s}] {chk.name} ({req}): {chk.detail}")
        if self.degraded_reasons:
            lines.append("Degraded features:")
            for reason in self.degraded_reasons:
                lines.append(f"  - {reason}")
        return "\n".join(lines)


def _check_gtk_stack() -> tuple[bool, str]:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401

        gtk_ver = f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"
        adw_ver = f"{Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}"
        return (True, f"GTK {gtk_ver}, Libadwaita {adw_ver}")
    except Exception as exc:
        return (False, f"GTK4/Libadwaita unavailable: {exc}")


def _check_packaged_assets() -> tuple[bool, str]:
    pkg_dir = Path(__file__).resolve().parent
    weights = pkg_dir / "safety_ml_weights.npz"
    skills_dir = pkg_dir / "skills" / "packs"
    pack_count = len(list(skills_dir.glob("*.json"))) if skills_dir.is_dir() else 0
    if not weights.is_file():
        return (False, f"Missing SafeBlur weights: {weights}")
    if pack_count == 0:
        return (False, f"No built-in skill packs found in {skills_dir}")
    return (True, f"Packaged SafeBlur weights + {pack_count} skill pack(s)")


def run_preflight_checks(
    cfg: VimonaRuntimeConfig | None = None,
    *,
    require_display: bool = False,
) -> PreflightReport:
    """Inspect OS, display, GTK4, Playwright/Firefox, engines, RAM/GPU, & package data (`M1`–`M10`)."""
    conf = cfg or get_config()
    sys_name = sys.platform.lower()
    os_supported = sys_name.startswith("linux")
    os_label = f"{platform.system()} {platform.release()} ({platform.machine()})"

    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    x11 = bool(os.environ.get("DISPLAY"))
    display_backend = "wayland" if wayland else ("x11" if x11 else "headless")

    checks: list[PreflightCheckItem] = []
    degraded: list[str] = []

    # M1: OS support check
    if os_supported:
        checks.append(PreflightCheckItem("os_platform", "ok", os_label, required=True))
    else:
        checks.append(
            PreflightCheckItem(
                "os_platform",
                "error",
                f"Unsupported OS '{os_label}'; unshare network isolation & /dev/shm require Linux.",
                required=True,
            )
        )
        degraded.append("Linux-only netns air-gap isolation unavailable on this OS.")

    # M2: Display check
    if wayland or x11:
        checks.append(PreflightCheckItem("display_server", "ok", f"Active {display_backend} display", required=require_display))
    else:
        st: CheckStatus = "error" if require_display else "warn"
        checks.append(PreflightCheckItem("display_server", st, "No WAYLAND_DISPLAY or DISPLAY set", required=require_display))

    # M3: GTK4 / Libadwaita / Python runtime
    py_ok = sys.version_info >= (3, 11)
    checks.append(
        PreflightCheckItem(
            "python_version",
            "ok" if py_ok else "error",
            f"Python {platform.python_version()} (requires >= 3.11)",
            required=True,
        )
    )
    gtk_ok, gtk_detail = _check_gtk_stack()
    checks.append(PreflightCheckItem("gtk4_libadwaita", "ok" if gtk_ok else "error", gtk_detail, required=True))

    # M10: Packaged weights & skill assets
    assets_ok, assets_detail = _check_packaged_assets()
    checks.append(PreflightCheckItem("packaged_assets", "ok" if assets_ok else "warn", assets_detail, required=False))
    if not assets_ok:
        degraded.append("SafeBlur or built-in skill packs missing; safety classifier stays fail-closed.")

    # M4: Playwright & Firefox (either current interpreter OR comfy_dir worker venv)
    pw_spec = importlib.util.find_spec("playwright")
    venv_py = conf.comfy_dir / "venv/bin/python"
    pw_source = "current interpreter" if pw_spec is not None else ""
    if pw_spec is None and venv_py.is_file():
        try:
            chk = subprocess.run(
                [str(venv_py), "-c", "import playwright"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if chk.returncode == 0:
                pw_source = f"worker venv ({venv_py})"
        except Exception:
            pass
    ff_bin = shutil.which("firefox")
    pw_ok = bool(pw_source)
    checks.append(
        PreflightCheckItem(
            "playwright",
            "ok" if pw_ok else "warn",
            f"playwright available via {pw_source}" if pw_ok else "playwright Python package missing",
        )
    )
    if not pw_ok:
        degraded.append("Zero-trace web research disabled (playwright missing).")
    if ff_bin:
        checks.append(PreflightCheckItem("firefox_binary", "ok", ff_bin))
    else:
        checks.append(PreflightCheckItem("firefox_binary", "warn", "system firefox not in PATH (using playwright cache)"))

    # M5 & M6: Local inference & media tools
    llama_ok = conf.llama_server_bin.is_file()
    sd_ok = conf.sd_server_bin.is_file()
    comfy_ok = (conf.comfy_dir / "main.py").is_file()
    ffmpeg_ok = bool(shutil.which(conf.ffmpeg_bin))

    checks.append(PreflightCheckItem("llama_server", "ok" if llama_ok else "warn", str(conf.llama_server_bin)))
    if not llama_ok:
        degraded.append("Local LLM chat engine missing (llama-server not found).")
    checks.append(PreflightCheckItem("sd_server", "ok" if sd_ok else "warn", str(conf.sd_server_bin)))
    checks.append(PreflightCheckItem("comfyui", "ok" if comfy_ok else "warn", str(conf.comfy_dir / "main.py")))
    if not (sd_ok or comfy_ok):
        degraded.append("Offline image/video engines missing (install sd-server or ComfyUI).")
    checks.append(PreflightCheckItem("ffmpeg", "ok" if ffmpeg_ok else "warn", str(conf.ffmpeg_bin)))

    # M7: Netns capability & hardware memory quick probe
    try:
        from .airgap import probe_unshare_netns_capability
        netns_ok, netns_msg = probe_unshare_netns_capability()
    except Exception as exc:
        netns_ok, netns_msg = (False, str(exc))
    checks.append(PreflightCheckItem("netns_isolation", "ok" if netns_ok else "warn", netns_msg))
    if not netns_ok:
        degraded.append("Fail-closed air-gapped ComfyUI/media isolation unavailable (`unshare -r -n` probe failed).")

    # M8: Custom env overrides visibility
    env_keys = [k for k in sorted(os.environ) if k.startswith("VIMONA_")]
    if env_keys:
        checks.append(PreflightCheckItem("env_overrides", "ok", ", ".join(env_keys)))

    can_start_gui = py_ok and gtk_ok and (os_supported or not require_display) and ((wayland or x11) or not require_display)
    capabilities = {
        "gui": can_start_gui,
        "text_llm": llama_ok,
        "image_fast_vulkan": sd_ok,
        "comfyui": comfy_ok,
        "video_ffmpeg": ffmpeg_ok,
        "web_research": pw_ok,
        "airgap_netns": netns_ok,
        "safeblur_assets": assets_ok,
    }
    return PreflightReport(
        os_platform=os_label,
        os_supported=os_supported,
        display_backend=display_backend,
        checks=checks,
        capabilities=capabilities,
        can_start_gui=can_start_gui,
        degraded_reasons=degraded,
    )
