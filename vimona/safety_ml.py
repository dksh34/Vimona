"""Lightweight Safe-Blur Micro-ML Engine (zero LLM overhead, explicit availability state).

IMPORTANT LIMITATIONS (`G13`):
This module uses a compact 2-layer n-gram text MLP (`1024`-D) plus a 28-feature
perceptual YCbCr/HSV skin + spatial + arterial chroma vision MLP (`128x128` input).
It is a lightweight local visual caution filter and is NOT an exhaustive content
moderation model — accuracy can vary under extreme colored stage lighting, unusual
camera angles, illustrated styles, or borderline compositions. When vision weights are
missing or invalid, image classification reports `status="unavailable"` and fails closed
by default (`fail_closed_unavailable=True`) rather than silently treating media as safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

import cv2
import numpy as np

WEIGHTS_PATH = Path(__file__).resolve().with_name("safety_ml_weights.npz")
SAFETY_SETTINGS_FILE = Path("~/.local/share/vimona/safety_settings.json").expanduser()

SCHEMA_VERSION = "2.0"
TEXT_DIM = 1024
VISION_DIM = 28
TEXT_THRESHOLD = 0.88
VISION_THRESHOLD = 0.92
BORDERLINE_MARGIN = 0.08
VIDEO_SAMPLE_FRAMES = 10

_WORD_RE = re.compile(r"[a-z0-9]+")

# Minimal unambiguous hard-NSFW/gore boost terms (only unambiguous terms; never
# triggers on everyday words like battle, sunset, execute, explicit, red, skin).
_UNAMBIGUOUS_NSFW_RE = re.compile(
    r"\b("
    r"nsfw|nsfl|porn|pornography|pornographic|hentai|erotica|"
    r"xxx|nude|nudes|nudity|topless|bottomless|"
    r"blowjob|handjob|footjob|rimjob|cunnilingus|fellatio|deepthroat|"
    r"cumshot|ejaculat\w*|creampie|bukkake|gangbang|masturbat\w*|"
    r"vagina|pussy|clitoris|penis|phallus|nipples?|areola|"
    r"dildo|vibrator|buttplug|bdsm|striptease|prostitut\w*|gorefest|dismember\w*|decapitat\w*|eviscerat\w*"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SafeBlurSettings:
    """Controlled threshold & fail-closed configuration (`G1`, `G2`, `G11`, `G12`)."""

    text_threshold: float = TEXT_THRESHOLD
    vision_threshold: float = VISION_THRESHOLD
    borderline_margin: float = BORDERLINE_MARGIN
    video_sample_frames: int = VIDEO_SAMPLE_FRAMES
    fail_closed_unavailable: bool = True
    fail_closed_on_error: bool = True
    blur_on_borderline: bool = False


@dataclass(frozen=True)
class ClassificationResult:
    """Rich classification output exposing status (`safe`|`flagged`|`borderline`|`unavailable`|`error`)."""

    flagged: bool
    score: float
    status: str  # "safe" | "flagged" | "borderline" | "unavailable" | "error"
    confidence: float
    text_status: str
    vision_status: str
    reason: str = ""
    fail_closed: bool = False


def get_safety_settings() -> SafeBlurSettings:
    try:
        if SAFETY_SETTINGS_FILE.is_file():
            raw = json.loads(SAFETY_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return SafeBlurSettings(
                    text_threshold=max(0.50, min(0.99, float(raw.get("text_threshold", TEXT_THRESHOLD)))),
                    vision_threshold=max(0.50, min(0.99, float(raw.get("vision_threshold", VISION_THRESHOLD)))),
                    borderline_margin=max(0.01, min(0.25, float(raw.get("borderline_margin", BORDERLINE_MARGIN)))),
                    video_sample_frames=max(5, min(24, int(raw.get("video_sample_frames", VIDEO_SAMPLE_FRAMES)))),
                    fail_closed_unavailable=bool(raw.get("fail_closed_unavailable", True)),
                    fail_closed_on_error=bool(raw.get("fail_closed_on_error", True)),
                    blur_on_borderline=bool(raw.get("blur_on_borderline", False)),
                )
    except Exception:
        pass
    return SafeBlurSettings()


def configure_safety_settings(
    *,
    text_threshold: float | None = None,
    vision_threshold: float | None = None,
    borderline_margin: float | None = None,
    video_sample_frames: int | None = None,
    fail_closed_unavailable: bool | None = None,
    fail_closed_on_error: bool | None = None,
    blur_on_borderline: bool | None = None,
) -> SafeBlurSettings:
    """Persist controlled SafeBlur thresholds safely (`G11`)."""
    cur = get_safety_settings()
    nxt = SafeBlurSettings(
        text_threshold=max(0.50, min(0.99, float(text_threshold if text_threshold is not None else cur.text_threshold))),
        vision_threshold=max(0.50, min(0.99, float(vision_threshold if vision_threshold is not None else cur.vision_threshold))),
        borderline_margin=max(0.01, min(0.25, float(borderline_margin if borderline_margin is not None else cur.borderline_margin))),
        video_sample_frames=max(5, min(24, int(video_sample_frames if video_sample_frames is not None else cur.video_sample_frames))),
        fail_closed_unavailable=cur.fail_closed_unavailable if fail_closed_unavailable is None else bool(fail_closed_unavailable),
        fail_closed_on_error=cur.fail_closed_on_error if fail_closed_on_error is None else bool(fail_closed_on_error),
        blur_on_borderline=cur.blur_on_borderline if blur_on_borderline is None else bool(blur_on_borderline),
    )
    SAFETY_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SAFETY_SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "text_threshold": nxt.text_threshold,
                "vision_threshold": nxt.vision_threshold,
                "borderline_margin": nxt.borderline_margin,
                "video_sample_frames": nxt.video_sample_frames,
                "fail_closed_unavailable": nxt.fail_closed_unavailable,
                "fail_closed_on_error": nxt.fail_closed_on_error,
                "blur_on_borderline": nxt.blur_on_borderline,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(SAFETY_SETTINGS_FILE)
    if _ENGINE is not None:
        _ENGINE.classify_file.cache_clear()
    return nxt


def _load_haar_face() -> cv2.CascadeClassifier | None:
    try:
        haar_dir = getattr(getattr(cv2, "data", None), "haarcascades", "")
        if not haar_dir:
            return None
        xml = os.path.join(haar_dir, "haarcascade_frontalface_default.xml")
        if not os.path.isfile(xml):
            return None
        clf = cv2.CascadeClassifier(xml)
        return None if clf.empty() else clf
    except Exception:
        return None


_FACE_CASCADE = _load_haar_face()


def _hash_idx_sign(token: str, dim: int = TEXT_DIM) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    val = int.from_bytes(digest, "little")
    idx = val % dim
    sign = 1.0 if ((val >> 63) & 1) == 0 else -1.0
    return idx, sign


def featurize_text(text: str | None) -> np.ndarray:
    """Extract normalized word unigram + bigram + char-4gram hashed feature vector."""
    vec = np.zeros((TEXT_DIM,), dtype=np.float32)
    if not text:
        return vec
    words = _WORD_RE.findall(text.lower())
    if not words:
        return vec

    for w in words:
        i, s = _hash_idx_sign(f"w:{w}")
        vec[i] += 1.0 * s
        if len(w) >= 4:
            for k in range(len(w) - 3):
                ci, cs = _hash_idx_sign(f"c4:{w[k:k+4]}")
                vec[ci] += 0.45 * cs

    for a, b in zip(words, words[1:]):
        i, s = _hash_idx_sign(f"bg:{a}_{b}")
        vec[i] += 1.35 * s

    norm = float(np.linalg.norm(vec))
    if norm > 1e-6:
        vec /= norm
    return vec


def featurize_image_rgb(rgb: np.ndarray) -> np.ndarray:
    """Compute a compact 28-D computer vision sensitivity descriptor vector from RGB uint8/float image."""
    feats = np.zeros((VISION_DIM,), dtype=np.float32)
    if rgb is None or rgb.size == 0:
        return feats

    if rgb.dtype != np.uint8:
        img_u8 = np.clip(rgb, 0, 255).astype(np.uint8)
    else:
        img_u8 = rgb

    if img_u8.ndim == 2:
        img_u8 = cv2.cvtColor(img_u8, cv2.COLOR_GRAY2RGB)
    elif img_u8.shape[2] > 3:
        img_u8 = img_u8[:, :, :3]

    small = cv2.resize(img_u8, (128, 128), interpolation=cv2.INTER_AREA)
    rf = small[:, :, 0].astype(np.float32)
    gf = small[:, :, 1].astype(np.float32)
    bf = small[:, :, 2].astype(np.float32)

    # Dual YCbCr + HSV human flesh mask (rejects wood grain, sunset sky, warm lamps)
    y = 0.299 * rf + 0.587 * gf + 0.114 * bf
    cb = 128.0 - 0.168736 * rf - 0.331264 * gf + 0.5 * bf
    cr = 128.0 + 0.5 * rf - 0.418688 * gf - 0.081312 * bf

    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    hue = hsv[:, :, 0].astype(np.float32)  # 0..179
    sat = hsv[:, :, 1].astype(np.float32) / 255.0
    val = hsv[:, :, 2].astype(np.float32) / 255.0

    ycbcr_skin = (
        (y > 62.0)
        & (y < 238.0)
        & (rf > gf)
        & ((rf - bf) > 16.0)
        & (cb >= 95.0)
        & (cb <= 125.0)
        & (cr >= 136.0)
        & (cr <= 168.0)
    )
    hsv_skin = ((hue <= 22.0) | (hue >= 168.0)) & (sat >= 0.14) & (sat <= 0.68) & (val >= 0.22)
    skin_mask = ycbcr_skin & hsv_skin

    face_mask = np.zeros((128, 128), dtype=bool)
    face_count = 0
    if _FACE_CASCADE is not None:
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        faces = _FACE_CASCADE.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=3, minSize=(14, 14))
        face_count = len(faces)
        for (fx, fy, fw, fh) in faces:
            x0 = max(0, int(fx - fw * 0.22))
            y0 = max(0, int(fy - fh * 0.25))
            x1 = min(128, int(fx + fw * 1.22))
            y1 = min(128, int(fy + fh * 1.35))
            face_mask[y0:y1, x0:x1] = True

    torso_skin = skin_mask & (~face_mask)
    raw_skin_ratio = float(np.mean(skin_mask))
    face_skin_ratio = float(np.mean(skin_mask & face_mask))
    torso_skin_ratio = float(np.mean(torso_skin))

    feats[0] = raw_skin_ratio
    feats[1] = torso_skin_ratio
    feats[2] = face_skin_ratio
    feats[3] = min(1.0, face_count / 3.0)

    for r_i in range(3):
        for c_i in range(3):
            patch = torso_skin[r_i * 42 : (r_i + 1) * 42 + 2, c_i * 42 : (c_i + 1) * 42 + 2]
            feats[4 + r_i * 3 + c_i] = float(np.mean(patch))

    center_patch = torso_skin[24:104, 24:104]
    border_mask = np.ones((128, 128), dtype=bool)
    border_mask[24:104, 24:104] = False
    center_skin = float(np.mean(center_patch))
    border_skin = float(np.mean(torso_skin[border_mask]))
    feats[13] = center_skin
    feats[14] = border_skin
    feats[15] = max(-1.0, min(1.0, center_skin - border_skin))

    num_lbl, _lbls, stats, _cent = cv2.connectedComponentsWithStats(torso_skin.astype(np.uint8), connectivity=8)
    largest_frac = 0.0
    if num_lbl > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        largest_frac = float(np.max(areas)) / float(128 * 128)
    feats[16] = largest_frac

    gray_f = y / 255.0
    gx = cv2.Sobel(gray_f, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_f, cv2.CV_32F, 0, 1, ksize=3)
    grad_mag = np.sqrt(gx * gx + gy * gy)
    lap = np.abs(cv2.Laplacian(gray_f, cv2.CV_32F, ksize=3))

    feats[17] = float(np.mean(grad_mag))
    feats[18] = float(np.mean(lap))
    if np.any(torso_skin):
        feats[19] = float(np.mean(grad_mag[torso_skin]))
        feats[20] = float(np.mean(lap[torso_skin]))
        feats[21] = float(np.std(val[torso_skin]))
        feats[22] = float(np.mean(sat[torso_skin]))
    else:
        feats[19:23] = 0.0

    crimson_mask = (
        ((hue <= 5.0) | (hue >= 174.0))
        & (sat >= 0.56)
        & (val >= 0.14)
        & (val <= 0.58)
        & ((rf - gf) > 45.0)
        & ((rf - bf) > 45.0)
    )
    feats[23] = float(np.mean(crimson_mask))
    feats[24] = float(np.mean(crimson_mask[24:104, 24:104]))

    h_energy = float(np.mean(np.abs(gy)))
    v_energy = float(np.mean(np.abs(gx))) + 1e-5
    feats[25] = min(3.0, h_energy / v_energy) / 3.0
    feats[26] = float(np.mean(sat))
    feats[27] = float(np.std(hue / 180.0))
    return feats


def _validate_weights_dict(weights: dict[str, np.ndarray]) -> tuple[bool, bool, str]:
    """Validate text & vision weight tensor dimensions, finiteness & schema (`G9`, `G10`)."""
    # Check version tag if bundled
    if "schema_version" in weights:
        try:
            ver = str(weights["schema_version"].item())
            if ver not in ("1.0", "2.0"):
                return (False, False, f"Incompatible weight schema {ver}")
        except Exception:
            return (False, False, "Invalid schema_version tensor")

    text_ok = False
    if all(k in weights for k in ("t_w1", "t_b1", "t_w2", "t_b2")):
        tw1, tb1, tw2, tb2 = weights["t_w1"], weights["t_b1"], weights["t_w2"], weights["t_b2"]
        if (
            tw1.ndim == 2
            and tw1.shape[0] == TEXT_DIM
            and tb1.shape == (tw1.shape[1],)
            and tw2.shape == (tw1.shape[1],)
            and tb2.ndim == 0
            and all(np.all(np.isfinite(a)) for a in (tw1, tb1, tw2, tb2))
        ):
            text_ok = True

    vis_ok = False
    if all(k in weights for k in ("v_mu", "v_sig", "v_w1", "v_b1", "v_w2", "v_b2")):
        vmu, vsig, vw1, vb1, vw2, vb2 = (
            weights["v_mu"],
            weights["v_sig"],
            weights["v_w1"],
            weights["v_b1"],
            weights["v_w2"],
            weights["v_b2"],
        )
        if (
            vmu.shape == (VISION_DIM,)
            and vsig.shape == (VISION_DIM,)
            and vw1.ndim == 2
            and vw1.shape[0] == VISION_DIM
            and vb1.shape == (vw1.shape[1],)
            and vw2.shape == (vw1.shape[1],)
            and vb2.ndim == 0
            and all(np.all(np.isfinite(a)) for a in (vmu, vsig, vw1, vb1, vw2, vb2))
        ):
            vis_ok = True

    if not text_ok and not vis_ok:
        return (False, False, "Weight dimensions/contents invalid")
    return (text_ok, vis_ok, "OK" if (text_ok and vis_ok) else "Partial classifier weights valid")


class SafeBlurMLEngine:
    """Runtime Safe-Blur classifier separating `ok`, `borderline`, `unavailable`, and `error` (`G1`–`G14`)."""

    def __init__(self, weights_path: Path = WEIGHTS_PATH) -> None:
        self._weights: dict[str, np.ndarray] | None = None
        self.text_available = False
        self.vision_available = False
        self.load_error = ""
        if weights_path.is_file():
            try:
                data = np.load(weights_path, allow_pickle=False)
                loaded = {
                    k: (data[k].astype(np.float32) if data[k].dtype.kind in "fiu" else data[k])
                    for k in data.files
                }
                t_ok, v_ok, msg = _validate_weights_dict(loaded)
                self.text_available = t_ok
                self.vision_available = v_ok
                if t_ok or v_ok:
                    self._weights = loaded
                else:
                    self.load_error = msg
            except Exception as exc:
                self._weights = None
                self.load_error = str(exc)
        else:
            self.load_error = f"Weights file missing at {weights_path}"

    @property
    def status(self) -> str:
        if self.text_available and self.vision_available:
            return "ready"
        if self.text_available or self.vision_available:
            return "partial_unavailable"
        return "unavailable"

    @staticmethod
    def _sigmoid(x: np.ndarray | float) -> np.ndarray | float:
        x_clamped = np.clip(x, -30.0, 30.0)
        return 1.0 / (1.0 + np.exp(-x_clamped))

    def score_text(self, text: str | None) -> float:
        if not text or not text.strip():
            return 0.0
        if not self.text_available or self._weights is None:
            return 0.96 if _UNAMBIGUOUS_NSFW_RE.search(text) else 0.05
        x = featurize_text(text)
        h = np.maximum(0.0, x @ self._weights["t_w1"] + self._weights["t_b1"])
        logit = float(h @ self._weights["t_w2"] + self._weights["t_b2"])
        prob = float(self._sigmoid(logit))
        if _UNAMBIGUOUS_NSFW_RE.search(text):
            prob = max(prob, 0.94)
        return prob

    def assess_text(self, text: str | None, threshold: float | None = None) -> ClassificationResult:
        cfg = get_safety_settings()
        th = float(threshold if threshold is not None else cfg.text_threshold)
        t_state = "ok" if self.text_available else "unavailable"
        if not (text or "").strip():
            return ClassificationResult(False, 0.0, "safe", 1.0, t_state, "n/a")
        prob = self.score_text(text)
        if not self.text_available and _UNAMBIGUOUS_NSFW_RE.search(text or ""):
            return ClassificationResult(True, prob, "flagged", 0.95, t_state, "n/a", "Hard keyword rule")
        if not self.text_available:
            return ClassificationResult(
                bool(cfg.fail_closed_unavailable),
                prob,
                "unavailable",
                0.0,
                "unavailable",
                "n/a",
                "Text weights unavailable",
                fail_closed=cfg.fail_closed_unavailable,
            )
        margin_low = max(0.0, th - cfg.borderline_margin)
        if prob >= th:
            st = "flagged"
            fl = True
        elif prob >= margin_low:
            st = "borderline"
            fl = bool(cfg.blur_on_borderline)
        else:
            st = "safe"
            fl = False
        conf = round(min(1.0, abs(prob - th) / max(0.05, cfg.borderline_margin)), 3)
        return ClassificationResult(fl, prob, st, conf, "ok", "n/a")

    def classify_text(self, text: str | None, threshold: float | None = None) -> tuple[bool, float]:
        res = self.assess_text(text, threshold=threshold)
        return (res.flagged, res.score)

    def score_image_rgb(self, rgb: np.ndarray) -> float:
        """Compute image score; returns -1.0 sentinel if vision weights unavailable (`G1`)."""
        if rgb is None or rgb.size == 0 or not self.vision_available or self._weights is None:
            return -1.0
        x = featurize_image_rgb(rgb)
        mu = self._weights["v_mu"]
        sig = np.maximum(1e-4, self._weights["v_sig"])
        xn = (x - mu) / sig
        h = np.maximum(0.0, xn @ self._weights["v_w1"] + self._weights["v_b1"])
        logit = float(h @ self._weights["v_w2"] + self._weights["v_b2"])
        return float(self._sigmoid(logit))

    def assess_image_rgb(
        self,
        rgb: np.ndarray,
        context_text: str = "",
        threshold: float | None = None,
    ) -> ClassificationResult:
        cfg = get_safety_settings()
        th_v = float(threshold if threshold is not None else cfg.vision_threshold)
        t_res = self.assess_text(context_text) if context_text else None
        t_prob = t_res.score if t_res is not None else 0.0
        if t_res is not None and t_res.flagged:
            try:
                vp = self.score_image_rgb(rgb) if (rgb is not None and isinstance(rgb, np.ndarray) and rgb.size > 0) else 0.0
            except Exception:
                vp = 0.0
            eff = max(t_prob, vp if vp >= 0.0 else 0.0)
            return ClassificationResult(
                True,
                eff,
                "flagged",
                1.0,
                t_res.text_status,
                "ok" if self.vision_available else "unavailable",
                "Context triggered",
            )

        # G1 & G2: Missing or corrupt vision weights MUST report unavailable & fail-closed (never silent 0.0 safe!)
        if not self.vision_available or self._weights is None:
            return ClassificationResult(
                flagged=bool(cfg.fail_closed_unavailable),
                score=0.0,
                status="unavailable",
                confidence=0.0,
                text_status=t_res.text_status if t_res else ("ok" if self.text_available else "unavailable"),
                vision_status="unavailable",
                reason=self.load_error or "Vision classifier weights unavailable",
                fail_closed=cfg.fail_closed_unavailable,
            )

        if rgb is None or not isinstance(rgb, np.ndarray) or rgb.size == 0 or rgb.ndim not in (2, 3):
            return ClassificationResult(
                flagged=bool(cfg.fail_closed_on_error),
                score=0.0,
                status="error",
                confidence=0.0,
                text_status=t_res.text_status if t_res else "ok",
                vision_status="error",
                reason="Empty or invalid RGB array",
                fail_closed=cfg.fail_closed_on_error,
            )

        try:
            v_prob = self.score_image_rgb(rgb)
        except Exception as exc:
            return ClassificationResult(
                flagged=bool(cfg.fail_closed_on_error),
                score=0.0,
                status="error",
                confidence=0.0,
                text_status=t_res.text_status if t_res else "ok",
                vision_status="error",
                reason=f"Image scorer failed: {exc}",
                fail_closed=cfg.fail_closed_on_error,
            )

        margin_low = max(0.0, th_v - cfg.borderline_margin)
        if v_prob >= th_v:
            st = "flagged"
            fl = True
        elif v_prob >= margin_low:
            st = "borderline"
            fl = bool(cfg.blur_on_borderline)
        else:
            st = "safe"
            fl = False
        conf = round(min(1.0, abs(v_prob - th_v) / max(0.05, cfg.borderline_margin)), 3)
        return ClassificationResult(
            flagged=fl,
            score=v_prob,
            status=st,
            confidence=conf,
            text_status=t_res.text_status if t_res else "ok",
            vision_status="ok",
        )

    def classify_image_rgb(
        self,
        rgb: np.ndarray,
        context_text: str = "",
        threshold: float | None = None,
    ) -> tuple[bool, float]:
        res = self.assess_image_rgb(rgb, context_text=context_text, threshold=threshold)
        return (res.flagged, res.score)

    def assess_file(
        self,
        file_path_str: str,
        prompt_hint: str = "",
        title: str = "",
    ) -> ClassificationResult:
        cfg = get_safety_settings()
        ctx = f"{title} {prompt_hint}".strip()
        ctx_res = self.assess_text(ctx) if ctx else ClassificationResult(False, 0.0, "safe", 1.0, "ok", "ok")
        if ctx_res.flagged:
            return ctx_res

        if not self.vision_available or self._weights is None:
            return ClassificationResult(
                flagged=bool(cfg.fail_closed_unavailable),
                score=ctx_res.score,
                status="unavailable",
                confidence=0.0,
                text_status=ctx_res.text_status,
                vision_status="unavailable",
                reason=self.load_error or "Vision classifier unavailable",
                fail_closed=cfg.fail_closed_unavailable,
            )

        if not file_path_str:
            return ClassificationResult(
                flagged=bool(cfg.fail_closed_on_error),
                score=ctx_res.score,
                status="error",
                confidence=0.0,
                text_status=ctx_res.text_status,
                vision_status="error",
                reason="Missing path",
                fail_closed=cfg.fail_closed_on_error,
            )
        p = Path(file_path_str)
        if not p.is_file():
            return ClassificationResult(
                flagged=bool(cfg.fail_closed_on_error),
                score=ctx_res.score,
                status="error",
                confidence=0.0,
                text_status=ctx_res.text_status,
                vision_status="error",
                reason="File not found",
                fail_closed=cfg.fail_closed_on_error,
            )

        ext = p.suffix.lower()
        try:
            if ext in (".mp4", ".webm", ".mkv", ".mov", ".avi"):
                cap = cv2.VideoCapture(str(p))
                try:
                    if not cap.isOpened():
                        return ClassificationResult(
                            flagged=bool(cfg.fail_closed_on_error),
                            score=ctx_res.score,
                            status="error",
                            confidence=0.0,
                            text_status=ctx_res.text_status,
                            vision_status="error",
                            reason="Could not open video stream",
                            fail_closed=cfg.fail_closed_on_error,
                        )
                    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    target_n = min(max(1, n), cfg.video_sample_frames)
                    if n <= target_n:
                        idxs = list(range(max(1, n)))
                    else:
                        idxs = sorted({int(round(i * (n - 1) / (target_n - 1))) for i in range(target_n)})
                    scores: list[float] = []
                    statuses: list[str] = []
                    flags = 0
                    failed_reads = 0
                    for idx in idxs:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                        ok, bgr = cap.read()
                        if not ok or bgr is None:
                            failed_reads += 1
                            continue
                        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                        r_frm = self.assess_image_rgb(rgb, context_text=ctx)
                        if r_frm.status in ("error", "unavailable"):
                            return r_frm
                        scores.append(r_frm.score)
                        statuses.append(r_frm.status)
                        if r_frm.flagged:
                            flags += 1
                    if not scores or (len(idxs) > 0 and failed_reads > len(idxs) // 2):
                        return ClassificationResult(
                            flagged=bool(cfg.fail_closed_on_error),
                            score=ctx_res.score,
                            status="error",
                            confidence=0.0,
                            text_status=ctx_res.text_status,
                            vision_status="error",
                            reason="Unreadable or truncated video frames",
                            fail_closed=cfg.fail_closed_on_error,
                        )
                    # Peak & top-k aggregation instead of pure mean dilution (`G7`, `G8`)
                    top_k = sorted(scores, reverse=True)[: max(1, min(3, len(scores)))]
                    top_k_mean = sum(top_k) / len(top_k)
                    peak = max(scores)
                    is_vid_flagged = (
                        flags >= 2
                        or (len(scores) <= 2 and flags >= 1)
                        or peak >= cfg.vision_threshold
                        or top_k_mean >= (cfg.vision_threshold * 0.95)
                    )
                    st = "flagged" if is_vid_flagged else ("borderline" if "borderline" in statuses else "safe")
                    return ClassificationResult(
                        flagged=is_vid_flagged,
                        score=peak if is_vid_flagged else top_k_mean,
                        status=st,
                        confidence=round(min(1.0, abs(peak - cfg.vision_threshold) / 0.1), 3),
                        text_status=ctx_res.text_status,
                        vision_status="ok",
                    )
                finally:
                    cap.release()
            else:
                bgr = cv2.imread(str(p))
                if bgr is None:
                    return ClassificationResult(
                        flagged=bool(cfg.fail_closed_on_error),
                        score=ctx_res.score,
                        status="error",
                        confidence=0.0,
                        text_status=ctx_res.text_status,
                        vision_status="error",
                        reason="Unreadable image or unsupported format",
                        fail_closed=cfg.fail_closed_on_error,
                    )
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                return self.assess_image_rgb(rgb, context_text=ctx)
        except Exception as exc:
            return ClassificationResult(
                flagged=bool(cfg.fail_closed_on_error),
                score=ctx_res.score,
                status="error",
                confidence=0.0,
                text_status=ctx_res.text_status,
                vision_status="error",
                reason=f"Classification error: {exc}",
                fail_closed=cfg.fail_closed_on_error,
            )

    @lru_cache(maxsize=256)
    def classify_file(
        self,
        file_path_str: str,
        prompt_hint: str = "",
        title: str = "",
    ) -> tuple[bool, float]:
        res = self.assess_file(file_path_str, prompt_hint=prompt_hint, title=title)
        return (res.flagged, res.score)


def _compute_binary_metrics(tp: int, fp: int, tn: int, fn: int) -> dict[str, Any]:
    prec = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    fnr = fn / (fn + tp) if (fn + tp) > 0 else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "total": tp + fp + tn + fn,
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "fpr": round(fpr, 4),
        "fnr": round(fnr, 4),
    }


def evaluate_safeblur_dataset(engine: SafeBlurMLEngine | None = None) -> dict[str, Any]:
    """Run controlled labeled text+vision regression benchmark with separate precision/recall/FPR/FNR (`G3`–`G6`, `G14`)."""
    eng = engine or get_safety_ml_engine()
    if eng.status != "ready":
        return {"status": "unavailable", "reason": eng.load_error}

    # Expanded labeled text corpus (unambiguous explicit/NSFL vs technical/medical/aesthetic benign)
    pos_texts = [
        "explicit nude nsfw photography",
        "pornographic topless model",
        "dismembered gorefest scene",
        "uncensored hardcore xxx sex scene",
        "explicit erotica uncensored lingerie strip",
        "graphic nsfl bloody decapitation",
        "nsfw uncensored hentai explicit",
        "nude woman boudoir uncensored full body",
        "pornographic explicit adult couple",
        "extreme graphic gore eviscerated",
        "explicit sensual nude beach portrait",
        "uncensored bdsm fetish explicit",
    ]
    neg_texts = [
        "sunset mountain lake alpenglow",
        "sports car on wet neon street",
        "kill process in linux kernel",
        "blonde woman smiling wearing thick winter coat in snow",
        "skin-tone ceramic vase on wooden table",
        "vintage film camera 50mm lens street photography",
        "terracotta pottery studio warm natural light",
        "architectural concrete brutalist museum interior",
        "software developer debugging bash script in terminal",
        "alpine snow pine trees golden hour mist",
        "red sports jacket portrait outdoors overcast",
        "anatomy textbook diagram skeletal muscle structure",
    ]
    t_tp = sum(1 for t in pos_texts if eng.assess_text(t).flagged)
    t_fn = len(pos_texts) - t_tp
    t_fp = sum(1 for t in neg_texts if eng.assess_text(t).flagged)
    t_tn = len(neg_texts) - t_fp
    text_metrics = _compute_binary_metrics(t_tp, t_fp, t_tn, t_fn)

    # Synthetic vision regression dataset covering lighting, colors, foliage/wood/brick, & high-exposure flesh/crimson checks
    benign_imgs: list[tuple[str, np.ndarray]] = []
    # 1. Night neon cyan/magenta scene
    im_neon = np.full((128, 128, 3), (18, 22, 48), dtype=np.uint8)
    im_neon[20:100, 30:98] = (20, 180, 210)
    benign_imgs.append(("cool_neon", im_neon))
    # 2. Warm wooden cabin amber planks
    im_wood = np.full((128, 128, 3), (145, 92, 54), dtype=np.uint8)
    for y in range(0, 128, 8):
        im_wood[y : y + 2, :] = (88, 52, 28)
    benign_imgs.append(("warm_wood", im_wood))
    # 3. Sunlit alpine greenery + blue sky
    im_alp = np.full((128, 128, 3), (110, 165, 235), dtype=np.uint8)
    im_alp[64:, :] = (42, 110, 56)
    benign_imgs.append(("daylight_landscape", im_alp))
    # 4. Dark/low-key architectural corridor
    im_dark = np.full((128, 128, 3), (24, 24, 28), dtype=np.uint8)
    im_dark[30:90, 50:78] = (72, 68, 64)
    benign_imgs.append(("lowkey_dark", im_dark))
    # 5. Silver/monochrome cityscape
    im_mono = np.full((128, 128, 3), (92, 96, 102), dtype=np.uint8)
    im_mono[20:110, 20:50] = (140, 144, 150)
    benign_imgs.append(("monochrome_city", im_mono))
    # 6. Ocean teal wave gradients
    im_sea = np.full((128, 128, 3), (26, 98, 124), dtype=np.uint8)
    im_sea[:40, :] = (165, 205, 230)
    benign_imgs.append(("ocean_teal", im_sea))
    # 7. Autumn forest leaves (amber/gold/green)
    im_aut = np.full((128, 128, 3), (62, 88, 44), dtype=np.uint8)
    im_aut[24:96, 24:104] = (182, 134, 42)
    benign_imgs.append(("autumn_foliage", im_aut))
    # 8. Sunset lavender sky over dark ridge
    im_sunset = np.full((128, 128, 3), (118, 86, 142), dtype=np.uint8)
    im_sunset[80:, :] = (28, 24, 34)
    benign_imgs.append(("lavender_dusk", im_sunset))
    # 9. Technical emerald wireframe blueprint
    im_grid = np.full((128, 128, 3), (12, 28, 24), dtype=np.uint8)
    im_grid[::16, :] = (44, 188, 140)
    im_grid[:, ::16] = (44, 188, 140)
    benign_imgs.append(("emerald_grid", im_grid))
    # 10. Clothed portrait placeholder (dark slate torso coat + bright background)
    im_coat = np.full((128, 128, 3), (195, 210, 222), dtype=np.uint8)
    im_coat[52:, 34:94] = (40, 48, 62)
    benign_imgs.append(("clothed_coat_scene", im_coat))

    positive_imgs: list[tuple[str, np.ndarray]] = []
    # Synthetic positive regression matrices across lighting & skin-tone/crimson spectra
    for idx, (bg, fg, box) in enumerate(
        [
            ((35, 38, 42), (218, 152, 128), (16, 112, 18, 110)),
            ((28, 30, 34), (176, 114, 86), (16, 112, 18, 110)),
            ((22, 24, 30), (144, 92, 68), (18, 110, 20, 108)),
            ((48, 52, 58), (226, 162, 136), (12, 116, 22, 106)),
            ((18, 20, 26), (198, 134, 106), (20, 108, 16, 112)),
            ((30, 30, 34), (135, 12, 16), (20, 108, 20, 108)),
            ((20, 22, 28), (148, 18, 22), (16, 112, 24, 104)),
            ((42, 40, 46), (162, 24, 28), (18, 110, 18, 110)),
            ((15, 18, 24), (208, 144, 118), (14, 114, 14, 114)),
            ((60, 64, 70), (188, 126, 98), (16, 112, 20, 108)),
        ],
        1,
    ):
        arr = np.full((128, 128, 3), bg, dtype=np.uint8)
        y0, y1, x0, x1 = box
        arr[y0:y1, x0:x1] = fg
        positive_imgs.append((f"syn_pos_{idx:02d}", arr))

    v_tp = sum(1 for _lbl, arr in positive_imgs if eng.assess_image_rgb(arr).flagged)
    v_fn = len(positive_imgs) - v_tp
    v_fp = sum(1 for _lbl, arr in benign_imgs if eng.assess_image_rgb(arr).flagged)
    v_tn = len(benign_imgs) - v_fp
    vision_metrics = _compute_binary_metrics(v_tp, v_fp, v_tn, v_fn)

    return {
        "status": "ok",
        "dataset_type": "synthetic_regression",
        "limitations": (
            "Measured on expanded synthetic regression fixtures; real-world precision/recall "
            "requires evaluation against a human-labeled photographic validation corpus."
        ),
        "text": text_metrics,
        "vision": vision_metrics,
        "text_fpr": text_metrics["fpr"],
        "text_fnr": text_metrics["fnr"],
        "vision_benign_fpr": vision_metrics["fpr"],
        "vision_fnr": vision_metrics["fnr"],
        "tested_lighting_profiles": [lbl for lbl, _ in benign_imgs],
    }


_ENGINE: SafeBlurMLEngine | None = None


def get_safety_ml_engine() -> SafeBlurMLEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = SafeBlurMLEngine()
    return _ENGINE


def reload_safety_ml_engine() -> SafeBlurMLEngine:
    global _ENGINE
    _ENGINE = SafeBlurMLEngine()
    return _ENGINE
