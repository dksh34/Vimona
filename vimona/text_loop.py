"""Native GTK4/Cairo port of React Bits <TextLoop /> curved ribbon marquee."""

from __future__ import annotations

import bisect
import math
import time
from typing import Literal

import cairo
import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

LoopShape = Literal["wave", "circle", "infinity", "arch", "line"]
LoopDirection = Literal["forward", "reverse"]

_VIEW_W = 1200.0
_VIEW_H = 520.0
_CX = _VIEW_W * 0.5
_CY = _VIEW_H * 0.5
_EDGE_PAD = 6.0
_MAX_FLING = 2600.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _smootherstep(t: float) -> float:
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def _hex_to_rgb(hex_str: str) -> tuple[float, float, float]:
    raw = hex_str.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) >= 6:
        try:
            return (
                int(raw[0:2], 16) / 255.0,
                int(raw[2:4], 16) / 255.0,
                int(raw[4:6], 16) / 255.0,
            )
        except ValueError:
            pass
    return (1.0, 1.0, 1.0)


def _tint_rgb(rgb: tuple[float, float, float], amount: float) -> tuple[float, float, float]:
    return (
        rgb[0] + (1.0 - rgb[0]) * amount,
        rgb[1] + (1.0 - rgb[1]) * amount,
        rgb[2] + (1.0 - rgb[2]) * amount,
    )


def _ink_on_rgb(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    vals = []
    for c in rgb:
        vals.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    lum = 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]
    if lum > 0.2:
        return (18 / 255.0, 15 / 255.0, 23 / 255.0)
    return (1.0, 1.0, 1.0)


