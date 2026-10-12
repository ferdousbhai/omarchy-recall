"""The application: one copy per session (a second launch, or `recall-app --toggle` from a shortcut, reaches the one
that runs), the engine, the panel, the tray icon and the shortcut."""

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, opener, paths  # noqa: E402
from .engine import Engine  # noqa: E402
from .log import log, open_log  # noqa: E402
from .settings import Settings  # noqa: E402


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.settings = Settings.load()
        self.engine = None
        self.panel = None
        self.settings_window = None
        self.onboarding = None
        self.download = None
        self.tray = None
        self.hotkey = None
        self.hotkey_tester = None   # onboarding's "try it now"
        self.debug = {}
        self.add_main_option("toggle", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Show or hide the search panel")
        self.add_main_option("search", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING, "Open the panel with a search",
                             "WORDS")
        self.add_main_option("settings", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Open Settings")
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start in the tray without opening a window (at login)")
        self.add_main_option("quit", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Quit Recall")
        self.add_main_option("snapshot", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Debug: save a window as a PNG after a few seconds, then quit", "FILE")
        self.add_main_option("select", 0, GLib.OptionFlags.NONE, GLib.OptionArg.INT,
                             "Debug: with --snapshot, select the Nth result (from 1)", "N")
        self.add_main_option("window", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Debug: the window --snapshot shows: panel, settings:PAGE, onboarding:STEP", "WINDOW")

    def do_startup(self):
        Adw.Application.do_startup(self)
        open_log(paths.Path(self.settings.index_directory))
        log(f"Recall starting (engine: {paths.engine_binary() or 'not built'})")
        self.hold()   # lives in the tray with no window open
        self._icons()
        self.engine = Engine(self.settings, self._status_changed)
        from .download import ModelDownload
        self.download = ModelDownload(self.settings.models_directory, self._status_changed, self.engine.model_arrived)
        from .power import PowerPolicy
        self.power = PowerPolicy(self.settings, self._power_changed)
        self.engine.backfill = self.power.blocker is None
        from .panel import SearchPanel
        self.panel = SearchPanel(self)
        self.watchers = []
        self._watch()
        for name, accels, action in (("quit", ["<Control>q"], self.quit_app),
                                     ("settings", ["<Control>comma"], self.show_settings)):
            item = Gio.SimpleAction.new(name, None)
            item.connect("activate", lambda *_, a=action: a())
            self.add_action(item)
            self.set_accels_for_action(f"app.{name}", accels)
        self.engine.start()
        if self.settings.onboarded and self.download.needs_start and self.download.phase == "needed":
            self.download.start()
        self._start_desktop()

    def _icons(self):
        """The app's icon, from the icon theme once installed, else from this checkout."""
        from gi.repository import Gdk
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        theme.add_search_path(str(paths.Path(__file__).resolve().parents[1] / "data"))
        Gtk.Window.set_default_icon_name(APP_ID)

    def _watch(self):
        """New and changed files go in a second or two after they land; code once you've stopped typing a minute."""
        for watcher in self.watchers:
            watcher.close()
        self.watchers = []
        from .watcher import Watcher
        try:
            if self.settings.roots:
                self.watchers.append(Watcher(self.settings.roots, self.engine.request_sync))
            if self.settings.code_roots:
                self.watchers.append(Watcher(self.settings.code_roots, lambda: self.engine.request_sync(code=True),
                                             latency=60, longest=600, name="code folders"))
        except OSError as error:
            log(f"no folder watching: {error}")

    def _power_changed(self):
        self.engine.set_backfill(self.power.blocker is None)
        self._status_changed(self.engine.status)

    def _start_desktop(self):
        """The tray icon and the global shortcut, where the desktop offers them."""
        try:
            from .tray import Tray
            self.tray = Tray(self)
        except Exception as error:  # noqa: BLE001 - the app works without a tray icon
            log(f"no tray icon: {error!r}")
        try:
            from .hotkey import Hotkey
            self.hotkey = Hotkey(self, self.settings.hotkey, self._hotkey_pressed)
        except Exception as error:  # noqa: BLE001 - nor without the shortcut (the launcher still opens it)
            log(f"no global shortcut: {error!r}")

    def do_command_line(self, command_line):
        options = command_line.get_options_dict().end().unpack()
        if options.get("quit"):
            self.quit_app()
        elif options.get("settings"):
            self.show_settings()
        elif options.get("snapshot"):
            self.debug["snapshot"] = options["snapshot"]
            window, _, part = options.get("window", "panel").partition(":")
            if window == "settings":
                self.show_settings(part or None)
                self.debug["window"] = self.settings_window
            elif window == "onboarding":
                self.show_onboarding()
                if part in self.onboarding.steps:
                    self.onboarding.go(self.onboarding.steps.index(part))
                self.debug["window"] = self.onboarding
            else:
                self.debug["select"] = options.get("select")
                self.panel.show_panel(options.get("search", ""))
                self.debug["window"] = self.panel
            GLib.timeout_add(6000, self._snapshot)
        elif "search" in options:
            self.panel.show_panel(options["search"])
        elif options.get("toggle"):
            self.toggle_panel()
        elif not options.get("background"):
            self.activate()
        return 0

    def do_activate(self):
        if not self.settings.onboarded and not self.settings.roots:
            self.show_onboarding()
        else:
            self.panel.show_panel()

    # MARK: Actions

    def _hotkey_pressed(self, timestamp=0):
        if self.hotkey_tester:
            return self.hotkey_tester()
        self.toggle_panel(timestamp)

    def toggle_panel(self, timestamp=0):
        if not self.settings.onboarded and not self.settings.roots:
            return self.show_onboarding()
        self.panel.toggle(timestamp)

    def open_row(self, row):
        opener.open_row(row, self.settings.code_editor, opener.landmark(row))

    def show_settings(self, page=None):
        from .settings_window import SettingsWindow
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
        if page:
            self.settings_window.show_page(page)
        self.settings_window.present()

    def show_onboarding(self):
        from .onboarding import Onboarding
        if self.onboarding is None:
            self.onboarding = Onboarding(self)
        self.onboarding.present()

    def choose_folders(self):
        self.panel.hide_panel()
        if not self.settings.onboarded:
            return self.show_onboarding()
        self.show_settings("folders")
        self.settings_window.add_folders()

    def choose_code_folders(self):
        self.panel.hide_panel()
        self.show_settings("code")
        self.settings_window.add_code_folders()

    def apply_folders(self, roots, excluded=None, types=None):
        """New folders to search: saved, and indexing starts on them."""
        self.settings.roots = sorted(set(roots))
        if excluded is not None:
            self.settings.excluded_folders = sorted(set(excluded))
        if types is not None:
            self.settings.excluded_types = sorted({t.strip().lstrip(".").lower() for t in types if t.strip()})
        self.settings.save()
        self.engine.folders_changed()
        self._watch()

    def apply_code_folders(self, roots, excluded=None):
        self.settings.code_roots = sorted(set(roots))
        if excluded is not None:
            self.settings.code_excluded = sorted(set(excluded))
        self.settings.save()
        self.engine.folders_changed()
        self._watch()

    def set_paused(self, paused):
        self.engine.set_paused(paused)

    def quit_app(self):
        log("quitting")
        if self.engine:
            self.engine.stop()
        if self.download:
            self.download.cancel()
        self.release()
        self.quit()

    def _status_changed(self, status):
        if self.panel:
            self.panel.update_status()
        if self.tray:
            self.tray.update()
        if self.settings_window:
            self.settings_window.status_changed()
        if self.onboarding:
            self.onboarding.status_changed()

    def _snapshot(self):
        """Debug: the panel as a PNG, then quit (for checking the UI without looking at the screen)."""
        path = self.debug.get("snapshot")
        window = self.debug.get("window", self.panel)
        width, height = window.get_width(), window.get_height()
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is not None:
            texture = window.get_native().get_renderer().render_texture(node, None)
            texture.save_to_png(path)
            log(f"snapshot: {path} ({width}×{height})")
        self.quit_app()
        return False


def main():
    os.environ.setdefault("GSK_RENDERER", "gl")
    app = App()
    return app.run(sys.argv)
