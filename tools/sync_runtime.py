#!/usr/bin/env python3
"""Build/check an explicit worker-code view while retaining private venv/data."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('_sync_delivery', ROOT/'scripts/runtime_manifest.py')
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


def public_files():
    return delivery.public_sources(ROOT)


def _runtime_path(target, relative):
    if target.parent.is_symlink() or target.is_symlink():
        raise ValueError('runtime_symlink')
    return delivery.checked_path(target, relative, error='runtime_symlink')


def _replace(destination, raw):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    info = destination.stat() if destination.exists() else None
    temporary = destination.with_name('.'+destination.name+'.sync-next')
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
        if os.name == 'posix':
            os.fchmod(fd, stat.S_IMODE(info.st_mode) if info else 0o644)
            if info:
                os.fchown(fd, info.st_uid, info.st_gid)
        os.close(fd)
        fd = None
        os.replace(temporary, destination)
    finally:
        if fd is not None:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def sync(apply=False):
    runtime = ROOT/'.runtime'
    target = runtime/'verification'
    record = runtime/'worker-manifest.json'
    if runtime.is_symlink() or target.is_symlink() or record.is_symlink():
        raise ValueError('runtime_symlink')
    sources = public_files()
    previous = json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError('runtime_manifest')
    # Preflight the entire plan, including stale entries, before writing/deleting.
    destinations = {name: _runtime_path(target, name) for name in sources}
    stale = {name: _runtime_path(target, name) for name in previous if name not in sources}
    contents = {name: source.read_bytes() for name, source in sources.items()}
    manifest = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}
    mismatch = [name for name, raw in contents.items()
                if not destinations[name].exists() or destinations[name].read_bytes() != raw]
    removed = [name for name, path in stale.items() if path.exists()]
    manifest_changed = previous != manifest
    if apply:
        runtime.mkdir(exist_ok=True, mode=0o700)
        target.mkdir(exist_ok=True, mode=0o755)
        target.chmod(0o755)
        for name in mismatch:
            destination = destinations[name]
            _replace(destination, contents[name])
        # Existing package directories must remain searchable to the workers.
        for destination in destinations.values():
            for parent in destination.parents:
                if parent == target:
                    break
                parent.chmod(0o755)
        for name in removed:
            stale[name].unlink()
        _replace(record, (json.dumps(manifest, sort_keys=True)+'\n').encode('utf-8'))
    return {'stage': 'runtime_code_synced' if apply else 'runtime_code_checked',
            'ok': apply or not (mismatch or removed or manifest_changed),
            'files': len(manifest), 'changed': len(mismatch), 'removed': len(removed)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args()
    result = sync(arguments.apply)
    print(json.dumps(result))
    raise SystemExit(0 if result['ok'] else 1)
