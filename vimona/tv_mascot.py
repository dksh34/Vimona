"""Procedural 3D-projected 1980s CRT Television mascot ('Vimona') for GTK4/Cairo."""

from __future__ import annotations

import math
import random
import time
from typing import Callable, Literal

import cairo
import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

TvMascotState = Literal["idle", "thinking", "working", "done", "limit"]

TV_MASCOT_STATES: list[tuple[TvMascotState, str]] = [
    ("idle", "Idle"),
    ("thinking", "Thinking"),
    ("working", "Working"),
    ("done", "Done"),
    ("limit", "At limit"),
]

DEFAULT_STATUS: dict[TvMascotState, str | None] = {
    "idle": None,
    "thinking": "Thinking…",
    "working": "Working…",
    "done": None,
    "limit": None,
}

SEAT_Y = -0.64


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _smooth(a: float, b: float, v: float) -> float:
    t = _clamp((v - a) / max(b - a, 1e-6), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _mix_rgb(
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    t: float,
) -> tuple[float, float, float]:
    t = _clamp(t, 0.0, 1.0)
    return (
        c1[0] + (c2[0] - c1[0]) * t,
        c1[1] + (c2[1] - c1[1]) * t,
        c1[2] + (c2[2] - c1[2]) * t,
    )


def _round_rect(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, abs(w) * 0.5, abs(h) * 0.5)
    if r <= 0.1:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi * 0.5, 0.0)
    cr.arc(x + w - r, y + h - r, r, 0.0, math.pi * 0.5)
    cr.arc(x + r, y + h - r, r, math.pi * 0.5, math.pi)
    cr.arc(x + r, y + r, r, math.pi, math.pi * 1.5)
    cr.close_path()


class _Pose:
    __slots__ = (
        "x",
        "y",
        "tilt",
        "sx",
        "sy",
        "yaw",
        "pitch",
        "look_x",
        "look_y",
        "eye_size",
        "squint",
        "happy",
        "blink",
        "glasses",
        "think",
        "fly",
        "swarm",
        "sweat",
        "gloom",
    )

    def __init__(self) -> None:
        self.x = 0.0
        self.y = 0.0
        self.tilt = 0.0
        self.sx = 1.0
        self.sy = 1.0
        self.yaw = 0.0
        self.pitch = 0.0
        self.look_x = 0.0
        self.look_y = 0.0
        self.eye_size = 1.0
        self.squint = 0.0
        self.happy = 0.0
        self.blink = 0.0
        self.glasses = 0.0
        self.think = 0.0
        self.fly = 0.0
        self.swarm = 0.0
        self.sweat = 0.0
        self.gloom = 0.0

    def copy(self) -> _Pose:
        p = _Pose()
        for k in self.__slots__:
            setattr(p, k, getattr(self, k))
        return p


_EXACT_FIELDS = ("x", "y", "tilt", "sx", "sy", "blink")


def _dress(pose: _Pose, state: TvMascotState) -> None:
    if state == "thinking":
        pose.look_x = 0.022
        pose.look_y = 0.024
        pose.squint = 1.0
        pose.think = 1.0
    elif state == "working":
        pose.glasses = 1.0
        pose.fly = 1.0
        pose.eye_size = 0.92
    elif state == "done":
        pose.happy = 1.0
        pose.swarm = 1.0
    elif state == "limit":
        pose.eye_size = 1.30
        pose.sweat = 1.0
        pose.gloom = 1.0
        pose.look_y = 0.006


def _fly_path(t: float) -> tuple[float, float, float]:
    a = t * 1.35
    return (
        1.14 * math.sin(a) + 0.03 * math.sin(t * 13.0),
        0.96 + 0.12 * math.sin(t * 2.7) + 0.03 * math.cos(t * 17.0),
        0.15 + 0.72 * math.cos(a),
    )


def _swarm_path(t: float, i: int) -> tuple[float, float, float]:
    a = t * (1.6 + i * 0.17) + (i * math.tau) / 5.0
    r = 1.14 + 0.1 * math.sin(i * 1.7)
    return (
        r * math.sin(a),
        1.02 + 0.22 * math.sin(i * 2.1) + 0.08 * math.sin(t * 4.0 + i),
        0.05 + 0.6 * math.cos(a),
    )


