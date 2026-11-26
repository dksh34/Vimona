"""Kernel-level network & mount namespace isolation (`unshare -r -n -m`) and privacy cleanup utilities.

Provides:
1. `IsolationStatus` / `AirGapIsolationError` — explicit state machine
   (`unavailable` | `starting` | `isolated` | `failed` | `stopped`) separating
   network/mount/resource restrictions from full sandbox claims (`A7`, `A12`).
2. `probe_unshare_netns_capability()` & `is_unshare_netns_available()` — verifies real
   user+network namespace creation and external egress failure (`A1`).
3. `detect_ram_persistence_risk()` — audits filesystem backing (`/proc/mounts` `tmpfs`)
   plus active OS swap/hibernation (`/proc/swaps`, `/sys/power/state`) (`B1`, `B9`, `B10`).
4. `scorched_earth_wipe(path)` — verifies UID + process ownership / descendant tree,
   waits for child termination before deletion, recursively removes the directory,
   and reports real failure if remnants exist (`B4`, `B5`, `B6`, `B7`, `B11`).
5. `AirGappedProcess` & `launch_airgapped_http_service()` — fail-closed supervisor in
   `unshare -r -n [-m]` with handshake verification, network egress tests, private
   `0700` socket directory, `X-Vimona-Bridge-Token` host-forwarder authentication,
   and reliable descendant tree cleanup (`A2`–`A12`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
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

from .errors import IsolationError
from .perf_profile import get_worker_env, wrap_worker_cmd_with_priority
from .platform_compat import get_current_uid, lock_file_exclusive, unlock_file


class AirGapIsolationError(IsolationError, RuntimeError):
    """Raised when fail-closed network/namespace isolation cannot be established or verified."""


ISOLATION_STATES = ("unavailable", "starting", "isolated", "partial", "failed", "stopped")


@dataclass(frozen=True)
class IsolationStatus:
    """Verified isolation runtime status and actual boundary guarantees."""

    state: str = "stopped"  # "unavailable" | "starting" | "isolated" | "partial" | "failed" | "stopped"
    level: str = "unisolated"  # "unisolated" | "user_net_ns_partial" | "user_net_ns" | "user_net_mount_ns_rlimit"
    detail: str = "All media engines stopped"
    verified_interfaces: tuple[str, ...] = ()
    ipv4_blocked: bool = False
    ipv6_blocked: bool = False
    dns_blocked: bool = False
    mount_isolated: bool = False
    no_new_privs: bool = False
    core_dumps_disabled: bool = False
    socket_dir: str = ""
    error: str = ""


_STATUS_LOCK = threading.Lock()
_CURRENT_ISOLATION_STATUS = IsolationStatus()
_STATUS_LISTENERS: list[Callable[[IsolationStatus], None]] = []
_NETNS_PROBE_LOCK = threading.Lock()
_NETNS_PROBE_CACHE: tuple[float, bool, str] | None = None
_LAST_WIPE_LOCK = threading.Lock()
_LAST_WIPE_DIAGNOSTICS: dict[str, Any] = {
    "ok": True,
    "target_dir": "",
    "surviving_pids": [],
    "dir_exists": False,
    "error": "",
}


def get_last_wipe_diagnostics() -> dict[str, Any]:
    """Return structured diagnostics from the most recent `scorched_earth_wipe()` run (`B7`)."""
    with _LAST_WIPE_LOCK:
        return dict(_LAST_WIPE_DIAGNOSTICS)


def _set_last_wipe_diagnostics(
    *,
    ok: bool,
    target_dir: str,
    surviving_pids: list[int],
    dir_exists: bool,
    error: str = "",
) -> None:
    with _LAST_WIPE_LOCK:
        _LAST_WIPE_DIAGNOSTICS.update(
            {
                "ok": bool(ok),
                "target_dir": target_dir,
                "surviving_pids": list(surviving_pids),
                "dir_exists": bool(dir_exists),
                "error": error,
            }
        )


def get_isolation_status() -> IsolationStatus:
    with _STATUS_LOCK:
        return _CURRENT_ISOLATION_STATUS


def register_isolation_listener(cb: Callable[[IsolationStatus], None]) -> None:
    with _STATUS_LOCK:
        if cb not in _STATUS_LISTENERS:
            _STATUS_LISTENERS.append(cb)


def _publish_isolation_status(status: IsolationStatus) -> None:
    with _STATUS_LOCK:
        global _CURRENT_ISOLATION_STATUS
        _CURRENT_ISOLATION_STATUS = status
        listeners = list(_STATUS_LISTENERS)
    for cb in listeners:
        try:
            cb(status)
        except Exception:
            pass


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
import ctypes, json, os, resource, signal, socket, struct, subprocess, sys, threading, time

sock_path = sys.argv[1]
inner_port = int(sys.argv[2])
ready_fd = int(sys.argv[3])
want_mount_tmp = sys.argv[4] == "1"
require_mount_tmp = sys.argv[5] == "1"
require_nnp = sys.argv[6] == "1"
require_core = sys.argv[7] == "1"
host_uid = int(sys.argv[8])
cmd = sys.argv[9:]

def _send_ready(payload: dict) -> None:
    try:
        os.write(ready_fd, (json.dumps(payload) + "\n").encode("utf-8"))
    except OSError:
        pass
    try:
        os.close(ready_fd)
    except OSError:
        pass

def _strip_sensitive_env() -> dict[str, str]:
    clean = dict(os.environ)
    for k in list(clean.keys()):
        ku = k.upper()
        if (
            ku.startswith("AWS_")
            or ku.endswith("_TOKEN")
            or ku.endswith("_API_KEY")
            or ku.endswith("_SECRET")
            or ku in (
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy",
                "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_ASKPASS",
            )
        ):
            clean.pop(k, None)
    clean["COMFYUI_DISABLE_FETCH"] = "1"
    return clean

core_disabled = False
try:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    soft_c, hard_c = resource.getrlimit(resource.RLIMIT_CORE)
    core_disabled = (soft_c == 0 and hard_c == 0)
except Exception:
    core_disabled = False

mount_isolated = False
if want_mount_tmp:
    try:
        r = subprocess.run(
            ["/usr/bin/mount", "-t", "tmpfs", "-o", "size=512M,mode=1777", "tmpfs", "/tmp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if r.returncode == 0:
            mounts_txt = open("/proc/mounts", "r", encoding="utf-8", errors="ignore").read()
            mount_isolated = any(
                len(ln.split()) >= 3 and ln.split()[1] == "/tmp" and ln.split()[2] == "tmpfs"
                for ln in reversed(mounts_txt.splitlines())
            )
    except OSError:
        mount_isolated = False

# Apply PR_SET_NO_NEW_PRIVS irreversibly on supervisor BEFORE spawning workload so child inherits it
no_new_privs_ok = False
try:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    # PR_SET_NO_NEW_PRIVS = 38, PR_GET_NO_NEW_PRIVS = 39
    if libc.prctl(38, 1, 0, 0, 0) == 0:
        no_new_privs_ok = (libc.prctl(39, 0, 0, 0, 0) == 1)
except Exception:
    no_new_privs_ok = False

if require_core and not core_disabled:
    _send_ready({
        "ready": False,
        "error": "Mandatory control failed: RLIMIT_CORE could not be set to 0",
        "mount_isolated": mount_isolated,
        "no_new_privs": no_new_privs_ok,
        "core_dumps_disabled": core_disabled,
    })
    raise SystemExit(13)

if require_mount_tmp and not mount_isolated:
    _send_ready({
        "ready": False,
        "error": "Mandatory control failed: private /tmp tmpfs mount unavailable",
        "mount_isolated": mount_isolated,
        "no_new_privs": no_new_privs_ok,
        "core_dumps_disabled": core_disabled,
    })
    raise SystemExit(14)

if require_nnp and not no_new_privs_ok:
    _send_ready({
        "ready": False,
        "error": "Mandatory control failed: PR_SET_NO_NEW_PRIVS enforcement failed",
        "mount_isolated": mount_isolated,
        "no_new_privs": no_new_privs_ok,
        "core_dumps_disabled": core_disabled,
    })
    raise SystemExit(15)

# Configure loopback inside isolated netns
for ip_cmd in (
    ["/usr/sbin/ip", "addr", "add", "127.0.0.1/8", "dev", "lo"],
    ["/usr/sbin/ip", "link", "set", "lo", "up"],
):
    try:
        subprocess.run(ip_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        pass

# Verify actual network namespace isolation via netlink (`socket.if_nameindex()`) + egress checks
net_ifaces = []
try:
    net_ifaces = sorted(name for _idx, name in socket.if_nameindex())
except Exception:
    try:
        ip_out = subprocess.check_output(["/usr/sbin/ip", "-o", "link", "show"], text=True, stderr=subprocess.DEVNULL)
        for ln in ip_out.splitlines():
            parts = ln.split(":", 2)
            if len(parts) >= 2:
                net_ifaces.append(parts[1].strip().split("@", 1)[0])
        net_ifaces = sorted(net_ifaces)
    except Exception:
        net_ifaces = []

def _is_egress_blocked(family: int, sock_type: int, addr: tuple) -> bool:
    s = None
    try:
        s = socket.socket(family, sock_type)
        s.settimeout(0.8)
        s.connect(addr)
        return False
    except OSError:
        return True
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

ipv4_blocked = _is_egress_blocked(socket.AF_INET, socket.SOCK_STREAM, ("1.1.1.1", 443))
ipv6_blocked = _is_egress_blocked(socket.AF_INET6, socket.SOCK_STREAM, ("2606:4700:4700::1111", 443))
dns_blocked = _is_egress_blocked(socket.AF_INET, socket.SOCK_DGRAM, ("8.8.8.8", 53))
extra_ifaces = [n for n in net_ifaces if n != "lo"]

if not (ipv4_blocked and ipv6_blocked and dns_blocked) or extra_ifaces:
    _send_ready({
        "ready": False,
        "error": f"Network verification failed (ifaces={net_ifaces}, ipv4_blk={ipv4_blocked}, ipv6_blk={ipv6_blocked}, dns_blk={dns_blocked})",
        "interfaces": net_ifaces,
        "ipv4_blocked": ipv4_blocked,
        "ipv6_blocked": ipv6_blocked,
        "dns_blocked": dns_blocked,
        "mount_isolated": mount_isolated,
        "no_new_privs": no_new_privs_ok,
        "core_dumps_disabled": core_disabled,
    })
    raise SystemExit(12)

try:
    if os.path.exists(sock_path):
        os.unlink(sock_path)
except OSError:
    pass

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
try:
    os.chmod(sock_path, 0o600)
except OSError as exc:
    try:
        srv.close()
        os.unlink(sock_path)
    except OSError:
        pass
    _send_ready({
        "ready": False,
        "error": f"Failed to restrict AF_UNIX socket permissions: {exc}",
        "interfaces": net_ifaces,
        "ipv4_blocked": ipv4_blocked,
        "ipv6_blocked": ipv6_blocked,
        "dns_blocked": dns_blocked,
        "mount_isolated": mount_isolated,
        "no_new_privs": no_new_privs_ok,
        "core_dumps_disabled": core_disabled,
    })
    raise SystemExit(16)
srv.listen(32)
srv.settimeout(0.4)

def _child_preexec():
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass
    try:
        libc_c = ctypes.CDLL("libc.so.6", use_errno=True)
        libc_c.prctl(38, 1, 0, 0, 0)
        libc_c.prctl(1, signal.SIGTERM, 0, 0, 0)
    except Exception:
        pass

clean_env = _strip_sensitive_env()
child = subprocess.Popen(cmd, env=clean_env, start_new_session=True, preexec_fn=_child_preexec)
time.sleep(0.05)
if child.poll() is not None:
    try:
        srv.close()
        os.unlink(sock_path)
    except OSError:
        pass
    _send_ready({
        "ready": False,
        "error": f"Workload exited immediately with code {child.returncode}",
        "interfaces": net_ifaces,
        "ipv4_blocked": ipv4_blocked,
        "ipv6_blocked": ipv6_blocked,
        "dns_blocked": dns_blocked,
        "mount_isolated": mount_isolated,
        "no_new_privs": no_new_privs_ok,
        "core_dumps_disabled": core_disabled,
    })
    raise SystemExit(child.returncode or 1)

_send_ready({
    "ready": True,
    "child_pid": child.pid,
    "interfaces": net_ifaces or ["lo"],
    "ipv4_blocked": ipv4_blocked,
    "ipv6_blocked": ipv6_blocked,
    "dns_blocked": dns_blocked,
    "mount_isolated": mount_isolated,
    "no_new_privs": no_new_privs_ok,
    "core_dumps_disabled": core_disabled,
})

active_lock = threading.Lock()
active_sockets: set[socket.socket] = set()
fwd_threads: list[threading.Thread] = []

def _collect_tree(root_pid: int, root_start_ticks: int | None = None) -> list[tuple[int, int]]:
    def _stat_info(p: int) -> tuple[int, int] | None:
        try:
            st_txt = open(f"/proc/{p}/stat", "r", encoding="utf-8", errors="ignore").read()
            rp = st_txt.rfind(")")
            if rp == -1:
                return None
            parts = st_txt[rp + 2 :].split()
            return (int(parts[1]), int(parts[19]))
        except Exception:
            return None

    r_info = _stat_info(root_pid)
    if r_info is None:
        return []
    if root_start_ticks is not None and r_info[1] != root_start_ticks:
        return []
    out = [(root_pid, r_info[1])]
    seen = {root_pid}
    children_map: dict[int, list[tuple[int, int]]] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            info = _stat_info(pid)
            if info is not None:
                children_map.setdefault(info[0], []).append((pid, info[1]))
        stack = [root_pid]
        while stack:
            cur = stack.pop()
            for ch, ch_st in children_map.get(cur, []):
                if ch not in seen:
                    seen.add(ch)
                    out.append((ch, ch_st))
                    stack.append(ch)
    except Exception:
        pass
    return out

def _kill_child_tree() -> None:
    root_info = _collect_tree(child.pid)
    root_st = root_info[0][1] if root_info else None
    tracked = {p: st for p, st in root_info}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p, st in _collect_tree(child.pid, root_st):
            tracked.setdefault(p, st)
        for p, expected_st in sorted(tracked.items(), reverse=True):
            cur = [x for x in _collect_tree(p, expected_st) if x[0] == p]
            if not cur:
                continue
            try:
                if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                    pfd = os.pidfd_open(p, 0)
                    try:
                        if _collect_tree(p, expected_st):
                            signal.pidfd_send_signal(pfd, sig, None, 0)
                    finally:
                        os.close(pfd)
                else:
                    os.kill(p, sig)
            except OSError:
                pass
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if child.poll() is not None and not _collect_tree(child.pid, root_st):
                    break
                time.sleep(0.05)

def _cleanup(*_args):
    _kill_child_tree()
    try:
        child.wait(timeout=2.0)
    except Exception:
        pass
    with active_lock:
        for s in list(active_sockets):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        active_sockets.clear()
    for t in fwd_threads:
        if t.is_alive():
            t.join(timeout=0.5)
    try:
        srv.close()
    except OSError:
        pass
    try:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
    except OSError:
        pass
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _cleanup)
signal.signal(signal.SIGINT, _cleanup)

SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)

def _fwd(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            buf = src.recv(65536)
            if not buf:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                break
            dst.sendall(buf)
    except OSError:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    finally:
        with active_lock:
            active_sockets.discard(src)
            active_sockets.discard(dst)
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
    # Verify AF_UNIX peer UID strictly (fail closed if peer credentials cannot be verified)
    try:
        creds = client.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, struct.calcsize("3i"))
        _pid, peer_uid, _gid = struct.unpack("3i", creds)
        if peer_uid not in (0, host_uid):
            client.close()
            continue
    except Exception:
        try:
            client.close()
        except OSError:
            pass
        continue
    try:
        upstream = socket.create_connection(("127.0.0.1", inner_port), timeout=3.0)
        upstream.settimeout(None)
    except OSError:
        try:
            client.close()
        except OSError:
            pass
        continue
    with active_lock:
        active_sockets.add(client)
        active_sockets.add(upstream)
    t1 = threading.Thread(target=_fwd, args=(client, upstream), daemon=True)
    t2 = threading.Thread(target=_fwd, args=(upstream, client), daemon=True)
    fwd_threads[:] = [t for t in fwd_threads if t.is_alive()] + [t1, t2]
    t1.start()
    t2.start()
'''


def _read_proc_uid(pid: int) -> int | None:
    try:
        st = Path(f"/proc/{pid}").stat()
        return int(st.st_uid)
    except OSError:
        return None


def _read_proc_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="ignore")
        rparen = raw.rfind(")")
        if rparen == -1:
            return None
        fields = raw[rparen + 2 :].split()
        # starttime is field index 19 after state (index 21 overall)
        return int(fields[19]) if len(fields) > 19 else None
    except Exception:
        return None


def _read_proc_identity(pid: int) -> tuple[int, int, str] | None:
    """Return `(uid, start_ticks, state_char)` for `pid`, or None if inaccessible/gone."""
    try:
        entry = Path(f"/proc/{pid}")
        st = entry.stat()
        raw = (entry / "stat").read_text(encoding="utf-8", errors="ignore")
        rparen = raw.rfind(")")
        if rparen == -1:
            return None
        fields = raw[rparen + 2 :].split()
        if len(fields) <= 19:
            return None
        return (int(st.st_uid), int(fields[19]), str(fields[0]))
    except Exception:
        return None


def is_pid_alive_with_start(pid: int, expected_start_ticks: int | None = None) -> bool:
    """Return True if `pid` exists and (when provided) matches `expected_start_ticks`."""
    if pid <= 1 or not Path(f"/proc/{pid}").is_dir():
        return False
    if expected_start_ticks is not None:
        cur_ticks = _read_proc_start_ticks(pid)
        if cur_ticks is None or cur_ticks != expected_start_ticks:
            return False
    return True


def collect_descendant_identities(
    root_pid: int,
    root_start_ticks: int | None = None,
) -> dict[int, tuple[int, int]]:
    """Return `{pid: (uid, start_ticks)}` for verified `root_pid` and all descendants."""
    proc_root = Path("/proc")
    if not proc_root.is_dir() or root_pid <= 1:
        return {}
    root_ident = _read_proc_identity(root_pid)
    if root_ident is None:
        return {}
    if root_start_ticks is not None and root_ident[1] != root_start_ticks:
        return {}

    children_by_ppid: dict[int, list[tuple[int, int, int]]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            st = entry.stat()
            raw = (entry / "stat").read_text(encoding="utf-8", errors="ignore")
            rparen = raw.rfind(")")
            if rparen == -1:
                continue
            fields = raw[rparen + 2 :].split()
            if len(fields) <= 19:
                continue
            ppid = int(fields[1])
            start_ticks = int(fields[19])
            children_by_ppid.setdefault(ppid, []).append((pid, int(st.st_uid), start_ticks))
        except Exception:
            continue

    out: dict[int, tuple[int, int]] = {root_pid: (root_ident[0], root_ident[1])}
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        for ch, uid, st_ticks in children_by_ppid.get(cur, []):
            if ch not in out:
                out[ch] = (uid, st_ticks)
                stack.append(ch)
    return out


def collect_descendant_pids(root_pid: int) -> list[int]:
    """Return `root_pid` and all currently alive descendant PIDs ordered parent->child."""
    if not Path("/proc").is_dir():
        return [root_pid] if root_pid > 0 else []
    return list(collect_descendant_identities(root_pid).keys())


def _send_signal_verified(
    pid: int,
    sig: int,
    *,
    expected_uid: int,
    expected_start_ticks: int | None,
) -> tuple[bool, str]:
    """Verify UID & start_ticks immediately before signaling (via pidfd when available)."""
    if expected_start_ticks is None:
        return (False, f"Cannot verify start-time identity for PID {pid}; refusing signal.")
    ident = _read_proc_identity(pid)
    if ident is None:
        return (False, f"Process {pid} already exited.")
    cur_uid, cur_ticks, state_ch = ident
    if cur_uid != expected_uid:
        return (False, f"UID mismatch on PID {pid}; refusing signal.")
    if cur_ticks != expected_start_ticks:
        return (False, f"PID {pid} was reused; aborted safely.")
    if state_ch in ("Z", "X", "x"):
        return (False, f"Process {pid} already zombie/dead.")

    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        pfd = -1
        try:
            pfd = os.pidfd_open(pid, 0)
            re_ident = _read_proc_identity(pid)
            if re_ident is None or re_ident[0] != expected_uid or re_ident[1] != expected_start_ticks:
                return (False, f"PID {pid} identity changed before signal; aborted safely.")
            signal.pidfd_send_signal(pfd, sig, None, 0)
            return (True, f"Signaled PID {pid} via pidfd")
        except ProcessLookupError:
            return (False, f"Process {pid} already exited.")
        except PermissionError:
            return (False, f"Permission denied signaling PID {pid}.")
        except OSError:
            pass
        finally:
            if pfd >= 0:
                try:
                    os.close(pfd)
                except OSError:
                    pass

    re_ident2 = _read_proc_identity(pid)
    if re_ident2 is None or re_ident2[0] != expected_uid or re_ident2[1] != expected_start_ticks:
        return (False, f"PID {pid} identity changed; aborted safely.")
    try:
        os.kill(pid, sig)
        return (True, f"Signaled PID {pid}")
    except OSError as exc:
        return (False, str(exc))


def kill_process_tree_verified(
    root_pid: int,
    *,
    root_start_ticks: int | None = None,
    extra_pids: list[int] | None = None,
    extra_pid_identities: dict[int, int] | None = None,
    term_timeout_s: float = 4.0,
    kill_timeout_s: float = 2.0,
) -> bool:
    """Terminate an owned process tree with strict PID+start_ticks revalidation (`A10`, `B5`, `K1`–`K3`)."""
    my_uid = os.getuid() if hasattr(os, "getuid") else 0
    my_pid = os.getpid()
    if root_pid <= 1 or root_pid == my_pid:
        return True

    # Capture root & initial descendant identities once upfront
    root_ident = _read_proc_identity(root_pid)
    if root_start_ticks is None and root_ident is not None and root_ident[0] == my_uid:
        root_start_ticks = root_ident[1]

    tracked: dict[int, int] = {}
    if root_ident is not None and root_ident[0] == my_uid and (
        root_start_ticks is None or root_ident[1] == root_start_ticks
    ):
        for pid, (uid, st_ticks) in collect_descendant_identities(root_pid, root_start_ticks).items():
            if pid > 1 and pid != my_pid and uid == my_uid:
                tracked[pid] = st_ticks

    if extra_pid_identities:
        for ep, est in extra_pid_identities.items():
            if int(ep) > 1 and int(ep) != my_pid:
                tracked.setdefault(int(ep), int(est))

    if extra_pids:
        for ep in extra_pids:
            ip = int(ep)
            if ip > 1 and ip != my_pid and ip not in tracked:
                ident = _read_proc_identity(ip)
                if ident is not None and ident[0] == my_uid:
                    tracked[ip] = ident[1]

    if not tracked:
        # Still attempt waitpid reap in case root exited as zombie
        try:
            if hasattr(os, "waitpid"):
                os.waitpid(root_pid, os.WNOHANG)
        except OSError:
            pass
        return True

    def _is_proc_live_verified(pid: int, expected_st: int) -> bool:
        try:
            if pid == root_pid and hasattr(os, "waitpid"):
                os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        except OSError:
            pass
        ident = _read_proc_identity(pid)
        if ident is None:
            return False
        uid, cur_st, state_ch = ident
        if uid != my_uid or cur_st != expected_st:
            return False
        return state_ch not in ("Z", "X", "x")

    def _alive_owned_targets() -> list[tuple[int, int]]:
        # Rescan descendants of every still-verified live parent so children spawned mid-shutdown are captured
        for pid, est in list(tracked.items()):
            if _is_proc_live_verified(pid, est):
                for d_pid, (d_uid, d_st) in collect_descendant_identities(pid, est).items():
                    if d_pid > 1 and d_pid != my_pid and d_uid == my_uid:
                        tracked.setdefault(d_pid, d_st)
        alive: list[tuple[int, int]] = []
        for pid, est in list(tracked.items()):
            if pid <= 1 or pid == my_pid:
                continue
            if _is_proc_live_verified(pid, est):
                alive.append((pid, est))
            else:
                # Remove exited or reused PID permanently so it can never be targeted after PID wrap
                ident_now = _read_proc_identity(pid)
                if ident_now is None or ident_now[1] != est:
                    tracked.pop(pid, None)
        alive.sort(key=lambda pair: pair[0], reverse=True)
        return alive

    targets = _alive_owned_targets()
    if not targets:
        return True

    for sig, timeout_s in (
        (signal.SIGTERM, term_timeout_s),
        (getattr(signal, "SIGKILL", signal.SIGTERM), kill_timeout_s),
    ):
        for pid, est in _alive_owned_targets():
            _send_signal_verified(pid, sig, expected_uid=my_uid, expected_start_ticks=est)
        deadline = time.monotonic() + max(0.05, timeout_s)
        while time.monotonic() < deadline:
            cur_targets = _alive_owned_targets()
            if not cur_targets:
                return True
            # Signal any newly discovered child spawned during shutdown
            for pid, est in cur_targets:
                if sig == getattr(signal, "SIGKILL", signal.SIGTERM):
                    _send_signal_verified(pid, sig, expected_uid=my_uid, expected_start_ticks=est)
            time.sleep(0.05)

    return not _alive_owned_targets()


def _find_verified_profile_pids_with_identities(
    target_dir: Path,
    owner_pid: int | None = None,
    allowed_pids: list[int] | None = None,
    allowed_identities: dict[int, int] | None = None,
) -> dict[int, int]:
    """Identify ONLY UID-owned `(pid -> start_ticks)` proven to belong to this sandboxed worker/profile."""
    my_uid = os.getuid() if hasattr(os, "getuid") else 0
    my_pid = os.getpid()
    pids: dict[int, int] = {}

    if allowed_identities:
        for apid, ast in allowed_identities.items():
            ip = int(apid)
            if ip > 1 and ip != my_pid:
                ident = _read_proc_identity(ip)
                if ident is not None and ident[0] == my_uid and ident[1] == int(ast):
                    pids[ip] = ident[1]

    if owner_pid is not None and owner_pid > 1 and owner_pid != my_pid:
        owner_st = (allowed_identities or {}).get(owner_pid)
        for dpid, (duid, dst) in collect_descendant_identities(owner_pid, owner_st).items():
            if dpid > 1 and dpid != my_pid and duid == my_uid:
                pids.setdefault(dpid, dst)

    if allowed_pids:
        for apid in allowed_pids:
            ip = int(apid)
            if ip > 1 and ip != my_pid and ip not in pids:
                ident = _read_proc_identity(ip)
                if ident is not None and ident[0] == my_uid:
                    pids[ip] = ident[1]

    proc_root = Path("/proc")
    target_resolved = str(target_dir.resolve()) if target_dir.exists() else str(target_dir)
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid <= 1 or pid == my_pid or pid in pids:
                continue
            ident = _read_proc_identity(pid)
            if ident is None or ident[0] != my_uid:
                continue
            try:
                args = [
                    a.decode("utf-8", errors="ignore")
                    for a in (entry / "cmdline").read_bytes().split(b"\x00")
                    if a
                ]
            except OSError:
                continue
            if not args:
                continue
            exe_name = Path(args[0]).name.lower()
            if "firefox" not in exe_name and "playwright" not in exe_name and "browser" not in exe_name:
                continue
            if any(
                a == target_resolved
                or a == f"--profile={target_resolved}"
                or a == f"-profile={target_resolved}"
                for a in args
            ) or (
                ("--profile" in args or "-profile" in args)
                and target_resolved in args
            ):
                pids[pid] = ident[1]
    return dict(sorted(pids.items()))


def _find_verified_profile_pids(
    target_dir: Path,
    owner_pid: int | None = None,
    allowed_pids: list[int] | None = None,
) -> list[int]:
    return list(_find_verified_profile_pids_with_identities(target_dir, owner_pid=owner_pid, allowed_pids=allowed_pids).keys())


def detect_ram_persistence_risk(path: Path | None = None) -> dict[str, Any]:
    """Audit real backing filesystem of `path` plus OS swap/hibernation state (`B1`, `B9`, `B10`)."""
    probe_path = path or SHM_ROOT
    fs_type = "unknown"
    is_ram_backed = False

    best_mount = ""
    mounts_file = Path("/proc/mounts")
    if mounts_file.is_file():
        try:
            target_str = str(probe_path.resolve() if probe_path.exists() else probe_path)
            for ln in mounts_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = ln.split()
                if len(parts) >= 3:
                    mnt_point, mnt_type = parts[1], parts[2]
                    if target_str == mnt_point or target_str.startswith(mnt_point.rstrip("/") + "/"):
                        if len(mnt_point) >= len(best_mount):
                            best_mount = mnt_point
                            fs_type = mnt_type
            is_ram_backed = fs_type in ("tmpfs", "ramfs")
        except OSError:
            pass

    swap_active = False
    swap_total_mb = 0.0
    swaps_file = Path("/proc/swaps")
    if swaps_file.is_file():
        try:
            lines = swaps_file.read_text(encoding="utf-8", errors="ignore").splitlines()[1:]
            for ln in lines:
                cols = ln.split()
                if len(cols) >= 3 and cols[2].isdigit():
                    swap_active = True
                    swap_total_mb += int(cols[2]) / 1024.0
        except OSError:
            pass

    hibernation_possible = False
    power_state = Path("/sys/power/state")
    if power_state.is_file():
        try:
            hibernation_possible = "disk" in power_state.read_text(encoding="utf-8", errors="ignore").split()
        except OSError:
            pass

    if not is_ram_backed:
        note = (
            f"Temporary directory uses '{fs_type}' (may be disk-backed, NOT guaranteed RAM-only)."
        )
    elif swap_active or hibernation_possible:
        note = (
            f"Volatile tmpfs cleared on exit; note OS swap ({swap_total_mb:.0f} MB) or hibernation may page RAM."
        )
    else:
        note = "Volatile tmpfs cleared on exit (no active OS swap detected)."

    return {
        "path": str(probe_path),
        "fs_type": fs_type,
        "is_ram_backed": is_ram_backed,
        "swap_active": swap_active,
        "swap_total_mb": round(swap_total_mb, 1),
        "hibernation_possible": hibernation_possible,
        "privacy_note": note,
    }


def scorched_earth_wipe(
    target_dir: Path | str | None,
    *,
    owner_pid: int | None = None,
    allowed_pids: list[int] | None = None,
    allowed_identities: dict[int, int] | None = None,
    wait_timeout_s: float = 4.0,
) -> bool:
    """Terminate verified owned child/browser processes, fail if any survive, remove dir, and verify (`B6`, `B7`)."""
    if not target_dir:
        _set_last_wipe_diagnostics(ok=True, target_dir="", surviving_pids=[], dir_exists=False)
        return True
    p = Path(target_dir)
    my_uid = os.getuid() if hasattr(os, "getuid") else 0

    try:
        st = p.lstat()
        if hasattr(st, "st_uid") and st.st_uid != my_uid:
            _set_last_wipe_diagnostics(
                ok=False,
                target_dir=str(p),
                surviving_pids=[],
                dir_exists=True,
                error="Refused wipe: directory owned by different UID.",
            )
            return False
    except FileNotFoundError:
        _set_last_wipe_diagnostics(ok=True, target_dir=str(p), surviving_pids=[], dir_exists=False)
        return True
    except OSError as exc:
        _set_last_wipe_diagnostics(
            ok=False,
            target_dir=str(p),
            surviving_pids=[],
            dir_exists=p.exists(),
            error=str(exc),
        )
        return False

    # Never follow a symlink into an unrelated directory during recursive wipe (`B11`)
    if p.is_symlink():
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            _set_last_wipe_diagnostics(
                ok=False,
                target_dir=str(p),
                surviving_pids=[],
                dir_exists=p.is_symlink() or p.exists(),
                error=str(exc),
            )
            return False
        ok_sym = not p.is_symlink() and not p.exists()
        _set_last_wipe_diagnostics(ok=ok_sym, target_dir=str(p), surviving_pids=[], dir_exists=not ok_sym)
        return ok_sym

    pid_map = _find_verified_profile_pids_with_identities(
        p,
        owner_pid=owner_pid,
        allowed_pids=allowed_pids,
        allowed_identities=allowed_identities,
    )
    surviving: list[int] = []
    for pid, start_ticks in pid_map.items():
        ok_kill = kill_process_tree_verified(
            pid,
            root_start_ticks=start_ticks,
            term_timeout_s=max(0.8, wait_timeout_s * 0.6),
            kill_timeout_s=max(0.5, wait_timeout_s * 0.4),
        )
        if not ok_kill:
            surviving.append(pid)

    # Re-scan for any owned processes spawned during shutdown before attempting deletion
    post_pid_map = _find_verified_profile_pids_with_identities(
        p,
        owner_pid=owner_pid,
        allowed_pids=allowed_pids,
        allowed_identities=allowed_identities,
    )
    for pid, start_ticks in post_pid_map.items():
        if pid not in surviving:
            ok_kill2 = kill_process_tree_verified(
                pid,
                root_start_ticks=start_ticks,
                term_timeout_s=max(0.5, wait_timeout_s * 0.35),
                kill_timeout_s=max(0.5, wait_timeout_s * 0.35),
            )
            if not ok_kill2 and pid not in surviving:
                surviving.append(pid)

    if surviving:
        msg = f"Cleanup aborted: verified owned process(es) survived termination: {surviving}"
        _set_last_wipe_diagnostics(
            ok=False,
            target_dir=str(p),
            surviving_pids=surviving,
            dir_exists=p.exists() or p.is_symlink(),
            error=msg,
        )
        return False

    def _onerror(_fn: Any, path_str: str, _exc: Any) -> None:
        try:
            os.chmod(path_str, 0o700)
        except OSError:
            pass

    rm_err = ""
    for _attempt in range(4):
        try:
            if p.is_symlink() or p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, onerror=_onerror)
        except OSError as exc:
            rm_err = str(exc)
            time.sleep(0.08)
        if not p.exists() and not p.is_symlink():
            break

    rem_surviving = list(
        _find_verified_profile_pids_with_identities(
            p,
            owner_pid=owner_pid,
            allowed_pids=allowed_pids,
            allowed_identities=allowed_identities,
        ).keys()
    )
    still_exists = p.exists() or p.is_symlink()
    ok_final = (not still_exists) and (not rem_surviving)
    if not ok_final and not rm_err:
        rm_err = (
            f"Owned process(es) still running: {rem_surviving}"
            if rem_surviving
            else f"Remnants remain at {p}"
        )
    _set_last_wipe_diagnostics(
        ok=ok_final,
        target_dir=str(p),
        surviving_pids=rem_surviving,
        dir_exists=still_exists,
        error="" if ok_final else rm_err,
    )
    return ok_final


def purge_legacy_burner_profile() -> bool:
    """Remove any leftover persistent `~/firefox-burner-profile` ONLY if owned by current user."""
    if not (LEGACY_BURNER_DIR.exists() or LEGACY_BURNER_DIR.is_symlink()):
        return True
    my_uid = os.getuid() if hasattr(os, "getuid") else 0
    try:
        st = LEGACY_BURNER_DIR.lstat()
        if st.st_uid != my_uid:
            return False
    except OSError:
        return False
    if LEGACY_BURNER_DIR.is_symlink():
        try:
            LEGACY_BURNER_DIR.unlink(missing_ok=True)
        except OSError:
            return False
        return not LEGACY_BURNER_DIR.is_symlink()
    return scorched_earth_wipe(LEGACY_BURNER_DIR)


def probe_unshare_netns_capability(*, force_refresh: bool = False) -> tuple[bool, str]:
    """Actually test user+network namespace creation (`unshare -r -n`) (`A1`)."""
    global _NETNS_PROBE_CACHE
    with _NETNS_PROBE_LOCK:
        now = time.monotonic()
        if not force_refresh and _NETNS_PROBE_CACHE is not None:
            ts, ok, reason = _NETNS_PROBE_CACHE
            if (now - ts) < 60.0:
                return (ok, reason)

    unshare_bin = shutil.which("unshare")
    if not unshare_bin:
        res = (False, "unshare executable not found")
        with _NETNS_PROBE_LOCK:
            _NETNS_PROBE_CACHE = (time.monotonic(), res[0], res[1])
        return res

    probe_code = (
        "import os, socket, sys; "
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
        "s.settimeout(0.4); "
        "blocked = False\n"
        "try:\n"
        "    s.connect(('1.1.1.1', 443))\n"
        "except OSError:\n"
        "    blocked = True\n"
        "finally:\n"
        "    s.close()\n"
        "if not blocked or os.getuid() != 0:\n"
        "    sys.exit(2)\n"
        "print('NETNS_OK')\n"
    )
    python_exe = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else os.sys.executable
    try:
        r = subprocess.run(
            [unshare_bin, "-r", "-n", python_exe, "-c", probe_code],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        ok = r.returncode == 0 and "NETNS_OK" in (r.stdout or "")
        reason = "verified unshare -r -n" if ok else ((r.stderr or r.stdout or f"exit {r.returncode}").strip()[:180])
    except Exception as exc:
        ok = False
        reason = str(exc)[:180]

    with _NETNS_PROBE_LOCK:
        _NETNS_PROBE_CACHE = (time.monotonic(), ok, reason)
    return (ok, reason)


def is_unshare_netns_available() -> bool:
    """Return True ONLY when creating an unprivileged user+net namespace actually succeeds."""
    ok, _ = probe_unshare_netns_capability()
    return ok


def _can_use_mount_namespace() -> bool:
    unshare_bin = shutil.which("unshare")
    if not unshare_bin:
        return False
    python_exe = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else os.sys.executable
    try:
        r = subprocess.run(
            [unshare_bin, "-r", "-n", "-m", python_exe, "-c", "print('MNS_OK')"],
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
        return r.returncode == 0 and "MNS_OK" in (r.stdout or "")
    except Exception:
        return False


def _sweep_stale_ag_socket_dirs(base_root: Path) -> None:
    """Clean only owned `vimona_ag_*` dirs whose `.ag.lock` can be acquired non-blocking."""
    if not base_root.is_dir():
        return
    uid = os.getuid() if hasattr(os, "getuid") else 0
    for d in base_root.glob("vimona_ag_*"):
        if not d.is_dir():
            continue
        try:
            if d.lstat().st_uid != uid:
                continue
            lock_f = d / ".ag.lock"
            if lock_f.exists():
                with open(lock_f, "r+b") as fh:
                    try:
                        lock_file_exclusive(fh, nonblocking=True)
                    except BlockingIOError:
                        continue  # Active live session owns this socket dir!
            if (time.time() - d.lstat().st_mtime) > 5.0 or lock_f.exists():
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """Direct AF_UNIX HTTP client bypassing TCP loopback (`A5`, `A6`)."""

    def __init__(self, sock_path: Path, timeout: float = 10.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._unix_path = str(sock_path)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


class AirGappedProcess:
    """Manages an offline subprocess inside `unshare -r -n [-m]` with authenticated AF_UNIX bridge."""

    def __init__(
        self,
        proc: subprocess.Popen[Any],
        sock_path: Path | None,
        outer_port: int,
        airgapped: bool,
        *,
        sock_dir: Path | None = None,
        lock_fh: Any = None,
        auth_token: str = "",
        isolation_status: IsolationStatus | None = None,
        require_auth: bool = True,
    ) -> None:
        self.proc = proc
        self.pid = proc.pid
        self._proc_start_ticks = _read_proc_start_ticks(proc.pid)
        self.last_cleanup_error: str = ""
        self.sock_path = sock_path
        self.sock_dir = sock_dir
        self._lock_fh = lock_fh
        self.outer_port = outer_port
        self.airgapped = airgapped
        self.auth_token = auth_token or secrets.token_hex(24)
        self.require_auth = bool(require_auth)
        self.isolation_status = isolation_status or IsolationStatus(
            state="isolated" if airgapped else "unavailable",
            level="user_net_ns" if airgapped else "unisolated",
            detail="Network-isolated (netns)" if airgapped else "Isolation unavailable",
        )
        self._stop_ev = threading.Event()
        self._tcp_srv: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._conn_lock = threading.Lock()
        self._active_sockets: set[socket.socket] = set()
        self._fwd_threads: list[threading.Thread] = []

    @property
    def isolation_state(self) -> str:
        return self.isolation_status.state

    def request_headers(self) -> dict[str, str]:
        return {"X-Vimona-Bridge-Token": self.auth_token}

    def http_request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        """Make an HTTP request directly over the private 0600 AF_UNIX socket (or authed TCP)."""
        hdrs = dict(headers or {})
        if self.sock_path is not None and self.sock_path.exists():
            conn = _UnixSocketHTTPConnection(self.sock_path, timeout=timeout)
            try:
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                return (resp.status, resp.read())
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        hdrs["X-Vimona-Bridge-Token"] = self.auth_token
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.outer_port}{path}",
            data=body,
            headers=hdrs,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (int(resp.status), resp.read())
        except urllib.error.HTTPError as he:
            return (int(he.code), he.read())

    def start_host_forwarder(self) -> None:
        """Bind authenticated `127.0.0.1:<outer_port>` forwarding into the private AF_UNIX socket."""
        if not self.airgapped or self.sock_path is None:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.outer_port))
        srv.listen(32)
        srv.settimeout(0.35)
        self._tcp_srv = srv

        def _track_close(*socks: socket.socket) -> None:
            with self._conn_lock:
                for sk in socks:
                    self._active_sockets.discard(sk)
            for sk in socks:
                try:
                    sk.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sk.close()
                except OSError:
                    pass

        def _pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while not self._stop_ev.is_set():
                    chunk = src.recv(65536)
                    if not chunk:
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        break
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                _track_close(src, dst)

        def _handle_client(client: socket.socket) -> None:
            assert self.sock_path is not None
            initial_forward = b""
            if self.require_auth:
                try:
                    client.settimeout(2.0)
                    buf = bytearray()
                    while b"\r\n\r\n" not in buf and len(buf) < 16384:
                        chunk = client.recv(4096)
                        if not chunk:
                            _track_close(client)
                            return
                        buf.extend(chunk)
                    sep = buf.find(b"\r\n\r\n")
                    if sep == -1:
                        client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                        _track_close(client)
                        return
                    hdr_bytes = bytes(buf[:sep])
                    rest = bytes(buf[sep + 4 :])
                    lines = hdr_bytes.split(b"\r\n")
                    req_line = lines[0]
                    provided_tok = ""
                    clean_lines = [req_line]
                    for hline in lines[1:]:
                        if b":" in hline:
                            hk, hv = hline.split(b":", 1)
                            if hk.strip().lower() == b"x-vimona-bridge-token":
                                provided_tok = hv.strip().decode("utf-8", errors="ignore")
                                continue
                        clean_lines.append(hline)
                    if not provided_tok or not hmac.compare_digest(provided_tok, self.auth_token):
                        client.sendall(
                            b"HTTP/1.1 403 Forbidden\r\nContent-Length: 9\r\nConnection: close\r\n\r\nForbidden"
                        )
                        _track_close(client)
                        return
                    initial_forward = b"\r\n".join(clean_lines) + b"\r\n\r\n" + rest
                    client.settimeout(None)
                except OSError:
                    _track_close(client)
                    return

            try:
                u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                u.settimeout(4.0)
                u.connect(str(self.sock_path))
                u.settimeout(None)
                if initial_forward:
                    u.sendall(initial_forward)
            except OSError:
                _track_close(client)
                return

            with self._conn_lock:
                self._active_sockets.add(client)
                self._active_sockets.add(u)
            t1 = threading.Thread(target=_pump, args=(client, u), daemon=True)
            t2 = threading.Thread(target=_pump, args=(u, client), daemon=True)
            with self._conn_lock:
                self._fwd_threads = [t for t in self._fwd_threads if t.is_alive()] + [t1, t2]
            t1.start()
            t2.start()

        def _accept_loop() -> None:
            while not self._stop_ev.is_set():
                if self.proc.poll() is not None:
                    break
                try:
                    client, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with self._conn_lock:
                    self._active_sockets.add(client)
                ht = threading.Thread(target=_handle_client, args=(client,), daemon=True)
                with self._conn_lock:
                    self._fwd_threads = [t for t in self._fwd_threads if t.is_alive()] + [ht]
                ht.start()

        self._accept_thread = threading.Thread(target=_accept_loop, daemon=True, name="vimona-ag-fwd")
        self._accept_thread.start()

    def poll(self) -> int | None:
        rc = self.proc.poll()
        if rc is not None and self.isolation_status.state == "isolated":
            self.isolation_status = IsolationStatus(
                state="stopped" if rc == 0 else "failed",
                level=self.isolation_status.level,
                detail="Engine stopped" if rc == 0 else f"Isolated engine exited ({rc})",
                verified_interfaces=self.isolation_status.verified_interfaces,
                ipv4_blocked=self.isolation_status.ipv4_blocked,
                ipv6_blocked=self.isolation_status.ipv6_blocked,
                dns_blocked=self.isolation_status.dns_blocked,
                mount_isolated=self.isolation_status.mount_isolated,
                no_new_privs=self.isolation_status.no_new_privs,
                core_dumps_disabled=self.isolation_status.core_dumps_disabled,
                error="" if rc == 0 else f"Exit code {rc}",
            )
            _publish_isolation_status(self.isolation_status)
        return rc

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    def terminate_cleanly(self) -> bool:
        self._stop_ev.set()
        if self._tcp_srv is not None:
            try:
                self._tcp_srv.close()
            except OSError:
                pass
            self._tcp_srv = None

        with self._conn_lock:
            for sk in list(self._active_sockets):
                try:
                    sk.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sk.close()
                except OSError:
                    pass
            self._active_sockets.clear()
            threads = list(self._fwd_threads)
            self._fwd_threads.clear()

        if self._accept_thread is not None and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=1.0)
        for t in threads:
            if t.is_alive():
                t.join(timeout=0.5)

        terminated_ok = True
        if self.proc.poll() is None:
            terminated_ok = kill_process_tree_verified(
                self.pid,
                root_start_ticks=self._proc_start_ticks,
                term_timeout_s=5.0,
                kill_timeout_s=2.5,
            )
            if terminated_ok:
                try:
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    terminated_ok = False
        if not terminated_ok:
            self.last_cleanup_error = f"Isolated process {self.pid} or descendant(s) survived termination."
            self.isolation_status = IsolationStatus(
                state="failed",
                level=self.isolation_status.level,
                detail="Cleanup failed: isolated process survived termination",
                socket_dir=str(self.sock_dir or ""),
                error=self.last_cleanup_error,
            )
            _publish_isolation_status(self.isolation_status)
            return False

        _unregister_pid(self.pid)

        if self.sock_path is not None:
            try:
                self.sock_path.unlink(missing_ok=True)
            except OSError:
                pass
        if self._lock_fh is not None:
            try:
                unlock_file(self._lock_fh)
            except OSError:
                pass
            try:
                self._lock_fh.close()
            except OSError:
                pass
            self._lock_fh = None
        if self.sock_dir is not None and self.sock_dir.is_dir():
            try:
                shutil.rmtree(self.sock_dir, ignore_errors=True)
            except OSError:
                pass

        self.last_cleanup_error = ""
        self.isolation_status = IsolationStatus(
            state="stopped",
            level="unisolated",
            detail="Isolated engine stopped cleanly",
        )
        _publish_isolation_status(self.isolation_status)
        return True


def launch_airgapped_http_service(
    cmd: list[str],
    inner_port: int,
    outer_port: int,
    protection_label: str,
    log_fh: Any,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    *,
    allow_unisolated: bool = False,
    handshake_timeout_s: float = 10.0,
    require_mount_ns: bool = True,
    require_no_new_privs: bool = True,
    require_core_dump_disabled: bool = True,
    allow_partial_isolation: bool = False,
) -> AirGappedProcess:
    """Launch `cmd` inside `unshare -r -n -m` with verified mandatory hardening & private AF_UNIX bridge.

    Fail closed by default: raises `AirGapIsolationError` if namespace creation, egress checks,
    private `/tmp` mount, `PR_SET_NO_NEW_PRIVS`, `RLIMIT_CORE=0`, or handshake fail.
    Pass `allow_partial_isolation=True` only when partial degradation is explicitly permitted.
    """
    _publish_isolation_status(
        IsolationStatus(state="starting", level="user_net_mount_ns_rlimit", detail="Starting namespace isolation…")
    )

    netns_ok, probe_reason = probe_unshare_netns_capability()
    worker_env = get_worker_env(env)
    if not netns_ok:
        if not allow_unisolated:
            fail_st = IsolationStatus(
                state="unavailable",
                level="unisolated",
                detail=f"Isolation unavailable: {probe_reason}",
                error=probe_reason,
            )
            _publish_isolation_status(fail_st)
            raise AirGapIsolationError(
                f"Fail-closed: user/network namespace isolation unavailable ({probe_reason})."
            )
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
        _register_pid(proc.pid, protection_label)
        st = IsolationStatus(
            state="unavailable",
            level="unisolated",
            detail="Running unisolated (explicit opt-in; netns unavailable)",
            error=probe_reason,
        )
        _publish_isolation_status(st)
        return AirGappedProcess(
            proc=proc,
            sock_path=None,
            outer_port=outer_port,
            airgapped=False,
            isolation_status=st,
        )

    base_root = SHM_ROOT if (SHM_ROOT.is_dir() and os.access(SHM_ROOT, os.W_OK)) else Path(tempfile.gettempdir())
    _sweep_stale_ag_socket_dirs(base_root)

    sock_dir = Path(tempfile.mkdtemp(prefix=f"vimona_ag_{os.getpid()}_", dir=str(base_root)))
    try:
        os.chmod(sock_dir, 0o700)
    except OSError:
        pass
    lock_file = sock_dir / ".ag.lock"
    lock_fh = open(lock_file, "a+b")
    try:
        lock_file_exclusive(lock_fh, nonblocking=True)
    except OSError:
        pass
    sock_path = sock_dir / "bridge.sock"

    mount_capable = _can_use_mount_namespace() and not str(sock_dir.resolve()).startswith("/tmp/")
    if require_mount_ns and not mount_capable and not allow_partial_isolation:
        try:
            unlock_file(lock_fh)
            lock_fh.close()
        except OSError:
            pass
        shutil.rmtree(sock_dir, ignore_errors=True)
        fail_st = IsolationStatus(
            state="failed",
            level="unisolated",
            detail="Mandatory private /tmp mount namespace unavailable",
            error="Mount namespace or /dev/shm socket dir unavailable",
        )
        _publish_isolation_status(fail_st)
        raise AirGapIsolationError(
            "Fail-closed: mandatory mount namespace isolation (/tmp tmpfs) is unavailable."
        )

    use_mount_ns = mount_capable
    ready_r, ready_w = os.pipe()
    os.set_inheritable(ready_w, True)

    python_exe = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else os.sys.executable
    host_uid = os.getuid() if hasattr(os, "getuid") else 0
    unshare_flags = ["-r", "-n", "-m"] if use_mount_ns else ["-r", "-n"]
    sup_req_mount = "1" if (require_mount_ns and not allow_partial_isolation) else "0"
    sup_req_nnp = "1" if (require_no_new_privs and not allow_partial_isolation) else "0"
    sup_req_core = "1" if (require_core_dump_disabled and not allow_partial_isolation) else "0"
    base_wrapped = [
        "unshare",
        *unshare_flags,
        python_exe,
        "-c",
        _AIRGAP_INNER_SUPERVISOR,
        str(sock_path),
        str(inner_port),
        str(ready_w),
        "1" if use_mount_ns else "0",
        sup_req_mount,
        sup_req_nnp,
        sup_req_core,
        str(host_uid),
        *cmd,
    ]
    wrapped_cmd, popen_flags = wrap_worker_cmd_with_priority(base_wrapped)

    try:
        proc = subprocess.Popen(
            wrapped_cmd,
            cwd=str(cwd) if cwd else None,
            env=worker_env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            creationflags=popen_flags,
            pass_fds=(ready_w,),
        )
    except Exception as exc:
        os.close(ready_r)
        os.close(ready_w)
        try:
            lock_fh.close()
        except OSError:
            pass
        shutil.rmtree(sock_dir, ignore_errors=True)
        err_st = IsolationStatus(state="failed", level="unisolated", detail="Failed to spawn supervisor", error=str(exc))
        _publish_isolation_status(err_st)
        raise AirGapIsolationError(f"Failed to launch air-gapped supervisor: {exc}") from exc

    os.close(ready_w)
    _register_pid(proc.pid, protection_label)

    # Wait for strict JSON readiness handshake from inside the namespace
    import select

    handshake: dict[str, Any] | None = None
    buf = bytearray()
    deadline = time.monotonic() + max(2.0, float(handshake_timeout_s))
    try:
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            rlist, _, _ = select.select([ready_r], [], [], min(0.25, remaining))
            if rlist:
                chunk = os.read(ready_r, 4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf:
                    line = bytes(buf).splitlines()[0].decode("utf-8", errors="ignore")
                    try:
                        handshake = json.loads(line)
                    except json.JSONDecodeError:
                        handshake = {"ready": False, "error": f"Malformed handshake: {line[:120]}"}
                    break
            elif proc.poll() is not None:
                break
    finally:
        try:
            os.close(ready_r)
        except OSError:
            pass

    ok_handshake = (
        isinstance(handshake, dict)
        and bool(handshake.get("ready"))
        and sock_path.exists()
        and proc.poll() is None
    )
    if not ok_handshake:
        err_msg = (
            str(handshake.get("error"))
            if isinstance(handshake, dict) and handshake.get("error")
            else (
                f"Supervisor exited with code {proc.returncode}"
                if proc.poll() is not None
                else "Timed out waiting for namespace handshake & socket"
            )
        )
        _unregister_pid(proc.pid)
        kill_process_tree_verified(proc.pid, term_timeout_s=2.0, kill_timeout_s=1.0)
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass
        try:
            unlock_file(lock_fh)
            lock_fh.close()
        except OSError:
            pass
        shutil.rmtree(sock_dir, ignore_errors=True)
        fail_st = IsolationStatus(
            state="failed",
            level="unisolated",
            detail="Namespace handshake failed",
            error=err_msg,
        )
        _publish_isolation_status(fail_st)
        raise AirGapIsolationError(f"Fail-closed: {err_msg}")

    assert isinstance(handshake, dict)
    ifaces_t = tuple(str(i) for i in handshake.get("interfaces", []))
    ipv4_blk = bool(handshake.get("ipv4_blocked", False))
    ipv6_blk = bool(handshake.get("ipv6_blocked", False))
    dns_blk = bool(handshake.get("dns_blocked", False))
    mount_iso = bool(handshake.get("mount_isolated", False))
    nnp_ok = bool(handshake.get("no_new_privs", False))
    core_ok = bool(handshake.get("core_dumps_disabled", False))

    net_ok = ipv4_blk and ipv6_blk and dns_blk and all(i == "lo" for i in ifaces_t)
    missing_controls: list[str] = []
    if require_mount_ns and not mount_iso:
        missing_controls.append("mount_isolated")
    if require_no_new_privs and not nnp_ok:
        missing_controls.append("no_new_privs")
    if require_core_dump_disabled and not core_ok:
        missing_controls.append("core_dumps_disabled")

    if (not net_ok) or (missing_controls and not allow_partial_isolation):
        reason = (
            "Network isolation verification failed"
            if not net_ok
            else f"Mandatory isolation control(s) failed: {', '.join(missing_controls)}"
        )
        _unregister_pid(proc.pid)
        kill_process_tree_verified(proc.pid, term_timeout_s=2.0, kill_timeout_s=1.0)
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass
        try:
            unlock_file(lock_fh)
            lock_fh.close()
        except OSError:
            pass
        shutil.rmtree(sock_dir, ignore_errors=True)
        fail_st = IsolationStatus(
            state="failed",
            level="unisolated",
            detail=reason,
            verified_interfaces=ifaces_t,
            ipv4_blocked=ipv4_blk,
            ipv6_blocked=ipv6_blk,
            dns_blocked=dns_blk,
            mount_isolated=mount_iso,
            no_new_privs=nnp_ok,
            core_dumps_disabled=core_ok,
            error=reason,
        )
        _publish_isolation_status(fail_st)
        raise AirGapIsolationError(f"Fail-closed: {reason}")

    if missing_controls:
        st_name = "partial"
        lvl = "user_net_ns_partial"
        detail_msg = f"Partial isolation (netns only; missing: {', '.join(missing_controls)})"
    else:
        st_name = "isolated"
        lvl = "user_net_mount_ns_rlimit" if mount_iso else "user_net_ns"
        detail_msg = "Network & mount isolated (netns + /tmp tmpfs + no_new_privs)"

    verified_st = IsolationStatus(
        state=st_name,
        level=lvl,
        detail=detail_msg,
        verified_interfaces=ifaces_t or ("lo",),
        ipv4_blocked=ipv4_blk,
        ipv6_blocked=ipv6_blk,
        dns_blocked=dns_blk,
        mount_isolated=mount_iso,
        no_new_privs=nnp_ok,
        core_dumps_disabled=core_ok,
        socket_dir=str(sock_dir),
    )
    _publish_isolation_status(verified_st)

    ag = AirGappedProcess(
        proc=proc,
        sock_path=sock_path,
        sock_dir=sock_dir,
        lock_fh=lock_fh,
        outer_port=outer_port,
        airgapped=True,
        isolation_status=verified_st,
        require_auth=True,
    )
    ag.start_host_forwarder()
    return ag
