"""Offline local chat history persistence (~/.local/share/vimona/chats.json)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .airgap import is_pid_alive_with_start, purge_legacy_burner_profile, scorched_earth_wipe
from .config import get_config
from .platform_compat import lock_file_exclusive, unlock_file

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

STATE_DIR = get_config().state_dir
CHATS_FILE = STATE_DIR / "chats.json"
EPHEMERAL_PREFIX = "vimona-session-media-"
INSTANCE_UUID = uuid.uuid4().hex[:8]
EPHEMERAL_MEDIA_DIR = Path(tempfile.gettempdir()) / f"{EPHEMERAL_PREFIX}{os.getpid()}_{INSTANCE_UUID}"
_MEDIA_LOCK_FH: Any = None


def _ensure_ephemeral_media_dir_locked() -> Path:
    """Create this process's private `0700` session media directory and acquire its flock."""
    global _MEDIA_LOCK_FH
    EPHEMERAL_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(EPHEMERAL_MEDIA_DIR, 0o700)
    except OSError:
        pass
    if _MEDIA_LOCK_FH is None:
        lock_path = EPHEMERAL_MEDIA_DIR / ".vimona_media.lock"
        fh = open(lock_path, "a+b")
        try:
            lock_file_exclusive(fh, nonblocking=True)
        except OSError:
            pass
        _MEDIA_LOCK_FH = fh
    owner_file = EPHEMERAL_MEDIA_DIR / ".vimona_owner.json"
    if not owner_file.exists():
        try:
            from .airgap import _read_proc_start_ticks

            owner_file.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "uid": os.getuid() if hasattr(os, "getuid") else 0,
                        "start_ticks": _read_proc_start_ticks(os.getpid()),
                    }
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
    return EPHEMERAL_MEDIA_DIR


def _can_safely_reap_media_dir(entry: Path) -> bool:
    """Return True ONLY if owned by current UID and no live session holds its lock or PID (`B2`, `B12`)."""
    my_uid = os.getuid() if hasattr(os, "getuid") else 0
    try:
        st = entry.lstat()
        if st.st_uid != my_uid or entry.is_symlink():
            return False
    except OSError:
        return False

    # Check flock first: an active Vimona instance holds LOCK_EX on .vimona_media.lock
    lock_path = entry / ".vimona_media.lock"
    if lock_path.exists():
        try:
            with open(lock_path, "r+b") as fh:
                try:
                    lock_file_exclusive(fh, nonblocking=True)
                    unlock_file(fh)
                except BlockingIOError:
                    return False  # Live concurrent session owns this directory!
        except OSError:
            return False

    # Secondary check: verify owner PID + start_ticks if owner metadata exists
    owner_file = entry / ".vimona_owner.json"
    if owner_file.is_file():
        try:
            meta = json.loads(owner_file.read_text(encoding="utf-8"))
            pid = int(meta.get("pid") or 0)
            st_ticks = meta.get("start_ticks")
            if pid > 1 and pid != os.getpid() and is_pid_alive_with_start(pid, st_ticks):
                return False
        except Exception:
            pass

    return True


def _release_current_media_lock() -> None:
    global _MEDIA_LOCK_FH
    if _MEDIA_LOCK_FH is not None:
        try:
            unlock_file(_MEDIA_LOCK_FH)
        except OSError:
            pass
        try:
            _MEDIA_LOCK_FH.close()
        except OSError:
            pass
        _MEDIA_LOCK_FH = None


