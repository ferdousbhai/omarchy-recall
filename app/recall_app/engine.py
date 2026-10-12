"""The engine's processes, on GLib's main loop: `recall serve` (search), `recall index-service` (indexing) and
`recall estimate` (folder sizes).

Everything here runs on the main thread: replies and events arrive as callbacks from the main loop.
"""

import json
import os
import time
from dataclasses import dataclass, field

from gi.repository import Gio, GLib

from . import paths
from .log import log


class LineProcess:
    """A child process that speaks JSON lines: `send` writes one, `on_line` gets each one it prints."""

    def __init__(self, argv, on_line, on_exit=None, env=None, stderr_path=None):
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
                                              | (Gio.SubprocessFlags.NONE if stderr_path
                                                 else Gio.SubprocessFlags.STDERR_SILENCE))
        for key, value in (env or {}).items():
            launcher.setenv(key, value, True)
        if stderr_path:
            launcher.set_stderr_file_path(str(stderr_path))
        self.on_line = on_line
        self.on_exit = on_exit
        self.process = launcher.spawnv(argv)
        self.stdin = self.process.get_stdin_pipe()
        self.stdout = Gio.DataInputStream.new(self.process.get_stdout_pipe())
        self.alive = True
        self._read()
        self.process.wait_async(None, self._exited)

    def _read(self):
        self.stdout.read_line_async(GLib.PRIORITY_DEFAULT, None, self._got_line)

    def _got_line(self, stream, result):
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:
            return
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if message is not None:
            try:
                self.on_line(message)
            except Exception as error:  # noqa: BLE001 - a bug in a handler must not stop the reading
                log(f"handler failed: {error!r}")
        self._read()

    def _exited(self, process, result):
        self.alive = False
        try:
            process.wait_finish(result)
        except GLib.Error:
            pass
        if self.on_exit:
            self.on_exit(process.get_exit_status() if process.get_if_exited() else -1)

    def send(self, text):
        if not self.alive:
            return
        try:
            self.stdin.write_all((text + "\n").encode(), None)
        except GLib.Error as error:
            log(f"can't write to {self.process.get_identifier()}: {error.message}")

    def stop(self):
        if self.alive:
            try:
                self.stdin.close(None)
            except GLib.Error:
                pass
            GLib.timeout_add_seconds(3, lambda: self.alive and self.process.force_exit() and False)


@dataclass
class Status:
    """What the tray and the windows show about indexing."""
    activity: str = "starting"   # starting, idle, syncing, indexing, finishing, waiting-model, failed
    done: int = 0
    total: int = 0
    message: str = ""
    files: int = 0
    searchable: int = 0
    pending: int = 0
    unfinished: int = 0
    vectors: int = 0
    paused: bool = False
    missing: list = field(default_factory=list)
    code_files: int = 0
    code_searchable: int = 0
    code_pending: int = 0
    code_indexing: tuple = None   # (done, total) while the code index is being read

    @property
    def progress(self):
        if self.activity in ("indexing", "finishing") and self.total:
            return self.done / self.total
        return None

    def line(self, has_folders=True):
        """The one line of status."""
        if not has_folders:
            return "No folders chosen yet"
        if self.activity == "failed":
            return f"⚠ {self.message}"
        if self.activity == "starting":
            return "Starting…"
        if self.activity == "syncing":
            return "Checking folders…"
        if self.activity == "indexing" and self.total:
            return f"Indexing {round(100 * self.done / self.total)}% · {self.done} of {self.total}"
        if self.activity == "finishing" and self.total:
            return f"Reading the rest of long files · {round(100 * self.done / self.total)}%"
        if self.code_indexing:
            done, total = self.code_indexing
            return f"Indexing code {round(100 * done / max(total, 1))}% · {done} of {total}"
        if self.activity == "waiting-model":
            return "Waiting for the model to download"
        if self.paused and self.pending:
            return f"Paused · {self.pending} files waiting"
        if self.pending:
            return f"{self.pending} files waiting to be indexed"
        if self.unfinished:
            return f"{self.unfinished} long files to finish" + (" · paused" if self.paused else "")
        return f"{self.searchable} files indexed" + (" · paused" if self.paused else "")


