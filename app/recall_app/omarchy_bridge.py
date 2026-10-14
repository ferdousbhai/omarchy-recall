"""Private Unix-socket API for the Omarchy panel; all engine work stays on GLib."""
import json
import os
import socketserver
import threading
from dataclasses import asdict
from pathlib import Path

from gi.repository import GLib
from . import opener
from .results import ResultRow


def socket_path():
    return Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'omarchy-recall.sock'


class Bridge:
    def __init__(self, app):
        self.app = app
        self.path = socket_path()
        self.path.unlink(missing_ok=True)
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(120)
                while raw := self.rfile.readline(1048577):
                    if len(raw) > 1048576:
                        return
                    done, result = threading.Event(), {}
                    try:
                        request = json.loads(raw)
                        if not isinstance(request, dict):
                            raise ValueError('expected an object')
                    except (ValueError, UnicodeError) as error:
                        self.wfile.write((json.dumps({'error': str(error)}) + '\n').encode())
                        continue

                    def complete(reply):
                        result.update(reply)
                        result['id'] = request.get('id')
                        done.set()

                    def dispatch():
                        try:
                            bridge.dispatch(request, complete)
                        except Exception as error:
                            complete({'error': str(error)})
                        return False

                    GLib.idle_add(dispatch)
                    if not done.wait(110):
                        result = {'id': request.get('id'), 'error': 'Search timed out'}
                    self.wfile.write((json.dumps(result) + '\n').encode())

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def dispatch(self, request, complete):
        app, op = self.app, request.get('op')
        if op == 'status':
            status = asdict(app.engine.status)
            status.update(line=app.download.line or app.engine.status.line(bool(app.settings.roots or app.settings.code_roots)),
                          roots=app.settings.roots, code_roots=app.settings.code_roots,
                          download=app.download.phase)
            complete(status)
        elif op == 'search':
            query = str(request.get('query', ''))[:4096]
            def found(reply):
                for hit in reply.get('hits', []):
                    row = ResultRow.from_hit(hit, reply.get('code', False))
                    hit.update(name=row.name, folder=row.folder, location=row.place, code=row.code)
                complete(reply)
            kinds = request.get('kinds')
            allowed = {'image', 'screenshot', 'pdf', 'doc', 'audio', 'video'}
            if kinds is not None and (not isinstance(kinds, list) or any(kind not in allowed for kind in kinds)):
                raise ValueError('Invalid search kinds')
            limit = int(request.get('limit', 30))
            if not 1 <= limit <= 100:
                raise ValueError('limit must be between 1 and 100')
            app.engine.search(query, found, meaning=request.get('meaning', True), kinds=kinds, limit=limit)
        elif op == 'text':
            segment = int(request['segment'])
            if segment <= 0:
                raise ValueError('segment must be positive')
            app.engine.request('text', complete, segment=segment, code=bool(request.get('code')))
        elif op == 'prewarm':
            app.engine.prewarm()
            complete({'ok': True})
        elif op == 'release':
            app.engine.release()
            complete({'ok': True})
        elif op == 'pause':
            app.set_paused(bool(request.get('paused')))
            complete({'ok': True})
        elif op == 'settings':
            app.show_settings()
            complete({'ok': True})
        elif op in ('open', 'reveal'):
            hit = request['hit']
            path = Path(hit['path']).resolve()
            roots = app.settings.code_roots if hit.get('code') else app.settings.roots
            if not path.is_file() or not any(path.is_relative_to(Path(root).resolve()) for root in roots):
                raise ValueError('File is outside indexed folders or no longer exists')
            if op == 'reveal':
                opener.reveal(str(path))
            else:
                opener.open_row(ResultRow.from_hit(hit, hit.get('code', False)), app.settings.code_editor)
            complete({'ok': True})
        else:
            complete({'error': f'Unknown operation: {op}'})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.path.unlink(missing_ok=True)
