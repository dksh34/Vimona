"""Tests for config, structured redacting logs, preflight checks, package data, & static hygiene (`L6`–`L12`, `M1`–`M10`, `N2`)."""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vimona import config, errors, logging_utils, preflight


REPO_ROOT = Path(__file__).resolve().parent.parent


class TestConfigAndErrors(unittest.TestCase):
    def test_error_hierarchy_relationships(self) -> None:
        self.assertTrue(issubclass(errors.IsolationError, errors.VimonaError))
        self.assertTrue(issubclass(errors.InsufficientMemoryError, errors.HandoffError))
        self.assertTrue(issubclass(errors.HandoffError, errors.VimonaError))
        self.assertTrue(issubclass(errors.ModelDownloadError, errors.VimonaError))
        self.assertTrue(issubclass(errors.PreflightError, errors.VimonaError))

    def test_env_config_overrides_and_safesearch_sanitization(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {
                "VIMONA_STATE_DIR": f"{td}/state",
                "VIMONA_MODELS_DIR": f"{td}/models",
                "VIMONA_COMFY_DIR": f"{td}/comfy",
                "VIMONA_SAFESEARCH": "invalid_val",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                cfg = config.reload_config()
                self.assertEqual(cfg.state_dir, Path(td) / "state")
                self.assertEqual(cfg.models_dir, Path(td) / "models")
                self.assertEqual(cfg.comfy_dir, Path(td) / "comfy")
                self.assertEqual(cfg.default_safesearch, "moderate")

            with mock.patch.dict(os.environ, {"VIMONA_SAFESEARCH": "strict"}, clear=False):
                cfg2 = config.reload_config()
                self.assertEqual(cfg2.default_safesearch, "strict")
        config.reload_config()


class TestLoggingRedaction(unittest.TestCase):
    def test_redact_sensitive_tokens_and_prompts(self) -> None:
        raw = (
            'Auth ghp_abcdefghijklmnopqrstuvwxyz012345 and hf_AbCdEfGhIjKlMnOpQrStUvWx '
            'X-Vimona-Bridge-Token=supersecret99 Bearer abcdefghijklmnop '
            '{"positive_prompt":"private scene text","token":"leakme"}'
        )
        red = logging_utils.redact_sensitive_text(raw)
        self.assertNotIn("ghp_abcdef", red)
        self.assertNotIn("hf_AbCdEf", red)
        self.assertNotIn("supersecret99", red)
        self.assertNotIn("abcdefghijklmnop", red)
        self.assertNotIn("private scene text", red)
        self.assertNotIn("leakme", red)
        self.assertIn("[REDACTED", red)

    def test_rotating_file_logger_writes_redacted_messages_with_0600_perms(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "logs"
            log = logging_utils.configure_logging(state_dir=sdir, level=logging.INFO, force=True)
            log.warning("Testing token leak hf_1234567890abcdef1234567890")
            for h in log.handlers:
                h.flush()
            logfile = sdir / "vimona.log"
            self.assertTrue(logfile.is_file())
            mode = logfile.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
            content = logfile.read_text(encoding="utf-8")
            self.assertNotIn("hf_1234567890abcdef1234567890", content)
            self.assertIn("[REDACTED_HF_TOKEN]", content)


class TestPreflightAndStaticAssets(unittest.TestCase):
    def test_preflight_detects_missing_binaries_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            cfg = config.VimonaRuntimeConfig(
                state_dir=p / "state",
                models_dir=p / "models",
                llm_dir=p / "llm",
                comfy_dir=p / "no_comfy",
                llama_server_bin=p / "no_llama",
                sd_server_bin=p / "no_sd",
                sd_cli_bin=p / "no_sd_cli",
                ffmpeg_bin="nonexistent_ffmpeg_bin_12345",
                ffprobe_bin="nonexistent_ffprobe_bin_12345",
                python_bin="/usr/bin/python3",
                default_llm_path=p / "missing.gguf",
                default_safesearch="moderate",
            )
            rep = preflight.run_preflight_checks(cfg=cfg, require_display=False)
            self.assertFalse(rep.capabilities["text_llm"])
            self.assertFalse(rep.capabilities["image_fast_vulkan"])
            self.assertFalse(rep.capabilities["comfyui"])
            self.assertTrue(rep.capabilities["safeblur_assets"])
            self.assertTrue(len(rep.degraded_reasons) >= 2)
            self.assertIn("Vimona Preflight Report", rep.format_text())

    def test_preflight_warns_cleanly_on_non_linux_mock(self) -> None:
        with (
            mock.patch("vimona.preflight.sys.platform", "win32"),
            mock.patch("vimona.preflight.shutil.which", return_value=None),
        ):
            rep = preflight.run_preflight_checks(require_display=True)
            self.assertFalse(rep.os_supported)
            self.assertFalse(rep.can_start_gui)

    def test_all_python_modules_ast_parse_cleanly_and_assets_exist(self) -> None:
        py_files = list((REPO_ROOT / "vimona").glob("*.py")) + list((REPO_ROOT / "tests").glob("*.py"))
        self.assertGreater(len(py_files), 20)
        for path in py_files:
            src = path.read_text(encoding="utf-8")
            ast.parse(src, filename=str(path))
        self.assertTrue((REPO_ROOT / "vimona" / "safety_ml_weights.npz").is_file())
        packs = list((REPO_ROOT / "vimona" / "skills" / "packs").glob("*.json"))
        self.assertGreaterEqual(len(packs), 10)
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()
        self.assertNotIn("pony diffusion", readme)
        self.assertNotIn("wan 2.1", readme)


if __name__ == "__main__":
    unittest.main()
