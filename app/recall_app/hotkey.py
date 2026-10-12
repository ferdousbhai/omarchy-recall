"""The global shortcut.

First through the desktop portal's GlobalShortcuts (Wayland, KDE Plasma 6, GNOME 48+): the desktop may ask once
whether Recall may have it, and it can be changed in the desktop's own shortcut settings. Where the portal binds no key
(Plasma 5.27 takes the request but leaves the key unset) and the session is X11, Recall grabs the key from the X
server itself, as X11 apps always have. Elsewhere, bind `recall-app --toggle` to a key in the desktop's settings.
"""

import ctypes
import ctypes.util
import os
import re

from gi.repository import Gdk, Gio, GLib

from .log import log

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
SHORTCUT_ID = "toggle-panel"


def portal_trigger(accelerator):
    """GTK's "<Shift><Super>space" as the portal writes it: "SHIFT+LOGO+space"."""
    names = {"shift": "SHIFT", "control": "CTRL", "primary": "CTRL", "ctrl": "CTRL", "alt": "ALT", "super": "LOGO",
             "meta": "LOGO"}
    modifiers = [names.get(m.lower(), m.upper()) for m in re.findall(r"<(\w+)>", accelerator)]
    key = re.sub(r"<\w+>", "", accelerator)
    return "+".join(modifiers + [key])


def display(accelerator):
    """For people: "Shift+Super+Space"."""
    names = {"shift": "Shift", "control": "Ctrl", "primary": "Ctrl", "alt": "Alt", "super": "Super", "meta": "Meta"}
    modifiers = [names.get(m.lower(), m) for m in re.findall(r"<(\w+)>", accelerator)]
    key = re.sub(r"<\w+>", "", accelerator)
    return "+".join(modifiers + [key[:1].upper() + key[1:]])


class Hotkey:
    def __init__(self, app, accelerator, on_press):
        self.app = app
        self.on_press = on_press
        self.accelerator = accelerator
        self.session = None
        self.bound = None   # what the shortcut is, once bound
        self.x11 = None
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.token = 0
        self.bus.signal_subscribe(PORTAL, SHORTCUTS, "Activated", PORTAL_PATH, None, Gio.DBusSignalFlags.NONE,
                                  self._activated)
        self._create_session()

    def _request(self, method, parameters, on_response):
        """Calls a portal method that answers through a Request object's Response signal."""
        self.token += 1
        token = f"recall{os.getpid()}_{self.token}"
        sender = self.bus.get_unique_name().lstrip(":").replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        def response(connection, sender_name, path, interface, signal, params):
            self.bus.signal_unsubscribe(subscription)
            code, results = params.unpack()
            on_response(code, results)
        subscription = self.bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response",
                                                 request_path, None, Gio.DBusSignalFlags.NONE, response)
        options = parameters[-1]
        options["handle_token"] = GLib.Variant("s", token)
        self.bus.call(PORTAL, PORTAL_PATH, SHORTCUTS, method, GLib.Variant(self._signature(method), tuple(parameters)),
                      None, Gio.DBusCallFlags.NONE, -1, None, self._called, method)

    @staticmethod
    def _signature(method):
        return {"CreateSession": "(a{sv})", "BindShortcuts": "(oa(sa{sv})sa{sv})"}[method]

    def _called(self, bus, result, method):
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            log(f"global shortcut: {method} failed: {error.message}")

    def _create_session(self):
        self._request("CreateSession", [{"session_handle_token": GLib.Variant("s", f"recall{os.getpid()}")}],
                      self._session_created)

    def _session_created(self, code, results):
        if code != 0:
            log(f"global shortcut: no session (response {code})")
            return
        self.session = results["session_handle"]
        self.bind(self.accelerator)

    def bind(self, accelerator):
        """Asks for `accelerator` (the desktop may ask you first, or keep the one you set there)."""
        self.accelerator = accelerator
        if self.x11:
            return self._grab_x11()
        if not self.session:
            return
        shortcut = (SHORTCUT_ID, {"description": GLib.Variant("s", "Open the Recall search panel"),
                                  "preferred_trigger": GLib.Variant("s", portal_trigger(accelerator))})
        self._request("BindShortcuts", [self.session, [shortcut], "", {}], self._bound)

    def _bound(self, code, results):
        if code == 0:
            for shortcut_id, properties in results.get("shortcuts", []):
                if shortcut_id == SHORTCUT_ID and properties.get("trigger_description"):
                    self.bound = properties["trigger_description"]
        if self.bound:
            log(f"global shortcut: {self.bound} (desktop portal)")
            return
        self._grab_x11()

    def _grab_x11(self):
        if self.x11 is None and "X11" in type(Gdk.Display.get_default()).__name__:
            try:
                self.x11 = X11Grab(self.on_press)
            except OSError as error:
                log(f"global shortcut: no X11 grab: {error}")
                return
        if self.x11 is None:
            log("global shortcut: the desktop set none; bind `recall-app --toggle` to a key in its settings")
            return
        if self.x11.grab(self.accelerator):
            self.bound = display(self.accelerator)
            log(f"global shortcut: {self.bound} (X11)")
        else:
            log(f"global shortcut: {display(self.accelerator)} is taken by another app")

    def _activated(self, connection, sender, path, interface, signal, parameters):
        session, shortcut_id = parameters.unpack()[:2]
        if session == self.session and shortcut_id == SHORTCUT_ID:
            self.on_press(parameters.unpack()[2] & 0xFFFFFFFF)


