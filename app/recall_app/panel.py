"""The search panel: what the shortcut opens.

As you type, a search by words answers at once (names, OCR, document text), then the same search with meaning once
the query encoder has your words (120 ms after you stop typing). Results come in groups by kind, the strong ones
first and the rest folded under "weaker matches"; the selected one shows in the preview on the right.

Keys: ↓ ↑ move · ↩ open at the page, moment or line · Ctrl+↩ show in the file manager · Ctrl+C copy the file ·
Esc close.
"""

import html
import re
import time

from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from . import highlight, media, opener, previews, x11
from .log import log
from .results import ResultSet

WIDTH, LIST_WIDTH, HEIGHT = 900, 340, 580
TYPING_PAUSE_MS = 120

EXAMPLES = ["a screenshot of an error message", "receipt", "someone laughing", "a dog on the beach",
            "slides about the budget"]
CODE_EXAMPLES = ["code: retry a request with backoff", "code: where the settings are saved",
                 "code: parse the command line options"]

CSS = b"""
.recall-panel { border-radius: 16px; }
.recall-field { font-size: 20px; min-height: 52px; border: none; box-shadow: none; background: transparent; }
.recall-field > text { padding-left: 4px; }
.recall-header { padding: 4px 14px; }
.recall-group { font-size: 11px; font-weight: bold; letter-spacing: 1px; opacity: 0.6; padding: 10px 12px 4px; }
.recall-row { padding: 6px 10px; border-radius: 8px; }
.recall-row-title { font-weight: 600; }
.recall-row-sub { font-size: 12px; opacity: 0.7; }
.recall-badge { font-size: 11px; padding: 1px 6px; border-radius: 9px; background: alpha(currentColor, 0.12); }
.recall-badge-osd { font-size: 12px; font-weight: bold; padding: 2px 8px; border-radius: 9px;
                   background: rgba(0, 0, 0, 0.62); color: white; }
.recall-weaker row:not(.recall-weaker-toggle) { opacity: 0.85; }
.recall-preview { padding: 14px 16px; }
.recall-preview-title { font-weight: bold; font-size: 15px; }
.recall-picture { border-radius: 10px; background: alpha(currentColor, 0.06); }
.recall-media { border-radius: 12px; background: alpha(currentColor, 0.05); padding: 8px 10px; }
.recall-passage { background: white; color: #222; border-radius: 8px; padding: 10px 12px; }
.recall-code { font-family: monospace; font-size: 12px; background: alpha(currentColor, 0.06); border-radius: 8px;
              padding: 8px 10px; }
.recall-footer { padding: 6px 14px; font-size: 12px; opacity: 0.75; }
.recall-accent { color: #D97B1A; }
.recall-empty-title { font-size: 17px; font-weight: bold; }
"""


def marked(text, limit=None):
    """«word» marks from the engine as bold Pango markup."""
    text = text or ""
    if limit and len(text) > limit:
        text = text[:limit] + "…"
    out, bold = [], False
    for piece in text.replace("»", "«").split("«"):
        out.append(f"<b>{html.escape(piece)}</b>" if bold else html.escape(piece))
        bold = not bold
    return "".join(out)


def highlighted(text, words):
    """`text` with `words` marked (case-insensitive), as Pango markup."""
    escaped = html.escape(text)
    for word in sorted(set(words), key=len, reverse=True):
        if not word.strip():
            continue
        lower = escaped.lower()
        target = html.escape(word).lower()
        out, start = [], 0
        while (found := lower.find(target, start)) >= 0:
            out.append(escaped[start:found])
            out.append(f'<span background="#ffe066" foreground="#222">{escaped[found:found + len(target)]}</span>')
            start = found + len(target)
        out.append(escaped[start:])
        escaped = "".join(out)
    return escaped


