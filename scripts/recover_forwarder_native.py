#!/usr/bin/env python3
"""Recover only this project's stuck Native and bridge using saved credentials."""
import hashlib
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
    spec = importlib.util.spec_from_file_location('_native_recovery_formal', root / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    formal.load_config()
    core = formal.core
    def emit(stage, **fields):
        print(json.dumps({'stage': stage, **fields}), flush=True)
    state = core.owner.read_json(core.STATUS)
    assert state.get('active') is False and state.get('cleanup_ok') is True
    assert state.get('native_session_unchanged') is True and state.get('uncertain') == 0
    text_audit = core.owner.read_json(core.owner.STATE / 'forward-send.json')
    assert text_audit.get('state') == 'accepted', 'pending_text_send'
    image_path = core.owner.STATE / 'forward-image-send.json'
    if image_path.exists():
        assert core.owner.read_json(image_path).get('state') in {'accepted', 'rejected'}, 'pending_image_send'
    database = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve(strict=True)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.2) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        counts = dict(connection.execute('SELECT state,COUNT(*) FROM inbox GROUP BY state'))
    assert not any(counts.get(key, 0) for key in ('queued', 'publishing', 'uncertain')), 'pending_input'
    pinned = core.groups.PinnedNative()
    pinned.open()
    try:
        token_digest = hashlib.sha256(pinned.token().encode()).digest()
    finally:
        pinned.close()
    baseline = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    def run(script, action, timeout):
        emit('recovery_step', action=action)
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(root / script), action],
                                capture_output=True, timeout=timeout)
        assert result.returncode == 0, 'recovery_' + action + '_failed'
    run('session_runtime.py', 'stop', 40)
    stopped = core.owner.read_json(core.owner.CONTROL / 'status.json')
    assert stopped.get('ok') is True and core.owner.cleanup_ok(stopped.get('cleanup')), 'native_stop_unclean'
    run('session_runtime.py', 'start-long', 45)
    restored = core.owner.read_json(core.owner.CONTROL / 'status.json')
    assert restored.get('accounts') == 1 and restored.get('ready') is True, 'saved_account_not_restored'
    if restored.get('connected') is not True:
        run('session_runtime.py', 'resume', 180)
    pinned = core.groups.PinnedNative()
    pinned.open()
    try:
        assert hashlib.sha256(pinned.token().encode()).digest() == token_digest, 'saved_identity_changed'
    finally:
        pinned.close()
    run('run_forwarder.py', 'restart', 40)
    deadline = time.monotonic() + 70
    while time.monotonic() < deadline:
        state = core.owner.read_json(core.STATUS)
        if state.get('ready') is True:
            break
        time.sleep(1)
    public = {key: state.get(key) for key in ('active', 'ready', 'code', 'sse', 'astrbot_connected',
        'received', 'forwarded', 'completed_replies', 'uncertain', 'queue', 'all_groups_enabled',
        'self_identity_verified', 'new_qr')}
    flags, failures = core.tap.compare_baselines(baseline, network, business)
    emit('forwarder_recovery_result', **public, saved_identity_preserved=True, **flags)
    assert not failures and state.get('ready') is True, 'bridge_not_ready'
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'stage': 'forwarder_recovery_failed', 'error_class': type(error).__name__,
                          'code': str(error) if isinstance(error, AssertionError) else 'recovery_failed'}), flush=True)
        raise SystemExit(1)
