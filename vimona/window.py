"""Main Fedora native desktop window for Vimona."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Callable
import uuid

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import __app_id__
from .controller import VimonaController
from .gooey_nav import GooeyNavItem, GooeyNavWidget
from .icons import SvgIcon
from .metallic_paint import MetallicPaintWidget
from .ram_manager import (
    InstalledAppsRamWindow,
    ProcessRamUsage,
    find_unnecessary_processes,
    safe_kill_process,
    scan_all_processes_by_ram,
    terminate_unnecessary_processes,
)
from .hf_models import (
    CURATED_HF_MODELS,
    HardwareProfile,
    HFModelSpec,
    detect_hardware_profile,
    download_hf_model,
    evaluate_model_fit,
    fetch_hf_repo_meta,
    find_local_model_path,
    get_models_for_category,
    get_selected_model_id,
    set_selected_model_id,
)
from .slide_commit import SlideCommitWidget
from .safety_blur import SafeBlurBox, is_explicit_text
from .skills_loader import load_all_skills
from .skills_manager import SkillsManagerOverlay
from .perf_profile import (
    get_footer_telemetry_label,
    get_performance_mode,
    get_thermal_cooldown_s,
    register_profile_listener,
    set_performance_mode,
)
from .models import (
    ChatSession,
    ConversationTurn,
    KeyframeSpec,
    MemoryHandoffData,
    PromptSpec,
    ReferenceCandidate,
    RenderPlaylistJob,
    VideoClipSpec,
)
from .storage import (
    EPHEMERAL_MEDIA_DIR,
    cleanup_ephemeral_media,
    delete_ephemeral_file,
    load_chats,
    save_chats,
    save_media_file,
)
from .tokens import COLORS, GEOMETRY
from .tv_mascot import TV_MASCOT_STATES, TvMascotState, TvMascotWidget
from .widgets import (
    AssistantProseView,
    FloatingComposer,
    GalleryItem,
    GalleryView,
    KeyframeGrid,
    LightboxInspector,
    MemoryHandoffCard,
    PromptBreakdownBlock,
    ReasoningAccordion,
    ReferencePickerCard,
    ResearchCard,
    ResearchPermissionCard,
    SystemStatePill,
    ThinkingLiveCard,
    UserMessagePill,
    VideoPlayerCard,
    copy_to_clipboard,
)


class VimonaWindow(Adw.ApplicationWindow):
    """Primary native Fedora window with collapsible chat drawer and 740px stream."""

    def __init__(
        self,
        app: Adw.Application,
        auto_boot_backend: bool = True,
    ) -> None:
        super().__init__(application=app, title="Vimona")
        self.add_css_class("vimona-window")
        self.set_default_size(1420, 880)
        self.set_size_request(540, 560)

        cleanup_ephemeral_media(include_current=False)
        self._chats: list[ChatSession] = load_chats()
        self._active_chat: ChatSession | None = None
        self._session_rows: dict[str, Gtk.Box] = {}
        self._keyframe_grids: list[KeyframeGrid] = []
        self._video_cards: list[VideoPlayerCard] = []
        self._user_pills: list[UserMessagePill] = []
        self._safe_blur_boxes: list[SafeBlurBox] = []
        self._unblurred_chat_ids: set[str] = set()
        self._handoff_cards: dict[str, MemoryHandoffCard] = {}
        self._perm_cards: dict[str, ResearchPermissionCard] = {}
        self._ref_picker_cards: dict[str, ReferencePickerCard] = {}
        self._stream_turns: dict[str, ConversationTurn] = {}
        self._stream_grids: dict[str, KeyframeGrid] = {}
        self._gallery_items: list[GalleryItem] = []
        self._deleted_gallery_paths: set[str] = set()
        self._thinking_card: ThinkingLiveCard | None = None
        self._toast_timer: int | None = None
        self._telemetry_timer: int | None = None
        self._mascot_done_timer: int | None = None
        self._ram_alert_dismissed: bool = False
        self._high_ram_override: bool = False
        self._waiting_high_ram_ui: bool = False
        self._ram_window: InstalledAppsRamWindow | None = None
        self._ram_popover: Gtk.Popover | None = None
        self._syncing_ram_switch: bool = False
        self._pending_queue: list[RenderPlaylistJob] = []
        self._active_playlist_job: RenderPlaylistJob | None = None
        self._playlist_batch_active: bool = False
        self._playlist_batch_done: int = 0
        self._playlist_batch_total: int = 0
        self._playlist_batch_started_at: float = 0.0
        self._playlist_had_sensitive: bool = False
        self._auto_clean_ram_between_jobs: bool = True
        self._quiet_batch_alerts: bool = True
        self._cooldown_timer: int | None = None
        self._last_ram_telemetry_txt: str = "RAM ready · Air-Gapped Netns"
        self._suppress_auto_dequeue: bool = False
        self._notifications_enabled: bool = True
        self._last_notification: dict[str, object] | None = None

        self._controller = VimonaController(self)

        self._root_overlay = Gtk.Overlay()
        self.set_content(self._root_overlay)

        self._split_view = Adw.OverlaySplitView()
        self._split_view.set_min_sidebar_width(244)
        self._split_view.set_max_sidebar_width(268)
        self._split_view.set_sidebar_width_fraction(0.20)
        self._split_view.set_show_sidebar(True)
        self._split_view.set_sidebar(self._build_sidebar())
        self._split_view.set_content(self._build_main_content())
        self._root_overlay.set_child(self._split_view)

        self._lightbox = LightboxInspector(on_toast=self.show_toast)
        self._root_overlay.add_overlay(self._lightbox)

        self._chat_select_rev = self._build_chat_selector_overlay()
        self._root_overlay.add_overlay(self._chat_select_rev)

        self._new_chat_mode_rev = self._build_new_chat_mode_overlay()
        self._root_overlay.add_overlay(self._new_chat_mode_rev)

        self._skills_mgr = SkillsManagerOverlay(
            parent_window=self,
            on_toast=self.show_toast,
            on_skills_changed=self._on_skills_count_changed,
        )
        self._root_overlay.add_overlay(self._skills_mgr)
        self._on_skills_count_changed(sum(1 for s in load_all_skills() if s.enabled))
        register_profile_listener(self._on_perf_profile_changed)

        self._toast_revealer = Gtk.Revealer()
        self._toast_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self._toast_revealer.set_transition_duration(180)
        self._toast_revealer.set_halign(Gtk.Align.CENTER)
        self._toast_revealer.set_valign(Gtk.Align.START)
        self._toast_revealer.set_margin_top(58)
        self._toast_revealer.set_can_target(False)
        self._toast_lbl = Gtk.Label()
        self._toast_lbl.add_css_class("vimona-toast")
        self._toast_revealer.set_child(self._toast_lbl)
        self._root_overlay.add_overlay(self._toast_revealer)

        bp_narrow = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(f"max-width: {GEOMETRY.split_collapse_width_px}px")
        )
        bp_narrow.add_setter(self._split_view, "collapsed", True)
        self.add_breakpoint(bp_narrow)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_global_key)
        self.add_controller(key_ctrl)

        ptr_ctrl = Gtk.EventControllerMotion()
        ptr_ctrl.connect("motion", self._on_window_pointer_motion)
        ptr_ctrl.connect("leave", self._on_window_pointer_leave)
        self.add_controller(ptr_ctrl)

        self.connect("close-request", self._on_close_request)

        if self._chats:
            self._load_session(self._chats[0])
        else:
            self.new_chat()

        self._telemetry_timer = GLib.timeout_add(1500, self._poll_telemetry)
        self._poll_telemetry()

    # -----------------------------------------------------------------------
    # Sidebar Construction
    # -----------------------------------------------------------------------

    def _build_sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.add_css_class("vimona-sidebar")

        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hdr.add_css_class("sidebar-header")
        self._sidebar_logo = MetallicPaintWidget(
            size=24,
            color="#eef2f9",
            density=2.2,
            softness=0.10,
            dispersion=0.35,
            distortion=0.07,
            edge_bend=0.72,
            speed=0.85,
            scale=0.88,
            mouse_tilt=0.50,
            intro=True,
            follow_pointer=True,
        )
        hdr.append(self._sidebar_logo)

        self._sidebar_wordmark = MetallicPaintWidget(
            width_px=86,
            height_px=26,
            text="Vimona",
            font_family="Sans",
            font_weight=800,
            color="#f6f9ff",
            density=3.2,
            softness=0.06,
            dispersion=0.28,
            distortion=0.05,
            edge_bend=0.58,
            speed=0.90,
            scale=0.86,
            mouse_tilt=0.50,
            intro=True,
            follow_pointer=True,
        )
        hdr.append(self._sidebar_wordmark)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        hdr.append(spacer)

        collapse_btn = Gtk.Button()
        collapse_btn.set_has_frame(False)
        collapse_btn.add_css_class("icon-btn")
        collapse_btn.set_cursor_from_name("pointer")
        collapse_btn.set_tooltip_text("Hide sidebar (Ctrl+\\)")
        collapse_btn.set_child(SvgIcon("sidebar", size_px=15, color=COLORS.text_secondary))
        collapse_btn.connect("clicked", lambda _b: self.toggle_sidebar())
        hdr.append(collapse_btn)
        sidebar.append(hdr)

        self._sidebar_nav = GooeyNavWidget(
            items=[
                GooeyNavItem(
                    id="chats",
                    label="Chats",
                    icon="sidebar",
                    tooltip="Active conversation",
                ),
                GooeyNavItem(
                    id="new",
                    label="New",
                    icon="compose",
                    tooltip="New chat (Ctrl+N)",
                ),
                GooeyNavItem(
                    id="gallery",
                    label="Gallery",
                    icon="gallery",
                    badge_count=0,
                    tooltip="Temporary session media gallery",
                ),
            ],
            initial_active_index=0 if self._chats else 1,
            pill_color="#f4f4f5",
            droplet_colors=["#f4f4f5", "#c8dcff", "#8ab4f8"],
            particle_count=15,
            spread=56.0,
            animation_time_ms=580.0,
            time_variance_ms=260.0,
            gooeyness=0.55,
            wobble=0.52,
            frame=True,
            hover_effect=True,
            height_px=40,
            on_change=self._on_sidebar_nav_changed,
        )
        self._sidebar_nav.set_margin_start(10)
        self._sidebar_nav.set_margin_end(10)
        self._sidebar_nav.set_margin_top(4)
        self._sidebar_nav.set_margin_bottom(10)
        sidebar.append(self._sidebar_nav)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self._sidebar_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._sidebar_list.set_margin_bottom(12)
        scroll.set_child(self._sidebar_list)
        sidebar.append(scroll)

        self._rebuild_sidebar_list()

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.add_css_class("sidebar-footer")
        f_lbl = Gtk.Label(label="Local & Uncensored")
        f_lbl.add_css_class("sidebar-footer-label")
        f_lbl.set_xalign(0.0)
        f_lbl.set_hexpand(True)
        footer.append(f_lbl)

        self._clear_chats_btn = Gtk.MenuButton()
        self._clear_chats_btn.set_has_frame(False)
        self._clear_chats_btn.add_css_class("clear-chats-btn")
        self._clear_chats_btn.set_cursor_from_name("pointer")
        self._clear_chats_btn.set_tooltip_text("Delete all chats")
        clr_lbl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        clr_lbl.append(SvgIcon("trash", size_px=12, color=COLORS.text_muted))
        clr_lbl.append(Gtk.Label(label="Clear"))
        self._clear_chats_btn.set_child(clr_lbl)
        self._clear_chats_btn.set_popover(self._build_clear_chats_popover())
        self._clear_chats_btn.set_visible(bool(self._chats))
        footer.append(self._clear_chats_btn)

        sidebar.append(footer)

        return sidebar

    def _build_clear_chats_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_size_request(210, -1)

        msg = Gtk.Label(label="Delete all saved chats?")
        msg.add_css_class("gallery-card-meta")
        msg.set_xalign(0.0)
        box.append(msg)

        sub = Gtk.Label(label="Clears your local sidebar chat history.")
        sub.add_css_class("session-row-meta")
        sub.set_xalign(0.0)
        sub.set_wrap(True)
        box.append(sub)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btns.set_halign(Gtk.Align.END)
        cancel_b = Gtk.Button(label="Cancel")
        cancel_b.set_has_frame(False)
        cancel_b.add_css_class("mini-action-btn")
        cancel_b.set_cursor_from_name("pointer")
        cancel_b.connect("clicked", lambda _b: pop.popdown())
        confirm_b = Gtk.Button(label="Delete all")
        confirm_b.set_has_frame(False)
        confirm_b.add_css_class("gallery-delete-btn")
        confirm_b.set_cursor_from_name("pointer")

        def _do_clear(_b: Gtk.Button) -> None:
            pop.popdown()
            self.delete_all_chats()

        confirm_b.connect("clicked", _do_clear)
        btns.append(cancel_b)
        btns.append(confirm_b)
        box.append(btns)
        pop.set_child(box)
        return pop

    def _rebuild_sidebar_list(self) -> None:
        self._session_rows.clear()
        if hasattr(self, "_clear_chats_btn"):
            self._clear_chats_btn.set_visible(bool(self._chats))
        c = self._sidebar_list.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._sidebar_list.remove(c)
            c = nxt

        if not self._chats:
            empty_lbl = Gtk.Label(label="No chats yet")
            empty_lbl.add_css_class("sidebar-footer-label")
            empty_lbl.set_xalign(0.0)
            empty_lbl.set_margin_start(14)
            empty_lbl.set_margin_top(12)
            self._sidebar_list.append(empty_lbl)
            return

        pinned = [ch for ch in self._chats if ch.pinned]
        recent = [ch for ch in self._chats if not ch.pinned]

        if pinned:
            lbl = Gtk.Label(label="Pinned")
            lbl.add_css_class("sidebar-section-label")
            lbl.set_xalign(0.0)
            self._sidebar_list.append(lbl)
            for ch in pinned:
                self._sidebar_list.append(self._make_session_row(ch))

        if recent:
            lbl = Gtk.Label(label="Recent")
            lbl.add_css_class("sidebar-section-label")
            lbl.set_xalign(0.0)
            self._sidebar_list.append(lbl)
            for ch in recent:
                self._sidebar_list.append(self._make_session_row(ch))

    def _make_session_row(self, chat: ChatSession) -> Gtk.Box:
        shell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        shell.add_css_class("session-row-shell")
        if self._active_chat is not None and chat.id == self._active_chat.id:
            shell.add_css_class("active")

        select_btn = Gtk.Button()
        select_btn.set_has_frame(False)
        select_btn.add_css_class("session-select-btn")
        select_btn.set_hexpand(True)
        select_btn.set_cursor_from_name("pointer")
        select_btn.connect("clicked", lambda _b, ch=chat: self._on_chat_clicked(ch))

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        t_lbl = Gtk.Label(label=chat.title)
        t_lbl.add_css_class("session-row-title")
        t_lbl.set_xalign(0.0)
        t_lbl.set_hexpand(True)
        t_lbl.set_ellipsize(Pango.EllipsizeMode.END)

        # Only blur the chat name if the chat name itself is explicit (nice names stay unblurred)
        explicit_title = is_explicit_text(chat.title)
        title_blur: SafeBlurBox | None = None
        if explicit_title:
            revealed = chat.id in self._unblurred_chat_ids

            def _on_row_blur_changed(now_blurred: bool, cid: str = chat.id) -> None:
                if now_blurred:
                    self._unblurred_chat_ids.discard(cid)
                else:
                    self._unblurred_chat_ids.add(cid)
                if self._active_chat is not None and self._active_chat.id == cid:
                    self._header_title_blur.set_revealed_override(not now_blurred)

            title_blur = SafeBlurBox(
                t_lbl,
                explicit=True,
                safe_blur_enabled=True,
                blur_radius=26.0,
                compact=True,
                show_hover_toggle=False,
                require_slide_confirm=True,
                on_state_changed=_on_row_blur_changed,
            )
            if revealed:
                title_blur.set_revealed_override(True)
            title_blur.set_hexpand(True)
            top.append(title_blur)
        else:
            top.append(t_lbl)

        if chat.pinned:
            top.append(SvgIcon("pin", size_px=11, color=COLORS.text_muted))

        select_btn.set_child(top)
        shell.append(select_btn)

        if title_blur is not None:
            title_blur.bind_hover_target(shell, position=Gtk.PositionType.RIGHT)

        more_btn = Gtk.MenuButton()
        more_btn.set_has_frame(False)
        more_btn.add_css_class("session-more-btn")
        more_btn.set_valign(Gtk.Align.CENTER)
        more_btn.set_cursor_from_name("pointer")
        more_btn.set_tooltip_text("Rename, pin, copy, or delete chat")
        more_btn.set_child(SvgIcon("more", size_px=14, color=COLORS.text_secondary))
        more_btn.set_popover(self._build_chat_actions_popover(chat, shell, more_btn))
        shell.append(more_btn)

        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("pressed", lambda *_a, mb=more_btn: mb.popup())
        shell.add_controller(rclick)

        self._session_rows[chat.id] = shell
        return shell

    def _build_chat_actions_popover(
        self,
        chat: ChatSession,
        anchor: Gtk.Widget,
        menu_btn: Gtk.MenuButton,
    ) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_size_request(168, -1)

        def _make_item(icon: str, label: str, danger: bool = False) -> Gtk.Button:
            b = Gtk.Button()
            b.set_has_frame(False)
            b.add_css_class("popover-item")
            if danger:
                b.add_css_class("danger")
            b.set_cursor_from_name("pointer")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(
                SvgIcon(icon, size_px=13, color="#FCA5A5" if danger else COLORS.text_secondary)
            )
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0.0)
            row.append(lbl)
            b.set_child(row)
            return b

        ren_btn = _make_item("edit", "Rename")
        ren_btn.connect(
            "clicked",
            lambda _b, c=chat, a=anchor, p=pop: (
                p.popdown(),
                self._open_rename_popover(c, a),
            ),
        )
        box.append(ren_btn)

        pin_btn = _make_item("pin", "Unpin" if chat.pinned else "Pin chat")
        pin_btn.connect(
            "clicked",
            lambda _b, c=chat, p=pop: (p.popdown(), self.toggle_pin_chat(c)),
        )
        box.append(pin_btn)

        exp_btn = _make_item("copy", "Copy as Markdown")
        exp_btn.connect(
            "clicked",
            lambda _b, c=chat, p=pop: (p.popdown(), self.export_chat_markdown(c)),
        )
        box.append(exp_btn)

        del_btn = _make_item("trash", "Delete chat", danger=True)
        del_btn.connect(
            "clicked",
            lambda _b, c=chat, p=pop: (p.popdown(), self.delete_chat(c)),
        )
        box.append(del_btn)

        pop.set_child(box)
        return pop

    def _open_rename_popover(self, chat: ChatSession, anchor: Gtk.Widget) -> None:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        pop.set_parent(anchor)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_size_request(240, -1)

        title = Gtk.Label(label="Rename chat")
        title.add_css_class("gallery-card-meta")
        title.set_xalign(0.0)
        box.append(title)

        entry = Gtk.Entry()
        entry.add_css_class("rename-entry")
        entry.set_text(chat.title)
        box.append(entry)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btns.set_halign(Gtk.Align.END)
        c_btn = Gtk.Button(label="Cancel")
        c_btn.set_has_frame(False)
        c_btn.add_css_class("mini-action-btn")
        c_btn.set_cursor_from_name("pointer")
        c_btn.connect("clicked", lambda _b: pop.popdown())

        s_btn = Gtk.Button(label="Save")
        s_btn.set_has_frame(False)
        s_btn.add_css_class("gallery-action-btn")
        s_btn.set_cursor_from_name("pointer")

        def _commit(*_a: object) -> None:
            new_title = entry.get_text().strip()
            if new_title:
                self.rename_chat(chat, new_title)
            pop.popdown()

        entry.connect("activate", _commit)
        s_btn.connect("clicked", _commit)
        btns.append(c_btn)
        btns.append(s_btn)
        box.append(btns)

        pop.set_child(box)
        pop.connect("closed", lambda _p: _p.unparent())
        pop.popup()
        entry.grab_focus()
        entry.select_region(0, -1)

    def rename_chat(self, chat: ChatSession, new_title: str) -> None:
        cleaned = new_title.strip()
        if not cleaned:
            return
        chat.title = cleaned
        save_chats(self._chats)
        if self._active_chat is chat and self._canvas_stack.get_visible_child_name() != "gallery":
            self._header_title.set_text(cleaned)
            self._header_title_blur.set_explicit(is_explicit_text(cleaned))
        self._rebuild_sidebar_list()
        self.show_toast("Chat renamed")

    def toggle_pin_chat(self, chat: ChatSession) -> None:
        chat.pinned = not chat.pinned
        save_chats(self._chats)
        self._rebuild_sidebar_list()
        self.show_toast("Chat pinned" if chat.pinned else "Chat unpinned")

    def export_chat_markdown(self, chat: ChatSession) -> None:
        lines = [f"# {chat.title}", ""]
        for turn in chat.turns:
            if turn.role == "user":
                lines.append(f"**You:** {turn.text}")
                lines.append("")
            else:
                lines.append("**Vimona:**")
                for blk in turn.prose:
                    if blk.text:
                        lines.append(blk.text)
                    elif blk.items:
                        lines.extend(f"- {it}" for it in blk.items)
                lines.append("")
        copy_to_clipboard(self, "\n".join(lines).strip())
        self.show_toast("Chat copied as Markdown")

    def delete_chat(self, chat: ChatSession) -> None:
        was_active = self._active_chat is not None and self._active_chat.id == chat.id
        self._chats = [c for c in self._chats if c.id != chat.id]
        save_chats(self._chats)
        self._rebuild_sidebar_list()
        if was_active:
            if self._chats:
                self._load_session(self._chats[0])
            else:
                self.new_chat()
        self.show_toast("Chat deleted")

    def delete_all_chats(self) -> None:
        self._chats.clear()
        save_chats(self._chats)
        self._rebuild_sidebar_list()
        self.new_chat()
        self.show_toast("All chats deleted")

    def _on_chat_clicked(self, chat: ChatSession) -> None:
        self._load_session(chat)
        if self._split_view.get_collapsed():
            self._split_view.set_show_sidebar(False)

    # -----------------------------------------------------------------------
    # Main Canvas & Minimal Header
    # -----------------------------------------------------------------------

    def _build_main_content(self) -> Gtk.Widget:
        toolbar_view = Adw.ToolbarView()

        self._header = Adw.HeaderBar()
        self._header.add_css_class("vimona-header")
        self._header.set_show_title(False)

        start_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._sidebar_btn = Gtk.Button()
        self._sidebar_btn.set_has_frame(False)
        self._sidebar_btn.add_css_class("icon-btn")
        self._sidebar_btn.set_cursor_from_name("pointer")
        self._sidebar_btn.set_tooltip_text("Toggle chats sidebar (Ctrl+\\)")
        self._sidebar_btn.set_child(SvgIcon("sidebar", size_px=16, color=COLORS.text_secondary))
        self._sidebar_btn.connect("clicked", lambda _b: self.toggle_sidebar())
        start_box.append(self._sidebar_btn)

        self._header_title = Gtk.Label(label="New chat")
        self._header_title.add_css_class("header-session-title")
        self._header_title.set_ellipsize(Pango.EllipsizeMode.END)
        self._header_title.set_valign(Gtk.Align.CENTER)
        self._header_title_blur = SafeBlurBox(
            self._header_title,
            explicit=False,
            safe_blur_enabled=True,
            blur_radius=26.0,
            compact=True,
            show_hover_toggle=True,
            require_slide_confirm=True,
        )
        self._header_title_blur.set_valign(Gtk.Align.CENTER)
        start_box.append(self._header_title_blur)

        self._header_mode_pill = Gtk.Label(label="Chat")
        self._header_mode_btn = Gtk.MenuButton()
        self._header_mode_btn.set_has_frame(False)
        self._header_mode_btn.add_css_class("header-pill-btn")
        self._header_mode_btn.add_css_class("header-mode-btn")
        self._header_mode_btn.add_css_class("mode-chat")
        self._header_mode_btn.set_valign(Gtk.Align.CENTER)
        self._header_mode_btn.set_cursor_from_name("pointer")
        self._header_mode_btn.set_tooltip_text("Switch active workspace mode (Chat, Images, or Video)")
        hm_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        hm_row.append(self._header_mode_pill)
        chev_m = Gtk.Label(label="▾")
        chev_m.add_css_class("research-tab-url")
        hm_row.append(chev_m)
        self._header_mode_btn.set_child(hm_row)
        self._header_mode_btn.set_popover(self._build_header_mode_popover())
        start_box.append(self._header_mode_btn)

        # Hardware-aware Hugging Face Model Selector Popup right beside Mode / Images pill
        self._hw_profile: HardwareProfile = detect_hardware_profile()
        self._hf_model_tab: str = "text"
        self._hf_dl_cancel = threading.Event()
        self._hf_dl_progress: dict[str, tuple[float, str]] = {}
        self._hf_models_lbl = Gtk.Label(label="Models")
        self._hf_models_btn = Gtk.MenuButton()
        self._hf_models_btn.set_has_frame(False)
        self._hf_models_btn.add_css_class("header-pill-btn")
        self._hf_models_btn.add_css_class("header-models-btn")
        self._hf_models_btn.set_valign(Gtk.Align.CENTER)
        self._hf_models_btn.set_cursor_from_name("pointer")
        self._hf_models_btn.set_tooltip_text(f"Hugging Face Models ({self._hw_profile.summary_label})")
        hf_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        hf_btn_row.append(SvgIcon("chip", size_px=12, color=COLORS.accent_primary))
        hf_btn_row.append(self._hf_models_lbl)
        chev_hf = Gtk.Label(label="▾")
        chev_hf.add_css_class("research-tab-url")
        hf_btn_row.append(chev_hf)
        self._hf_models_btn.set_child(hf_btn_row)
        self._hf_models_popover = Gtk.Popover()
        self._hf_models_popover.add_css_class("hf-popover")
        self._hf_models_popover.set_has_arrow(False)
        self._hf_models_popover.connect("notify::visible", self._on_hf_popover_shown)
        self._hf_models_btn.set_popover(self._hf_models_popover)
        self._sync_hf_models_button_label("Chat & Ideas")
        start_box.append(self._hf_models_btn)

        self._header.pack_start(start_box)

        self._state_pill = SystemStatePill(on_click=None)
        self._ram_menu_btn = Gtk.MenuButton()
        self._ram_menu_btn.add_css_class("ram-menu-btn")
        self._ram_menu_btn.set_has_frame(False)
        self._ram_menu_btn.set_cursor_from_name("pointer")
        self._ram_menu_btn.set_child(self._state_pill)
        self._ram_menu_btn.set_tooltip_text("RAM monitor, process killer & high-RAM override")
        self._ensure_ram_popover()
        self._ram_menu_btn.set_popover(self._ram_popover)
        self._ram_menu_btn.set_margin_end(6)
        self._header.pack_end(self._ram_menu_btn)

        # Unified Controls Dropdown (Quiet/Turbo, Skills, Select Chat, Alerts)
        self._controls_menu_btn = Gtk.MenuButton()
        self._controls_menu_btn.set_has_frame(False)
        self._controls_menu_btn.add_css_class("header-pill-btn")
        self._controls_menu_btn.set_valign(Gtk.Align.CENTER)
        self._controls_menu_btn.set_cursor_from_name("pointer")
        self._controls_menu_btn.set_tooltip_text("Workspace Controls: Performance, Skills, Select Chat & Alerts")
        ctl_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        ctl_btn_row.append(SvgIcon("grid", size_px=12, color=COLORS.text_secondary))
        self._controls_summary_lbl = Gtk.Label(label="Controls")
        ctl_btn_row.append(self._controls_summary_lbl)
        chev_c = Gtk.Label(label="▾")
        chev_c.add_css_class("research-tab-url")
        ctl_btn_row.append(chev_c)
        self._controls_menu_btn.set_child(ctl_btn_row)
        self._controls_menu_btn.set_margin_end(6)

        self._controls_popover = Gtk.Popover()
        self._controls_popover.set_has_arrow(False)
        ctl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        ctl_box.set_size_request(280, -1)
        ctl_box.set_margin_top(6)
        ctl_box.set_margin_bottom(6)
        ctl_box.set_margin_start(6)
        ctl_box.set_margin_end(6)

        perf_hdr = Gtk.Label(label="Hardware Profile")
        perf_hdr.add_css_class("research-perm-note")
        perf_hdr.set_xalign(0.0)
        ctl_box.append(perf_hdr)

        # Quiet / Eco vs. Turbo Hardware Performance Segmented Pill inside Controls dropdown
        self._perf_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self._perf_group.add_css_class("perf-seg-group")
        self._perf_group.set_hexpand(True)

        self._perf_eco_btn = Gtk.Button()
        self._perf_eco_btn.set_has_frame(False)
        self._perf_eco_btn.add_css_class("perf-seg-btn")
        self._perf_eco_btn.set_hexpand(True)
        self._perf_eco_btn.set_cursor_from_name("pointer")
        self._perf_eco_btn.set_tooltip_text("Quiet / Eco Mode: 50% CPU threads, low process niceness & 7s thermal cooldown")
        eco_b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        eco_b.set_halign(Gtk.Align.CENTER)
        eco_b.append(SvgIcon("leaf", size_px=11, color="#6EE7B7"))
        eco_b.append(Gtk.Label(label="Quiet"))
        self._perf_eco_btn.set_child(eco_b)
        self._perf_eco_btn.connect("clicked", lambda _b: self._set_perf_profile("eco"))
        self._perf_group.append(self._perf_eco_btn)

        self._perf_turbo_btn = Gtk.Button()
        self._perf_turbo_btn.set_has_frame(False)
        self._perf_turbo_btn.add_css_class("perf-seg-btn")
        self._perf_turbo_btn.set_hexpand(True)
        self._perf_turbo_btn.set_cursor_from_name("pointer")
        self._perf_turbo_btn.set_tooltip_text("Turbo Mode: Full CPU cores, maximum Vulkan throughput & back-to-back batching")
        tur_b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tur_b.set_halign(Gtk.Align.CENTER)
        tur_b.append(SvgIcon("bolt", size_px=11, color="#FCD34D"))
        tur_b.append(Gtk.Label(label="Turbo"))
        self._perf_turbo_btn.set_child(tur_b)
        self._perf_turbo_btn.connect("clicked", lambda _b: self._set_perf_profile("turbo"))
        self._perf_group.append(self._perf_turbo_btn)
        self._sync_perf_toggle_ui(get_performance_mode())
        ctl_box.append(self._perf_group)

        sep_ctl = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        ctl_box.append(sep_ctl)

        self._skills_btn = Gtk.Button()
        self._skills_btn.set_has_frame(False)
        self._skills_btn.add_css_class("popover-item")
        self._skills_btn.set_cursor_from_name("pointer")
        self._skills_btn.set_tooltip_text("Modular Skills Manager & .vimona-skill import/export")
        skb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        skb.append(SvgIcon("sparkles", size_px=13, color=COLORS.accent_primary))
        self._skills_lbl = Gtk.Label(label="Skills")
        self._skills_lbl.set_hexpand(True)
        self._skills_lbl.set_xalign(0.0)
        skb.append(self._skills_lbl)
        self._skills_btn.set_child(skb)
        self._skills_btn.connect("clicked", lambda _b: (self._controls_popover.popdown(), self._skills_mgr.open()))
        ctl_box.append(self._skills_btn)

        self._select_chat_btn = Gtk.Button()
        self._select_chat_btn.set_has_frame(False)
        self._select_chat_btn.add_css_class("popover-item")
        self._select_chat_btn.set_cursor_from_name("pointer")
        self._select_chat_btn.set_tooltip_text("Select & copy across the whole chat (Ctrl+Shift+A)")
        sc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sc_box.append(SvgIcon("copy", size_px=13, color=COLORS.text_secondary))
        sc_lbl = Gtk.Label(label="Select chat")
        sc_lbl.set_hexpand(True)
        sc_lbl.set_xalign(0.0)
        sc_box.append(sc_lbl)
        sc_k = Gtk.Label(label="Ctrl+Shift+A")
        sc_k.add_css_class("research-tab-url")
        sc_box.append(sc_k)
        self._select_chat_btn.set_child(sc_box)
        self._select_chat_btn.connect("clicked", lambda _b: (self._controls_popover.popdown(), self.open_chat_selector()))
        ctl_box.append(self._select_chat_btn)

        self._notify_btn = Gtk.Button()
        self._notify_btn.set_has_frame(False)
        self._notify_btn.add_css_class("popover-item")
        self._notify_btn.set_cursor_from_name("pointer")
        self._notify_btn.set_tooltip_text("Desktop notifications when AI finishes (On)")
        nb_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._notify_icon = SvgIcon("bell", size_px=13, color=COLORS.accent_primary)
        self._notify_lbl = Gtk.Label(label="Alerts")
        self._notify_lbl.set_hexpand(True)
        self._notify_lbl.set_xalign(0.0)
        self._notify_state_pill = Gtk.Label(label="On")
        self._notify_state_pill.add_css_class("new-chat-lock-tag")
        nb_box.append(self._notify_icon)
        nb_box.append(self._notify_lbl)
        nb_box.append(self._notify_state_pill)
        self._notify_btn.set_child(nb_box)
        self._notify_btn.connect("clicked", lambda _b: self._toggle_notifications())
        ctl_box.append(self._notify_btn)

        self._controls_popover.set_child(ctl_box)
        self._controls_menu_btn.set_popover(self._controls_popover)
        self._header.pack_end(self._controls_menu_btn)

        toolbar_view.add_top_bar(self._header)

        canvas_overlay = Gtk.Overlay()

        self._canvas_stack = Gtk.Stack()
        self._canvas_stack.add_css_class("vimona-canvas")
        self._canvas_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._canvas_stack.set_transition_duration(220)
        self._canvas_stack.set_hexpand(True)
        self._canvas_stack.set_vexpand(True)

        self._canvas_stack.add_named(self._build_empty_state(), "empty")
        self._canvas_stack.add_named(self._build_stream_view(), "stream")
        self._gallery_view = GalleryView(
            on_save_item=self._on_gallery_save_item,
            on_delete_item=self._on_gallery_delete_item,
            on_delete_all=self._on_gallery_delete_all,
            on_open_image=lambda kfs, idx: self._lightbox.open_keyframes(kfs, idx),
            on_open_video=lambda vc, pos: self._lightbox.open_video(vc, pos),
            on_back_to_chat=self._close_gallery_to_chat,
        )
        self._canvas_stack.add_named(self._gallery_view, "gallery")
        canvas_overlay.set_child(self._canvas_stack)

        self._composer_clamp = Adw.Clamp()
        self._composer_clamp.set_maximum_size(GEOMETRY.composer_max_px + 280)
        self._composer_clamp.set_tightening_threshold(600)
        self._composer_clamp.set_halign(Gtk.Align.FILL)
        self._composer_clamp.set_valign(Gtk.Align.END)

        dock_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        dock_row.set_valign(Gtk.Align.END)
        dock_row.set_margin_start(14)
        dock_row.set_margin_end(14)

        self._prompt_mascot = TvMascotWidget(
            size=108,
            name="Vimona",
            show_tag=True,
            initial_state="idle",
            follow_pointer=True,
            on_click=None,
        )
        self._prompt_mascot.set_halign(Gtk.Align.END)
        self._prompt_mascot.set_valign(Gtk.Align.END)
        self._prompt_mascot.set_margin_bottom(14)
        self._prompt_mascot.set_tooltip_text(
            "Vimona 80s CRT · Reflects live AI & RAM mode"
        )
        dock_row.append(self._prompt_mascot)

        composer_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        composer_stack.set_hexpand(True)
        composer_stack.set_valign(Gtk.Align.END)

        self._ram_alert_revealer = Gtk.Revealer()
        self._ram_alert_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._ram_alert_revealer.set_transition_duration(220)
        self._ram_alert_revealer.set_reveal_child(False)

        ram_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        ram_card.add_css_class("ram-alert-card")
        ram_card.append(SvgIcon("chip", size_px=16, color="#EBCB8B"))

        ram_txt_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        ram_txt_box.set_hexpand(True)
        self._ram_alert_title = Gtk.Label(label="RAM usage is too high")
        self._ram_alert_title.add_css_class("ram-alert-title")
        self._ram_alert_title.set_xalign(0.0)
        ram_txt_box.append(self._ram_alert_title)

        ram_sub = Gtk.Label(
            label="Kill a heavy process safely, or override to keep generating."
        )
        ram_sub.add_css_class("ram-alert-sub")
        ram_sub.set_xalign(0.0)
        ram_sub.set_wrap(True)
        self._ram_alert_sub = ram_sub
        ram_txt_box.append(ram_sub)
        ram_card.append(ram_txt_box)

        all_proc_btn = Gtk.Button(label="All Processes")
        all_proc_btn.set_has_frame(False)
        all_proc_btn.add_css_class("ram-alert-secondary")
        all_proc_btn.set_cursor_from_name("pointer")
        all_proc_btn.set_valign(Gtk.Align.CENTER)
        all_proc_btn.connect("clicked", lambda _b: self.open_ram_manager(tab="all"))
        ram_card.append(all_proc_btn)

        check_btn = Gtk.Button(label="Check Apps")
        check_btn.set_has_frame(False)
        check_btn.add_css_class("ram-alert-primary")
        check_btn.set_cursor_from_name("pointer")
        check_btn.set_valign(Gtk.Align.CENTER)
        check_btn.connect("clicked", lambda _b: self.open_ram_manager(tab="apps"))
        ram_card.append(check_btn)

        override_btn = Gtk.Button(label="Override & Continue")
        override_btn.set_has_frame(False)
        override_btn.add_css_class("ram-alert-override")
        override_btn.set_cursor_from_name("pointer")
        override_btn.set_valign(Gtk.Align.CENTER)
        override_btn.connect("clicked", lambda _b: self._on_override_continue_clicked())
        ram_card.append(override_btn)

        dismiss_btn = Gtk.Button(label="Dismiss")
        dismiss_btn.set_has_frame(False)
        dismiss_btn.add_css_class("mini-action-btn")
        dismiss_btn.set_cursor_from_name("pointer")
        dismiss_btn.set_valign(Gtk.Align.CENTER)
        dismiss_btn.connect("clicked", lambda _b: self._dismiss_ram_alert())
        ram_card.append(dismiss_btn)

        self._ram_alert_revealer.set_child(ram_card)
        composer_stack.append(self._ram_alert_revealer)

        self._composer = FloatingComposer(
            on_submit=self._on_user_submit,
            on_stop=self._on_stop_clicked,
            on_batch_changed=self._on_composer_batch_changed,
            on_queue=self._on_user_queue,
            on_skill_changed=self._on_composer_skill_changed,
        )
        composer_stack.append(self._composer)
        dock_row.append(composer_stack)

        # Symmetrical right spacer keeps the prompt box dead-center on wide/maximized displays
        self._mascot_balance_spacer = Gtk.Box()
        self._mascot_balance_spacer.set_size_request(108, 1)
        self._mascot_balance_spacer.set_can_target(False)
        dock_row.append(self._mascot_balance_spacer)

        self._composer_clamp.set_child(dock_row)
        canvas_overlay.add_overlay(self._composer_clamp)

        bp_hide_mascot = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 1080px")
        )
        bp_hide_mascot.add_setter(self._prompt_mascot, "visible", False)
        bp_hide_mascot.add_setter(self._mascot_balance_spacer, "visible", False)
        self.add_breakpoint(bp_hide_mascot)

        toolbar_view.set_content(canvas_overlay)
        return toolbar_view

    # -----------------------------------------------------------------------
    # Empty State & Stream Canvas
    # -----------------------------------------------------------------------

    def _build_empty_state(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(GEOMETRY.reading_column_max_px)
        clamp.set_tightening_threshold(560)

        center_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        center_box.set_valign(Gtk.Align.CENTER)
        center_box.set_vexpand(True)
        center_box.set_margin_start(22)
        center_box.set_margin_end(22)
        center_box.set_margin_top(16)
        center_box.set_margin_bottom(170)

        self._hero_logo = MetallicPaintWidget(
            size=96,
            color="#f2f6ff",
            density=2.5,
            softness=0.08,
            dispersion=0.40,
            distortion=0.08,
            edge_bend=0.70,
            speed=1.0,
            scale=0.88,
            mouse_tilt=0.55,
            intro=True,
            follow_pointer=True,
        )
        self._hero_logo.set_margin_bottom(2)
        center_box.append(self._hero_logo)

        greeting = Gtk.Label(label="How can I help today?")
        greeting.add_css_class("empty-greeting")
        greeting.set_justify(Gtk.Justification.CENTER)
        greeting.set_wrap(True)
        center_box.append(greeting)

        sub = Gtk.Label(
            label="Private, uncensored local AI — chat, search the web, or create images and videos."
        )
        sub.add_css_class("empty-subtitle")
        sub.set_justify(Gtk.Justification.CENTER)
        sub.set_wrap(True)
        center_box.append(sub)

        clamp.set_child(center_box)
        scroll.set_child(clamp)
        return scroll

    def _build_stream_view(self) -> Gtk.Widget:
        self._stream_scroll = Gtk.ScrolledWindow()
        self._stream_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        vadj = self._stream_scroll.get_vadjustment()
        vadj.connect("value-changed", self._on_stream_scrolled)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(GEOMETRY.reading_column_max_px)
        clamp.set_tightening_threshold(560)

        self._stream_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self._stream_box.add_css_class("stream-container")

        clamp.set_child(self._stream_box)
        self._stream_scroll.set_child(clamp)
        return self._stream_scroll

    def _on_stream_scrolled(self, adj: Gtk.Adjustment) -> None:
        if adj.get_value() > 6.0:
            self._header.add_css_class("scrolled")
        else:
            self._header.remove_css_class("scrolled")

    # -----------------------------------------------------------------------
    # Live Fedora Telemetry & Relay Status
    # -----------------------------------------------------------------------

    def _dismiss_ram_alert(self) -> None:
        self._ram_alert_dismissed = True
        self._ram_alert_revealer.set_reveal_child(False)

    def _get_ram_summary(self) -> tuple[float, float]:
        tel = self._controller.get_telemetry()
        return (tel.used_gb, tel.total_gb)

    def get_high_ram_override(self) -> bool:
        return self._high_ram_override

    def set_high_ram_override(self, enabled: bool) -> None:
        self._high_ram_override = bool(enabled)
        self._controller.set_high_ram_override(self._high_ram_override)
        if hasattr(self, "_ram_pop_switch"):
            self._syncing_ram_switch = True
            self._ram_pop_switch.set_active(self._high_ram_override)
            self._syncing_ram_switch = False
        if self._high_ram_override:
            self._ram_alert_revealer.set_reveal_child(False)
            self._waiting_high_ram_ui = False
        if self._ram_window is not None and self._ram_window.get_visible():
            self._ram_window.refresh()
        self._poll_telemetry()

    def _on_override_continue_clicked(self) -> None:
        self.set_high_ram_override(True)
        self.show_toast("High-RAM override enabled — generation will continue")

    def on_high_ram_gate_state(self, waiting: bool, used_gb: float, total_gb: float) -> bool:
        self._waiting_high_ram_ui = bool(waiting)
        if waiting and not self._high_ram_override:
            self._ram_alert_dismissed = False
            self._ram_alert_title.set_text(
                f"Generation paused — RAM is high ({used_gb:.1f}/{total_gb:.0f} GB)"
            )
            self._ram_alert_sub.set_text(
                "Kill a heavy process safely or click Override & Continue to keep generating."
            )
            if self._composer_clamp.get_visible():
                self._ram_alert_revealer.set_reveal_child(True)
        else:
            self._ram_alert_sub.set_text(
                "Kill a heavy process safely, or override to keep generating."
            )
        self._poll_telemetry()
        return False

    def _on_ram_app_closed(self, app_name: str, freed_mb: float) -> None:
        gb_str = f"{freed_mb / 1024.0:.2f} GB" if freed_mb >= 1024.0 else f"{freed_mb:.0f} MB"
        self.show_toast(f"Closed {app_name} (freed ~{gb_str} RAM)")
        GLib.timeout_add(650, lambda: (self._poll_telemetry(), False)[1])

    def _ensure_ram_popover(self) -> None:
        if self._ram_popover is not None:
            return
        self._ram_popover = Gtk.Popover()
        self._ram_popover.set_position(Gtk.PositionType.BOTTOM)
        self._ram_popover.set_autohide(True)
        self._ram_popover.set_has_arrow(True)
        self._ram_popover.add_css_class("ram-popover")

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_size_request(404, -1)

        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr.append(SvgIcon("chip", size_px=15, color=COLORS.accent_primary))
        title = Gtk.Label(label="RAM Processes")
        title.add_css_class("gallery-card-title")
        title.set_xalign(0.0)
        title.set_hexpand(True)
        hdr.append(title)

        self._ram_pop_ram_lbl = Gtk.Label(label="0.0 / 0 GB")
        self._ram_pop_ram_lbl.add_css_class("handoff-mono")
        hdr.append(self._ram_pop_ram_lbl)

        ref_btn = Gtk.Button(label="Refresh")
        ref_btn.set_has_frame(False)
        ref_btn.add_css_class("mini-action-btn")
        ref_btn.set_cursor_from_name("pointer")
        ref_btn.connect("clicked", lambda _b: self._populate_ram_popover())
        hdr.append(ref_btn)
        root.append(hdr)

        sub = Gtk.Label(
            label="Ranked highest RAM first. OS, desktop & AI model processes are locked."
        )
        sub.add_css_class("gallery-card-meta")
        sub.set_xalign(0.0)
        sub.set_wrap(True)
        root.append(sub)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_width(396)
        scroll.set_max_content_width(396)
        scroll.set_min_content_height(248)
        scroll.set_max_content_height(248)
        scroll.set_propagate_natural_height(False)

        self._ram_pop_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroll.set_child(self._ram_pop_list)
        root.append(scroll)

        ov_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ov_row.add_css_class("ram-popover-row")
        ov_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        ov_txt.set_hexpand(True)
        ov_t = Gtk.Label(label="Override high-RAM guard")
        ov_t.add_css_class("gallery-card-title")
        ov_t.set_xalign(0.0)
        ov_d = Gtk.Label(label="Continue generating even when RAM is near capacity")
        ov_d.add_css_class("gallery-card-meta")
        ov_d.set_xalign(0.0)
        ov_d.set_wrap(True)
        ov_txt.append(ov_t)
        ov_txt.append(ov_d)
        ov_row.append(ov_txt)

        self._ram_pop_switch = Gtk.Switch()
        self._ram_pop_switch.set_active(self._high_ram_override)
        self._ram_pop_switch.set_valign(Gtk.Align.CENTER)
        self._ram_pop_switch.connect("notify::active", self._on_popover_override_toggled)
        ov_row.append(self._ram_pop_switch)
        root.append(ov_row)

        clean_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        clean_lbl = Gtk.Label(label="Only allow Vimona & models")
        clean_lbl.add_css_class("gallery-card-meta")
        clean_lbl.set_xalign(0.0)
        clean_lbl.set_hexpand(True)
        clean_bar.append(clean_lbl)

        clean_btn = Gtk.Button(label="Close Unnecessary…")
        clean_btn.set_has_frame(False)
        clean_btn.add_css_class("ram-cut-btn")
        clean_btn.set_cursor_from_name("pointer")
        clean_btn.connect("clicked", lambda _b: self._prompt_clean_from_popover())
        clean_bar.append(clean_btn)
        root.append(clean_bar)

        self._ram_pop_clean_rev = Gtk.Revealer()
        self._ram_pop_clean_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._ram_pop_clean_rev.set_transition_duration(160)
        self._ram_pop_clean_rev.set_reveal_child(False)
        self._ram_pop_clean_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._ram_pop_clean_rev.set_child(self._ram_pop_clean_box)
        root.append(self._ram_pop_clean_rev)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        all_btn = Gtk.Button(label="All Processes Window")
        all_btn.set_has_frame(False)
        all_btn.add_css_class("ram-alert-secondary")
        all_btn.set_cursor_from_name("pointer")
        all_btn.set_hexpand(True)
        all_btn.connect(
            "clicked",
            lambda _b: (self._ram_popover.popdown(), self.open_ram_manager(tab="all")),
        )
        footer.append(all_btn)

        apps_btn = Gtk.Button(label="Installed Apps Only")
        apps_btn.set_has_frame(False)
        apps_btn.add_css_class("ram-alert-secondary")
        apps_btn.set_cursor_from_name("pointer")
        apps_btn.set_hexpand(True)
        apps_btn.connect(
            "clicked",
            lambda _b: (self._ram_popover.popdown(), self.open_ram_manager(tab="apps")),
        )
        footer.append(apps_btn)
        root.append(footer)

        self._ram_popover.set_child(root)
        self._ram_popover.connect("show", lambda _p: self._populate_ram_popover())

    def _on_popover_override_toggled(self, sw: Gtk.Switch, _p: object) -> None:
        if self._syncing_ram_switch:
            return
        self.set_high_ram_override(sw.get_active())

    def _populate_ram_popover(self) -> None:
        if self._ram_popover is None:
            return
        used_gb, total_gb = self._get_ram_summary()
        self._ram_pop_ram_lbl.set_text(f"{used_gb:.1f} / {total_gb:.0f} GB")

        self._syncing_ram_switch = True
        self._ram_pop_switch.set_active(self._high_ram_override)
        self._syncing_ram_switch = False

        child = self._ram_pop_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._ram_pop_list.remove(child)
            child = nxt

        procs = scan_all_processes_by_ram(min_rss_mb=4.0)
        if not procs:
            empty = Gtk.Label(label="No processes above 4 MB found.")
            empty.add_css_class("gallery-card-meta")
            self._ram_pop_list.append(empty)
            return

        for idx, proc in enumerate(procs[:16]):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("ram-popover-row")
            if idx == 0:
                row.add_css_class("top-consumer")

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)
            prefix = "#1 Top RAM · " if idx == 0 else ""
            pname = Gtk.Label(label=f"{prefix}{proc.name} ({proc.pid})")
            pname.add_css_class("gallery-card-title")
            pname.set_xalign(0.0)
            pname.set_ellipsize(Pango.EllipsizeMode.END)
            info.append(pname)

            ram_str = (
                f"{proc.rss_mb / 1024.0:.2f} GB"
                if proc.rss_mb >= 1024.0
                else f"{proc.rss_mb:.0f} MB"
            )
            pmeta = Gtk.Label(label=f"{ram_str} · {proc.cmd_preview}")
            pmeta.add_css_class("gallery-card-meta")
            pmeta.set_xalign(0.0)
            pmeta.set_ellipsize(Pango.EllipsizeMode.END)
            info.append(pmeta)
            row.append(info)

            if proc.protected:
                prot = Gtk.Label(label=proc.protection_reason or "Protected")
                prot.add_css_class("ram-protected-tag")
                prot.set_valign(Gtk.Align.CENTER)
                row.append(prot)
            else:
                kbtn = Gtk.Button(label="Kill")
                kbtn.set_has_frame(False)
                kbtn.add_css_class("ram-cut-btn")
                kbtn.set_cursor_from_name("pointer")
                kbtn.set_valign(Gtk.Align.CENTER)
                kbtn.connect(
                    "clicked",
                    lambda _b, item=proc: self._kill_from_popover(item),
                )
                row.append(kbtn)
            self._ram_pop_list.append(row)

    def _kill_from_popover(self, proc: ProcessRamUsage) -> None:
        ok, msg = safe_kill_process(proc)
        if ok:
            self._on_ram_app_closed(f"{proc.name} (PID {proc.pid})", proc.rss_mb)
            GLib.timeout_add(380, lambda: (self._populate_ram_popover(), False)[1])
        else:
            self.show_toast(msg)

    def _prompt_clean_from_popover(self) -> None:
        targets = find_unnecessary_processes()
        c = self._ram_pop_clean_box.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._ram_pop_clean_box.remove(c)
            c = nxt

        if not targets:
            self.show_toast("Only OS, Vimona & model processes are running")
            self._ram_pop_clean_rev.set_reveal_child(False)
            return

        total_mb = sum(p.rss_mb for p in targets)
        mb_txt = f"{total_mb / 1024.0:.2f} GB" if total_mb >= 1024.0 else f"{total_mb:.0f} MB"
        slider = SlideCommitWidget(
            label=f"Slide to allow ({len(targets)} · ~{mb_txt})",
            done_label="Cleaned",
            width=300,
            height=34,
            radius=17,
            track_color="#24272E",
            handle_color="#EBCB8B",
            success_color="#B44848",
            on_confirm=lambda items=targets: self._execute_clean_from_popover(items),
        )
        slider.set_hexpand(True)
        self._ram_pop_clean_box.append(slider)

        cancel_b = Gtk.Button(label="Cancel")
        cancel_b.set_has_frame(False)
        cancel_b.add_css_class("mini-action-btn")
        cancel_b.set_cursor_from_name("pointer")
        cancel_b.connect("clicked", lambda _b: self._ram_pop_clean_rev.set_reveal_child(False))
        self._ram_pop_clean_box.append(cancel_b)
        self._ram_pop_clean_rev.set_reveal_child(True)

    def _execute_clean_from_popover(self, targets: list[ProcessRamUsage]) -> None:
        killed, freed_mb = terminate_unnecessary_processes(targets)
        self._ram_pop_clean_rev.set_reveal_child(False)
        if killed > 0:
            self._on_ram_app_closed(f"{killed} unnecessary process(es)", freed_mb)
            GLib.timeout_add(450, lambda: (self._populate_ram_popover(), False)[1])

    def _open_ram_options_popover(self) -> None:
        self._ensure_ram_popover()
        if self._ram_popover is not None:
            self._ram_menu_btn.popup()

    def open_ram_manager(self, tab: str = "apps") -> None:
        if self._ram_window is not None and self._ram_window.get_visible():
            self._ram_window.set_tab(tab)
            self._ram_window.present()
            return
        self._ram_window = InstalledAppsRamWindow(
            parent=self,
            get_ram_summary=self._get_ram_summary,
            on_app_closed=self._on_ram_app_closed,
            initial_tab=tab,
            get_override=self.get_high_ram_override,
            on_toggle_override=self.set_high_ram_override,
        )
        self._ram_window.connect("close-request", lambda _w: setattr(self, "_ram_window", None) or False)
        self._ram_window.present()

    def _poll_telemetry(self) -> bool:
        tel = self._controller.get_telemetry()
        mem_str = f"RAM {tel.used_gb:.1f}/{tel.total_gb:.0f} GB"
        self._last_ram_telemetry_txt = f"{tel.used_gb:.1f} / {tel.total_gb:.1f} GB RAM · Air-Gapped Netns"
        if tel.owner == "research":
            self._state_pill.set_state("research", f"Firefox Search · {mem_str}")
        elif tel.owner == "media":
            self._state_pill.set_state("handoff", f"ComfyUI · {mem_str}")
        elif tel.owner == "transition":
            self._state_pill.set_state("handoff", f"{tel.detail} · {mem_str}")
        elif tel.owner == "text":
            self._state_pill.set_state("chat", f"Text · {mem_str}")
        else:
            self._state_pill.set_state("idle", f"Offline · {mem_str}")
        self._composer.set_runtime_status(tel.owner, tel.active_model, mem_str)

        near_limit = tel.total_gb > 0 and tel.used_gb >= (tel.total_gb - 2.2)
        if near_limit and not self._high_ram_override:
            if self._waiting_high_ram_ui:
                self._ram_alert_title.set_text(
                    f"Generation paused — RAM is high ({tel.used_gb:.1f}/{tel.total_gb:.0f} GB)"
                )
            else:
                self._ram_alert_title.set_text(f"RAM is high ({tel.used_gb:.1f}/{tel.total_gb:.0f} GB)")
            if not self._ram_alert_dismissed and self._composer_clamp.get_visible():
                self._ram_alert_revealer.set_reveal_child(True)
        else:
            if not near_limit:
                self._ram_alert_dismissed = False
            self._ram_alert_revealer.set_reveal_child(False)

        if near_limit and not self._high_ram_override:
            self._set_prompt_mascot_state("limit", f"RAM {tel.used_gb:.1f} GB")
            if self._thinking_card is not None:
                if self._waiting_high_ram_ui:
                    self._thinking_card.set_phase(
                        "limit",
                        "Generation paused — RAM is high…",
                        f"Kill processes safely or click Override & Continue ({mem_str})",
                        badge=f"RAM {tel.used_gb:.1f} GB",
                    )
                else:
                    self._thinking_card.set_phase(
                        "limit",
                        "Memory approaching ceiling…",
                        f"Managing 16 GB budget ({mem_str})",
                        badge=f"RAM {tel.used_gb:.1f} GB",
                    )
        elif tel.owner == "media":
            self._set_prompt_mascot_state("working", "Working…")
            if self._handoff_cards:
                self._remove_thinking_card()
            elif self._thinking_card is not None:
                self._thinking_card.set_phase(
                    "working",
                    f"Vimona is working ({tel.active_model})…",
                    "Zero-overlap GPU/RAM rendering in ComfyUI",
                    badge="Working…",
                )
        elif tel.owner == "transition":
            self._set_prompt_mascot_state("working", tel.detail or "Switching…")
            if self._handoff_cards:
                self._remove_thinking_card()
            elif self._thinking_card is not None:
                self._thinking_card.set_phase(
                    "working",
                    tel.detail or "Swapping local models…",
                    f"16 GB relay handoff · {mem_str}",
                    badge=tel.detail or "Switching…",
                )
        elif tel.owner == "research":
            self._set_prompt_mascot_state("thinking", "Searching…")
            if self._thinking_card is not None and not self._thinking_card.has_live_trace:
                self._thinking_card.set_phase(
                    "thinking",
                    "Vimona is pulling web references…",
                    "Isolated /dev/shm memory-only Firefox sandbox (zero trace on SSD)",
                    badge="Searching…",
                )
        elif self._thinking_card is not None:
            self._set_prompt_mascot_state("thinking", "Thinking…")
            if not self._thinking_card.has_live_trace:
                self._thinking_card.set_phase(
                    "thinking",
                    "Vimona is thinking…",
                    f"Running local uncensored inference ({tel.active_model})",
                    badge="Thinking…",
                )
        else:
            self._set_prompt_mascot_state("idle")
        return True

    def _set_prompt_mascot_state(
        self,
        state: TvMascotState,
        custom_status: str | None = None,
        force_live: bool = False,
    ) -> None:
        if self._mascot_done_timer is not None and not force_live and state == "idle":
            return
        if force_live and state != "done" and self._mascot_done_timer is not None:
            GLib.source_remove(self._mascot_done_timer)
            self._mascot_done_timer = None
        self._prompt_mascot.set_state(state, custom_status=custom_status)

    def _trigger_prompt_mascot_done(self) -> None:
        if self._mascot_done_timer is not None:
            GLib.source_remove(self._mascot_done_timer)
        self._prompt_mascot.set_state("done")

        def _settle() -> bool:
            self._mascot_done_timer = None
            if self._thinking_card is None:
                self._prompt_mascot.set_state("idle")
            return False

        self._mascot_done_timer = GLib.timeout_add(3600, _settle)

    def _on_window_pointer_motion(
        self, _ctrl: Gtk.EventControllerMotion, x: float, y: float
    ) -> None:
        self._prompt_mascot.set_global_pointer(x, y, self)
        self._sidebar_logo.set_global_pointer(x, y, self)
        self._sidebar_wordmark.set_global_pointer(x, y, self)
        self._hero_logo.set_global_pointer(x, y, self)
        if self._thinking_card is not None:
            self._thinking_card.mascot.set_global_pointer(x, y, self)

    def _on_window_pointer_leave(self, _ctrl: Gtk.EventControllerMotion) -> None:
        self._prompt_mascot.sim.clear_pointer()
        self._sidebar_logo.clear_pointer()
        self._sidebar_wordmark.clear_pointer()
        self._hero_logo.clear_pointer()
        if self._thinking_card is not None:
            self._thinking_card.mascot.sim.clear_pointer()

    # -----------------------------------------------------------------------
    # Ephemeral Session Gallery (Save / Delete Only When Chosen)
    # -----------------------------------------------------------------------

    def _on_sidebar_nav_changed(self, _index: int, item: GooeyNavItem) -> None:
        if item.id == "new":
            self.new_chat()
            return
        if item.id == "gallery":
            self.open_gallery()
            return
        if item.id == "chats":
            if self._canvas_stack.get_visible_child_name() == "gallery":
                self._close_gallery_to_chat()
            elif (
                (self._active_chat is None or (not self._active_chat.turns and self._chats))
                and self._chats
            ):
                self._load_session(self._chats[0])
            if self._split_view.get_collapsed():
                self._split_view.set_show_sidebar(False)

    def open_gallery(self) -> None:
        self._sidebar_nav.set_active_index(2, animate=self.get_realized(), notify=False)
        for btn in self._session_rows.values():
            btn.remove_css_class("active")
        self._header_title.set_text("Gallery")
        self._header_title_blur.set_explicit(False)
        self._composer_clamp.set_visible(False)
        self._refresh_gallery()
        self._canvas_stack.set_visible_child_name("gallery")
        if self._split_view.get_collapsed():
            self._split_view.set_show_sidebar(False)

    def _close_gallery_to_chat(self) -> None:
        if self._active_chat is not None:
            self._load_session(self._active_chat)
        else:
            self.new_chat()

    def _register_turn_gallery_items(
        self, turn: ConversationTurn, prompt_hint: str = "", refresh: bool = True
    ) -> None:
        added = False
        for kf in turn.keyframes:
            if (
                kf.image_path
                and kf.image_path not in self._deleted_gallery_paths
                and Path(kf.image_path).is_file()
            ):
                if not any(it.file_path == kf.image_path for it in self._gallery_items):
                    self._gallery_items.insert(
                        0,
                        GalleryItem(
                            id=kf.id or str(uuid.uuid4()),
                            kind="image",
                            title=kf.title or Path(kf.image_path).name,
                            file_path=kf.image_path,
                            keyframe=kf,
                            prompt_hint=prompt_hint,
                        ),
                    )
                    added = True
        if (
            turn.video_clip is not None
            and turn.video_clip.video_path
            and turn.video_clip.video_path not in self._deleted_gallery_paths
            and Path(turn.video_clip.video_path).is_file()
        ):
            vp = turn.video_clip.video_path
            if not any(it.file_path == vp for it in self._gallery_items):
                self._gallery_items.insert(
                    0,
                    GalleryItem(
                        id=turn.video_clip.id or str(uuid.uuid4()),
                        kind="video",
                        title=turn.video_clip.title or Path(vp).name,
                        file_path=vp,
                        video_clip=turn.video_clip,
                        prompt_hint=prompt_hint,
                    ),
                )
                added = True
        if added and refresh:
            self._refresh_gallery()

    def _sync_gallery_sources(self) -> None:
        """Ensure every existing image/video from chat turns or ephemeral scratch is registered."""
        all_chats = list(self._chats)
        if self._active_chat is not None and self._active_chat not in all_chats:
            all_chats.append(self._active_chat)
        for ch in all_chats:
            last_u = ""
            for t in ch.turns:
                if t.role == "user" and t.text:
                    last_u = t.text
                elif t.keyframes or t.video_clip is not None:
                    spec_p = t.prompt_spec.raw_prompt if t.prompt_spec is not None else ""
                    self._register_turn_gallery_items(
                        t,
                        prompt_hint=f"{last_u} {spec_p}".strip(),
                        refresh=False,
                    )
        for t in self._stream_turns.values():
            spec_p = t.prompt_spec.raw_prompt if t.prompt_spec is not None else ""
            self._register_turn_gallery_items(t, prompt_hint=spec_p, refresh=False)

        if EPHEMERAL_MEDIA_DIR.is_dir():
            known_paths = {it.file_path for it in self._gallery_items}
            try:
                disk_files = sorted(
                    EPHEMERAL_MEDIA_DIR.iterdir(),
                    key=lambda p: p.stat().st_mtime,
                )
            except OSError:
                disk_files = []
            for p in disk_files:
                if not p.is_file():
                    continue
                sp = str(p)
                if sp in known_paths or sp in self._deleted_gallery_paths:
                    continue
                ext = p.suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp"):
                    kf = KeyframeSpec(
                        id=f"ephem-{uuid.uuid4().hex[:6]}",
                        code="01",
                        title=p.name,
                        image_path=sp,
                    )
                    self._gallery_items.insert(
                        0,
                        GalleryItem(
                            id=kf.id,
                            kind="image",
                            title=p.name,
                            file_path=sp,
                            keyframe=kf,
                        ),
                    )
                elif ext in (".mp4", ".webm", ".mov", ".mkv"):
                    vc = VideoClipSpec(
                        id=f"ephem-vid-{uuid.uuid4().hex[:6]}",
                        title=p.name,
                        duration_s=4.0,
                        fps=24,
                        width=480,
                        height=272,
                        video_path=sp,
                    )
                    self._gallery_items.insert(
                        0,
                        GalleryItem(
                            id=vc.id,
                            kind="video",
                            title=p.name,
                            file_path=sp,
                            video_clip=vc,
                        ),
                    )

    def _refresh_gallery(self) -> None:
        self._sync_gallery_sources()
        self._gallery_items = [
            it
            for it in self._gallery_items
            if it.file_path not in self._deleted_gallery_paths and Path(it.file_path).is_file()
        ]
        n = len(self._gallery_items)
        self._sidebar_nav.set_item_badge("gallery", n)
        self._gallery_view.set_items(self._gallery_items)

    def _on_gallery_save_item(self, item: GalleryItem) -> None:
        src = Path(item.file_path)
        if not src.is_file():
            self.show_toast("Temporary file no longer available")
            self._refresh_gallery()
            return
        dlg = Gtk.FileDialog()
        dlg.set_title("Save Video" if item.kind == "video" else "Save Image")
        dlg.set_initial_name(src.name)
        default_dir = Path.home() / ("Videos" if item.kind == "video" else "Pictures")
        if default_dir.is_dir():
            dlg.set_initial_folder(Gio.File.new_for_path(str(default_dir)))

        def _done(d: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
            try:
                gfile = d.save_finish(res)
            except GLib.Error:
                return
            dest_str = gfile.get_path() if gfile else None
            if not dest_str:
                return
            try:
                saved = save_media_file(src, Path(dest_str))
                item.saved_path = str(saved)
                self._refresh_gallery()
                self.show_toast(f"Saved {saved.name}")
            except Exception as exc:
                self.show_toast(f"Save failed: {exc}")

        dlg.save(self, None, _done)

    def _strip_media_from_chats(self, target_paths: set[str]) -> None:
        for ch in self._chats:
            for turn in ch.turns:
                if turn.keyframes:
                    turn.keyframes = [k for k in turn.keyframes if k.image_path not in target_paths]
                if turn.video_clip and turn.video_clip.video_path in target_paths:
                    turn.video_clip = None

    def _on_gallery_delete_item(self, item: GalleryItem) -> None:
        self._deleted_gallery_paths.add(item.file_path)
        delete_ephemeral_file(item.file_path)
        self._gallery_items = [it for it in self._gallery_items if it.file_path != item.file_path]
        self._strip_media_from_chats({item.file_path})
        self._refresh_gallery()
        self.show_toast("Deleted from temporary session")

    def _on_gallery_delete_all(self) -> None:
        removed_paths = {it.file_path for it in self._gallery_items}
        self._deleted_gallery_paths.update(removed_paths)
        for p in removed_paths:
            delete_ephemeral_file(p)
        self._gallery_items.clear()
        self._strip_media_from_chats(removed_paths)
        self._refresh_gallery()
        self.show_toast("Deleted all unsaved session media")

    # -----------------------------------------------------------------------
    # Session & Stream Management
    # -----------------------------------------------------------------------

    def _load_session(self, chat: ChatSession) -> None:
        target_nav = 0 if (chat.turns or chat in self._chats) else 1
        self._sidebar_nav.set_active_index(target_nav, animate=self.get_realized(), notify=False)
        self._composer_clamp.set_visible(True)
        self._active_chat = chat
        for sid, btn in self._session_rows.items():
            if sid == chat.id:
                btn.add_css_class("active")
            else:
                btn.remove_css_class("active")

        self._header_title.set_text(chat.title)
        self._header_title_blur.set_explicit(is_explicit_text(chat.title))
        self._header_title_blur.set_revealed_override(
            True if chat.id in self._unblurred_chat_ids else None
        )
        self._composer.set_active_skill(chat.active_skill)
        self._composer.set_locked_mode(chat.active_skill if chat.mode_locked else None)
        self._sync_header_mode_and_models(chat.active_skill, locked=chat.mode_locked)
        self._composer.set_busy(False)
        self._composer.set_aquarium_mode(not bool(chat.turns), animate=False)

        self._clear_stream()
        if not chat.turns:
            self._header.remove_css_class("scrolled")
            self._canvas_stack.set_visible_child_name("empty")
        else:
            for turn in chat.turns:
                self._append_turn_widget(turn)
            self._canvas_stack.set_visible_child_name("stream")
            GLib.idle_add(self._scroll_to_bottom)

    def get_chat_messages_for_llm(self, chat_id: str) -> list[dict[str, str]]:
        target = None
        for ch in self._chats:
            if ch.id == chat_id:
                target = ch
                break
        if target is None and self._active_chat and self._active_chat.id == chat_id:
            target = self._active_chat
        if target is None:
            return []

        msgs: list[dict[str, str]] = []
        for turn in target.turns:
            if turn.role == "user":
                if turn.text.strip():
                    msgs.append({"role": "user", "content": turn.text})
            else:
                parts: list[str] = []
                for blk in turn.prose:
                    if blk.text:
                        parts.append(blk.text)
                    elif blk.items:
                        parts.extend(f"- {it}" for it in blk.items)
                if turn.research is not None:
                    ref_tokens = [
                        rf.style_tokens or rf.lighting_mood or rf.title
                        for rf in turn.research.references[:2]
                        if (rf.style_tokens or rf.lighting_mood or rf.title)
                    ]
                    parts.append(
                        f'[Zero-Trace Web Research query="{turn.research.query}"'
                        + (f'; style_tokens="{"; ".join(ref_tokens)}"' if ref_tokens else "")
                        + "]"
                    )
                if turn.prompt_spec is not None and turn.prompt_spec.raw_prompt:
                    tok_bits = [
                        f"{k}={v}"
                        for k, v in turn.prompt_spec.tokens
                        if k.lower() in ("motion", "negative") and v
                    ]
                    spec_line = f"[Previous Visual Prompt Spec: {turn.prompt_spec.raw_prompt}]"
                    if tok_bits:
                        spec_line += " (" + "; ".join(tok_bits) + ")"
                    parts.append(spec_line)
                if turn.keyframes:
                    parts.append(f"[Rendered {len(turn.keyframes)} image keyframe(s)]")
                if turn.video_clip is not None:
                    parts.append(f"[Rendered {turn.video_clip.duration_s:.1f}s video clip]")
                if parts:
                    msgs.append({"role": "assistant", "content": "\n".join(parts)})
        return msgs

    def get_last_keyframe_b64(self, chat_id: str) -> str | None:
        """Return the latest generated keyframe PNG/JPG bytes as base64 for follow-up editing/animation."""
        target = next((ch for ch in self._chats if ch.id == chat_id), None)
        if target is None and self._active_chat and self._active_chat.id == chat_id:
            target = self._active_chat
        if target is None:
            return None
        for turn in reversed(target.turns):
            for kf in turn.keyframes:
                p = getattr(kf, "image_path", None)
                if p and Path(p).is_file():
                    try:
                        import base64 as _b64
                        return _b64.b64encode(Path(p).read_bytes()).decode("ascii")
                    except Exception:
                        pass
        return None

    def _turn_prompt_hint(self, chat: ChatSession | None, turn: ConversationTurn) -> str:
        parts: list[str] = []
        if turn.prompt_spec is not None:
            parts.append(turn.prompt_spec.raw_prompt)
        if chat is not None:
            prev_user = ""
            for t in chat.turns:
                if t.id == turn.id:
                    break
                if t.role == "user" and t.text:
                    prev_user = t.text
            if prev_user:
                parts.append(prev_user)
        return " ".join(parts)

    def stream_keyframe(
        self,
        chat_id: str,
        stream_id: str,
        kf: KeyframeSpec,
        total_requested: int,
        positive_prompt: str,
    ) -> bool:
        """Deliver one generated image immediately into the live chat & gallery."""
        target_chat = next((ch for ch in self._chats if ch.id == chat_id), None)
        if target_chat is None and self._active_chat is not None and self._active_chat.id == chat_id:
            target_chat = self._active_chat
            self._chats.insert(0, target_chat)
        last_u = ""
        if target_chat is not None:
            last_u = next(
                (t.text for t in reversed(target_chat.turns) if t.role == "user" and t.text),
                "",
            )
        hint = f"{last_u} {positive_prompt}".strip()

        turn = self._stream_turns.get(stream_id)
        if turn is None:
            turn = ConversationTurn(
                id=stream_id,
                role="assistant",
                prompt_spec=PromptSpec(
                    title="Rendered Prompt",
                    tokens=[("Positive", positive_prompt)],
                    raw_prompt=positive_prompt,
                ),
                keyframes=[kf],
                keyframe_batch_default=total_requested if total_requested in (1, 2, 4, 8) else 4,
            )
            self._stream_turns[stream_id] = turn
            self._register_turn_gallery_items(turn, prompt_hint=hint)
            if target_chat is not None:
                target_chat.turns.append(turn)
                save_chats(self._chats)
                if (
                    self._active_chat is target_chat
                    and self._canvas_stack.get_visible_child_name() != "gallery"
                ):
                    grid = self._append_turn_widget(
                        turn,
                        animate_media_reveal=False,
                        prompt_hint=hint,
                        return_grid=True,
                    )
                    if grid is not None:
                        self._stream_grids[stream_id] = grid
                    self._canvas_stack.set_visible_child_name("stream")
                    GLib.idle_add(self._scroll_to_bottom)
        else:
            turn.keyframes.append(kf)
            self._register_turn_gallery_items(turn, prompt_hint=hint)
            if target_chat is not None:
                save_chats(self._chats)
            grid = self._stream_grids.get(stream_id)
            if grid is not None and self._active_chat is target_chat:
                grid.add_keyframe(kf)
                GLib.idle_add(self._scroll_to_bottom)
        return False

    def append_turn(self, chat_id: str, turn: ConversationTurn) -> bool:
        """Append a turn to a chat session (ephemeral media never auto-saved)."""
        target_chat = next((ch for ch in self._chats if ch.id == chat_id), None)
        hint = ""
        if target_chat is not None:
            last_u = next((t.text for t in reversed(target_chat.turns) if t.role == "user" and t.text), "")
            spec_p = turn.prompt_spec.raw_prompt if turn.prompt_spec is not None else ""
            hint = f"{last_u} {spec_p}".strip()
        self._register_turn_gallery_items(turn, prompt_hint=hint)
        if turn.handoff is not None:
            self._remove_thinking_card()
        if target_chat is not None:
            target_chat.turns.append(turn)
            save_chats(self._chats)
            if self._active_chat is target_chat and self._canvas_stack.get_visible_child_name() != "gallery":
                self._append_turn_widget(turn, animate_media_reveal=True, prompt_hint=hint)
                self._canvas_stack.set_visible_child_name("stream")
                GLib.idle_add(self._scroll_to_bottom)
        return False

    def update_handoff_turn(self, turn_id: str, data: MemoryHandoffData) -> bool:
        card = self._handoff_cards.get(turn_id)
        if card is not None:
            card.update(data)
        return False

    def show_research_permission(
        self,
        perm_id: str,
        chat_id: str,
        query: str,
        url: str,
        on_decision: Callable[[bool], None],
    ) -> bool:
        card = ResearchPermissionCard(query=query, url=url, on_decision=on_decision)
        self._perm_cards[perm_id] = card
        if self._active_chat and self._active_chat.id == chat_id:
            if self._thinking_card is not None and self._thinking_card.get_parent() is self._stream_box:
                self._stream_box.remove(self._thinking_card)
                self._stream_box.append(card)
                self._stream_box.append(self._thinking_card)
                self._thinking_card.set_phase(
                    "thinking",
                    "Waiting for permission…",
                    "Slide to spawn zero-trace /dev/shm Firefox reference sandbox",
                    badge="Permission",
                )
            else:
                self._stream_box.append(card)
            self._canvas_stack.set_visible_child_name("stream")
            GLib.idle_add(self._scroll_to_bottom)
        return False

    def show_reference_picker(
        self,
        picker_id: str,
        chat_id: str,
        query: str,
        trace_summary: str,
        references: list[ReferenceCandidate],
        upgraded_prompt: str,
        will_render_video: bool,
        on_confirm: Callable[[ReferenceCandidate | None, str], None],
        on_cancel: Callable[[], None],
    ) -> bool:
        card = ReferencePickerCard(
            query=query,
            trace_summary=trace_summary,
            references=references,
            upgraded_prompt=upgraded_prompt,
            will_render_video=will_render_video,
            on_confirm=on_confirm,
            on_cancel=on_cancel,
            on_open_reference=self._lightbox.open_reference,
        )
        self._ref_picker_cards[picker_id] = card
        if self._active_chat and self._active_chat.id == chat_id:
            if self._thinking_card is not None and self._thinking_card.get_parent() is self._stream_box:
                self._stream_box.remove(self._thinking_card)
                self._stream_box.append(card)
                self._stream_box.append(self._thinking_card)
                self._thinking_card.set_phase(
                    "thinking",
                    "Waiting for reference selection…",
                    "Pick a Safe-Blurred reference or proceed offline",
                    badge="References",
                )
            else:
                self._stream_box.append(card)
            self._canvas_stack.set_visible_child_name("stream")
            GLib.idle_add(self._scroll_to_bottom)
        return False

    def finish_reference_picker(self, picker_id: str, status_text: str) -> bool:
        card = self._ref_picker_cards.get(picker_id)
        if card is not None:
            card.mark_finished(status_text)
        return False

    def finish_research_permission(
        self, perm_id: str, ok: bool, error_msg: str | None = None
    ) -> bool:
        card = self._perm_cards.get(perm_id)
        if card is not None:
            card.mark_finished(ok, error_msg=error_msg)
        return False

    def _remove_thinking_card(self) -> None:
        if self._thinking_card is not None:
            parent = self._thinking_card.get_parent()
            if parent is self._stream_box:
                self._stream_box.remove(self._thinking_card)
            self._thinking_card = None

    def update_live_thinking(self, reasoning_txt: str, content_preview: str = "") -> bool:
        if self._thinking_card is not None:
            self._thinking_card.set_thinking_trace(reasoning_txt, content_preview)
            GLib.idle_add(self._scroll_to_bottom)
        return False

    def _on_skills_count_changed(self, active_cnt: int) -> None:
        if hasattr(self, "_skills_lbl"):
            self._skills_lbl.set_text(f"Skills ({active_cnt})")

    def _set_perf_profile(self, mode: str) -> None:
        chosen = set_performance_mode(mode)
        self._on_perf_profile_changed(chosen)
        self.show_toast(
            "Quiet / Eco Mode active (50% CPU + thermal cap)"
            if chosen == "eco"
            else "Turbo Mode active (full CPU & Vulkan throughput)"
        )

    def _on_perf_profile_changed(self, mode: str) -> None:
        GLib.idle_add(self._apply_perf_ui_sync, mode)

    def _apply_perf_ui_sync(self, mode: str) -> bool:
        self._sync_perf_toggle_ui(mode)
        if hasattr(self, "_composer"):
            self._composer.refresh_perf_footer()
        return False

    def _sync_perf_toggle_ui(self, mode: str) -> None:
        if not hasattr(self, "_perf_eco_btn"):
            return
        self._perf_eco_btn.remove_css_class("active-eco")
        self._perf_turbo_btn.remove_css_class("active-turbo")
        if mode == "eco":
            self._perf_eco_btn.add_css_class("active-eco")
        else:
            self._perf_turbo_btn.add_css_class("active-turbo")

    def update_playlist_job_status(self, status: str) -> bool:
        if self._active_playlist_job is not None:
            self._active_playlist_job.status = status
            self._sync_queue_ui()
        return False

    def _toggle_auto_clean_ram(self) -> None:
        self._auto_clean_ram_between_jobs = not self._auto_clean_ram_between_jobs
        self._sync_queue_ui()
        self.show_toast(
            f"Auto-Clean RAM Between Jobs: {'On' if self._auto_clean_ram_between_jobs else 'Off'}"
        )

    def _toggle_quiet_batch_alerts(self) -> None:
        self._quiet_batch_alerts = not self._quiet_batch_alerts
        self._sync_queue_ui()
        self.show_toast(
            f"Batch Notifications: {'Single summary' if self._quiet_batch_alerts else 'Per job'}"
        )

    def _on_user_queue(
        self, prompt: str, skill: str, batch_count: int, clip_seconds: int
    ) -> None:
        chat = self._active_chat
        if chat is None:
            return
        if not self._playlist_batch_active:
            self._playlist_batch_active = True
            self._playlist_batch_done = 0
            self._playlist_batch_total = 1 if getattr(self._composer, "_is_busy", False) else 0
            self._playlist_batch_started_at = time.monotonic()
            self._playlist_had_sensitive = is_explicit_text(chat.title)
        if is_explicit_text(prompt):
            self._playlist_had_sensitive = True

        job = RenderPlaylistJob(
            id=f"job-{uuid.uuid4().hex[:6]}",
            chat_id=chat.id,
            prompt=prompt,
            skill=skill,
            mode="Video" if "video" in skill.lower() else ("Research" if "search" in skill.lower() else "Image"),
            model="Wan 2.1" if "video" in skill.lower() else ("light-uncensored" if "chat" in skill.lower() or "search" in skill.lower() else "Pony V6 XL"),
            batch_count=batch_count,
            clip_seconds=clip_seconds,
            status="Queued",
        )
        self._pending_queue.append(job)
        self._playlist_batch_total += 1
        self._sync_queue_ui()
        if not (self._playlist_batch_active and self._quiet_batch_alerts):
            self.show_toast(f"Added to Render Playlist ({len(self._pending_queue)} queued)")

    def _sync_queue_ui(self) -> None:
        self._composer.render_queue(
            self._pending_queue,
            on_remove=self._remove_queued_item,
            on_clear=self._clear_queue,
            on_run_next=self._dispatch_next_queued,
            on_move=self._move_queued_item,
            active_job=self._active_playlist_job,
            done_count=self._playlist_batch_done,
            total_count=self._playlist_batch_total,
            ram_text=self._last_ram_telemetry_txt,
            auto_clean_ram=self._auto_clean_ram_between_jobs,
            on_toggle_auto_clean=self._toggle_auto_clean_ram,
            quiet_alerts=self._quiet_batch_alerts,
            on_toggle_quiet_alerts=self._toggle_quiet_batch_alerts,
        )

    def _move_queued_item(self, idx: int, delta: int) -> None:
        nxt_idx = idx + delta
        if 0 <= idx < len(self._pending_queue) and 0 <= nxt_idx < len(self._pending_queue):
            self._pending_queue[idx], self._pending_queue[nxt_idx] = (
                self._pending_queue[nxt_idx],
                self._pending_queue[idx],
            )
            self._sync_queue_ui()

    def _remove_queued_item(self, idx: int) -> None:
        if 0 <= idx < len(self._pending_queue):
            self._pending_queue.pop(idx)
            if self._playlist_batch_total > 0:
                self._playlist_batch_total = max(
                    self._playlist_batch_done + (1 if self._active_playlist_job else 0) + len(self._pending_queue),
                    0,
                )
            if not self._pending_queue and self._active_playlist_job is None:
                self._playlist_batch_active = False
            self._sync_queue_ui()

    def _clear_queue(self) -> None:
        if self._cooldown_timer is not None:
            GLib.source_remove(self._cooldown_timer)
            self._cooldown_timer = None
        self._pending_queue.clear()
        if self._active_playlist_job is None:
            self._playlist_batch_active = False
            self._playlist_batch_done = 0
            self._playlist_batch_total = 0
        else:
            self._playlist_batch_total = self._playlist_batch_done + 1
        self._sync_queue_ui()

    def _dispatch_next_queued(self) -> bool:
        if not self._pending_queue or getattr(self._composer, "_is_busy", False):
            return False
        if not self._playlist_batch_active:
            self._playlist_batch_active = True
            self._playlist_batch_done = 0
            self._playlist_batch_total = len(self._pending_queue)
            self._playlist_batch_started_at = time.monotonic()

        nxt = self._pending_queue.pop(0)
        nxt.status = "Air-Gapped Rendering" if nxt.mode in ("Image", "Video") else "Queued"
        self._active_playlist_job = nxt
        self._sync_queue_ui()

        target_cid = nxt.chat_id
        if self._active_chat is None or self._active_chat.id != target_cid:
            found = next((c for c in self._chats if c.id == target_cid), None)
            if found is not None:
                self._load_session(found)
        self._controller.prepare_between_job_hygiene(
            auto_clean=self._auto_clean_ram_between_jobs
        )
        self._on_user_submit(
            nxt.prompt,
            nxt.skill,
            nxt.batch_count,
            nxt.clip_seconds,
        )
        return False

    def _schedule_next_with_cooldown(self) -> None:
        cooldown_s = int(round(get_thermal_cooldown_s()))
        if cooldown_s <= 0 or not self._pending_queue:
            GLib.idle_add(self._dispatch_next_queued)
            return
        if self._cooldown_timer is not None:
            GLib.source_remove(self._cooldown_timer)
            self._cooldown_timer = None
        rem = [cooldown_s]
        self._pending_queue[0].status = f"Cooldown ({rem[0]}s)"
        self._sync_queue_ui()

        def _tick() -> bool:
            if not self._pending_queue or self._suppress_auto_dequeue:
                self._cooldown_timer = None
                return False
            rem[0] -= 1
            if rem[0] <= 0:
                self._cooldown_timer = None
                self._pending_queue[0].status = "Queued"
                self._sync_queue_ui()
                self._dispatch_next_queued()
                return False
            self._pending_queue[0].status = f"Cooldown ({rem[0]}s)"
            self._sync_queue_ui()
            return True

        self._cooldown_timer = GLib.timeout_add(1000, _tick)

    def set_busy(self, busy: bool) -> bool:
        self._composer.set_busy(busy)
        if busy:
            self._set_prompt_mascot_state("thinking", "Thinking…", force_live=True)
        else:
            self._remove_thinking_card()
            self._trigger_prompt_mascot_done()
            suppress = self._suppress_auto_dequeue
            self._suppress_auto_dequeue = False
            if self._playlist_batch_active and not suppress:
                self._playlist_batch_done += 1
            if self._active_playlist_job is not None:
                self._active_playlist_job.status = "Cancelled" if suppress else "Done"
                self._active_playlist_job = None
            self._sync_queue_ui()
            if self._pending_queue and not suppress:
                self._schedule_next_with_cooldown()
            elif self._playlist_batch_active and not suppress and not self._pending_queue:
                done_cnt = self._playlist_batch_done
                tot_cnt = max(done_cnt, self._playlist_batch_total)
                elapsed_s = max(0, int(round(time.monotonic() - self._playlist_batch_started_at)))
                mins, secs = divmod(elapsed_s, 60)
                t_fmt = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
                is_sens = self._playlist_had_sensitive
                cid = self._active_chat.id if self._active_chat else ""
                self._playlist_batch_active = False
                self._playlist_batch_done = 0
                self._playlist_batch_total = 0
                self._playlist_had_sensitive = False
                self._sync_queue_ui()
                self.notify_turn_finished(
                    cid,
                    "Batch Complete",
                    f"{done_cnt}/{tot_cnt} renders saved to In-App Gallery · Total time: {t_fmt}",
                    sensitive=is_sens,
                )
        return False

    def _clear_stream(self) -> None:
        self._remove_thinking_card()
        self._keyframe_grids.clear()
        self._video_cards.clear()
        self._user_pills.clear()
        self._safe_blur_boxes.clear()
        self._handoff_cards.clear()
        self._perm_cards.clear()
        self._ref_picker_cards.clear()
        self._stream_grids.clear()
        c = self._stream_box.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._stream_box.remove(c)
            c = nxt

    def _on_user_msg_edited(self, turn: ConversationTurn, new_text: str) -> None:
        turn.text = new_text
        save_chats(self._chats)
        self.show_toast("Message updated")

    def _on_user_msg_deleted(self, turn: ConversationTurn) -> None:
        turn.text = ""
        save_chats(self._chats)
        self._rebuild_sidebar_list()
        self.show_toast("Prompt deleted — output kept")

    def _append_turn_widget(
        self,
        turn: ConversationTurn,
        animate_media_reveal: bool = False,
        prompt_hint: str = "",
        return_grid: bool = False,
    ) -> KeyframeGrid | None:
        if turn.role == "user":
            pill = UserMessagePill(
                text=turn.text,
                on_toast=self.show_toast,
                on_edited=lambda txt, t=turn: self._on_user_msg_edited(t, txt),
                on_deleted=lambda t=turn: self._on_user_msg_deleted(t),
                on_quote=self._on_quote_selection,
                safe_blur_enabled=True,
            )
            self._user_pills.append(pill)
            self._stream_box.append(pill)
            return

        turn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)

        role_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        role_row.append(SvgIcon("vimona", size_px=14, color=COLORS.accent_primary))
        role_lbl = Gtk.Label(label="Vimona")
        role_lbl.add_css_class("assistant-role-tag")
        role_row.append(role_lbl)
        turn_box.append(role_row)

        resolved_hint = prompt_hint or self._turn_prompt_hint(self._active_chat, turn)

        if turn.reasoning is not None:
            has_real_trace = bool(
                turn.reasoning.summary
                and "via llama-server" not in turn.reasoning.summary.lower()
            )
            acc = ReasoningAccordion(
                turn.reasoning,
                expanded=has_real_trace,
                on_toast=self.show_toast,
                on_quote=self._on_quote_selection,
            )
            r_text = " ".join([turn.reasoning.summary] + list(turn.reasoning.bullets))
            if is_explicit_text(r_text):
                bb = SafeBlurBox(
                    acc,
                    explicit=True,
                    safe_blur_enabled=True,
                    blur_radius=32.0,
                )
                self._safe_blur_boxes.append(bb)
                turn_box.append(bb)
            else:
                turn_box.append(acc)

        if turn.research is not None:
            turn_box.append(
                ResearchCard(
                    turn.research,
                    on_open_reference=self._lightbox.open_reference,
                )
            )

        if turn.handoff is not None:
            h_card = MemoryHandoffCard(turn.handoff)
            self._handoff_cards[turn.id] = h_card
            turn_box.append(h_card)

        if turn.prose:
            tabs_for_cite = turn.research.tabs if turn.research is not None else None
            prose_w = AssistantProseView(
                turn.prose,
                tabs=tabs_for_cite,
                on_toast=self.show_toast,
                on_quote=self._on_quote_selection,
                on_select_all_chat=self.open_chat_selector,
                on_select_camera=self._on_camera_angle_selected,
            )
            prose_txt = " ".join(
                [b.text for b in turn.prose if b.text] + [it for b in turn.prose for it in b.items]
            )
            if is_explicit_text(prose_txt):
                bb = SafeBlurBox(
                    prose_w,
                    explicit=True,
                    safe_blur_enabled=True,
                    blur_radius=36.0,
                )
                self._safe_blur_boxes.append(bb)
                turn_box.append(bb)
            else:
                turn_box.append(prose_w)

        if turn.prompt_spec is not None:
            pb = PromptBreakdownBlock(
                turn.prompt_spec,
                on_toast=self.show_toast,
                on_quote=self._on_quote_selection,
            )
            if is_explicit_text(turn.prompt_spec.raw_prompt):
                bb = SafeBlurBox(
                    pb,
                    explicit=True,
                    safe_blur_enabled=True,
                    blur_radius=36.0,
                )
                self._safe_blur_boxes.append(bb)
                turn_box.append(bb)
            else:
                turn_box.append(pb)

        hold_ms = 3000 if animate_media_reveal else 0

        grid_created: KeyframeGrid | None = None
        if turn.keyframes:
            grid = KeyframeGrid(
                keyframes=turn.keyframes,
                initial_count=turn.keyframe_batch_default,
                on_open_lightbox=self._lightbox.open_keyframes,
                min_placeholder_ms=hold_ms,
                prompt_hint=resolved_hint,
                safe_blur_enabled=True,
            )
            grid_created = grid
            self._keyframe_grids.append(grid)
            turn_box.append(grid)

        if turn.video_clip is not None:
            vc = VideoPlayerCard(
                clip=turn.video_clip,
                on_open_lightbox=self._lightbox.open_video,
                min_placeholder_ms=hold_ms,
                prompt_hint=resolved_hint,
                safe_blur_enabled=True,
            )
            self._video_cards.append(vc)
            turn_box.append(vc)

        self._stream_box.append(turn_box)
        if return_grid:
            return grid_created
        return None

    def _scroll_to_bottom(self) -> bool:
        adj = self._stream_scroll.get_vadjustment()
        adj.set_value(max(0.0, adj.get_upper() - adj.get_page_size()))
        return False

    # -----------------------------------------------------------------------
    # Message Submission
    # -----------------------------------------------------------------------

    def _on_user_submit(self, prompt: str, skill: str, batch_count: int, clip_seconds: int) -> None:
        if self._active_chat is None:
            self.new_chat()
        chat = self._active_chat
        assert chat is not None

        if not chat.turns:
            short_title = prompt[:38].strip() + ("…" if len(prompt) > 38 else "")
            chat.title = short_title or "New chat"
            chat.active_skill = chat.active_skill if chat.mode_locked else skill
            if chat not in self._chats:
                self._chats.insert(0, chat)
            self._header_title.set_text(chat.title)
            self._header_title_blur.set_explicit(is_explicit_text(chat.title))
            self._rebuild_sidebar_list()
            self._sidebar_nav.set_active_index(0, animate=self.get_realized(), notify=False)
            self._canvas_stack.set_visible_child_name("stream")
            self._composer.set_aquarium_mode(False, animate=True)

        effective_skill = chat.active_skill if chat.mode_locked else skill
        user_turn = ConversationTurn(id=str(uuid.uuid4()), role="user", text=prompt)
        chat.turns.append(user_turn)
        save_chats(self._chats)
        self._append_turn_widget(user_turn)

        self._remove_thinking_card()
        self._thinking_card = ThinkingLiveCard(explicit_hint=is_explicit_text(prompt))
        self._stream_box.append(self._thinking_card)
        GLib.idle_add(self._scroll_to_bottom)

        self._suppress_auto_dequeue = False
        self._composer.set_busy(True)
        self._set_prompt_mascot_state("thinking", "Thinking…", force_live=True)
        self._controller.handle_submit(chat.id, prompt, effective_skill, batch_count, clip_seconds)

    def _on_stop_clicked(self) -> None:
        self._suppress_auto_dequeue = True
        self._controller.cancel_active()
        self._waiting_high_ram_ui = False
        self._remove_thinking_card()
        self._composer.set_busy(False)
        self._sync_queue_ui()
        self._set_prompt_mascot_state("idle", force_live=True)
        self.show_toast("Stopped")

    def _on_composer_batch_changed(self, count: int) -> None:
        for grid in self._keyframe_grids:
            grid.set_count(count)

    # -----------------------------------------------------------------------
    # Actions, Cleanup, & Keyboard Shortcuts
    # -----------------------------------------------------------------------

    def _build_chat_selector_overlay(self) -> Gtk.Revealer:
        rev = Gtk.Revealer()
        rev.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        rev.set_transition_duration(180)
        rev.set_reveal_child(False)
        rev.set_visible(False)

        scrim = Gtk.Overlay()
        bg = Gtk.Box()
        bg.add_css_class("lightbox-scrim")
        bg.set_hexpand(True)
        bg.set_vexpand(True)
        scrim.set_child(bg)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        center.add_css_class("chat-select-card")
        center.set_size_request(760, 560)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr = Gtk.Label(label="Select & Copy Full Conversation")
        hdr.add_css_class("research-perm-title")
        hdr.set_hexpand(True)
        hdr.set_xalign(0.0)
        top.append(hdr)

        close_b = Gtk.Button(label="Close")
        close_b.set_has_frame(False)
        close_b.add_css_class("mini-action-btn")
        close_b.set_cursor_from_name("pointer")
        close_b.connect("clicked", lambda _b: self.close_chat_selector())
        top.append(close_b)
        center.append(top)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)

        self._chat_select_tv = Gtk.TextView()
        self._chat_select_tv.add_css_class("prose-textview")
        self._chat_select_tv.set_editable(False)
        self._chat_select_tv.set_cursor_visible(True)
        self._chat_select_tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll.set_child(self._chat_select_tv)
        center.append(scroll)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sel_all = Gtk.Button(label="Select Everything")
        sel_all.set_has_frame(False)
        sel_all.add_css_class("mini-action-btn")
        sel_all.set_cursor_from_name("pointer")
        sel_all.connect("clicked", lambda _b: self._chat_select_all_in_dialog())
        bar.append(sel_all)

        ask_sel = Gtk.Button(label="↳ Ask Vimona about selection")
        ask_sel.set_has_frame(False)
        ask_sel.add_css_class("mini-action-btn")
        ask_sel.set_cursor_from_name("pointer")
        ask_sel.connect("clicked", lambda _b: self._quote_from_chat_selector())
        bar.append(ask_sel)

        copy_sel = Gtk.Button(label="Copy Selection")
        copy_sel.set_has_frame(False)
        copy_sel.add_css_class("mini-action-btn")
        copy_sel.set_cursor_from_name("pointer")
        copy_sel.connect("clicked", lambda _b: self._copy_from_chat_selector(only_selection=True))
        bar.append(copy_sel)

        copy_all = Gtk.Button(label="Copy Entire Chat")
        copy_all.add_css_class("ref-render-btn")
        copy_all.set_cursor_from_name("pointer")
        copy_all.connect("clicked", lambda _b: self._copy_from_chat_selector(only_selection=False))
        bar.append(copy_all)
        center.append(bar)

        scrim.add_overlay(center)
        rev.set_child(scrim)
        return rev

    def open_chat_selector(self) -> None:
        chat = self._active_chat
        lines: list[str] = []
        if chat is not None:
            lines.append(f"{chat.title}")
            lines.append("=" * min(48, max(8, len(chat.title))))
            lines.append("")
            for turn in chat.turns:
                if turn.role == "user":
                    if turn.text:
                        lines.append(f"You:\n{turn.text}\n")
                else:
                    parts: list[str] = []
                    for blk in turn.prose:
                        if blk.text:
                            parts.append(blk.text)
                        elif blk.items:
                            parts.extend(f"• {it}" for it in blk.items)
                    if turn.prompt_spec is not None:
                        for pk, pv in turn.prompt_spec.tokens:
                            parts.append(f"{pk}: {pv}")
                    if turn.research is not None and turn.research.tabs:
                        parts.append(
                            "Sources: "
                            + " · ".join(f"[{i+1}] {tb.title} ({tb.url})" for i, tb in enumerate(turn.research.tabs))
                        )
                    if parts:
                        lines.append("Vimona:\n" + "\n".join(parts) + "\n")
        buf = self._chat_select_tv.get_buffer()
        buf.set_text("\n".join(lines).strip() or "No messages in this chat yet.")
        self._chat_select_rev.set_visible(True)
        self._chat_select_rev.set_reveal_child(True)
        self._chat_select_tv.grab_focus()

    def close_chat_selector(self) -> None:
        self._chat_select_rev.set_reveal_child(False)
        GLib.timeout_add(190, lambda: (self._chat_select_rev.set_visible(False), False)[1])

    def _chat_select_all_in_dialog(self) -> None:
        buf = self._chat_select_tv.get_buffer()
        buf.select_range(buf.get_start_iter(), buf.get_end_iter())
        self._chat_select_tv.grab_focus()

    def _quote_from_chat_selector(self) -> None:
        buf = self._chat_select_tv.get_buffer()
        bounds = buf.get_selection_bounds()
        if not bounds:
            self.show_toast("Highlight any text first")
            return
        txt = buf.get_text(bounds[0], bounds[1], False).strip()
        if txt:
            self.close_chat_selector()
            self._on_quote_selection(txt)

    def _copy_from_chat_selector(self, only_selection: bool) -> None:
        buf = self._chat_select_tv.get_buffer()
        if only_selection:
            bounds = buf.get_selection_bounds()
            if not bounds:
                self.show_toast("Highlight text first, or click Copy Entire Chat")
                return
            txt = buf.get_text(bounds[0], bounds[1], False).strip()
        else:
            txt = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if txt:
            copy_to_clipboard(self, txt)
            self.show_toast("Selection copied" if only_selection else "Entire chat copied")

    def _on_quote_selection(self, snippet: str) -> None:
        self._composer.set_quoted_text(snippet)

    def _on_camera_angle_selected(self, angle: str) -> None:
        self._composer.set_camera_angle(angle)
        self.show_toast(f"Camera framing: {angle}")

    def toggle_sidebar(self) -> None:
        self._split_view.set_show_sidebar(not self._split_view.get_show_sidebar())

    def _build_new_chat_mode_overlay(self) -> Gtk.Revealer:
        rev = Gtk.Revealer()
        rev.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        rev.set_transition_duration(180)
        rev.set_reveal_child(False)
        rev.set_visible(False)

        scrim = Gtk.Overlay()
        bg = Gtk.Box()
        bg.add_css_class("lightbox-scrim")
        bg.set_hexpand(True)
        bg.set_vexpand(True)
        bg_click = Gtk.GestureClick()
        bg_click.connect("released", lambda *_a: self.select_new_chat_mode(None))
        bg.add_controller(bg_click)
        scrim.set_child(bg)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.add_css_class("new-chat-mode-dialog")
        card.set_size_request(580, -1)
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label(label="What are we creating in this chat?")
        title.add_css_class("new-chat-dialog-title")
        title.set_hexpand(True)
        title.set_xalign(0.0)
        top.append(title)

        dismiss_b = Gtk.Button(label="×")
        dismiss_b.set_has_frame(False)
        dismiss_b.add_css_class("mini-action-btn")
        dismiss_b.set_cursor_from_name("pointer")
        dismiss_b.connect("clicked", lambda _b: self.select_new_chat_mode(None))
        top.append(dismiss_b)
        card.append(top)

        sub = Gtk.Label(
            label="Choose Make Images or Make Video to dedicate this entire chat to that mode, or keep it open."
        )
        sub.add_css_class("research-perm-note")
        sub.set_wrap(True)
        sub.set_xalign(0.0)
        card.append(sub)

        primary_row = Gtk.Grid()
        primary_row.set_column_spacing(12)
        primary_row.set_column_homogeneous(True)

        media_choices = [
            (
                "Make Images",
                "gallery",
                "Make Images",
                "1–8 fast local Pony V6 XL keyframes per prompt.",
            ),
            (
                "Make Video",
                "play",
                "Make Video",
                "Keyframes + 24fps optical-flow Wan 2.1 clips.",
            ),
        ]
        for col_i, (mode_val, icon_name, label, desc) in enumerate(media_choices):
            btn = Gtk.Button()
            btn.set_has_frame(False)
            btn.add_css_class("new-chat-mode-card")
            btn.set_cursor_from_name("pointer")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            ic_wrap = Gtk.Box()
            ic_wrap.add_css_class("new-chat-icon-pill")
            ic_wrap.set_halign(Gtk.Align.START)
            ic_wrap.set_valign(Gtk.Align.CENTER)
            ic_w = SvgIcon(icon_name, size_px=18, color="#93C5FD")
            ic_w.set_halign(Gtk.Align.CENTER)
            ic_w.set_valign(Gtk.Align.CENTER)
            ic_w.set_hexpand(True)
            ic_wrap.append(ic_w)
            head.append(ic_wrap)

            lock_tag = Gtk.Label(label="Locks Chat")
            lock_tag.add_css_class("new-chat-lock-tag")
            lock_tag.set_halign(Gtk.Align.END)
            lock_tag.set_valign(Gtk.Align.START)
            lock_tag.set_hexpand(True)
            head.append(lock_tag)
            inner.append(head)

            tl = Gtk.Label(label=label)
            tl.add_css_class("research-perm-title")
            tl.set_xalign(0.0)
            inner.append(tl)

            dl = Gtk.Label(label=desc)
            dl.add_css_class("research-perm-note")
            dl.set_wrap(True)
            dl.set_xalign(0.0)
            inner.append(dl)

            btn.set_child(inner)
            btn.connect("clicked", lambda _b, mv=mode_val: self.select_new_chat_mode(mv))
            primary_row.attach(btn, col_i, 0, 1, 1)
        card.append(primary_row)

        open_btn = Gtk.Button()
        open_btn.set_has_frame(False)
        open_btn.add_css_class("new-chat-mode-card")
        open_btn.set_cursor_from_name("pointer")
        orow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        orow.append(SvgIcon("globe", size_px=16, color=COLORS.accent_primary))
        otbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        otbox.set_hexpand(True)
        otl = Gtk.Label(label="Chat & Web References (Unlocked)")
        otl.add_css_class("research-perm-title")
        otl.set_xalign(0.0)
        odl = Gtk.Label(label="Brainstorm, pull zero-trace /dev/shm Firefox refs, or switch freely per turn.")
        odl.add_css_class("research-perm-note")
        odl.set_wrap(True)
        odl.set_xalign(0.0)
        otbox.append(otl)
        otbox.append(odl)
        orow.append(otbox)
        open_btn.set_child(orow)
        open_btn.connect("clicked", lambda _b: self.select_new_chat_mode(None))
        card.append(open_btn)

        scrim.add_overlay(card)
        rev.set_child(scrim)
        return rev

    def select_new_chat_mode(self, mode: str | None) -> None:
        self._new_chat_mode_rev.set_reveal_child(False)
        GLib.timeout_add(190, lambda: (self._new_chat_mode_rev.set_visible(False), False)[1])
        chat = self._active_chat
        if chat is None:
            return
        if mode in ("Make Images", "Make Video"):
            chat.active_skill = mode
            chat.mode_locked = True
            self._composer.set_locked_mode(mode)
            self._sync_header_mode_and_models(mode, locked=True)
            self.show_toast(f"Chat locked to {mode}")
        else:
            chat.active_skill = "Chat & Ideas"
            chat.mode_locked = False
            self._composer.set_locked_mode(None)
            self._sync_header_mode_and_models("Chat & Ideas", locked=False)
        save_chats(self._chats)
        if hasattr(self, "_composer"):
            self._composer.focus_input()

    def new_chat(self, ask_mode: bool = True) -> None:
        draft = ChatSession(id=str(uuid.uuid4()), title="New chat")
        self._load_session(draft)
        if ask_mode and hasattr(self, "_new_chat_mode_rev"):
            self._new_chat_mode_rev.set_visible(True)
            self._new_chat_mode_rev.set_reveal_child(True)
        elif hasattr(self, "_composer"):
            self._composer.focus_input()

    def show_toast(self, message: str) -> bool:
        if self._toast_timer is not None:
            GLib.source_remove(self._toast_timer)
            self._toast_timer = None
        self._toast_lbl.set_text(message)
        self._toast_revealer.set_reveal_child(True)

        def _hide() -> bool:
            self._toast_timer = None
            self._toast_revealer.set_reveal_child(False)
            return False

        self._toast_timer = GLib.timeout_add(1900, _hide)
        return False

    def _toggle_notifications(self) -> None:
        self._notifications_enabled = not self._notifications_enabled
        if self._notifications_enabled:
            self._notify_icon.set_icon("bell", COLORS.accent_primary)
            if hasattr(self, "_notify_state_pill"):
                self._notify_state_pill.set_text("On")
            self._notify_btn.set_tooltip_text("Desktop notifications when AI finishes (On)")
            self.show_toast("Completion notifications On")
        else:
            self._notify_icon.set_icon("bell-off", COLORS.text_muted)
            if hasattr(self, "_notify_state_pill"):
                self._notify_state_pill.set_text("Off")
            self._notify_btn.set_tooltip_text("Desktop notifications when AI finishes (Off)")
            self.show_toast("Completion notifications Off")

    @staticmethod
    def _skill_to_model_category(skill: str) -> str:
        low = (skill or "").lower()
        if "video" in low:
            return "video"
        if "image" in low:
            return "image"
        return "text"

    def _on_composer_skill_changed(self, skill: str) -> None:
        chat = getattr(self, "_active_chat", None)
        locked = bool(chat.mode_locked) if chat is not None else False
        if chat is not None and not locked:
            chat.active_skill = skill
        self._sync_header_mode_and_models(skill, locked=locked)

    def _sync_header_mode_and_models(self, skill: str, locked: bool = False) -> None:
        if hasattr(self, "_header_mode_pill") and hasattr(self, "_header_mode_btn"):
            low = (skill or "").lower()
            for cls in ("mode-chat", "mode-images", "mode-video"):
                self._header_mode_btn.remove_css_class(cls)
            if "video" in low:
                self._header_mode_pill.set_text("Video")
                self._header_mode_btn.add_css_class("mode-video")
            elif "image" in low:
                self._header_mode_pill.set_text("Images")
                self._header_mode_btn.add_css_class("mode-images")
            elif "search" in low or "web" in low:
                self._header_mode_pill.set_text("Web")
                self._header_mode_btn.add_css_class("mode-chat")
            else:
                self._header_mode_pill.set_text("Chat")
                self._header_mode_btn.add_css_class("mode-chat")
        self._sync_hf_models_button_label(skill)

    def _sync_hf_models_button_label(self, skill: str) -> None:
        if not hasattr(self, "_hf_models_lbl"):
            return
        cat = self._skill_to_model_category(skill)
        self._hf_model_tab = cat
        sel_id = get_selected_model_id(cat, hw=self._hw_profile)
        models = get_models_for_category(cat, hw=self._hw_profile)
        active = next((m for m in models if m.id == sel_id), models[0] if models else None)
        if active is not None:
            self._hf_models_lbl.set_text(active.name)
        else:
            self._hf_models_lbl.set_text("Models")
        if hasattr(self, "_hf_models_popover") and self._hf_models_popover.get_visible():
            self._rebuild_hf_models_popover(cat)

    def _build_header_mode_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_size_request(210, -1)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)

        for lbl_txt, desc_txt, mode_val in (
            ("Chat", "Text, code, & web refs", None),
            ("Images", "Pony / SDXL keyframe studio", "Make Images"),
            ("Video", "Wan 2.1 / ComfyUI motion", "Make Video"),
        ):
            b = Gtk.Button()
            b.set_has_frame(False)
            b.add_css_class("popover-item")
            b.set_cursor_from_name("pointer")
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            t = Gtk.Label(label=lbl_txt)
            t.set_xalign(0.0)
            d = Gtk.Label(label=desc_txt)
            d.add_css_class("session-row-meta")
            d.set_xalign(0.0)
            vb.append(t)
            vb.append(d)
            b.set_child(vb)
            b.connect("clicked", lambda _btn, mv=mode_val, p=pop: (p.popdown(), self.select_new_chat_mode(mv)))
            box.append(b)
        pop.set_child(box)
        return pop

    def _on_hf_popover_shown(self, pop: Gtk.Popover, _pspec: object) -> None:
        if pop.get_visible():
            cur_skill = self._composer._active_skill if hasattr(self, "_composer") else "Chat & Ideas"
            tab = self._skill_to_model_category(cur_skill)
            self._hf_model_tab = tab
            self._rebuild_hf_models_popover(tab)

    def _rebuild_hf_models_popover(self, category: str) -> None:
        self._hf_model_tab = category
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_size_request(430, 440)

        # Header + Hardware summary banner
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        htitle = Gtk.Label(label="Hugging Face Models")
        htitle.add_css_class("new-chat-dialog-title")
        htitle.set_xalign(0.0)
        htitle.set_hexpand(True)
        top.append(htitle)
        root.append(top)

        hw_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        hw_banner.add_css_class("hf-hw-banner")
        hw_dot = Gtk.Label(label="●")
        hw_dot.add_css_class("hf-hw-dot")
        hw_banner.append(hw_dot)
        hw_lbl = Gtk.Label(label=f"Hardware Tuned · {self._hw_profile.summary_label}")
        hw_lbl.set_xalign(0.0)
        hw_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        hw_lbl.set_hexpand(True)
        hw_banner.append(hw_lbl)
        root.append(hw_banner)

        # Mode category tabs (automatically lands on Video for Video Chat, Image for Images, Text for Chat)
        tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for cat_key, cat_title in (("text", "Text Models"), ("image", "Image Models"), ("video", "Video Models")):
            tb = Gtk.Button(label=cat_title)
            tb.set_has_frame(False)
            tb.add_css_class("quick-select-chip")
            if cat_key == category:
                tb.add_css_class("selected")
            tb.set_cursor_from_name("pointer")
            tb.connect("clicked", lambda _b, c=cat_key: self._rebuild_hf_models_popover(c))
            tabs.append(tb)
        root.append(tabs)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)

        cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        cards.set_margin_end(6)
        selected_id = get_selected_model_id(category, hw=self._hw_profile)
        models = get_models_for_category(category, hw=self._hw_profile)
        total_in_cat = sum(1 for m in CURATED_HF_MODELS if m.category == category.lower())
        hidden_cnt = max(0, total_in_cat - len(models))

        if hidden_cnt > 0:
            flt_lbl = Gtk.Label(
                label=(
                    f"Showing {len(models)} tuned {category} model{'s' if len(models) != 1 else ''} "
                    f"({hidden_cnt} heavy/unsuited hidden)"
                )
            )
            flt_lbl.add_css_class("hf-filter-note")
            flt_lbl.set_xalign(0.0)
            flt_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            cards.append(flt_lbl)

        if not models:
            empty_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            empty_card.add_css_class("hf-model-card")
            et = Gtk.Label(label=f"No {category} models match this hardware cleanly")
            et.add_css_class("hf-model-title")
            et.set_xalign(0.0)
            ed = Gtk.Label(
                label=f"Detected {self._hw_profile.summary_label} lacks sufficient GPU/RAM headroom for {category} weights."
            )
            ed.add_css_class("hf-desc-lbl")
            ed.set_wrap(True)
            ed.set_xalign(0.0)
            empty_card.append(et)
            empty_card.append(ed)
            cards.append(empty_card)

        for spec in models:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            card.add_css_class("hf-model-card")
            if spec.id == selected_id:
                card.add_css_class("selected")

            r1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            nm = Gtk.Label(label=spec.name)
            nm.add_css_class("hf-model-title")
            nm.set_xalign(0.0)
            nm.set_hexpand(True)
            r1.append(nm)

            fit_tier, fit_txt = evaluate_model_fit(spec, self._hw_profile)
            fit_badge = Gtk.Label(label=fit_txt)
            fit_badge.add_css_class(
                "hf-fit-best" if fit_tier == "best_fit" else ("hf-fit-fits" if fit_tier == "fits" else "hf-fit-heavy")
            )
            r1.append(fit_badge)
            card.append(r1)

            meta_lbl = Gtk.Label(label=f"{spec.hf_repo} · {spec.quant}")
            meta_lbl.add_css_class("hf-repo-meta")
            meta_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            meta_lbl.set_xalign(0.0)
            card.append(meta_lbl)

            desc_lbl = Gtk.Label(label=spec.summary)
            desc_lbl.add_css_class("hf-desc-lbl")
            desc_lbl.set_wrap(True)
            desc_lbl.set_xalign(0.0)
            card.append(desc_lbl)

            act_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            act_row.set_margin_top(2)
            local_p = find_local_model_path(spec)
            dl_state = self._hf_dl_progress.get(spec.id)

            hf_stat = Gtk.Label(label="Installed ✓" if local_p else "Hugging Face Hub")
            hf_stat.add_css_class("hf-stat-lbl")
            hf_stat.set_hexpand(True)
            hf_stat.set_xalign(0.0)
            hf_stat.set_valign(Gtk.Align.CENTER)
            act_row.append(hf_stat)

            # Query lightweight live Hugging Face API stats asynchronously
            def _fetch_stats(repo=spec.hf_repo, lbl=hf_stat, installed=bool(local_p)) -> None:
                meta = fetch_hf_repo_meta(repo)
                if meta:
                    dl_k = meta["downloads"]
                    dl_str = f"{dl_k // 1000}k downloads" if dl_k >= 1000 else f"{dl_k} downloads"
                    txt = f"{'Installed · ' if installed else ''}♥ {meta['likes']} · {dl_str}"
                    GLib.idle_add(lambda l=lbl, t=txt: (l.set_text(t) if l.get_parent() is not None else None, False)[1])

            threading.Thread(target=_fetch_stats, daemon=True).start()

            if dl_state is not None:
                frac, dl_msg = dl_state
                pb = Gtk.ProgressBar()
                pb.set_fraction(frac)
                pb.set_text(dl_msg)
                pb.set_show_text(True)
                pb.set_hexpand(True)
                card.append(pb)
            elif local_p is not None:
                is_cur = spec.id == selected_id
                if is_cur:
                    pill = Gtk.Label(label="Active ✓")
                    pill.add_css_class("hf-active-pill")
                    pill.set_valign(Gtk.Align.CENTER)
                    act_row.append(pill)
                else:
                    btn = Gtk.Button(label="Use Model")
                    btn.set_has_frame(False)
                    btn.add_css_class("hf-use-btn")
                    btn.set_cursor_from_name("pointer")
                    btn.set_valign(Gtk.Align.CENTER)
                    btn.connect("clicked", lambda _b, s=spec: self._select_hf_model(s))
                    act_row.append(btn)
            else:
                btn = Gtk.Button(label=f"Get · {spec.size_gb:.1f} GB")
                btn.set_has_frame(False)
                btn.add_css_class("hf-get-btn")
                btn.set_cursor_from_name("pointer")
                btn.set_valign(Gtk.Align.CENTER)
                btn.connect("clicked", lambda _b, s=spec: self._download_and_select_hf_model(s))
                act_row.append(btn)

            card.append(act_row)
            cards.append(card)

        scroll.set_child(cards)
        root.append(scroll)
        self._hf_models_popover.set_child(root)

    def _select_hf_model(self, spec: HFModelSpec) -> None:
        set_selected_model_id(spec.category, spec.id)
        cur_skill = self._composer._active_skill if hasattr(self, "_composer") else "Chat & Ideas"
        self._sync_hf_models_button_label(cur_skill)
        self._rebuild_hf_models_popover(spec.category)
        self.show_toast(f"Selected {spec.name} ({spec.quant})")

    def _download_and_select_hf_model(self, spec: HFModelSpec) -> None:
        if spec.id in self._hf_dl_progress:
            return
        self._hf_dl_progress[spec.id] = (0.0, "Connecting to Hugging Face…")
        self._rebuild_hf_models_popover(spec.category)

        def _worker() -> None:
            try:
                def _prog(frac: float, msg: str) -> None:
                    self._hf_dl_progress[spec.id] = (frac, msg)
                    GLib.idle_add(lambda: (self._rebuild_hf_models_popover(spec.category), False)[1])

                download_hf_model(spec, on_progress=_prog, cancel_event=self._hf_dl_cancel)
                self._hf_dl_progress.pop(spec.id, None)
                GLib.idle_add(lambda: (self._select_hf_model(spec), False)[1])
            except Exception as exc:
                self._hf_dl_progress.pop(spec.id, None)
                err_txt = str(exc)
                GLib.idle_add(
                    lambda: (
                        self._rebuild_hf_models_popover(spec.category),
                        self.show_toast(f"HF download failed: {err_txt[:80]}"),
                        False,
                    )[2]
                )

        threading.Thread(target=_worker, daemon=True, name=f"hf-dl-{spec.id}").start()

    def focus_chat_by_id(self, chat_id: str) -> None:
        found = next((c for c in self._chats if c.id == chat_id), None)
        if found is not None and self._active_chat is not found:
            self._load_session(found)
        elif self._canvas_stack.get_visible_child_name() == "gallery":
            self._close_gallery_to_chat()

    def present_for_notification(self, chat_id: str = "") -> bool:
        """Raise and focus Vimona (unminimizing if needed) on the specified chat."""
        if chat_id:
            self.focus_chat_by_id(chat_id)
        if not self.get_visible():
            self.set_visible(True)
        self.unminimize()
        self.present()
        return False

    def _has_desktop_registration(self) -> bool:
        desktop_name = f"{__app_id__}.desktop"
        service_name = f"{__app_id__}.service"
        has_desktop = any(
            (base / desktop_name).is_file()
            for base in (
                Path("~/.local/share/applications").expanduser(),
                Path("/usr/share/applications"),
                Path("/usr/local/share/applications"),
            )
        )
        has_service = any(
            (base / service_name).is_file()
            for base in (
                Path("~/.local/share/dbus-1/services").expanduser(),
                Path("/usr/share/dbus-1/services"),
            )
        )
        return has_desktop and has_service

    def notify_turn_finished(
        self,
        chat_id: str,
        title: str,
        body: str,
        sensitive: bool = False,
    ) -> bool:
        """Notify the user in-app and via native OS desktop notification when AI finishes."""
        if not self._notifications_enabled:
            return False
        if (
            self._playlist_batch_active
            and self._quiet_batch_alerts
            and title != "Batch Complete"
        ):
            return False

        target_chat = next((c for c in self._chats if c.id == chat_id), None)
        chat_sens = bool(target_chat and is_explicit_text(target_chat.title))
        is_sens = bool(sensitive or chat_sens or is_explicit_text(title) or is_explicit_text(body))

        clean_title = re.sub(r"\s+", " ", (title or "Vimona finished")).strip()
        clean_body = re.sub(r"\s+", " ", (body or "")).strip()
        if len(clean_body) > 120:
            clean_body = clean_body[:117].rstrip() + "…"

        if is_sens:
            desk_body = "Safe-Blur protected result ready inside Vimona."
            toast_msg = f"{clean_title} · Safe-Blur protected"
        else:
            desk_body = clean_body or "Response ready."
            toast_msg = f"{clean_title} · {desk_body[:54]}" if desk_body else clean_title

        self._last_notification = {
            "chat_id": chat_id,
            "title": clean_title,
            "body": desk_body,
            "sensitive": is_sens,
        }
        self.show_toast(toast_msg)

        if os.environ.get("VIMONA_DISABLE_OS_NOTIFY") == "1":
            return False

        app = self.get_application()
        if app is not None and self._has_desktop_registration():
            try:
                notif = Gio.Notification.new(clean_title)
                notif.set_body(desk_body)
                notif.set_icon(Gio.ThemedIcon.new(__app_id__))
                notif.set_priority(Gio.NotificationPriority.HIGH)
                target_var = GLib.Variant.new_string(chat_id or "")
                notif.set_default_action_and_target("app.focus-chat", target_var)
                notif.add_button_with_target("Open Vimona", "app.focus-chat", target_var)
                app.send_notification("vimona-turn-done", notif)
                return False
            except Exception:
                pass

        notify_bin = shutil.which("notify-send")
        if notify_bin:
            def _run_interactive_notify() -> None:
                try:
                    res = subprocess.run(
                        [
                            notify_bin,
                            "--app-name=Vimona",
                            f"--icon={__app_id__}",
                            "--action=default=Open Vimona",
                            "--wait",
                            clean_title,
                            desk_body,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if res.stdout.strip():
                        GLib.idle_add(self.present_for_notification, chat_id)
                except Exception:
                    pass

            threading.Thread(
                target=_run_interactive_notify,
                daemon=True,
                name="vimona-notify-wait",
            ).start()
        return False

    def shutdown_backend(self) -> None:
        if self._telemetry_timer is not None:
            GLib.source_remove(self._telemetry_timer)
            self._telemetry_timer = None
        if self._mascot_done_timer is not None:
            GLib.source_remove(self._mascot_done_timer)
            self._mascot_done_timer = None
        if self._cooldown_timer is not None:
            GLib.source_remove(self._cooldown_timer)
            self._cooldown_timer = None
        if hasattr(self, "_ram_menu_btn") and self._ram_popover is not None:
            self._ram_menu_btn.set_popover(None)
            self._ram_popover = None
        self._controller.shutdown()
        cleanup_ephemeral_media(include_current=True)

    def _on_close_request(self, _win: Gtk.Window) -> bool:
        self.shutdown_backend()
        return False

    def _on_global_key(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)

        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if hasattr(self, "_new_chat_mode_rev") and self._new_chat_mode_rev.get_reveal_child():
            if keyval == Gdk.KEY_Escape:
                self.select_new_chat_mode(None)
                return True
        if hasattr(self, "_skills_mgr") and self._skills_mgr.get_reveal_child():
            if keyval == Gdk.KEY_Escape:
                self._skills_mgr.close()
                return True
        if self._chat_select_rev.get_reveal_child():
            if keyval == Gdk.KEY_Escape:
                self.close_chat_selector()
                return True
        if self._lightbox.get_reveal_child():
            if keyval == Gdk.KEY_Escape:
                self._lightbox.close()
                return True
            if keyval in (Gdk.KEY_Left, Gdk.KEY_comma):
                self._lightbox.navigate(-1)
                return True
            if keyval in (Gdk.KEY_Right, Gdk.KEY_period):
                self._lightbox.navigate(1)
                return True
            if keyval == Gdk.KEY_space:
                self._lightbox.toggle_video_play()
                return True
            if keyval in (Gdk.KEY_plus, Gdk.KEY_equal):
                self._lightbox.adjust_zoom(0.35)
                return True
            if keyval == Gdk.KEY_minus:
                self._lightbox.adjust_zoom(-0.35)
                return True
            if keyval == Gdk.KEY_0:
                self._lightbox.reset_view()
                return True
            if keyval in (Gdk.KEY_i, Gdk.KEY_I):
                self._lightbox.toggle_drawer()
                return True

        if ctrl and shift and keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.open_chat_selector()
            return True
        if ctrl and keyval in (Gdk.KEY_n, Gdk.KEY_N):
            self.new_chat()
            return True
        if ctrl and keyval in (Gdk.KEY_backslash, Gdk.KEY_b, Gdk.KEY_B):
            self.toggle_sidebar()
            return True
        if ctrl and keyval in (Gdk.KEY_k, Gdk.KEY_K):
            self._composer.focus_input()
            return True

        return False
