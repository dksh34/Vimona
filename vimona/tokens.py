"""Vimona Design System — Nordic Obsidian & Mineral Slate dark theme tokens.

Crafted for calm readability on Fedora Linux:
- Deep matte obsidian-slate surfaces (#121417 / #16181C / #1D2026 / #21252C)
  preventing optical halation while feeling distinct, cool, and crisp.
- Soft snow-white primary ink (#ECEFF4) and muted frost secondary text (#A3ACB9).
- Muted seafoam-mineral accent (#6FB3A8) and quiet glacier-slate (#7AA2D4).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColorTokens:
    # Pure neutral matte graphite surfaces (zero blue/teal/neon tint)
    bg_sidebar: str = "#141414"
    bg_canvas: str = "#191919"
    bg_surface: str = "#202020"
    bg_elevated: str = "#262626"
    bg_composer: str = "#242424"
    bg_user_pill: str = "#2B2B2B"
    bg_hover: str = "#2E2E2E"
    bg_active: str = "#363636"
    bg_code_block: str = "#1D1D1D"
    bg_lightbox_scrim: str = "rgba(12, 12, 12, 0.975)"
    bg_popover: str = "#232323"

    # Subtle hairline borders
    border_subtle: str = "rgba(255, 255, 255, 0.065)"
    border_default: str = "rgba(255, 255, 255, 0.095)"
    border_focus: str = "rgba(255, 255, 255, 0.22)"
    border_strong: str = "rgba(255, 255, 255, 0.14)"

    # Neutral alabaster ink hierarchy
    text_primary: str = "#EDEDED"
    text_secondary: str = "#A3A3A3"
    text_muted: str = "#737373"
    text_faint: str = "#525252"

    # Restrained monochrome & warm-stone accents (human-crafted, zero AI teal/neon)
    accent_primary: str = "#E2E2E2"
    accent_primary_soft: str = "rgba(255, 255, 255, 0.08)"
    accent_primary_border: str = "rgba(255, 255, 255, 0.16)"

    accent_sage: str = "#8CB89A"
    accent_sage_soft: str = "rgba(140, 184, 154, 0.12)"

    accent_slate: str = "#A8B5C8"
    accent_slate_soft: str = "rgba(168, 181, 200, 0.10)"

    accent_amber: str = "#C9A875"
    accent_amber_soft: str = "rgba(201, 168, 117, 0.12)"
    accent_amber_border: str = "rgba(201, 168, 117, 0.28)"

    # Tactile primary action button
    btn_primary_bg: str = "#EDEDED"
    btn_primary_fg: str = "#141414"
    btn_primary_hover: str = "#FFFFFF"
    btn_disabled_bg: str = "rgba(255, 255, 255, 0.10)"
    btn_disabled_fg: str = "rgba(255, 255, 255, 0.30)"


@dataclass(frozen=True)
class GeometryTokens:
    reading_column_max_px: int = 740
    composer_max_px: int = 740
    sidebar_width_px: int = 260
    header_height_px: int = 48
    split_collapse_width_px: int = 860
    radius_composer: int = 22
    radius_user_pill: int = 18
    radius_media: int = 12


@dataclass(frozen=True)
class TypographyTokens:
    ui_sans: str = '"Inter", "Adwaita Sans", "Cantarell", sans-serif'
    editorial_serif: str = '"Inter Display", "Inter", "Adwaita Sans", sans-serif'
    mono: str = '"JetBrains Mono", "Adwaita Mono", monospace'


COLORS = ColorTokens()
GEOMETRY = GeometryTokens()
TYPOGRAPHY = TypographyTokens()


def build_stylesheet() -> str:
    """Generate the complete GTK4 CSS stylesheet for Vimona."""
    c = COLORS
    g = GEOMETRY
    t = TYPOGRAPHY

    return f"""
/* ==========================================================================
   Vimona Design System — Nordic Obsidian GTK4 / Libadwaita Stylesheet
   ========================================================================== */

window,
.vimona-window,
.vimona-canvas,
.gallery-view,
scrolledwindow,
viewport {{
    background-color: {c.bg_canvas};
    color: {c.text_primary};
    font-family: {t.ui_sans};
    font-size: 14px;
}}

/* Clean HeaderBar */
headerbar,
.vimona-header {{
    background-color: {c.bg_canvas};
    background-image: none;
    border-bottom: 1px solid transparent;
    box-shadow: none;
    min-height: {g.header_height_px}px;
    padding: 4px 10px;
}}

.vimona-header.scrolled {{
    border-bottom: 1px solid {c.border_subtle};
}}

.vimona-header windowcontrols button {{
    min-width: 24px;
    min-height: 24px;
    padding: 2px;
    margin: 0 2px;
    border-radius: 999px;
    background-color: transparent;
    color: {c.text_secondary};
    transition: background-color 150ms cubic-bezier(0.22, 1, 0.36, 1),
                color 150ms cubic-bezier(0.22, 1, 0.36, 1);
}}

.vimona-header windowcontrols button:hover {{
    background-color: {c.bg_hover};
    color: {c.text_primary};
}}

/* Sidebar surface */
.vimona-sidebar {{
    background-color: {c.bg_sidebar};
    border-right: 1px solid {c.border_subtle};
}}

.sidebar-header {{
    padding: 8px 12px 6px 14px;
    min-height: {g.header_height_px}px;
}}

.brand-wordmark {{
    font-family: {t.editorial_serif};
    font-size: 15.5px;
    font-weight: 600;
    letter-spacing: -0.01em;
    color: {c.text_primary};
}}

.sidebar-section-label {{
    font-size: 11.5px;
    font-weight: 500;
    color: {c.text_muted};
    padding: 12px 14px 4px 14px;
}}

button.sidebar-new-btn {{
    background-color: {c.bg_surface};
    background-image: none;
    border: 1px solid {c.border_default};
    border-radius: 10px;
    padding: 7px 12px;
    margin: 4px 10px 8px 10px;
    color: {c.text_primary};
    font-size: 13px;
    font-weight: 500;
    box-shadow: none;
    transition: background-color 160ms cubic-bezier(0.22, 1, 0.36, 1),
                border-color 160ms cubic-bezier(0.22, 1, 0.36, 1);
}}

button.sidebar-new-btn:hover {{
    background-color: {c.bg_hover};
    border-color: {c.border_strong};
}}

button.sidebar-gallery-btn {{
    background-color: transparent;
    background-image: none;
    color: {c.text_secondary};
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 7px 11px;
    margin: 2px 10px 6px 10px;
    font-size: 13px;
    font-weight: 500;
    box-shadow: none;
    transition: background-color 150ms ease, border-color 150ms ease, color 150ms ease;
}}

button.sidebar-gallery-btn:hover {{
    background-color: rgba(236, 239, 244, 0.05);
    color: {c.text_primary};
}}

button.sidebar-gallery-btn.active {{
    background-color: {c.bg_elevated};
    border-color: {c.border_default};
    color: {c.text_primary};
}}

