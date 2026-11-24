"""Native GTK4/Cairo/NumPy port of React Bits <GooeyNav /> for Vimona."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
import time
from typing import Callable

import cairo
import gi
import numpy as np

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("Rsvg", "2.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo, Rsvg

from .icons import ICON_PATHS, _get_svg_handle


@dataclass
class GooeyNavItem:
    id: str
    label: str
    icon: str | None = None
    badge_count: int = 0
    tooltip: str = ""


@dataclass
class _Droplet:
    p0x: float
    p0y: float
    p1x: float
    p1y: float
    p2x: float
    p2y: float
    radius: float
    delay: float
    dur: float
    rgb: tuple[float, float, float]


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def _auto_contrast_ink(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    lum = 0.2126 * _lin(rgb[0]) + 0.7152 * _lin(rgb[1]) + 0.0722 * _lin(rgb[2])
    if lum > 0.38:
        return _hex_to_rgb("#16151c")
    return _hex_to_rgb("#f4f4f5")


def _bez2(p0: float, p1: float, p2: float, t: float) -> float:
    mt = 1.0 - t
    return mt * mt * p0 + 2.0 * mt * t * p1 + t * t * p2


def _eval_bezier_pos(d: _Droplet, e: float) -> tuple[float, float]:
    return (
        _bez2(d.p0x, d.p1x, d.p2x, e),
        _bez2(d.p0y, d.p1y, d.p2y, e),
    )


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    r = max(0, min(255, int(round(rgb[0] * 255.0))))
    g = max(0, min(255, int(round(rgb[1] * 255.0))))
    b = max(0, min(255, int(round(rgb[2] * 255.0))))
    return f"#{r:02x}{g:02x}{b:02x}"


def _draw_svg_path_icon(
    cr: cairo.Context,
    icon_name: str,
    x: float,
    y: float,
    size_px: float,
    rgb: tuple[float, float, float],
    alpha: float,
) -> None:
    if icon_name not in ICON_PATHS or alpha <= 0.01:
        return
    handle = _get_svg_handle(icon_name, _rgb_to_hex(rgb), 1.85)
    viewport = Rsvg.Rectangle()
    viewport.x = 0.0
    viewport.y = 0.0
    viewport.width = size_px
    viewport.height = size_px
    cr.save()
    cr.translate(x, y)
    if alpha >= 0.99:
        handle.render_document(cr, viewport)
    else:
        cr.push_group()
        handle.render_document(cr, viewport)
        cr.pop_group_to_source()
        cr.paint_with_alpha(alpha)
    cr.restore()


class GooeyNavWidget(Gtk.DrawingArea):
    """Liquid metaball-morphing navbar ported from React Bits <GooeyNav />.

    Uses a real-time 2D vectorized signed-distance field (`sdPill`, `sdCapsule`,
    polynomial smooth-minimum metaball fusion) rendered via Cairo + contrast-masked
    text/icon layers so the active indicator bursts into droplets and melts into
    the newly selected pill.
    """

    def __init__(
        self,
        items: list[GooeyNavItem],
        initial_active_index: int = 0,
        pill_color: str = "#f4f4f5",
        droplet_colors: list[str] | None = None,
        particle_count: int = 15,
        spread: float = 54.0,
        animation_time_ms: float = 560.0,
        time_variance_ms: float = 260.0,
        gooeyness: float = 0.55,
        wobble: float = 0.55,
        frame: bool = True,
        hover_effect: bool = True,
        height_px: int = 44,
        on_change: Callable[[int, GooeyNavItem], None] | None = None,
    ) -> None:
        super().__init__()
        self._items: list[GooeyNavItem] = list(items)
        self._active_index: int = max(0, min(initial_active_index, max(0, len(self._items) - 1)))
        self._on_change = on_change

        self._pill_rgb = _hex_to_rgb(pill_color)
        self._active_ink_rgb = _auto_contrast_ink(self._pill_rgb)
        self._base_ink_rgb = _hex_to_rgb("#eef1f7")
        self._droplet_rgbs = (
            [_hex_to_rgb(c) for c in droplet_colors] if droplet_colors else [self._pill_rgb]
        )

        self._particle_count = max(0, particle_count)
        self._spread = float(spread)
        self._anim_ms = float(animation_time_ms)
        self._time_var_ms = float(time_variance_ms)
        self._gooeyness = max(0.0, min(1.0, float(gooeyness)))
        self._wobble = max(0.0, min(1.0, float(wobble)))
        self._frame = frame
        self._hover_effect = hover_effect
        self._height_px = height_px

        self.set_hexpand(True)
        self.set_size_request(196, height_px)
        self.set_cursor_from_name("pointer")
        self.set_focusable(True)

        # Layout & spring pill geometry
        self._item_rects: list[tuple[float, float, float, float]] = []
        self._pill_cx: float = 0.0
        self._pill_cy: float = 0.0
        self._pill_hw: float = 20.0
        self._pill_hh: float = 14.0
        self._from_cx: float = 0.0
        self._from_hw: float = 20.0
        self._to_cx: float = 0.0
        self._to_hw: float = 20.0
        self._vel_cx: float = 0.0
        self._vel_hw: float = 0.0
        self._initialized_geom: bool = False

        # Hover state
        self._hover_idx: int = -1
        self._hover_alpha: float = 0.0
        self._hover_cx: float = 0.0
        self._hover_hw: float = 20.0

        # Active droplet transition state
        self._droplets: list[_Droplet] = []
        self._trans_start: float = 0.0
        self._max_dur: float = 0.65
        self._is_animating: bool = False
        self._frozen_now: float | None = None
        self._last_tick_t: float = 0.0
        self._tick_id: int | None = None

        # Preallocated grid cache
        self._grid_w: int = 0
        self._grid_h: int = 0
        self._gx: np.ndarray | None = None
        self._gy: np.ndarray | None = None
        self._rng = random.Random(2042)

        self.set_draw_func(self._on_draw)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.add_controller(click)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

        self.connect("destroy", self._on_destroy)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_index(self) -> int:
        return self._active_index

    def set_active_index(self, index: int, animate: bool = True, notify: bool = False) -> None:
        if not self._items:
            return
        index = max(0, min(index, len(self._items) - 1))
        prev = self._active_index
        if index == prev and self._initialized_geom:
            if notify and self._on_change is not None:
                self._on_change(index, self._items[index])
            return

        self._active_index = index
        w = max(196.0, float(self.get_width()))
        h = max(float(self._height_px), float(self.get_height()))
        self._recompute_item_rects(w, h)

        if index < len(self._item_rects):
            tx, ty, tw, th = self._item_rects[index]
            to_cx = tx + tw * 0.5
            to_cy = ty + th * 0.5
            to_hw = tw * 0.5
            to_hh = th * 0.5
        else:
            return

        if not animate or not self._initialized_geom or prev == index:
            self._frozen_now = None
            self._pill_cx = to_cx
            self._pill_cy = to_cy
            self._pill_hw = to_hw
            self._pill_hh = to_hh
            self._to_cx = to_cx
            self._to_hw = to_hw
            self._from_cx = to_cx
            self._from_hw = to_hw
            self._droplets.clear()
            self._is_animating = False
            self._initialized_geom = True
            self.queue_draw()
        else:
            self._from_cx = self._pill_cx
            self._from_hw = self._pill_hw
            self._to_cx = to_cx
            self._to_hw = to_hw
            self._pill_cy = to_cy
            self._pill_hh = to_hh
            self._spawn_droplets(
                from_cx=self._from_cx,
                from_hw=self._from_hw,
                to_cx=to_cx,
                to_hw=to_hw,
                cy=to_cy,
                hh=to_hh,
            )
            self._trans_start = time.monotonic()
            self._last_tick_t = self._trans_start
            self._frozen_now = None
            self._is_animating = True
            self._ensure_tick()

        if notify and self._on_change is not None:
            self._on_change(index, self._items[index])

    def set_item_badge(self, item_id: str, count: int) -> None:
        changed = False
        for it in self._items:
            if it.id == item_id and it.badge_count != count:
                it.badge_count = count
                changed = True
        if changed:
            self._initialized_geom = False
            self.queue_draw()

    def trigger_preview_transition(self, target_index: int, freeze_at_ms: float | None = None) -> None:
        """Advance state for tests / visual capture without firing on_change."""
        self.set_active_index(target_index, animate=True, notify=False)
        if freeze_at_ms is not None:
            # Advance simulation deterministically by small steps
            steps = max(1, int(round(freeze_at_ms / 16.0)))
            dt = (freeze_at_ms / 1000.0) / steps
            now = self._trans_start
            for _ in range(steps):
                now += dt
                self._step_physics(now, dt)
            self._frozen_now = now
            self._stop_tick()
            self.queue_draw()

    # ------------------------------------------------------------------
    # Geometry & Physics
    # ------------------------------------------------------------------

    def _measure_item_natural_width(self, cr: cairo.Context | None, item: GooeyNavItem) -> float:
        base_w = 0.0
        if item.icon:
            base_w += 13.0 + 5.0
        if cr is not None:
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string("Sans Medium 9.5"))
            layout.set_text(item.label, -1)
            ink, _log = layout.get_pixel_extents()
            base_w += max(22.0, float(ink.width) + 4.0)
        else:
            base_w += len(item.label) * 6.8 + 4.0
        if item.badge_count > 0:
            digits = len(str(item.badge_count))
            base_w += 14.0 + digits * 5.0
        return base_w + 14.0

    def _recompute_item_rects(self, w: float, h: float, cr: cairo.Context | None = None) -> None:
        n = len(self._items)
        if n == 0:
            self._item_rects = []
            return
        frame_pad = 4.0 if self._frame else 2.0
        inner_w = max(80.0, w - frame_pad * 2.0)
        inner_h = max(24.0, h - frame_pad * 2.0)
        gap = 3.0

        nat_widths = [self._measure_item_natural_width(cr, it) for it in self._items]
        avail = max(60.0, inner_w - gap * max(0, n - 1))
        total_nat = max(1.0, sum(nat_widths))
        extra = avail - total_nat
        widths = [max(44.0, nw + extra / n) for nw in nat_widths]

        # Re-normalize strictly to inner_w so nothing overflows
        scale = avail / max(1.0, sum(widths))
        widths = [ww * scale for ww in widths]

        rects: list[tuple[float, float, float, float]] = []
        cx = frame_pad
        for ww in widths:
            rects.append((cx, frame_pad, ww, inner_h))
            cx += ww + gap
        self._item_rects = rects

        if not self._initialized_geom and 0 <= self._active_index < len(rects):
            rx, ry, rw, rh = rects[self._active_index]
            self._pill_cx = rx + rw * 0.5
            self._pill_cy = ry + rh * 0.5
            self._pill_hw = rw * 0.5
            self._pill_hh = rh * 0.5
            self._from_cx = self._pill_cx
            self._from_hw = self._pill_hw
            self._to_cx = self._pill_cx
            self._to_hw = self._pill_hw
            self._initialized_geom = True
        elif self._initialized_geom and 0 <= self._active_index < len(rects) and not self._is_animating:
            rx, ry, rw, rh = rects[self._active_index]
            self._pill_cx = rx + rw * 0.5
            self._pill_cy = ry + rh * 0.5
            self._pill_hw = rw * 0.5
            self._pill_hh = rh * 0.5
            self._to_cx = self._pill_cx
            self._to_hw = self._pill_hw

    def _spawn_droplets(
        self,
        from_cx: float,
        from_hw: float,
        to_cx: float,
        to_hw: float,
        cy: float,
        hh: float,
    ) -> None:
        self._droplets.clear()
        if self._particle_count <= 0:
            self._max_dur = 0.42
            return

        rng = self._rng
        dist = abs(to_cx - from_cx)
        sign = 1.0 if to_cx >= from_cx else -1.0
        base_dur = max(0.28, self._anim_ms / 1000.0)
        var_dur = self._time_var_ms / 1000.0
        max_end = 0.40

        for i in range(self._particle_count):
            t_start = rng.uniform(0.0, 0.14)
            dur = max(0.26, base_dur + rng.uniform(-0.35 * var_dur, 0.5 * var_dur))
            max_end = max(max_end, t_start + dur)

            p0x = from_cx + rng.uniform(-from_hw * 0.65, from_hw * 0.65)
            p0y = cy + rng.uniform(-hh * 0.55, hh * 0.55)

            p2x = to_cx + rng.uniform(-to_hw * 0.62, to_hw * 0.62)
            p2y = cy + rng.uniform(-hh * 0.52, hh * 0.52)

            mid_x = 0.5 * (p0x + p2x) + sign * rng.uniform(-0.12, 0.18) * max(24.0, dist)
            arc_dir = -1.0 if (i % 2 == 0) else 1.0
            arc_amp = self._spread * rng.uniform(0.16, 0.52)
            p1y = cy + arc_dir * min(hh * 0.95, arc_amp * 0.38)
            p1x = mid_x

            radius = rng.uniform(3.8, 8.2) * (0.75 + 0.48 * self._gooeyness)
            rgb = self._droplet_rgbs[i % len(self._droplet_rgbs)]

            self._droplets.append(
                _Droplet(
                    p0x=p0x,
                    p0y=p0y,
                    p1x=p1x,
                    p1y=p1y,
                    p2x=p2x,
                    p2y=p2y,
                    radius=radius,
                    delay=t_start,
                    dur=dur,
                    rgb=rgb,
                )
            )
        self._max_dur = max_end + 0.18

    def _ensure_tick(self) -> None:
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(16, self._on_tick)

    def _stop_tick(self) -> None:
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _step_physics(self, now: float, dt: float) -> bool:
        dt = min(0.05, max(0.001, dt))

        # Underdamped spring physics matching <GooeyNav /> wobble
        # Lower wobble -> crisper damping; higher wobble -> lively overshoot
        omega = 13.5 + 4.5 * self._wobble
        zeta = 0.72 - 0.28 * self._wobble
        stiffness = omega * omega
        damping = 2.0 * zeta * omega

        ax = stiffness * (self._to_cx - self._pill_cx) - damping * self._vel_cx
        self._vel_cx += ax * dt
        self._pill_cx += self._vel_cx * dt

        aw = stiffness * (self._to_hw - self._pill_hw) - damping * self._vel_hw
        self._vel_hw += aw * dt
        self._pill_hw += self._vel_hw * dt

        target_hover_alpha = 1.0 if (self._hover_effect and self._hover_idx >= 0 and self._hover_idx != self._active_index) else 0.0
        self._hover_alpha += (target_hover_alpha - self._hover_alpha) * min(1.0, dt * 14.0)

        elapsed = now - self._trans_start
        still_droplets = self._is_animating and elapsed < self._max_dur
        still_spring = (
            abs(self._to_cx - self._pill_cx) > 0.15
            or abs(self._vel_cx) > 0.8
            or abs(self._to_hw - self._pill_hw) > 0.15
            or abs(self._vel_hw) > 0.8
        )
        still_hover = abs(target_hover_alpha - self._hover_alpha) > 0.01

        if not still_droplets and not still_spring:
            self._pill_cx = self._to_cx
            self._pill_hw = self._to_hw
            self._vel_cx = 0.0
            self._vel_hw = 0.0
            self._droplets.clear()
            self._is_animating = False

        return still_droplets or still_spring or still_hover

    def _on_tick(self) -> bool:
        now = time.monotonic()
        dt = now - self._last_tick_t if self._last_tick_t > 0 else 0.016
        self._last_tick_t = now
        keep_going = self._step_physics(now, dt)
        self.queue_draw()
        if not keep_going:
            self._tick_id = None
            return False
        return True

    # ------------------------------------------------------------------
    # Input Events
    # ------------------------------------------------------------------

    def _hit_test(self, x: float, y: float) -> int:
        for i, (rx, ry, rw, rh) in enumerate(self._item_rects):
            if rx - 1.5 <= x <= rx + rw + 1.5 and 0 <= y <= max(self._height_px, self.get_height()):
                return i
        return -1

    def _on_motion(self, _ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        idx = self._hit_test(x, y)
        if idx != self._hover_idx:
            self._hover_idx = idx
            if 0 <= idx < len(self._item_rects):
                rx, _ry, rw, _rh = self._item_rects[idx]
                self._hover_cx = rx + rw * 0.5
                self._hover_hw = rw * 0.5
                tip = self._items[idx].tooltip
                self.set_tooltip_text(tip if tip else None)
            else:
                self.set_tooltip_text(None)
            self._last_tick_t = time.monotonic()
            self._ensure_tick()

    def _on_leave(self, _ctrl: Gtk.EventControllerMotion) -> None:
        if self._hover_idx != -1:
            self._hover_idx = -1
            self.set_tooltip_text(None)
            self._last_tick_t = time.monotonic()
            self._ensure_tick()

    def _on_pressed(self, _gesture: Gtk.GestureClick, _n: int, _x: float, _y: float) -> None:
        self.grab_focus()

    def _on_released(self, _gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        idx = self._hit_test(x, y)
        if idx >= 0:
            self.set_active_index(idx, animate=True, notify=True)

    def _on_key_pressed(
        self, _ctrl: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType
    ) -> bool:
        if not self._items:
            return False
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            self.set_active_index((self._active_index - 1) % len(self._items), animate=True, notify=True)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            self.set_active_index((self._active_index + 1) % len(self._items), animate=True, notify=True)
            return True
        return False

    def _on_destroy(self, _w: Gtk.Widget) -> None:
        self._stop_tick()

    # ------------------------------------------------------------------
    # Liquid Metaball SDF & Rendering
    # ------------------------------------------------------------------

    def _ensure_grid(self, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
        if w != self._grid_w or h != self._grid_h or self._gx is None or self._gy is None:
            xs = np.arange(w, dtype=np.float32) + 0.5
            ys = np.arange(h, dtype=np.float32) + 0.5
            gx, gy = np.meshgrid(xs, ys)
            self._grid_w = w
            self._grid_h = h
            self._gx = gx
            self._gy = gy
        return self._gx, self._gy

    @staticmethod
    def _sd_pill(px: np.ndarray, py: np.ndarray, hw: float, hh: float) -> np.ndarray:
        r = max(1.0, hh)
        bx = max(0.0, hw - r)
        dx = np.maximum(np.abs(px) - bx, 0.0)
        return np.hypot(dx, py) - r

    @staticmethod
    def _sd_capsule(
        gx: np.ndarray,
        gy: np.ndarray,
        ax: float,
        ay: float,
        bx: float,
        by: float,
        r: float,
    ) -> np.ndarray:
        pax = gx - ax
        pay = gy - ay
        bax = bx - ax
        bay = by - ay
        denom = bax * bax + bay * bay
        if denom < 1e-4:
            return np.hypot(pax, pay) - r
        h = np.clip((pax * bax + pay * bay) / denom, 0.0, 1.0)
        return np.hypot(pax - bax * h, pay - bay * h) - r

    @staticmethod
    def _smooth_union(
        a: np.ndarray,
        b: np.ndarray,
        k: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        if k <= 0.001:
            choose_b = b < a
            return np.minimum(a, b), choose_b.astype(np.float32)
        h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
        d = b * (1.0 - h) + a * h - k * h * (1.0 - h)
        # h -> 1.0 when b > a (pixel belongs to a); (1 - h) -> blend weight toward b
        return d, (1.0 - h)

    def _compute_liquid_surfaces(
        self, iw: int, ih: int, now: float
    ) -> tuple[cairo.ImageSurface, cairo.ImageSurface]:
        """Evaluate React Bits <GooeyNav /> SDF field -> (BGRA liquid surface, A8 alpha mask)."""
        gx, gy = self._ensure_grid(iw, ih)

        hw = max(6.0, self._pill_hw)
        hh = max(6.0, self._pill_hh)
        d = self._sd_pill(gx - self._pill_cx, gy - self._pill_cy, hw, hh)

        pr0, pg0, pb0 = self._pill_rgb
        col_r = np.full_like(d, pr0)
        col_g = np.full_like(d, pg0)
        col_b = np.full_like(d, pb0)

        merge_k = 0.0 if self._gooeyness <= 0.01 else (2.5 + 15.0 * self._gooeyness)
        elapsed = now - self._trans_start

        if self._is_animating and self._droplets:
            for drop in self._droplets:
                t = (elapsed - drop.delay) / drop.dur
                if t <= 0.0 or t >= 1.0:
                    continue
                # Smooth ease with brisk emergence and soft landing
                e = t * t * (3.0 - 2.0 * t)
                x, y = _eval_bezier_pos(drop, e)
                e_prev = max(0.0, e - 0.065)
                px, py = _eval_bezier_pos(drop, e_prev)

                # Envelope swells mid-flight and shrinks cleanly into destination
                env = math.sin(math.pi * e) ** 0.55
                rad = drop.radius * (0.42 + 0.72 * env)
                if rad < 0.75:
                    continue

                dp = self._sd_capsule(gx, gy, x, y, px, py, rad)
                d, w_b = self._smooth_union(d, dp, merge_k)
                dr, dg, db = drop.rgb
                col_r = col_r * (1.0 - w_b) + dr * w_b
                col_g = col_g * (1.0 - w_b) + dg * w_b
                col_b = col_b * (1.0 - w_b) + db * w_b

        # Smooth antialiased boundary & top glass sheen + bottom soft shadow
        alpha = np.clip(0.5 - d / 1.15, 0.0, 1.0)

        # Subtle upper rim highlight inside the liquid pill
        edge_dist = np.abs(d)
        rim = np.clip(1.0 - edge_dist / 1.8, 0.0, 1.0) * alpha
        top_bias = np.clip((self._pill_cy - gy) / max(1.0, hh), 0.0, 1.0)
        rim_boost = rim * (0.10 + 0.16 * top_bias)
        col_r = np.clip(col_r + (1.0 - col_r) * rim_boost, 0.0, 1.0)
        col_g = np.clip(col_g + (1.0 - col_g) * rim_boost, 0.0, 1.0)
        col_b = np.clip(col_b + (1.0 - col_b) * rim_boost, 0.0, 1.0)

        # Soft downward shadow beneath the pill
        shadow = np.clip(0.32 - (d + 1.2) * 0.08, 0.0, 0.30) * (1.0 - alpha)

        # Combine into premultiplied ARGB32 buffer (little-endian BGRA bytes)
        buf = np.empty((ih, iw, 4), dtype=np.uint8)
        out_a = np.clip(alpha + shadow * (1.0 - alpha), 0.0, 1.0)
        # Shadow is pure black (0,0,0), liquid contributes alpha * color
        buf[..., 0] = np.clip(col_b * alpha * 255.0 + 0.5, 0, 255).astype(np.uint8)
        buf[..., 1] = np.clip(col_g * alpha * 255.0 + 0.5, 0, 255).astype(np.uint8)
        buf[..., 2] = np.clip(col_r * alpha * 255.0 + 0.5, 0, 255).astype(np.uint8)
        buf[..., 3] = np.clip(out_a * 255.0 + 0.5, 0, 255).astype(np.uint8)

        liq_surface = cairo.ImageSurface.create_for_data(
            bytearray(buf.tobytes()),
            cairo.Format.ARGB32,
            iw,
            ih,
            iw * 4,
        )

        # Separate crisp A8 mask purely of the opaque liquid body (excluding drop shadow)
        # so text inside the pill switches smoothly to high-contrast ink.
        mask_arr = np.clip(alpha * 255.0 + 0.5, 0, 255).astype(np.uint8)
        stride_a8 = cairo.Format.A8.stride_for_width(iw)
        if stride_a8 == iw:
            mask_bytes = bytearray(mask_arr.tobytes())
        else:
            padded = np.zeros((ih, stride_a8), dtype=np.uint8)
            padded[:, :iw] = mask_arr
            mask_bytes = bytearray(padded.tobytes())

        mask_surface = cairo.ImageSurface.create_for_data(
            mask_bytes,
            cairo.Format.A8,
            iw,
            ih,
            stride_a8,
        )
        return liq_surface, mask_surface

    def _draw_capsule_path(
        self, cr: cairo.Context, x: float, y: float, w: float, h: float, r: float
    ) -> None:
        r = min(r, w * 0.5, h * 0.5)
        cr.new_sub_path()
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.arc(x + w - r, y + r, r, -0.5 * math.pi, 0.0)
        cr.arc(x + w - r, y + h - r, r, 0.0, 0.5 * math.pi)
        cr.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()

    def _draw_items_layer(
        self,
        cr: cairo.Context,
        ink_rgb: tuple[float, float, float],
        is_contrast_layer: bool,
    ) -> None:
        for i, item in enumerate(self._items):
            if i >= len(self._item_rects):
                break
            rx, ry, rw, rh = self._item_rects[i]

            if is_contrast_layer:
                alpha = 0.98
            elif i == self._hover_idx:
                alpha = 0.95
            else:
                alpha = 0.68

            # Measure total horizontal content width (icon + label + optional badge)
            icon_sz = 12.5
            icon_gap = 4.5
            badge_gap = 4.5
            has_icon = bool(item.icon and item.icon in ICON_PATHS)
            has_badge = item.badge_count > 0

            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string("Sans Medium 9.5"))
            layout.set_text(item.label, -1)
            ink, log = layout.get_pixel_extents()
            lbl_w = float(log.width)
            lbl_h = float(log.height)

            badge_str = str(item.badge_count) if has_badge else ""
            badge_w = 0.0
            if has_badge:
                badge_w = max(15.0, 8.0 + len(badge_str) * 5.6)

            content_w = (
                (icon_sz + icon_gap if has_icon else 0.0)
                + lbl_w
                + (badge_gap + badge_w if has_badge else 0.0)
            )
            cur_x = rx + (rw - content_w) * 0.5
            mid_y = ry + rh * 0.5

            if has_icon and item.icon is not None:
                _draw_svg_path_icon(
                    cr,
                    item.icon,
                    cur_x,
                    mid_y - icon_sz * 0.5,
                    icon_sz,
                    ink_rgb,
                    alpha,
                )
                cur_x += icon_sz + icon_gap

            cr.save()
            cr.set_source_rgba(ink_rgb[0], ink_rgb[1], ink_rgb[2], alpha)
            cr.move_to(cur_x, mid_y - lbl_h * 0.5 - 0.5)
            PangoCairo.show_layout(cr, layout)
            cr.restore()
            cur_x += lbl_w

            if has_badge:
                cur_x += badge_gap
                bw = badge_w
                bh = 15.0
                by = mid_y - bh * 0.5
                self._draw_capsule_path(cr, cur_x, by, bw, bh, bh * 0.5)
                if is_contrast_layer:
                    cr.set_source_rgba(ink_rgb[0], ink_rgb[1], ink_rgb[2], 0.14)
                else:
                    cr.set_source_rgba(0.54, 0.71, 0.98, 0.20)
                cr.fill()

                b_layout = PangoCairo.create_layout(cr)
                b_layout.set_font_description(Pango.FontDescription.from_string("Monospace Bold 8"))
                b_layout.set_text(badge_str, -1)
                _bi, b_log = b_layout.get_pixel_extents()
                if is_contrast_layer:
                    cr.set_source_rgba(ink_rgb[0], ink_rgb[1], ink_rgb[2], 0.92)
                else:
                    cr.set_source_rgba(0.68, 0.80, 1.0, 0.96)
                cr.move_to(cur_x + (bw - b_log.width) * 0.5, mid_y - b_log.height * 0.5)
                PangoCairo.show_layout(cr, b_layout)

    def _on_draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int) -> None:
        if width <= 4 or height <= 4 or not self._items:
            return
        w = float(width)
        h = float(height)
        self._recompute_item_rects(w, h, cr)

        # 1. Outer frosted frame track (matches <GooeyNav frame={true} theme="dark" />)
        if self._frame:
            r = h * 0.5
            # Soft ambient shadow
            self._draw_capsule_path(cr, 0.5, 1.5, w - 1.0, h - 2.0, r)
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.24)
            cr.fill()

            # Translucent frosted track body
            self._draw_capsule_path(cr, 0.5, 0.5, w - 1.0, h - 1.0, r)
            grad = cairo.LinearGradient(0, 0.5, 0, h - 0.5)
            grad.add_color_stop_rgba(0.0, 1.0, 1.0, 1.0, 0.065)
            grad.add_color_stop_rgba(1.0, 1.0, 1.0, 1.0, 0.032)
            cr.set_source(grad)
            cr.fill_preserve()

            cr.set_source_rgba(1.0, 1.0, 1.0, 0.14)
            cr.set_line_width(1.0)
            cr.stroke()

            # Subtle upper inner highlight
            self._draw_capsule_path(cr, 1.5, 1.25, w - 3.0, h - 3.0, max(1.0, r - 1.0))
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.05)
            cr.set_line_width(0.8)
            cr.stroke()

        # 2. Hover highlight pill under inactive item
        if self._hover_effect and self._hover_alpha > 0.01:
            hh = self._pill_hh
            hx = self._hover_cx - self._hover_hw
            hy = self._pill_cy - hh
            self._draw_capsule_path(cr, hx, hy, self._hover_hw * 2.0, hh * 2.0, hh)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.085 * self._hover_alpha)
            cr.fill()

        # 3. Liquid SDF metaball pill + droplets
        now = self._frozen_now if self._frozen_now is not None else time.monotonic()
        liq_surf, mask_surf = self._compute_liquid_surfaces(width, height, now)
        cr.save()
        cr.set_source_surface(liq_surf, 0.0, 0.0)
        cr.paint()
        cr.restore()

        # 4. Base ink items (outside active pill)
        self._draw_items_layer(cr, self._base_ink_rgb, is_contrast_layer=False)

        # 5. Contrast-inverted items clipped by exact liquid SDF mask
        cr.push_group_with_content(cairo.Content.COLOR_ALPHA)
        self._draw_items_layer(cr, self._active_ink_rgb, is_contrast_layer=True)
        grp = cr.pop_group()
        cr.save()
        cr.set_source(grp)
        cr.mask_surface(mask_surf, 0.0, 0.0)
        cr.restore()
