#!/usr/bin/env python3
"""Archive both snapshots and restore a verified, fully settled previous one."""
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parent


def copy_private(source, target):
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with source.open('rb') as src, os.fdopen(fd, 'wb') as dst:
        shutil.copyfileobj(src, dst, 65536)
        dst.flush()
        os.fsync(dst.fileno())


def main():
    if os.geteuid() != 0 or sys.argv[1:]:
        return 2
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location('_valid_restore', ROOT / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core, owner = formal.core, formal.core.owner
    require = core.require
    require(not owner.owner_active(), 'owner_running')
    require(core.tap.details(core.UNIT).get('ActiveState') != 'active', 'bridge_running')
    state = owner.read_json(owner.CONTROL / 'status.json')
    require(state.get('code') == 'outbox_review_failed' and owner.cleanup_ok(state.get('cleanup')),
            'unexpected_restore_failure')
    listed = subprocess.run(['/usr/bin/systemctl', 'list-units', '--all', '--plain', '--no-legend',
        'astrbot-wechat-login-*.service', 'astrbot-wechat-slirp-tap-helper-*.service'],
        capture_output=True, timeout=4)
    require(listed.returncode == 0 and not listed.stdout.strip(), 'native_units_remain')
    store = owner.companion('session_state_store')
    snapshots = store.SnapshotStore(owner.STATE / 'checkpoints')
    current = snapshots.latest()
    require(current is not None and current.name == 'current.zip', 'snapshot_layout')
    previous = current.with_name('previous.zip')
    with snapshots._private() as directory:
        snapshots._slot(directory, 'current.zip')
        require(snapshots._slot(directory, 'previous.zip') is not None, 'previous_missing')
        require(snapshots._slot(directory, '.next.zip') is None, 'snapshot_staging_present')
    work = Path(tempfile.mkdtemp(prefix='astrbot-wechat-restore-review.', dir='/var/tmp'))
    try:
        with zipfile.ZipFile(previous, 'r') as archive:
            _, count, size = store._archive_manifest(archive)
            names = [i.filename for i in archive.infolist() if i.filename.endswith('/agent/control.sqlite')]
            require(len(names) == 1, 'snapshot_schema')
            for suffix in ('', '-wal', '-shm'):
                if names[0] + suffix in archive.namelist():
                    with archive.open(names[0] + suffix) as src, (work / ('control.sqlite' + suffix)).open('wb') as dst:
                        shutil.copyfileobj(src, dst, 65536)
        db = sqlite3.connect((work / 'control.sqlite').as_uri() + '?mode=ro', uri=True, timeout=.2)
        spool_path = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve()
        spool = sqlite3.connect(spool_path.as_uri() + '?mode=ro', uri=True, timeout=.2)
        try:
            for connection in (db, spool):
                connection.execute('PRAGMA query_only=ON')
                connection.execute('PRAGMA trusted_schema=OFF')
            require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'previous_database_invalid')
            require(db.execute("SELECT count(*) FROM outbox WHERE state IS NULL OR state NOT IN ('accepted','failed','cancelled')").fetchone() == (0,), 'previous_pending_outbox')
            require(db.execute("SELECT count(*) FROM media_jobs WHERE kind IS NULL OR kind NOT IN ('send','download') OR (kind='send' AND (state IS NULL OR state NOT IN ('accepted','failed','cancelled','unsupported','expired')))").fetchone() == (0,), 'previous_pending_media')
            audit_path = owner.STATE / 'forward-send.json'
            audit = owner.read_json(audit_path)
            row = db.execute('SELECT state,idempotency_key,destination FROM outbox WHERE id=?', (audit.get('sendId'),)).fetchone()
            require(audit.get('state') == 'accepted' and row and row[0] == 'accepted'
                and row[1] == audit.get('key') and hashlib.sha256(row[2].encode()).hexdigest() == audit.get('destinationSha256')
                and audit_path.stat().st_mtime <= previous.stat().st_mtime, 'last_send_not_in_previous')
            counts = dict(spool.execute('SELECT state,count(*) FROM inbox GROUP BY state'))
            require(not any(counts.get(k) for k in ('queued','publishing','uncertain')), 'spool_requires_review')
            cursors = [json.loads(v)['cursor'] if v.startswith('{') else v for n,v in
                spool.execute('SELECT name,value FROM checkpoints') if n.startswith('profile:')]
            maximum = db.execute('SELECT max(sequence) FROM events').fetchone()[0]
            require(all(c.isdecimal() and int(c) <= maximum for c in cursors), 'previous_cursor_behind_bridge')
        finally:
            spool.close()
            db.close()
    finally:
        for suffix in ('','-wal','-shm','-journal'):
            (work / ('control.sqlite' + suffix)).unlink(missing_ok=True)
        work.rmdir()
    disk = os.statvfs(owner.STATE)
    require(disk.f_bavail * disk.f_frsize >= 128*1024*1024, 'backup_space')
    source = ROOT / 'run_login_probe.py'
    source_info = source.lstat()
    require(stat.S_ISREG(source_info.st_mode) and source_info.st_nlink == 1, 'source_permissions')
    raw = source.read_bytes()
    require(raw.count(b'LimitFSIZE="8M"') == 1, 'native_budget_source_changed')
    updated = raw.replace(b'LimitFSIZE="8M"', b'LimitFSIZE="16M"')
    ast.parse(updated)
    backup = owner.STATE / 'recovery-native-db-20261027'
    require(not backup.exists(), 'recovery_already_attempted')
    backup.mkdir(mode=0o700)
    copy_private(current, backup / 'current-corrupt.zip')
    copy_private(previous, backup / 'previous-valid.zip')
    copy_private(source, backup / 'run_login_probe.py')
    owner.write_json(backup / 'review.json', {'snapshot_files': count, 'snapshot_bytes': size,
        'last_send_verified': True, 'spool_settled': True, 'bridge_cursors': cursors, 'snapshot_max_cursor': maximum})
    host = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net','user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    temporary = current.with_name('.next.zip')
    copy_private(previous, temporary)
    os.replace(temporary, current)
    replacement = source.with_name('.run_login_probe.recovered-next')
    fd = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, stat.S_IMODE(source_info.st_mode))
    with os.fdopen(fd, 'wb') as stream:
        stream.write(updated)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(replacement, source_info.st_uid, source_info.st_gid)
    os.replace(replacement, source)
    owner.start_owner(False, seconds=owner.LONG_RUNTIME_SECONDS)
    core.trial.ensure_session()
    status = owner.read_json(owner.CONTROL / 'status.json')
    flags, failures = core.tap.compare_baselines(host, network, business)
    require(not failures and status.get('qrVersion') == 0 and status.get('connected') is True, 'restored_session_requires_review')
    formal.start()
    until = time.monotonic() + 35
    while time.monotonic() < until:
        now = owner.read_json(core.STATUS)
        if now.get('ready') is True:
            print(json.dumps({'stage':'previous_snapshot_restored','ok':True,'new_qr':False,
                'bridge_ready':True,'native_file_limit_mib':16,'native_storage_limit_mib':32,**flags},sort_keys=True))
            return 0
        time.sleep(.5)
    raise ValueError('bridge_not_ready')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'stage':'previous_snapshot_restore_failed','ok':False,
                          'code':getattr(error,'stage',getattr(error,'code','restore_failed'))}))
        raise SystemExit(1)
