"""Offline local chat history persistence (~/.local/share/vimona/chats.json)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import (
    ChatSession,
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

STATE_DIR = Path("~/.local/share/vimona").expanduser()
CHATS_FILE = STATE_DIR / "chats.json"
EPHEMERAL_PREFIX = "vimona-session-media-"
EPHEMERAL_MEDIA_DIR = Path(tempfile.gettempdir()) / f"{EPHEMERAL_PREFIX}{os.getpid()}"


def cleanup_ephemeral_media(include_current: bool = True) -> None:
    """Delete volatile unsaved session media directories, /dev/shm profiles, and stray temp files."""
    tmp_root = Path(tempfile.gettempdir())
    for entry in tmp_root.glob(f"{EPHEMERAL_PREFIX}*"):
        if not entry.is_dir():
            continue
        if not include_current and entry == EPHEMERAL_MEDIA_DIR:
            continue
        shutil.rmtree(entry, ignore_errors=True)
    shm_root = Path("/dev/shm")
    if shm_root.is_dir():
        uid = os.getuid() if hasattr(os, "getuid") else 0
        for entry in shm_root.glob("vimona_ephem_*"):
            try:
                if entry.stat().st_uid == uid:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass
    legacy_burner = Path("~/firefox-burner-profile").expanduser()
    if legacy_burner.exists() or legacy_burner.is_symlink():
        shutil.rmtree(legacy_burner, ignore_errors=True)
    for subdir in (Path("~/comfyui/output").expanduser(), Path("~/comfyui/input").expanduser()):
        if not subdir.is_dir():
            continue
        for item in subdir.glob("vimona_*"):
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass


def stage_ephemeral_media(src: Path) -> Path:
    """Move a newly generated file out of ComfyUI output into volatile session scratch."""
    EPHEMERAL_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = EPHEMERAL_MEDIA_DIR / f"{uuid.uuid4().hex[:8]}_{src.name}"
    try:
        shutil.move(str(src), str(dest))
        return dest
    except Exception:
        return src


def save_media_file(src: Path, dest: Path) -> Path:
    """Explicitly copy an ephemeral session image or video to a user-chosen path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest))
    return dest


def delete_ephemeral_file(path_str: str | None) -> None:
    """Immediately delete a temporary session image/video from disk."""
    if not path_str:
        return
    try:
        Path(path_str).unlink(missing_ok=True)
    except OSError:
        pass


