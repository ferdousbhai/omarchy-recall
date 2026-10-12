"""When a folder's first pass may run. New files always go in right away; the long
first pass waits while the laptop saves power (power-profiles-daemon's "power-saver"), and on
battery when "Index on battery power" is off. Both come from D-Bus as they change: UPower and power-profiles-daemon.
"""

from gi.repository import Gio, GLib

from .log import log


class PowerPolicy:
    def __init__(self, settings, on_change):
        self.settings = settings
        self.on_change = on_change
        self.on_battery = False
        self.power_saver = False
        self.upower = self._proxy("org.freedesktop.UPower", "/org/freedesktop/UPower", "org.freedesktop.UPower")
        self.profiles = self._proxy("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles",
                                    "org.freedesktop.UPower.PowerProfiles") \
            or self._proxy("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles")
        self._read()

    def _proxy(self, name, path, interface):
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None, name, path,
                                                   interface, None)
        except GLib.Error:
            return None
        if proxy.get_name_owner() is None:
            return None
        proxy.connect("g-properties-changed", lambda *_: self._read(notify=True))
        return proxy

    def _read(self, notify=False):
        before = self.blocker
        battery = self.upower.get_cached_property("OnBattery") if self.upower else None
        self.on_battery = bool(battery.unpack()) if battery is not None else False
        profile = self.profiles.get_cached_property("ActiveProfile") if self.profiles else None
        self.power_saver = profile is not None and profile.unpack() == "power-saver"
        if notify and self.blocker != before:
            log(f"power: first pass {'waits: ' + self.blocker if self.blocker else 'may run'}")
            self.on_change()

    @property
    def blocker(self):
        """Why the first pass waits now, or None."""
        if getattr(self, "power_saver", False):
            return "power saver"
        if getattr(self, "on_battery", False) and not self.settings.index_on_battery:
            return "on battery"
        return None

    def settings_changed(self):
        self.on_change()