.gallery-count-pill {{
    font-size: 11px;
    font-weight: 500;
    color: {c.text_secondary};
    background-color: rgba(255, 255, 255, 0.06);
    border-radius: 999px;
    padding: 1px 7px;
}}

.gallery-container {{
    padding: 22px 26px 40px 26px;
    background-color: {c.bg_canvas};
    color: {c.text_primary};
}}

.gallery-title {{
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    color: {c.text_primary};
}}

.gallery-notice {{
    background-color: rgba(236, 239, 244, 0.035);
    border: 1px solid {c.border_subtle};
    border-radius: 11px;
    padding: 9px 13px;
    font-size: 12.5px;
    color: {c.text_secondary};
}}

.gallery-card {{
    background-color: {c.bg_surface};
    border: 1px solid {c.border_default};
    border-radius: 13px;
    padding: 8px;
}}

.gallery-thumb-box {{
    border-radius: 9px;
    background-color: #101215;
}}

.gallery-card-meta {{
    font-size: 12px;
    font-weight: 500;
    color: {c.text_primary};
}}

.gallery-card-sub {{
    font-size: 11px;
    color: {c.text_muted};
}}

.gallery-saved-pill {{
    font-size: 10.5px;
    font-weight: 600;
    color: {c.accent_sage};
    background-color: {c.accent_sage_soft};
    border-radius: 999px;
    padding: 1px 7px;
}}

button.gallery-action-btn {{
    background-color: {c.bg_elevated};
    background-image: none;
    border: 1px solid {c.border_default};
    border-radius: 8px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
    color: {c.text_primary};
    box-shadow: none;
}}

button.gallery-action-btn:hover {{
    background-color: {c.bg_hover};
    border-color: {c.border_strong};
}}

button.gallery-delete-btn {{
    background-color: rgba(239, 68, 68, 0.12);
    background-image: none;
    border: 1px solid rgba(239, 68, 68, 0.28);
    border-radius: 8px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
    color: #FCA5A5;
    box-shadow: none;
}}

button.gallery-delete-btn:hover {{
    background-color: rgba(239, 68, 68, 0.22);
    border-color: rgba(239, 68, 68, 0.48);
}}

.sidebar-shortcut-hint {{
    font-family: {t.mono};
    font-size: 10.5px;
    color: {c.text_muted};
    background-color: rgba(236, 239, 244, 0.05);
    border-radius: 5px;
    padding: 1px 5px;
}}

.session-row-shell {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 2px 4px;
    margin: 1px 8px;
    transition: background-color 140ms cubic-bezier(0.22, 1, 0.36, 1),
                border-color 140ms cubic-bezier(0.22, 1, 0.36, 1);
}}

.session-row-shell:hover {{
    background-color: rgba(236, 239, 244, 0.045);
}}

.session-row-shell.active {{
    background-color: {c.bg_elevated};
    border-color: {c.border_subtle};
}}

button.session-select-btn {{
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: 7px;
    padding: 5px 6px;
    min-height: 26px;
    color: {c.text_secondary};
    box-shadow: none;
}}

.session-row-shell:hover button.session-select-btn,
.session-row-shell.active button.session-select-btn {{
    color: {c.text_primary};
}}

.session-row-title {{
    font-size: 13px;
    font-weight: 450;
}}

.session-row-shell.active .session-row-title {{
    font-weight: 500;
}}

menubutton.session-more-btn {{
    background: transparent;
    border: none;
    padding: 0;
    margin: 0;
    opacity: 0.0;
    transition: opacity 120ms ease;
}}

.session-row-shell:hover menubutton.session-more-btn,
.session-row-shell.active menubutton.session-more-btn,
menubutton.session-more-btn:focus-within {{
    opacity: 1.0;
}}

menubutton.session-more-btn > button {{
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: 6px;
    padding: 2px 5px;
    min-width: 24px;
    min-height: 24px;
    box-shadow: none;
}}

menubutton.session-more-btn > button:hover {{
    background-color: rgba(236, 239, 244, 0.09);
}}

button.popover-item.danger {{
    color: #FCA5A5;
}}

button.popover-item.danger:hover {{
    background-color: rgba(239, 68, 68, 0.14);
    color: #FECACA;
}}

entry.rename-entry {{
    background-color: {c.bg_canvas};
    color: {c.text_primary};
    border: 1px solid {c.border_default};
    border-radius: 8px;
    padding: 5px 8px;
    font-size: 13px;
}}

entry.rename-entry:focus {{
    border-color: {c.border_focus};
}}

button.clear-chats-btn {{
    background-color: transparent;
    background-image: none;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 4px 8px;
    min-height: 24px;
    font-size: 11.5px;
    color: {c.text_muted};
    box-shadow: none;
}}

button.clear-chats-btn:hover {{
    background-color: rgba(239, 68, 68, 0.12);
    border-color: rgba(239, 68, 68, 0.25);
    color: #FCA5A5;
}}

.session-row-meta {{
    font-size: 11px;
    color: {c.text_muted};
}}

.sidebar-footer {{
    border-top: 1px solid {c.border_subtle};
    padding: 10px 14px;
}}

.sidebar-footer-label {{
    font-size: 11.5px;
    color: {c.text_muted};
}}

.uncensored-pill {{
    font-size: 11px;
    font-weight: 500;
    color: {c.text_secondary};
    background-color: transparent;
    padding: 0;
}}

/* Quiet icon buttons */
button.icon-btn {{
    background-color: transparent;
    background-image: none;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 5px;
    min-width: 28px;
    min-height: 28px;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 140ms cubic-bezier(0.22, 1, 0.36, 1),
                color 140ms cubic-bezier(0.22, 1, 0.36, 1);
}}

button.icon-btn:hover {{
    background-color: {c.bg_hover};
    color: {c.text_primary};
}}

button.icon-btn:active {{
    background-color: {c.bg_active};
}}

.header-session-title {{
    font-size: 13.5px;
    font-weight: 500;
    color: {c.text_primary};
}}

/* Top Bar Status Indicator (unboxed & quiet when idle, subtle only when busy) */
.state-pill {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 999px;
    padding: 3px 8px;
    font-size: 12px;
    font-weight: 450;
    color: {c.text_muted};
    transition: background-color 180ms cubic-bezier(0.22, 1, 0.36, 1),
                border-color 180ms cubic-bezier(0.22, 1, 0.36, 1);
}}

.state-pill.active-chat {{
    background-color: rgba(255, 255, 255, 0.05);
    border-color: {c.border_subtle};
    color: {c.text_secondary};
}}

.state-pill.research {{
    background-color: {c.accent_slate_soft};
    border-color: rgba(168, 181, 200, 0.22);
    color: #CBD4E1;
}}

.state-pill.handoff {{
    background-color: {c.accent_amber_soft};
    border-color: {c.accent_amber_border};
    color: #DFC396;
}}

