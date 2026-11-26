"""Tests for hardware profiling tiers and resumable/verified model downloads (`C1`–`C12`, `D1`–`D14`, `N5`, `N9`)."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from vimona import hf_models


def _make_fake_gguf_bytes(payload: bytes = b"TEST_WEIGHTS" * 128) -> bytes:
    # GGUF magic header + version 3 + tensor_count=0 + kv_count=0 + padded body
    return b"GGUF" + struct.pack("<IQQ", 3, 0, 0) + payload


class TestHardwareProfiles(unittest.TestCase):
    def test_conservative_low_memory_cpu_fallback_and_no_double_counting(self) -> None:
        hw_low = hf_models.HardwareProfile(
            cpu_name="Generic 4-Core",
            cpu_cores=4,
            total_ram_gb=8.0,
            avail_ram_gb=3.2,
            gpu_name="CPU Fallback",
            vram_gb=0.0,
            backend="cpu",
            unified_memory=True,
            gtt_gb=0.0,
            cpu_fallback_only=True,
        )
        # UMA must NOT double-count shared RAM + VRAM as separate pools (`D8`)
        self.assertLessEqual(hw_low.gpu_pool_gb, hw_low.effective_ram_gb)
        big_spec = next(m for m in hf_models.CURATED_HF_MODELS if m.size_gb >= 6.0)
        tier, _ = hf_models.evaluate_model_fit(big_spec, hw_low)
        self.assertEqual(tier, "heavy")

        hw_workstation = hf_models.HardwareProfile(
            cpu_name="Ryzen 9",
            cpu_cores=16,
            total_ram_gb=64.0,
            avail_ram_gb=48.0,
            gpu_name="NVIDIA RTX 4090",
            vram_gb=24.0,
            backend="cuda",
            unified_memory=False,
            gtt_gb=0.0,
            gpu_count=1,
            vulkan_verified=True,
            cuda_verified=True,
        )
        # Peak inference memory scales above disk size for KV cache & video frames (`D3`–`D6`, `D14`)
        small_llm = next(m for m in hf_models.CURATED_HF_MODELS if m.category == "text")
        peak_short = hf_models.estimate_peak_inference_memory_gb(small_llm, ctx_size=2048)
        peak_long = hf_models.estimate_peak_inference_memory_gb(small_llm, ctx_size=16384)
        self.assertGreater(peak_short, small_llm.size_gb)
        self.assertGreater(peak_long, peak_short)
        tier_ws, _ = hf_models.evaluate_model_fit(small_llm, hw_workstation)
        self.assertIn(tier_ws, ("best_fit", "fits"))

    def test_detect_hardware_fallback_when_proc_meminfo_missing(self) -> None:
        with (
            mock.patch.object(hf_models, "_HW_STATIC_CACHE", None),
            mock.patch("vimona.hf_models._read_total_and_avail_ram_gb", return_value=(6.0, 3.0)),
            mock.patch("vimona.hf_models._detect_gpu_details", return_value=("CPU Fallback", 0.0, "cpu", True, 0.0, 0, (), False, False)),
        ):
            hw = hf_models.detect_hardware_profile(force_refresh=True)
            self.assertEqual(hw.total_ram_gb, 6.0)
            self.assertEqual(hw.avail_ram_gb, 3.0)
            self.assertTrue(hw.cpu_fallback_only)
        hf_models.detect_hardware_profile(force_refresh=True)


class TestModelDownloadsAndVerification(unittest.TestCase):
    def test_format_validation_and_corrupt_header_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            text_spec = next(m for m in hf_models.CURATED_HF_MODELS if m.category == "text")
            vid_spec = next(m for m in hf_models.CURATED_HF_MODELS if m.category == "video")

            bad_gguf = Path(td) / "bad.gguf"
            bad_gguf.write_bytes(b"<!DOCTYPE html>404</html>" * 100)
            ok, reason = hf_models.is_file_compatible_with_backend(text_spec, bad_gguf)
            self.assertFalse(ok)
            self.assertIn("GGUF", reason)

            tiny_gguf = Path(td) / "tiny.gguf"
            tiny_gguf.write_bytes(b"GGUF")
            ok_tiny, reason_tiny = hf_models.is_file_compatible_with_backend(text_spec, tiny_gguf)
            self.assertFalse(ok_tiny)
            self.assertIn("truncated", reason_tiny.lower())

            good_gguf = Path(td) / "good.gguf"
            good_gguf.write_bytes(_make_fake_gguf_bytes())
            ok2, _ = hf_models.is_file_compatible_with_backend(text_spec, good_gguf)
            self.assertTrue(ok2)

            bad_st = Path(td) / "bad.safetensors"
            bad_st.write_bytes(struct.pack("<Q", 0) + b"{bad}")
            ok3, _ = hf_models.is_file_compatible_with_backend(vid_spec, bad_st)
            self.assertFalse(ok3)

    def test_download_sha256_resume_and_truncation_detection(self) -> None:
        data = _make_fake_gguf_bytes(b"VIMONA_PAYLOAD_" * 80000)
        good_sha = hashlib.sha256(data).hexdigest()

        class _RangeResp(io.BytesIO):
            def __init__(self, body: bytes, status: int, headers_dict: dict[str, str], url: str = "") -> None:
                super().__init__(body)
                self.status = status
                self.headers = headers_dict
                self._url = url

            def geturl(self) -> str:
                return self._url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            llm_dir = root / "llm"
            llm_dir.mkdir(parents=True, exist_ok=True)
            spec = hf_models.HFModelSpec(
                id="test-tiny-llm",
                name="Test Tiny LLM",
                category="text",
                hf_repo="unsloth/gemma-3-1b-it-GGUF",
                hf_file="test-model.gguf",
                size_gb=len(data) / (1024 ** 3),
                min_ram_gb=2.0,
                quant="Q4_K_M",
                summary="Test weight",
            )

            # Pre-seed partial .part file to test Range resume (`C5`)
            partial_len = len(data) // 2
            part_file = llm_dir / "test-model.gguf.part"
            part_file.write_bytes(data[:partial_len])

            def _fake_open(url_str, headers, *, timeout_s=30.0, max_redirects=6):
                rng = headers.get("Range")
                self.assertEqual(rng, f"bytes={partial_len}-")
                rest = data[partial_len:]
                return _RangeResp(
                    rest,
                    206,
                    {
                        "Content-Length": str(len(rest)),
                        "Content-Range": f"bytes {partial_len}-{len(data) - 1}/{len(data)}",
                    },
                    url=url_str,
                )

            with (
                mock.patch.object(hf_models, "_CFG", mock.MagicMock(llm_dir=llm_dir, models_dir=root / "m", comfy_dir=root / "c")),
                mock.patch.object(hf_models, "MODELS_STATE_FILE", root / "models_config.json"),
                mock.patch.object(hf_models, "LOCAL_MODELS_DIR", root / "m"),
                mock.patch("vimona.hf_models._search_roots_for_category", return_value=[llm_dir]),
                mock.patch("vimona.hf_models._open_validated_hf_download", side_effect=_fake_open),
            ):
                # Wrong checksum must reject cleanly (`C1`)
                with self.assertRaises(hf_models.ModelDownloadError):
                    hf_models.download_hf_model(
                        spec,
                        expected_sha256="0" * 64,
                        expected_bytes=len(data),
                    )
                # Re-seed .part for clean SHA-256 resumed install
                part_file.write_bytes(data[:partial_len])
                dest = hf_models.download_hf_model(
                    spec,
                    expected_sha256=good_sha,
                    expected_bytes=len(data),
                )
                self.assertTrue(dest.is_file())
                self.assertEqual(dest.read_bytes(), data)
                self.assertFalse(part_file.exists())

    def test_download_url_host_boundary_and_pre_connection_redirect_validation(self) -> None:
        # Strict DNS-label boundary checks reject deceptive suffix hosts, credentials, non-443 ports, & non-HTTPS
        blocked_urls = [
            "https://evilhuggingface.co/unsloth/model.gguf",
            "https://evilhf.co/model.gguf",
            "http://huggingface.co/unsloth/model.gguf",
            "https://user:pass@huggingface.co/unsloth/model.gguf",
            "https://huggingface.co:8443/unsloth/model.gguf",
            "https://cas-bridge.xethub.hf.co.attacker.example/file",
        ]
        for bad_u in blocked_urls:
            with self.assertRaises(hf_models.ModelDownloadError, msg=f"Should reject {bad_u}"):
                hf_models._validate_download_url(bad_u, resolve_dns=False)

        with mock.patch(
            "vimona.hf_models.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaises(hf_models.ModelDownloadError):
                hf_models._validate_download_url("https://huggingface.co/unsloth/model.gguf", resolve_dns=True)

        with mock.patch(
            "vimona.hf_models.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
        ):
            host, port, _pq, ips = hf_models._validate_download_url(
                "https://us.aws.cdn.hf.co/repos/weights.safetensors?download=true",
                resolve_dns=True,
            )
            self.assertEqual(host, "us.aws.cdn.hf.co")
            self.assertEqual(port, 443)
            self.assertEqual(ips, {"93.184.216.34"})

        # Redirect validation occurs BEFORE connecting to the redirect target
        for bad_redir in ("https://evil.example.com/pwn.gguf", "https://127.0.0.1/secret.gguf"):
            fake_resp = mock.MagicMock()
            fake_resp.status = 302
            fake_resp.getheader.side_effect = lambda name, _r=bad_redir: _r if str(name).lower() == "location" else ""
            fake_conn = mock.MagicMock()
            fake_conn.getresponse.return_value = fake_resp
            with (
                mock.patch(
                    "vimona.hf_models.socket.getaddrinfo",
                    return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
                ),
                mock.patch("vimona.hf_models._PinnedHTTPSConnection", return_value=fake_conn) as conn_cls,
            ):
                with self.assertRaises(hf_models.ModelDownloadError):
                    hf_models._open_validated_hf_download(
                        "https://huggingface.co/org/repo/resolve/main/m.gguf",
                        {"User-Agent": "Vimona/1.0"},
                        max_redirects=3,
                    )
                self.assertEqual(conn_cls.call_count, 1)

        # Redirect loop enforces max_redirects cap cleanly
        loop_resp = mock.MagicMock()
        loop_resp.status = 302
        loop_resp.getheader.side_effect = lambda name: "https://hf.co/next.gguf" if str(name).lower() == "location" else ""
        loop_conn = mock.MagicMock()
        loop_conn.getresponse.return_value = loop_resp
        with (
            mock.patch(
                "vimona.hf_models.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            ),
            mock.patch("vimona.hf_models._PinnedHTTPSConnection", return_value=loop_conn) as loop_cls,
        ):
            with self.assertRaises(hf_models.ModelDownloadError):
                hf_models._open_validated_hf_download(
                    "https://huggingface.co/org/repo/resolve/main/m.gguf",
                    {"User-Agent": "Vimona/1.0"},
                    max_redirects=2,
                )
            self.assertEqual(loop_cls.call_count, 3)

    def test_download_chunked_and_resumed_cumulative_size_limit(self) -> None:
        chunk_300k = b"GGUF" + b"\x00" * (300 * 1024 - 4)

        class _ChunkedResp:
            def __init__(self, chunks: list[bytes], status: int = 200, headers_dict: dict[str, str] | None = None, url: str = "") -> None:
                self._chunks = list(chunks)
                self.status = status
                self.headers = headers_dict or {}
                self._url = url
                self.reads = 0

            def geturl(self) -> str:
                return self._url

            def read(self, _amt: int = -1) -> bytes:
                if not self._chunks:
                    return b""
                self.reads += 1
                return self._chunks.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            llm_dir = root / "llm"
            llm_dir.mkdir(parents=True, exist_ok=True)
            spec = hf_models.HFModelSpec(
                id="test-chunk-cap",
                name="Test Chunk Cap",
                category="text",
                hf_repo="unsloth/gemma-3-1b-it-GGUF",
                hf_file="chunk-cap.gguf",
                size_gb=0.001,
                min_ram_gb=2.0,
                quant="Q4_K_M",
                summary="Chunk cap test",
            )
            custom_cfg = hf_models.DownloadConfig(max_file_bytes=1_500_000, max_mb_per_sec=0.0)
            resp_200 = _ChunkedResp([chunk_300k] * 7, status=200, headers_dict={}, url=spec.download_url)

            with (
                mock.patch.object(hf_models, "_CFG", mock.MagicMock(llm_dir=llm_dir, models_dir=root / "m", comfy_dir=root / "c")),
                mock.patch.object(hf_models, "MODELS_STATE_FILE", root / "models_config.json"),
                mock.patch.object(hf_models, "LOCAL_MODELS_DIR", root / "m"),
                mock.patch("vimona.hf_models._search_roots_for_category", return_value=[llm_dir]),
                mock.patch("vimona.hf_models.get_download_config", return_value=custom_cfg),
                mock.patch("vimona.hf_models._open_validated_hf_download", return_value=resp_200),
            ):
                with self.assertRaises(hf_models.ModelDownloadError) as ctx:
                    hf_models.download_hf_model(spec)
                self.assertIn("limit", str(ctx.exception).lower())
                self.assertEqual(resp_200.reads, 5)  # 5 * 300K = 1.5MB; aborted cleanly on 6th chunk BEFORE write
                self.assertFalse((llm_dir / "chunk-cap.gguf").exists())

                # Resumed 206 download where pre-existing .part + incoming chunks exceed max_file_bytes
                part_p = llm_dir / "chunk-cap.gguf.part"
                part_p.write_bytes(chunk_300k * 4)  # 1,228,800 bytes
                resumed_hdrs = {"Content-Range": f"bytes {len(chunk_300k) * 4}-1499999/1500000"}
                resp_206 = _ChunkedResp([chunk_300k, chunk_300k], status=206, headers_dict=resumed_hdrs, url=spec.download_url)
                with mock.patch("vimona.hf_models._open_validated_hf_download", return_value=resp_206):
                    with self.assertRaises(hf_models.ModelDownloadError):
                        hf_models.download_hf_model(spec)
                    self.assertFalse((llm_dir / "chunk-cap.gguf").exists())

    def test_resumed_download_content_range_validation(self) -> None:
        data = _make_fake_gguf_bytes(b"RANGE_CHECK_" * 110000)
        good_sha = hashlib.sha256(data).hexdigest()
        total = len(data)
        partial_len = total // 2
        tail = data[partial_len:]

        class _MockResp(io.BytesIO):
            def __init__(self, body: bytes, status: int, headers_dict: dict[str, str], url: str) -> None:
                super().__init__(body)
                self.status = status
                self.headers = headers_dict
                self._url = url

            def geturl(self) -> str:
                return self._url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            llm_dir = root / "llm"
            llm_dir.mkdir(parents=True, exist_ok=True)
            spec = hf_models.HFModelSpec(
                id="test-range-val",
                name="Test Range Validation",
                category="text",
                hf_repo="unsloth/gemma-3-1b-it-GGUF",
                hf_file="range-val.gguf",
                size_gb=total / (1024 ** 3),
                min_ram_gb=2.0,
                quant="Q4_K_M",
                summary="Range validation test",
            )
            part_file = llm_dir / "range-val.gguf.part"

            bad_cases = [
                # Wrong start offset
                {"Content-Length": str(len(tail)), "Content-Range": f"bytes 0-{total - 1}/{total}"},
                # Wrong total size
                {"Content-Length": str(len(tail)), "Content-Range": f"bytes {partial_len}-{total - 1}/{total + 1024}"},
                # Non-EOF ending offset
                {"Content-Length": str(len(tail)), "Content-Range": f"bytes {partial_len}-{partial_len + 100}/{total}"},
                # Wildcard / malformed Content-Range
                {"Content-Length": str(len(tail)), "Content-Range": f"bytes */{total}"},
                # Missing Content-Range on 206
                {"Content-Length": str(len(tail))},
                # Disagreement between Content-Length and Content-Range length
                {"Content-Length": "123", "Content-Range": f"bytes {partial_len}-{total - 1}/{total}"},
            ]

            with (
                mock.patch.object(hf_models, "_CFG", mock.MagicMock(llm_dir=llm_dir, models_dir=root / "m", comfy_dir=root / "c")),
                mock.patch.object(hf_models, "MODELS_STATE_FILE", root / "models_config.json"),
                mock.patch.object(hf_models, "LOCAL_MODELS_DIR", root / "m"),
                mock.patch("vimona.hf_models._search_roots_for_category", return_value=[llm_dir]),
            ):
                for hdrs in bad_cases:
                    part_file.write_bytes(data[:partial_len])
                    resp = _MockResp(tail, 206, hdrs, spec.download_url)
                    with mock.patch("vimona.hf_models._open_validated_hf_download", return_value=resp):
                        with self.assertRaises(hf_models.ModelDownloadError, msg=f"Should reject 206 headers {hdrs}"):
                            hf_models.download_hf_model(spec, expected_sha256="", expected_bytes=0)
                    self.assertFalse((llm_dir / "range-val.gguf").exists())

                # If server ignores Range and sends HTTP 200, restart cleanly from byte 0
                part_file.write_bytes(b"CORRUPT_STALE_PREFIX" * 60000)
                full_resp = _MockResp(data, 200, {"Content-Length": str(total)}, spec.download_url)
                with mock.patch("vimona.hf_models._open_validated_hf_download", return_value=full_resp):
                    dest = hf_models.download_hf_model(spec, expected_sha256=good_sha, expected_bytes=total)
                self.assertEqual(dest.read_bytes(), data)

    def test_three_process_download_lock_race(self) -> None:
        data = _make_fake_gguf_bytes(b"MULTIPROC_LOCK_" * 90000)
        good_sha = hashlib.sha256(data).hexdigest()
        repo_root = str(Path(__file__).resolve().parent.parent)

        worker_script = """
