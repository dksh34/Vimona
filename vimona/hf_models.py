"""Hardware-aware Hugging Face curated model catalog, matcher, and downloader."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

MODELS_STATE_FILE = Path("~/.local/share/vimona/models_config.json").expanduser()
LOCAL_MODELS_DIR = Path("~/.local/share/vimona/models").expanduser()

_HF_META_CACHE: dict[str, dict[str, Any]] = {}
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

    @property
    def summary_label(self) -> str:
        gpu = self.gpu_name or "Integrated GPU"
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

    @property
    def hf_url(self) -> str:
        return f"https://huggingface.co/{self.hf_repo}"

    @property
    def download_url(self) -> str:
        return f"https://huggingface.co/{self.hf_repo}/resolve/main/{self.hf_file}?download=true"


CURATED_HF_MODELS: tuple[HFModelSpec, ...] = (
    # --- Lightweight Text Models ---
    HFModelSpec(
        id="hf_qwen25_7b_q4",
        name="Qwen 2.5 7B Instruct",
        category="text",
        hf_repo="bartowski/Qwen2.5-7B-Instruct-GGUF",
        hf_file="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        size_gb=4.7,
        min_ram_gb=10.0,
        quant="Q4_K_M GGUF",
        summary="Strong reasoning, coding & tool use on 16 GB RAM",
        tags=("recommended", "coding", "chat"),
    ),
    HFModelSpec(
        id="hf_qwen3_8b_q4",
        name="Qwen 3 8B",
        category="text",
        hf_repo="Qwen/Qwen3-8B-GGUF",
        hf_file="Qwen3-8B-Q4_K_M.gguf",
        size_gb=5.0,
        min_ram_gb=10.0,
        quant="Q4_K_M GGUF",
        summary="Crisp structured thinking & multi-turn instruction accuracy",
        tags=("reasoning", "chat"),
    ),
    HFModelSpec(
        id="hf_llama31_8b_q4",
        name="Llama 3.1 8B Instruct",
        category="text",
        hf_repo="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        hf_file="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        size_gb=4.9,
        min_ram_gb=10.0,
        quant="Q4_K_M GGUF",
        summary="Fast fluent prose & general workstation dialogue",
        tags=("chat", "writing"),
    ),
    HFModelSpec(
        id="hf_qwen25_coder_7b_q4",
        name="Qwen 2.5 Coder 7B",
        category="text",
        hf_repo="bartowski/Qwen2.5-Coder-7B-Instruct-GGUF",
        hf_file="Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
        size_gb=4.7,
        min_ram_gb=10.0,
        quant="Q4_K_M GGUF",
        summary="High-accuracy Python, GTK4, SQL & systems debugging",
        tags=("coding",),
    ),
    HFModelSpec(
        id="hf_gemma3_4b_q4",
        name="Gemma 3 4B IT",
        category="text",
        hf_repo="bartowski/google_gemma-3-4b-it-GGUF",
        hf_file="google_gemma-3-4b-it-Q4_K_M.gguf",
        size_gb=2.5,
        min_ram_gb=6.0,
        quant="Q4_K_M GGUF",
        summary="Ultra-lightweight 4B footprint ideal for 8 GB laptops & Eco mode",
        tags=("ultra-light", "eco"),
    ),
    HFModelSpec(
        id="hf_qwen25_3b_q6",
        name="Qwen 2.5 3B Instruct",
        category="text",
        hf_repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
        hf_file="qwen2.5-3b-instruct-q6_k.gguf",
        size_gb=2.6,
        min_ram_gb=6.0,
        quant="Q6_K GGUF",
        summary="Sub-second local router & snappy prompt engineering",
        tags=("ultra-light", "fast"),
    ),
    HFModelSpec(
        id="hf_qwen25_14b_q4",
        name="Qwen 2.5 14B Instruct",
        category="text",
        hf_repo="bartowski/Qwen2.5-14B-Instruct-GGUF",
        hf_file="Qwen2.5-14B-Instruct-Q4_K_M.gguf",
        size_gb=9.0,
        min_ram_gb=20.0,
        quant="Q4_K_M GGUF",
        summary="Heavyweight workstation model (needs 24 GB+ RAM/VRAM)",
        tags=("heavy",),
    ),
    # --- High-Quality Lightweight Image Models ---
    HFModelSpec(
        id="hf_pony_v6_xl_q8",
        name="Pony Diffusion V6 XL",
        category="image",
        hf_repo="city96/PonyDiffusionV6XL-gguf",
        hf_file="pony-diffusion-v6-xl-Q8_0.gguf",
        size_gb=6.8,
        min_ram_gb=12.0,
        quant="Q8_0 GGUF",
        summary="Active Vulkan keyframe engine: sharp composition & stylized/DSLR versatility",
        tags=("recommended", "active-default"),
    ),
    HFModelSpec(
        id="hf_realvis_xl_v5_lightning",
        name="RealVisXL V5.0 Lightning",
        category="image",
        hf_repo="SG161222/RealVisXL_V5.0_Lightning",
        hf_file="RealVisXL_V5.0_Lightning_fp16.safetensors",
        size_gb=6.5,
        min_ram_gb=12.0,
        quant="FP16 Lightning (4-step)",
        summary="Crisp natural DSLR skin pores, eyes & architecture in 4–6 steps",
        tags=("recommended", "photoreal"),
    ),
    HFModelSpec(
        id="hf_juggernaut_xl_lightning",
        name="Juggernaut XL v9 Lightning",
        category="image",
        hf_repo="RunDiffusion/Juggernaut-XL-v9",
        hf_file="JuggernautXL_v9_RunDiffusionPhoto_v2.safetensors",
        size_gb=6.6,
        min_ram_gb=12.0,
        quant="FP16 SDXL",
        summary="Cinema-grade moody photography, cars & realistic lighting",
        tags=("photoreal", "cinema"),
    ),
    HFModelSpec(
        id="hf_dreamshaper_xl_turbo",
        name="DreamShaper XL Turbo v2",
        category="image",
        hf_repo="Lykon/dreamshaper-xl-v2-turbo",
        hf_file="DreamShaperXL_Turbo_v2_1.safetensors",
        size_gb=6.5,
        min_ram_gb=12.0,
        quant="FP16 Turbo (6-step)",
        summary="Balanced fast illustration & glossy editorial scenes",
        tags=("art", "fast"),
    ),
    HFModelSpec(
        id="hf_sdxl_turbo_fp16",
        name="SDXL Turbo 1.0",
        category="image",
        hf_repo="stabilityai/sdxl-turbo",
        hf_file="sd_xl_turbo_1.0_fp16.safetensors",
        size_gb=6.5,
        min_ram_gb=10.0,
        quant="FP16 Distilled",
        summary="1–4 step rapid drafts & ultra-low thermal Eco rendering",
        tags=("ultra-light", "fast"),
    ),
    HFModelSpec(
        id="hf_flux1_dev_q4",
        name="FLUX.1-dev",
        category="image",
        hf_repo="city96/FLUX.1-dev-gguf",
        hf_file="flux1-dev-Q4_K_S.gguf",
        size_gb=6.8,
        min_ram_gb=20.0,
        quant="Q4_K_S GGUF",
        summary="High prompt adherence & typography (needs 20 GB+ RAM w/ T5)",
        tags=("heavy",),
    ),
    # --- Video Models (Shown Automatically in Video Chat) ---
    HFModelSpec(
        id="hf_wan21_vace_1_3b",
        name="Wan 2.1 VACE 1.3B",
        category="video",
        hf_repo="Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        hf_file="split_files/diffusion_models/wan2.1_vace_1.3B_fp16.safetensors",
        size_gb=2.9,
        min_ram_gb=12.0,
        quant="FP16 (6-step CausVid)",
        summary="Active 720p I2V & keyframe anchor animation tuned for 16 GB iGPUs",
        tags=("recommended", "active-default"),
    ),
    HFModelSpec(
        id="hf_wan21_t2v_1_3b",
        name="Wan 2.1 T2V 1.3B",
        category="video",
        hf_repo="Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        hf_file="split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors",
        size_gb=2.8,
        min_ram_gb=12.0,
        quant="FP16",
        summary="Fast direct text-to-video synthesis for lightweight laptops",
        tags=("recommended", "fast"),
    ),
    HFModelSpec(
        id="hf_ltx_video_2b",
        name="LTX-Video 0.9.6 2B",
        category="video",
        hf_repo="Lightricks/LTX-Video",
        hf_file="ltxv-2b-0.9.6-dev-04-25.safetensors",
        size_gb=3.4,
        min_ram_gb=12.0,
        quant="FP8 Lightweight",
        summary="Rapid temporal-coherent landscape & atmospheric motion",
        tags=("fast",),
    ),
    HFModelSpec(
        id="hf_wan21_i2v_14b_q4",
        name="Wan 2.1 I2V 14B 480p",
        category="video",
        hf_repo="city96/Wan2.1-I2V-14B-480P-gguf",
        hf_file="wan2.1-i2v-14b-480p-Q4_K_M.gguf",
        size_gb=9.2,
        min_ram_gb=22.0,
        quant="Q4_K_M GGUF",
        summary="Flagship 14B physical motion fidelity (needs 24 GB+ RAM)",
        tags=("heavy",),
    ),
    HFModelSpec(
        id="hf_wan21_t2v_14b_q4",
        name="Wan 2.1 T2V 14B",
        category="video",
        hf_repo="city96/Wan2.1-T2V-14B-gguf",
        hf_file="wan2.1-t2v-14b-Q4_K_M.gguf",
        size_gb=8.8,
        min_ram_gb=22.0,
        quant="Q4_K_M GGUF",
        summary="High-capacity 14B cinema generation (needs 24 GB+ RAM)",
        tags=("heavy",),
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


def _detect_gpu() -> tuple[str, float, str, bool]:
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
                return (name, vram, "cuda", False)
    except Exception:
        pass

    # 2. Try Linux DRM / Vulkan summary for AMD/Intel iGPU or dGPU
    drm_root = Path("/sys/class/drm")
    gpu_name = ""
    vram_gb = 0.0
    if drm_root.is_dir():
        for card in sorted(drm_root.glob("card[0-9]/device")):
            vram_file = card / "mem_info_vram_total"
            if vram_file.is_file():
                try:
                    raw_b = int(vram_file.read_text(encoding="utf-8").strip())
                    vram_gb = max(vram_gb, round(raw_b / (1024.0 ** 3), 1))
                except Exception:
                    pass
            uev = card / "uevent"
            if uev.is_file() and not gpu_name:
                try:
                    txt = uev.read_text(encoding="utf-8", errors="ignore")
                    if "amdgpu" in txt.lower():
                        gpu_name = "AMD Radeon Vulkan"
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
        return (gpu_name, vram_gb, "vulkan", is_uma)
    return ("CPU Fallback", 0.0, "cpu", True)


def detect_hardware_profile() -> HardwareProfile:
    tot_ram, avail_ram = _read_total_and_avail_ram_gb()
    gpu_name, vram_gb, backend, is_uma = _detect_gpu()
    return HardwareProfile(
        cpu_name=_detect_cpu_name(),
        cpu_cores=os.cpu_count() or 4,
        total_ram_gb=tot_ram,
        avail_ram_gb=avail_ram,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        backend=backend,
        unified_memory=is_uma,
    )


def evaluate_model_fit(spec: HFModelSpec, hw: HardwareProfile) -> tuple[str, str]:
    """Return `(tier, label)` matching the hardware profile honestly."""
    usable = hw.total_ram_gb if hw.unified_memory else max(hw.vram_gb, hw.total_ram_gb * 0.75)
    if usable >= spec.min_ram_gb + 2.0:
        if "recommended" in spec.tags or spec.size_gb <= max(4.0, usable * 0.45):
            return ("best_fit", f"Recommended · {spec.size_gb:.1f} GB")
        return ("fits", f"Fits {hw.total_ram_gb:.0f} GB · {spec.size_gb:.1f} GB")
    if usable >= spec.min_ram_gb - 1.5:
        return ("fits", f"Fits · {spec.size_gb:.1f} GB")
    return ("heavy", f"Needs {spec.min_ram_gb:.0f}+ GB")


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


def get_selected_model_id(category: str, hw: HardwareProfile | None = None) -> str:
    cfg = _load_models_config()
    key = category.lower()
    saved = cfg.get(key)
    if saved and any(m.id == saved and m.category == key for m in CURATED_HF_MODELS):
        return saved
    prof = hw or detect_hardware_profile()
    return get_recommended_models_for_hardware(prof)[key].id


def set_selected_model_id(category: str, model_id: str) -> HFModelSpec | None:
    key = category.lower()
    spec = next((m for m in CURATED_HF_MODELS if m.id == model_id and m.category == key), None)
    if spec is None:
        return None
    cfg = _load_models_config()
    cfg[key] = model_id
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
        spec = next((m for m in CURATED_HF_MODELS if m.id == sel_id), None)
        if spec is not None:
            return find_local_model_path(spec)
    return None


def get_models_for_category(category: str, hw: HardwareProfile | None = None) -> list[HFModelSpec]:
    """Return handpicked HF models for `category` sorted cleanly with hardware best-fits first."""
    key = category.lower()
    prof = hw or detect_hardware_profile()
    rank_map = {"best_fit": 0, "fits": 1, "heavy": 2}
    items = [m for m in CURATED_HF_MODELS if m.category == key]
    items.sort(
        key=lambda m: (
            0 if find_local_model_path(m) is not None else 1,
            rank_map.get(evaluate_model_fit(m, prof)[0], 2),
            m.size_gb,
        )
    )
    return items


def get_recommended_models_for_hardware(hw: HardwareProfile) -> dict[str, HFModelSpec]:
    out: dict[str, HFModelSpec] = {}
    for cat in ("text", "image", "video"):
        ordered = get_models_for_category(cat, hw=hw)
        out[cat] = ordered[0]
    return out


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
        set_selected_model_id(spec.category, spec.id)
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

    set_selected_model_id(spec.category, spec.id)
    return dest_path
