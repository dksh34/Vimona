"""Tests for hardware profiling tiers and resumable/verified model downloads (`C1`–`C12`, `D1`–`D14`, `N5`, `N9`)."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
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
            def __init__(self, body: bytes, status: int, headers_dict: dict[str, str]) -> None:
                super().__init__(body)
                self.status = status
                self.headers = headers_dict

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

            def _fake_urlopen(req, timeout=30.0):
                rng = req.headers.get("Range")
                self.assertEqual(rng, f"bytes={partial_len}-")
                rest = data[partial_len:]
                return _RangeResp(rest, 206, {"Content-Length": str(len(rest))})

            with (
                mock.patch.object(hf_models, "_CFG", mock.MagicMock(llm_dir=llm_dir, models_dir=root / "m", comfy_dir=root / "c")),
                mock.patch.object(hf_models, "MODELS_STATE_FILE", root / "models_config.json"),
                mock.patch.object(hf_models, "LOCAL_MODELS_DIR", root / "m"),
                mock.patch("vimona.hf_models._search_roots_for_category", return_value=[llm_dir]),
                mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen),
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


if __name__ == "__main__":
    unittest.main()
