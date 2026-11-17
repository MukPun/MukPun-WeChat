"""Root-only bounded consistent WAL evidence; never copies/freezes or writes SQL."""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from . import session_persistent_store as storage, restore_admission as admission
from . import pending_send_outcomes as pending


def _require(value):
    if not value:
        raise ValueError('persistent_send_evidence')


def _directory(parent, name, uid, dev, stack):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    _require(info.st_uid == uid and info.st_dev == dev and stat.S_IMODE(info.st_mode) == 0o700)
    return fd


def _file(parent, name, uid, dev, stack):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    stack.callback(os.close, fd)
    return fd, storage._file(fd, uid=uid, dev=dev)


def _binding(core, pinned, metadata, data):
    _require(pinned.unchanged())
    _require(storage.storage_metadata(core.owner.STATE) == metadata)
    for fd in (data, pinned.host.data_fd):
        info = os.fstat(fd)
        _require((info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)) ==
                 (metadata['dev'], metadata['ino'], pinned.host.client_uid, 0o700))
    _require(storage._profile(data, pinned.host.client_uid) == metadata['profileSha256'])


@contextmanager
def database(core, pinned, targets):
    """A 1s SQL budget and 50ms lock wait; materialize only target/pending rows.

    The process, persistent directory and all DB path components are pinned and
    rechecked. SQLite reads real WAL, with one transaction spanning classification,
    audit archival and admission selection. No immutable=1, backup or messages scan.
    """
    profile = None
    try:
        _require(isinstance(targets, list) and 1 <= len(targets) <= pending.MAX_ENTRIES)
        _require(getattr(pinned.host, 'storage_mode', None) == 'persistent')
        metadata = storage.storage_metadata(core.owner.STATE)
        _require(metadata['state'] == 'leased' and metadata['uid'] == pinned.host.client_uid)
        with ExitStack() as stack:
            root = stack.enter_context(storage._directory(core.owner.STATE / 'persistent'))
            storage._private(root)
            data = _directory(root, 'data', metadata['uid'], metadata['dev'], stack)
            _binding(core, pinned, metadata, data)
            hashes = {}
            for name in storage.KEYS:
                fd, info = _file(data, name, metadata['uid'], metadata['dev'], stack)
                _require(0 < info.st_size <= 8192)
                raw = os.read(fd, 8193)
                _require(len(raw) == info.st_size)
                hashes[name] = hashlib.sha256(raw).hexdigest()
            profile = admission.profile_sha256(hashes)
            accounts = _directory(data, 'accounts', metadata['uid'], metadata['dev'], stack)
            with os.scandir(accounts) as entries:
                account = next(entries, None)
                _require(account is not None and next(entries, None) is None)
                account = account.name
            account_fd = _directory(accounts, account, metadata['uid'], metadata['dev'], stack)
            agent = _directory(account_fd, 'agent', metadata['uid'], metadata['dev'], stack)
            control, info = _file(agent, 'control.sqlite', metadata['uid'], metadata['dev'], stack)
            _require(0 < info.st_size <= storage.MAX_FILE_BYTES)
            # SQLite read-only WAL access must not create a missing shared index.
            wal_exists = False
            for name in ('control.sqlite-wal', 'control.sqlite-shm', 'control.sqlite-journal'):
                try:
                    _, side = _file(agent, name, metadata['uid'], metadata['dev'], stack)
                    _require(side.st_size <= storage.MAX_FILE_BYTES)
                    wal_exists |= name.endswith('-wal') and side.st_size > 0
                except FileNotFoundError:
                    if name.endswith('-shm') and wal_exists:
                        raise pending.PendingSend(profile)
            path = Path('/proc/self/fd') / str(agent) / 'control.sqlite'
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.05)
            stack.callback(db.close)
            deadline = time.monotonic() + 1
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            records = admission.pending_records(db)
            module = core.recovery_dispositions
            proof = dict(source='persistent-native-wal-v1', nativePid=pinned.host.client_pid,
                         nativeTicks=pinned.host.client_ticks, persistentProfileSha256=metadata['profileSha256'],
                         profileSha256=profile, accountSha256=hashlib.sha256(account.encode()).hexdigest(),
                         pending=[(kind, admission.fingerprint(account, kind, row)) for kind, row in records], targets=[])
            for kind, audit in targets:
                row = module._matching(db, audit, kind)
                proof['targets'].append((kind, module._evidence(audit),
                    admission.fingerprint(account, kind, row) if row is not None else None))
            view = dict(profileSha256=profile, account=account,
                        databaseViewSha256=hashlib.sha256(admission._encoded(proof)).hexdigest())

            def verify():
                _binding(core, pinned, metadata, data)
                # Prove the public path still resolves to the opened inode.
                with storage._directory(core.owner.STATE / 'persistent/data/accounts' / account / 'agent') as current:
                    _require((os.fstat(current).st_dev, os.fstat(current).st_ino) ==
                             (os.fstat(agent).st_dev, os.fstat(agent).st_ino))
                current = os.stat('control.sqlite', dir_fd=agent, follow_symlinks=False)
                _require((current.st_dev, current.st_ino, current.st_uid, current.st_nlink) ==
                         (info.st_dev, info.st_ino, metadata['uid'], 1))
                for name in ('control.sqlite-wal', 'control.sqlite-shm', 'control.sqlite-journal'):
                    try:
                        side = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=agent)
                    except FileNotFoundError:
                        continue
                    try:
                        storage._file(side, uid=metadata['uid'], dev=metadata['dev'])
                    finally:
                        os.close(side)
            verify()
            yield db, view, verify
            verify()
            db.rollback()
    except pending.PendingSend:
        raise
    except Exception as error:
        # Shared admission helpers normalize SQLite failures to ValueError.
        # Follow their retained cause only for transient SQLite lock/budget codes.
        cause = error
        while cause is not None:
            code = getattr(cause, 'sqlite_errorcode', 0) & 255
            if profile and code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT}:
                raise pending.PendingSend(profile) from None
            cause = cause.__cause__ or cause.__context__
        raise ValueError('persistent_send_evidence') from None