class TvMascotSim:
    """Real-time 3D-projected 1980s CRT television mascot simulation."""

    def __init__(self, state: TvMascotState = "idle", follow_pointer: bool = True) -> None:
        self.state: TvMascotState = state
        self.follow_pointer = follow_pointer
        self.animate = True

        now = time.monotonic()
        self._start = now
        self._last = now
        self._state_since = 0.0
        self._last_state: TvMascotState = state

        self._pose = _Pose()
        _dress(self._pose, state)
        self._from_pose: _Pose | None = None
        self._blend_at = 0.0

        self._blink_at = 1.2 + random.random() * 2.0
        self._gaze = [0.0, 0.0, 0.0]
        self._aim_eye = [0.0, 0.0]
        self._aim_head = [0.0, 0.0]
        self._pointer: tuple[float, float] | None = None
        self._fly_pos = _fly_path(0.0)
        self._fly_last = [self._fly_pos] * 5

    def set_state(self, state: TvMascotState) -> None:
        if state != self.state:
            self.state = state

    def set_pointer(self, nx: float, ny: float) -> None:
        """Normalized pointer relative to widget center in [-1, 1]."""
        self._pointer = (_clamp(nx, -1.0, 1.0), _clamp(ny, -1.0, 1.0))

    def clear_pointer(self) -> None:
        self._pointer = None

    def _target_pose(self, t: float) -> _Pose:
        state = self.state
        since = t - self._state_since
        p = _Pose()
        _dress(p, state)
        breath = math.sin(t * 1.6)
        calm = state == "idle" or (state == "done" and since > 1.2)

        if calm:
            if self.follow_pointer and self._pointer is not None:
                p.look_x = self._aim_eye[0] * 0.032
                p.look_y = self._aim_eye[1] * 0.022
            else:
                if t > self._gaze[2]:
                    self._gaze = [
                        (random.random() - 0.5) * 0.04,
                        (random.random() - 0.5) * 0.024,
                        t + 1.6 + random.random() * 2.6,
                    ]
                p.look_x = self._gaze[0]
                p.look_y = self._gaze[1]
            p.y = 0.025 * max(0.0, math.sin(t * 1.1))
            p.sy = 1.0 + 0.016 * breath
            p.sx = 1.0 - 0.010 * breath
            p.tilt = 0.022 * math.sin(t * 0.7)
            p.yaw = 0.08 * math.sin(t * 0.45)
            if state == "done":
                opened = since >= 3.0
                p.happy = 0.0 if opened else 1.0
                p.swarm = 0.0 if opened else 1.0
        elif state == "thinking":
            p.look_x += 0.005 * math.sin(t * 0.9)
            p.tilt = 0.065 + 0.016 * math.sin(t * 0.8)
            p.yaw = 0.22
            p.pitch = -0.09
            p.sy = 1.0 + 0.012 * breath
        elif state == "working":
            dx = _clamp(self._fly_pos[0] / 1.14, -1.0, 1.0)
            p.look_x = dx * 0.034 * (0.5 if self._fly_pos[2] < 0 else 1.0)
            p.look_y = 0.022
            p.yaw = dx * 0.34
            p.pitch = -0.10
            p.tilt = -dx * 0.045
            p.sy = 1.0 + 0.013 * breath
        elif state == "done":
            u = since / 1.2
            if u < 0.15:
                s = math.sin((u / 0.15) * math.pi)
                p.sy = 1.0 - 0.13 * s
                p.sx = 1.0 + 0.10 * s
            elif u < 0.72:
                v = (u - 0.15) / 0.57
                p.y = 0.46 * 4.0 * v * (1.0 - v)
                p.sy = 1.0 + 0.08 * math.sin(v * math.pi)
                p.sx = 1.0 - 0.05 * math.sin(v * math.pi)
            elif u < 0.9:
                s = math.sin(((u - 0.72) / 0.18) * math.pi)
                p.sy = 1.0 - 0.11 * s
                p.sx = 1.0 + 0.09 * s
            p.tilt = -0.07 * math.sin(u * math.tau)
        elif state == "limit":
            u = since % 2.2
            shake = 1.0 - u / 0.6 if u < 0.6 else 0.0
            p.x = 0.045 * math.sin(u * 38.0) * shake
            p.sx = 1.01
            p.sy = 0.96
            p.pitch = 0.04

        p.x += self._aim_head[0] * 0.05
        p.yaw += self._aim_head[0] * 0.42
        p.pitch -= self._aim_head[1] * 0.22

        if t > self._blink_at + 0.15:
            self._blink_at = t + (6.0 if state == "limit" else 3.4) + random.random() * 3.2
        b = (t - self._blink_at) / 0.15
        if 0.0 <= b <= 1.0 and p.happy < 0.5:
            p.blink = b / 0.4 if b < 0.4 else 1.0 - (b - 0.4) / 0.6
        return p

    def step(self) -> float:
        now = time.monotonic()
        t = now - self._start
        dt = min(0.08, max(0.001, now - self._last))
        self._last = now

        if not self.animate:
            self._pose = _Pose()
            _dress(self._pose, self.state)
            self._fly_pos = _fly_path(0.6)
            return 1.1

        if self.state != self._last_state:
            self._from_pose = self._pose.copy()
            self._blend_at = t
            self._state_since = t
            self._last_state = self.state

        pt = self._pointer if self.follow_pointer else None
        px = pt[0] if pt else 0.0
        py = pt[1] if pt else 0.0
        ke = 1.0 - math.exp(-dt * 12.0)
        kh = 1.0 - math.exp(-dt * 5.0)
        self._aim_eye[0] += (px - self._aim_eye[0]) * ke
        self._aim_eye[1] += (py - self._aim_eye[1]) * ke
        self._aim_head[0] += (px - self._aim_head[0]) * kh
        self._aim_head[1] += (py - self._aim_head[1]) * kh

        self._fly_pos = _fly_path(t)
        goal = self._target_pose(t)
        s = min(1.0, (t - self._blend_at) / 0.4) if self._from_pose is not None else 1.0
        blend = s * s * s * (s * (s * 6.0 - 15.0) + 10.0)
        if s >= 1.0:
            self._from_pose = None
        ease = 1.0 - math.exp(-dt * 10.0)

        for key in _Pose.__slots__:
            gv = getattr(goal, key)
            cv = getattr(self._pose, key)
            if self._from_pose is not None and s < 1.0 and key in _EXACT_FIELDS and key != "blink":
                fv = getattr(self._from_pose, key)
                setattr(self._pose, key, fv + (gv - fv) * blend)
            elif key in _EXACT_FIELDS:
                setattr(self._pose, key, gv)
            else:
                setattr(self._pose, key, cv + (gv - cv) * ease)
        return t

    # ------------------------------------------------------------------ 3D projection & draw

    def _xform(self, x: float, y: float, z: float, p: _Pose) -> tuple[float, float, float]:
        # Scale from seat pivot, then shift to cabinet center
        sx, sy = x * p.sx, (y - SEAT_Y) * p.sy + SEAT_Y
        sz = z * p.sx
        # Rotate pitch (X), yaw (Y), tilt (Z) around TV center
        cy_c = -0.05
        ry = sy - cy_c
        # Z tilt
        ct, st = math.cos(p.tilt), math.sin(p.tilt)
        tx = sx * ct - ry * st
        ty = sx * st + ry * ct
        # X pitch
        cp, sp = math.cos(p.pitch), math.sin(p.pitch)
        py = ty * cp - sz * sp
        pz = ty * sp + sz * cp
        # Y yaw
        cy, syaw = math.cos(p.yaw), math.sin(p.yaw)
        yx = tx * cy + pz * syaw
        yz = -tx * syaw + pz * cy
        return (yx + p.x, py + cy_c + p.y, yz)

    @staticmethod
    def _proj(
        pt: tuple[float, float, float],
        cx: float,
        cy: float,
        scale: float,
    ) -> tuple[float, float, float]:
        cam_z = 6.4
        dz = max(1.0, cam_z - pt[2])
        f = cam_z / dz
        return (cx + pt[0] * scale * f, cy - (pt[1] - 0.14) * scale * f, pt[2])

    def _draw_quad(
        self,
        cr: cairo.Context,
        pts2: list[tuple[float, float, float]],
        col: tuple[float, float, float],
        alpha: float = 1.0,
        stroke_col: tuple[float, float, float] | None = None,
        stroke_w: float = 1.0,
    ) -> None:
        cr.new_path()
        cr.move_to(pts2[0][0], pts2[0][1])
        for px, py, _ in pts2[1:]:
            cr.line_to(px, py)
        cr.close_path()
        cr.set_source_rgba(col[0], col[1], col[2], alpha)
        if stroke_col is not None:
            cr.fill_preserve()
            cr.set_source_rgba(stroke_col[0], stroke_col[1], stroke_col[2], alpha)
            cr.set_line_width(stroke_w)
            cr.stroke()
        else:
            cr.fill()

    def _draw_fly(
        self,
        cr: cairo.Context,
        cx: float,
        cy: float,
        scale: float,
        t: float,
        idx: int,
        at: tuple[float, float, float],
        amount: float,
    ) -> None:
        px, py, pz = self._proj(at, cx, cy, scale)
        depth_scale = (6.4 / max(1.0, 6.4 - pz)) * (scale / 90.0) * amount
        r_body = max(2.0, 5.2 * depth_scale)
        flap = math.sin(t * 68.0 + idx) * 0.65

        cr.save()
        cr.translate(px, py)
        # Translucent fluttering wings
        for side in (-1.0, 1.0):
            cr.save()
            cr.rotate(side * (0.45 + flap * 0.45))
            cr.scale(1.0, 0.48)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.82 * amount)
            cr.arc(side * r_body * 0.95, -r_body * 0.9, r_body * 0.95, 0, math.tau)
            cr.fill()
            cr.restore()
        # Glossy bug abdomen & head
        cr.set_source_rgba(0.125, 0.141, 0.169, 0.96 * amount)
        cr.save()
        cr.scale(1.15, 0.85)
        cr.arc(0.0, 0.0, r_body, 0, math.tau)
        cr.fill()
        cr.restore()
        # Specular shine
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.42 * amount)
        cr.arc(-r_body * 0.3, -r_body * 0.3, r_body * 0.32, 0, math.tau)
        cr.fill()
        cr.restore()

    def draw(self, cr: cairo.Context, w: int, h: int, t: float | None = None) -> None:
        if w <= 2 or h <= 2:
            return
        if t is None:
            t = time.monotonic() - self._start

        p = self._pose
        cx = w * 0.5
        cy = h * 0.52
        scale = min(w, h) * 0.34

        # 1. Soft floor shadow under his rubber feet
        lift = _clamp(p.y / 0.46, 0.0, 1.0)
        sh_x, sh_y, _ = self._proj((p.x, SEAT_Y - 0.02, 0.0), cx, cy, scale)
        sh_rx = scale * 0.96 * (1.0 - lift * 0.38)
        sh_ry = sh_rx * 0.24
        sh_alpha = 0.42 * (1.0 - lift * 0.65)
        grad = cairo.RadialGradient(sh_x, sh_y, 0.0, sh_x, sh_y, max(sh_rx, 1.0))
        grad.add_color_stop_rgba(0.0, 0.0, 0.0, 0.0, sh_alpha)
        grad.add_color_stop_rgba(0.55, 0.0, 0.0, 0.0, sh_alpha * 0.45)
        grad.add_color_stop_rgba(1.0, 0.0, 0.0, 0.0, 0.0)
        cr.save()
        cr.translate(sh_x, sh_y)
        cr.scale(1.0, sh_ry / max(sh_rx, 1.0))
        cr.set_source(grad)
        cr.arc(0.0, 0.0, sh_rx, 0.0, math.tau)
        cr.fill()
        cr.restore()

        # Prepare active flies sorted into behind-TV vs in-front-of-TV
        active_flies: list[tuple[int, tuple[float, float, float], float]] = []
        for i in range(5):
            working = p.fly if i == 0 else 0.0
            pop = _clamp(p.swarm * 1.4 - i * 0.08, 0.0, 1.0)
            amount = max(working, pop)
            if amount <= 0.02:
                continue
            if working >= pop:
                at = self._fly_pos
            else:
                sx, sy, sz = _swarm_path(t, i)
                away = 1.0 - p.swarm
                at = (sx * (1.0 + away * 1.4), sy + away * 0.7, sz * (1.0 + away * 1.4))
            at = (at[0] + p.x, at[1], at[2])
            active_flies.append((i, at, amount))

        for i, at, amount in sorted(active_flies, key=lambda item: item[1][2]):
            if at[2] < 0.15:
                self._draw_fly(cr, cx, cy, scale, t, i, at, amount)

        # 2. Classic 1980s CRT TV 3D Cabinet Geometry (Sony Trinitron / Zenith / RCA blend)
        # Watertight closed 6-face tapered cuboid drawn strictly back-to-front (painter's algo)
        hw, y_top, y_bot, hd = 0.84, 0.54, -0.56, 0.50
        bhw, by_top, by_bot, bhd = 0.74, 0.48, -0.50, -0.48

        # Cabinet 8 vertices (slightly tapered rear CRT chassis -> zero sharp artifacts)
        verts_3d = {
            "ftl": (-hw, y_top, hd),
            "ftr": (hw, y_top, hd),
            "fbr": (hw, y_bot, hd),
            "fbl": (-hw, y_bot, hd),
            "btl": (-bhw, by_top, bhd),
            "btr": (bhw, by_top, bhd),
            "bbr": (bhw, by_bot, bhd),
            "bbl": (-bhw, by_bot, bhd),
        }
        xformed = {k: self._xform(v[0], v[1], v[2], p) for k, v in verts_3d.items()}
        p2 = {k: self._proj(v, cx, cy, scale) for k, v in xformed.items()}

        # Grounded rubber feet directly beneath the lower walnut rails
        foot_off = ((hw - 0.26, y_bot - 0.015, hd - 0.16), (-hw + 0.26, y_bot - 0.015, hd - 0.16))
        for fx, fy, fz in foot_off:
            fp = self._proj(self._xform(fx, fy, fz, p), cx, cy, scale)
            cr.set_source_rgba(0.10, 0.11, 0.12, 0.98)
            cr.save()
            cr.translate(fp[0], fp[1])
            cr.rotate(p.tilt)
            cr.scale(1.0, 0.55)
            cr.arc(0.0, 0.0, scale * 0.075, 0.0, math.tau)
            cr.fill()
            cr.restore()

        # Walnut wood palette (goes pale/greyed on limit/gloom)
        grey_cab = (0.64, 0.66, 0.65)
        walnut_back = _mix_rgb((0.24, 0.15, 0.09), grey_cab, p.gloom * 0.65)
        walnut_top = _mix_rgb((0.56, 0.36, 0.22), grey_cab, p.gloom * 0.65)
        walnut_front = _mix_rgb((0.46, 0.28, 0.17), grey_cab, p.gloom * 0.65)
        walnut_side_l = _mix_rgb((0.39, 0.24, 0.15), grey_cab, p.gloom * 0.65)
        walnut_side_r = _mix_rgb((0.33, 0.20, 0.12), grey_cab, p.gloom * 0.65)
        walnut_bot = _mix_rgb((0.22, 0.13, 0.08), grey_cab, p.gloom * 0.65)
        edge_trim = _mix_rgb((0.26, 0.16, 0.10), (0.45, 0.47, 0.46), p.gloom * 0.65)

        # Draw all back/side/top/bottom faces sorted strictly back-to-front (painter's algo)
        cr.save()
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        hull_faces = [
            (("btl", "btr", "bbr", "bbl"), walnut_back),
            (("fbl", "fbr", "bbr", "bbl"), walnut_bot),
            (("btl", "ftl", "fbl", "bbl"), walnut_side_l),
            (("ftr", "btr", "bbr", "fbr"), walnut_side_r),
            (("btl", "btr", "ftr", "ftl"), walnut_top),
        ]
        hull_faces.sort(key=lambda item: sum(xformed[k][2] for k in item[0]) * 0.25)
        for names, col in hull_faces:
            self._draw_quad(cr, [p2[n] for n in names], col, stroke_col=edge_trim, stroke_w=1.4)
            if names == ("btl", "btr", "ftr", "ftl"):
                for u in (0.3, 0.65):
                    g0 = (_lerp(p2["btl"][0], p2["ftl"][0], u), _lerp(p2["btl"][1], p2["ftl"][1], u))
                    g1 = (_lerp(p2["btr"][0], p2["ftr"][0], u), _lerp(p2["btr"][1], p2["ftr"][1], u))
                    cr.set_source_rgba(0.75, 0.54, 0.36, 0.18)
                    cr.set_line_width(1.2)
                    cr.move_to(*g0)
                    cr.line_to(*g1)
                    cr.stroke()
            elif names in (("btl", "ftl", "fbl", "bbl"), ("ftr", "btr", "bbr", "fbr")):
                # Subtle horizontal walnut grain lines along side panels
                for u in (0.32, 0.66):
                    g0 = (_lerp(p2[names[0]][0], p2[names[3]][0], u), _lerp(p2[names[0]][1], p2[names[3]][1], u))
                    g1 = (_lerp(p2[names[1]][0], p2[names[2]][0], u), _lerp(p2[names[1]][1], p2[names[2]][1], u))
                    cr.set_source_rgba(0.68, 0.48, 0.31, 0.14)
                    cr.set_line_width(1.0)
                    cr.move_to(*g0)
                    cr.line_to(*g1)
                    cr.stroke()
        cr.restore()

        # 3. Chrome Top Rabbit-Ear Antennas & UHF Loop (mounted atop walnut roof)
        ant_base = self._proj(self._xform(0.0, y_top + 0.01, -0.06, p), cx, cy, scale)
        ear_l_tip = self._proj(self._xform(-0.56, y_top + 0.68, -0.14, p), cx, cy, scale)
        ear_r_tip = self._proj(self._xform(0.56, y_top + 0.68, -0.14, p), cx, cy, scale)
        loop_top = self._proj(self._xform(0.0, y_top + 0.42, -0.12, p), cx, cy, scale)

        # Chrome mount puck
        cr.set_source_rgba(0.18, 0.20, 0.23, 1.0)
        cr.save()
        cr.translate(ant_base[0], ant_base[1])
        cr.scale(1.0, 0.42)
        cr.arc(0.0, 0.0, scale * 0.13, 0.0, math.tau)
        cr.fill()
        cr.restore()

        # UHF wire loop behind rabbit ears
        cr.set_source_rgba(0.70, 0.74, 0.79, 0.85)
        cr.set_line_width(max(1.5, scale * 0.022))
        cr.save()
        lx = (ant_base[0] + loop_top[0]) * 0.5
        ly = (ant_base[1] + loop_top[1]) * 0.5
        cr.translate(lx, ly)
        cr.rotate(p.tilt * 0.8)
        cr.scale(0.82, 1.0)
        cr.arc(0.0, 0.0, max(3.0, abs(ant_base[1] - loop_top[1]) * 0.48), 0.0, math.tau)
        cr.stroke()
        cr.restore()

        # Left & Right telescoping rabbit-ear antennas + shiny tips
        for tip in (ear_l_tip, ear_r_tip):
            cr.set_source_rgba(0.84, 0.88, 0.92, 0.96)
            cr.set_line_width(max(1.8, scale * 0.028))
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(ant_base[0], ant_base[1] - 1.0)
            cr.line_to(tip[0], tip[1])
            cr.stroke()
            # Chrome ball finial
            ball_r = max(2.2, scale * 0.042)
            cr.set_source_rgba(0.95, 0.97, 1.0, 1.0)
            cr.arc(tip[0], tip[1], ball_r, 0.0, math.tau)
            cr.fill()
            cr.set_source_rgba(0.45, 0.50, 0.56, 0.55)
            cr.set_line_width(1.0)
            cr.stroke()

        # 4. Front Face (Walnut Outer Lip + Silver Chrome Rim + Charcoal Bezel + Curved CRT)
        # Draw the projected front cabinet quad first
        self._draw_quad(cr, [p2[n] for n in ("ftl", "ftr", "fbr", "fbl")], walnut_front, stroke_col=edge_trim, stroke_w=1.5)

        # Helper to map normalized front-face local coordinates u in [-1, 1], v in [-1, 1] into 2D
        hh = (y_top - y_bot) * 0.5
        def front_pt(u: float, v: float, z_bump: float = 0.008) -> tuple[float, float]:
            pt3 = self._xform(u * hw, (y_bot + y_top) * 0.5 + v * hh, hd + z_bump, p)
            pr = self._proj(pt3, cx, cy, scale)
            return (pr[0], pr[1])

        def draw_front_poly(
            coords: list[tuple[float, float]],
            col: tuple[float, float, float],
            alpha: float = 1.0,
            z_bump: float = 0.02,
            stroke_col: tuple[float, float, float] | None = None,
            stroke_w: float = 1.2,
        ) -> None:
            pts = [front_pt(u, v, z_bump) for u, v in coords]
            cr.new_path()
            cr.move_to(*pts[0])
            for x0, y0 in pts[1:]:
                cr.line_to(x0, y0)
            cr.close_path()
            cr.set_source_rgba(col[0], col[1], col[2], alpha)
            if stroke_col is not None:
                cr.fill_preserve()
                cr.set_source_rgba(stroke_col[0], stroke_col[1], stroke_col[2], alpha)
                cr.set_line_width(stroke_w)
                cr.stroke()
            else:
                cr.fill()

        # Brushed aluminum outer front trim
        silver_col = _mix_rgb((0.77, 0.80, 0.84), (0.85, 0.86, 0.86), p.gloom * 0.4)
        draw_front_poly([(-0.92, 0.89), (0.92, 0.89), (0.92, -0.89), (-0.92, -0.89)], silver_col, z_bump=0.012)
        # Charcoal inner front plate
        draw_front_poly(
            [(-0.88, 0.83), (0.88, 0.83), (0.88, -0.83), (-0.88, -0.83)],
            (0.115, 0.128, 0.145),
            z_bump=0.018,
            stroke_col=(0.06, 0.07, 0.08),
            stroke_w=1.2,
        )

        # Curved Convex CRT Screen Frame on left/center (u: -0.80..0.44, v: -0.73..0.73)
        scr_pts: list[tuple[float, float]] = []
        su0, su1, sv0, sv1 = -0.79, 0.44, -0.72, 0.72
        # Rounded octagonal / CRT pillow silhouette
        ring = [
            (su0 + 0.08, sv1), (su1 - 0.08, sv1),
            (su1, sv1 - 0.11), (su1, sv0 + 0.11),
            (su1 - 0.08, sv0), (su0 + 0.08, sv0),
            (su0, sv0 + 0.11), (su0, sv1 - 0.11),
        ]
        # Inset dark shadow rim around tube
        draw_front_poly(ring, (0.045, 0.055, 0.065), z_bump=0.024)

        # Inner glowing curved phosphor glass CRT surface
        su0_i, su1_i, sv0_i, sv1_i = -0.74, 0.39, -0.65, 0.65
        scr_center = front_pt((su0_i + su1_i) * 0.5, 0.0, z_bump=0.06)
        tl_s = front_pt(su0_i, sv1_i, z_bump=0.035)
        tr_s = front_pt(su1_i, sv1_i, z_bump=0.035)
        br_s = front_pt(su1_i, sv0_i, z_bump=0.035)
        bl_s = front_pt(su0_i, sv0_i, z_bump=0.035)

        scr_col_edge = _mix_rgb((0.065, 0.115, 0.125), (0.32, 0.35, 0.35), p.gloom * 0.85)
        scr_col_core = _mix_rgb((0.12, 0.21, 0.22), (0.52, 0.56, 0.54), p.gloom * 0.85)

        cr.save()
        # Curved pillow contour with quadratic bulge on top/right/bottom/left
        cr.new_path()
        tm_s = front_pt((su0_i + su1_i) * 0.5, sv1_i + 0.04, z_bump=0.05)
        rm_s = front_pt(su1_i + 0.03, 0.0, z_bump=0.05)
        bm_s = front_pt((su0_i + su1_i) * 0.5, sv0_i - 0.04, z_bump=0.05)
        lm_s = front_pt(su0_i - 0.03, 0.0, z_bump=0.05)
        cr.move_to(*tl_s)
        cr.curve_to(tm_s[0], tm_s[1], tm_s[0], tm_s[1], *tr_s)
        cr.curve_to(rm_s[0], rm_s[1], rm_s[0], rm_s[1], *br_s)
        cr.curve_to(bm_s[0], bm_s[1], bm_s[0], bm_s[1], *bl_s)
        cr.curve_to(lm_s[0], lm_s[1], lm_s[0], lm_s[1], *tl_s)
        cr.close_path()

        tube_r = max(12.0, math.hypot(tr_s[0] - tl_s[0], tr_s[1] - tl_s[1]) * 0.72)
        scr_grad = cairo.RadialGradient(scr_center[0], scr_center[1], 1.0, scr_center[0], scr_center[1], tube_r)
        scr_grad.add_color_stop_rgba(0.0, *scr_col_core, 1.0)
        scr_grad.add_color_stop_rgba(1.0, *scr_col_edge, 1.0)
        cr.set_source(scr_grad)
        cr.fill_preserve()
        cr.clip()

        # Subtle CRT phosphor scanlines inside screen
        scr_w_px = abs(tr_s[0] - tl_s[0])
        scr_h_px = abs(bl_s[1] - tl_s[1])
        top_y = min(tl_s[1], tr_s[1]) - 6.0
        bot_y = max(bl_s[1], br_s[1]) + 6.0
        left_x = min(tl_s[0], bl_s[0]) - 6.0
        right_x = max(tr_s[0], br_s[0]) + 6.0
        cr.set_source_rgba(0.02, 0.05, 0.05, 0.24)
        cr.set_line_width(1.0)
        y_scan = top_y
        step_y = max(3.0, scr_h_px / 18.0)
        while y_scan < bot_y:
            cr.move_to(left_x, y_scan)
            cr.line_to(right_x, y_scan)
            y_scan += step_y
        cr.stroke()

        # Limit-state TV static flicker bars inside the CRT screen
        if p.gloom > 0.08:
            rng_phase = int(t * 24)
            for k in range(5):
                sy_line = top_y + ((rng_phase * 37 + k * 53) % 100) / 100.0 * (bot_y - top_y)
                cr.set_source_rgba(0.95, 0.98, 0.95, 0.28 * p.gloom)
                cr.rectangle(left_x, sy_line, right_x - left_x, max(1.5, scr_h_px * 0.04))
                cr.fill()

        # Expressive Glowing Phosphor Eyes inside CRT Screen
        eye_glow = _mix_rgb((0.70, 0.95, 0.42), (0.94, 0.98, 0.92), p.gloom * 0.5)
        eye_core = _mix_rgb((0.92, 0.99, 0.62), (0.98, 1.0, 0.98), p.gloom * 0.5)
        for side in (-1.0, 1.0):
            eu = -0.175 + side * 0.23 + p.look_x * 6.5
            ev = 0.06 - 0.07 * p.squint + p.look_y * 6.0
            ex, ey = front_pt(eu, ev, z_bump=0.06)
            squash = (1.0 - 0.88 * p.blink) * (1.0 - 0.56 * p.squint)
            widen = 1.0 + 0.08 * p.blink + 0.10 * p.squint
            open_amt = 1.0 - p.happy

            if open_amt > 0.02:
                rx = max(2.0, scr_w_px * 0.075 * p.eye_size * widen)
                ry = max(0.8, scr_h_px * 0.165 * p.eye_size * squash * open_amt)
                cr.save()
                cr.translate(ex, ey)
                cr.rotate(p.tilt + side * 0.04)
                # Phosphor outer glow
                cr.set_source_rgba(*eye_glow, 0.34 * open_amt)
                _round_rect(cr, -rx * 1.3, -ry * 1.2, rx * 2.6, ry * 2.4, rx * 1.1)
                cr.fill()
                # Crisp pill eye
                cr.set_source_rgba(*eye_core, 0.98 * open_amt)
                _round_rect(cr, -rx, -ry, rx * 2.0, ry * 2.0, rx * 0.9)
                cr.fill()
                # Cute dark pupil glint when startled at limit or big eyes
                if p.gloom > 0.15:
                    cr.set_source_rgba(0.06, 0.10, 0.10, 0.88 * p.gloom)
                    cr.arc(0.0, 0.0, max(1.5, rx * 0.42), 0.0, math.tau)
                    cr.fill()
                cr.restore()

            if p.happy > 0.02:
                # Arched happy CRT eyes (^^)
                ax, ay = front_pt(-0.175 + side * 0.23 + p.look_x * 3.0, 0.08, z_bump=0.06)
                ar = max(4.0, scr_w_px * 0.085)
                cr.save()
                cr.translate(ax, ay + ar * 0.25)
                cr.rotate(p.tilt)
                cr.set_source_rgba(*eye_core, p.happy)
                cr.set_line_width(max(2.2, scr_w_px * 0.036))
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.arc(0.0, 0.0, ar, math.pi * 1.12, math.pi * 1.88)
                cr.stroke()
                cr.restore()

        # Glossy curved CRT glass sheen in top-left corner
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.11)
        cr.move_to(tl_s[0] + scr_w_px * 0.08, tl_s[1] + scr_h_px * 0.06)
        cr.line_to(tl_s[0] + scr_w_px * 0.44, tl_s[1] + scr_h_px * 0.06)
        cr.line_to(tl_s[0] + scr_w_px * 0.18, tl_s[1] + scr_h_px * 0.34)
        cr.line_to(tl_s[0] + scr_w_px * 0.08, tl_s[1] + scr_h_px * 0.34)
        cr.close_path()
        cr.fill()
        cr.restore()

        # 5. Right Control Panel: Speaker Grille + Dual Rotary Channel/Volume Dials + Power LED
        # Slotted speaker louvers (top-right)
        for k in range(5):
            vy = 0.62 - k * 0.072
            p_a = front_pt(0.54, vy, z_bump=0.022)
            p_b = front_pt(0.80, vy, z_bump=0.022)
            cr.set_source_rgba(0.04, 0.05, 0.06, 0.92)
            cr.set_line_width(max(1.4, scale * 0.024))
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(*p_a)
            cr.line_to(*p_b)
            cr.stroke()

        # Upper VHF & lower UHF rotary dials
        dial_r = max(3.5, scale * 0.095)
        for idx_d, (dv, rot_speed) in enumerate([(0.06, 0.25), (-0.34, -0.15)]):
            dc = front_pt(0.67, dv, z_bump=0.04)
            # Outer brushed ring
            cr.set_source_rgba(0.28, 0.31, 0.36, 1.0)
            cr.arc(dc[0], dc[1], dial_r * 1.18, 0.0, math.tau)
            cr.fill()
            # Chrome dial face
            cr.set_source_rgba(0.76, 0.79, 0.83, 1.0)
            cr.arc(dc[0], dc[1], dial_r * 0.85, 0.0, math.tau)
            cr.fill()
            # Indicator notch
            ang = 0.8 + idx_d * 1.1 + (t * rot_speed if self.state == "working" else 0.0)
            cr.set_source_rgba(0.10, 0.12, 0.14, 0.95)
            cr.set_line_width(max(1.2, dial_r * 0.22))
            cr.move_to(dc[0], dc[1])
            cr.line_to(dc[0] + math.cos(ang) * dial_r * 0.72, dc[1] + math.sin(ang) * dial_r * 0.72)
            cr.stroke()

        # Tiny glowing power indicator LED below dials
        led = front_pt(0.67, -0.62, z_bump=0.028)
        led_r = max(1.8, scale * 0.028)
        if self.state == "limit":
            cr.set_source_rgba(0.96, 0.32, 0.26, 0.95)
        elif self.state in ("thinking", "working"):
            cr.set_source_rgba(0.98, 0.75, 0.24, 0.95)
        else:
            cr.set_source_rgba(0.38, 0.90, 0.56, 0.95)
        cr.arc(led[0], led[1], led_r, 0.0, math.tau)
        cr.fill()

        # 6. Working State Prop: Retro Horn-Rimmed Glasses sliding onto CRT eyes
        if p.glasses > 0.01:
            g_alpha = _clamp(p.glasses * 1.35, 0.0, 1.0)
            drop_v = (1.0 - p.glasses) * 0.48
            gx_l = front_pt(-0.405, 0.06 + drop_v, z_bump=0.11)
            gx_r = front_pt(0.055, 0.06 + drop_v, z_bump=0.11)
            bridge = front_pt(-0.175, 0.13 + drop_v, z_bump=0.12)
            lens_w = max(5.0, scr_w_px * 0.20)
            lens_h = max(4.0, scr_h_px * 0.22)
            cr.save()
            for lx, ly in (gx_l, gx_r):
                cr.save()
                cr.translate(lx, ly)
                cr.rotate(p.tilt)
                _round_rect(cr, -lens_w, -lens_h, lens_w * 2.0, lens_h * 2.0, lens_w * 0.38)
                cr.set_source_rgba(0.95, 0.99, 1.0, 0.16 * p.glasses)
                cr.fill_preserve()
                cr.set_source_rgba(0.07, 0.075, 0.085, g_alpha)
                cr.set_line_width(max(2.0, scale * 0.042))
                cr.stroke()
                cr.restore()
            # Nose bridge between lenses (cubic Bézier approximation of quadratic arch)
            cr.set_source_rgba(0.07, 0.075, 0.085, g_alpha)
            cr.set_line_width(max(2.0, scale * 0.04))
            p0x, p0y = gx_l[0] + lens_w * 0.92, gx_l[1] - lens_h * 0.18
            p2x, p2y = gx_r[0] - lens_w * 0.92, gx_r[1] - lens_h * 0.18
            ctrl_x, ctrl_y = bridge[0], bridge[1] - lens_h * 0.34
            cr.move_to(p0x, p0y)
            cr.curve_to(
                p0x + (2.0 / 3.0) * (ctrl_x - p0x),
                p0y + (2.0 / 3.0) * (ctrl_y - p0y),
                p2x + (2.0 / 3.0) * (ctrl_x - p2x),
                p2y + (2.0 / 3.0) * (ctrl_y - p2y),
                p2x,
                p2y,
            )
            cr.stroke()
            cr.restore()

        # 7. Thinking State Prop: Pearlescent Thought Bubbles rising top-right
        if p.think > 0.01:
            life = (t / 2.6) % 1.0
            bubbles_spec = (
                (0.78, 0.82, 0.25, 0.058),
                (0.94, 1.02, 0.22, 0.090),
                (1.02, 1.27, 0.14, 0.185),
            )
            for idx_b, (bx, by, bz, br) in enumerate(bubbles_spec):
                show = _smooth(0.04 + idx_b * 0.12, 0.14 + idx_b * 0.12, life) * (1.0 - _smooth(0.9, 0.98, life)) * p.think
                if show <= 0.01:
                    continue
                bscale = (0.5 + 0.5 * show) * min(1.0, show * 3.0)
                bp = self._proj(
                    (bx + p.x * 0.6, by + 0.02 * math.sin(t * 2.0 + idx_b), bz),
                    cx,
                    cy,
                    scale,
                )
                rad = max(2.0, scale * br * bscale)
                if idx_b < 2:
                    cr.set_source_rgba(0.97, 0.98, 1.0, show)
                    cr.arc(bp[0], bp[1], rad, 0.0, math.tau)
                    cr.fill()
                else:
                    # Fluffy 4-lobe cloud + 3 bouncing dots
                    lobes = (
                        (0.0, 0.0, rad),
                        (-rad * 0.86, rad * 0.26, rad * 0.72),
                        (rad * 0.86, rad * 0.22, rad * 0.72),
                        (rad * 0.16, -rad * 0.54, rad * 0.72),
                    )
                    cr.set_source_rgba(0.98, 0.99, 1.0, show)
                    for lx_off, ly_off, lr in lobes:
                        cr.arc(bp[0] + lx_off, bp[1] + ly_off, lr, 0.0, math.tau)
                        cr.fill()
                    dot_r = max(1.3, rad * 0.16)
                    for j, dx_off in enumerate((-rad * 0.45, 0.0, rad * 0.45)):
                        bounce = max(0.0, math.sin(t * 7.0 - j * 0.9)) * rad * 0.18
                        cr.set_source_rgba(0.07, 0.08, 0.09, show)
                        cr.arc(bp[0] + dx_off, bp[1] + rad * 0.05 - bounce, dot_r, 0.0, math.tau)
                        cr.fill()

        # 8. Limit State Prop: Icy-blue sweat drop sliding down side of cabinet
        if p.sweat > 0.02:
            slide = (t * 0.45) % 1.0
            sw_alpha = p.sweat * _smooth(0.0, 0.12, slide) * (1.0 - _smooth(0.8, 1.0, slide))
            if sw_alpha > 0.02:
                sp = self._proj(self._xform(hw + 0.06, 0.42 - slide * 0.24, 0.22, p), cx, cy, scale)
                sr = max(2.2, scale * 0.068 * p.sweat)
                cr.save()
                cr.translate(sp[0], sp[1])
                cr.set_source_rgba(0.75, 0.90, 1.0, 0.92 * sw_alpha)
                cr.move_to(0.0, -sr * 1.45)
                cr.curve_to(sr * 1.05, -sr * 0.15, sr * 1.05, sr * 0.95, 0.0, sr * 1.0)
                cr.curve_to(-sr * 1.05, sr * 0.95, -sr * 1.05, -sr * 0.15, 0.0, -sr * 1.45)
                cr.close_path()
                cr.fill()
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.65 * sw_alpha)
                cr.arc(-sr * 0.25, sr * 0.2, sr * 0.25, 0.0, math.tau)
                cr.fill()
                cr.restore()

        # 9. Active Flies in front of the TV cabinet
        for i, at, amount in sorted(active_flies, key=lambda item: item[1][2]):
            if at[2] >= 0.15:
                self._draw_fly(cr, cx, cy, scale, t, i, at, amount)


