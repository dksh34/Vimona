"""Real image & MP4 video frame decoding for Vimona's GTK4 UI."""

from __future__ import annotations

import os
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

import cairo
import cv2
import gi
import numpy as np

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf


def pixbuf_to_cairo_surface(pb: GdkPixbuf.Pixbuf | None) -> cairo.ImageSurface | None:
    """Convert a GdkPixbuf.Pixbuf (RGB or RGBA) strictly in memory into a Cairo ARGB32 surface."""
    if pb is None:
        return None
    w, h = pb.get_width(), pb.get_height()
    if w <= 0 or h <= 0:
        return None
    n_ch = pb.get_n_channels()
    rowstride = pb.get_rowstride()
    raw = np.frombuffer(pb.get_pixels(), dtype=np.uint8).reshape((h, rowstride))
    src = raw[:, : w * n_ch].reshape((h, w, n_ch))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    dst = np.frombuffer(surf.get_data(), dtype=np.uint8).reshape(
        (h, surf.get_stride() // 4, 4)
    )[:, :w, :]
    dst[..., 0] = src[..., 2]
    dst[..., 1] = src[..., 1]
    dst[..., 2] = src[..., 0]
    if n_ch == 4:
        alpha = src[..., 3]
        if np.any(alpha < 255):
            af = alpha.astype(np.float32) / 255.0
            dst[..., 0] = (dst[..., 0].astype(np.float32) * af).astype(np.uint8)
            dst[..., 1] = (dst[..., 1].astype(np.float32) * af).astype(np.uint8)
            dst[..., 2] = (dst[..., 2].astype(np.float32) * af).astype(np.uint8)
        dst[..., 3] = alpha
    else:
        dst[..., 3] = 255
    surf.mark_dirty()
    return surf


_TEX_CACHE: OrderedDict[str, Gdk.Texture] = OrderedDict()


def load_image_texture(image_path: str | None) -> Gdk.Texture | None:
    """Load an image file from disk into a Gdk.Texture if present (never caches missing/empty)."""
    if not image_path or not os.path.isfile(image_path):
        return None
    cached = _TEX_CACHE.get(image_path)
    if cached is not None:
        _TEX_CACHE.move_to_end(image_path)
        return cached
    try:
        tex = Gdk.Texture.new_from_filename(image_path)
        if tex is not None:
            _TEX_CACHE[image_path] = tex
            while len(_TEX_CACHE) > 64:
                _TEX_CACHE.popitem(last=False)
        return tex
    except Exception:
        return None


@lru_cache(maxsize=32)
def load_image_surface(image_path: str | None) -> cairo.ImageSurface | None:
    """Load an image file from disk into a Cairo ImageSurface for zoomed inspection."""
    if not image_path or not os.path.isfile(image_path):
        return None
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file(image_path)
        return pixbuf_to_cairo_surface(pb)
    except Exception:
        return None


class VideoFrameDecoder:
    """Random-access OpenCV MP4 decoder returning Cairo ImageSurfaces."""

    def __init__(self, video_path: str | None) -> None:
        self._path = video_path
        self._cap: cv2.VideoCapture | None = None
        self.frame_count: int = 0
        self.fps: float = 16.0
        self.width: int = 832
        self.height: int = 480
        self._cache: OrderedDict[int, tuple[cairo.ImageSurface, np.ndarray]] = OrderedDict()

        if video_path and os.path.isfile(video_path):
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                self._cap = cap
                self.frame_count = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
                fps_val = float(cap.get(cv2.CAP_PROP_FPS) or 16.0)
                self.fps = fps_val if fps_val > 0 else 16.0
                self.width = max(1, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 832))
                self.height = max(1, int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480))

    @property
    def is_ready(self) -> bool:
        return self._cap is not None and self.frame_count > 0

    def get_frame_surface(self, frame_idx: int) -> cairo.ImageSurface | None:
        if not self.is_ready or self._cap is None:
            return None
        idx = max(0, min(self.frame_count - 1, frame_idx))
        if idx in self._cache:
            self._cache.move_to_end(idx)
            return self._cache[idx][0]

        current_pos = int(self._cap.get(cv2.CAP_PROP_POS_FRAMES))
        if abs(current_pos - idx) > 1 or current_pos > idx:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
        ok, bgr = self._cap.read()
        if not ok or bgr is None:
            return None
        bgra = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA))
        h, w, _ = bgra.shape
        surf = cairo.ImageSurface.create_for_data(bgra, cairo.FORMAT_ARGB32, w, h, w * 4)
        self._cache[idx] = (surf, bgra)
        if len(self._cache) > 48:
            self._cache.popitem(last=False)
        return surf

    def close(self) -> None:
        self._cache.clear()
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def probe_video_info(video_path: Path) -> tuple[float, int, int, int]:
    """Return (duration_s, fps, width, height) for a video file."""
    dec = VideoFrameDecoder(str(video_path))
    try:
        if not dec.is_ready:
            return (4.0, 16, 832, 480)
        dur = dec.frame_count / max(1.0, dec.fps)
        return (dur, int(round(dec.fps)), dec.width, dec.height)
    finally:
        dec.close()


def draw_neutral_frame(
    cr: cairo.Context,
    w: float,
    h: float,
    frame_label: str = "",
) -> None:
    """Draw a clean neutral graphite media frame when no media texture is attached."""
    cr.save()
    pat = cairo.LinearGradient(0, 0, 0, h)
    pat.add_color_stop_rgb(0.0, 0.11, 0.11, 0.11)
    pat.add_color_stop_rgb(1.0, 0.08, 0.08, 0.08)
    cr.set_source(pat)
    cr.rectangle(0, 0, w, h)
    cr.fill()

    if frame_label:
        cr.set_source_rgba(0.93, 0.93, 0.93, 0.35)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12.0)
        ext = cr.text_extents(frame_label)
        cr.move_to((w - ext.width) * 0.5, (h + ext.height) * 0.5)
        cr.show_text(frame_label)
    cr.restore()