.state-pill.completed {{
    background-color: transparent;
    border-color: transparent;
    color: {c.text_muted};
}}

menubutton.ram-menu-btn > button {{
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: 999px;
    padding: 0;
    margin: 0;
    min-height: 0;
    min-width: 0;
    box-shadow: none;
}}

menubutton.ram-menu-btn > button:hover,
menubutton.ram-menu-btn > button:checked {{
    background-color: rgba(255, 255, 255, 0.05);
    box-shadow: none;
}}

/* Minimal scrollbars */
scrollbar slider {{
    background-color: rgba(236, 239, 244, 0.14);
    border-radius: 999px;
    min-width: 5px;
    min-height: 28px;
    margin: 2px;
}}

scrollbar slider:hover {{
    background-color: rgba(236, 239, 244, 0.26);
}}

scrollbar trough {{
    background-color: transparent;
}}

/* Empty State */
.empty-greeting {{
    font-family: {t.editorial_serif};
    font-size: 29px;
    font-weight: 600;
    color: {c.text_primary};
    letter-spacing: -0.02em;
}}

.empty-subtitle {{
    font-size: 14px;
    color: {c.text_secondary};
}}

button.starter-card {{
    background-color: {c.bg_surface};
    background-image: none;
    border: 1px solid {c.border_subtle};
    border-radius: 13px;
    padding: 13px 15px;
    box-shadow: none;
    transition: background-color 160ms cubic-bezier(0.22, 1, 0.36, 1),
                border-color 160ms cubic-bezier(0.22, 1, 0.36, 1);
}}

button.starter-card:hover {{
    background-color: {c.bg_elevated};
    border-color: {c.border_strong};
}}

.starter-card-title {{
    font-size: 13.5px;
    font-weight: 500;
    color: {c.text_primary};
}}

.starter-card-desc {{
    font-size: 12.5px;
    color: {c.text_muted};
}}

/* Conversational Stream */
.stream-container {{
    padding: 18px 22px 250px 22px;
}}

/* User Message Pill */
.user-pill {{
    background-color: {c.bg_user_pill};
    border: 1px solid {c.border_default};
    border-radius: {g.radius_user_pill}px;
    padding: 11px 16px;
}}

.user-pill-text {{
    font-size: 14.5px;
    color: {c.text_primary};
}}

.message-action-bar {{
    opacity: 0.0;
    transition: opacity 150ms cubic-bezier(0.22, 1, 0.36, 1);
}}

.user-message-wrapper:hover .message-action-bar,
.message-action-bar.visible {{
    opacity: 1.0;
}}

button.mini-action-btn {{
    background-color: {c.bg_surface};
    background-image: none;
    border: 1px solid {c.border_subtle};
    border-radius: 7px;
    padding: 3px 8px;
    min-height: 24px;
    font-size: 11.5px;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 140ms ease, color 140ms ease;
}}

button.mini-action-btn:hover {{
    background-color: {c.bg_hover};
    color: {c.text_primary};
}}

/* Assistant Unboxed Prose */
.assistant-role-tag {{
    font-size: 12.5px;
    font-weight: 600;
    color: {c.text_secondary};
}}

.prose-h2 {{
    font-family: {t.ui_sans};
    font-size: 17px;
    font-weight: 600;
    color: {c.text_primary};
    margin-top: 6px;
}}

.prose-h3 {{
    font-family: {t.ui_sans};
    font-size: 14.5px;
    font-weight: 600;
    color: {c.text_primary};
    margin-top: 4px;
}}

.prose-body {{
    font-size: 14.5px;
    color: {c.text_primary};
}}

.prose-bullet-dot {{
    font-size: 14px;
    color: {c.text_muted};
}}

textview.prose-textview {{
    background-color: transparent;
    color: {c.text_primary};
    font-family: {t.ui_sans};
    font-size: 14.5px;
    caret-color: {c.text_primary};
}}

textview.prose-textview text {{
    background-color: transparent;
    color: {c.text_primary};
}}

textview.prose-textview text selection {{
    background-color: rgba(59, 130, 246, 0.36);
    color: #FFFFFF;
}}

button.inline-cite-chip {{
    background-color: rgba(255, 255, 255, 0.085);
    background-image: none;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 999px;
    padding: 0 7px;
    min-height: 18px;
    font-size: 11px;
    font-weight: 500;
    color: {c.text_secondary};
    margin-left: 3px;
    margin-right: 2px;
    box-shadow: none;
    transition: background-color 120ms ease, color 120ms ease, border-color 120ms ease;
}}

button.inline-cite-chip:hover {{
    background-color: rgba(96, 165, 250, 0.20);
    border-color: rgba(96, 165, 250, 0.45);
    color: #F8FAFC;
}}

popover.selection-float-popover contents {{
    background-color: #1A1C22;
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 999px;
    padding: 3px 6px;
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.48);
}}

button.sel-action-pill {{
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: 999px;
    padding: 4px 10px;
    min-height: 26px;
    font-size: 12px;
    font-weight: 500;
    color: {c.text_primary};
    box-shadow: none;
    transition: background-color 120ms ease;
}}

button.sel-action-pill:hover {{
    background-color: rgba(255, 255, 255, 0.10);
}}

.msg-footer-bar {{
    margin-top: 2px;
}}

menubutton.sources-pill-btn > button {{
    background-color: rgba(255, 255, 255, 0.055);
    background-image: none;
    border: 1px solid {c.border_subtle};
    border-radius: 999px;
    padding: 3px 10px;
    min-height: 24px;
    font-size: 11.5px;
    font-weight: 500;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 120ms ease, color 120ms ease, border-color 120ms ease;
}}

menubutton.sources-pill-btn > button:hover {{
    background-color: {c.bg_hover};
    border-color: {c.border_strong};
    color: {c.text_primary};
}}

.composer-quote-bar {{
    background-color: rgba(255, 255, 255, 0.05);
    border-left: 2.5px solid #60A5FA;
    border-radius: 8px;
    padding: 5px 8px;
    margin-bottom: 4px;
}}

.composer-quote-text {{
    font-size: 12px;
    color: {c.text_secondary};
}}

.chat-select-card {{
    background-color: #16181D;
    border: 1px solid {c.border_strong};
    border-radius: 16px;
    padding: 16px;
    box-shadow: 0 20px 48px rgba(0, 0, 0, 0.65);
}}

button.quick-select-chip {{
    background-color: rgba(255, 255, 255, 0.045);
    background-image: none;
    border: 1px solid {c.border_subtle};
    border-radius: 999px;
    padding: 3px 10px;
    min-height: 24px;
    font-size: 11.5px;
    font-weight: 500;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease;
}}

button.quick-select-chip:hover {{
    background-color: {c.bg_hover};
    border-color: {c.border_strong};
    color: {c.text_primary};
}}

button.quick-select-chip.selected {{
    background-color: rgba(59, 130, 246, 0.18);
    border-color: rgba(96, 165, 250, 0.55);
    color: #93C5FD;
}}

