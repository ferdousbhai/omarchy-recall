"""Settings: General, Folders, Code and Storage."""

import os

from gi.repository import Adw, Gdk, GLib, Gtk

from . import hotkey as hotkeys
from . import opener, paths, widgets
from .log import log


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Recall Settings", default_width=640, default_height=640,
                         search_enabled=False)
        self.set_hide_on_close(True)
        self.app = app
        self.pages = {}
        self._general()
        self.folders_page = self._page("folders", "Folders", "folder-symbolic")
        self.code_page = self._page("code", "Code", "text-x-script-symbolic")
        self._storage()
        self.refresh()
        self.refresh_timer = None

    def _page(self, name, title, icon):
        page = Adw.PreferencesPage(name=name, title=title, icon_name=icon)
        self.add(page)
        self.pages[name] = page
        return page

    def show_page(self, name):
        if name in self.pages:
            self.set_visible_page(self.pages[name])

    @staticmethod
    def _clear(page):
        for group in list(getattr(page, "_groups", [])):
            page.remove(group)
        page._groups = []

    @staticmethod
    def _group(page, title=None, description=None):
        group = Adw.PreferencesGroup(title=title or "", description=description or "")
        page.add(group)
        page._groups = getattr(page, "_groups", []) + [group]
        return group

    # MARK: General

    def _general(self):
        page = self._page("general", "General", "preferences-system-symbolic")
        group = self._group(page)
        self.shortcut_row = Adw.ActionRow(title="Search shortcut", subtitle="Opens the search panel from any app.")
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", lambda _: self._record_shortcut())
        self.shortcut_label = Gtk.ShortcutLabel(accelerator=self.app.settings.hotkey, valign=Gtk.Align.CENTER)
        self.shortcut_row.add_suffix(self.shortcut_label)
        self.shortcut_row.add_suffix(change)
        group.add(self.shortcut_row)
        login = Adw.SwitchRow(title="Open Recall when you log in", subtitle="So the shortcut always works.",
                              active=widgets.starts_at_login())
        login.connect("notify::active", lambda row, _: widgets.set_starts_at_login(row.get_active()))
        group.add(login)

        about = self._group(page)
        about.add(Adw.ActionRow(title="Recall for Omarchy",
                                subtitle="Search your files by what's in them. Nothing you index leaves this computer."))

    def _record_shortcut(self):
        dialog = Adw.Window(transient_for=self, modal=True, title="Search shortcut", default_width=420,
                            default_height=200)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=24, margin_bottom=24,
                      margin_start=24, margin_end=24, valign=Gtk.Align.CENTER)
        label = Gtk.Label(label="Press the new shortcut…")
        label.add_css_class("title-3")
        note = Gtk.Label(label="Use Ctrl, Alt or Super with a key. Esc cancels.")
        note.add_css_class("dim-label")
        box.append(label)
        box.append(note)
        dialog.set_content(box)
        keys = Gtk.EventControllerKey()

        def pressed(_controller, keyval, keycode, state):
            mods = state & Gtk.accelerator_get_default_mod_mask()
            if keyval == Gdk.KEY_Escape and not mods:
                dialog.close()
                return True
            if keyval in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Alt_L,
                          Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R):
                return True
            if not mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
                note.set_text("Use Ctrl, Alt or Super with a key.")
                return True
            accelerator = Gtk.accelerator_name(Gdk.keyval_to_lower(keyval), mods)
            self.app.settings.hotkey = accelerator
            self.app.settings.save()
            if self.app.hotkey:
                self.app.hotkey.bind(accelerator)
            self.shortcut_label.set_accelerator(accelerator)
            log(f"shortcut: {hotkeys.display(accelerator)}")
            dialog.close()
            return True
        keys.connect("key-pressed", pressed)
        dialog.add_controller(keys)
        dialog.present()

    # MARK: Folders and Code

    def refresh(self):
        """Rebuilds the folder pages from what's indexed under each folder (from the engine)."""
        self.app.engine.root_summaries(lambda roots: self._folders(roots))
        if self.app.settings.code_roots:
            self.app.engine.root_summaries(lambda roots: self._code(roots), code=True)
        else:
            self._code([])

    def status_changed(self):
        if self.get_visible() and self.refresh_timer is None:
            def again():
                self.refresh_timer = None
                self.refresh()
                return False
            self.refresh_timer = GLib.timeout_add_seconds(2, again)
        self._update_storage()

    def _folders(self, summaries):
        page = self.folders_page
        self._clear(page)
        app, status = self.app, self.app.engine.status
        progress = self._group(page)
        line = Adw.ActionRow(title=status.line(bool(app.settings.roots)))
        details = f"{status.searchable} of {status.files} files searchable"
        if status.pending:
            details += f" · {status.pending} waiting"
        if status.unfinished:
            details += f" · {status.unfinished} long files to finish"
        if app.power.blocker and status.pending:
            details += f" · first pass waits: {app.power.blocker}"
        line.set_subtitle(details)
        pause = Gtk.Button(label="Resume" if status.paused else "Pause", valign=Gtk.Align.CENTER)
        pause.connect("clicked", lambda _: (app.set_paused(not status.paused), GLib.timeout_add(300, lambda: self.refresh() and False)))
        line.add_suffix(pause)
        progress.add(line)

        folders = self._group(page, "Folders to search")
        by_path = {summary["path"]: summary for summary in summaries}
        for root in app.settings.roots:
            summary = by_path.get(root, {})
            row = self._folder_row(root, summary)
            remove = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Stop searching it")
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _, r=root, s=summary: self._confirm_remove(r, s, code=False))
            row.add_suffix(remove)
            folders.add(row)
        add = Gtk.Button(label="Add Folders…", halign=Gtk.Align.START, margin_top=8)
        add.connect("clicked", lambda _: self.add_folders())
        folders.add(add)

        skips = self._group(page, "Skip these folders", "None needed for hidden folders, caches, code projects and "
                                                        "app folders: they're skipped anyway.")
        for folder in app.settings.excluded_folders:
            row = Adw.ActionRow(title=os.path.basename(folder), subtitle=paths.tilde(folder))
            remove = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _, f=folder: self._unskip(f))
            row.add_suffix(remove)
            skips.add(row)
        skip = Gtk.Button(label="Skip a Folder…", halign=Gtk.Align.START, margin_top=8)
        skip.connect("clicked", lambda _: widgets.pick_folders(self, "Skip a Folder", self._skip, accept="Skip"))
        skips.add(skip)

        types = self._group(page, "Skip these file types",
                            "Pictures, screenshots, PDFs, documents (txt, md, rtf, doc, docx, odt, html), slides (pptx), "
                            "spreadsheets (xlsx), recordings "
                            "and videos are read.")
        entry = Adw.EntryRow(title="e.g. heic, txt", show_apply_button=True)
        entry.set_text(", ".join(app.settings.excluded_types))
        entry.connect("apply", lambda row: app.apply_folders(app.settings.roots, types=row.get_text().replace(" ", ",").split(",")))
        types.add(entry)

        power = self._group(page)
        battery = Adw.SwitchRow(title="Index on battery power", active=app.settings.index_on_battery,
                                subtitle="When it's off, a folder's first pass waits for a charger. New files always "
                                         "go in right away.")
        battery.connect("notify::active", lambda row, _: self._set_battery(row.get_active()))
        power.add(battery)

    def _folder_row(self, root, summary):
        missing = summary.get("missing") or not os.path.isdir(root)
        row = Adw.ActionRow(title=os.path.basename(root.rstrip("/")) or root)
        parts = [paths.tilde(root)]
        if missing:
            parts.append("Not connected · kept for when it's back")
        elif summary:
            parts.append(f"{summary.get('indexed', 0)} of {summary.get('files', 0)} indexed")
            counts = widgets.kind_counts(summary.get("kinds", {}))
            if counts:
                parts.append(counts)
            states = []
            for key, words in (("pending", "waiting"), ("unfinished", "long files to finish"),
                               ("skipped", "skipped"), ("failed", "couldn't be read")):
                if summary.get(key):
                    states.append(f"{summary[key]} {words}")
            if states:
                parts.append(" · ".join(states))
        row.set_subtitle("\n".join(parts))
        row.set_subtitle_lines(4)
        row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic" if missing else "folder-symbolic"))
        return row

    def _confirm_remove(self, root, summary, code):
        files = summary.get("files", 0)
        dialog = Adw.MessageDialog(transient_for=self, heading=f"Stop searching “{os.path.basename(root)}”?",
                                   body=f"Its {files} files leave the index. Adding it back later indexes them again.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Stop Searching")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(_dialog, response):
            if response != "remove":
                return
            if code:
                self.app.apply_code_folders([r for r in self.app.settings.code_roots if r != root])
            else:
                self.app.apply_folders([r for r in self.app.settings.roots if r != root])
            GLib.timeout_add(500, lambda: self.refresh() and False)
        dialog.connect("response", answered)
        dialog.present()

    def add_folders(self):
        def picked(folders):
            roots = list(self.app.settings.roots)
            for folder in folders:
                if any(folder == r or folder.startswith(r.rstrip("/") + "/") for r in roots):
                    continue   # searched already, as part of a folder that's chosen
                roots = [r for r in roots if not r.startswith(folder.rstrip("/") + "/")] + [folder]
            self.app.apply_folders(roots)
            GLib.timeout_add(500, lambda: self.refresh() and False)
        widgets.pick_folders(self, "Add Folders to Search", picked, accept="Add")

    def _skip(self, folders):
        self.app.apply_folders(self.app.settings.roots, excluded=self.app.settings.excluded_folders + folders)
        GLib.timeout_add(300, lambda: self.refresh() and False)

    def _unskip(self, folder):
        self.app.apply_folders(self.app.settings.roots,
                               excluded=[f for f in self.app.settings.excluded_folders if f != folder])
        GLib.timeout_add(300, lambda: self.refresh() and False)

    def _set_battery(self, on):
        self.app.settings.index_on_battery = on
        self.app.settings.save()
        self.app.power.settings_changed()

    def _code(self, summaries):
        page = self.code_page
        self._clear(page)
        app = self.app
        intro = self._group(page, None, "Recall reads the code in the folders you choose, so a search that starts with "
                                        "“code:” finds the function that does what you say, or the line that "
                                        "defines a name. Code has an index of its own.")
        if not app.settings.code_roots:
            choose = Gtk.Button(label="Choose Code Folders…", halign=Gtk.Align.START)
            choose.add_css_class("suggested-action")
            choose.connect("clicked", lambda _: self.add_code_folders())
            intro.add(choose)
            return
        status = app.engine.status
        size = widgets.size_of(os.path.join(app.settings.index_directory, "code.sqlite"))
        intro.add(Adw.ActionRow(title=f"{status.code_searchable} of {status.code_files} code files searchable",
                                subtitle=f"Code index: {widgets.rough_bytes(size)}"))
        folders = self._group(page, "Code folders")
        by_path = {summary["path"]: summary for summary in summaries}
        for root in app.settings.code_roots:
            row = self._folder_row(root, by_path.get(root, {}))
            remove = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _, r=root, s=by_path.get(root, {}): self._confirm_remove(r, s, code=True))
            row.add_suffix(remove)
            folders.add(row)
        add = Gtk.Button(label="Add Folders…", halign=Gtk.Align.START, margin_top=8)
        add.connect("clicked", lambda _: self.add_code_folders())
        folders.add(add)

        editor = self._group(page)
        editors = opener.installed_editors()
        names = [e.name for e in editors] + ["The file's default app"]
        ids = [e.id for e in editors] + [""]
        combo = Adw.ComboRow(title="Open code in", model=Gtk.StringList.new(names))
        current = opener.chosen_editor(app.settings.code_editor)
        combo.set_selected(ids.index(current.id) if current and current.id in ids else len(ids) - 1)

        def chose(row, _):
            app.settings.code_editor = ids[row.get_selected()]
            app.settings.save()
        combo.connect("notify::selected", chose)
        editor.add(combo)

        off = self._group(page)
        turn_off = Gtk.Button(label="Turn Off Code Search…", halign=Gtk.Align.START)
        turn_off.add_css_class("destructive-action")
        turn_off.connect("clicked", lambda _: self._turn_off_code(size))
        off.add(turn_off)

    def add_code_folders(self):
        def picked(folders):
            self.app.apply_code_folders(sorted(set(self.app.settings.code_roots + folders)))
            GLib.timeout_add(500, lambda: self.refresh() and False)
        widgets.pick_folders(self, "Choose Code Folders", picked, accept="Add")

    def _turn_off_code(self, size):
        dialog = Adw.MessageDialog(transient_for=self, heading="Turn off code search?",
                                   body=f"The code index ({widgets.rough_bytes(size)}) is deleted. Turning it on "
                                        "again reads the code again.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("off", "Turn Off")
        dialog.set_response_appearance("off", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(_dialog, response):
            if response == "off":
                self.app.apply_code_folders([], excluded=[])
                GLib.timeout_add_seconds(3, self._delete_code_index)
                self.refresh()
        dialog.connect("response", answered)
        dialog.present()

    def _delete_code_index(self):
        if self.app.engine.helper and self.app.engine.helper.alive:
            return True   # once the helper has let go of it
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.path.join(self.app.settings.index_directory, "code.sqlite" + suffix))
            except OSError:
                pass
        return False

    # MARK: Storage

    def _storage(self):
        page = self._page("storage", "Storage", "drive-harddisk-symbolic")
        model = self._group(page, "Model")
        model.add(Adw.ActionRow(title="EmbeddingGemma 2", subtitle="Google · Apache 2.0 · 865 MB"))
        self.model_row = Adw.ActionRow(title="Status")
        self.download_button = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
        self.download_button.connect("clicked", lambda _: self.app.download.start())
        self.model_row.add_suffix(self.download_button)
        model.add(self.model_row)
        model.add(self._location_row(self.app.settings.models_directory))

        index = self._group(page, "Index")
        self.index_row = Adw.ActionRow(title="Size on disk")
        index.add(self.index_row)
        index.add(self._location_row(self.app.settings.index_directory))

        privacy = self._group(page, "Privacy", "Nothing you index leaves this computer. Recall goes online only to "
                                               "download the model once.")
        privacy.add(Adw.ActionRow(title="Credits", subtitle="EmbeddingGemma 2 by Google (Apache 2.0), run by "
                                                            "llama.cpp (MIT); text in screenshots read by PaddleOCR's "
                                                            "PP-OCR models (Apache 2.0)."))
        self._update_storage()

    def _location_row(self, folder):
        row = Adw.ActionRow(title="Location", subtitle=paths.tilde(folder), activatable=True)
        row.add_suffix(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        row.connect("activated", lambda _: opener.open_default(folder) if os.path.isdir(folder) else None)
        return row

    def _update_storage(self):
        download = self.app.download
        self.model_row.set_subtitle("Downloaded" if download.phase == "ready" else (download.line or ""))
        self.download_button.set_visible(download.needs_start)
        index = self.app.settings.index_directory
        main = widgets.size_of(os.path.join(index, "index.sqlite"))
        code = widgets.size_of(os.path.join(index, "code.sqlite"))
        self.index_row.set_subtitle(widgets.rough_bytes(main) + (f" · code: {widgets.rough_bytes(code)}" if code else ""))
