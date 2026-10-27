#!/usr/bin/env python3
"""Inspect only queue schema/state counts in a private offline snapshot copy."""
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent


def main():
    if os.geteuid() != 0 or sys.argv[1:] not in ([], ['previous']):
        return 2
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location('_snapshot_review', ROOT / 'session_runtime.py')
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    store = owner.companion('session_state_store')
    latest = store.SnapshotStore(owner.STATE / 'checkpoints').latest()
    if sys.argv[1:] == ['previous']:
        latest = latest.with_name('previous.zip')
    work = Path(tempfile.mkdtemp(prefix='astrbot-wechat-outbox-review.', dir='/var/tmp'))
    result = {'stage': 'snapshot_outbox_inspection', 'slot': latest.name}
    try:
        with zipfile.ZipFile(latest, 'r') as archive:
            store._archive_manifest(archive)
            selected = [i for i in archive.infolist() if i.filename.endswith('/agent/control.sqlite')]
            result['database_entries'] = len(selected)
            result['database_basenames'] = sorted(set(Path(i.filename).name for i in archive.infolist()
                if i.filename.endswith(('.sqlite', '.db'))))[:12]
            if len(selected) != 1:
                print(json.dumps(result, sort_keys=True))
                return 1
            name = selected[0].filename
            result['database_parts'] = {suffix or 'main': archive.getinfo(name + suffix).file_size
                                       for suffix in ('', '-wal', '-shm') if name + suffix in archive.namelist()}
            for suffix in ('', '-wal', '-shm'):
                if name + suffix in archive.namelist():
                    with archive.open(name + suffix) as src, (work / ('control.sqlite' + suffix)).open('wb') as dst:
                        shutil.copyfileobj(src, dst, 65536)
        with (work / 'control.sqlite').open('rb') as source:
            result['sqlite_header'] = source.read(16) == b'SQLite format 3\0'
        connection = sqlite3.connect((work / 'control.sqlite').as_uri() + '?mode=ro', uri=True, timeout=.2)
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            tables = dict(connection.execute("SELECT name,type FROM sqlite_master WHERE name IN ('outbox','media_jobs')"))
            result['table_names'] = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 40")]
            result['tables'] = tables
            result['columns'] = {}
            result['states'] = {}
            for table in ('outbox', 'media_jobs'):
                if tables.get(table) != 'table':
                    continue
                cols = [r[1] for r in connection.execute('PRAGMA table_info(' + table + ')')]
                result['columns'][table] = cols
                if 'state' in cols:
                    result['states'][table] = [(v if isinstance(v, str) and len(v) < 30 else '<other>', n)
                        for v,n in connection.execute('SELECT state,count(*) FROM ' + table + ' GROUP BY state LIMIT 20')]
                if table == 'media_jobs' and 'kind' in cols:
                    result['media_kinds'] = [(v if isinstance(v, str) and len(v) < 30 else '<other>', n)
                        for v,n in connection.execute('SELECT kind,count(*) FROM media_jobs GROUP BY kind LIMIT 10')]
            result['quick_check_ok'] = connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
            audit_path = owner.STATE / 'forward-send.json'
            audit = owner.read_json(audit_path)
            row = connection.execute('SELECT state,idempotency_key,destination FROM outbox WHERE id=?',
                                     (audit.get('sendId'),)).fetchone()
            import hashlib
            result['last_send_in_snapshot'] = bool(row and row[0] == 'accepted' and row[1] == audit.get('key')
                and hashlib.sha256(row[2].encode()).hexdigest() == audit.get('destinationSha256'))
            result['last_send_before_snapshot'] = audit_path.stat().st_mtime <= latest.stat().st_mtime
            result['journal_mode'] = connection.execute('PRAGMA journal_mode').fetchone()[0]
            for table in ('events', 'event_log'):
                if table in result['table_names']:
                    result['columns'][table] = [r[1] for r in connection.execute('PRAGMA table_info(' + table + ')')]
            spool = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve()
            sc = sqlite3.connect(spool.as_uri() + '?mode=ro', uri=True, timeout=.2)
            try:
                result['spool_counts'] = dict(sc.execute('SELECT state,count(*) FROM inbox GROUP BY state'))
                saved = [v for n,v in sc.execute('SELECT name,value FROM checkpoints LIMIT 200') if n.startswith('profile:')]
                result['bridge_cursors'] = [json.loads(v).get('cursor') if v.startswith('{') else v for v in saved]
            finally:
                sc.close()
        except sqlite3.DatabaseError as error:
            result['sqlite_error_name'] = getattr(error, 'sqlite_errorname', 'SQLITE_ERROR')
            message = str(error)
            result['sqlite_error_kind'] = ('malformed' if 'malformed' in message else 'not_database' if 'not a database' in message
                                          else 'readonly' if 'readonly' in message else 'other')
        finally:
            connection.close()
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        for suffix in ('', '-wal', '-shm', '-journal'):
            (work / ('control.sqlite' + suffix)).unlink(missing_ok=True)
        work.rmdir()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'stage': 'snapshot_outbox_inspection_failed', 'error_class': type(error).__name__}))
        raise SystemExit(1)
