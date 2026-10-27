#!/usr/bin/env python3
"""One no-QR restore after inspecting a dead Native and complete snapshots."""
import importlib.util
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent


def main():
    if os.geteuid() != 0 or sys.argv[1:]:
        return 2
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    spec = importlib.util.spec_from_file_location('_reviewed_session', ROOT / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core, owner = formal.core, formal.core.owner
    require = core.require
    require(not owner.owner_active(), 'owner_running')
    old = owner.read_json(owner.CONTROL / 'status.json')
    require(old.get('code') == 'native_exited' and old.get('accounts') == 1, 'unexpected_old_state')
    cleanup = old.get('cleanup', {})
    require(cleanup.get('host_namespace_unchanged') is True and cleanup.get('host_network_unchanged') is True,
            'old_host_cleanup_requires_review')
    records = [r.get('record', r) for r in old.get('records', []) if isinstance(r, dict)]
    require(any(r.get('stage') == 'runtime_stopped' and r.get('ok') is True for r in records),
            'old_runtime_cleanup_requires_review')
    units = subprocess.run(['/usr/bin/systemctl', 'list-units', '--all', '--plain', '--no-legend',
        'astrbot-wechat-login-*.service', 'astrbot-wechat-slirp-tap-helper-*.service'],
        capture_output=True, timeout=4)
    require(units.returncode == 0 and not units.stdout.strip(), 'old_native_units_remain')
    require(owner.read_json(owner.STATE / 'forward-send.json').get('state') == 'accepted', 'send_requires_review')
    store = owner.companion('session_state_store')
    latest = store.SnapshotStore(owner.STATE / 'checkpoints').latest()
    require(latest is not None, 'saved_session_missing')
    with zipfile.ZipFile(latest, 'r') as archive:
        _, count, size = store._archive_manifest(archive)
    require(count > 0 and size > 0, 'saved_session_empty')
    previous = owner.STATE / 'pre-image-output-session-exit.json'
    require(not previous.exists(), 'restore_already_attempted')
    host = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    owner.write_json(previous, old)
    owner.start_owner(False, seconds=owner.LONG_RUNTIME_SECONDS)
    # Existing status calls explicitly forbid fresh login; no initialize path.
    core.trial.ensure_session()
    status = owner.read_json(owner.CONTROL / 'status.json')
    flags, failures = core.tap.compare_baselines(host, network, business)
    require(not failures and status.get('qrVersion') == 0 and status.get('connected') is True,
            'restored_session_requires_review')
    print(json.dumps({'stage': 'reviewed_session_restored', 'ok': True, 'new_qr': False,
                      'snapshot_files': count, 'snapshot_bytes': size, **flags}, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'stage': 'reviewed_session_restore_failed', 'ok': False,
                          'code': getattr(error, 'stage', getattr(error, 'code', 'restore_failed'))}))
        raise SystemExit(1)
