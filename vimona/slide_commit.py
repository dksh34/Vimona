"""Native GTK4/Cairo port of React Bits <SlideCommit /> slide-to-allow pill."""

from __future__ import annotations

import math
import time
from typing import Callable, Literal

import cairo
import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

SlidePhase = Literal["idle", "pending", "done", "error"]

_PAD = 4.0
_SQUASH_MAX = 0.08
_SQUASH_DIV = 110.0
_SWELL = 1.03
_SHAKE_KEYFRAMES = (0.0, -5.0, 5.0, -3.0, 3.0, -1.0, 0.0)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _hex_to_rgb(hex_str: str) -> tuple[float, float, float]:
    raw = hex_str.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) >= 6:
        try:
            r = int(raw[0:2], 16) / 255.0
            g = int(raw[2:4], 16) / 255.0
            b = int(raw[4:6], 16) / 255.0
            return (r, g, b)
        except ValueError:
            pass
    return (1.0, 1.0, 1.0)


def _on_color_rgb(hex_str: str) -> tuple[float, float, float]:
    r, g, b = _hex_to_rgb(hex_str)
    yiq = ((r * 255.0) * 299.0 + (g * 255.0) * 587.0 + (b * 255.0) * 114.0) / 1000.0
    if yiq >= 128.0:
        return (17 / 255.0, 17 / 255.0, 17 / 255.0)
    return (1.0, 1.0, 1.0)


def _mix_rgb(
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    t: float,
) -> tuple[float, float, float]:
    t = _clamp(t, 0.0, 1.0)
    return (c1[0] + (c2[0] - c1[0]) * t, c1[1] + (c2[1] - c1[1]) * t, c1[2] + (c2[2] - c1[2]) * t)


def _round_rect(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(max(r, 0.0), abs(w) * 0.5, abs(h) * 0.5)
    if r <= 0.1:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi * 0.5, 0.0)
    cr.arc(x + w - r, y + h - r, r, 0.0, math.pi * 0.5)
    cr.arc(x + r, y + h - r, r, math.pi * 0.5, math.pi)
    cr.arc(x + r, y + r, r, math.pi, math.pi * 1.5)
    cr.close_path()


class _SpringValue:
    """Analytical/semi-implicit spring motion value matching Framer Motion springs."""

    def __init__(self, initial: float = 0.0) -> None:
        self.value = initial
        self.velocity = 0.0
        self.target = initial
        self.active = False
        self.stiffness = 580.0
        self.damping = 45.0
        self.mass = 0.9

    def set_immediate(self, val: float) -> None:
        self.value = val
        self.target = val
        self.velocity = 0.0
        self.active = False

    def animate_to(
        self,
        target: float,
        stiffness: float,
        damping: float,
        mass: float = 0.9,
        velocity: float | None = None,
    ) -> None:
        self.target = target
        self.stiffness = stiffness
        self.damping = damping
        self.mass = max(0.1, mass)
        if velocity is not None:
            self.velocity = velocity
        self.active = True

    def step(self, dt: float) -> bool:
        if not self.active:
            return False
        substeps = 4
        h = min(dt, 0.05) / substeps
        for _ in range(substeps):
            disp = self.value - self.target
            accel = (-self.stiffness * disp - self.damping * self.velocity) / self.mass
            self.velocity += accel * h
            self.value += self.velocity * h
        if abs(self.value - self.target) < 0.05 and abs(self.velocity) < 1.0:
            self.value = self.target
            self.velocity = 0.0
            self.active = False
        return True


