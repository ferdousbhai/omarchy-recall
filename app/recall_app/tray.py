"""The tray icon: a StatusNotifierItem with its DBusMenu, spoken over D-Bus directly.
KDE Plasma shows these natively; GNOME with the AppIndicator extension (Ubuntu's default) does too.

The icon is a magnifier over a document's lines, drawn as a ring for indexing progress and with two bars when
indexing is paused. Clicking it opens the search panel; its menu has the status and the rest.
"""

import math
import os
import sys

import cairo
from gi.repository import Adw, Gio, GLib

from .log import log

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ia{sv})" name="removedProps"/>
    </signal>
  </interface>
</node>
"""

ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"


def draw_icon(side, progress=None, paused=False, dark_panel=True):
    """The icon as ARGB32 in network byte order (what StatusNotifierItem wants), `side` pixels square: a magnifier over
    a page's lines. While indexing, the lens is a ring that fills clockwise from the top; paused, two bars replace the
    lines."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, side, side)
    c = cairo.Context(surface)
    c.scale(side / 64, side / 64)
    color = (0.93, 0.93, 0.93) if dark_panel else (0.13, 0.13, 0.13)
    cx, cy, radius = 27, 27, 19
    c.set_line_cap(cairo.LINE_CAP_ROUND)
    # The handle.
    c.set_source_rgb(*color)
    c.set_line_width(8)
    c.move_to(cx + radius * 0.72, cy + radius * 0.72)
    c.line_to(57, 57)
    c.stroke()
    # The lens: whole, or faint with the done part of it.
    c.set_line_width(5.5)
    if progress is None:
        c.arc(cx, cy, radius, 0, 2 * math.pi)
        c.stroke()
    else:
        c.set_source_rgba(*color, 0.3)
        c.arc(cx, cy, radius, 0, 2 * math.pi)
        c.stroke()
        c.set_source_rgb(*color)
        c.arc(cx, cy, radius, -math.pi / 2, -math.pi / 2 + 2 * math.pi * max(progress, 0.03))
        c.stroke()
    # Inside: a page's lines, or the pause bars.
    c.set_source_rgb(*color)
    if paused:
        c.rectangle(cx - 7, cy - 8, 4.5, 16)
        c.rectangle(cx + 2.5, cy - 8, 4.5, 16)
        c.fill()
    else:
        c.set_line_width(3.6)
        for y, width in ((cy - 7, 18), (cy, 13), (cy + 7, 16)):
            c.move_to(cx - 9, y)
            c.line_to(cx - 9 + width, y)
        c.stroke()
    surface.flush()
    data = bytearray(surface.get_data())
    # Cairo's ARGB32 is native-endian (BGRA on x86); the spec wants ARGB, big-endian.
    if sys.byteorder == "little":
        for i in range(0, len(data), 4):
            data[i:i + 4] = data[i:i + 4][::-1]
    return side, side, bytes(data)