menubutton.camera-chip > button {{
    background-color: rgba(255, 255, 255, 0.06);
    background-image: none;
    border: 1px solid {c.border_subtle};
    border-radius: 999px;
    padding: 3px 10px;
    min-height: 28px;
    font-size: 12px;
    font-weight: 500;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 120ms ease, color 120ms ease, border-color 120ms ease;
}}

menubutton.camera-chip > button:hover {{
    background-color: {c.bg_hover};
    border-color: {c.border_strong};
    color: {c.text_primary};
}}

.thinking-trace-box {{
    background-color: rgba(15, 17, 22, 0.72);
    border: 1px solid {c.border_subtle};
    border-radius: 10px;
    padding: 8px 10px;
    margin-top: 6px;
}}

textview.thinking-trace-tv {{
    background-color: transparent;
    color: {c.text_secondary};
    font-family: {t.mono};
    font-size: 12px;
}}

textview.thinking-trace-tv text {{
    background-color: transparent;
    color: {c.text_secondary};
}}

.composer-queue-bar {{
    background-color: rgba(255, 255, 255, 0.045);
    border: 1px solid {c.border_subtle};
    border-radius: 10px;
    padding: 6px 9px;
    margin-bottom: 4px;
}}

.composer-queue-item {{
    background-color: rgba(255, 255, 255, 0.04);
    border-radius: 7px;
    padding: 3px 8px;
}}

.new-chat-mode-dialog {{
    background-color: #16181E;
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 20px;
    padding: 24px;
    box-shadow: 0 28px 64px rgba(0, 0, 0, 0.78);
}}

.new-chat-dialog-title {{
    font-size: 17px;
    font-weight: 600;
    letter-spacing: -0.015em;
    color: {c.text_primary};
}}

button.new-chat-mode-card {{
    background-color: rgba(255, 255, 255, 0.04);
    background-image: none;
    border: 1px solid {c.border_default};
    border-radius: 14px;
    padding: 16px;
    box-shadow: none;
    transition: background-color 140ms ease, border-color 140ms ease;
}}

button.new-chat-mode-card:hover {{
    background-color: rgba(96, 165, 250, 0.12);
    border-color: #60A5FA;
}}

.new-chat-icon-pill {{
    background-color: rgba(96, 165, 250, 0.14);
    border: 1px solid rgba(96, 165, 250, 0.28);
    border-radius: 11px;
    min-width: 40px;
    min-height: 40px;
}}

.new-chat-lock-tag {{
    font-size: 10px;
    font-weight: 600;
    color: #93C5FD;
    background-color: rgba(59, 130, 246, 0.14);
    border-radius: 999px;
    padding: 1px 7px;
}}

.chat-mode-lock-pill {{
    font-size: 11px;
    font-weight: 600;
    color: #93C5FD;
    background-color: rgba(59, 130, 246, 0.14);
    border: 1px solid rgba(96, 165, 250, 0.30);
    border-radius: 999px;
    padding: 2px 9px;
}}

/* Collapsible Reasoning Accordion */
.reasoning-accordion {{
    background-color: rgba(236, 239, 244, 0.025);
    border: 1px solid {c.border_subtle};
    border-radius: 11px;
    padding: 2px 4px;
}}

button.reasoning-toggle {{
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: 9px;
    padding: 6px 10px;
    min-height: 30px;
    color: {c.text_secondary};
    font-size: 12.5px;
    font-weight: 500;
    box-shadow: none;
    transition: background-color 140ms ease, color 140ms ease;
}}

button.reasoning-toggle:hover {{
    background-color: rgba(236, 239, 244, 0.04);
    color: {c.text_primary};
}}

.reasoning-skill-tag {{
    font-size: 11px;
    font-weight: 500;
    color: {c.text_secondary};
    background-color: rgba(255, 255, 255, 0.06);
    border-radius: 6px;
    padding: 1px 7px;
}}

.reasoning-body {{
    padding: 8px 12px 11px 12px;
    border-top: 1px solid {c.border_subtle};
    font-size: 13px;
    color: {c.text_secondary};
}}

/* Retro 80s TV Vimona Mascot Pill & Live Thinking Banner */
.tv-mascot-pill {{
    background-image: linear-gradient(
        to bottom,
        rgba(56, 58, 66, 0.86) 0%,
        rgba(28, 30, 35, 0.82) 100%
    );
    border: 1px solid rgba(255, 255, 255, 0.22);
    border-radius: 12px;
    padding: 4px 12px 5px 12px;
    min-width: 68px;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.44);
    transition: border-color 180ms ease;
}}

.tv-mascot-pill.thinking {{
    border-color: rgba(163, 230, 53, 0.38);
}}

.tv-mascot-pill.working {{
    border-color: rgba(125, 211, 252, 0.42);
}}

.tv-mascot-pill.limit {{
    border-color: rgba(248, 113, 113, 0.48);
}}

.tv-mascot-name {{
    font-size: 12.5px;
    font-weight: 600;
    letter-spacing: -0.01em;
    color: #FAFAFA;
}}

.tv-mascot-status {{
    font-size: 10.5px;
    font-weight: 500;
    color: #A1A1AA;
    padding-top: 1px;
}}

.tv-mascot-pill.thinking .tv-mascot-status {{
    color: #BEF264;
}}

.tv-mascot-pill.working .tv-mascot-status {{
    color: #7DD3FC;
}}

.tv-mascot-pill.limit .tv-mascot-status {{
    color: #FCA5A5;
}}

.thinking-live-card {{
    background-color: rgba(236, 239, 244, 0.03);
    border: 1px solid {c.border_subtle};
    border-radius: 14px;
    padding: 10px 14px;
}}

.thinking-live-title {{
    font-size: 13.5px;
    font-weight: 600;
    color: {c.text_primary};
}}

.thinking-live-sub {{
    font-size: 12.5px;
    color: {c.text_muted};
}}

/* Internet Research Permission Card */
.research-perm-card {{
    background-color: {c.bg_surface};
    border: 1px solid rgba(168, 181, 200, 0.30);
    border-radius: 15px;
    padding: 14px 16px;
}}

.research-perm-title {{
    font-size: 14px;
    font-weight: 600;
    color: {c.text_primary};
}}

.research-perm-target {{
    font-family: {t.mono};
    font-size: 12px;
    color: {c.text_primary};
    background-color: rgba(255, 255, 255, 0.045);
    border: 1px solid {c.border_subtle};
    border-radius: 8px;
    padding: 6px 10px;
}}

.research-perm-note {{
    font-size: 12px;
    color: {c.text_muted};
}}

button.perm-decline-btn {{
    background-color: transparent;
    background-image: none;
    border: 1px solid {c.border_default};
    border-radius: 999px;
    padding: 0 16px;
    min-height: 46px;
    font-size: 13px;
    font-weight: 500;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 140ms ease, color 140ms ease, border-color 140ms ease;
}}

