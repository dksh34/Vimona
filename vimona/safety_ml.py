"""Lightweight dedicated Safe-Blur Micro-ML Engine (zero LLM overhead).

Combines:
1. TextSensitivityMLP: Blake2b n-gram/bigram hashed 2-layer neural classifier
   trained on explicit NSFW / graphic gore vs benign photography, cinema, portraits,
   action choreography, and code prompts (~0.2 ms CPU).
2. VisualSensitivityMLP: 28-feature perceptual OpenCV + NumPy classifier using
   YCbCr/HSV skin intersection, Haar frontal-face subtraction, 3x3 spatial torso
   distribution, Sobel seam/wood-grain texture entropy, and arterial gore chroma
   (~1.5 ms CPU, <150 KB serialized weights).
"""

from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np

WEIGHTS_PATH = Path(__file__).resolve().with_name("safety_ml_weights.npz")

TEXT_DIM = 1024
TEXT_THRESHOLD = 0.78
VISION_THRESHOLD = 0.86

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
    feats = np.zeros((28,), dtype=np.float32)
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

    # Subtract detected clothed/portrait faces + head halo so normal portraits aren't flagged as nudity
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

    # 3x3 spatial torso-skin layout (indices 4..12)
    cell_vals: list[float] = []
    for r_i in range(3):
        for c_i in range(3):
            patch = torso_skin[r_i * 42 : (r_i + 1) * 42 + 2, c_i * 42 : (c_i + 1) * 42 + 2]
            cell_vals.append(float(np.mean(patch)))
    feats[4:13] = cell_vals

    # Center vs border focus: portraits/wood spans border, intimate close-ups center heavily
    center_patch = torso_skin[24:104, 24:104]
    border_mask = np.ones((128, 128), dtype=bool)
    border_mask[24:104, 24:104] = False
    center_skin = float(np.mean(center_patch))
    border_skin = float(np.mean(torso_skin[border_mask]))
    feats[13] = center_skin
    feats[14] = border_skin
    feats[15] = max(-1.0, min(1.0, center_skin - border_skin))

    # Largest connected torso-skin component compactness
    num_lbl, _lbls, stats, _cent = cv2.connectedComponentsWithStats(torso_skin.astype(np.uint8), connectivity=8)
    largest_frac = 0.0
    if num_lbl > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        largest_frac = float(np.max(areas)) / float(128 * 128)
    feats[16] = largest_frac

    # Texture & Sobel architectural/wood edge energy inside skin vs full image
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

    # Arterial crimson gore signature (dark deep crimson separate from magenta neon & sky orange)
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

    # Horizontal-stripe score (separates wooden planks/cabin walls & blinds from curved anatomy)
    h_energy = float(np.mean(np.abs(gy)))
    v_energy = float(np.mean(np.abs(gx))) + 1e-5
    feats[25] = min(3.0, h_energy / v_energy) / 3.0

    # Global brightness & saturation distribution
    feats[26] = float(np.mean(sat))
    feats[27] = float(np.std(hue / 180.0))
    return feats


class SafeBlurMLEngine:
    """Runtime neural inference wrapper for Safe-Blur decisions (text + image + video)."""

    def __init__(self, weights_path: Path = WEIGHTS_PATH) -> None:
        self._weights: dict[str, np.ndarray] | None = None
        if weights_path.is_file():
            try:
                data = np.load(weights_path)
                self._weights = {k: data[k].astype(np.float32) for k in data.files}
            except Exception:
                self._weights = None

    @staticmethod
    def _sigmoid(x: np.ndarray | float) -> np.ndarray | float:
        x_clamped = np.clip(x, -30.0, 30.0)
        return 1.0 / (1.0 + np.exp(-x_clamped))

    def score_text(self, text: str | None) -> float:
        if not text or not text.strip():
            return 0.0
        if self._weights is None or "t_w1" not in self._weights:
            return 0.96 if _UNAMBIGUOUS_NSFW_RE.search(text) else 0.05
        x = featurize_text(text)
        h = np.maximum(0.0, x @ self._weights["t_w1"] + self._weights["t_b1"])
        logit = float(h @ self._weights["t_w2"] + self._weights["t_b2"])
        prob = float(self._sigmoid(logit))
        if _UNAMBIGUOUS_NSFW_RE.search(text):
            prob = max(prob, 0.94)
        return prob

    def classify_text(self, text: str | None, threshold: float = TEXT_THRESHOLD) -> tuple[bool, float]:
        prob = self.score_text(text)
        return (prob >= threshold, prob)

    def score_image_rgb(self, rgb: np.ndarray) -> float:
        if rgb is None or rgb.size == 0 or self._weights is None or "v_w1" not in self._weights:
            return 0.0
        x = featurize_image_rgb(rgb)
        mu = self._weights["v_mu"]
        sig = np.maximum(1e-4, self._weights["v_sig"])
        xn = (x - mu) / sig
        h = np.maximum(0.0, xn @ self._weights["v_w1"] + self._weights["v_b1"])
        logit = float(h @ self._weights["v_w2"] + self._weights["v_b2"])
        return float(self._sigmoid(logit))

    def classify_image_rgb(
        self,
        rgb: np.ndarray,
        context_text: str = "",
        threshold: float = VISION_THRESHOLD,
    ) -> tuple[bool, float]:
        t_prob = self.score_text(context_text) if context_text else 0.0
        if t_prob >= 0.88:
            return (True, max(t_prob, self.score_image_rgb(rgb)))
        v_prob = self.score_image_rgb(rgb)
        if context_text and t_prob >= 0.68 and v_prob >= 0.55:
            return (True, max(t_prob, v_prob))
        return (v_prob >= threshold, max(v_prob, t_prob * 0.7))

    @lru_cache(maxsize=256)
    def classify_file(
        self,
        file_path_str: str,
        prompt_hint: str = "",
        title: str = "",
    ) -> tuple[bool, float]:
        ctx = f"{title} {prompt_hint}".strip()
        ctx_flagged, ctx_prob = self.classify_text(ctx)
        if ctx_flagged:
            return (True, ctx_prob)

        if not file_path_str:
            return (False, ctx_prob)
        p = Path(file_path_str)
        if not p.is_file():
            return (False, ctx_prob)

        ext = p.suffix.lower()
        try:
            if ext in (".mp4", ".webm", ".mkv", ".mov", ".avi"):
                cap = cv2.VideoCapture(str(p))
                try:
                    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    idxs = [0] if n <= 2 else [0, n // 2, max(0, n - 2)]
                    scores: list[float] = []
                    flags = 0
                    for idx in idxs:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                        ok, bgr = cap.read()
                        if not ok or bgr is None:
                            continue
                        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                        flag, vp = self.classify_image_rgb(rgb, context_text=ctx)
                        scores.append(vp)
                        if flag:
                            flags += 1
                    if not scores:
                        return (False, ctx_prob * 0.7)
                    mean_p = sum(scores) / len(scores)
                    is_vid_flagged = flags >= 2 or (len(scores) == 1 and flags == 1) or mean_p >= VISION_THRESHOLD
                    return (is_vid_flagged, max(scores) if is_vid_flagged else mean_p)
                finally:
                    cap.release()
            else:
                bgr = cv2.imread(str(p))
                if bgr is None:
                    return (False, ctx_prob)
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                return self.classify_image_rgb(rgb, context_text=ctx)
        except Exception:
            return (False, ctx_prob)


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
