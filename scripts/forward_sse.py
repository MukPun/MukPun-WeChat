"""Read-only Native SSE subscription; reconnect only after durable cursor ACK.

No event polling, login, connection lifecycle or historical sync APIs.
"""
from datetime import datetime
import http.client
import hashlib
import json
import queue
import threading
import time
import re

MAX_EVENT = 65536


class StreamError(Exception):
    pass


def frames(response):
    identity, kind, data, size = None, 'message', [], 0
    while True:
        raw = response.readline(MAX_EVENT + 1)
        if not raw:
            return
        if len(raw) > MAX_EVENT:
            raise StreamError('sse_line_size')
        if not raw.endswith(b'\n'):
            return  # EOF mid-frame: reconnect from the last acknowledged ID.
        size += len(raw)
        if size > MAX_EVENT:
            raise StreamError('sse_event_size')
        line = raw.rstrip(b'\r\n').decode('utf-8')
        if not line:
            if data:
                yield identity, kind, json.loads('\n'.join(data))
            identity, kind, data, size = None, 'message', [], 0
        elif line.startswith(':'):
            # Keepalive comments have no payload and cannot grow indefinitely.
            if not data:
                size = 0
        else:
            field, _, value = line.partition(':')
            if value.startswith(' '):
                value = value[1:]
            if field == 'id':
                identity = value
            elif field == 'event':
                kind = value
            elif field == 'data':
                data.append(value)


def cursor(value):
    if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,18}', value) or int(value) > 2**63 - 1:
        raise StreamError('invalid_cursor')
    return value


def event_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def verify_checkpoint(reader, after, anchor, deadline):
    cursor(after)
    if not isinstance(anchor, str) or not re.fullmatch('[0-9a-f]{64}', anchor) or int(after) == 0:
        raise StreamError('invalid_recovery_anchor')
    # A single recovery lookup, never an event-polling receive loop. A restored
    # SDK snapshot can have reused cursor numbers; verify content, not just ID.
    page = reader._get(reader.policy.events(str(int(after) - 1)), deadline, timeout=10)
    items = page.get('items')
    if (not isinstance(items, list) or not items or not isinstance(items[0], dict)
            or items[0].get('cursor') != after or event_digest(items[0]) != anchor):
        raise StreamError('recovery_anchor_mismatch')


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise StreamError('invalid_time')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise StreamError('invalid_time')
    return int(parsed.timestamp())


class Pump:
    def __init__(self, account, token, after, deadline, *, port=26174):
        if account != 'wechat-primary' or not 32 <= len(token) <= 1024 or not all(33 <= ord(c) <= 126 for c in token):
            raise StreamError('config')
        self.account, self.token, self.after, self.deadline, self.port = account, token, cursor(after), deadline, port
        self.items = queue.Queue(maxsize=1)
        self.advanced = threading.Event()
        self.stopped = threading.Event()
        self.connection = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.reconnects = 0
        self.connected = threading.Event()

    def ack(self, value):
        self.after = cursor(value)
        self.advanced.set()

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.advanced.set()
        if self.connection:
            self.connection.close()

    def run(self):
        delay = .5
        while not self.stopped.is_set() and time.monotonic() < self.deadline:
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
            self.connection = connection
            try:
                path = '/v1/accounts/' + self.account + '/events/stream?after=' + self.after
                connection.request('GET', path, headers={'Authorization': 'Bearer ' + self.token,
                    'Accept': 'text/event-stream', 'Accept-Encoding': 'identity'})
                response = connection.getresponse()
                if (response.status != 200 or response.getheader('Content-Type', '').split(';')[0] != 'text/event-stream'
                        or response.getheader('Content-Encoding', 'identity') != 'identity'):
                    if response.status in {401, 403, 404}:
                        self.items.put({'error': 'sse_endpoint_rejected'})
                        return
                    raise StreamError('sse_response')
                self.connected.set()
                for identity, kind, data in frames(response):
                    if self.stopped.is_set():
                        return
                    if kind == 'error':
                        # Expired cursors must be visible. Never skip to newest.
                        self.items.put({'error': 'sse_cursor_or_server_error'})
                        return
                    current = cursor(identity)
                    if not isinstance(data, dict) or data.get('cursor') != current or int(current) <= int(self.after):
                        raise StreamError('sse_cursor')
                    self.advanced.clear()
                    self.items.put(data)
                    while not self.advanced.wait(.2):
                        if self.stopped.is_set() or time.monotonic() >= self.deadline:
                            return
                    delay = .5
            except Exception:
                self.reconnects += 1
            finally:
                self.connected.clear()
                connection.close()
            self.stopped.wait(delay)
            delay = min(delay * 2, 5)