button.perm-decline-btn:hover {{
    background-color: rgba(239, 68, 68, 0.12);
    border-color: rgba(239, 68, 68, 0.35);
    color: #FCA5A5;
}}

.text-loop-banner {{
    border-radius: 12px;
    background-color: rgba(15, 16, 22, 0.72);
    border: 1px solid rgba(82, 39, 255, 0.32);
}}

/* Prompt Summary Block */
.prompt-block {{
    background-color: {c.bg_code_block};
    border: 1px solid {c.border_default};
    border-radius: 12px;
}}

.prompt-block-header {{
    padding: 8px 12px;
    border-bottom: 1px solid {c.border_subtle};
}}

.prompt-block-label {{
    font-size: 12px;
    font-weight: 600;
    color: {c.text_secondary};
}}

button.prompt-copy-btn {{
    background-color: transparent;
    background-image: none;
    border: 1px solid {c.border_subtle};
    border-radius: 7px;
    padding: 2px 9px;
    min-height: 24px;
    font-size: 11.5px;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 140ms ease, color 140ms ease, border-color 140ms ease;
}}

button.prompt-copy-btn:hover {{
    background-color: {c.bg_hover};
    color: {c.text_primary};
    border-color: {c.border_strong};
}}

.prompt-token-key {{
    font-size: 12px;
    font-weight: 600;
    color: {c.text_muted};
    min-width: 72px;
}}

.prompt-token-val {{
    font-size: 13px;
    color: {c.text_primary};
}}

/* Web Search Card */
.research-card {{
    background-color: {c.bg_surface};
    border: 1px solid {c.border_default};
    border-radius: 13px;
    padding: 12px 14px;
}}

.research-badge {{
    font-size: 11px;
    font-weight: 500;
    color: {c.text_secondary};
    background-color: rgba(255, 255, 255, 0.06);
    border-radius: 6px;
    padding: 2px 8px;
}}

.research-tab-row {{
    background-color: rgba(236, 239, 244, 0.03);
    border: 1px solid {c.border_subtle};
    border-radius: 8px;
    padding: 7px 10px;
}}

.research-tab-title {{
    font-size: 12.5px;
    font-weight: 500;
    color: {c.text_primary};
}}

.research-tab-url {{
    font-family: {t.mono};
    font-size: 11px;
    color: {c.text_muted};
}}

/* Ephemeral Web Reference Picker Card */
.ref-picker-card {{
    background-color: {c.bg_surface};
    border: 1px solid rgba(168, 181, 200, 0.34);
    border-radius: 15px;
    padding: 14px 16px;
}}

.ref-zero-trace-badge {{
    font-size: 11px;
    font-weight: 600;
    color: #86EFAC;
    background-color: rgba(34, 197, 94, 0.14);
    border: 1px solid rgba(34, 197, 94, 0.28);
    border-radius: 999px;
    padding: 2px 9px;
}}

.ref-thumb-tile {{
    background-color: rgba(255, 255, 255, 0.03);
    border: 1.5px solid {c.border_default};
    border-radius: 12px;
    padding: 8px;
    transition: border-color 140ms ease, background-color 140ms ease;
}}

.ref-thumb-tile:hover {{
    border-color: rgba(168, 181, 200, 0.55);
    background-color: rgba(255, 255, 255, 0.055);
}}

.ref-thumb-tile.selected {{
    border-color: #60A5FA;
    background-color: rgba(59, 130, 246, 0.11);
}}

.ref-palette-pill {{
    font-size: 10.5px;
    font-weight: 500;
    color: {c.text_secondary};
    background-color: rgba(255, 255, 255, 0.06);
    border-radius: 999px;
    padding: 2px 7px;
}}

button.ref-render-btn {{
    background-color: #2563EB;
    background-image: none;
    border: 1px solid rgba(96, 165, 250, 0.45);
    border-radius: 999px;
    padding: 0 18px;
    min-height: 36px;
    font-size: 13px;
    font-weight: 600;
    color: #F8FAFC;
    box-shadow: none;
    transition: background-color 140ms ease;
}}

button.ref-render-btn:hover {{
    background-color: #1D4ED8;
}}

button.ref-render-btn:disabled {{
    background-color: rgba(255, 255, 255, 0.08);
    border-color: {c.border_subtle};
    color: {c.text_muted};
}}

button.ref-secondary-btn {{
    background-color: transparent;
    background-image: none;
    border: 1px solid {c.border_default};
    border-radius: 999px;
    padding: 0 14px;
    min-height: 36px;
    font-size: 12.5px;
    font-weight: 500;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 140ms ease, color 140ms ease;
}}

button.ref-secondary-btn:hover {{
    background-color: {c.bg_hover};
    color: {c.text_primary};
}}

/* Memory Handoff Progress Card */
.handoff-card {{
    background-color: {c.bg_surface};
    border: 1px solid {c.border_default};
    border-radius: 13px;
    padding: 13px 15px;
}}

.handoff-title {{
    font-size: 13.5px;
    font-weight: 600;
    color: {c.text_primary};
}}

.handoff-sub {{
    font-size: 12.5px;
    color: {c.text_secondary};
}}

.handoff-mono {{
    font-family: {t.mono};
    font-size: 11px;
    color: {c.text_muted};
}}

.handoff-progress trough {{
    background-color: rgba(236, 239, 244, 0.08);
    border-radius: 999px;
    min-height: 5px;
}}

.handoff-progress progress {{
    background-color: {c.accent_primary};
    border-radius: 999px;
    min-height: 5px;
}}

.handoff-stage-pill {{
    font-size: 11.5px;
    font-weight: 500;
    padding: 3px 9px;
    border-radius: 7px;
    background-color: rgba(236, 239, 244, 0.04);
    color: {c.text_muted};
}}

.handoff-stage-pill.done {{
    color: {c.text_primary};
    background-color: rgba(255, 255, 255, 0.08);
}}

.handoff-stage-pill.current {{
    color: #DFC396;
    background-color: {c.accent_amber_soft};
}}

/* Image Grid */
.keyframe-tile {{
    background-color: {c.bg_surface};
    border: 1px solid {c.border_default};
    border-radius: {g.radius_media}px;
    transition: border-color 160ms cubic-bezier(0.22, 1, 0.36, 1);
}}

.keyframe-tile:hover {{
    border-color: {c.border_focus};
}}

.keyframe-badge {{
    font-size: 11px;
    font-weight: 500;
    color: rgba(242, 244, 248, 0.88);
    background-color: rgba(16, 17, 20, 0.56);
    border: none;
    border-radius: 7px;
    padding: 2px 8px;
}}

button.grid-layout-pill {{
    background-color: transparent;
    background-image: none;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 7px;
    min-height: 22px;
    min-width: 22px;
    font-family: {t.mono};
    font-size: 11px;
    color: {c.text_muted};
    box-shadow: none;
}}

button.grid-layout-pill:hover {{
    background-color: {c.bg_hover};
    color: {c.text_primary};
}}

