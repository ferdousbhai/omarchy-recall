"""Where Recall's files are, as the engine has them (RecallKit's AppPaths and ModelFiles): XDG directories."""

import os
import shutil
from pathlib import Path

HOME = Path.home()
DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "recall"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "recall"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "recall"
INDEX = DATA / "index"
MODELS = DATA / "Models"

REPO = Path(__file__).resolve().parents[2]


def engine_binary():
    """The engine: $RECALL_BIN, else next to the app (a package), else this checkout's build, else $PATH."""
    candidates = [os.environ.get("RECALL_BIN"), Path(__file__).resolve().parents[1] / "bin/recall",
                  REPO / "target/release/recall", REPO / "target/debug/recall", shutil.which("recall-engine")]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def tilde(path):
    path = str(path)
    home = str(HOME)
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path