def cleanup_ephemeral_media(include_current: bool = True) -> dict[str, Any]:
    """Safely delete inactive/owned volatile session media without colliding with active instances (`B2`–`B12`)."""
    errors: list[str] = []
    removed: list[str] = []
    skipped_active: list[str] = []
    my_uid = os.getuid() if hasattr(os, "getuid") else 0

    tmp_root = Path(tempfile.gettempdir())
    for entry in tmp_root.glob(f"{EPHEMERAL_PREFIX}*"):
        if not entry.is_dir():
            continue
        if entry == EPHEMERAL_MEDIA_DIR:
            if not include_current:
                continue
            _release_current_media_lock()
            if scorched_earth_wipe(entry):
                removed.append(str(entry))
            else:
                errors.append(f"Failed to remove current media dir: {entry}")
            continue

        if not _can_safely_reap_media_dir(entry):
            skipped_active.append(str(entry))
            continue
        if scorched_earth_wipe(entry):
            removed.append(str(entry))
        else:
            errors.append(f"Failed to remove stale media dir: {entry}")

    # Sweep orphaned /dev/shm research dirs ONLY when their .worker.lock is unlocked (`B3`, `B4`)
    shm_root = Path("/dev/shm")
    if shm_root.is_dir():
        for entry in shm_root.glob("vimona_ephem_*"):
            try:
                if entry.lstat().st_uid != my_uid or entry.is_symlink() or not entry.is_dir():
                    continue
                wlock = entry / ".worker.lock"
                if wlock.exists():
                    with open(wlock, "r+b") as fh:
                        try:
                            lock_file_exclusive(fh, nonblocking=True)
                            unlock_file(fh)
                        except BlockingIOError:
                            skipped_active.append(str(entry))
                            continue
                if scorched_earth_wipe(entry):
                    removed.append(str(entry))
                else:
                    errors.append(f"Failed to remove stale shm dir: {entry}")
            except OSError as exc:
                errors.append(f"{entry}: {exc}")

    if not purge_legacy_burner_profile():
        errors.append("Failed to remove legacy ~/firefox-burner-profile")

    # Only remove ComfyUI staging files belonging to our PID (or orphaned pid-tagged files whose PID died)
    for subdir in (Path("~/comfyui/output").expanduser(), Path("~/comfyui/input").expanduser()):
        if not subdir.is_dir():
            continue
        for item in subdir.glob("vimona_*"):
            try:
                if item.lstat().st_uid != my_uid:
                    continue
                name = item.name
                # Preserve files belonging to another active Vimona PID (`pid<N>_`)
                if "_pid" in name:
                    try:
                        after_pid = name.split("_pid", 1)[1].split("_", 1)[0]
                        owner_pid = int(after_pid)
                        if owner_pid != os.getpid() and Path(f"/proc/{owner_pid}").is_dir():
                            skipped_active.append(str(item))
                            continue
                    except Exception:
                        pass
                elif not include_current and any(
                    d.name.startswith(f"{EPHEMERAL_PREFIX}{os.getpid()}_") for d in [EPHEMERAL_MEDIA_DIR]
                ):
                    pass
                item.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{item}: {exc}")

    return {"ok": len(errors) == 0, "removed": removed, "skipped_active": skipped_active, "errors": errors}


def stage_ephemeral_media(src: Path) -> Path:
    """Move a newly generated file out of ComfyUI output into volatile session scratch."""
    media_dir = _ensure_ephemeral_media_dir_locked()
    dest = media_dir / f"{uuid.uuid4().hex[:8]}_{src.name}"
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


_CHATS_IO_LOCK = threading.RLock()
CHATS_SCHEMA_VERSION = 2
_LAST_STORAGE_STATUS: dict[str, Any] = {"state": "ok", "detail": ""}


def get_last_storage_status() -> dict[str, Any]:
    return dict(_LAST_STORAGE_STATUS)


def _is_vimona_owned_media_path(p: Path) -> bool:
    """Ensure media deletion NEVER escapes Vimona scratch directories (`I8`, `I9`)."""
    try:
        resolved = p.resolve()
    except OSError:
        resolved = p
    try:
        cur_ephem = EPHEMERAL_MEDIA_DIR.resolve()
        if cur_ephem != Path("/").resolve() and resolved.is_relative_to(cur_ephem) and resolved != cur_ephem:
            return True
    except OSError:
        pass
    allowed_roots = [
        Path(tempfile.gettempdir()).resolve(),
        Path("/dev/shm").resolve(),
        Path("~/comfyui/output").expanduser().resolve(),
        Path("~/comfyui/input").expanduser().resolve(),
    ]
    for root in allowed_roots:
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        top = rel.parts[0] if rel.parts else ""
        if root == Path(tempfile.gettempdir()).resolve() and top.startswith(EPHEMERAL_PREFIX):
            return True
        if root == Path("/dev/shm").resolve() and top.startswith("vimona_ephem_"):
            return True
        if top.startswith("vimona_"):
            return True
    return False


def delete_ephemeral_file(path_str: str | None) -> bool:
    """Immediately delete a verified Vimona-owned temporary media file (`I8`, `I9`)."""
    if not path_str:
        return False
    p = Path(path_str)
    if not _is_vimona_owned_media_path(p):
        return False
    try:
        p.unlink(missing_ok=True)
        return not p.exists()
    except OSError:
        return False


def apply_storage_retention_policy(
    max_corrupt_files: int = 5,
    max_age_days: int = 30,
) -> dict[str, Any]:
    """Prune stale quarantined corrupt chat snapshots beyond explicit retention bounds (`I10`)."""
    if not STATE_DIR.is_dir():
        return {"removed": 0}
    quarantined = sorted(
        STATE_DIR.glob("chats.corrupt.*.json"),
        key=lambda f: f.stat().st_mtime if f.exists() else 0.0,
        reverse=True,
    )
    removed = 0
    cutoff = time.time() - max(1, max_age_days) * 86400.0
    for idx, f in enumerate(quarantined):
        try:
            if idx >= max_corrupt_files or f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return {"removed": removed, "retained": max(0, len(quarantined) - removed)}