class Tray:
    def __init__(self, app):
        self.app = app
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.revision = 1
        self.items = {}       # id → (properties, action)
        self.layout = []      # ids in order
        self.icon_state = None
        item = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.bus.register_object(ITEM_PATH, item, self._item_call, self._item_property, None)
        self.bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_property, None)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        # Register now, and again whenever a tray (the watcher) comes up later, as when the panel restarts.
        Gio.bus_watch_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher", Gio.BusNameWatcherFlags.NONE,
                                         self._register, None)
        self._build_menu()

    def _register(self, *_):
        try:
            self.bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                               "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                               GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, 3000, None)
            log("tray icon registered")
        except GLib.Error as error:
            log(f"no tray to show the icon in: {error.message}")

    # MARK: The item

    def _item_property(self, connection, sender, path, interface, name):
        status = self.app.engine.status
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", "recall")
        if name == "Title":
            return GLib.Variant("s", "Recall")
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "WindowId":
            return GLib.Variant("i", 0)
        if name in ("IconName", "OverlayIconName", "AttentionIconName"):
            return GLib.Variant("s", "")
        if name == "IconPixmap":
            dark = Adw.StyleManager.get_default().get_dark()
            progress, paused = self._progress(), status.paused
            return GLib.Variant("a(iiay)", [draw_icon(side, progress, paused, dark) for side in (22, 32, 44, 64)])
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", ("", [], "Recall", self._status_line()))
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        return None

    def _item_call(self, connection, sender, path, interface, method, parameters, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: self.app.toggle_panel() and False)
        invocation.return_value(None)

    def _progress(self):
        download = self.app.download
        if download and download.active:
            return download.progress
        return self.app.engine.status.progress

    def _status_line(self):
        download = self.app.download
        if download and download.line:
            return download.line
        return self.app.engine.status.line(bool(self.app.settings.roots or self.app.settings.code_roots))

    def update(self):
        """The status changed: a new icon when it looks different (2% steps), the tooltip and the menu."""
        progress = self._progress()
        state = (None if progress is None else round(progress * 50), self.app.engine.status.paused)
        if state != self.icon_state:
            self.icon_state = state
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon")
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip")
        self._build_menu()

    def _emit(self, path, interface, signal, parameters=None):
        try:
            self.bus.emit_signal(None, path, interface, signal, parameters)
        except GLib.Error:
            pass

    # MARK: The menu

    def _build_menu(self):
        app = self.app
        status = app.engine.status
        has_folders = bool(app.settings.roots or app.settings.code_roots)
        entries = [("status", {"label": self._status_line(), "enabled": False}, None),
                   ("sep1", {"type": "separator"}, None)]
        if not has_folders:
            entries.append(("choose", {"label": "Choose Folders to Search…"}, app.choose_folders))
        entries.append(("search", {"label": "Search…"}, app.panel.show_panel))
        if has_folders:
            entries.append(("pause", {"label": "Resume Indexing" if status.paused else "Pause Indexing"},
                            lambda: app.set_paused(not app.engine.status.paused)))
        download = app.download
        if download and download.needs_start:
            entries.append(("download", {"label": "Download the Model"}, download.start))
        entries += [("sep2", {"type": "separator"}, None),
                    ("settings", {"label": "Settings…"}, app.show_settings),
                    ("quit", {"label": "Quit Recall"}, app.quit_app)]
        items, layout = {}, []
        for index, (key, properties, action) in enumerate(entries, start=1):
            items[index] = (properties, action)
            layout.append(index)
        changed = [(i, p) for i, (p, _) in items.items()] != [(i, p) for i, (p, _) in self.items.items()]
        self.items, self.layout = items, layout
        if changed:
            self.revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))

    @staticmethod
    def _variants(properties):
        out = {}
        for key, value in properties.items():
            kind = "b" if isinstance(value, bool) else "s"
            out[key] = GLib.Variant(kind, value)
        return out

    def _node(self, item_id):
        properties = self._variants(self.items[item_id][0]) if item_id else {"children-display": GLib.Variant("s", "submenu")}
        children = [GLib.Variant("(ia{sv}av)", (i, self._variants(self.items[i][0]), [])) for i in self.layout] \
            if item_id == 0 else []
        return (item_id, properties, children)

    def _menu_property(self, connection, sender, path, interface, name):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(name)

    def _menu_call(self, connection, sender, path, interface, method, parameters, invocation):
        if method == "GetLayout":
            parent = parameters.unpack()[0]
            node = self._node(parent if parent in self.items else 0)
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, node)))
        elif method == "GetGroupProperties":
            ids = parameters.unpack()[0] or list(self.items)
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (
                [(i, self._variants(self.items[i][0])) for i in ids if i in self.items],)))
        elif method == "GetProperty":
            item_id, name = parameters.unpack()
            value = self.items.get(item_id, ({}, None))[0].get(name, "")
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("b" if isinstance(value, bool) else "s", value),)))
        elif method == "Event":
            item_id, event = parameters.unpack()[:2]
            self._clicked(item_id, event)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _, _ in parameters.unpack()[0]:
                self._clicked(item_id, event)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            self._build_menu()
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            self._build_menu()
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _clicked(self, item_id, event):
        if event != "clicked" or item_id not in self.items:
            return
        action = self.items[item_id][1]
        if action:
            GLib.idle_add(lambda: action() and False)
