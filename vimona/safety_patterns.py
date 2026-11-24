"""Sensitive/explicit text classifier backed by Vimona's compact Micro-ML engine."""

from __future__ import annotations

from .safety_ml import get_safety_ml_engine


def is_explicit_text(text: str | None) -> bool:
    """Return True only when the lightweight TextSensitivityMLP flags explicit NSFW/gore content."""
    if not text:
        return False
    flagged, _score = get_safety_ml_engine().classify_text(text)
    return flagged
