"""ComfyUI API workflow builders for Pony V6 XL (GGUF) and Wan 2.1 VACE 1.3B."""

from __future__ import annotations

from pathlib import Path
import random
from typing import Any

LORAS_DIR = Path("~/comfyui/models/loras").expanduser()
DMD2_SDXL_LORA = "dmd2_sdxl_4step_lora_fp16.safetensors"
WAN_CAUSVID_LORA = "Wan21_CausVid_bidirect2_T2V_1_3B_lora_rank32.safetensors"

DEFAULT_NEGATIVE_PONY = (
    "score_4, score_3, score_2, score_1, lowres, bad anatomy, bad hands, "
    "missing fingers, extra digits, blurry, watermark, text, signature"
)

DEFAULT_NEGATIVE_WAN = (
    "static, frozen, blurry, overexposed, low quality, jpeg artifacts, "
    "warped anatomy, morphing face, flicker, text, watermark"
)


def build_pony_keyframe_workflow(
    positive_prompt: str,
    negative_prompt: str = "",
    batch_size: int = 1,
    width: int = 768,
    height: int = 448,
    seed: int | None = None,
    filename_prefix: str = "vimona_kf",
    use_fast_lora: bool = True,
    reference_image_name: str | None = None,
    denoise_strength: float = 0.72,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build Pony V6 XL GGUF keyframe generation API payload.

    When DMD2 4-step LoRA is available, uses 6-step LCM (CFG 1.7) for ~3-4x speedup
    while preserving Pony V6 XL anatomy and style accuracy.
    """
    actual_seed = seed if seed is not None else random.randint(1, 2_147_483_647)
    pos = positive_prompt.strip()
    if "score_9" not in pos:
        pos = f"score_9, score_8_up, score_7_up, score_6_up, {pos}"
    neg = negative_prompt.strip() or DEFAULT_NEGATIVE_PONY
    clamped_batch = max(1, min(8, int(batch_size)))
    if overrides and overrides.get("booster") is False:
        use_fast_lora = False
    has_dmd2 = use_fast_lora and (LORAS_DIR / DMD2_SDXL_LORA).is_file()
    eff_cfg = float(overrides["cfg"]) if (overrides and isinstance(overrides.get("cfg"), (int, float))) else (1.65 if has_dmd2 else 6.5)
    eff_sampler = str(overrides["sampler"]).strip() if (overrides and overrides.get("sampler")) else ("lcm" if has_dmd2 else "euler_ancestral")

    wf: dict[str, Any] = {
        "1": {
            "class_type": "UnetLoaderGGUF",
            "inputs": {
                "unet_name": "pony-diffusion-v6-xl-Q8_0.gguf",
            },
        },
        "2": {
            "class_type": "DualCLIPLoader",
            "inputs": {
                "clip_name1": "clip_l.safetensors",
                "clip_name2": "clip_g.safetensors",
                "type": "sdxl",
            },
        },
        "3": {
            "class_type": "CLIPSetLastLayer",
            "inputs": {
                "clip": ["2", 0],
                "stop_at_clip_layer": -2,
            },
        },
        "4": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": "sdxl_vae.safetensors",
            },
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": pos,
                "clip": ["3", 0],
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": neg,
                "clip": ["3", 0],
            },
        },
        "7": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": width,
                "height": height,
                "batch_size": clamped_batch,
            },
        },
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["15", 0] if has_dmd2 else ["1", 0],
                "positive": ["5", 0],
                "negative": ["6", 0],
                "latent_image": ["7", 0],
                "seed": actual_seed,
                "steps": 6 if has_dmd2 else 18,
                "cfg": eff_cfg,
                "sampler_name": eff_sampler,
                "scheduler": "simple" if has_dmd2 else "normal",
                "denoise": 1.0,
            },
        },
        "9": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["8", 0],
                "vae": ["4", 0],
            },
        },
        "10": {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["9", 0],
                "filename_prefix": filename_prefix,
            },
        },
    }
    if reference_image_name:
        wf["16"] = {
            "class_type": "LoadImage",
            "inputs": {"image": reference_image_name},
        }
        wf["17"] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["16", 0],
                "upscale_method": "bilinear",
                "width": width,
                "height": height,
                "crop": "center",
            },
        }
        wf["18"] = {
            "class_type": "VAEEncode",
            "inputs": {
                "pixels": ["17", 0],
                "vae": ["4", 0],
            },
        }
        wf["8"]["inputs"]["latent_image"] = ["18", 0]
        wf["8"]["inputs"]["denoise"] = max(0.35, min(0.92, float(denoise_strength)))
    if has_dmd2:
        wf["15"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["1", 0],
                "lora_name": DMD2_SDXL_LORA,
                "strength_model": 0.78,
            },
        }
    return wf


def build_wan_vace_window_workflow(
    motion_prompt: str,
    reference_image_name: str,
    control_start_image_name: str | None = None,
    negative_prompt: str = "",
    width: int = 480,
    height: int = 272,
    frames: int = 17,
    fps: int = 8,
    seed: int | None = None,
    filename_prefix: str = "vimona_win",
    use_fast_lora: bool = True,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fast Wan 2.1 VACE 1.3B sliding-window workflow.

    Uses CausVid 1.3B step-distilled LoRA + native EasyCache when available
    for 5x fewer UNet passes while keeping reference character fidelity.
    """
    ov = dict(overrides or {})
    if ov.get("booster") is False:
        use_fast_lora = False
    actual_seed = seed if seed is not None else random.randint(1, 2_147_483_647)
    neg = negative_prompt.strip() or DEFAULT_NEGATIVE_WAN
    valid_frames = max(5, ((int(frames) - 1) // 4) * 4 + 1)
    has_causvid = use_fast_lora and (LORAS_DIR / WAN_CAUSVID_LORA).is_file()
    flow_shift = float(ov.get("flow_shift") or 8.0)
    cfg_val = float(ov.get("cfg")) if ov.get("cfg") is not None else (1.0 if has_causvid else 5.0)
    sampler_name = str(ov.get("sampler") or "uni_pc")

    model_src: list[Any] = ["15", 0] if has_causvid else ["2", 0]

    workflow: dict[str, Any] = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "wan2.1_vace_1.3B_fp16.safetensors",
                "weight_dtype": "default",
            },
        },
        "2": {
            "class_type": "ModelSamplingSD3",
            "inputs": {
                "model": ["1", 0],
                "shift": flow_shift,
            },
        },
        "3": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "type": "wan",
                "device": "cpu",
            },
        },
        "4": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": "wan_2.1_vae.safetensors",
            },
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": motion_prompt.strip(),
                "clip": ["3", 0],
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": neg,
                "clip": ["3", 0],
            },
        },
        "7": {
            "class_type": "LoadImage",
            "inputs": {
                "image": reference_image_name,
            },
        },
        "8": {
            "class_type": "WanVaceToVideo",
            "inputs": {
                "positive": ["5", 0],
                "negative": ["6", 0],
                "vae": ["4", 0],
                "width": width,
                "height": height,
                "length": valid_frames,
                "batch_size": 1,
                "strength": 1.0,
                "reference_image": ["7", 0],
            },
        },
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "model": model_src,
                "positive": ["8", 0],
                "negative": ["8", 1],
                "latent_image": ["8", 2],
                "seed": actual_seed,
                "steps": 5 if has_causvid else 10,
                "cfg": cfg_val,
                "sampler_name": sampler_name,
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "10": {
            "class_type": "TrimVideoLatent",
            "inputs": {
                "samples": ["9", 0],
                "trim_amount": ["8", 3],
            },
        },
        "11": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["10", 0],
                "vae": ["4", 0],
            },
        },
        "12": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "images": ["11", 0],
                "frame_rate": fps,
                "loop_count": 0,
                "filename_prefix": filename_prefix,
                "format": "video/h264-mp4",
                "pingpong": False,
                "save_output": True,
                "pix_fmt": "yuv420p",
                "crf": 19,
            },
        },
    }

    if has_causvid:
        workflow["15"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["2", 0],
                "lora_name": WAN_CAUSVID_LORA,
                "strength_model": 0.60,
            },
        }

    if control_start_image_name:
        workflow["13"] = {
            "class_type": "LoadImage",
            "inputs": {
                "image": control_start_image_name,
            },
        }
        workflow["14"] = {
            "class_type": "SolidMask",
            "inputs": {
                "value": 0.0,
                "width": width,
                "height": height,
            },
        }
        workflow["8"]["inputs"]["control_video"] = ["13", 0]
        workflow["8"]["inputs"]["control_masks"] = ["14", 0]

    return workflow
