"""Zero-trace ephemeral web reference puller driven via headless Firefox in `/dev/shm` RAM.

Replaces the legacy persistent burner profile:
1. Purges any leftover `~/firefox-burner-profile` from older builds.
2. Allocates a single-prompt temporary directory on Linux `/dev/shm` RAM disk
   (falling back to OS temp only if `/dev/shm` is unavailable).
3. Launches Playwright headless Firefox in memory-only private mode pointed
   exclusively at that disposable directory (`browser.cache.disk.enable=False`).
4. Searches the web, inspects top pages inside the sandboxed browser, pulls
   candidate reference images strictly into Python RAM buffers (never saved to
   browser disk cache), extracts local lighting/color/palette/style tokens +
   Safe-Blur flags, and then destroys Firefox + runs `scorched_earth_wipe()`
   before returning control so offline rendering begins with zero browser trace.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .platform_compat import disable_core_dumps, lock_file_exclusive, unlock_file

_MAX_RESULTS_CAP = 10
_MAX_IMAGES_CAP = 8
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_PIXELS = 16_000_000
_MAX_IMAGE_DIM = 4096
_MAX_RESEARCH_BYTES = 24 * 1024 * 1024
_MAX_WORKER_STDOUT_BYTES = 36 * 1024 * 1024
_MAX_WORKER_STDERR_BYTES = 256 * 1024
_MAX_NAVIGATIONS = 8
_ALLOWED_HTTP_PORTS: frozenset[int] = frozenset({80})
_ALLOWED_HTTPS_PORTS: frozenset[int] = frozenset({443})
_ALLOWED_RESEARCH_PORTS: frozenset[int] = frozenset({80, 443})
_MAX_PROXY_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_PROXY_CONN_BYTES = 10 * 1024 * 1024
_MAX_PROXY_CONCURRENT_CONNS = 16
_MAX_PROXY_TOTAL_CONNECTIONS = 180
_PROXY_IO_TIMEOUT_S = 10.0
_PROXY_DEFAULT_DEADLINE_S = 50.0
_SAFESEARCH_KP_MAP = {"strict": "1", "moderate": "-1", "off": "-2"}

from .airgap import (
    _read_proc_start_ticks,
    collect_descendant_identities,
    collect_descendant_pids,
    detect_ram_persistence_risk,
    get_last_wipe_diagnostics,
    is_pid_alive_with_start,
    kill_process_tree_verified,
    purge_legacy_burner_profile,
    scorched_earth_wipe,
)
from .config import get_config
from .safety_ml import get_safety_ml_engine
from .safety_patterns import is_explicit_text

COMFY_VENV_PYTHON = get_config().comfy_dir / "venv/bin/python"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHM_DIR = Path("/dev/shm")

_MEMORY_ONLY_FIREFOX_PREFS: dict[str, Any] = {
    "browser.cache.disk.enable": False,
    "browser.cache.disk.capacity": 0,
    "browser.cache.disk_cache_ssl": False,
    "browser.cache.offline.enable": False,
    "browser.cache.memory.enable": True,
    "browser.privatebrowsing.autostart": True,
    "places.history.enabled": False,
    "browser.formfill.enable": False,
    "browser.sessionstore.resume_from_crash": False,
    "browser.sessionstore.interval": 86400000,
    "dom.storage.next_gen": False,
    "privacy.sanitize.sanitizeOnShutdown": True,
    "privacy.clearOnShutdown.cache": True,
    "privacy.clearOnShutdown.cookies": True,
    "privacy.clearOnShutdown.history": True,
    "privacy.clearOnShutdown.offlineApps": True,
    "privacy.clearOnShutdown.sessions": True,
    "toolkit.telemetry.enabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "app.update.enabled": False,
    "network.proxy.no_proxies_on": "",
    "network.proxy.allow_hijacking_localhost": True,
}


_INJECTION_MARKER_RE = re.compile(
    r"(?i)(?:<\|[^|>]{1,40}\|>|</?tool_call>|^\s*(?:system|assistant|developer)\s*:|"
    r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions)"
)


def _clean_text(raw: str, max_len: int = 320) -> str:
    # Strip control/zero-width chars to mitigate prompt injection (`F11`, `F12`)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\ufeff]", " ", raw or "")
    collapsed = re.sub(r"\s+", " ", cleaned).strip()
    if len(collapsed) > max_len:
        return collapsed[: max_len - 1].rstrip() + "…"
    return collapsed


def sanitize_untrusted_web_text(raw: str, max_len: int = 320) -> str:
    """Neutralize prompt-injection / tool-spoofing markers in scraped web text (`F11`, `F12`)."""
    txt = _clean_text(raw, max_len=max_len)
    return _INJECTION_MARKER_RE.sub("[filtered-directive]", txt)


def _is_non_public_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        return _is_non_public_ip(ip_obj.ipv4_mapped)
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or not ip_obj.is_global
    )


def validate_public_http_url(
    url_str: str,
    *,
    resolve_dns: bool = True,
    pinned_hosts: dict[str, set[str]] | None = None,
    allowed_ports: frozenset[int] | set[int] | None = None,
) -> tuple[bool, str, set[str]]:
    """Validate public HTTP/HTTPS destinations, web ports, and block SSRF / DNS rebinding (`F1`–`F4`)."""
    raw = (url_str or "").strip()
    if not raw:
        return (False, "Empty URL", set())
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return (False, "Malformed URL", set())

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return (False, f"Unsupported scheme '{parsed.scheme}' (only http/https permitted)", set())
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        return (False, "Credentials in URL authority are blocked", set())
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return (False, "Missing hostname", set())
    if host in ("localhost", "ip6-localhost", "ip6-loopback") or host.endswith(
        (".local", ".localhost", ".internal", ".localdomain", ".home", ".lan", ".corp", ".arpa")
    ):
        return (False, f"Blocked local/internal host '{host}'", set())

    try:
        port = parsed.port
        if port is not None and not (1 <= int(port) <= 65535):
            return (False, "Invalid port", set())
    except ValueError:
        return (False, "Invalid port", set())
    eff_port = int(port) if port is not None else (443 if scheme == "https" else 80)
    permitted_ports = (
        allowed_ports
        if allowed_ports is not None
        else (_ALLOWED_HTTPS_PORTS if scheme == "https" else _ALLOWED_HTTP_PORTS)
    )
    if eff_port not in permitted_ports:
        return (False, f"Blocked non-web port {eff_port} for scheme '{scheme}'", set())

    resolved_ips: set[str] = set()
    # Direct IP literal check
    try:
        lit = ipaddress.ip_address(host)
        if _is_non_public_ip(lit):
            return (False, f"Blocked non-public IP literal '{host}'", set())
        resolved_ips.add(str(lit))
    except ValueError:
        if resolve_dns:
            try:
                infos = socket.getaddrinfo(host, eff_port, type=socket.SOCK_STREAM)
            except OSError as exc:
                return (False, f"DNS resolution failed for '{host}': {exc}", set())
            if not infos:
                return (False, f"No DNS records for '{host}'", set())
            for _fam, _st, _pr, _cn, sockaddr in infos:
                ip_txt = str(sockaddr[0]).split("%", 1)[0]
                try:
                    ip_obj = ipaddress.ip_address(ip_txt)
                except ValueError:
                    return (False, f"Unparseable resolved IP '{ip_txt}'", set())
                if _is_non_public_ip(ip_obj):
                    return (False, f"Blocked resolution of '{host}' -> private/local IP {ip_txt}", set())
                resolved_ips.add(str(ip_obj))

            # DNS rebinding defense (`F3`): ensure hostname did not rebind to completely new IP set mid-session
            if pinned_hosts is not None:
                prev = pinned_hosts.get(host)
                if prev is not None and resolved_ips and not (resolved_ips & prev):
                    return (False, f"DNS rebind detected for '{host}' ({prev} -> {resolved_ips})", set())
                if resolved_ips:
                    pinned_hosts.setdefault(host, set()).update(resolved_ips)

    return (True, "OK", resolved_ips)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP client that dials a pre-validated pinned IP instead of re-resolving DNS (`F1`–`F3`)."""

    def __init__(self, orig_host: str, port: int, pinned_ip: str, timeout: float = 7.0) -> None:
        super().__init__(orig_host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS client that dials a pre-validated pinned IP and verifies TLS against original hostname."""

    def __init__(self, orig_host: str, port: int, pinned_ip: str, timeout: float = 7.0) -> None:
        super().__init__(orig_host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip
        self._sni_host = orig_host

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(raw_sock, server_hostname=self._sni_host)


def _ordered_validated_ips(ips: set[str]) -> list[str]:
    """Return validated IPs deterministically with IPv4 preferred before IPv6."""
    return sorted(ips, key=lambda ip: (1 if ":" in ip else 0, ip))


def _dial_validated_ips(ips: set[str], port: int, *, timeout: float) -> socket.socket:
    """Connect sequentially across validated IPs, propagating final OSError if none succeed."""
    ordered = _ordered_validated_ips(ips)
    last_exc: OSError | None = None
    for target_ip in ordered:
        try:
            return socket.create_connection((target_ip, port), timeout=timeout)
        except OSError as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    raise OSError("No validated target IPs available")


def fetch_public_image_pinned(
    url: str,
    *,
    pinned_hosts: dict[str, set[str]],
    max_redirects: int = 3,
    max_bytes: int = _MAX_IMAGE_BYTES,
    timeout_s: float = 7.0,
    allowed_ports: frozenset[int] | set[int] | None = None,
) -> tuple[bytes, str] | None:
    """Fetch public HTTP/HTTPS image with per-hop SSRF/port/redirect validation & pinned-IP dialing (`F1`–`F3`)."""
    if max_bytes <= 0:
        return None
    cur_url = (url or "").strip()
    for hop in range(max_redirects + 1):
        ok, _err, resolved_ips = validate_public_http_url(
            cur_url,
            resolve_dns=True,
            pinned_hosts=pinned_hosts,
            allowed_ports=allowed_ports,
        )
        if not ok or not resolved_ips:
            return None
        parsed = urllib.parse.urlparse(cur_url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        default_port = 443 if scheme == "https" else 80
        port = int(parsed.port or default_port)
        path_q = parsed.path or "/"
        if parsed.query:
            path_q = f"{path_q}?{parsed.query}"
        host_hdr = f"[{host}]" if ":" in host else host
        if port != default_port:
            host_hdr = f"{host_hdr}:{port}"

        req_headers = {
            "Host": host_hdr,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5",
            "Connection": "close",
        }
        conn: http.client.HTTPConnection | None = None
        resp: http.client.HTTPResponse | None = None
        for target_ip in _ordered_validated_ips(resolved_ips):
            cand_conn: http.client.HTTPConnection = (
                _PinnedHTTPSConnection(host, port, target_ip, timeout=timeout_s)
                if scheme == "https"
                else _PinnedHTTPConnection(host, port, target_ip, timeout=timeout_s)
            )
            try:
                cand_conn.request("GET", path_q, headers=req_headers)
                resp = cand_conn.getresponse()
                conn = cand_conn
                break
            except ssl.SSLError:
                try:
                    cand_conn.close()
                except Exception:
                    pass
                return None
            except OSError:
                try:
                    cand_conn.close()
                except Exception:
                    pass
                continue
            except Exception:
                try:
                    cand_conn.close()
                except Exception:
                    pass
                return None

        if conn is None or resp is None:
            return None

        try:
            status = int(resp.status)
            if status in (301, 302, 303, 307, 308):
                loc = str(resp.getheader("Location") or "").strip()
                if not loc or hop >= max_redirects:
                    return None
                cur_url = urllib.parse.urljoin(cur_url, loc)
                continue
            if status < 200 or status >= 300:
                return None
            cl_raw = resp.getheader("Content-Length") or ""
            if cl_raw.isdigit() and int(cl_raw) > max_bytes:
                return None
            buf = bytearray()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                if (len(buf) + len(chunk)) > max_bytes:
                    return None
                buf.extend(chunk)
            return (bytes(buf), cur_url)
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return None


class _ValidatingLocalProxy:
    """Loopback HTTP/CONNECT proxy enforcing web ports, SSRF/pinned-IP validation, and byte/conn budgets (`F1`–`F3`).

    `max_total_connections` caps accepted proxy client connections (single-request plain HTTP
    connections with `Connection: close` plus opaque HTTPS `CONNECT` tunnels). Individual
    encrypted requests multiplexed inside an HTTPS tunnel are not inspected/counted separately
    and are bounded by `max_conn_bytes`, `max_total_bytes`, I/O timeouts, and `deadline_s`.
    """

    def __init__(
        self,
        pinned_hosts: dict[str, set[str]],
        *,
        allowed_ports: frozenset[int] | set[int] | None = None,
        max_total_bytes: int = _MAX_PROXY_TOTAL_BYTES,
        max_conn_bytes: int = _MAX_PROXY_CONN_BYTES,
        max_concurrent_conns: int = _MAX_PROXY_CONCURRENT_CONNS,
        max_total_connections: int = _MAX_PROXY_TOTAL_CONNECTIONS,
        conn_timeout_s: float = _PROXY_IO_TIMEOUT_S,
        deadline_s: float = _PROXY_DEFAULT_DEADLINE_S,
    ) -> None:
        self._pinned_hosts = pinned_hosts
        self._allowed_ports = allowed_ports
        self._max_total_bytes = max(1024, int(max_total_bytes))
        self._max_conn_bytes = max(1024, int(max_conn_bytes))
        self._max_concurrent_conns = max(1, int(max_concurrent_conns))
        self._max_total_connections = max(1, int(max_total_connections))
        self._conn_timeout_s = max(0.2, float(conn_timeout_s))
        self._deadline = time.monotonic() + max(0.5, float(deadline_s))
        self._stop_ev = threading.Event()
        self._srv: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._sockets: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self.total_bytes: int = 0
        self.total_connections: int = 0
        self.http_connections: int = 0
        self.connect_tunnels: int = 0
        self.active_conns: int = 0
        self.peak_active_conns: int = 0
        self.rejected_connections: int = 0
        self.budget_exceeded: bool = False
        self.limit_reason: str = ""
        self.port: int = 0

    def _close_socks(self, *socks: socket.socket) -> None:
        with self._lock:
            for sk in socks:
                self._sockets.discard(sk)
        for sk in socks:
            try:
                sk.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sk.close()
            except OSError:
                pass

    def _abort_all_conns(self) -> None:
        with self._lock:
            socks = list(self._sockets)
            self._sockets.clear()
        for sk in socks:
            try:
                sk.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sk.close()
            except OSError:
                pass

    def _charge_bytes(self, nbytes: int, conn_state: dict[str, int], conn_lock: threading.Lock) -> bool:
        if nbytes <= 0:
            return True
        with conn_lock:
            if (conn_state["bytes"] + nbytes) > self._max_conn_bytes:
                with self._lock:
                    self.budget_exceeded = True
                    self.limit_reason = self.limit_reason or f"Connection byte cap exceeded ({self._max_conn_bytes} bytes)"
                self._abort_all_conns()
                return False
            conn_state["bytes"] += nbytes

        with self._lock:
            if time.monotonic() >= self._deadline:
                self.budget_exceeded = True
                self.limit_reason = self.limit_reason or "Research proxy deadline exceeded"
                over = True
            elif (self.total_bytes + nbytes) > self._max_total_bytes:
                self.budget_exceeded = True
                self.limit_reason = self.limit_reason or f"Aggregate proxy byte budget exceeded ({self._max_total_bytes} bytes)"
                over = True
            else:
                self.total_bytes += nbytes
                over = False
        if over:
            self._abort_all_conns()
            return False
        return True

    def _relay(
        self,
        src: socket.socket,
        dst: socket.socket,
        conn_state: dict[str, int],
        conn_lock: threading.Lock,
    ) -> None:
        try:
            while not self._stop_ev.is_set() and not self.budget_exceeded:
                rem_s = self._deadline - time.monotonic()
                if rem_s <= 0:
                    with self._lock:
                        self.budget_exceeded = True
                        self.limit_reason = self.limit_reason or "Research proxy deadline exceeded"
                    self._abort_all_conns()
                    break
                io_to = max(0.05, min(self._conn_timeout_s, rem_s))
                src.settimeout(io_to)
                dst.settimeout(io_to)
                data = src.recv(32768)
                if not data:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    break
                if not self._charge_bytes(len(data), conn_state, conn_lock):
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            self._close_socks(src, dst)

    def _send_http_error(self, client: socket.socket, status_line: str, msg: str) -> None:
        body = msg.encode("ascii", errors="replace")
        try:
            client.sendall(
                f"HTTP/1.1 {status_line}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + body
            )
        except OSError:
            pass
        self._close_socks(client)

    def _send_forbidden(self, client: socket.socket) -> None:
        self._send_http_error(client, "403 Forbidden", "Blocked")

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        slot_acquired = False
        rev_thread: threading.Thread | None = None
        try:
            rem_hdr = min(5.0, self._conn_timeout_s, max(0.1, self._deadline - time.monotonic()))
            client.settimeout(rem_hdr)
            buf = bytearray()
            while b"\r\n\r\n" not in buf and len(buf) < 32768:
                chunk = client.recv(4096)
                if not chunk:
                    self._close_socks(client)
                    return
                buf.extend(chunk)
            sep = buf.find(b"\r\n\r\n")
            if sep == -1:
                with self._lock:
                    self.rejected_connections += 1
                self._send_forbidden(client)
                return
            head_bytes = bytes(buf[:sep])
            body_rem = bytes(buf[sep + 4 :])
            lines = head_bytes.split(b"\r\n")
            req_parts = lines[0].decode("latin-1", errors="replace").split()
            if len(req_parts) < 3:
                with self._lock:
                    self.rejected_connections += 1
                self._send_forbidden(client)
                return
            method, target, proto = req_parts[0].upper(), req_parts[1], req_parts[2]

            pre_err: tuple[str, str] | None = None
            with self._lock:
                if time.monotonic() >= self._deadline:
                    self.rejected_connections += 1
                    self.budget_exceeded = True
                    self.limit_reason = self.limit_reason or "Research proxy deadline exceeded"
                    pre_err = ("504 Gateway Timeout", "Proxy deadline exceeded")
                elif self.budget_exceeded:
                    self.rejected_connections += 1
                    pre_err = ("429 Too Many Requests", "Proxy byte budget exceeded")
                elif self.total_connections >= self._max_total_connections:
                    self.rejected_connections += 1
                    self.budget_exceeded = True
                    self.limit_reason = (
                        self.limit_reason or f"Proxy connection/tunnel limit ({self._max_total_connections}) exceeded"
                    )
                    pre_err = ("429 Too Many Requests", "Connection/tunnel cap exceeded")
                else:
                    self.total_connections += 1
            if pre_err is not None:
                self._send_http_error(client, *pre_err)
                return

            if method == "CONNECT":
                if target.startswith("[") and "]:" in target:
                    host_part, port_str = target[1:].split("]:", 1)
                    synth_url = f"https://[{host_part}]:{port_str}/"
                elif ":" in target:
                    host_part, port_str = target.rsplit(":", 1)
                    synth_url = f"https://{host_part}:{port_str}/"
                else:
                    synth_url = f"https://{target}/"
                    port_str = "443"
                if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
                    with self._lock:
                        self.rejected_connections += 1
                    self._send_forbidden(client)
                    return
                port_num = int(port_str)
                connect_ports = self._allowed_ports if self._allowed_ports is not None else _ALLOWED_HTTPS_PORTS
                if port_num not in connect_ports:
                    with self._lock:
                        self.rejected_connections += 1
                    self._send_forbidden(client)
                    return
                ok, _err, resolved_ips = validate_public_http_url(
                    synth_url,
                    resolve_dns=True,
                    pinned_hosts=self._pinned_hosts,
                    allowed_ports=connect_ports,
                )
                if not ok or not resolved_ips:
                    with self._lock:
                        self.rejected_connections += 1
                    self._send_forbidden(client)
                    return
            else:
                ok, _err, resolved_ips = validate_public_http_url(
                    target,
                    resolve_dns=True,
                    pinned_hosts=self._pinned_hosts,
                    allowed_ports=self._allowed_ports,
                )
                if not ok or not resolved_ips:
                    with self._lock:
                        self.rejected_connections += 1
                    self._send_forbidden(client)
                    return
                parsed = urllib.parse.urlparse(target)
                port_num = int(parsed.port or (443 if parsed.scheme.lower() == "https" else 80))

            slot_err: tuple[str, str] | None = None
            with self._lock:
                if time.monotonic() >= self._deadline:
                    self.rejected_connections += 1
                    self.budget_exceeded = True
                    self.limit_reason = self.limit_reason or "Research proxy deadline exceeded"
                    slot_err = ("504 Gateway Timeout", "Proxy deadline exceeded")
                elif self.budget_exceeded:
                    self.rejected_connections += 1
                    slot_err = ("429 Too Many Requests", "Proxy byte budget exceeded")
                elif self.active_conns >= self._max_concurrent_conns:
                    self.rejected_connections += 1
                    slot_err = ("503 Service Unavailable", "Too many concurrent connections")
                else:
                    self.active_conns += 1
                    if self.active_conns > self.peak_active_conns:
                        self.peak_active_conns = self.active_conns
                    slot_acquired = True
            if slot_err is not None:
                self._send_http_error(client, *slot_err)
                return

            rem_dial = max(0.2, min(self._conn_timeout_s, self._deadline - time.monotonic()))
            upstream = _dial_validated_ips(resolved_ips, port_num, timeout=rem_dial)
            with self._lock:
                self._sockets.add(upstream)
                if method == "CONNECT":
                    self.connect_tunnels += 1
                else:
                    self.http_connections += 1
            io_to = max(0.2, min(self._conn_timeout_s, self._deadline - time.monotonic()))
            upstream.settimeout(io_to)
            client.settimeout(io_to)

            conn_state = {"bytes": 0}
            conn_lock = threading.Lock()
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if body_rem:
                    if not self._charge_bytes(len(body_rem), conn_state, conn_lock):
                        return
                    upstream.sendall(body_rem)
                rev_thread = threading.Thread(
                    target=self._relay,
                    args=(upstream, client, conn_state, conn_lock),
                    daemon=True,
                )
                with self._lock:
                    self._threads = [t for t in self._threads if t.is_alive()] + [rev_thread]
                rev_thread.start()
                self._relay(client, upstream, conn_state, conn_lock)
            else:
                parsed = urllib.parse.urlparse(target)
                path_q = parsed.path or "/"
                if parsed.query:
                    path_q = f"{path_q}?{parsed.query}"
                clean_hdrs = [f"{method} {path_q} {proto}".encode("latin-1")]
                has_req_body_hdr = False
                for hl in lines[1:]:
                    hl_l = hl.lower()
                    if hl_l.startswith((b"proxy-connection:", b"connection:", b"keep-alive:")):
                        continue
                    if hl_l.startswith((b"content-length:", b"transfer-encoding:")):
                        has_req_body_hdr = True
                    clean_hdrs.append(hl)
                clean_hdrs.append(b"Connection: close")

                single_bodyless_http = (method in ("GET", "HEAD", "OPTIONS", "DELETE") and not has_req_body_hdr)
                if single_bodyless_http:
                    # Drop pipelined/keep-alive tail bytes so 1 plain-HTTP connection maps to at most 1 request
                    payload = b"\r\n".join(clean_hdrs) + b"\r\n\r\n"
                    upstream.sendall(payload)
                    try:
                        upstream.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    self._relay(upstream, client, conn_state, conn_lock)
                else:
                    payload = b"\r\n".join(clean_hdrs) + b"\r\n\r\n" + body_rem
                    if body_rem and not self._charge_bytes(len(body_rem), conn_state, conn_lock):
                        return
                    upstream.sendall(payload)
                    rev_thread = threading.Thread(
                        target=self._relay,
                        args=(upstream, client, conn_state, conn_lock),
                        daemon=True,
                    )
                    with self._lock:
                        self._threads = [t for t in self._threads if t.is_alive()] + [rev_thread]
                    rev_thread.start()
                    self._relay(client, upstream, conn_state, conn_lock)
        except Exception:
            if upstream is not None:
                self._close_socks(client, upstream)
            else:
                self._close_socks(client)
        finally:
            if upstream is not None:
                self._close_socks(client, upstream)
            else:
                self._close_socks(client)
            if rev_thread is not None and rev_thread.is_alive():
                rev_thread.join(timeout=1.0)
            if slot_acquired:
                with self._lock:
                    self.active_conns = max(0, self.active_conns - 1)

    def start(self) -> int:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        srv.settimeout(0.3)
        self._srv = srv
        self.port = int(srv.getsockname()[1])

        def _accept_loop() -> None:
            while not self._stop_ev.is_set():
                try:
                    c, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with self._lock:
                    live_threads = [t for t in self._threads if t.is_alive()]
                    max_threads = max(32, self._max_concurrent_conns * 4)
                    if len(live_threads) >= max_threads:
                        self.rejected_connections += 1
                        too_busy = True
                    else:
                        too_busy = False
                        self._sockets.add(c)
                if too_busy:
                    self._send_http_error(c, "503 Service Unavailable", "Proxy handler limit reached")
                    continue
                ht = threading.Thread(target=self._handle_client, args=(c,), daemon=True)
                with self._lock:
                    self._threads = live_threads + [ht]
                ht.start()

        self._accept_thread = threading.Thread(target=_accept_loop, daemon=True, name="vimona-search-proxy")
        self._accept_thread.start()
        return self.port

    def stop(self) -> None:
        self._stop_ev.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None
        with self._lock:
            socks = list(self._sockets)
            self._sockets.clear()
            threads = list(self._threads)
            self._threads.clear()
        for sk in socks:
            try:
                sk.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sk.close()
            except OSError:
                pass
        if self._accept_thread is not None and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=1.0)
        for t in threads:
            if t.is_alive():
                t.join(timeout=0.5)


def sniff_image_format(raw_bytes: bytes) -> str | None:
    """Identify raster image format strictly from binary magic bytes (`F8`)."""
    if len(raw_bytes) < 12:
        return None
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
        return "webp"
    return None


class _EphemeralDirLock:
    """Tracks verified exclusive lock ownership for one ephemeral research directory (`B4`)."""

    def __init__(self, fh: Any = None, *, acquired: bool = False) -> None:
        self.fh = fh
        self.acquired = bool(acquired)

    def release(self) -> None:
        fh = self.fh
        self.fh = None
        was_acquired = self.acquired
        self.acquired = False
        if fh is not None:
            if was_acquired:
                try:
                    unlock_file(fh)
                except OSError:
                    pass
            try:
                fh.close()
            except OSError:
                pass


def _alloc_ephemeral_ram_dir(
    *, allow_disk_fallback: bool = False, worker_tag: str = ""
) -> tuple[Path, _EphemeralDirLock, str, dict[str, Any]]:
    """Create a private `0700` fail-closed flock-locked dir on `/dev/shm` tmpfs (`B1`, `B4`)."""
    safe_tag = re.sub(r"[^A-Za-z0-9_-]+", "", worker_tag or "")[:24]
    dir_prefix = f"vimona_ephem_{os.getpid()}_{safe_tag}_" if safe_tag else f"vimona_ephem_{os.getpid()}_"
    use_shm = SHM_DIR.is_dir() and os.access(SHM_DIR, os.W_OK)
    if use_shm:
        risk = detect_ram_persistence_risk(SHM_DIR)
        if not risk["is_ram_backed"] and not allow_disk_fallback:
            raise RuntimeError(
                f"{SHM_DIR} is backed by '{risk['fs_type']}' rather than tmpfs/ramfs; refusing to run without opt-in."
            )
        p = Path(tempfile.mkdtemp(prefix=dir_prefix, dir=str(SHM_DIR)))
        profile_kind = (
            "/dev/shm tmpfs"
            if risk["is_ram_backed"]
            else f"disk-backed {SHM_DIR} ({risk['fs_type']})"
        )
    else:
        if not allow_disk_fallback:
            raise RuntimeError(
                "/dev/shm is unavailable and disk-backed /tmp fallback is disabled for privacy."
            )
        tmp_base = Path(tempfile.gettempdir())
        p = Path(tempfile.mkdtemp(prefix=dir_prefix, dir=str(tmp_base)))
        risk = detect_ram_persistence_risk(p)
        profile_kind = (
            "OS temp (tmpfs)"
            if risk["is_ram_backed"]
            else f"DISK-BACKED OS temp ({risk['fs_type']} — NOT RAM-only)"
        )

    lock_fh = None
    lock_acquired = False
    try:
        os.chmod(p, 0o700)
        lock_fh = open(p / ".worker.lock", "a+b")
        lock_file_exclusive(lock_fh, nonblocking=True)
        lock_acquired = True
        (p / ".worker_owner.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "uid": os.getuid() if hasattr(os, "getuid") else 0,
                    "start_ticks": _read_proc_start_ticks(os.getpid()),
                    "worker_tag": safe_tag,
                    "lock_acquired": True,
                    "created_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        return p, _EphemeralDirLock(lock_fh, acquired=True), profile_kind, risk
    except Exception as exc:
        _EphemeralDirLock(lock_fh, acquired=lock_acquired).release()
        scorched_earth_wipe(p, owner_pid=os.getpid())
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
        raise RuntimeError(f"Failed to acquire exclusive ephemeral research lock for {p}: {exc}") from exc


def _describe_rgb_color(r: float, g: float, b: float) -> str:
    """Map an RGB triple (0-255) to a concise visual palette token."""
    mx = max(r, g, b)
    mn = min(r, g, b)
    if mx < 42:
        return "obsidian black"
    if mn > 215:
        return "crisp silver white"
    if (mx - mn) < 22:
        return "charcoal slate" if mx < 120 else "cool misty gray"
    if r >= g and r >= b:
        if g > 155 and b < 95:
            return "warm golden amber"
        if g > 110 and b > 140:
            return "neon magenta pink"
        return "crimson neon red" if mx > 165 else "deeprust crimson"
    if g >= r and g >= b:
        if b > 145:
            return "electric cyan teal"
        return "emerald neon green" if mx > 150 else "deep forest green"
    # b dominant
    if r > 145:
        return "electric violet purple"
    if g > 155:
        return "electric cyan blue"
    return "deep cobalt blue" if mx > 130 else "midnight navy blue"


def analyze_image_bytes_in_ram(
    raw_bytes: bytes,
    alt_text: str = "",
    page_title: str = "",
    query: str = "",
) -> dict[str, Any] | None:
    """Decode image purely from RAM bytes, extract lighting/palette tokens, & Safe-Blur flag."""
    import numpy as np
    from PIL import Image, ImageOps

    raw_fmt = sniff_image_format(raw_bytes)
    if raw_fmt is None:
        return None

    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if w < 180 or h < 180 or w > _MAX_IMAGE_DIM or h > _MAX_IMAGE_DIM or (w * h) > _MAX_IMAGE_PIXELS:
                return None
            # Reject extreme banner/divider strips (e.g. 10:1 nav bars)
            aspect = max(w / max(1, h), h / max(1, w))
            if aspect > 4.2:
                return None
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            if w > _MAX_IMAGE_DIM or h > _MAX_IMAGE_DIM or (w * h) > _MAX_IMAGE_PIXELS:
                return None

            rgb_im = im.convert("RGB")
            # Resize normalized reference copy in RAM (max edge 768px) for fast groundings & previews
            ref_im = rgb_im.copy()
            ref_im.thumbnail((768, 768), Image.Resampling.LANCZOS)
            ref_w, ref_h = ref_im.size
            out_buf = io.BytesIO()
            ref_im.save(out_buf, format="JPEG", quality=88)
            norm_b64 = base64.b64encode(out_buf.getvalue()).decode("ascii")

            # Fast 64x64 NumPy feature + YCbCr explicit-exposure inspection
            small = rgb_im.resize((64, 64), Image.Resampling.BILINEAR)
            arr = np.asarray(small, dtype=np.float32)
    except Exception:
        return None

    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    mean_luma = float(np.mean(luma))
    contrast = float(np.std(luma))
    max_rgb = np.maximum(np.maximum(r, g), b)
    min_rgb = np.minimum(np.minimum(r, g), b)
    sat = np.where(max_rgb > 1.0, (max_rgb - min_rgb) / np.maximum(max_rgb, 1.0), 0.0)
    mean_sat = float(np.mean(sat))

    # Dominant 4 quantized colors via spatial subsampling / Pillow median cut
    quant = small.quantize(colors=4, method=Image.Quantize.MEDIANCUT)
    pal_raw = quant.getpalette() or []
    counts = sorted(quant.getcolors() or [], reverse=True)
    palette_tags: list[str] = []
    seen_names: set[str] = set()
    for _cnt, idx in counts[:4]:
        base = idx * 3
        if base + 2 < len(pal_raw):
            cr, cg, cb = float(pal_raw[base]), float(pal_raw[base + 1]), float(pal_raw[base + 2])
            cname = _describe_rgb_color(cr, cg, cb)
            if cname not in seen_names:
                seen_names.add(cname)
                palette_tags.append(cname)

    # Lighting / mood analysis
    mood_parts: list[str] = []
    if mean_luma < 82:
        mood_parts.append("low-key nocturnal lighting")
    elif mean_luma > 175:
        mood_parts.append("bright high-key daylight")
    else:
        mood_parts.append("balanced cinematic lighting")

    if contrast > 64:
        mood_parts.append("high-contrast dramatic shadows")
    elif contrast < 36:
        mood_parts.append("soft diffused atmosphere")

    if mean_sat > 0.42:
        mood_parts.append("vivid saturated highlights")
    elif mean_sat < 0.14:
        mood_parts.append("muted desaturated tones")

    warm_bias = float(np.mean(r - b))
    if warm_bias > 22:
        mood_parts.append("warm tungsten amber cast")
    elif warm_bias < -20:
        mood_parts.append("cool cyan-blue atmosphere")

    lighting_mood = ", ".join(mood_parts[:3])

    # Dedicated SafeBlur Micro-ML Vision + Context classification BEFORE returning to UI
    combined_txt = f"{alt_text} {page_title} {query}".strip()
    is_explicit, _sc = get_safety_ml_engine().classify_image_rgb(
        np.clip(arr, 0, 255).astype(np.uint8),
        context_text=combined_txt,
    )

    style_tokens = ", ".join(
        p for p in [lighting_mood, ", ".join(palette_tags[:3]), _clean_text(alt_text, 90)] if p
    )
    return {
        "width": ref_w,
        "height": ref_h,
        "palette_tags": palette_tags,
        "lighting_mood": lighting_mood,
        "style_tokens": style_tokens,
        "explicit": is_explicit,
        "image_bytes_b64": norm_b64,
    }


def _extract_ddg_html_results(page: Any, limit: int = 5) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    cards = page.locator(".result")
    count = min(limit, cards.count())
    for i in range(count):
        card = cards.nth(i)
        title_loc = card.locator(".result__a").first
        snip_loc = card.locator(".result__snippet").first
        url_loc = card.locator(".result__url").first

        title = _clean_text(title_loc.inner_text() if title_loc.count() else "", 120)
        href = title_loc.get_attribute("href") if title_loc.count() else ""
        disp_url = _clean_text(url_loc.inner_text() if url_loc.count() else "", 120)
        snippet = _clean_text(snip_loc.inner_text() if snip_loc.count() else "", 280)

        if href and "uddg=" in href:
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if "uddg" in parsed and parsed["uddg"]:
                href = parsed["uddg"][0]

        final_url = href or disp_url
        if title and final_url:
            items.append(
                {
                    "title": title,
                    "url": final_url,
                    "takeaway": snippet or "Reference page inspected in /dev/shm sandbox.",
                }
            )
    return items


def _collect_page_candidate_images(page: Any, page_url: str, max_per_page: int = 4) -> list[dict[str, str]]:
    """Inspect a loaded page DOM for candidate visual references + alt metadata."""
    try:
        candidates = page.evaluate(
            """() => {
                const seen = new Set();
                const out = [];
                const push = (src, alt) => {
                    if (!src || src.startsWith('data:') || src.endsWith('.svg')) return;
                    const low = src.toLowerCase();
                    if (low.includes('favicon') || low.includes('logo') || low.includes('sprite') || low.includes('icon')) return;
                    if (seen.has(src)) return;
                    seen.add(src);
                    out.push({ src, alt: (alt || '').trim() });
                };
                for (const sel of [
                    "meta[property='og:image']",
                    "meta[name='twitter:image']",
                    "meta[property='og:image:secure_url']"
                ]) {
                    const el = document.querySelector(sel);
                    if (el && el.content) push(el.content, document.title || '');
                }
                const imgs = Array.from(document.querySelectorAll('img'));
                imgs.sort((a, b) => ((b.naturalWidth || 0) * (b.naturalHeight || 0)) - ((a.naturalWidth || 0) * (a.naturalHeight || 0)));
                for (const img of imgs) {
                    const src = img.currentSrc || img.getAttribute('src') || img.getAttribute('data-src') || '';
                    const alt = img.getAttribute('alt') || img.getAttribute('title') || document.title || '';
                    push(src, alt);
                }
                return out.slice(0, 12);
            }"""
        )
    except Exception:
        return []

    out: list[dict[str, str]] = []
    for item in candidates or []:
        raw_src = str(item.get("src", "")).strip()
        if not raw_src:
            continue
        abs_url = urllib.parse.urljoin(page_url, raw_src)
        if not abs_url.startswith(("http://", "https://")):
            continue
        out.append({"image_url": abs_url, "alt": _clean_text(str(item.get("alt", "")), 140)})
        if len(out) >= max_per_page:
            break
    return out


def _collect_ddg_image_results(
    page: Any,
    query: str,
    limit: int = 8,
    *,
    safesearch: str = "moderate",
) -> list[dict[str, str]]:
    """Query DuckDuckGo Images inside the isolated RAM tab with explicit SafeSearch (`F13`)."""
    encoded = urllib.parse.quote_plus(query)
    kp_val = _SAFESEARCH_KP_MAP.get(safesearch, "-1")
    url = f"https://duckduckgo.com/?q={encoded}&kp={kp_val}&iax=images&ia=images"
    candidates: list[dict[str, str]] = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1600)
        raw_items = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                for (const img of document.querySelectorAll('img')) {
                    let src = img.currentSrc || img.getAttribute('src') || img.getAttribute('data-src') || '';
                    if (!src) continue;
                    if (src.startsWith('//')) src = 'https:' + src;
                    if (!src.startsWith('http')) continue;
                    // DDG proxies thumbnails at external-content.duckduckgo.com/iu/?u=...
                    if (seen.has(src)) continue;
                    seen.add(src);
                    const alt = (img.getAttribute('alt') || '').trim();
                    out.push({ src, alt });
                }
                return out.slice(0, 16);
            }"""
        )
        for item in raw_items or []:
            src = str(item.get("src", ""))
            alt = _clean_text(str(item.get("alt", "")), 140)
            # Filter out tiny DDG UI badges
            if "duckduckgo.com/assets" in src or "duckduckgo.com/favicon" in src:
                continue
            candidates.append(
                {
                    "image_url": src,
                    "source_url": url,
                    "title": alt or f"Visual Reference: {query[:48]}",
                    "alt": alt or query,
                }
            )
            if len(candidates) >= limit:
                break
    except Exception:
        pass
    return candidates


