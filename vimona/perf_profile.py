"""Quiet/Eco vs. Turbo hardware performance profile, CPU thread caps, and OS priority helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import threading
from typing import Callable, Literal

PerformanceMode = Literal["eco", "turbo"]

STATE_DIR = Path("~/.local/share/vimona").expanduser()
PREFS_FILE = STATE_DIR / "perf_profile.json"

_LOCK = threading.RLock()
_CURRENT_MODE: PerformanceMode = "turbo"
_COOLDOWN_SECONDS: float = 7.0
_LISTENERS: list[Callable[[PerformanceMode], None]] = []


def _load_saved_prefs() -> None:
    global _CURRENT_MODE, _COOLDOWN_SECONDS
    try:
        if PREFS_FILE.is_file():
            raw = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                m = str(raw.get("mode") or "turbo").lower()
                if m in ("eco", "turbo"):
                    _CURRENT_MODE = m  # type: ignore[assignment]
                cd = raw.get("cooldown_s")
                if isinstance(cd, (int, float)):
                    _COOLDOWN_SECONDS = max(5.0, min(10.0, float(cd)))
    except Exception:
        pass


def _save_prefs() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PREFS_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "mode": _CURRENT_MODE,
                    "cooldown_s": _COOLDOWN_SECONDS,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(PREFS_FILE)
    except Exception:
        pass


_load_saved_prefs()


def get_performance_mode() -> PerformanceMode:
    with _LOCK:
        return _CURRENT_MODE


def set_performance_mode(mode: str) -> PerformanceMode:
    global _CURRENT_MODE
    clean: PerformanceMode = "eco" if str(mode).lower() in ("eco", "quiet") else "turbo"
    with _LOCK:
        changed = _CURRENT_MODE != clean
        _CURRENT_MODE = clean
        _save_prefs()
        listeners = list(_LISTENERS)
    if changed:
        for cb in listeners:
            try:
                cb(clean)
            except Exception:
                pass
    return clean


def register_profile_listener(cb: Callable[[PerformanceMode], None]) -> None:
    with _LOCK:
        if cb not in _LISTENERS:
            _LISTENERS.append(cb)


def get_thermal_cooldown_s() -> float:
    with _LOCK:
        return float(_COOLDOWN_SECONDS) if _CURRENT_MODE == "eco" else 0.0


def set_thermal_cooldown_s(seconds: float) -> float:
    global _COOLDOWN_SECONDS
    with _LOCK:
        _COOLDOWN_SECONDS = max(5.0, min(10.0, float(seconds)))
        _save_prefs()
        return _COOLDOWN_SECONDS


def get_worker_thread_cap() -> int:
    total = os.cpu_count() or 4
    with _LOCK:
        if _CURRENT_MODE == "eco":
            return max(1, total // 2)
        return max(1, total)


def is_eco_mode() -> bool:
    return get_performance_mode() == "eco"


def get_footer_telemetry_label() -> str:
    return "Vulkan Eco · Thermal Cap" if is_eco_mode() else "Vulkan Fast · Turbo"


def get_worker_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    cap = str(get_worker_thread_cap())
    if is_eco_mode():
        env["OMP_NUM_THREADS"] = cap
        env["OPENBLAS_NUM_THREADS"] = cap
        env["MKL_NUM_THREADS"] = cap
        env["NUMEXPR_NUM_THREADS"] = cap
        env["OPENCV_THREAD_COUNT"] = cap
    else:
        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OPENCV_THREAD_COUNT"):
            env.pop(k, None)
    return env


def apply_auxiliary_thread_limit() -> None:
    """Cap OpenCV auxiliary threads in-process when Quiet/Eco mode is active."""
    try:
        import cv2
        cv2.setNumThreads(get_worker_thread_cap())
    except Exception:
        pass


def wrap_worker_cmd_with_priority(cmd: list[str]) -> tuple[list[str], int]:
    """Prefix low-priority Linux nice/ionice wrappers when in Eco mode; returns (cmd, popen_flags)."""
    if not is_eco_mode():
        return (list(cmd), 0)
    if sys.platform.startswith("win"):
        below_normal = getattr(__import__("subprocess"), "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        return (list(cmd), int(below_normal))

    prefix: list[str] = []
    ionice_bin = shutil.which("ionice")
    nice_bin = shutil.which("nice")
    if ionice_bin:
        prefix.extend([ionice_bin, "-c", "3"])
    if nice_bin:
        prefix.extend([nice_bin, "-n", "12"])
    return (prefix + list(cmd), 0)
