"""The model download: EmbeddingGemma 2's two files (865 MB) at the revision the
engine pins (RecallKit's ModelFiles), into the models folder, each checked by size and SHA-256 before it gets its
name. A copy in the Hugging Face cache is used instead when it's there. Downloads resume where they stopped, and
retry after 2, 5, 15, then every 30 s while the network is away.
"""

import hashlib
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from gi.repository import GLib

from .log import log

REPOSITORY = "ggml-org/embeddinggemma-2-GGUF"
REVISION = "bfcd298762cc34d0357ece5ebdd31791a3a374d8"
FILES = [  # in download order: the text model first, all that search needs
    ("embeddinggemma-2-Q8_0.gguf", 309_855_456, "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135"),
    ("mmproj-embeddinggemma-2-Q8_0.gguf", 554_821_024,
     "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f"),
]
TOTAL = sum(size for _, size, _ in FILES)
RETRY_AFTER = [2, 5, 15]


def huggingface_snapshot():
    hub = os.environ.get("HF_HUB_CACHE") or (os.environ.get("HF_HOME") and os.path.join(os.environ["HF_HOME"], "hub")) \
        or os.path.expanduser("~/.cache/huggingface/hub")
    return Path(hub) / f"models--{REPOSITORY.replace('/', '--')}" / "snapshots" / REVISION


def missing(folder):
    return [file for file in FILES if not (Path(folder) / file[0]).exists()]


class Failed(Exception):
    pass


class ModelDownload:
    """phase: needed, downloading, verifying, retrying, failed, ready. `on_change` is called on the main thread."""

    def __init__(self, folder, on_change, on_ready):
        self.folder = Path(folder)
        self.on_change = on_change
        self.on_ready = on_ready
        self.phase = "ready" if not missing(folder) else "needed"
        self.message = ""
        self.received = 0         # bytes of the whole download done
        self.speed = 0.0
        self.cancelled = False
        self.thread = None

    @property
    def active(self):
        return self.phase in ("downloading", "verifying", "retrying")

    @property
    def needs_start(self):
        return self.phase in ("needed", "failed")

    @property
    def progress(self):
        return min(1.0, self.received / TOTAL)

    @property
    def progress_text(self):
        percent = f"{int(100 * self.progress)}%"
        if self.speed > 0:
            left = (TOTAL - self.received) / self.speed
            return f"{percent} · " + ("less than a minute left" if left < 60 else f"about {round(left / 60)} min left")
        return percent

    @property
    def line(self):
        """For the tray and the windows; None when there's nothing to say."""
        return {"downloading": f"Downloading the model · {self.progress_text}",
                "verifying": "Checking the model download…",
                "retrying": f"Model download: {self.message or 'waiting for the network'}",
                "failed": "⚠ The model download stopped",
                "needed": "The model isn't downloaded yet"}.get(self.phase)

    def start(self):
        if self.thread and self.thread.is_alive() or self.phase == "ready":
            return
        self.cancelled = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def cancel(self):
        self.cancelled = True

    def _set(self, phase, message=""):
        def apply():
            self.phase, self.message = phase, message
            self.on_change()
            if phase == "ready":
                self.on_ready()
            return False
        GLib.idle_add(apply)

    def _run(self):
        attempt, damaged = 0, 0
        while not self.cancelled:
            try:
                self._download_all()
                self._set("ready")
                log("model: ready")
                return
            except Failed as error:
                if str(error).startswith("damaged"):
                    damaged += 1
                    if damaged < 2:
                        continue
                    error = Failed("The download was damaged twice. Try again later.")
                log(f"model: {error}")
                self._set("failed", str(error))
                return
            except (urllib.error.URLError, OSError, TimeoutError) as error:
                wait = RETRY_AFTER[attempt] if attempt < len(RETRY_AFTER) else 30
                attempt += 1
                log(f"model: {error}; again in {wait} s")
                self._set("retrying", "waiting for a network connection")
                time.sleep(wait)

    def _download_all(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        needed = missing(self.folder)
        done = TOTAL - sum(size for _, size, _ in needed)
        free = shutil.disk_usage(self.folder).free
        if free < sum(size for _, size, _ in needed) + 200_000_000:
            raise Failed(f"Not enough disk space: the model needs {TOTAL // 1_000_000} MB")
        for name, size, digest in needed:
            final = self.folder / name
            cached = huggingface_snapshot() / name
            if cached.exists() and cached.resolve().stat().st_size == size:
                self._set("verifying")
                if self._sha256(cached.resolve()) == digest:
                    shutil.copyfile(cached.resolve(), final.with_suffix(".partial"))
                    os.replace(final.with_suffix(".partial"), final)
                    done += size
                    self.received = done
                    log(f"model: {name} copied from the Hugging Face cache")
                    continue
            partial = self.folder / (name + ".partial")
            self._fetch(name, size, partial, done)
            self._set("verifying")
            if partial.stat().st_size != size or self._sha256(partial) != digest:
                partial.unlink(missing_ok=True)
                raise Failed(f"damaged: {name}")
            os.replace(partial, final)
            done += size
            self.received = done

    def _fetch(self, name, size, partial, before):
        url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}"
        have = partial.stat().st_size if partial.exists() else 0
        if have >= size:
            return
        request = urllib.request.Request(url, headers={"Range": f"bytes={have}-", "User-Agent": "Recall"})
        self._set("downloading")
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status == 200:
                have = 0   # the server sent it all again
            elif response.status != 206:
                raise Failed(f"The download failed (HTTP {response.status})")
            window = []   # (time, bytes) over the last ~6 s, for the speed
            last_update = 0
            with open(partial, "ab" if have else "wb") as out:
                while not self.cancelled:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    have += len(chunk)
                    now = time.monotonic()
                    window = [(t, b) for t, b in window if now - t < 6] + [(now, have)]
                    if len(window) > 1 and window[-1][0] > window[0][0]:
                        self.speed = (window[-1][1] - window[0][1]) / (window[-1][0] - window[0][0])
                    self.received = before + have
                    if now - last_update > 0.2:
                        last_update = now
                        GLib.idle_add(lambda: self.on_change() and False)
        if self.cancelled:
            raise Failed("cancelled")

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as file:
            while chunk := file.read(8 << 20):
                digest.update(chunk)
        return digest.hexdigest()
