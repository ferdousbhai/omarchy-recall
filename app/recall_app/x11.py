"""What the panel needs from the X11 window manager that GTK 4 no longer asks for: to come to the front when the
shortcut opens it (KWin's focus-stealing prevention otherwise leaves it in the taskbar, waiting for a click), to stay
above other windows, and to stay out of the taskbar, as a launcher's window does. Nothing here runs on Wayland.
"""

import ctypes
import ctypes.util

from gi.repository import Gdk

_x = None
_display = None


class ClientMessage(ctypes.Structure):
    """XClientMessageEvent, with its data as five longs."""
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class Event(ctypes.Union):
    _fields_ = [("message", ClientMessage), ("pad", ctypes.c_long * 24)]


def _connect():
    global _x, _display
    if _display is None:
        _x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        _x.XOpenDisplay.restype = ctypes.c_void_p
        _x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        _x.XDefaultRootWindow.restype = ctypes.c_ulong
        _x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        _x.XInternAtom.restype = ctypes.c_ulong
        _x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        _x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(Event)]
        _x.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
        _x.XFlush.argtypes = [ctypes.c_void_p]
        _display = _x.XOpenDisplay(None)
    return _display


def is_x11():
    return "X11" in type(Gdk.Display.get_default()).__name__


def xid(window):
    from gi.repository import GdkX11
    surface = window.get_surface()
    return GdkX11.X11Surface.get_xid(surface) if surface else None


def _send(window_id, message, *data):
    display = _connect()
    if not display:
        return
    event = Event()
    event.message.type = 33   # ClientMessage
    event.message.send_event = 1
    event.message.window = window_id
    event.message.message_type = _x.XInternAtom(display, message.encode(), 0)
    event.message.format = 32
    for index, value in enumerate(data):
        event.message.data[index] = value
    # To the root window, as the window manager listens there (SubstructureRedirect | SubstructureNotify).
    _x.XSendEvent(display, _x.XDefaultRootWindow(display), 0, (1 << 20) | (1 << 19), ctypes.byref(event))
    _x.XFlush(display)


def atom(name):
    return _x.XInternAtom(_connect(), name.encode(), 0)


def launcher_window(window):
    """Above other windows, and not in the taskbar or the pager."""
    if not is_x11() or not window.get_surface():
        return
    from gi.repository import GdkX11
    surface = window.get_surface()
    GdkX11.X11Surface.set_skip_taskbar_hint(surface, True)
    GdkX11.X11Surface.set_skip_pager_hint(surface, True)
    window_id = xid(window)
    if window_id:
        _connect()
        _send(window_id, "_NET_WM_STATE", 1, atom("_NET_WM_STATE_ABOVE"), 0, 1)   # 1: add


def activate(window, timestamp=0):
    """Brings the window to the front with the focus. Source 2 says a pager or taskbar asks (the user did, through
    the shortcut), which window managers honor without focus-stealing checks."""
    if not is_x11():
        return
    window_id = xid(window)
    if window_id:
        _send(window_id, "_NET_ACTIVE_WINDOW", 2, timestamp, 0)


def place(window, width, top=0.16):
    """Centers the window across the monitor under the pointer, its top `top` of the way down, as a launcher sits:
    it grows downward from there, with room under it (the window manager would put it anywhere)."""
    if not is_x11():
        return
    window_id = xid(window)
    monitors = Gdk.Display.get_default().get_monitors()
    # The monitor with the pointer (GTK 4 doesn't say where the pointer is outside our windows; X does).
    pointer = _pointer()
    monitor = None
    for index in range(monitors.get_n_items()):
        candidate = monitors.get_item(index)
        area = candidate.get_geometry()
        if pointer and area.x <= pointer[0] < area.x + area.width and area.y <= pointer[1] < area.y + area.height:
            monitor = candidate
            break
    monitor = monitor or (monitors.get_item(0) if monitors.get_n_items() else None)
    if not window_id or monitor is None:
        return
    area = monitor.get_geometry()
    display_x = _connect()
    _x.XMoveWindow(display_x, window_id, area.x + (area.width - width) // 2, area.y + int(area.height * top))
    _x.XFlush(display_x)


def _pointer():
    """Where the pointer is on the screen, from X."""
    display = _connect()
    if not display:
        return None
    root, child = ctypes.c_ulong(), ctypes.c_ulong()
    root_x, root_y, win_x, win_y = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    mask = ctypes.c_uint()
    _x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    _x.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
    found = _x.XQueryPointer(display, _x.XDefaultRootWindow(display), ctypes.byref(root), ctypes.byref(child),
                             ctypes.byref(root_x), ctypes.byref(root_y), ctypes.byref(win_x), ctypes.byref(win_y),
                             ctypes.byref(mask))
    return (root_x.value, root_y.value) if found else None
