#!/usr/bin/env python3
"""Promote the all-group bridge release, preserving the Native login and state."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
STAGE = ROOT / 'all-groups-staging'
FLAT = {'run_private_forwarder.py', 'forward_native_worker.py', 'forward_host_worker.py',
        'group_trigger.py', 'forward_config.py'}
PACKAGE = {'continuous.py', 'forward_gateway.py', 'protocol.py', 'state.py', 'group_trigger.py', 'forward_config.py'}
TESTS = {'test_onebot_all_groups.py', 'test_onebot_native_forward.py'}
ALLOWED = (FLAT | {'verification/scripts/' + n for n in FLAT}
           | {'verification/wechat_bridge/' + n for n in PACKAGE}
           | {'verification/tests/' + n for n in TESTS})


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def main():
    assert sys.argv[1:] in (['inspect'], ['apply']) and os.geteuid() == 0, 'root_required'
    helper = load('_all_groups_deploy_helpers', ROOT / 'deploy_group_context.py')
    old = load('_all_groups_old_entry', ROOT / 'run_forwarder.py')
    core = old.core
    read, replace = helper.read, helper.replace
    snapshot = lambda: json.loads(read(core.STATUS)[0])
    current = snapshot()
    manifest = json.loads(read(STAGE / 'manifest.json')[0])
    assert set(manifest) == ALLOWED, 'unexpected_release_files'
    changes = []
    for name in sorted(ALLOWED):
        source, target = STAGE / name, ROOT / name
        assert not any(p.is_symlink() for p in (source, *source.parents, target, *target.parents))
        raw, _ = read(source)
        assert hashlib.sha256(raw).hexdigest() == manifest[name], 'release_digest'
        ast.parse(raw)
        prior, info = read(target) if target.exists() else (None, None)
        changes.append((name, target, raw, prior, info))
    public = ('active', 'ready', 'code', 'received', 'forwarded', 'completed_replies', 'uncertain',
              'queue', 'sse', 'astrbot_connected', 'all_groups_enabled', 'new_qr', 'self_identity_verified')
    if sys.argv[1] == 'inspect':
        print(json.dumps({'stage': 'all_groups_review', 'files': len(changes),
                          **{k: current.get(k) for k in public}}))
        return
    assert current.get('active') is True and current.get('ready') is True, 'bridge_not_ready'
    assert current.get('uncertain') == 0 and not current.get('image_egress_active'), 'pending_send'
    assert not any(current.get('queue', {}).get(k) for k in ('queued', 'publishing', 'uncertain')), 'pending_input'
    native = core.groups.PinnedNative()
    native.open()
    host = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    config = ROOT.parent / 'forwarder.json'
    config_raw, config_info = read(config)
    backup = ROOT / ('all-groups-backup-' + time.strftime('%Y%m%d-%H%M%S'))
    stopped, promoted = False, False
    def run(action):
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(ROOT / 'run_forwarder.py'), action],
                                capture_output=True, timeout=40)
        assert result.returncode == 0, 'bridge_command_' + action
    try:
        backup.mkdir(mode=0o700)
        for name, _, _, prior, _ in changes:
            if prior is not None:
                path = backup / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(prior)
                path.chmod(0o600)
        (backup / 'forwarder.json').write_bytes(config_raw)
        (backup / 'forwarder.json').chmod(0o600)
        run('stop')
        stopped = True
        receipt = snapshot()
        assert receipt.get('cleanup_ok') is True and receipt.get('native_session_unchanged') is True
        assert receipt.get('uncertain') == 0
        assert not any(receipt.get('queue', {}).get(k) for k in ('queued', 'publishing', 'uncertain'))
        for _, path, raw, _, info in changes:
            replace(path, raw, info)
        promoted = True
        updated = {**json.loads(config_raw), 'allGroupsEnabled': True}
        replace(config, json.dumps(updated, ensure_ascii=True, separators=(',', ':')).encode(), config_info)
        run('start')
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            current = snapshot()
            if current.get('ready') is True and current.get('all_groups_enabled') is True:
                break
            time.sleep(.5)
        assert current.get('ready') is True and current.get('all_groups_enabled') is True, 'all_groups_not_ready'
        assert current.get('uncertain') == 0 and current.get('new_qr') is False
        assert current.get('self_identity_verified') is True and native.unchanged()
        flags, failures = core.tap.compare_baselines(host, network, business)
        assert not failures, 'runtime_baseline_changed'
        print(json.dumps({'stage': 'all_groups_deployed', 'ok': True, 'backup': str(backup),
                          'native_session_unchanged': True, **flags, **{k: current.get(k) for k in public}}))
    except Exception:
        now = snapshot()
        clean = now.get('uncertain', 0) == 0 and not any(now.get('queue', {}).get(k) for k in ('queued', 'publishing', 'uncertain'))
        if stopped and clean and (not promoted or now.get('received', 0) == 0):
            subprocess.run(['/usr/bin/systemctl', 'stop', core.UNIT], capture_output=True, timeout=30)
            for _, path, _, prior, info in changes:
                if prior is not None:
                    replace(path, prior, info)
                elif path.exists():
                    path.unlink()
            replace(config, config_raw, config_info)
            run('start')
            print(json.dumps({'stage': 'all_groups_rollback', 'ok': True}))
        raise
    finally:
        native.close()


if __name__ == '__main__':
    main()
