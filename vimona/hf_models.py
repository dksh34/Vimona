"""Hardware-aware Hugging Face catalog, live Hub discovery, matcher, and downloader."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

MODELS_STATE_FILE = Path("~/.local/share/vimona/models_config.json").expanduser()
LOCAL_MODELS_DIR = Path("~/.local/share/vimona/models").expanduser()

_HF_META_CACHE: dict[str, dict[str, Any]] = {}
_HF_SEARCH_CACHE: dict[tuple[str, str, str], list["HFModelSpec"]] = {}
_DYNAMIC_HF_MODELS: dict[str, "HFModelSpec"] = {}
_CACHE_LOCK = threading.Lock()


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

    @property
    def effective_ram_gb(self) -> float:
        """Usable memory headroom after reserving ~2.5 GB for OS/desktop."""
        return max(4.0, round(self.total_ram_gb - 2.5, 1))

    @property
    def gpu_pool_gb(self) -> float:
        """Memory budget directly addressable by GPU compute."""
        if self.backend == "cuda" and not self.unified_memory:
            return round(self.vram_gb, 1)
        if self.backend == "vulkan":
            if not self.unified_memory and self.vram_gb >= 6.0:
                return round(self.vram_gb, 1)
            if self.gtt_gb > 0:
                return round(self.vram_gb + self.gtt_gb, 1)
            return round(self.effective_ram_gb * 0.65, 1)
        return 0.0

    @property
    def tier(self) -> str:
        if self.total_ram_gb >= 24.0 and (
            self.gpu_pool_gb >= 16.0 or (self.unified_memory and self.total_ram_gb >= 28.0)
        ):
            return "workstation"
        if self.total_ram_gb < 11.0 or (self.backend == "cpu" and self.total_ram_gb < 16.0):
            return "low"
        return "balanced"

    @property
    def summary_label(self) -> str:
        gpu = self.gpu_name or "Integrated GPU"
        if self.backend == "cuda" and self.vram_gb > 0:
            return f"{gpu} · {self.vram_gb:.1f} GB VRAM · {self.total_ram_gb:.1f} GB RAM · CUDA"
        if self.backend == "vulkan" and self.unified_memory:
            return f"{gpu} · {self.gpu_pool_gb:.1f} GB GPU Pool · {self.total_ram_gb:.1f} GB RAM · VULKAN"
        return f"{gpu} · {self.total_ram_gb:.1f} GB RAM · {self.backend.upper()}"


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


def _read_total_and_avail_ram_gb() -> tuple[float, float]:
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
            tot = vals.get("MemTotal", 16 * 1024 * 1024) / (1024.0 * 1024.0)
            avail = vals.get("MemAvailable", vals.get("MemFree", tot * 1024 * 1024)) / (1024.0 * 1024.0)
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
    return (16.0, 10.0)


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


def _detect_gpu_details() -> tuple[str, float, str, bool, float]:
    # 1. Try nvidia-smi for discrete NVIDIA VRAM
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().splitlines()[0].split(",")
            if len(parts) >= 2:
                name = parts[0].strip()
                vram = round(float(parts[1].strip()) / 1024.0, 1)
                return (name, vram, "cuda", False, 0.0)
    except Exception:
        pass

    # 2. Try Linux DRM / Vulkan summary for AMD/Intel iGPU or dGPU
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
                        gpu_name = _gpu_name_from_cpu(_detect_cpu_name()) or "AMD Radeon Vulkan"
                    elif "i915" in txt.lower() or "xe" in txt.lower():
                        gpu_name = "Intel Arc / Iris Vulkan"
                except OSError:
                    pass

    try:
        r2 = subprocess.run(
            ["vulkaninfo", "--summary"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if r2.returncode == 0:
            for ln in r2.stdout.splitlines():
                if "deviceName" in ln and "=" in ln:
                    cand = ln.split("=", 1)[1].strip()
                    if cand and "llvmpipe" not in cand.lower():
                        gpu_name = cand
                        break
    except Exception:
        pass

    if gpu_name:
        is_uma = vram_gb <= 4.1
        return (gpu_name, vram_gb, "vulkan", is_uma, gtt_gb)
    return ("CPU Fallback", 0.0, "cpu", True, 0.0)


def _detect_gpu() -> tuple[str, float, str, bool]:
    name, vram_gb, backend, is_uma, _gtt = _detect_gpu_details()
    return (name, vram_gb, backend, is_uma)


def detect_hardware_profile() -> HardwareProfile:
    tot_ram, avail_ram = _read_total_and_avail_ram_gb()
    gpu_name, vram_gb, backend, is_uma, gtt_gb = _detect_gpu_details()
    return HardwareProfile(
        cpu_name=_detect_cpu_name(),
        cpu_cores=os.cpu_count() or 4,
        total_ram_gb=tot_ram,
        avail_ram_gb=avail_ram,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        backend=backend,
        unified_memory=is_uma,
        gtt_gb=gtt_gb,
    )


def evaluate_model_fit(spec: HFModelSpec, hw: HardwareProfile) -> tuple[str, str]:
    """Return `(tier, label)` matching the hardware GPU pool, backend, and RAM strictly."""
    if hw.backend not in spec.backends:
        req = "/".join(b.upper() for b in spec.backends)
        return ("heavy", f"Needs {req}")
    if hw.backend != "cpu" and spec.min_gpu_pool_gb > 0 and hw.gpu_pool_gb < spec.min_gpu_pool_gb:
        return ("heavy", f"Needs {spec.min_gpu_pool_gb:.0f}+ GB GPU")
    if hw.backend == "cpu" and spec.min_gpu_pool_gb > 4.0:
        return ("heavy", "Needs GPU")

    headroom = hw.total_ram_gb - spec.min_ram_gb
    if headroom >= 2.0 and (hw.backend != "cpu" or spec.size_gb <= 3.0):
        badge = spec.tuning_note or "Recommended"
        return ("best_fit", f"{badge} · {spec.size_gb:.1f} GB")
    if headroom >= 0.0 or (hw.total_ram_gb >= spec.min_ram_gb - 0.5 and spec.size_gb <= 3.0):
        return ("fits", f"Fits · {spec.size_gb:.1f} GB")
    return ("heavy", f"Needs {spec.min_ram_gb:.0f}+ GB")


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


def find_local_model_path(spec: HFModelSpec) -> Path | None:
    """Locate an installed model file matching `spec` locally across standard Vimona/Comfy dirs."""
    fname = Path(spec.hf_file).name
    for root in _search_roots_for_category(spec.category):
        cand = root / fname
        if cand.is_file() or cand.is_symlink():
            return cand
    # Special check for default symlinked light-uncensored.gguf -> check resolved filename
    if spec.category == "text":
        def_llm = Path("~/models/llm/light-uncensored.gguf").expanduser()
        if def_llm.exists():
            try:
                resolved = def_llm.resolve()
                if resolved.name.lower() == fname.lower():
                    return resolved
            except OSError:
                pass
            if spec.id == "hf_qwen25_7b_q4":
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
    if raw_p:
        p = Path(raw_p).expanduser()
        if p.exists():
            return p
    sel_id = cfg.get(key, "")
    if sel_id:
        spec = _resolve_model_spec(sel_id)
        if spec is not None:
            return find_local_model_path(spec)
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


def download_hf_model(
    spec: HFModelSpec,
    on_progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Download a curated GGUF/Safetensors model directly from Hugging Face cleanly."""
    dest_dir = _search_roots_for_category(spec.category)[-1]
    if spec.category == "text" and Path("~/models/llm").expanduser().is_dir():
        dest_dir = Path("~/models/llm").expanduser()
    elif spec.category == "image" and Path("~/comfyui/models/checkpoints").expanduser().is_dir():
        dest_dir = Path("~/comfyui/models/checkpoints").expanduser()
    elif spec.category == "video" and Path("~/comfyui/models/diffusion_models").expanduser().is_dir():
        dest_dir = Path("~/comfyui/models/diffusion_models").expanduser()

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / Path(spec.hf_file).name
    if dest_path.is_file():
        set_selected_model_id(spec.category, spec.id, spec_obj=spec)
        return dest_path

    part_path = dest_path.with_suffix(dest_path.suffix + ".part")
    req = urllib.request.Request(spec.download_url, headers={"User-Agent": "Vimona/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            read_b = 0
            with open(part_path, "wb") as fh:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("Download cancelled")
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    read_b += len(chunk)
                    if on_progress is not None:
                        frac = (read_b / total) if total > 0 else 0.0
                        mb_done = read_b / (1024.0 * 1024.0)
                        mb_tot = total / (1024.0 * 1024.0) if total > 0 else 0.0
                        lbl = f"{mb_done:.0f} / {mb_tot:.0f} MB" if total > 0 else f"{mb_done:.0f} MB"
                        on_progress(min(1.0, frac), lbl)
        if read_b < 1024 * 1024:
            raise RuntimeError("Downloaded file unexpectedly tiny; check repo availability.")
        part_path.replace(dest_path)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise

    set_selected_model_id(spec.category, spec.id, spec_obj=spec)
    return dest_path