button.grid-layout-pill.active {{
    background-color: {c.bg_elevated};
    border-color: {c.border_default};
    color: {c.text_primary};
}}

/* Video Player Card */
.video-card {{
    background-color: {c.bg_surface};
    border: 1px solid {c.border_default};
    border-radius: 14px;
}}

.video-controls-bar {{
    background-color: {c.bg_surface};
    border-top: 1px solid {c.border_subtle};
    padding: 7px 12px;
}}

.video-timecode {{
    font-family: {t.mono};
    font-size: 11.5px;
    color: {c.text_secondary};
}}

button.video-play-circle {{
    background-color: rgba(18, 20, 23, 0.84);
    background-image: none;
    border: 1px solid rgba(236, 239, 244, 0.22);
    border-radius: 999px;
    min-width: 46px;
    min-height: 46px;
    color: {c.text_primary};
    box-shadow: none;
    transition: background-color 160ms cubic-bezier(0.22, 1, 0.36, 1),
                border-color 160ms cubic-bezier(0.22, 1, 0.36, 1);
}}

button.video-play-circle:hover {{
    background-color: rgba(34, 38, 46, 0.96);
    border-color: rgba(236, 239, 244, 0.38);
}}

/* Floating Bottom Composer */
.composer-scrim {{
    background-image: linear-gradient(
        to bottom,
        rgba(22, 24, 28, 0.0) 0%,
        rgba(22, 24, 28, 0.72) 34%,
        rgba(22, 24, 28, 0.98) 76%,
        {c.bg_canvas} 100%
    );
    padding: 18px 20px 12px 20px;
}}

.aquarium-tank {{
    border-radius: 22px;
    border: 1px solid rgba(255, 255, 255, 0.14);
    background-color: #7FD3E3;
    box-shadow: 0 18px 42px rgba(0, 0, 0, 0.45);
}}

.aquarium-inner {{
    padding: 26px 20px 12px 20px;
}}

.aquarium-caption {{
    font-size: 11.5px;
    font-weight: 600;
    letter-spacing: 0.25px;
    color: #06303B;
    background-color: rgba(238, 251, 255, 0.52);
    border: 1px solid rgba(255, 255, 255, 0.55);
    border-radius: 999px;
    padding: 2px 11px;
    margin-top: 9px;
}}

.composer-pill {{
    background-color: {c.bg_composer};
    border: 1px solid {c.border_default};
    border-radius: {g.radius_composer}px;
    padding: 10px 12px 8px 14px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.34);
    transition: border-color 180ms cubic-bezier(0.22, 1, 0.36, 1),
                background-color 180ms cubic-bezier(0.22, 1, 0.36, 1);
}}

.composer-pill.focused {{
    border-color: {c.border_focus};
    background-color: #272727;
}}

.composer-pill.aquarium-glass {{
    background-color: rgba(255, 255, 255, 0.54);
    border: 1px solid rgba(255, 255, 255, 0.72);
    border-radius: 20px;
    box-shadow: 0 10px 28px rgba(4, 34, 42, 0.16);
}}

.composer-pill.aquarium-glass.focused {{
    background-color: rgba(255, 255, 255, 0.68);
    border-color: rgba(255, 255, 255, 0.92);
}}

.composer-textview {{
    background-color: transparent;
    color: {c.text_primary};
    font-family: {t.ui_sans};
    font-size: 14.5px;
    caret-color: {c.text_primary};
}}

.composer-textview text {{
    background-color: transparent;
    color: {c.text_primary};
}}

.composer-pill.aquarium-glass .composer-textview,
.composer-pill.aquarium-glass .composer-textview text {{
    color: #06303B;
    caret-color: #06303B;
}}

.composer-placeholder {{
    font-size: 14.5px;
    color: {c.text_muted};
}}

.composer-pill.aquarium-glass .composer-placeholder {{
    color: rgba(11, 71, 87, 0.62);
}}

.composer-toolbar {{
    padding-top: 8px;
}}

menubutton.skill-chip {{
    background: transparent;
    border: none;
    padding: 0;
    margin: 0;
}}

menubutton.skill-chip > button {{
    background-color: transparent;
    background-image: none;
    border: 1px solid {c.border_subtle};
    border-radius: 999px;
    padding: 3px 10px;
    min-height: 26px;
    min-width: 0;
    font-size: 12.5px;
    font-weight: 500;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 140ms ease, color 140ms ease, border-color 140ms ease;
}}

menubutton.skill-chip > button:hover {{
    background-color: rgba(236, 239, 244, 0.06);
    color: {c.text_primary};
    border-color: {c.border_default};
}}

.composer-pill.aquarium-glass menubutton.skill-chip > button {{
    border-color: rgba(11, 71, 87, 0.24);
    color: #0B4757;
}}

.composer-pill.aquarium-glass menubutton.skill-chip > button:hover {{
    background-color: rgba(11, 71, 87, 0.08);
    color: #06303B;
    border-color: rgba(11, 71, 87, 0.42);
}}

.selector-group {{
    background-color: transparent;
    border: none;
    padding: 0 2px;
}}

button.selector-option {{
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: 7px;
    padding: 2px 7px;
    font-size: 12px;
    font-weight: 450;
    color: {c.text_muted};
    min-height: 24px;
    min-width: 22px;
    box-shadow: none;
    transition: background-color 130ms ease, color 130ms ease;
}}

button.selector-option:hover {{
    background-color: rgba(236, 239, 244, 0.05);
    color: {c.text_primary};
}}

button.selector-option.selected {{
    background-color: rgba(236, 239, 244, 0.11);
    color: {c.text_primary};
    font-weight: 550;
}}

.composer-pill.aquarium-glass button.selector-option {{
    color: rgba(11, 71, 87, 0.72);
}}

.composer-pill.aquarium-glass button.selector-option:hover {{
    background-color: rgba(11, 71, 87, 0.08);
    color: #06303B;
}}

.composer-pill.aquarium-glass button.selector-option.selected {{
    background-color: rgba(11, 71, 87, 0.16);
    color: #06303B;
    font-weight: 600;
}}

.selector-prefix {{
    font-size: 11.5px;
    color: {c.text_muted};
    padding-left: 4px;
    padding-right: 4px;
}}

.composer-pill.aquarium-glass .selector-prefix {{
    color: rgba(11, 71, 87, 0.72);
}}

.toolbar-divider {{
    background-color: {c.border_subtle};
    min-width: 1px;
    min-height: 14px;
    margin: 4px 2px;
}}

.composer-pill.aquarium-glass .toolbar-divider {{
    background-color: rgba(11, 71, 87, 0.18);
}}

button.send-btn {{
    background-color: {c.btn_primary_bg};
    background-image: none;
    color: {c.btn_primary_fg};
    border: none;
    border-radius: 999px;
    min-width: 30px;
    min-height: 30px;
    padding: 0;
    box-shadow: none;
    transition: background-color 150ms cubic-bezier(0.22, 1, 0.36, 1),
                opacity 150ms ease;
}}

