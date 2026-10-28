#!/usr/bin/env python3
"""Deploy only nickname enrichment, retaining the online Native and config."""
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
STAGE = ROOT / 'nickname-staging'
FLAT = {'forward_native_worker.py', 'run_private_forwarder.py', 'sender_names.py'}
PACKAGE = {'continuous.py', 'protocol.py', 'state.py', 'durable.py', 'sender_names.py'}
TESTS = {'test_onebot_sender_names.py', 'test_onebot_native_forward.py'}
ALLOWED = FLAT | {'verification/scripts/' + name for name in FLAT} | {
    'verification/wechat_bridge/' + name for name in PACKAGE} | {'verification/tests/' + name for name in TESTS}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def main():
    assert os.geteuid() == 0 and sys.argv[1:] in (['inspect'], ['apply'])
    helper = load('_nickname_deploy_helpers', ROOT / 'deploy_group_context.py')
    formal = load('_nickname_deploy_formal', ROOT / 'run_forwarder.py')
    core, read, replace = formal.core, helper.read, helper.replace
    snapshot = lambda: json.loads(read(core.STATUS)[0])
    current = snapshot()
    manifest = json.loads(read(STAGE / 'manifest.json')[0])
    assert set(manifest) == ALLOWED
    changes = []
    for name in sorted(ALLOWED):
        source, target = STAGE / name, ROOT / name
        assert not any(path.is_symlink() for path in (source, *source.parents, target, *target.parents))
        raw, _ = read(source)
        assert hashlib.sha256(raw).hexdigest() == manifest[name]
        ast.parse(raw)
        prior, info = read(target) if target.exists() else (None, None)
        changes.append((name, target, raw, prior, info))
    public = ('active', 'ready', 'code', 'sse', 'astrbot_connected', 'received', 'forwarded', 'uncertain', 'queue')
    if sys.argv[1:] == ['inspect']:
        print(json.dumps({'stage': 'sender_names_review', 'files': len(changes), **{key: current.get(key) for key in public}}))
        return
    assert current.get('ready') is True and current.get('active') is True
    assert current.get('uncertain') == 0 and not current.get('image_egress_active')
    assert not any(current.get('queue', {}).get(key) for key in ('queued', 'publishing', 'uncertain'))
    native = core.groups.PinnedNative()
    native.open()
    baseline = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    config = ROOT.parent / 'forwarder.json'
    config_raw, _ = read(config)
    backup = ROOT / ('sender-names-backup-' + time.strftime('%Y%m%d-%H%M%S'))
    stopped, promoted = False, False
    def run(action):
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(ROOT / 'run_forwarder.py'), action],
                                capture_output=True, timeout=40)
        assert result.returncode == 0, 'bridge_command_' + action
    try:
        backup.mkdir(mode=0o700)
        for name, _, _, prior, _ in changes:
            if prior is not None:
                target = backup / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(prior)
                target.chmod(0o600)
        run('stop')
        stopped = True
        receipt = snapshot()
        assert receipt.get('cleanup_ok') is True and receipt.get('native_session_unchanged') is True
        assert receipt.get('uncertain') == 0 and not any(receipt.get('queue', {}).get(key) for key in ('queued', 'publishing', 'uncertain'))
        for _, target, raw, _, info in changes:
            replace(target, raw, info)
        promoted = True
        run('start')
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            current = snapshot()
            if current.get('ready') is True:
                break
            time.sleep(.5)
        assert current.get('ready') is True and current.get('self_identity_verified') is True
        assert current.get('uncertain') == 0 and current.get('new_qr') is False
        assert native.unchanged() and read(config)[0] == config_raw
        flags, failures = core.tap.compare_baselines(baseline, network, business)
        assert not failures
        print(json.dumps({'stage': 'sender_names_deployed', 'ok': True, 'backup': str(backup),
                          'native_session_unchanged': True, 'configuration_unchanged': True,
                          **flags, **{key: current.get(key) for key in public}}))
    except Exception:
        current = snapshot()
        clean = current.get('uncertain', 0) == 0 and not any(current.get('queue', {}).get(key) for key in ('queued', 'publishing', 'uncertain'))
        if stopped and clean and (not promoted or current.get('received', 0) == 0):
            subprocess.run(['/usr/bin/systemctl', 'stop', core.UNIT], capture_output=True, timeout=30)
            for _, target, _, prior, info in changes:
                if prior is None:
                    target.unlink(missing_ok=True)
                else:
                    replace(target, prior, info)
            run('start')
            print(json.dumps({'stage': 'sender_names_rollback', 'ok': True}))
        raise
    finally:
        native.close()


if __name__ == '__main__':
    main()