def _quad_pts(
    p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float], n: int = 36
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for i in range(n + 1):
        t = i / n
        mt = 1.0 - t
        x = mt * mt * p0[0] + 2.0 * mt * t * p1[0] + t * t * p2[0]
        y = mt * mt * p0[1] + 2.0 * mt * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _cubic_pts(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    n: int = 44,
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for i in range(n + 1):
        t = i / n
        mt = 1.0 - t
        mt2 = mt * mt
        t2 = t * t
        x = mt2 * mt * p0[0] + 3.0 * mt2 * t * p1[0] + 3.0 * mt * t2 * p2[0] + t2 * t * p3[0]
        y = mt2 * mt * p0[1] + 3.0 * mt2 * t * p1[1] + 3.0 * mt * t2 * p2[1] + t2 * t * p3[1]
        pts.append((x, y))
    return pts


def _circle_radius(curviness: float, ribbon_width: float) -> float:
    return min(90.0 + max(0.0, curviness) * 0.95, max(20.0, _CY - max(0.0, ribbon_width) * 0.5 - _EDGE_PAD))


def _sample_shape(
    shape: LoopShape, curviness: float, ribbon_width: float, mirrored: bool = False
) -> tuple[list[tuple[float, float]], bool]:
    c = max(0.0, curviness)
    room = max(20.0, _CY - max(0.0, ribbon_width) * 0.5 - _EDGE_PAD)
    flip = -1.0 if mirrored else 1.0

    if shape == "circle":
        r = _circle_radius(curviness, ribbon_width)
        rx = r * 1.22 if mirrored else r
        ry = r * 0.42 if mirrored else r
        pts = [(_CX + rx * math.cos(math.pi + (i / 180) * math.tau), _CY + ry * math.sin(math.pi + (i / 180) * math.tau)) for i in range(181)]
        return (pts, True)

    if shape == "infinity":
        r = 150.0 + c * 1.4
        h = min(60.0 + c * 0.95, room)
        segs = [
            _cubic_pts((_CX, _CY), (_CX + r * 0.55, _CY - h), (_CX + r, _CY - h), (_CX + r, _CY)),
            _cubic_pts((_CX + r, _CY), (_CX + r, _CY + h), (_CX + r * 0.55, _CY + h), (_CX, _CY)),
            _cubic_pts((_CX, _CY), (_CX - r * 0.55, _CY - h), (_CX - r, _CY - h), (_CX - r, _CY)),
            _cubic_pts((_CX - r, _CY), (_CX - r, _CY + h), (_CX - r * 0.55, _CY + h), (_CX, _CY)),
        ]
        pts = segs[0] + segs[1][1:] + segs[2][1:] + segs[3][1:]
        return (pts, True)

    if shape == "arch":
        rise = min(120.0 + c * 1.1, room * 2.0) * flip
        pts = _quad_pts((120.0, _CY + rise * 0.5), (_CX, _CY - rise * 1.5), (_VIEW_W - 120.0, _CY + rise * 0.5), n=96)
        return (pts, False)

    if shape == "line":
        pts = [(-320.0 + (i / 40) * (_VIEW_W + 640.0), _CY) for i in range(41)]
        return (pts, False)

    # Default: "wave" — multi-arch smooth quadratic Bezier spline across -320..1520
    a = min(c * 2.2, room * 2.0) * flip
    anchors = [-320.0, 0.0, 320.0, 640.0, 960.0, 1280.0, _VIEW_W + 320.0]
    pts: list[tuple[float, float]] = []
    ctrl_y_sign = -1.0
    for idx in range(len(anchors) - 1):
        x0, x2 = anchors[idx], anchors[idx + 1]
        xc = (x0 + x2) * 0.5
        seg = _quad_pts((x0, _CY), (xc, _CY + a * ctrl_y_sign), (x2, _CY), n=34)
        if pts:
            pts.extend(seg[1:])
        else:
            pts.extend(seg)
        ctrl_y_sign *= -1.0
    return (pts, False)


class _CurveLUT:
    """Arc-length lookup table for fast exact point & tangent evaluation."""

    def __init__(
        self, pts: list[tuple[float, float]], closed: bool, tilt_deg: float
    ) -> None:
        self.closed = closed
        if tilt_deg:
            rad = math.radians(tilt_deg)
            ca, sa = math.cos(rad), math.sin(rad)
            self.pts = [
                (_CX + (px - _CX) * ca - (py - _CY) * sa, _CY + (px - _CX) * sa + (py - _CY) * ca)
                for px, py in pts
            ]
        else:
            self.pts = pts

        self.cum: list[float] = [0.0]
        total = 0.0
        for i in range(1, len(self.pts)):
            dx = self.pts[i][0] - self.pts[i - 1][0]
            dy = self.pts[i][1] - self.pts[i - 1][1]
            total += math.hypot(dx, dy)
            self.cum.append(total)
        self.length = max(1.0, total)

    def eval_at(self, s: float) -> tuple[float, float, float]:
        s = s % self.length
        idx = bisect.bisect_right(self.cum, s) - 1
        idx = max(0, min(len(self.pts) - 2, idx))
        s0, s1 = self.cum[idx], self.cum[idx + 1]
        seg_len = max(1e-6, s1 - s0)
        u = (s - s0) / seg_len
        p0, p1 = self.pts[idx], self.pts[idx + 1]
        x = p0[0] + (p1[0] - p0[0]) * u
        y = p0[1] + (p1[1] - p0[1]) * u
        ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
        return (x, y, ang)


class TextLoopWidget(Gtk.DrawingArea):
    """Smooth 60fps curved-ribbon text loop ported from React Bits <TextLoop />."""

    def __init__(
        self,
        text: str = "GENERATING",
        shape: LoopShape = "wave",
        speed: float = 95.0,
        direction: LoopDirection = "forward",
        separator: str = "✦",
        curviness: float = 82.0,
        tilt: float = 0.0,
        font_size: int = 44,
        font_weight: int = 800,
        letter_spacing: float = 2.0,
        uppercase: bool = True,
        ribbon: bool = True,
        ribbon_color: str = "#5227FF",
        ribbon_width: float = 78.0,
        edge_fade: float = 0.25,
        pause_on_hover: bool = True,
        cross: bool = False,
        cross_color: str | None = None,
        cross_angle: float = 14.0,
        draggable: bool = False,
        outline: bool = False,
        outline_width: float = 1.6,
        color: str | None = None,
        height_px: int = 88,
    ) -> None:
        super().__init__()
        self._raw_text = text
        self._shape = shape
        self._speed = max(0.0, float(speed))
        self._direction = direction
        self._separator = separator
        self._curviness = curviness
        self._tilt = tilt
        self._font_size = font_size
        self._font_weight = font_weight
        self._letter_spacing = letter_spacing
        self._uppercase = uppercase
        self._ribbon = ribbon
        self._ribbon_rgb = _hex_to_rgb(ribbon_color)
        self._ribbon_width = ribbon_width
        self._edge_fade = _clamp(edge_fade, 0.0, 1.0)
        self._pause_on_hover = pause_on_hover
        self._cross = cross
        self._cross_rgb = _hex_to_rgb(cross_color) if cross_color else _tint_rgb(self._ribbon_rgb, 0.62)
        self._cross_angle = cross_angle
        self._draggable = draggable
        self._outline = outline
        self._outline_width = outline_width
        self._fill_rgb = _hex_to_rgb(color) if color else ((1.0, 1.0, 1.0) if ribbon else (0.93, 0.94, 0.96))
        self._cross_fill_rgb = _ink_on_rgb(self._cross_rgb) if ribbon else self._cross_rgb
        self._cross_dim = 0.45 if (not ribbon and not cross_color) else 1.0

        self._travel = 0.0
        self._hover_scale = 1.0
        self._hover_target = 1.0
        self._fling = 0.0
        self._dragging = False
        self._last_drag = (0.0, 0.0, 0.0)
        self._drag_vel = 0.0
        self._last_tick = time.monotonic()
        self._tick_id: int | None = None

        self._rebuild_geometry()

        self.set_hexpand(True)
        self.set_size_request(-1, height_px)
        if draggable:
            self.set_cursor_from_name("grab")
        self.set_draw_func(self._on_draw)

        if pause_on_hover or draggable:
            motion = Gtk.EventControllerMotion()
            if pause_on_hover:
                motion.connect("enter", self._on_enter)
                motion.connect("leave", self._on_leave)
            if draggable:
                motion.connect("motion", self._on_motion)
            self.add_controller(motion)

        if draggable:
            click = Gtk.GestureClick()
            click.set_button(1)
            click.connect("pressed", self._on_press)
            click.connect("released", self._on_release)
            click.connect("cancel", self._on_release)
            self.add_controller(click)

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)

    def _rebuild_geometry(self) -> None:
        half = self._cross_angle * 0.5 if self._cross else 0.0
        self._main_angle = self._tilt + half
        self._cross_angle_deg = self._tilt - half
        pts_m, closed_m = _sample_shape(self._shape, self._curviness, self._ribbon_width, False)
        self._main_lut = _CurveLUT(pts_m, closed_m, self._main_angle)
        self._cross_lut: _CurveLUT | None = None
        if self._cross:
            mir = self._shape in ("wave", "arch", "circle")
            pts_c, closed_c = _sample_shape(self._shape, self._curviness, self._ribbon_width, mir)
            self._cross_lut = _CurveLUT(pts_c, closed_c, self._cross_angle_deg)

        base = self._raw_text.upper() if self._uppercase else self._raw_text
        gap = f"\u00A0{self._separator}\u00A0" if self._separator else "\u00A0\u00A0\u00A0"
        self._unit = f"{base}{gap}"
        self._glyphs_cache_key: tuple[str, int, int, float] | None = None
        self._glyph_layouts: list[tuple[str, Pango.Layout, float, float]] = []
        self._unit_advance: float = 0.0

    def set_text(self, text: str) -> None:
        if text != self._raw_text:
            self._raw_text = text
            self._rebuild_geometry()
            self.queue_draw()

    def _ensure_glyphs(self) -> None:
        key = (self._unit, self._font_size, self._font_weight, self._letter_spacing)
        if self._glyphs_cache_key == key:
            return
        self._glyphs_cache_key = key
        self._glyph_layouts = []
        desc = Pango.FontDescription.from_string(f"Inter {self._font_weight} {self._font_size}px")
        total = 0.0
        for ch in self._unit:
            lay = self.create_pango_layout(ch)
            lay.set_font_description(desc)
            lw, lh = lay.get_pixel_size()
            adv = max(4.0, float(lw)) + self._letter_spacing
            self._glyph_layouts.append((ch, lay, adv, float(lh)))
            total += adv
        self._unit_advance = max(10.0, total)

    def _on_realize(self, _w: Gtk.Widget) -> None:
        if self._tick_id is None:
            self._last_tick = time.monotonic()
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_unrealize(self, _w: Gtk.Widget) -> None:
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def _on_enter(self, *_a: object) -> None:
        self._hover_target = 0.0

    def _on_leave(self, *_a: object) -> None:
        self._hover_target = 1.0

    def _on_press(self, _g: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        self._dragging = True
        self._last_drag = (x, y, time.monotonic())
        self._drag_vel = 0.0
        self.set_cursor_from_name("grabbing")

    def _on_motion(self, _c: Gtk.EventControllerMotion, x: float, y: float) -> None:
        if not self._dragging:
            return
        now = time.monotonic()
        lx, ly, lt = self._last_drag
        scale = _VIEW_W / max(1.0, float(self.get_width()))
        ang = math.radians(self._main_angle)
        delta = ((x - lx) * math.cos(ang) + (y - ly) * math.sin(ang)) * scale
        self._travel += delta
        dt = max(0.004, now - lt)
        self._drag_vel = self._drag_vel * 0.6 + (delta / dt) * 0.4
        self._last_drag = (x, y, now)
        self.queue_draw()

    def _on_release(self, *_a: object) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.set_cursor_from_name("grab")
        now = time.monotonic()
        if now - self._last_drag[2] <= 0.09:
            sign = -1.0 if self._direction == "reverse" else 1.0
            base = self._speed * sign
            rel = _clamp(self._drag_vel, -_MAX_FLING, _MAX_FLING)
            self._fling = rel - base * self._hover_scale

    def _on_tick(self, _w: Gtk.Widget, _clock: Gdk.FrameClock) -> bool:
        now = time.monotonic()
        dt = min(0.05, max(0.001, now - self._last_tick))
        self._last_tick = now

        if not self._dragging:
            tau = 0.25 if self._hover_target < self._hover_scale else 0.35
            self._hover_scale += (self._hover_target - self._hover_scale) * (1.0 - math.exp(-dt / tau))
            self._fling *= math.exp(-dt / 0.55)
            sign = -1.0 if self._direction == "reverse" else 1.0
            v = self._speed * sign * self._hover_scale + self._fling
            if abs(v) >= 0.01:
                self._travel = (self._travel + v * dt) % self._main_lut.length
                self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw_band_and_text(
        self,
        cr: cairo.Context,
        lut: _CurveLUT,
        travel_s: float,
        ribbon_rgb: tuple[float, float, float],
        text_rgb: tuple[float, float, float],
        text_alpha: float,
    ) -> None:
        if self._ribbon:
            cr.save()
            cr.new_path()
            cr.move_to(lut.pts[0][0], lut.pts[0][1])
            for px, py in lut.pts[1:]:
                cr.line_to(px, py)
            if lut.closed:
                cr.close_path()
            cr.set_source_rgba(*ribbon_rgb, 1.0)
            cr.set_line_width(self._ribbon_width)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.stroke()
            cr.restore()

        if self._unit_advance <= 0:
            return
        reps = max(1, int(math.ceil(lut.length / self._unit_advance)) + 1)
        s_offset = travel_s % self._unit_advance
        # Start slightly before 0 so wrap seam stays completely seamless
        cur_s = s_offset - self._unit_advance
        end_s = lut.length + self._unit_advance

        while cur_s < end_s:
            for ch, lay, adv, lh in self._glyph_layouts:
                mid_s = cur_s + adv * 0.5
                if 0.0 <= mid_s <= lut.length or lut.closed:
                    px, py, ang = lut.eval_at(mid_s)
                    if -80.0 <= px <= _VIEW_W + 80.0 and -80.0 <= py <= _VIEW_H + 80.0:
                        cr.save()
                        cr.translate(px, py)
                        cr.rotate(ang)
                        cr.move_to(-(adv - self._letter_spacing) * 0.5, -lh * 0.52)
                        if self._outline:
                            PangoCairo.layout_path(cr, lay)
                            cr.set_source_rgba(*text_rgb, text_alpha)
                            cr.set_line_width(self._outline_width)
                            cr.set_line_join(cairo.LINE_JOIN_ROUND)
                            cr.stroke()
                        else:
                            cr.set_source_rgba(*text_rgb, text_alpha)
                            PangoCairo.show_layout(cr, lay)
                        cr.restore()
                cur_s += adv

    def _on_draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        if w <= 4 or h <= 4:
            return
        self._ensure_glyphs()

        cr.save()
        if self._edge_fade > 0.01:
            cr.push_group()

        scale = min(w / _VIEW_W, h / _VIEW_H)
        if self._shape in ("wave", "line"):
            scale = w / _VIEW_W
        tx = (w - _VIEW_W * scale) * 0.5
        ty = (h - _VIEW_H * scale) * 0.5
        cr.save()
        cr.translate(tx, ty)
        cr.scale(scale, scale)

        if self._cross and self._cross_lut is not None:
            self._draw_band_and_text(
                cr,
                self._cross_lut,
                -self._travel,
                self._cross_rgb,
                self._cross_fill_rgb,
                self._cross_dim,
            )
            if self._ribbon:
                # Soft drop shadow under main crossing ribbon
                cr.save()
                cr.translate(0.0, max(4.0, self._ribbon_width * 0.08))
                cr.new_path()
                cr.move_to(self._main_lut.pts[0][0], self._main_lut.pts[0][1])
                for px, py in self._main_lut.pts[1:]:
                    cr.line_to(px, py)
                cr.set_source_rgba(0.0, 0.0, 0.0, 0.34)
                cr.set_line_width(self._ribbon_width + 6.0)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.stroke()
                cr.restore()

        self._draw_band_and_text(
            cr,
            self._main_lut,
            self._travel,
            self._ribbon_rgb,
            self._fill_rgb,
            1.0,
        )
        cr.restore()

        if self._edge_fade > 0.01:
            pat = cr.pop_group()
            fade_w = _clamp(self._edge_fade, 0.0, 1.0) * 0.30
            mask = cairo.LinearGradient(0.0, 0.0, float(w), 0.0)
            for i in range(9):
                u = i / 8.0
                mask.add_color_stop_rgba(u * fade_w, 1.0, 1.0, 1.0, _smootherstep(u))
            for i in range(8, -1, -1):
                u = i / 8.0
                mask.add_color_stop_rgba(1.0 - u * fade_w, 1.0, 1.0, 1.0, _smootherstep(u))
            cr.set_source(pat)
            cr.mask(mask)
        cr.restore()
