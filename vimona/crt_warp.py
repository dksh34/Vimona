"""Native GTK4/Cairo/NumPy port of React Bits <CRTWarp /> for Vimona media loading placeholders."""

from __future__ import annotations

import math
import time
from typing import Literal

import cairo
import gi
import numpy as np

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

TAU = math.tau
INTRO_SECONDS = 1.6
FLIP_SECONDS = 0.8
CELL_ASPECT = 0.56

MaskType = Literal["none", "aperture", "shadow", "slot"]


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def _smooth(t: float) -> float:
    t = _clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _ease_in_out(t: float) -> float:
    t = _clamp(t, 0.0, 1.0)
    return 4.0 * t * t * t if t < 0.5 else 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 6:
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)
    return (0.78, 0.33, 0.97)


def _seed_for(channel: int) -> tuple[float, float, float, float]:
    if channel == 0:
        return (0.0, 0.0, 0.0, 0.0)
    coeffs = (
        (12.9898, 1.7, 43758.5453),
        (78.233, 4.1, 24634.6345),
        (37.719, 2.9, 17431.7731),
        (91.113, 0.3, 31337.1337),
    )
    out = []
    for k1, k2, k3 in coeffs:
        v = math.sin(channel * k1 + k2) * k3
        out.append((v - math.floor(v)) * TAU)
    return (out[0], out[1], out[2], out[3])


def _intro_at(t: float) -> tuple[float, float, float]:
    if t >= INTRO_SECONDS:
        return (1.0, 1.0, 1.0)
    reveal = _smooth(t / INTRO_SECONDS)
    warm = _smooth(t / 0.6)
    focus = _smooth((t - 0.15) / (INTRO_SECONDS - 0.15))
    return (reveal, warm, focus)