class SlideCommitWidget(Gtk.DrawingArea):
    """Interactive spring-physics 'Slide to allow' confirmation pill for GTK4."""

    def __init__(
        self,
        label: str = "Slide to allow",
        done_label: str = "Allowed",
        error_label: str = "Denied",
        on_confirm: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        track_color: str = "#262626",
        handle_color: str = "#f5f5f5",
        success_color: str = "#22c55e",
        danger_color: str = "#e5484d",
        width: int = 280,
        height: int = 52,
        radius: int = 26,
        speed: float = 50.0,
        return_bounce: float = 0.38,
        landing_dip: float = 0.026,
        hold_ms: int = 0,
        disabled: bool = False,
    ) -> None:
        super().__init__()
        self._label = label
        self._done_label = done_label
        self._error_label = error_label
        self._on_confirm = on_confirm
        self._on_done = on_done
        self._on_error = on_error

        self._w = float(width)
        self._h = float(height)
        self._r = _clamp(float(radius), 0.0, self._h * 0.5)
        self._grip = self._h - _PAD * 2.0
        self._inner = self._w - _PAD * 2.0
        self._travel = max(1.0, self._inner - self._grip)
        self._grip_r = max(0.0, self._r - _PAD)

        self._k = 260.0 + (_clamp(speed, 0.0, 100.0) / 100.0) * 640.0
        self._mass = 0.9
        self._critical = 2.0 * math.sqrt(self._k * self._mass)
        self._home_damping = self._critical * (1.0 - _clamp(return_bounce, 0.0, 0.5))
        self._landing_dip = max(0.0, landing_dip)
        self._hold_ms = max(0, hold_ms)
        self._disabled = disabled

        self._track_rgb = _hex_to_rgb(track_color)
        self._ink_rgb = _hex_to_rgb(handle_color)
        self._ok_rgb = _hex_to_rgb(success_color)
        self._no_rgb = _hex_to_rgb(danger_color)
        self._on_ink_rgb = _on_color_rgb(handle_color)
        self._on_ok_rgb = _on_color_rgb(success_color)
        self._on_no_rgb = _on_color_rgb(danger_color)

        self._phase: SlidePhase = "idle"
        self._held = False
        self._hot = False
        self._dragging = False
        self._grab_offset: float | None = None
        self._moved = False
        self._hist: list[tuple[float, float]] = []

        self._x = _SpringValue(0.0)
        self._anchor = _SpringValue(0.0)
        self._cap_rgb = self._ink_rgb
        self._fg_rgb = self._on_ink_rgb
        self._target_cap_rgb = self._ink_rgb
        self._target_fg_rgb = self._on_ink_rgb

        self._spin_phase = 0.0
        self._done_alpha = 0.0
        self._error_alpha = 0.0
        self._shown = 1.0

        self._pulse_t: float | None = None
        self._shake_t: float | None = None
        self._hold_timer: int | None = None
        self._home_timer: int | None = None

        self._last_step = time.monotonic()
        self._tick_id: int | None = None

        self.set_size_request(width, height)
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.set_focusable(not disabled)
        self.set_cursor_from_name("grab" if not disabled else "default")
        self.set_draw_func(self._on_draw)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_hover_enter)
        motion.connect("leave", self._on_hover_leave)
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        click.connect("cancel", self._on_cancelled)
        self.add_controller(click)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)

    @property
    def phase(self) -> SlidePhase:
        return self._phase

    def _ensure_tick(self) -> None:
        if self._tick_id is None and self.get_realized():
            self._last_step = time.monotonic()
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_realize(self, _widget: Gtk.Widget) -> None:
        self._ensure_tick()

    def _on_unrealize(self, _widget: Gtk.Widget) -> None:
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None
        if self._hold_timer is not None:
            GLib.source_remove(self._hold_timer)
            self._hold_timer = None
        if self._home_timer is not None:
            GLib.source_remove(self._home_timer)
            self._home_timer = None

    def set_disabled(self, disabled: bool) -> None:
        self._disabled = disabled
        self.set_focusable(not disabled)
        self.set_cursor_from_name("default" if disabled else "grab")
        self.queue_draw()

    def start_pending(self) -> None:
        """Transition into async spinner phase at full travel."""
        self._clear_timers()
        self._phase = "pending"
        self._x.set_immediate(self._travel)
        self._anchor.set_immediate(0.0)
        self._target_cap_rgb = self._ink_rgb
        self._target_fg_rgb = self._on_ink_rgb
        self.set_cursor_from_name("default")
        self._ensure_tick()
        self.queue_draw()

    def finish_done(self, done_label: str | None = None) -> None:
        """Unfurl done pill with green fill, checkmark, and landing dip."""
        self._clear_timers()
        if done_label is not None:
            self._done_label = done_label
        self._phase = "done"
        self._anchor.set_immediate(self._x.value)
        self._x.animate_to(0.0, self._k, self._critical, self._mass)
        self._target_cap_rgb = self._ok_rgb
        self._target_fg_rgb = self._on_ok_rgb
        if self._landing_dip > 0.0:
            self._pulse_t = 0.0
        self.set_cursor_from_name("default")
        self._ensure_tick()
        self.queue_draw()
        if self._on_done is not None:
            self._on_done()
        if self._hold_ms > 0:
            self._hold_timer = GLib.timeout_add(self._hold_ms, self._settle_idle)

    def fail_error(self, error_label: str | None = None) -> None:
        """Show error tint + horizontal shake and spring back home."""
        self._clear_timers()
        if error_label is not None:
            self._error_label = error_label
        self._phase = "error"
        self._target_cap_rgb = self._no_rgb
        self._target_fg_rgb = self._on_no_rgb
        self._shake_t = 0.0
        self.set_cursor_from_name("grab")
        self._ensure_tick()
        self.queue_draw()
        if self._on_error is not None:
            self._on_error(self._error_label)

        def _delay_home() -> bool:
            self._home_timer = None
            if not self._dragging:
                self._x.animate_to(0.0, self._k, self._home_damping, self._mass, velocity=0.0)
            return False

        def _reset_phase() -> bool:
            self._hold_timer = None
            self._phase = "idle"
            self._target_cap_rgb = self._ink_rgb
            self._target_fg_rgb = self._on_ink_rgb
            return False

        self._home_timer = GLib.timeout_add(280, _delay_home)
        self._hold_timer = GLib.timeout_add(max(self._hold_ms, 1500), _reset_phase)

    def _settle_idle(self) -> bool:
        self._hold_timer = None
        self._phase = "idle"
        self._anchor.animate_to(0.0, 320.0, 2.0 * math.sqrt(320.0 * 0.9), 0.9)
        self._target_cap_rgb = self._ink_rgb
        self._target_fg_rgb = self._on_ink_rgb
        self.set_cursor_from_name("grab" if not self._disabled else "default")
        return False

    def _clear_timers(self) -> None:
        if self._hold_timer is not None:
            GLib.source_remove(self._hold_timer)
            self._hold_timer = None
        if self._home_timer is not None:
            GLib.source_remove(self._home_timer)
            self._home_timer = None

    # ------------------------------------------------------------------ Input

    def _on_hover_enter(self, *_a: object) -> None:
        if not self._disabled:
            self._hot = True
            self.queue_draw()

    def _on_hover_leave(self, *_a: object) -> None:
        self._hot = False
        self.queue_draw()

    def _local_x(self, px: float) -> float:
        alloc_w = max(1.0, float(self.get_width()))
        return px / (alloc_w / self._w)

    def _on_pressed(self, _g: Gtk.GestureClick, _n: int, x: float, _y: float) -> None:
        if self._disabled or self._dragging or self._phase in ("pending", "done"):
            return
        self._x.active = False
        self._dragging = True
        self._held = True
        self._moved = False
        self._grab_offset = self._local_x(x) - self._x.value
        self._hist = [(time.monotonic(), self._x.value)]
        self.set_cursor_from_name("grabbing")
        self.grab_focus()
        self.queue_draw()

    def _on_motion(self, _ctrl: Gtk.EventControllerMotion, x: float, _y: float) -> None:
        if not self._dragging:
            return
        at = self._local_x(x)
        if self._grab_offset is None:
            self._grab_offset = at - self._x.value
            return
        nxt = _clamp(at - self._grab_offset, 0.0, self._travel)
        if abs(nxt - self._x.value) > 0.5:
            self._moved = True
        now = time.monotonic()
        self._hist.append((now, nxt))
        if len(self._hist) > 4:
            self._hist.pop(0)
        self._x.set_immediate(nxt)
        self.queue_draw()

    def _commit_drag(self) -> None:
        self._clear_timers()
        self._x.set_immediate(self._travel)
        if self._on_confirm is not None:
            self.start_pending()
            self._on_confirm()
        else:
            self.finish_done()

    def _on_released(self, *_a: object) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._held = False
        self._grab_offset = None
        self.set_cursor_from_name("grab" if not self._disabled else "default")
        if self._x.value >= self._travel - 2.0:
            self._commit_drag()
        elif self._moved:
            vel = 0.0
            if len(self._hist) >= 2:
                t0, x0 = self._hist[0]
                t1, x1 = self._hist[-1]
                vel = (x1 - x0) / max(0.004, t1 - t0)
            self._x.animate_to(0.0, self._k, self._home_damping, self._mass, velocity=min(0.0, vel))
            self._ensure_tick()
        self.queue_draw()

    def _on_cancelled(self, *_a: object) -> None:
        self._on_released()

    def _on_key_pressed(
        self, _ctrl: Gtk.EventControllerKey, keyval: int, _kc: int, _state: Gdk.ModifierType
    ) -> bool:
        if self._disabled or self._phase in ("pending", "done"):
            return False
        step = self._travel / 10.0
        if keyval == Gdk.KEY_End:
            self._commit_drag()
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Up):
            nxt = min(self._travel, self._x.value + step)
            self._x.set_immediate(nxt)
            if nxt >= self._travel:
                self._commit_drag()
            self.queue_draw()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Down):
            self._x.set_immediate(max(0.0, self._x.value - step))
            self.queue_draw()
            return True
        if keyval in (Gdk.KEY_Home, Gdk.KEY_Escape):
            self._x.animate_to(0.0, self._k, self._home_damping, self._mass, velocity=0.0)
            self._ensure_tick()
            return True
        return False

    # ------------------------------------------------------------------ Animation Tick & Render

    def _on_tick(self, _w: Gtk.Widget, _clock: Gdk.FrameClock) -> bool:
        now = time.monotonic()
        dt = min(0.05, max(0.001, now - self._last_step))
        self._last_step = now

        dirty = False
        if self._x.step(dt):
            dirty = True
        if self._anchor.step(dt):
            dirty = True

        color_rate = 1.0 - math.exp(-dt * 12.0)
        if any(abs(a - b) > 0.005 for a, b in zip(self._cap_rgb, self._target_cap_rgb)):
            self._cap_rgb = _mix_rgb(self._cap_rgb, self._target_cap_rgb, color_rate)
            dirty = True
        else:
            self._cap_rgb = self._target_cap_rgb

        if any(abs(a - b) > 0.005 for a, b in zip(self._fg_rgb, self._target_fg_rgb)):
            self._fg_rgb = _mix_rgb(self._fg_rgb, self._target_fg_rgb, color_rate)
            dirty = True
        else:
            self._fg_rgb = self._target_fg_rgb

        target_shown = 0.0 if self._phase in ("pending", "done") else 1.0
        if abs(self._shown - target_shown) > 0.005:
            self._shown += (target_shown - self._shown) * (1.0 - math.exp(-dt * 14.0))
            dirty = True
        else:
            self._shown = target_shown

        target_done = 1.0 if self._phase == "done" else 0.0
        if abs(self._done_alpha - target_done) > 0.005:
            self._done_alpha += (target_done - self._done_alpha) * (1.0 - math.exp(-dt * 14.0))
            dirty = True
        else:
            self._done_alpha = target_done

        target_err = 1.0 if self._phase == "error" else 0.0
        if abs(self._error_alpha - target_err) > 0.005:
            self._error_alpha += (target_err - self._error_alpha) * (1.0 - math.exp(-dt * 12.0))
            dirty = True
        else:
            self._error_alpha = target_err

        if self._phase == "pending":
            self._spin_phase = (self._spin_phase + dt * 5.8) % math.tau
            dirty = True

        if self._pulse_t is not None:
            self._pulse_t += dt / 0.46
            if self._pulse_t >= 1.0:
                self._pulse_t = None
            dirty = True

        if self._shake_t is not None:
            self._shake_t += dt / 0.45
            if self._shake_t >= 1.0:
                self._shake_t = None
            dirty = True

        if dirty:
            self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _on_draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        if w <= 2 or h <= 2:
            return
        cr.save()
        if self._disabled:
            cr.push_group()

        # Root track transforms (shake + landing dip pulse)
        shake_dx = 0.0
        if self._shake_t is not None:
            idx = self._shake_t * (len(_SHAKE_KEYFRAMES) - 1)
            i0 = min(len(_SHAKE_KEYFRAMES) - 2, int(idx))
            frac = idx - i0
            shake_dx = _SHAKE_KEYFRAMES[i0] + (_SHAKE_KEYFRAMES[i0 + 1] - _SHAKE_KEYFRAMES[i0]) * frac

        pulse_scale = 1.0
        if self._pulse_t is not None:
            pt = self._pulse_t
            if pt < 0.62:
                pulse_scale = 1.0 - self._landing_dip * math.sin((pt / 0.62) * (math.pi * 0.5))
            else:
                pulse_scale = 1.0 - self._landing_dip * math.cos(((pt - 0.62) / 0.38) * (math.pi * 0.5))

        cr.translate(w * 0.5 + shake_dx, h * 0.5)
        cr.scale(pulse_scale, pulse_scale)
        cr.translate(-self._w * 0.5, -self._h * 0.5)

        # 1. Track pill background
        cr.set_source_rgba(*self._track_rgb, 1.0)
        _round_rect(cr, 0.0, 0.0, self._w, self._h, self._r)
        cr.fill_preserve()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.08)
        cr.set_line_width(1.0)
        cr.stroke()

        seen = _clamp(self._x.value, 0.0, self._travel)
        say_alpha = _clamp(1.0 - (seen / max(1.0, self._travel * 0.55)), 0.0, 1.0)
        font_sz = _clamp(round(self._h * 0.27), 12, 16)

        # 2. Instruction / error label centered inside track
        if say_alpha > 0.01:
            txt_plain = self._label
            txt_err = self._error_label
            if self._error_alpha < 0.98:
                self._draw_centered_text(
                    cr,
                    txt_plain,
                    self._w * 0.5 + 10.0,
                    self._h * 0.5,
                    font_sz,
                    500,
                    (*self._ink_rgb, 0.52 * say_alpha * (1.0 - self._error_alpha)),
                )
            if self._error_alpha > 0.02:
                self._draw_centered_text(
                    cr,
                    txt_err,
                    self._w * 0.5 + 10.0,
                    self._h * 0.5,
                    font_sz,
                    500,
                    (*self._no_rgb, say_alpha * self._error_alpha),
                )

        # 3. Extending & squashing capsule handle
        edge = seen + self._grip + _clamp(self._anchor.value - seen, 0.0, self._travel)
        swell = _SWELL if (self._hot and not self._held and self._phase == "idle") else 1.0
        q = 1.0 - min(_SQUASH_MAX, max(0.0, -self._x.value) / _SQUASH_DIV)
        sx = q * swell
        sy = swell / max(0.5, q)

        cr.save()
        ox = _PAD + seen
        oy = _PAD + self._grip * 0.5
        cr.translate(ox, oy)
        cr.scale(sx, sy)
        cr.translate(-ox, -oy)

        _round_rect(cr, _PAD, _PAD, max(self._grip, edge), self._grip, self._grip_r)
        cr.set_source_rgba(*self._cap_rgb, 1.0)
        cr.fill()

        # Center point of active capsule content: _PAD + (seen + edge) * 0.5
        cx_c = _PAD + (seen + edge) * 0.5
        cy_c = _PAD + self._grip * 0.5

        # Arrow icon (fades out at end of travel)
        arrow_alpha = self._shown * _clamp(1.0 - (seen - self._travel * 0.55) / max(1.0, self._travel * 0.4), 0.0, 1.0)
        if arrow_alpha > 0.02:
            self._draw_arrow(cr, cx_c, cy_c, self._grip * 0.22, (*self._fg_rgb, arrow_alpha))

        # Pending rotating spinner
        if self._phase == "pending":
            sr = self._grip * 0.20
            cr.set_source_rgba(*self._fg_rgb, 0.25)
            cr.set_line_width(2.2)
            cr.arc(cx_c, cy_c, sr, 0.0, math.tau)
            cr.stroke()
            cr.set_source_rgba(*self._fg_rgb, 0.96)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.arc(cx_c, cy_c, sr, self._spin_phase, self._spin_phase + math.pi * 0.65)
            cr.stroke()

        # Done checkmark + doneLabel
        if self._done_alpha > 0.02:
            layout = self.create_pango_layout(self._done_label)
            desc = Pango.FontDescription.from_string(f"Inter Semi-Bold {font_sz}px")
            layout.set_font_description(desc)
            lw, lh = layout.get_pixel_size()
            icon_w = self._grip * 0.34
            total_w = icon_w + 7.0 + lw
            start_x = cx_c - total_w * 0.5
            self._draw_check(cr, start_x + icon_w * 0.5, cy_c, icon_w * 0.5, (*self._fg_rgb, self._done_alpha))
            cr.set_source_rgba(*self._fg_rgb, self._done_alpha)
            cr.move_to(start_x + icon_w + 7.0, cy_c - lh * 0.5)
            PangoCairo.show_layout(cr, layout)

        cr.restore()

        if self._disabled:
            cr.pop_group_to_source()
            cr.paint_with_alpha(0.55)
        cr.restore()

    def _draw_centered_text(
        self,
        cr: cairo.Context,
        text: str,
        cx: float,
        cy: float,
        font_sz: int,
        weight: int,
        rgba: tuple[float, float, float, float],
    ) -> None:
        if rgba[3] <= 0.01:
            return
        layout = self.create_pango_layout(text)
        desc = Pango.FontDescription.from_string(f"Inter {weight} {font_sz}px")
        layout.set_font_description(desc)
        lw, lh = layout.get_pixel_size()
        cr.set_source_rgba(*rgba)
        cr.move_to(cx - lw * 0.5, cy - lh * 0.5)
        PangoCairo.show_layout(cr, layout)

    @staticmethod
    def _draw_arrow(
        cr: cairo.Context, cx: float, cy: float, r: float, rgba: tuple[float, float, float, float]
    ) -> None:
        cr.save()
        cr.set_source_rgba(*rgba)
        cr.set_line_width(2.0)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.move_to(cx - r * 0.85, cy)
        cr.line_to(cx + r * 0.85, cy)
        cr.stroke()
        cr.move_to(cx + r * 0.25, cy - r * 0.58)
        cr.line_to(cx + r * 0.85, cy)
        cr.line_to(cx + r * 0.25, cy + r * 0.58)
        cr.stroke()
        cr.restore()

    @staticmethod
    def _draw_check(
        cr: cairo.Context, cx: float, cy: float, r: float, rgba: tuple[float, float, float, float]
    ) -> None:
        cr.save()
        cr.set_source_rgba(*rgba)
        cr.set_line_width(2.3)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.move_to(cx - r * 0.72, cy + r * 0.05)
        cr.line_to(cx - r * 0.14, cy + r * 0.62)
        cr.line_to(cx + r * 0.82, cy - r * 0.52)
        cr.stroke()
        cr.restore()