def _execute_in_firefox(
    query: str = "",
    url: str = "",
    max_results: int = 4,
    pull_images: bool = True,
    max_images: int = 4,
    safesearch: str = "moderate",
    *,
    timeout_s: float = 55.0,
    worker_tag: str = "",
) -> dict[str, Any]:
    max_results = max(1, min(_MAX_RESULTS_CAP, int(max_results)))
    max_images = max(0, min(_MAX_IMAGES_CAP, int(max_images))) if pull_images else 0
    ss_mode = safesearch if safesearch in _SAFESEARCH_KP_MAP else "moderate"
    kp_val = _SAFESEARCH_KP_MAP[ss_mode]

    pinned_hosts: dict[str, set[str]] = {}
    if url:
        ok_u, err_u, _ips = validate_public_http_url(url, resolve_dns=True, pinned_hosts=pinned_hosts)
        if not ok_u:
            raise ValueError(f"Blocked URL ({err_u})")

    purge_legacy_burner_profile()
    disable_core_dumps()
    ephem_dir, ephem_lock, profile_kind, ram_risk = _alloc_ephemeral_ram_dir(worker_tag=worker_tag)
    if not isinstance(ephem_lock, _EphemeralDirLock) or not ephem_lock.acquired:
        scorched_earth_wipe(ephem_dir, owner_pid=os.getpid())
        if ephem_dir.exists():
            shutil.rmtree(ephem_dir, ignore_errors=True)
        raise RuntimeError(f"Exclusive ephemeral research lock was not acquired for {ephem_dir}")
    profile_dir = ephem_dir / "profile"
    ff_home = ephem_dir / "home"
    ff_cfg = ephem_dir / "xdg_config"
    ff_cache = ephem_dir / "xdg_cache"
    ff_data = ephem_dir / "xdg_data"
    ff_tmp = ephem_dir / "tmp"
    for d in (profile_dir, ff_home, ff_cfg, ff_cache, ff_data, ff_tmp):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass

    isolated_ff_env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().endswith(("_TOKEN", "_API_KEY", "_SECRET")) and not k.upper().startswith("AWS_")
    }
    isolated_ff_env.update(
        {
            "HOME": str(ff_home),
            "XDG_CONFIG_HOME": str(ff_cfg),
            "XDG_CACHE_HOME": str(ff_cache),
            "XDG_DATA_HOME": str(ff_data),
            "TMPDIR": str(ff_tmp),
            "MOZ_CRASHREPORTER_DISABLE": "1",
            "XRE_NO_CRASH_REPORTER": "1",
        }
    )

    results: list[dict[str, str]] = []
    references: list[dict[str, Any]] = []
    seen_img_urls: set[str] = set()
    seen_hashes: set[str] = set()
    browser_killed = False
    recorded_identities: dict[int, int] = {}
    wiped = False
    wipe_error = ""
    budget_state = {"bytes_used": 0, "nav_count": 0}

    def _try_pull_image(
        ctx: Any,
        img_url: str,
        source_url: str,
        title: str,
        alt_text: str,
    ) -> None:
        if len(references) >= max_images or img_url in seen_img_urls:
            return
        if budget_state["bytes_used"] >= _MAX_RESEARCH_BYTES:
            return
        ok_i, _err_i, _ips = validate_public_http_url(
            img_url, resolve_dns=True, pinned_hosts=pinned_hosts
        )
        if not ok_i:
            return
        seen_img_urls.add(img_url)
        try:
            remaining_budget = max(0, _MAX_RESEARCH_BYTES - budget_state["bytes_used"])
            fetched = fetch_public_image_pinned(
                img_url,
                pinned_hosts=pinned_hosts,
                max_redirects=3,
                max_bytes=min(_MAX_IMAGE_BYTES, remaining_budget),
                timeout_s=7.0,
            )
            if fetched is None:
                return
            raw_bytes, final_img_url = fetched
            if not raw_bytes or len(raw_bytes) < 4096 or len(raw_bytes) > _MAX_IMAGE_BYTES:
                return
            if (budget_state["bytes_used"] + len(raw_bytes)) > _MAX_RESEARCH_BYTES:
                return
            # Check magic bytes directly instead of trusting HTTP Content-Type header (`F8`)
            if sniff_image_format(raw_bytes) is None:
                return
            budget_state["bytes_used"] += len(raw_bytes)
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if digest in seen_hashes:
                return
            feat = analyze_image_bytes_in_ram(
                raw_bytes,
                alt_text=sanitize_untrusted_web_text(alt_text, 140),
                page_title=sanitize_untrusted_web_text(title, 120),
                query=query,
            )
            if feat is None:
                return
            norm_digest = hashlib.sha256(feat["image_bytes_b64"].encode("ascii")).hexdigest()
            if norm_digest in seen_hashes:
                return
            seen_hashes.add(digest)
            seen_hashes.add(norm_digest)
            references.append(
                {
                    "id": f"ref-{len(references) + 1:02d}",
                    "title": sanitize_untrusted_web_text(title or alt_text or "Reference", 90),
                    "source_url": source_url,
                    "image_url": final_img_url,
                    "alt_text": sanitize_untrusted_web_text(alt_text, 140),
                    **feat,
                }
            )
        except Exception:
            return

    local_proxy = _ValidatingLocalProxy(
        pinned_hosts,
        deadline_s=max(5.0, float(timeout_s) - 3.0),
    )
    proxy_port = local_proxy.start()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            ctx = pw.firefox.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                firefox_user_prefs=_MEMORY_ONLY_FIREFOX_PREFS,
                proxy={"server": f"http://127.0.0.1:{proxy_port}"},
                args=["-private"],
                env=isolated_ff_env,
            )
            # SSRF, redirect, DNS rebind, & resource-bloat route guard (`F1`–`F5`, `F10`)
            def _route_guard(route: Any) -> None:
                req = route.request
                rtype = getattr(req, "resource_type", "")
                if rtype in ("media", "font", "websocket", "beacon", "eventsource"):
                    route.abort("blockedbyclient")
                    return
                if req.is_navigation_request():
                    if budget_state["nav_count"] >= _MAX_NAVIGATIONS:
                        route.abort("blockedbyclient")
                        return
                    budget_state["nav_count"] += 1
                ok_r, _err_r, _ = validate_public_http_url(
                    req.url, resolve_dns=True, pinned_hosts=pinned_hosts
                )
                if not ok_r:
                    route.abort("blockedbyclient")
                    return
                route.continue_()

            ctx.route("**/*", _route_guard)
            recorded_identities.update(
                {
                    p: st
                    for p, (_u, st) in collect_descendant_identities(os.getpid()).items()
                    if p != os.getpid()
                }
            )
            try:
                page = ctx.new_page()
                page.set_default_timeout(14000)

                if url:
                    page.goto(url, wait_until="domcontentloaded", timeout=18000)
                    final_page_url = page.url or url
                    ok_post, err_post, _ = validate_public_http_url(
                        final_page_url, resolve_dns=True, pinned_hosts=pinned_hosts
                    )
                    if not ok_post:
                        raise ValueError(f"Blocked redirected destination ({err_post})")
                    title = sanitize_untrusted_web_text(page.title(), 120) or final_page_url
                    body_text = ""
                    try:
                        body_text = page.locator("main, article, body").first.inner_text()[:8000]
                    except Exception:
                        pass
                    results.append(
                        {
                            "title": title,
                            "url": final_page_url,
                            "takeaway": sanitize_untrusted_web_text(body_text, 420) or "Fetched page.",
                        }
                    )
                    if pull_images and not local_proxy.budget_exceeded:
                        for cand in _collect_page_candidate_images(page, final_page_url, max_per_page=max_images * 2):
                            if local_proxy.budget_exceeded:
                                break
                            _try_pull_image(ctx, cand["image_url"], final_page_url, title, cand["alt"])
                            if len(references) >= max_images:
                                break

                elif query:
                    encoded = urllib.parse.quote_plus(query)
                    ddg_url = f"https://html.duckduckgo.com/html/?q={encoded}&kp={kp_val}"
                    page.goto(ddg_url, wait_until="domcontentloaded", timeout=18000)
                    results = [
                        {
                            "title": sanitize_untrusted_web_text(r.get("title", ""), 120),
                            "url": r.get("url", ""),
                            "takeaway": sanitize_untrusted_web_text(r.get("takeaway", ""), 280),
                        }
                        for r in _extract_ddg_html_results(page, limit=max_results)
                        if validate_public_http_url(r.get("url", ""), resolve_dns=False)[0]
                    ]

                    if not results and not local_proxy.budget_exceeded:
                        page.goto(
                            f"https://duckduckgo.com/?q={encoded}&kp={kp_val}&ia=web",
                            wait_until="domcontentloaded",
                            timeout=18000,
                        )
                        page.wait_for_timeout(1500)
                        links = page.locator("article h2 a")
                        for idx in range(min(max_results, links.count())):
                            a = links.nth(idx)
                            href = a.get_attribute("href") or ""
                            title = sanitize_untrusted_web_text(a.inner_text(), 120)
                            if title and href and validate_public_http_url(href, resolve_dns=False)[0]:
                                results.append(
                                    {
                                        "title": title,
                                        "url": href,
                                        "takeaway": f"Fetched from DuckDuckGo (SafeSearch {ss_mode}).",
                                    }
                                )

                    if pull_images and not local_proxy.budget_exceeded:
                        for tab in results[:3]:
                            if len(references) >= max_images or local_proxy.budget_exceeded:
                                break
                            tab_url = tab.get("url", "")
                            ok_tu, _eu, _ = validate_public_http_url(
                                tab_url, resolve_dns=True, pinned_hosts=pinned_hosts
                            )
                            if not ok_tu:
                                continue
                            try:
                                page.goto(tab_url, wait_until="domcontentloaded", timeout=10000)
                                if local_proxy.budget_exceeded:
                                    break
                                post_u = page.url or tab_url
                                if not validate_public_http_url(post_u, resolve_dns=True, pinned_hosts=pinned_hosts)[0]:
                                    continue
                                page.wait_for_timeout(450)
                                page_title = sanitize_untrusted_web_text(page.title(), 120) or tab.get("title", "")
                                for cand in _collect_page_candidate_images(page, post_u, max_per_page=3):
                                    if local_proxy.budget_exceeded:
                                        break
                                    _try_pull_image(
                                        ctx,
                                        cand["image_url"],
                                        post_u,
                                        page_title,
                                        cand["alt"],
                                    )
                                    if len(references) >= max_images:
                                        break
                            except Exception:
                                continue

                        if len(references) < max_images and not local_proxy.budget_exceeded:
                            for cand in _collect_ddg_image_results(
                                page, query, limit=max_images * 3, safesearch=ss_mode
                            ):
                                if local_proxy.budget_exceeded:
                                    break
                                _try_pull_image(
                                    ctx,
                                    cand["image_url"],
                                    cand["source_url"],
                                    cand["title"],
                                    cand["alt"],
                                )
                                if len(references) >= max_images:
                                    break
            finally:
                for p, (_u, st) in collect_descendant_identities(os.getpid()).items():
                    if p != os.getpid():
                        recorded_identities.setdefault(p, st)
                try:
                    ctx.close()
                except Exception:
                    pass
                # Wait explicitly for browser/child processes to exit before wiping files (`B6`)
                exited = True
                for bpid, bst in list(recorded_identities.items()):
                    if not kill_process_tree_verified(
                        bpid,
                        root_start_ticks=bst,
                        term_timeout_s=2.5,
                        kill_timeout_s=1.5,
                    ):
                        exited = False
                browser_killed = exited
    finally:
        local_proxy.stop()
        ephem_lock.release()
        wiped = scorched_earth_wipe(
            ephem_dir,
            owner_pid=os.getpid(),
            allowed_identities=recorded_identities,
            wait_timeout_s=4.0,
        )
        if not wiped or ephem_dir.exists():
            diag = get_last_wipe_diagnostics()
            wipe_error = str(diag.get("error") or f"Cleanup verification failed: remnants remain at {ephem_dir}")
            wiped = False

    wiped_verified = bool(wiped and not ephem_dir.exists())
    return {
        "query": query or url,
        "safesearch": f"{ss_mode} (kp={kp_val})",
        "results": results,
        "references": references,
        "trace_report": {
            "profile_kind": profile_kind,
            "storage_path": str(ephem_dir),
            "fs_type": ram_risk.get("fs_type", "unknown"),
            "ram_backed": bool(ram_risk.get("is_ram_backed", False)),
            "ram_persistence_risk": ram_risk,
            "temp_dir": str(ephem_dir),
            "browser_killed": browser_killed,
            "wiped": wiped_verified,
            "cleanup_status": "complete" if (wiped_verified and browser_killed) else "failed",
            "wipe_error": wipe_error,
            "bytes_budget_used": budget_state["bytes_used"],
            "proxy_bytes_used": local_proxy.total_bytes,
            "proxy_connections": local_proxy.total_connections,
            "proxy_http_connections": local_proxy.http_connections,
            "proxy_connect_tunnels": local_proxy.connect_tunnels,
            "proxy_rejected_connections": local_proxy.rejected_connections,
            "proxy_budget_exceeded": local_proxy.budget_exceeded,
            "proxy_limit_reason": local_proxy.limit_reason,
            "safesearch": ss_mode,
            "images_extracted": len(references),
        },
    }


