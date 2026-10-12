"""The app's log: timestamped lines in <index>/app.log and on stderr."""

import sys
import time

from . import paths

_file = None


def open_log(index_directory=paths.INDEX):
    global _file
    try:
        index_directory.mkdir(parents=True, exist_ok=True)
        _file = open(index_directory / "app.log", "a", buffering=1, encoding="utf-8")  # noqa: SIM115 - kept open
    except OSError:
        _file = None


def log(message):
    now = time.time()
    line = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d} {message}"
    print(line, file=sys.stderr, flush=True)
    if _file:
        _file.write(line + "\n")