class Engine:
    """Owns the engine's processes for the app."""

    def __init__(self, settings, on_status):
        self.settings = settings
        self.on_status = on_status
        self.status = Status()
        self.binary = paths.engine_binary()
        self.index = settings.index_directory
        os.makedirs(self.index, exist_ok=True)
        self.serve = None
        self.helper = None
        self.requests = {}          # id → callback
        self.next_id = 1
        self.helper_wanted = False  # restart the helper when it exits (the folders changed meanwhile)
        self.sync_owed = False
        self.backfill = True
        self.last_reload = 0
        self.reload_pending = False

    @property
    def env(self):
        return {"RECALL_MODELS": self.settings.models_directory}

    # MARK: Search (recall serve)

    def ensure_serve(self):
        if self.serve and self.serve.alive:
            return True
        if not self.binary:
            return False
        self.serve = LineProcess([self.binary, "serve", "--index", self.index, "--idle",
                                  str(self.settings.encoder_idle_seconds)],
                                 self._serve_reply, self._serve_exited, env=self.env,
                                 stderr_path=os.path.join(self.index, "serve.log"))
        self.configure()
        return True

    def configure(self):
        s = self.settings
        self.request("configure", None, roots=s.roots, skipped=s.excluded_folders, types=s.excluded_types,
                     code_roots=s.code_roots, code_skipped=s.code_excluded)

    def request(self, op, callback, **fields):
        if not self.ensure_serve():
            if callback:
                callback({"error": "the recall engine isn't built (cargo build --release)"})
            return None
        request_id = self.next_id
        self.next_id += 1
        if callback:
            self.requests[request_id] = callback
        self.serve.send(json.dumps({"id": request_id, "op": op, **fields}))
        return request_id

    def _serve_reply(self, reply):
        callback = self.requests.pop(reply.get("id"), None)
        if callback:
            callback(reply)

    def _serve_exited(self, status):
        log(f"recall serve exited ({status})")
        for callback in self.requests.values():
            callback({"error": "the search engine stopped"})
        self.requests.clear()

    def search(self, query, callback, meaning=True, prefix_last=False, kinds=None, limit=40):
        fields = {"query": query, "meaning": meaning, "prefix_last": prefix_last, "limit": limit}
        if kinds:
            fields["kinds"] = sorted(kinds)
        return self.request("search", callback, **fields)

    def prewarm(self):
        self.request("prewarm", None)

    def release(self):
        if self.serve and self.serve.alive:
            self.request("release", None)

    def passage(self, segment, callback, code=False):
        self.request("text", lambda reply: callback(reply.get("text")), segment=segment, code=code)

    def rank(self, query, texts, callback):
        """How well each text matches the query by meaning (`callback(scores)`; empty when it can't say)."""
        self.request("rank", lambda reply: callback(reply.get("scores") or []), query=query, texts=texts)

    def root_summaries(self, callback, code=False):
        self.request("roots", lambda reply: callback(reply.get("roots") or []), code=code)

    def _reload_vectors(self):
        """New vectors arrived (indexing): searches see them, at most every 2 s."""
        now = time.monotonic()
        if now - self.last_reload >= 2:
            self.last_reload = now
            if self.serve and self.serve.alive:
                self.request("reload", None)
        elif not self.reload_pending:
            self.reload_pending = True

            def later():
                self.reload_pending = False
                self._reload_vectors()
                return False
            GLib.timeout_add(2100, later)

    # MARK: Indexing (recall index-service)

    def start(self):
        self.ensure_serve()
        self.start_helper()

    def start_helper(self):
        s = self.settings
        if not self.binary or not (s.roots or s.code_roots) or self.status.paused:
            self.status.activity = "idle"
            self._changed()
            return
        if self.helper and self.helper.alive:
            self.helper_wanted = True   # it restarts with the new folders once it has stopped
            self.helper.send("stop")
            return
        argv = [self.binary, "index-service", "--index", self.index, "--prune"]
        if self.backfill:
            argv.append("--backfill")
        for folder in s.excluded_folders:
            argv += ["--exclude", folder]
        for kind in s.excluded_types:
            argv += ["--exclude-ext", kind]
        for folder in s.code_roots:
            argv += ["--code-root", folder]
        for folder in s.code_excluded:
            argv += ["--code-exclude", folder]
        argv += s.roots
        log("index-service: " + " ".join(argv[2:]))
        self.helper = LineProcess(argv, self._event, self._helper_exited, env=self.env,
                                  stderr_path=os.path.join(self.index, "indexer.log"))
        self.status.activity = "syncing"
        self._changed()

    def request_sync(self, code=False):
        if self.helper and self.helper.alive:
            self.helper.send("sync code" if code else "sync")
        else:
            self.start_helper()

    def set_paused(self, paused):
        self.status.paused = paused
        if paused and self.helper and self.helper.alive:
            self.helper.send("stop")
        elif not paused:
            self.start_helper()
        self._changed()

    def set_backfill(self, allowed):
        self.backfill = allowed
        if self.helper and self.helper.alive:
            self.helper.send("backfill on" if allowed else "backfill off")

    def folders_changed(self):
        self.configure()
        self.start_helper()

    def _helper_exited(self, status):
        log(f"index-service exited ({status})")
        if self.helper_wanted or self.sync_owed:
            self.helper_wanted = self.sync_owed = False
            GLib.idle_add(lambda: self.start_helper() and False)
        elif self.status.activity not in ("failed", "waiting-model"):
            self.status.activity = "idle"
        self._reload_vectors()
        self._changed()

    def _event(self, event):
        kind = event.get("event")
        code = event.get("code", False)
        s = self.status
        for key in ("files", "searchable", "pending", "unfinished", "vectors", "code_files", "code_searchable",
                    "code_pending"):
            if key in event:
                setattr(s, key, event[key])
        if kind == "sync":
            s.activity = "syncing" if not code else s.activity
        elif kind == "file":
            if code:
                s.code_indexing = (event.get("done", 0), event.get("total", 0))
            else:
                s.activity, s.done, s.total = "indexing", event.get("done", 0), event.get("total", 0)
            self._reload_vectors()
        elif kind == "rest":
            s.activity, s.done, s.total = "finishing", event.get("done", 0), event.get("total", 0)
            self._reload_vectors()
        elif kind == "summary":
            if code:
                s.code_indexing = None
            self._reload_vectors()
        elif kind == "idle":
            s.activity, s.code_indexing = "idle", None
            self._reload_vectors()
        elif kind == "waiting":
            s.activity = "waiting-model"
        elif kind == "missing":
            s.missing = event.get("folders", []) + event.get("unreadable", [])
        elif kind == "error":
            log(f"indexing: {event.get('message')}")
            if event.get("fatal"):
                s.activity, s.message = "failed", event.get("message", "indexing stopped")
        elif kind == "reindex":
            log(f"the model changed ({event.get('from')} → {event.get('to')}): indexing everything again")
        self._changed()

    def _changed(self):
        self.on_status(self.status)

    def model_arrived(self):
        self.request_sync()

    def stop(self):
        for process in (self.helper, self.serve):
            if process:
                process.stop()

    # MARK: Estimates (recall estimate)

    def estimate(self, folders, on_folder, on_done=None, code=False, excluded=(), types=()):
        """How long a first pass over each folder takes: `on_folder(estimate)` for each, as they come."""
        if not self.binary or not folders:
            if on_done:
                on_done()
            return None
        argv = [self.binary, "estimate", "--json"] + (["--code"] if code else [])
        for folder in excluded:
            argv += ["--exclude", folder]
        if types:
            argv += ["--exclude-ext", ",".join(types)]
        return LineProcess(argv + list(folders), on_folder, (lambda _: on_done()) if on_done else None)