def _build_cleanup_trace(
    sweep_res: dict[str, Any],
    *,
    browser_killed: bool,
    ss_mode: str,
    worker_pid: int | None = None,
) -> dict[str, Any]:
    """Assemble honest privacy cleanup report without claiming `wiped=True` on skipped/errored sweeps (`B7`)."""
    surviving = [int(p) for p in sweep_res.get("surviving_pids", []) if int(p) > 0]
    if not browser_killed and worker_pid and int(worker_pid) not in surviving:
        surviving.append(int(worker_pid))
    errors = list(sweep_res.get("errors", []))
    skipped = list(sweep_res.get("skipped", []))
    remaining = list(sweep_res.get("target_dirs_remaining", []))
    raw_status = str(sweep_res.get("status") or ("complete" if sweep_res.get("ok") else "failed"))

    if not browser_killed or raw_status == "failed" or errors or remaining:
        cleanup_status = "failed"
    elif raw_status == "partial" or skipped:
        cleanup_status = "partial"
    else:
        cleanup_status = "complete"

    wiped = bool(browser_killed and cleanup_status == "complete" and not skipped and not errors and not remaining)
    err_parts = [str(e) for e in errors if e]
    if not browser_killed:
        err_parts.append(f"Worker process {worker_pid or '?'} or descendant survived termination")
    if skipped:
        err_parts.append(f"Skipped {len(skipped)} live/locked research dir(s)")
    if remaining:
        err_parts.append(f"Target dir(s) still remain: {', '.join(remaining)}")

    return {
        "wiped": wiped,
        "cleanup_status": cleanup_status,
        "wipe_error": "; ".join(err_parts),
        "browser_killed": bool(browser_killed),
        "safesearch": ss_mode,
        "images_extracted": 0,
        "skipped_dirs": skipped,
        "surviving_pids": sorted(set(surviving)),
        "remaining_dirs": remaining,
    }