def save_chats(chats: list[ChatSession]) -> None:
    """Persist text chat history locally without saving generated images or videos."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload: list[dict[str, Any]] = []
    for ch in chats:
        ch_dict = asdict(ch)
        for turn in ch_dict.get("turns", []):
            if isinstance(turn, dict):
                turn["keyframes"] = []
                turn["video_clip"] = None
                if isinstance(turn.get("research"), dict):
                    turn["research"]["references"] = []
        payload.append(ch_dict)
    tmp = CHATS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CHATS_FILE)


def load_chats() -> list[ChatSession]:
    """Load saved chats from disk; returns empty list if none exist."""
    if not CHATS_FILE.is_file():
        return []
    try:
        raw = json.loads(CHATS_FILE.read_text(encoding="utf-8"))
        return [_decode_chat(item) for item in raw if isinstance(item, dict)]
    except Exception:
        return []


def _decode_chat(d: dict[str, Any]) -> ChatSession:
    turns = [_decode_turn(t) for t in d.get("turns", []) if isinstance(t, dict)]
    return ChatSession(
        id=str(d.get("id", "")),
        title=str(d.get("title", "New chat")),
        pinned=bool(d.get("pinned", False)),
        active_skill=str(d.get("active_skill", "Chat & Ideas")),
        mode_locked=bool(d.get("mode_locked", False)),
        turns=turns,
    )


def _decode_turn(d: dict[str, Any]) -> ConversationTurn:
    reasoning = None
    if isinstance(d.get("reasoning"), dict):
        r = d["reasoning"]
        reasoning = ReasoningStep(
            skill=str(r.get("skill", "")),
            duration=str(r.get("duration", "")),
            summary=str(r.get("summary", "")),
            bullets=[str(b) for b in r.get("bullets", [])],
        )

    prose = [
        ProseBlock(
            kind=p.get("kind", "p"),
            text=str(p.get("text", "")),
            items=[str(i) for i in p.get("items", [])],
        )
        for p in d.get("prose", [])
        if isinstance(p, dict)
    ]

    prompt_spec = None
    if isinstance(d.get("prompt_spec"), dict):
        ps = d["prompt_spec"]
        prompt_spec = PromptSpec(
            title=str(ps.get("title", "")),
            tokens=[(str(k), str(v)) for k, v in ps.get("tokens", [])],
            raw_prompt=str(ps.get("raw_prompt", "")),
        )

    research = None
    if isinstance(d.get("research"), dict):
        rs = d["research"]
        tabs = [
            ResearchTab(
                title=str(tb.get("title", "")),
                url=str(tb.get("url", "")),
                takeaway=str(tb.get("takeaway", "")),
            )
            for tb in rs.get("tabs", [])
            if isinstance(tb, dict)
        ]
        refs = [
            ReferenceCandidate(
                id=str(rf.get("id", "")),
                title=str(rf.get("title", "")),
                source_url=str(rf.get("source_url", "")),
                image_url=str(rf.get("image_url", "")),
                alt_text=str(rf.get("alt_text", "")),
                palette_tags=[str(t) for t in rf.get("palette_tags", [])],
                lighting_mood=str(rf.get("lighting_mood", "")),
                style_tokens=str(rf.get("style_tokens", "")),
                explicit=bool(rf.get("explicit", False)),
                width=int(rf.get("width", 0)),
                height=int(rf.get("height", 0)),
                image_bytes_b64="",
            )
            for rf in rs.get("references", [])
            if isinstance(rf, dict)
        ]
        research = ResearchStepData(
            browser_label=str(rs.get("browser_label", "")),
            query=str(rs.get("query", "")),
            tabs=tabs,
            status=str(rs.get("status", "")),
            references=refs,
            trace_summary=str(rs.get("trace_summary", "")),
        )

    handoff = None
    if isinstance(d.get("handoff"), dict):
        hf = d["handoff"]
        handoff = MemoryHandoffData(
            offloaded_model=str(hf.get("offloaded_model", "")),
            active_pipeline=str(hf.get("active_pipeline", "")),
            vram_allocated=str(hf.get("vram_allocated", "")),
            ram_cache=str(hf.get("ram_cache", "")),
            stage_index=int(hf.get("stage_index", 0)),
            stages=[str(s) for s in hf.get("stages", [])],
            progress_fraction=float(hf.get("progress_fraction", 0.0)),
            frame_status=str(hf.get("frame_status", "")),
            eta_label=str(hf.get("eta_label", "")),
        )

    keyframes = [
        KeyframeSpec(
            id=str(kf.get("id", "")),
            code=str(kf.get("code", "")),
            title=str(kf.get("title", "")),
            image_path=kf.get("image_path"),
            shot_type=str(kf.get("shot_type", "")),
            focal_length=str(kf.get("focal_length", "")),
            aperture=str(kf.get("aperture", "")),
            seed=int(kf.get("seed", 0)),
            camera_move=str(kf.get("camera_move", "")),
            notes=str(kf.get("notes", "")),
        )
        for kf in d.get("keyframes", [])
        if isinstance(kf, dict)
    ]

    video_clip = None
    if isinstance(d.get("video_clip"), dict):
        vc = d["video_clip"]
        video_clip = VideoClipSpec(
            id=str(vc.get("id", "")),
            title=str(vc.get("title", "")),
            duration_s=float(vc.get("duration_s", 4.0)),
            fps=int(vc.get("fps", 16)),
            width=int(vc.get("width", 832)),
            height=int(vc.get("height", 480)),
            video_path=vc.get("video_path"),
            camera_move=str(vc.get("camera_move", "")),
            seed=int(vc.get("seed", 0)),
            prompt_summary=str(vc.get("prompt_summary", "")),
        )

    return ConversationTurn(
        id=str(d.get("id", "")),
        role=d.get("role", "user"),
        text=str(d.get("text", "")),
        reasoning=reasoning,
        prose=prose,
        prompt_spec=prompt_spec,
        research=research,
        handoff=handoff,
        keyframes=keyframes,
        keyframe_batch_default=int(d.get("keyframe_batch_default", 4)),
        video_clip=video_clip,
    )