class X11Grab:
    """A key grabbed on the X server's root window: its presses come to us whichever window has the focus."""

    SHIFT, LOCK, CONTROL, MOD1, MOD2, MOD4 = 1, 2, 4, 8, 16, 64   # Mod1 is Alt, Mod2 Num Lock, Mod4 Super
    KEY_PRESS = 2
    MODIFIERS = {"shift": SHIFT, "control": CONTROL, "primary": CONTROL, "ctrl": CONTROL, "alt": MOD1,
                 "super": MOD4, "meta": MOD4}

    def __init__(self, on_press):
        self.on_press = on_press
        self.x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        for name, args, result in (("XDefaultRootWindow", [ctypes.c_void_p], ctypes.c_ulong),
                                   ("XStringToKeysym", [ctypes.c_char_p], ctypes.c_ulong),
                                   ("XKeysymToKeycode", [ctypes.c_void_p, ctypes.c_ulong], ctypes.c_ubyte),
                                   ("XGrabKey", [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
                                                 ctypes.c_int, ctypes.c_int, ctypes.c_int], ctypes.c_int),
                                   ("XUngrabKey", [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong],
                                    ctypes.c_int),
                                   ("XSync", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
                                   ("XPending", [ctypes.c_void_p], ctypes.c_int),
                                   ("XNextEvent", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
                                   ("XConnectionNumber", [ctypes.c_void_p], ctypes.c_int)):
            function = getattr(self.x, name)
            function.argtypes, function.restype = args, result
        # Our own connection, read on the main loop.
        self.display = self.x.XOpenDisplay(None)
        if not self.display:
            raise OSError("can't open the X display")
        self.root = self.x.XDefaultRootWindow(self.display)
        self.failed = False
        handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def on_error(_display, _event):
            self.failed = True   # BadAccess: someone else has that key
            return 0
        self.error_handler = handler(on_error)   # kept alive for as long as X may call it
        self.x.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        self.x.XSetErrorHandler.restype = ctypes.c_void_p
        self.grabbed = None
        self.event = ctypes.create_string_buffer(192)   # an XEvent
        GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, self.x.XConnectionNumber(self.display), GLib.IOCondition.IN,
                              self._readable)

    def _variants(self, modifiers):
        # The same key with Caps Lock or Num Lock on is a different grab to X.
        return [modifiers | extra for extra in (0, self.LOCK, self.MOD2, self.LOCK | self.MOD2)]

    def grab(self, accelerator):
        self.ungrab()
        modifiers = 0
        for name in re.findall(r"<(\w+)>", accelerator):
            modifiers |= self.MODIFIERS.get(name.lower(), 0)
        key = re.sub(r"<\w+>", "", accelerator)
        keycode = self.x.XKeysymToKeycode(self.display, self.x.XStringToKeysym(key.encode()))
        if not keycode:
            return False
        self.failed = False
        # Our error handler only while grabbing: the handler is process-wide, and GTK's X connection has its own.
        previous = self.x.XSetErrorHandler(ctypes.cast(self.error_handler, ctypes.c_void_p))
        for variant in self._variants(modifiers):
            self.x.XGrabKey(self.display, keycode, variant, self.root, 1, 1, 1)   # GrabModeAsync
        self.x.XSync(self.display, 0)
        self.x.XSetErrorHandler(previous)
        if self.failed:
            self.grabbed = (keycode, modifiers)
            self.ungrab()
            return False
        self.grabbed = (keycode, modifiers)
        return True

    def ungrab(self):
        if self.grabbed:
            keycode, modifiers = self.grabbed
            for variant in self._variants(modifiers):
                self.x.XUngrabKey(self.display, keycode, variant, self.root)
            self.x.XSync(self.display, 0)
            self.grabbed = None

    def _readable(self, *_):
        while self.x.XPending(self.display):
            self.x.XNextEvent(self.display, self.event)
            if ctypes.c_int.from_buffer(self.event).value == self.KEY_PRESS:
                # XKeyEvent's time (after type, serial, send_event, display, window, root, subwindow): the window
                # manager brings a window to the front for a key press it can date.
                timestamp = ctypes.c_ulong.from_buffer(self.event, 56).value
                GLib.idle_add(lambda: self.on_press(timestamp) and False)
        return True
