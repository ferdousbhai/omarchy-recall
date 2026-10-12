"""Small shared pieces of the windows: durations and sizes in words, folder pickers, start at login."""

import os
from pathlib import Path

from gi.repository import Gio, GLib, Gtk

from . import APP_ID, paths

KIND_WORDS = {"screenshot": ("screenshot", "screenshots"), "image": ("image", "images"), "pdf": ("PDF", "PDFs"),
              "doc": ("document", "documents"), "audio": ("recording", "recordings"), "video": ("video", "videos"),
              "code": ("file", "files")}


def rough_duration(seconds):
    if seconds < 1:
        return "< 1 s"
    if seconds < 90:
        return f"{round(seconds)} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def rough_bytes(size):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1000:
            return f"{size:.0f} {unit}" if unit in ("bytes", "KB") else f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1000
    return f"{size:.1f} PB"


def kind_counts(kinds):
    """"136 screenshots, 104 images, 12 PDFs"."""
    parts = []
    for kind in ("screenshot", "image", "pdf", "doc", "video", "audio", "code"):
        count = kinds.get(kind, 0)
        if count:
            singular, plural = KIND_WORDS[kind]
            parts.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(parts)


def pick_folders(parent, title, on_picked, accept="Choose"):
    """The folder picker (several at once); `on_picked([paths])`."""
    dialog = Gtk.FileDialog(title=title, accept_label=accept, modal=True)
    dialog.set_initial_folder(Gio.File.new_for_path(str(paths.HOME)))

    def done(dialog, result):
        try:
            files = dialog.select_multiple_folders_finish(result)
        except GLib.Error:
            return
        picked = [files.get_item(i).get_path() for i in range(files.get_n_items())]
        on_picked([p for p in picked if p])
    dialog.select_multiple_folders(parent, None, done)


def size_of(path):
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(str(path) + suffix)
        except OSError:
            pass
    return total


# MARK: Start at login

AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME") or paths.HOME / ".config") / "autostart" / f"{APP_ID}.desktop"


def launcher():
    """How to start this copy of the app."""
    return str(Path(__file__).resolve().parents[1] / "recall-app")


def starts_at_login():
    return AUTOSTART.exists()


def set_starts_at_login(on):
    if on:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text("[Desktop Entry]\nType=Application\nName=Recall\nComment=Search your files by what's "
                             f"in them\nExec={launcher()} --background\nIcon={APP_ID}\nTerminal=false\n"
                             "X-GNOME-Autostart-enabled=true\nNoDisplay=false\n")
    else:
        AUTOSTART.unlink(missing_ok=True)


def user_folders():
    """The usual folders to search, as this desktop names them (XDG user directories)."""
    folders = []
    for directory in (GLib.UserDirectory.DIRECTORY_DESKTOP, GLib.UserDirectory.DIRECTORY_DOCUMENTS,
                      GLib.UserDirectory.DIRECTORY_DOWNLOAD, GLib.UserDirectory.DIRECTORY_PICTURES,
                      GLib.UserDirectory.DIRECTORY_VIDEOS, GLib.UserDirectory.DIRECTORY_MUSIC):
        path = GLib.get_user_special_dir(directory)
        if path and os.path.isdir(path) and os.path.realpath(path) != str(paths.HOME) and path not in folders:
            folders.append(path)
    # A desktop that names none of them: the usual names.
    for name in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"):
        path = str(paths.HOME / name)
        if os.path.isdir(path) and path not in folders:
            folders.append(path)
    return folders


def code_folders():
    """Folders under home that are a git repo or hold some."""
    skip = {"Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music", "Public", "Templates", "snap"}
    found = []
    try:
        names = sorted(os.listdir(paths.HOME))
    except OSError:
        return found
    for name in names:
        folder = paths.HOME / name
        if name.startswith(".") or name in skip or not folder.is_dir():
            continue
        try:
            if (folder / ".git").exists() or any((folder / child / ".git").exists() for child in os.listdir(folder)
                                                 if not child.startswith(".")):
                found.append(str(folder))
        except OSError:
            continue
    return found
