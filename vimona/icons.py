"""Crisp true-vector SVG and Cairo icon system for Vimona.

Renders directly onto the GTK4 Cairo context at native surface DPI via
Gtk.DrawingArea + Rsvg.Handle so every icon and the Vimona mark remain
mathematically sharp with zero bitmap scaling blur.
"""

from __future__ import annotations

from functools import lru_cache

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Rsvg", "2.0")
from gi.repository import Gtk, Rsvg

ICON_PATHS: dict[str, str] = {
    "sidebar": (
        '<rect x="3" y="4" width="18" height="16" rx="3" />'
        '<line x1="9" y1="4" x2="9" y2="20" />'
    ),
    "compose": (
        '<path d="M12 5v14" />'
        '<path d="M5 12h14" />'
    ),
    "pin": (
        '<path d="M14.5 3.5l6 6-2 2-1.5-.5-4 4 .5 2.5-1.5 1.5-3.5-3.5-4 4-1-1 4-4-3.5-3.5 1.5-1.5 2.5.5 4-4-.5-1.5z" />'
    ),
    "chevron-right": '<path d="M9 6l6 6-6 6" />',
    "chevron-down": '<path d="M6 9l6 6 6-6" />',
    "chevron-left": '<path d="M15 6l-6 6 6 6" />',
    "copy": (
        '<rect x="9" y="9" width="11" height="11" rx="2.2" />'
        '<path d="M5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5" />'
    ),
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5" />',
    "edit": (
        '<path d="M15.5 4.5l4 4L8.5 19.5H4.5v-4z" />'
        '<path d="M13 7l4 4" />'
    ),
    "arrow-up": (
        '<path d="M12 18.5V5.5" />'
        '<path d="M6.5 11L12 5.5 17.5 11" />'
    ),
    "stop": '<rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="currentColor" stroke="none" />',
    "play": '<path d="M7.5 5.5l11 6.5-11 6.5z" fill="currentColor" stroke="none" />',
    "pause": (
        '<rect x="7" y="5.5" width="3.2" height="13" rx="1" fill="currentColor" stroke="none" />'
        '<rect x="13.8" y="5.5" width="3.2" height="13" rx="1" fill="currentColor" stroke="none" />'
    ),
    "volume": (
        '<path d="M5 9.5h3.5L13 5.5v13l-4.5-4H5z" />'
        '<path d="M16.5 9a4.5 4.5 0 0 1 0 6" />'
        '<path d="M19 6.8a7.8 7.8 0 0 1 0 10.4" />'
    ),
    "mute": (
        '<path d="M5 9.5h3.5L13 5.5v13l-4.5-4H5z" />'
        '<path d="M16.5 9.5l4 5" />'
        '<path d="M20.5 9.5l-4 5" />'
    ),
    "expand": (
        '<path d="M15 4h5v5" />'
        '<path d="M9 20H4v-5" />'
        '<path d="M20 4l-6 6" />'
        '<path d="M4 20l6-6" />'
    ),
    "close": (
        '<path d="M6 6l12 12" />'
        '<path d="M18 6L6 18" />'
    ),
    "zoom-in": (
        '<circle cx="10.5" cy="10.5" r="6.5" />'
        '<path d="M15.5 15.5L20 20" />'
        '<path d="M10.5 7.8v5.4" />'
        '<path d="M7.8 10.5h5.4" />'
    ),
    "zoom-out": (
        '<circle cx="10.5" cy="10.5" r="6.5" />'
        '<path d="M15.5 15.5L20 20" />'
        '<path d="M7.8 10.5h5.4" />'
    ),
    "info": (
        '<circle cx="12" cy="12" r="8.5" />'
        '<path d="M12 11v5" />'
        '<circle cx="12" cy="7.8" r="0.8" fill="currentColor" stroke="none" />'
    ),
    "globe": (
        '<circle cx="12" cy="12" r="8.5" />'
        '<ellipse cx="12" cy="12" rx="3.8" ry="8.5" />'
        '<path d="M3.8 9.5h16.4" />'
        '<path d="M3.8 14.5h16.4" />'
    ),
    "chip": (
        '<rect x="6.5" y="6.5" width="11" height="11" rx="2" />'
        '<rect x="9.5" y="9.5" width="5" height="5" rx="0.8" />'
        '<path d="M9.5 3.5v3M14.5 3.5v3M9.5 17.5v3M14.5 17.5v3" />'
        '<path d="M3.5 9.5h3M3.5 14.5h3M17.5 9.5h3M17.5 14.5h3" />'
    ),
    "grid": (
        '<rect x="4" y="4" width="6.8" height="6.8" rx="1.5" />'
        '<rect x="13.2" y="4" width="6.8" height="6.8" rx="1.5" />'
        '<rect x="4" y="13.2" width="6.8" height="6.8" rx="1.5" />'
        '<rect x="13.2" y="13.2" width="6.8" height="6.8" rx="1.5" />'
    ),
    "layers": (
        '<path d="M12 4.5L3.5 9l8.5 4.5L20.5 9z" />'
        '<path d="M3.5 13l8.5 4.5 8.5-4.5" />'
        '<path d="M3.5 17l8.5 4.5 8.5-4.5" />'
    ),
    "loop": (
        '<path d="M17 4.5l3 3-3 3" />'
        '<path d="M4 11V9.5A2 2 0 0 1 6 7.5h14" />'
        '<path d="M7 19.5l-3-3 3-3" />'
        '<path d="M20 13v1.5a2 2 0 0 1-2 2H4" />'
    ),
    "step-back": (
        '<path d="M7 6v12" />'
        '<path d="M17 6.5l-7 5.5 7 5.5z" fill="currentColor" stroke="none" />'
    ),
    "step-fwd": (
        '<path d="M17 6v12" />'
        '<path d="M7 6.5l7 5.5-7 5.5z" fill="currentColor" stroke="none" />'
    ),
    "gallery": (
        '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />'
        '<circle cx="9" cy="9.5" r="1.4" fill="currentColor" stroke="none" />'
        '<path d="M4.5 16.5l4.8-4.5 3.6 3.3 2.6-2.4 4 3.6" />'
    ),
    "download": (
        '<path d="M12 4.5v10.5" />'
        '<path d="M7.5 11.5L12 16l4.5-4.5" />'
        '<path d="M4.5 18.5h15" />'
    ),
    "trash": (
        '<path d="M4.5 6.5h15" />'
        '<path d="M9.5 4h5" />'
        '<path d="M6.5 6.5l1 13a1.5 1.5 0 0 0 1.5 1.4h6a1.5 1.5 0 0 0 1.5-1.4l1-13" />'
        '<path d="M10 10.5v6M14 10.5v6" />'
    ),
    "more": (
        '<circle cx="6.5" cy="12" r="1.4" fill="currentColor" stroke="none" />'
        '<circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />'
        '<circle cx="17.5" cy="12" r="1.4" fill="currentColor" stroke="none" />'
    ),
    "eye": (
        '<path d="M2 12s3.8-6.5 10-6.5S22 12 22 12s-3.8 6.5-10 6.5S2 12 2 12z" />'
        '<circle cx="12" cy="12" r="2.8" />'
    ),
    "eye-off": (
        '<path d="M17.9 17.9A10.4 10.4 0 0 1 12 18.5C5.8 18.5 2 12 2 12a18.2 18.2 0 0 1 5.1-5.4" />'
        '<path d="M9.9 5.7A9.6 9.6 0 0 1 12 5.5c6.2 0 10 6.5 10 6.5a18.5 18.5 0 0 1-2.5 3.4" />'
        '<path d="M3 3l18 18" />'
    ),
    "bell": (
        '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />'
        '<path d="M13.7 21a2 2 0 0 1-3.4 0" />'
    ),
    "bell-off": (
        '<path d="M13.7 21a2 2 0 0 1-3.4 0" />'
        '<path d="M8.6 8.6C8.2 9.9 8 11.4 8 13c0 4-3 4-3 4h11" />'
        '<path d="M18 13c0-3.2-2.2-6-6-6-.7 0-1.4.1-2 .3" />'
        '<path d="M3 3l18 18" />'
    ),
    "leaf": (
        '<path d="M19 5c-7 0-12 3.5-13 10.5C6 18 8 19.5 10.5 19.5c5.5 0 8.5-4.5 8.5-14.5z" />'
        '<path d="M6 19c2.5-3 5.5-5.5 9-7.5" />'
    ),
    "bolt": (
        '<path d="M13 2L4.5 13.5H11L10 22L19.5 10.5H13L13 2z" />'
    ),
    "sparkles": (
        '<path d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6L12 3z" />'
        '<path d="M18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2z" />'
    ),
    "arrow-down": (
        '<path d="M12 5v14" />'
        '<path d="M6 13l6 6 6-6" />'
    ),
}


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) == 6:
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)
    return (0.93, 0.93, 0.93)


