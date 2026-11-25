"""Hand-crafted GTK4 custom widgets for Vimona."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import math
from pathlib import Path
import re
import threading
import time
from typing import Callable
import urllib.parse

import cairo
import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk, Pango

from .aquarium import AquariumSimulation
from .crt_warp import CRTWarpWidget
from .electric_border import ElectricBorderOverlay
from .icons import SvgIcon
from .safety_blur import SafeBlurBox, _BlurLayer, is_explicit_media, is_explicit_text
from .slide_commit import SlideCommitWidget
from .text_loop import TextLoopWidget
from .tv_mascot import TV_MASCOT_STATES, TvMascotState, TvMascotWidget
from .media import (
    VideoFrameDecoder,
    draw_neutral_frame,
    load_image_surface,
    load_image_texture,
    pixbuf_to_cairo_surface,
)
from .models import (
    AVAILABLE_SKILLS,
    KeyframeSpec,
    MemoryHandoffData,
    PromptSpec,
    ProseBlock,
    ReasoningStep,
    ReferenceCandidate,
    RenderPlaylistJob,
    ResearchStepData,
    ResearchTab,
    SystemMode,
    VideoClipSpec,
)
from .perf_profile import get_footer_telemetry_label
from .tokens import COLORS


def copy_to_clipboard(widget: Gtk.Widget, text: str) -> None:
    """Copy plain text to the Wayland/X11 system clipboard."""
    display = widget.get_display()
    if display is None:
        return
    display.get_clipboard().set(text)


# ---------------------------------------------------------------------------
# Top Bar Status Pill
# ---------------------------------------------------------------------------

class SystemStatePill(Gtk.Box):
    """Calm status pill with animated indicator dot when active."""

    def __init__(self, on_click: Callable[[], None] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.add_css_class("state-pill")
        self.set_valign(Gtk.Align.CENTER)
        if on_click is not None:
            self.set_cursor_from_name("pointer")
            self.set_tooltip_text("Click to check which installed apps use RAM")
            click = Gtk.GestureClick()
            click.connect("released", lambda *_a: on_click())
            self.add_controller(click)

        self._mode: SystemMode = "idle"
        self._last_text: str = "Ready"
        self._phase: float = 0.0
        self._tick_id: int | None = None

        self._dot = Gtk.DrawingArea()
        self._dot.set_size_request(10, 10)
        self._dot.set_valign(Gtk.Align.CENTER)
        self._dot.set_draw_func(self._draw_dot)
        self.append(self._dot)

        self._label = Gtk.Label(label="Ready")
        self._label.set_valign(Gtk.Align.CENTER)
        self.append(self._label)

        self.connect("unrealize", self._on_unrealize)

    def set_state(self, mode: SystemMode, text: str) -> None:
        if self._mode == mode and self._last_text == text:
            return
        self._mode = mode
        self._last_text = text
        self._label.set_text(text)
        for cls in ("active-chat", "research", "handoff", "completed"):
            self.remove_css_class(cls)
        if mode == "chat":
            self.add_css_class("active-chat")
        elif mode == "research":
            self.add_css_class("research")
        elif mode == "handoff":
            self.add_css_class("handoff")
        elif mode == "completed":
            self.add_css_class("completed")

        should_animate = mode in ("chat", "research", "handoff")
        self._dot.set_visible(should_animate)
        if should_animate and self._tick_id is None:
            self._tick_id = self.add_tick_callback(self._on_tick)
        elif not should_animate and self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None
        self._dot.queue_draw()

    def _on_tick(self, _widget: Gtk.Widget, _frame_clock: Gdk.FrameClock) -> bool:
        self._phase = (self._phase + 0.06) % math.tau
        self._dot.queue_draw()
        return True

    def _on_unrealize(self, _widget: Gtk.Widget) -> None:
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def _draw_dot(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        cx, cy = w * 0.5, h * 0.5
        if self._mode == "handoff":
            rgb = (0.788, 0.659, 0.459)
        elif self._mode == "research":
            rgb = (0.659, 0.710, 0.784)
        elif self._mode == "chat":
            rgb = (0.929, 0.929, 0.929)
        else:
            rgb = (0.45, 0.45, 0.45)

        if self._mode in ("chat", "research", "handoff"):
            pulse = 0.5 + 0.5 * math.sin(self._phase)
            cr.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.16 + 0.22 * pulse)
            cr.arc(cx, cy, 4.6, 0, math.tau)
            cr.fill()

        cr.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.95)
        cr.arc(cx, cy, 2.7, 0, math.tau)
        cr.fill()


# ---------------------------------------------------------------------------
# User Message Pill
# ---------------------------------------------------------------------------

class UserMessagePill(Gtk.Box):
    """Right-aligned rounded pill with Edit, Copy, Quote, Delete-to-empty-textbox, and Safe-Blur."""

    def __init__(
        self,
        text: str,
        on_toast: Callable[[str], None],
        on_edited: Callable[[str], None] | None = None,
        on_deleted: Callable[[], None] | None = None,
        on_quote: Callable[[str], None] | None = None,
        safe_blur_enabled: bool = True,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.add_css_class("user-message-wrapper")
        self.set_halign(Gtk.Align.END)
        self.set_hexpand(True)

        self._text = text
        self._on_toast = on_toast
        self._on_edited = on_edited
        self._on_deleted = on_deleted
        self._on_quote = on_quote
        self._safe_blur_enabled = safe_blur_enabled
        self._editing = False

        self._pill = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._pill.add_css_class("user-pill")
        self._pill.set_halign(Gtk.Align.END)

        self._label = Gtk.Label(label=text)
        self._label.add_css_class("user-pill-text")
        self._label.set_wrap(True)
        self._label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._label.set_max_width_chars(62)
        self._label.set_xalign(0.0)
        self._label.set_selectable(True)

        self._blur_box = SafeBlurBox(
            self._label,
            explicit=is_explicit_text(text),
            safe_blur_enabled=safe_blur_enabled,
            blur_radius=36.0,
        )
        self._pill.append(self._blur_box)

        self._empty_entry = Gtk.Entry()
        self._empty_entry.add_css_class("empty-prompt-entry")
        self._empty_entry.set_text("")
        self._empty_entry.set_tooltip_text("Deleted prompt (type text and press Enter to restore)")
        self._empty_entry.connect("activate", self._on_empty_entry_activate)
        self._empty_entry.set_visible(False)
        self._pill.append(self._empty_entry)

        self._edit_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._edit_box.set_visible(False)
        self._edit_view = Gtk.TextView()
        self._edit_view.add_css_class("composer-textview")
        self._edit_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._edit_view.set_size_request(360, 56)
        self._edit_box.append(self._edit_view)

        edit_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        edit_btns.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.set_has_frame(False)
        cancel_btn.add_css_class("mini-action-btn")
        cancel_btn.set_cursor_from_name("pointer")
        cancel_btn.connect("clicked", lambda _b: self._exit_edit(save=False))
        save_btn = Gtk.Button(label="Save")
        save_btn.set_has_frame(False)
        save_btn.add_css_class("mini-action-btn")
        save_btn.set_cursor_from_name("pointer")
        save_btn.connect("clicked", lambda _b: self._exit_edit(save=True))
        edit_btns.append(cancel_btn)
        edit_btns.append(save_btn)
        self._edit_box.append(edit_btns)
        self._pill.append(self._edit_box)

        self.append(self._pill)

        self._action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._action_bar.add_css_class("message-action-bar")
        self._action_bar.set_halign(Gtk.Align.END)

        self._copy_btn = self._make_action_btn("copy", "Copy")
        self._copy_btn.connect("clicked", self._on_copy)
        self._edit_btn = self._make_action_btn("edit", "Edit")
        self._edit_btn.connect("clicked", lambda _b: self._enter_edit())
        self._del_btn = self._make_action_btn("trash", "Delete")
        self._del_btn.connect("clicked", self._on_delete_prompt_clicked)
        if self._on_quote is not None:
            q_btn = Gtk.Button(label="↳ Quote")
            q_btn.set_has_frame(False)
            q_btn.add_css_class("mini-action-btn")
            q_btn.set_cursor_from_name("pointer")
            q_btn.connect("clicked", lambda _b: self._on_quote and self._text and self._on_quote(self._text))
            self._action_bar.append(q_btn)
        self._action_bar.append(self._edit_btn)
        self._action_bar.append(self._copy_btn)
        self._action_bar.append(self._del_btn)
        self.append(self._action_bar)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: self._action_bar.add_css_class("visible"))
        motion.connect("leave", lambda *_a: self._action_bar.remove_css_class("visible"))
        self.add_controller(motion)

        if not text:
            self._apply_deleted_empty_state()

    def set_safe_blur_enabled(self, enabled: bool) -> None:
        self._safe_blur_enabled = enabled
        self._blur_box.set_safe_blur_enabled(enabled)

    def _apply_deleted_empty_state(self) -> None:
        self._text = ""
        self._label.set_text("")
        self._blur_box.set_explicit(False)
        self._blur_box.set_visible(False)
        self._empty_entry.set_text("")
        self._empty_entry.set_visible(True)
        self._pill.add_css_class("deleted-empty")
        self._copy_btn.set_visible(False)
        self._del_btn.set_visible(False)

    def _on_empty_entry_activate(self, entry: Gtk.Entry) -> None:
        restored = entry.get_text().strip()
        if not restored:
            return
        self._text = restored
        self._label.set_text(restored)
        self._blur_box.set_explicit(is_explicit_text(restored))
        self._blur_box.set_safe_blur_enabled(self._safe_blur_enabled)
        self._empty_entry.set_visible(False)
        self._pill.remove_css_class("deleted-empty")
        self._blur_box.set_visible(True)
        self._copy_btn.set_visible(True)
        self._del_btn.set_visible(True)
        if self._on_edited is not None:
            self._on_edited(restored)

    def _on_delete_prompt_clicked(self, _btn: Gtk.Button) -> None:
        self._apply_deleted_empty_state()
        if self._on_deleted is not None:
            self._on_deleted()

    def _make_action_btn(self, icon_name: str, label: str) -> Gtk.Button:
        btn = Gtk.Button()
        btn.set_has_frame(False)
        btn.add_css_class("mini-action-btn")
        btn.set_cursor_from_name("pointer")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        box.append(SvgIcon(icon_name, size_px=12, color=COLORS.text_secondary))
        box.append(Gtk.Label(label=label))
        btn.set_child(box)
        return btn

    def _on_copy(self, _btn: Gtk.Button) -> None:
        copy_to_clipboard(self, self._text)
        self._on_toast("Message copied")
        box = self._copy_btn.get_child()
        if isinstance(box, Gtk.Box):
            first = box.get_first_child()
            if isinstance(first, SvgIcon):
                first.set_icon("check", COLORS.accent_sage)
                GLib.timeout_add(1400, lambda: (first.set_icon("copy", COLORS.text_secondary), False)[1])

    def _enter_edit(self) -> None:
        if self._editing:
            return
        self._editing = True
        self._edit_view.get_buffer().set_text(self._text)
        self._blur_box.set_visible(False)
        self._empty_entry.set_visible(False)
        self._edit_box.set_visible(True)
        self._edit_view.grab_focus()

    def _exit_edit(self, save: bool) -> None:
        if save:
            buf = self._edit_view.get_buffer()
            start, end = buf.get_bounds()
            new_text = buf.get_text(start, end, False).strip()
            if new_text:
                self._text = new_text
                self._label.set_text(new_text)
                self._blur_box.set_explicit(is_explicit_text(new_text))
                self._blur_box.set_safe_blur_enabled(self._safe_blur_enabled)
                self._pill.remove_css_class("deleted-empty")
                self._copy_btn.set_visible(True)
                self._del_btn.set_visible(True)
                if self._on_edited is not None:
                    self._on_edited(new_text)
                self._on_toast("Message updated")
        self._editing = False
        self._edit_box.set_visible(False)
        if self._text:
            self._blur_box.set_visible(True)
        else:
            self._empty_entry.set_visible(True)


# ---------------------------------------------------------------------------
# Collapsible Reasoning Accordion
# ---------------------------------------------------------------------------

class ReasoningAccordion(Gtk.Box):
    """Quiet collapsible explanation with continuous selectable text & quote support."""

    def __init__(
        self,
        step: ReasoningStep,
        expanded: bool = False,
        on_toast: Callable[[str], None] | None = None,
        on_quote: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("reasoning-accordion")
        self._expanded = expanded
        self._on_toast = on_toast
        self._on_quote = on_quote

        toggle = Gtk.Button()
        toggle.set_has_frame(False)
        toggle.add_css_class("reasoning-toggle")
        toggle.set_cursor_from_name("pointer")
        toggle.connect("clicked", self._toggle)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._chevron = SvgIcon(
            "chevron-down" if expanded else "chevron-right",
            size_px=13,
            color=COLORS.text_muted,
        )
        row.append(self._chevron)

        title_lbl = Gtk.Label(label="How I planned this")
        title_lbl.set_halign(Gtk.Align.START)
        row.append(title_lbl)

        skill_tag = Gtk.Label(label=step.skill)
        skill_tag.add_css_class("reasoning-skill-tag")
        row.append(skill_tag)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)

        dur = Gtk.Label(label=step.duration)
        dur.add_css_class("handoff-mono")
        row.append(dur)

        toggle.set_child(row)
        self.append(toggle)

        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_transition_duration(210)
        self._revealer.set_reveal_child(expanded)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        body.add_css_class("reasoning-body")

        tv_mini = TvMascotWidget(
            size=76,
            name="Vimona",
            show_tag=True,
            initial_state="thinking",
            follow_pointer=False,
            status_labels={"thinking": "Thinking…"},
        )
        tv_mini.set_valign(Gtk.Align.START)
        body.append(tv_mini)

        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        text_col.set_hexpand(True)

        tv = Gtk.TextView()
        tv.add_css_class("prose-textview")
        tv.set_editable(False)
        tv.set_cursor_visible(False)
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.set_hexpand(True)
        rbuf = tv.get_buffer()
        r_lines = [step.summary] + [f"• {b}" for b in step.bullets]
        rbuf.set_text("\n".join(ln for ln in r_lines if ln))
        text_col.append(tv)

        r_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        rcopy = Gtk.Button(label="Copy reasoning")
        rcopy.set_has_frame(False)
        rcopy.add_css_class("mini-action-btn")
        rcopy.set_cursor_from_name("pointer")
        rcopy.connect(
            "clicked",
            lambda _b: (
                copy_to_clipboard(self, rbuf.get_text(rbuf.get_start_iter(), rbuf.get_end_iter(), False)),
                self._on_toast("Reasoning copied") if self._on_toast else None,
            ),
        )
        r_bar.append(rcopy)
        if self._on_quote is not None:
            rquote = Gtk.Button(label="↳ Quote selection")
            rquote.set_has_frame(False)
            rquote.add_css_class("mini-action-btn")
            rquote.set_cursor_from_name("pointer")

            def _q_reason(_b: Gtk.Button) -> None:
                bnds = rbuf.get_selection_bounds()
                txt = (
                    rbuf.get_text(bnds[0], bnds[1], False).strip()
                    if bnds
                    else rbuf.get_text(rbuf.get_start_iter(), rbuf.get_end_iter(), False).strip()
                )
                if txt and self._on_quote:
                    self._on_quote(txt)

            rquote.connect("clicked", _q_reason)
            r_bar.append(rquote)
        text_col.append(r_bar)

        body.append(text_col)
        self._revealer.set_child(body)
        self.append(self._revealer)

    def _toggle(self, _btn: Gtk.Button) -> None:
        self._expanded = not self._expanded
        self._revealer.set_reveal_child(self._expanded)
        self._chevron.set_icon("chevron-down" if self._expanded else "chevron-right", COLORS.text_muted)


class ThinkingLiveCard(Gtk.Box):
    """Active thinking indicator in the stream featuring the 80s CRT Vimona mascot."""

    def __init__(self, explicit_hint: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.add_css_class("thinking-live-card")
        self.set_valign(Gtk.Align.START)
        self._explicit_hint = explicit_hint

        self.mascot = TvMascotWidget(
            size=92,
            name="Vimona",
            show_tag=True,
            initial_state="thinking",
            follow_pointer=True,
        )
        self.append(self.mascot)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        col.set_valign(Gtk.Align.CENTER)
        col.set_hexpand(True)

        self._title = Gtk.Label(label="Vimona is thinking…")
        self._title.add_css_class("thinking-live-title")
        self._title.set_xalign(0.0)
        col.append(self._title)

        self._detail = Gtk.Label(label="Running local uncensored inference on light-uncensored.gguf")
        self._detail.add_css_class("thinking-live-sub")
        self._detail.set_xalign(0.0)
        self._detail.set_wrap(True)
        col.append(self._detail)

        self._trace_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._trace_box.add_css_class("thinking-trace-box")
        self._trace_box.set_visible(False)

        tr_lbl = Gtk.Label(label="Live Thinking")
        tr_lbl.add_css_class("research-tab-url")
        tr_lbl.set_xalign(0.0)
        self._trace_box.append(tr_lbl)

        self._trace_scroll = Gtk.ScrolledWindow()
        self._trace_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._trace_scroll.set_propagate_natural_height(True)
        self._trace_scroll.set_min_content_height(38)
        self._trace_scroll.set_max_content_height(145)

        self._trace_tv = Gtk.TextView()
        self._trace_tv.add_css_class("thinking-trace-tv")
        self._trace_tv.set_editable(False)
        self._trace_tv.set_cursor_visible(False)
        self._trace_tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._trace_scroll.set_child(self._trace_tv)
        self._trace_blur = SafeBlurBox(
            self._trace_scroll,
            explicit=explicit_hint,
            safe_blur_enabled=True,
            blur_radius=32.0,
        )
        self._trace_box.append(self._trace_blur)
        col.append(self._trace_box)

        self.has_live_trace = False
        self.append(col)

    def set_phase(self, state: TvMascotState, title: str, detail: str, badge: str | None = None) -> None:
        self.mascot.set_state(state, custom_status=badge)
        self._title.set_text(title)
        self._detail.set_text(detail)

    def set_thinking_trace(self, reasoning: str, content_preview: str = "") -> None:
        txt = (reasoning or "").strip()
        if not txt and content_preview:
            txt = content_preview.strip()
        if not txt:
            return
        self.has_live_trace = True
        self._title.set_text("Vimona is thinking…")
        self._detail.set_text("Streaming reasoning live:")
        self.mascot.set_state("thinking", custom_status="Live")
        buf = self._trace_tv.get_buffer()
        buf.set_text(txt)
        if hasattr(self, "_trace_blur"):
            self._trace_blur.set_explicit(is_explicit_text(txt))
        self._trace_box.set_visible(True)
        adj = self._trace_scroll.get_vadjustment()
        GLib.idle_add(lambda: (adj.set_value(max(0.0, adj.get_upper() - adj.get_page_size())), False)[1])


# ---------------------------------------------------------------------------
# Prompt Summary Block
# ---------------------------------------------------------------------------

class PromptBreakdownBlock(Gtk.Box):
    """Structured prompt & camera summary with continuous multi-field text selection."""

    def __init__(
        self,
        spec: PromptSpec,
        on_toast: Callable[[str], None],
        on_quote: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("prompt-block")
        self._spec = spec
        self._on_toast = on_toast
        self._on_quote = on_quote

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("prompt-block-header")

        title = Gtk.Label(label=spec.title)
        title.add_css_class("prompt-block-label")
        title.set_xalign(0.0)
        title.set_hexpand(True)
        header.append(title)

        if self._on_quote is not None:
            q_btn = Gtk.Button(label="↳ Ask / Tweak")
            q_btn.set_has_frame(False)
            q_btn.add_css_class("prompt-copy-btn")
            q_btn.set_cursor_from_name("pointer")
            header.append(q_btn)
        else:
            q_btn = None

        self._copy_btn = Gtk.Button()
        self._copy_btn.set_has_frame(False)
        self._copy_btn.add_css_class("prompt-copy-btn")
        self._copy_btn.set_cursor_from_name("pointer")
        self._copy_btn.connect("clicked", self._on_copy)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self._copy_icon = SvgIcon("copy", size_px=12, color=COLORS.text_secondary)
        self._copy_lbl = Gtk.Label(label="Copy prompt")
        btn_box.append(self._copy_icon)
        btn_box.append(self._copy_lbl)
        self._copy_btn.set_child(btn_box)
        header.append(self._copy_btn)
        self.append(header)

        tv = Gtk.TextView()
        tv.add_css_class("prose-textview")
        tv.set_editable(False)
        tv.set_cursor_visible(False)
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.set_margin_top(10)
        tv.set_margin_bottom(12)
        tv.set_margin_start(12)
        tv.set_margin_end(12)
        pbuf = tv.get_buffer()
        key_tag = pbuf.create_tag("pkey", weight=Pango.Weight.SEMIBOLD, foreground="#94A3B8")
        val_tag = pbuf.create_tag("pval", foreground="#E5E7EB")
        for idx, (key, val) in enumerate(spec.tokens):
            if idx > 0:
                pbuf.insert(pbuf.get_end_iter(), "\n")
            pbuf.insert_with_tags(pbuf.get_end_iter(), f"{key}: ", key_tag)
            pbuf.insert_with_tags(pbuf.get_end_iter(), val, val_tag)
        self._tv = tv
        self.append(tv)

        if q_btn is not None:
            def _quote_prompt(_b: Gtk.Button) -> None:
                bnds = pbuf.get_selection_bounds()
                txt = (
                    pbuf.get_text(bnds[0], bnds[1], False).strip()
                    if bnds
                    else spec.raw_prompt
                )
                if txt and self._on_quote:
                    self._on_quote(txt)

            q_btn.connect("clicked", _quote_prompt)

    def _on_copy(self, _btn: Gtk.Button) -> None:
        copy_to_clipboard(self, self._spec.raw_prompt)
        self._on_toast("Prompt copied")
        self._copy_icon.set_icon("check", COLORS.accent_sage)
        self._copy_lbl.set_text("Copied")

        def _reset() -> bool:
            self._copy_icon.set_icon("copy", COLORS.text_secondary)
            self._copy_lbl.set_text("Copy prompt")
            return False

        GLib.timeout_add(1600, _reset)


# ---------------------------------------------------------------------------
# Unboxed Assistant Prose (Continuous ChatGPT-Style Selection + Citations)
# ---------------------------------------------------------------------------

_INLINE_TOKEN_RE = re.compile(
    r"`([^`\n]+)`"
    r"|\*\*([^*\n]+)\*\*"
    r"|\*([^*\n]+)\*"
    r"|\[([^\]\n]+)\]\((https?://[^\s)]+)\)"
    r"|\[(\d+)\]"
)


def _short_domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host or url[:24]
    except Exception:
        return url[:24]


def _make_source_details_popover(
    anchor_widget: Gtk.Widget,
    tab: ResearchTab,
    on_toast: Callable[[str], None] | None = None,
    on_quote: Callable[[str], None] | None = None,
) -> Gtk.Popover:
    pop = Gtk.Popover()
    pop.set_parent(anchor_widget)
    pop.add_css_class("ram-popover")
    pop.set_has_arrow(True)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_size_request(300, -1)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)

    dom_lbl = Gtk.Label(label=_short_domain(tab.url))
    dom_lbl.add_css_class("ref-palette-pill")
    dom_lbl.set_halign(Gtk.Align.START)
    box.append(dom_lbl)

    title_lbl = Gtk.Label(label=tab.title)
    title_lbl.add_css_class("research-perm-title")
    title_lbl.set_wrap(True)
    title_lbl.set_xalign(0.0)
    title_lbl.set_selectable(True)
    box.append(title_lbl)

    if tab.takeaway:
        tk_lbl = Gtk.Label(label=tab.takeaway)
        tk_lbl.add_css_class("handoff-sub")
        tk_lbl.set_wrap(True)
        tk_lbl.set_xalign(0.0)
        tk_lbl.set_selectable(True)
        box.append(tk_lbl)

    url_lbl = Gtk.Label(label=tab.url)
    url_lbl.add_css_class("research-tab-url")
    url_lbl.set_ellipsize(Pango.EllipsizeMode.END)
    url_lbl.set_xalign(0.0)
    url_lbl.set_selectable(True)
    box.append(url_lbl)

    btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    if on_quote is not None:
        q_btn = Gtk.Button(label="↳ Quote in chat")
        q_btn.set_has_frame(False)
        q_btn.add_css_class("mini-action-btn")
        q_btn.set_cursor_from_name("pointer")
        q_btn.connect(
            "clicked",
            lambda _b: (
                pop.popdown(),
                on_quote(f"{tab.title} ({tab.url}): {tab.takeaway}".strip(": ")),
            ),
        )
        btns.append(q_btn)

    copy_btn = Gtk.Button(label="Copy URL")
    copy_btn.set_has_frame(False)
    copy_btn.add_css_class("mini-action-btn")
    copy_btn.set_cursor_from_name("pointer")
    copy_btn.connect(
        "clicked",
        lambda _b: (
            copy_to_clipboard(anchor_widget, tab.url),
            pop.popdown(),
            on_toast("Source URL copied") if on_toast else None,
        ),
    )
    btns.append(copy_btn)
    box.append(btns)
    pop.set_child(box)
    return pop


CAMERA_ANGLE_OPTIONS: tuple[str, ...] = (
    "Auto",
    "Eye-Level 35mm",
    "Low-Angle 24mm",
    "High-Angle",
    "Close-Up 85mm",
    "Wide Establishing",
    "Dutch Tilt",
    "Over-Shoulder",
    "Dynamic Tracking",
)


class AssistantProseView(Gtk.Box):
    """Unboxed prose rendered in one continuous selectable TextView with inline citations."""

    def __init__(
        self,
        blocks: list[ProseBlock],
        tabs: list[ResearchTab] | None = None,
        on_toast: Callable[[str], None] | None = None,
        on_quote: Callable[[str], None] | None = None,
        on_select_all_chat: Callable[[], None] | None = None,
        on_select_camera: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._tabs = list(tabs or [])
        self._on_toast = on_toast
        self._on_quote = on_quote
        self._on_select_all_chat = on_select_all_chat
        self._on_select_camera = on_select_camera
        self._camera_chips: dict[str, Gtk.Button] = {}
        self._popovers: list[Gtk.Popover] = []

        self._tv = Gtk.TextView()
        self._tv.add_css_class("prose-textview")
        self._tv.set_editable(False)
        self._tv.set_cursor_visible(False)
        self._tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._tv.set_hexpand(True)
        self._tv.set_vexpand(False)
        self._tv.set_left_margin(0)
        self._tv.set_right_margin(0)
        self._tv.set_top_margin(2)
        self._tv.set_bottom_margin(2)

        buf = self._tv.get_buffer()
        self._tag_h2 = buf.create_tag(
            "h2", weight=Pango.Weight.SEMIBOLD, scale=1.18, pixels_above_lines=8, pixels_below_lines=3, foreground="#F3F4F6"
        )
        self._tag_h3 = buf.create_tag(
            "h3", weight=Pango.Weight.SEMIBOLD, scale=1.06, pixels_above_lines=6, pixels_below_lines=2, foreground="#E5E7EB"
        )
        self._tag_body = buf.create_tag(
            "body", pixels_above_lines=1, pixels_below_lines=4, foreground="#E2E8F0"
        )
        self._tag_bullet = buf.create_tag(
            "bullet", left_margin=16, indent=-12, pixels_above_lines=1, pixels_below_lines=3, foreground="#E2E8F0"
        )
        self._tag_bold = buf.create_tag("bold", weight=Pango.Weight.BOLD, foreground="#F8FAFC")
        self._tag_italic = buf.create_tag("italic", style=Pango.Style.ITALIC)
        self._tag_code = buf.create_tag(
            "code", family="monospace", scale=0.92, background="#1F222A", foreground="#E2E8F0"
        )
        self._tag_link = buf.create_tag(
            "link", underline=Pango.Underline.SINGLE, foreground="#93C5FD"
        )

        self._plain_chunks: list[str] = []
        for bi, blk in enumerate(blocks):
            if bi > 0:
                buf.insert(buf.get_end_iter(), "\n")
                self._plain_chunks.append("\n")
            if blk.kind == "h2":
                self._insert_rich_text(buf, blk.text, self._tag_h2)
                buf.insert(buf.get_end_iter(), "\n")
                self._plain_chunks.append(blk.text + "\n")
            elif blk.kind == "h3":
                self._insert_rich_text(buf, blk.text, self._tag_h3)
                buf.insert(buf.get_end_iter(), "\n")
                self._plain_chunks.append(blk.text + "\n")
            elif blk.kind == "p":
                self._insert_rich_text(buf, blk.text, self._tag_body)
                buf.insert(buf.get_end_iter(), "\n")
                self._plain_chunks.append(blk.text + "\n")
            elif blk.kind == "ul":
                for ii, item in enumerate(blk.items):
                    if ii > 0:
                        buf.insert(buf.get_end_iter(), "\n")
                    start_off = buf.get_char_count()
                    buf.insert_with_tags(buf.get_end_iter(), "• ", self._tag_bullet)
                    self._insert_rich_text(buf, item, self._tag_bullet)
                    buf.apply_tag(
                        self._tag_bullet,
                        buf.get_iter_at_offset(start_off),
                        buf.get_end_iter(),
                    )
                    self._plain_chunks.append(f"- {item}\n")

        # Trim trailing newline so footer sits tightly under content
        end_it = buf.get_end_iter()
        if buf.get_char_count() > 0:
            prev_it = buf.get_iter_at_offset(buf.get_char_count() - 1)
            if buf.get_text(prev_it, end_it, False) == "\n":
                buf.delete(prev_it, end_it)

        self.append(self._tv)

        # Floating selection popover ("↳ Ask Vimona" + "Copy") anchored above active selection
        self._sel_popover = Gtk.Popover()
        self._sel_popover.set_parent(self._tv)
        self._sel_popover.set_autohide(False)
        self._sel_popover.set_has_arrow(True)
        self._sel_popover.set_position(Gtk.PositionType.TOP)
        self._sel_popover.add_css_class("selection-float-popover")

        pop_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        if self._on_quote is not None:
            ask_btn = Gtk.Button(label="↳ Ask Vimona")
            ask_btn.set_has_frame(False)
            ask_btn.add_css_class("sel-action-pill")
            ask_btn.set_cursor_from_name("pointer")
            ask_btn.connect("clicked", self._on_quote_selection_clicked)
            pop_row.append(ask_btn)

        sel_copy_btn = Gtk.Button(label="Copy")
        sel_copy_btn.set_has_frame(False)
        sel_copy_btn.add_css_class("sel-action-pill")
        sel_copy_btn.set_cursor_from_name("pointer")
        sel_copy_btn.connect("clicked", self._on_copy_selection_clicked)
        pop_row.append(sel_copy_btn)
        self._sel_popover.set_child(pop_row)
        self._popovers.append(self._sel_popover)

        self._dragging = False
        click_ctrl = Gtk.GestureClick()
        click_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click_ctrl.connect("pressed", self._on_tv_pressed)
        click_ctrl.connect("released", self._on_tv_released)
        self._tv.add_controller(click_ctrl)
        buf.connect("notify::has-selection", self._on_selection_changed)

        # Compact ChatGPT-style footer: Copy · Select all · [Select chat] · [Sources (N)]
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        footer.add_css_class("msg-footer-bar")

        copy_all_btn = Gtk.Button()
        copy_all_btn.set_has_frame(False)
        copy_all_btn.add_css_class("mini-action-btn")
        copy_all_btn.set_cursor_from_name("pointer")
        copy_all_btn.set_tooltip_text("Copy response")
        cb_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        cb_box.append(SvgIcon("copy", size_px=11, color=COLORS.text_secondary))
        cb_box.append(Gtk.Label(label="Copy"))
        copy_all_btn.set_child(cb_box)
        copy_all_btn.connect("clicked", self._on_copy_entire_reply)
        footer.append(copy_all_btn)

        sel_all_btn = Gtk.Button(label="Select text")
        sel_all_btn.set_has_frame(False)
        sel_all_btn.add_css_class("mini-action-btn")
        sel_all_btn.set_cursor_from_name("pointer")
        sel_all_btn.set_tooltip_text("Highlight everything in this reply")
        sel_all_btn.connect("clicked", self._on_select_all_reply)
        footer.append(sel_all_btn)

        if self._on_select_all_chat is not None:
            sel_chat_btn = Gtk.Button(label="Select chat")
            sel_chat_btn.set_has_frame(False)
            sel_chat_btn.add_css_class("mini-action-btn")
            sel_chat_btn.set_cursor_from_name("pointer")
            sel_chat_btn.set_tooltip_text("Select across the entire conversation (Ctrl+Shift+A)")
            sel_chat_btn.connect("clicked", lambda _b: self._on_select_all_chat and self._on_select_all_chat())
            footer.append(sel_chat_btn)

        self._quote_inline_btn = Gtk.Button(label="↳ Quote selection")
        self._quote_inline_btn.set_has_frame(False)
        self._quote_inline_btn.add_css_class("mini-action-btn")
        self._quote_inline_btn.set_cursor_from_name("pointer")
        self._quote_inline_btn.set_tooltip_text("Highlight any text above or click Select text to quote it into your prompt")
        self._quote_inline_btn.connect("clicked", self._on_quote_selection_clicked)
        footer.append(self._quote_inline_btn)

        if self._tabs:
            src_btn = Gtk.MenuButton()
            src_btn.set_has_frame(False)
            src_btn.add_css_class("sources-pill-btn")
            src_btn.set_cursor_from_name("pointer")
            src_btn.set_tooltip_text("Inspect and select retrieved web sources")
            s_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            s_box.append(SvgIcon("globe", size_px=11, color=COLORS.accent_slate))
            s_box.append(Gtk.Label(label=f"Sources ({len(self._tabs)})"))
            src_btn.set_child(s_box)
            src_btn.set_popover(self._build_sources_popover())
            footer.append(src_btn)

        self.append(footer)

        if self._on_select_camera is not None:
            cam_strip = Gtk.FlowBox()
            cam_strip.set_selection_mode(Gtk.SelectionMode.NONE)
            cam_strip.set_column_spacing(5)
            cam_strip.set_row_spacing(5)
            cam_lbl = Gtk.Label(label="Camera:")
            cam_lbl.add_css_class("research-tab-url")
            cam_lbl.set_valign(Gtk.Align.CENTER)
            cam_strip.append(cam_lbl)
            for angle in CAMERA_ANGLE_OPTIONS[1:]:
                cb = Gtk.Button(label=angle)
                cb.set_has_frame(False)
                cb.add_css_class("quick-select-chip")
                cb.set_cursor_from_name("pointer")
                cb.connect("clicked", lambda _b, ang=angle: self._pick_camera_chip(ang))
                self._camera_chips[angle] = cb
                cam_strip.append(cb)
            self.append(cam_strip)

    def _insert_rich_text(self, buf: Gtk.TextBuffer, text: str, base_tag: Gtk.TextTag) -> None:
        pos = 0
        for m in _INLINE_TOKEN_RE.finditer(text):
            if m.start() > pos:
                buf.insert_with_tags(buf.get_end_iter(), text[pos : m.start()], base_tag)
            code_txt, bold_txt, italic_txt, link_lbl, link_url, cite_num = m.groups()
            if code_txt is not None:
                buf.insert_with_tags(buf.get_end_iter(), code_txt, base_tag, self._tag_code)
            elif bold_txt is not None:
                buf.insert_with_tags(buf.get_end_iter(), bold_txt, base_tag, self._tag_bold)
            elif italic_txt is not None:
                buf.insert_with_tags(buf.get_end_iter(), italic_txt, base_tag, self._tag_italic)
            elif link_lbl is not None and link_url is not None:
                buf.insert_with_tags(buf.get_end_iter(), link_lbl, base_tag, self._tag_link)
            elif cite_num is not None:
                idx = int(cite_num) - 1
                if 0 <= idx < len(self._tabs):
                    tab = self._tabs[idx]
                    anchor = buf.create_child_anchor(buf.get_end_iter())
                    chip = Gtk.Button(label=_short_domain(tab.url))
                    chip.set_has_frame(False)
                    chip.add_css_class("inline-cite-chip")
                    chip.set_cursor_from_name("pointer")
                    chip.set_tooltip_text(f"{tab.title}\n{tab.url}")
                    pop = _make_source_details_popover(chip, tab, on_toast=self._on_toast, on_quote=self._on_quote)
                    self._popovers.append(pop)
                    chip.connect("clicked", lambda _b, p=pop: p.popup())
                    self._tv.add_child_at_anchor(chip, anchor)
                else:
                    buf.insert_with_tags(buf.get_end_iter(), m.group(0), base_tag)
            pos = m.end()
        if pos < len(text):
            buf.insert_with_tags(buf.get_end_iter(), text[pos:], base_tag)

    def _build_sources_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.add_css_class("ram-popover")
        pop.set_has_arrow(True)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(False)
        scroll.set_size_request(360, 240)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_top(6)
        vbox.set_margin_bottom(6)
        vbox.set_margin_start(6)
        vbox.set_margin_end(6)

        hdr = Gtk.Label(label="Referenced Sources (Selectable)")
        hdr.add_css_class("research-perm-title")
        hdr.set_xalign(0.0)
        vbox.append(hdr)

        for idx, tab in enumerate(self._tabs):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row.add_css_class("research-tab-row")

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            badge = Gtk.Label(label=f"[{idx + 1}] {_short_domain(tab.url)}")
            badge.add_css_class("ref-palette-pill")
            top.append(badge)

            t_lbl = Gtk.Label(label=tab.title)
            t_lbl.add_css_class("research-tab-title")
            t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            t_lbl.set_hexpand(True)
            t_lbl.set_xalign(0.0)
            t_lbl.set_selectable(True)
            top.append(t_lbl)
            row.append(top)

            if tab.takeaway:
                tk = Gtk.Label(label=tab.takeaway)
                tk.add_css_class("handoff-sub")
                tk.set_wrap(True)
                tk.set_xalign(0.0)
                tk.set_selectable(True)
                row.append(tk)

            acts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if self._on_quote is not None:
                qb = Gtk.Button(label="↳ Use in prompt")
                qb.set_has_frame(False)
                qb.add_css_class("mini-action-btn")
                qb.set_cursor_from_name("pointer")
                qb.connect(
                    "clicked",
                    lambda _b, tb=tab, p=pop: (
                        p.popdown(),
                        self._on_quote and self._on_quote(f"{tb.title} ({tb.url})"),
                    ),
                )
                acts.append(qb)

            cpb = Gtk.Button(label="Copy URL")
            cpb.set_has_frame(False)
            cpb.add_css_class("mini-action-btn")
            cpb.set_cursor_from_name("pointer")
            cpb.connect(
                "clicked",
                lambda _b, u=tab.url: (
                    copy_to_clipboard(self, u),
                    self._on_toast("Source URL copied") if self._on_toast else None,
                ),
            )
            acts.append(cpb)
            row.append(acts)
            vbox.append(row)

        scroll.set_child(vbox)
        pop.set_child(scroll)
        return pop

    def _on_tv_pressed(self, *_a: object) -> None:
        self._dragging = True
        self._sel_popover.popdown()

    def _on_tv_released(self, *_a: object) -> None:
        self._dragging = False
        GLib.idle_add(self._maybe_show_selection_popover)

    def _pick_camera_chip(self, angle: str) -> None:
        for k, b in self._camera_chips.items():
            if k == angle:
                b.add_css_class("selected")
            else:
                b.remove_css_class("selected")
        if self._on_select_camera is not None:
            self._on_select_camera(angle)

    def _on_selection_changed(self, buf: Gtk.TextBuffer, _pspec: object) -> None:
        has_sel = buf.get_has_selection()
        if not has_sel:
            self._sel_popover.popdown()
        elif not self._dragging:
            GLib.idle_add(self._maybe_show_selection_popover)

    def _maybe_show_selection_popover(self) -> bool:
        buf = self._tv.get_buffer()
        bounds = buf.get_selection_bounds()
        if not bounds:
            self._sel_popover.popdown()
            return False
        sel_start, _sel_end = bounds
        loc = self._tv.get_iter_location(sel_start)
        wx, wy = self._tv.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, loc.x, loc.y)
        rect = Gdk.Rectangle()
        rect.x = max(0, int(wx))
        rect.y = max(0, int(wy))
        rect.width = max(12, int(loc.width) if loc.width > 0 else 24)
        rect.height = max(12, int(loc.height) if loc.height > 0 else 18)
        self._sel_popover.set_pointing_to(rect)
        if self._tv.get_mapped():
            self._sel_popover.popup()
        return False

    def _get_selected_text(self) -> str:
        buf = self._tv.get_buffer()
        bounds = buf.get_selection_bounds()
        if not bounds:
            return ""
        return buf.get_text(bounds[0], bounds[1], False).strip()

    def _on_quote_selection_clicked(self, _btn: Gtk.Button) -> None:
        txt = self._get_selected_text()
        self._sel_popover.popdown()
        if not txt:
            if self._on_toast:
                self._on_toast("Highlight text first, or click Select text")
            return
        if self._on_quote is not None:
            self._on_quote(txt)

    def _on_copy_selection_clicked(self, _btn: Gtk.Button) -> None:
        txt = self._get_selected_text()
        self._sel_popover.popdown()
        if txt:
            copy_to_clipboard(self, txt)
            if self._on_toast:
                self._on_toast("Selection copied")

    def _on_copy_entire_reply(self, _btn: Gtk.Button) -> None:
        buf = self._tv.get_buffer()
        txt = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not txt:
            txt = "".join(self._plain_chunks).strip()
        copy_to_clipboard(self, txt)
        if self._on_toast:
            self._on_toast("Response copied")

    def _on_select_all_reply(self, _btn: Gtk.Button) -> None:
        buf = self._tv.get_buffer()
        buf.select_range(buf.get_start_iter(), buf.get_end_iter())
        self._tv.grab_focus()
        GLib.idle_add(self._maybe_show_selection_popover)


# ---------------------------------------------------------------------------
# Firefox Web Search Permission & Result Cards
# ---------------------------------------------------------------------------

class ResearchPermissionCard(Gtk.Box):
    """Interactive SlideCommit permission prompt before Firefox internet research."""

    def __init__(
        self,
        query: str,
        url: str,
        on_decision: Callable[[bool], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("research-perm-card")

        self._on_decision = on_decision
        self._decided = False

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.append(SvgIcon("globe", size_px=15, color=COLORS.accent_slate))
        title = Gtk.Label(label="Allow zero-trace web reference pull?")
        title.add_css_class("research-perm-title")
        title.set_hexpand(True)
        title.set_xalign(0.0)
        top.append(title)

        self._status_badge = Gtk.Label(label="Waiting")
        self._status_badge.add_css_class("research-badge")
        top.append(self._status_badge)
        self.append(top)

        target_txt = f"URL: {url}" if url else f"Query: {query}"
        t_lbl = Gtk.Label(label=target_txt)
        t_lbl.add_css_class("research-perm-target")
        t_lbl.set_wrap(True)
        t_lbl.set_xalign(0.0)
        t_lbl.set_selectable(True)
        self._target_blur = SafeBlurBox(
            t_lbl,
            explicit=is_explicit_text(url or query),
            safe_blur_enabled=True,
            blur_radius=28.0,
            compact=True,
        )
        self.append(self._target_blur)

        note = Gtk.Label(
            label="Spawns a disposable Firefox sandbox in /dev/shm RAM, extracts refs into memory, then wipes all trace before air-gapped render."
        )
        note.add_css_class("research-perm-note")
        note.set_wrap(True)
        note.set_xalign(0.0)
        self.append(note)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        controls.set_margin_top(2)

        self.slider = SlideCommitWidget(
            label="Slide to allow",
            done_label="Allowed",
            error_label="Denied",
            on_confirm=self._handle_allow,
            track_color="#262626",
            handle_color="#f5f5f5",
            success_color="#22c55e",
            danger_color="#e5484d",
            width=268,
            height=48,
            radius=24,
            speed=50,
            return_bounce=0.38,
            landing_dip=0.026,
            hold_ms=0,
        )
        controls.append(self.slider)

        self._decline_btn = Gtk.Button(label="Decline")
        self._decline_btn.set_has_frame(False)
        self._decline_btn.add_css_class("perm-decline-btn")
        self._decline_btn.set_cursor_from_name("pointer")
        self._decline_btn.connect("clicked", self._handle_decline)
        controls.append(self._decline_btn)

        self.append(controls)

    def _handle_allow(self) -> None:
        if self._decided:
            return
        self._decided = True
        self._decline_btn.set_visible(False)
        self._status_badge.set_text("Searching…")
        self._on_decision(True)

    def _handle_decline(self, _btn: Gtk.Button) -> None:
        if self._decided:
            return
        self._decided = True
        self._decline_btn.set_sensitive(False)
        self._status_badge.set_text("Denied")
        self.slider.fail_error("Denied")
        self.slider.set_disabled(True)
        self._on_decision(False)

    def mark_finished(self, ok: bool, error_msg: str | None = None) -> None:
        if ok:
            self._status_badge.set_text("Allowed")
            self.slider.finish_done("Allowed")
        else:
            self._status_badge.set_text("Failed" if self._decided else "Denied")
            self.slider.fail_error(error_msg or "Denied")
            self.slider.set_disabled(True)


def _texture_from_ram_b64(b64_str: str) -> Gdk.Texture | None:
    """Decode base64 reference image bytes strictly in RAM into a Gdk.Texture."""
    if not b64_str:
        return None
    try:
        raw = base64.b64decode(b64_str)
        loader = GdkPixbuf.PixbufLoader()
        loader.write(raw)
        loader.close()
        pb = loader.get_pixbuf()
        if pb is None:
            return None
        return Gdk.Texture.new_for_pixbuf(pb)
    except Exception:
        return None


def _cairo_surface_from_ram_b64(b64_str: str) -> cairo.ImageSurface | None:
    """Decode base64 reference image bytes strictly in RAM into a Cairo ImageSurface."""
    if not b64_str:
        return None
    try:
        raw = base64.b64decode(b64_str)
        loader = GdkPixbuf.PixbufLoader()
        loader.write(raw)
        loader.close()
        return pixbuf_to_cairo_surface(loader.get_pixbuf())
    except Exception:
        return None


class ResearchCard(Gtk.Box):
    """Displays ephemeral /dev/shm Firefox research results and Safe-Blurred RAM references."""

    def __init__(
        self,
        data: ResearchStepData,
        on_open_reference: Callable[[list[ReferenceCandidate], int, bool], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("research-card")
        self._on_open_reference = on_open_reference

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.append(SvgIcon("globe", size_px=15, color=COLORS.accent_slate))
        lbl = Gtk.Label(label=data.browser_label)
        lbl.add_css_class("research-tab-title")
        lbl.set_hexpand(True)
        lbl.set_xalign(0.0)
        top.append(lbl)

        if data.trace_summary:
            wipe_badge = Gtk.Label(label=data.trace_summary)
            wipe_badge.add_css_class("ref-zero-trace-badge")
            top.append(wipe_badge)

        badge = Gtk.Label(label=data.status)
        badge.add_css_class("research-badge")
        top.append(badge)
        self.append(top)

        query_lbl = Gtk.Label(label=f'Searched: "{data.query}"')
        query_lbl.add_css_class("research-tab-url")
        query_lbl.set_wrap(True)
        query_lbl.set_xalign(0.0)
        query_blur = SafeBlurBox(
            query_lbl,
            explicit=is_explicit_text(data.query),
            safe_blur_enabled=True,
            blur_radius=28.0,
            compact=True,
        )
        self.append(query_blur)

        if data.references:
            strip = Gtk.FlowBox()
            strip.set_selection_mode(Gtk.SelectionMode.NONE)
            strip.set_max_children_per_line(4)
            strip.set_column_spacing(8)
            strip.set_row_spacing(8)
            for idx, ref in enumerate(data.references):
                tex = _texture_from_ram_b64(ref.image_bytes_b64)
                if tex is None:
                    continue
                tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                tile.add_css_class("ref-thumb-tile")
                tile.set_size_request(148, -1)
                tile.set_cursor_from_name("pointer")
                tile.set_tooltip_text("Click to inspect full image in-app (zero browser history)")

                pic = Gtk.Picture.new_for_paintable(tex)
                pic.set_content_fit(Gtk.ContentFit.COVER)
                pic.set_size_request(132, 92)
                pic.set_overflow(Gtk.Overflow.HIDDEN)

                explicit = ref.explicit or is_explicit_text(f"{ref.title} {ref.alt_text}")
                bb = SafeBlurBox(pic, explicit=explicit, safe_blur_enabled=True, blur_radius=26.0)
                tile.append(bb)

                if on_open_reference is not None:
                    r_click = Gtk.GestureClick()
                    r_click.connect(
                        "released",
                        lambda _g, _n, _x, _y, i=idx, blur_box=bb: on_open_reference(
                            data.references, i, blur_box.is_blurred
                        ),
                    )
                    tile.add_controller(r_click)

                cap_txt = ref.lighting_mood or ref.title
                meta_lbl = Gtk.Label(label=cap_txt)
                meta_lbl.add_css_class("research-tab-url")
                meta_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                meta_lbl.set_xalign(0.0)
                if is_explicit_text(cap_txt):
                    tile.append(
                        SafeBlurBox(
                            meta_lbl,
                            explicit=True,
                            safe_blur_enabled=True,
                            blur_radius=16.0,
                            compact=True,
                        )
                    )
                else:
                    tile.append(meta_lbl)
                strip.append(tile)
            self.append(strip)

        for tab in data.tabs:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            row.add_css_class("research-tab-row")

            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            t_lbl = Gtk.Label(label=tab.title)
            t_lbl.add_css_class("research-tab-title")
            t_lbl.set_xalign(0.0)
            t_lbl.set_hexpand(True)
            t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            u_lbl = Gtk.Label(label=tab.url)
            u_lbl.add_css_class("research-tab-url")
            u_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            head.append(t_lbl)
            head.append(u_lbl)
            row.append(head)

            takeaway = Gtk.Label(label=tab.takeaway)
            takeaway.add_css_class("handoff-sub")
            takeaway.set_wrap(True)
            takeaway.set_xalign(0.0)
            row.append(takeaway)
            tab_exp = is_explicit_text(f"{tab.title} {tab.url} {tab.takeaway}")
            if tab_exp:
                self.append(
                    SafeBlurBox(
                        row,
                        explicit=True,
                        safe_blur_enabled=True,
                        blur_radius=30.0,
                        compact=True,
                    )
                )
            else:
                self.append(row)


class ReferencePickerCard(Gtk.Box):
    """Interactive Safe-Blurred visual reference selector prior to air-gapped rendering."""

    def __init__(
        self,
        query: str,
        trace_summary: str,
        references: list[ReferenceCandidate],
        upgraded_prompt: str,
        will_render_video: bool,
        on_confirm: Callable[[ReferenceCandidate | None, str], None],
        on_cancel: Callable[[], None],
        on_open_reference: Callable[[list[ReferenceCandidate], int, bool], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("ref-picker-card")

        self._refs = list(references)
        self._upgraded_prompt = upgraded_prompt
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        self._on_open_reference = on_open_reference
        self._decided = False
        self._tile_boxes: list[Gtk.Box] = []
        # Pre-select the first safe reference if any exist
        self._selected_idx: int | None = None
        for idx, rf in enumerate(self._refs):
            if not (rf.explicit or is_explicit_text(f"{rf.title} {rf.alt_text} {query}")):
                self._selected_idx = idx
                break
        if self._selected_idx is None and self._refs:
            self._selected_idx = 0

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.append(SvgIcon("gallery", size_px=15, color=COLORS.accent_slate))
        hdr = Gtk.Label(label="Pick Reference Image (Safe-Blur Filtered)")
        hdr.add_css_class("research-perm-title")
        hdr.set_hexpand(True)
        hdr.set_xalign(0.0)
        top.append(hdr)

        wipe_badge = Gtk.Label(label=trace_summary or "/dev/shm Wiped ✓ · Air-Gap Ready")
        wipe_badge.add_css_class("ref-zero-trace-badge")
        top.append(wipe_badge)
        self.append(top)

        q_lbl = Gtk.Label(label=f"Query: {query}")
        q_lbl.add_css_class("research-perm-target")
        q_lbl.set_wrap(True)
        q_lbl.set_xalign(0.0)
        q_blur = SafeBlurBox(
            q_lbl,
            explicit=is_explicit_text(query),
            safe_blur_enabled=True,
            blur_radius=28.0,
            compact=True,
        )
        self.append(q_blur)

        sub = Gtk.Label(
            label="Hover blurred cards to reveal; click a card to ground offline rendering."
        )
        sub.add_css_class("research-perm-note")
        sub.set_wrap(True)
        sub.set_xalign(0.0)
        self.append(sub)

        if self._refs:
            grid = Gtk.Grid()
            grid.set_column_spacing(10)
            grid.set_row_spacing(10)
            grid.set_column_homogeneous(True)
            for idx, ref in enumerate(self._refs[:4]):
                tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                tile.add_css_class("ref-thumb-tile")
                tile.set_cursor_from_name("pointer")
                if idx == self._selected_idx:
                    tile.add_css_class("selected")

                is_exp = ref.explicit or is_explicit_text(f"{ref.title} {ref.alt_text}")
                bb_ref: SafeBlurBox | None = None
                tex = _texture_from_ram_b64(ref.image_bytes_b64)
                if tex is not None:
                    pic = Gtk.Picture.new_for_paintable(tex)
                    pic.set_content_fit(Gtk.ContentFit.COVER)
                    pic.set_size_request(-1, 132)
                    pic.set_overflow(Gtk.Overflow.HIDDEN)
                    bb = SafeBlurBox(pic, explicit=is_exp, safe_blur_enabled=True, blur_radius=26.0)
                    bb_ref = bb
                    if on_open_reference is not None:
                        img_ov = Gtk.Overlay()
                        img_ov.set_child(bb)
                        full_btn = Gtk.Button(label="Full")
                        full_btn.set_has_frame(False)
                        full_btn.add_css_class("mini-action-btn")
                        full_btn.set_halign(Gtk.Align.END)
                        full_btn.set_valign(Gtk.Align.START)
                        full_btn.set_margin_top(6)
                        full_btn.set_margin_end(6)
                        full_btn.set_cursor_from_name("pointer")
                        full_btn.set_tooltip_text("View full image in-app (zero browser history)")
                        full_btn.connect(
                            "clicked",
                            lambda _b, i=idx, blur_box=bb: on_open_reference(
                                self._refs, i, blur_box.is_blurred
                            ),
                        )
                        img_ov.add_overlay(full_btn)
                        tile.append(img_ov)
                    else:
                        tile.append(bb)

                t_title = ref.title or f"Reference {idx + 1}"
                t_lbl = Gtk.Label(label=t_title)
                t_lbl.add_css_class("research-tab-title")
                t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                t_lbl.set_xalign(0.0)
                if is_explicit_text(t_title):
                    tile.append(
                        SafeBlurBox(
                            t_lbl,
                            explicit=True,
                            safe_blur_enabled=True,
                            blur_radius=16.0,
                            compact=True,
                        )
                    )
                else:
                    tile.append(t_lbl)

                tags_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                if ref.lighting_mood:
                    m_lbl = Gtk.Label(label=ref.lighting_mood.split(",")[0].strip())
                    m_lbl.add_css_class("ref-palette-pill")
                    m_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                    tags_box.append(m_lbl)
                for pt in ref.palette_tags[:2]:
                    p_lbl = Gtk.Label(label=pt)
                    p_lbl.add_css_class("ref-palette-pill")
                    p_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                    tags_box.append(p_lbl)
                tile.append(tags_box)

                click = Gtk.GestureClick()

                def _on_tile_click(_g: Gtk.GestureClick, n_press: int, _x: float, _y: float, i: int = idx, blur_box: SafeBlurBox | None = bb_ref) -> None:
                    self._select_index(i)
                    if n_press >= 2 and on_open_reference is not None:
                        on_open_reference(self._refs, i, bool(blur_box.is_blurred) if blur_box else False)

                click.connect("released", _on_tile_click)
                tile.add_controller(click)
                self._tile_boxes.append(tile)
                grid.attach(tile, idx % 2, idx // 2, 1, 1)
            self.append(grid)

        prompt_box = Gtk.Label(label=f"Upgraded prompt: {upgraded_prompt}")
        prompt_box.add_css_class("research-perm-target")
        prompt_box.set_wrap(True)
        prompt_box.set_xalign(0.0)
        prompt_box.set_selectable(True)
        prompt_blur = SafeBlurBox(
            prompt_box,
            explicit=is_explicit_text(upgraded_prompt) or is_explicit_text(query),
            safe_blur_enabled=True,
            blur_radius=28.0,
            compact=True,
        )
        self.append(prompt_blur)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        render_lbl = (
            "Render Images + Video (Air-Gapped)"
            if will_render_video
            else "Render With Selected (Air-Gapped)"
        )
        self._render_btn = Gtk.Button(label=render_lbl)
        self._render_btn.add_css_class("ref-render-btn")
        self._render_btn.set_cursor_from_name("pointer")
        self._render_btn.connect("clicked", self._handle_confirm_ref)
        actions.append(self._render_btn)

        self._text_only_btn = Gtk.Button(label="Text Prompt Only")
        self._text_only_btn.add_css_class("ref-secondary-btn")
        self._text_only_btn.set_cursor_from_name("pointer")
        self._text_only_btn.connect("clicked", self._handle_text_only)
        actions.append(self._text_only_btn)

        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.add_css_class("ref-secondary-btn")
        self._cancel_btn.set_cursor_from_name("pointer")
        self._cancel_btn.connect("clicked", self._handle_cancel)
        actions.append(self._cancel_btn)
        self.append(actions)

    def _select_index(self, idx: int) -> None:
        if self._decided:
            return
        self._selected_idx = idx
        for i, tb in enumerate(self._tile_boxes):
            if i == idx:
                tb.add_css_class("selected")
            else:
                tb.remove_css_class("selected")

    def _handle_confirm_ref(self, _btn: Gtk.Button) -> None:
        if self._decided:
            return
        self._decided = True
        self._lock_buttons("Air-Gap Render Started ✓")
        chosen = (
            self._refs[self._selected_idx]
            if self._selected_idx is not None and 0 <= self._selected_idx < len(self._refs)
            else None
        )
        self._on_confirm(chosen, self._upgraded_prompt)

    def _handle_text_only(self, _btn: Gtk.Button) -> None:
        if self._decided:
            return
        self._decided = True
        self._lock_buttons("Rendering Text-Only (Air-Gapped) ✓")
        self._on_confirm(None, self._upgraded_prompt)

    def _handle_cancel(self, _btn: Gtk.Button) -> None:
        if self._decided:
            return
        self._decided = True
        self._lock_buttons("Cancelled")
        self._on_cancel()

    def _lock_buttons(self, status_text: str) -> None:
        self._render_btn.set_label(status_text)
        self._render_btn.set_sensitive(False)
        self._text_only_btn.set_sensitive(False)
        self._cancel_btn.set_sensitive(False)

    def mark_finished(self, status_text: str) -> None:
        self._decided = True
        self._lock_buttons(status_text)


# ---------------------------------------------------------------------------
# GPU Memory Handoff Progress Card
# ---------------------------------------------------------------------------

class MemoryHandoffCard(Gtk.Box):
    """Shows chat-to-media GPU memory handoff state."""

    def __init__(self, data: MemoryHandoffData) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("handoff-card")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        self._icon = SvgIcon(
            "chip",
            size_px=16,
            color=COLORS.accent_amber if data.progress_fraction < 0.99 else COLORS.accent_primary,
        )
        top.append(self._icon)
        self._title_lbl = Gtk.Label(label=data.active_pipeline)
        self._title_lbl.add_css_class("handoff-title")
        self._title_lbl.set_xalign(0.0)
        self._title_lbl.set_hexpand(True)
        top.append(self._title_lbl)

        self._eta_lbl = Gtk.Label(label=data.eta_label)
        self._eta_lbl.add_css_class("handoff-mono")
        top.append(self._eta_lbl)
        self.append(top)

        # Curved TextLoop "GENERATING" wave ribbon while generation is active (not thinking)
        self._loop_revealer = Gtk.Revealer()
        self._loop_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._loop_revealer.set_transition_duration(220)
        self._loop_revealer.set_reveal_child(data.progress_fraction < 0.99)
        loop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        loop_box.add_css_class("text-loop-banner")
        loop_box.set_overflow(Gtk.Overflow.HIDDEN)
        self._text_loop = TextLoopWidget(
            text="GENERATING",
            shape="wave",
            speed=95.0,
            separator="✦",
            curviness=36.0,
            font_size=44,
            font_weight=800,
            letter_spacing=2.0,
            uppercase=True,
            ribbon=True,
            ribbon_color="#5227FF",
            ribbon_width=82.0,
            edge_fade=0.22,
            pause_on_hover=True,
            height_px=104,
        )
        loop_box.append(self._text_loop)
        self._loop_revealer.set_child(loop_box)
        self.append(self._loop_revealer)

        mem_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._offload_lbl = Gtk.Label(label=data.offloaded_model)
        self._offload_lbl.add_css_class("handoff-mono")
        self._offload_lbl.set_xalign(0.0)
        self._offload_lbl.set_hexpand(True)
        mem_row.append(self._offload_lbl)

        self._vram_lbl = Gtk.Label(label=f"{data.vram_allocated} · {data.ram_cache}")
        self._vram_lbl.add_css_class("handoff-mono")
        mem_row.append(self._vram_lbl)
        self.append(mem_row)

        self._progress = Gtk.ProgressBar()
        self._progress.add_css_class("handoff-progress")
        self._progress.set_fraction(data.progress_fraction)
        self.append(self._progress)

        self._status_lbl = Gtk.Label(label=data.frame_status)
        self._status_lbl.add_css_class("handoff-sub")
        self._status_lbl.set_xalign(0.0)
        self.append(self._status_lbl)

        self._stages_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._render_stages(data)
        self.append(self._stages_row)

    def update(self, data: MemoryHandoffData) -> None:
        self._icon.set_icon(
            "chip",
            COLORS.accent_amber if data.progress_fraction < 0.99 else COLORS.accent_primary,
        )
        self._loop_revealer.set_reveal_child(data.progress_fraction < 0.99)
        self._title_lbl.set_text(data.active_pipeline)
        self._eta_lbl.set_text(data.eta_label)
        self._offload_lbl.set_text(data.offloaded_model)
        self._vram_lbl.set_text(f"{data.vram_allocated} · {data.ram_cache}")
        self._progress.set_fraction(data.progress_fraction)
        self._status_lbl.set_text(data.frame_status)
        self._render_stages(data)

    def _render_stages(self, data: MemoryHandoffData) -> None:
        c = self._stages_row.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._stages_row.remove(c)
            c = nxt
        for idx, st in enumerate(data.stages):
            pill = Gtk.Label(label=st)
            pill.add_css_class("handoff-stage-pill")
            if idx < data.stage_index:
                pill.add_css_class("done")
            elif idx == data.stage_index:
                pill.add_css_class("current")
            self._stages_row.append(pill)


# ---------------------------------------------------------------------------
# CRT Media Loading Gate (enforces >= 3s <CRTWarp /> placeholder when active)
# ---------------------------------------------------------------------------

class CRTMediaPlaceholderGate(Gtk.Stack):
    """Displays <CRTWarp /> while image/video loads, enforcing `min_hold_ms` (>=3000ms)."""

    def __init__(
        self,
        min_hold_ms: int = 3000,
        variation: int = 0,
        status_label: str = "Loading…",
        on_revealed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.set_transition_duration(320)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._on_revealed = on_revealed
        self._media_ready = False
        self._hold_done = min_hold_ms <= 0
        self._revealed = False
        self._hold_timer: int | None = None
        self._pause_timer: int | None = None
        self._started_at = time.monotonic()
        self._min_hold_ms = max(0, int(min_hold_ms))

        crt_overlay = Gtk.Overlay()
        crt_overlay.set_hexpand(True)
        crt_overlay.set_vexpand(True)

        self.crt = CRTWarpWidget(
            color="#c755f7",
            background_color="#120f17",
            mask="aperture",
            curvature=0.25,
            persistence=0.40,
            variation=variation,
            corners=0.14,
            scanline_strength=0.60,
            bloom=1.45,
            intro=True,
            mouse_react=True,
            change_on_click=True,
        )
        crt_overlay.set_child(self.crt)

        if status_label:
            pill = Gtk.Label(label=status_label)
            pill.add_css_class("keyframe-badge")
            pill.set_halign(Gtk.Align.END)
            pill.set_valign(Gtk.Align.END)
            pill.set_margin_bottom(8)
            pill.set_margin_end(8)
            crt_overlay.add_overlay(pill)

        self.add_named(crt_overlay, "crt")
        self._content_holder = Gtk.Box()
        self._content_holder.set_hexpand(True)
        self._content_holder.set_vexpand(True)
        self.add_named(self._content_holder, "content")

        self.set_visible_child_name("crt")

        if self._min_hold_ms > 0:
            self._hold_timer = GLib.timeout_add(self._min_hold_ms, self._on_hold_elapsed)

        self.connect("destroy", self._on_destroy)

    @property
    def is_revealed(self) -> bool:
        return self._revealed

    @property
    def min_hold_ms(self) -> int:
        return self._min_hold_ms

    def set_content_widget(self, child: Gtk.Widget) -> None:
        prev = self._content_holder.get_first_child()
        while prev is not None:
            nxt = prev.get_next_sibling()
            self._content_holder.remove(prev)
            prev = nxt
        child.set_hexpand(True)
        child.set_vexpand(True)
        self._content_holder.append(child)
        self._media_ready = True
        self._maybe_reveal()

    def _on_hold_elapsed(self) -> bool:
        self._hold_timer = None
        self._hold_done = True
        self._maybe_reveal()
        return False

    def _maybe_reveal(self) -> None:
        if self._revealed or not self._media_ready or not self._hold_done:
            return
        self._revealed = True
        self.set_visible_child_name("content")
        self._pause_timer = GLib.timeout_add(360, self._pause_crt_after_fade)
        if self._on_revealed is not None:
            self._on_revealed()

    def _pause_crt_after_fade(self) -> bool:
        self._pause_timer = None
        if self._revealed:
            self.crt.set_paused(True)
        return False

    def _on_destroy(self, _w: Gtk.Widget) -> None:
        if self._hold_timer is not None:
            GLib.source_remove(self._hold_timer)
            self._hold_timer = None
        if self._pause_timer is not None:
            GLib.source_remove(self._pause_timer)
            self._pause_timer = None


# ---------------------------------------------------------------------------
# Inline Image Grid (1, 2, 4, or 8 layouts)
# ---------------------------------------------------------------------------

class KeyframeGrid(Gtk.Box):
    """Responsive image grid supporting 1, 2, 4, or 8 layouts."""

    def __init__(
        self,
        keyframes: list[KeyframeSpec],
        initial_count: int,
        on_open_lightbox: Callable[[list[KeyframeSpec], int], None],
        min_placeholder_ms: int = 0,
        prompt_hint: str = "",
        safe_blur_enabled: bool = True,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._all_keyframes = list(keyframes)
        self._count = initial_count if initial_count in (1, 2, 4, 8) else 4
        self._on_open_lightbox = on_open_lightbox
        self._min_placeholder_ms = max(0, int(min_placeholder_ms))
        self._prompt_hint = prompt_hint
        self._safe_blur_enabled = safe_blur_enabled
        self._created_at = time.monotonic()
        self._layout_btns: dict[int, Gtk.Button] = {}
        self._gates: list[CRTMediaPlaceholderGate] = []
        self._blur_boxes: list[SafeBlurBox] = []

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.append(SvgIcon("grid", size_px=14, color=COLORS.text_secondary))
        lbl = Gtk.Label(label="Images")
        lbl.add_css_class("assistant-role-tag")
        lbl.set_xalign(0.0)
        lbl.set_hexpand(True)
        header.append(lbl)

        switcher = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for n in (1, 2, 4, 8):
            b = Gtk.Button(label=f"{n}")
            b.set_has_frame(False)
            b.add_css_class("grid-layout-pill")
            b.set_cursor_from_name("pointer")
            b.set_tooltip_text(f"Show {n} image{'s' if n > 1 else ''}")
            b.connect("clicked", lambda _btn, cnt=n: self.set_count(cnt))
            self._layout_btns[n] = b
            switcher.append(b)
        header.append(switcher)
        self.append(header)

        self._grid = Gtk.Grid()
        self._grid.set_column_spacing(10)
        self._grid.set_row_spacing(10)
        self._grid.set_column_homogeneous(True)
        self.append(self._grid)

        self._rebuild()

    def set_count(self, count: int) -> None:
        if count not in (1, 2, 4, 8):
            return
        self._count = count
        self._rebuild()

    def add_keyframe(self, kf: KeyframeSpec) -> None:
        """Append one rendered image immediately into the next grid cell without rebuilding."""
        self._all_keyframes.append(kf)
        idx = len(self._all_keyframes) - 1
        if idx >= self._count:
            return
        cols = 1 if self._count == 1 else (2 if self._count in (2, 4) else 4)
        tile_h = 310 if self._count == 1 else (196 if self._count in (2, 4) else 118)
        tile = self._build_tile(kf, idx, tile_h, 0)
        self._grid.attach(tile, idx % cols, idx // cols, 1, 1)

    def set_safe_blur_enabled(self, enabled: bool) -> None:
        self._safe_blur_enabled = enabled
        for bb in self._blur_boxes:
            bb.set_safe_blur_enabled(enabled)

    def _rebuild(self) -> None:
        for n, btn in self._layout_btns.items():
            if n == self._count:
                btn.add_css_class("active")
            else:
                btn.remove_css_class("active")

        self._gates.clear()
        self._blur_boxes.clear()
        child = self._grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._grid.remove(child)
            child = nxt

        visible_frames = self._all_keyframes[: self._count]
        cols = 1 if self._count == 1 else (2 if self._count in (2, 4) else 4)
        tile_h = 310 if self._count == 1 else (196 if self._count in (2, 4) else 118)

        elapsed_ms = int((time.monotonic() - self._created_at) * 1000.0)
        rem_hold_ms = max(0, self._min_placeholder_ms - elapsed_ms)

        for idx, kf in enumerate(visible_frames):
            tile = self._build_tile(kf, idx, tile_h, rem_hold_ms)
            col = idx % cols
            row = idx // cols
            self._grid.attach(tile, col, row, 1, 1)

    def _build_tile(self, kf: KeyframeSpec, idx: int, height_px: int, hold_ms: int) -> Gtk.Widget:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tile.add_css_class("keyframe-tile")
        tile.set_overflow(Gtk.Overflow.HIDDEN)
        tile.set_cursor_from_name("pointer")

        overlay = Gtk.Overlay()
        overlay.set_size_request(-1, height_px)

        badge = Gtk.Label(label=kf.code)
        badge.add_css_class("keyframe-badge")
        badge.set_halign(Gtk.Align.START)
        badge.set_valign(Gtk.Align.START)
        badge.set_margin_top(8)
        badge.set_margin_start(8)

        caption_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        caption_box.set_valign(Gtk.Align.END)
        caption_box.set_margin_bottom(8)
        caption_box.set_margin_start(8)
        caption_box.set_margin_end(8)
        caption_box.set_opacity(0.0)

        cap_lbl = Gtk.Label(label=kf.title)
        cap_lbl.add_css_class("keyframe-badge")
        cap_lbl.set_hexpand(True)
        cap_lbl.set_xalign(0.0)
        cap_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        caption_box.append(cap_lbl)

        exp_badge = Gtk.Box()
        exp_badge.add_css_class("keyframe-badge")
        exp_badge.append(SvgIcon("expand", size_px=12, color=COLORS.text_primary))
        caption_box.append(exp_badge)

        gate = CRTMediaPlaceholderGate(
            min_hold_ms=hold_ms,
            variation=idx,
            status_label="",
        )
        self._gates.append(gate)
        overlay.set_child(gate)
        overlay.add_overlay(badge)
        overlay.add_overlay(caption_box)

        def _apply_loaded_tex(tex: Gdk.Texture | None) -> bool:
            if tex is not None:
                pic = Gtk.Picture.new_for_paintable(tex)
                pic.set_content_fit(Gtk.ContentFit.COVER)
                pic.set_can_shrink(True)
                pic.set_hexpand(True)
                pic.set_vexpand(True)
                explicit = is_explicit_media(kf.image_path, self._prompt_hint, kf.title)
                blur_wrap = SafeBlurBox(
                    pic,
                    explicit=explicit,
                    safe_blur_enabled=self._safe_blur_enabled,
                    blur_radius=24.0,
                )
                self._blur_boxes.append(blur_wrap)
                gate.set_content_widget(blur_wrap)
            else:
                ph = Gtk.DrawingArea()
                ph.set_hexpand(True)
                ph.set_vexpand(True)
                ph.set_draw_func(lambda _a, cr, w, h, lbl=kf.title: draw_neutral_frame(cr, float(w), float(h), lbl))
                gate.set_content_widget(ph)
            return False

        if hold_ms > 0:
            def _bg_load() -> None:
                tex = load_image_texture(kf.image_path)
                GLib.idle_add(_apply_loaded_tex, tex)

            threading.Thread(target=_bg_load, daemon=True, name=f"vimona-img-load-{idx}").start()
        else:
            tex = load_image_texture(kf.image_path)
            _apply_loaded_tex(tex)

        motion = Gtk.EventControllerMotion()
        motion.connect(
            "enter",
            lambda *_a, cb=caption_box, g=gate: cb.set_opacity(1.0 if g.is_revealed else 0.0),
        )
        motion.connect("leave", lambda *_a, cb=caption_box: cb.set_opacity(0.0))
        tile.add_controller(motion)

        click = Gtk.GestureClick()
        click.connect(
            "released",
            lambda _g, _n, _x, _y, i=idx, g=gate: (
                self._on_open_lightbox(self._all_keyframes, i) if g.is_revealed else None
            ),
        )
        tile.add_controller(click)

        tile.append(overlay)
        return tile


# ---------------------------------------------------------------------------
# Embedded Video Player Card
# ---------------------------------------------------------------------------

class VideoPlayerCard(Gtk.Box):
    """Custom embedded video player with frame-accurate scrubber."""

    def __init__(
        self,
        clip: VideoClipSpec,
        on_open_lightbox: Callable[[VideoClipSpec, float], None],
        min_placeholder_ms: int = 0,
        prompt_hint: str = "",
        safe_blur_enabled: bool = True,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("video-card")
        self.set_overflow(Gtk.Overflow.HIDDEN)

        self._clip = clip
        self._on_open_lightbox = on_open_lightbox
        self._prompt_hint = prompt_hint
        self._safe_blur_enabled = safe_blur_enabled
        self._decoder: VideoFrameDecoder | None = None
        self._total_frames = max(1, int(clip.duration_s * clip.fps))
        self._t_norm = 0.0
        self._playing = False
        self._loop = True
        self._muted = True
        self._tick_id: int | None = None
        self._last_tick_us: int = 0
        self._scrubber_hover = False

        overlay = Gtk.Overlay()
        self._canvas = Gtk.DrawingArea()
        self._canvas.set_hexpand(True)
        self._canvas.set_size_request(-1, 290)
        self._canvas.set_draw_func(self._draw_video)
        self._canvas.set_cursor_from_name("pointer")
        canvas_click = Gtk.GestureClick()
        canvas_click.connect("released", lambda *_a: self.toggle_play())
        self._canvas.add_controller(canvas_click)

        self._center_play_btn = Gtk.Button()
        self._center_play_btn.set_has_frame(False)
        self._center_play_btn.add_css_class("video-play-circle")
        self._center_play_btn.set_halign(Gtk.Align.CENTER)
        self._center_play_btn.set_valign(Gtk.Align.CENTER)
        self._center_play_btn.set_cursor_from_name("pointer")
        self._center_play_btn.set_child(SvgIcon("play", size_px=18, color=COLORS.text_primary))
        self._center_play_btn.connect("clicked", lambda _b: self.toggle_play())

        top_badge = Gtk.Label(label=f"Video · {clip.duration_s:.0f}s")
        top_badge.add_css_class("keyframe-badge")
        top_badge.set_halign(Gtk.Align.START)
        top_badge.set_valign(Gtk.Align.START)
        top_badge.set_margin_top(10)
        top_badge.set_margin_start(10)

        expand_btn = Gtk.Button()
        expand_btn.set_has_frame(False)
        expand_btn.add_css_class("keyframe-badge")
        expand_btn.set_cursor_from_name("pointer")
        expand_btn.set_tooltip_text("Open video full screen")
        expand_btn.set_halign(Gtk.Align.END)
        expand_btn.set_valign(Gtk.Align.START)
        expand_btn.set_margin_top(10)
        expand_btn.set_margin_end(10)
        expand_btn.set_child(SvgIcon("expand", size_px=13, color=COLORS.text_primary))
        expand_btn.connect("clicked", lambda _b: self._launch_lightbox())

        def _on_video_revealed() -> None:
            self._center_play_btn.set_visible(not self._playing)
            expand_btn.set_visible(True)
            self._canvas.queue_draw()

        self._media_gate = CRTMediaPlaceholderGate(
            min_hold_ms=min_placeholder_ms,
            variation=2,
            status_label="",
            on_revealed=_on_video_revealed,
        )
        self._media_gate.set_size_request(-1, 290)

        overlay.set_child(self._media_gate)
        overlay.add_overlay(top_badge)
        overlay.add_overlay(expand_btn)
        overlay.add_overlay(self._center_play_btn)

        self.append(overlay)

        self._video_blur_box = SafeBlurBox(
            self._canvas,
            explicit=is_explicit_media(clip.video_path, self._prompt_hint, clip.title),
            safe_blur_enabled=self._safe_blur_enabled,
            blur_radius=24.0,
        )

        # Hide play/expand overlays while CRT placeholder holds for >=3s
        self._center_play_btn.set_visible(False)
        expand_btn.set_visible(False)

        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ctrl.add_css_class("video-controls-bar")

        self._play_btn = Gtk.Button()
        self._play_btn.set_has_frame(False)
        self._play_btn.add_css_class("icon-btn")
        self._play_btn.set_cursor_from_name("pointer")
        self._play_icon = SvgIcon("play", size_px=14, color=COLORS.text_primary)
        self._play_btn.set_child(self._play_icon)
        self._play_btn.connect("clicked", lambda _b: self.toggle_play())
        ctrl.append(self._play_btn)

        step_back = Gtk.Button()
        step_back.set_has_frame(False)
        step_back.add_css_class("icon-btn")
        step_back.set_cursor_from_name("pointer")
        step_back.set_tooltip_text("Previous frame")
        step_back.set_child(SvgIcon("step-back", size_px=13, color=COLORS.text_secondary))
        step_back.connect("clicked", lambda _b: self.step_frame(-1))
        ctrl.append(step_back)

        step_fwd = Gtk.Button()
        step_fwd.set_has_frame(False)
        step_fwd.add_css_class("icon-btn")
        step_fwd.set_cursor_from_name("pointer")
        step_fwd.set_tooltip_text("Next frame")
        step_fwd.set_child(SvgIcon("step-fwd", size_px=13, color=COLORS.text_secondary))
        step_fwd.connect("clicked", lambda _b: self.step_frame(1))
        ctrl.append(step_fwd)

        self._scrubber = Gtk.DrawingArea()
        self._scrubber.set_hexpand(True)
        self._scrubber.set_size_request(120, 22)
        self._scrubber.set_cursor_from_name("pointer")
        self._scrubber.set_draw_func(self._draw_scrubber)

        scrub_drag = Gtk.GestureDrag()
        scrub_drag.connect("drag-begin", self._on_scrub_begin)
        scrub_drag.connect("drag-update", self._on_scrub_update)
        self._scrubber.add_controller(scrub_drag)

        scrub_click = Gtk.GestureClick()
        scrub_click.connect("pressed", self._on_scrub_press)
        self._scrubber.add_controller(scrub_click)

        scrub_motion = Gtk.EventControllerMotion()
        scrub_motion.connect("enter", lambda *_a: self._set_scrubber_hover(True))
        scrub_motion.connect("leave", lambda *_a: self._set_scrubber_hover(False))
        self._scrubber.add_controller(scrub_motion)

        ctrl.append(self._scrubber)

        self._timecode = Gtk.Label()
        self._timecode.add_css_class("video-timecode")
        ctrl.append(self._timecode)

        self._loop_btn = Gtk.Button()
        self._loop_btn.set_has_frame(False)
        self._loop_btn.add_css_class("icon-btn")
        self._loop_btn.set_cursor_from_name("pointer")
        self._loop_btn.set_tooltip_text("Loop video")
        self._loop_icon = SvgIcon("loop", size_px=14, color=COLORS.accent_primary)
        self._loop_btn.set_child(self._loop_icon)
        self._loop_btn.connect("clicked", lambda _b: self._toggle_loop())
        ctrl.append(self._loop_btn)

        self._mute_btn = Gtk.Button()
        self._mute_btn.set_has_frame(False)
        self._mute_btn.add_css_class("icon-btn")
        self._mute_btn.set_cursor_from_name("pointer")
        self._mute_btn.set_tooltip_text("Mute / unmute")
        self._mute_icon = SvgIcon("mute", size_px=14, color=COLORS.text_muted)
        self._mute_btn.set_child(self._mute_icon)
        self._mute_btn.connect("clicked", lambda _b: self._toggle_mute())
        ctrl.append(self._mute_btn)

        self.append(ctrl)
        self._update_timecode()
        self.connect("unrealize", self._on_unrealize)

        def _apply_decoder(dec: VideoFrameDecoder) -> bool:
            self._decoder = dec
            if dec.is_ready:
                self._total_frames = dec.frame_count
            self._video_blur_box.set_explicit(
                is_explicit_media(clip.video_path, self._prompt_hint, clip.title)
            )
            self._media_gate.set_content_widget(self._video_blur_box)
            self._update_timecode()
            self._scrubber.queue_draw()
            return False

        if min_placeholder_ms > 0:
            def _bg_decode() -> None:
                dec = VideoFrameDecoder(clip.video_path)
                GLib.idle_add(_apply_decoder, dec)

            threading.Thread(target=_bg_decode, daemon=True, name="vimona-vid-load").start()
        else:
            dec = VideoFrameDecoder(clip.video_path)
            _apply_decoder(dec)

    def set_safe_blur_enabled(self, enabled: bool) -> None:
        self._safe_blur_enabled = enabled
        self._video_blur_box.set_safe_blur_enabled(enabled)

    def _set_scrubber_hover(self, hovered: bool) -> None:
        self._scrubber_hover = hovered
        self._scrubber.queue_draw()

    def _launch_lightbox(self) -> None:
        if self._playing:
            self.toggle_play()
        self._on_open_lightbox(self._clip, self._t_norm)

    def toggle_play(self) -> None:
        if not self._media_gate.is_revealed:
            return
        self._playing = not self._playing
        self._center_play_btn.set_visible(not self._playing)
        self._play_icon.set_icon("pause" if self._playing else "play", COLORS.text_primary)
        if self._playing:
            self._last_tick_us = 0
            if self._tick_id is None:
                self._tick_id = self.add_tick_callback(self._on_tick)
        elif self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def step_frame(self, delta: int) -> None:
        if self._playing:
            self.toggle_play()
        frame = round(self._t_norm * self._total_frames) + delta
        frame = max(0, min(self._total_frames - 1, frame))
        self._t_norm = frame / max(1, self._total_frames)
        self._canvas.queue_draw()
        self._scrubber.queue_draw()
        self._update_timecode()

    def _toggle_loop(self) -> None:
        self._loop = not self._loop
        self._loop_icon.set_icon("loop", COLORS.accent_primary if self._loop else COLORS.text_muted)

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self._mute_icon.set_icon(
            "mute" if self._muted else "volume",
            COLORS.text_muted if self._muted else COLORS.text_primary,
        )

    def _on_tick(self, _w: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        now = clock.get_frame_time()
        if self._last_tick_us == 0:
            self._last_tick_us = now
            return True
        dt = (now - self._last_tick_us) / 1_000_000.0
        self._last_tick_us = now
        self._t_norm += dt / self._clip.duration_s
        if self._t_norm >= 1.0:
            if self._loop:
                self._t_norm %= 1.0
            else:
                self._t_norm = 1.0
                self.toggle_play()
                return False
        self._canvas.queue_draw()
        self._scrubber.queue_draw()
        self._update_timecode()
        return True

    def _on_scrub_begin(self, _g: Gtk.GestureDrag, start_x: float, _start_y: float) -> None:
        self._scrub_start_x = start_x
        self._set_t_from_x(start_x)

    def _on_scrub_update(self, _g: Gtk.GestureDrag, offset_x: float, _offset_y: float) -> None:
        self._set_t_from_x(self._scrub_start_x + offset_x)

    def _on_scrub_press(self, _g: Gtk.GestureClick, _n: int, x: float, _y: float) -> None:
        self._set_t_from_x(x)

    def _set_t_from_x(self, x: float) -> None:
        w = max(1.0, float(self._scrubber.get_width()))
        self._t_norm = max(0.0, min(1.0, x / w))
        self._canvas.queue_draw()
        self._scrubber.queue_draw()
        self._update_timecode()

    def _update_timecode(self) -> None:
        cur_s = self._t_norm * self._clip.duration_s
        cur_f = int(self._t_norm * self._total_frames)
        self._timecode.set_text(
            f"{cur_s:05.2f}s / {self._clip.duration_s:04.2f}s · f{cur_f:03d}"
        )

    def _draw_video(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        if self._decoder is not None and self._decoder.is_ready:
            frame_idx = int(round(self._t_norm * max(0, self._total_frames - 1)))
            surf = self._decoder.get_frame_surface(frame_idx)
            if surf is not None:
                sw, sh = float(surf.get_width()), float(surf.get_height())
                scale = min(float(w) / sw, float(h) / sh)
                dw, dh = sw * scale, sh * scale
                dx, dy = (float(w) - dw) * 0.5, (float(h) - dh) * 0.5
                cr.set_source_rgb(0.06, 0.06, 0.06)
                cr.rectangle(0, 0, w, h)
                cr.fill()
                cr.save()
                cr.translate(dx, dy)
                cr.scale(scale, scale)
                cr.set_source_surface(surf, 0, 0)
                cr.paint()
                cr.restore()
                return
        draw_neutral_frame(cr, float(w), float(h), self._clip.title)

    def _draw_scrubber(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        cy = h * 0.5
        pad = 4.0
        track_w = max(10.0, float(w) - pad * 2.0)

        cr.set_source_rgba(0.93, 0.93, 0.93, 0.14)
        cr.set_line_width(3.0)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(pad, cy)
        cr.line_to(pad + track_w, cy)
        cr.stroke()

        px = pad + track_w * self._t_norm
        cr.set_source_rgba(0.93, 0.93, 0.93, 0.90)
        cr.move_to(pad, cy)
        cr.line_to(px, cy)
        cr.stroke()

        r = 5.5 if self._scrubber_hover else 4.0
        cr.set_source_rgb(0.93, 0.93, 0.93)
        cr.arc(px, cy, r, 0, math.tau)
        cr.fill()

    def _on_unrealize(self, _w: Gtk.Widget) -> None:
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None
        if self._decoder is not None:
            self._decoder.close()


# ---------------------------------------------------------------------------
# Floating Bottom Composer
# ---------------------------------------------------------------------------

class FloatingComposer(Gtk.Box):
    """Clean floating pill composer with auto-resize and flat inline controls."""

    def __init__(
        self,
        on_submit: Callable[..., None],
        on_stop: Callable[[], None],
        on_batch_changed: Callable[[int], None] | None = None,
        on_queue: Callable[..., None] | None = None,
        on_skill_changed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("composer-scrim")

        self._on_submit = on_submit
        self._on_stop = on_stop
        self._on_batch_changed = on_batch_changed
        self._on_queue = on_queue
        self._on_skill_changed = on_skill_changed
        self._is_busy = False
        self._locked_mode: str | None = None
        self._active_skill = AVAILABLE_SKILLS[0][0]
        self._batch_count = 4
        self._clip_seconds = 4
        self._clip_durations: list[int] = [4, 8, 12]
        self._steps_override: int | None = None
        self._steps_syncing = False
        self._steps_warn_dismiss_id: int | None = None
        self._camera_angle = CAMERA_ANGLE_OPTIONS[0]

        self._aquarium = AquariumSimulation(fish_count=22)
        self._aq_mode = True
        self._aq_alpha = 1.0
        self._aq_tick_id: int | None = None
        self._dissolve_timer: int | None = None
        self._dissolve_start: float = 0.0

        self._tank = Gtk.Overlay()
        self._tank.add_css_class("aquarium-tank")
        self._tank.set_overflow(Gtk.Overflow.HIDDEN)

        self._aq_canvas = Gtk.DrawingArea()
        self._aq_canvas.set_hexpand(True)
        self._aq_canvas.set_vexpand(True)
        self._aq_canvas.set_content_height(182)
        self._aq_canvas.set_draw_func(self._draw_aquarium)
        self._tank.set_child(self._aq_canvas)

        self._tank_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._tank_inner.set_margin_top(22)
        self._tank_inner.set_margin_bottom(10)
        self._tank_inner.set_margin_start(16)
        self._tank_inner.set_margin_end(16)
        self._tank_inner.set_valign(Gtk.Align.CENTER)

        self._pill = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._pill.add_css_class("composer-pill")
        self._pill.add_css_class("aquarium-glass")

        self._queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._queue_box.add_css_class("composer-queue-bar")
        self._queue_box.set_visible(False)
        self._pill.append(self._queue_box)

        self._quoted_text: str = ""
        self._quote_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._quote_bar.add_css_class("composer-quote-bar")
        self._quote_bar.set_visible(False)
        self._quote_lbl = Gtk.Label()
        self._quote_lbl.add_css_class("composer-quote-text")
        self._quote_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._quote_lbl.set_hexpand(True)
        self._quote_lbl.set_xalign(0.0)
        self._quote_blur = SafeBlurBox(
            self._quote_lbl,
            explicit=False,
            safe_blur_enabled=True,
            blur_radius=26.0,
            compact=True,
        )
        self._quote_blur.set_hexpand(True)
        self._quote_bar.append(self._quote_blur)
        quote_clear = Gtk.Button(label="×")
        quote_clear.set_has_frame(False)
        quote_clear.add_css_class("mini-action-btn")
        quote_clear.set_cursor_from_name("pointer")
        quote_clear.set_tooltip_text("Clear quote")
        quote_clear.connect("clicked", lambda _b: self.clear_quoted_text())
        self._quote_bar.append(quote_clear)
        self._pill.append(self._quote_bar)

        text_overlay = Gtk.Overlay()
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.add_css_class("composer-scroll")
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.set_min_content_height(36)
        self._scroll.set_max_content_height(156)

        self._textview = Gtk.TextView()
        self._textview.add_css_class("composer-textview")
        self._textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._textview.set_top_margin(4)
        self._textview.set_bottom_margin(4)
        self._textview.set_left_margin(2)
        self._textview.set_right_margin(2)
        self._scroll.set_child(self._textview)
        self._input_blur_layer = _BlurLayer(self._scroll, radius=28.0, compact=True)
        text_overlay.set_child(self._input_blur_layer)

        self._input_blur_scrim = Gtk.Box()
        self._input_blur_scrim.add_css_class("safe-blur-scrim-compact")
        self._input_blur_scrim.set_can_target(False)
        self._input_blur_scrim.set_visible(False)
        text_overlay.add_overlay(self._input_blur_scrim)

        self._input_blur_badge = Gtk.Label(label="safe-blur · click to edit")
        self._input_blur_badge.add_css_class("safe-blur-label-compact")
        self._input_blur_badge.set_halign(Gtk.Align.CENTER)
        self._input_blur_badge.set_valign(Gtk.Align.CENTER)
        self._input_blur_badge.set_can_target(False)
        self._input_blur_badge.set_visible(False)
        text_overlay.add_overlay(self._input_blur_badge)

        self._placeholder = Gtk.Label(
            label="Message Vimona, or describe an image or video…"
        )
        self._placeholder.add_css_class("composer-placeholder")
        self._placeholder.set_halign(Gtk.Align.START)
        self._placeholder.set_valign(Gtk.Align.START)
        self._placeholder.set_margin_top(4)
        self._placeholder.set_margin_start(3)
        self._placeholder.set_can_target(False)
        text_overlay.add_overlay(self._placeholder)

        self._pill.append(text_overlay)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("composer-toolbar")

        left_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        left_rows.set_hexpand(True)

        row_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._skill_btn = Gtk.MenuButton()
        self._skill_btn.set_has_frame(False)
        self._skill_btn.add_css_class("skill-chip")
        self._skill_btn.set_cursor_from_name("pointer")
        skill_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._skill_icon = SvgIcon("layers", size_px=12, color="#0B4757")
        skill_box.append(self._skill_icon)
        self._skill_lbl = Gtk.Label(label=self._active_skill)
        skill_box.append(self._skill_lbl)
        self._skill_chev = SvgIcon("chevron-down", size_px=11, color="#0B4757")
        skill_box.append(self._skill_chev)
        self._skill_btn.set_child(skill_box)
        self._skill_btn.set_popover(self._build_skill_popover())
        row_top.append(self._skill_btn)

        div3 = Gtk.Box()
        div3.add_css_class("toolbar-divider")
        div3.set_valign(Gtk.Align.CENTER)
        row_top.append(div3)

        self._camera_btn = Gtk.MenuButton()
        self._camera_btn.set_has_frame(False)
        self._camera_btn.add_css_class("camera-chip")
        self._camera_btn.set_cursor_from_name("pointer")
        self._camera_btn.set_tooltip_text("Select camera angle & lens framing")
        cam_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        cam_box.append(SvgIcon("gallery", size_px=11, color=COLORS.accent_slate))
        self._camera_lbl = Gtk.Label(label=f"Cam: {self._camera_angle}")
        cam_box.append(self._camera_lbl)
        cam_box.append(SvgIcon("chevron-down", size_px=10, color=COLORS.text_muted))
        self._camera_btn.set_child(cam_box)
        self._camera_btn.set_popover(self._build_camera_popover())
        row_top.append(self._camera_btn)
        left_rows.append(row_top)

        row_media = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._batch_buttons: dict[int, Gtk.Button] = {}
        batch_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        batch_group.add_css_class("selector-group")
        b_pre = Gtk.Label(label="Images")
        b_pre.add_css_class("selector-prefix")
        batch_group.append(b_pre)
        for cnt in (1, 2, 4, 8):
            btn = Gtk.Button(label=str(cnt))
            btn.set_has_frame(False)
            btn.add_css_class("selector-option")
            if cnt == self._batch_count:
                btn.add_css_class("selected")
            btn.set_cursor_from_name("pointer")
            btn.set_tooltip_text(f"Number of images to generate: {cnt}")
            btn.connect("clicked", lambda _b, c=cnt: self.set_batch_count(c))
            self._batch_buttons[cnt] = btn
            batch_group.append(btn)
        row_media.append(batch_group)

        self._div2 = Gtk.Box()
        self._div2.add_css_class("toolbar-divider")
        self._div2.set_valign(Gtk.Align.CENTER)
        row_media.append(self._div2)

        self._clip_buttons: dict[int, Gtk.Button] = {}
        self._clip_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        self._clip_group.add_css_class("selector-group")
        row_media.append(self._clip_group)
        self.update_video_durations(self._clip_durations)

        self._div3 = Gtk.Box()
        self._div3.add_css_class("toolbar-divider")
        self._div3.set_valign(Gtk.Align.CENTER)
        row_media.append(self._div3)

        self._steps_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        self._steps_group.add_css_class("selector-group")
        s_pre = Gtk.Label(label="Steps")
        s_pre.add_css_class("selector-prefix")
        self._steps_group.append(s_pre)

        self._steps_auto_btn = Gtk.Button(label="Auto")
        self._steps_auto_btn.set_has_frame(False)
        self._steps_auto_btn.add_css_class("selector-option")
        self._steps_auto_btn.add_css_class("selected")
        self._steps_auto_btn.set_cursor_from_name("pointer")
        self._steps_auto_btn.set_tooltip_text("Use boosted 4–6 step Auto recipe")
        self._steps_auto_btn.connect("clicked", lambda _b: self.set_steps_override(None))
        self._steps_group.append(self._steps_auto_btn)

        self._steps_entry = Gtk.Entry()
        self._steps_entry.add_css_class("steps-entry")
        self._steps_entry.set_placeholder_text("Custom")
        self._steps_entry.set_width_chars(6)
        self._steps_entry.set_max_width_chars(6)
        self._steps_entry.set_max_length(8)
        self._steps_entry.set_alignment(0.5)
        self._steps_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self._steps_entry.set_tooltip_text(
            "Enter custom denoising steps (1–60) — process will take longer than boosted Auto"
        )
        self._steps_entry.connect("changed", self._on_steps_entry_changed)
        self._steps_group.append(self._steps_entry)

        self._steps_warn_popover = Gtk.Popover()
        self._steps_warn_popover.set_parent(self._steps_entry)
        self._steps_warn_popover.set_position(Gtk.PositionType.TOP)
        self._steps_warn_popover.set_autohide(False)
        self._steps_warn_popover.set_can_focus(False)
        self._steps_warn_popover.add_css_class("steps-warn-popover")
        warn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        warn_icon = SvgIcon("alert", size_px=15, color="#F59E0B")
        warn_icon.set_valign(Gtk.Align.START)
        warn_box.append(warn_icon)
        warn_txt_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._steps_warn_title = Gtk.Label(label="Custom Steps Active")
        self._steps_warn_title.add_css_class("steps-warn-title")
        self._steps_warn_title.set_xalign(0.0)
        self._steps_warn_body = Gtk.Label(
            label="The process will take longer than boosted Auto (4–6 steps)."
        )
        self._steps_warn_body.add_css_class("steps-warn-body")
        self._steps_warn_body.set_xalign(0.0)
        self._steps_warn_body.set_wrap(True)
        self._steps_warn_body.set_max_width_chars(34)
        warn_txt_col.append(self._steps_warn_title)
        warn_txt_col.append(self._steps_warn_body)
        warn_box.append(warn_txt_col)
        warn_close = Gtk.Button(label="×")
        warn_close.set_has_frame(False)
        warn_close.add_css_class("mini-action-btn")
        warn_close.set_valign(Gtk.Align.START)
        warn_close.set_cursor_from_name("pointer")
        warn_close.connect("clicked", lambda _b: self._hide_steps_warning())
        warn_box.append(warn_close)
        self._steps_warn_popover.set_child(warn_box)

        row_media.append(self._steps_group)
        left_rows.append(row_media)

        toolbar.append(left_rows)

        right_acts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        right_acts.set_valign(Gtk.Align.END)

        self._stop_btn = Gtk.Button()
        self._stop_btn.set_has_frame(False)
        self._stop_btn.add_css_class("composer-stop-btn")
        self._stop_btn.set_cursor_from_name("pointer")
        self._stop_btn.set_sensitive(False)
        self._stop_btn.set_tooltip_text("Stop active generation")
        stop_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self._stop_icon = SvgIcon("stop", size_px=11, color=COLORS.text_muted)
        self._stop_lbl = Gtk.Label(label="Stop")
        stop_box.append(self._stop_icon)
        stop_box.append(self._stop_lbl)
        self._stop_btn.set_child(stop_box)
        self._stop_btn.connect("clicked", lambda _b: self._on_stop())
        right_acts.append(self._stop_btn)

        self._send_btn = Gtk.Button()
        self._send_btn.set_has_frame(False)
        self._send_btn.add_css_class("send-btn")
        self._send_btn.add_css_class("disabled")
        self._send_btn.set_cursor_from_name("pointer")
        self._send_icon = SvgIcon("arrow-up", size_px=15, color=COLORS.text_muted)
        self._send_btn.set_child(self._send_icon)
        self._send_btn.connect("clicked", self._on_send_or_stop)
        right_acts.append(self._send_btn)
        toolbar.append(right_acts)

        self._pill.append(toolbar)
        self._tank_inner.append(self._pill)

        self._aq_caption = Gtk.Label(label="Local & Uncensored")
        self._aq_caption.add_css_class("aquarium-caption")
        self._aq_caption.set_halign(Gtk.Align.CENTER)
        self._aq_caption.set_can_target(False)
        self._tank_inner.append(self._aq_caption)

        self._tank.add_overlay(self._tank_inner)
        self._tank.set_measure_overlay(self._tank_inner, True)

        self._electric_wrap = Gtk.Overlay()
        pill_pad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        pill_pad.set_margin_top(8)
        pill_pad.set_margin_bottom(8)
        pill_pad.set_margin_start(8)
        pill_pad.set_margin_end(8)
        pill_pad.append(self._tank)
        self._electric_wrap.set_child(pill_pad)

        self._electric_border = ElectricBorderOverlay(
            color="#7df9ff",
            speed=1.0,
            chaos=0.5,
            spread=0.6,
            thickness=1.5,
            strands=3,
            glow=0.6,
            intensity=0.8,
            arcs=1.0,
            flicker=0.5,
            border_radius=18.0,
            padding=8.0,
            active=True,
            intro=True,
            interactive=True,
            cursor_radius=90.0,
        )
        self._electric_wrap.add_overlay(self._electric_border)
        self.append(self._electric_wrap)

        eb_motion = Gtk.EventControllerMotion()
        eb_motion.connect("motion", lambda _c, x, y: self._electric_border.on_pointer_move(x, y))
        eb_motion.connect("leave", lambda *_a: self._electric_border.on_pointer_leave())
        self._electric_wrap.add_controller(eb_motion)

        eb_click = Gtk.GestureClick()
        eb_click.connect("pressed", lambda _g, _n, x, y: self._electric_border.trigger_pulse(x, y))
        self._electric_wrap.add_controller(eb_click)

        aq_motion = Gtk.EventControllerMotion()
        aq_motion.connect("motion", lambda _c, x, y: self._aquarium.on_pointer_move(x, y) if self._aq_mode else None)
        aq_motion.connect("leave", lambda *_a: self._aquarium.on_pointer_leave())
        self._tank.add_controller(aq_motion)

        aq_click = Gtk.GestureClick()
        aq_click.connect("pressed", lambda _g, _n, x, y: self._aquarium.add_ripple(x, y) if self._aq_mode else None)
        self._tank.add_controller(aq_click)

        self._aq_tick_id = self._aq_canvas.add_tick_callback(self._on_aquarium_tick)
        self._aq_canvas.connect("unrealize", self._on_aquarium_unrealize)

        # Live active-model status bar & connected-models popover right under textbox
        self._runtime_owner = "idle"
        self._runtime_model = "light-uncensored.gguf"
        self._model_badges: dict[str, Gtk.Label] = {}

        self._model_btn = Gtk.MenuButton()
        self._model_btn.set_has_frame(False)
        self._model_btn.add_css_class("model-status-btn")
        self._model_btn.set_halign(Gtk.Align.CENTER)
        self._model_btn.set_cursor_from_name("pointer")
        self._model_btn.set_tooltip_text("Click to inspect connected local models and memory status")

        m_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._model_dot = Gtk.DrawingArea()
        self._model_dot.set_size_request(8, 8)
        self._model_dot.set_valign(Gtk.Align.CENTER)
        self._model_dot.set_draw_func(self._draw_model_dot)
        m_box.append(self._model_dot)

        self._model_lbl = Gtk.Label(
            label=f"Text: light-uncensored.gguf · Images: Pony V6 XL · Video: Wan 2.1 VACE · {get_footer_telemetry_label()}"
        )
        self._model_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        m_box.append(self._model_lbl)
        self._model_btn.set_child(m_box)
        self._model_btn.set_popover(self._build_models_popover())
        self.append(self._model_btn)

        buf = self._textview.get_buffer()
        buf.connect("changed", self._on_text_changed)

        focus_ctrl = Gtk.EventControllerFocus()
        focus_ctrl.connect("enter", lambda *_a: (self._pill.add_css_class("focused"), self._sync_composer_query_blur()))
        focus_ctrl.connect("leave", lambda *_a: (self._pill.remove_css_class("focused"), self._sync_composer_query_blur()))
        self._textview.add_controller(focus_ctrl)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self._textview.add_controller(key_ctrl)

    def _build_models_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_size_request(360, -1)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)

        hdr = Gtk.Label(label="CONNECTED LOCAL MODELS (16 GB RELAY)")
        hdr.add_css_class("prompt-block-label")
        hdr.set_xalign(0.0)
        box.append(hdr)

        entries = [
            ("text", "Text · light-uncensored.gguf", "llama-server · Vulkan0 · --load-mode none · 4096 ctx"),
            ("pony", "Images · pony-diffusion-v6-xl-Q8_0.gguf", "Vulkan sd-server + DMD2 · Air-gapped netns"),
            ("wan", "Video · wan2.1_vace_1.3B_fp16.safetensors", "ComfyUI Wan VACE + CausVid · 24fps flow · Air-gapped"),
            ("research", "Web Refs · Ephemeral /dev/shm Firefox", "Memory-only sandbox · Safe-Blur · Wiped before render"),
        ]
        for key, title, sub in entries:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            left.set_hexpand(True)
            t = Gtk.Label(label=title)
            t.add_css_class("model-status-row-title")
            t.set_xalign(0.0)
            s = Gtk.Label(label=sub)
            s.add_css_class("session-row-meta")
            s.set_xalign(0.0)
            s.set_wrap(True)
            left.append(t)
            left.append(s)
            row.append(left)

            badge = Gtk.Label(label="Standby (0 MB)")
            badge.add_css_class("model-status-badge-standby")
            badge.set_valign(Gtk.Align.CENTER)
            self._model_badges[key] = badge
            row.append(badge)
            box.append(row)

        pop.set_child(box)
        return pop

    def _draw_model_dot(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        cx, cy = w * 0.5, h * 0.5
        if self._runtime_owner == "text":
            cr.set_source_rgba(0.549, 0.722, 0.604, 0.95)
        elif self._runtime_owner == "media":
            cr.set_source_rgba(0.788, 0.659, 0.459, 0.95)
        elif self._runtime_owner == "research":
            cr.set_source_rgba(0.659, 0.710, 0.784, 0.95)
        elif self._runtime_owner == "transition":
            cr.set_source_rgba(0.788, 0.659, 0.459, 0.65)
        else:
            cr.set_source_rgba(0.45, 0.45, 0.45, 0.8)
        cr.arc(cx, cy, 3.0, 0, math.tau)
        cr.fill()

    def refresh_perf_footer(self, mem_str: str = "") -> None:
        self.set_runtime_status(self._runtime_owner, self._runtime_model, mem_str or getattr(self, "_last_mem_str", "RAM ready"))

    def set_runtime_status(self, owner: str, active_model: str, mem_str: str) -> None:
        self._runtime_owner = owner
        self._runtime_model = active_model
        self._last_mem_str = mem_str
        perf_tag = get_footer_telemetry_label()
        if owner == "text":
            self._model_lbl.set_text(
                f"Working: {active_model} · Ready: Pony V6 XL, Wan 2.1 VACE, Firefox · {perf_tag} · {mem_str}"
            )
        elif owner == "media":
            self._model_lbl.set_text(
                f"Rendering: {active_model} · Text AI offline (0 MB) · {perf_tag} · {mem_str}"
            )
        elif owner == "research":
            self._model_lbl.set_text(
                f"Searching: {active_model} · {perf_tag} · {mem_str}"
            )
        elif owner == "transition":
            self._model_lbl.set_text(f"{active_model} · {perf_tag} · {mem_str}")
        else:
            self._model_lbl.set_text(
                f"All engines offline · {perf_tag} · {mem_str}"
            )
        self._model_dot.queue_draw()

        is_wan = "wan" in active_model.lower()
        is_pony = "pony" in active_model.lower()
        states = {
            "text": owner == "text",
            "pony": owner == "media" and (is_pony or not is_wan),
            "wan": owner == "media" and is_wan,
            "research": owner == "research",
        }
        for key, badge in self._model_badges.items():
            active = states.get(key, False)
            badge.remove_css_class("model-status-badge-active")
            badge.remove_css_class("model-status-badge-standby")
            if active:
                badge.add_css_class("model-status-badge-active")
                badge.set_text("Active in RAM")
            else:
                badge.add_css_class("model-status-badge-standby")
                badge.set_text("Standby (0 MB)")

    def _build_skill_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_size_request(264, -1)
        self._skill_pop = pop

        for name, desc in AVAILABLE_SKILLS:
            btn = Gtk.Button()
            btn.set_has_frame(False)
            btn.add_css_class("popover-item")
            if name == self._active_skill:
                btn.add_css_class("active")
            btn.set_cursor_from_name("pointer")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t = Gtk.Label(label=name)
            t.set_xalign(0.0)
            d = Gtk.Label(label=desc)
            d.add_css_class("session-row-meta")
            d.set_xalign(0.0)
            d.set_wrap(True)
            inner.append(t)
            inner.append(d)
            btn.set_child(inner)
            btn.connect("clicked", lambda _b, s=name: self._select_skill(s))
            box.append(btn)

        pop.set_child(box)
        return pop

    def _select_skill(self, skill: str) -> None:
        self.set_active_skill(skill)
        self._skill_pop.popdown()

    def set_active_skill(self, skill: str) -> None:
        self._active_skill = skill
        self._skill_lbl.set_text(skill)
        if self._on_skill_changed is not None:
            self._on_skill_changed(skill)

    def set_batch_count(self, count: int) -> None:
        self._batch_count = count
        for n, b in self._batch_buttons.items():
            if n == count:
                b.add_css_class("selected")
            else:
                b.remove_css_class("selected")
        if self._on_batch_changed is not None:
            self._on_batch_changed(count)

    def set_clip_seconds(self, seconds: int) -> None:
        self._clip_seconds = seconds
        for s, b in self._clip_buttons.items():
            if s == seconds:
                b.add_css_class("selected")
            else:
                b.remove_css_class("selected")

    def update_video_durations(self, durations: list[int], reason: str = "") -> None:
        """Rebuild the Video duration pills to match current hardware & selected video model."""
        valid = [int(d) for d in durations if int(d) > 0] or [4, 8, 12]
        self._clip_durations = valid
        child = self._clip_group.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._clip_group.remove(child)
            child = nxt
        self._clip_buttons.clear()

        if self._clip_seconds not in valid:
            self._clip_seconds = min(valid, key=lambda d: abs(d - self._clip_seconds))

        c_pre = Gtk.Label(label="Video")
        c_pre.add_css_class("selector-prefix")
        if reason:
            c_pre.set_tooltip_text(reason)
        self._clip_group.append(c_pre)
        for sec in valid:
            btn = Gtk.Button(label=f"{sec}s")
            btn.set_has_frame(False)
            btn.add_css_class("selector-option")
            if sec == self._clip_seconds:
                btn.add_css_class("selected")
            btn.set_cursor_from_name("pointer")
            tip = f"Video clip length: {sec}s"
            if reason:
                tip = f"{tip} · {reason}"
            btn.set_tooltip_text(tip)
            btn.connect("clicked", lambda _b, s=sec: self.set_clip_seconds(s))
            self._clip_buttons[sec] = btn
            self._clip_group.append(btn)
        if reason:
            self._clip_group.set_tooltip_text(reason)

    def get_steps_override(self) -> int | None:
        return self._steps_override

    def _hide_steps_warning(self) -> None:
        if self._steps_warn_dismiss_id is not None:
            GLib.source_remove(self._steps_warn_dismiss_id)
            self._steps_warn_dismiss_id = None
        if hasattr(self, "_steps_warn_popover") and self._steps_warn_popover.get_visible():
            self._steps_warn_popover.popdown()

    def _show_steps_warning(self, steps: int) -> None:
        if self._steps_warn_dismiss_id is not None:
            GLib.source_remove(self._steps_warn_dismiss_id)
            self._steps_warn_dismiss_id = None
        self._steps_warn_title.set_text(f"Custom Steps: {steps}")
        self._steps_warn_body.set_text(
            f"Running {steps} custom steps — the process will take longer than boosted Auto."
        )
        if self._steps_entry.get_realized() or self.get_realized():
            self._steps_warn_popover.popup()
        self._steps_warn_dismiss_id = GLib.timeout_add(
            4000, lambda: (self._hide_steps_warning(), False)[1]
        )

    def set_steps_override(self, steps: int | None, show_warning: bool = True) -> None:
        self._steps_syncing = True
        try:
            if steps is None or int(steps) <= 0:
                self._steps_override = None
                self._steps_entry.set_text("")
                self._steps_entry.remove_css_class("custom-active")
                self._steps_auto_btn.add_css_class("selected")
                self._hide_steps_warning()
            else:
                val = max(1, min(60, int(steps)))
                self._steps_override = val
                self._steps_entry.set_text(str(val))
                self._steps_entry.add_css_class("custom-active")
                self._steps_auto_btn.remove_css_class("selected")
                if show_warning:
                    self._show_steps_warning(val)
        finally:
            self._steps_syncing = False

    def _on_steps_entry_changed(self, entry: Gtk.Entry) -> None:
        if self._steps_syncing:
            return
        raw = entry.get_text().strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            self._steps_override = None
            entry.remove_css_class("custom-active")
            self._steps_auto_btn.add_css_class("selected")
            self._hide_steps_warning()
            if raw:
                def _clear_later() -> bool:
                    self._steps_syncing = True
                    try:
                        entry.set_text("")
                    finally:
                        self._steps_syncing = False
                    return False

                GLib.idle_add(_clear_later)
            return
        val = max(1, min(60, int(digits)))
        target_txt = str(val)
        if target_txt != raw:
            def _sync_later(txt: str = target_txt) -> bool:
                self._steps_syncing = True
                try:
                    if entry.get_text() != txt:
                        entry.set_text(txt)
                        entry.set_position(-1)
                finally:
                    self._steps_syncing = False
                return False

            GLib.idle_add(_sync_later)
        self._steps_override = val
        self._steps_auto_btn.remove_css_class("selected")
        entry.add_css_class("custom-active")
        self._show_steps_warning(val)

    def set_busy(self, busy: bool) -> None:
        self._is_busy = busy
        self._sync_send_button()

    def focus_input(self) -> None:
        self._textview.grab_focus()

    def get_text(self) -> str:
        buf = self._textview.get_buffer()
        s, e = buf.get_bounds()
        return buf.get_text(s, e, False)

    def _draw_aquarium(
        self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int
    ) -> None:
        if self._aq_alpha <= 0.01:
            return
        if self._aq_alpha >= 0.99:
            self._aquarium.draw(cr, w, h)
        else:
            cr.push_group()
            self._aquarium.draw(cr, w, h)
            cr.pop_group_to_source()
            cr.paint_with_alpha(self._aq_alpha)

    def _on_aquarium_tick(self, _widget: Gtk.Widget, _clock: Gdk.FrameClock) -> bool:
        if self._aq_alpha <= 0.01 and not self._aq_mode:
            self._aq_tick_id = None
            return GLib.SOURCE_REMOVE
        self._aquarium.step()
        self._aq_canvas.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _on_aquarium_unrealize(self, _widget: Gtk.Widget) -> None:
        if self._aq_tick_id is not None:
            self._aq_canvas.remove_tick_callback(self._aq_tick_id)
            self._aq_tick_id = None
        if self._dissolve_timer is not None:
            GLib.source_remove(self._dissolve_timer)
            self._dissolve_timer = None

    @property
    def is_aquarium_mode(self) -> bool:
        return self._aq_mode

    def set_aquarium_mode(self, enabled: bool, animate: bool = True) -> None:
        if self._dissolve_timer is not None:
            GLib.source_remove(self._dissolve_timer)
            self._dissolve_timer = None
        self._aq_mode = enabled

        if enabled:
            self._aq_alpha = 1.0
            self._tank.add_css_class("aquarium-tank")
            self._pill.add_css_class("aquarium-glass")
            self._aq_canvas.set_visible(True)
            self._aq_canvas.set_content_height(182)
            self._tank_inner.set_margin_top(22)
            self._tank_inner.set_margin_bottom(10)
            self._tank_inner.set_margin_start(16)
            self._tank_inner.set_margin_end(16)
            self._aq_caption.set_visible(True)
            self._aq_caption.set_opacity(1.0)
            self._skill_icon.set_icon("layers", "#0B4757")
            self._skill_chev.set_icon("chevron-down", "#0B4757")
            if self._aq_tick_id is None:
                self._aq_tick_id = self._aq_canvas.add_tick_callback(self._on_aquarium_tick)
            self._sync_send_button()
            self._aq_canvas.queue_draw()
            return

        if not animate:
            self._aq_alpha = 0.0
            self._tank.remove_css_class("aquarium-tank")
            self._pill.remove_css_class("aquarium-glass")
            self._aq_canvas.set_visible(False)
            self._aq_canvas.set_content_height(0)
            self._tank_inner.set_margin_top(0)
            self._tank_inner.set_margin_bottom(0)
            self._tank_inner.set_margin_start(0)
            self._tank_inner.set_margin_end(0)
            self._aq_caption.set_visible(False)
            self._aq_caption.set_opacity(0.0)
            self._skill_icon.set_icon("layers", COLORS.text_secondary)
            self._skill_chev.set_icon("chevron-down", COLORS.text_secondary)
            self._sync_send_button()
            return

        # Smooth ~560ms dissolve after user sends a message
        self._dissolve_start = time.monotonic()
        switched_glass = False

        def _step_dissolve() -> bool:
            nonlocal switched_glass
            dur = 0.56
            t = min(1.0, (time.monotonic() - self._dissolve_start) / dur)
            ease = 1.0 - (1.0 - t) ** 3
            self._aq_alpha = max(0.0, 1.0 - ease)
            self._aq_canvas.set_content_height(max(0, int(round(182 * (1.0 - ease)))))
            self._tank_inner.set_margin_top(int(round(22 * (1.0 - ease))))
            self._tank_inner.set_margin_bottom(int(round(10 * (1.0 - ease))))
            self._tank_inner.set_margin_start(int(round(16 * (1.0 - ease))))
            self._tank_inner.set_margin_end(int(round(16 * (1.0 - ease))))
            self._aq_caption.set_opacity(max(0.0, 1.0 - ease * 1.6))

            if not switched_glass and t >= 0.32:
                switched_glass = True
                self._tank.remove_css_class("aquarium-tank")
                self._pill.remove_css_class("aquarium-glass")
                self._skill_icon.set_icon("layers", COLORS.text_secondary)
                self._skill_chev.set_icon("chevron-down", COLORS.text_secondary)
                self._sync_send_button()

            self._aq_canvas.queue_draw()
            if t >= 1.0:
                self._dissolve_timer = None
                self._aq_alpha = 0.0
                self._aq_canvas.set_visible(False)
                self._aq_caption.set_visible(False)
                return False
            return True

        self._dissolve_timer = GLib.timeout_add(16, _step_dissolve)

    def _sync_composer_query_blur(self) -> None:
        if not hasattr(self, "_input_blur_layer"):
            return
        is_sens = is_explicit_text(self.get_text())
        blurred = bool(is_sens and not self._textview.has_focus())
        self._input_blur_layer.set_blurred(blurred)
        self._input_blur_scrim.set_visible(blurred)
        self._input_blur_badge.set_visible(blurred)

    def _on_text_changed(self, buf: Gtk.TextBuffer) -> None:
        has_text = bool(self.get_text().strip())
        self._placeholder.set_visible(not bool(self.get_text()))
        self._sync_send_button(has_text=has_text)
        self._sync_composer_query_blur()
        if self._aq_mode:
            it = buf.get_iter_at_mark(buf.get_insert())
            rect = self._textview.get_iter_location(it)
            bx, by = self._textview.buffer_to_window_coords(
                Gtk.TextWindowType.WIDGET, rect.x, rect.y + rect.height * 0.5
            )
            res = self._textview.translate_coordinates(self._tank, float(bx), float(by))
            if res is not None:
                self._aquarium.notify_typing(res[0], res[1])

    def _sync_send_button(self, has_text: bool | None = None) -> None:
        if has_text is None:
            has_text = bool(self.get_text().strip())
        self._send_btn.remove_css_class("disabled")
        self._send_btn.remove_css_class("stop")

        if hasattr(self, "_stop_btn"):
            if self._is_busy:
                self._stop_btn.add_css_class("active")
                self._stop_btn.set_sensitive(True)
                self._stop_icon.set_icon("stop", "#FFFFFF")
            else:
                self._stop_btn.remove_css_class("active")
                self._stop_btn.set_sensitive(False)
                self._stop_icon.set_icon("stop", COLORS.text_muted)

        if self._is_busy:
            if has_text:
                self._send_icon.set_icon("arrow-up", "#121212")
                self._send_btn.set_tooltip_text("Queue message (Enter)")
            else:
                self._send_btn.add_css_class("disabled")
                self._send_icon.set_icon("arrow-up", COLORS.text_muted)
                self._send_btn.set_tooltip_text("Type a message to queue, or click Stop")
        elif has_text:
            self._send_icon.set_icon("arrow-up", "#121212")
            self._send_btn.set_tooltip_text("Send (Enter)")
        else:
            self._send_btn.add_css_class("disabled")
            self._send_icon.set_icon("arrow-up", COLORS.text_muted)
            self._send_btn.set_tooltip_text("Type a message to send")

    def _on_key_pressed(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape and is_explicit_text(self.get_text()):
            root = self.get_root()
            if root is not None and hasattr(root, "set_focus"):
                root.set_focus(None)
                return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                return False
            self._submit_current()
            return True
        return False

    def _on_send_or_stop(self, _btn: Gtk.Button) -> None:
        self._submit_current()

    def set_locked_mode(self, mode: str | None) -> None:
        """Lock this chat composer to Make Images, Make Video, or open (None)."""
        self._locked_mode = mode if mode in ("Make Images", "Make Video") else None
        if self._locked_mode == "Make Images":
            self.set_active_skill("Make Images")
            self._skill_lbl.set_text("Images Only (Locked)")
            self._skill_btn.set_sensitive(False)
            self._skill_btn.set_tooltip_text("Locked to Make Images for this chat (+ New for Video/Chat)")
            self._div2.set_visible(False)
            self._clip_group.set_visible(False)
        elif self._locked_mode == "Make Video":
            self.set_active_skill("Make Video")
            self._skill_lbl.set_text("Video Mode (Locked)")
            self._skill_btn.set_sensitive(False)
            self._skill_btn.set_tooltip_text("Locked to Make Video for this chat (+ New for Images/Chat)")
            self._div2.set_visible(True)
            self._clip_group.set_visible(True)
        else:
            self._skill_btn.set_sensitive(True)
            self._skill_lbl.set_text(self._active_skill)
            self._skill_btn.set_tooltip_text("Switch active mode")
            self._div2.set_visible(True)
            self._clip_group.set_visible(True)

    def render_queue(
        self,
        items: list[RenderPlaylistJob] | list[dict[str, object]],
        on_remove: Callable[[int], None],
        on_clear: Callable[[], None],
        on_run_next: Callable[[], None] | None = None,
        on_move: Callable[[int, int], None] | None = None,
        active_job: RenderPlaylistJob | None = None,
        done_count: int = 0,
        total_count: int = 0,
        ram_text: str = "",
        auto_clean_ram: bool = True,
        on_toggle_auto_clean: Callable[[], None] | None = None,
        quiet_alerts: bool = True,
        on_toggle_quiet_alerts: Callable[[], None] | None = None,
    ) -> None:
        c = self._queue_box.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._queue_box.remove(c)
            c = nxt
        if not items and active_job is None:
            self._queue_box.set_visible(False)
            return

        tot = max(total_count, done_count + (1 if active_job else 0) + len(items))
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ql = Gtk.Label(
            label=f"Render Playlist · {done_count}/{tot} done"
            if tot > 0
            else f"Render Playlist ({len(items)})"
        )
        ql.add_css_class("research-tab-title")
        ql.set_xalign(0.0)
        hdr.append(ql)

        if ram_text:
            rl = Gtk.Label(label=ram_text)
            rl.add_css_class("research-tab-url")
            rl.set_ellipsize(Pango.EllipsizeMode.END)
            rl.set_hexpand(True)
            rl.set_xalign(0.0)
            hdr.append(rl)
        else:
            spacer = Gtk.Box()
            spacer.set_hexpand(True)
            hdr.append(spacer)

        if on_toggle_auto_clean is not None:
            ac = Gtk.Button(label=f"Auto-Clean RAM: {'On' if auto_clean_ram else 'Off'}")
            ac.set_has_frame(False)
            ac.add_css_class("playlist-toggle-chip")
            if auto_clean_ram:
                ac.add_css_class("active")
            ac.set_cursor_from_name("pointer")
            ac.set_tooltip_text("Unload LLM (0 GB) & sweep unnecessary processes between queued jobs")
            ac.connect("clicked", lambda _b: on_toggle_auto_clean())
            hdr.append(ac)

        if on_toggle_quiet_alerts is not None:
            qa = Gtk.Button(label=f"Batch Alert: {'Single' if quiet_alerts else 'Each'}")
            qa.set_has_frame(False)
            qa.add_css_class("playlist-toggle-chip")
            if quiet_alerts:
                qa.add_css_class("active")
            qa.set_cursor_from_name("pointer")
            qa.set_tooltip_text("Suppress per-job spam and send one summary notification at completion")
            qa.connect("clicked", lambda _b: on_toggle_quiet_alerts())
            hdr.append(qa)

        if items and on_run_next is not None and not self._is_busy:
            rb = Gtk.Button(label="Run Playlist")
            rb.set_has_frame(False)
            rb.add_css_class("mini-action-btn")
            rb.set_cursor_from_name("pointer")
            rb.connect("clicked", lambda _b: on_run_next())
            hdr.append(rb)

        if items:
            cb = Gtk.Button(label="Clear")
            cb.set_has_frame(False)
            cb.add_css_class("mini-action-btn")
            cb.set_cursor_from_name("pointer")
            cb.connect("clicked", lambda _b: on_clear())
            hdr.append(cb)
        self._queue_box.append(hdr)

        if tot > 0:
            pbar = Gtk.ProgressBar()
            pbar.add_css_class("playlist-progress")
            frac = min(1.0, (done_count + (0.45 if active_job else 0.0)) / max(1, tot))
            pbar.set_fraction(frac)
            self._queue_box.append(pbar)

        def _build_job_row(job: RenderPlaylistJob, display_idx: int, q_idx: int | None, is_running: bool) -> Gtk.Box:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.add_css_class("playlist-job-card")
            if is_running:
                row.add_css_class("active")

            p_txt = re.sub(r"\s+", " ", job.prompt).strip()
            lbl = Gtk.Label(label=f"{display_idx}. {p_txt}")
            lbl.add_css_class("composer-quote-text")
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_hexpand(True)
            lbl.set_xalign(0.0)
            q_blur = SafeBlurBox(
                lbl,
                explicit=is_explicit_text(p_txt),
                safe_blur_enabled=True,
                blur_radius=28.0,
                compact=True,
            )
            q_blur.set_hexpand(True)
            row.append(q_blur)

            m_pill = Gtk.Label(label=f"{job.mode} · {job.model}")
            m_pill.add_css_class("ref-palette-pill")
            row.append(m_pill)

            st_lbl = Gtk.Label(label=job.status)
            st_lbl.add_css_class("playlist-status-pill")
            st_low = job.status.lower()
            if "render" in st_low:
                st_lbl.add_css_class("rendering")
            elif "check" in st_low or "cooldown" in st_low or "clean" in st_low:
                st_lbl.add_css_class("checking")
            elif "done" in st_low:
                st_lbl.add_css_class("done")
            elif "fail" in st_low or "cancel" in st_low:
                st_lbl.add_css_class("failed")
            row.append(st_lbl)

            if q_idx is not None:
                if on_move is not None and len(items) > 1:
                    up_b = Gtk.Button(label="↑")
                    up_b.set_has_frame(False)
                    up_b.add_css_class("mini-action-btn")
                    up_b.set_sensitive(q_idx > 0)
                    up_b.set_cursor_from_name("pointer")
                    up_b.connect("clicked", lambda _b, i=q_idx: on_move(i, -1))
                    row.append(up_b)

                    dn_b = Gtk.Button(label="↓")
                    dn_b.set_has_frame(False)
                    dn_b.add_css_class("mini-action-btn")
                    dn_b.set_sensitive(q_idx < len(items) - 1)
                    dn_b.set_cursor_from_name("pointer")
                    dn_b.connect("clicked", lambda _b, i=q_idx: on_move(i, 1))
                    row.append(dn_b)

                rm = Gtk.Button(label="×")
                rm.set_has_frame(False)
                rm.add_css_class("mini-action-btn")
                rm.set_cursor_from_name("pointer")
                rm.set_tooltip_text("Cancel job")
                rm.connect("clicked", lambda _b, i=q_idx: on_remove(i))
                row.append(rm)
            return row

        scroll_list = Gtk.ScrolledWindow()
        scroll_list.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll_list.set_propagate_natural_height(True)
        scroll_list.set_max_content_height(150)

        cards_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        idx_offset = 1
        if active_job is not None:
            cards_col.append(_build_job_row(active_job, 1, None, True))
            idx_offset = 2

        for q_i, raw_it in enumerate(items):
            if isinstance(raw_it, RenderPlaylistJob):
                job = raw_it
            else:
                sk = str(raw_it.get("skill", "Make Images"))
                job = RenderPlaylistJob(
                    id=str(raw_it.get("id", f"j-{q_i}")),
                    chat_id=str(raw_it.get("chat_id", "")),
                    prompt=str(raw_it.get("prompt", "")),
                    skill=sk,
                    mode="Video" if "video" in sk.lower() else ("Research" if "search" in sk.lower() else "Image"),
                    model="Wan 2.1" if "video" in sk.lower() else "Pony V6 XL",
                    batch_count=int(raw_it.get("batch", 4)),
                    clip_seconds=int(raw_it.get("clip", 4)),
                )
            cards_col.append(_build_job_row(job, q_i + idx_offset, q_i, False))

        scroll_list.set_child(cards_col)
        self._queue_box.append(scroll_list)
        self._queue_box.set_visible(True)

    def _build_camera_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_size_request(210, -1)
        self._camera_pop = pop
        self._camera_pop_btns: dict[str, Gtk.Button] = {}
        for ang in CAMERA_ANGLE_OPTIONS:
            btn = Gtk.Button(label=ang)
            btn.set_has_frame(False)
            btn.add_css_class("popover-item")
            if ang == self._camera_angle:
                btn.add_css_class("active")
            btn.set_cursor_from_name("pointer")
            btn.connect("clicked", lambda _b, a=ang: self._select_camera_angle(a))
            self._camera_pop_btns[ang] = btn
            box.append(btn)
        pop.set_child(box)
        return pop

    def _select_camera_angle(self, angle: str) -> None:
        self.set_camera_angle(angle)
        self._camera_pop.popdown()

    def set_camera_angle(self, angle: str) -> None:
        self._camera_angle = angle if angle in CAMERA_ANGLE_OPTIONS else "Auto"
        self._camera_lbl.set_text(f"Cam: {self._camera_angle}")
        for a, b in getattr(self, "_camera_pop_btns", {}).items():
            if a == self._camera_angle:
                b.add_css_class("active")
            else:
                b.remove_css_class("active")

    def get_camera_angle(self) -> str:
        return self._camera_angle

    def set_quoted_text(self, snippet: str) -> None:
        cleaned = re.sub(r"\s+", " ", snippet or "").strip()
        if not cleaned:
            return
        self._quoted_text = cleaned
        preview = cleaned if len(cleaned) <= 130 else cleaned[:127].rstrip() + "…"
        self._quote_lbl.set_text(f'↳ "{preview}"')
        if hasattr(self, "_quote_blur"):
            self._quote_blur.set_explicit(is_explicit_text(cleaned))
        self._quote_bar.set_visible(True)
        self._textview.grab_focus()

    def clear_quoted_text(self) -> None:
        self._quoted_text = ""
        self._quote_lbl.set_text("")
        if hasattr(self, "_quote_blur"):
            self._quote_blur.set_explicit(False)
        self._quote_bar.set_visible(False)

    def _submit_current(self) -> None:
        text = self.get_text().strip()
        if not text:
            return
        if self._quoted_text:
            text = f'> "{self._quoted_text}"\n\n{text}'
            self.clear_quoted_text()
        if getattr(self, "_camera_angle", "Auto") != "Auto":
            text = f"{text} [Camera framing: {self._camera_angle}]"
        if self._aq_mode:
            res_aq = self._send_btn.translate_coordinates(self._tank, 15.0, 15.0)
            if res_aq is not None:
                self._aquarium.trigger_gulp(res_aq[0], res_aq[1])
        res = self._send_btn.translate_coordinates(self._electric_wrap, 15.0, 15.0)
        if res is not None:
            self._electric_border.trigger_pulse(res[0], res[1])
        self._textview.get_buffer().set_text("")
        steps_ov = self._steps_override
        self._hide_steps_warning()
        cb = self._on_queue if (self._is_busy and self._on_queue is not None) else self._on_submit
        import inspect
        try:
            sig = inspect.signature(cb)
            accepts_steps = len(sig.parameters) >= 5 or any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
            )
        except (TypeError, ValueError):
            accepts_steps = True
        if accepts_steps:
            cb(text, self._active_skill, self._batch_count, self._clip_seconds, steps_ov)
        else:
            cb(text, self._active_skill, self._batch_count, self._clip_seconds)


# ---------------------------------------------------------------------------
# Full-Screen Lightbox Inspector
# ---------------------------------------------------------------------------

class LightboxInspector(Gtk.Revealer):
    """Modal full-window media inspector with zoom, pan, scrubbing, and details."""

    def __init__(self, on_toast: Callable[[str], None]) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.set_transition_duration(200)
        self.set_reveal_child(False)
        self.set_visible(False)

        self._on_toast = on_toast
        self._mode: str = "keyframe"
        self._keyframes: list[KeyframeSpec] = []
        self._kf_index: int = 0
        self._references: list[ReferenceCandidate] = []
        self._ref_index: int = 0
        self._ref_surfaces: dict[str, cairo.ImageSurface] = {}
        self._clip: VideoClipSpec | None = None
        self._decoder: VideoFrameDecoder | None = None
        self._t_norm: float = 0.0
        self._playing: bool = False
        self._tick_id: int | None = None
        self._last_tick_us: int = 0

        self._zoom: float = 1.0
        self._pan_x: float = 0.0
        self._pan_y: float = 0.0
        self._drag_origin_pan = (0.0, 0.0)
        self._drawer_open = True

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("lightbox-scrim")
        root.set_hexpand(True)
        root.set_vexpand(True)

        topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        topbar.add_css_class("lightbox-topbar")

        self._title_lbl = Gtk.Label(label="Inspector")
        self._title_lbl.add_css_class("header-session-title")
        self._title_lbl.set_xalign(0.0)
        self._title_lbl.set_hexpand(True)
        topbar.append(self._title_lbl)

        z_out = Gtk.Button()
        z_out.set_has_frame(False)
        z_out.add_css_class("lightbox-pill-btn")
        z_out.set_cursor_from_name("pointer")
        z_out.set_child(SvgIcon("zoom-out", size_px=14, color=COLORS.text_secondary))
        z_out.connect("clicked", lambda _b: self.adjust_zoom(-0.35))
        topbar.append(z_out)

        self._zoom_lbl = Gtk.Button(label="100%")
        self._zoom_lbl.set_has_frame(False)
        self._zoom_lbl.add_css_class("lightbox-pill-btn")
        self._zoom_lbl.set_cursor_from_name("pointer")
        self._zoom_lbl.set_tooltip_text("Reset zoom (0)")
        self._zoom_lbl.connect("clicked", lambda _b: self.reset_view())
        topbar.append(self._zoom_lbl)

        z_in = Gtk.Button()
        z_in.set_has_frame(False)
        z_in.add_css_class("lightbox-pill-btn")
        z_in.set_cursor_from_name("pointer")
        z_in.set_child(SvgIcon("zoom-in", size_px=14, color=COLORS.text_secondary))
        z_in.connect("clicked", lambda _b: self.adjust_zoom(0.35))
        topbar.append(z_in)

        self._info_btn = Gtk.Button()
        self._info_btn.set_has_frame(False)
        self._info_btn.add_css_class("lightbox-pill-btn")
        self._info_btn.add_css_class("active")
        self._info_btn.set_cursor_from_name("pointer")
        self._info_btn.set_tooltip_text("Toggle Details (I)")
        info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        info_box.append(SvgIcon("info", size_px=14, color=COLORS.text_primary))
        info_box.append(Gtk.Label(label="Details"))
        self._info_btn.set_child(info_box)
        self._info_btn.connect("clicked", lambda _b: self.toggle_drawer())
        topbar.append(self._info_btn)

        close_btn = Gtk.Button()
        close_btn.set_has_frame(False)
        close_btn.add_css_class("lightbox-pill-btn")
        close_btn.set_cursor_from_name("pointer")
        close_btn.set_tooltip_text("Close (Esc)")
        close_btn.set_child(SvgIcon("close", size_px=14, color=COLORS.text_primary))
        close_btn.connect("clicked", lambda _b: self.close())
        topbar.append(close_btn)
        root.append(topbar)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_vexpand(True)
        body.set_hexpand(True)

        stage_overlay = Gtk.Overlay()
        stage_overlay.set_hexpand(True)
        stage_overlay.set_vexpand(True)

        self._stage = Gtk.DrawingArea()
        self._stage.set_hexpand(True)
        self._stage.set_vexpand(True)
        self._stage.set_draw_func(self._draw_stage)

        scroll_ctrl = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_ctrl.connect("scroll", self._on_scroll_zoom)
        self._stage.add_controller(scroll_ctrl)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_pan_begin)
        drag.connect("drag-update", self._on_pan_update)
        self._stage.add_controller(drag)

        dbl = Gtk.GestureClick()
        dbl.connect("released", self._on_stage_click)
        self._stage.add_controller(dbl)

        self._stage_blur = SafeBlurBox(
            self._stage,
            explicit=False,
            safe_blur_enabled=True,
            blur_radius=44.0,
            show_hover_toggle=True,
            require_slide_confirm=True,
            hover_position=Gtk.PositionType.BOTTOM,
        )
        self._stage_blur.set_hexpand(True)
        self._stage_blur.set_vexpand(True)
        stage_overlay.set_child(self._stage_blur)

        self._prev_btn = Gtk.Button()
        self._prev_btn.set_has_frame(False)
        self._prev_btn.add_css_class("video-play-circle")
        self._prev_btn.set_halign(Gtk.Align.START)
        self._prev_btn.set_valign(Gtk.Align.CENTER)
        self._prev_btn.set_margin_start(16)
        self._prev_btn.set_cursor_from_name("pointer")
        self._prev_btn.set_child(SvgIcon("chevron-left", size_px=18, color=COLORS.text_primary))
        self._prev_btn.connect("clicked", lambda _b: self.navigate(-1))
        stage_overlay.add_overlay(self._prev_btn)

        self._next_btn = Gtk.Button()
        self._next_btn.set_has_frame(False)
        self._next_btn.add_css_class("video-play-circle")
        self._next_btn.set_halign(Gtk.Align.END)
        self._next_btn.set_valign(Gtk.Align.CENTER)
        self._next_btn.set_margin_end(16)
        self._next_btn.set_cursor_from_name("pointer")
        self._next_btn.set_child(SvgIcon("chevron-right", size_px=18, color=COLORS.text_primary))
        self._next_btn.connect("clicked", lambda _b: self.navigate(1))
        stage_overlay.add_overlay(self._next_btn)

        self._bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._bottom_bar.add_css_class("lightbox-bottom-controls")
        self._bottom_bar.set_halign(Gtk.Align.CENTER)
        self._bottom_bar.set_valign(Gtk.Align.END)
        stage_overlay.add_overlay(self._bottom_bar)

        body.append(stage_overlay)

        self._drawer_rev = Gtk.Revealer()
        self._drawer_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self._drawer_rev.set_transition_duration(200)
        self._drawer_rev.set_reveal_child(True)

        self._drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._drawer_box.add_css_class("lightbox-drawer")
        self._drawer_rev.set_child(self._drawer_box)
        body.append(self._drawer_rev)

        root.append(body)
        self.set_child(root)

        self.connect("notify::child-revealed", self._on_revealed_changed)

    def open_keyframes(self, keyframes: list[KeyframeSpec], index: int, blurred: bool = False) -> None:
        self._stop_playback()
        self._mode = "keyframe"
        self._keyframes = keyframes
        self._kf_index = max(0, min(len(keyframes) - 1, index))
        self._stage_blur.set_explicit(blurred)
        self._prev_btn.set_visible(len(keyframes) > 1)
        self._next_btn.set_visible(len(keyframes) > 1)
        self.reset_view()
        self._refresh_content()
        self.set_visible(True)
        self.set_reveal_child(True)

    def open_video(self, clip: VideoClipSpec, start_t_norm: float = 0.0, blurred: bool = False) -> None:
        self._stop_playback()
        if self._decoder is not None:
            self._decoder.close()
        self._decoder = VideoFrameDecoder(clip.video_path)
        self._mode = "video"
        self._clip = clip
        self._t_norm = start_t_norm
        self._stage_blur.set_explicit(blurred)
        self._prev_btn.set_visible(False)
        self._next_btn.set_visible(False)
        self.reset_view()
        self._refresh_content()
        self.set_visible(True)
        self.set_reveal_child(True)

    def open_reference(
        self,
        references: list[ReferenceCandidate],
        index: int = 0,
        blurred: bool = False,
    ) -> None:
        """Open a RAM-resident web reference in-app at full scale without touching browser history."""
        if not references:
            return
        self._stop_playback()
        if self._decoder is not None:
            self._decoder.close()
            self._decoder = None
        self._mode = "reference"
        self._references = list(references)
        self._ref_index = max(0, min(len(self._references) - 1, index))
        self._stage_blur.set_explicit(blurred)
        self._prev_btn.set_visible(len(self._references) > 1)
        self._next_btn.set_visible(len(self._references) > 1)
        self.reset_view()
        self._refresh_content()
        self.set_visible(True)
        self.set_reveal_child(True)

    def close(self) -> None:
        self._stop_playback()
        if self._decoder is not None:
            self._decoder.close()
            self._decoder = None
        self.set_reveal_child(False)

    def _on_revealed_changed(self, *_args) -> None:
        if not self.get_reveal_child() and not self.get_child_revealed():
            self.set_visible(False)

    def navigate(self, delta: int) -> None:
        if self._mode == "keyframe" and self._keyframes:
            self._kf_index = (self._kf_index + delta) % len(self._keyframes)
            self.reset_view()
            self._refresh_content()
        elif self._mode == "reference" and self._references:
            self._ref_index = (self._ref_index + delta) % len(self._references)
            self.reset_view()
            self._refresh_content()
        elif self._mode == "video" and self._clip:
            total = int(self._clip.duration_s * self._clip.fps)
            frame = max(0, min(total - 1, round(self._t_norm * total) + delta))
            self._t_norm = frame / max(1, total)
            self._stage.queue_draw()
            self._refresh_video_bar()

    def toggle_drawer(self) -> None:
        self._drawer_open = not self._drawer_open
        self._drawer_rev.set_reveal_child(self._drawer_open)
        if self._drawer_open:
            self._info_btn.add_css_class("active")
        else:
            self._info_btn.remove_css_class("active")

    def adjust_zoom(self, delta: float) -> None:
        self._zoom = max(1.0, min(4.0, self._zoom + delta))
        if self._zoom <= 1.01:
            self._zoom = 1.0
            self._pan_x = 0.0
            self._pan_y = 0.0
        self._zoom_lbl.set_label(f"{int(self._zoom * 100)}%")
        self._stage.queue_draw()

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._zoom_lbl.set_label("100%")
        self._stage.queue_draw()

    def toggle_video_play(self) -> None:
        if self._mode != "video" or self._clip is None:
            return
        self._playing = not self._playing
        if self._playing:
            self._last_tick_us = 0
            if self._tick_id is None:
                self._tick_id = self.add_tick_callback(self._on_tick)
        else:
            self._stop_playback()
        self._refresh_video_bar()

    def _stop_playback(self) -> None:
        self._playing = False
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def _on_tick(self, _w: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        if self._clip is None or not self._playing:
            return False
        now = clock.get_frame_time()
        if self._last_tick_us == 0:
            self._last_tick_us = now
            return True
        dt = (now - self._last_tick_us) / 1_000_000.0
        self._last_tick_us = now
        self._t_norm = (self._t_norm + dt / self._clip.duration_s) % 1.0
        self._stage.queue_draw()
        self._refresh_video_bar()
        return True

    def _refresh_content(self) -> None:
        for container in (self._bottom_bar, self._drawer_box):
            c = container.get_first_child()
            while c is not None:
                nxt = c.get_next_sibling()
                container.remove(c)
                c = nxt

        if self._mode == "keyframe" and self._keyframes:
            kf = self._keyframes[self._kf_index]
            self._title_lbl.set_text(f"Image {kf.code} · {kf.title}")
            self._populate_keyframe_drawer(kf)
            self._populate_keyframe_strip()
        elif self._mode == "reference" and self._references:
            ref = self._references[self._ref_index]
            self._title_lbl.set_text(f"Ref {self._ref_index + 1} · {ref.title or 'Web Reference'}")
            self._populate_reference_drawer(ref)
            self._populate_reference_strip()
        elif self._mode == "video" and self._clip:
            c = self._clip
            self._title_lbl.set_text(f"Video · {c.title}")
            self._populate_video_drawer(c)
            self._build_video_bottom_bar()
            self._refresh_video_bar()
        self._stage.queue_draw()

    def _populate_keyframe_strip(self) -> None:
        for idx, kf in enumerate(self._keyframes):
            b = Gtk.Button(label=kf.code)
            b.set_has_frame(False)
            b.add_css_class("grid-layout-pill")
            if idx == self._kf_index:
                b.add_css_class("active")
            b.set_cursor_from_name("pointer")
            b.connect("clicked", lambda _btn, i=idx: self._jump_kf(i))
            self._bottom_bar.append(b)

    def _jump_kf(self, idx: int) -> None:
        self._kf_index = idx
        self.reset_view()
        self._refresh_content()

    def _populate_reference_strip(self) -> None:
        for idx, _rf in enumerate(self._references):
            b = Gtk.Button(label=f"Ref {idx + 1}")
            b.set_has_frame(False)
            b.add_css_class("grid-layout-pill")
            if idx == self._ref_index:
                b.add_css_class("active")
            b.set_cursor_from_name("pointer")
            b.connect("clicked", lambda _btn, i=idx: self._jump_ref(i))
            self._bottom_bar.append(b)

    def _jump_ref(self, idx: int) -> None:
        self._ref_index = idx
        self.reset_view()
        self._refresh_content()

    def _get_reference_surface(self, ref: ReferenceCandidate) -> cairo.ImageSurface | None:
        key = ref.id or ref.image_url or ref.title
        surf = self._ref_surfaces.get(key)
        if surf is None and ref.image_bytes_b64:
            surf = _cairo_surface_from_ram_b64(ref.image_bytes_b64)
            if surf is not None:
                self._ref_surfaces[key] = surf
        return surf

    def _populate_reference_drawer(self, ref: ReferenceCandidate) -> None:
        surf = self._get_reference_surface(ref)
        w = surf.get_width() if surf is not None else ref.width
        h = surf.get_height() if surf is not None else ref.height
        self._add_drawer_heading("IN-APP RAM PREVIEW")
        rows = [
            ("PRIVACY", "Opened in RAM · Zero browser history"),
            ("TITLE", ref.title or f"Reference {self._ref_index + 1}"),
            ("SIZE", f"{w} × {h}" if w and h else "Unknown"),
            ("LIGHTING", ref.lighting_mood or "Natural / ambient"),
            ("PALETTE", ", ".join(ref.palette_tags) or "Balanced"),
        ]
        if ref.style_tokens:
            rows.append(("STYLE TOKENS", ref.style_tokens))
        if ref.source_url:
            rows.append(("SOURCE URL (NOT OPENED)", ref.source_url))
        for k, v in rows:
            self._add_meta_row(k, v)
        if ref.source_url or ref.image_url:
            copy_url = Gtk.Button(label="Copy source link")
            copy_url.set_has_frame(False)
            copy_url.add_css_class("mini-action-btn")
            copy_url.set_cursor_from_name("pointer")
            copy_url.connect(
                "clicked",
                lambda _b: (
                    copy_to_clipboard(self, ref.source_url or ref.image_url),
                    self._on_toast("Link copied (not opened in browser)"),
                ),
            )
            self._drawer_box.append(copy_url)

    def _build_video_bottom_bar(self) -> None:
        self._lb_play_btn = Gtk.Button()
        self._lb_play_btn.set_has_frame(False)
        self._lb_play_btn.add_css_class("icon-btn")
        self._lb_play_btn.set_cursor_from_name("pointer")
        self._lb_play_icon = SvgIcon("play", size_px=15, color=COLORS.text_primary)
        self._lb_play_btn.set_child(self._lb_play_icon)
        self._lb_play_btn.connect("clicked", lambda _b: self.toggle_video_play())
        self._bottom_bar.append(self._lb_play_btn)

        back = Gtk.Button()
        back.set_has_frame(False)
        back.add_css_class("icon-btn")
        back.set_cursor_from_name("pointer")
        back.set_child(SvgIcon("step-back", size_px=13, color=COLORS.text_secondary))
        back.connect("clicked", lambda _b: self.navigate(-1))
        self._bottom_bar.append(back)

        fwd = Gtk.Button()
        fwd.set_has_frame(False)
        fwd.add_css_class("icon-btn")
        fwd.set_cursor_from_name("pointer")
        fwd.set_child(SvgIcon("step-fwd", size_px=13, color=COLORS.text_secondary))
        fwd.connect("clicked", lambda _b: self.navigate(1))
        self._bottom_bar.append(fwd)

        self._lb_scrub = Gtk.DrawingArea()
        self._lb_scrub.set_size_request(240, 22)
        self._lb_scrub.set_cursor_from_name("pointer")
        self._lb_scrub.set_draw_func(self._draw_lb_scrub)
        press = Gtk.GestureClick()
        press.connect("pressed", self._on_lb_scrub_press)
        self._lb_scrub.add_controller(press)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_lb_scrub_drag_begin)
        drag.connect("drag-update", self._on_lb_scrub_drag_update)
        self._lb_scrub.add_controller(drag)
        self._bottom_bar.append(self._lb_scrub)

        self._lb_tc = Gtk.Label()
        self._lb_tc.add_css_class("video-timecode")
        self._bottom_bar.append(self._lb_tc)

    def _refresh_video_bar(self) -> None:
        if self._mode != "video" or self._clip is None:
            return
        if hasattr(self, "_lb_play_icon"):
            self._lb_play_icon.set_icon("pause" if self._playing else "play", COLORS.text_primary)
        if hasattr(self, "_lb_scrub"):
            self._lb_scrub.queue_draw()
        if hasattr(self, "_lb_tc"):
            total = int(self._clip.duration_s * self._clip.fps)
            cur_s = self._t_norm * self._clip.duration_s
            cur_f = int(self._t_norm * total)
            self._lb_tc.set_text(f"{cur_s:05.2f}s / {self._clip.duration_s:04.2f}s · f{cur_f:03d}")

    def _on_lb_scrub_press(self, _g: Gtk.GestureClick, _n: int, x: float, _y: float) -> None:
        self._set_lb_t(x)

    def _on_lb_scrub_drag_begin(self, _g: Gtk.GestureDrag, x: float, _y: float) -> None:
        self._lb_drag_x = x
        self._set_lb_t(x)

    def _on_lb_scrub_drag_update(self, _g: Gtk.GestureDrag, dx: float, _dy: float) -> None:
        self._set_lb_t(self._lb_drag_x + dx)

    def _set_lb_t(self, x: float) -> None:
        w = max(1.0, float(self._lb_scrub.get_width()))
        self._t_norm = max(0.0, min(1.0, x / w))
        self._stage.queue_draw()
        self._refresh_video_bar()

    def _draw_lb_scrub(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        cy = h * 0.5
        pad = 4.0
        tw = max(10.0, float(w) - pad * 2.0)
        cr.set_source_rgba(0.93, 0.93, 0.93, 0.16)
        cr.set_line_width(3.0)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(pad, cy)
        cr.line_to(pad + tw, cy)
        cr.stroke()
        px = pad + tw * self._t_norm
        cr.set_source_rgba(0.93, 0.93, 0.93, 0.90)
        cr.move_to(pad, cy)
        cr.line_to(px, cy)
        cr.stroke()
        cr.set_source_rgb(0.93, 0.93, 0.93)
        cr.arc(px, cy, 4.5, 0, math.tau)
        cr.fill()

    def _populate_keyframe_drawer(self, kf: KeyframeSpec) -> None:
        self._add_drawer_heading("IMAGE DETAILS")
        rows = [
            ("IMAGE", kf.code),
            ("FRAMING", kf.shot_type),
            ("LENS LOOK", f"{kf.focal_length} · {kf.aperture}"),
            ("MOTION", kf.camera_move),
            ("SIZE", "1920 × 804"),
            ("SEED", str(kf.seed)),
        ]
        for k, v in rows:
            self._add_meta_row(k, v)
        self._add_drawer_heading("NOTES")
        notes = Gtk.Label(label=kf.notes or "Balanced atmospheric frame.")
        notes.add_css_class("handoff-sub")
        notes.set_wrap(True)
        notes.set_xalign(0.0)
        self._drawer_box.append(notes)

        copy_seed = Gtk.Button(label="Copy image details")
        copy_seed.set_has_frame(False)
        copy_seed.add_css_class("mini-action-btn")
        copy_seed.set_cursor_from_name("pointer")
        copy_seed.connect(
            "clicked",
            lambda _b: (
                copy_to_clipboard(self, f"Image {kf.code} | {kf.focal_length} {kf.aperture} | seed={kf.seed}"),
                self._on_toast("Image details copied"),
            ),
        )
        self._drawer_box.append(copy_seed)

    def _populate_video_drawer(self, clip: VideoClipSpec) -> None:
        self._add_drawer_heading("VIDEO DETAILS")
        rows = [
            ("DURATION", f"{clip.duration_s:.1f}s ({int(clip.duration_s * clip.fps)} frames)"),
            ("FRAME RATE", f"{clip.fps} fps"),
            ("SIZE", f"{clip.width} × {clip.height}"),
            ("CAMERA", clip.camera_move),
            ("SEED", str(clip.seed)),
        ]
        for k, v in rows:
            self._add_meta_row(k, v)
        self._add_drawer_heading("DESCRIPTION")
        notes = Gtk.Label(label=clip.prompt_summary)
        notes.add_css_class("handoff-sub")
        notes.set_wrap(True)
        notes.set_xalign(0.0)
        self._drawer_box.append(notes)

    def _add_drawer_heading(self, text: str) -> None:
        lbl = Gtk.Label(label=text)
        lbl.add_css_class("prompt-block-label")
        lbl.set_xalign(0.0)
        lbl.set_margin_top(6)
        self._drawer_box.append(lbl)

    def _add_meta_row(self, key: str, val: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        k_lbl = Gtk.Label(label=key)
        k_lbl.add_css_class("lightbox-meta-key")
        k_lbl.set_xalign(0.0)
        v_lbl = Gtk.Label(label=val)
        v_lbl.add_css_class("lightbox-meta-val")
        v_lbl.set_xalign(0.0)
        v_lbl.set_wrap(True)
        v_lbl.set_selectable(True)
        box.append(k_lbl)
        box.append(v_lbl)
        self._drawer_box.append(box)

    def _on_scroll_zoom(self, _ctrl: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        self.adjust_zoom(-0.22 * dy)
        return True

    def _on_pan_begin(self, _g: Gtk.GestureDrag, _x: float, _y: float) -> None:
        self._drag_origin_pan = (self._pan_x, self._pan_y)

    def _on_pan_update(self, _g: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._zoom <= 1.0:
            return
        self._pan_x = self._drag_origin_pan[0] + dx
        self._pan_y = self._drag_origin_pan[1] + dy
        self._stage.queue_draw()

    def _on_stage_click(self, _g: Gtk.GestureClick, n_press: int, _x: float, _y: float) -> None:
        if n_press == 2:
            if self._zoom > 1.05:
                self.reset_view()
            else:
                self.adjust_zoom(1.0)

    def _draw_stage(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        if w <= 2 or h <= 2:
            return
        pad_x, pad_y = 48.0, 52.0
        avail_w = max(80.0, float(w) - pad_x * 2.0)
        avail_h = max(60.0, float(h) - pad_y * 2.0)
        aspect = 2.39 if self._mode == "video" else (16.0 / 9.0)
        ref_surf: cairo.ImageSurface | None = None
        if self._mode == "reference" and self._references:
            ref = self._references[self._ref_index]
            ref_surf = self._get_reference_surface(ref)
            if ref_surf is not None and ref_surf.get_height() > 0:
                aspect = max(0.25, float(ref_surf.get_width()) / float(ref_surf.get_height()))
            elif ref.width > 0 and ref.height > 0:
                aspect = max(0.25, float(ref.width) / float(ref.height))

        if avail_w / avail_h > aspect:
            frame_h = avail_h
            frame_w = frame_h * aspect
        else:
            frame_w = avail_w
            frame_h = frame_w / aspect

        cr.save()
        cx, cy = w * 0.5 + self._pan_x, h * 0.5 + self._pan_y
        cr.translate(cx, cy)
        cr.scale(self._zoom, self._zoom)
        cr.translate(-frame_w * 0.5, -frame_h * 0.5)

        if self._mode == "keyframe" and self._keyframes:
            kf = self._keyframes[self._kf_index]
            surf = load_image_surface(kf.image_path)
            if surf is not None:
                cr.save()
                cr.scale(frame_w / surf.get_width(), frame_h / surf.get_height())
                cr.set_source_surface(surf, 0, 0)
                cr.paint()
                cr.restore()
            else:
                draw_neutral_frame(cr, frame_w, frame_h, kf.title)
        elif self._mode == "reference" and self._references:
            ref = self._references[self._ref_index]
            if ref_surf is not None:
                cr.save()
                cr.scale(frame_w / ref_surf.get_width(), frame_h / ref_surf.get_height())
                cr.set_source_surface(ref_surf, 0, 0)
                cr.paint()
                cr.restore()
            else:
                draw_neutral_frame(cr, frame_w, frame_h, ref.title or "Reference")
        elif self._mode == "video" and self._clip:
            if self._decoder is not None and self._decoder.is_ready:
                idx = int(round(self._t_norm * max(0, self._decoder.frame_count - 1)))
                surf = self._decoder.get_frame_surface(idx)
                if surf is not None:
                    cr.save()
                    cr.scale(frame_w / surf.get_width(), frame_h / surf.get_height())
                    cr.set_source_surface(surf, 0, 0)
                    cr.paint()
                    cr.restore()
                else:
                    draw_neutral_frame(cr, frame_w, frame_h, self._clip.title)
            else:
                draw_neutral_frame(cr, frame_w, frame_h, self._clip.title)

        cr.set_source_rgba(0.925, 0.937, 0.957, 0.14)
        cr.set_line_width(1.0 / max(1.0, self._zoom))
        cr.rectangle(0, 0, frame_w, frame_h)
        cr.stroke()
        cr.restore()


# ---------------------------------------------------------------------------
# Ephemeral Session Gallery View (Save / Delete Explicitly)
# ---------------------------------------------------------------------------

@dataclass
class GalleryItem:
    id: str
    kind: str  # "image" | "video"
    title: str
    file_path: str
    keyframe: KeyframeSpec | None = None
    video_clip: VideoClipSpec | None = None
    saved_path: str | None = None
    prompt_hint: str = ""


class GalleryView(Gtk.Box):
    """Dedicated session gallery for temporary images & videos (Save or Delete)."""

    def __init__(
        self,
        on_save_item: Callable[[GalleryItem], None],
        on_delete_item: Callable[[GalleryItem], None],
        on_delete_all: Callable[[], None],
        on_open_image: Callable[[list[KeyframeSpec], int], None],
        on_open_video: Callable[[VideoClipSpec, float], None],
        on_back_to_chat: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("gallery-view")
        self._on_save_item = on_save_item
        self._on_delete_item = on_delete_item
        self._on_delete_all = on_delete_all
        self._on_open_image = on_open_image
        self._on_open_video = on_open_video
        self._on_back_to_chat = on_back_to_chat
        self._items: list[GalleryItem] = []
        self._safe_blur_enabled: bool = True
        self._blur_boxes: list[SafeBlurBox] = []

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(940)
        clamp.set_tightening_threshold(600)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        root.add_css_class("gallery-container")

        # Header row
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_col.set_hexpand(True)
        self._title_lbl = Gtk.Label(label="Gallery")
        self._title_lbl.add_css_class("gallery-title")
        self._title_lbl.set_xalign(0.0)
        title_col.append(self._title_lbl)

        self._sub_lbl = Gtk.Label(label="0 items in temporary session memory")
        self._sub_lbl.add_css_class("gallery-card-sub")
        self._sub_lbl.set_xalign(0.0)
        title_col.append(self._sub_lbl)
        hdr.append(title_col)

        self._gallery_blur_btn = Gtk.Button()
        self._gallery_blur_btn.set_has_frame(False)
        self._gallery_blur_btn.add_css_class("safe-blur-toggle")
        self._gallery_blur_btn.add_css_class("active")
        self._gallery_blur_btn.set_cursor_from_name("pointer")
        self._gallery_blur_btn.set_valign(Gtk.Align.CENTER)
        self._gallery_blur_btn.set_tooltip_text("Turn Safe-Blur on or off for Gallery photos and videos")
        gb_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._gallery_blur_icon = SvgIcon("eye-off", size_px=13, color=COLORS.text_primary)
        self._gallery_blur_lbl = Gtk.Label(label="Safe-Blur: On")
        gb_inner.append(self._gallery_blur_icon)
        gb_inner.append(self._gallery_blur_lbl)
        self._gallery_blur_btn.set_child(gb_inner)
        self._gallery_blur_btn.connect("clicked", lambda _b: self.set_safe_blur_enabled(not self._safe_blur_enabled))
        hdr.append(self._gallery_blur_btn)

        self._del_all_btn = Gtk.Button()
        self._del_all_btn.set_has_frame(False)
        self._del_all_btn.add_css_class("gallery-delete-btn")
        self._del_all_btn.set_cursor_from_name("pointer")
        self._del_all_btn.set_valign(Gtk.Align.CENTER)
        da_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        da_box.append(SvgIcon("trash", size_px=13, color="#FCA5A5"))
        da_box.append(Gtk.Label(label="Delete all"))
        self._del_all_btn.set_child(da_box)
        self._del_all_btn.connect("clicked", lambda _b: self._on_delete_all())
        hdr.append(self._del_all_btn)

        back_btn = Gtk.Button(label="Back to chat")
        back_btn.set_has_frame(False)
        back_btn.add_css_class("gallery-action-btn")
        back_btn.set_cursor_from_name("pointer")
        back_btn.set_valign(Gtk.Align.CENTER)
        back_btn.connect("clicked", lambda _b: self._on_back_to_chat())
        hdr.append(back_btn)
        root.append(hdr)

        notice = Gtk.Label(
            label="Images and videos are never saved automatically — they stay in temporary memory only until you save them here or close Vimona."
        )
        notice.add_css_class("gallery-notice")
        notice.set_wrap(True)
        notice.set_xalign(0.0)
        root.append(notice)

        self._flow = Gtk.FlowBox()
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_homogeneous(True)
        self._flow.set_min_children_per_line(2)
        self._flow.set_max_children_per_line(3)
        self._flow.set_valign(Gtk.Align.START)
        self._flow.set_row_spacing(14)
        self._flow.set_column_spacing(14)
        root.append(self._flow)

        self._empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._empty_box.set_margin_top(48)
        self._empty_box.set_halign(Gtk.Align.CENTER)
        self._empty_box.append(SvgIcon("gallery", size_px=34, color=COLORS.text_muted))
        e_title = Gtk.Label(label="No images or videos yet")
        e_title.add_css_class("gallery-card-meta")
        self._empty_box.append(e_title)
        e_sub = Gtk.Label(
            label="Generate images or videos in chat — they will appear here ready to preview, save, or delete."
        )
        e_sub.add_css_class("gallery-card-sub")
        e_sub.set_wrap(True)
        e_sub.set_justify(Gtk.Justification.CENTER)
        self._empty_box.append(e_sub)
        root.append(self._empty_box)

        clamp.set_child(root)
        scroll.set_child(clamp)
        self.append(scroll)
        self.set_items([])

    def set_safe_blur_enabled(self, enabled: bool) -> None:
        self._safe_blur_enabled = enabled
        if enabled:
            self._gallery_blur_btn.add_css_class("active")
            self._gallery_blur_lbl.set_text("Safe-Blur: On")
            self._gallery_blur_icon.set_icon("eye-off", COLORS.text_primary)
        else:
            self._gallery_blur_btn.remove_css_class("active")
            self._gallery_blur_lbl.set_text("Safe-Blur: Off")
            self._gallery_blur_icon.set_icon("eye", COLORS.text_secondary)
        for bb in self._blur_boxes:
            bb.set_safe_blur_enabled(enabled)

    def set_items(self, items: list[GalleryItem]) -> None:
        self._items = [it for it in items if Path(it.file_path).is_file()]
        self._blur_boxes.clear()
        c = self._flow.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._flow.remove(c)
            c = nxt

        count = len(self._items)
        self._sub_lbl.set_text(
            f"{count} item{'s' if count != 1 else ''} in temporary session memory"
        )
        self._del_all_btn.set_visible(count > 0)
        self._empty_box.set_visible(count == 0)
        self._flow.set_visible(count > 0)

        for it in self._items:
            self._flow.insert(self._build_card(it), -1)

    def _build_card(self, item: GalleryItem) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("gallery-card")

        thumb_ov = Gtk.Overlay()
        thumb_ov.add_css_class("gallery-thumb-box")
        thumb_ov.set_overflow(Gtk.Overflow.HIDDEN)
        thumb_ov.set_size_request(250, 165)
        thumb_ov.set_vexpand(False)
        thumb_ov.set_cursor_from_name("pointer")

        if item.kind == "image":
            tex = load_image_texture(item.file_path)
            if tex is None and Path(item.file_path).is_file():
                try:
                    tex = Gdk.Texture.new_from_filename(item.file_path)
                except Exception:
                    tex = None
            if tex is not None:
                pic = Gtk.Picture.new_for_paintable(tex)
                pic.set_content_fit(Gtk.ContentFit.COVER)
                pic.set_can_shrink(True)
                pic.set_hexpand(True)
                pic.set_vexpand(True)
                bb = SafeBlurBox(
                    pic,
                    explicit=is_explicit_media(item.file_path, item.prompt_hint, item.title),
                    safe_blur_enabled=self._safe_blur_enabled,
                    blur_radius=40.0,
                )
                self._blur_boxes.append(bb)
                thumb_ov.set_child(bb)
            else:
                da = Gtk.DrawingArea()
                da.set_size_request(-1, 165)
                da.set_draw_func(lambda _a, cr, w, h, t=item.title: draw_neutral_frame(cr, float(w), float(h), t))
                thumb_ov.set_child(da)
        else:
            dec = VideoFrameDecoder(item.file_path)
            da = Gtk.DrawingArea()
            da.set_size_request(-1, 165)
            da.set_hexpand(True)
            da.set_vexpand(False)

            def _draw_vid(_a: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
                if dec.is_ready:
                    surf = dec.get_frame_surface(0)
                    if surf is not None:
                        sx = w / max(1, surf.get_width())
                        sy = h / max(1, surf.get_height())
                        scale = max(sx, sy)
                        cr.save()
                        cr.translate((w - surf.get_width() * scale) * 0.5, (h - surf.get_height() * scale) * 0.5)
                        cr.scale(scale, scale)
                        cr.set_source_surface(surf, 0, 0)
                        cr.paint()
                        cr.restore()
                        return
                draw_neutral_frame(cr, float(w), float(h), item.title)

            da.set_draw_func(_draw_vid)
            bb = SafeBlurBox(
                da,
                explicit=is_explicit_media(item.file_path, item.prompt_hint, item.title),
                safe_blur_enabled=self._safe_blur_enabled,
                blur_radius=40.0,
            )
            self._blur_boxes.append(bb)
            thumb_ov.set_child(bb)

        kind_lbl = Gtk.Label(label="Video" if item.kind == "video" else "Image")
        kind_lbl.add_css_class("keyframe-badge")
        kind_lbl.set_halign(Gtk.Align.START)
        kind_lbl.set_valign(Gtk.Align.START)
        kind_lbl.set_margin_top(8)
        kind_lbl.set_margin_start(8)
        thumb_ov.add_overlay(kind_lbl)

        if item.saved_path:
            saved_lbl = Gtk.Label(label="Saved")
            saved_lbl.add_css_class("gallery-saved-pill")
            saved_lbl.set_halign(Gtk.Align.END)
            saved_lbl.set_valign(Gtk.Align.START)
            saved_lbl.set_margin_top(8)
            saved_lbl.set_margin_end(8)
            thumb_ov.add_overlay(saved_lbl)

        click = Gtk.GestureClick()
        if item.kind == "image" and item.keyframe is not None:
            all_kfs = [g.keyframe for g in self._items if g.keyframe is not None]
            idx = next((i for i, k in enumerate(all_kfs) if k.image_path == item.file_path), 0)
            click.connect("released", lambda _g, _n, _x, _y, kfs=all_kfs, ii=idx: self._on_open_image(kfs, ii))
        elif item.kind == "video" and item.video_clip is not None:
            click.connect("released", lambda _g, _n, _x, _y, vc=item.video_clip: self._on_open_video(vc, 0.0))
        thumb_ov.add_controller(click)
        card.append(thumb_ov)

        meta_lbl = Gtk.Label(label=item.title)
        meta_lbl.add_css_class("gallery-card-meta")
        meta_lbl.set_xalign(0.0)
        meta_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        card.append(meta_lbl)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        save_btn = Gtk.Button()
        save_btn.set_has_frame(False)
        save_btn.add_css_class("gallery-action-btn")
        save_btn.set_hexpand(True)
        save_btn.set_cursor_from_name("pointer")
        s_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        s_inner.set_halign(Gtk.Align.CENTER)
        s_inner.append(SvgIcon("download", size_px=13, color=COLORS.text_primary))
        s_inner.append(Gtk.Label(label="Save" if not item.saved_path else "Save as…"))
        save_btn.set_child(s_inner)
        save_btn.connect("clicked", lambda _b, it=item: self._on_save_item(it))
        actions.append(save_btn)

        del_btn = Gtk.Button()
        del_btn.set_has_frame(False)
        del_btn.add_css_class("gallery-delete-btn")
        del_btn.set_cursor_from_name("pointer")
        del_btn.set_tooltip_text("Delete immediately")
        d_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        d_inner.set_halign(Gtk.Align.CENTER)
        d_inner.append(SvgIcon("trash", size_px=13, color="#FCA5A5"))
        d_inner.append(Gtk.Label(label="Delete"))
        del_btn.set_child(d_inner)
        del_btn.connect("clicked", lambda _b, it=item: self._on_delete_item(it))
        actions.append(del_btn)

        card.append(actions)
        return card
