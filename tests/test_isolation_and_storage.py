"""Tests for kernel network/mount isolation, fail-closed checks, and atomic/concurrent storage (`A1`–`A12`, `B1`–`B12`, `I1`–`I10`, `N7`, `N8`)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from vimona import airgap, storage


class TestNetworkIsolation(unittest.TestCase):
    def test_probe_unshare_fail_closed_when_binary_missing(self) -> None:
        with mock.patch("vimona.airgap.shutil.which", return_value=None):
            ok, reason = airgap.probe_unshare_netns_capability(force_refresh=True)
            self.assertFalse(ok)
            self.assertIn("unshare", reason.lower())
            with tempfile.TemporaryFile("w+") as log_fh:
                with self.assertRaises(airgap.AirGapIsolationError):
                    airgap.launch_airgapped_http_service(
                        cmd=["/bin/true"],
                        inner_port=18999,
                        outer_port=19000,
                        protection_label="test",
                        log_fh=log_fh,
                        allow_unisolated=False,
                    )
        airgap.probe_unshare_netns_capability(force_refresh=True)

    def test_real_netns_egress_block_and_authenticated_bridge_forwarder(self) -> None:
        ok, _ = airgap.probe_unshare_netns_capability(force_refresh=True)
        if not ok:
            self.skipTest("unshare -r -n unavailable in this environment")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s1:
            s1.bind(("127.0.0.1", 0))
            inner_port = int(s1.getsockname()[1])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
            s2.bind(("127.0.0.1", 0))
            outer_port = int(s2.getsockname()[1])

        py_srv = (
            "import http.server, socket, sys\n"
            f"port = {inner_port}\n"
            "try:\n"
            "  sock = socket.create_connection(('1.1.1.1', 53), timeout=0.4)\n"
            "  sock.close(); sys.stderr.write('EGRESS_LEAK\\n'); sys.exit(99)\n"
            "except OSError: pass\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "  def do_GET(self):\n"
            "    self.send_response(200); self.end_headers(); self.wfile.write(b'ok-isolated')\n"
            "  def log_message(self, *a): pass\n"
            "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
        )
        with tempfile.TemporaryFile("w+") as log_fh:
            ag = airgap.launch_airgapped_http_service(
                cmd=["python3", "-c", py_srv],
                inner_port=inner_port,
                outer_port=outer_port,
                protection_label="test-netns-http",
                log_fh=log_fh,
                handshake_timeout_s=12.0,
            )
            try:
                self.assertTrue(ag.airgapped)
                self.assertEqual(ag.isolation_status.state, "isolated")
                self.assertTrue(ag.isolation_status.ipv4_blocked)
                self.assertTrue(ag.isolation_status.dns_blocked)
                self.assertTrue(ag.isolation_status.mount_isolated)
                self.assertTrue(ag.isolation_status.no_new_privs)
                self.assertTrue(ag.isolation_status.core_dumps_disabled)
                self.assertIsNotNone(ag.sock_dir)
                self.assertEqual(ag.sock_dir.stat().st_mode & 0o777, 0o700)

                status_ok, body = ag.http_request("GET", "/ping", timeout=3.0)
                self.assertEqual(status_ok, 200)
                self.assertEqual(body, b"ok-isolated")

                # Unauthenticated request on host TCP forwarder must fail (`A6`)
                with socket.create_connection(("127.0.0.1", ag.outer_port), timeout=2.0) as raw_sk:
                    raw_sk.sendall(b"GET /ping HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                    resp_raw = raw_sk.recv(1024)
                self.assertIn(b"403", resp_raw)
            finally:
                self.assertTrue(ag.terminate_cleanly())
                self.assertEqual(ag.isolation_status.state, "stopped")
                if ag.sock_dir is not None:
                    self.assertFalse(ag.sock_dir.exists())

    def test_kill_process_tree_sigterm_ignored_descendants_and_pid_reuse_guard(self) -> None:
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux /proc process-tree identity test")
        parent_code = (
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "gc = subprocess.Popen([sys.executable, '-c', "
            "\"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)\"])\n"
            "print(gc.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        p = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True)
        try:
            assert p.stdout is not None
            grand_pid = int(p.stdout.readline().strip())
            parent_ticks = airgap._read_proc_start_ticks(p.pid)
            grand_ticks = airgap._read_proc_start_ticks(grand_pid)
            self.assertIsNotNone(parent_ticks)
            self.assertIsNotNone(grand_ticks)

            killed = airgap.kill_process_tree_verified(
                p.pid,
                root_start_ticks=parent_ticks,
                term_timeout_s=0.3,
                kill_timeout_s=2.0,
            )
            self.assertTrue(killed)
            p.wait(timeout=2.0)
            for _ in range(20):
                if not airgap.is_pid_alive_with_start(grand_pid, grand_ticks):
                    break
                time.sleep(0.05)
            self.assertFalse(airgap.is_pid_alive_with_start(grand_pid, grand_ticks))
        finally:
            if p.stdout is not None:
                p.stdout.close()
            if p.poll() is None:
                p.kill()
                p.wait(timeout=1.0)

        # PID-reuse guard refuses to signal when start_ticks does not match
        p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            real_ticks = airgap._read_proc_start_ticks(p2.pid)
            self.assertIsNotNone(real_ticks)
            ok_sig, reason = airgap._send_signal_verified(
                p2.pid,
                signal.SIGTERM,
                expected_uid=airgap.get_current_uid(),
                expected_start_ticks=int(real_ticks) + 999999,
            )
            self.assertFalse(ok_sig)
            self.assertIn("reused", reason.lower())
            self.assertIsNone(p2.poll())

            self.assertTrue(
                airgap.kill_process_tree_verified(
                    p2.pid,
                    root_start_ticks=real_ticks,
                    term_timeout_s=0.6,
                    kill_timeout_s=1.0,
                )
            )
            p2.wait(timeout=2.0)
        finally:
            if p2.poll() is None:
                p2.kill()
                p2.wait(timeout=1.0)

    def test_scorched_earth_wipe_and_terminate_cleanly_fail_closed_when_process_survives(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "ephem"
            target.mkdir()
            (target / "secret.bin").write_bytes(b"sensitive")

            with (
                mock.patch("vimona.airgap._find_verified_profile_pids_with_identities", return_value={999999: 123}),
                mock.patch("vimona.airgap.kill_process_tree_verified", return_value=False),
            ):
                ok_wipe = airgap.scorched_earth_wipe(target)
                self.assertFalse(ok_wipe)
                self.assertTrue(target.exists())
                diag = airgap.get_last_wipe_diagnostics()
                self.assertFalse(diag["ok"])
                self.assertTrue(diag["dir_exists"])
                self.assertIn(999999, diag["surviving_pids"])

            sock_dir = Path(td) / "ag_sock"
            sock_dir.mkdir()
            sock_path = sock_dir / "bridge.sock"
            fake_proc = mock.MagicMock()
            fake_proc.pid = 999998
            fake_proc.returncode = None
            fake_proc.poll.return_value = None
            ag = airgap.AirGappedProcess(
                proc=fake_proc,
                sock_path=sock_path,
                outer_port=19055,
                airgapped=True,
                isolation_status=airgap.IsolationStatus(state="isolated", level="user_net_mount_ns_rlimit"),
                sock_dir=sock_dir,
            )
            with mock.patch("vimona.airgap.kill_process_tree_verified", return_value=False):
                self.assertFalse(ag.terminate_cleanly())
                self.assertEqual(ag.isolation_status.state, "failed")
                self.assertTrue(bool(ag.last_cleanup_error))
                self.assertTrue(sock_dir.exists())

    def test_mandatory_isolation_controls_fail_closed_and_partial_opt_in(self) -> None:
        self.assertIn("SO_PEERCRED", airgap._AIRGAP_INNER_SUPERVISOR)
        self.assertIn("client.close()", airgap._AIRGAP_INNER_SUPERVISOR)

        def _launch_with_handshake(payload: dict, *, allow_partial: bool) -> airgap.AirGappedProcess:
            bound_socks: list[socket.socket] = []

            def _fake_popen(cmd, **kwargs):
                sock_arg = next(str(a) for a in cmd if str(a).endswith("bridge.sock"))
                pass_fds = tuple(kwargs.get("pass_fds") or ())
                self.assertTrue(pass_fds)
                ready_w = int(pass_fds[0])
                us = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                us.bind(sock_arg)
                us.listen(4)
                bound_socks.append(us)
                os.write(ready_w, (json.dumps(payload) + "\n").encode("utf-8"))
                dummy = mock.MagicMock()
                dummy.pid = 999997
                dummy.returncode = None
                dummy.poll.return_value = None
                dummy.wait.return_value = 0
                return dummy

            with (
                mock.patch("vimona.airgap.probe_unshare_netns_capability", return_value=(True, "ok")),
                mock.patch("vimona.airgap._can_use_mount_namespace", return_value=True),
                mock.patch("vimona.airgap.kill_process_tree_verified", return_value=True),
                mock.patch("vimona.airgap.subprocess.Popen", side_effect=_fake_popen),
                tempfile.TemporaryFile("w+") as log_fh,
            ):
                try:
                    return airgap.launch_airgapped_http_service(
                        cmd=["/bin/true"],
                        inner_port=18901,
                        outer_port=19901,
                        protection_label="test-mandatory",
                        log_fh=log_fh,
                        allow_partial_isolation=allow_partial,
                    )
                finally:
                    for s in bound_socks:
                        try:
                            s.close()
                        except OSError:
                            pass

        for bad_ctrl in ("mount_isolated", "no_new_privs", "core_dumps_disabled"):
            hdr = {
                "ready": True,
                "interfaces": ["lo"],
                "ipv4_blocked": True,
                "ipv6_blocked": True,
                "dns_blocked": True,
                "mount_isolated": True,
                "no_new_privs": True,
                "core_dumps_disabled": True,
            }
            hdr[bad_ctrl] = False
            with self.assertRaises(airgap.AirGapIsolationError):
                _launch_with_handshake(hdr, allow_partial=False)

        ag_partial = _launch_with_handshake(
            {
                "ready": True,
                "interfaces": ["lo"],
                "ipv4_blocked": True,
                "ipv6_blocked": True,
                "dns_blocked": True,
                "mount_isolated": False,
                "no_new_privs": True,
                "core_dumps_disabled": True,
            },
            allow_partial=True,
        )
        try:
            self.assertEqual(ag_partial.isolation_status.state, "partial")
            self.assertEqual(ag_partial.isolation_status.level, "user_net_ns_partial")
        finally:
            with mock.patch("vimona.airgap.kill_process_tree_verified", return_value=True):
                ag_partial.terminate_cleanly()


class TestStorageAndConcurrency(unittest.TestCase):
    def test_schema_v1_migration_backup_recovery_quarantine_and_concurrent_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / "state"
            ephem = root / "ephem"
            ephem.mkdir(parents=True, exist_ok=True)
            with (
                mock.patch.object(storage, "STATE_DIR", sdir),
                mock.patch.object(storage, "CHATS_FILE", sdir / "chats.json"),
                mock.patch.object(storage, "EPHEMERAL_MEDIA_DIR", ephem),
            ):
                sdir.mkdir(parents=True, exist_ok=True)
                v1 = [{"id": "c1", "title": "Legacy", "ask_mode": False, "turns": []}]
                storage.CHATS_FILE.write_text(json.dumps(v1), encoding="utf-8")

                loaded = storage.load_chats()
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0].title, "Legacy")

                storage.save_chats(loaded)
                self.assertEqual(sdir.stat().st_mode & 0o777, 0o700)
                self.assertEqual(storage.CHATS_FILE.stat().st_mode & 0o777, 0o600)
                # Second save populates .bak
                storage.save_chats(loaded)
                self.assertTrue((sdir / "chats.json.bak").is_file())

                # Concurrent threaded writes do not corrupt JSON
                errs: list[Exception] = []

                def _writer(idx: int) -> None:
                    try:
                        storage.save_chats([storage.ChatSession(id=f"c{idx}", title=f"T{idx}")])
                    except Exception as exc:
                        errs.append(exc)

                threads = [threading.Thread(target=_writer, args=(i,)) for i in range(6)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(errs, [])
                final_parsed = json.loads(storage.CHATS_FILE.read_text(encoding="utf-8"))
                self.assertEqual(final_parsed["schema_version"], storage.CHATS_SCHEMA_VERSION)

                # Corrupt file quarantined and restored from .bak
                storage.CHATS_FILE.write_text("{corrupt", encoding="utf-8")
                restored = storage.load_chats()
                self.assertGreaterEqual(len(restored), 1)
                self.assertEqual(storage.get_last_storage_status()["state"], "recovered_from_backup")
                self.assertTrue(list(sdir.glob("chats.corrupt.*.json")))

                # Boundary guard prevents deleting outside files
                outside = root / "dont_touch.txt"
                outside.write_text("safe", encoding="utf-8")
                self.assertFalse(storage.delete_ephemeral_file(str(outside)))
                self.assertTrue(outside.is_file())

                inside = ephem / "safe_to_delete.png"
                inside.write_bytes(b"123")
                self.assertTrue(storage.delete_ephemeral_file(str(inside)))
                self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()
