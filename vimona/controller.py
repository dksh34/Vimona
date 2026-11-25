"""End-to-end backend orchestrator connecting Vimona UI, LLM, Firefox, and ComfyUI."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gi.repository import GLib

from .media import probe_video_info
from .memory_relay import MemoryRelayManager, MemoryTelemetry
from .safety_patterns import is_explicit_text
from .models import (
    ConversationTurn,
    KeyframeSpec,
    MemoryHandoffData,
    PromptSpec,
    ProseBlock,
    ReasoningStep,
    ReferenceCandidate,
    ResearchStepData,
    ResearchTab,
    VideoClipSpec,
)
from .search_worker import run_ephemeral_research
from .ram_manager import find_unnecessary_processes, terminate_unnecessary_processes
from .skills_loader import (
    build_system_prompt,
    collect_skill_media_patches,
    engineer_message_context,
)

if TYPE_CHECKING:
    from .window import VimonaWindow

_TOOL_CALL_RE = re.compile(
    r"```(?:tool_call|json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_tool_call(text: str) -> tuple[dict[str, Any] | None, str]:
    """Extract a structured tool-call JSON block if present and return (tool_dict, remaining_text)."""
    match = _TOOL_CALL_RE.search(text)
    if match:
        raw_json = match.group(1)
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict) and "tool" in parsed:
                cleaned = (text[: match.start()] + text[match.end() :]).strip()
                return (parsed, cleaned)
        except json.JSONDecodeError:
            pass

    # Fallback: check for bare JSON object containing "tool"
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidate = text[brace_start : brace_end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "tool" in parsed:
                cleaned = (text[:brace_start] + text[brace_end + 1 :]).strip()
                return (parsed, cleaned)
        except json.JSONDecodeError:
            pass

    # Fallback 2: recover partial/truncated JSON tool block (e.g. cut off near max_tokens)
    if brace_start != -1 and '"tool"' in text[brace_start:]:
        tail = text[brace_start:]
        t_m = re.search(r'"tool"\s*:\s*"([^"]+)"', tail)
        if t_m:
            partial: dict[str, Any] = {"tool": t_m.group(1)}
            for k in ("positive_prompt", "negative_prompt", "motion_prompt", "query", "url", "render_prompt"):
                km = re.search(rf'"{k}"\s*:\s*"((?:\\.|[^"\\])*)"', tail)
                if km:
                    try:
                        partial[k] = json.loads(f'"{km.group(1)}"')
                    except Exception:
                        partial[k] = km.group(1)
            for ik in ("batch_count", "video_seconds"):
                im = re.search(rf'"{ik}"\s*:\s*(\d+)', tail)
                if im:
                    partial[ik] = int(im.group(1))
            if "render_after" in tail:
                bm = re.search(r'"render_after"\s*:\s*(true|false)', tail, re.IGNORECASE)
                if bm:
                    partial["render_after"] = bm.group(1).lower() == "true"
            return (partial, text[:brace_start].strip())

    return (None, text.strip())


def _parse_prose_blocks(text: str) -> list[ProseBlock]:
    """Convert markdown-formatted LLM text into structured ProseBlock items."""
    blocks: list[ProseBlock] = []
    bullet_buf: list[str] = []
    para_buf: list[str] = []

    def _flush_para() -> None:
        if para_buf:
            blocks.append(ProseBlock(kind="p", text=" ".join(para_buf)))
            para_buf.clear()

    def _flush_bullets() -> None:
        if bullet_buf:
            blocks.append(ProseBlock(kind="ul", items=list(bullet_buf)))
            bullet_buf.clear()

    for line in text.splitlines():
        s = line.strip()
        if not s:
            _flush_para()
            _flush_bullets()
            continue
        if s.startswith("## "):
            _flush_para()
            _flush_bullets()
            blocks.append(ProseBlock(kind="h2", text=s[3:].strip()))
        elif s.startswith("### "):
            _flush_para()
            _flush_bullets()
            blocks.append(ProseBlock(kind="h3", text=s[4:].strip()))
        elif s.startswith("# "):
            _flush_para()
            _flush_bullets()
            blocks.append(ProseBlock(kind="h2", text=s[2:].strip()))
        elif s.startswith("- ") or s.startswith("* "):
            _flush_para()
            bullet_buf.append(s[2:].strip())
        else:
            _flush_bullets()
            para_buf.append(s)

    _flush_para()
    _flush_bullets()
    return blocks


_FOLLOWUP_STOPWORDS = frozenset(
    {
        "make", "turn", "change", "same", "with", "that", "this", "from", "into",
        "more", "less", "very", "just", "now", "then", "also", "add", "keep",
        "only", "want", "like", "give", "show", "scene", "image", "video",
        "clip", "shot", "look", "feel", "camera", "framing", "style", "please",
    }
)
_RESET_TOPIC_RE = re.compile(
    r"\b(new scene|something else|unrelated|ignore previous|start fresh|forget (that|all)|completely different)\b",
    re.IGNORECASE,
)
_FOLLOWUP_START_RE = re.compile(
    r"^\s*(now|also|and|then|but|instead|make (?:it|that|this)|turn (?:it|that|this)|"
    r"change|edit|modify|adjust|update|add|remove|replace|same|keep|more|less|"
    r"zoom|pan|tilt|closer|wider|darker|brighter|animate|convert|put)\b",
    re.IGNORECASE,
)
_FOLLOWUP_REF_RE = re.compile(
    r"\b(it|that|this scene|that scene|that shot|that image|this image|that video|"
    r"same (?:scene|person|character|subject|place|setting)|from (?:above|below|behind|side))\b",
    re.IGNORECASE,
)
_REUSE_FRAME_RE = re.compile(
    r"\b(animate (?:it|that|this)|make (?:it|that|this) (?:a |into a )?video|"
    r"(?:this|that|same|previous) (?:image|picture|photo|frame|keyframe))\b",
    re.IGNORECASE,
)


def _extract_prior_visual_anchor(prior_history: list[dict[str, str]]) -> str:
    """Return the most descriptive prior visual prompt or user subject from earlier turns."""
    spec_pat = re.compile(r"\[Previous Visual Prompt Spec:\s*([^\]]+)\]")
    res_pat = re.compile(r'\[Zero-Trace Web Research query="([^"]+)"')
    for msg in reversed(prior_history):
        content = (msg.get("content") or "").strip()
        if msg.get("role") == "assistant":
            m_spec = spec_pat.search(content)
            if m_spec and m_spec.group(1).strip():
                return m_spec.group(1).strip()
            m_res = res_pat.search(content)
            if m_res and m_res.group(1).strip():
                return m_res.group(1).strip()
    for msg in reversed(prior_history):
        if msg.get("role") == "user":
            txt = (msg.get("content") or "").strip()
            txt = re.sub(r"\[Camera framing:[^\]]+\]", "", txt).strip()
            if txt:
                return txt
    return ""


def _resolve_followup_prompt(
    history: list[dict[str, str]],
    latest_prompt: str,
    candidate_prompt: str = "",
) -> tuple[str, bool]:
    """Ensure follow-up prompts inherit established subject/setting context from prior turns."""
    prior_history = history[:-1] if history else []
    prior = _extract_prior_visual_anchor(prior_history)
    raw_latest = re.sub(r"\[Camera framing:[^\]]+\]", "", latest_prompt or "").strip()
    base_out = (candidate_prompt or latest_prompt).strip()
    if not prior or not raw_latest or _RESET_TOPIC_RE.search(raw_latest):
        return (base_out, False)

    words = [w for w in re.findall(r"[a-zA-Z0-9]+", raw_latest.lower()) if w not in _FOLLOWUP_STOPWORDS]
    is_followup = bool(
        _FOLLOWUP_START_RE.search(raw_latest)
        or _FOLLOWUP_REF_RE.search(raw_latest)
        or len(words) <= 5
    )
    reuse_frame = bool(_REUSE_FRAME_RE.search(raw_latest))
    if not is_followup:
        return (base_out, reuse_frame)

    prior_tokens = [
        w
        for w in re.findall(r"[a-zA-Z0-9]{4,}", prior.lower())
        if w not in _FOLLOWUP_STOPWORDS
    ]
    cand_low = (candidate_prompt or "").lower()
    overlap = sum(1 for tok in set(prior_tokens) if tok in cand_low)
    needed_overlap = 2 if len(set(prior_tokens)) >= 3 else 1
    if candidate_prompt and candidate_prompt.strip().lower() != latest_prompt.strip().lower() and overlap >= needed_overlap:
        return (candidate_prompt.strip(), reuse_frame)

    clean_prior = prior.rstrip(",. ")
    clean_latest = latest_prompt.strip()
    return (f"{clean_prior}, {clean_latest}", reuse_frame)


class VimonaController:
    """Runs local inference, Firefox web research, and ComfyUI RAM relay off the UI thread."""

    def __init__(self, window: VimonaWindow) -> None:
        self.window = window
        self.relay = MemoryRelayManager()
        self._cancel_event = threading.Event()
        self._active_worker: threading.Thread | None = None
        self._search_active = False
        self.high_ram_override: bool = False
        self.high_ram_margin_gb: float = 2.2
        self._ram_override_event = threading.Event()
        self._waiting_high_ram: bool = False
        self._turn_completion: dict[int, tuple[str, str, bool]] = {}

    def _set_turn_done(
        self,
        cancel_ev: threading.Event,
        title: str,
        body: str,
        sensitive: bool = False,
    ) -> None:
        self._turn_completion[id(cancel_ev)] = (title, body, sensitive)

    def start_background_boot(self) -> None:
        """Launch llama-server on Vulkan0 in background without blocking GTK startup."""
        def _boot() -> None:
            try:
                self.relay.ensure_llm_running()
            except Exception as exc:
                GLib.idle_add(self.window.show_toast, f"Text engine error: {exc}")

        threading.Thread(target=_boot, daemon=True, name="vimona-llm-boot").start()

    def get_telemetry(self) -> MemoryTelemetry:
        tel = self.relay.get_telemetry()
        if self._search_active:
            return MemoryTelemetry(
                owner="research",
                used_gb=tel.used_gb,
                total_gb=tel.total_gb,
                available_gb=tel.available_gb,
                detail="Firefox Search",
                active_model="Firefox Burner (kp=-2)",
            )
        return tel

    def set_high_ram_override(self, enabled: bool) -> None:
        """Allow generation to continue even when system RAM is near capacity."""
        self.high_ram_override = bool(enabled)
        if self.high_ram_override:
            self._ram_override_event.set()
        else:
            self._ram_override_event.clear()

    def is_waiting_high_ram(self) -> bool:
        return self._waiting_high_ram

    def _is_ram_high(self) -> tuple[bool, float, float]:
        tel = self.get_telemetry()
        high = tel.total_gb > 0 and tel.used_gb >= (tel.total_gb - self.high_ram_margin_gb)
        return (high, tel.used_gb, tel.total_gb)

    def _wait_if_high_ram(self, cancel_ev: threading.Event) -> bool:
        """Pause generation cleanly when RAM is high unless Override is enabled or RAM drops."""
        notified = False
        try:
            while not cancel_ev.is_set():
                if self.high_ram_override:
                    return True
                is_high, used_gb, total_gb = self._is_ram_high()
                if not is_high:
                    return True
                self._waiting_high_ram = True
                if not notified:
                    notified = True
                    GLib.idle_add(
                        self.window.on_high_ram_gate_state, True, used_gb, total_gb
                    )
                self._ram_override_event.wait(0.4)
            return False
        finally:
            self._waiting_high_ram = False
            if notified:
                GLib.idle_add(self.window.on_high_ram_gate_state, False, 0.0, 0.0)

    def prepare_between_job_hygiene(self, auto_clean: bool = True) -> dict[str, object]:
        """Ensure Text engine (Gemma) is unloaded (0.0 GB) and sweep memory between queued jobs."""
        self.relay.stop_llm()
        cleaned_count = 0
        freed_mb = 0.0
        tel = self.relay.get_telemetry()
        needs_sweep = auto_clean and (tel.total_gb <= 0 or tel.used_gb >= max(4.0, tel.total_gb - 5.5))
        if needs_sweep:
            try:
                targets = find_unnecessary_processes()
                if targets:
                    res = terminate_unnecessary_processes(targets)
                    cleaned_count = int(res[0]) if isinstance(res, tuple) else 0
                    freed_mb = float(res[1]) if isinstance(res, tuple) and len(res) > 1 else 0.0
            except Exception:
                pass
        return {
            "llm_unloaded": self.relay._llama_proc is None,
            "cleaned_count": cleaned_count,
            "freed_mb": freed_mb,
        }

    def cancel_active(self) -> None:
        self._cancel_event.set()
        self._ram_override_event.set()
        self._search_active = False

    def shutdown(self) -> None:
        self._cancel_event.set()
        self._ram_override_event.set()
        self.relay.shutdown_all()

    def handle_submit(
        self,
        chat_id: str,
        prompt: str,
        skill: str,
        batch_count: int,
        clip_seconds: int,
        steps_override: int | None = None,
    ) -> None:
        self._cancel_event = threading.Event()
        if self.high_ram_override:
            self._ram_override_event.set()
        else:
            self._ram_override_event.clear()
        worker = threading.Thread(
            target=self._run_turn,
            args=(chat_id, prompt, skill, batch_count, clip_seconds, self._cancel_event, steps_override),
            daemon=True,
            name="vimona-turn",
        )
        self._active_worker = worker
        worker.start()

    # -----------------------------------------------------------------------
    # Worker Execution
    # -----------------------------------------------------------------------

    def _run_turn(
        self,
        chat_id: str,
        prompt: str,
        skill: str,
        batch_count: int,
        clip_seconds: int,
        cancel_ev: threading.Event,
        steps_override: int | None = None,
    ) -> None:
        t0 = time.monotonic()
        try:
            if not self._wait_if_high_ram(cancel_ev):
                return

            if skill == "Web Search":
                self._handle_direct_research(
                    chat_id, prompt, skill, batch_count, clip_seconds, cancel_ev, steps_override=steps_override
                )
                return

            if skill in ("Make Images", "Make Video"):
                video_sec = clip_seconds if skill == "Make Video" else 0
                self._handle_direct_media(
                    chat_id, prompt, skill, batch_count, video_sec, cancel_ev, steps_override=steps_override
                )
                return

            # Default "Chat & Ideas": ask llama-server with modular skills injected (live thinking stream)
            messages = self._build_messages(chat_id, skill, batch_count, clip_seconds, user_prompt=prompt)
            reply_text, model_reasoning = self.relay.chat_completion(
                messages,
                max_tokens=1024,
                on_stream_delta=lambda r, c: GLib.idle_add(self.window.update_live_thinking, r, c),
                cancel_event=cancel_ev,
            )
            if cancel_ev.is_set():
                return

            tool_call, clean_text = _extract_tool_call(reply_text)
            if tool_call is not None:
                tool_name = str(tool_call.get("tool", "")).lower()
                if tool_name == "web_search":
                    hist = self.window.get_chat_messages_for_llm(chat_id)
                    q = str(tool_call.get("query", "")).strip()
                    u = str(tool_call.get("url", "")).strip()
                    r_after = bool(tool_call.get("render_after", False))
                    r_prompt = str(tool_call.get("render_prompt") or prompt).strip()
                    if not u:
                        q, _ = _resolve_followup_prompt(hist, prompt, q)
                    r_prompt, reuse_frame = _resolve_followup_prompt(hist, prompt, r_prompt)
                    r_bc = int(tool_call.get("batch_count") or batch_count)
                    r_vs = int(tool_call.get("video_seconds") or 0)
                    r_beats = [str(b) for b in tool_call.get("beats", [])]
                    if not r_after and re.search(
                        r"\b(make|generate|render|create|draw|paint|animate|clip|video)\b",
                        prompt,
                        re.IGNORECASE,
                    ):
                        r_after = True
                    self._execute_research_and_followup(
                        chat_id,
                        prompt,
                        q,
                        u,
                        skill,
                        r_bc,
                        clip_seconds,
                        cancel_ev,
                        render_after=r_after,
                        render_prompt=r_prompt,
                        video_seconds=r_vs,
                        beats=r_beats,
                        steps_override=steps_override,
                    )
                    return

                if tool_name == "render_media":
                    hist = self.window.get_chat_messages_for_llm(chat_id)
                    pos = str(tool_call.get("positive_prompt") or prompt)
                    pos, reuse_frame = _resolve_followup_prompt(hist, prompt, pos)
                    neg = str(tool_call.get("negative_prompt") or "")
                    mot_raw = str(tool_call.get("motion_prompt") or pos)
                    mot, _ = _resolve_followup_prompt(hist, prompt, mot_raw)
                    bc = int(tool_call.get("batch_count") or batch_count)
                    vs = int(tool_call.get("video_seconds") or 0)
                    beats = [str(b) for b in tool_call.get("beats", [])]
                    init_b64 = self.window.get_last_keyframe_b64(chat_id) if reuse_frame else None
                    self._execute_media_relay(
                        chat_id=chat_id,
                        skill="Make Video" if vs > 0 else "Make Images",
                        positive_prompt=pos,
                        negative_prompt=neg,
                        motion_prompt=mot,
                        batch_count=bc,
                        video_seconds=vs,
                        beats=beats,
                        intro_text=clean_text,
                        cancel_ev=cancel_ev,
                        init_image_b64=init_b64,
                        init_strength=0.70,
                        steps_override=steps_override,
                    )
                    return

            dur = f"{time.monotonic() - t0:.1f}s"
            turn = ConversationTurn(
                id=str(uuid.uuid4()),
                role="assistant",
                reasoning=ReasoningStep(
                    skill=skill,
                    duration=dur,
                    summary=model_reasoning or "Generated locally via llama-server (Vulkan0).",
                    bullets=["Context assembled from modular markdown skills."],
                ),
                prose=_parse_prose_blocks(clean_text or reply_text),
            )
            GLib.idle_add(self.window.append_turn, chat_id, turn)
            reply_summary = (clean_text or reply_text).strip()
            self._set_turn_done(
                cancel_ev,
                "Vimona finished replying",
                reply_summary,
                sensitive=is_explicit_text(prompt) or is_explicit_text(reply_summary),
            )
        except Exception as exc:
            err_turn = ConversationTurn(
                id=str(uuid.uuid4()),
                role="assistant",
                prose=[ProseBlock(kind="p", text=f"Error: {exc}")],
            )
            GLib.idle_add(self.window.append_turn, chat_id, err_turn)
            self._set_turn_done(
                cancel_ev,
                "Vimona finished with error",
                str(exc),
                sensitive=is_explicit_text(prompt),
            )
        finally:
            self._search_active = False
            self._waiting_high_ram = False
            done_info = self._turn_completion.pop(id(cancel_ev), None)
            if done_info is not None and not cancel_ev.is_set():
                GLib.idle_add(
                    self.window.notify_turn_finished,
                    chat_id,
                    done_info[0],
                    done_info[1],
                    done_info[2],
                )
            GLib.idle_add(self.window.set_busy, False)

    def _build_messages(
        self,
        chat_id: str,
        skill: str,
        batch_count: int,
        clip_seconds: int,
        user_prompt: str = "",
    ) -> list[dict[str, str]]:
        history = self.window.get_chat_messages_for_llm(chat_id)
        if not user_prompt and history:
            for msg in reversed(history):
                if msg.get("role") == "user":
                    user_prompt = str(msg.get("content") or "")
                    break
        sys_prompt = build_system_prompt(
            active_mode=skill,
            batch_count=batch_count,
            clip_seconds=clip_seconds,
            user_prompt=user_prompt,
        )
        return engineer_message_context(sys_prompt, history)

    def _handle_direct_research(
        self,
        chat_id: str,
        prompt: str,
        skill: str,
        batch_count: int,
        clip_seconds: int,
        cancel_ev: threading.Event,
        steps_override: int | None = None,
    ) -> None:
        url = ""
        query = prompt
        url_match = re.search(r"https?://\S+", prompt)
        if url_match:
            url = url_match.group(0).rstrip(".,)")
            query = ""
        else:
            hist = self.window.get_chat_messages_for_llm(chat_id)
            query, _ = _resolve_followup_prompt(hist, prompt)
        self._execute_research_and_followup(
            chat_id,
            prompt,
            query,
            url,
            skill,
            batch_count,
            clip_seconds,
            cancel_ev,
            steps_override=steps_override,
        )

    def _execute_research_and_followup(
        self,
        chat_id: str,
        user_prompt: str,
        query: str,
        url: str,
        skill: str,
        batch_count: int,
        clip_seconds: int,
        cancel_ev: threading.Event,
        render_after: bool = False,
        render_prompt: str = "",
        video_seconds: int = 0,
        beats: list[str] | None = None,
        steps_override: int | None = None,
    ) -> None:
        # Step 1: Gate ephemeral /dev/shm Firefox access behind interactive SlideCommit permission
        perm_id = str(uuid.uuid4())
        decision_ev = threading.Event()
        decision = {"allowed": False}

        def _on_perm_decision(allowed: bool) -> None:
            decision["allowed"] = allowed
            decision_ev.set()

        GLib.idle_add(
            self.window.show_research_permission,
            perm_id,
            chat_id,
            query,
            url,
            _on_perm_decision,
        )
        while not decision_ev.wait(timeout=0.15):
            if cancel_ev.is_set() or self.relay._shutting_down:
                GLib.idle_add(self.window.finish_research_permission, perm_id, False, "Cancelled")
                return

        if not decision["allowed"] or cancel_ev.is_set():
            declined_turn = ConversationTurn(
                id=str(uuid.uuid4()),
                role="assistant",
                prose=[
                    ProseBlock(
                        kind="p",
                        text="Zero-trace web reference pull was declined. No external request or browser sandbox was spawned.",
                    )
                ],
            )
            GLib.idle_add(self.window.append_turn, chat_id, declined_turn)
            return

        # Step 2: Spawn /dev/shm RAM Firefox, scrape refs + visual tokens into RAM, then scorched-earth wipe
        self._search_active = True
        try:
            search_res = run_ephemeral_research(
                query=query,
                url=url,
                max_results=4,
                pull_images=True,
                max_images=4,
            )
        finally:
            self._search_active = False

        if cancel_ev.is_set():
            GLib.idle_add(self.window.finish_research_permission, perm_id, False, "Cancelled")
            return

        GLib.idle_add(
            self.window.finish_research_permission,
            perm_id,
            not bool(search_res.get("error")),
            str(search_res.get("error") or "") or None,
        )

        trace = search_res.get("trace_report") or {}
        wiped_ok = bool(trace.get("wiped", True))
        trace_badge = (
            f"{trace.get('profile_kind', '/dev/shm')} wiped ✓"
            if wiped_ok
            else "Sandbox cleaned"
        )

        raw_results = search_res.get("results", [])
        tabs = [
            ResearchTab(
                title=str(r.get("title", "Result")),
                url=str(r.get("url", "")),
                takeaway=str(r.get("takeaway", "")),
            )
            for r in raw_results
        ]
        if not tabs and search_res.get("error"):
            tabs = [
                ResearchTab(
                    title="Search error",
                    url="dev-shm-ephemeral-wiped",
                    takeaway=str(search_res["error"]),
                )
            ]

        ref_candidates: list[ReferenceCandidate] = [
            ReferenceCandidate(
                id=str(rf.get("id", f"ref-{i + 1:02d}")),
                title=str(rf.get("title", f"Reference {i + 1}")),
                source_url=str(rf.get("source_url", "")),
                image_url=str(rf.get("image_url", "")),
                alt_text=str(rf.get("alt_text", "")),
                palette_tags=[str(t) for t in rf.get("palette_tags", [])],
                lighting_mood=str(rf.get("lighting_mood", "")),
                style_tokens=str(rf.get("style_tokens", "")),
                explicit=bool(rf.get("explicit", False)),
                width=int(rf.get("width", 0)),
                height=int(rf.get("height", 0)),
                image_bytes_b64=str(rf.get("image_bytes_b64", "")),
            )
            for i, rf in enumerate(search_res.get("references", []))
            if isinstance(rf, dict)
        ]

        # Build concise visual token summary extracted directly from RAM image buffers + metadata
        visual_tokens: list[str] = []
        for rc in ref_candidates[:3]:
            if rc.style_tokens:
                visual_tokens.append(f"{rc.title}: {rc.style_tokens}")
        visual_summary = "; ".join(visual_tokens)
        snippets_txt = "\n".join(
            f"- {tb.title} ({tb.url}): {tb.takeaway}" for tb in tabs
        )

        base_prompt = render_prompt or user_prompt
        if render_after:
            # Ask local LLM to craft an upgraded diffusion prompt combining extracted lighting/palette tokens
            upgraded_prompt = base_prompt
            if visual_summary:
                top_style = ref_candidates[0].style_tokens if ref_candidates else ""
                upgraded_prompt = f"{base_prompt}, {top_style}".strip(", ")
            try:
                msgs = self._build_messages(chat_id, skill, batch_count, video_seconds or clip_seconds)
                msgs.append(
                    {
                        "role": "system",
                        "content": (
                            "The ephemeral /dev/shm browser pulled visual references and has already been wiped. "
                            f"Page context:\n{snippets_txt}\nExtracted style & lighting tokens:\n{visual_summary}\n"
                            "Write ONE concise comma-separated Pony V6 XL visual prompt (under 55 words, no quotes, "
                            "no tool_call block) fusing the user's request with those lighting, color palette, and framing tokens:"
                        ),
                    }
                )
                llm_upgraded, _ = self.relay.chat_completion(
                    msgs,
                    temperature=0.45,
                    max_tokens=160,
                    on_stream_delta=lambda r, c: GLib.idle_add(self.window.update_live_thinking, r, c),
                    cancel_event=cancel_ev,
                )
                cleaned = llm_upgraded.strip().strip('"').strip("'")
                if cleaned and "tool_call" not in cleaned.lower():
                    upgraded_prompt = cleaned
            except Exception:
                pass

            # Append Safe-Blurred RAM Reference Summary card first
            res_turn = ConversationTurn(
                id=str(uuid.uuid4()),
                role="assistant",
                research=ResearchStepData(
                    browser_label="Ephemeral Firefox (/dev/shm wiped)",
                    query=query or url,
                    tabs=tabs,
                    status=f"{len(ref_candidates)} refs in RAM · 0B on SSD",
                    references=ref_candidates,
                    trace_summary=trace_badge,
                ),
                prose=[
                    ProseBlock(
                        kind="p",
                        text=(
                            f"Browser sandbox destroyed ({trace_badge}). Select a Safe-Blurred reference "
                            "below to launch offline air-gapped rendering."
                        ),
                    )
                ],
            )
            GLib.idle_add(self.window.append_turn, chat_id, res_turn)

            # Step 3: Interactive Safe-Blurred Reference Picker (wait cleanly for user choice)
            picker_id = str(uuid.uuid4())
            pick_ev = threading.Event()
            pick_state: dict[str, Any] = {"confirmed": False, "ref": None, "prompt": upgraded_prompt}

            def _on_pick_confirm(chosen: ReferenceCandidate | None, final_prompt: str) -> None:
                pick_state["confirmed"] = True
                pick_state["ref"] = chosen
                pick_state["prompt"] = final_prompt or upgraded_prompt
                pick_ev.set()

            def _on_pick_cancel() -> None:
                pick_state["confirmed"] = False
                pick_ev.set()

            GLib.idle_add(
                self.window.show_reference_picker,
                picker_id,
                chat_id,
                query or url,
                trace_badge,
                ref_candidates,
                upgraded_prompt,
                video_seconds > 0,
                _on_pick_confirm,
                _on_pick_cancel,
            )
            while not pick_ev.wait(timeout=0.15):
                if cancel_ev.is_set() or self.relay._shutting_down:
                    GLib.idle_add(self.window.finish_reference_picker, picker_id, "Cancelled")
                    return

            if not pick_state["confirmed"] or cancel_ev.is_set():
                return

            chosen_ref: ReferenceCandidate | None = pick_state["ref"]
            ref_b64 = chosen_ref.image_bytes_b64 if chosen_ref is not None else None

            # Step 4: Air-gapped offline rendering (llama-server shut down, netns sealed)
            self._execute_media_relay(
                chat_id=chat_id,
                skill="Make Video" if video_seconds > 0 else "Make Images",
                positive_prompt=str(pick_state["prompt"]),
                negative_prompt="",
                motion_prompt=str(pick_state["prompt"]),
                batch_count=batch_count,
                video_seconds=video_seconds,
                beats=beats or [],
                intro_text="",
                cancel_ev=cancel_ev,
                init_image_b64=ref_b64,
                init_strength=0.74,
                steps_override=steps_override,
            )
            return

        synthesis = ""
        try:
            msgs = self._build_messages(chat_id, skill, batch_count, clip_seconds)
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "Here are zero-trace ephemeral Firefox search results (profile wiped from /dev/shm) "
                        "and extracted visual style tokens:\n"
                        f"{snippets_txt}\nVisual lighting/palette tokens: {visual_summary or 'None'}\n"
                        "Synthesize the useful details directly for the user without emitting another tool call."
                    ),
                }
            )
            synthesis, model_reasoning = self.relay.chat_completion(
                msgs,
                max_tokens=768,
                on_stream_delta=lambda r, c: GLib.idle_add(self.window.update_live_thinking, r, c),
                cancel_event=cancel_ev,
            )
        except Exception:
            model_reasoning = ""
            synthesis = "Fetched reference results above via zero-trace /dev/shm Firefox session."

        turn = ConversationTurn(
            id=str(uuid.uuid4()),
            role="assistant",
            reasoning=ReasoningStep(
                skill=skill,
                duration="Verified",
                summary=model_reasoning or "Synthesized zero-trace ephemeral Firefox results.",
                bullets=[f"Pulled {len(ref_candidates)} visual refs into RAM and wiped /dev/shm sandbox."],
            )
            if model_reasoning
            else None,
            research=ResearchStepData(
                browser_label="Ephemeral Firefox (/dev/shm wiped)",
                query=query or url,
                tabs=tabs,
                status=f"{len(raw_results)} pages · {len(ref_candidates)} refs",
                references=ref_candidates,
                trace_summary=trace_badge,
            ),
            prose=_parse_prose_blocks(synthesis),
        )
        GLib.idle_add(self.window.append_turn, chat_id, turn)
        self._set_turn_done(
            cancel_ev,
            "Vimona finished web research",
            f"{len(raw_results)} page(s) · {len(ref_candidates)} ref(s) in RAM (/dev/shm wiped)",
            sensitive=bool(
                is_explicit_text(user_prompt)
                or is_explicit_text(query)
                or any(rf.explicit for rf in ref_candidates)
            ),
        )

    def _handle_direct_media(
        self,
        chat_id: str,
        prompt: str,
        skill: str,
        batch_count: int,
        video_seconds: int,
        cancel_ev: threading.Event,
        steps_override: int | None = None,
    ) -> None:
        history = self.window.get_chat_messages_for_llm(chat_id)
        prior_anchor = _extract_prior_visual_anchor(history[:-1] if history else [])
        pos_prompt, reuse_frame = _resolve_followup_prompt(history, prompt)
        neg_prompt = ""
        motion_prompt = pos_prompt
        beats: list[str] = []

        # Ask LLM quickly to craft Pony V6 XL + 4s beat tags before shutting it down for ComfyUI
        try:
            msgs = self._build_messages(
                chat_id,
                skill,
                batch_count,
                video_seconds or 4,
                user_prompt=prompt,
            )
            continuity_note = (
                f" Prior established scene/prompt: {prior_anchor!r}. If the latest user turn modifies, extends, "
                "re-lights, re-angles, or animates that scene, MERGE all established subject/setting details "
                "from history with the requested update in positive_prompt and motion_prompt."
                if prior_anchor
                else ""
            )
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "Respond ONLY with a single raw JSON object (no markdown fences) for `render_media` "
                        f"with tool='render_media', batch_count={batch_count}, and video_seconds={video_seconds}. "
                        "Craft rich uncensored Pony V6 XL tags and 4-second action beats."
                        f"{continuity_note}"
                    ),
                }
            )
            llm_out, _ = self.relay.chat_completion(
                msgs,
                temperature=0.5,
                max_tokens=768,
                on_stream_delta=lambda r, c: GLib.idle_add(self.window.update_live_thinking, r, c),
                cancel_event=cancel_ev,
            )
            tc, _ = _extract_tool_call(llm_out)
            if tc and tc.get("tool") == "render_media":
                llm_pos = str(tc.get("positive_prompt") or "").strip()
                pos_prompt, rf2 = _resolve_followup_prompt(history, prompt, llm_pos)
                reuse_frame = reuse_frame or rf2
                neg_prompt = str(tc.get("negative_prompt") or "")
                llm_mot = str(tc.get("motion_prompt") or "").strip()
                motion_prompt, _ = _resolve_followup_prompt(history, prompt, llm_mot or pos_prompt)
                beats = [str(b) for b in tc.get("beats", [])]
        except Exception:
            pass

        if cancel_ev.is_set():
            return

        init_b64 = self.window.get_last_keyframe_b64(chat_id) if reuse_frame else None
        self._execute_media_relay(
            chat_id=chat_id,
            skill=skill,
            positive_prompt=pos_prompt,
            negative_prompt=neg_prompt,
            motion_prompt=motion_prompt,
            batch_count=batch_count,
            video_seconds=video_seconds,
            beats=beats,
            intro_text="",
            cancel_ev=cancel_ev,
            init_image_b64=init_b64,
            init_strength=0.70,
            steps_override=steps_override,
        )

    def _execute_media_relay(
        self,
        chat_id: str,
        skill: str,
        positive_prompt: str,
        negative_prompt: str,
        motion_prompt: str,
        batch_count: int,
        video_seconds: int,
        beats: list[str],
        intro_text: str,
        cancel_ev: threading.Event,
        init_image_b64: str | None = None,
        init_strength: float = 0.72,
        steps_override: int | None = None,
    ) -> None:
        if not self._wait_if_high_ram(cancel_ev):
            return
        t0 = time.monotonic()
        handoff_turn_id = str(uuid.uuid4())
        stream_turn_id = str(uuid.uuid4())
        init_handoff = MemoryHandoffData(
            offloaded_model="Stopping text engine…",
            active_pipeline="Zero-Overlap Vulkan + ROCm Media Engine",
            vram_allocated="Preparing…",
            ram_cache="Fast Vulkan SDXL + 24fps Video",
            stage_index=0,
            stages=["1. Free text RAM", "2. Make images"]
            + ([f"3. Render {video_seconds}s video"] if video_seconds > 0 else []),
            progress_fraction=0.04,
            frame_status="Reclaiming text memory before launching image engine…",
            eta_label="Starting",
        )
        GLib.idle_add(
            self.window.append_turn,
            chat_id,
            ConversationTurn(
                id=handoff_turn_id,
                role="assistant",
                handoff=init_handoff,
            ),
        )

        streamed_keyframes: list[KeyframeSpec] = []

        def _on_progress(data: MemoryHandoffData) -> None:
            GLib.idle_add(self.window.update_handoff_turn, handoff_turn_id, data)

        def _on_image_ready(idx: int, staged_path: Path, engine_label: str) -> None:
            kf = KeyframeSpec(
                id=f"kf-{idx + 1:02d}",
                code=f"{idx + 1:02d}",
                title=staged_path.name,
                image_path=str(staged_path),
                shot_type=engine_label or "Pony V6 XL (GGUF Q8_0)",
                focal_length="lcm · DMD2" if "dmd2" in (engine_label or "").lower() else "euler_a",
                aperture="CFG 1.6 · 5 steps" if "dmd2" in (engine_label or "").lower() else "Fast Vulkan",
                camera_move="CLIP skip -2",
                notes=positive_prompt,
            )
            streamed_keyframes.append(kf)
            GLib.idle_add(
                self.window.stream_keyframe,
                chat_id,
                stream_turn_id,
                kf,
                batch_count,
                positive_prompt,
            )

        neg_patch, node_overrides = collect_skill_media_patches(
            user_prompt=positive_prompt,
            active_mode=skill,
        )
        eff_overrides: dict[str, Any] = dict(node_overrides or {})
        if isinstance(steps_override, int) and steps_override > 0:
            eff_overrides["steps"] = max(1, min(60, int(steps_override)))
        effective_neg = ", ".join(p for p in (negative_prompt.strip(), neg_patch.strip()) if p)

        img_paths, video_path = self.relay.run_media_relay(
            positive_prompt=positive_prompt,
            negative_prompt=effective_neg,
            motion_prompt=motion_prompt,
            batch_count=batch_count,
            video_seconds=video_seconds,
            beats=beats,
            on_progress=_on_progress,
            on_image_ready=_on_image_ready,
            cancel_event=cancel_ev,
            init_image_b64=init_image_b64,
            init_strength=init_strength,
            node_overrides=eff_overrides or None,
            on_stage=lambda st_lbl: GLib.idle_add(self.window.update_playlist_job_status, st_lbl),
        )

        if cancel_ev.is_set():
            return

        # Only populate keyframes if an image wasn't streamed live (e.g., mocked relay)
        final_keyframes: list[KeyframeSpec] = []
        if not streamed_keyframes and img_paths:
            final_keyframes = [
                KeyframeSpec(
                    id=f"kf-{idx + 1:02d}",
                    code=f"{idx + 1:02d}",
                    title=p.name,
                    image_path=str(p),
                    shot_type="Pony V6 XL (GGUF Q8_0)",
                    focal_length="lcm · DMD2",
                    aperture="CFG 1.6 · 5 steps",
                    camera_move="CLIP skip -2",
                    notes=positive_prompt,
                )
                for idx, p in enumerate(img_paths)
            ]

        video_spec: VideoClipSpec | None = None
        if video_path is not None and video_path.is_file():
            dur, fps, vw, vh = probe_video_info(video_path)
            video_spec = VideoClipSpec(
                id=f"clip-{int(time.time())}",
                title=video_path.name,
                duration_s=dur,
                fps=fps,
                width=vw,
                height=vh,
                video_path=str(video_path),
                camera_move="Wan 2.1 VACE 1.3B + CausVid · 24fps",
                prompt_summary=motion_prompt,
            )

        elapsed = f"{time.monotonic() - t0:.1f}s"
        prose_blocks = _parse_prose_blocks(intro_text) if intro_text else []
        if not streamed_keyframes and not final_keyframes and video_spec is None:
            prose_blocks.append(
                ProseBlock(
                    kind="p",
                    text="Render finished without output files. Check ~/.local/share/vimona logs.",
                )
            )

        rendered_count = len(streamed_keyframes) or len(final_keyframes)
        result_turn = ConversationTurn(
            id=str(uuid.uuid4()),
            role="assistant",
            reasoning=ReasoningStep(
                skill=skill,
                duration=elapsed,
                summary="Streamed 1-by-1 via Vulkan SDXL + optical-flow Wan 2.1 video → all engines back offline.",
                bullets=[
                    f"Delivered {rendered_count} sequential keyframe(s) live to chat & Gallery.",
                    f"Video output: {video_path.name} ({elapsed})" if video_spec else f"Completed in {elapsed}.",
                ],
            ),
            prose=prose_blocks,
            prompt_spec=None if streamed_keyframes else PromptSpec(
                title="Rendered Prompt",
                tokens=[
                    ("Positive", positive_prompt),
                    ("Negative", negative_prompt or "Default Pony technical negative"),
                    ("Motion", motion_prompt if video_seconds > 0 else "N/A"),
                ],
                raw_prompt=positive_prompt,
            ),
            keyframes=final_keyframes,
            keyframe_batch_default=batch_count if batch_count in (1, 2, 4, 8) else 4,
            video_clip=video_spec,
        )
        GLib.idle_add(self.window.append_turn, chat_id, result_turn)
        if video_spec is not None:
            n_title = "Vimona finished video"
            n_body = f"Rendered {video_spec.duration_s:.1f}s video + {rendered_count} keyframe(s) in {elapsed}"
        elif rendered_count > 0:
            n_title = "Vimona finished images"
            n_body = f"Generated {rendered_count} image(s) in {elapsed}"
        else:
            n_title = "Vimona finished render"
            n_body = f"Completed in {elapsed}"
        self._set_turn_done(
            cancel_ev,
            n_title,
            n_body,
            sensitive=is_explicit_text(positive_prompt) or is_explicit_text(motion_prompt),
        )
