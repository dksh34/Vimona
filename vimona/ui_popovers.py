"""Hugging Face model picker & header mode popovers extracted cleanly from `window.py` (`L1`)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from .hf_models import (
    CURATED_HF_MODELS,
    HFModelSpec,
    download_hf_model,
    evaluate_model_fit,
    fetch_hf_repo_meta,
    find_local_model_path,
    get_models_for_category,
    get_selected_model_id,
    get_video_duration_options,
    search_hf_hub_tuned,
    set_selected_model_id,
)

if TYPE_CHECKING:
    from .window import VimonaWindow


def build_header_mode_popover(win: VimonaWindow) -> Gtk.Popover:
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
        ("Images", "SDXL keyframe studio", "Make Images"),
        ("Video", "Motion & keyframe synthesis", "Make Video"),
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
        b.connect("clicked", lambda _btn, mv=mode_val, p=pop: (p.popdown(), win.select_new_chat_mode(mv)))
        box.append(b)
    pop.set_child(box)
    return pop


def rebuild_hf_models_popover(win: VimonaWindow, category: str) -> None:
    win._hf_model_tab = category
    win._hf_search_query = getattr(win, "_hf_search_query", "")
    win._hf_search_timer = getattr(win, "_hf_search_timer", 0)
    win._hf_live_results = getattr(win, "_hf_live_results", {})

    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    root.set_size_request(445, 535)

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
    hw_lbl = Gtk.Label(label=f"Hardware Tuned · {win._hw_profile.summary_label}")
    hw_lbl.set_xalign(0.0)
    hw_lbl.set_ellipsize(Pango.EllipsizeMode.END)
    hw_lbl.set_hexpand(True)
    hw_banner.append(hw_lbl)
    root.append(hw_banner)

    tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    for cat_key, cat_title in (("text", "Text Models"), ("image", "Image Models"), ("video", "Video Models")):
        tb = Gtk.Button(label=cat_title)
        tb.set_has_frame(False)
        tb.add_css_class("quick-select-chip")
        if cat_key == category:
            tb.add_css_class("selected")
        tb.set_cursor_from_name("pointer")
        tb.connect("clicked", lambda _b, c=cat_key: (setattr(win, "_hf_search_query", ""), rebuild_hf_models_popover(win, c)))
        tabs.append(tb)
    root.append(tabs)

    search_entry = Gtk.SearchEntry()
    search_entry.add_css_class("vimona-search-entry")
    search_entry.set_placeholder_text("Search all of Hugging Face (hardware-tuned)…")
    search_entry.set_text(win._hf_search_query)
    search_entry.connect("search-changed", lambda e, c=category: on_hf_search_changed(win, c, e.get_text()))
    win._hf_search_entry = search_entry
    root.append(search_entry)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_vexpand(True)
    scroll.set_hexpand(True)

    cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    cards.set_margin_end(6)
    win._hf_cards_box = cards
    populate_hf_cards(win, category, search_pending=False)

    scroll.set_child(cards)
    root.append(scroll)
    win._hf_models_popover.set_child(root)


def on_hf_search_changed(win: VimonaWindow, category: str, text: str) -> None:
    win._hf_search_query = text.strip()
    if getattr(win, "_hf_search_timer", 0):
        try:
            GLib.source_remove(win._hf_search_timer)
        except Exception:
            pass
        win._hf_search_timer = 0
    q = win._hf_search_query
    need_hub = len(q) >= 2 and (category, q.lower()) not in win._hf_live_results
    populate_hf_cards(win, category, search_pending=need_hub)
    if need_hub:

        def _kick() -> bool:
            win._hf_search_timer = 0
            threading.Thread(
                target=run_hf_hub_search,
                args=(win, category, q),
                daemon=True,
            ).start()
            return False

        win._hf_search_timer = GLib.timeout_add(300, _kick)


def run_hf_hub_search(win: VimonaWindow, category: str, query: str) -> None:
    found = search_hf_hub_tuned(query, category, hw=win._hw_profile, limit=8)
    win._hf_live_results[(category, query.lower())] = found

    def _apply() -> bool:
        if (
            getattr(win, "_hf_model_tab", "") == category
            and getattr(win, "_hf_search_query", "").lower() == query.lower()
            and hasattr(win, "_hf_cards_box")
            and win._hf_cards_box.get_parent() is not None
        ):
            populate_hf_cards(win, category, search_pending=False)
        return False

    GLib.idle_add(_apply)


def populate_hf_cards(win: VimonaWindow, category: str, search_pending: bool = False) -> None:
    cards = win._hf_cards_box
    ch = cards.get_first_child()
    while ch is not None:
        nxt = ch.get_next_sibling()
        cards.remove(ch)
        ch = nxt

    selected_id = get_selected_model_id(category, hw=win._hw_profile)
    curated_tuned = get_models_for_category(category, hw=win._hw_profile)
    total_in_cat = sum(1 for m in CURATED_HF_MODELS if m.category == category.lower())
    q = getattr(win, "_hf_search_query", "").strip().lower()

    if q:
        local_hits = [
            m
            for m in curated_tuned
            if q in m.name.lower() or q in m.hf_repo.lower() or q in m.summary.lower() or any(q in t for t in m.tags)
        ]
        live_hits = [
            m for m in win._hf_live_results.get((category, q), []) if m.id not in {x.id for x in local_hits}
        ]
        models = local_hits + live_hits
        note_txt = (
            f"Searching Hugging Face Hub for '{q}' (hardware-tuned)…"
            if search_pending
            else f"Showing {len(models)} hardware-tuned '{q}' match{'es' if len(models) != 1 else ''} across curated + Hugging Face Hub"
        )
    else:
        models = curated_tuned
        hidden_cnt = max(0, total_in_cat - len(models))
        note_txt = (
            f"Showing {len(models)} hardware-tuned {category} models ({hidden_cnt} oversized/unsuited hidden) · search above for any HF Hub weight"
            if hidden_cnt > 0
            else f"Showing {len(models)} hardware-tuned {category} models"
        )

    flt_lbl = Gtk.Label(label=note_txt)
    flt_lbl.add_css_class("hf-filter-note")
    flt_lbl.set_wrap(True)
    flt_lbl.set_xalign(0.0)
    cards.append(flt_lbl)

    if not models:
        empty_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        empty_card.add_css_class("hf-model-card")
        et = Gtk.Label(
            label=(
                "Querying Hugging Face Hub…"
                if search_pending
                else f"No {category} models match this hardware cleanly"
            )
        )
        et.add_css_class("hf-model-title")
        et.set_xalign(0.0)
        ed = Gtk.Label(
            label=f"Only weights fitting {win._hw_profile.summary_label} are shown to avoid swap thrashing or OOM crashes."
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

        fit_tier, fit_txt = evaluate_model_fit(spec, win._hw_profile)
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
        dl_state = win._hf_dl_progress.get(spec.id)

        hf_stat = Gtk.Label(label="Installed ✓" if local_p else "Hugging Face Hub")
        hf_stat.add_css_class("hf-stat-lbl")
        hf_stat.set_hexpand(True)
        hf_stat.set_xalign(0.0)
        hf_stat.set_valign(Gtk.Align.CENTER)
        act_row.append(hf_stat)

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
            if spec.id == selected_id:
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
                btn.connect("clicked", lambda _b, s=spec: select_hf_model(win, s))
                act_row.append(btn)
        else:
            btn = Gtk.Button(label=f"Get · {spec.size_gb:.1f} GB")
            btn.set_has_frame(False)
            btn.add_css_class("hf-get-btn")
            btn.set_cursor_from_name("pointer")
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect("clicked", lambda _b, s=spec: download_and_select_hf_model(win, s))
            act_row.append(btn)

        card.append(act_row)
        cards.append(card)


def select_hf_model(win: VimonaWindow, spec: HFModelSpec) -> None:
    set_selected_model_id(spec.category, spec.id, spec_obj=spec)
    cur_skill = win._composer._active_skill if hasattr(win, "_composer") else "Chat & Ideas"
    win._sync_hf_models_button_label(cur_skill)
    win._sync_video_duration_options()
    rebuild_hf_models_popover(win, spec.category)
    if spec.category == "video":
        durs, _ = get_video_duration_options(win._hw_profile, spec)
        win.show_toast(f"Selected {spec.name} ({spec.quant}) · Video up to {durs[-1]}s")
    else:
        win.show_toast(f"Selected {spec.name} ({spec.quant})")


def download_and_select_hf_model(win: VimonaWindow, spec: HFModelSpec) -> None:
    if spec.id in win._hf_dl_progress:
        return
    win._hf_dl_progress[spec.id] = (0.0, "Connecting to Hugging Face…")
    rebuild_hf_models_popover(win, spec.category)

    def _worker() -> None:
        try:

            def _prog(frac: float, msg: str) -> None:
                win._hf_dl_progress[spec.id] = (frac, msg)
                GLib.idle_add(lambda: (rebuild_hf_models_popover(win, spec.category), False)[1])

            download_hf_model(spec, on_progress=_prog, cancel_event=win._hf_dl_cancel)
            win._hf_dl_progress.pop(spec.id, None)
            GLib.idle_add(lambda: (select_hf_model(win, spec), False)[1])
        except Exception as exc:
            win._hf_dl_progress.pop(spec.id, None)
            err_txt = str(exc)
            GLib.idle_add(
                lambda: (
                    rebuild_hf_models_popover(win, spec.category),
                    win.show_toast(f"HF download failed: {err_txt[:80]}"),
                    False,
                )[2]
            )

    threading.Thread(target=_worker, daemon=True, name=f"hf-dl-{spec.id}").start()
