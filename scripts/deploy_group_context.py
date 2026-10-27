#!/usr/bin/env python3
"""Promote only the reviewed bridge files; preserve Native/Docker/FRP identity."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
STAGE = ROOT / 'context-staging'
BACKUP = ROOT / 'context-backup-20261026'
ENTRY = ROOT / 'run_forwarder.py'
STEP = 'startup'


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 4 * 1024 * 1024
        return os.read(fd, 4 * 1024 * 1024 + 1), info
    finally:
        os.close(fd)


def replace(path, raw, info=None):
    target = path.with_name(path.name + '.context-next')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
        os.fchmod(fd, stat.S_IMODE(info.st_mode) if info else 0o644)
        if info:
            os.fchown(fd, info.st_uid, info.st_gid)
    finally:
        os.close(fd)
    os.replace(target, path)


def run(*args):
    result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(ENTRY), *args],
                            capture_output=True, timeout=40)
    assert result.returncode == 0, 'owned_bridge_command_failed'


def main():
    global STEP
    args = sys.argv[1:]
    assert os.geteuid() == 0 and args in (['inspect'], ['apply'], ['inspect', '--output-policy'], ['apply', '--output-policy'],
                                        ['inspect', '--image-output'], ['apply', '--image-output'],
                                        ['inspect', '--image-receipt'], ['apply', '--image-receipt'],
                                        ['inspect', '--image-chunks'], ['apply', '--image-chunks'],
                                        ['inspect', '--image-compat'], ['apply', '--image-compat'])
    output_policy = args[-1] in {'--output-policy', '--image-output', '--image-receipt', '--image-chunks', '--image-compat'}
    image_output = args[-1] in {'--image-output', '--image-receipt', '--image-chunks', '--image-compat'}
    backup_name = ('image-compat-backup-20261027' if args[-1] == '--image-compat' else
                   'image-chunks-backup-20261027' if args[-1] == '--image-chunks' else
                   'image-receipt-backup-20261027' if args[-1] == '--image-receipt' else
                   'image-output-backup-20261027' if image_output else 'output-policy-backup-20261027')
    backup = ROOT / backup_name if output_policy else BACKUP
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    spec = importlib.util.spec_from_file_location('_context_deploy_old', ENTRY)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    core = old.core
    snapshot = lambda: json.loads(read(core.STATUS)[0])
    current = snapshot()
    public = ('active', 'ready', 'code', 'updated', 'ok', 'cleanup_ok', 'sse', 'astrbot_connected', 'received', 'forwarded', 'completed_replies',
              'uncertain', 'queue', 'group_context_enabled', 'group_traffic', 'group_wake_mode', 'new_qr',
              'image_input_enabled', 'image_egress_active', 'remaining_seconds', 'self_identity_verified', 'unsupported_actions',
              'image_output_enabled', 'image_output_requests', 'image_output_sent', 'image_output_rejected', 'onebot_link', 'last_output_error')
    if args[0] == 'inspect':
        info = core.tap.details(core.UNIT)
        print(json.dumps({'stage': 'context_status', **{k: current.get(k) for k in public},
                          'service': {k: info.get(k) for k in ('ActiveState', 'SubState', 'MainPID', 'Result')}}))
        return
    STEP = 'live_preflight'
    assert current.get('active') is True and current.get('ready') is True
    assert current.get('uncertain') == 0 and not current.get('image_egress_active')
    assert not any(current.get('queue', {}).get(k) for k in ('queued', 'publishing', 'uncertain'))
    flat = {'run_forwarder.py', 'run_private_forwarder.py', 'forward_native_worker.py',
            'forward_host_worker.py', 'group_trigger.py', 'forward_config.py'}
    package = {'continuous.py', 'forward_gateway.py', 'group_trigger.py', 'forward_config.py'}
    tests = {'test_onebot_context.py', 'test_onebot_native_forward.py', 'test_onebot_continuous_wire.py'}
    if output_policy:
        package.add('protocol.py')
        tests.add('test_onebot_output_policy.py')
    if image_output:
        flat |= {'image_output.py', 'image_send_native.py', 'image_output_budget.py'}
        package |= {'image_output.py', 'transport.py'}
        tests |= {'test_onebot_action_transport.py', 'test_onebot_image_output.py', 'test_onebot_image_output_native.py',
                  'test_onebot_image_output_budget.py'}
    allowed = flat | {'verification/wechat_bridge/' + n for n in package}
    allowed |= {'verification/scripts/' + n for n in flat} | {'verification/tests/' + n for n in tests}
    manifest = json.loads(read(STAGE / 'manifest.json')[0])
    STEP = 'manifest'
    assert set(manifest) == allowed
    changes = []
    STEP = 'staged_files'
    for name in sorted(allowed):
        source = STAGE / name
        assert not any(p.is_symlink() for p in (source, *source.parents))
        raw, _ = read(source)
        assert hashlib.sha256(raw).hexdigest() == manifest[name], 'stage_digest_mismatch'
        ast.parse(raw)
        target = ROOT / name
        assert target.parent.is_dir() and not any(p.is_symlink() for p in target.parents)
        prior, info = read(target) if target.exists() else (None, None)
        changes.append((name, target, raw, prior, info))
    native = core.groups.PinnedNative()
    native.open()
    host = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    config = ROOT.parent / 'forwarder.json'
    config_raw, config_info = read(config)
    stopped = False
    promoted = False
    try:
        STEP = 'backup'
        backup.mkdir(mode=0o700, exist_ok=False)
        for name, _, _, prior, _ in changes:
            if prior is not None:
                path = backup / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(prior)
                path.chmod(0o600)
        (backup / 'forwarder.json').write_bytes(config_raw)
        (backup / 'forwarder.json').chmod(0o600)
        run('stop')
        STEP = 'stopped_receipt'
        stopped = True
        receipt = snapshot()
        assert receipt.get('cleanup_ok') is True and receipt.get('native_session_unchanged') is True
        assert receipt.get('uncertain') == 0
        assert not any(receipt.get('queue', {}).get(k) for k in ('queued', 'publishing', 'uncertain'))
        for _, path, raw, _, info in changes:
            replace(path, raw, info)
        promoted = True
        STEP = 'new_bridge_start'
        if args[-1] == '--image-chunks':
            run('image-output', 'enable')
            before_config, after_config = json.loads(config_raw), json.loads(read(config)[0])
            assert after_config.pop('imageOutputEnabled') is True
            before_config.pop('imageOutputEnabled', None)
            assert before_config == after_config, 'unrelated_configuration_changed'
        elif not output_policy:
            run('context', 'enable')
        else:
            assert read(config)[0] == config_raw, 'configuration_changed'
        run('start')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = snapshot()
            if current.get('ready') is True and current.get('group_context_enabled') is True:
                break
            time.sleep(.5)
        assert current.get('ready') is True and current.get('group_context_enabled') is True, 'context_not_ready'
        if args[-1] == '--image-chunks':
            assert current.get('image_output_enabled') is True, 'image_output_not_enabled'
        assert current.get('uncertain') == 0 and current.get('new_qr') is False
        assert current.get('self_identity_verified') is True and native.unchanged()
        flags, failures = core.tap.compare_baselines(host, network, business)
        assert not failures
        print(json.dumps({'stage': 'image_output_code_deployed' if image_output else 'output_policy_deployed' if output_policy else 'context_deployed', 'ok': True,
                          'configuration_unchanged': read(config)[0] == config_raw if output_policy else None,
                          'native_session_unchanged': True,
                          **flags, **{k: current.get(k) for k in public}}, sort_keys=True))
    except Exception:
        # Never overwrite a post-upgrade uncertain send/pending question.
        # Rollback is limited to a bridge that has handled no new input.
        now = snapshot()
        clean = now.get('uncertain', 0) == 0 and not any(now.get('queue', {}).get(k) for k in ('queued', 'publishing', 'uncertain'))
        if stopped and clean and (not promoted or now.get('received', 0) == 0):
            subprocess.run(['/usr/bin/systemctl', 'stop', old.core.UNIT], capture_output=True, timeout=30)
            for _, path, _, prior, info in changes:
                if prior is not None:
                    replace(path, prior, info)
                elif path.exists():
                    path.unlink()
            replace(config, config_raw, config_info)
            run('start')
            print(json.dumps({'stage': 'context_rollback', 'ok': True, 'native_session_unchanged': native.unchanged()}))
        raise
    finally:
        native.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'stage': 'context_deployment_failed', 'ok': False, 'step': STEP,
                          'error_class': type(error).__name__}))
        raise SystemExit(1)
