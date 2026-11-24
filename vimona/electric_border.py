"""Native GTK4/Cairo port of React Bits <ElectricBorder /> animated current."""

from __future__ import annotations

import math
import random
import time

import cairo
import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


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
    return (0.49, 0.98, 1.0)


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = _clamp((x - edge0) / max(1e-6, edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _wobble(x: float, seed: int) -> float:
    return (
        0.55 * math.sin(x * 1.13 + seed * 1.7)
        + 0.30 * math.sin(x * 2.47 + seed * 3.1)
        + 0.15 * math.cos(x * 4.19 + seed * 5.3)
    )


def _sample_rounded_perimeter(
    bx: float, by: float, bw: float, bh: float, r: float, n_pts: int = 160
) -> tuple[list[tuple[float, float, float, float, float]], float]:
    """Return [(px, py, nx, ny, s), ...] around rounded rect plus total perimeter."""
    r = _clamp(r, 0.0, min(bw, bh) * 0.5)
    top_straight = max(0.0, bw - 2.0 * r)
    side_straight = max(0.0, bh - 2.0 * r)
    arc_len = (math.pi * 0.5) * r
    perim = 2.0 * top_straight + 2.0 * side_straight + 4.0 * arc_len
    if perim <= 1.0:
        return ([], 1.0)

    cx0, cy0 = bx + bw * 0.5, by + bh * 0.5
    hx, hy = bw * 0.5, bh * 0.5

    def eval_at(s: float) -> tuple[float, float, float, float]:
        u = s % perim
        # Top edge (left -> right)
        if u < top_straight:
            return (cx0 - hx + r + u, by, 0.0, -1.0)
        u -= top_straight
        # Top-right corner
        if u < arc_len:
            a = -math.pi * 0.5 + (u / max(1e-6, r))
            return (cx0 + hx - r + r * math.cos(a), cy0 - hy + r + r * math.sin(a), math.cos(a), math.sin(a))
        u -= arc_len
        # Right edge (top -> bottom)
        if u < side_straight:
            return (cx0 + hx, cy0 - hy + r + u, 1.0, 0.0)
        u -= side_straight
        # Bottom-right corner
        if u < arc_len:
            a = u / max(1e-6, r)
            return (cx0 + hx - r + r * math.cos(a), cy0 + hy - r + r * math.sin(a), math.cos(a), math.sin(a))
        u -= arc_len
        # Bottom edge (right -> left)
        if u < top_straight:
            return (cx0 + hx - r - u, cy0 + hy, 0.0, 1.0)
        u -= top_straight
        # Bottom-left corner
        if u < arc_len:
            a = math.pi * 0.5 + (u / max(1e-6, r))
            return (cx0 - hx + r + r * math.cos(a), cy0 + hy - r + r * math.sin(a), math.cos(a), math.sin(a))
        u -= arc_len
        # Left edge (bottom -> top)
        if u < side_straight:
            return (bx, cy0 + hy - r - u, -1.0, 0.0)
        u -= side_straight
        # Top-left corner
        a = math.pi + (u / max(1e-6, r))
        return (cx0 - hx + r + r * math.cos(a), cy0 - hy + r + r * math.sin(a), math.cos(a), math.sin(a))

    samples: list[tuple[float, float, float, float, float]] = []
    for i in range(n_pts):
        s = (i / n_pts) * perim
        px, py, nx, ny = eval_at(s)
        samples.append((px, py, nx, ny, s))
    return (samples, perim)


class ElectricBorderOverlay(Gtk.DrawingArea):
    """Non-interactive multi-strand crackling electric border around any widget."""

    def __init__(
        self,
        color: str = "#7df9ff",
        glow_color: str | None = None,
        speed: float = 1.0,
        chaos: float = 0.5,
        spread: float = 0.6,
        thickness: float = 1.5,
        strands: int = 3,
        glow: float = 0.6,
        intensity: float = 0.8,
        arcs: float = 1.0,
        flicker: float = 0.5,
        border_radius: float = 22.0,
        padding: float = 12.0,
        active: bool = True,
        intro: bool = True,
        interactive: bool = True,
        cursor_radius: float = 90.0,
    ) -> None:
        super().__init__()
        self._base_rgb = _hex_to_rgb(color)
        self._core_rgb = tuple(c + (1.0 - c) * 0.58 for c in self._base_rgb)
        self._glow_rgb = _hex_to_rgb(glow_color) if glow_color else self._base_rgb

        self._speed = max(0.0, speed)
        self._chaos = _clamp(chaos, 0.0, 2.0)
        self._spread = _clamp(spread, 0.0, 2.0)
        self._thickness = max(0.25, thickness)
        self._strands = int(_clamp(round(strands), 1, 6))
        self._glow = _clamp(glow, 0.0, 3.0)
        self._intensity = max(0.0, intensity)
        self._arcs = max(0.0, arcs)
        self._flicker = _clamp(flicker, 0.0, 1.0)
        self._border_radius = max(0.0, border_radius)
        self._padding = max(0.0, padding)
        self._active = active
        self._interactive = interactive
        self._cursor_radius = max(1.0, cursor_radius)

        self._time = random.random() * 100.0
        self._power = 0.0 if (active and intro) else (1.0 if active else 0.0)
        self._reveal = 0.0 if (active and intro) else (1.0 if active else 0.0)
        self._hover = [0.0, 0.0, 0.0]  # x, y, power
        self._pointer_near = False
        self._pointer_xy = (0.0, 0.0)
        self._sparks: list[dict[str, float]] = []
        self._pulses: list[tuple[float, float]] = []  # (s_origin, born_time)

        self._geom_key: tuple[int, int] | None = None
        self._samples: list[tuple[float, float, float, float, float]] = []
        self._perimeter: float = 1.0
        self._last_tick = time.monotonic()
        self._tick_id: int | None = None

        self.set_can_target(False)
        self.set_draw_func(self._on_draw)
        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)

    def on_pointer_move(self, x: float, y: float) -> None:
        if not self._interactive:
            return
        w, h = float(self.get_width()), float(self.get_height())
        self._pointer_xy = (x, y)
        self._pointer_near = -18.0 <= x <= w + 18.0 and -18.0 <= y <= h + 18.0

    def on_pointer_leave(self) -> None:
        self._pointer_near = False

    def trigger_pulse(self, x: float, y: float) -> None:
        if not self._interactive or not self._active or not self._samples:
            return
        best_s = 0.0
        best_d2 = 1e18
        for px, py, _nx, _ny, s in self._samples[::3]:
            d2 = (px - x) ** 2 + (py - y) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_s = s
        self._pulses.append((best_s, self._time))
        if len(self._pulses) > 3:
            self._pulses.pop(0)
        if self._arcs > 0 and len(self._sparks) < 4:
            self._sparks.append(self._make_spark(best_s))

    def _make_spark(self, near_s: float | None = None) -> dict[str, float]:
        span = min(self._perimeter * 0.22, 48.0 + random.random() * 75.0)
        start = near_s if near_s is not None else random.random() * self._perimeter
        s0 = (start - span * 0.5) % self._perimeter
        s1 = (start + span * 0.5) % self._perimeter
        bow = (1.0 if random.random() > 0.5 else -1.0) * (5.0 + random.random() * 9.0)
        return {
            "s0": s0,
            "s1": s1,
            "bow": bow,
            "seed": 1.0 + random.random() * 50.0,
            "born": self._time,
            "life": 0.30 + random.random() * 0.35,
        }

    def _on_realize(self, _w: Gtk.Widget) -> None:
        if self._tick_id is None:
            self._last_tick = time.monotonic()
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_unrealize(self, _w: Gtk.Widget) -> None:
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def _ensure_perimeter(self, w: int, h: int) -> None:
        if self._geom_key == (w, h):
            return
        self._geom_key = (w, h)
        pad = self._padding
        bw = max(4.0, float(w) - pad * 2.0)
        bh = max(4.0, float(h) - pad * 2.0)
        self._samples, self._perimeter = _sample_rounded_perimeter(
            pad, pad, bw, bh, self._border_radius, n_pts=168
        )

    def step(self, dt: float = 1.0 / 60.0) -> None:
        self._time += dt * self._speed
        target_power = 1.0 if self._active else 0.0
        self._power += (target_power - self._power) * (1.0 - math.exp(-dt / 0.28))
        if self._active:
            self._reveal = min(1.0, self._reveal + dt / 0.85)

        if self._interactive and self._pointer_near and self._active:
            if self._hover[2] < 0.01:
                self._hover[0], self._hover[1] = self._pointer_xy
            self._hover[0] += (self._pointer_xy[0] - self._hover[0]) * (1.0 - math.exp(-dt * 12.0))
            self._hover[1] += (self._pointer_xy[1] - self._hover[1]) * (1.0 - math.exp(-dt * 12.0))
            self._hover[2] += (1.0 - self._hover[2]) * (1.0 - math.exp(-dt / 0.25))
        else:
            self._hover[2] += (0.0 - self._hover[2]) * (1.0 - math.exp(-dt / 0.45))

        self._sparks = [sp for sp in self._sparks if (self._time - sp["born"]) <= sp["life"]]
        self._pulses = [(ps, bt) for ps, bt in self._pulses if (self._time - bt) <= 2.2]

        if self._active and self._reveal >= 0.95 and self._arcs > 0 and len(self._sparks) < 4:
            chance = dt * self._speed * self._arcs
            if random.random() < chance * (0.9 + 3.0 * self._hover[2]):
                self._sparks.append(self._make_spark())

    def _on_tick(self, _w: Gtk.Widget, _clock: Gdk.FrameClock) -> bool:
        now = time.monotonic()
        dt = min(0.05, max(0.001, now - self._last_tick))
        self._last_tick = now
        self.step(dt)
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _sample_point(self, target_s: float) -> tuple[float, float, float, float]:
        if not self._samples:
            return (0.0, 0.0, 0.0, -1.0)
        idx = int(((target_s % self._perimeter) / self._perimeter) * len(self._samples)) % len(self._samples)
        px, py, nx, ny, _ = self._samples[idx]
        return (px, py, nx, ny)

    def _on_draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        if w <= 8 or h <= 8:
            return
        self._ensure_perimeter(w, h)
        if not self._samples or self._power <= 0.002:
            return

        t = self._time
        p_smooth = self._power * self._power * (3.0 - 2.0 * self._power)
        front = self._reveal * (self._perimeter * 0.5 + 80.0)
        breath = 1.0 + self._flicker * 0.35 * _wobble(t * 4.6 + 7.0, 3)
        gain = self._intensity * breath
        cr_r2 = self._cursor_radius * self._cursor_radius

        cr.save()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        # 1. Soft perimeter neon bloom along base border
        cr.new_path()
        cr.move_to(self._samples[0][0], self._samples[0][1])
        for px, py, _, _, _ in self._samples[1:]:
            cr.line_to(px, py)
        cr.close_path()
        for lw, al in ((11.0, 0.07), (5.0, 0.17)):
            cr.set_source_rgba(*self._glow_rgb, al * self._glow * p_smooth)
            cr.set_line_width(lw)
            cr.stroke_preserve()
        cr.new_path()

        # 2. Multi-strand periodic crackling lightning strands
        n = len(self._samples)
        bend_amp = self._spread * 4.2
        ripple_amp = self._spread * 2.2
        crinkle_amp = self._chaos * 2.1

        c1 = max(2, int(round(self._perimeter / 75.0)))
        c2 = max(4, int(round(self._perimeter / 25.0)))
        c3 = max(8, int(round(self._perimeter / 11.0)))

        for strand_idx in range(self._strands):
            lead = strand_idx == 0
            seed = strand_idx * 7 + 3
            spark_mod = 1.0 - self._flicker * 0.28 * (0.5 + 0.5 * _wobble(t * 13.0 + strand_idx * 2.3, seed + 6))
            strand_range = 0.72 if lead else (1.10 + strand_idx * 0.18)

            pts_xy: list[tuple[float, float]] = []
            for idx in range(n):
                px, py, nx, ny, s = self._samples[idx]
                along = min(s, self._perimeter - s)
                if self._reveal < 1.0 and along > front:
                    continue

                theta = (s / self._perimeter) * math.tau
                a = math.sin(c1 * theta + t * 1.4 + strand_idx * 3.1) * 0.6 + math.cos(c1 * theta - t * 0.9 + seed) * 0.4
                b = math.sin(c2 * theta - t * 3.1 + strand_idx * 1.7) * 0.65 + math.sin((c2 + 1) * theta + t * 2.3 + seed) * 0.35
                c = math.sin(c3 * theta + t * 6.4 + seed) * 0.6 + math.cos((c3 + 3) * theta - t * 5.2 + seed) * 0.4

                # Cursor heat + click surge
                heat = 0.0
                if self._hover[2] > 0.01:
                    d2 = (px - self._hover[0]) ** 2 + (py - self._hover[1]) ** 2
                    heat = self._hover[2] * math.exp(-d2 / cr_r2)
                for ps, bt in self._pulses:
                    gap = abs(s - ps)
                    gap = min(gap, self._perimeter - gap)
                    age = t - bt
                    wave = (gap - age * 560.0) / 46.0
                    heat += math.exp(-wave * wave) * math.exp(-age * 1.4)
                heat = min(1.3, heat)

                disp = ((a * bend_amp + b * ripple_amp) * strand_range + c * crinkle_amp) * (1.0 + heat * 0.65)
                pts_xy.append((px + nx * disp, py + ny * disp))

            if len(pts_xy) < 3:
                continue

            cr.new_path()
            cr.move_to(pts_xy[0][0], pts_xy[0][1])
            for x0, y0 in pts_xy[1:]:
                cr.line_to(x0, y0)
            if self._reveal >= 1.0:
                cr.close_path()

            base_w = self._thickness * (1.0 if lead else 0.62)
            alpha_k = p_smooth * spark_mod * (1.0 if lead else 0.62)

            # Glow halo around strand
            cr.set_source_rgba(*self._glow_rgb, _clamp(0.32 * self._glow * alpha_k * gain, 0.0, 1.0))
            cr.set_line_width(base_w * 3.2)
            cr.stroke_preserve()

            # Colored body stroke
            cr.set_source_rgba(*self._base_rgb, _clamp(0.88 * alpha_k * gain, 0.0, 1.0))
            cr.set_line_width(base_w * 1.35)
            cr.stroke_preserve()

            # White-hot filament core
            cr.set_source_rgba(*self._core_rgb, _clamp((0.96 if lead else 0.72) * alpha_k * gain, 0.0, 1.0))
            cr.set_line_width(max(0.7, base_w * 0.65))
            cr.stroke()

        # 3. Spontaneous / cursor-triggered jumping electrical arcs
        for sp in self._sparks:
            k = _clamp((t - sp["born"]) / sp["life"], 0.0, 1.0)
            env = math.sin(math.pi * k) * p_smooth
            if env <= 0.02:
                continue
            ax, ay, _, _ = self._sample_point(sp["s0"])
            bx, by, _, _ = self._sample_point(sp["s1"])
            dx, dy = bx - ax, by - ay
            seg_len = max(1.0, math.hypot(dx, dy))
            nx, ny = -dy / seg_len, dx / seg_len
            cr.new_path()
            steps = 12
            for j in range(steps + 1):
                u = j / steps
                taper = math.sin(math.pi * u)
                jag = _wobble(u * 5.2 + sp["seed"] + t * 8.0, int(sp["seed"])) * 3.8 * (0.6 + self._chaos)
                off = (sp["bow"] + jag) * taper
                qx = ax + dx * u + nx * off
                qy = ay + dy * u + ny * off
                if j == 0:
                    cr.move_to(qx, qy)
                else:
                    cr.line_to(qx, qy)
            cr.set_source_rgba(*self._glow_rgb, _clamp(0.40 * env * self._glow, 0.0, 1.0))
            cr.set_line_width(self._thickness * 2.8)
            cr.stroke_preserve()
            cr.set_source_rgba(*self._core_rgb, _clamp(0.95 * env, 0.0, 1.0))
            cr.set_line_width(max(0.8, self._thickness * 0.8))
            cr.stroke()

        cr.restore()
