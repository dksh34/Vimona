"""Tests for RAM process verification, descendant cleanup, and relay mutual exclusion (`E1`–`E13`, `K1`–`K12`, `N6`, `N10`)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from vimona import memory_relay, ram_manager


class TestRamProcessLifecycle(unittest.TestCase):
    def test_protected_pid_remains_locked_until_dead_and_start_ticks_validated(self) -> None:
        p = subprocess.Popen(["python3", "-c", "import time; time.sleep(15)"])
        try:
            ram_manager.register_protected_pid(p.pid, "Test model engine")
            prot_map = ram_manager._get_protected_pid_map()
            self.assertIn(p.pid, prot_map)

            live_ok, reason = ram_manager._is_pid_protected_live(p.pid)
            self.assertTrue(live_ok)
            self.assertIn("Test model engine", reason)

            proc_usage = ram_manager.ProcessRamUsage(
                pid=p.pid,
                name="python3",
                cmd_preview="python3 sleep",
                rss_mb=32.0,
                uid=os.getuid(),
                protected=True,
                start_ticks=ram_manager._read_pid_start_ticks(p.pid),
            )
            killed, msg = ram_manager.safe_kill_process(proc_usage)
            self.assertFalse(killed)
            self.assertIn("Protected", msg)
        finally:
            ram_manager.unregister_protected_pid(p.pid)
            p.terminate()
            p.wait(timeout=5)

    def test_pid_reuse_start_ticks_mismatch_aborts_safely(self) -> None:
        p = subprocess.Popen(["python3", "-c", "import time; time.sleep(15)"])
        try:
            real_ticks = ram_manager._read_pid_start_ticks(p.pid)
            self.assertIsNotNone(real_ticks)
            proc_stale = ram_manager.ProcessRamUsage(
                pid=p.pid,
                name="python3",
                cmd_preview="python3 sleep",
                rss_mb=10.0,
                uid=os.getuid(),
                protected=False,
                start_ticks=(real_ticks or 100) + 999999,
            )
            ok, msg = ram_manager.safe_kill_process(proc_stale)
            self.assertFalse(ok)
            self.assertIn("reused", msg.lower())
            self.assertIsNone(p.poll())
        finally:
            p.terminate()
            p.wait(timeout=5)

    def test_system_process_and_exec_candidate_parsing(self) -> None:
        # Freedesktop field codes and quoted spaces parsed safely (`K5`)
        cands = ram_manager._extract_exec_candidates('"/usr/bin/my app" --flag=%U')
        self.assertIn("my app", cands)
        # Missing comm treated conservatively as system-protected (`K8`)
        self.assertTrue(ram_manager._is_system_process("", "", [], ""))
        self.assertTrue(ram_manager._is_system_process("systemd", "/usr/lib/systemd/systemd", ["systemd"], "user.slice"))


class TestMemoryRelayMutualExclusion(unittest.TestCase):
    def test_terminate_proc_group_kills_child_descendants_and_reaps(self) -> None:
        child_script = (
            "import subprocess, time, sys\n"
            "c = subprocess.Popen(['python3', '-c', 'import time; time.sleep(30)'])\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        parent = subprocess.Popen([sys.executable, "-c", child_script], stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            assert parent.stdout is not None
            child_pid = int((parent.stdout.readline() or "0").strip())
        finally:
            if parent.stdout is not None:
                parent.stdout.close()
        self.assertGreater(child_pid, 0)
        try:
            metrics = memory_relay._terminate_proc_group(parent, "test-engine", max_handoff_timeout_s=5.0)
            self.assertTrue(metrics.engine_exited)
            self.assertTrue(metrics.descendants_gone)
            self.assertIsNotNone(parent.poll())
            # Wait briefly for kernel reaper if child needed SIGKILL
            deadline = time.monotonic() + 1.5
            while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(Path(f"/proc/{child_pid}").exists())
        finally:
            try:
                os.kill(child_pid, 9)
            except OSError:
                pass
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=2)

    def test_concurrent_engine_slot_rejection_and_dead_process_recovery(self) -> None:
        mgr = memory_relay.MemoryRelayManager()
        # Hold _engine_slot in another thread -> chat_completion rejects cleanly (`E8`)
        acquired = mgr._engine_slot.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with self.assertRaises(memory_relay.HandoffError):
                mgr.chat_completion([{"role": "user", "content": "hi"}])
        finally:
            mgr._engine_slot.release()

        # Unexpected dead child process state recovery (`E11`)
        dead_proc = subprocess.Popen(["python3", "-c", "pass"])
        dead_proc.wait(timeout=5)
        mgr._llama_proc = dead_proc
        mgr._owner = "text"
        mgr._reconcile_dead_processes()
        self.assertIsNone(mgr._llama_proc)
        self.assertEqual(mgr._owner, "idle")

        # Insufficient RAM raises InsufficientMemoryError cleanly (`E12`)
        with mock.patch("vimona.memory_relay.read_proc_meminfo", return_value=(15.5, 16.0, 0.25)):
            with self.assertRaises(memory_relay.InsufficientMemoryError):
                mgr._check_min_available_ram(2.0, "ComfyUI")


if __name__ == "__main__":
    unittest.main()
