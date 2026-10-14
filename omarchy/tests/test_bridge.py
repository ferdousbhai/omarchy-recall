import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'app'))
from gi.repository import GLib
from recall_app.omarchy_bridge import Bridge
from recall_app import opener
from recall_app.results import ResultRow


class BridgeTests(unittest.TestCase):
    def test_socket_transport_permissions_and_dispatch(self):
        with tempfile.TemporaryDirectory() as runtime, patch.dict(os.environ, XDG_RUNTIME_DIR=runtime):
            app = SimpleNamespace(engine=SimpleNamespace(release=lambda: None))
            bridge = Bridge(app)
            loop = GLib.MainLoop()
            thread = threading.Thread(target=loop.run)
            thread.start()
            try:
                self.assertEqual(bridge.path.stat().st_mode & 0o777, 0o600)
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(bridge.path))
                    connection.sendall(b'{"id":7,"op":"release"}\n')
                    self.assertEqual(json.loads(connection.makefile('rb').readline()), {'id': 7, 'ok': True})
            finally:
                loop.quit()
                thread.join()
                bridge.close()
            self.assertFalse(bridge.path.exists())

    def test_media_and_code_open_at_matching_location(self):
        hit = {'path': '/tmp/clip.mp4', 'kind': 'video', 'segment': 'frame', 'loc': 42.5}
        with patch.object(opener, 'default_app', return_value=None), \
             patch.object(opener.shutil, 'which', side_effect=lambda command: command == 'mpv'), \
             patch.object(opener, 'spawn', return_value=True) as spawn:
            opener.open_row(ResultRow.from_hit(hit))
            spawn.assert_called_once_with(['mpv', '--start=42.5', '/tmp/clip.mp4'])
        hit = {'path': '/tmp/code.py', 'kind': 'code', 'segment': 'lines', 'loc': 10, 'focus': 17}
        with patch.object(opener.shutil, 'which', return_value='/usr/bin/xdg-terminal-exec'), \
             patch.object(opener, 'spawn', return_value=True) as spawn:
            opener.open_row(ResultRow.from_hit(hit, code=True), 'nvim')
            spawn.assert_called_once_with(['xdg-terminal-exec', 'nvim', '+17', '/tmp/code.py'])

    def test_open_rejects_paths_outside_roots(self):
        bridge = Bridge.__new__(Bridge)
        bridge.app = SimpleNamespace(settings=SimpleNamespace(roots=['/tmp/allowed'], code_roots=[]))
        with self.assertRaisesRegex(ValueError, 'outside indexed'):
            bridge.dispatch({'op': 'open', 'hit': {'path': '/etc/passwd'}}, lambda _: None)


if __name__ == '__main__':
    unittest.main()
