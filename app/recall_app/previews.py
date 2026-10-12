"""Pictures for results: a photo or screenshot itself, the video
frame at the moment that matched, the PDF page that matched. Made off the main thread (ffmpeg, pdftoppm), kept on
disk in ~/.cache/recall/previews and in memory while a window is open."""

import hashlib
import os
import subprocess
import threading
from collections import OrderedDict

from gi.repository import Gdk, GdkPixbuf, GLib

from . import paths

FOLDER = paths.CACHE / "previews"
_memory = OrderedDict()      # key → Gdk.Texture
_waiting = {}                # key → [callbacks]
_limit = 120


def clear():
    _memory.clear()


def _key(path, loc, side):
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0
    return hashlib.sha1(f"{path}|{mtime}|{loc}|{side}".encode()).hexdigest()


def picture(row, side, callback):
    """Calls `callback(texture or None)` on the main thread: at once when it's cached, else when it's made."""
    loc = row.moment if row.kind == "video" else row.page if row.kind == "pdf" else None
    if row.kind not in ("image", "screenshot", "video", "pdf"):
        return callback(None)
    key = _key(row.path, loc, side)
    if key in _memory:
        _memory.move_to_end(key)
        return callback(_memory[key])
    if key in _waiting:
        _waiting[key].append(callback)
        return None
    _waiting[key] = [callback]
    threading.Thread(target=_make, args=(row.kind, row.path, loc, side, key), daemon=True).start()
    return None


def _make(kind, path, loc, side, key):
    texture = None
    try:
        pixbuf = None
        if kind in ("image", "screenshot"):
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, side, side, True)
            pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
        else:
            FOLDER.mkdir(parents=True, exist_ok=True)
            out = FOLDER / f"{key}.png"
            if not out.exists():
                if kind == "video":
                    seconds = max(0.0, (loc or 0.0))
                    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{seconds:.2f}", "-i", path,
                               "-frames:v", "1", "-vf", f"scale=w='min({side},iw)':h='min({side},ih)'"
                               ":force_original_aspect_ratio=decrease", str(out)]
                else:
                    page = str(int(loc or 1))
                    command = ["pdftoppm", "-f", page, "-l", page, "-png", "-scale-to", str(side), "-singlefile",
                               path, str(out.with_suffix(""))]
                subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30, check=False)
            if out.exists():
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(out), side, side, True)
        if pixbuf:
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
    except (GLib.Error, OSError, subprocess.SubprocessError):
        texture = None
    GLib.idle_add(_done, key, texture)


def _done(key, texture):
    if texture:
        _memory[key] = texture
        while len(_memory) > _limit:
            _memory.popitem(last=False)
    for callback in _waiting.pop(key, []):
        callback(texture)
    return False


def file_icon(path):
    """The icon name of a file's type, for rows without a picture."""
    from gi.repository import Gio
    content_type, _ = Gio.content_type_guess(path, None)
    icon = Gio.content_type_get_icon(content_type)
    names = icon.get_names() if hasattr(icon, "get_names") else []
    return names[0] if names else "text-x-generic"


def run(work, callback):
    """`work()` on a worker thread, then `callback(result)` on the main thread (None if it failed)."""
    def thread():
        try:
            result = work()
        except Exception as error:  # noqa: BLE001 - a preview that can't be made is just not shown
            from .log import log
            log(f"preview: {error!r}")
            result = None
        GLib.idle_add(lambda: callback(result) and False)
    threading.Thread(target=thread, daemon=True).start()
