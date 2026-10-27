#!/usr/bin/env python3
"""Read only public lifecycle fields and private snapshot sizes, never secrets."""
import importlib.util
import json
import os
from pathlib import Path
import resource
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent


def main():
    if os.geteuid() != 0 or sys.argv[1:]:
        return 2
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    spec = importlib.util.spec_from_file_location('_saved_inspection', ROOT / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core, owner = formal.core, formal.core.owner
    value = owner.read_json(owner.CONTROL / 'status.json')
    fields = ('ready', 'connected', 'accounts', 'ok', 'code', 'busy', 'done', 'qrVersion', 'sequence')
    public = {k: value.get(k) for k in fields}
    cleanup = value.get('cleanup')
    public['cleanup'] = {k: v for k, v in cleanup.items() if type(v) is bool} if isinstance(cleanup, dict) else None
    if isinstance(cleanup, dict):
        public['cleanup_codes'] = {k: [x for x in v if isinstance(x, str) and re.fullmatch('[a-z_]{1,80}', x)]
                                   for k, v in cleanup.items() if isinstance(v, list)}
    records = [r.get('record', r) for r in value.get('records', [])[-8:] if isinstance(r, dict)]
    public['records'] = [{k: r[k] for k in ('stage', 'code', 'ok', 'accounts', 'connected') if k in r}
                         for r in records if isinstance(r, dict)]
    directory = owner.STATE / 'checkpoints/private'
    slots = {}
    for name in ('current.zip', 'previous.zip', '.next.zip'):
        path = directory / name
        if path.exists() and not path.is_symlink():
            info = path.stat()
            slots[name] = {'bytes': info.st_size, 'mtime': int(info.st_mtime), 'mode': oct(info.st_mode & 0o777)}
            store = owner.companion('session_state_store')
            with zipfile.ZipFile(path, 'r') as archive:
                _, count, size = store._archive_manifest(archive)
                slots[name].update(valid=True, files=count, source_bytes=size)
    audit = owner.read_json(owner.STATE / 'forward-send.json')
    send = {k: audit.get(k) for k in ('state', 'bytes')}
    baseline_path = owner.STATE / 'core-acceptance-baseline.json'
    flags = {}
    business_changes = {}
    if baseline_path.exists():
        baseline = owner.read_json(baseline_path)
        flags, _ = core.tap.compare_baselines(baseline['host'], baseline['network'],
                                             [tuple(row) for row in baseline['business']])
        before, now = set(tuple(row) for row in baseline['business']), set(core.tap.business_snapshot())
        business_changes = {'removed': sorted(before-now), 'added': sorted(now-before),
                            'frp_unchanged': {x for x in before if x[1] in {'frpc','frps'}} ==
                                             {x for x in now if x[1] in {'frpc','frps'}}}
    listed = subprocess.run(['/usr/bin/systemctl','list-units','--all','--plain','--no-legend',
        'astrbot-wechat-login-*.service','astrbot-wechat-slirp-tap-helper-*.service'],
        capture_output=True,text=True,timeout=4)
    units = [line.split()[:4] for line in listed.stdout.splitlines()][:12]
    print(json.dumps({'stage': 'saved_session_inspection', 'owner_active': owner.owner_active(),
                      'status': public, 'snapshots': slots, 'last_text_send': send,
                      'prior_host_baseline': flags, 'business_changes': business_changes,
                      'remaining_own_units': units}, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'stage': 'saved_session_inspection_failed', 'error_class': type(error).__name__}))
        raise SystemExit(1)
