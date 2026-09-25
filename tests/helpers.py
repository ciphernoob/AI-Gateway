import json
import os
import socket
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def test_environment():
    return dict(line.split('=', 1) for line in (ROOT / '.env.example').read_text().splitlines()
                if line and not line.startswith('#'))


def http(url, data=None, key=None, timeout=12):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = Request(url, data=None if data is None else json.dumps(data).encode(), headers=headers)
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode()
        return response.status, dict(response.headers), body


def eventually(fn, predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while True:
        value = fn()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise AssertionError('Timed out waiting for expected state: ' + str(value)[:300])
        time.sleep(.1)


class Redis:
    """Small RESP2 test/operator client; no third party runtime dependency."""
    def __init__(self, host='redis', port=6379, password=None):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.file = self.sock.makefile('rb')
        if password:
            self.call('AUTH', password)

    def call(self, *parts):
        encoded = [str(x).encode() if not isinstance(x, bytes) else x for x in parts]
        wire = b'*%d\r\n' % len(parts) + b''.join(b'$%d\r\n' % len(x) + x + b'\r\n' for x in encoded)
        self.sock.sendall(wire)
        return self.read()

    def read(self):
        line = self.file.readline()
        if not line:
            raise ConnectionError('Redis disconnected')
        kind, value = line[:1], line[1:-2]
        if kind == b'-':
            raise RuntimeError(value.decode())
        if kind == b'+':
            return value.decode()
        if kind == b':':
            return int(value)
        size = int(value)
        if size < 0:
            return None
        if kind == b'*':
            return [self.read() for _ in range(size)]
        data = self.file.read(size)
        self.file.read(2)
        return data.decode()

    def close(self):
        self.file.close()
        self.sock.close()
