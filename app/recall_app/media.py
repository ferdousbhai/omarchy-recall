"""Playing a result where it matched, in the preview: a video shows
the frame of the moment and plays from there; a recording shows its waveform. Under both, the timeline marks the
stretch that matched, with the playhead; a click on it seeks.

Playback goes through GTK's media support (GStreamer).
"""

import json
import subprocess
import threading
from array import array

from gi.repository import GLib, Gtk

from . import paths
from .highlight import _key, _mtime
from .results import clock

AMBER = (0.949, 0.635, 0.165)


def peaks(path, bins=240):
    """The loudness of `bins` stretches of a file's sound, 0…1 (cached: a long recording takes a moment)."""
    folder = paths.CACHE / "waveforms"
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / f"{_key(path, _mtime(path), bins)}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "1", "-ar",
                             "4000", "-f", "s16le", "pipe:1"], capture_output=True, timeout=120, check=False)
    samples = array("h")
    samples.frombytes(result.stdout[:len(result.stdout) // 2 * 2])
    if not samples:
        return []
    size = max(1, len(samples) // bins)
    values = [max((abs(v) for v in samples[i:i + size]), default=0) / 32768 for i in range(0, len(samples), size)]
    top = max(values) or 1
    values = [round(v / top, 3) for v in values[:bins]]
    cache.write_text(json.dumps(values))
    return values


class Timeline(Gtk.DrawingArea):
    """The whole file, the stretch that matched in amber, and the playhead. With `waveform`, as bars of loudness."""

    def __init__(self, on_seek, height=26):
        super().__init__(content_height=height, hexpand=True)
        self.duration = 0.0
        self.start = self.end = None
        self.playhead = None
        self.waveform = []
        self.on_seek = on_seek
        self.set_draw_func(self._draw)
        click = Gtk.GestureClick()
        click.connect("pressed", self._clicked)
        self.add_controller(click)
        self.set_cursor_from_name("pointer")

    def show(self, duration, start, end, waveform=None):
        self.duration, self.start, self.end = duration or 0.0, start, end
        self.waveform = waveform or []
        self.playhead = None
        self.queue_draw()

    def set_playhead(self, seconds):
        self.playhead = seconds
        self.queue_draw()

    def _x(self, seconds, width):
        return 0 if not self.duration else max(0, min(width, width * seconds / self.duration))

    def _draw(self, area, cr, width, height):
        color = area.get_color()
        fg = (color.red, color.green, color.blue)
        if self.waveform:
            count = len(self.waveform)
            step = width / count
            for index, value in enumerate(self.waveform):
                t = self.duration * (index + 0.5) / count
                inside = self.start is not None and self.start <= t <= (self.end or self.start)
                cr.set_source_rgba(*(AMBER if inside else fg), 1.0 if inside else 0.35)
                bar = max(2, value * (height - 4))
                cr.rectangle(index * step + step * 0.15, (height - bar) / 2, step * 0.7, bar)
                cr.fill()
        else:
            cr.set_source_rgba(*fg, 0.18)
            cr.rectangle(0, height / 2 - 3, width, 6)
            cr.fill()
            if self.start is not None:
                x0 = self._x(self.start, width)
                x1 = max(x0 + 4, self._x(self.end if self.end is not None else self.start + 3, width))
                cr.set_source_rgb(*AMBER)
                cr.rectangle(x0, height / 2 - 5, x1 - x0, 10)
                cr.fill()
        if self.playhead is not None:
            x = self._x(self.playhead, width)
            cr.set_source_rgb(*fg)
            cr.rectangle(x - 1, 0, 2, height)
            cr.fill()

    def _clicked(self, gesture, _presses, x, _y):
        if self.duration:
            self.on_seek(self.duration * x / max(1, self.get_width()))


class MediaPreview(Gtk.Box):
    """A video's frame (then the video itself) or a recording's waveform, the timeline, and play/pause."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("recall-media")
        self.row = None
        self.stream = None
        self.overlay = Gtk.Overlay()
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.add_css_class("recall-picture")
        self.picture.set_size_request(-1, 230)
        self.overlay.set_child(self.picture)
        self.big_play = Gtk.Button(icon_name="media-playback-start-symbolic", halign=Gtk.Align.CENTER,
                                   valign=Gtk.Align.CENTER)
        self.big_play.add_css_class("osd")
        self.big_play.add_css_class("circular")
        self.big_play.set_size_request(56, 56)
        self.big_play.connect("clicked", lambda _: self.toggle())
        self.overlay.add_overlay(self.big_play)
        self.badge = Gtk.Label(halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=8, margin_bottom=8)
        self.badge.add_css_class("recall-badge-osd")
        self.overlay.add_overlay(self.badge)
        self.append(self.overlay)

        controls = Gtk.Box(spacing=8)
        self.play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER)
        self.play.add_css_class("flat")
        self.play.connect("clicked", lambda _: self.toggle())
        controls.append(self.play)
        self.timeline = Timeline(self.seek)
        controls.append(self.timeline)
        self.append(controls)
        self.time = Gtk.Label(xalign=0)
        self.time.add_css_class("recall-row-sub")
        self.append(self.time)

    def show(self, row, frame=None):
        self.stop()
        self.row = row
        duration = float(row.info.get("duration") or 0) or None
        start = row.loc or 0.0
        end = row.loc_end if row.loc_end is not None else start
        video = row.kind == "video"
        self.picture.set_paintable(frame)
        self.picture.set_size_request(-1, 230 if video else 120)
        self.badge.set_text(clock(start))
        self.badge.set_visible(video)
        self.big_play.set_visible(True)
        self.timeline.show(duration, start, end)
        self._label(None)
        # A recording has no picture: its waveform is the timeline, taller.
        self.overlay.set_visible(video)
        self.timeline.set_content_height(26 if video else 90)
        # The waveform comes off the main thread; the timeline shows the stretch already.
        path = row.path

        def load():
            values = peaks(path)
            GLib.idle_add(lambda: self.row is row and self.timeline.show(duration, start, end, values) and False)
        threading.Thread(target=load, daemon=True).start()

    def set_frame(self, texture):
        if self.stream is None or not self.stream.get_playing():
            self.picture.set_paintable(texture)

    def _label(self, now):
        row = self.row
        if not row:
            return
        start = row.loc or 0.0
        end = row.loc_end if row.loc_end is not None else start
        duration = float(row.info.get("duration") or 0)
        text = f"{clock(start)} – {clock(end)}" if end > start + 1 else clock(start)
        if duration:
            text += f" of {clock(duration)}"
        if now is not None:
            text = f"{clock(now)}  ·  matched {text}"
        self.time.set_text(text)

    # MARK: Playing

    def toggle(self):
        if self.stream and self.stream.get_playing():
            self.stream.pause()
            self._playing(False)
            return
        if self.stream is None:
            self._open()
        else:
            self.stream.play()
            self._playing(True)

    def _open(self):
        row = self.row
        stream = Gtk.MediaFile.new_for_filename(row.path)
        self.stream = stream
        if row.kind == "video":
            self.picture.set_paintable(stream)
        start = row.loc or 0.0

        def prepared(media, _):
            if media.is_prepared() and media is self.stream:
                media.seek(int(start * 1_000_000))
                media.play()
        stream.connect("notify::prepared", prepared)
        stream.connect("notify::timestamp", lambda media, _: media is self.stream and self._tick(media))
        stream.connect("notify::error", lambda media, _: media.get_error() and self._failed(media.get_error()))
        stream.connect("notify::ended", lambda media, _: media.get_ended() and self._playing(False))
        self._playing(True)

    def _tick(self, media):
        seconds = media.get_timestamp() / 1_000_000
        self.timeline.set_playhead(seconds)
        self._label(seconds)

    def _failed(self, error):
        self.time.set_text(f"Can't play it here: {error.message}")
        self._playing(False)

    def _playing(self, playing):
        icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        self.play.set_icon_name(icon)
        self.big_play.set_visible(not playing)

    def seek(self, seconds):
        if self.stream is None:
            self._open()
            self.stream.connect("notify::prepared", lambda media, _: media.is_prepared() and media.seek(
                int(seconds * 1_000_000)))
            return
        self.stream.seek(int(seconds * 1_000_000))
        if not self.stream.get_playing():
            self.stream.play()
            self._playing(True)

    def stop(self):
        if self.stream:
            self.stream.pause()
            self.stream = None
        self._playing(False)
