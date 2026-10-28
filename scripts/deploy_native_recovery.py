#!/usr/bin/env python3
"""Apply two reviewed restore fixes while this project's Native is stopped."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    assert os.geteuid() == 0 and sys.argv[1:] == ['apply']
    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('_native_recovery_deploy', root / 'deploy_group_context.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    spec = importlib.util.spec_from_file_location('_native_recovery_owner', root / 'session_runtime.py')
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    assert not owner.owner_active()
    stage = root / 'native-recovery-staging'
    manifest = json.loads((stage / 'manifest.json').read_text())
    assert set(manifest) == {'session_runtime_client.py', 'session_runtime_host.py'}
    changes = []
    for name, digest in manifest.items():
        raw, _ = helper.read(stage / name)
        assert hashlib.sha256(raw).hexdigest() == digest
        ast.parse(raw)
        for target in (root / name, root / 'verification/scripts' / name):
            assert not target.is_symlink()
            prior, info = helper.read(target)
            changes.append((target, raw, prior, info))
    backup = root / ('native-recovery-backup-' + time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(mode=0o700)
    for target, raw, prior, info in changes:
        saved = backup / target.relative_to(root)
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(prior)
        saved.chmod(0o600)
        helper.replace(target, raw, info)
    print(json.dumps({'stage': 'native_recovery_code_updated', 'backup': str(backup)}), flush=True)
    result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(root / 'restore_stopped_forwarder.py'), 'apply'],
                            timeout=300)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