class TvMascotWidget(Gtk.Box):
    """Interactive 1980s CRT Television 'Vimona' mascot with liquid-glass status pill."""

    def __init__(
        self,
        size: int = 122,
        name: str = "Vimona",
        show_tag: bool = True,
        initial_state: TvMascotState = "idle",
        follow_pointer: bool = True,
        status_labels: dict[TvMascotState, str | None] | None = None,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        self._size = size
        self._name = name
        self._labels = dict(DEFAULT_STATUS)
        if status_labels:
            self._labels.update(status_labels)
        self._on_click = on_click
        self._tick_id: int | None = None
        self._last_t: float = 0.0

        self.sim = TvMascotSim(state=initial_state, follow_pointer=follow_pointer)

        overlay = Gtk.Overlay()
        self._canvas = Gtk.DrawingArea()
        self._canvas.set_size_request(size, size)
        if show_tag:
            self._canvas.set_margin_bottom(max(24, int(round(size * 0.20))))
        self._canvas.set_draw_func(self._on_draw)
        if on_click is not None:
            self._canvas.set_cursor_from_name("pointer")
        overlay.set_child(self._canvas)

        self._pill: Gtk.Box | None = None
        self._status_revealer: Gtk.Revealer | None = None
        self._status_lbl: Gtk.Label | None = None

        if show_tag:
            self._pill = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            self._pill.add_css_class("tv-mascot-pill")
            self._pill.set_halign(Gtk.Align.CENTER)
            self._pill.set_valign(Gtk.Align.END)
            if on_click is not None:
                self._pill.set_cursor_from_name("pointer")

            name_lbl = Gtk.Label(label=name)
            name_lbl.add_css_class("tv-mascot-name")
            name_lbl.set_halign(Gtk.Align.CENTER)
            self._pill.append(name_lbl)

            self._status_revealer = Gtk.Revealer()
            self._status_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
            self._status_revealer.set_transition_duration(220)
            self._status_lbl = Gtk.Label()
            self._status_lbl.add_css_class("tv-mascot-status")
            self._status_lbl.set_halign(Gtk.Align.CENTER)
            self._status_revealer.set_child(self._status_lbl)
            self._pill.append(self._status_revealer)

            overlay.add_overlay(self._pill)
            self._sync_pill(initial_state)

        self.append(overlay)

        if follow_pointer:
            motion = Gtk.EventControllerMotion()
            motion.connect("motion", self._on_local_motion)
            motion.connect("leave", lambda *_a: self.sim.clear_pointer())
            self.add_controller(motion)

        if on_click is not None:
            click = Gtk.GestureClick()
            click.connect("pressed", lambda *_a: on_click())
            self.add_controller(click)

        self._canvas.connect("realize", self._on_realize)
        self._canvas.connect("unrealize", self._on_unrealize)

    def _on_realize(self, _widget: Gtk.Widget) -> None:
        if self._tick_id is None:
            self._tick_id = self._canvas.add_tick_callback(self._on_tick)

    def _on_unrealize(self, _widget: Gtk.Widget) -> None:
        if self._tick_id is not None:
            self._canvas.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def _on_tick(self, _widget: Gtk.Widget, _clock: Gdk.FrameClock) -> bool:
        self._last_t = self.sim.step()
        self._canvas.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _on_draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        self.sim.draw(cr, w, h, self._last_t)

    def _on_local_motion(self, _ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        w = max(1.0, float(self.get_width()))
        h = max(1.0, float(self.get_height()))
        nx = ((x - w * 0.5) / w) * 1.6
        ny = ((h * 0.5 - y) / h) * 1.6
        self.sim.set_pointer(nx, ny)

    def set_global_pointer(self, root_x: float, root_y: float, root_widget: Gtk.Widget) -> None:
        res = root_widget.translate_coordinates(self._canvas, root_x, root_y)
        if res is None:
            return
        w = max(1.0, float(self._canvas.get_width()))
        h = max(1.0, float(self._canvas.get_height()))
        nx = ((res[0] - w * 0.5) / w) * 0.9
        ny = ((h * 0.5 - res[1]) / h) * 0.9
        self.sim.set_pointer(nx, ny)

    def set_state(self, state: TvMascotState, custom_status: str | None = None) -> None:
        self.sim.set_state(state)
        self._sync_pill(state, custom_status)

    def _sync_pill(self, state: TvMascotState, custom_status: str | None = None) -> None:
        if self._status_revealer is None or self._status_lbl is None:
            return
        text = custom_status if custom_status is not None else self._labels.get(state)
        if text:
            self._status_lbl.set_text(text)
            self._status_revealer.set_reveal_child(True)
        else:
            self._status_revealer.set_reveal_child(False)
        if self._pill is not None:
            for s_cls in ("thinking", "working", "limit"):
                self._pill.remove_css_class(s_cls)
            if state in ("thinking", "working", "limit"):
                self._pill.add_css_class(state)