class SearchPanel(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Recall")
        self.app = app
        self.engine = app.engine
        self.set_decorated(False)
        self.set_default_size(WIDTH, 56)
        self.set_resizable(False)
        self.add_css_class("recall-panel")
        self.set_hide_on_close(True)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.generation = 0
        self.results = None
        self.final = False           # the meaning search of this generation has answered
        self.rows = []               # selectable: ResultRow, or "weaker"
        self.selected = None
        self.navigated = False
        self.show_weaker = False
        self.shown_at = 0
        self.search_timer = None
        self.note_timer = None

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        # Header: the field, a spinner while meaning warms up, the indexing badge.
        header = Gtk.Box(spacing=8)
        header.add_css_class("recall-header")
        self.icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        self.icon.set_pixel_size(24)
        self.icon.add_css_class("recall-accent")
        header.append(self.icon)
        self.field = Gtk.Entry(hexpand=True)
        self.field.add_css_class("recall-field")
        self.field.connect("changed", self._query_changed)
        self.field.connect("activate", lambda _: self.open_selected())
        header.append(self.field)
        self.spinner = Gtk.Spinner()
        header.append(self.spinner)
        self.badge = Gtk.Label()
        self.badge.add_css_class("recall-badge")
        header.append(self.badge)
        root.append(header)

        self.separator = Gtk.Separator()
        root.append(self.separator)

        # Content: an empty state, or the list and the preview.
        self.stack = Gtk.Stack(vexpand=True)
        root.append(self.stack)
        self.empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER,
                             halign=Gtk.Align.CENTER)
        self.stack.add_named(self.empty, "empty")

        split = Gtk.Box()
        self.stack.add_named(split, "results")
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_size_request(LIST_WIDTH, -1)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.connect("row-selected", self._row_selected)
        self.list.connect("row-activated", lambda _, row: self.open_selected())
        scroller.set_child(self.list)
        self.list_scroller = scroller
        split.append(scroller)
        split.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.preview = Preview(self)
        split.append(self.preview)

        self.footer_separator = Gtk.Separator()
        root.append(self.footer_separator)
        footer = Gtk.Box(spacing=12)
        footer.add_css_class("recall-footer")
        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.keys = Gtk.Label(xalign=1)
        footer.append(self.status)
        footer.append(self.keys)
        self.footer = footer
        root.append(footer)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.connect("notify::is-active", self._activity_changed)
        self._set_expanded(False)
        self.update_status()

    # MARK: Showing and hiding

    def toggle(self, timestamp=0):
        if self.get_visible() and self.is_active():
            self.hide_panel()
        else:
            self.show_panel(timestamp=timestamp)

    def show_panel(self, query=None, timestamp=0):
        log("panel: shown")
        self.engine.prewarm()
        if query is not None:
            self.field.set_text(query)
        # Straight to the front with the focus, as a launcher's window: not a taskbar entry waiting for a click.
        if timestamp:
            self.present_with_time(timestamp)
        else:
            self.present()
        x11.launcher_window(self)
        x11.place(self, WIDTH)
        x11.activate(self, timestamp)
        GLib.timeout_add(80, lambda: (x11.place(self, WIDTH), not self.is_active() and x11.activate(self, timestamp))
                         and False)
        self.shown_at = time.monotonic()
        self.field.grab_focus()
        self.field.select_region(0, -1)   # the last search stays, as in Spotlight, ready to type over
        if not self.field.get_text():
            self._show_empty()

    def hide_panel(self):
        self.preview.stop()
        self.set_visible(False)
        previews.clear()

    def _activity_changed(self, *_):
        # Clicking elsewhere closes it, as a launcher does (not in the first moment: the shortcut's own focus dance).
        if not self.is_active() and self.get_visible() and time.monotonic() - self.shown_at > 0.5:
            self.hide_panel()

    def _set_expanded(self, expanded):
        self.separator.set_visible(expanded)
        self.stack.set_visible(expanded)
        self.footer_separator.set_visible(expanded)
        self.footer.set_visible(expanded)
        self.set_default_size(WIDTH, HEIGHT if expanded else 56)
        self.set_size_request(WIDTH, HEIGHT if expanded else -1)

    # MARK: Searching

    def _query_changed(self, _field):
        self.generation += 1
        generation = self.generation
        self.final = False
        self.navigated = False
        if self.search_timer:
            GLib.source_remove(self.search_timer)
            self.search_timer = None
        query = self.field.get_text().strip()
        if not query:
            self.results = None
            self.spinner.stop()
            self._show_empty()
            return
        code = query.lower().startswith("code:")
        self.icon.set_from_icon_name("text-x-script-symbolic" if code else "system-search-symbolic")
        if code and not self.app.settings.has_code:
            return self._show_empty(code_off=True)
        if code and not query[5:].strip():
            return self._show_empty(code_hint=True)
        # Words now; meaning once the typing pauses.
        self.engine.search(query, lambda reply: self._got(generation, reply, final=False), meaning=False,
                           prefix_last=True)

        def with_meaning():
            self.search_timer = None
            self.spinner.start()
            self.engine.search(query, lambda reply: self._got(generation, reply, final=True), meaning=True,
                               prefix_last=True)
            return False
        self.search_timer = GLib.timeout_add(TYPING_PAUSE_MS, with_meaning)

    def _got(self, generation, reply, final):
        if generation != self.generation or reply.get("skipped"):
            return
        if final:
            self.spinner.stop()
        elif self.final:
            return   # a late words-only answer never replaces the full one
        if "error" in reply:
            log(f"search: {reply['error']}")
            self.status.set_text(f"⚠ {reply['error']}")
            return
        self.final = final
        self.results = ResultSet(reply)
        self.meaning_problem = reply.get("encoder_error") if final and not reply.get("meaning") else None
        self._show_results()

    def _show_empty(self, code_off=False, code_hint=False, no_matches=False):
        child = self.empty.get_first_child()
        while child:
            self.empty.remove(child)
            child = self.empty.get_first_child()
        self.rows, self.selected = [], None
        settings = self.app.settings

        def title(text):
            label = Gtk.Label(label=text)
            label.add_css_class("recall-empty-title")
            self.empty.append(label)

        def note(text):
            label = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=60)
            label.add_css_class("dim-label")
            self.empty.append(label)

        def button(text, action):
            widget = Gtk.Button(label=text, halign=Gtk.Align.CENTER)
            widget.connect("clicked", lambda _: action())
            self.empty.append(widget)
            return widget

        if not self.field.get_text().strip():
            if not settings.roots:
                title("No folders chosen yet")
                button("Choose Folders…", self.app.choose_folders).add_css_class("suggested-action")
                self._set_expanded(True)
            else:
                self._set_expanded(False)
            self._update_footer()
            return
        if code_off:
            title("Code search is off")
            note("Pick the folders with your projects, and Recall reads their code too.")
            button("Choose Code Folders…", self.app.choose_code_folders)
        elif code_hint:
            title("Search your code")
            note("Say what the code does, or type a name that's in it.")
            for example in CODE_EXAMPLES:
                button(example, lambda e=example: self.field.set_text(e)).add_css_class("flat")
        elif no_matches:
            title("No good matches in your code" if self.results and self.results.code else "No good matches")
            note("Try what you'd see or hear in it, or words you remember from it.")
        self.stack.set_visible_child_name("empty")
        self._set_expanded(True)
        self._update_footer()

    def _show_results(self):
        results = self.results
        if results.empty:
            return self._show_empty(no_matches=True)
        previous = self.selected.id if self.selected and self.navigated and self.selected != "weaker" else None
        self.list.remove_all()
        self.rows = []
        for key, title, icon, rows, total in results.groups:
            header = Gtk.ListBoxRow(selectable=False, activatable=False)
            label = Gtk.Label(label=title.upper() + (f"  ·  {total}" if total > len(rows) else ""), xalign=0)
            label.add_css_class("recall-group")
            header.set_child(label)
            self.list.append(header)
            for row in rows:
                self._append(row)
        if results.weaker:
            toggle = Gtk.ListBoxRow()
            toggle.add_css_class("recall-weaker-toggle")
            arrow = "▾" if self.show_weaker else "▸"
            toggle.set_child(Gtk.Label(label=f"{arrow} {len(results.weaker)} weaker matches", xalign=0,
                                       margin_start=12, margin_top=8, margin_bottom=8))
            toggle.result = "weaker"
            self.list.append(toggle)
            self.rows.append("weaker")
            if self.show_weaker:
                for row in results.weaker:
                    self._append(row, weaker=True)
        self.stack.set_visible_child_name("results")
        self._set_expanded(True)
        # The selection follows the top hit until you move it yourself.
        target = next((r for r in self.rows if r != "weaker" and r.id == previous), None) or self.rows[0]
        wanted = self.app.debug.get("select")
        if wanted and self.final and wanted <= len(self.rows):
            target, self.navigated = self.rows[wanted - 1], True
        self._select(target)
        self._update_footer()

    def _append(self, row, weaker=False):
        widget = Gtk.ListBoxRow()
        widget.result = row
        box = Gtk.Box(spacing=10)
        box.add_css_class("recall-row")
        if weaker:
            box.set_opacity(0.8)
        thumb = Gtk.Image(pixel_size=36)
        thumb.set_from_icon_name(previews.file_icon(row.path))
        thumb.set_size_request(40, 40)
        previews.picture(row, 80, lambda texture, image=thumb: texture and image.set_from_paintable(texture))
        box.append(thumb)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        line = Gtk.Box(spacing=6)
        name = Gtk.Label(label=row.name, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True)
        name.add_css_class("recall-row-title")
        line.append(name)
        if row.place:
            badge = Gtk.Label(label=row.place)
            badge.add_css_class("recall-badge")
            line.append(badge)
        text.append(line)
        sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        sub.add_css_class("recall-row-sub")
        if row.matched_words and row.excerpt:
            sub.set_markup(marked(row.excerpt, 200))
        else:
            sub.set_text(row.folder)
        text.append(sub)
        box.append(text)
        widget.set_child(box)
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        drag.connect("prepare", lambda *_: file_content(row.path))
        widget.add_controller(drag)
        menu = Gtk.GestureClick(button=3)
        menu.connect("pressed", lambda gesture, n, x, y, r=row, w=widget: self._context_menu(r, w, x, y))
        widget.add_controller(menu)
        self.list.append(widget)
        self.rows.append(row)

    # MARK: Selection

    def _select(self, target):
        self.selected = target
        child = self.list.get_first_child()
        while child:
            if getattr(child, "result", None) is target:
                self.list.select_row(child)
                adjustment = self.list_scroller.get_vadjustment()
                ok, bounds = child.compute_bounds(self.list)
                if ok:
                    top, bottom = bounds.get_y(), bounds.get_y() + bounds.get_height()
                    if top < adjustment.get_value():
                        adjustment.set_value(top)
                    elif bottom > adjustment.get_value() + adjustment.get_page_size():
                        adjustment.set_value(bottom - adjustment.get_page_size())
                break
            child = child.get_next_sibling()

    def _row_selected(self, _list, widget):
        if widget is None:
            return
        target = getattr(widget, "result", None)
        self.selected = target
        if target == "weaker":
            self.preview.show_weaker(len(self.results.weaker) if self.results else 0)
        elif target is not None:
            self.preview.show(target)
        self._update_footer()

    def _move(self, step):
        if not self.rows:
            return
        index = self.rows.index(self.selected) if self.selected in self.rows else -1
        index = max(0, min(len(self.rows) - 1, index + step))
        self.navigated = True
        self._select(self.rows[index])

    # MARK: Actions

    def open_selected(self):
        if self.selected == "weaker":
            self.show_weaker = not self.show_weaker
            self.navigated = True
            self._show_results()
            self._select("weaker")
            return
        if self.selected:
            self.app.open_row(self.selected)
            self.hide_panel()

    def reveal_selected(self):
        if self.selected and self.selected != "weaker":
            opener.reveal(self.selected.path)
            self.hide_panel()

    def copy_selected(self):
        if not self.selected or self.selected == "weaker":
            return
        self.get_clipboard().set_content(file_content(self.selected.path))
        self.status.set_text(f"Copied {self.selected.name}")
        if self.note_timer:
            GLib.source_remove(self.note_timer)

        def restore():
            self.note_timer = None
            self._update_footer()
            return False
        self.note_timer = GLib.timeout_add(1600, restore)

    def _context_menu(self, row, widget, x, y):
        self._select(row)
        menu = Gio.Menu()
        menu.append(opener.open_title(row, self.app.settings.code_editor), "panel.open")
        menu.append("Show in Folder", "panel.reveal")
        menu.append("Copy", "panel.copy")
        menu.append("Copy Path", "panel.copy-path")
        group = Gio.SimpleActionGroup()
        for name, action in (("open", self.open_selected), ("reveal", self.reveal_selected),
                             ("copy", self.copy_selected),
                             ("copy-path", lambda: self.get_clipboard().set(row.path))):
            item = Gio.SimpleAction.new(name, None)
            item.connect("activate", lambda *_, a=action: a())
            group.add_action(item)
        widget.insert_action_group("panel", group)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    def _key(self, _controller, keyval, _keycode, state):
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            self.hide_panel()
            return True
        if keyval == Gdk.KEY_Down:
            self._move(1)
            return True
        if keyval == Gdk.KEY_Up:
            self._move(-1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.reveal_selected() if control else self.open_selected()
            return True
        if keyval == Gdk.KEY_space and self.navigated and not control and self.preview.toggle_media():
            return True
        if control and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            # Copies the file, unless you're copying text you selected in the field.
            bounds = self.field.get_selection_bounds()
            if self.navigated or not bounds or bounds[0] == bounds[1]:
                self.copy_selected()
                return True
        if control and keyval in (Gdk.KEY_comma,):
            self.app.show_settings()
            return True
        return False

    # MARK: Status

    def update_status(self):
        status = self.engine.status
        badge = None
        if self.app.download and self.app.download.active:
            badge = f"Downloading model · {round(100 * self.app.download.progress)}%"
        elif status.progress is not None:
            badge = f"Indexing {round(100 * status.progress)}%"
        elif status.paused:
            badge = "Indexing paused"
        self.badge.set_visible(badge is not None)
        self.badge.set_text(badge or "")
        self._update_footer()

    def _update_footer(self):
        if self.note_timer:
            return
        results = self.results
        query = self.field.get_text().strip()
        status = ""
        if query and results and not results.empty:
            strong, weaker = len(results.ranked), len(results.weaker)
            status = f"{strong} result{'s' if strong != 1 else ''}" + (" in your code" if results.code else "") \
                + (f" · {weaker} weaker" if weaker else "")
            if not self.final:
                status = "Matching words…"
            elif getattr(self, "meaning_problem", None):
                status += " · words only: " + self.meaning_problem
            if self.engine.status.pending:
                status += f" · {self.engine.status.pending} files still being indexed"
        elif query and not self.final:
            status = "Matching words…"
        self.status.set_text(status)
        row = self.selected if self.selected not in (None, "weaker") else None
        if row:
            play = "Space Play   " if row.kind in ("video", "audio") and row.segment in ("frame", "audio") else ""
            self.keys.set_text(f"↩ {opener.open_title(row, self.app.settings.code_editor)}   {play}"
                               "Ctrl+↩ Show in folder   Ctrl+C Copy")
        else:
            self.keys.set_text("")


class Preview(Gtk.Box):
    """The selected result, showing where it matched: the words marked on its PDF page or screenshot, its video at
    the moment (playable), its recording's waveform with the stretch, its passage or its code, then its name, folder,
    details and why it matched."""

    def __init__(self, panel):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        self.set_size_request(WIDTH - LIST_WIDTH - 2, -1)
        self.panel = panel
        self.add_css_class("recall-preview")
        self.row = None

        # The picture, with a badge for where on it ("p. 2 · 3 matches").
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.add_css_class("recall-picture")
        self.picture.set_size_request(-1, 250)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda g, n, x, y: n == 2 and panel.open_selected())
        self.picture.add_controller(click)
        picture_box = Gtk.Overlay()
        picture_box.set_child(self.picture)
        self.badge = Gtk.Label(halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=8, margin_bottom=8)
        self.badge.add_css_class("recall-badge-osd")
        picture_box.add_overlay(self.badge)
        self.spinner = Gtk.Spinner(halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=10, margin_bottom=10)
        picture_box.add_overlay(self.spinner)

        self.media_view = media.MediaPreview()
        self.passage = Gtk.Label(wrap=True, xalign=0, yalign=0, selectable=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                 max_width_chars=50)
        self.passage.add_css_class("recall-passage")
        self.code = Gtk.Label(xalign=0, yalign=0, selectable=True)
        self.code.add_css_class("recall-code")
        self.icon = Gtk.Image(pixel_size=96)
        self.media = Gtk.Stack(vhomogeneous=False, interpolate_size=True)
        self.media.add_named(picture_box, "picture")
        self.media.add_named(self.media_view, "media")
        self.media.add_named(self.icon, "icon")
        for name, widget in (("passage", self.passage), ("code", self.code)):
            scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
            scroller.set_size_request(-1, 250)
            scroller.set_child(widget)
            self.media.add_named(scroller, name)
        self.append(self.media)

        self.title = Gtk.Label(xalign=0, wrap=True, selectable=True, max_width_chars=40,
                               wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.title.add_css_class("recall-preview-title")
        self.append(self.title)
        self.folder = Gtk.Button(halign=Gtk.Align.START)
        self.folder_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=50)
        self.folder.set_child(self.folder_label)
        self.folder.set_tooltip_text("Show in folder")
        self.folder.add_css_class("flat")
        self.folder.connect("clicked", lambda _: panel.reveal_selected())
        self.append(self.folder)
        self.details = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
        self.details.add_css_class("dim-label")
        self.append(self.details)

        why = Gtk.Box(spacing=6)
        self.why_icon = Gtk.Image()
        self.why_icon.add_css_class("recall-accent")
        why.append(self.why_icon)
        self.reason = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
        why.append(self.reason)
        self.append(why)
        self.excerpt = Gtk.Label(xalign=0, wrap=True, lines=3, ellipsize=Pango.EllipsizeMode.END, max_width_chars=50,
                                 wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.append(self.excerpt)

        buttons = Gtk.Box(spacing=8, valign=Gtk.Align.END, vexpand=True)
        self.open_button = Gtk.Button()
        self.open_button.add_css_class("suggested-action")
        self.open_button.connect("clicked", lambda _: panel.open_selected())
        buttons.append(self.open_button)
        reveal = Gtk.Button(label="Show in Folder")
        reveal.connect("clicked", lambda _: panel.reveal_selected())
        buttons.append(reveal)
        self.buttons = buttons
        self.append(buttons)

    def stop(self):
        self.media_view.stop()

    def toggle_media(self):
        """Space: play or pause the selected video or recording here. False when it isn't one."""
        if self.row and self.media.get_visible_child_name() == "media":
            self.media_view.toggle()
            return True
        return False

    def show_weaker(self, count):
        self.stop()
        self.row = None
        self.media.set_visible_child_name("icon")
        self.icon.set_from_icon_name("view-more-symbolic")
        self.title.set_text(f"{count} weaker matches")
        self.folder.set_visible(False)
        self.details.set_text("These scored less than half as well as the best result. Press ↩ to show them.")
        self.reason.set_text("")
        self.why_icon.set_visible(False)
        self.excerpt.set_text("")
        self.buttons.set_visible(False)

    def show(self, row):
        if row is self.row:
            return
        self.stop()
        self.row = row
        self.title.set_text(row.name)
        self.folder.set_visible(True)
        self.folder_label.set_text(row.folder)
        self.details.set_text(row.details)
        self.reason.set_text(row.reason)
        self.why_icon.set_visible(True)
        self.why_icon.set_from_icon_name("starred-symbolic" if row.matched_meaning else "format-text-bold-symbolic")
        self.buttons.set_visible(True)
        self.open_button.set_label(opener.open_title(row, self.panel.app.settings.code_editor))
        query = self.panel.field.get_text()
        wanted = highlight.terms(query, row.found_words)
        words = sorted(wanted, key=len, reverse=True)
        self.excerpt.set_markup(marked(row.excerpt, 300) if row.matched_words and row.excerpt else "")
        self.excerpt.set_visible(bool(row.matched_words and row.excerpt))

        if row.kind in ("video", "audio") and row.segment in ("frame", "audio"):
            self.media.set_visible_child_name("media")
            self.media_view.show(row)
            if self.panel.final:
                log("panel: preview ready")
            if row.kind == "video":
                previews.picture(row, 640, lambda texture, r=row: r is self.row and self.media_view.set_frame(texture))
            return
        if row.code or row.segment == "chunk" or row.kind == "doc":
            self.media.set_visible_child_name("code" if row.code else "passage")
            self.excerpt.set_visible(False)   # the passage shows the words already
            label = self.code if row.code else self.passage
            label.set_text("…")
            self.panel.engine.passage(row.segment_id, lambda text, r=row: self._passage(r, text, words),
                                      code=row.code)
            return
        if row.kind in ("pdf", "image", "screenshot"):
            self.media.set_visible_child_name("picture")
            self.picture.set_paintable(None)
            self.badge.set_visible(bool(row.place))
            self.badge.set_text(row.place)
            previews.picture(row, 640, lambda texture, r=row: self._picture(r, texture))
            # Then the same picture with where it matched marked on it: the sentence (or line) that matches best
            # by meaning, ranked by the engine, and the query's words.
            if not self.panel.final:
                return   # the search with meaning will answer in a moment: mark its result, not this one
            if row.kind == "pdf" and row.page:
                units = lambda: highlight.pdf_units(row.path, row.page) or highlight.pdf_units_scanned(row.path, row.page)
                marks = lambda unit: highlight.pdf_page(row.path, row.page, wanted, unit=unit)
            elif row.kind == "screenshot" or row.segment == "ocr":
                units = lambda: highlight.picture_units(row.path)
                marks = lambda unit: highlight.picture(row.path, wanted, unit=unit)
            else:
                return
            self.spinner.start()

            def ranked(texts, r=row):
                if r is not self.row:
                    return
                if not texts:
                    return previews.run(lambda: marks(None), lambda result: self._marked(r, result))

                def scored(scores):
                    best = max(range(len(scores)), key=scores.__getitem__) if scores else None
                    previews.run(lambda: marks(best), lambda result: self._marked(r, result))
                self.panel.engine.rank(query, texts, scored)
            previews.run(units, ranked)
            return
        self.media.set_visible_child_name("icon")
        self.icon.set_from_icon_name(previews.file_icon(row.path))

    def _picture(self, row, texture):
        if row is not self.row or texture is None or self.picture.get_paintable() is not None:
            return
        self.picture.set_paintable(texture)

    def _marked(self, row, result):
        if row is not self.row:
            return
        self.spinner.stop()
        if not result:
            return
        path, count = result
        try:
            self.picture.set_paintable(Gdk.Texture.new_from_filename(path))
        except GLib.Error:
            return
        log("panel: preview ready")
        where = row.place
        found = f"{count} word{'s' if count != 1 else ''} found" if count else "best match marked"
        text = " · ".join(part for part in (where, found) if part)
        self.badge.set_visible(bool(text))
        self.badge.set_text(text)

    def _passage(self, row, text, words):
        if row is not self.row:
            return
        text = text or row.excerpt or ""
        if row.code:
            first = int(row.loc or 1)
            focus = row.code_line
            out = []
            for index, line in enumerate(text.split("\n")):
                number = first + index
                content = highlighted(line, words)
                if number == focus:
                    content = f'<span background="#F2A22A40">{content}</span>'
                out.append(f'<span alpha="50%">{number:>5}</span>  {content}')
            self.code.set_markup("\n".join(out))
            return
        total = row.info.get("chunks")
        part = f"Part {int(row.loc)} of {total}\n\n" if row.loc and total and str(total) != "1" else ""
        header = f'<span alpha="60%" size="small">{html.escape(part)}</span>'
        # A window around the first word found, then around the sentence that
        # matches best by meaning, marked, once the engine has ranked them.
        lower = highlight.fold(text)
        hits = [lower.find(w) for w in words if w and lower.find(w) >= 0]
        self.passage.set_markup(header + self._window(text, min(hits) if hits else 0, words))
        spans = [(m.start(), m.end()) for m in re.finditer(r"[^.!?\n]+(?:[.!?]+|$)", text) if len(m.group().strip()) > 15]
        if not spans or not self.panel.final:
            return

        def scored(scores, r=row):
            if r is not self.row or not scores:
                return
            best = spans[max(range(len(scores)), key=scores.__getitem__)]
            self.passage.set_markup(header + self._window(text, best[0], words, best))
            log("panel: preview ready")
        self.panel.engine.rank(self.panel.field.get_text(), [text[a:b].strip() for a, b in spans], scored)

    @staticmethod
    def _window(text, at, words, sentence=None):
        """900 characters of `text` from a little before `at`, the words marked, and `sentence` banded."""
        start = max(0, at - 160)
        end = min(len(text), start + 900)
        if sentence:
            a, b = max(sentence[0], start), min(sentence[1], end)
            body = (highlighted(text[start:a], words) + '<span background="#F2A22A55">'
                    + highlighted(text[a:b], words) + "</span>" + highlighted(text[b:end], words))
        else:
            body = highlighted(text[start:end], words)
        return ("…" if start else "") + body + ("…" if end < len(text) else "")


def file_content(path):
    """A file on the clipboard or in a drag: as a file for file managers, and as its path for text fields."""
    uri = Gio.File.new_for_path(path).get_uri()
    return Gdk.ContentProvider.new_union([
        Gdk.ContentProvider.new_for_bytes("text/uri-list", GLib.Bytes.new(f"{uri}\r\n".encode())),
        Gdk.ContentProvider.new_for_bytes("x-special/gnome-copied-files", GLib.Bytes.new(f"copy\n{uri}".encode())),
        Gdk.ContentProvider.new_for_bytes("application/x-kde-cutselection", GLib.Bytes.new(b"0")),
        Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new(path.encode())),
    ])
