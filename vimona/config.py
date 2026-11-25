"""Centralized runtime path & environment configuration for Vimona (`L12`, `M8`)."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


def _env_path(env_key: str, default: str) -> Path:
    raw = os.environ.get(env_key, "").strip()
    return Path(raw or default).expanduser()


def _resolve_bin(env_key: str, default_path: str, cmd_fallback: str) -> Path:
    raw = os.environ.get(env_key, "").strip()
    if raw:
        return Path(raw).expanduser()
    candidate = Path(default_path).expanduser()
    if candidate.is_file():
        return candidate
    which = shutil.which(cmd_fallback)
    return Path(which) if which else candidate


@dataclass(frozen=True)
class VimonaRuntimeConfig:
    """Resolved runtime paths and defaults, overridable via `VIMONA_*` env vars."""

    state_dir: Path
    models_dir: Path
    llm_dir: Path
    comfy_dir: Path
    llama_server_bin: Path
    sd_server_bin: Path
    sd_cli_bin: Path
    ffmpeg_bin: str
    ffprobe_bin: str
    python_bin: str
    default_llm_path: Path
    default_safesearch: str


def build_runtime_config() -> VimonaRuntimeConfig:
    state_dir = _env_path("VIMONA_STATE_DIR", "~/.local/share/vimona")
    models_dir = _env_path("VIMONA_MODELS_DIR", str(state_dir / "models"))
    llm_dir = _env_path("VIMONA_LLM_DIR", "~/models/llm")
    comfy_dir = _env_path("VIMONA_COMFY_DIR", "~/comfyui")
    llama_bin = _resolve_bin("VIMONA_LLAMA_BIN", "~/llama.cpp/build/bin/llama-server", "llama-server")
    sd_bin = _resolve_bin("VIMONA_SD_BIN", "~/stable-diffusion.cpp/build/bin/sd-server", "sd-server")
    sd_cli = _resolve_bin("VIMONA_SD_CLI_BIN", "~/stable-diffusion.cpp/build/bin/sd-cli", "sd-cli")
    ffmpeg_bin = os.environ.get("VIMONA_FFMPEG_BIN", "").strip() or (shutil.which("ffmpeg") or "ffmpeg")
    ffprobe_bin = os.environ.get("VIMONA_FFPROBE_BIN", "").strip() or (shutil.which("ffprobe") or "ffprobe")
    py_bin = os.environ.get("VIMONA_PYTHON_BIN", "").strip() or sys.executable
    default_llm = _env_path("VIMONA_DEFAULT_LLM", str(llm_dir / "light-uncensored.gguf"))
    safesearch = (os.environ.get("VIMONA_SAFESEARCH", "moderate") or "moderate").strip().lower()
    if safesearch not in ("strict", "moderate", "off"):
        safesearch = "moderate"
    return VimonaRuntimeConfig(
        state_dir=state_dir,
        models_dir=models_dir,
        llm_dir=llm_dir,
        comfy_dir=comfy_dir,
        llama_server_bin=llama_bin,
        sd_server_bin=sd_bin,
        sd_cli_bin=sd_cli,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        python_bin=py_bin,
        default_llm_path=default_llm,
        default_safesearch=safesearch,
    )


_CONFIG: VimonaRuntimeConfig | None = None


def get_config() -> VimonaRuntimeConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = build_runtime_config()
    return _CONFIG


def reload_config() -> VimonaRuntimeConfig:
    global _CONFIG
    _CONFIG = build_runtime_config()
    return _CONFIG
