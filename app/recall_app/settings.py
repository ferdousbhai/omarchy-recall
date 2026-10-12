"""The app's settings, in ~/.config/recall/settings.json."""

import json
from dataclasses import asdict, dataclass, field, fields

from . import paths


@dataclass
class Settings:
    roots: list = field(default_factory=list)            # folders searched
    excluded_folders: list = field(default_factory=list)
    excluded_types: list = field(default_factory=list)   # extensions, lowercase, no dot
    code_roots: list = field(default_factory=list)        # code search is on when there are some
    code_excluded: list = field(default_factory=list)
    code_editor: str = ""                                  # an editor's id (opener.EDITORS), "" for the default app
    hotkey: str = "<Shift><Super>space"                   # a portal shortcut trigger, as GTK writes accelerators
    index_on_battery: bool = True
    encoder_idle_seconds: int = 600
    onboarded: bool = False
    index_directory: str = str(paths.INDEX)
    models_directory: str = str(paths.MODELS)

    FILE = paths.CONFIG / "settings.json"

    @classmethod
    def load(cls):
        try:
            data = json.loads(cls.FILE.read_text())
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def save(self):
        self.FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2))
        temporary.replace(self.FILE)

    @property
    def has_code(self):
        return bool(self.code_roots)