def _smoothstep_arr(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / max(1e-5, e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _sep_blur(arr: np.ndarray, radius_px: int = 3) -> np.ndarray:
    """Fast separable Gaussian-like blur via box/triangle passes matching glow halo."""
    if radius_px <= 0:
        return arr
    out = arr.astype(np.float32, copy=True)
    for _ in range(2):
        # Horizontal 5-tap kernel [1, 2, 3, 2, 1]/9
        p = np.pad(out, ((0, 0), (2, 2)), mode="edge")
        out = (p[:, 0:-4] + 2.0 * p[:, 1:-3] + 3.0 * p[:, 2:-2] + 2.0 * p[:, 3:-1] + p[:, 4:]) / 9.0
        # Vertical 5-tap kernel
        p = np.pad(out, ((2, 2), (0, 0)), mode="edge")
        out = (p[0:-4, :] + 2.0 * p[1:-3, :] + 3.0 * p[2:-2, :] + 2.0 * p[3:-1, :] + p[4:, :]) / 9.0
    return out


class CRTWarpWidget(Gtk.DrawingArea):
    """Curved phosphor CRT tube plasma placeholder ported from React Bits <CRTWarp />."""

    def __init__(
        self,
        color: str = "#c755f7",
        background_color: str = "#120f17",
        light_mode: bool = False,
        mask: MaskType = "aperture",
        speed: float = 0.55,
        variation: int = 0,
        wave_amplitude: float = 0.30,
        wave_frequency: float = 2.5,
        levels: int = 3,
        columns: int = 64,
        persistence: float = 0.42,
        curvature: float = 0.25,
        corners: float = 0.32,
        scanline_strength: float = 0.60,
        scanline_frequency: float = 160.0,
        mask_strength: float = 0.32,
        bloom: float = 1.45,
        bloom_radius: float = 1.0,
        rgb_shift: float = 0.004,
        noise: float = 0.10,
        flicker: float = 0.15,
        interference: float = 0.22,
        vignette: float = 0.32,
        brightness: float = 1.0,
        intro: bool = True,
        mouse_react: bool = True,
        mouse_strength: float = 0.5,
        change_on_click: bool = True,
        fps: int = 36,
        paused: bool = False,
    ) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_cursor_from_name("pointer")

        self._ink = np.array(_hex_to_rgb(color), dtype=np.float32)
        self._paper = np.array(_hex_to_rgb(background_color), dtype=np.float32) if background_color != "transparent" else None
        self._light_mode = light_mode
        self._mask_type = {"none": 0, "aperture": 1, "shadow": 2, "slot": 3}.get(mask, 1)
        self._speed = speed
        self._variation = int(variation)
        self._amplitude = max(0.0, wave_amplitude) / 0.28
        self._frequency = max(0.01, wave_frequency) / 2.2
        self._levels = max(2, min(12, int(levels)))
        self._columns = max(12, min(200, int(columns)))
        self._persistence = _clamp(persistence, 0.0, 0.96)
        self._curve = 1.1 + 0.42 / curvature if curvature > 0.001 else 0.0
        self._corners = _clamp(corners, 0.0, 1.0)
        self._scanline = _clamp(scanline_strength, 0.0, 1.0)
        self._lines = _clamp(scanline_frequency, 24.0, 480.0)
        self._mask_strength = _clamp(mask_strength, 0.0, 1.0)
        self._bloom = max(0.0, bloom)
        self._bloom_radius = max(0.05, bloom_radius)
        self._rgb_shift = rgb_shift
        self._noise = max(0.0, noise)
        self._flicker = max(0.0, flicker)
        self._interference = max(0.0, interference)
        self._vignette = _clamp(vignette, 0.0, 1.0)
        self._brightness = max(0.0, brightness)
        self._intro = intro
        self._mouse_react = mouse_react
        self._mouse_strength = max(0.0, mouse_strength)
        self._change_on_click = change_on_click
        self._interval_ms = max(16, int(round(1000.0 / max(1, fps))))
        self._paused = paused

        self._time = 0.0
        self._clock = 0.0
        self._last_t = time.monotonic()
        self._intro_started = not intro
        self._intro_clock = 0.0
        self._flip_at: float | None = None
        self._flipped = True
        self._channel = 0
        self._frozen_mode = False

        # Magnetic pointer physics [x, y, vx, vy, strength]
        self._ptr_x = 0.5
        self._ptr_y = 0.5
        self._mag_x = 0.5
        self._mag_y = 0.5
        self._mag_vx = 0.0
        self._mag_vy = 0.0
        self._mag_strength = 0.0
        self._mag_goal = 0.0

        # Simulation buffers
        self._grid_dims: tuple[int, int] = (0, 0)
        self._prev_cells: np.ndarray | None = None
        self._grid_uv_x: np.ndarray | None = None
        self._grid_uv_y: np.ndarray | None = None

        # Precomputed curved display mesh keyed by internal render size (rw, rh)
        self._mesh_dims: tuple[int, int] = (0, 0)
        self._mesh: dict[str, np.ndarray] = {}

        self._tick_id: int | None = None
        self.set_draw_func(self._on_draw)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", self._on_clicked)
        self.add_controller(click)

        self.connect("map", lambda _w: self._sync_tick())
        self.connect("unmap", lambda _w: self._stop_tick())
        self.connect("destroy", lambda _w: self._stop_tick())

    # ------------------------------------------------------------------
    # Public Controls
    # ------------------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._stop_tick()
        else:
            self._frozen_mode = False
            self._sync_tick()

    def restart_intro(self) -> None:
        self._intro_clock = 0.0
        self._intro_started = True
        self._frozen_mode = False
        self._sync_tick()

    def trigger_channel_flip(self) -> None:
        if self._flip_at is None:
            self._flip_at = self._clock
            self._flipped = False

    def snapshot_at(self, time_s: float = 1.8, clock_s: float = 1.8, variation: int | None = None) -> None:
        """Advance simulation deterministically for tests / visual snapshots."""
        self._frozen_mode = True
        self._stop_tick()
        if variation is not None:
            self._variation = int(variation)
        self._intro_clock = clock_s
        self._clock = clock_s
        self._time = time_s
        # Warm up cell persistence a few frames so phosphor trails populate cleanly
        for step in range(4):
            t_step = max(0.0, time_s - (3 - step) * 0.03 * self._speed)
            self._step_cells(t_step, 0.03, reveal=1.0, rise=1.0)
        self.queue_draw()

    # ------------------------------------------------------------------
    # Event Handlers & Animation Loop
    # ------------------------------------------------------------------

    def _on_motion(self, _ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        w = max(1.0, float(self.get_width()))
        h = max(1.0, float(self.get_height()))
        nx = _clamp(x / w, 0.0, 1.0)
        ny = _clamp(y / h, 0.0, 1.0)
        if self._mag_goal == 0.0 and self._mag_strength < 0.02:
            self._mag_x, self._mag_y = nx, ny
            self._mag_vx, self._mag_vy = 0.0, 0.0
        self._ptr_x, self._ptr_y = nx, ny
        self._mag_goal = 1.0

    def _on_leave(self, _ctrl: Gtk.EventControllerMotion) -> None:
        self._mag_goal = 0.0

    def _on_clicked(self, _g: Gtk.GestureClick, _n: int, _x: float, _y: float) -> None:
        if self._change_on_click:
            self.trigger_channel_flip()

    def _sync_tick(self) -> None:
        if self._frozen_mode or self._paused or not self.get_mapped():
            self._stop_tick()
            return
        if self._tick_id is None:
            self._last_t = time.monotonic()
            self._tick_id = GLib.timeout_add(self._interval_ms, self._on_tick)

    def _stop_tick(self) -> None:
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _on_tick(self) -> bool:
        if self._paused or self._frozen_mode or not self.get_mapped():
            self._tick_id = None
            return False
        now = time.monotonic()
        dt = min(0.08, max(0.001, now - self._last_t))
        self._last_t = now

        self._clock += dt
        self._intro_clock += dt
        if not self._paused:
            self._time += dt * self._speed

        # Damped spring for CRT magnetic pointer curl
        omega = TAU / 0.3
        for _ in range(1):
            ax = -omega * omega * (self._mag_x - self._ptr_x) - 2.0 * omega * self._mag_vx
            ay = -omega * omega * (self._mag_y - self._ptr_y) - 2.0 * omega * self._mag_vy
            self._mag_vx += ax * dt
            self._mag_vy += ay * dt
            self._mag_x += self._mag_vx * dt
            self._mag_y += self._mag_vy * dt

        goal = self._mag_goal if self._mouse_react else 0.0
        self._mag_strength += (goal - self._mag_strength) * (1.0 - math.exp(-dt / 0.18))

        reveal, _w, _f = _intro_at(self._intro_clock) if self._intro else (1.0, 1.0, 1.0)
        rise = dt / 0.22 if reveal < 1.0 else 1.0
        self._step_cells(self._time, dt, reveal=reveal, rise=rise)

        self.queue_draw()
        return True

    # ------------------------------------------------------------------
    # Simulation Passes (CONTENT -> GLOW -> DISPLAY)
    # ------------------------------------------------------------------

    def _ensure_cells(self, rw: int, rh: int) -> tuple[int, int]:
        cols = self._columns
        rows = max(6, min(140, int(round((cols * CELL_ASPECT * rh) / max(rw, 1)))))
        if (cols, rows) != self._grid_dims or self._prev_cells is None:
            self._grid_dims = (cols, rows)
            self._prev_cells = np.zeros((rows, cols), dtype=np.float32)
            xs = (np.arange(cols, dtype=np.float32) + 0.5) / float(cols)
            ys = (np.arange(rows, dtype=np.float32) + 0.5) / float(rows)
            self._grid_uv_x, self._grid_uv_y = np.meshgrid(xs, ys)
        return cols, rows

    def _step_cells(self, t: float, dt: float, reveal: float, rise: float) -> None:
        if self._prev_cells is None or self._grid_uv_x is None or self._grid_uv_y is None:
            return
        sx, sy, sz, sw = _seed_for(self._variation + self._channel)
        uv_x = (self._grid_uv_x - 0.5) * self._frequency + 0.5
        uv_y = (self._grid_uv_y - 0.5) * self._frequency + 0.5

        f = np.zeros_like(uv_x)
        f += 0.7 * np.sin(0.5 * uv_x + t / 5.0 + sx)
        f += 3.0 * np.sin(1.6 * uv_y + t / 5.0 + sy)
        f += np.sin(10.0 * (uv_y * np.sin(t / 2.0 + sz) + uv_x * np.cos(t / 5.0)) + t / 2.0)

        cx = uv_x + 0.5 * np.sin(t / 2.0 + sw)
        cy = uv_y + 0.5 * np.cos(t / 4.0 + sx * 0.7)
        f += 0.4 * np.sin(np.sqrt(100.0 * cx * cx + 100.0 * cy * cy + 1.0) + t)
        f += 0.9 * np.sin(np.sqrt(75.0 * cx * cx + 25.0 * cy * cy + 1.0) + t)
        f -= 1.4 * np.sin(np.sqrt(256.0 * cx * cx + 25.0 * cy * cy + 1.0) + t)
        f += 0.3 * np.sin(0.5 * uv_y + uv_x + math.sin(t))

        s = (0.5 + 0.499 * np.sin(f * self._amplitude)) * ((1.0 / self._levels) * (1.0 - reveal) + reveal)
        level = np.floor(s * self._levels) / max(self._levels - 1.0, 1.0)

        decay = self._persistence ** (dt * 60.0)
        lifted = np.minimum(level, self._prev_cells + rise)
        self._prev_cells = np.maximum(lifted, np.maximum(0.0, self._prev_cells * decay - 0.004)).astype(np.float32)

    def _ensure_display_mesh(self, rw: int, rh: int) -> dict[str, np.ndarray]:
        if (rw, rh) == self._mesh_dims and self._mesh:
            return self._mesh

        xs = (np.arange(rw, dtype=np.float32) + 0.5) / float(rw)
        ys = (np.arange(rh, dtype=np.float32) + 0.5) / float(rh)
        fx, fy = np.meshgrid(xs, ys)
        aspect = float(rw) / max(1.0, float(rh))

        # Exact CRT barrel distortion curve(frag)
        if self._curve > 0.0:
            px = (fx - 0.5) * 2.0
            py = (fy - 0.5) * 2.0
            corner = self._curve / math.sqrt(max(self._curve * self._curve - 2.0, 0.001))
            denom = np.sqrt(np.maximum(self._curve * self._curve - (px * px + py * py), 0.001))
            ux = ((self._curve * px / denom) / corner) * 0.5 + 0.5
            uy = ((self._curve * py / denom) / corner) * 0.5 + 0.5
        else:
            ux, uy = fx.copy(), fy.copy()

        # Rounded CRT tube bezel shape mask
        shape = np.ones_like(fx)
        if self._corners > 0.0:
            margin = self._corners * 0.035
            radius = self._corners * 0.10
            cx_p = np.abs((ux - 0.5) * aspect) - (0.5 - margin) * aspect + radius
            cy_p = np.abs(uy - 0.5) - (0.5 - margin) + radius
            sd = np.hypot(np.maximum(cx_p, 0.0), np.maximum(cy_p, 0.0)) + np.minimum(np.maximum(cx_p, cy_p), 0.0) - radius
            aa = 2.0 / float(max(rw, rh))
            inner = 1.0 - _smoothstep_arr(-aa, aa, sd)
            bevel = 0.65 + 0.35 * _smoothstep_arr(0.0, 0.055, -sd)
            shape = inner * bevel
            ux = (ux - 0.5) / max(0.1, 1.0 - 2.0 * margin) + 0.5
            uy = (uy - 0.5) / max(0.1, 1.0 - 2.0 * margin) + 0.5

        # Vignette
        vx = fx - 0.5
        vy = fy - 0.5
        vig_mask = np.clip(1.0 - (vx * vx + vy * vy) * 2.0, 0.0, 1.0)
        vignette = (1.0 - self._vignette) + self._vignette * (vig_mask * vig_mask * (3.0 - 2.0 * vig_mask))

        # Phosphor Aperture / Shadow / Slot RGB triad mask
        px_x, px_y = np.meshgrid(np.arange(rw, dtype=np.float32), np.arange(rh, dtype=np.float32))
        if self._mask_type == 0 or self._mask_strength <= 0.0:
            mask_rgb = np.ones((rh, rw, 3), dtype=np.float32)
        else:
            low = 1.0 - self._mask_strength
            m = np.full((rh, rw, 3), low, dtype=np.float32)
            col = px_x.copy()
            if self._mask_type == 2:
                col += np.mod(np.floor(px_y / 2.0), 2.0) * 1.5
            stripe = np.mod(np.floor(col), 3.0).astype(np.int32)
            m[stripe == 0, 0] = 1.0
            m[stripe == 1, 1] = 1.0
            m[stripe == 2, 2] = 1.0
            if self._mask_type == 2:
                odd_row = (np.mod(px_y, 2.0) > 0.5)[..., None]
                m = np.where(odd_row, m * (1.0 - 0.28 * self._mask_strength), m)
            elif self._mask_type == 3:
                slot = np.mod(px_y + np.mod(np.floor(px_x / 3.0), 2.0) * 2.0, 4.0)
                slot_dim = (slot < 1.0)[..., None]
                m = np.where(slot_dim, m * (1.0 - 0.55 * self._mask_strength), m)
            norm = math.sqrt((1.0 + 2.0 * low) / 3.0)
            mask_rgb = (m / norm).astype(np.float32)

        # Static phosphor film grain base coordinates
        grain_seed = np.mod(px_x * 123.34 + px_y * 456.21, 1.0)
        grain_hash = np.mod(np.sin(px_x * 12.9898 + px_y * 78.233) * 43758.5453, 1.0) - 0.5

        self._mesh_dims = (rw, rh)
        self._mesh = {
            "fx": fx.astype(np.float32),
            "fy": fy.astype(np.float32),
            "ux": ux.astype(np.float32),
            "uy": uy.astype(np.float32),
            "shape": shape.astype(np.float32),
            "vignette": vignette.astype(np.float32),
            "mask_rgb": mask_rgb.astype(np.float32),
            "grain_hash": (grain_hash * 0.65 + (grain_seed - 0.5) * 0.35).astype(np.float32),
            "aspect": np.array(aspect, dtype=np.float32),
        }
        return self._mesh

    @staticmethod
    def _sample_cells_beam(
        cells: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        lines: float,
        scanline: float,
        focus: float,
    ) -> np.ndarray:
        """Sample quantized grid with horizontal CRT smoothing & vertical scanline beam shaping."""
        rows, cols = cells.shape
        in_bounds = (u >= 0.0) & (u <= 1.0) & (v >= 0.0) & (v <= 1.0)

        uc = np.clip(u, 0.0, 1.0) * cols - 0.5
        c0 = np.floor(uc)
        f = uc - c0
        soft = 0.5 * (1.0 - focus) + 0.14 * focus
        t = np.clip((f - (0.5 - soft)) / max(1e-4, 2.0 * soft), 0.0, 1.0)
        w = t * t * (3.0 - 2.0 * t)

        ix0 = np.clip(c0.astype(np.int32), 0, cols - 1)
        ix1 = np.clip(ix0 + 1, 0, cols - 1)

        ly = np.clip(v, 0.0, 1.0) * lines - 0.5
        r0 = np.floor(ly)
        fy = ly - r0

        row_a = np.clip(np.floor(((r0 + 0.5) / lines) * rows).astype(np.int32), 0, rows - 1)
        row_b = np.clip(np.floor(((r0 + 1.5) / lines) * rows).astype(np.int32), 0, rows - 1)

        a0 = cells[row_a, ix0]
        a1 = cells[row_a, ix1]
        va = np.power(np.clip(a0 * (1.0 - w) + a1 * w, 0.0, 1.0), 1.25)

        b0 = cells[row_b, ix0]
        b1 = cells[row_b, ix1]
        vb = np.power(np.clip(b0 * (1.0 - w) + b1 * w, 0.0, 1.0), 1.25)

        nearest = np.where(fy < 0.5, va, vb)
        sa = 0.16 + 0.18 * np.sqrt(va)
        sb = 0.16 + 0.18 * np.sqrt(vb)
        lit = va * np.exp(-0.5 * (fy * fy) / (sa * sa)) + vb * np.exp(-0.5 * ((1.0 - fy) ** 2) / (sb * sb))

        s_mix = scanline * (0.3 + 0.7 * focus)
        beam = nearest * (1.0 - s_mix) + (lit * 1.18) * s_mix
        return np.where(in_bounds, beam, 0.0).astype(np.float32)

    @staticmethod
    def _sample_glow(glow_map: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        gh, gw = glow_map.shape
        gx = np.clip(np.clip(u, 0.0, 1.0) * (gw - 1), 0.0, gw - 1.001)
        gy = np.clip(np.clip(v, 0.0, 1.0) * (gh - 1), 0.0, gh - 1.001)
        x0 = gx.astype(np.int32)
        y0 = gy.astype(np.int32)
        fx = gx - x0
        fy = gy - y0
        x1 = np.minimum(x0 + 1, gw - 1)
        y1 = np.minimum(y0 + 1, gh - 1)
        top = glow_map[y0, x0] * (1.0 - fx) + glow_map[y0, x1] * fx
        bot = glow_map[y1, x0] * (1.0 - fx) + glow_map[y1, x1] * fx
        out = top * (1.0 - fy) + bot * fy
        in_b = (u >= 0.0) & (u <= 1.0) & (v >= 0.0) & (v <= 1.0)
        return np.where(in_b, out, 0.0)

    def _on_draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int) -> None:
        if width <= 4 or height <= 4:
            return

        # Render internal CRT pipeline at responsive buffer (capped cleanly for buttery FPS)
        max_rw = 256
        scale = min(1.0, float(max_rw) / float(width))
        rw = max(48, int(round(width * scale)))
        rh = max(36, int(round(height * scale)))

        self._ensure_cells(rw, rh)
        assert self._prev_cells is not None
        if np.max(self._prev_cells) < 1e-4:
            reveal_init, _, _ = _intro_at(self._intro_clock) if self._intro else (1.0, 1.0, 1.0)
            self._step_cells(self._time, 0.016, reveal=max(0.25, reveal_init), rise=1.0)

        mesh = self._ensure_display_mesh(rw, rh)
        fx = mesh["fx"]
        fy = mesh["fy"]
        ux = mesh["ux"]
        uy = mesh["uy"]
        shape = mesh["shape"]
        vignette = mesh["vignette"]
        mask_rgb = mesh["mask_rgb"]
        aspect = float(mesh["aspect"])

        reveal, warm, focus = _intro_at(self._intro_clock) if self._intro else (1.0, 1.0, 1.0)

        # Channel-flip static + roll state
        roll = 0.0
        tear = 0.0
        static_noise = 0.0
        flip_gain = 1.0
        if self._flip_at is not None:
            t_flip = self._clock - self._flip_at
            if not self._flipped and t_flip >= 0.22:
                self._flipped = True
                self._channel += 1
            if t_flip >= FLIP_SECONDS:
                self._flip_at = None
            else:
                p = t_flip / FLIP_SECONDS
                roll = _ease_in_out(p)
                tear = math.sin(math.pi * _clamp(t_flip / 0.5, 0.0, 1.0)) * 0.6
                static_noise = math.sin(math.pi * _clamp(t_flip / 0.55, 0.0, 1.0)) * 0.75
                flip_gain = 1.0 - 0.3 * math.sin(math.pi * p)

        uv_x = ux.copy()
        uv_y = uy.copy()
        blank = np.ones_like(uv_y)
        if abs(roll) > 1e-4:
            uv_y = np.mod(uv_y + roll, 1.0)
            off = roll - math.floor(roll)
            reach = _smooth(min(off, 1.0 - off) / 0.08)
            edge_b = _smoothstep_arr(0.0, 0.05, uv_y) * _smoothstep_arr(1.0, 0.95, 1.0 - uv_y + 1.0 - 1.0)
            edge_b = _smoothstep_arr(0.0, 0.05, uv_y) * (1.0 - _smoothstep_arr(0.95, 1.0, uv_y))
            blank = (1.0 - reach) + edge_b * reach

        lines = min(self._lines, float(rh) * 0.85)
        if self._interference > 0.0 or tear > 0.0:
            row_id = np.floor(uv_y * lines)
            tick = math.floor(self._clock * 24.0)
            jitter = np.mod(np.sin(row_id * 17.37 + tick * 1.91) * 43758.5, 1.0) - 0.5
            uv_x = uv_x + self._interference * 0.003 * jitter
            if tear > 0.0:
                uv_x = uv_x + tear * (np.sin(uv_y * 21.0 + self._clock * 37.0) * 0.6 + jitter) * 0.05

        # Mouse magnetic beam deflection swirl
        dpx = (fx - self._mag_x) * aspect
        dpy = fy - self._mag_y
        rad_m = 0.24
        fall = np.exp(-(dpx * dpx + dpy * dpy) / (rad_m * rad_m))
        mag_amp = self._mag_strength * self._mouse_strength * 2.0
        swirl_x = (-dpy / aspect) * fall * mag_amp * 0.5
        swirl_y = dpx * fall * mag_amp * 0.5
        spread_r, spread_g, spread_b = (1.0, 0.6, 0.2) if self._mask_type > 0 else (0.85, 0.85, 0.85)

        cells = self._prev_cells
        lv_r = self._sample_cells_beam(cells, uv_x + swirl_x * spread_r + self._rgb_shift, uv_y + swirl_y * spread_r, lines, self._scanline, focus)
        lv_g = self._sample_cells_beam(cells, uv_x + swirl_x * spread_g, uv_y + swirl_y * spread_g, lines, self._scanline, focus)
        lv_b = self._sample_cells_beam(cells, uv_x + swirl_x * spread_b - self._rgb_shift, uv_y + swirl_y * spread_b, lines, self._scanline, focus)

        if self._mask_type > 0 and mag_amp > 0.002:
            turn = self._clock * 0.35
            ct, st = math.cos(turn), math.sin(turn)
            lobe = (dpx * ct + dpy * st) / rad_m
            side = (-dpx * st + dpy * ct) / rad_m
            factor = fall * mag_amp * 0.9
            lv_r *= 1.0 + factor * lobe
            lv_g *= 1.0 + factor * (-0.35 * lobe + 0.6 * side)
            lv_b *= 1.0 + factor * (-lobe)

        # Glow halo from squared intensity blurred smoothly
        glow_low = _sep_blur(cells * cells, radius_px=max(1, int(round(2 * self._bloom_radius))))
        glow = self._sample_glow(glow_low, uv_x, uv_y) * self._bloom

        hum = 1.0 - self._interference * 0.24 * _smoothstep_arr(0.2, 1.0, 0.5 + 0.5 * np.sin((fy - self._clock * 0.09) * TAU))
        flick = 1.0 + self._flicker * 0.09 * math.sin(self._clock * 47.0 + math.sin(self._clock * 13.0))
        gain = self._brightness * hum * flick * vignette * blank * flip_gain * warm

        if static_noise > 0.001:
            n_rng = np.mod(np.sin(fx * 91.7 + fy * 37.3 + (self._clock % 10.0) * 19.3) * 13579.1, 1.0)
            st_val = n_rng * n_rng * 1.15
            lv_r = lv_r * (1.0 - static_noise) + st_val * static_noise
            lv_g = lv_g * (1.0 - static_noise) + st_val * static_noise
            lv_b = lv_b * (1.0 - static_noise) + st_val * static_noise

        grain_mod = 1.0 + mesh["grain_hash"] * self._noise * 1.6
        lv_r = lv_r * gain * grain_mod * mask_rgb[..., 0] * shape
        lv_g = lv_g * gain * grain_mod * mask_rgb[..., 1] * shape
        lv_b = lv_b * gain * grain_mod * mask_rgb[..., 2] * shape
        glow = glow * gain * shape * (1.0 + 0.6 * (1.0 - focus))

        ir, ig, ib = float(self._ink[0]), float(self._ink[1]), float(self._ink[2])
        peak = max(ir, ig, ib, 0.001)
        wr = 1.0 - ir / peak
        wg = 1.0 - ig / peak
        wb = 1.0 - ib / peak

        if not self._light_mode:
            cov_r = np.clip(lv_r, 0.0, 1.0)
            cov_g = np.clip(lv_g, 0.0, 1.0)
            cov_b = np.clip(lv_b, 0.0, 1.0)

            light_r = ir * cov_r * np.power(np.maximum(cov_r, 1e-4), 0.7 * wr) + ir * glow * 0.5 * (1.0 - cov_r)
            light_g = ig * cov_g * np.power(np.maximum(cov_g, 1e-4), 0.7 * wg) + ig * glow * 0.5 * (1.0 - cov_g)
            light_b = ib * cov_b * np.power(np.maximum(cov_b, 1e-4), 0.7 * wb) + ib * glow * 0.5 * (1.0 - cov_b)

            ex_r = np.maximum(lv_r - 1.1, 0.0)
            ex_g = np.maximum(lv_g - 1.1, 0.0)
            ex_b = np.maximum(lv_b - 1.1, 0.0)
            light_r = np.clip(light_r + (1.0 - light_r) * (1.0 - np.exp(-ex_r * 0.6)), 0.0, 1.0)
            light_g = np.clip(light_g + (1.0 - light_g) * (1.0 - np.exp(-ex_g * 0.6)), 0.0, 1.0)
            light_b = np.clip(light_b + (1.0 - light_b) * (1.0 - np.exp(-ex_b * 0.6)), 0.0, 1.0)

            if self._paper is not None:
                pr, pg, pb = float(self._paper[0]), float(self._paper[1]), float(self._paper[2])
                out_r = pr + light_r * (1.0 - pr)
                out_g = pg + light_g * (1.0 - pg)
                out_b = pb + light_b * (1.0 - pb)
                out_a = np.ones_like(out_r)
            else:
                out_r, out_g, out_b = light_r, light_g, light_b
                out_a = np.maximum(np.maximum(light_r, light_g), light_b)
        else:
            cov_r = np.clip(lv_r * 0.86, 0.0, 0.88) + np.clip(glow * 0.12, 0.0, 0.2) * (1.0 - np.clip(lv_r * 0.86, 0.0, 0.88))
            cov_g = np.clip(lv_g * 0.86, 0.0, 0.88) + np.clip(glow * 0.12, 0.0, 0.2) * (1.0 - np.clip(lv_g * 0.86, 0.0, 0.88))
            cov_b = np.clip(lv_b * 0.86, 0.0, 0.88) + np.clip(glow * 0.12, 0.0, 0.2) * (1.0 - np.clip(lv_b * 0.86, 0.0, 0.88))
            if self._paper is not None:
                pr, pg, pb = float(self._paper[0]), float(self._paper[1]), float(self._paper[2])
                out_r = pr * (1.0 - cov_r) + ir * cov_r
                out_g = pg * (1.0 - cov_g) + ig * cov_g
                out_b = pb * (1.0 - cov_b) + ib * cov_b
                out_a = np.ones_like(out_r)
            else:
                out_r, out_g, out_b = ir * cov_r, ig * cov_g, ib * cov_b
                out_a = np.maximum(np.maximum(cov_r, cov_g), cov_b)

        buf = np.empty((rh, rw, 4), dtype=np.uint8)
        buf[..., 0] = np.clip(out_b * 255.0 + 0.5, 0, 255).astype(np.uint8)
        buf[..., 1] = np.clip(out_g * 255.0 + 0.5, 0, 255).astype(np.uint8)
        buf[..., 2] = np.clip(out_r * 255.0 + 0.5, 0, 255).astype(np.uint8)
        buf[..., 3] = np.clip(out_a * 255.0 + 0.5, 0, 255).astype(np.uint8)

        surf = cairo.ImageSurface.create_for_data(
            bytearray(buf.tobytes()),
            cairo.Format.ARGB32,
            rw,
            rh,
            rw * 4,
        )
        cr.save()
        cr.scale(float(width) / float(rw), float(height) / float(rh))
        cr.set_source_surface(surf, 0.0, 0.0)
        cr.get_source().set_filter(cairo.Filter.BILINEAR)
        cr.paint()
        cr.restore()