import hashlib, io, json, sys, time
from pathlib import Path
from unittest import mock
from vimona import hf_models

mode, root_s, sha, nbytes_s = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
root = Path(root_s)
llm_dir = root / "llm"
llm_dir.mkdir(parents=True, exist_ok=True)
nbytes = int(nbytes_s)
payload = (root / "payload.bin").read_bytes()

spec = hf_models.HFModelSpec(
    id="test-lock-race",
    name="Test Lock Race",
    category="text",
    hf_repo="unsloth/gemma-3-1b-it-GGUF",
    hf_file="lock-race.gguf",
    size_gb=len(payload) / (1024 ** 3),
    min_ram_gb=2.0,
    quant="Q4_K_M",
    summary="Lock race test",
)

class _SlowResp:
    status = 200
    headers = {"Content-Length": str(len(payload))}
    def __init__(self):
        self._pos = 0
        self._signaled = False
    def geturl(self):
        return spec.download_url
    def read(self, n):
        if mode == "holder" and not self._signaled:
            self._signaled = True
            (root / "holder_ready").write_text("1", encoding="utf-8")
            deadline = time.monotonic() + 12.0
            while not (root / "release_holder").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        ch = payload[self._pos : self._pos + min(n, 300000)]
        self._pos += len(ch)
        return ch
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass

