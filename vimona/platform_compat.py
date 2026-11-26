"""Cross-platform OS capability & file-locking helpers (`Windows`/POSIX import-safe)."""

from __future__ import annotations

import os
import sys
from typing import Any

IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform == "win32"

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # type: ignore[assignment]

try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None  # type: ignore[assignment]

try:
    import resource as _resource
except ImportError:
    _resource = None  # type: ignore[assignment]


def get_current_uid() -> int:
    """Return current POSIX UID or `0` on Windows."""
    if hasattr(os, "getuid"):
        return int(os.getuid())
    return 0


def disable_core_dumps() -> bool:
    """Disable core dumps via `RLIMIT_CORE=0` on POSIX platforms."""
    if _resource is None:
        return False
    try:
        _resource.setrlimit(_resource.RLIMIT_CORE, (0, 0))
        soft_c, hard_c = _resource.getrlimit(_resource.RLIMIT_CORE)
        return soft_c == 0 and hard_c == 0
    except Exception:
        return False


def lock_file_exclusive(fh: Any, *, nonblocking: bool = True) -> None:
    """Acquire an exclusive byte/file lock cross-platform; raises `BlockingIOError` on contention."""
    fd = fh.fileno()
    if _fcntl is not None:
        flags = _fcntl.LOCK_EX | (_fcntl.LOCK_NB if nonblocking else 0)
        _fcntl.flock(fd, flags)
        return
    if _msvcrt is not None:
        mode = _msvcrt.LK_NBLCK if nonblocking else _msvcrt.LK_LOCK
        pos = fh.tell()
        try:
            fh.seek(0)
            _msvcrt.locking(fd, mode, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc
        finally:
            try:
                fh.seek(pos)
            except OSError:
                pass
        return
    raise OSError("No file locking backend available on this platform")


def unlock_file(fh: Any) -> None:
    """Release an exclusive file lock cross-platform."""
    if fh is None:
        return
    fd = fh.fileno()
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        pos = fh.tell()
        try:
            fh.seek(0)
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                fh.seek(pos)
            except OSError:
                pass
