"""Pixel-art aquarium boid simulation rendered in Cairo + NumPy for Vimona's composer.

Faithfully implements the quarter-resolution Bayer-dithered water, moving
caustics, light rays, sand, swaying seaweed, ripples, rising bubbles, and
4-species boid fish schooling from aquarium-prompt.tsx with FILTER_NEAREST
pixel-art scaling.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

import cairo
import numpy as np

PX = 4  # CSS pixels per tank pixel

_BAYER = np.array(
    [
        [0, 8, 2, 10],
        [12, 4, 14, 6],
        [3, 11, 1, 9],
        [15, 7, 13, 5],
    ],
    dtype=np.float32,
) / 16.0 - 0.5


@dataclass(frozen=True)
class Species:
    body: tuple[int, int, int]
    belly: tuple[int, int, int]
    fin: tuple[int, int, int]
    stripe: tuple[int, int, int] | None
    length: float
    tall: float
    school: int
    speed: float


SPECIES: list[Species] = [
    Species((255, 128, 34), (255, 170, 80), (214, 82, 16), (255, 255, 255), 11.5, 0.50, 3, 1.00),  # clownfish
    Species((36, 92, 220), (70, 140, 240), (255, 214, 40), None, 12.0, 0.55, 4, 1.05),             # blue tang
    Species((255, 214, 32), (255, 236, 110), (240, 180, 0), None, 9.5, 0.62, 4, 1.10),             # yellow tang
    Species((170, 206, 228), (222, 240, 250), (120, 160, 190), None, 6.8, 0.38, 10, 1.35),         # sardines
]


@dataclass
class Fish:
    x: float
    y: float
    vx: float
    vy: float
    s: int
    size: float
    phase: float
    dart: float = 0.0
    gone: float = 0.0


@dataclass
class Bubble:
    x: float
    y: float
    r: int
    v: float
    wob: float


@dataclass
class Ripple:
    x: float
    y: float
    t0: float


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return hi if v > hi else (lo if v < lo else v)


class AquariumSimulation:
    """Self-contained interactive pixel-art aquarium world."""

    def __init__(self, fish_count: int = 24) -> None:
        self._fish_count = fish_count
        self.fish: list[Fish] = []
        self.bubbles: list[Bubble] = []
        self.ripples: list[Ripple] = []
        self.ptr_x: float = -999.0
        self.ptr_y: float = -999.0
        self.ptr_on: bool = False
        self.caret_x: float = 0.0
        self.caret_y: float = 0.0
        self.caret_t: float = -99.0
        self.gulp_x: float = 0.0
        self.gulp_y: float = 0.0
        self.gulp_t: float = -99.0
        self.w: float = 0.0
        self.h: float = 0.0
        self._t0 = time.monotonic()
        self._last = self._t0

    def ensure_size(self, w: float, h: float) -> None:
        if w <= 4 or h <= 4:
            return
        first = not self.fish
        self.w = w
        self.h = h
        if first:
            self._spawn_fish(self._fish_count, w, h)

    def _spawn_fish(self, n: int, w: float, h: float) -> None:
        out: list[Fish] = []
        s = 0
        while len(out) < n:
            sp = SPECIES[s % len(SPECIES)]
            cx = 36.0 + random.random() * max(20.0, w - 72.0)
            cy = 24.0 + random.random() * max(20.0, h - 56.0)
            direction = -1.0 if random.random() < 0.5 else 1.0
            for _ in range(sp.school):
                if len(out) >= n:
                    break
                out.append(
                    Fish(
                        x=cx + (random.random() - 0.5) * 60.0,
                        y=cy + (random.random() - 0.5) * 32.0,
                        vx=direction * (0.6 + random.random() * 0.5),
                        vy=(random.random() - 0.5) * 0.3,
                        s=s % len(SPECIES),
                        size=0.82 + random.random() * 0.28,
                        phase=random.random() * math.tau,
                    )
                )
            s += 1
        self.fish = out

    def on_pointer_move(self, x: float, y: float) -> None:
        self.ptr_x = x
        self.ptr_y = y
        self.ptr_on = True

    def on_pointer_leave(self) -> None:
        self.ptr_on = False

    def add_ripple(self, x: float, y: float) -> None:
        self.ripples.append(Ripple(x=x, y=y, t0=time.monotonic()))

    def notify_typing(self, x: float, y: float) -> None:
        now = time.monotonic()
        self.caret_x = x
        self.caret_y = y
        self.caret_t = now
        self.bubbles.append(
            Bubble(
                x=x + (random.random() - 0.5) * 8.0,
                y=y - 8.0,
                r=1 + random.randint(0, 1),
                v=0.5 + random.random() * 0.4,
                wob=random.random() * 6.0,
            )
        )

    def trigger_gulp(self, x: float, y: float) -> None:
        self.gulp_x = x
        self.gulp_y = y
        self.gulp_t = time.monotonic()

    def step(self) -> None:
        if not self.fish or self.w <= 4 or self.h <= 4:
            return
        now = time.monotonic()
        dt = min(0.05, max(0.001, now - self._last))
        self._last = now
        k = dt * 60.0

        w_px, h_px = self.w, self.h
        typing = (now - self.caret_t) < 1.6
        gulping = (now - self.gulp_t) < 1.4
        ts = now - self._t0

        for i, f in enumerate(self.fish):
            sp = SPECIES[f.s]
            ax = 0.0
            ay = 0.0
            cx = 0.0
            cy = 0.0
            avx = 0.0
            avy = 0.0
            n = 0

            for j, o in enumerate(self.fish):
                if i == j:
                    continue
                dx = o.x - f.x
                dy = o.y - f.y
                d2 = dx * dx + dy * dy
                min_d = (sp.length + SPECIES[o.s].length) * PX * 0.56
                if 0.01 < d2 < min_d * min_d:
                    d = math.sqrt(d2)
                    ax -= (dx / d) * (min_d - d) * 0.012
                    ay -= (dy / d) * (min_d - d) * 0.012
                if o.s == f.s and d2 < 4900.0:
                    cx += o.x
                    cy += o.y
                    avx += o.vx
                    avy += o.vy
                    n += 1

            if n:
                ax += (cx / n - f.x) * 0.0005 + (avx / n - f.vx) * 0.022
                ay += (cy / n - f.y) * 0.0005 + (avy / n - f.vy) * 0.022

            hx = w_px * (0.5 + 0.38 * math.sin(ts * 0.07 + f.s * 1.9))
            hy = h_px * (0.46 + 0.28 * math.cos(ts * 0.09 + f.s * 2.7))
            ax += (hx - f.x) * 0.00035
            ay += (hy - f.y) * 0.00035

            f.phase += (0.18 + math.hypot(f.vx, f.vy) * 0.12) * k
            ax += math.sin(ts * 0.7 + i * 1.7) * 0.012
            ay += math.cos(ts * 0.9 + i * 2.3) * 0.008

            mx = 30.0
            my = 22.0
            if f.x < mx:
                ax += (mx - f.x) * 0.006
            if f.x > w_px - mx:
                ax -= (f.x - (w_px - mx)) * 0.006
            if f.y < my:
                ay += (my - f.y) * 0.006
            if f.y > h_px - my - 22.0:
                ay -= (f.y - (h_px - my - 22.0)) * 0.008

            if self.ptr_on and not gulping:
                dx = f.x - self.ptr_x
                dy = f.y - self.ptr_y
                d = math.hypot(dx, dy)
                if 0.1 < d < 110.0:
                    p = (110.0 - d) / 110.0
                    ax += (dx / d) * p * 0.6
                    ay += (dy / d) * p * 0.6
                    f.dart = max(f.dart, p)

            for rp in self.ripples:
                if (now - rp.t0) > 0.6:
                    continue
                dx = f.x - rp.x
                dy = f.y - rp.y
                d = math.hypot(dx, dy) or 1.0
                if d < 160.0:
                    ax += (dx / d) * 0.5 * (1.0 - d / 160.0)
                    ay += (dy / d) * 0.5 * (1.0 - d / 160.0)
                    f.dart = 1.0

            if typing and not gulping:
                dx = self.caret_x - f.x
                dy = self.caret_y - f.y
                d = math.hypot(dx, dy) or 1.0
                want = 46.0 + (i % 5) * 9.0
                pull = (d - want) * 0.0016 * (1.0 - (now - self.caret_t) / 1.6)
                ax += (dx / d) * pull
                ay += (dy / d) * pull

            if gulping:
                dx = self.gulp_x - f.x
                dy = self.gulp_y - f.y
                d = math.hypot(dx, dy) or 1.0
                ax += (dx / d) * 0.32
                ay += (dy / d) * 0.32
                if d < 26.0:
                    f.gone = min(1.0, f.gone + 0.12 * k)
            elif f.gone > 0.0:
                f.gone = max(0.0, f.gone - 0.03 * k)
                if f.gone > 0.9:
                    f.x = -20.0 if random.random() < 0.5 else w_px + 20.0
                    f.y = 24.0 + random.random() * max(16.0, h_px - 60.0)
                    f.vx = 1.0 if f.x < 0.0 else -1.0

            f.dart = max(0.0, f.dart - 0.02 * k)
            max_v = 1.25 * sp.speed * (1.0 + f.dart * 2.2 + (1.6 if gulping else 0.0))
            f.vx += ax * k
            f.vy += ay * k
            v = math.hypot(f.vx, f.vy)
            min_v = 0.35 * sp.speed
            if v > max_v:
                f.vx *= max_v / v
                f.vy *= max_v / v
            elif v < min_v:
                scale_v = min_v / max(v, 0.01)
                f.vx *= scale_v
                f.vy *= scale_v
            f.vy *= 0.985
            f.x += f.vx * k
            f.y += f.vy * k

        if random.random() < 0.045 * k:
            self.bubbles.append(
                Bubble(
                    x=w_px * (0.08 + random.random() * 0.84),
                    y=h_px - 18.0,
                    r=1 + random.randint(0, 1),
                    v=0.35 + random.random() * 0.4,
                    wob=random.random() * 6.0,
                )
            )

        alive_b: list[Bubble] = []
        for b in self.bubbles:
            b.y -= b.v * k
            b.wob += 0.08 * k
            if b.y > -10.0:
                alive_b.append(b)
        self.bubbles = alive_b
        self.ripples = [r for r in self.ripples if (now - r.t0) < 1.2]

    def draw(self, cr: cairo.Context, w: int, h: int) -> None:
        if w <= 4 or h <= 4:
            return
        self.ensure_size(float(w), float(h))
        lw = max(8, (w + PX - 1) // PX)
        lh = max(8, (h + PX - 1) // PX)
        now = time.monotonic()
        t = now - self._t0

        # Vectorized Bayer-dithered water + moving caustics + top light rays
        xs = np.arange(lw, dtype=np.float32)[None, :]
        ys = np.arange(lh, dtype=np.float32)[:, None]
        gy = ys / float(lh)

        c1 = np.sin(xs * 0.21 + t * 0.9 + np.sin(ys * 0.17 + t * 0.6) * 1.6) + np.sin(
            ys * 0.19 - t * 0.7 + np.sin(xs * 0.13 - t * 0.5) * 1.4
        )
        caustic = np.where(c1 > 1.25, 1.0 - gy * 0.7, 0.0)
        rays = (np.maximum(0.0, np.sin((xs - ys * 0.55) * 0.09 + t * 0.15)) ** 6) * (1.0 - gy) * 0.5
        dither = _BAYER[(np.arange(lh)[:, None] & 3), (np.arange(lw)[None, :] & 3)] * 0.09
        level = np.round(np.clip(gy + dither, 0.0, 1.0) * 6.0) / 6.0

        glow = caustic * 0.22 + rays * 0.35
        r_ch = 168.0 - 120.0 * level
        g_ch = 236.0 - 62.0 * level
        b_ch = 242.0 - 46.0 * level
        r_ch = r_ch + (255.0 - r_ch) * glow
        g_ch = g_ch + (255.0 - g_ch) * glow
        b_ch = b_ch + (255.0 - b_ch) * glow

        buf = np.empty((lh, lw, 4), dtype=np.uint8)
        # Cairo ARGB32 little-endian memory order: B, G, R, A
        buf[:, :, 0] = np.clip(b_ch, 0, 255).astype(np.uint8)
        buf[:, :, 1] = np.clip(g_ch, 0, 255).astype(np.uint8)
        buf[:, :, 2] = np.clip(r_ch, 0, 255).astype(np.uint8)
        buf[:, :, 3] = 255

        # Sand floor & swaying weed along the bottom
        sand_top = max(2, lh - 6)
        for y in range(sand_top, lh):
            for x in range(lw):
                bump = math.sin(x * 0.3) * 0.8 + math.sin(x * 0.11) * 1.4
                if y < sand_top + 2 + bump:
                    continue
                shade = (18 if (x * 7 + y * 13) % 5 == 0 else 0) + (y - sand_top) * 3
                buf[y, x, 2] = max(0, 236 - shade)
                buf[y, x, 1] = max(0, 214 - shade)
                buf[y, x, 0] = max(0, 170 - shade)

        for s in range(0, lw, 11):
            base = s + ((s * 7) % 5)
            tall = 7 + ((s * 13) % 7)
            for k in range(tall):
                wx = round(base + math.sin(t * 1.3 + s + k * 0.35) * (k * 0.12))
                wy = sand_top - k + 2
                if 0 <= wx < lw and 0 <= wy < lh:
                    dark = (k % 3 == 0)
                    buf[wy, wx, 2] = 34 if dark else 52
                    buf[wy, wx, 1] = 130 if dark else 160
                    buf[wy, wx, 0] = 96 if dark else 110

        # Bubbles
        for b in self.bubbles:
            bx = round((b.x + math.sin(b.wob) * 3.0) / PX)
            by = round(b.y / PX)
            for yy in range(-b.r, b.r + 1):
                for xx in range(-b.r, b.r + 1):
                    px_i = bx + xx
                    py_i = by + yy
                    if not (0 <= px_i < lw and 0 <= py_i < lh):
                        continue
                    r2 = xx * xx + yy * yy
                    if r2 > b.r * b.r + 0.5:
                        continue
                    rim = (r2 >= b.r * b.r - 0.5) or (b.r == 1)
                    mix = 0.75 if rim else 0.25
                    buf[py_i, px_i, 0] = int(buf[py_i, px_i, 0] + (255 - buf[py_i, px_i, 0]) * mix)
                    buf[py_i, px_i, 1] = int(buf[py_i, px_i, 1] + (255 - buf[py_i, px_i, 1]) * mix)
                    buf[py_i, px_i, 2] = int(buf[py_i, px_i, 2] + (255 - buf[py_i, px_i, 2]) * mix)

        # Ripples
        for rp in self.ripples:
            age = now - rp.t0
            rad = (age * 140.0) / PX
            alpha = 0.6 * (1.0 - age / 1.2)
            if alpha <= 0.0:
                continue
            rcx = rp.x / PX
            rcy = rp.y / PX
            steps = max(12, round(rad * 6))
            for st in range(steps):
                ang = (st / steps) * math.tau
                px_i = round(rcx + math.cos(ang) * rad)
                py_i = round(rcy + math.sin(ang) * rad * 0.8)
                if 0 <= px_i < lw and 0 <= py_i < lh:
                    buf[py_i, px_i, 0] = int(buf[py_i, px_i, 0] + (255 - buf[py_i, px_i, 0]) * alpha)
                    buf[py_i, px_i, 1] = int(buf[py_i, px_i, 1] + (255 - buf[py_i, px_i, 1]) * alpha)
                    buf[py_i, px_i, 2] = int(buf[py_i, px_i, 2] + (255 - buf[py_i, px_i, 2]) * alpha)

        # Fish
        for f in self.fish:
            self._paint_fish(buf, lw, lh, f)

        surface = cairo.ImageSurface.create_for_data(buf, cairo.FORMAT_ARGB32, lw, lh, lw * 4)
        cr.save()
        cr.scale(w / float(lw), h / float(lh))
        cr.set_source_surface(surface, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_NEAREST)
        cr.paint()
        cr.restore()

    def _paint_fish(self, buf: np.ndarray, w: int, h: int, f: Fish) -> None:
        sp = SPECIES[f.s]
        L = sp.length * f.size
        T = L * sp.tall
        cx = f.x / PX
        cy = f.y / PX
        facing = 1.0 if f.vx >= 0.0 else -1.0
        tilt = _clamp(math.atan2(f.vy, abs(f.vx) + 0.001), -0.6, 0.6) * facing
        ca = math.cos(-tilt)
        sa = math.sin(-tilt)
        wag = math.sin(f.phase) * 0.35
        rad = math.ceil(L * 0.75)
        alpha = 1.0 - f.gone
        if alpha <= 0.02:
            return

        y0 = max(0, math.floor(cy - rad))
        y1 = min(h - 1, math.floor(cy + rad))
        x0 = max(0, math.floor(cx - rad))
        x1 = min(w - 1, math.floor(cx + rad))
        for py in range(y0, y1 + 1):
            for px in range(x0, x1 + 1):
                dx = (px + 0.5 - cx) * facing
                dy = py + 0.5 - cy
                u = dx * ca - dy * sa * facing
                v = dx * sa * facing + dy * ca
                col: tuple[int, int, int] | None = None
                bu = u / (L * 0.5)
                bv = v / (T * 0.5)
                if bu * bu + bv * bv <= 1.0:
                    if v > T * 0.12:
                        col = sp.belly
                    elif v < -T * 0.22:
                        col = sp.fin
                    else:
                        col = sp.body
                    if sp.stripe and (abs(u - L * 0.12) < 0.9 or abs(u + L * 0.18) < 0.8):
                        col = sp.stripe
                    if abs(u - L * 0.30) < 0.75 and abs(v + T * 0.08) < 0.75:
                        col = (18, 22, 30)
                else:
                    tu = -u - L * 0.42
                    tv = v - wag * tu
                    if 0.0 < tu < L * 0.32 and abs(tv) < tu * 0.95 + 0.4:
                        col = sp.fin
                    elif (
                        -L * 0.15 < u < L * 0.15
                        and v < -T * 0.45
                        and v > -T * 0.45 - (L * 0.15 - abs(u)) * 0.9
                    ):
                        col = sp.fin
                if col is None:
                    continue
                if alpha >= 0.99:
                    buf[py, px, 2] = col[0]
                    buf[py, px, 1] = col[1]
                    buf[py, px, 0] = col[2]
                else:
                    b0 = int(buf[py, px, 0])
                    g0 = int(buf[py, px, 1])
                    r0 = int(buf[py, px, 2])
                    buf[py, px, 2] = max(0, min(255, int(r0 + (col[0] - r0) * alpha)))
                    buf[py, px, 1] = max(0, min(255, int(g0 + (col[1] - g0) * alpha)))
                    buf[py, px, 0] = max(0, min(255, int(b0 + (col[2] - b0) * alpha)))