button.send-btn:hover {{
    background-color: {c.btn_primary_hover};
}}

button.send-btn.disabled {{
    background-color: {c.btn_disabled_bg};
    color: {c.btn_disabled_fg};
}}

button.send-btn.stop {{
    background-color: {c.bg_active};
    border: 1px solid {c.border_strong};
    color: {c.text_primary};
}}

button.send-btn.stop:hover {{
    background-color: #3B4250;
}}

.composer-pill.aquarium-glass button.send-btn {{
    background-color: #0B4757;
    color: #FFFFFF;
}}

.composer-pill.aquarium-glass button.send-btn:hover {{
    background-color: #06303B;
}}

.composer-pill.aquarium-glass button.send-btn.disabled {{
    background-color: rgba(11, 71, 87, 0.14);
    color: #4A7B88;
}}

.composer-pill.aquarium-glass button.send-btn.stop {{
    background-color: #B42318;
    border: 1px solid rgba(255, 255, 255, 0.45);
    color: #FFFFFF;
}}

.composer-pill.aquarium-glass button.send-btn.stop:hover {{
    background-color: #912018;
}}

button.composer-stop-btn {{
    background-color: rgba(255, 255, 255, 0.05);
    background-image: none;
    color: {c.text_muted};
    border: 1px solid {c.border_subtle};
    border-radius: 999px;
    padding: 0 11px;
    min-height: 28px;
    font-size: 12px;
    font-weight: 600;
    box-shadow: none;
    opacity: 0.62;
    transition: background-color 150ms ease, opacity 150ms ease, border-color 150ms ease;
}}

button.composer-stop-btn.active {{
    background-color: #B42318;
    border-color: rgba(255, 255, 255, 0.28);
    color: #FFFFFF;
    opacity: 1.0;
}}

button.composer-stop-btn.active:hover {{
    background-color: #D92D20;
}}

.composer-pill.aquarium-glass button.composer-stop-btn {{
    background-color: rgba(11, 71, 87, 0.12);
    border-color: rgba(11, 71, 87, 0.24);
    color: #0B4757;
}}

.composer-pill.aquarium-glass button.composer-stop-btn.active {{
    background-color: #B42318;
    border-color: rgba(255, 255, 255, 0.55);
    color: #FFFFFF;
}}

.composer-footnote {{
    font-size: 11px;
    color: {c.text_faint};
    padding-top: 5px;
}}

menubutton.model-status-btn {{
    background: transparent;
    border: none;
    padding: 0;
    margin-top: 5px;
}}

menubutton.model-status-btn > button {{
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: 999px;
    padding: 2px 10px;
    min-height: 20px;
    min-width: 0;
    font-size: 11.5px;
    color: {c.text_muted};
    box-shadow: none;
    transition: background-color 140ms ease, color 140ms ease;
}}

menubutton.model-status-btn > button:hover {{
    background-color: rgba(255, 255, 255, 0.045);
    color: {c.text_secondary};
}}

.model-status-row-title {{
    font-size: 12.5px;
    font-weight: 500;
    color: {c.text_primary};
}}

.model-status-badge-active {{
    font-size: 10.5px;
    font-weight: 600;
    color: {c.accent_sage};
    background-color: {c.accent_sage_soft};
    border-radius: 6px;
    padding: 1px 6px;
}}

.model-status-badge-standby {{
    font-size: 10.5px;
    color: {c.text_muted};
    background-color: rgba(255, 255, 255, 0.05);
    border-radius: 6px;
    padding: 1px 6px;
}}

/* Modal Lightbox Inspector */
.lightbox-scrim {{
    background-color: {c.bg_lightbox_scrim};
}}

.lightbox-topbar {{
    padding: 12px 18px;
    border-bottom: 1px solid {c.border_subtle};
    background-color: rgba(18, 20, 23, 0.88);
}}

button.lightbox-pill-btn {{
    background-color: {c.bg_surface};
    background-image: none;
    border: 1px solid {c.border_default};
    border-radius: 9px;
    padding: 5px 10px;
    font-size: 12px;
    color: {c.text_secondary};
    box-shadow: none;
}}

button.lightbox-pill-btn:hover {{
    background-color: {c.bg_hover};
    color: {c.text_primary};
}}

button.lightbox-pill-btn.active {{
    background-color: {c.bg_active};
    border-color: {c.border_strong};
    color: {c.text_primary};
}}

.lightbox-drawer {{
    background-color: {c.bg_sidebar};
    border-left: 1px solid {c.border_subtle};
    min-width: 280px;
    padding: 16px;
}}

.lightbox-meta-key {{
    font-family: {t.mono};
    font-size: 10.5px;
    color: {c.text_muted};
}}

.lightbox-meta-val {{
    font-family: {t.mono};
    font-size: 12px;
    color: {c.text_primary};
}}

.lightbox-bottom-controls {{
    background-color: rgba(22, 24, 28, 0.94);
    border: 1px solid {c.border_default};
    border-radius: 14px;
    padding: 8px 14px;
    margin: 14px;
}}

/* Popovers */
popover > contents {{
    background-color: {c.bg_popover};
    border: 1px solid {c.border_strong};
    border-radius: 12px;
    padding: 6px;
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.44);
}}

button.popover-item {{
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: 8px;
    padding: 7px 10px;
    color: {c.text_secondary};
    font-size: 13px;
    box-shadow: none;
}}

button.popover-item:hover {{
    background-color: {c.bg_hover};
    color: {c.text_primary};
}}

button.popover-item.active {{
    background-color: {c.accent_primary_soft};
    color: {c.text_primary};
}}

/* Subtle Feedback Toast */
.vimona-toast {{
    background-color: {c.bg_elevated};
    border: 1px solid {c.border_strong};
    border-radius: 999px;
    padding: 6px 14px;
    font-size: 12.5px;
    color: {c.text_primary};
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.38);
}}

/* High-RAM Prompt Alert & Installed Apps RAM Manager */
.ram-alert-card {{
    background-color: #251F19;
    border: 1px solid rgba(235, 203, 139, 0.45);
    border-radius: 14px;
    padding: 10px 14px;
    margin: 0 18px 2px 18px;
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.44);
}}

.ram-alert-title {{
    font-size: 13px;
    font-weight: 600;
    color: {c.text_primary};
}}

.ram-alert-sub {{
    font-size: 12px;
    color: {c.text_secondary};
}}

button.ram-alert-primary {{
    background-color: #EBCB8B;
    background-image: none;
    color: #191612;
    border: none;
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    box-shadow: none;
}}

button.ram-alert-primary:hover {{
    background-color: #F4DAA4;
}}

button.ram-alert-override {{
    background-color: rgba(163, 190, 140, 0.16);
    background-image: none;
    color: #A3BE8C;
    border: 1px solid rgba(163, 190, 140, 0.42);
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    box-shadow: none;
}}

button.ram-alert-override:hover {{
    background-color: rgba(163, 190, 140, 0.28);
}}

