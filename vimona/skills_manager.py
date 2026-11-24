"""Dark-mode Skills Manager modal with `.vimona-skill` import/export, creation form, and live router toggles."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .icons import SvgIcon
from .skills_loader import (
    SKILL_CATEGORIES,
    SkillSpec,
    delete_custom_skill,
    export_skill_file,
    import_skill_file,
    load_all_skills,
    save_custom_skill,
    set_skill_enabled,
)
from .tokens import COLORS


class SkillsManagerOverlay(Gtk.Revealer):
    """Centered dark-mode Skills Manager for enabling, creating, importing, and exporting skills."""

    def __init__(
        self,
        parent_window: Gtk.Window,
        on_toast: Callable[[str], None],
        on_skills_changed: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__()
        self._window = parent_window
        self._on_toast = on_toast
        self._on_skills_changed = on_skills_changed
        self._skills: list[SkillSpec] = []
        self._filter_cat: str = "All"
        self._search_text: str = ""
        self._creating: bool = False
        self._visible_limit: int = 45
        self._cat_btns: dict[str, Gtk.Button] = {}

        self.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.set_transition_duration(180)
        self.set_reveal_child(False)
        self.set_visible(False)

        scrim = Gtk.Overlay()
        bg = Gtk.Box()
        bg.add_css_class("lightbox-scrim")
        bg.set_hexpand(True)
        bg.set_vexpand(True)
        bg_click = Gtk.GestureClick()
        bg_click.connect("released", lambda *_a: self.close())
        bg.add_controller(bg_click)
        scrim.set_child(bg)

        self._card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._card.add_css_class("new-chat-mode-dialog")
        self._card.set_size_request(760, 590)
        self._card.set_halign(Gtk.Align.CENTER)
        self._card.set_valign(Gtk.Align.CENTER)

        # Support drag-and-drop of `.vimona-skill` files anywhere onto the dialog
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_file_drop)
        self._card.add_controller(drop)

        # Header row
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.append(SvgIcon("sparkles", size_px=16, color=COLORS.accent_primary))
        hdr = Gtk.Label(label="Skills Manager")
        hdr.add_css_class("new-chat-dialog-title")
        hdr.set_xalign(0.0)
        top.append(hdr)

        self._active_badge = Gtk.Label(label="0 active")
        self._active_badge.add_css_class("new-chat-lock-tag")
        self._active_badge.set_valign(Gtk.Align.CENTER)
        top.append(self._active_badge)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)

        self._import_btn = Gtk.Button(label="Import .vimona-skill")
        self._import_btn.set_has_frame(False)
        self._import_btn.add_css_class("modal-pill-btn")
        self._import_btn.set_cursor_from_name("pointer")
        self._import_btn.set_tooltip_text("Import or drag-and-drop a .vimona-skill JSON/YAML file")
        self._import_btn.connect("clicked", lambda _b: self._prompt_import())
        top.append(self._import_btn)

        self._create_btn = Gtk.Button(label="+ Create Skill")
        self._create_btn.add_css_class("ref-render-btn")
        self._create_btn.set_cursor_from_name("pointer")
        self._create_btn.connect("clicked", lambda _b: self._toggle_create_view(not self._creating))
        top.append(self._create_btn)

        close_b = Gtk.Button(label="×")
        close_b.set_has_frame(False)
        close_b.add_css_class("modal-close-btn")
        close_b.set_valign(Gtk.Align.CENTER)
        close_b.set_cursor_from_name("pointer")
        close_b.set_tooltip_text("Close (Esc)")
        close_b.connect("clicked", lambda _b: self.close())
        top.append(close_b)
        self._card.append(top)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(150)
        self._stack.set_vexpand(True)
        self._stack.set_hexpand(True)

        self._stack.add_named(self._build_browse_page(), "browse")
        self._stack.add_named(self._build_create_page(), "create")
        self._card.append(self._stack)

        scrim.add_overlay(self._card)
        self.set_child(scrim)
        self.refresh()

    def _build_browse_page(self) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.add_css_class("vimona-search-entry")
        self._search_entry.set_placeholder_text("Search skills, triggers, authors…")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        bar.append(self._search_entry)
        page.append(bar)

        cats_flow = Gtk.FlowBox()
        cats_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        cats_flow.set_homogeneous(False)
        cats_flow.set_column_spacing(6)
        cats_flow.set_row_spacing(6)
        cats_flow.set_min_children_per_line(4)
        cats_flow.set_max_children_per_line(12)
        for cat in ("All", *SKILL_CATEGORIES):
            b = Gtk.Button(label=cat)
            b.set_has_frame(False)
            b.add_css_class("quick-select-chip")
            if cat == "All":
                b.add_css_class("selected")
            b.set_cursor_from_name("pointer")
            b.connect("clicked", lambda _btn, c=cat: self._set_category_filter(c))
            self._cat_btns[cat] = b
            cats_flow.append(b)
        page.append(cats_flow)

        hint = Gtk.Label(
            label="Domain skill library indexed locally; only the top 2–3 matching skills are injected per turn for zero context bloat."
        )
        hint.add_css_class("research-perm-note")
        hint.set_wrap(True)
        hint.set_xalign(0.0)
        page.append(hint)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)

        self._list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._list_box.set_margin_end(6)
        scroll.set_child(self._list_box)
        page.append(scroll)
        return page

    def _build_create_page(self) -> Gtk.Box:
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        def _row(label: str, widget: Gtk.Widget) -> Gtk.Box:
            r = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("research-perm-title")
            lbl.set_xalign(0.0)
            r.append(lbl)
            r.append(widget)
            return r

        row1 = Gtk.Grid()
        row1.set_column_spacing(10)
        row1.set_column_homogeneous(True)
        self._f_name = Gtk.Entry()
        self._f_name.set_placeholder_text("e.g., Neon Noir Anamorphic")
        self._f_author = Gtk.Entry()
        self._f_author.set_text("User")
        self._f_version = Gtk.Entry()
        self._f_version.set_text("1.0.0")
        row1.attach(_row("Skill Name", self._f_name), 0, 0, 2, 1)
        row1.attach(_row("Author", self._f_author), 2, 0, 1, 1)
        row1.attach(_row("Version", self._f_version), 3, 0, 1, 1)
        form.append(row1)

        row2 = Gtk.Grid()
        row2.set_column_spacing(10)
        row2.set_column_homogeneous(True)
        self._f_cat = Gtk.DropDown.new_from_strings(list(SKILL_CATEGORIES))
        self._f_model = Gtk.DropDown.new_from_strings(["Pony V6 XL", "Wan 2.1", "Both"])
        self._f_triggers = Gtk.Entry()
        self._f_triggers.set_placeholder_text("noir, anamorphic, neon, rain, night")
        row2.attach(_row("Category", self._f_cat), 0, 0, 1, 1)
        row2.attach(_row("Target Model", self._f_model), 1, 0, 1, 1)
        row2.attach(_row("Trigger Keywords (comma-separated)", self._f_triggers), 2, 0, 2, 1)
        form.append(row2)

        self._f_instructions = Gtk.TextView()
        self._f_instructions.add_css_class("composer-textview")
        self._f_instructions.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._f_instructions.set_size_request(-1, 110)
        instr_frame = Gtk.Frame()
        instr_frame.set_child(self._f_instructions)
        form.append(_row("Prompt Engineering Rules (injected into Gemma)", instr_frame))

        self._f_neg = Gtk.Entry()
        self._f_neg.set_placeholder_text("flat lighting, washed out colors, jpeg noise")
        form.append(_row("Negative Prompt Patch (optional)", self._f_neg))

        ov_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._f_cfg = Gtk.Entry()
        self._f_cfg.set_placeholder_text("e.g., 1.8 (optional)")
        self._f_booster = Gtk.CheckButton(label="Booster Engine / Fast LoRA ON")
        self._f_booster.set_active(True)
        ov_row.append(_row("CFG Override", self._f_cfg))
        ov_row.append(self._f_booster)
        form.append(ov_row)

        scroll.set_child(form)
        wrap.append(scroll)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        cancel_b = Gtk.Button(label="Cancel")
        cancel_b.set_has_frame(False)
        cancel_b.add_css_class("mini-action-btn")
        cancel_b.set_cursor_from_name("pointer")
        cancel_b.connect("clicked", lambda _b: self._toggle_create_view(False))
        btns.append(cancel_b)

        save_b = Gtk.Button(label="Save .vimona-skill")
        save_b.add_css_class("ref-render-btn")
        save_b.set_cursor_from_name("pointer")
        save_b.connect("clicked", lambda _b: self._on_save_custom_clicked())
        btns.append(save_b)
        wrap.append(btns)
        return wrap

    def _toggle_create_view(self, creating: bool) -> None:
        self._creating = creating
        self._stack.set_visible_child_name("create" if creating else "browse")
        self._create_btn.set_label("Back to Skills" if creating else "+ Create Skill")

    def open(self) -> None:
        self._toggle_create_view(False)
        self.refresh()
        self.set_visible(True)
        self.set_reveal_child(True)

    def close(self) -> None:
        self.set_reveal_child(False)
        GLib.timeout_add(190, lambda: (self.set_visible(False), False)[1])

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip().lower()
        self._visible_limit = 45
        self._render_list()

    def _set_category_filter(self, cat: str) -> None:
        self._filter_cat = cat
        self._visible_limit = 45
        for c, b in self._cat_btns.items():
            if c == cat:
                b.add_css_class("selected")
            else:
                b.remove_css_class("selected")
        self._render_list()

    def refresh(self) -> int:
        self._skills = load_all_skills()
        enabled_cnt = sum(1 for s in self._skills if s.enabled)
        self._active_badge.set_text(f"{enabled_cnt}/{len(self._skills)} active")
        if self._on_skills_changed is not None:
            self._on_skills_changed(enabled_cnt)
        self._render_list()
        return enabled_cnt

    def _render_list(self) -> None:
        c = self._list_box.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self._list_box.remove(c)
            c = nxt

        matched: list[SkillSpec] = []
        for spec in self._skills:
            if self._filter_cat != "All" and spec.category.lower() != self._filter_cat.lower():
                continue
            if self._search_text:
                hay = f"{spec.name} {spec.author} {spec.category} {' '.join(spec.trigger_keywords)} {spec.system_instructions}".lower()
                if self._search_text not in hay:
                    continue
            matched.append(spec)

        for spec in matched[: self._visible_limit]:
            self._list_box.append(self._build_skill_card(spec))

        remaining = len(matched) - self._visible_limit
        if remaining > 0:
            more_btn = Gtk.Button(label=f"Show {remaining} more matching skills ({len(matched)} total)")
            more_btn.set_has_frame(False)
            more_btn.add_css_class("mini-action-btn")
            more_btn.set_cursor_from_name("pointer")
            more_btn.connect("clicked", lambda _b: self._show_more_skills())
            self._list_box.append(more_btn)

        if not matched:
            empty = Gtk.Label(label="No skills match this filter. Click '+ Create Skill' or import a '.vimona-skill' file.")
            empty.add_css_class("research-perm-note")
            empty.set_margin_top(24)
            self._list_box.append(empty)

    def _show_more_skills(self) -> None:
        self._visible_limit += 150
        self._render_list()

    def _build_skill_card(self, spec: SkillSpec) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("skill-card")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=spec.name)
        title.add_css_class("hf-model-title")
        title.set_xalign(0.0)
        head.append(title)

        cat_pill = Gtk.Label(label=spec.category)
        cat_pill.add_css_class("skill-cat-pill")
        cat_low = spec.category.lower()
        if "image" in cat_low:
            cat_pill.add_css_class("cat-image")
        elif "video" in cat_low:
            cat_pill.add_css_class("cat-video")
        elif "camera" in cat_low or "lighting" in cat_low:
            cat_pill.add_css_class("cat-camera")
        elif "web" in cat_low:
            cat_pill.add_css_class("cat-web")
        head.append(cat_pill)

        meta = Gtk.Label(label=f"v{spec.version} · {spec.author}")
        meta.add_css_class("skill-meta-lbl")
        meta.set_hexpand(True)
        meta.set_xalign(0.0)
        head.append(meta)

        exp_btn = Gtk.Button(label="Export")
        exp_btn.set_has_frame(False)
        exp_btn.add_css_class("skill-export-btn")
        exp_btn.set_cursor_from_name("pointer")
        exp_btn.set_tooltip_text("Export as shareable .vimona-skill file")
        exp_btn.connect("clicked", lambda _b, sp=spec: self._prompt_export(sp))
        head.append(exp_btn)

        if not spec.builtin:
            del_btn = Gtk.Button(label="Delete")
            del_btn.set_has_frame(False)
            del_btn.add_css_class("skill-export-btn")
            del_btn.set_cursor_from_name("pointer")
            del_btn.connect("clicked", lambda _b, sp=spec: self._on_delete_skill(sp))
            head.append(del_btn)

        sw = Gtk.Switch()
        sw.add_css_class("vimona-switch")
        sw.set_active(spec.enabled)
        sw.set_valign(Gtk.Align.CENTER)
        sw.connect(
            "state-set",
            lambda _s, state, sp=spec: (self._on_toggle_skill(sp, bool(state)), False)[1],
        )
        head.append(sw)
        card.append(head)

        if spec.trigger_keywords:
            trig = Gtk.Label(label="Triggers · " + ", ".join(spec.trigger_keywords[:8]))
            trig.add_css_class("skill-triggers-lbl")
            trig.set_ellipsize(Pango.EllipsizeMode.END)
            trig.set_xalign(0.0)
            card.append(trig)

        clean_body = re.sub(r"(?:^|\s)#+\s*", " ", " ".join(spec.system_instructions.splitlines()))
        clean_body = re.sub(r"\s{2,}", " ", clean_body).strip()
        preview_txt = clean_body[:175].strip()
        if len(clean_body) > 175:
            preview_txt += "…"
        desc = Gtk.Label(label=preview_txt)
        desc.add_css_class("skill-summary-lbl")
        desc.set_wrap(True)
        desc.set_xalign(0.0)
        card.append(desc)
        return card

    def _on_toggle_skill(self, spec: SkillSpec, enabled: bool) -> None:
        spec.enabled = enabled
        set_skill_enabled(spec.id, enabled)
        enabled_cnt = sum(1 for s in self._skills if s.enabled)
        self._active_badge.set_text(f"{enabled_cnt}/{len(self._skills)} active")
        if self._on_skills_changed is not None:
            self._on_skills_changed(enabled_cnt)

    def _on_delete_skill(self, spec: SkillSpec) -> None:
        if delete_custom_skill(spec):
            self._on_toast(f"Deleted skill '{spec.name}'")
            self.refresh()

    def _on_save_custom_clicked(self) -> None:
        name = self._f_name.get_text().strip()
        buf = self._f_instructions.get_buffer()
        s, e = buf.get_bounds()
        instr = buf.get_text(s, e, False).strip()
        if not name or not instr:
            self._on_toast("Enter both a Skill Name and Prompt Rules")
            return
        cat_idx = self._f_cat.get_selected()
        cat = SKILL_CATEGORIES[cat_idx] if 0 <= cat_idx < len(SKILL_CATEGORIES) else "Image"
        mdl_list = ["Pony V6 XL", "Wan 2.1", "Both"]
        mdl_idx = self._f_model.get_selected()
        tgt_model = mdl_list[mdl_idx] if 0 <= mdl_idx < len(mdl_list) else "Pony V6 XL"
        triggers = [t.strip() for t in self._f_triggers.get_text().split(",") if t.strip()]
        overrides: dict[str, object] = {
            "target_model": tgt_model,
            "booster": self._f_booster.get_active(),
        }
        cfg_txt = self._f_cfg.get_text().strip()
        if cfg_txt:
            try:
                overrides["cfg"] = float(cfg_txt)
            except ValueError:
                pass

        spec = SkillSpec(
            id="",
            name=name,
            version=self._f_version.get_text().strip() or "1.0.0",
            author=self._f_author.get_text().strip() or "User",
            category=cat,
            trigger_keywords=triggers,
            system_instructions=instr,
            negative_prompt_patch=self._f_neg.get_text().strip(),
            comfyui_node_overrides=overrides,
            enabled=True,
            builtin=False,
        )
        path = save_custom_skill(spec)
        self._f_name.set_text("")
        self._f_triggers.set_text("")
        buf.set_text("")
        self._f_neg.set_text("")
        self._f_cfg.set_text("")
        self._on_toast(f"Saved skill to {path.name}")
        self._toggle_create_view(False)
        self.refresh()

    def _on_file_drop(self, _drop: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        if isinstance(value, Gio.File):
            p = value.get_path()
            if p:
                return self._do_import_path(Path(p))
        return False

    def _do_import_path(self, path: Path) -> bool:
        try:
            spec = import_skill_file(path)
            self._on_toast(f"Imported skill: {spec.name}")
            self.refresh()
            return True
        except Exception as exc:
            self._on_toast(f"Import failed: {exc}")
            return False

    def _prompt_import(self) -> None:
        dlg = Gtk.FileDialog()
        dlg.set_title("Import .vimona-skill File")

        def _done(d: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
            try:
                gf = d.open_finish(res)
            except GLib.Error:
                return
            p = gf.get_path() if gf else None
            if p:
                self._do_import_path(Path(p))

        dlg.open(self._window, None, _done)

    def _prompt_export(self, spec: SkillSpec) -> None:
        dlg = Gtk.FileDialog()
        dlg.set_title(f"Export {spec.name}")
        dlg.set_initial_name(f"{spec.id}.vimona-skill")

        def _done(d: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
            try:
                gf = d.save_finish(res)
            except GLib.Error:
                return
            p = gf.get_path() if gf else None
            if p:
                try:
                    saved = export_skill_file(spec, Path(p))
                    self._on_toast(f"Exported {saved.name}")
                except Exception as exc:
                    self._on_toast(f"Export failed: {exc}")

        dlg.save(self._window, None, _done)