def run_ephemeral_research(
    query: str = "",
    url: str = "",
    max_results: int = 4,
    pull_images: bool = True,
    max_images: int = 4,
    timeout_s: float = 55.0,
    safesearch: str = "moderate",
    cancel_event: Any = None,
) -> dict[str, Any]:
    """Run ephemeral Firefox research with SSRF/port/budget protection & ownership-safe wipe (`F1`–`F15`)."""
    max_results = max(1, min(_MAX_RESULTS_CAP, int(max_results)))
    max_images = max(0, min(_MAX_IMAGES_CAP, int(max_images)))
    ss_mode = safesearch if safesearch in _SAFESEARCH_KP_MAP else "moderate"

    if url:
        ok_u, err_u, _ = validate_public_http_url(url, resolve_dns=True)
        if not ok_u:
            return {
                "query": url,
                "error": f"Blocked destination URL: {err_u}",
                "results": [],
                "references": [],
                "trace_report": {
                    "wiped": True,
                    "cleanup_status": "complete",
                    "browser_killed": True,
                    "images_extracted": 0,
                    "safesearch": ss_mode,
                },
            }

    purge_legacy_burner_profile()
    if not COMFY_VENV_PYTHON.is_file():
        return {
            "query": query or url,
            "error": f"Python interpreter not found at {COMFY_VENV_PYTHON}",
            "results": [],
            "references": [],
            "trace_report": {
                "wiped": True,
                "cleanup_status": "complete",
                "browser_killed": True,
                "images_extracted": 0,
                "safesearch": ss_mode,
            },
        }

    worker_tag = f"w{os.getpid()}_{uuid.uuid4().hex[:10]}"
    cmd = [
        str(COMFY_VENV_PYTHON),
        "-m",
        "vimona.search_worker",
        "--max-results",
        str(max_results),
        "--max-images",
        str(max_images),
        "--safesearch",
        ss_mode,
        "--worker-tag",
        worker_tag,
        "--timeout",
        str(max(1.0, float(timeout_s))),
    ]
    if not pull_images:
        cmd.append("--no-images")
    if url:
        cmd.extend(["--url", url])
    else:
        cmd.extend(["--query", query])

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    worker_start_ticks = _read_proc_start_ticks(proc.pid)
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_overflow = threading.Event()

    def _drain_pipe(pipe: Any, target: bytearray, cap: int, overflow_ev: threading.Event | None) -> None:
        if pipe is None:
            return
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                if len(target) < cap:
                    remaining = cap - len(target)
                    target.extend(chunk[:remaining])
                    if len(chunk) > remaining and overflow_ev is not None:
                        overflow_ev.set()
                elif overflow_ev is not None:
                    overflow_ev.set()
        except Exception:
            pass

    t_out = threading.Thread(
        target=_drain_pipe,
        args=(proc.stdout, stdout_buf, _MAX_WORKER_STDOUT_BYTES, stdout_overflow),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_drain_pipe,
        args=(proc.stderr, stderr_buf, _MAX_WORKER_STDERR_BYTES, None),
        daemon=True,
    )
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + max(0.2, float(timeout_s))
    cancelled = False
    timed_out = False
    overflowed = False
    try:
        while proc.poll() is None:
            if stdout_overflow.is_set():
                overflowed = True
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)

        if cancelled or timed_out or overflowed:
            killed_ok = kill_process_tree_verified(
                proc.pid,
                root_start_ticks=worker_start_ticks,
                term_timeout_s=2.0,
                kill_timeout_s=1.5,
            )
            try:
                proc.wait(timeout=1.5)
            except Exception:
                pass
            t_out.join(timeout=1.5)
            t_err.join(timeout=1.0)
            sweep_res = _sweep_orphan_shm_profiles(
                worker_tag=worker_tag,
                owner_pid=proc.pid,
                owner_start_ticks=worker_start_ticks,
            )
            if overflowed:
                err_msg = "Research worker output exceeded maximum size limit."
            elif cancelled:
                err_msg = "Research cancelled."
            else:
                err_msg = "Ephemeral Firefox worker timed out."
            return {
                "query": query or url,
                "error": err_msg,
                "results": [],
                "references": [],
                "trace_report": _build_cleanup_trace(
                    sweep_res,
                    browser_killed=killed_ok,
                    ss_mode=ss_mode,
                    worker_pid=proc.pid,
                ),
            }

        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
        t_out.join(timeout=3.0)
        t_err.join(timeout=2.0)
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass

    if stdout_overflow.is_set():
        killed_ov = kill_process_tree_verified(
            proc.pid,
            root_start_ticks=worker_start_ticks,
            term_timeout_s=2.0,
            kill_timeout_s=1.5,
        )
        sweep_res = _sweep_orphan_shm_profiles(
            worker_tag=worker_tag,
            owner_pid=proc.pid,
            owner_start_ticks=worker_start_ticks,
        )
        return {
            "query": query or url,
            "error": "Research worker output exceeded maximum size limit.",
            "results": [],
            "references": [],
            "trace_report": _build_cleanup_trace(
                sweep_res,
                browser_killed=killed_ov,
                ss_mode=ss_mode,
                worker_pid=proc.pid,
            ),
        }

    stdout = stdout_buf.decode("utf-8", errors="replace")
    stderr = stderr_buf.decode("utf-8", errors="replace")

    sweep_res = _sweep_orphan_shm_profiles(
        worker_tag=worker_tag,
        owner_pid=proc.pid,
        owner_start_ticks=worker_start_ticks,
    )
    if proc.returncode != 0:
        return {
            "query": query or url,
            "error": _clean_text(stderr, 260) or f"Worker exited with code {proc.returncode}",
            "results": [],
            "references": [],
            "trace_report": _build_cleanup_trace(
                sweep_res,
                browser_killed=(proc.poll() is not None),
                ss_mode=ss_mode,
                worker_pid=proc.pid,
            ),
        }

    try:
        parsed_out = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return {
            "query": query or url,
            "error": "Invalid JSON returned from ephemeral worker.",
            "results": [],
            "references": [],
            "trace_report": _build_cleanup_trace(
                sweep_res,
                browser_killed=(proc.poll() is not None),
                ss_mode=ss_mode,
                worker_pid=proc.pid,
            ),
        }

    if isinstance(parsed_out, dict):
        tr = parsed_out.setdefault("trace_report", {})
        if isinstance(tr, dict):
            temp_dir_str = str(tr.get("temp_dir") or "")
            dir_absent = (not Path(temp_dir_str).exists()) if temp_dir_str else bool(tr.get("wiped"))
            sweep_status = str(sweep_res.get("status") or ("complete" if sweep_res.get("ok") else "failed"))
            if not dir_absent and temp_dir_str and temp_dir_str not in sweep_res.get("target_dirs_remaining", []):
                sweep_res.setdefault("target_dirs_remaining", []).append(temp_dir_str)
            tr["wiped"] = bool(
                tr.get("wiped")
                and tr.get("browser_killed")
                and dir_absent
                and not sweep_res.get("target_dirs_remaining")
            )
            tr["skipped_dirs"] = sweep_res.get("skipped", [])
            tr["surviving_pids"] = sweep_res.get("surviving_pids", [])
            if not tr["wiped"] or sweep_status == "failed":
                tr["cleanup_status"] = "failed"
            elif sweep_status == "partial":
                tr["cleanup_status"] = "partial"
            else:
                tr["cleanup_status"] = "complete"
    return parsed_out


