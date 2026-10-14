import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli


class CliTests(unittest.TestCase):
    def test_optional_ocr_does_not_contact_service(self):
        with patch('subprocess.call', return_value=0) as call, patch.object(cli, 'request') as request:
            self.assertEqual(cli.main(['ocr']), 0)
        self.assertTrue(call.call_args.args[0][1].endswith('/scripts/setup-ocr.sh'))
        request.assert_not_called()

    def test_setup_can_skip_ocr(self):
        with patch('subprocess.run') as run, patch('subprocess.call', return_value=0) as call:
            self.assertEqual(cli.main(['setup', '--skip-ocr']), 0)
        run.assert_not_called()
        self.assertTrue(call.call_args.args[0][1].endswith('/omarchy/install.py'))

    def test_search_passes_filters_limit_and_keyword_mode(self):
        output = io.StringIO()
        with patch.object(cli, 'request', return_value={'hits': []}) as request, contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['search', 'release deadline', '--kind', 'audio', '--kind', 'video', '--limit', '3', '--keyword-only']), 0)
        request.assert_called_once_with({'op': 'search', 'query': 'release deadline', 'meaning': False, 'limit': 3, 'kinds': ['audio', 'video']})
        self.assertEqual(json.loads(output.getvalue()), {'hits': []})

    def test_code_and_passage_requests(self):
        with patch.object(cli, 'request', return_value={}) as request, contextlib.redirect_stdout(io.StringIO()):
            cli.main(['search', '--code', 'exponential backoff'])
            self.assertEqual(request.call_args.args[0]['query'], 'code: exponential backoff')
            cli.main(['text', '45', '--code'])
            request.assert_called_with({'op': 'text', 'segment': 45, 'code': True})

    def test_invalid_filter_combination_does_not_contact_service(self):
        with patch.object(cli, 'request') as request, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                cli.main(['search', 'foo', '--code', '--kind', 'image'])
            self.assertEqual(error.exception.code, 2)
            request.assert_not_called()

    def test_service_failure_has_json_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, cli.__file__, 'status'], capture_output=True, text=True,
                                    env=dict(os.environ, XDG_RUNTIME_DIR=directory))
        self.assertEqual(result.returncode, 1)
        self.assertIn('unavailable', json.loads(result.stdout)['error'])
        self.assertEqual(result.stderr, '')

    def test_semantic_fallback_is_explicit(self):
        output = io.StringIO()
        with patch.object(cli, 'request', return_value={'hits': [], 'encoder_error': 'model missing'}), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['search', 'foo']), 0)
        self.assertIn('keyword', json.loads(output.getvalue())['warning'])


if __name__ == '__main__':
    unittest.main()

class DoctorTests(unittest.TestCase):
    def test_doctor_does_not_need_the_service(self):
        output = io.StringIO()
        with patch('subprocess.run', return_value=type('Reply', (), {'returncode': 0, 'stdout': '{"selected_device":"CPU"}', 'stderr': ''})()) as run, patch.object(cli, 'request') as request, contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['doctor']), 0)
        self.assertEqual(json.loads(output.getvalue())['selected_device'], 'CPU')
        self.assertEqual(run.call_args.args[0][-1], 'devices')
        request.assert_not_called()
