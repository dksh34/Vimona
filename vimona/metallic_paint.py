"""Native GTK4/Cairo/NumPy port of React Bits <MetallicPaint /> for Vimona logos."""

from __future__ import annotations

from functools import lru_cache
import math
import time
from typing import Callable

import cairo
import gi
import numpy as np

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Gtk, Pango, PangoCairo

PI = math.pi


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def _hex_to_rgba(hex_color: str) -> tuple[float, float, float, float]:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 6:
        return (
            int(h[0:2], 16) / 255.0,
            int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0,
            1.0,
        )
    if len(h) == 8:
        return (
            int(h[0:2], 16) / 255.0,
            int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0,
            int(h[6:8], 16) / 255.0,
        )
    return (1.0, 1.0, 1.0, 1.0)


def _smoothstep(
    edge0: np.ndarray | float,
    edge1: np.ndarray | float,
    x: np.ndarray | float,
) -> np.ndarray | float:
    diff = edge1 - edge0
    if isinstance(diff, np.ndarray):
        safe = np.where(np.abs(diff) < 1e-6, 1e-6, diff)
        t = np.clip((x - edge0) / safe, 0.0, 1.0)
        return t * t * (3.0 - 2.0 * t)
    if abs(diff) < 1e-6:
        return np.where(x >= edge1, 1.0, 0.0) if isinstance(x, np.ndarray) else (1.0 if x >= edge1 else 0.0)
    t = np.clip((x - edge0) / diff, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _snoise_2d(vx: np.ndarray, vy: np.ndarray) -> np.ndarray:
    """Vectorized NumPy match of GLSL 2D Simplex Noise (`snoise`)."""
    c_x = 0.211324865405187
    c_y = 0.366025403784439
    c_z = -0.577350269189626
    c_w = 0.024390243902439

    s = (vx + vy) * c_y
    ix = np.floor(vx + s)
    iy = np.floor(vy + s)
    t = (ix + iy) * c_x
    x0 = vx - (ix - t)
    y0 = vy - (iy - t)

    cond = x0 > y0
    i1x = np.where(cond, 1.0, 0.0).astype(np.float32)
    i1y = np.where(cond, 0.0, 1.0).astype(np.float32)

    x1 = x0 - i1x + c_x
    y1 = y0 - i1y + c_x
    x2 = x0 + c_z
    y2 = y0 + c_z

    ix_m = np.mod(ix, 289.0)
    iy_m = np.mod(iy, 289.0)

    def _perm(val: np.ndarray) -> np.ndarray:
        return np.mod(((val * 34.0) + 1.0) * val, 289.0)

    py = _perm(iy_m)
    py1 = _perm(iy_m + i1y)
    py2 = _perm(iy_m + 1.0)

    p0 = _perm(py + ix_m)
    p1 = _perm(py1 + ix_m + i1x)
    p2 = _perm(py2 + ix_m + 1.0)

    def _contrib(p: np.ndarray, dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
        m = np.maximum(0.5 - (dx * dx + dy * dy), 0.0)
        m2 = m * m
        m4 = m2 * m2
        x = 2.0 * np.mod(p * c_w, 1.0) - 1.0
        h = np.abs(x) - 0.5
        ox = np.floor(x + 0.5)
        a0 = x - ox
        norm = 1.79284291400159 - 0.85373472095314 * (a0 * a0 + h * h)
        g = a0 * dx + h * dy
        return m4 * norm * g

    return 130.0 * (_contrib(p0, x0, y0) + _contrib(p1, x1, y1) + _contrib(p2, x2, y2))


def draw_vimona_emblem_mask(cr: cairo.Context, w: float, h: float, scale_fill: float = 0.88) -> None:
    """Draw Vimona's geometric winged 'V' emblem into the mask surface.

    Shaped to give wide sculpted ribbons so the Poisson depth curvature and
    diagonal chrome bands flow clearly inside both wings.
    """
    s = min(w, h) * scale_fill
    cr.save()
    cr.translate((w - s) * 0.5, (h - s) * 0.5)
    cr.scale(s / 24.0, s / 24.0)

    cr.set_source_rgba(0.0, 0.0, 0.0, 1.0)

    # Rear right ribbon wing
    cr.move_to(11.3, 20.1)
    cr.line_to(18.2, 4.4)
    cr.line_to(21.9, 4.4)
    cr.line_to(13.8, 20.1)
    cr.close_path()
    cr.fill()

    # Main foreground ribbon wing
    cr.move_to(2.1, 4.4)
    cr.line_to(6.6, 4.4)
    cr.line_to(14.0, 20.1)
    cr.line_to(9.7, 20.1)
    cr.close_path()
    cr.fill()

    cr.restore()


@lru_cache(maxsize=24)
def _build_cached_shape_field(
    shape_key: str,
    text: str,
    font_family: str,
    font_weight: int,
    width: int,
    height: int,
    scale_fill: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rasterize vector shape -> alpha mask, run vectorized Red-Black SOR Poisson depth solver,
    and compute 3x3 blurred edge distance map (`img.r`, `img.g` equivalent).
    """
    surf = cairo.ImageSurface(cairo.Format.ARGB32, width, height)
    cr = cairo.Context(surf)
    cr.set_operator(cairo.Operator.CLEAR)
    cr.paint()
    cr.set_operator(cairo.Operator.OVER)

    if text:
        layout = PangoCairo.create_layout(cr)
        weight_str = "Heavy" if font_weight >= 800 else ("Bold" if font_weight >= 700 else "Medium")
        font_pt = max(12.0, height * 0.48 * scale_fill)
        layout.set_font_description(Pango.FontDescription.from_string(f"{font_family} {weight_str} {font_pt:.1f}"))
        layout.set_text(text, -1)
        _ink, log = layout.get_pixel_extents()
        if log.width > 0 and log.height > 0:
            fit = min((width * scale_fill) / log.width, (height * scale_fill) / log.height, 1.4)
            cr.save()
            cr.translate(width * 0.5, height * 0.5)
            cr.scale(fit, fit)
            cr.translate(-log.width * 0.5, -log.height * 0.5)
            cr.set_source_rgba(0.0, 0.0, 0.0, 1.0)
            PangoCairo.show_layout(cr, layout)
            cr.restore()
    else:
        draw_vimona_emblem_mask(cr, float(width), float(height), scale_fill=scale_fill)

    surf.flush()
    raw = np.frombuffer(surf.get_data(), dtype=np.uint8).reshape((height, width, 4))
    alpha_f = raw[..., 3].astype(np.float32) / 255.0
    mask = alpha_f > 0.08

    # Interior pixels (not adjacent to mask boundary / canvas edge)
    interior = np.zeros_like(mask)
    if height > 2 and width > 2:
        core = (
            mask[1:-1, 1:-1]
            & mask[1:-1, :-2]
            & mask[1:-1, 2:]
            & mask[:-2, 1:-1]
            & mask[2:, 1:-1]
            & mask[:-2, :-2]
            & mask[:-2, 2:]
            & mask[2:, :-2]
            & mask[2:, 2:]
        )
        interior[1:-1, 1:-1] = core

    yy, xx = np.indices((height, width))
    red_mask = interior & (((xx + yy) & 1) == 0)
    black_mask = interior & (((xx + yy) & 1) == 1)

    field = np.zeros((height, width), dtype=np.float32)
    # 44 iterations of vectorized Red-Black SOR relaxation (matching GL/JS Poisson pass)
    for _ in range(44):
        nbr_r = (
            field[1:-1, 2:]
            + field[1:-1, :-2]
            + field[:-2, 1:-1]
            + field[2:, 1:-1]
        )
        upd_r = 1.9 * ((0.01 + nbr_r) * 0.25) - 0.9 * field[1:-1, 1:-1]
        rm_inner = red_mask[1:-1, 1:-1]
        field[1:-1, 1:-1] = np.where(rm_inner, upd_r, field[1:-1, 1:-1])

        nbr_b = (
            field[1:-1, 2:]
            + field[1:-1, :-2]
            + field[:-2, 1:-1]
            + field[2:, 1:-1]
        )
        upd_b = 1.9 * ((0.01 + nbr_b) * 0.25) - 0.9 * field[1:-1, 1:-1]
        bm_inner = black_mask[1:-1, 1:-1]
        field[1:-1, 1:-1] = np.where(bm_inner, upd_b, field[1:-1, 1:-1])

    peak = float(np.max(field[interior])) if np.any(interior) else 1.0
    if peak <= 1e-6:
        peak = 1.0

    # depth = 1.0 at boundary / outside, -> 0.0 at thickest center
    depth = np.where(mask, np.clip(1.0 - field / peak, 0.0, 1.0), 1.0).astype(np.float32)

    # Exact blurEdge3x3 equivalent (r ~ 1.5..2.5 texels scaled softly to resolution)
    pad = np.pad(depth, 2, mode="edge")
    edge_blur = (
        4.0 * pad[2:-2, 2:-2]
        + 2.0 * (pad[0:-4, 2:-2] + pad[4:, 2:-2] + pad[2:-2, 0:-4] + pad[2:-2, 4:])
        + (pad[0:-4, 0:-4] + pad[0:-4, 4:] + pad[4:, 0:-4] + pad[4:, 4:])
    ) / 16.0

    return depth.astype(np.float32), alpha_f.astype(np.float32), edge_blur.astype(np.float32)


class MetallicPaintWidget(Gtk.DrawingArea):
    """Liquid chrome/gold/tinted metallic paint logo shader rendered natively in GTK4."""

    def __init__(
        self,
        size: int = 88,
        width_px: int | None = None,
        height_px: int | None = None,
        text: str = "",
        font_family: str = "Sans",
        font_weight: int = 800,
        color: str = "#ffffff",
        density: float = 2.0,
        softness: float = 0.10,
        dispersion: float = 0.34,
        distortion: float = 0.08,
        edge_bend: float = 0.65,
        angle: float = 70.0,
        speed: float = 1.0,
        scale: float = 0.84,
        mouse_tilt: float = 0.50,
        light_mode: bool = False,
        intro: bool = True,
        paused: bool = False,
        follow_pointer: bool = True,
    ) -> None:
        super().__init__()
        self._w = width_px if width_px is not None else size
        self._h = height_px if height_px is not None else size
        self._text = text
        self._font_family = font_family
        self._font_weight = font_weight
        self._tint_rgba = _hex_to_rgba(color)

        self._density = _clamp(density, 0.5, 12.0)
        self._softness = _clamp(softness, 0.0, 1.0)
        self._dispersion = _clamp(dispersion, -1.0, 1.0)
        self._distortion = _clamp(distortion, 0.0, 1.0)
        self._edge_bend = _clamp(edge_bend, 0.0, 1.0)
        self._angle = angle
        self._speed = speed
        self._scale = _clamp(scale, 0.15, 0.96)
        self._mouse_tilt = _clamp(mouse_tilt, 0.0, 1.5)
        self._light_mode = light_mode
        self._intro = intro
        self._paused = paused
        self._follow_pointer = follow_pointer

        self.set_content_width(self._w)
        self.set_content_height(self._h)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._on_draw)

        self._time: float = 0.0
        self._last_t: float = time.monotonic()
        self._reveal: float = 0.0 if intro else 1.0
        self._reveal_start: float | None = None
        self._tilt_x: float = 0.0
        self._tilt_y: float = 0.0
        self._vel_x: float = 0.0
        self._vel_y: float = 0.0
        self._target_x: float = 0.0
        self._target_y: float = 0.0
        self._frozen_mode: bool = False
        self._tick_id: int | None = None

        # Cached shader precomputation keyed by integer size
        self._cache_dims: tuple[int, int] = (0, 0)
        self._pre: dict[str, np.ndarray] = {}

        if follow_pointer:
            motion = Gtk.EventControllerMotion()
            motion.connect("motion", self._on_motion)
            motion.connect("leave", self._on_leave)
            self.add_controller(motion)

        self.connect("map", lambda _w: self._sync_tick())
        self.connect("unmap", lambda _w: self._stop_tick())
        self.connect("destroy", lambda _w: self._stop_tick())

    # ------------------------------------------------------------------
    # Public Controls
    # ------------------------------------------------------------------

    def set_global_pointer(self, wx: float, wy: float, root_win: Gtk.Widget) -> None:
        if not self._follow_pointer or not self.get_mapped():
            return
        res = root_win.translate_coordinates(self, wx, wy)
        if res is None:
            return
        lx, ly = res
        w = max(1.0, float(self.get_width()))
        h = max(1.0, float(self.get_height()))
        # Gentle global parallax tilt (-0.75..0.75 across window)
        nx = _clamp(((lx / w) * 2.0 - 1.0) * 0.35, -1.1, 1.1)
        ny = _clamp(((ly / h) * 2.0 - 1.0) * 0.35, -1.1, 1.1)
        self._target_x = nx
        self._target_y = ny

    def clear_pointer(self) -> None:
        self._target_x = 0.0
        self._target_y = 0.0

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._stop_tick()
        else:
            self._sync_tick()

    def snapshot_at(self, time_s: float = 1.6, reveal: float = 1.0) -> None:
        """Deterministically set phase for testing/verification snapshots."""
        self._frozen_mode = True
        self._time = time_s
        self._reveal = _clamp(reveal, 0.0, 1.0)
        self._stop_tick()
        self.queue_draw()

    # ------------------------------------------------------------------
    # Animation & Input
    # ------------------------------------------------------------------

    def _on_motion(self, _ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        w = max(1.0, float(self.get_width()))
        h = max(1.0, float(self.get_height()))
        self._target_x = _clamp(((x / w) * 2.0 - 1.0), -1.2, 1.2)
        self._target_y = _clamp(((y / h) * 2.0 - 1.0), -1.2, 1.2)

    def _on_leave(self, _ctrl: Gtk.EventControllerMotion) -> None:
        self._target_x = 0.0
        self._target_y = 0.0

    def _sync_tick(self) -> None:
        if self._frozen_mode or self._paused or not self.get_mapped():
            self._stop_tick()
            return
        if self._tick_id is None and (self._speed != 0.0 or self._reveal < 1.0):
            self._last_t = time.monotonic()
            self._tick_id = GLib.timeout_add(30, self._on_tick)

    def _stop_tick(self) -> None:
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _on_tick(self) -> bool:
        if not self.get_mapped() or self._paused or self._frozen_mode:
            self._tick_id = None
            return False
        now = time.monotonic()
        dt = min(0.05, max(0.001, now - self._last_t))
        self._last_t = now

        if self._speed != 0.0:
            self._time += dt * self._speed

        if self._reveal < 1.0:
            if self._reveal_start is None:
                self._reveal_start = now
            prog = _clamp((now - self._reveal_start) / 1.35, 0.0, 1.0)
            self._reveal = 1.0 - (1.0 - prog) ** 3
            if self._speed != 0.0:
                self._time += dt * 1.8 * (1.0 - prog) ** 2

        stiffness = 60.0
        damping = 14.0
        self._vel_x += (stiffness * (self._target_x * self._mouse_tilt - self._tilt_x) - damping * self._vel_x) * dt
        self._vel_y += (stiffness * (self._target_y * self._mouse_tilt - self._tilt_y) - damping * self._vel_y) * dt
        self._tilt_x += self._vel_x * dt
        self._tilt_y += self._vel_y * dt

        self.queue_draw()
        return True

    # ------------------------------------------------------------------
    # Vectorized <MetallicPaint /> Fragment Shader Evaluation
    # ------------------------------------------------------------------

    def _ensure_precomputed(self, w: int, h: int) -> dict[str, np.ndarray]:
        if self._cache_dims == (w, h) and self._pre:
            return self._pre

        # Compute super-sampled shape & shader buffer at 2x when <= 128px for retina sharpness
        scale_ss = 2 if max(w, h) <= 128 else 1
        sw, sh = w * scale_ss, h * scale_ss
        depth, alpha, edge_blur = _build_cached_shape_field(
            "text" if self._text else "vimona",
            self._text,
            self._font_family,
            self._font_weight,
            sw,
            sh,
            self._scale,
        )

        xs = (np.arange(sw, dtype=np.float32) + 0.5) / float(sw)
        ys = (np.arange(sh, dtype=np.float32) + 0.5) / float(sh)
        uv_x, uv_y = np.meshgrid(xs, ys)

        ang = (-self._angle + 70.0) * PI / 180.0
        cos_a = math.cos(ang)
        sin_a = math.sin(ang)
        rx = uv_x - 0.5
        ry = uv_y - 0.5
        rot_x = (rx * cos_a - ry * sin_a) + 0.5
        rot_y = (rx * sin_a + ry * cos_a) + 0.5

        edge = (np.power(np.clip(edge_blur, 0.0, 1.0), 1.6) * _smoothstep(0.0, 0.4, self._edge_bend)).astype(np.float32)
        diag_bl_tr = (rot_x - rot_y).astype(np.float32)
        diag_tl_br = (rot_x + rot_y).astype(np.float32)

        # Active mask bounding box speeds up rendering (~2-3x fewer pixels computed)
        ys_nz, xs_nz = np.nonzero(alpha > 0.005)
        if len(xs_nz) > 0:
            x0, x1 = int(xs_nz.min()), int(xs_nz.max()) + 1
            y0, y1 = int(ys_nz.min()), int(ys_nz.max()) + 1
        else:
            x0, x1, y0, y1 = 0, sw, 0, sh

        sl = (slice(y0, y1), slice(x0, x1))
        sub_edge = edge[sl]
        sub_depth = depth[sl]
        sub_alpha = alpha[sl]
        sub_uv_x = uv_x[sl]
        sub_uv_y = uv_y[sl]
        sub_bl_tr = diag_bl_tr[sl]
        sub_tl_br = diag_tl_br[sl]

        self._cache_dims = (w, h)
        self._pre = {
            "ss": np.array(scale_ss),
            "x0": np.array(x0),
            "x1": np.array(x1),
            "y0": np.array(y0),
            "y1": np.array(y1),
            "w": np.array(sw),
            "h": np.array(sh),
            "uv_x": sub_uv_x,
            "uv_y": sub_uv_y,
            "edge": sub_edge,
            "depth": sub_depth,
            "alpha": sub_alpha,
            "bl_tr": sub_bl_tr,
            "tl_br": sub_tl_br,
        }
        return self._pre

    @staticmethod
    def _color_changes(
        c1: np.ndarray | float,
        c2: np.ndarray | float,
        stripe: np.ndarray,
        w0: np.ndarray,
        w1: np.ndarray,
        w2: np.ndarray,
        blur: np.ndarray,
        bump: np.ndarray,
        tint_c: float,
        tint_a: float,
    ) -> np.ndarray:
        def _mix(a: np.ndarray | float, b: np.ndarray | float, t: np.ndarray | float) -> np.ndarray | float:
            return a * (1.0 - t) + b * t

        ch = _mix(c2, c1, _smoothstep(0.0, 2.0 * blur, stripe))
        border = w0
        ch = _mix(ch, c2, _smoothstep(border, border + 2.0 * blur, stripe))

        bump_s = _smoothstep(0.2, 0.8, bump)
        border = w0 + 0.4 * (1.0 - bump_s) * w1
        ch = _mix(ch, c1, _smoothstep(border, border + 2.0 * blur, stripe))

        border = w0 + 0.5 * (1.0 - bump_s) * w1
        ch = _mix(ch, c2, _smoothstep(border, border + 2.0 * blur, stripe))

        border = w0 + w1
        ch = _mix(ch, c1, _smoothstep(border, border + 2.0 * blur, stripe))

        grad_t = (stripe - w0 - w1) / np.maximum(w2, 1e-4)
        gradient = _mix(c1, c2, _smoothstep(0.0, 1.0, grad_t))
        ch = _mix(ch, gradient, _smoothstep(border, border + 0.5 * blur, stripe))

        tinted = 1.0 - np.minimum(1.0, (1.0 - ch) / max(tint_c, 1e-4))
        ch = ch * (1.0 - tint_a) + tinted * tint_a
        return np.clip(ch, 0.0, 1.0)

    def _on_draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int) -> None:
        if width <= 2 or height <= 2:
            return
        pre = self._ensure_precomputed(width, height)
        ss = float(int(pre["ss"]))
        sw, sh = int(pre["w"]), int(pre["h"])
        x0, x1 = int(pre["x0"]), int(pre["x1"])
        y0, y1 = int(pre["y0"]), int(pre["y1"])
        bw, bh = x1 - x0, y1 - y0
        if bw <= 0 or bh <= 0:
            return

        uv_x = pre["uv_x"]
        uv_y = pre["uv_y"]
        edge0 = pre["edge"]
        depth = pre["depth"]
        opacity = pre["alpha"]
        bl_tr = pre["bl_tr"]
        tl_br = pre["tl_br"]

        t = 0.3 * (self._time + 2.8)
        cycle_width = self._density
        tilt_x = self._tilt_x
        tilt_y = self._tilt_y

        grad_ux = uv_x - 0.5 - tilt_x * 0.18
        grad_uy = uv_y - 0.5 + tilt_y * 0.12
        dist = np.hypot(grad_ux, grad_uy + 0.2 * bl_tr)

        theta = (0.25 - 0.2 * bl_tr) * PI
        ct = np.cos(theta)
        st = np.sin(theta)
        direction = ct * grad_ux + st * grad_uy

        bump = 1.0 - np.power(1.8 * dist, 1.2)
        bump = bump * np.power(np.maximum(uv_y, 1e-4), 0.3)

        thin_strip1 = 0.12 / cycle_width * (1.0 - 0.4 * bump)
        thin_strip2 = 0.07 / cycle_width * (1.0 + 0.4 * bump)
        wide_strip = 1.0 - thin_strip1 - thin_strip2

        noise = _snoise_2d(uv_x - t, uv_y - t)
        edge = edge0 + (1.0 - edge0) * self._distortion * noise

        edge_s = _smoothstep(0.0, 1.0, edge)
        contour_s = _smoothstep(0.5, 1.0, self._edge_bend)
        direction = direction + bl_tr
        direction = direction - 2.0 * noise * bl_tr * (edge_s * (1.0 - edge_s))
        direction = direction * ((1.0 - contour_s) + (1.0 - edge) * contour_s)
        direction = direction - 1.7 * edge * contour_s
        direction = direction + 0.2 * (self._edge_bend ** 4) * (1.0 - edge_s)

        bump = bump * np.clip(np.power(np.maximum(uv_y, 1e-4), 0.1), 0.3, 1.0)
        direction = direction * (0.1 + (1.1 - edge) * bump)
        direction = direction * (0.4 + 0.6 * (1.0 - _smoothstep(0.5, 1.0, edge)))
        direction = direction + 0.18 * (_smoothstep(0.1, 0.2, uv_y) * (1.0 - _smoothstep(0.2, 0.4, uv_y)))
        direction = direction + 0.03 * (_smoothstep(0.1, 0.2, 1.0 - uv_y) * (1.0 - _smoothstep(0.2, 0.4, 1.0 - uv_y)))
        direction = direction * (0.5 + 0.5 * uv_y * uv_y)
        direction = direction * cycle_width
        direction = direction - (t + tilt_x * 0.35 + tilt_y * 0.2)

        color_disp = np.clip(1.0 - bump, 0.0, 1.0)
        disp_red = (
            color_disp
            + 0.03 * bump * noise
            + 5.0
            * (_smoothstep(-0.1, 0.2, uv_y) * (1.0 - _smoothstep(0.1, 0.5, uv_y)))
            * (_smoothstep(0.4, 0.6, bump) * (1.0 - _smoothstep(0.4, 1.0, bump)))
            - bl_tr
        ) * (self._dispersion / 20.0)
        disp_blue = (
            color_disp * 1.3
            + (_smoothstep(0.0, 0.4, uv_y) * (1.0 - _smoothstep(0.1, 0.8, uv_y)))
            * (_smoothstep(0.4, 0.6, bump) * (1.0 - _smoothstep(0.4, 0.8, bump)))
            - 0.2 * edge
        ) * (self._dispersion / 20.0)

        softness = 0.05 * self._softness
        small_canvas = 1.0 - _smoothstep(100.0, 500.0, float(min(sw, sh)))
        blur = (
            softness
            + 0.5 * _smoothstep(1.0, 10.0, cycle_width) * edge_s
            + small_canvas * edge_s
            + 0.025
        )
        r_extra_blur = softness * (0.05 + 0.1 * (self._dispersion / 20.0) * bump)
        g_extra_blur = softness * 0.05 / np.maximum(0.001, np.abs(1.0 - bl_tr))

        w0 = thin_strip1 * cycle_width
        w1 = thin_strip2 * cycle_width - 0.02 * _smoothstep(0.0, 1.0, edge + bump)
        w2 = wide_strip

        light = 1.0 if self._light_mode else 0.0
        c1_r = 0.98 * (1.0 - light) + 0.90 * light
        c1_g = 0.98 * (1.0 - light) + 0.90 * light
        c1_b = 1.00 * (1.0 - light) + 0.93 * light
        # Keep dark reflection valleys above #141414 canvas so dark letters stay legible
        c2_rg = 0.19 if not self._light_mode else 0.10
        c2_b = (0.21 if not self._light_mode else 0.10) + 0.10 * _smoothstep(0.7, 1.3, tl_br)

        tr, tg, tb, ta = self._tint_rgba
        stripe_r = np.mod(direction + disp_red, 1.0)
        stripe_g = np.mod(direction, 1.0)
        stripe_b = np.mod(direction - disp_blue, 1.0)

        r = self._color_changes(c1_r, c2_rg, stripe_r, w0, w1, w2, blur + r_extra_blur, bump, tr, ta)
        g = self._color_changes(c1_g, c2_rg, stripe_g, w0, w1, w2, blur + g_extra_blur, bump, tg, ta)
        b = self._color_changes(c1_b, c2_b, stripe_b, w0, w1, w2, blur, bump, tb, ta)

        # Sheen tint blend
        sheen_r = 1.0 * (1.0 - 0.45 * ta) + tr * (0.45 * ta)
        sheen_g = 1.0 * (1.0 - 0.45 * ta) + tg * (0.45 * ta)
        sheen_b = 1.0 * (1.0 - 0.45 * ta) + tb * (0.45 * ta)
        r *= sheen_r
        g *= sheen_g
        b *= sheen_b

        if self._light_mode:
            rim = _smoothstep(0.78, 1.0, depth)
            r *= 1.0 - rim * 0.62
            g *= 1.0 - rim * 0.62
            b *= 1.0 - rim * 0.58

        # Outline-inward liquid pour intro
        pour = self._reveal * 1.25 - 0.1
        out_alpha = opacity * _smoothstep(1.0 - pour, 1.15 - pour, depth) * _smoothstep(0.0, 0.25, self._reveal)

        # Crisp liquid silver bevel highlight along shape border on dark backgrounds
        bevel = np.clip(1.0 - np.abs(depth - 0.86) * 3.8, 0.0, 1.0) * 0.24 * (1.0 - light)
        r = np.clip(r + bevel * sheen_r, 0.0, 1.0)
        g = np.clip(g + bevel * sheen_g, 0.0, 1.0)
        b = np.clip(b + bevel * sheen_b, 0.0, 1.0)

        buf = np.empty((bh, bw, 4), dtype=np.uint8)
        buf[..., 0] = np.clip(b * out_alpha * 255.0 + 0.5, 0, 255).astype(np.uint8)
        buf[..., 1] = np.clip(g * out_alpha * 255.0 + 0.5, 0, 255).astype(np.uint8)
        buf[..., 2] = np.clip(r * out_alpha * 255.0 + 0.5, 0, 255).astype(np.uint8)
        buf[..., 3] = np.clip(out_alpha * 255.0 + 0.5, 0, 255).astype(np.uint8)

        surf = cairo.ImageSurface.create_for_data(
            bytearray(buf.tobytes()),
            cairo.Format.ARGB32,
            bw,
            bh,
            bw * 4,
        )
        cr.save()
        if ss > 1.0:
            cr.scale(1.0 / ss, 1.0 / ss)
        cr.set_source_surface(surf, float(x0), float(y0))
        cr.paint()
        cr.restore()