def refresh_admission(core, pinned, db, metadata, verify):
    """Publish only exact safe rows from this view and locked preserved ledger.

    Legitimate nonterminal tracked actions defer publication. Unknown or modified
    reviewed rows are integrity errors, never additional restore permission.
    """
    module = core.recovery_dispositions
    ledger = module.ledger_for(core, metadata['profileSha256'])
    with ledger._directory() as directory:
        lock = os.open(ledger.path.name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                       0o600, dir_fd=directory)
        try:
            module.dispositions._check_file(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise pending.PendingSend(metadata['profileSha256']) from None
            value = ledger._read(directory)
            available = {(v['kind'], v['recordSha256']): v for v in value['entries']}
            path = core.owner.STATE / 'persistent/admission.json'
            try:
                old = json.loads(core.owner.read_private(path, admission.MAX_BYTES), object_pairs_hook=admission._unique_object)
            except FileNotFoundError:
                old = None
            if old is not None:
                admission.validate_admission(old)
                _require(old['profileSha256'] == value['profileSha256'] and old['ledgerRevision'] <= value['revision'])
                _require(all(available.get((e['kind'], e['recordSha256'])) == e for e in old['entries']))
            selected = []
            waiting = pending.read(core)
            if waiting:
                _require(waiting['profileSha256'] == value['profileSha256'])
                _require(all((e['nativePid'], e['nativeTicks']) ==
                         (pinned.host.client_pid, pinned.host.client_ticks) for e in waiting['entries']))
            deferred = False
            for kind, row in admission.pending_records(db):
                key = (kind, admission.fingerprint(metadata['account'], kind, row))
                entry = available.get(key)
                if entry is not None and admission._safe_pending(kind, row, entry):
                    selected.append(entry)
                    continue
                tracked = [e for e in (waiting or {}).get('entries', []) if e['kind'] == kind
                           and module._matching(db, e['audit'], kind) == row]
                _require(bool(tracked))
                # _record distinguishes genuinely running rows from corruption.
                try:
                    module._record(core, db, metadata, tracked[0]['audit'], kind, 'unconfirmed_quarantined', record=False)
                except pending.PendingSend:
                    deferred = True
                else:
                    raise ValueError('persistent_send_evidence')
            verify()
            if deferred:
                return False
            result = dict(schemaVersion=1, profileSha256=value['profileSha256'], ledgerRevision=value['revision'], entries=selected)
            admission.validate_database(db, metadata['account'], result)
            if result != old:
                module.write_evidence(core, path, result)
            return True
        finally:
            os.close(lock)
