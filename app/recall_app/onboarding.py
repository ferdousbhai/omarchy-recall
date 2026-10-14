"""The first run: welcome → folders → code (when there are projects) → shortcut →
ready. The model starts downloading at "Get Started", and indexing starts as soon as the folders are chosen."""

import os

from gi.repository import Adw, Gtk, Pango

from . import hotkey as hotkeys
from . import opener, paths, widgets


class Onboarding(Adw.Window):
    def __init__(self, app):
        super().__init__(application=app, title="Welcome to Recall", default_width=680, default_height=620)
        self.set_hide_on_close(True)
        self.app = app
        self.code_candidates = widgets.code_folders()
        self.steps = ["welcome", "folders"] + (["code"] if self.code_candidates else []) + ["shortcut", "ready"]
        self.step = 0
        self.chosen = {folder: True for folder in widgets.user_folders()}
        for root in app.settings.roots:
            self.chosen[root] = True
        self.estimates = {}
        self.search_code = False
        self.code_chosen = {folder: True for folder in self.code_candidates}
        self.estimating = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(outer)
        outer.append(Adw.HeaderBar(show_title=False))
        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        outer.append(self.stack)
        footer = Gtk.Box(spacing=12, margin_start=24, margin_end=24, margin_top=12, margin_bottom=16)
        self.dots = Gtk.Label()
        footer.append(self.dots)
        self.footer_note = Gtk.Label(hexpand=True, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.footer_note.add_css_class("dim-label")
        footer.append(self.footer_note)
        self.back = Gtk.Button(label="Back")
        self.back.connect("clicked", lambda _: self.go(self.step - 1))
        footer.append(self.back)
        self.next = Gtk.Button()
        self.next.add_css_class("suggested-action")
        self.next.connect("clicked", lambda _: self.forward())
        footer.append(self.next)
        outer.append(footer)

        for step in self.steps:
            self.stack.add_named(getattr(self, f"_{step}")(), step)
        self.go(0)

    # MARK: Steps

    @staticmethod
    def _page(title, subtitle=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=36, margin_end=36,
                      margin_top=12)
        heading = Gtk.Label(label=title, xalign=0, wrap=True)
        heading.add_css_class("title-1")
        box.append(heading)
        if subtitle:
            note = Gtk.Label(label=subtitle, xalign=0, wrap=True)
            note.add_css_class("dim-label")
            box.append(note)
        return box

    def _welcome(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER,
                      margin_start=48, margin_end=48)
        icon = Gtk.Image.new_from_icon_name("io.github.pdstn12.Recall")
        icon.set_pixel_size(112)
        box.append(icon)
        name = Gtk.Label(label="Recall")
        name.add_css_class("title-1")
        box.append(name)
        tagline = Gtk.Label(label="Describe it. Recall it.")
        tagline.add_css_class("title-4")
        box.append(tagline)
        for title, example in (("Screenshots and photos", "“payment declined error”, “a dog on the beach”"),
                               ("PDFs and documents", "“the clause about pets in the lease”"),
                               ("Moments inside audio and video", "“where they talk about sleep”, “zebra in a video”")):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=4)
            label = Gtk.Label(label=title, xalign=0)
            label.add_css_class("heading")
            row.append(label)
            hint = Gtk.Label(label=example, xalign=0)
            hint.add_css_class("dim-label")
            row.append(hint)
            box.append(row)
        privacy = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, margin_top=10,
                            label="Everything stays on this computer. Recall downloads Google's EmbeddingGemma 2 "
                                  "model once (865 MB) and never sends your files anywhere.")
        privacy.add_css_class("dim-label")
        box.append(privacy)
        return box

    def _folders(self):
        box = self._page("What should Recall search?",
                         "Pick folders, and Recall reads the pictures, PDFs, documents, recordings and videos in them. "
                         "Code projects, hidden folders and caches are skipped.")
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.folder_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.folder_list.add_css_class("boxed-list")
        scroller.set_child(self.folder_list)
        box.append(scroller)
        add = Gtk.Button(label="Add Another Folder…", halign=Gtk.Align.START)
        add.connect("clicked", lambda _: widgets.pick_folders(self, "Add Folders", self._add_folders, "Add"))
        box.append(add)
        self._fill_folders()
        return box

    def _fill_folders(self):
        self.folder_list.remove_all()
        for folder in self.chosen:
            row = Adw.ActionRow(title=os.path.basename(folder) or folder, subtitle=paths.tilde(folder))
            check = Gtk.CheckButton(active=self.chosen[folder], valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda button, f=folder: self._toggle(f, button.get_active()))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            estimate = self.estimates.get(folder)
            label = Gtk.Label()
            if estimate is None:
                label.set_text("…")
            elif not estimate.get("readable", True):
                label.set_text("can't read it")
            else:
                text = f"≈ {widgets.rough_duration(estimate.get('seconds', 0))}"
                if estimate.get("later_seconds"):
                    text += f" + {widgets.rough_duration(estimate['later_seconds'])} later"
                label.set_text(text)
                counts = widgets.kind_counts(estimate.get("files", {}))
                row.set_subtitle(paths.tilde(folder) + ("\n" + counts if counts else "\nNothing to search here yet"))
                row.set_subtitle_lines(2)
            label.add_css_class("dim-label")
            row.add_suffix(label)
            self.folder_list.append(row)
        self._estimate()

    def _toggle(self, folder, on):
        self.chosen[folder] = on
        self._update_footer()

    def _add_folders(self, folders):
        for folder in folders:
            self.chosen[folder] = True
        self._fill_folders()

    def _estimate(self):
        waiting = [f for f in self.chosen if f not in self.estimates]
        if not waiting or self.estimating:
            return self._update_footer()

        def got(estimate):
            self.estimates[estimate.get("folder")] = estimate

        def done():
            self.estimating = None
            for folder in waiting:
                self.estimates.setdefault(folder, {"readable": False})
            self._fill_folders()
        self.estimating = self.app.engine.estimate(waiting, got, done)

    def _code(self):
        box = self._page("Search your code too?",
                         "Recall can read your projects, so “code: retry a failed call with backoff” finds the "
                         "function, and a name goes to where it's defined. It skips what git ignores, vendored code "
                         "and anything that looks like a key.")
        switch = Adw.SwitchRow(title="Search my code", active=False)
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        group.append(switch)
        box.append(group)
        folders = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, sensitive=False)
        folders.add_css_class("boxed-list")
        for folder in self.code_candidates:
            row = Adw.ActionRow(title=os.path.basename(folder), subtitle=paths.tilde(folder))
            check = Gtk.CheckButton(active=True, valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda b, f=folder: self.code_chosen.__setitem__(f, b.get_active()))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            folders.append(row)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(folders)
        box.append(scroller)

        def toggled(row, _):
            self.search_code = row.get_active()
            folders.set_sensitive(self.search_code)
        switch.connect("notify::active", toggled)
        editors = opener.installed_editors()
        if editors:
            note = Gtk.Label(label=f"Code opens in {editors[0].name} at the line (you can change it in Settings).",
                             xalign=0, wrap=True)
            note.add_css_class("dim-label")
            box.append(note)
        return box

    def _shortcut(self):
        box = self._page("Open Recall from anywhere")
        accelerator = self.app.settings.hotkey
        label = Gtk.ShortcutLabel(accelerator=accelerator, halign=Gtk.Align.START, margin_top=12)
        box.append(label)
        self.try_label = Gtk.Label(label=f"Try it now: press {hotkeys.display(accelerator)}.", xalign=0)
        box.append(self.try_label)
        tips = Gtk.Label(xalign=0, margin_top=18, use_markup=True,
                         label="<b>IN THE SEARCH PANEL</b>\n↩  Open the file, at the page or moment\n"
                               "Ctrl+↩  Show it in the folder\nCtrl+C  Copy the file\n↓ ↑  Move through the results\n"
                               "Esc  Close")
        box.append(tips)
        if not (self.app.hotkey and self.app.hotkey.bound):
            note = Gtk.Label(wrap=True, xalign=0, margin_top=12,
                             label="If the shortcut doesn't open Recall, assign one to “recall-app --toggle” in your "
                                   "desktop's keyboard shortcut settings.")
            note.add_css_class("dim-label")
            box.append(note)
        return box

    def _ready(self):
        box = self._page("You're all set")
        self.ready_note = Gtk.Label(xalign=0, wrap=True)
        box.append(self.ready_note)
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=12)
        group.add_css_class("boxed-list")
        self.login = Adw.SwitchRow(title="Open Recall when you log in", subtitle="So the shortcut always works.",
                                   active=True)
        group.append(self.login)
        self.battery = Adw.SwitchRow(title="Index on battery power", active=self.app.settings.index_on_battery,
                                     subtitle="When it's off, the first pass waits for a charger.")
        group.append(self.battery)
        box.append(group)
        tray = Gtk.Label(wrap=True, xalign=0, margin_top=12,
                         label="Recall lives in the tray: from there you can see how indexing is going, pause it, or "
                               "open Settings.")
        tray.add_css_class("dim-label")
        box.append(tray)
        return box

    # MARK: Moving

    def go(self, index):
        self.step = max(0, min(len(self.steps) - 1, index))
        name = self.steps[self.step]
        self.stack.set_visible_child_name(name)
        self.back.set_visible(self.step > 0)
        self.next.set_label({"welcome": "Get Started", "ready": "Done"}.get(name, "Continue"))
        self.dots.set_text(" ".join("●" if i == self.step else "○" for i in range(len(self.steps))))
        if name == "shortcut":
            self.app.hotkey_tester = self._shortcut_works
        else:
            self.app.hotkey_tester = None
        if name == "ready":
            self._update_ready()
        self._update_footer()

    def forward(self):
        name = self.steps[self.step]
        if name == "welcome":
            self.app.download.start()   # the go-ahead for the 865 MB
        elif name == "folders":
            roots = [f for f, on in self.chosen.items() if on]
            if not roots:
                return
            self.app.apply_folders(roots)   # indexing starts now
        elif name == "code":
            self.app.apply_code_folders([f for f, on in self.code_chosen.items() if on] if self.search_code else [])
        elif name == "ready":
            return self.finish()
        self.go(self.step + 1)

    def finish(self):
        settings = self.app.settings
        settings.index_on_battery = self.battery.get_active()
        settings.onboarded = True
        settings.save()
        widgets.set_starts_at_login(self.login.get_active())
        self.app.power.settings_changed()
        self.app.hotkey_tester = None
        self.close()
        self.app.onboarding = None

    def _shortcut_works(self):
        self.try_label.set_markup("<b>That's it. It works from any app.</b>")

    def _update_footer(self):
        name = self.steps[self.step]
        download = self.app.download
        if name == "folders":
            chosen = [f for f, on in self.chosen.items() if on]
            seconds = sum(self.estimates.get(f, {}).get("seconds", 0) for f in chosen)
            self.footer_note.set_text(f"First pass: about {widgets.rough_duration(seconds)}" if chosen
                                      else "Check the folders to search")
            self.next.set_sensitive(bool(chosen))
        else:
            self.next.set_sensitive(True)
            self.footer_note.set_text(download.line or ("Model ready" if download.phase == "ready" else "")
                                      if name != "welcome" else "")

    def _update_ready(self):
        chosen = [f for f, on in self.chosen.items() if on]
        seconds = sum(self.estimates.get(f, {}).get("seconds", 0) for f in chosen)
        if self.app.download.phase == "ready":
            text = f"Reading your folders takes about {widgets.rough_duration(seconds)}, newest files first."
        else:
            text = "Indexing starts once the model has downloaded. Until then, search finds files by name."
        self.ready_note.set_text(text)

    def status_changed(self):
        self._update_footer()
