"""Zero-overlap shared-memory / VRAM process relay & Linux memory telemetry.

Coordinates mutual exclusion so text inference (`llama-server`) and media
diffusion (`sd-server` / `ComfyUI`) never contend for unified/system memory
simultaneously, verifying process exit and memory reclamation across handoffs.
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
    AirGapIsolationError,
    AirGappedProcess,
    kill_process_tree_verified,
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


class HandoffError(RuntimeError):
    """Raised when an engine handoff fails, times out, or leaves live descendants (`E5`, `E6`)."""


class InsufficientMemoryError(HandoffError):
    """Raised when available host RAM cannot safely host the target engine (`E12`)."""


@dataclass(frozen=True)
class MemoryHandoffMetrics:
    before_avail_gb: float
    after_avail_gb: float
    reclaimed_gb: float
    duration_s: float
    engine_exited: bool
    descendants_gone: bool


@dataclass(frozen=True)
class MemoryTelemetry:
    owner: str  # "idle" | "text" | "media" | "transition" | "research"
    used_gb: float
    total_gb: float
    available_gb: float
    detail: str
    active_model: str = ""
    last_handoff: MemoryHandoffMetrics | None = None


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
        return (0.0, 8.0, 4.0)

    total_kb = vals_kb.get("MemTotal") or 0
    if total_kb <= 0:
        return (0.0, 8.0, 4.0)
    avail_kb = vals_kb.get("MemAvailable", vals_kb.get("MemFree", total_kb))
    used_kb = max(0, total_kb - avail_kb)
    gb = 1024.0 * 1024.0
    return (used_kb / gb, total_kb / gb, avail_kb / gb)


def _terminate_proc_group(
    proc: subprocess.Popen[Any] | None,
    name: str,
    *,
    max_handoff_timeout_s: float = 12.0,
) -> MemoryHandoffMetrics:
    """Terminate an engine and ALL descendants, verify exit, measure RAM reclamation (`E1`–`E10`)."""
    t0 = time.monotonic()
    _u, _t, before_avail = read_proc_meminfo()
    if proc is None:
        return MemoryHandoffMetrics(before_avail, before_avail, 0.0, 0.0, True, True)

    descendants_gone = True
    try:
        if proc.poll() is None:
            descendants_gone = kill_process_tree_verified(
                proc.pid,
                term_timeout_s=min(6.0, max_handoff_timeout_s * 0.6),
                kill_timeout_s=min(4.0, max_handoff_timeout_s * 0.4),
            )
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if proc.poll() is not None and descendants_gone:
            unregister_protected_pid(proc.pid)

    exited = proc.poll() is not None
    if not exited or not descendants_gone:
        raise HandoffError(
            f"Failed to stop {name} (PID {proc.pid}) within {max_handoff_timeout_s:.1f}s timeout."
        )

    # Wait briefly while polling MemAvailable until kernel page reclamation stabilizes (`E3`, `E4`)
    _u2, _t2, after_avail = read_proc_meminfo()
    prev_avail = after_avail
    settle_deadline = time.monotonic() + 1.2
    while time.monotonic() < settle_deadline:
        time.sleep(0.12)
        _u2, _t2, cur_avail = read_proc_meminfo()
        after_avail = cur_avail
        if (cur_avail - prev_avail) < 0.03 and (cur_avail - before_avail) >= 0.0:
            break
        prev_avail = cur_avail

    return MemoryHandoffMetrics(
        before_avail_gb=before_avail,
        after_avail_gb=after_avail,
        reclaimed_gb=round(after_avail - before_avail, 3),
        duration_s=round(time.monotonic() - t0, 3),
        engine_exited=True,
        descendants_gone=True,
    )


class MemoryRelayManager:
    """Coordinates verified mutual-exclusion between text LLM and media diffusion engines."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._engine_slot = threading.Lock()
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
        self._last_handoff: MemoryHandoffMetrics | None = None

    def _reconcile_dead_processes(self) -> None:
        """Recover relay state if a child engine terminated unexpectedly (`E11`)."""
        if self._llama_proc is not None and self._llama_proc.poll() is not None:
            rc = self._llama_proc.returncode
            unregister_protected_pid(self._llama_proc.pid)
            self._llama_proc = None
            if self._comfy_proc is None and not self._fast_img_server.is_running():
                self._set_owner(
                    "idle",
                    f"Text engine exited cleanly" if rc == 0 else f"Text engine stopped ({rc})",
                )
        if self._comfy_ag is not None and self._comfy_ag.poll() is not None:
            rc = self._comfy_ag.returncode
            try:
                self._comfy_ag.terminate_cleanly()
            except Exception:
                pass
            self._comfy_ag = None
            self._comfy_proc = None
            if self._llama_proc is None:
                self._set_owner("idle", f"Media engine stopped ({rc})")
        elif self._comfy_proc is not None and self._comfy_proc.poll() is not None:
            unregister_protected_pid(self._comfy_proc.pid)
            self._comfy_proc = None
            if self._llama_proc is None:
                self._set_owner("idle", "Media engine stopped")

    def get_telemetry(self) -> MemoryTelemetry:
        self._reconcile_dead_processes()
        used, total, avail = read_proc_meminfo()
        return MemoryTelemetry(
            owner=self._owner,
            used_gb=used,
            total_gb=total,
            available_gb=avail,
            detail=self._detail,
            active_model=self._active_model,
            last_handoff=self._last_handoff,
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

    def _check_min_available_ram(self, min_avail_gb: float, target_name: str) -> None:
        """Distinguish host memory pressure from failed handoff before starting an engine (`E12`)."""
        _u, total, avail = read_proc_meminfo()
        if total > 0 and avail < min_avail_gb:
            raise InsufficientMemoryError(
                f"Cannot launch {target_name}: only {avail:.2f} GB RAM available "
                f"(need >= {min_avail_gb:.1f} GB free; close heavy apps or lower batch/tier)."
            )

    def ensure_llm_running(
        self,
        timeout_s: float = 45.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Verify media engines are stopped & memory reclaimed before starting llama-server (`E1`–`E12`)."""
        with self._lock:
            self._reconcile_dead_processes()
            if self._shutting_down or (cancel_event and cancel_event.is_set()):
                return
            if self._fast_img_server.is_running():
                self._set_owner("transition", "Stopping image engine…")
                self._fast_img_server.stop()
            if cancel_event and cancel_event.is_set():
                return
            if self._comfy_ag is not None or self._comfy_proc is not None:
                self._set_owner("transition", "Stopping media engine…")
                if self._comfy_ag is not None:
                    self._comfy_ag.terminate_cleanly()
                    self._comfy_ag = None
                else:
                    self._last_handoff = _terminate_proc_group(self._comfy_proc, "comfyui")
                self._comfy_proc = None
            if cancel_event and cancel_event.is_set():
                return

            if self._llama_proc is not None and self._llama_proc.poll() is None:
                if self._http_ok(f"http://{LLAMA_HOST}:{self._llama_port}/health"):
                    self._set_owner("text", "Text engine", "local-llm")
                    return
                self._last_handoff = _terminate_proc_group(self._llama_proc, "llama-server-unhealthy")
                self._llama_proc = None

            if not LLAMA_SERVER_BIN.is_file():
                raise FileNotFoundError(f"llama-server not found at {LLAMA_SERVER_BIN}")
            try:
                from .hf_models import detect_hardware_profile, get_selected_model_path
                active_llm_path = get_selected_model_path("text") or LLM_MODEL_PATH
                hw = detect_hardware_profile()
            except Exception:
                active_llm_path = LLM_MODEL_PATH
                hw = None
            if not active_llm_path.is_file():
                raise FileNotFoundError(f"LLM model not found at {active_llm_path}")

            model_size_gb = active_llm_path.stat().st_size / (1024.0 ** 3)
            self._check_min_available_ram(min(2.5, max(0.8, model_size_gb * 0.5)), "llama-server")

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._llama_port = _pick_free_port(18081)
            self._set_owner("transition", "Starting text engine…", f"Loading {active_llm_path.name}…")
            cores = hw.cpu_cores if hw is not None else (os.cpu_count() or 4)
            threads = str(max(2, min(cores - 2, 12) if cores > 4 else cores))
            tier = hw.tier if hw is not None else "balanced"
            backend = hw.backend if hw is not None else "vulkan"
            ctx_size = "2048" if tier == "low" else ("8192" if tier == "workstation" else "4096")
            cmd = [
                str(LLAMA_SERVER_BIN),
                "--model",
                str(active_llm_path),
                "--threads",
                threads,
                "--load-mode",
                "none",
                "--ctx-size",
                ctx_size,
                "--reasoning-budget",
                "96",
                "--host",
                LLAMA_HOST,
                "--port",
                str(self._llama_port),
            ]
            if backend == "vulkan":
                cmd.extend(["--device", "Vulkan0", "--flash-attn", "on", "--n-gpu-layers", "99"])
            elif backend == "cuda":
                cmd.extend(["--flash-attn", "on", "--n-gpu-layers", "99"])
            else:
                cmd.extend(["--n-gpu-layers", "0"])
            log_fh = open(LOG_DIR / "llama-server.log", "w", encoding="utf-8")
            try:
                self._llama_proc = subprocess.Popen(
                    cmd,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                try:
                    log_fh.close()
                except OSError:
                    pass
            register_protected_pid(self._llama_proc.pid, "Text AI (locked)")

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._shutting_down or (cancel_event and cancel_event.is_set()):
                self.stop_llm()
                return
            proc_ref = self._llama_proc
            if proc_ref is not None and proc_ref.poll() is not None:
                rc = proc_ref.returncode
                unregister_protected_pid(proc_ref.pid)
                self._llama_proc = None
                self._set_owner("idle", "LLM failed to start")
                raise RuntimeError(f"llama-server exited with code {rc}")
            if self._http_ok(f"http://{LLAMA_HOST}:{self._llama_port}/health"):
                self._set_owner("text", "Text engine", active_llm_path.name)
                return
            time.sleep(0.35)

        self.stop_llm()
        raise TimeoutError("Timed out waiting for llama-server health endpoint.")

    def stop_llm(self) -> MemoryHandoffMetrics | None:
        with self._lock:
            metrics = None
            if self._llama_proc is not None:
                self._set_owner("transition", "Releasing text RAM…")
                metrics = _terminate_proc_group(self._llama_proc, "llama-server")
                self._last_handoff = metrics
                self._llama_proc = None
            if self._comfy_proc is None:
                self._set_owner("idle", "All engines offline", "All offline (chat to wake text AI)")
            return metrics

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
        """Query local llama-server (mutex-protected across concurrent requests, `E8`, `E9`)."""
        if not self._engine_slot.acquire(timeout=2.0):
            raise HandoffError("Inference engine is currently busy with another request.")
        try:
            use_stream = on_stream_delta is not None
            norm_msgs = self._normalize_messages(messages)
            for attempt in range(2):
                if cancel_event and cancel_event.is_set():
                    return ("", "")
                self.ensure_llm_running(cancel_event=cancel_event)
                if cancel_event and cancel_event.is_set():
                    return ("", "")
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
        finally:
            self._engine_slot.release()

    # -----------------------------------------------------------------------
    # Media Engine (ComfyUI with strict --lowvram + ROCm)
    # -----------------------------------------------------------------------

    def _start_comfyui(
        self,
        timeout_s: float = 90.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        with self._lock:
            self._reconcile_dead_processes()
            if self._shutting_down or (cancel_event and cancel_event.is_set()):
                return
            if self._fast_img_server.is_running():
                self._set_owner("transition", "Stopping image engine…")
                self._fast_img_server.stop()
            if self._llama_proc is not None:
                self._set_owner("transition", "Freeing text RAM…")
                self._last_handoff = _terminate_proc_group(self._llama_proc, "llama-server")
                self._llama_proc = None
            if cancel_event and cancel_event.is_set():
                return

            if self._comfy_proc is not None and self._comfy_proc.poll() is None:
                if self._comfy_http_ok("/system_stats"):
                    self._set_owner("media", "Media engine")
                    return
                self.stop_comfyui()

            if not COMFY_VENV_PYTHON.is_file() or not COMFY_MAIN.is_file():
                raise FileNotFoundError(f"ComfyUI not found in {COMFY_DIR}")

            self._check_min_available_ram(2.0, "ComfyUI")

            COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
            COMFY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._comfy_port = _pick_free_port(18188)
            inner_port = _pick_free_port(18189)

            self._set_owner("transition", "Starting net-isolated media engine…", "Starting ComfyUI (offline)…")
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
            try:
                self._comfy_ag = launch_airgapped_http_service(
                    cmd=cmd,
                    inner_port=inner_port,
                    outer_port=self._comfy_port,
                    protection_label="Media AI (net-isolated, locked)",
                    log_fh=comfy_log,
                    cwd=COMFY_DIR,
                    env=env,
                )
            finally:
                try:
                    comfy_log.close()
                except OSError:
                    pass
            self._comfy_proc = self._comfy_ag.proc

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._shutting_down or (cancel_event and cancel_event.is_set()):
                self.stop_comfyui()
                return
            if self._comfy_proc is not None and self._comfy_proc.poll() is not None:
                rc = self._comfy_proc.returncode
                self.stop_comfyui()
                self._set_owner("idle", "ComfyUI failed")
                raise RuntimeError(f"ComfyUI exited with code {rc}")
            if self._comfy_http_ok("/system_stats"):
                self._set_owner("media", "Media engine (net-isolated)")
                return
            time.sleep(0.5)

        self.stop_comfyui()
        raise TimeoutError("Timed out waiting for net-isolated ComfyUI API.")

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
        run_tag = f"pid{os.getpid()}_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(2).hex()}"
        temp_comfy_files: list[Path] = []
        total_imgs = max(1, min(8, int(batch_count)))

        if not self._engine_slot.acquire(timeout=2.0):
            raise HandoffError("Cannot start media render: inference engine slot is already active.")
        try:
            _emit(0, 0.06, "Stopping text engine & verifying reclaim…", "Reclaiming memory before isolated media boot", "Handoff")
            self.stop_llm()
            if cancel_event and cancel_event.is_set():
                return ([], None)
            self._check_min_available_ram(1.5, "media render engine")

            # Stage 2: One-by-one image generation (Net-isolated Vulkan sd-server preferred)
            use_vulkan = has_vulkan_image_engine()
            if on_stage is not None:
                on_stage("Net-Isolated Rendering")
            if use_vulkan:
                self._set_owner("media", "Loading net-isolated image AI", "sd-server (net-isolated)")
                _emit(
                    1,
                    0.12,
                    "Booting net-isolated Vulkan SDXL engine…",
                    "Namespace isolation verifying interfaces & egress block",
                    "Booting",
                )
                try:
                    self._fast_img_server.start(cancel_event=cancel_event)
                except AirGapIsolationError:
                    raise
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
                self._start_comfyui(cancel_event=cancel_event)
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

            # Stage 3: Chained Sliding Windows Video -> 24fps MP4
            if video_seconds > 0 and keyframes:
                self._start_comfyui(cancel_event=cancel_event)
                self._set_owner("media", "Rendering video", "Wan 2.1 VACE 1.3B + CausVid (ROCm)")
                anchor_src = keyframes[0]
                anchor_name = f"vimona_anchor_{run_tag}{anchor_src.suffix}"
                anchor_dst = COMFY_INPUT_DIR / anchor_name
                shutil.copy2(anchor_src, anchor_dst)
                temp_comfy_files.append(anchor_dst)

                window_count = max(1, min(16, round(video_seconds / 2.0)))
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
                        width=640,
                        height=368,
                        frames=17,
                        fps=10,
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
            self._engine_slot.release()

    def _comfy_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        hdrs = dict(extra or {})
        if self._comfy_ag is not None:
            hdrs.update(self._comfy_ag.request_headers())
        return hdrs

    def _comfy_http_ok(self, path: str) -> bool:
        try:
            if self._comfy_ag is not None:
                st, _ = self._comfy_ag.http_request("GET", path, timeout=1.5)
                return 200 <= st < 300
            return self._http_ok(f"http://{COMFY_HOST}:{self._comfy_port}{path}")
        except Exception:
            return False

    def _submit_and_wait(
        self,
        workflow: dict[str, Any],
        cancel_event: threading.Event | None = None,
        on_tick: Callable[[float], None] | None = None,
    ) -> list[Path]:
        payload = json.dumps({"prompt": workflow}).encode("utf-8")
        if self._comfy_ag is not None:
            st, raw = self._comfy_ag.http_request(
                "POST", "/prompt", body=payload, headers={"Content-Type": "application/json"}, timeout=20.0
            )
            if not (200 <= st < 300):
                raise RuntimeError(f"ComfyUI rejected prompt ({st})")
            res = json.loads(raw.decode("utf-8"))
        else:
            req = urllib.request.Request(
                f"http://{COMFY_HOST}:{self._comfy_port}/prompt",
                data=payload,
                headers=self._comfy_headers({"Content-Type": "application/json"}),
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        prompt_id = res["prompt_id"]

        tick = 0
        while True:
            if self._shutting_down or (cancel_event and cancel_event.is_set()):
                try:
                    if self._comfy_ag is not None:
                        self._comfy_ag.http_request(
                            "POST", "/interrupt", body=b"{}", headers={"Content-Type": "application/json"}, timeout=3.0
                        )
                    else:
                        urllib.request.urlopen(
                            urllib.request.Request(
                                f"http://{COMFY_HOST}:{self._comfy_port}/interrupt",
                                data=b"{}",
                                headers=self._comfy_headers({"Content-Type": "application/json"}),
                            ),
                            timeout=3,
                        )
                except Exception:
                    pass
                return []

            try:
                if self._comfy_ag is not None:
                    hst, hraw = self._comfy_ag.http_request("GET", f"/history/{prompt_id}", timeout=5.0)
                    hist = json.loads(hraw.decode("utf-8")) if 200 <= hst < 300 else {}
                else:
                    hreq = urllib.request.Request(
                        f"http://{COMFY_HOST}:{self._comfy_port}/history/{prompt_id}",
                        headers=self._comfy_headers(),
                    )
                    with urllib.request.urlopen(hreq, timeout=5) as hresp:
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