def _draw_vimona_mark(cr: cairo.Context, w: float, h: float, color: str) -> None:
    """Draw a sculpted liquid-chrome geometric Vimona ribbon 'V' in pure vector Cairo."""
    s = min(w, h)
    cr.save()
    cr.translate((w - s) * 0.5, (h - s) * 0.5)
    cr.scale(s / 24.0, s / 24.0)

    # Rear right wing — cooler deep gunmetal-to-silver chrome reflection band
    g_rear = cairo.LinearGradient(11.3, 4.4, 21.9, 20.1)
    g_rear.add_color_stop_rgba(0.00, 0.72, 0.77, 0.84, 0.85)
    g_rear.add_color_stop_rgba(0.32, 0.16, 0.18, 0.22, 0.90)
    g_rear.add_color_stop_rgba(0.54, 0.94, 0.96, 1.00, 0.92)
    g_rear.add_color_stop_rgba(0.76, 0.26, 0.30, 0.36, 0.88)
    g_rear.add_color_stop_rgba(1.00, 0.68, 0.74, 0.82, 0.86)
    cr.set_source(g_rear)
    cr.move_to(11.3, 20.1)
    cr.line_to(18.2, 4.4)
    cr.line_to(21.9, 4.4)
    cr.line_to(13.8, 20.1)
    cr.close_path()
    cr.fill_preserve()
    cr.set_source_rgba(1.0, 1.0, 1.0, 0.32)
    cr.set_line_width(0.45)
    cr.stroke()

    # Foreground left wing — bright mercury-chrome specular ribbon with subtle prism dispersion
    g_front = cairo.LinearGradient(2.1, 4.4, 14.0, 20.1)
    g_front.add_color_stop_rgba(0.00, 0.98, 0.99, 1.00, 0.98)
    g_front.add_color_stop_rgba(0.28, 0.22, 0.24, 0.30, 0.98)
    g_front.add_color_stop_rgba(0.48, 0.99, 0.99, 1.00, 1.00)
    g_front.add_color_stop_rgba(0.68, 0.48, 0.56, 0.68, 0.96)
    g_front.add_color_stop_rgba(0.84, 0.14, 0.16, 0.20, 0.98)
    g_front.add_color_stop_rgba(1.00, 0.92, 0.95, 1.00, 0.98)
    cr.set_source(g_front)
    cr.move_to(2.1, 4.4)
    cr.line_to(6.6, 4.4)
    cr.line_to(14.0, 20.1)
    cr.line_to(9.7, 20.1)
    cr.close_path()
    cr.fill_preserve()
    cr.set_source_rgba(1.0, 1.0, 1.0, 0.45)
    cr.set_line_width(0.45)
    cr.stroke()

    cr.restore()


