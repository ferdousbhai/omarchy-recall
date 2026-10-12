"""Folder watching through inotify: one watch per folder
under the chosen ones, read on the main loop. A change asks for a sync once things go quiet for a moment.

Hidden folders and the folders the crawler never walks (node_modules, venv, build…) aren't watched, as their changes
never matter. When the kernel's watch limit runs out, the rest of the tree is synced every few minutes instead.
"""

import ctypes
import ctypes.util
import os
import struct

from gi.repository import GLib

from .log import log

IN_MODIFY, IN_ATTRIB, IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = \
    0x2, 0x4, 0x8, 0x40, 0x80, 0x100, 0x200
IN_DELETE_SELF, IN_MOVE_SELF, IN_ISDIR, IN_Q_OVERFLOW, IN_IGNORED = 0x400, 0x800, 0x40000000, 0x4000, 0x8000
IN_ONLYDIR, IN_NONBLOCK, IN_CLOEXEC = 0x01000000, 0o4000, 0o2000000
MASK = IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF \
    | IN_ATTRIB | IN_ONLYDIR

# The crawler's own skips (Rules.skippedFolderNames and codeSkippedFolderNames), and more noise of the same kind.
NOISE = {"node_modules", "bower_components", "venv", "site-packages", "__pycache__", "DerivedData", "Pods",
         "Carthage", "vendor", "build", "dist", "target", "out", ".build", "Vendor", "third_party"}


class Watcher:
    """Watches `roots`; `on_change()` once changes settle for `latency` seconds, at most every `longest` seconds."""

    def __init__(self, roots, on_change, latency=0.75, longest=None, name="folders"):
        self.on_change = on_change
        self.latency = latency
        self.longest = longest
        self.name = name
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.watches = {}   # descriptor → folder
        self.full = False   # the watch limit ran out
        self.timer = None
        self.first_change = None
        self.source = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT_IDLE, self.fd, GLib.IOCondition.IN, self._read)
        for root in roots:
            self._add_tree(root)
        log(f"watching {len(self.watches)} {self.name}" + (" (the watch limit ran out: some are synced every "
                                                            "5 min instead)" if self.full else ""))
        self.poll = GLib.timeout_add_seconds(300, self._poll) if self.full else None

    def close(self):
        for source in (self.source, self.timer, self.poll):
            if source:
                GLib.source_remove(source)
        self.source = self.timer = self.poll = None
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _add_tree(self, top):
        for folder, subfolders, _ in os.walk(top, onerror=lambda _: None):
            subfolders[:] = [name for name in subfolders if not name.startswith(".") and name not in NOISE
                             and not name.endswith(".noindex")]
            if not self._add(folder):
                subfolders[:] = []
                if self.full:
                    return

    def _add(self, folder):
        descriptor = self.libc.inotify_add_watch(self.fd, os.fsencode(folder), MASK)
        if descriptor < 0:
            if ctypes.get_errno() == 28:   # ENOSPC: max_user_watches reached
                self.full = True
            return False
        self.watches[descriptor] = folder
        return True

    def _read(self, *_):
        try:
            data = os.read(self.fd, 64 * 1024)
        except BlockingIOError:
            return True
        except OSError:
            return False
        offset, relevant = 0, False
        while offset + 16 <= len(data):
            descriptor, mask, _, length = struct.unpack_from("iIII", data, offset)
            name = data[offset + 16:offset + 16 + length].split(b"\0", 1)[0].decode(errors="replace")
            offset += 16 + length
            if mask & IN_Q_OVERFLOW:
                relevant = True
                continue
            if mask & IN_IGNORED:
                self.watches.pop(descriptor, None)
                continue
            if name.startswith("."):
                continue   # dotfiles and editors' temporary files
            folder = self.watches.get(descriptor)
            if folder is None:
                continue
            relevant = True
            # A new folder (made, or moved in) is watched too.
            if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO) and name not in NOISE:
                self._add_tree(os.path.join(folder, name))
        if relevant:
            self._changed()
        return True

    def _changed(self):
        now = GLib.get_monotonic_time() / 1e6
        if self.first_change is None:
            self.first_change = now
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        # Busy folders (code being edited) still sync now and then.
        wait = self.latency
        if self.longest is not None:
            wait = min(wait, max(0.0, self.first_change + self.longest - now))
        self.timer = GLib.timeout_add(int(wait * 1000), self._fire)

    def _fire(self):
        self.timer = None
        self.first_change = None
        self.on_change()
        return False

    def _poll(self):
        self.on_change()
        return True
