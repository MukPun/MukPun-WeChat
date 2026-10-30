"""GET-only recovery of one fully proved, unsegmented text submission.

Called under bridge.lock before workers start. Never creates a Native send,
changes its lifecycle, or clears an unrelated unknown outcome.
"""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
DATABASE = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite')
HEX32 = re.compile(r'[0-9a-f]{32}\Z')


def companion(name, path):
    spec = importlib.util.spec_from_file_location('_text_recovery_' + name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


loops = companion('loop_guard', HERE.parent / 'wechat_bridge/loop_guard.py')


def verify_readback(audit, intent, message):
    try:
        seed = {key: audit[key] for key in ('sendId', 'destinationSha256', 'textSha256', 'bytes')}
        loops.verified_sender(seed, intent, message, 'wechat-primary', {intent['destination']})
        server, client = intent.get('serverId'), intent.get('clientId')
        return (isinstance(server, str) and re.fullmatch(r'[1-9][0-9]{0,19}', server) is not None
            and int(server) <= 2**64 - 1 and isinstance(client, str)
            and re.fullmatch(r'[1-9][0-9]{0,9}', client) is not None and int(client) <= 2**32 - 1
            and message.get('serverId') == server)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def read_bound(lookup, audit, deadline):
    """Only the audited send and the projection named by that send are read."""
    lookup.policy.path = lookup.policy.prefix + '/sends/' + audit['sendId']
    intent = lookup._get(lookup.policy.path, deadline, timeout=8)
    if (intent.get('id') != audit['sendId'] or intent.get('accountId') != 'wechat-primary'
            or intent.get('state') != 'accepted' or not intent.get('localMessageId')):
        return False
    message = lookup._get(lookup.policy.message(intent['localMessageId']), deadline, timeout=8)
    return verify_readback(audit, intent, message)


def readback(pinned, audit):
    # The token crosses an anonymous stdin pipe, never argv, env or a file.
    value = subprocess.run(['/usr/bin/nsenter', '--net=/proc/' + str(pinned.host.client_pid) + '/ns/net',
        '--', '/usr/bin/python3', '-I', '-B', str(Path(__file__).resolve()), '--read'],
        input=json.dumps({'token': pinned.token(), 'audit': audit}).encode(),
        capture_output=True, timeout=22)
    return value.returncode == 0 and value.stdout == b'{"verified":true}\n'


def workers_stopped(core):
    value = subprocess.run(['/usr/bin/systemctl', 'list-units', '--all', '--type=service',
        '--plain', '--no-legend', '--no-pager'], capture_output=True, text=True, timeout=10)
    if value.returncode:
        return False
    for line in value.stdout.splitlines():
        name = line.split()[0] if line.split() else ''
        if not any(re.fullmatch(re.escape(prefix) + r'[0-9a-f]{12}\.service', name)
                   for prefix in ('astrbot-wechat-private-host-', core.groups.READER_PREFIX)):
            continue
        code, state = core.tap.load_state(name)
        if state.get('LoadState') == 'not-found':
            continue
        if (code or state.get('ActiveState') not in ('inactive', 'failed')
                or state.get('MainPID') != '0'):
            return False
        group = state.get('ControlGroup')
        if group:
            if not group.startswith('/') or '..' in Path(group).parts:
                return False
            procs = Path('/sys/fs/cgroup') / group.lstrip('/') / 'cgroup.procs'
            if procs.exists() and procs.read_text().strip():
                return False
    return True


def require(value):
    if not value:
        raise ValueError('text_recovery_unverified')


def unknown_result(raw):
    if raw is None:
        return True
    value = json.loads(raw)
    return (isinstance(value, dict) and value.get('status') == 'failed'
        and type(value.get('retcode')) is int and value['retcode'] == 1408
        and value.get('data') is None and value.get('message') == 'outcome_unknown'
        and value.get('wording', 'outcome_unknown') == 'outcome_unknown'
        and not set(value) - {'status', 'retcode', 'data', 'message', 'wording'})


def backup(core, audit_raw, db):
    parent = core.owner.STATE / 'text-recovery'
    core.owner.private_directory(parent)
    # The same frozen audit reuses its first complete backup on every retry.
    target = parent / ('receipt-' + hashlib.sha256(audit_raw).hexdigest()[:12])
    if target.exists():
        require(not target.is_symlink() and target.is_dir())
        names = {p.name for p in target.iterdir()}
        require(names <= {'audit.json', 'bridge.sqlite', 'bridge.sqlite-journal',
                         '.backup-ready.json.next', 'backup-ready.json', 'complete.json'})
        for name in names:
            info = (target / name).lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600)
        if 'backup-ready.json' in names:
            require(names in ({'audit.json', 'bridge.sqlite', 'backup-ready.json'},
                              {'audit.json', 'bridge.sqlite', 'backup-ready.json', 'complete.json'}))
            require((target / 'audit.json').read_bytes() == audit_raw)
            marker = core.owner.read_json(target / 'backup-ready.json')
            info = (target / 'bridge.sqlite').stat()
            require(0 < info.st_size <= 8 * 1024 * 1024)
            with (target / 'bridge.sqlite').open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            require(marker == {'auditSha256': hashlib.sha256(audit_raw).hexdigest(),
                'databaseBytes': info.st_size, 'databaseSha256': digest})
            with closing(sqlite3.connect((target / 'bridge.sqlite').as_uri() + '?mode=ro', uri=True)) as saved:
                require(saved.execute('PRAGMA quick_check').fetchall() == [('ok',)])
                require({'identities', 'submissions', 'inbox'} <=
                    {r[0] for r in saved.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            return target
        # No ready marker means this copy never authorized a source mutation.
        # Discard only these owned partial-copy files and rebuild from source.
        require('complete.json' not in names)
        for name in names:
            (target / name).unlink()
    core.owner.private_directory(target)
    fd = os.open(target / 'audit.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(audit_raw)
        stream.flush()
        os.fsync(stream.fileno())
    with closing(sqlite3.connect(target / 'bridge.sqlite')) as saved:
        db.backup(saved)
    (target / 'bridge.sqlite').chmod(0o600)
    fd = os.open(target / 'bridge.sqlite', os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    with (target / 'bridge.sqlite').open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    core.owner.write_json(target / 'backup-ready.json', {'auditSha256': hashlib.sha256(audit_raw).hexdigest(),
        'databaseBytes': (target / 'bridge.sqlite').stat().st_size, 'databaseSha256': digest})
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


def prune_backups(parent):
    completed = []
    for path in parent.iterdir():
        if (not re.fullmatch(r'receipt-[0-9a-f]{12}', path.name) or path.is_symlink()
                or not path.is_dir() or {p.name for p in path.iterdir()} !=
                {'audit.json', 'bridge.sqlite', 'backup-ready.json', 'complete.json'}):
            continue
        items = list(path.iterdir())
        if all(stat.S_ISREG(p.lstat().st_mode) and p.lstat().st_nlink == 1
               and p.lstat().st_uid == os.geteuid() for p in items):
            completed.append(path)
    for path in sorted(completed, key=lambda p: p.stat().st_mtime, reverse=True)[3:]:
        for name in ('audit.json', 'bridge.sqlite', 'backup-ready.json', 'complete.json'):
            (path / name).unlink()
        path.rmdir()


def reconcile(core, audit_path, settings):
    pinned = None
    try:
        raw = audit_path.read_bytes()
        require(len(raw) <= 4096 and settings is not None)
        audit = json.loads(raw)
        require(audit.get('state') in ('submitted', 'unknown'))
        loops.proof({key: audit[key] for key in ('sendId', 'destinationSha256', 'textSha256', 'bytes')})
        require(type(audit.get('segmentBytes')) is int and audit['segmentBytes'] == settings['segmentBytes']
            and 1024 <= audit['segmentBytes'] <= 8192 and 0 < audit['bytes'] < audit['segmentBytes'] - 4
            and all(isinstance(audit.get(k), str) and HEX32.fullmatch(audit[k]) for k in ('key', 'requestId'))
            and all(type(audit.get(k)) is int and audit[k] > 1 for k in ('nativeClientPid', 'nativeClientTicks'))
            and workers_stopped(core))
        pinned = core.groups.PinnedNative()
        pinned.open()
        require((pinned.host.client_pid, pinned.host.client_ticks) ==
                (audit['nativeClientPid'], audit['nativeClientTicks']))
        require(readback(pinned, audit) and pinned.unchanged() and audit_path.read_bytes() == raw)
        # DynamicUser StateDirectory uses /var/lib/<name> -> private/<name>.
        # Resolve that directory alias, but never follow a database-file link.
        require(not DATABASE.is_symlink())
        database = DATABASE.parent.resolve(strict=True) / DATABASE.name
        require(not any(p.is_symlink() for p in (database, *database.parents)))
        info = database.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
            and 0 < info.st_size <= 8 * 1024 * 1024)
        with closing(sqlite3.connect(database, timeout=.5)) as db:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA max_page_count=2048')
            require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)])
            require(db.execute("SELECT count(*) FROM inbox WHERE state IN ('publishing','uncertain')").fetchone()[0] == 0)
            rows = db.execute('SELECT key,native_key,result FROM submissions LIMIT 4097').fetchall()
            require(len(rows) <= 4096)
            matching = [row for row in rows if isinstance(row[1], str) and HEX32.fullmatch(row[1])
                and hashlib.sha256((row[1] + ':0').encode()).hexdigest()[:32] == audit['key']]
            require(len(matching) == 1)
            row = matching[0]
            found = db.execute("SELECT id FROM identities WHERE kind='outgoing' AND raw=?", (audit['sendId'],)).fetchone()
            unresolved = unknown_result(row[2])
            require(unresolved or found and json.loads(row[2]) ==
                    {'status': 'ok', 'retcode': 0, 'data': {'message_id': found[0]}})
            saved = backup(core, raw, db)
            require(workers_stopped(core) and pinned.unchanged() and audit_path.read_bytes() == raw)
            db.execute('BEGIN IMMEDIATE')
            require(db.execute('SELECT key,native_key,result FROM submissions WHERE key=?', (row[0],)).fetchone() == row)
            found = db.execute("SELECT id FROM identities WHERE kind='outgoing' AND raw=?", (audit['sendId'],)).fetchone()
            if found:
                mid = found[0]
            else:
                require(db.execute('SELECT count(*) FROM identities').fetchone()[0] < 8192)
                mid = db.execute("INSERT INTO identities(kind,raw) VALUES('outgoing',?)", (audit['sendId'],)).lastrowid
            answer = {'status': 'ok', 'retcode': 0, 'data': {'message_id': mid}}
            require(unknown_result(row[2]) or json.loads(row[2]) == answer)
            if unresolved:
                db.execute('UPDATE submissions SET result=? WHERE key=? AND native_key=?',
                           (json.dumps(answer, separators=(',', ':')), row[0], row[1]))
            require(pinned.unchanged() and audit_path.read_bytes() == raw)
            db.commit()
        # A crash here is retried idempotently using the same outgoing identity.
        temporary = audit_path.with_name('.' + audit_path.name + '.next')
        if temporary.exists() or temporary.is_symlink():
            info = temporary.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 4096)
            require(pinned.unchanged() and audit_path.read_bytes() == raw)
            temporary.unlink()
        core.owner.write_json(audit_path, {**audit, 'state': 'accepted',
            'review': 'native_receipt_and_projection_verified'})
        core.owner.write_json(saved / 'complete.json', {'verified': True, 'resent': False, 'time': int(time.time())})
        prune_backups(saved.parent)
        return True
    except Exception:
        return False
    finally:
        if pinned is not None:
            pinned.close()


def main():
    if sys.argv[1:] != ['--read']:
        return 2
    try:
        value = json.loads(sys.stdin.buffer.read(8193))
        require(set(value) == {'token', 'audit'})
        contact = companion('contact', HERE / 'contact_receive_protocol.py')
        lookup = contact.ContactReceiveClient('wechat-primary', value['token'])
        verified = read_bound(lookup, value['audit'], time.monotonic() + 18)
        print('{"verified":true}' if verified else '{"verified":false}', flush=True)
        return 0 if verified else 1
    except Exception:
        print('{"verified":false}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
