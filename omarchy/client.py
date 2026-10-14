#!/usr/bin/python3
"""JSON-lines transport for QML, or a one-shot command for scripts."""
import json
import os
import socket
import sys
from pathlib import Path


def request(payload):
    path = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'omarchy-recall.sock'
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(115)
            connection.connect(str(path))
            connection.sendall((json.dumps(payload) + '\n').encode())
            with connection.makefile('rb') as stream:
                line = stream.readline(10485761)
                if not line or len(line) > 10485760:
                    raise ValueError('Invalid response from search service')
                return json.loads(line)
    except (OSError, ValueError) as error:
        return {'id': payload.get('id'), 'error': f'Search service unavailable: {error}'}


if __name__ == '__main__':
    if len(sys.argv) > 1:
        op = sys.argv[1]
        payload = {'op': op}
        if op == 'search':
            payload['query'] = ' '.join(sys.argv[2:])
        if op == 'pause':
            payload['paused'] = len(sys.argv) < 3 or sys.argv[2] != 'off'
        print(json.dumps(request(payload)))
    else:
        for line in sys.stdin:
            try:
                payload = json.loads(line)
                reply = request(payload)
            except (ValueError, TypeError) as error:
                reply = {'error': str(error)}
            print(json.dumps(reply), flush=True)