def _sweep_orphan_shm_profiles(
    *,
    worker_tag: str = "",
    owner_pid: int | None = None,
    owner_start_ticks: int | None = None,
    base_dirs: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """Safely sweep dead/unlocked `vimona_ephem_*` dirs and report `complete`|`partial`|`failed` (`B3`, `B4`, `B7`)."""
    my_uid = os.getuid() if hasattr(os, "getuid") else 0
    errors: list[str] = []
    removed: list[str] = []
    skipped: list[dict[str, Any]] = []
    surviving_pids: list[int] = []
    target_dirs_remaining: list[str] = []
    safe_tag = re.sub(r"[^A-Za-z0-9_-]+", "", worker_tag or "")[:24]

    scan_bases = base_dirs if base_dirs is not None else (SHM_DIR, Path(tempfile.gettempdir()))
    for base in scan_bases:
        if not base.exists():
            continue
        if not base.is_dir():
            errors.append(f"Cleanup scan base is not a directory: {base}")
            continue
        try:
            entries = list(base.glob("vimona_ephem_*"))
        except OSError as exc:
            errors.append(f"Scan failed for {base}: {exc}")
            continue

        for item in entries:
            try:
                st = item.lstat()
                if st.st_uid != my_uid or item.is_symlink() or not item.is_dir():
                    continue
                owner_p = item / ".worker_owner.json"
                item_pid: int | None = None
                item_ticks: int | None = None
                item_tag = ""
                if owner_p.is_file():
                    try:
                        meta = json.loads(owner_p.read_text(encoding="utf-8"))
                        raw_pid = int(meta.get("pid") or 0)
                        if raw_pid > 1:
                            item_pid = raw_pid
                        item_ticks = meta.get("start_ticks")
                        item_tag = str(meta.get("worker_tag") or "")
                    except Exception:
                        pass

                is_target = bool(
                    (safe_tag and item_tag == safe_tag)
                    or (safe_tag and f"_{safe_tag}_" in item.name)
                    or (owner_pid is not None and item_pid == owner_pid)
                )

                # Never interfere with active concurrent worker holding .worker.lock
                lock_p = item / ".worker.lock"
                if lock_p.exists():
                    with open(lock_p, "r+b") as fh:
                        try:
                            lock_file_exclusive(fh, nonblocking=True)
                            unlock_file(fh)
                        except BlockingIOError:
                            skipped.append({"path": str(item), "reason": "lock_held", "owner_pid": item_pid})
                            if item_pid is not None:
                                surviving_pids.append(item_pid)
                            if is_target:
                                target_dirs_remaining.append(str(item))
                            continue

                if item_pid is not None and is_pid_alive_with_start(item_pid, item_ticks):
                    skipped.append({"path": str(item), "reason": "owner_alive", "owner_pid": item_pid})
                    surviving_pids.append(item_pid)
                    if is_target:
                        target_dirs_remaining.append(str(item))
                    continue

                if not owner_p.is_file() and not is_target and (time.time() - st.st_mtime) < 15.0:
                    skipped.append({"path": str(item), "reason": "startup_grace", "owner_pid": None})
                    continue

                id_map = {item_pid: int(item_ticks)} if (item_pid is not None and item_ticks is not None) else None
                if scorched_earth_wipe(item, owner_pid=item_pid, allowed_identities=id_map) and not item.exists():
                    removed.append(str(item))
                else:
                    diag = get_last_wipe_diagnostics()
                    diag_pids = [int(p) for p in diag.get("surviving_pids", []) if int(p) > 0]
                    surviving_pids.extend(diag_pids)
                    errors.append(str(diag.get("error") or f"Failed orphan cleanup: {item}"))
                    if is_target or item.exists():
                        target_dirs_remaining.append(str(item))
            except OSError as exc:
                errors.append(f"{item}: {exc}")

    if owner_pid is not None and owner_pid > 1 and owner_start_ticks is not None:
        if is_pid_alive_with_start(owner_pid, owner_start_ticks):
            surviving_pids.append(owner_pid)

    if errors or target_dirs_remaining:
        status = "failed"
    elif skipped:
        status = "partial"
    else:
        status = "complete"

    return {
        "ok": status == "complete",
        "status": status,
        "removed": removed,
        "skipped": skipped,
        "errors": errors,
        "surviving_pids": sorted({int(p) for p in surviving_pids if int(p) > 0}),
        "target_dirs_remaining": target_dirs_remaining,
    }


# Compatibility alias for existing callers
run_firefox_search = run_ephemeral_research


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vimona ephemeral Firefox reference worker")
    parser.add_argument("--query", default="", help="Search query")
    parser.add_argument("--url", default="", help="Direct public HTTP/HTTPS URL to inspect")
    parser.add_argument("--max-results", type=int, default=4)
    parser.add_argument("--max-images", type=int, default=4)
    parser.add_argument("--safesearch", choices=("strict", "moderate", "off"), default="moderate")
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("--worker-tag", default="")
    parser.add_argument("--timeout", type=float, default=55.0)
    args = parser.parse_args(argv)

    max_res = max(1, min(_MAX_RESULTS_CAP, int(args.max_results)))
    max_img = max(0, min(_MAX_IMAGES_CAP, int(args.max_images)))
    try:
        payload = _execute_in_firefox(
            query=args.query,
            url=args.url,
            max_results=max_res,
            pull_images=not args.no_images,
            max_images=max_img,
            safesearch=args.safesearch,
            timeout_s=args.timeout,
            worker_tag=args.worker_tag,
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
