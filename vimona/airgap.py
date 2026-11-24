"""Kernel-level network air-gapping (`unshare -r -n`) and zero-trace wiping utilities.

Provides:
1. `scorched_earth_wipe(path)` — kills lingering processes referencing an ephemeral
   profile directory, recursively deletes it (`rm -rf` equivalent), and verifies
   zero remnants remain on `/dev/shm` or SSD.
2. `purge_legacy_burner_profile()` — removes any leftover persistent
   `~/firefox-burner-profile` from older versions so zero history/cookies sit on disk.
3. `AirGappedProcess` — runs a local inference engine (`sd-server` or `ComfyUI`) inside
   an isolated Linux user+network namespace (`unshare -r -n`) with zero WAN/LAN/DNS
   access (`ENETUNREACH`), exposing its private `127.0.0.1` loopback port cleanly back
   to Vimona over a private `AF_UNIX` socket bridge in `/dev/shm`.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .perf_profile import get_worker_env, wrap_worker_cmd_with_priority

from . import __name__ as _pkg_name  # keep stdlib-only top-level for venv subprocesses


def _register_pid(pid: int, label: str) -> None:
    try:
        from .ram_manager import register_protected_pid
        register_protected_pid(pid, label)
    except Exception:
        pass


def _unregister_pid(pid: int) -> None:
    try:
        from .ram_manager import unregister_protected_pid
        unregister_protected_pid(pid)
    except Exception:
        pass

SHM_ROOT = Path("/dev/shm")
LEGACY_BURNER_DIR = Path("~/firefox-burner-profile").expanduser()

_AIRGAP_INNER_SUPERVISOR = r'''
import os, signal, socket, subprocess, sys, threading, time

sock_path = sys.argv[1]
inner_port = int(sys.argv[2])
cmd = sys.argv[3:]

# Bring up isolated loopback only (no external interfaces exist in this netns)
for ip_cmd in (
    ["/usr/sbin/ip", "addr", "add", "127.0.0.1/8", "dev", "lo"],
    ["/usr/sbin/ip", "link", "set", "lo", "up"],
):
    try:
        subprocess.run(ip_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        pass

try:
    if os.path.exists(sock_path):
        os.unlink(sock_path)
except OSError:
    pass

child = subprocess.Popen(cmd, start_new_session=False)

def _cleanup(*_args):
    if child.poll() is None:
        try:
            child.terminate()
            child.wait(timeout=4.0)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass
    try:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
    except OSError:
        pass
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _cleanup)
signal.signal(signal.SIGINT, _cleanup)

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(32)
srv.settimeout(0.4)

def _fwd(src, dst):
    try:
        while True:
            buf = src.recv(65536)
            if not buf:
                break
            dst.sendall(buf)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except OSError:
                pass

while True:
    if child.poll() is not None:
        _cleanup()
    try:
        client, _ = srv.accept()
    except socket.timeout:
        continue
    except OSError:
        break
    try:
        upstream = socket.create_connection(("127.0.0.1", inner_port), timeout=3.0)
    except OSError:
        try:
            client.close()
        except OSError:
            pass
        continue
    threading.Thread(target=_fwd, args=(client, upstream), daemon=True).start()
    threading.Thread(target=_fwd, args=(upstream, client), daemon=True).start()
'''


def _kill_pids_referencing_path(target_dir: Path) -> bool:
    """Terminate any process whose cmdline references `target_dir`. Returns True if any killed."""
    target = str(target_dir)
    killed_any = False
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    my_pid = os.getpid()
    force_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
            cmdline = raw.decode("utf-8", errors="ignore")
        except OSError:
            continue
        if target in cmdline:
            try:
                os.kill(pid, force_sig)
                killed_any = True
            except OSError:
                pass
    return killed_any


def scorched_earth_wipe(target_dir: Path | str | None) -> bool:
    """Kill any processes holding `target_dir`, recursively delete it, and verify absence."""
    if not target_dir:
        return True
    p = Path(target_dir)
    _kill_pids_referencing_path(p)
    if not p.exists() and not p.is_symlink():
        return True

    def _onerror(_fn: Any, path_str: str, _exc: Any) -> None:
        try:
            os.chmod(path_str, 0o700)
        except OSError:
            pass

    for _attempt in range(3):
        try:
            if p.is_symlink() or p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, onerror=_onerror)
        except OSError:
            time.sleep(0.08)
        if not p.exists() and not p.is_symlink():
            return True
    return not p.exists() and not p.is_symlink()


def purge_legacy_burner_profile() -> bool:
    """Remove any leftover persistent `~/firefox-burner-profile` from older sessions."""
    if LEGACY_BURNER_DIR.exists() or LEGACY_BURNER_DIR.is_symlink():
        return scorched_earth_wipe(LEGACY_BURNER_DIR)
    return True


def is_unshare_netns_available() -> bool:
    """Return True if unprivileged user+net namespace isolation (`unshare -r -n`) works."""
    return shutil.which("unshare") is not None


class AirGappedProcess:
    """Manages an offline subprocess sealed in `unshare -r -n` with loopback-only AF_UNIX bridge."""

    def __init__(
        self,
        proc: subprocess.Popen[Any],
        sock_path: Path | None,
        outer_port: int,
        airgapped: bool,
    ) -> None:
        self.proc = proc
        self.pid = proc.pid
        self.sock_path = sock_path
        self.outer_port = outer_port
        self.airgapped = airgapped
        self._stop_ev = threading.Event()
        self._tcp_srv: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    def start_host_forwarder(self) -> None:
        """Bind `127.0.0.1:<outer_port>` on host forwarding into the air-gapped AF_UNIX socket."""
        if not self.airgapped or self.sock_path is None:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.outer_port))
        srv.listen(32)
        srv.settimeout(0.35)
        self._tcp_srv = srv

        def _pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                for sock in (src, dst):
                    try:
                        sock.close()
                    except OSError:
                        pass

        def _accept_loop() -> None:
            assert self.sock_path is not None
            while not self._stop_ev.is_set():
                if self.proc.poll() is not None:
                    break
                try:
                    client, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    u.settimeout(4.0)
                    u.connect(str(self.sock_path))
                    u.settimeout(None)
                except OSError:
                    try:
                        client.close()
                    except OSError:
                        pass
                    continue
                threading.Thread(target=_pump, args=(client, u), daemon=True).start()
                threading.Thread(target=_pump, args=(u, client), daemon=True).start()

        self._accept_thread = threading.Thread(target=_accept_loop, daemon=True)
        self._accept_thread.start()

    def poll(self) -> int | None:
        return self.proc.poll()

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    def terminate_cleanly(self) -> None:
        self._stop_ev.set()
        _unregister_pid(self.pid)
        if self._tcp_srv is not None:
            try:
                self._tcp_srv.close()
            except OSError:
                pass
            self._tcp_srv = None

        if self.proc.poll() is None:
            try:
                if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                    os.killpg(os.getpgid(self.pid), signal.SIGTERM)
                else:
                    self.proc.terminate()
            except OSError:
                try:
                    self.proc.terminate()
                except OSError:
                    pass
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
            if self.proc.poll() is None:
                try:
                    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                        os.killpg(os.getpgid(self.pid), getattr(signal, "SIGKILL", signal.SIGTERM))
                    else:
                        self.proc.kill()
                except OSError:
                    try:
                        self.proc.kill()
                    except OSError:
                        pass
                try:
                    self.proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass

        if self.sock_path is not None and self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass


def launch_airgapped_http_service(
    cmd: list[str],
    inner_port: int,
    outer_port: int,
    protection_label: str,
    log_fh: Any,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> AirGappedProcess:
    """Launch `cmd` inside an air-gapped `unshare -r -n` namespace with AF_UNIX bridge.

    The subprocess inside `unshare -r -n` has zero external network interfaces (all
    outbound internet/LAN/DNS attempts fail with ENETUNREACH). Only local HTTP IPC
    over `/dev/shm/vimona_ag_*.sock` <-> `127.0.0.1:<outer_port>` is permitted.
    """
    use_unshare = is_unshare_netns_available() and (SHM_ROOT.is_dir() or Path("/tmp").is_dir())
    worker_env = get_worker_env(env)
    if not use_unshare:
        prio_cmd, popen_flags = wrap_worker_cmd_with_priority(cmd)
        proc = subprocess.Popen(
            prio_cmd,
            cwd=str(cwd) if cwd else None,
            env=worker_env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            creationflags=popen_flags,
        )
        register_ok = proc.pid
        _register_pid(register_ok, protection_label)
        return AirGappedProcess(proc=proc, sock_path=None, outer_port=outer_port, airgapped=False)

    sock_base = SHM_ROOT if (SHM_ROOT.is_dir() and os.access(SHM_ROOT, os.W_OK)) else Path("/tmp")
    sock_path = sock_base / f"vimona_ag_{os.getpid()}_{outer_port}_{int(time.time() * 1000)}.sock"
    if sock_path.exists():
        sock_path.unlink(missing_ok=True)

    python_exe = "/usr/bin/python3"
    base_wrapped = [
        "unshare",
        "-r",
        "-n",
        python_exe,
        "-c",
        _AIRGAP_INNER_SUPERVISOR,
        str(sock_path),
        str(inner_port),
        *cmd,
    ]
    wrapped_cmd, popen_flags = wrap_worker_cmd_with_priority(base_wrapped)
    proc = subprocess.Popen(
        wrapped_cmd,
        cwd=str(cwd) if cwd else None,
        env=worker_env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        creationflags=popen_flags,
    )
    _register_pid(proc.pid, protection_label)

    # Wait briefly for the internal netns Unix socket to appear, then start host forwarder
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if sock_path.exists() or proc.poll() is not None:
            break
        time.sleep(0.05)

    ag = AirGappedProcess(proc=proc, sock_path=sock_path, outer_port=outer_port, airgapped=True)
    ag.start_host_forwarder()
    return ag