@lru_cache(maxsize=256)
def _get_svg_handle(name: str, color: str, stroke_width: float) -> Rsvg.Handle:
    body = ICON_PATHS.get(name, "")
    colored_body = body.replace("currentColor", color)
    svg_xml = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="{stroke_width}" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f"{colored_body}</svg>"
    )
    return Rsvg.Handle.new_from_data(svg_xml.encode("utf-8"))


class SvgIcon(Gtk.DrawingArea):
    """True-vector GTK4 icon widget rendered directly into Cairo at native DPI."""

    def __init__(
        self,
        name: str,
        size_px: int = 16,
        color: str = "#EDEDED",
        stroke_width: float = 1.65,
    ) -> None:
        super().__init__()
        self._name = name
        self._size_px = size_px
        self._color = color
        self._stroke_width = stroke_width
        self.set_content_width(size_px)
        self.set_content_height(size_px)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def set_icon(self, name: str | None = None, color: str | None = None) -> None:
        if name is not None:
            self._name = name
        if color is not None:
            self._color = color
        self.queue_draw()

    def _draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        if self._name == "vimona":
            _draw_vimona_mark(cr, float(w), float(h), self._color)
            return

        handle = _get_svg_handle(self._name, self._color, self._stroke_width)
        viewport = Rsvg.Rectangle()
        viewport.x = 0.0
        viewport.y = 0.0
        viewport.width = float(w)
        viewport.height = float(h)
        handle.render_document(cr, viewport)
