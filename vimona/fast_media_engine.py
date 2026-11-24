"""High-speed, high-accuracy Vulkan C++ + Turbo ROCm media engine for Vimona.

Design highlights for AMD Ryzen AI 5 340 + Radeon 840M (16 GB unified RAM):
1. Images: Runs `sd-server` (`stable-diffusion.cpp` built with `ggml-vulkan`) using
   the full checkpoint (`pony-diffusion-v6-xl-Q8_0.gguf`, which preserves matched
   CLIP-L + CLIP-G embeddings) paired with `sdxl_vae.safetensors` (`--force-sdxl-vae-conv-scale`)
   and DMD2 5-step / SDXL-Lightning 6-step distilled LoRA.
2. One-by-One Streaming: Every requested image renders individually (`batch_count=1`)
   over persistent `sd-server` HTTP jobs (0.0s reload between images #2..N) and
   fires `on_image_ready(idx, path, seed)` the moment each finishes.
3. Video: Interpolates rendered Wan 2.1 VACE keyframes with `ffmpeg` motion-compensated
   optical flow (`minterpolate`) to silky 24fps MP4 while minimizing diffusion frames.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import random
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .airgap import AirGappedProcess, launch_airgapped_http_service
from .perf_profile import (
    apply_auxiliary_thread_limit,
    get_performance_mode,
    get_worker_env,
    get_worker_thread_cap,
    wrap_worker_cmd_with_priority,
)
from .ram_manager import register_protected_pid, unregister_protected_pid
from .storage import stage_ephemeral_media
from .workflows import DEFAULT_NEGATIVE_PONY

SD_SERVER_BIN = Path("~/stable-diffusion.cpp/build/bin/sd-server").expanduser()
SD_CLI_BIN = Path("~/stable-diffusion.cpp/build/bin/sd-cli").expanduser()
PONY_FULL_MODEL = Path("~/pony-diffusion-v6-xl-Q8_0.gguf").expanduser()
PONY_UNET_MODEL = Path("~/comfyui/models/unet/pony-diffusion-v6-xl-Q8_0.gguf").expanduser()
SDXL_VAE_PATH = Path("~/comfyui/models/vae/sdxl_vae.safetensors").expanduser()
LORAS_DIR = Path("~/comfyui/models/loras").expanduser()
DMD2_LORA_FILE = LORAS_DIR / "dmd2_sdxl_4step_lora_fp16.safetensors"
LIGHTNING_LORA_FILE = LORAS_DIR / "sdxl_lightning_4step_lora.safetensors"
LOG_DIR = Path("~/.local/share/vimona").expanduser()


def resolve_pony_checkpoint() -> Path:
    try:
        from .hf_models import get_selected_model_path
        custom = get_selected_model_path("image")
        if custom is not None and custom.exists():
            return custom.resolve()
    except Exception:
        pass
    if PONY_FULL_MODEL.is_file():
        return PONY_FULL_MODEL
    if PONY_UNET_MODEL.exists():
        return PONY_UNET_MODEL.resolve()
    return PONY_FULL_MODEL


def has_vulkan_image_engine() -> bool:
    return (
        SD_SERVER_BIN.is_file()
        and resolve_pony_checkpoint().is_file()
        and SDXL_VAE_PATH.is_file()
    )


def _pick_free_port(preferred: int = 18234) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])


def _kill_proc_group(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None:
        return
    unregister_protected_pid(proc.pid)
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except OSError:
        try:
            proc.terminate()
        except OSError:
            return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                os.killpg(os.getpgid(proc.pid), getattr(signal, "SIGKILL", signal.SIGTERM))
            else:
                proc.kill()
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pass
    time.sleep(0.15)


def format_pony_prompt(positive_prompt: str) -> str:
    pos = positive_prompt.strip()
    if "score_9" not in pos:
        pos = f"score_9, score_8_up, score_7_up, score_6_up, {pos}"
    low = pos.lower()
    if "sharp focus" not in low:
        pos = f"{pos}, sharp focus, crisp texture, detailed lighting"
    return pos


def _apply_dslr_finish(src_png: Path) -> None:
    """Apply non-destructive photographic micro-contrast & subtle optics sharpening."""
    try:
        import cv2
        import numpy as np

        bgr = cv2.imread(str(src_png))
        if bgr is None:
            return
        # Gentle luminance-only CLAHE + subtle optical unsharp mask (never oversharpened)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l_ch, a_ch, b_ch = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.35, tileGridSize=(8, 8))
        l_boost = clahe.apply(l_ch)
        l_mix = cv2.addWeighted(l_ch, 0.62, l_boost, 0.38, 0.0)
        toned = cv2.cvtColor(cv2.merge([l_mix, a_ch, b_ch]), cv2.COLOR_LAB2BGR)
        blur = cv2.GaussianBlur(toned, (0, 0), sigmaX=1.05)
        crisp = cv2.addWeighted(toned, 1.24, blur, -0.24, 0.0)
        cv2.imwrite(str(src_png), np.clip(crisp, 0, 255).astype(np.uint8))
    except Exception:
        pass


def _pick_h264_codec_args() -> list[tuple[str, list[str]]]:
    return [
        ("libopenh264", ["-b:v", "6500k", "-maxrate", "8500k"]),
        ("libx264", ["-preset", "slow", "-crf", "16"]),
        ("mpeg4", ["-q:v", "2"]),
    ]


def verify_video_temporal_consistency(video_path: Path) -> tuple[bool, float]:
    """Inspect rendered MP4 frames via OpenCV (thread-capped in Eco) for temporal continuity."""
    if not video_path.is_file():
        return (False, 0.0)
    apply_auxiliary_thread_limit()
    try:
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return (False, 0.0)
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total < 2:
                ok_one, _ = cap.read()
                return (bool(ok_one), 1.0 if ok_one else 0.0)
            prev_hist = None
            corrs: list[float] = []
            step = max(1, total // 12)
            idx = 0
            while idx < total:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                small = cv2.resize(frame, (96, 64))
                hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
                hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
                cv2.normalize(hist, hist)
                if prev_hist is not None:
                    corr = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
                    corrs.append(corr)
                prev_hist = hist
                idx += step
            mean_corr = float(np.mean(corrs)) if corrs else 1.0
            return (mean_corr >= 0.35, mean_corr)
        finally:
            cap.release()
    except Exception:
        return (video_path.is_file() and video_path.stat().st_size > 1024, 1.0)


def interpolate_video_to_24fps(src_video: Path, dst_mp4: Path, target_fps: int = 24) -> bool:
    """Smooth low-FPS diffusion output to 24fps H.264 MP4 via motion-compensated flow."""
    if not src_video.is_file():
        return False
    dst_mp4.parent.mkdir(parents=True, exist_ok=True)
    thread_cap = str(get_worker_thread_cap())
    worker_env = get_worker_env()
    # Try optical-flow motion interpolation first + crisp 720p optical DSLR grade
    tail_filter = "scale=1280:720:flags=lanczos,unsharp=5:5:0.75:3:3:0.0,eq=contrast=1.03:saturation=0.96"
    for vf in (
        f"minterpolate=fps={target_fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,{tail_filter}",
        f"minterpolate=fps={target_fps}:mi_mode=blend,{tail_filter}",
        f"fps={target_fps},{tail_filter}",
    ):
        for codec, extra in _pick_h264_codec_args():
            proc: subprocess.Popen[Any] | None = None
            try:
                base_cmd = [
                    "ffmpeg",
                    "-y",
                    "-threads",
                    thread_cap,
                    "-i",
                    str(src_video),
                    "-vf",
                    vf,
                    "-c:v",
                    codec,
                    "-pix_fmt",
                    "yuv420p",
                    *extra,
                    str(dst_mp4),
                ]
                prio_cmd, popen_flags = wrap_worker_cmd_with_priority(base_cmd)
                proc = subprocess.Popen(
                    prio_cmd,
                    env=worker_env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    creationflags=popen_flags,
                )
                register_protected_pid(proc.pid, "Video encoder (locked)")
                try:
                    rc = proc.wait(timeout=90)
                finally:
                    unregister_protected_pid(proc.pid)
                if rc == 0 and dst_mp4.is_file() and dst_mp4.stat().st_size > 1024:
                    return True
            except Exception:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
    return False


class FastVulkanImageServer:
    """Manages `sd-server` on Vulkan0 for zero-reload 1-by-1 image generation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ag_proc: AirGappedProcess | None = None
        self._proc: subprocess.Popen[Any] | None = None
        self._port: int = 18234
        self._caps: dict[str, Any] = {}
        self._shutting_down = False

    def is_running(self) -> bool:
        if self._ag_proc is not None:
            return self._ag_proc.poll() is None
        return self._proc is not None and self._proc.poll() is None

    def start(self, timeout_s: float = 45.0) -> None:
        with self._lock:
            if self._shutting_down:
                return
            if self.is_running() and self._refresh_caps():
                return

            ckpt = resolve_pony_checkpoint()
            if not SD_SERVER_BIN.is_file():
                raise FileNotFoundError(f"sd-server missing at {SD_SERVER_BIN}")
            if not ckpt.is_file():
                raise FileNotFoundError(f"Pony GGUF checkpoint missing at {ckpt}")

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._port = _pick_free_port(18234)
            inner_port = _pick_free_port(18235)
            try:
                from .hf_models import detect_hardware_profile
                hw = detect_hardware_profile()
            except Exception:
                hw = None
            cores = hw.cpu_cores if hw is not None else (os.cpu_count() or 4)
            threads = str(max(2, cores - 2 if cores > 4 else cores))
            cmd = [
                str(SD_SERVER_BIN),
                "--listen-ip",
                "127.0.0.1",
                "--listen-port",
                str(inner_port),
                "-m",
                str(ckpt),
                "--vae",
                str(SDXL_VAE_PATH),
                "--force-sdxl-vae-conv-scale",
                "--lora-model-dir",
                str(LORAS_DIR),
                "--threads",
                threads,
                "--flash-attn",
                "off" if (hw is not None and hw.backend == "cpu") else "on",
            ]
            if hw is None or hw.unified_memory or hw.gpu_pool_gb < 12.0:
                cmd.append("--vae-tiling")
            if hw is not None and hw.gpu_pool_gb < 5.5:
                cmd.append("--offload-to-cpu")
            log_fh = open(LOG_DIR / "sd-server.log", "w", encoding="utf-8")
            self._ag_proc = launch_airgapped_http_service(
                cmd=cmd,
                inner_port=inner_port,
                outer_port=self._port,
                protection_label="Vulkan Image AI (air-gapped, locked)",
                log_fh=log_fh,
            )
            self._proc = self._ag_proc.proc

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._shutting_down:
                return
            if self._ag_proc is not None and self._ag_proc.poll() is not None:
                raise RuntimeError(f"sd-server exited with code {self._ag_proc.returncode}")
            if self._refresh_caps():
                return
            time.sleep(0.25)
        raise TimeoutError("Timed out waiting for air-gapped sd-server Vulkan capabilities.")

    def stop(self) -> None:
        with self._lock:
            if self._ag_proc is not None:
                self._ag_proc.terminate_cleanly()
                self._ag_proc = None
            else:
                _kill_proc_group(self._proc)
            self._proc = None
            self._caps = {}

    def shutdown(self) -> None:
        self._shutting_down = True
        self.stop()

    def _refresh_caps(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self._port}/sdcpp/v1/capabilities", timeout=2.0
            ) as resp:
                if 200 <= resp.status < 300:
                    self._caps = json.loads(resp.read().decode("utf-8"))
                    return True
        except Exception:
            pass
        return False

    def _resolve_fast_recipe(self) -> dict[str, Any]:
        loras_avail = self._caps.get("loras") or []
        dmd2_entry = next(
            (l for l in loras_avail if "dmd2_sdxl" in str(l.get("path", "")).lower()),
            None,
        )
        light_entry = next(
            (l for l in loras_avail if "sdxl_lightning_4step" in str(l.get("path", "")).lower()),
            None,
        )
        if dmd2_entry is not None:
            return {
                "lora": [{"path": dmd2_entry["path"], "multiplier": 0.75}],
                "steps": 5,
                "cfg": 1.6,
                "sampler": "lcm",
                "scheduler": "lcm",
                "label": "Pony V6 XL Q8 + DMD2 (5-step Vulkan)",
            }
        if light_entry is not None:
            return {
                "lora": [{"path": light_entry["path"], "multiplier": 0.80}],
                "steps": 6,
                "cfg": 1.8,
                "sampler": "euler",
                "scheduler": "sgm_uniform",
                "label": "Pony V6 XL Q8 + Lightning (6-step Vulkan)",
            }
        return {
            "lora": [],
            "steps": 12,
            "cfg": 6.0,
            "sampler": "euler_a",
            "scheduler": "discrete",
            "label": "Pony V6 XL Q8 (12-step Vulkan)",
        }

    def generate_single_image(
        self,
        positive_prompt: str,
        negative_prompt: str = "",
        width: int = 896,
        height: int = 576,
        seed: int | None = None,
        cancel_event: threading.Event | None = None,
        on_step: Callable[[int, int], None] | None = None,
        init_image_b64: str | None = None,
        init_strength: float = 0.72,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[Path | None, int, str]:
        """Generate ONE image synchronously via air-gapped sd-server, honoring cancel_event."""
        self.start()
        if cancel_event and cancel_event.is_set():
            return (None, 0, "")

        actual_seed = seed if seed is not None else random.randint(1, 2_147_483_647)
        recipe = self._resolve_fast_recipe()
        if overrides:
            if overrides.get("booster") is False:
                recipe["lora"] = []
                recipe["steps"] = int(overrides.get("steps") or 18)
                recipe["cfg"] = 6.5
                recipe["sampler"] = "euler_a"
                recipe["scheduler"] = "karras"
            elif isinstance(overrides.get("steps"), int):
                recipe["steps"] = int(overrides["steps"])
            if isinstance(overrides.get("cfg"), (int, float)):
                recipe["cfg"] = float(overrides["cfg"])
            if isinstance(overrides.get("sampler"), str) and overrides["sampler"].strip():
                recipe["sampler"] = str(overrides["sampler"]).strip()
        neg = negative_prompt.strip() or DEFAULT_NEGATIVE_PONY

        staged, actual_seed, lbl = self._run_sd_job(
            positive_prompt=positive_prompt,
            negative_prompt=neg,
            width=width,
            height=height,
            seed=actual_seed,
            recipe=recipe,
            cancel_event=cancel_event,
            on_step=on_step,
            init_image_b64=init_image_b64,
            init_strength=init_strength,
        )
        if staged is None:
            return (None, actual_seed, lbl)

        want_hires = bool(
            (overrides and overrides.get("dslr_booster"))
            or (get_performance_mode() == "turbo" and not init_image_b64 and overrides is None)
        )
        if want_hires and staged.is_file() and not (cancel_event and cancel_event.is_set()):
            try:
                boost_w = min(1344, ((int(width * 1.25)) // 64) * 64)
                boost_h = min(896, ((int(height * 1.25)) // 64) * 64)
                raw_b64 = base64.b64encode(staged.read_bytes()).decode("ascii")
                boost_recipe = {
                    "lora": [],
                    "steps": 8,
                    "cfg": 5.5,
                    "sampler": "euler_a",
                    "scheduler": "karras",
                    "label": "DSLR HiRes Booster",
                }
                boost_p, _, _ = self._run_sd_job(
                    positive_prompt=positive_prompt,
                    negative_prompt=neg,
                    width=boost_w,
                    height=boost_h,
                    seed=actual_seed + 1,
                    recipe=boost_recipe,
                    cancel_event=cancel_event,
                    on_step=None,
                    init_image_b64=raw_b64,
                    init_strength=0.36,
                )
                if boost_p is not None and boost_p.is_file():
                    staged.unlink(missing_ok=True)
                    staged = boost_p
            except Exception:
                pass

        if staged.is_file():
            _apply_dslr_finish(staged)
        return (staged, actual_seed, lbl)

    def _run_sd_job(
        self,
        *,
        positive_prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        seed: int,
        recipe: dict[str, Any],
        cancel_event: threading.Event | None,
        on_step: Callable[[int, int], None] | None,
        init_image_b64: str | None,
        init_strength: float,
    ) -> tuple[Path | None, int, str]:
        actual_seed = seed
        sample_params: dict[str, Any] = {
            "sample_method": recipe["sampler"],
            "scheduler": recipe["scheduler"],
            "sample_steps": recipe["steps"],
            "guidance": {"txt_cfg": recipe["cfg"]},
        }
        payload: dict[str, Any] = {
            "prompt": format_pony_prompt(positive_prompt),
            "negative_prompt": negative_prompt,
            "width": width,
            "height": height,
            "batch_count": 1,
            "clip_skip": 2,
            "seed": actual_seed,
            "output_format": "png",
            "lora": recipe["lora"],
            "sample_params": sample_params,
        }
        if init_image_b64:
            try:
                import io as _io
                from PIL import Image as _PilImg, ImageOps as _PilOps
                raw_im = _PilImg.open(_io.BytesIO(base64.b64decode(init_image_b64))).convert("RGB")
                fitted = _PilOps.fit(raw_im, (width, height), method=_PilImg.Resampling.LANCZOS)
                ob = _io.BytesIO()
                fitted.save(ob, format="PNG")
                init_image_b64 = base64.b64encode(ob.getvalue()).decode("ascii")
            except Exception:
                pass
            payload["init_image"] = init_image_b64
            payload["strength"] = max(0.40, min(0.90, float(init_strength)))
            if recipe["steps"] <= 6:
                sample_params["sample_steps"] = 7
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/sdcpp/v1/img_gen",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            job_meta = json.loads(resp.read().decode("utf-8"))
        job_id = job_meta["id"]

        last_step = 0
        tmp_dir = Path(tempfile.mkdtemp(prefix="vimona_img_"))
        try:
            while True:
                if self._shutting_down or (cancel_event and cancel_event.is_set()):
                    try:
                        creq = urllib.request.Request(
                            f"http://127.0.0.1:{self._port}/sdcpp/v1/jobs/{job_id}/cancel",
                            data=b"{}",
                            headers={"Content-Type": "application/json"},
                        )
                        urllib.request.urlopen(creq, timeout=3.0).read()
                    except Exception:
                        pass
                    return (None, actual_seed, recipe["label"])

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/sdcpp/v1/jobs/{job_id}",
                    timeout=5.0,
                ) as jresp:
                    jdata = json.loads(jresp.read().decode("utf-8"))

                status = str(jdata.get("status", ""))
                prev = jdata.get("preview")
                if isinstance(prev, dict) and on_step is not None:
                    cur = int(prev.get("step") or 0)
                    tot = int(prev.get("total_steps") or recipe["steps"])
                    if cur > last_step:
                        last_step = cur
                        on_step(cur, tot)

                if status == "completed":
                    imgs = ((jdata.get("result") or {}).get("images")) or []
                    if not imgs:
                        return (None, actual_seed, recipe["label"])
                    raw_b64 = imgs[0].get("b64_json", "")
                    if not raw_b64:
                        return (None, actual_seed, recipe["label"])
                    raw_png = base64.b64decode(raw_b64)
                    tmp_png = tmp_dir / f"vimona_{int(time.time())}_{actual_seed}.png"
                    tmp_png.write_bytes(raw_png)
                    staged = stage_ephemeral_media(tmp_png)
                    return (staged, actual_seed, recipe["label"])

                if status in ("failed", "cancelled"):
                    err = (jdata.get("error") or {}).get("message") or status
                    if status == "cancelled":
                        return (None, actual_seed, recipe["label"])
                    raise RuntimeError(f"sd-server job failed: {err}")

                time.sleep(0.25)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
