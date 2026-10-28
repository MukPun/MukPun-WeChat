#!/usr/bin/env python3
"""Restore a clean saved account after unexpected Native exit, without QR."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


def main():
    assert os.geteuid() == 0 and sys.argv[1:] == ['apply']
    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('_stopped_restore', root / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core = formal.core
    assert not core.owner.owner_active(), 'owner_still_running'
    state = core.owner.read_json(core.owner.CONTROL / 'status.json')
    assert state.get('code') in {'native_exited', 'outbox_review_failed'} and state.get('accounts') in (0, 1)
    assert core.owner.cleanup_ok(state.get('cleanup')), 'unclean_native_exit'
    store = core.owner.companion('session_state_store').SnapshotStore(core.owner.STATE / 'checkpoints')
    assert store.latest() is not None, 'snapshot_missing'
    assert core.owner.read_json(core.owner.STATE / 'forward-send.json').get('state') == 'accepted'
    image_path = core.owner.STATE / 'forward-image-send.json'
    if image_path.exists():
        assert core.owner.read_json(image_path).get('state') in {'accepted', 'rejected'}
    database = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve(strict=True)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.2) as connection:
        connection.execute('PRAGMA query_only=ON')
        counts = dict(connection.execute('SELECT state,COUNT(*) FROM inbox GROUP BY state'))
    assert not any(counts.get(key, 0) for key in ('queued', 'publishing', 'uncertain'))
    def run(script, action, timeout):
        print(json.dumps({'stage': 'stopped_restore_step', 'action': action}), flush=True)
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(root / script), action],
                                capture_output=True, timeout=timeout)
        assert result.returncode == 0, 'restore_' + action + '_failed'
    run('session_runtime.py', 'start-long', 45)
    state = core.owner.read_json(core.owner.CONTROL / 'status.json')
    assert state.get('ready') is True and state.get('accounts') == 1
    if state.get('connected') is not True:
        run('session_runtime.py', 'resume', 180)
    run('run_forwarder.py', 'start', 40)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = core.owner.read_json(core.STATUS)
        if state.get('ready') is True:
            break
        time.sleep(.5)
    print(json.dumps({'stage': 'stopped_restore_result', **{key: state.get(key) for key in
        ('active', 'ready', 'code', 'sse', 'astrbot_connected', 'uncertain', 'received', 'forwarded', 'queue', 'new_qr')}}), flush=True)
    assert state.get('ready') is True and state.get('new_qr') is False


if __name__ == '__main__':
    main()
