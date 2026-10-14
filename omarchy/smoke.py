#!/usr/bin/python3
"""Real model + extraction + retrieval smoke test in a disposable, isolated index."""
import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
engine = repo / 'target/release/recall-engine'
with tempfile.TemporaryDirectory(prefix='recall-smoke-') as temporary:
    base = Path(temporary)
    files = base / 'files'
    files.mkdir()
    (files / 'travel.md').write_text('The Japan holiday starts in Tokyo. Take the train to Kamakura to see the Great Buddha. Dinner is in Shibuya.')
    (files / 'cooking.md').write_text('Bake the sourdough bread in a hot oven for forty minutes.')
    shutil.copy2(repo / 'tests/reference/photo.jpg', files / 'photo.jpg')
    shutil.copy2(repo / 'tests/reference/speech.wav', files / 'speech.wav')
    code = base / 'code'
    code.mkdir()
    (code / 'backoff.py').write_text('import time\n\ndef retry_with_backoff(operation, attempts=4):\n    """Retry a failed operation with exponentially increasing delays."""\n    for attempt in range(attempts):\n        try:\n            return operation()\n        except OSError:\n            time.sleep(2 ** attempt)\n    raise RuntimeError("all retries failed")\n')
    index = base / 'index'
    def run(*args):
        return subprocess.run([str(engine), *args, '--index', str(index)], check=True, capture_output=True, text=True)
    run('index', str(files))
    run('index', '--code', str(code))
    with sqlite3.connect(index / 'index.sqlite') as connection:
        states = dict(connection.execute('select kind, state from files'))
        assert states.get('audio') == 'done', states
        assert states.get('image') == 'done', states
    reply = json.loads(run('search', 'Japanese vacation with a temple visit', '--json', '--zgate', '0').stdout)
    hits = reply if isinstance(reply, list) else reply['hits']
    assert hits and hits[0]['path'].endswith('travel.md'), hits
    reply = json.loads(run('search', 'code: retry an operation using exponential delays', '--json', '--zgate', '0').stdout)
    hits = reply if isinstance(reply, list) else reply['hits']
    assert hits and hits[0]['path'].endswith('backoff.py'), hits
    print('PASS: real text/image/audio indexing and semantic document/code retrieval')
