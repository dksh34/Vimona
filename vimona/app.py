"""GTK4 / Libadwaita application entry point for Vimona."""

from __future__ import annotations

import atexit
import signal
import sys

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import __app_id__
from .tokens import build_stylesheet
from .window import VimonaWindow


class VimonaApplication(Adw.Application):
    """Native Fedora Linux desktop application."""

    def __init__(self, auto_boot_backend: bool = True) -> None:
        super().__init__(application_id=__app_id__)
        self._window: VimonaWindow | None = None
        self._auto_boot_backend = auto_boot_backend
        atexit.register(self._cleanup)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        style_mgr = Adw.StyleManager.get_default()
        style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        focus_action = Gio.SimpleAction.new("focus-chat", GLib.VariantType.new("s"))
        focus_action.connect("activate", self._on_focus_chat_action)
        self.add_action(focus_action)

        open_action = Gio.SimpleAction.new("open-app", None)
        open_action.connect("activate", lambda *_a: self.do_activate())
        self.add_action(open_action)

        provider = Gtk.CssProvider()
        provider.load_from_string(build_stylesheet())
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def do_activate(self) -> None:
        if self._window is None:
            self._window = VimonaWindow(self, auto_boot_backend=self._auto_boot_backend)
        if self._window.is_suspended() or not self._window.get_visible():
            self._window.set_visible(True)
        self._window.unminimize()
        self._window.present()

    def _on_focus_chat_action(self, _action: Gio.SimpleAction, param: GLib.Variant | None) -> None:
        if self._window is None:
            self._window = VimonaWindow(self, auto_boot_backend=self._auto_boot_backend)
        chat_id = param.get_string() if param is not None else ""
        if chat_id and hasattr(self._window, "focus_chat_by_id"):
            self._window.focus_chat_by_id(chat_id)
        if not self._window.get_visible():
            self._window.set_visible(True)
        self._window.unminimize()
        self._window.present()

    def do_shutdown(self) -> None:
        self._cleanup()
        Adw.Application.do_shutdown(self)

    def _cleanup(self) -> None:
        if self._window is not None:
            self._window.shutdown_backend()


def main(argv: list[str] | None = None) -> int:
    app = VimonaApplication()

    def _sig_handler(_signum: int, _frame: object) -> None:
        app._cleanup()
        app.quit()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    return app.run(argv if argv is not None else sys.argv)
