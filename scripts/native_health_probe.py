"""Read-only Native account health probe for the continuous forwarder.

The bridge's own readiness signals stay healthy when the WeChat connection
itself dies: the SSE stream only talks to the loopback API of a Native server
that keeps answering, and the OneBot reverse socket only reflects AstrBot. A
silently dropped WeChat connection therefore stayed invisible until a user
noticed that messages stopped arriving.

Namespace entry is per-thread on Linux, so this module never forks a child: a
short-lived thread enters the pinned handle's network namespace, performs one
bounded GET on the account resource and exits. The main thread keeps the host
namespace, and the API token crosses a Python argument, never argv, env, a file
or a subprocess pipe.

It only reads: it never connects, resumes, logs in, sends, cancels, forgets
credentials or changes any lifecycle state.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
ACCOUNT = 'wechat-primary'
CONNECTIONS = ('online', 'reconnecting', 'disconnected')
CONNECTED = 'online'
INTERVAL_SECONDS = 30.0
READ_TIMEOUT_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 8.0
MAX_EXCHANGE = 4096


def companion(name, path):
    spec = importlib.util.spec_from_file_location('_health_probe_' + name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


contact = companion('contact', HERE / 'contact_receive_protocol.py')


def read_account(lookup, deadline):
    """One authorized GET; no other Native route is ever touched."""
    path = lookup.policy.prefix
    lookup.policy.path = path
    value = lookup._get(path, deadline, timeout=REQUEST_TIMEOUT_SECONDS)
    authenticated = value.get('authenticated')
    connection = value.get('connection')
    if value.get('id') != ACCOUNT or type(authenticated) is not bool or connection not in CONNECTIONS:
        return {'ok': False, 'code': 'invalid_response'}
    return {'ok': True,
            'authenticated': authenticated,
            'connection': connection,
            'connected': authenticated is True and connection == CONNECTED}


def probe_once(pinned):
    """Read the account state from inside the pinned network namespace."""
    outcome = {}

    def work():
        try:
            os.setns(pinned.netfd, os.CLONE_NEWNET)
        except Exception:
            # A closed or replaced handle stays a reported outcome, never a crash.
            outcome['record'] = {'ok': False, 'code': 'probe_namespace_failed'}
            return
        try:
            token = pinned.token()
        except Exception:
            outcome['record'] = {'ok': False, 'code': 'probe_token_unavailable'}
            return
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        try:
            lookup = contact.ContactReceiveClient(ACCOUNT, token)
            outcome['record'] = read_account(lookup, deadline)
        except contact.ContactError as error:
            outcome['record'] = {'ok': False, 'code': error.code}
        except Exception:
            outcome['record'] = {'ok': False, 'code': 'probe_failed'}

    thread = threading.Thread(target=work, name='native-health-read', daemon=True)
    thread.start()
    thread.join(timeout=READ_TIMEOUT_SECONDS + 1.0)
    record = outcome.get('record')
    if thread.is_alive() or not isinstance(record, dict):
        return {'ok': False, 'code': 'probe_timeout'}
    return record


class Probe:
    """Publish the last known account state; never assume it is healthy."""

    def __init__(self, pinned, interval=INTERVAL_SECONDS):
        self.pinned = pinned
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._state = {'native_connected': False,
                       'native_connection': 'unchecked',
                       'native_probe_code': 'starting',
                       'native_checked_at': 0}

    def _store(self, record):
        value = {'native_checked_at': int(time.time()),
                 'native_probe_code': 'ok' if record.get('ok') is True else str(record.get('code'))}
        if record.get('ok') is True:
            value['native_connected'] = record.get('connected') is True
            value['native_connection'] = record.get('connection')
        else:
            # An unreadable state is never evidence of a healthy account.
            value['native_connected'] = False
            value['native_connection'] = 'unreachable'
        with self._lock:
            self._state.update(value)

    def _read(self):
        self._store(probe_once(self.pinned))

    def start(self):
        # One synchronous read so the first published status already carries truth.
        self._read()
        self._thread = threading.Thread(target=self._run, name='native-health', daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval):
            self._read()

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def main():
    """Standalone mode for manual inspection: one read, token on stdin."""
    if sys.argv[1:] != ['--read']:
        return 2
    try:
        value = json.loads(sys.stdin.buffer.read(MAX_EXCHANGE + 1))
    except Exception:
        return 2
    if not isinstance(value, dict) or set(value) != {'token'}:
        return 2
    try:
        lookup = contact.ContactReceiveClient(ACCOUNT, value['token'])
        record = read_account(lookup, time.monotonic() + REQUEST_TIMEOUT_SECONDS)
    except contact.ContactError as error:
        record = {'ok': False, 'code': error.code}
    except Exception:
        record = {'ok': False, 'code': 'probe_failed'}
    print(json.dumps(record, sort_keys=True), flush=True)
    return 0 if record.get('ok') is True else 1


if __name__ == '__main__':
    raise SystemExit(main())
