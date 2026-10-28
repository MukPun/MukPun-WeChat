#!/usr/bin/env python3
"""Inspect nickname availability for up to three observed groups, GET only."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import time


def main():
    assert os.geteuid() == 0 and sys.argv[1:] in ([], ['--refresh'], ['--refresh-next'], ['--events'])
    refresh = sys.argv[1:] in (['--refresh'], ['--refresh-next'])
    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('_nickname_probe_formal', root / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core = formal.core
    database = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve(strict=True)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.2) as connection:
        connection.execute('PRAGMA query_only=ON')
        rooms = [row[0] for row in connection.execute('SELECT destination FROM message_routes GROUP BY destination ORDER BY MAX(id) DESC LIMIT 3')]
        users = [row[0] for row in connection.execute("SELECT raw FROM identities WHERE kind='user' ORDER BY id DESC LIMIT 6")]
        checkpoints = [row[0] for row in connection.execute("SELECT value FROM checkpoints WHERE name LIKE 'profile:%' ORDER BY rowid DESC LIMIT 1")]
        checkpoint = json.loads(checkpoints[0]) if checkpoints and checkpoints[0].startswith('{') else {'cursor': checkpoints[0] if checkpoints else '0'}
    rooms = [room for room in rooms if core.trigger.group_id(room)]
    if refresh:
        rooms = rooms[1:2] if sys.argv[1:] == ['--refresh-next'] else rooms[:1]
    assert rooms, 'no_observed_groups'
    pinned = core.groups.PinnedNative()
    unit = core.groups.READER_PREFIX + secrets.token_hex(6) + '.service'
    bundle, capture = None, None
    files = core.FILES + ('nickname_probe_runtime.py',)
    try:
        pinned.open()
        bundle = Path(tempfile.mkdtemp(prefix='astrbot-wechat-nickname-probe.', dir='/var/tmp'))
        for name in files:
            source = root / ('nickname_probe_worker.py' if name == 'forward_native_worker.py'
                             else 'forward_native_worker.py' if name == 'nickname_probe_runtime.py' else name)
            if not source.exists():
                source = root.parent / 'wechat_bridge' / name
            core.groups.legacy.write_public(bundle, name, core.groups.legacy.source_text(source))
        bundle.chmod(0o755)
        properties = core.groups.reader_properties(bundle, 'awnp-' + secrets.token_hex(4), pinned.host.parent, pinned.netfd, 60)
        args = ['/usr/bin/systemd-run', '--system', '--quiet', '--collect', '--wait', '--pipe', '--unit=' + unit,
                *properties, '/usr/bin/python3', '-I', '-B', '/opt/forward_native_worker.py']
        capture = core.Duplex(args, {'account': 'wechat-primary', 'token': pinned.token(), 'rooms': rooms, 'users': users,
                                    'refresh': refresh, 'inspectEvents': sys.argv[1:] == ['--events'], 'after': checkpoint['cursor']})
        deadline, complete = time.monotonic() + 45, False
        while time.monotonic() < deadline and not complete:
            for record in capture.tick():
                if record.get('kind') == 'isolated' and record.get('ok') is True:
                    core.groups.verify_reader(unit, pinned, 'forward_native_worker.py')
                    capture.acknowledge()
                elif record.get('kind') == 'nickname_refresh':
                    print(json.dumps(record), flush=True)
                elif record.get('kind') == 'nickname_probe':
                    print(json.dumps(record), flush=True)
                    complete = True
                else:
                    raise RuntimeError('nickname_probe_frame')
            assert complete or capture.process.poll() is None, 'nickname_probe_exited'
        assert complete, 'nickname_probe_timeout'
    finally:
        core.tap.stop_unit(unit, True)
        if capture:
            capture.close()
        unchanged = pinned.host is not None and pinned.unchanged()
        pinned.close()
        if bundle:
            for name in files:
                (bundle / name).unlink(missing_ok=True)
            bundle.rmdir()
        print(json.dumps({'stage': 'nickname_probe_cleanup', 'native_session_unchanged': unchanged}), flush=True)


if __name__ == '__main__':
    main()
