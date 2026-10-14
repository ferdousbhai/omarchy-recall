#!/usr/bin/python3
"""Compare warmed CPU/Vulkan inference on public fixtures, without touching the live index."""
import base64
import json
import math
import os
import statistics
import struct
import subprocess
import tempfile
import time
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
results, vectors = {}, {}
for backend in ('cpu', 'vulkan'):
    with tempfile.TemporaryFile(mode='w+') as log:
        worker = subprocess.Popen([str(repo / 'target/release/recall'), 'worker'], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=log, text=True,
                                  env=dict(os.environ, RECALL_DEVICE=backend))
        try:
            ready = json.loads(worker.stdout.readline())
            assert ready.get('ready'), ready
            device = ready['info']['device']
            assert (backend == 'cpu' and device == 'CPU') or (backend == 'vulkan' and device.startswith('Vulkan')), device
            metrics = {'device': device, 'load_seconds': ready['info']['load_seconds']}
            cases = {
                'text': {'op': 'text', 'inputs': ['task: search result | query: a dog playing on the beach']},
                'image': {'op': 'image', 'inputs': [str(repo / 'tests/reference/photo.jpg')], 'budget': 140},
                'audio': {'op': 'audio', 'inputs': [str(repo / 'tests/reference/speech.wav')]},
            }
            for name, case in cases.items():
                elapsed = []
                for iteration in range(4):
                    start = time.monotonic()
                    worker.stdin.write(json.dumps({'id': iteration, **case}) + '\n'); worker.stdin.flush()
                    reply = json.loads(worker.stdout.readline())
                    elapsed.append(time.monotonic() - start)
                    assert reply.get('ok') and not any(reply.get('errors', [])), reply
                raw = base64.b64decode(reply['vectors'])
                vectors[backend, name] = struct.unpack('<' + 'f' * (len(raw) // 4), raw)
                metrics[name + '_seconds'] = round(statistics.median(elapsed[1:]), 4)
            results[backend] = metrics
        finally:
            worker.stdin.close()
            worker.wait(timeout=30)
for name in ('text', 'image', 'audio'):
    a, b = vectors['cpu', name], vectors['vulkan', name]
    cosine = sum(x*y for x, y in zip(a, b)) / math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    assert cosine >= 0.999, (name, cosine)
    results[name + '_agreement'] = round(cosine, 6)
    results[name + '_speedup'] = round(results['cpu'][name + '_seconds'] / results['vulkan'][name + '_seconds'], 2)
print(json.dumps(results, indent=2))