def _ensure_private_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass


def save_chats(chats: list[ChatSession]) -> None:
    """Atomically persist private chat history with flock, fsync, 0600 perms, & backup (`I1`–`I7`)."""
    with _CHATS_IO_LOCK:
        _ensure_private_state_dir()
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

        envelope = {
            "schema_version": CHATS_SCHEMA_VERSION,
            "saved_at": time.time(),
            "chats": payload,
        }
        serialized = json.dumps(envelope, indent=2, ensure_ascii=False)

        lock_path = STATE_DIR / ".chats.lock"
        with open(lock_path, "a+b") as lfh:
            lock_file_exclusive(lfh, nonblocking=False)
            try:
                bak_file = STATE_DIR / "chats.json.bak"
                if CHATS_FILE.is_file() and CHATS_FILE.stat().st_size > 2:
                    try:
                        # Only back up if current file parses cleanly
                        json.loads(CHATS_FILE.read_text(encoding="utf-8"))
                        shutil.copy2(str(CHATS_FILE), str(bak_file))
                        os.chmod(bak_file, 0o600)
                    except Exception:
                        pass

                fd, tmp_name = tempfile.mkstemp(prefix=".chats.", suffix=".tmp", dir=str(STATE_DIR))
                tmp_path = Path(tmp_name)
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(serialized)
                        fh.flush()
                        os.fsync(fh.fileno())
                    tmp_path.replace(CHATS_FILE)
                    try:
                        os.chmod(CHATS_FILE, 0o600)
                    except OSError:
                        pass
                    try:
                        dfd = os.open(str(STATE_DIR), os.O_RDONLY)
                        try:
                            os.fsync(dfd)
                        finally:
                            os.close(dfd)
                    except OSError:
                        pass
                except Exception:
                    tmp_path.unlink(missing_ok=True)
                    raise
                _LAST_STORAGE_STATUS.update({"state": "ok", "detail": f"Saved v{CHATS_SCHEMA_VERSION}"})
            finally:
                unlock_file(lfh)


def _decode_chats_payload(raw_obj: Any) -> tuple[list[ChatSession], int]:
    """Validate versioned schema & migrate v1 bare lists to v2 cleanly (`I1`, `I2`)."""
    if isinstance(raw_obj, list):
        # Legacy v1 bare array format
        return ([_decode_chat(item) for item in raw_obj if isinstance(item, dict)], 1)
    if isinstance(raw_obj, dict):
        ver = int(raw_obj.get("schema_version") or 1)
        if ver > CHATS_SCHEMA_VERSION:
            raise ValueError(f"Unsupported future chats schema_version={ver}")
        chats_list = raw_obj.get("chats")
        if not isinstance(chats_list, list):
            raise ValueError("Missing 'chats' list in versioned schema")
        return ([_decode_chat(item) for item in chats_list if isinstance(item, dict)], ver)
    raise ValueError("Invalid chats root JSON type")


def load_chats() -> list[ChatSession]:
    """Load saved chats safely with corruption quarantine & `.bak` recovery (`I1`–`I4`)."""
    with _CHATS_IO_LOCK:
        if not CHATS_FILE.is_file():
            _LAST_STORAGE_STATUS.update({"state": "ok", "detail": "No saved chats yet"})
            return []
        try:
            raw_obj = json.loads(CHATS_FILE.read_text(encoding="utf-8"))
            chats, ver = _decode_chats_payload(raw_obj)
            _LAST_STORAGE_STATUS.update({"state": "ok", "detail": f"Loaded v{ver}"})
            return chats
        except Exception as exc:
            # Quarantine corrupt chats.json instead of silently losing user history (`I3`, `I4`)
            _ensure_private_state_dir()
            quarantine = STATE_DIR / f"chats.corrupt.{int(time.time())}.json"
            try:
                shutil.copy2(str(CHATS_FILE), str(quarantine))
                os.chmod(quarantine, 0o600)
            except OSError:
                pass
            bak_file = STATE_DIR / "chats.json.bak"
            if bak_file.is_file():
                try:
                    bak_obj = json.loads(bak_file.read_text(encoding="utf-8"))
                    chats, _ver = _decode_chats_payload(bak_obj)
                    _LAST_STORAGE_STATUS.update(
                        {
                            "state": "recovered_from_backup",
                            "detail": f"Corrupt chats quarantined to {quarantine.name}; recovered from backup ({exc})",
                        }
                    )
                    apply_storage_retention_policy()
                    return chats
                except Exception:
                    pass
            _LAST_STORAGE_STATUS.update(
                {
                    "state": "corrupt_quarantined",
                    "detail": f"Corrupt chats quarantined to {quarantine.name}: {exc}",
                }
            )
            apply_storage_retention_policy()
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
