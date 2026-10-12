"""Recall: the desktop app (GTK 4 + libadwaita) over the `recall` engine.

The engine runs as processes: `recall serve` answers searches, `recall index-service`
indexes, `recall estimate` sizes up folders. This package is the window, the tray icon and the shortcut around them.
"""

import os

# $RECALL_APP_ID: a test copy that runs beside the real one (one copy per app id runs at a time).
APP_ID = os.environ.get("RECALL_APP_ID", "io.github.pdstn12.Recall")
