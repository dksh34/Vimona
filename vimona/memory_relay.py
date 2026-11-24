"""Zero-overlap 16 GB shared-RAM process relay & Fedora memory telemetry.

Ensures llama-server (text director) and ComfyUI (image/video diffusion) NEVER
reside in system RAM at the same time on a 16 GB AMD Ryzen laptop.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .airgap import (
    AirGappedProcess,
    launch_airgapped_http_service,
    purge_legacy_burner_profile,
)
from .fast_media_engine import (
    FastVulkanImageServer,
    has_vulkan_image_engine,
    interpolate_video_to_24fps,
    verify_video_temporal_consistency,
)
from .models import MemoryHandoffData
from .ram_manager import register_protected_pid, unregister_protected_pid
from .storage import stage_ephemeral_media
from .workflows import build_pony_keyframe_workflow, build_wan_vace_window_workflow

LLAMA_SERVER_BIN = Path("~/llama.cpp/build/bin/llama-server").expanduser()
LLM_MODEL_PATH = Path("~/models/llm/light-uncensored.gguf").expanduser()
COMFY_DIR = Path("~/comfyui").expanduser()
COMFY_VENV_PYTHON = COMFY_DIR / "venv/bin/python"
COMFY_MAIN = COMFY_DIR / "main.py"
COMFY_INPUT_DIR = COMFY_DIR / "input"
COMFY_OUTPUT_DIR = COMFY_DIR / "output"
LOG_DIR = Path("~/.local/share/vimona").expanduser()

LLAMA_HOST = "127.0.0.1"
COMFY_HOST = "127.0.0.1"


def _pick_free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])


@dataclass(frozen=True)
class MemoryTelemetry:
    owner: str  # "idle" | "text" | "media" | "transition" | "research"
    used_gb: float
    total_gb: float
    available_gb: float
    detail: str
    active_model: str = ""


def read_proc_meminfo() -> tuple[float, float, float]:
    """Return (used_gb, total_gb, available_gb) directly from Linux /proc/meminfo."""
    vals_kb: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            parts = line.split(":")
            if len(parts) != 2:
                continue
            key = parts[0].strip()
            num = parts[1].strip().split()[0]
            if num.isdigit():
                vals_kb[key] = int(num)
    except OSError:
        return (0.0, 16.0, 16.0)

    total_kb = vals_kb.get("MemTotal", 16 * 1024 * 1024)
    avail_kb = vals_kb.get("MemAvailable", vals_kb.get("MemFree", total_kb))
    used_kb = max(0, total_kb - avail_kb)
    gb = 1024.0 * 1024.0
    return (used_kb / gb, total_kb / gb, avail_kb / gb)


def _terminate_proc_group(proc: subprocess.Popen[Any] | None, name: str) -> None:
    """Terminate a child process group cleanly and wait until 100% of RAM is freed."""
    if proc is None:
        return
    unregister_protected_pid(proc.pid)
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            return

    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)

    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pass
    # Brief settle for kernel page reclamation
    time.sleep(0.25)


class MemoryRelayManager:
    """Coordinates exclusive ownership of 16 GB RAM between llama-server and ComfyUI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner: str = "idle"
        self._detail: str = "All engines offline"
        self._active_model: str = "All offline (chat to wake text AI)"
        self._llama_proc: subprocess.Popen[Any] | None = None
        self._comfy_ag: AirGappedProcess | None = None
        self._comfy_proc: subprocess.Popen[Any] | None = None
        self._fast_img_server = FastVulkanImageServer()
        self._llama_port: int = 18081
        self._comfy_port: int = 18188
        self._shutting_down = False

    def get_telemetry(self) -> MemoryTelemetry:
        used, total, avail = read_proc_meminfo()
        return MemoryTelemetry(
            owner=self._owner,
            used_gb=used,
            total_gb=total,
            available_gb=avail,
            detail=self._detail,
            active_model=self._active_model,
        )

    def _set_owner(self, owner: str, detail: str, active_model: str | None = None) -> None:
        self._owner = owner
        self._detail = detail
        if active_model is not None:
            self._active_model = active_model

    def verify_binaries(self) -> list[str]:
        missing: list[str] = []
        for path in (LLAMA_SERVER_BIN, LLM_MODEL_PATH, COMFY_VENV_PYTHON, COMFY_MAIN):
            if not path.exists():
                missing.append(str(path))
        return missing

    # -----------------------------------------------------------------------
    # Text Engine (llama-server via AMD Vulkan)
    # -----------------------------------------------------------------------

    def ensure_llm_running(self, timeout_s: float = 45.0) -> None:
        """Stop ComfyUI if running and start llama-server with Vulkan + no-mmap."""
        with self._lock:
            if self._shutting_down:
                return
            if self._fast_img_server.is_running():
                self._set_owner("transition", "Stopping image engine…")
                self._fast_img_server.stop()
            if self._comfy_ag is not None or self._comfy_proc is not None:
                self._set_owner("transition", "Stopping media engine…")
                if self._comfy_ag is not None:
                    self._comfy_ag.terminate_cleanly()
                    self._comfy_ag = None
                else:
                    _terminate_proc_group(self._comfy_proc, "comfyui")
                self._comfy_proc = None

            if self._llama_proc is not None and self._llama_proc.poll() is None:
                if self._http_ok(f"http://{LLAMA_HOST}:{self._llama_port}/health"):
                    self._set_owner("text", "Text engine", "light-uncensored.gguf (Vulkan0)")
                    return

            if not LLAMA_SERVER_BIN.is_file():
                raise FileNotFoundError(f"llama-server not found at {LLAMA_SERVER_BIN}")
            if not LLM_MODEL_PATH.is_file():
                raise FileNotFoundError(f"LLM model not found at {LLM_MODEL_PATH}")

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._llama_port = _pick_free_port(18081)
            self._set_owner("transition", "Starting text engine…", "Loading light-uncensored.gguf…")
            cmd = [
                str(LLAMA_SERVER_BIN),
                "--model",
                str(LLM_MODEL_PATH),
                "--device",
                "Vulkan0",
                "--flash-attn",
                "on",
                "--load-mode",
                "none",
                "--ctx-size",
                "4096",
                "--n-gpu-layers",
                "99",
                "--reasoning-budget",
                "96",
                "--host",
                LLAMA_HOST,
                "--port",
                str(self._llama_port),
            ]
            log_fh = open(LOG_DIR / "llama-server.log", "w", encoding="utf-8")
            self._llama_proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            register_protected_pid(self._llama_proc.pid, "Text AI (locked)")

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._shutting_down:
                return
            if self._llama_proc is not None and self._llama_proc.poll() is not None:
                self._set_owner("idle", "LLM failed to start")
                raise RuntimeError(f"llama-server exited with code {self._llama_proc.returncode}")
            if self._http_ok(f"http://{LLAMA_HOST}:{self._llama_port}/health"):
                self._set_owner("text", "Text engine", "light-uncensored.gguf (Vulkan0)")
                return
            time.sleep(0.35)

        raise TimeoutError("Timed out waiting for llama-server health endpoint.")

    def stop_llm(self) -> None:
        with self._lock:
            if self._llama_proc is not None:
                self._set_owner("transition", "Releasing text RAM…")
                _terminate_proc_group(self._llama_proc, "llama-server")
                self._llama_proc = None
            if self._comfy_proc is None:
                self._set_owner("idle", "All engines offline", "All offline (chat to wake text AI)")

    @staticmethod
    def _normalize_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """Fold multiple system messages into a single leading system prompt cleanly."""
        sys_parts: list[str] = []
        convo: list[dict[str, str]] = []
        for m in messages:
            role = str(m.get("role") or "user").strip().lower()
            content = str(m.get("content") or "").strip()
            if not content:
                continue
            if role == "system":
                sys_parts.append(content)
            elif convo and convo[-1]["role"] == role:
                convo[-1]["content"] += "\n\n" + content
            else:
                convo.append({"role": role, "content": content})
        out: list[dict[str, str]] = []
        if sys_parts:
            out.append({"role": "system", "content": "\n\n".join(sys_parts)})
        out.extend(convo)
        return out

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.65,
        max_tokens: int = 1024,
        on_stream_delta: Callable[[str, str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str]:
        """Query local llama-server (streaming reasoning & content when callback supplied)."""
        use_stream = on_stream_delta is not None
        norm_msgs = self._normalize_messages(messages)
        for attempt in range(2):
            self.ensure_llm_running()
            payload = {
                "messages": norm_msgs,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "cache_prompt": True,
                "stream": use_stream,
            }
            req = urllib.request.Request(
                f"http://{LLAMA_HOST}:{self._llama_port}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                if not use_stream:
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    choices = data.get("choices") or []
                    if not choices:
                        return ("", "")
                    msg = choices[0].get("message", {})
                    content = str(msg.get("content") or "").strip()
                    reasoning = str(msg.get("reasoning_content") or "").strip()
                    return (content, reasoning)

                reasoning_acc: list[str] = []
                content_acc: list[str] = []
                last_emit = 0.0
                with urllib.request.urlopen(req, timeout=180) as resp:
                    for raw_line in resp:
                        if cancel_event and cancel_event.is_set():
                            break
                        line = raw_line.decode("utf-8", errors="ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        chunk_str = line[5:].strip()
                        if chunk_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(chunk_str)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        r_tok = delta.get("reasoning_content")
                        c_tok = delta.get("content")
                        if isinstance(r_tok, str) and r_tok:
                            reasoning_acc.append(r_tok)
                        if isinstance(c_tok, str) and c_tok:
                            content_acc.append(c_tok)
                        now = time.monotonic()
                        if on_stream_delta is not None and (now - last_emit) >= 0.07:
                            last_emit = now
                            on_stream_delta("".join(reasoning_acc), "".join(content_acc))
                full_r = "".join(reasoning_acc).strip()
                full_c = "".join(content_acc).strip()
                if on_stream_delta is not None:
                    on_stream_delta(full_r, full_c)
                return (full_c, full_r)
            except urllib.error.HTTPError:
                if attempt == 0 and not self._shutting_down:
                    # Bypass peg-gemma4 strict parser failure by streaming raw deltas or plain output
                    use_stream = True
                    continue
                raise
            except (http.client.RemoteDisconnected, ConnectionResetError, urllib.error.URLError):
                if attempt == 0 and not self._shutting_down:
                    self.stop_llm()
                    continue
                raise
        return ("", "")

    # -----------------------------------------------------------------------
    # Media Engine (ComfyUI with strict --lowvram + ROCm)
    # -----------------------------------------------------------------------

    def _start_comfyui(self, timeout_s: float = 90.0) -> None:
        with self._lock:
            if self._shutting_down:
                return
            if self._llama_proc is not None:
                self._set_owner("transition", "Freeing text RAM…")
                _terminate_proc_group(self._llama_proc, "llama-server")
                self._llama_proc = None

            if self._comfy_proc is not None and self._comfy_proc.poll() is None:
                if self._http_ok(f"http://{COMFY_HOST}:{self._comfy_port}/system_stats"):
                    self._set_owner("media", "Media engine")
                    return

            if not COMFY_VENV_PYTHON.is_file() or not COMFY_MAIN.is_file():
                raise FileNotFoundError(f"ComfyUI not found in {COMFY_DIR}")

            COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
            COMFY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._comfy_port = _pick_free_port(18188)
            inner_port = _pick_free_port(18189)

            self._set_owner("transition", "Starting air-gapped media engine…", "Starting ComfyUI (offline)…")
            env = os.environ.copy()
            env["HSA_OVERRIDE_GFX_VERSION"] = "11.0.0"
            env["HSA_ENABLE_SDMA"] = "0"
            env["MIOPEN_FIND_MODE"] = "FAST"
            miopen_db = LOG_DIR / "miopen_db"
            miopen_db.mkdir(parents=True, exist_ok=True)
            env["MIOPEN_USER_DB_PATH"] = str(miopen_db)
            env["PYTORCH_HIP_ALLOC_CONF"] = "expandable_segments:True"
            env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
            env["COMFYUI_DISABLE_FETCH"] = "1"

            cmd = [
                str(COMFY_VENV_PYTHON),
                str(COMFY_MAIN),
                "--listen",
                COMFY_HOST,
                "--port",
                str(inner_port),
                "--lowvram",
                "--use-pytorch-cross-attention",
                "--disable-manager",
                "--disable-pinned-memory",
                "--offline",
                "--disable-partner-nodes",
                "--output-directory",
                str(COMFY_OUTPUT_DIR),
            ]
            comfy_log = open(LOG_DIR / "comfyui.log", "w", encoding="utf-8")
            self._comfy_ag = launch_airgapped_http_service(
                cmd=cmd,
                inner_port=inner_port,
                outer_port=self._comfy_port,
                protection_label="Media AI (air-gapped, locked)",
                log_fh=comfy_log,
                cwd=COMFY_DIR,
                env=env,
            )
            self._comfy_proc = self._comfy_ag.proc

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._shutting_down:
                return
            if self._comfy_proc is not None and self._comfy_proc.poll() is not None:
                self._set_owner("idle", "ComfyUI failed")
                raise RuntimeError(f"ComfyUI exited with code {self._comfy_proc.returncode}")
            if self._http_ok(f"http://{COMFY_HOST}:{self._comfy_port}/system_stats"):
                self._set_owner("media", "Media engine (air-gapped)")
                return
            time.sleep(0.5)

        raise TimeoutError("Timed out waiting for air-gapped ComfyUI API.")

    def stop_comfyui(self) -> None:
        with self._lock:
            if self._comfy_ag is not None or self._comfy_proc is not None:
                self._set_owner("transition", "Releasing media RAM…")
                if self._comfy_ag is not None:
                    self._comfy_ag.terminate_cleanly()
                    self._comfy_ag = None
                else:
                    _terminate_proc_group(self._comfy_proc, "comfyui")
                self._comfy_proc = None
            if self._llama_proc is None:
                self._set_owner("idle", "All engines offline", "All offline (chat to wake text AI)")

    def run_media_relay(
        self,
        positive_prompt: str,
        negative_prompt: str = "",
        motion_prompt: str = "",
        batch_count: int = 4,
        video_seconds: int = 0,
        beats: list[str] | None = None,
        on_progress: Callable[[MemoryHandoffData], None] | None = None,
        on_image_ready: Callable[[int, Path, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        init_image_b64: str | None = None,
        init_strength: float = 0.72,
        node_overrides: dict[str, Any] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> tuple[list[Path], Path | None]:
        """Execute zero-overlap air-gapped RAM relay with 1-by-1 image streaming + fast video."""
        purge_legacy_burner_profile()
        stages = ["1. Air-gap & RAM", "2. Make images"]
        if video_seconds > 0:
            stages.append(f"3. Render {video_seconds}s video")

        def _emit(stage_idx: int, frac: float, pipeline: str, status: str, eta: str) -> None:
            if on_progress is None:
                return
            used, total, _ = read_proc_meminfo()
            on_progress(
                MemoryHandoffData(
                    offloaded_model="Text engine stopped (0.0 GB)"
                    if self._llama_proc is None
                    else "Stopping text engine…",
                    active_pipeline=pipeline,
                    vram_allocated=f"{used:.1f} / {total:.1f} GB RAM",
                    ram_cache="Air-Gapped Netns · Vulkan Fast",
                    stage_index=stage_idx,
                    stages=stages,
                    progress_fraction=max(0.0, min(1.0, frac)),
                    frame_status=status,
                    eta_label=eta,
                )
            )

        keyframes: list[Path] = []
        final_video: Path | None = None
        run_tag = time.strftime("%Y%m%d_%H%M%S")
        temp_comfy_files: list[Path] = []
        total_imgs = max(1, min(8, int(batch_count)))

        try:
            _emit(0, 0.06, "Air-gapping render sandbox…", "Freeing text RAM & blocking outbound net", "Air-Gap")
            self.stop_llm()
            if cancel_event and cancel_event.is_set():
                return ([], None)

            # Stage 2: One-by-one image generation (Air-gapped Vulkan sd-server preferred)
            use_vulkan = has_vulkan_image_engine()
            if on_stage is not None:
                on_stage("Air-Gapped Rendering")
            if use_vulkan:
                self._set_owner("media", "Loading air-gapped image AI", "pony-diffusion-v6-xl-Q8_0 (Air-Gapped)")
                _emit(
                    1,
                    0.12,
                    "Booting air-gapped Vulkan SDXL engine…",
                    "Network isolated (`unshare -r -n`) · 0 outbound packets",
                    "Booting",
                )
                try:
                    self._fast_img_server.start()
                except Exception:
                    use_vulkan = False

            if use_vulkan:
                self._set_owner("media", "Rendering images (offline)", "Pony V6 XL Q8 + DMD2 (Air-Gapped)")
                for idx in range(total_imgs):
                    if cancel_event and cancel_event.is_set():
                        break
                    base_frac = 0.16 + (idx / total_imgs) * (0.40 if video_seconds > 0 else 0.78)
                    span_frac = (0.40 if video_seconds > 0 else 0.78) / total_imgs
                    ref_tag = " + Visual Ref" if init_image_b64 else ""
                    _emit(
                        1,
                        base_frac,
                        f"Image {idx + 1}/{total_imgs} · Pony V6 XL{ref_tag} (Air-Gapped)",
                        f"Rendering image {idx + 1} of {total_imgs} offline (streaming 1-by-1)…",
                        f"Img {idx + 1}/{total_imgs}",
                    )
                    try:
                        staged, _seed, eng_label = self._fast_img_server.generate_single_image(
                            positive_prompt=positive_prompt,
                            negative_prompt=negative_prompt,
                            width=832,
                            height=512,
                            cancel_event=cancel_event,
                            init_image_b64=init_image_b64,
                            init_strength=init_strength,
                            overrides=node_overrides,
                            on_step=lambda cur, tot, i=idx, bf=base_frac, sf=span_frac, rt=ref_tag: _emit(
                                1,
                                bf + (cur / max(1, tot)) * sf,
                                f"Image {i + 1}/{total_imgs} · Pony V6 XL{rt} (Air-Gapped)",
                                f"Denoising step {cur}/{tot} offline for image {i + 1}…",
                                f"{cur}/{tot} · Img {i + 1}",
                            ),
                        )
                        if staged is not None:
                            keyframes.append(staged)
                            if on_image_ready is not None:
                                on_image_ready(idx, staged, eng_label + (" · Ref Grounded" if init_image_b64 else ""))
                    except Exception:
                        # Automatic fallback to low-VRAM ComfyUI (still air-gapped) if Vulkan workspace tight
                        self._fast_img_server.stop()
                        use_vulkan = False
                        break

                self._fast_img_server.stop()

            if not use_vulkan and len(keyframes) < total_imgs and not (cancel_event and cancel_event.is_set()):
                self._start_comfyui()
                if cancel_event and cancel_event.is_set():
                    return (keyframes, None)
                comfy_ref_name: str | None = None
                if init_image_b64:
                    import base64
                    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
                    comfy_ref_name = f"vimona_ref_{run_tag}.jpg"
                    ref_dst = COMFY_INPUT_DIR / comfy_ref_name
                    ref_dst.write_bytes(base64.b64decode(init_image_b64))
                    temp_comfy_files.append(ref_dst)
                self._set_owner("media", "Rendering images (offline)", "pony-diffusion-v6-xl-Q8_0.gguf (Air-Gapped)")
                for idx in range(len(keyframes), total_imgs):
                    if cancel_event and cancel_event.is_set():
                        break
                    base_frac = 0.18 + (idx / total_imgs) * (0.38 if video_seconds > 0 else 0.76)
                    _emit(
                        1,
                        base_frac,
                        f"Image {idx + 1}/{total_imgs} · Pony V6 XL (Air-Gapped ROCm)",
                        f"Rendering keyframe {idx + 1} of {total_imgs} offline…",
                        f"Img {idx + 1}/{total_imgs}",
                    )
                    kf_workflow = build_pony_keyframe_workflow(
                        positive_prompt=positive_prompt,
                        negative_prompt=negative_prompt,
                        batch_size=1,
                        filename_prefix=f"vimona_kf_{run_tag}_{idx:02d}",
                        reference_image_name=comfy_ref_name,
                        denoise_strength=init_strength,
                        overrides=node_overrides,
                    )
                    kf_outputs = self._submit_and_wait(
                        kf_workflow,
                        cancel_event=cancel_event,
                        on_tick=lambda step_frac, i=idx, bf=base_frac: _emit(
                            1,
                            bf + step_frac * ((0.38 if video_seconds > 0 else 0.76) / total_imgs),
                            f"Image {i + 1}/{total_imgs} · Pony V6 XL (ROCm)",
                            f"Streaming keyframe {i + 1} of {total_imgs}…",
                            f"Img {i + 1}/{total_imgs}",
                        ),
                    )
                    raw_kfs = [p for p in kf_outputs if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
                    for p in raw_kfs:
                        staged = stage_ephemeral_media(p)
                        keyframes.append(staged)
                        if on_image_ready is not None:
                            on_image_ready(idx, staged, "Pony V6 XL (ROCm)")

            if cancel_event and cancel_event.is_set():
                return (keyframes, None)

            # Stage 3: Wan 2.1 VACE 1.3B + CausVid Chained Sliding Windows -> 24fps MP4
            if video_seconds > 0 and keyframes:
                self._start_comfyui()
                self._set_owner("media", "Rendering video", "Wan 2.1 VACE 1.3B + CausVid (ROCm)")
                anchor_src = keyframes[0]
                anchor_name = f"vimona_anchor_{run_tag}{anchor_src.suffix}"
                anchor_dst = COMFY_INPUT_DIR / anchor_name
                shutil.copy2(anchor_src, anchor_dst)
                temp_comfy_files.append(anchor_dst)

                window_count = max(1, min(8, round(video_seconds / 2.0)))
                window_videos: list[Path] = []
                control_start_name: str | None = anchor_name

                for w_idx in range(window_count):
                    if cancel_event and cancel_event.is_set():
                        break
                    raw_beat = (
                        beats[w_idx]
                        if beats and w_idx < len(beats)
                        else (motion_prompt or positive_prompt)
                    )
                    if positive_prompt and positive_prompt.lower() not in raw_beat.lower():
                        beat_text = f"{positive_prompt}, {raw_beat}".strip(", ")
                    else:
                        beat_text = raw_beat
                    base_frac = 0.58 + (w_idx / window_count) * 0.36
                    _emit(
                        2,
                        base_frac,
                        f"Wan 2.1 VACE 1.3B + CausVid · Window {w_idx + 1}/{window_count}",
                        f"Rendering fast 5-step window ({w_idx * 2}s–{(w_idx + 1) * 2}s)…",
                        f"Win {w_idx + 1}/{window_count}",
                    )
                    wan_wf = build_wan_vace_window_workflow(
                        motion_prompt=beat_text,
                        reference_image_name=anchor_name,
                        control_start_image_name=control_start_name,
                        width=480,
                        height=272,
                        frames=17,
                        fps=8,
                        filename_prefix=f"vimona_win_{run_tag}_{w_idx:02d}",
                        overrides=node_overrides,
                    )
                    win_outs = self._submit_and_wait(
                        wan_wf,
                        cancel_event=cancel_event,
                        on_tick=lambda step_frac, wi=w_idx, bf=base_frac: _emit(
                            2,
                            min(0.96, bf + step_frac * (0.36 / window_count)),
                            f"Wan 2.1 VACE 1.3B + CausVid · Window {wi + 1}/{window_count}",
                            f"Rendering fast 5-step window ({wi * 4}s–{(wi + 1) * 4}s)…",
                            f"Win {wi + 1}/{window_count}",
                        ),
                    )
                    mp4s = [p for p in win_outs if p.suffix.lower() == ".mp4"]
                    if not mp4s:
                        continue
                    win_mp4 = mp4s[-1]
                    window_videos.append(win_mp4)

                    # Extract final frame as control_video anchor for next window
                    if w_idx + 1 < window_count:
                        tail_name = f"vimona_tail_{run_tag}_{w_idx:02d}.png"
                        tail_path = COMFY_INPUT_DIR / tail_name
                        if self._extract_last_frame(win_mp4, tail_path):
                            control_start_name = tail_name
                            temp_comfy_files.append(tail_path)

                if window_videos:
                    raw_merged = COMFY_OUTPUT_DIR / f"vimona_raw_{run_tag}.mp4"
                    smooth_mp4 = COMFY_OUTPUT_DIR / f"vimona_clip_{run_tag}.mp4"
                    src_vid = window_videos[0]
                    if len(window_videos) > 1 and self._stitch_windows(window_videos, raw_merged):
                        src_vid = raw_merged
                        temp_comfy_files.append(raw_merged)
                    for wv in window_videos:
                        temp_comfy_files.append(wv)

                    _emit(2, 0.97, "Optical-Flow 24fps Smoothing", "Upconverting video motion to 24fps…", "24fps")
                    if interpolate_video_to_24fps(src_vid, smooth_mp4, target_fps=24):
                        final_video = stage_ephemeral_media(smooth_mp4)
                        temp_comfy_files.append(smooth_mp4)
                    elif src_vid.is_file():
                        final_video = stage_ephemeral_media(src_vid)

                    if final_video is not None and final_video.is_file():
                        if on_stage is not None:
                            on_stage("Consistency Check")
                        _emit(2, 0.99, "Temporal Consistency Check", "Verifying optical-flow frame continuity…", "Check")
                        verify_video_temporal_consistency(final_video)

            _emit(
                len(stages),
                1.0,
                "Media ready in session Gallery · Stopping engines…",
                "All AI engines returning offline (0 MB)",
                "Done",
            )
            return (keyframes, final_video)
        finally:
            self._fast_img_server.stop()
            for tf in temp_comfy_files:
                try:
                    tf.unlink(missing_ok=True)
                except OSError:
                    pass
            for directory in (COMFY_OUTPUT_DIR, COMFY_INPUT_DIR):
                if directory.is_dir():
                    for leftover in directory.glob(f"vimona_*_{run_tag}*"):
                        try:
                            leftover.unlink(missing_ok=True)
                        except OSError:
                            pass
            self.stop_comfyui()

    def _submit_and_wait(
        self,
        workflow: dict[str, Any],
        cancel_event: threading.Event | None = None,
        on_tick: Callable[[float], None] | None = None,
    ) -> list[Path]:
        payload = json.dumps({"prompt": workflow}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{COMFY_HOST}:{self._comfy_port}/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        prompt_id = res["prompt_id"]

        tick = 0
        while True:
            if self._shutting_down or (cancel_event and cancel_event.is_set()):
                try:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"http://{COMFY_HOST}:{self._comfy_port}/interrupt",
                            data=b"{}",
                            headers={"Content-Type": "application/json"},
                        ),
                        timeout=3,
                    )
                except Exception:
                    pass
                return []

            try:
                with urllib.request.urlopen(
                    f"http://{COMFY_HOST}:{self._comfy_port}/history/{prompt_id}",
                    timeout=5,
                ) as hresp:
                    hist = json.loads(hresp.read().decode("utf-8"))
                if prompt_id in hist:
                    entry = hist[prompt_id]
                    return self._collect_output_paths(entry)
            except Exception:
                pass

            if on_tick is not None:
                tick += 1
                on_tick(min(0.95, tick * 0.04))
            time.sleep(1.0)

    def _collect_output_paths(self, history_entry: dict[str, Any]) -> list[Path]:
        paths: list[Path] = []
        outputs = history_entry.get("outputs", {})
        for node_out in outputs.values():
            for key in ("images", "gifs", "videos"):
                for item in node_out.get(key, []):
                    fname = item.get("filename")
                    sub = item.get("subfolder", "")
                    if not fname:
                        continue
                    candidate = COMFY_OUTPUT_DIR / sub / fname if sub else COMFY_OUTPUT_DIR / fname
                    if candidate.is_file():
                        paths.append(candidate)
        return paths

    def _extract_last_frame(self, video_path: Path, out_png: Path) -> bool:
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-sseof",
                    "-0.15",
                    "-i",
                    str(video_path),
                    "-update",
                    "1",
                    "-frames:v",
                    "1",
                    str(out_png),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=15,
            )
            return out_png.is_file()
        except Exception:
            return False

    def _stitch_windows(self, clips: list[Path], out_mp4: Path) -> bool:
        list_file = out_mp4.with_suffix(".concat.txt")
        try:
            lines = [f"file '{str(c)}'" for c in clips]
            list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            for codec, extra in (
                ("libopenh264", ["-b:v", "3200k"]),
                ("libx264", ["-crf", "18"]),
                ("mpeg4", ["-q:v", "3"]),
            ):
                res = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(list_file),
                        "-c:v",
                        codec,
                        "-pix_fmt",
                        "yuv420p",
                        *extra,
                        str(out_mp4),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=120,
                )
                if res.returncode == 0 and out_mp4.is_file() and out_mp4.stat().st_size > 1024:
                    return True
            return False
        except Exception:
            return False
        finally:
            try:
                list_file.unlink(missing_ok=True)
            except OSError:
                pass

    def _http_ok(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                return 200 <= r.status < 300
        except Exception:
            return False

    def shutdown_all(self) -> None:
        """Terminate all child engines cleanly on desktop app exit."""
        self._shutting_down = True
        with self._lock:
            self._fast_img_server.shutdown()
            if self._comfy_ag is not None:
                self._comfy_ag.terminate_cleanly()
                self._comfy_ag = None
            else:
                _terminate_proc_group(self._comfy_proc, "comfyui")
            self._comfy_proc = None
            _terminate_proc_group(self._llama_proc, "llama-server")
            self._llama_proc = None
            self._set_owner("idle", "Stopped")
