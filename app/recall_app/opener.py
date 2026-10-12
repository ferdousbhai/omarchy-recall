"""Opening a result where it matched: a PDF at its page with the
words searched for, a video or recording at its moment, code in your editor at its line, and showing a file in the
file manager.

Each app takes its own arguments for that, so the default app is used when it's one we know how to point, else a
known one that's installed, else the default app plainly.
"""

import os
import shutil
from dataclasses import dataclass

from gi.repository import Gio, GLib

from .log import log


@dataclass
class Editor:
    id: str
    name: str
    command: str
    args: callable   # (path, line) → arguments

    @property
    def installed(self):
        return shutil.which(self.command) is not None


EDITORS = [
    Editor("code", "Visual Studio Code", "code", lambda p, n: ["-g", f"{p}:{n}"]),
    Editor("codium", "VSCodium", "codium", lambda p, n: ["-g", f"{p}:{n}"]),
    Editor("cursor", "Cursor", "cursor", lambda p, n: ["-g", f"{p}:{n}"]),
    Editor("windsurf", "Windsurf", "windsurf", lambda p, n: ["-g", f"{p}:{n}"]),
    Editor("zed", "Zed", "zed", lambda p, n: [f"{p}:{n}"]),
    Editor("subl", "Sublime Text", "subl", lambda p, n: [f"{p}:{n}"]),
    Editor("pycharm", "PyCharm", "pycharm", lambda p, n: ["--line", str(n), p]),
    Editor("idea", "IntelliJ IDEA", "idea", lambda p, n: ["--line", str(n), p]),
    Editor("clion", "CLion", "clion", lambda p, n: ["--line", str(n), p]),
    Editor("goland", "GoLand", "goland", lambda p, n: ["--line", str(n), p]),
    Editor("webstorm", "WebStorm", "webstorm", lambda p, n: ["--line", str(n), p]),
    Editor("rustrover", "RustRover", "rustrover", lambda p, n: ["--line", str(n), p]),
    Editor("kate", "Kate", "kate", lambda p, n: ["-l", str(n), p]),
    Editor("gnome-text-editor", "Text Editor", "gnome-text-editor", lambda p, n: [f"+{n}", p]),
    Editor("gedit", "gedit", "gedit", lambda p, n: [f"+{n}", p]),
]


def installed_editors():
    return [editor for editor in EDITORS if editor.installed]


def chosen_editor(editor_id):
    """The setting; else the first editor installed; None means the file's default app."""
    if editor_id == "":
        return None
    by_id = {editor.id: editor for editor in EDITORS}
    if editor_id in by_id and by_id[editor_id].installed:
        return by_id[editor_id]
    installed = installed_editors()
    return installed[0] if installed else None


# Default apps we know how to point at a page (with words to find) or a moment, by their .desktop name.
PDF_VIEWERS = {
    "org.gnome.Evince": ("evince", lambda page, words: ["-i", str(page)] + (["-l", words] if words else [])),
    "org.gnome.Papers": ("papers", lambda page, words: ["-i", str(page)] + (["-l", words] if words else [])),
    "okularApplication_pdf": ("okular", lambda page, words: ["-p", str(page)] + (["--find", words] if words else [])),
    "org.kde.okular": ("okular", lambda page, words: ["-p", str(page)] + (["--find", words] if words else [])),
    "atril": ("atril", lambda page, words: ["-i", str(page)] + (["-l", words] if words else [])),
    "xreader": ("xreader", lambda page, words: ["-i", str(page)] + (["-l", words] if words else [])),
    "org.pwmt.zathura": ("zathura", lambda page, words: ["-P", str(page)] + (["-f", words] if words else [])),
}
PLAYERS = {
    "vlc": ("vlc", lambda seconds: [f"--start-time={seconds:.1f}"]),
    "org.videolan.VLC": ("vlc", lambda seconds: [f"--start-time={seconds:.1f}"]),
    "mpv": ("mpv", lambda seconds: [f"--start={seconds:.1f}"]),
    "io.github.celluloid_player.Celluloid": ("celluloid", lambda seconds: [f"--mpv-start={seconds:.1f}"]),
    "smplayer": ("smplayer", lambda seconds: ["-ss", f"{seconds:.0f}"]),
    "org.kde.haruna": ("haruna", lambda seconds: []),   # no start option; opens at the start
}


def default_app(path):
    content_type, _ = Gio.content_type_guess(path, None)
    return Gio.AppInfo.get_default_for_type(content_type, False)


def desktop_name(app):
    return (app.get_id() or "").removesuffix(".desktop") if app else ""


def spawn(argv):
    log("open: " + " ".join(argv))
    try:
        GLib.spawn_async(argv, flags=GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_DEV_NULL
                         | GLib.SpawnFlags.STDERR_TO_DEV_NULL)
        return True
    except GLib.Error as error:
        log(f"can't start {argv[0]}: {error.message}")
        return False


def open_default(path):
    try:
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
    except GLib.Error as error:
        log(f"can't open {path}: {error.message}")
        spawn(["xdg-open", path])


def open_title(row, editor_id=""):
    """The open action's name, as the panel's footer and buttons say it."""
    if row.code:
        editor = chosen_editor(editor_id)
        return f"Open in {editor.name}" if editor else "Open"
    if row.moment is not None and row.moment >= 1:
        name = desktop_name(default_app(row.path))
        if name in PLAYERS or any(shutil.which(command) for command, _ in PLAYERS.values()):
            return f"Open at {clock_text(row.moment)}"
    if row.page and row.page > 1:
        return f"Open at page {row.page}"
    return "Open"


def clock_text(seconds):
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def open_row(row, editor_id="", words=None):
    """Opens a result where it matched."""
    if row.code:
        editor = chosen_editor(editor_id)
        if editor and spawn([editor.command] + editor.args(row.path, row.code_line)):
            return
        return open_default(row.path)
    app = default_app(row.path)
    name = desktop_name(app)
    if row.page:
        viewer = PDF_VIEWERS.get(name)
        if viewer and shutil.which(viewer[0]):
            command, args = viewer
            if spawn([command] + args(row.page, words) + [row.path]):
                return
    if row.moment is not None and row.moment >= 1:
        player = PLAYERS.get(name) or next(((c, a) for c, a in PLAYERS.values() if shutil.which(c)), None)
        if player and shutil.which(player[0]):
            command, args = player
            if spawn([command] + args(row.moment) + [row.path]):
                return
    open_default(row.path)


def reveal(path):
    """Shows the file selected in the file manager (Dolphin, Files, Nemo… all answer FileManager1)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                      "org.freedesktop.FileManager1", "ShowItems",
                      GLib.Variant("(ass)", ([Gio.File.new_for_path(path).get_uri()], "")),
                      None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error as error:
        log(f"FileManager1 didn't answer ({error.message}): opening the folder")
        open_default(os.path.dirname(path))


def landmark(row):
    """A word to search for in the PDF viewer, so it lands on the words that matched: the first one found."""
    words = row.found_words
    return words[0] if words else None