button.ram-alert-secondary {{
    background-color: rgba(255, 255, 255, 0.06);
    background-image: none;
    color: {c.text_primary};
    border: 1px solid {c.border_subtle};
    border-radius: 999px;
    padding: 5px 11px;
    font-size: 12px;
    font-weight: 500;
    box-shadow: none;
}}

button.ram-alert-secondary:hover {{
    background-color: rgba(255, 255, 255, 0.12);
}}

.ram-app-row {{
    background-color: {c.bg_surface};
    border: 1px solid {c.border_subtle};
    border-radius: 12px;
    padding: 10px 14px;
}}

popover.ram-popover > contents {{
    background-color: {c.bg_elevated};
    border: 1px solid {c.border_strong};
    border-radius: 14px;
    padding: 12px 14px;
    box-shadow: 0 16px 38px rgba(0, 0, 0, 0.55);
}}

.ram-popover-row {{
    background-color: {c.bg_surface};
    border: 1px solid {c.border_subtle};
    border-radius: 10px;
    padding: 7px 10px;
}}

.ram-popover-row.top-consumer {{
    border-color: rgba(235, 203, 139, 0.48);
    background-color: rgba(235, 203, 139, 0.06);
}}

.ram-protected-tag {{
    font-size: 10.5px;
    font-weight: 500;
    color: {c.text_muted};
    background-color: rgba(255, 255, 255, 0.05);
    border: 1px solid {c.border_subtle};
    border-radius: 999px;
    padding: 2px 8px;
}}

button.ram-cut-btn {{
    background-color: #B44848;
    background-image: none;
    color: #FFFFFF;
    border: none;
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    box-shadow: none;
}}

button.ram-cut-btn:hover {{
    background-color: #CC5858;
}}

/* Safe-Blur Preview Overlay & Hover Control */
.safe-blur-scrim {{
    background-color: rgba(12, 13, 17, 0.78);
    border-radius: 10px;
}}

.safe-blur-scrim-compact {{
    background-color: rgba(13, 14, 19, 0.84);
    border-radius: 6px;
}}

.safe-blur-label {{
    background-color: rgba(20, 22, 27, 0.86);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 999px;
    padding: 4px 12px;
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.01em;
    color: rgba(244, 246, 250, 0.94);
    text-shadow: 0 1px 4px rgba(0, 0, 0, 0.65);
}}

.safe-blur-label-compact {{
    background-color: rgba(20, 22, 27, 0.82);
    border: none;
    border-radius: 999px;
    padding: 0 7px;
    font-size: 10px;
    font-weight: 500;
    color: rgba(244, 246, 250, 0.90);
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.65);
}}

button.safe-blur-hover-btn {{
    background-color: rgba(24, 25, 29, 0.72);
    background-image: none;
    border: none;
    border-radius: 999px;
    padding: 4px 7px;
    min-width: 24px;
    min-height: 24px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.32);
}}

button.safe-blur-hover-btn:hover {{
    background-color: rgba(42, 44, 52, 0.92);
}}

button.safe-blur-toggle {{
    background-color: rgba(255, 255, 255, 0.05);
    background-image: none;
    border: none;
    border-radius: 999px;
    padding: 6px 12px;
    min-height: 30px;
    font-size: 12.5px;
    font-weight: 500;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 140ms ease, color 140ms ease;
}}

button.safe-blur-toggle:hover {{
    background-color: rgba(255, 255, 255, 0.10);
    color: {c.text_primary};
}}

button.safe-blur-toggle.active {{
    background-color: rgba(255, 255, 255, 0.11);
    color: {c.text_primary};
}}

/* Deleted Prompt Empty Textbox Slot */
.user-pill.deleted-empty {{
    background-color: {c.bg_surface};
    border: 1px dashed {c.border_strong};
    border-radius: 14px;
    padding: 4px 12px;
    min-width: 240px;
    min-height: 36px;
}}

entry.empty-prompt-entry {{
    background-color: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
    color: {c.text_primary};
    font-size: 13.5px;
    min-width: 210px;
    min-height: 26px;
}}

/* Quiet / Eco vs. Turbo Performance Segmented Toggle */
.perf-seg-group {{
    background-color: rgba(255, 255, 255, 0.045);
    border: 1px solid {c.border_default};
    border-radius: 999px;
    padding: 2px;
}}

button.perf-seg-btn {{
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: 999px;
    padding: 3px 9px;
    min-height: 22px;
    font-size: 11px;
    font-weight: 600;
    color: {c.text_secondary};
    box-shadow: none;
    transition: background-color 140ms ease, color 140ms ease;
}}

button.perf-seg-btn:hover {{
    color: {c.text_primary};
}}

button.perf-seg-btn.active-eco {{
    background-color: rgba(16, 185, 129, 0.18);
    color: #6EE7B7;
}}

button.perf-seg-btn.active-turbo {{
    background-color: rgba(245, 158, 11, 0.20);
    color: #FCD34D;
}}

/* Overnight Render Playlist Cards & Status Pills */
.playlist-job-card {{
    background-color: rgba(255, 255, 255, 0.035);
    border: 1px solid {c.border_subtle};
    border-radius: 10px;
    padding: 6px 8px;
}}

.playlist-job-card.active {{
    border-color: rgba(96, 165, 250, 0.45);
    background-color: rgba(59, 130, 246, 0.08);
}}

.playlist-status-pill {{
    font-size: 10px;
    font-weight: 600;
    border-radius: 999px;
    padding: 1px 7px;
    background-color: rgba(255, 255, 255, 0.08);
    color: {c.text_secondary};
}}

.playlist-status-pill.rendering {{
    background-color: rgba(59, 130, 246, 0.18);
    color: #93C5FD;
}}

.playlist-status-pill.checking {{
    background-color: rgba(245, 158, 11, 0.18);
    color: #FCD34D;
}}

.playlist-status-pill.done {{
    background-color: rgba(34, 197, 94, 0.18);
    color: #86EFAC;
}}

.playlist-status-pill.failed {{
    background-color: rgba(239, 68, 68, 0.18);
    color: #FCA5A5;
}}

button.playlist-toggle-chip {{
    background-color: rgba(255, 255, 255, 0.04);
    background-image: none;
    border: 1px solid {c.border_subtle};
    border-radius: 999px;
    padding: 2px 8px;
    min-height: 20px;
    font-size: 10.5px;
    font-weight: 500;
    color: {c.text_muted};
    box-shadow: none;
}}

button.playlist-toggle-chip.active {{
    background-color: rgba(96, 165, 250, 0.14);
    border-color: rgba(96, 165, 250, 0.35);
    color: #93C5FD;
}}

progressbar.playlist-progress trough {{
    min-height: 3px;
    background-color: rgba(255, 255, 255, 0.08);
    border-radius: 999px;
}}

progressbar.playlist-progress progress {{
    min-height: 3px;
    background-color: #60A5FA;
    border-radius: 999px;
}}
"""
