"""Structured logging with automatic secret, token, & prompt redaction (`L7`, `L8`)."""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import get_config

_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # GitHub & HuggingFace tokens
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"), "[REDACTED_HF_TOKEN]"),
    # Bearer / bridge auth headers
    (re.compile(r"(?i)(authorization|x-vimona-bridge-token|cookie|set-cookie)\s*[:=]\s*[^\s,;\"']+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{12,}\b"), "Bearer [REDACTED]"),
    # Sensitive inline JSON prompt keys if ever accidentally formatted into diagnostics
    (re.compile(r'(?i)"(positive_prompt|negative_prompt|motion_prompt|api_key|token|password|secret)"\s*:\s*"[^"]*"'), r'"\1":"[REDACTED]"'),
)

_LOG_CONFIGURED = False


def redact_sensitive_text(text: str) -> str:
    """Scrub tokens, auth headers, and sensitive prompt fields from log text (`L8`)."""
    out = str(text or "")
    for pat, repl in _REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


class RedactingFilter(logging.Filter):
    """Sanitize formatted log records against token/prompt leakage (`L8`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = redact_sensitive_text(msg)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
            if record.exc_text:
                record.exc_text = redact_sensitive_text(record.exc_text)
        except Exception:
            pass
        return True


def configure_logging(
    state_dir: Path | None = None,
    level: int = logging.INFO,
    force: bool = False,
) -> logging.Logger:
    """Configure bounded rotating file + stderr structured logging (`L7`, `L8`)."""
    global _LOG_CONFIGURED
    root_log = logging.getLogger("vimona")
    if _LOG_CONFIGURED and not force:
        return root_log

    root_log.setLevel(level)
    root_log.propagate = False
    for existing in list(root_log.handlers):
        root_log.removeHandler(existing)
        try:
            existing.close()
        except Exception:
            pass

    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    filt = RedactingFilter()

    sh = logging.StreamHandler()
    sh.setLevel(max(level, logging.WARNING))
    sh.setFormatter(fmt)
    sh.addFilter(filt)
    root_log.addHandler(sh)

    target_dir = (state_dir or get_config().state_dir).expanduser()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(target_dir, 0o700)
        log_file = target_dir / "vimona.log"
        fh = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        fh.addFilter(filt)
        root_log.addHandler(fh)
        if log_file.is_file():
            os.chmod(log_file, 0o600)
    except OSError:
        # Still operate safely even if state dir isn't writable in read-only sandbox
        pass

    _LOG_CONFIGURED = True
    return root_log