with (
    mock.patch.object(hf_models, "_CFG", mock.MagicMock(llm_dir=llm_dir, models_dir=root / "m", comfy_dir=root / "c")),
    mock.patch.object(hf_models, "MODELS_STATE_FILE", root / "models_config.json"),
    mock.patch.object(hf_models, "LOCAL_MODELS_DIR", root / "m"),
    mock.patch("vimona.hf_models._search_roots_for_category", return_value=[llm_dir]),
    mock.patch("vimona.hf_models._open_validated_hf_download", return_value=_SlowResp()),
):
    try:
        out = hf_models.download_hf_model(spec, expected_sha256=sha, expected_bytes=nbytes)
        print("OK:" + str(out))
        sys.exit(0)
    except hf_models.ModelDownloadError as exc:
        print("LOCK_ERR:" + str(exc))
        sys.exit(2)
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "payload.bin").write_bytes(data)
            env = dict(os.environ)
            env["PYTHONPATH"] = repo_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

            proc_a = subprocess.Popen(
                [sys.executable, "-c", worker_script, "holder", str(root), good_sha, str(len(data))],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                ready_file = root / "holder_ready"
                deadline = time.monotonic() + 8.0
                while not ready_file.exists() and time.monotonic() < deadline:
                    if proc_a.poll() is not None:
                        break
                    time.sleep(0.05)
                if not ready_file.exists():
                    out_early, err_early = proc_a.communicate(timeout=2.0)
                    self.fail(f"Holder failed early: rc={proc_a.returncode}, out={out_early}, err={err_early}")

                lock_file = root / "llm" / "lock-race.gguf.part.lock"
                self.assertTrue(lock_file.is_file())
                lock_ino = lock_file.stat().st_ino

                # Process B fails lock acquisition and MUST NOT break/unlink lock_file
                res_b = subprocess.run(
                    [sys.executable, "-c", worker_script, "contender", str(root), good_sha, str(len(data))],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=8.0,
                )
                self.assertEqual(res_b.returncode, 2, res_b.stdout + res_b.stderr)
                self.assertIn("LOCK_ERR:", res_b.stdout)
                self.assertTrue(lock_file.is_file(), "Contender B unlinked persistent lock file!")
                self.assertEqual(lock_file.stat().st_ino, lock_ino)

                # Process C also tries while A is still downloading and MUST still be blocked
                res_c = subprocess.run(
                    [sys.executable, "-c", worker_script, "contender", str(root), good_sha, str(len(data))],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=8.0,
                )
                self.assertEqual(res_c.returncode, 2, res_c.stdout + res_c.stderr)
                self.assertIn("LOCK_ERR:", res_c.stdout)

                # Release Process A and verify clean completion
                (root / "release_holder").write_text("1", encoding="utf-8")
                out_a, err_a = proc_a.communicate(timeout=10.0)
                self.assertEqual(proc_a.returncode, 0, out_a + err_a)
                self.assertIn("OK:", out_a)
                self.assertEqual((root / "llm" / "lock-race.gguf").read_bytes(), data)
                self.assertTrue(lock_file.is_file(), "Persistent lock file should remain on disk")
            finally:
                (root / "release_holder").write_text("1", encoding="utf-8")
                if proc_a.poll() is None:
                    proc_a.kill()
                    proc_a.wait(timeout=2.0)
                for strm in (proc_a.stdout, proc_a.stderr):
                    if strm is not None:
                        try:
                            strm.close()
                        except Exception:
                            pass


if __name__ == "__main__":
    unittest.main()
