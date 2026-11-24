"""Explicit content detector and native GTK4 GPU `safe-blur` hover-popup wrapper."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Callable

import gi
import numpy as np

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import GdkPixbuf, GLib, Gtk

from .icons import SvgIcon
from .media import VideoFrameDecoder
from .safety_patterns import is_explicit_text
from .slide_commit import SlideCommitWidget
from .tokens import COLORS


@lru_cache(maxsize=256)
def _check_media_pixels_explicit(file_path_str: str) -> bool:
    """Fast cached YCbCr skin-exposure analyzer for images and video frame 0."""
    p = Path(file_path_str)
    if not p.is_file():
        return False
    arr: np.ndarray | None = None
    ext = p.suffix.lower()

    try:
        if ext in (".mp4", ".webm", ".mkv", ".mov", ".avi"):
            dec = VideoFrameDecoder(file_path_str)
            try:
                if dec.is_ready:
                    surf = dec.get_frame_surface(0)
                    if surf is not None:
                        w, h = surf.get_width(), surf.get_height()
                        raw = np.frombuffer(surf.get_data(), dtype=np.uint8).reshape((h, w, 4))
                        step_y = max(1, h // 64)
                        step_x = max(1, w // 64)
                        arr = raw[::step_y, ::step_x, 2::-1].astype(np.float32)
            finally:
                dec.close()
        else:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(file_path_str, 64, 64, False)
            if pb is not None:
                n_ch = pb.get_n_channels()
                w, h = pb.get_width(), pb.get_height()
                stride = pb.get_rowstride()
                pixels = np.frombuffer(pb.get_pixels(), dtype=np.uint8).reshape((h, stride))
                rgb = pixels[:, : w * n_ch].reshape((h, w, n_ch))[..., :3]
                arr = rgb.astype(np.float32)
    except Exception:
        return False

    if arr is None or arr.size == 0:
        return False

    h, w, _ = arr.shape
    y0, y1 = max(0, int(h * 0.08)), min(h, int(h * 0.92))
    x0, x1 = max(0, int(w * 0.08)), min(w, int(w * 0.92))
    core = arr[y0:y1, x0:x1]
    if core.size == 0:
        return False

    r, g, b = core[..., 0], core[..., 1], core[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b

    skin = (
        (y > 68.0)
        & (r > g)
        & ((r - b) > 18.0)
        & (cb >= 85.0)
        & (cb <= 124.0)
        & (cr >= 136.0)
        & (cr <= 168.0)
    )
    return float(np.mean(skin)) >= 0.52


def is_explicit_media(file_path: str | None, prompt_hint: str = "", title: str = "") -> bool:
    """Return True if an image or video is explicit by prompt/title context or visual exposure."""
    if is_explicit_text(prompt_hint) or is_explicit_text(title):
        return True
    if file_path:
        return _check_media_pixels_explicit(file_path)
    return False


class _BlurLayer(Gtk.Box):
    """Single-child container applying heavy multi-pass GPU blur + contrast dimming."""

    def __init__(self, child: Gtk.Widget, radius: float = 36.0, compact: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._radius = float(radius)
        self._compact = compact
        self._blurred = False
        self._child = child
        self.append(child)

    @property
    def blurred(self) -> bool:
        return self._blurred

    def set_blurred(self, blurred: bool) -> None:
        if self._blurred != blurred:
            self._blurred = blurred
            self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self._blurred and self._radius > 0.5:
            min_r = 24.0 if self._compact else 44.0
            eff_r = max(min_r, self._radius * 2.15)
            inner_opacity = 0.22 if self._compact else 0.26
            snapshot.push_blur(eff_r * 0.65)
            snapshot.push_blur(eff_r)
            snapshot.push_opacity(inner_opacity)
            self.snapshot_child(self._child, snapshot)
            snapshot.pop()
            snapshot.pop()
            snapshot.pop()
        else:
            self.snapshot_child(self._child, snapshot)


class SafeBlurBox(Gtk.Overlay):
    """Preview blur wrapper with `safe-blur` text and hover popup -> SlideCommit confirm."""

    def __init__(
        self,
        child: Gtk.Widget,
        explicit: bool = False,
        safe_blur_enabled: bool = True,
        blur_radius: float = 36.0,
        compact: bool = False,
        show_hover_toggle: bool = True,
        require_slide_confirm: bool = True,
        on_state_changed: Callable[[bool], None] | None = None,
        hover_position: Gtk.PositionType = Gtk.PositionType.TOP,
    ) -> None:
        super().__init__()
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._explicit = explicit
        self._global_enabled = safe_blur_enabled
        self._user_override: bool | None = None
        self._require_slide = require_slide_confirm
        self._on_state_changed = on_state_changed
        self._compact = compact
        self._hover_position = hover_position

        # Hover menu & confirmation slider popover state
        self._hover_target_bound = False
        self._widget_hovered = False
        self._pop_hovered = False
        self._open_timer: int | None = None
        self._close_timer: int | None = None
        self._hover_pop: Gtk.Popover | None = None
        self._confirm_pop: Gtk.Popover | None = None

        self._blur_layer = _BlurLayer(child, radius=blur_radius, compact=compact)
        self.set_child(self._blur_layer)

        self._scrim = Gtk.Box()
        self._scrim.add_css_class("safe-blur-scrim-compact" if compact else "safe-blur-scrim")
        self._scrim.set_can_target(False)
        self.add_overlay(self._scrim)

        self._badge = Gtk.Label(label="safe-blur")
        self._badge.add_css_class("safe-blur-label-compact" if compact else "safe-blur-label")
        self._badge.set_halign(Gtk.Align.CENTER)
        self._badge.set_valign(Gtk.Align.CENTER)
        self._badge.set_can_target(False)
        self.add_overlay(self._badge)

        if show_hover_toggle:
            self.bind_hover_target(self, position=hover_position)

        self.connect("unrealize", self._on_cleanup)
        self.connect("destroy", self._on_cleanup)
        self._sync()

    @property
    def is_blurred(self) -> bool:
        effective = self._user_override if self._user_override is not None else self._global_enabled
        return self._explicit and effective

    def set_explicit(self, explicit: bool) -> None:
        self._explicit = explicit
        if not explicit:
            self._close_hover_popover()
        self._sync()

    def set_safe_blur_enabled(self, enabled: bool) -> None:
        self._global_enabled = enabled
        self._user_override = None
        if not enabled:
            self._close_hover_popover()
        self._sync()

    def set_revealed_override(self, revealed: bool | None) -> None:
        self._user_override = (not revealed) if revealed is not None else None
        self._sync()

    def bind_hover_target(
        self,
        target: Gtk.Widget,
        position: Gtk.PositionType = Gtk.PositionType.TOP,
    ) -> None:
        """Bind CAPTURE-phase motion hover listener to show the popup menu reliably."""
        self._hover_position = position
        self._hover_anchor: Gtk.Widget = target
        if self._hover_target_bound:
            return
        self._hover_target_bound = True
        motion = Gtk.EventControllerMotion()
        motion.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        motion.connect("enter", lambda *_a: self._on_target_enter())
        motion.connect("motion", lambda *_a: self._on_target_enter())
        motion.connect("leave", lambda *_a: self._on_target_leave())
        target.add_controller(motion)

    def _should_show_hover_menu(self) -> bool:
        if not self._explicit:
            return False
        # Only expose individual menu while blurred or individually revealed
        return self.is_blurred or (self._user_override is False)

    def _on_target_enter(self) -> None:
        if not self._should_show_hover_menu():
            return
        self._widget_hovered = True
        self._cancel_close_timer()
        if self._confirm_pop is not None and self._confirm_pop.get_visible():
            return
        if self._hover_pop is not None and self._hover_pop.get_visible():
            return
        if self._open_timer is None:
            self._open_timer = GLib.timeout_add(120, self._open_hover_popover)

    def _on_target_leave(self) -> None:
        self._widget_hovered = False
        self._cancel_open_timer()
        self._schedule_close_check()

    def _cancel_open_timer(self) -> None:
        if self._open_timer is not None:
            GLib.source_remove(self._open_timer)
            self._open_timer = None

    def _cancel_close_timer(self) -> None:
        if self._close_timer is not None:
            GLib.source_remove(self._close_timer)
            self._close_timer = None

    def _schedule_close_check(self) -> None:
        self._cancel_close_timer()
        self._close_timer = GLib.timeout_add(380, self._maybe_close_popover)

    def _maybe_close_popover(self) -> bool:
        self._close_timer = None
        if self._widget_hovered or self._pop_hovered:
            return False
        self._close_hover_popover()
        return False

    def _close_hover_popover(self) -> None:
        self._cancel_open_timer()
        self._cancel_close_timer()
        self._pop_hovered = False
        if self._hover_pop is not None:
            try:
                self._hover_pop.popdown()
            except Exception:
                pass
            self._hover_pop = None

    def _close_confirm_popover(self) -> None:
        if self._confirm_pop is not None:
            try:
                self._confirm_pop.popdown()
            except Exception:
                pass
            self._confirm_pop = None

    def _open_confirm_popover(self) -> bool:
        """Open a fresh autohide-grabbed popover containing the SlideCommit slider."""
        if not self._explicit or not self.is_blurred:
            return False
        anchor = getattr(self, "_hover_anchor", self)
        if not anchor.get_mapped():
            return False
        self._close_confirm_popover()

        pop = Gtk.Popover()
        pop.set_has_arrow(True)
        pop.set_position(self._hover_position)
        pop.set_autohide(True)
        pop.set_parent(anchor)
        self._confirm_pop = pop

        confirm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        confirm_box.set_margin_top(6)
        confirm_box.set_margin_bottom(6)
        confirm_box.set_margin_start(6)
        confirm_box.set_margin_end(6)

        hdr = Gtk.Label(label="Confirm Safe-Blur Off?")
        hdr.add_css_class("research-perm-title")
        hdr.set_xalign(0.0)
        confirm_box.append(hdr)

        sub = Gtk.Label(label="Slide to reveal this content")
        sub.add_css_class("research-perm-note")
        sub.set_xalign(0.0)
        confirm_box.append(sub)

        def _on_slide_done() -> None:
            self._confirm_unblur()
            GLib.timeout_add(240, lambda: (self._close_confirm_popover(), False)[1])

        slider_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        slider = SlideCommitWidget(
            label="Slide to reveal",
            done_label="Revealed",
            error_label="Cancelled",
            on_confirm=_on_slide_done,
            track_color="#262626",
            handle_color="#f5f5f5",
            success_color="#22c55e",
            danger_color="#e5484d",
            width=216,
            height=40,
            radius=20,
            speed=50,
            return_bounce=0.38,
            landing_dip=0.026,
            hold_ms=0,
        )
        slider_row.append(slider)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.set_has_frame(False)
        cancel_btn.add_css_class("mini-action-btn")
        cancel_btn.set_cursor_from_name("pointer")
        cancel_btn.set_valign(Gtk.Align.CENTER)
        cancel_btn.connect("clicked", lambda _b: self._close_confirm_popover())
        slider_row.append(cancel_btn)
        confirm_box.append(slider_row)

        def _on_c_closed(p: Gtk.Popover) -> None:
            if self._confirm_pop is p:
                self._confirm_pop = None
            GLib.idle_add(lambda: (p.unparent() if p.get_parent() is not None else None, False)[1])

        pop.set_child(confirm_box)
        pop.connect("closed", _on_c_closed)
        pop.popup()
        return False

    def _open_hover_popover(self) -> bool:
        self._open_timer = None
        if not self._should_show_hover_menu():
            return False
        if self._confirm_pop is not None and self._confirm_pop.get_visible():
            return False
        anchor = getattr(self, "_hover_anchor", self)
        if not anchor.get_mapped():
            return False
        if self._hover_pop is not None and self._hover_pop.get_visible():
            return False

        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        pop.set_position(self._hover_position)
        pop.set_autohide(False)
        pop.set_parent(anchor)
        self._hover_pop = pop

        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        menu_box.set_margin_top(2)
        menu_box.set_margin_bottom(2)
        menu_box.set_margin_start(2)
        menu_box.set_margin_end(2)

        action_btn = Gtk.Button()
        action_btn.set_has_frame(False)
        action_btn.add_css_class("popover-item")
        action_btn.set_cursor_from_name("pointer")
        row_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        if self.is_blurred:
            row_inner.append(SvgIcon("eye", size_px=13, color=COLORS.text_primary))
            row_inner.append(Gtk.Label(label="Turn off Safe-Blur"))
        else:
            row_inner.append(SvgIcon("eye-off", size_px=13, color=COLORS.text_secondary))
            row_inner.append(Gtk.Label(label="Turn on Safe-Blur"))
        action_btn.set_child(row_inner)
        menu_box.append(action_btn)

        def _on_action_clicked(_b: Gtk.Button) -> None:
            currently_blurred = self.is_blurred
            self._close_hover_popover()
            if not currently_blurred:
                self._user_override = True
                self._sync()
                if self._on_state_changed is not None:
                    self._on_state_changed(True)
                return
            if self._require_slide:
                GLib.idle_add(self._open_confirm_popover)
            else:
                self._confirm_unblur()

        action_btn.connect("clicked", _on_action_clicked)

        pop_motion = Gtk.EventControllerMotion()
        pop_motion.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        pop_motion.connect("enter", lambda *_a: (setattr(self, "_pop_hovered", True), self._cancel_close_timer()))
        pop_motion.connect("motion", lambda *_a: (setattr(self, "_pop_hovered", True), self._cancel_close_timer()))
        pop_motion.connect("leave", lambda *_a: (setattr(self, "_pop_hovered", False), self._schedule_close_check()))
        menu_box.add_controller(pop_motion)

        def _on_pop_closed(p: Gtk.Popover) -> None:
            self._pop_hovered = False
            if self._hover_pop is p:
                self._hover_pop = None
            GLib.idle_add(lambda: (p.unparent() if p.get_parent() is not None else None, False)[1])

        pop.set_child(menu_box)
        pop.connect("closed", _on_pop_closed)
        pop.popup()
        return False

    def request_toggle(self, anchor: Gtk.Widget | None = None) -> None:
        if not self._explicit:
            return
        if anchor is not None:
            self._hover_anchor = anchor
        if self.is_blurred:
            if self._require_slide:
                self._close_hover_popover()
                GLib.idle_add(self._open_confirm_popover)
            else:
                self._confirm_unblur()
        else:
            self._user_override = True
            self._sync()
            if self._on_state_changed is not None:
                self._on_state_changed(True)

    def _confirm_unblur(self) -> None:
        self._user_override = False
        self._sync()
        if self._on_state_changed is not None:
            self._on_state_changed(False)

    def _on_cleanup(self, _w: Gtk.Widget) -> None:
        self._close_hover_popover()
        self._close_confirm_popover()

    def _sync(self) -> None:
        active = self.is_blurred
        self._blur_layer.set_blurred(active)
        self._scrim.set_visible(active)
        self._badge.set_visible(active)
