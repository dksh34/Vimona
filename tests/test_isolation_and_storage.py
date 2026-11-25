"""Tests for kernel network/mount isolation, fail-closed checks, and atomic/concurrent storage (`A1`–`A12`, `B1`–`B12`, `I1`–`I10`, `N7`, `N8`)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import threading
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
                ag.terminate_cleanly()
                self.assertEqual(ag.isolation_status.state, "stopped")
                if ag.sock_dir is not None:
                    self.assertFalse(ag.sock_dir.exists())


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
