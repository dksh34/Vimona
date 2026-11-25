"""Hardware-aware Hugging Face catalog, live Hub discovery, matcher, and downloader."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

MODELS_STATE_FILE = Path("~/.local/share/vimona/models_config.json").expanduser()
LOCAL_MODELS_DIR = Path("~/.local/share/vimona/models").expanduser()

_HF_META_CACHE: dict[str, dict[str, Any]] = {}
_HF_FILE_TREE_CACHE: dict[str, list[dict[str, Any]]] = {}
_HF_SEARCH_CACHE: dict[tuple[str, str, str], list["HFModelSpec"]] = {}
_HW_STATIC_CACHE: tuple[float, tuple[str, int, str, float, str, bool, float, int, tuple[str, ...]]] | None = None
_ENGINE_CAPS_CACHE: tuple[float, dict[str, bool]] | None = None
_DYNAMIC_HF_MODELS: dict[str, "HFModelSpec"] = {}
_CACHE_LOCK = threading.Lock()
_DOWNLOAD_LOCK = threading.Lock()
_ACTIVE_DOWNLOADS: set[str] = set()


class ModelDownloadError(RuntimeError):
    """Raised when a model download or integrity check fails (`C1`–`C12`)."""


@dataclass
class DownloadConfig:
    """Configurable download guardrails (`C8`, `C9`)."""

    max_file_bytes: int = 45 * 1024 ** 3
    max_mb_per_sec: float = 0.0  # 0 = unlimited
    connect_timeout_s: float = 30.0
    max_redirects: int = 6
    require_sha256_when_known: bool = True
    allowed_https_suffixes: tuple[str, ...] = (
        "huggingface.co",
        ".huggingface.co",
        ".hf.co",
    )


_DOWNLOAD_CONFIG = DownloadConfig()


def get_download_config() -> DownloadConfig:
    return DownloadConfig(**vars(_DOWNLOAD_CONFIG))


def configure_downloads(
    *,
    max_file_bytes: int | None = None,
    max_mb_per_sec: float | None = None,
    connect_timeout_s: float | None = None,
    require_sha256_when_known: bool | None = None,
) -> DownloadConfig:
    if max_file_bytes is not None:
        _DOWNLOAD_CONFIG.max_file_bytes = max(1024 * 1024, int(max_file_bytes))
    if max_mb_per_sec is not None:
        _DOWNLOAD_CONFIG.max_mb_per_sec = max(0.0, float(max_mb_per_sec))
    if connect_timeout_s is not None:
        _DOWNLOAD_CONFIG.connect_timeout_s = max(2.0, float(connect_timeout_s))
    if require_sha256_when_known is not None:
        _DOWNLOAD_CONFIG.require_sha256_when_known = bool(require_sha256_when_known)
    return get_download_config()


@dataclass(frozen=True)
class HardwareProfile:
    cpu_name: str
    cpu_cores: int
    total_ram_gb: float
    avail_ram_gb: float
    gpu_name: str
    vram_gb: float
    backend: str  # "cuda" | "vulkan" | "cpu"
    unified_memory: bool
    gtt_gb: float = 0.0
    gpu_count: int = 1
    gpu_devices: tuple[str, ...] = ()
    vulkan_verified: bool = False
    cuda_verified: bool = False
    cpu_fallback_only: bool = False

    @property
    def os_overhead_gb(self) -> float:
        """Dynamic estimate of active OS + desktop memory footprint (`D2`)."""
        observed_used = max(0.0, self.total_ram_gb - self.avail_ram_gb)
        return round(max(1.5, min(6.0, observed_used if observed_used > 0.5 else 2.2)), 1)

    @property
    def effective_ram_gb(self) -> float:
        """Usable free RAM right now (not conflating total RAM with available memory, `D1`)."""
        return round(max(1.0, self.avail_ram_gb), 1)

    @property
    def dedicated_vram_gb(self) -> float:
        if self.backend == "cpu":
            return 0.0
        if not self.unified_memory:
            return round(self.vram_gb, 1)
        return round(min(self.vram_gb, self.total_ram_gb), 1)

    @property
    def shared_gtt_gb(self) -> float:
        """Portion of GPU pool backed by shared system RAM (never double-counted, `D7`, `D8`)."""
        if self.backend != "vulkan" or not self.unified_memory:
            return 0.0
        raw_gtt = self.gtt_gb if self.gtt_gb > 0 else max(0.0, self.avail_ram_gb * 0.55)
        capped = min(raw_gtt, max(0.0, self.avail_ram_gb - 0.5))
        return round(max(0.0, capped), 1)

    @property
    def gpu_pool_gb(self) -> float:
        """Conservative directly addressable GPU memory without double-counting shared RAM (`D7`, `D8`)."""
        if self.backend == "cuda" and not self.unified_memory:
            return round(self.vram_gb, 1)
        if self.backend == "vulkan":
            if not self.unified_memory and self.vram_gb >= 4.0:
                return round(self.vram_gb, 1)
            combined = self.dedicated_vram_gb + self.shared_gtt_gb
            # Unified APU pool can never exceed real available system memory + dedicated carveout
            return round(min(combined, max(self.vram_gb, self.avail_ram_gb)), 1)
        return 0.0

    @property
    def tier(self) -> str:
        if self.total_ram_gb >= 24.0 and (
            self.gpu_pool_gb >= 15.0 or (self.unified_memory and self.total_ram_gb >= 28.0 and self.avail_ram_gb >= 18.0)
        ):
            return "workstation"
        if self.total_ram_gb < 11.0 or self.avail_ram_gb < 5.0 or (self.backend == "cpu" and self.total_ram_gb < 16.0):
            return "low"
        return "balanced"

    @property
    def summary_label(self) -> str:
        gpu = self.gpu_name or "Integrated GPU"
        if self.backend == "cuda" and self.vram_gb > 0:
            extra = f" ×{self.gpu_count}" if self.gpu_count > 1 else ""
            return f"{gpu}{extra} · {self.vram_gb:.1f} GB VRAM · {self.avail_ram_gb:.1f}/{self.total_ram_gb:.1f} GB RAM · CUDA"
        if self.backend == "vulkan" and self.unified_memory:
            return f"{gpu} · {self.gpu_pool_gb:.1f} GB Pool · {self.avail_ram_gb:.1f}/{self.total_ram_gb:.1f} GB RAM · VULKAN"
        return f"{gpu} · {self.avail_ram_gb:.1f}/{self.total_ram_gb:.1f} GB RAM · {self.backend.upper()}"


@dataclass(frozen=True)
class HFModelSpec:
    id: str
    name: str
    category: str  # "text" | "image" | "video"
    hf_repo: str
    hf_file: str
    size_gb: float
    min_ram_gb: float
    quant: str
    summary: str
    tags: tuple[str, ...] = ()
    backends: tuple[str, ...] = ("cuda", "vulkan", "cpu")
    min_gpu_pool_gb: float = 0.0
    max_ram_gb: float | None = None
    tuning_note: str = ""
    sha256: str = ""
    expected_bytes: int = 0

    @property
    def hf_url(self) -> str:
        return f"https://huggingface.co/{self.hf_repo}"

    @property
    def download_url(self) -> str:
        return f"https://huggingface.co/{self.hf_repo}/resolve/main/{self.hf_file}?download=true"


CURATED_HF_MODELS: tuple[HFModelSpec, ...] = (
    # --- Text Models: Every Tier (1.5B -> 70B) ---
    HFModelSpec(
        id="hf_qwen25_1_5b_q4",
        name="Qwen 2.5 1.5B Instruct",
        category="text",
        hf_repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        hf_file="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        size_gb=1.0,
        min_ram_gb=4.0,
        max_ram_gb=10.5,
        quant="Q4_K_M GGUF",
        summary="Ultra-tiny 1.0 GB engine tuned strictly for 4–8 GB netbooks & CPUs",
        tags=("ultra-light", "fast"),
        tuning_note="Nano 1.5B",
    ),
    HFModelSpec(
        id="hf_llama32_3b_q4",
        name="Llama 3.2 3B Instruct",
        category="text",
        hf_repo="bartowski/Llama-3.2-3B-Instruct-GGUF",
        hf_file="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        size_gb=1.9,
        min_ram_gb=4.5,
        max_ram_gb=11.5,
        quant="Q4_K_M GGUF",
        summary="Featherweight 1.9 GB assistant for 8 GB laptops & CPU-only mode",
        tags=("ultra-light", "chat"),
        tuning_note="Low-RAM 3B",
    ),
    HFModelSpec(
        id="hf_phi4_mini_q4",
        name="Phi-4 Mini Instruct",
        category="text",
        hf_repo="unsloth/Phi-4-mini-instruct-GGUF",
        hf_file="Phi-4-mini-instruct-Q4_K_M.gguf",
        size_gb=2.3,
        min_ram_gb=5.5,
        max_ram_gb=18.0,
        quant="Q4_K_M GGUF",
        summary="Compact 3.8B logic & tool-calling model tuned for constrained RAM",
        tags=("ultra-light", "reasoning"),
        tuning_note="Compact Phi-4",
    ),
    HFModelSpec(
        id="hf_qwen3_4b_q4",
        name="Qwen 3 4B",
        category="text",
        hf_repo="Qwen/Qwen3-4B-GGUF",
        hf_file="Qwen3-4B-Q4_K_M.gguf",
        size_gb=2.3,
        min_ram_gb=5.5,
        max_ram_gb=18.0,
        quant="Q4_K_M GGUF",
        summary="Fast thinking & structured prompting in a 2.3 GB laptop footprint",
        tags=("ultra-light", "eco", "reasoning"),
        tuning_note="Eco 4B",
    ),
    HFModelSpec(
        id="hf_gemma3_4b_q4",
        name="Gemma 3 4B IT",
        category="text",
        hf_repo="bartowski/google_gemma-3-4b-it-GGUF",
        hf_file="google_gemma-3-4b-it-Q4_K_M.gguf",
        size_gb=2.5,
        min_ram_gb=6.0,
        max_ram_gb=20.0,
        quant="Q4_K_M GGUF",
        summary="Ultra-light 4B Eco footprint for 8–16 GB laptops",
        tags=("ultra-light", "eco"),
        tuning_note="Eco 4B",
    ),
    HFModelSpec(
        id="hf_qwen25_3b_q6",
        name="Qwen 2.5 3B Instruct",
        category="text",
        hf_repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
        hf_file="qwen2.5-3b-instruct-q6_k.gguf",
        size_gb=2.6,
        min_ram_gb=5.5,
        max_ram_gb=11.5,
        quant="Q6_K GGUF",
        summary="High-precision 3B engine tuned strictly for <= 11 GB RAM systems",
        tags=("ultra-light", "fast"),
        tuning_note="Low-RAM 3B",
    ),
    HFModelSpec(
        id="hf_deepseek_r1_7b_q4",
        name="DeepSeek-R1 Distill Qwen 7B",
        category="text",
        hf_repo="bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        hf_file="DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf",
        size_gb=4.4,
        min_ram_gb=10.5,
        quant="Q4_K_M GGUF",
        summary="Step-by-step chain-of-thought reasoning & math on 14–16 GB RAM",
        tags=("reasoning",),
        tuning_note="Reasoning 7B",
    ),
    HFModelSpec(
        id="hf_qwen25_7b_q4",
        name="Qwen 2.5 7B Instruct",
        category="text",
        hf_repo="bartowski/Qwen2.5-7B-Instruct-GGUF",
        hf_file="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        size_gb=4.7,
        min_ram_gb=11.0,
        quant="Q4_K_M GGUF",
        summary="Balanced reasoning, coding & tool use tuned for 14–16 GB RAM",
        tags=("recommended", "coding", "chat"),
        tuning_note="Tuned 7B Q4",
    ),
    HFModelSpec(
        id="hf_qwen25_coder_7b_q4",
        name="Qwen 2.5 Coder 7B",
        category="text",
        hf_repo="bartowski/Qwen2.5-Coder-7B-Instruct-GGUF",
        hf_file="Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
        size_gb=4.7,
        min_ram_gb=11.0,
        quant="Q4_K_M GGUF",
        summary="High-accuracy Python, GTK4, SQL & systems debugging",
        tags=("coding",),
        tuning_note="Tuned 7B Coder",
    ),
    HFModelSpec(
        id="hf_llama31_8b_q4",
        name="Llama 3.1 8B Instruct",
        category="text",
        hf_repo="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        hf_file="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        size_gb=4.9,
        min_ram_gb=11.5,
        quant="Q4_K_M GGUF",
        summary="Fast fluent prose & general workstation dialogue",
        tags=("chat", "writing"),
        tuning_note="Tuned 8B Q4",
    ),
    HFModelSpec(
        id="hf_qwen3_8b_q4",
        name="Qwen 3 8B",
        category="text",
        hf_repo="Qwen/Qwen3-8B-GGUF",
        hf_file="Qwen3-8B-Q4_K_M.gguf",
        size_gb=5.0,
        min_ram_gb=11.5,
        quant="Q4_K_M GGUF",
        summary="Crisp structured thinking & multi-turn instruction accuracy",
        tags=("recommended", "reasoning", "chat"),
        tuning_note="Tuned 8B Q4",
    ),
    HFModelSpec(
        id="hf_mistral_nemo_12b_q4",
        name="Mistral Nemo 12B Instruct",
        category="text",
        hf_repo="bartowski/Mistral-Nemo-Instruct-2407-GGUF",
        hf_file="Mistral-Nemo-Instruct-2407-Q4_0.gguf",
        size_gb=6.6,
        min_ram_gb=14.0,
        min_gpu_pool_gb=7.5,
        quant="Q4_0 GGUF",
        summary="Long-context 12B prose & multilingual synthesis",
        tags=("writing", "chat"),
        backends=("cuda", "vulkan"),
        tuning_note="Balanced 12B",
    ),
    HFModelSpec(
        id="hf_deepseek_r1_14b_q4",
        name="DeepSeek-R1 Distill Qwen 14B",
        category="text",
        hf_repo="bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF",
        hf_file="DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
        size_gb=8.4,
        min_ram_gb=20.0,
        min_gpu_pool_gb=12.0,
        quant="Q4_K_M GGUF",
        summary="Deep workstation analytical reasoning (needs 20+ GB RAM/VRAM)",
        tags=("heavy", "reasoning"),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation 14B",
    ),
    HFModelSpec(
        id="hf_phi4_14b_q4",
        name="Phi-4 14B",
        category="text",
        hf_repo="bartowski/phi-4-GGUF",
        hf_file="phi-4-Q4_K_M.gguf",
        size_gb=8.4,
        min_ram_gb=20.0,
        min_gpu_pool_gb=12.0,
        quant="Q4_K_M GGUF",
        summary="High-density STEM & coding precision for 24+ GB workstations",
        tags=("heavy", "coding"),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation 14B",
    ),
    HFModelSpec(
        id="hf_qwen25_14b_q4",
        name="Qwen 2.5 14B Instruct",
        category="text",
        hf_repo="bartowski/Qwen2.5-14B-Instruct-GGUF",
        hf_file="Qwen2.5-14B-Instruct-Q4_K_M.gguf",
        size_gb=9.0,
        min_ram_gb=20.0,
        min_gpu_pool_gb=12.0,
        quant="Q4_K_M GGUF",
        summary="High-capacity 14B workstation model (needs 20+ GB RAM/VRAM)",
        tags=("heavy", "recommended"),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation 14B",
    ),
    HFModelSpec(
        id="hf_qwen3_30b_a3b_q4",
        name="Qwen 3 30B-A3B MoE",
        category="text",
        hf_repo="Qwen/Qwen3-30B-A3B-GGUF",
        hf_file="Qwen3-30B-A3B-Q4_K_M.gguf",
        size_gb=17.3,
        min_ram_gb=28.0,
        min_gpu_pool_gb=20.0,
        quant="Q4_K_M GGUF (MoE)",
        summary="Ultra-fast 3B-active Mixture-of-Experts on 24+ GB GPUs / 32+ GB RAM",
        tags=("heavy", "recommended"),
        backends=("cuda", "vulkan"),
        tuning_note="Pro MoE 30B",
    ),
    HFModelSpec(
        id="hf_qwen25_coder_32b_q4",
        name="Qwen 2.5 Coder 32B",
        category="text",
        hf_repo="bartowski/Qwen2.5-Coder-32B-Instruct-GGUF",
        hf_file="Qwen2.5-Coder-32B-Instruct-Q4_0.gguf",
        size_gb=17.4,
        min_ram_gb=28.0,
        min_gpu_pool_gb=20.0,
        quant="Q4_0 GGUF",
        summary="Flagship 32B systems engineering & refactor engine (24+ GB VRAM)",
        tags=("heavy", "coding"),
        backends=("cuda", "vulkan"),
        tuning_note="Pro 32B Coder",
    ),
    HFModelSpec(
        id="hf_qwen3_32b_q4",
        name="Qwen 3 32B",
        category="text",
        hf_repo="Qwen/Qwen3-32B-GGUF",
        hf_file="Qwen3-32B-Q4_K_M.gguf",
        size_gb=18.4,
        min_ram_gb=30.0,
        min_gpu_pool_gb=22.0,
        quant="Q4_K_M GGUF",
        summary="Dense 32B flagship reasoning for 24+ GB VRAM workstations",
        tags=("heavy",),
        backends=("cuda", "vulkan"),
        tuning_note="Pro 32B Dense",
    ),
    HFModelSpec(
        id="hf_llama33_70b_q4",
        name="Llama 3.3 70B Instruct",
        category="text",
        hf_repo="bartowski/Llama-3.3-70B-Instruct-GGUF",
        hf_file="Llama-3.3-70B-Instruct-Q4_0.gguf",
        size_gb=37.4,
        min_ram_gb=56.0,
        min_gpu_pool_gb=42.0,
        quant="Q4_0 GGUF",
        summary="Datacenter/multi-GPU 70B powerhouse (needs 48+ GB VRAM / 64 GB UMA)",
        tags=("heavy",),
        backends=("cuda", "vulkan"),
        tuning_note="Ultra 70B",
    ),
    # --- Image Models: Every Tier (2.6 GB Q4 -> FLUX & SD 3.5 Large) ---
    HFModelSpec(
        id="hf_realvis_xl_v5_lightning_q4",
        name="RealVisXL V5.0 Lightning Q4",
        category="image",
        hf_repo="off-grid-ai/realvisxl-v5.0-lightning-GGUF",
        hf_file="realvisxl-v5.0-lightning-Q4_K_S.gguf",
        size_gb=2.6,
        min_ram_gb=7.5,
        min_gpu_pool_gb=3.5,
        max_ram_gb=20.0,
        quant="Q4_K_S GGUF (4-step)",
        summary="2.6 GB quantized DSLR portraits & scenes tuned for iGPUs & low VRAM",
        tags=("recommended", "photoreal", "ultra-light"),
        backends=("vulkan", "cuda", "cpu"),
        tuning_note="iGPU Q4 GGUF",
    ),
    HFModelSpec(
        id="hf_dreamshaper_xl_turbo_q4",
        name="DreamShaper XL Turbo Q4",
        category="image",
        hf_repo="off-grid-ai/dreamshaper-xl-v2-turbo-GGUF",
        hf_file="dreamshaper-xl-v2-turbo-Q4_K_S.gguf",
        size_gb=2.6,
        min_ram_gb=7.5,
        min_gpu_pool_gb=3.5,
        max_ram_gb=20.0,
        quant="Q4_K_S GGUF (6-step)",
        summary="2.6 GB fast editorial & cinematic concept art tuned for laptops",
        tags=("art", "fast"),
        backends=("vulkan", "cuda", "cpu"),
        tuning_note="iGPU Q4 GGUF",
    ),
    HFModelSpec(
        id="hf_juggernaut_xl_q4",
        name="Juggernaut XL v9 Q4",
        category="image",
        hf_repo="off-grid-ai/juggernaut-xl-v9-GGUF",
        hf_file="juggernaut-xl-v9-Q4_K_S.gguf",
        size_gb=2.8,
        min_ram_gb=8.0,
        min_gpu_pool_gb=3.5,
        max_ram_gb=20.0,
        quant="Q4_K_S GGUF",
        summary="2.8 GB cinema cars & moody photography tuned for 8 GB GPU pool",
        tags=("photoreal", "cinema"),
        backends=("vulkan", "cuda"),
        tuning_note="iGPU Q4 GGUF",
    ),
    HFModelSpec(
        id="hf_sdxl_turbo_q8",
        name="SDXL Turbo 1.0 Q8",
        category="image",
        hf_repo="OlegSkutte/sdxl-turbo-GGUF",
        hf_file="sd_xl_turbo_1.0.q8_0.gguf",
        size_gb=3.8,
        min_ram_gb=10.5,
        min_gpu_pool_gb=5.5,
        max_ram_gb=22.0,
        quant="Q8_0 GGUF (1-step)",
        summary="3.8 GB instant 1–4 step draft synthesis for Vulkan iGPUs",
        tags=("fast",),
        backends=("vulkan", "cuda"),
        tuning_note="Vulkan Q8 Draft",
    ),
    HFModelSpec(
        id="hf_pony_v6_xl_q8",
        name="Pony Diffusion V6 XL",
        category="image",
        hf_repo="off-grid-ai/pony-diffusion-v6-xl-GGUF",
        hf_file="pony-diffusion-v6-xl-Q8_0.gguf",
        size_gb=3.9,
        min_ram_gb=10.5,
        min_gpu_pool_gb=5.5,
        max_ram_gb=22.0,
        quant="Q8_0 GGUF (Vulkan)",
        summary="Tuned Vulkan Q8_0 keyframe engine: sharp composition & DSLR versatility",
        tags=("recommended", "active-default"),
        backends=("vulkan", "cuda"),
        tuning_note="Vulkan Q8 GGUF",
    ),
    HFModelSpec(
        id="hf_realvis_xl_v5_lightning_q8",
        name="RealVisXL V5.0 Lightning Q8",
        category="image",
        hf_repo="off-grid-ai/realvisxl-v5.0-lightning-GGUF",
        hf_file="realvisxl-v5.0-lightning-Q8_0.gguf",
        size_gb=3.9,
        min_ram_gb=11.0,
        min_gpu_pool_gb=6.0,
        max_ram_gb=22.0,
        quant="Q8_0 GGUF (4-step)",
        summary="Near-lossless Q8 DSLR photorealism fitting cleanly inside 8 GB GPU pool",
        tags=("photoreal", "recommended"),
        backends=("vulkan", "cuda"),
        tuning_note="Vulkan Q8 DSLR",
    ),
    HFModelSpec(
        id="hf_juggernaut_xl_q8",
        name="Juggernaut XL v9 Q8",
        category="image",
        hf_repo="off-grid-ai/juggernaut-xl-v9-GGUF",
        hf_file="juggernaut-xl-v9-Q8_0.gguf",
        size_gb=4.1,
        min_ram_gb=12.0,
        min_gpu_pool_gb=6.5,
        max_ram_gb=22.0,
        quant="Q8_0 GGUF",
        summary="High-precision Q8 automotive & dramatic lighting tuned for 8–12 GB GPUs",
        tags=("photoreal", "cinema"),
        backends=("vulkan", "cuda"),
        tuning_note="Vulkan Q8 GGUF",
    ),
    HFModelSpec(
        id="hf_flux1_schnell_q3",
        name="FLUX.1-schnell Q3",
        category="image",
        hf_repo="city96/FLUX.1-schnell-gguf",
        hf_file="flux1-schnell-Q3_K_S.gguf",
        size_gb=4.9,
        min_ram_gb=16.0,
        min_gpu_pool_gb=9.5,
        quant="Q3_K_S GGUF (4-step)",
        summary="4-step quantized FLUX typography & prompt adherence (10+ GB GPU / 16+ GB RAM)",
        tags=("heavy", "fast"),
        backends=("cuda", "vulkan"),
        tuning_note="Mid-Tier FLUX",
    ),
    HFModelSpec(
        id="hf_sd35_medium_q4",
        name="Stable Diffusion 3.5 Medium",
        category="image",
        hf_repo="gpustack/stable-diffusion-v3-5-medium-GGUF",
        hf_file="stable-diffusion-v3-5-medium-pure-Q4_0.gguf",
        size_gb=4.9,
        min_ram_gb=16.0,
        min_gpu_pool_gb=10.0,
        quant="Q4_0 GGUF",
        summary="Clean DiT typography & composition for 12+ GB VRAM / 16+ GB workstations",
        tags=("heavy",),
        backends=("cuda", "vulkan"),
        tuning_note="Mid-Tier SD3.5",
    ),
    HFModelSpec(
        id="hf_flux1_schnell_q4",
        name="FLUX.1-schnell Q4",
        category="image",
        hf_repo="city96/FLUX.1-schnell-gguf",
        hf_file="flux1-schnell-Q4_K_S.gguf",
        size_gb=6.3,
        min_ram_gb=20.0,
        min_gpu_pool_gb=12.0,
        quant="Q4_K_S GGUF (4-step)",
        summary="Fast 4-step high-fidelity FLUX synthesis for 12+ GB VRAM rigs",
        tags=("heavy", "recommended"),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation FLUX",
    ),
    HFModelSpec(
        id="hf_realvis_xl_v5_lightning",
        name="RealVisXL V5.0 Lightning FP16",
        category="image",
        hf_repo="SG161222/RealVisXL_V5.0_Lightning",
        hf_file="RealVisXL_V5.0_Lightning_fp16.safetensors",
        size_gb=6.5,
        min_ram_gb=20.0,
        min_gpu_pool_gb=11.0,
        quant="FP16 Lightning (4-step)",
        summary="Full-precision DSLR portraits for >= 12 GB VRAM / workstation rigs",
        tags=("recommended", "photoreal", "heavy"),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation FP16",
    ),
    HFModelSpec(
        id="hf_juggernaut_xl_lightning",
        name="Juggernaut XL v9 FP16",
        category="image",
        hf_repo="RunDiffusion/Juggernaut-XL-v9",
        hf_file="Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
        size_gb=6.6,
        min_ram_gb=20.0,
        min_gpu_pool_gb=11.0,
        quant="FP16 SDXL",
        summary="Full-precision cinema lighting for >= 12 GB VRAM workstations",
        tags=("photoreal", "cinema", "heavy"),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation FP16",
    ),
    HFModelSpec(
        id="hf_flux1_dev_q4",
        name="FLUX.1-dev",
        category="image",
        hf_repo="city96/FLUX.1-dev-gguf",
        hf_file="flux1-dev-Q4_K_S.gguf",
        size_gb=6.8,
        min_ram_gb=22.0,
        min_gpu_pool_gb=14.0,
        quant="Q4_K_S GGUF",
        summary="High-capacity prompt adherence & text (needs 22+ GB RAM w/ T5)",
        tags=("heavy", "recommended"),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation FLUX",
    ),
    HFModelSpec(
        id="hf_sd35_large_q8",
        name="Stable Diffusion 3.5 Large Q8",
        category="image",
        hf_repo="gpustack/stable-diffusion-v3-5-large-GGUF",
        hf_file="stable-diffusion-v3-5-large-Q8_0.gguf",
        size_gb=14.9,
        min_ram_gb=28.0,
        min_gpu_pool_gb=20.0,
        quant="Q8_0 GGUF",
        summary="8B-parameter studio DiT fidelity for 24+ GB RTX / workstation cards",
        tags=("heavy",),
        backends=("cuda", "vulkan"),
        tuning_note="Pro 24GB+ DiT",
    ),
    # --- Video Models: Every Tier (0.9 GB Q4 -> 14B & HunyuanVideo) ---
    HFModelSpec(
        id="hf_wan21_t2v_1_3b_q4",
        name="Wan 2.1 T2V 1.3B Q4",
        category="video",
        hf_repo="samuelchristlie/Wan2.1-T2V-1.3B-GGUF",
        hf_file="Wan2.1-T2V-1.3B-Q4_K_M.gguf",
        size_gb=0.9,
        min_ram_gb=8.0,
        min_gpu_pool_gb=3.5,
        max_ram_gb=19.0,
        quant="Q4_K_M GGUF",
        summary="0.9 GB quantized text-to-video tuned for 8–12 GB laptops & low VRAM",
        tags=("ultra-light", "fast"),
        backends=("vulkan", "cuda"),
        tuning_note="Ultra-Light Video",
    ),
    HFModelSpec(
        id="hf_ltx_video_2b_q4",
        name="LTX-Video 2B Q4",
        category="video",
        hf_repo="city96/LTX-Video-0.9.5-gguf",
        hf_file="ltx-video-2b-v0.9.5-Q4_K_M.gguf",
        size_gb=1.2,
        min_ram_gb=8.5,
        min_gpu_pool_gb=4.0,
        max_ram_gb=19.0,
        quant="Q4_K_M GGUF",
        summary="1.2 GB temporal-coherent video generation for 8–16 GB notebooks",
        tags=("ultra-light", "fast"),
        backends=("vulkan", "cuda"),
        tuning_note="iGPU Q4 Video",
    ),
    HFModelSpec(
        id="hf_wan21_vace_1_3b_q4",
        name="Wan 2.1 VACE 1.3B Q4",
        category="video",
        hf_repo="samuelchristlie/Wan2.1-VACE-1.3B-GGUF",
        hf_file="Wan2.1-VACE-1.3B-Q4_K_M.gguf",
        size_gb=1.4,
        min_ram_gb=9.5,
        min_gpu_pool_gb=4.5,
        max_ram_gb=20.0,
        quant="Q4_K_M GGUF",
        summary="1.4 GB quantized I2V/keyframe anchor motion for 12–16 GB iGPUs",
        tags=("fast",),
        backends=("vulkan", "cuda"),
        tuning_note="iGPU Q4 VACE",
    ),
    HFModelSpec(
        id="hf_wan21_t2v_1_3b",
        name="Wan 2.1 T2V 1.3B",
        category="video",
        hf_repo="Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        hf_file="split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors",
        size_gb=2.6,
        min_ram_gb=10.5,
        min_gpu_pool_gb=5.0,
        max_ram_gb=22.0,
        quant="FP16",
        summary="Compact 2.6 GB direct text-to-video synthesis for laptops",
        tags=("recommended", "fast"),
        backends=("vulkan", "cuda"),
        tuning_note="iGPU 1.3B T2V",
    ),
    HFModelSpec(
        id="hf_ltx_video_2b",
        name="LTX-Video 0.9.6 2B",
        category="video",
        hf_repo="Lightricks/LTX-Video",
        hf_file="ltxv-2b-0.9.6-dev-04-25.safetensors",
        size_gb=3.4,
        min_ram_gb=12.0,
        min_gpu_pool_gb=6.0,
        quant="FP8 Lightweight",
        summary="Rapid temporal-coherent landscape & atmospheric motion",
        tags=("fast",),
        backends=("vulkan", "cuda"),
        tuning_note="Tuned 2B FP8",
    ),
    HFModelSpec(
        id="hf_wan21_vace_1_3b",
        name="Wan 2.1 VACE 1.3B",
        category="video",
        hf_repo="Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        hf_file="split_files/diffusion_models/wan2.1_vace_1.3B_fp16.safetensors",
        size_gb=4.0,
        min_ram_gb=12.0,
        min_gpu_pool_gb=6.0,
        quant="FP16 (6-step CausVid)",
        summary="Tuned 720p I2V & keyframe animation for 16 GB iGPU & dGPU rigs",
        tags=("recommended", "active-default"),
        backends=("vulkan", "cuda"),
        tuning_note="Tuned 1.3B VACE",
    ),
    HFModelSpec(
        id="hf_hunyuan_video_q4",
        name="HunyuanVideo 720p Q4",
        category="video",
        hf_repo="city96/HunyuanVideo-gguf",
        hf_file="hunyuan-video-t2v-720p-Q4_K_M.gguf",
        size_gb=7.3,
        min_ram_gb=20.0,
        min_gpu_pool_gb=12.0,
        quant="Q4_K_M GGUF",
        summary="Cinema 13B lighting & human physics tuned for 16+ GB VRAM rigs",
        tags=("heavy", "cinema"),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation 13B",
    ),
    HFModelSpec(
        id="hf_wan21_t2v_14b_q4",
        name="Wan 2.1 T2V 14B",
        category="video",
        hf_repo="city96/Wan2.1-T2V-14B-gguf",
        hf_file="wan2.1-t2v-14b-Q4_K_M.gguf",
        size_gb=9.4,
        min_ram_gb=22.0,
        min_gpu_pool_gb=14.0,
        quant="Q4_K_M GGUF",
        summary="High-capacity 14B cinema generation (needs 22+ GB RAM / 14+ GB VRAM)",
        tags=("heavy",),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation 14B",
    ),
    HFModelSpec(
        id="hf_wan21_i2v_14b_q4",
        name="Wan 2.1 I2V 14B 480p",
        category="video",
        hf_repo="city96/Wan2.1-I2V-14B-480P-gguf",
        hf_file="wan2.1-i2v-14b-480p-Q4_K_M.gguf",
        size_gb=10.6,
        min_ram_gb=22.0,
        min_gpu_pool_gb=14.0,
        quant="Q4_K_M GGUF",
        summary="Flagship 14B physical motion fidelity (needs 22+ GB RAM / 14+ GB VRAM)",
        tags=("heavy", "recommended"),
        backends=("cuda", "vulkan"),
        tuning_note="Workstation 14B",
    ),
    HFModelSpec(
        id="hf_wan21_i2v_720p_14b_fp8",
        name="Wan 2.1 I2V 720p 14B FP8",
        category="video",
        hf_repo="Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        hf_file="split_files/diffusion_models/wan2.1_i2v_720p_14B_fp8_e4m3fn.safetensors",
        size_gb=15.3,
        min_ram_gb=32.0,
        min_gpu_pool_gb=22.0,
        quant="FP8 E4M3FN",
        summary="Native 720p 14B I2V workstation powerhouse (needs 24+ GB VRAM)",
        tags=("heavy",),
        backends=("cuda", "vulkan"),
        tuning_note="Pro 24GB+ FP8",
    ),
)

# Pinned trusted SHA-256 digests and exact file byte sizes from Hugging Face Hub (`C1`–`C3`)
TRUSTED_CURATED_CHECKSUMS: dict[str, tuple[int, str]] = {
    "hf_qwen25_1_5b_q4": (1117320736, "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"),
    "hf_llama32_3b_q4": (2019377696, "6c1a2b41161032677be168d354123594c0e6e67d2b9227c84f296ad037c728ff"),
    "hf_phi4_mini_q4": (2491874272, "88c00229914083cd112853aab84ed51b87bdf6b9ce42f532d8c85c7c63b1730a"),
    "hf_qwen3_4b_q4": (2497280256, "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"),
    "hf_gemma3_4b_q4": (2489758112, "4996030242583a40aa151ff93f49ed787ac8c25e4120c3ae4588b2e2a7d1ae94"),
    "hf_qwen25_3b_q6": (2793410976, "12da1a5d3fa6905111d8798b00ed49e0f6425441598c8c41bb37a2c36d49d0f3"),
    "hf_deepseek_r1_7b_q4": (4683073504, "731ece8d06dc7eda6f6572997feb9ee1258db0784827e642909d9b565641937b"),
    "hf_qwen25_7b_q4": (4683074240, "65b8fcd92af6b4fefa935c625d1ac27ea29dcb6ee14589c55a8f115ceaaa1423"),
    "hf_qwen25_coder_7b_q4": (4683074336, "1664fccab734674a50763490a8c6931b70e3f2f8ec10031b54806d30e5f956b6"),
    "hf_llama31_8b_q4": (4920739232, "7b064f5842bf9532c91456deda288a1b672397a54fa729aa665952863033557c"),
    "hf_qwen3_8b_q4": (5027783488, "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"),
    "hf_mistral_nemo_12b_q4": (7094641792, "cc7a72b30db1d9eac3ed7cab1f69d408621a01e12b4dc29a1c54639a74311017"),
    "hf_deepseek_r1_14b_q4": (8988110240, "0b319bd0572f2730bfe11cc751defe82045fad5085b4e60591ac2cd2d9633181"),
    "hf_phi4_14b_q4": (9053114816, "009aba717c09d4a35890c7d35eb59d54e1dba884c7c526e7197d9c13ab5911d9"),
    "hf_qwen25_14b_q4": (8988110976, "e47ad95dad6ff848b431053b375adb5d39321290ea2c638682577dafca87c008"),
    "hf_qwen3_30b_a3b_q4": (18556685824, "0d003f6662faee786ed5da3e31b29c978de5ae5d275c8794c606a7f3c01aa8f5"),
    "hf_qwen25_coder_32b_q4": (18711010272, "4f6cdbed22cd958ef298af1ccc3e343d154d58de4e13038cb6ff66d8ac7a14d8"),
    "hf_qwen3_32b_q4": (19762149024, "efd971561896866f0e910cce52761ca77b1b138090c7f15fe284676d57d1f689"),
    "hf_llama33_70b_q4": (40116538336, "564d5978d93c54b6ec1c6f25cd2ab11a8383f0902ea92546dd6e2c78509c4c1c"),
    "hf_realvis_xl_v5_lightning_q4": (2797755008, "2c821ab5b61c572b7085d7951e5247a05688e5f77c64d84724073235cb90875e"),
    "hf_dreamshaper_xl_turbo_q4": (2798084896, "3864355a401eb48f7909f6ee8ab14ef2a7e1e259e27ee85fc4a234f333d588c8"),
    "hf_juggernaut_xl_q4": (2965023776, "a2d08292fd9c036f9f1b0bc7754d5e2ac262ecec79a0f2fcdc9b2c35afd56705"),
    "hf_sdxl_turbo_q8": (4098988672, "f5659e6cdad699b1432870cc9fe811359e45c8a331687be505d0ba05c8364720"),
    "hf_pony_v6_xl_q8": (4180186816, "d5a3a5204e4ac9043f2e42c616ef6884457c450da0f8cb6f69982ef4649e5ddf"),
    "hf_realvis_xl_v5_lightning_q8": (4180188864, "394933bd1db6b2c7792d4a3db05583d571d296b61a6bd6987f1d7d9ae2971c98"),
    "hf_juggernaut_xl_q8": (4347454816, "2203d1673204685a649eb9b64f1e91a9fc4ba2f17adb6d30ad5abc7351a9d411"),
    "hf_flux1_schnell_q3": (5212210208, "da68cf0db4bc71a9a2fb1b357521eb37f04c06c59025ce487d061ec4652905be"),
    "hf_sd35_medium_q4": (5293558784, "44ce4b7f7932bd47370fd8cc182ea7e61adb5bb639205da1f530b97410f3080a"),
    "hf_flux1_schnell_q4": (6783943712, "4fd16477b3a5296d0cf722c4b92a9fd7f30d09ac7495826e4465d8de9c9fd973"),
    "hf_realvis_xl_v5_lightning": (6938065512, "fabcadd9330dcc4f9702063428d40b9d4d07168d8acefc819b8d1d9db466b3ec"),
    "hf_juggernaut_xl_lightning": (7105348188, "c9e3e68f89b8e38689e1097d4be4573cf308de4e3fd044c64ca697bdb4aa8bca"),
    "hf_flux1_dev_q4": (6805988640, "75bb19459b5240c9f373b5af527584af15b675867fa142efadf7478d6abbf62b"),
    "hf_sd35_large_q8": (15949811072, "69f4297a8c8e48187486c603ac47f6919dddd49bc0a869553afd4784982406fe"),
    "hf_wan21_t2v_1_3b_q4": (982716640, "1e22a68152e6c4432524c61c78185efa35e6b305483ceb5fe238b2d51964cea7"),
    "hf_ltx_video_2b_q4": (1330049536, "44ffff441fc941b8eb7db12bb3f02d5e5ef3b71f00abf9b9849657ca2109a097"),
    "hf_wan21_vace_1_3b_q4": (1469522624, "1f5bbad1507cba9bd2bfa7a8cbc76488c944c5bd844a0e7f6be37ee3e6025eca"),
    "hf_wan21_t2v_1_3b": (2838303560, "be531024cd9018cb5b48c40cfbb6a6191645b1c792eb8bf4f8c1c6e10f924dc5"),
    "hf_ltx_video_2b": (6340743924, "e6fb3fe995e23c1f2457bc26c554789a8d74718c17f1853baab33939bd8dfb56"),
    "hf_wan21_vace_1_3b": (4309519800, "640ccc0577e6a5d4bb15cd91b11b699ef914fc55f126c5a1c544e152130784f2"),
    "hf_hunyuan_video_q4": (7883680512, "ef5c2bb989fce20ab39341db98adfbb3d072d9dc709b02391603755d433c1446"),
    "hf_wan21_t2v_14b_q4": (10124581504, "c9a64d612831debbac77af93744d4b5196b840bf65e9f9f5b958d03fc3baddf2"),
    "hf_wan21_i2v_14b_q4": (11341184384, "d91f7139acadb42ea05cdf97b311e5099f714f11fbe4d90916500e2f53cbba82"),
    "hf_wan21_i2v_720p_14b_fp8": (16397245448, "b2051cd29d6b2f0c924fa7a3e78a4772f0134d7b059f21590dcce416f4f6cbe8"),
}


def get_expected_checksum(spec: HFModelSpec) -> tuple[int, str]:
    """Return `(expected_bytes, sha256_hex)` for curated or metadata-backed specs (`C1`, `C3`)."""
    if spec.sha256 or spec.expected_bytes > 0:
        return (int(spec.expected_bytes), spec.sha256.lower().strip())
    pinned = TRUSTED_CURATED_CHECKSUMS.get(spec.id)
    if pinned is not None:
        return pinned
    return (0, "")


def _read_total_and_avail_ram_gb() -> tuple[float, float]:
    """Read real MemTotal and MemAvailable; conservative (6.0, 3.0) fallback if unreadable (`D1`, `D13`)."""
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        vals: dict[str, int] = {}
        try:
            for ln in meminfo.read_text(encoding="utf-8").splitlines():
                if ":" not in ln:
                    continue
                k, v = ln.split(":", 1)
                num = v.strip().split()[0]
                if num.isdigit():
                    vals[k.strip()] = int(num)
            tot_kb = vals.get("MemTotal") or 0
            if tot_kb > 0:
                tot = tot_kb / (1024.0 * 1024.0)
                avail_kb = vals.get("MemAvailable", vals.get("MemFree", 0))
                avail = avail_kb / (1024.0 * 1024.0)
                return (round(tot, 1), round(avail, 1))
        except OSError:
            pass
    if os.name == "nt":
        try:
            import ctypes

            class _MemStat(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemStat()
            stat.dwLength = ctypes.sizeof(_MemStat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
            gb = 1024.0 ** 3
            return (round(stat.ullTotalPhys / gb, 1), round(stat.ullAvailPhys / gb, 1))
        except Exception:
            pass
    return (6.0, 3.0)


def _detect_cpu_name() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        try:
            for ln in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ln.lower().startswith("model name") and ":" in ln:
                    return ln.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "x86_64 CPU"


def _gpu_name_from_cpu(cpu_name: str) -> str:
    m = re.search(r"w/\s*(.+)$", cpu_name, flags=re.IGNORECASE)
    if m:
        cand = m.group(1).strip()
        if "radeon" in cand.lower() and not cand.lower().startswith("amd"):
            return f"AMD {cand}"
        return cand
    return ""


def detect_engine_capabilities(*, force_refresh: bool = False) -> dict[str, bool]:
    """Probe actual binary & GPU backend availability for llama.cpp, sd.cpp, and ComfyUI (`D11`)."""
    global _ENGINE_CAPS_CACHE
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force_refresh and _ENGINE_CAPS_CACHE is not None and (now - _ENGINE_CAPS_CACHE[0]) < 60.0:
            return dict(_ENGINE_CAPS_CACHE[1])

    caps = {
        "llama_cpp": Path("~/llama.cpp/build/bin/llama-server").expanduser().is_file(),
        "sd_cpp": Path("~/stable-diffusion.cpp/build/bin/sd-server").expanduser().is_file(),
        "comfyui": Path("~/comfyui/main.py").expanduser().is_file()
        and Path("~/comfyui/venv/bin/python").expanduser().is_file(),
        "vulkan_icd": False,
        "cuda_smi": False,
    }
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=1.2, check=False)
        caps["cuda_smi"] = r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:
        pass
    try:
        rv = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=1.5, check=False)
        if rv.returncode == 0:
            for ln in rv.stdout.splitlines():
                if "deviceName" in ln and "=" in ln and "llvmpipe" not in ln.lower():
                    caps["vulkan_icd"] = True
                    break
    except Exception:
        pass

    with _CACHE_LOCK:
        _ENGINE_CAPS_CACHE = (time.monotonic(), dict(caps))
    return caps


def _detect_gpu_details() -> tuple[str, float, str, bool, float, int, tuple[str, ...], bool, bool]:
    # 1. Multi-GPU nvidia-smi inspection (`D9`): pick highest-VRAM CUDA device cleanly
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            gpus: list[tuple[str, float]] = []
            for line in r.stdout.strip().splitlines():
                parts = line.split(",")
                if len(parts) >= 2:
                    gname = parts[0].strip()
                    try:
                        gvram = round(float(parts[1].strip()) / 1024.0, 1)
                    except ValueError:
                        continue
                    gpus.append((gname, gvram))
            if gpus:
                gpus.sort(key=lambda g: g[1], reverse=True)
                best_name, best_vram = gpus[0]
                return (best_name, best_vram, "cuda", False, 0.0, len(gpus), tuple(g[0] for g in gpus), False, True)
    except Exception:
        pass

    # 2. Verify Vulkan availability explicitly via vulkaninfo rather than trusting DRM blindly (`D10`)
    drm_root = Path("/sys/class/drm")
    gpu_name = ""
    vram_gb = 0.0
    gtt_gb = 0.0
    if drm_root.is_dir():
        for card in sorted(drm_root.glob("card[0-9]/device")):
            vram_file = card / "mem_info_vram_total"
            if vram_file.is_file():
                try:
                    raw_b = int(vram_file.read_text(encoding="utf-8").strip())
                    vram_gb = max(vram_gb, round(raw_b / (1024.0 ** 3), 1))
                except Exception:
                    pass
            gtt_file = card / "mem_info_gtt_total"
            if gtt_file.is_file():
                try:
                    raw_gtt = int(gtt_file.read_text(encoding="utf-8").strip())
                    gtt_gb = max(gtt_gb, round(raw_gtt / (1024.0 ** 3), 1))
                except Exception:
                    pass
            uev = card / "uevent"
            if uev.is_file() and not gpu_name:
                try:
                    txt = uev.read_text(encoding="utf-8", errors="ignore")
                    if "amdgpu" in txt.lower():
                        gpu_name = _gpu_name_from_cpu(_detect_cpu_name()) or "AMD Radeon"
                    elif "i915" in txt.lower() or "xe" in txt.lower():
                        gpu_name = "Intel Arc / Iris"
                except OSError:
                    pass

    vulkan_devs: list[str] = []
    vulkaninfo_ran = False
    try:
        r2 = subprocess.run(
            ["vulkaninfo", "--summary"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        vulkaninfo_ran = True
        if r2.returncode == 0:
            for ln in r2.stdout.splitlines():
                if "deviceName" in ln and "=" in ln:
                    cand = ln.split("=", 1)[1].strip()
                    if cand and "llvmpipe" not in cand.lower():
                        vulkan_devs.append(cand)
    except Exception:
        pass

    if vulkan_devs:
        gpu_name = vulkan_devs[0]
        is_uma = vram_gb <= 4.1
        return (gpu_name, vram_gb, "vulkan", is_uma, gtt_gb, len(vulkan_devs), tuple(vulkan_devs), True, False)

    # If vulkaninfo ran cleanly and found NO hardware Vulkan device (or only llvmpipe), do NOT assume Vulkan (`D10`)
    if gpu_name and not vulkaninfo_ran and Path("/usr/share/vulkan/icd.d").is_dir() and any(Path("/usr/share/vulkan/icd.d").glob("*.json")):
        is_uma = vram_gb <= 4.1
        return (gpu_name, vram_gb, "vulkan", is_uma, gtt_gb, 1, (gpu_name,), False, False)

    return ("CPU Fallback", 0.0, "cpu", True, 0.0, 0, (), False, False)


def _detect_gpu() -> tuple[str, float, str, bool]:
    name, vram_gb, backend, is_uma, _gtt, _cnt, _devs, _vok, _cok = _detect_gpu_details()
    return (name, vram_gb, backend, is_uma)


def detect_hardware_profile(*, force_refresh: bool = False) -> HardwareProfile:
    """Probe live RAM + cached/refreshable GPU/CPU profile (`D1`, `D2`, `D9`–`D13`)."""
    global _HW_STATIC_CACHE
    tot_ram, avail_ram = _read_total_and_avail_ram_gb()
    now = time.monotonic()
    static_tuple = None
    with _CACHE_LOCK:
        if not force_refresh and _HW_STATIC_CACHE is not None and (now - _HW_STATIC_CACHE[0]) < 10.0:
            static_tuple = _HW_STATIC_CACHE[1]
    if static_tuple is None:
        cpu_nm = _detect_cpu_name()
        cores = os.cpu_count() or 2
        gname, vram_gb, backend, is_uma, gtt_gb, gcnt, gdevs, vok, cok = _detect_gpu_details()
        static_tuple = (cpu_nm, cores, gname, vram_gb, backend, is_uma, gtt_gb, gcnt, gdevs, vok, cok)
        with _CACHE_LOCK:
            _HW_STATIC_CACHE = (time.monotonic(), static_tuple)
    cpu_nm, cores, gname, vram_gb, backend, is_uma, gtt_gb, gcnt, gdevs, vok, cok = static_tuple
    return HardwareProfile(
        cpu_name=cpu_nm,
        cpu_cores=cores,
        total_ram_gb=tot_ram,
        avail_ram_gb=avail_ram,
        gpu_name=gname,
        vram_gb=vram_gb,
        backend=backend,
        unified_memory=is_uma,
        gtt_gb=gtt_gb,
        gpu_count=gcnt,
        gpu_devices=gdevs,
        vulkan_verified=vok,
        cuda_verified=cok,
        cpu_fallback_only=(backend == "cpu"),
    )


def estimate_peak_inference_memory_gb(
    spec: HFModelSpec,
    *,
    ctx_size: int = 4096,
    width: int = 832,
    height: int = 512,
    batch_count: int = 1,
    video_frames: int = 17,
) -> float:
    """Estimate peak runtime inference memory rather than raw disk weight size (`D3`–`D6`, `D14`)."""
    weights_gb = max(0.3, float(spec.size_gb))
    if spec.category == "text":
        # Weight buffers + KV cache scaled by context length + compute scratch (`D4`)
        kv_gb = (max(512, ctx_size) / 4096.0) * max(0.35, min(2.4, weights_gb * 0.14))
        return round(weights_gb * 1.12 + kv_gb + 0.45, 2)
    if spec.category == "image":
        # Checkpoint + CLIP/T5 text encoder overhead + VAE decode + batch tensors (`D5`)
        mp = (max(256, width) * max(256, height)) / 1_000_000.0
        encoder_gb = 1.6 if weights_gb >= 4.8 else 0.9
        tensor_gb = 0.65 * mp * max(1, min(8, batch_count)) ** 0.35
        return round(weights_gb * 1.15 + encoder_gb + tensor_gb, 2)
    # Video: diffusion weights + text encoder + multi-frame latent/temporal buffers (`D6`)
    mp = (max(256, width) * max(256, height)) / 1_000_000.0
    frame_factor = max(1.0, video_frames / 17.0)
    encoder_gb = 2.6 if weights_gb >= 4.0 else 1.8
    temporal_gb = 1.1 * mp * frame_factor
    return round(weights_gb * 1.18 + encoder_gb + temporal_gb, 2)


def evaluate_model_fit(spec: HFModelSpec, hw: HardwareProfile) -> tuple[str, str]:
    """Return `(tier, label)` using live available RAM, peak memory, & non-overlapping GPU pool (`D1`–`D14`)."""
    if hw.backend not in spec.backends:
        req = "/".join(b.upper() for b in spec.backends)
        return ("heavy", f"Needs {req}")
    if hw.backend != "cpu" and spec.min_gpu_pool_gb > 0 and hw.gpu_pool_gb < spec.min_gpu_pool_gb:
        return ("heavy", f"Needs {spec.min_gpu_pool_gb:.0f}+ GB GPU")
    if hw.backend == "cpu" and spec.min_gpu_pool_gb > 4.0:
        return ("heavy", "Needs GPU")

    # Estimate peak runtime memory (`D3`, `D14`) and check live available RAM (`D1`, `D2`, `D8`)
    ctx_est = 2048 if hw.total_ram_gb < 11.0 else (8192 if hw.total_ram_gb >= 24.0 else 4096)
    peak_gb = estimate_peak_inference_memory_gb(spec, ctx_size=ctx_est)
    effective_total = max(hw.total_ram_gb, hw.avail_ram_gb)

    # On discrete GPU (`not unified_memory`), GPU VRAM relieves system RAM; on UMA, BOTH draw from RAM (`D8`)
    if not hw.unified_memory and hw.backend != "cpu" and hw.vram_gb >= peak_gb * 0.8:
        ram_needed = max(spec.min_ram_gb, peak_gb * 0.45)
    else:
        ram_needed = max(spec.min_ram_gb, peak_gb)

    headroom_total = effective_total - spec.min_ram_gb
    headroom_live = hw.avail_ram_gb - (ram_needed * 0.65)
    if headroom_total >= 1.5 and headroom_live >= 0.0 and (hw.backend != "cpu" or spec.size_gb <= 3.0):
        badge = spec.tuning_note or "Recommended"
        return ("best_fit", f"{badge} · ~{peak_gb:.1f} GB peak")
    if headroom_total >= 0.0 or (effective_total >= spec.min_ram_gb - 0.5 and spec.size_gb <= 3.0):
        return ("fits", f"Fits · ~{peak_gb:.1f} GB peak")
    return ("heavy", f"Needs {max(spec.min_ram_gb, peak_gb):.0f}+ GB")


def is_model_tuned_for_hardware(spec: HFModelSpec, hw: HardwareProfile, local_installed: bool | None = None) -> bool:
    """True when `spec` belongs in the hardware-tuned catalog for `hw`."""
    is_local = (find_local_model_path(spec) is not None) if local_installed is None else local_installed
    if is_local:
        return True
    fit_tier, _ = evaluate_model_fit(spec, hw)
    if fit_tier == "heavy":
        return False
    if spec.max_ram_gb is not None:
        if spec.category == "text":
            if hw.total_ram_gb > spec.max_ram_gb:
                return False
        else:
            if hw.tier == "workstation":
                return False
    return True


def _search_roots_for_category(category: str) -> list[Path]:
    cat = category.lower()
    if cat == "text":
        return [
            Path("~/models/llm").expanduser(),
            LOCAL_MODELS_DIR / "text",
        ]
    if cat == "image":
        return [
            Path("~").expanduser(),
            Path("~/comfyui/models/unet").expanduser(),
            Path("~/comfyui/models/checkpoints").expanduser(),
            LOCAL_MODELS_DIR / "image",
        ]
    return [
        Path("~/comfyui/models/diffusion_models").expanduser(),
        Path("~/comfyui/models/unet").expanduser(),
        LOCAL_MODELS_DIR / "video",
    ]


def find_local_model_path(spec: HFModelSpec, *, verify_integrity: bool = True) -> Path | None:
    """Locate and validate an installed model file matching `spec` locally (`C2`, `C11`)."""
    fname = Path(spec.hf_file).name
    for root in _search_roots_for_category(spec.category):
        cand = root / fname
        if cand.is_file() or cand.is_symlink():
            if not cand.exists():
                continue
            if verify_integrity:
                ok_int, _msg = verify_local_model_integrity(spec, cand, check_hash=True)
                if not ok_int:
                    continue
            return cand
    # Pre-installed local alias fallback (`light-uncensored.gguf` user custom LLM)
    if spec.category == "text":
        def_llm = Path("~/models/llm/light-uncensored.gguf").expanduser()
        if def_llm.exists() and def_llm.is_file():
            try:
                resolved = def_llm.resolve()
                if resolved.name.lower() == fname.lower():
                    if not verify_integrity or verify_local_model_integrity(spec, resolved, check_hash=True)[0]:
                        return resolved
            except OSError:
                pass
            if spec.id == "hf_qwen25_7b_q4":
                if not verify_integrity or is_file_compatible_with_backend(spec, def_llm)[0]:
                    return def_llm
    return None


def _load_models_config() -> dict[str, str]:
    try:
        if MODELS_STATE_FILE.is_file():
            raw = json.loads(MODELS_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        pass
    return {}


def _save_models_config(cfg: dict[str, str]) -> None:
    MODELS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODELS_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(MODELS_STATE_FILE)


def _resolve_model_spec(model_id: str, category: str | None = None) -> HFModelSpec | None:
    spec = next((m for m in CURATED_HF_MODELS if m.id == model_id), None)
    if spec is None:
        with _CACHE_LOCK:
            spec = _DYNAMIC_HF_MODELS.get(model_id)
    if spec is not None and category is not None and spec.category != category.lower():
        return None
    return spec


def get_selected_model_id(category: str, hw: HardwareProfile | None = None) -> str:
    cfg = _load_models_config()
    key = category.lower()
    prof = hw or detect_hardware_profile()
    tuned = get_models_for_category(key, hw=prof)
    saved = cfg.get(key)
    if saved:
        if any(m.id == saved for m in tuned):
            return saved
        dyn = _resolve_model_spec(saved, key)
        if dyn is not None and is_model_tuned_for_hardware(dyn, prof):
            return saved
        if saved.startswith("hf_hub_") and cfg.get(f"{key}_repo"):
            return saved
    if tuned:
        return tuned[0].id
    rec = get_recommended_models_for_hardware(prof)
    return rec[key].id if key in rec else ""


def set_selected_model_id(category: str, model_id: str, spec_obj: HFModelSpec | None = None) -> HFModelSpec | None:
    key = category.lower()
    if spec_obj is not None and spec_obj.id == model_id and spec_obj.category == key:
        with _CACHE_LOCK:
            _DYNAMIC_HF_MODELS[spec_obj.id] = spec_obj
        spec: HFModelSpec | None = spec_obj
    else:
        spec = _resolve_model_spec(model_id, key)
    if spec is None:
        return None
    cfg = _load_models_config()
    cfg[key] = model_id
    cfg[f"{key}_repo"] = spec.hf_repo
    cfg[f"{key}_file"] = spec.hf_file
    local_p = find_local_model_path(spec)
    if local_p is not None:
        cfg[f"{key}_path"] = str(local_p)
    _save_models_config(cfg)
    return spec


def get_selected_model_path(category: str) -> Path | None:
    cfg = _load_models_config()
    key = category.lower()
    raw_p = cfg.get(f"{key}_path", "")
    sel_id = cfg.get(key, "")
    spec = _resolve_model_spec(sel_id) if sel_id else None
    if raw_p:
        p = Path(raw_p).expanduser()
        if p.exists() and p.is_file():
            if spec is not None:
                if p.name == "light-uncensored.gguf":
                    if is_file_compatible_with_backend(spec, p)[0]:
                        return p
                elif verify_local_model_integrity(spec, p, check_hash=True)[0]:
                    return p
            else:
                return p
    if spec is not None:
        return find_local_model_path(spec, verify_integrity=True)
    return None


def _pick_quant_file_for_hardware(
    files: list[dict[str, Any]],
    hw: HardwareProfile,
    category: str,
) -> tuple[str, float, str] | None:
    """Pick the best GGUF/Safetensors weight file in a Hugging Face repo for `hw`."""
    candidates: list[tuple[str, float, str]] = []
    for item in files:
        p = str(item.get("path") or "")
        if not p.endswith((".gguf", ".safetensors")):
            continue
        low = p.lower()
        if any(bad in low for bad in ("mmproj", "imatrix", "vae", "clip", "t5xxl")):
            continue
        size_b = int(item.get("size") or 0)
        if size_b < 200 * 1024 * 1024:
            continue
        size_gb = round(size_b / (1024.0 ** 3), 1)
        m_q = re.search(r"(q\d_k_[ms]|q\d_0|q\d_1|q\d|fp16|fp8|bf16)", low)
        quant = m_q.group(1).upper() if m_q else ("FP16" if low.endswith(".safetensors") else "GGUF")
        candidates.append((p, size_gb, quant))
    if not candidates:
        return None

    # Filter files that cannot fit inside total system RAM cleanly
    budget = hw.gpu_pool_gb if (category in ("image", "video") and hw.backend != "cpu") else hw.total_ram_gb
    max_file_gb = max(1.2, min(hw.total_ram_gb * 0.65, budget * 0.85 if budget > 0 else hw.total_ram_gb * 0.5))
    fitting = [c for c in candidates if c[1] <= max_file_gb]
    if not fitting:
        return None

    # Prefer balanced Q4_K_M / Q4_K_S on low/balanced tiers, higher precision Q8/Q6/FP16 on workstations
    if hw.tier == "workstation":
        pref = ("Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "FP16", "FP8", "Q4_K_S", "Q4_0")
    else:
        pref = ("Q4_K_M", "Q4_K_S", "Q4_0", "Q5_K_M", "Q3_K_M", "Q8_0", "FP8", "FP16")
    pref_idx = {q: i for i, q in enumerate(pref)}
    fitting.sort(key=lambda c: (pref_idx.get(c[2], 99), c[1] if hw.tier != "workstation" else -c[1]))
    return fitting[0]


def search_hf_hub_tuned(
    query: str,
    category: str,
    hw: HardwareProfile | None = None,
    limit: int = 8,
    timeout_s: float = 3.5,
) -> list[HFModelSpec]:
    """Query Hugging Face Hub API live and return ONLY models tuned for `hw`."""
    q = query.strip()
    if len(q) < 2:
        return []
    key = category.lower()
    prof = hw or detect_hardware_profile()
    cache_key = (key, q.lower(), f"{prof.tier}:{prof.backend}:{round(prof.total_ram_gb)}:{round(prof.gpu_pool_gb)}")
    with _CACHE_LOCK:
        if cache_key in _HF_SEARCH_CACHE:
            return list(_HF_SEARCH_CACHE[cache_key])

    suffix = "gguf" if "gguf" not in q.lower() else ""
    search_term = f"{q} {suffix}".strip() if suffix else q
    url = (
        f"https://huggingface.co/api/models?search={urllib.parse.quote(search_term)}"
        f"&sort=downloads&direction=-1&limit=18"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Vimona/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw_list = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    if not isinstance(raw_list, list):
        return []

    curated_repos = {m.hf_repo.lower() for m in CURATED_HF_MODELS}
    candidate_repos: list[dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        repo_id = str(item.get("id") or "")
        if not repo_id or repo_id.lower() in curated_repos or item.get("gated") or item.get("private"):
            continue
        tags = {str(t).lower() for t in (item.get("tags") or [])}
        pipe = str(item.get("pipeline_tag") or "").lower()
        rid_low = repo_id.lower()
        if key == "text":
            if pipe and pipe not in ("text-generation", "conversational"):
                continue
            if any(x in rid_low for x in ("flux", "sdxl", "pony", "realvis", "juggernaut", "wan2", "ltx", "hunyuan")):
                continue
        elif key == "image":
            if not (
                pipe == "text-to-image"
                or tags & {"diffusers", "stable-diffusion", "stable-diffusion-xl", "flux", "text-to-image"}
                or any(k in rid_low for k in ("sdxl", "flux", "pony", "realvis", "juggernaut", "dreamshaper", "illustrious", "sd3", "diffusion"))
            ):
                continue
        elif key == "video":
            if not (
                pipe in ("text-to-video", "image-to-video")
                or tags & {"text-to-video", "image-to-video"}
                or any(k in rid_low for k in ("wan2", "wan-", "ltx", "hunyuan", "cogvideo", "mochi", "video"))
            ):
                continue
        candidate_repos.append(item)
        if len(candidate_repos) >= 10:
            break

    def _inspect_repo(item: dict[str, Any]) -> HFModelSpec | None:
        repo_id = str(item["id"])
        turl = f"https://huggingface.co/api/models/{repo_id}/tree/main"
        treq = urllib.request.Request(turl, headers={"User-Agent": "Vimona/1.0"})
        try:
            with urllib.request.urlopen(treq, timeout=2.5) as resp:
                files = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        if not isinstance(files, list):
            return None
        picked = _pick_quant_file_for_hardware(files, prof, key)
        if picked is None:
            return None
        fpath, size_gb, quant = picked
        if key == "text":
            min_ram = max(4.0, round(size_gb * 1.55 + 2.5, 1))
            min_gpu = 0.0
            backends: tuple[str, ...] = ("cuda", "vulkan", "cpu")
        else:
            min_ram = max(6.5, round(size_gb * 1.9 + 2.5, 1))
            min_gpu = max(3.0, round(size_gb * 1.15 + 0.8, 1))
            backends = ("cuda", "vulkan")

        dl_cnt = int(item.get("downloads") or 0)
        likes_cnt = int(item.get("likes") or 0)
        with _CACHE_LOCK:
            _HF_META_CACHE[repo_id] = {"id": repo_id, "downloads": dl_cnt, "likes": likes_cnt}

        short_nm = repo_id.split("/", 1)[-1].replace("-GGUF", "").replace("-gguf", "").replace("-", " ").strip()
        spec_id = f"hf_hub_{re.sub(r'[^a-z0-9]+', '_', repo_id.lower()).strip('_')}"
        spec = HFModelSpec(
            id=spec_id,
            name=short_nm[:44],
            category=key,
            hf_repo=repo_id,
            hf_file=fpath,
            size_gb=size_gb,
            min_ram_gb=min_ram,
            min_gpu_pool_gb=min_gpu,
            quant=f"{quant} GGUF" if "GGUF" not in quant and fpath.endswith(".gguf") else quant,
            summary=f"Live Hugging Face Hub weight ({size_gb:.1f} GB) tuned to your hardware envelope",
            tags=("hub-live",),
            backends=backends,
            tuning_note=f"HF {quant}",
        )
        if not is_model_tuned_for_hardware(spec, prof):
            return None
        return spec

    results: list[HFModelSpec] = []
    if candidate_repos:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for sp in pool.map(_inspect_repo, candidate_repos):
                if sp is not None:
                    with _CACHE_LOCK:
                        _DYNAMIC_HF_MODELS[sp.id] = sp
                    results.append(sp)
                    if len(results) >= limit:
                        break

    with _CACHE_LOCK:
        _HF_SEARCH_CACHE[cache_key] = list(results)
    return results


def get_models_for_category(
    category: str,
    hw: HardwareProfile | None = None,
    include_incompatible: bool = False,
) -> list[HFModelSpec]:
    """Return strictly hardware-tuned HF models for `category` sorted by local availability & fit."""
    key = category.lower()
    prof = hw or detect_hardware_profile()
    rank_map = {"best_fit": 0, "fits": 1, "heavy": 2}
    raw_items = [m for m in CURATED_HF_MODELS if m.category == key]
    local_map = {m.id: (find_local_model_path(m) is not None) for m in raw_items}
    if include_incompatible:
        items = raw_items
    else:
        items = [m for m in raw_items if is_model_tuned_for_hardware(m, prof, local_installed=local_map[m.id])]

    prefer_capacity = prof.tier == "workstation"
    items.sort(
        key=lambda m: (
            0 if local_map[m.id] else 1,
            rank_map.get(evaluate_model_fit(m, prof)[0], 2),
            0 if "recommended" in m.tags else 1,
            (-m.min_ram_gb if prefer_capacity else m.size_gb),
            m.size_gb,
        )
    )
    return items


def get_recommended_models_for_hardware(hw: HardwareProfile) -> dict[str, HFModelSpec]:
    out: dict[str, HFModelSpec] = {}
    for cat in ("text", "image", "video"):
        ordered = get_models_for_category(cat, hw=hw)
        if ordered:
            out[cat] = ordered[0]
    return out


def get_video_duration_options(
    hw: HardwareProfile,
    video_spec: HFModelSpec | None = None,
) -> tuple[list[int], str]:
    """Compute available video clip durations scaled by GPU hardware tier & video model footprint.

    Lighter/quantized video models or stronger GPUs unlock longer sliding-window clip lengths;
    heavier checkpoints or tighter memory envelopes scale durations down safely.
    """
    model_name = video_spec.name if video_spec is not None else "2B Video"
    size_gb = float(video_spec.size_gb) if video_spec is not None else 3.2
    quant_low = (video_spec.quant if video_spec is not None else "").lower()
    is_ultra_light = size_gb <= 2.2 or "q4" in quant_low or "q5" in quant_low
    is_light = size_gb <= 5.0
    is_medium = 5.0 < size_gb <= 10.0

    if hw.tier == "workstation" and hw.gpu_pool_gb >= 15.0:
        if is_ultra_light:
            durations = [4, 8, 12, 16, 24, 32]
        elif is_light:
            durations = [4, 8, 12, 16, 24]
        elif is_medium:
            durations = [4, 8, 12, 16]
        else:
            durations = [4, 8, 12]
    elif hw.tier == "low" or hw.gpu_pool_gb < 7.0:
        durations = [2, 4, 8] if is_ultra_light else [2, 4]
    else:
        # Balanced (e.g. 14–16 GB Vulkan APU or 8–12 GB dGPU)
        if is_ultra_light:
            durations = [4, 8, 12, 16]
        elif is_light:
            durations = [4, 8, 12]
        elif is_medium:
            durations = [4, 8]
        else:
            durations = [4]

    tier_lbl = "Workstation" if hw.tier == "workstation" else ("Eco/Low" if hw.tier == "low" else "Balanced")
    pool_lbl = f"{hw.gpu_pool_gb:.0f} GB {hw.backend.upper()}" if hw.gpu_pool_gb > 0 else f"{hw.total_ram_gb:.0f} GB RAM"
    reason = f"Up to {durations[-1]}s · tuned for {model_name} ({size_gb:.1f} GB) on {tier_lbl} ({pool_lbl})"
    return (durations, reason)


def get_active_video_duration_options(hw: HardwareProfile | None = None) -> tuple[list[int], str]:
    """Resolve the active video model + detected hardware and return tuned clip lengths."""
    eff_hw = hw or detect_hardware_profile()
    sel_id = get_selected_model_id("video", hw=eff_hw)
    spec = _resolve_model_spec(sel_id, "video")
    if spec is None:
        models = get_models_for_category("video", hw=eff_hw)
        spec = models[0] if models else None
    return get_video_duration_options(eff_hw, spec)


def fetch_hf_repo_meta(hf_repo: str, timeout_s: float = 3.0) -> dict[str, Any] | None:
    """Fetch public Hugging Face model metadata (`downloads`, `likes`) with caching."""
    with _CACHE_LOCK:
        if hf_repo in _HF_META_CACHE:
            return dict(_HF_META_CACHE[hf_repo])
    url = f"https://huggingface.co/api/models/{hf_repo}"
    req = urllib.request.Request(url, headers={"User-Agent": "Vimona/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict):
                meta = {
                    "id": str(data.get("id") or hf_repo),
                    "downloads": int(data.get("downloads") or 0),
                    "likes": int(data.get("likes") or 0),
                }
                with _CACHE_LOCK:
                    _HF_META_CACHE[hf_repo] = meta
                return meta
    except Exception:
        return None
    return None


def validate_hf_model_metadata(spec: HFModelSpec, timeout_s: float = 4.0) -> dict[str, Any]:
    """Verify Hugging Face Hub repository/file existence, size & SHA-256 OID (`C12`)."""
    sub = spec.hf_file.rsplit("/", 1)[0] if "/" in spec.hf_file else ""
    turl = f"https://huggingface.co/api/models/{spec.hf_repo}/tree/main" + (f"/{sub}" if sub else "")
    req = urllib.request.Request(turl, headers={"User-Agent": "Vimona/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            files = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Metadata check failed: {exc}"}
    if not isinstance(files, list):
        return {"ok": False, "error": "Unexpected Hugging Face file tree payload"}
    hit = next((f for f in files if isinstance(f, dict) and f.get("path") == spec.hf_file), None)
    if hit is None:
        return {"ok": False, "error": f"File missing on Hugging Face repo: {spec.hf_file}"}
    remote_bytes = int(hit.get("size") or 0)
    remote_sha = str(((hit.get("lfs") or {}).get("oid")) or "").lower().strip()
    pinned_bytes, pinned_sha = get_expected_checksum(spec)
    outdated = False
    if pinned_bytes > 0 and remote_bytes > 0 and pinned_bytes != remote_bytes:
        outdated = True
    if pinned_sha and remote_sha and pinned_sha != remote_sha:
        outdated = True
    return {
        "ok": True,
        "remote_bytes": remote_bytes,
        "remote_sha256": remote_sha,
        "outdated_vs_pinned": outdated,
    }


def is_file_compatible_with_backend(
    spec: HFModelSpec,
    path: Path,
    hw: HardwareProfile | None = None,
) -> tuple[bool, str]:
    """Validate weight file magic/structure + hardware backend compatibility (`C11`)."""
    if not path.is_file():
        return (False, "Model file does not exist")
    prof = hw or detect_hardware_profile()
    if prof.backend not in spec.backends:
        return (False, f"Model requires {'/'.join(spec.backends)} backend, active is {prof.backend}")
    low = path.name.lower()
    try:
        size = path.stat().st_size
    except OSError as exc:
        return (False, str(exc))
    if size < 1024:
        return (False, "Model file truncated (<1 KB)")
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return (False, str(exc))

    if low.endswith(".gguf"):
        if not head.startswith(b"GGUF"):
            return (False, "Invalid GGUF magic header (file corrupt or HTML error page)")
        return (True, "GGUF header valid")
    if low.endswith(".safetensors"):
        if len(head) < 8:
            return (False, "Truncated safetensors header")
        hdr_len = int.from_bytes(head[:8], "little")
        if hdr_len <= 0 or hdr_len > 100 * 1024 * 1024 or (8 + hdr_len) > size:
            return (False, "Corrupt safetensors header length")
        return (True, "Safetensors header valid")
    return (True, "Binary present")


_INTEGRITY_CACHE: dict[tuple[str, int, int], tuple[bool, str]] = {}


def _compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(2 * 1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest().lower()


def verify_local_model_integrity(
    spec: HFModelSpec,
    path: Path,
    *,
    expected_sha256: str = "",
    expected_bytes: int = 0,
    check_hash: bool = True,
) -> tuple[bool, str]:
    """Verify local model file size, container header, & SHA-256 digest (`C1`–`C3`)."""
    if not path.is_file():
        return (False, "File missing")
    try:
        st = path.stat()
    except OSError as exc:
        return (False, str(exc))
    cache_key = (str(path), int(st.st_size), int(st.st_mtime_ns))

    exp_bytes, exp_sha = get_expected_checksum(spec)
    if expected_bytes > 0:
        exp_bytes = expected_bytes
    if expected_sha256:
        exp_sha = expected_sha256.lower().strip()

    if exp_bytes > 0 and st.st_size != exp_bytes:
        return (False, f"Size mismatch: got {st.st_size} bytes, expected {exp_bytes} bytes")
    if st.st_size < 1024 * 1024:
        return (False, f"File unexpectedly small ({st.st_size} bytes)")

    compat_ok, compat_msg = is_file_compatible_with_backend(spec, path)
    if not compat_ok:
        return (False, compat_msg)

    if check_hash and exp_sha:
        with _CACHE_LOCK:
            cached = _INTEGRITY_CACHE.get(cache_key)
        if cached is not None:
            return cached
        sidecar = path.with_suffix(path.suffix + ".vimona.sha256")
        if sidecar.is_file():
            try:
                sc = json.loads(sidecar.read_text(encoding="utf-8"))
                if (
                    int(sc.get("size") or -1) == st.st_size
                    and int(sc.get("mtime_ns") or -1) == st.st_mtime_ns
                    and str(sc.get("sha256") or "").lower() == exp_sha
                ):
                    res = (True, f"SHA-256 verified ({exp_sha[:12]}…)")
                    with _CACHE_LOCK:
                        _INTEGRITY_CACHE[cache_key] = res
                    return res
            except Exception:
                pass
        actual_sha = _compute_file_sha256(path)
        if actual_sha != exp_sha:
            res = (False, f"SHA-256 mismatch (got {actual_sha[:12]}…, expected {exp_sha[:12]}…)")
            with _CACHE_LOCK:
                _INTEGRITY_CACHE[cache_key] = res
            return res
        try:
            sidecar.write_text(
                json.dumps({"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": actual_sha}),
                encoding="utf-8",
            )
        except OSError:
            pass
        res = (True, f"SHA-256 verified ({actual_sha[:12]}…)")
        with _CACHE_LOCK:
            _INTEGRITY_CACHE[cache_key] = res
        return res

    return (True, "Header & size verified")


def _validate_download_url(url_str: str) -> None:
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme != "https":
        raise ModelDownloadError(f"Blocked non-HTTPS download URL: {url_str}")
    host = (parsed.hostname or "").lower()
    allowed = _DOWNLOAD_CONFIG.allowed_https_suffixes
    if not any(host == s or host.endswith(s) for s in allowed):
        raise ModelDownloadError(f"Blocked untrusted download host: {host}")


def download_hf_model(
    spec: HFModelSpec,
    on_progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    expected_sha256: str = "",
    expected_bytes: int = 0,
) -> Path:
    """Download a model atomically with SHA-256, Range resume, disk check, & concurrency lock (`C1`–`C12`)."""
    roots = _search_roots_for_category(spec.category)
    dest_dir = next((r for r in roots if r.is_dir()), roots[-1])

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / Path(spec.hf_file).name
    dl_key = str(dest_path.resolve()) if dest_path.parent.exists() else str(dest_path)

    with _DOWNLOAD_LOCK:
        if dl_key in _ACTIVE_DOWNLOADS:
            raise ModelDownloadError(f"Download already in progress for {spec.name}")
        _ACTIVE_DOWNLOADS.add(dl_key)

    part_path = dest_path.with_suffix(dest_path.suffix + ".part")
    lock_path = dest_path.with_suffix(dest_path.suffix + ".part.lock")
    lock_fh = None
    try:
        lock_fh = open(lock_path, "a+b")
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ModelDownloadError(f"Another process is already downloading {spec.name}") from exc

        exp_bytes, exp_sha = get_expected_checksum(spec)
        if expected_bytes > 0:
            exp_bytes = expected_bytes
        if expected_sha256:
            exp_sha = expected_sha256.lower().strip()

        # C2: Verify existing installed file BEFORE accepting it
        if dest_path.is_file():
            ok_existing, err_existing = verify_local_model_integrity(
                spec,
                dest_path,
                expected_sha256=exp_sha,
                expected_bytes=exp_bytes,
                check_hash=bool(exp_sha),
            )
            if ok_existing:
                set_selected_model_id(spec.category, spec.id, spec_obj=spec)
                return dest_path
            # Corrupt or truncated pre-existing file: move aside cleanly before re-downloading
            corrupt_bak = dest_path.with_suffix(dest_path.suffix + ".corrupt")
            dest_path.replace(corrupt_bak)
            corrupt_bak.unlink(missing_ok=True)

        # C7 & C8: Configurable size limits and disk preflight check
        cfg = get_download_config()
        est_bytes = exp_bytes if exp_bytes > 0 else int(max(0.1, spec.size_gb) * (1024 ** 3))
        if est_bytes > cfg.max_file_bytes:
            raise ModelDownloadError(
                f"Model {spec.name} ({est_bytes // (1024**2)} MB) exceeds configured max limit "
                f"({cfg.max_file_bytes // (1024**2)} MB)."
            )

        resume_from = 0
        if part_path.is_file():
            p_size = part_path.stat().st_size
            if 0 < p_size and (exp_bytes <= 0 or p_size <= exp_bytes):
                resume_from = p_size
            else:
                part_path.unlink(missing_ok=True)

        needed_bytes = max(1024 * 1024, est_bytes - resume_from)
        disk_free = shutil.disk_usage(dest_dir).free
        reserve = max(512 * 1024 * 1024, int(est_bytes * 0.05))
        if disk_free < (needed_bytes + reserve):
            raise ModelDownloadError(
                f"Insufficient disk space in {dest_dir}: {disk_free / (1024**3):.1f} GB free, "
                f"need {(needed_bytes + reserve) / (1024**3):.1f} GB."
            )

        _validate_download_url(spec.download_url)
        headers = {"User-Agent": "Vimona/1.0"}
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
        req = urllib.request.Request(spec.download_url, headers=headers)

        keep_part_on_error = False
        try:
            with urllib.request.urlopen(req, timeout=cfg.connect_timeout_s) as resp:
                resp_url = resp.geturl() if hasattr(resp, "geturl") else spec.download_url
                if resp_url:
                    _validate_download_url(resp_url)
                status_code = int(getattr(resp, "status", 200) or 200)
                if status_code >= 400:
                    raise ModelDownloadError(f"HTTP {status_code} fetching {spec.name}")

                if not exp_sha:
                    etag = str(resp.headers.get("X-Linked-Etag") or resp.headers.get("ETag") or "").strip('"').lower()
                    if re.fullmatch(r"[0-9a-f]{64}", etag):
                        exp_sha = etag

                cl_raw = resp.headers.get("Content-Length") or "0"
                content_len = int(cl_raw) if str(cl_raw).isdigit() else 0
                if content_len > cfg.max_file_bytes:
                    raise ModelDownloadError(f"Remote Content-Length ({content_len}) exceeds limit.")

                if status_code == 206 and resume_from > 0:
                    mode = "ab"
                    read_b = resume_from
                    total = (resume_from + content_len) if content_len > 0 else exp_bytes
                else:
                    mode = "wb"
                    read_b = 0
                    resume_from = 0
                    total = content_len if content_len > 0 else exp_bytes

                if exp_bytes > 0 and content_len > 0 and (resume_from + content_len) != exp_bytes and status_code in (200, 206):
                    raise ModelDownloadError(
                        f"Content-Length mismatch for {spec.name}: expected {exp_bytes} total bytes, server reported {resume_from + content_len}."
                    )

                throttle_bps = cfg.max_mb_per_sec * 1024.0 * 1024.0
                stream_t0 = time.monotonic()
                session_bytes = 0
                keep_part_on_error = True
                with open(part_path, mode) as fh:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise InterruptedError("Download cancelled (partial progress saved)")
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        read_b += len(chunk)
                        session_bytes += len(chunk)
                        if throttle_bps > 0:
                            expected_t = session_bytes / throttle_bps
                            elapsed_t = time.monotonic() - stream_t0
                            if expected_t > elapsed_t:
                                time.sleep(min(0.25, expected_t - elapsed_t))
                        if on_progress is not None:
                            frac = (read_b / total) if total > 0 else 0.0
                            mb_done = read_b / (1024.0 * 1024.0)
                            mb_tot = total / (1024.0 * 1024.0) if total > 0 else 0.0
                            lbl = f"{mb_done:.0f} / {mb_tot:.0f} MB" if total > 0 else f"{mb_done:.0f} MB"
                            on_progress(min(1.0, frac), lbl)
                    fh.flush()
                    os.fsync(fh.fileno())

            # Detect incomplete Content-Length delivery (`C4`)
            if content_len > 0 and session_bytes != content_len:
                raise ModelDownloadError(
                    f"Incomplete download for {spec.name}: got {session_bytes} of {content_len} bytes."
                )

            if read_b < 1024 * 1024:
                keep_part_on_error = False
                raise ModelDownloadError("Downloaded file unexpectedly tiny (< 1 MB); check repo availability.")

            # Verify final size + SHA-256 + backend file header BEFORE atomic install (`C1`, `C3`, `C10`, `C11`)
            ok_part, err_part = verify_local_model_integrity(
                spec,
                part_path,
                expected_sha256=exp_sha,
                expected_bytes=exp_bytes,
                check_hash=bool(exp_sha),
            )
            if not ok_part:
                keep_part_on_error = False
                raise ModelDownloadError(f"Integrity verification failed for {spec.name}: {err_part}")

            part_path.replace(dest_path)
            # Fsync directory entry for crash consistency (`C10`)
            try:
                dfd = os.open(str(dest_dir), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass

        except Exception:
            if not keep_part_on_error or not part_path.is_file() or part_path.stat().st_size < 1024 * 1024:
                part_path.unlink(missing_ok=True)
            raise

        set_selected_model_id(spec.category, spec.id, spec_obj=spec)
        return dest_path
    finally:
        with _DOWNLOAD_LOCK:
            _ACTIVE_DOWNLOADS.discard(dl_key)
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                lock_fh.close()
            except OSError:
                pass
        lock_path.unlink(missing_ok=True)
