#!/usr/bin/env python3
"""Bootstrap the recovery manifest tree from the legacy receive metadata.

    check   read-only plan: resolves every input and reports what apply would do.
    apply   root only, broker excluded, full backups, every evidence rechecked.

This migration owns only recovery metadata: the saved snapshot, the private
send-disposition ledger, the bridge retention window and the manifest tree. It
never edits Native rows, outbox rows or inbox payloads, never repairs an
uncertain/publishing row, never initializes a login and never submits a send.
Every write is an atomic rename or an exclusive create, so an interrupted run
converges on the next one.
"""
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
BACKUPS = 'recovery-migration-backups'
RETAIN_BACKUPS = 4
MAX_CHECKPOINTS = 128
PROTECTED_WINDOW = 4096

core = None  # Bound by main(); tests inject a module before calling the steps.


def module(name):
    path = HERE / (name + '.py')
    spec = importlib.util.spec_from_file_location('_migration_' + name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


class Migration(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def fail(code):
    raise Migration(code)


def bind(value):
    global core
    core = value
    return value


def checked():
    return core.recovery_checkpoints


def snapshots():
    return checked().snapshot


def store():
    return checked().manifests.Store(core.owner.STATE / 'recovery')


def saved_snapshot():
    """The published current pair, else the newest saved checkpoint slot."""
    root = core.owner.STATE / 'recovery'
    current = store().current() if root.is_dir() else None
    if current is not None:
        return current[0]['snapshotSha256'], current[1]
    latest = snapshots().state.SnapshotStore(core.owner.STATE / 'checkpoints').latest()
    if latest is None:
        fail('migration_snapshot_missing')
    return None, latest


def bridge_lock():
    """Exclusive and non-blocking: success proves no broker holds the bridge."""
    try:
        descriptor = os.open(core.owner.STATE / 'bridge.lock',
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        fail('migration_bridge_active')
    try:
        checked().snapshot.state._check_object(os.fstat(descriptor), stat.S_ISREG,
                                               uid=os.geteuid(), mode=0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        fail('migration_bridge_active')
    except Exception:
        os.close(descriptor)
        fail('migration_bridge_unsafe')
    return descriptor


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def coverage(db):
    """The numbers recovery_coverage() reports, without creating its table."""
    if 'recovery_window' in tables(db):
        floor, epoch, lost = db.execute(
            'SELECT floor_seq,epoch,lost_through FROM recovery_window').fetchone()
    else:
        lost = db.execute(
            'SELECT max(coalesce((SELECT max(i.seq-1) FROM inbox i WHERE i.seq>1 AND '
            'NOT EXISTS(SELECT 1 FROM inbox p WHERE p.seq=i.seq-1)),0),'
            "CASE WHEN coalesce((SELECT seq FROM sqlite_sequence WHERE name='inbox'),0)>"
            "coalesce((SELECT max(seq) FROM inbox),0) THEN "
            "coalesce((SELECT seq FROM sqlite_sequence WHERE name='inbox'),0) ELSE 0 END)"
        ).fetchone()[0]
        floor, epoch = None, None
    first = db.execute('SELECT min(seq) FROM inbox').fetchone()[0]
    high = db.execute("SELECT seq FROM sqlite_sequence WHERE name='inbox'").fetchone()
    return dict(floorSeq=floor, retainedFloor=lost + 1, firstSeq=first,
                highSeq=high[0] if high else 0, epoch=epoch)


def checkpoint(db):
    """Pick the single newest canonical checkpoint under a bounded scan."""
    rows = db.execute('SELECT name,value FROM checkpoints LIMIT ?',
                      (MAX_CHECKPOINTS + 1,)).fetchall()
    if not rows:
        fail('migration_checkpoint_missing')
    if len(rows) > MAX_CHECKPOINTS:
        fail('migration_checkpoint_ambiguous')
    saved = dict(rows)
    canonical = [name for name in saved if str(name).startswith('profile:')]
    names = canonical if canonical else list(saved)
    if len(names) != 1:
        fail('migration_checkpoint_ambiguous')
    name = names[0]
    raw = saved[name]
    if not isinstance(raw, str) or not 0 < len(raw) <= 256:
        fail('migration_checkpoint_invalid')
    value = (json.loads(raw, object_pairs_hook=core.recovery_dispositions.admission._unique_object)
             if raw.startswith('{') else dict(cursor=raw, anchor=None))
    if not isinstance(value, dict) or set(value) != {'cursor', 'anchor'}:
        fail('migration_checkpoint_invalid')
    return name, value


def boundary_of(archive, value):
    """Resolve the real stored anchor. Never fall back to the newest event."""
    try:
        cursor = snapshots().cursor(value['cursor'])
    except Exception:
        fail('migration_checkpoint_invalid')
    with snapshots().copied_database(archive) as (native, _):
        found = snapshots()._event(native, int(cursor))
        if found is None or found['cursor'] != cursor:
            fail('migration_anchor_missing')
        if value['anchor'] is not None and value['anchor'] != found['anchor']:
            fail('migration_anchor_missing')
        return found


def profile_of(archive):
    """The same lineage identity the coordinator derives before every restore."""
    try:
        with snapshots().archive_file(archive) as raw, \
                snapshots().state._checked_zip(raw) as opened:
            token = opened.read('api-token').decode('ascii').strip()
        if not 0 < len(token) <= 4096:
            fail('migration_snapshot_invalid')
        return 'profile:' + hashlib.sha256(token.encode()).hexdigest()
    except Migration:
        raise
    except Exception:
        fail('migration_snapshot_invalid')


def seed_of():
    """The accepted send the restored Native must re-prove before startup."""
    path = core.owner.STATE / 'forward-send.json'
    if not path.exists() or path.is_symlink():
        fail('migration_forward_missing')
    try:
        audit = json.loads(core.owner.read_private(path),
                           object_pairs_hook=core.recovery_dispositions.admission._unique_object)
    except Exception:
        fail('migration_forward_invalid')
    if not isinstance(audit, dict) or audit.get('state') != 'accepted':
        fail('migration_forward_review')
    try:
        return core.loops.proof(dict(sendId=audit['sendId'],
                                     destinationSha256=audit['destinationSha256'],
                                     textSha256=audit['textSha256'], bytes=audit['bytes']))
    except Exception:
        fail('migration_forward_invalid')


def observe():
    """Resolve every input without writing anything, anywhere."""
    published, archive = saved_snapshot()
    live = checked().BRIDGE_PUBLIC
    with checked().bridge(live) as opened:
        if opened.db.execute(
                "SELECT count(*) FROM inbox WHERE state='publishing'").fetchone()[0]:
            fail('migration_bridge_publishing')
        window = coverage(opened.db)
        name, value = checkpoint(opened.db)
    boundary = boundary_of(archive, value)
    seed = seed_of()
    observed = snapshots().inspect(archive, self_proof_candidates=[seed])
    if observed['nativeBoundary'] is None:
        fail('migration_snapshot_empty')
    if observed['selfProof'] != seed:
        fail('migration_forward_invalid')
    if int(boundary['cursor']) > int(observed['nativeBoundary']['cursor']):
        fail('migration_snapshot_behind')
    if published is not None and published != observed['snapshotSha256']:
        fail('migration_snapshot_changed')
    ledger = core.recovery_dispositions.ledger_for(core, observed['profileSha256']).read()
    return dict(published=published, archive=archive, live=live, window=window,
                checkpoint=name, checkpointValue=value, boundary=boundary, seed=seed,
                observed=observed, ledgerRevision=ledger['revision'],
                generation=profile_of(archive))


def plan(report):
    """The non-sensitive summary printed by both modes."""
    observed = report['observed']
    return dict(stage='recovery_migration_plan', published=report['published'] is not None,
                snapshot=observed['snapshotSha256'], profileSha256=observed['profileSha256'],
                generation=report['generation'], checkpointName=report['checkpoint'],
                bridgeBoundary=report['boundary'], nativeBoundary=observed['nativeBoundary'],
                coverage=report['window'], pending=len(observed['pending']),
                selfProof=report['seed'], ledgerRevision=report['ledgerRevision'])


def backup(report, stamp):
    """Copy the snapshot, a consistent bridge DB, audits, hold, ledger, ticket."""
    root = core.owner.STATE / BACKUPS
    core.owner.private_directory(root)
    target = root / stamp
    core.owner.private_directory(target)
    directory = snapshots().state._directory
    maximum = snapshots().state.MAX_ARCHIVE_BYTES

    def put(name, raw):
        with directory(target, private=True) as handle:
            try:
                descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=handle)
            except FileExistsError:
                # A stamp names one immutable backup set; the first copy wins.
                return
            with os.fdopen(descriptor, 'wb') as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())

    with snapshots().archive_file(report['archive']) as raw:
        put('snapshot.zip', raw.read())
    with tempfile.TemporaryDirectory(prefix='migration-bridge.', dir=core.owner.STATE) as scratch:
        saved = Path(scratch) / 'bridge.sqlite'
        with checked().bridge(report['live']) as opened:
            checked().backup(opened, saved)
        put('bridge.sqlite', saved.read_bytes())
    names = ['send-dispositions.json', 'recovery-ticket.json', 'forward-image-hold.json']
    names += [name for name, _ in core.recovery_dispositions.AUDITS]
    names += [name + '.recovery.json' for name, _ in core.recovery_dispositions.AUDITS]
    for name in dict.fromkeys(names):
        path = core.owner.STATE / name
        if path.exists() and not path.is_symlink():
            put(name, core.owner.read_private(path, maximum))
    with directory(target, private=True) as handle:
        os.fsync(handle)
    for stale in sorted(os.listdir(root))[:-RETAIN_BACKUPS]:
        shutil.rmtree(root / stale)
    return target


def install_retention(report):
    """Create the owned retention row and its lineage epoch when absent."""
    with checked().bridge(report['live']) as opened:
        if 'recovery_window' not in tables(opened.db):
            with opened.db:
                opened.db.executescript(
                    'CREATE TABLE IF NOT EXISTS recovery_window('
                    'singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
                    'floor_seq INTEGER, epoch TEXT, lost_through INTEGER NOT NULL);'
                    'INSERT OR IGNORE INTO recovery_window SELECT 1,NULL,NULL,max('
                    'coalesce((SELECT max(i.seq-1) FROM inbox i WHERE i.seq>1 AND '
                    'NOT EXISTS(SELECT 1 FROM inbox p WHERE p.seq=i.seq-1)),0),'
                    "CASE WHEN coalesce((SELECT seq FROM sqlite_sequence WHERE name='inbox'),0)>"
                    "coalesce((SELECT max(seq) FROM inbox),0) THEN "
                    "coalesce((SELECT seq FROM sqlite_sequence WHERE name='inbox'),0) ELSE 0 END)")
        window = coverage(opened.db)
        floor = window['floorSeq']
        if floor is None:
            floor = window['retainedFloor']
        # A window that already lost everything it claims to retain cannot be
        # covered by any manifest; name the real problem before touching the
        # live bridge instead of failing later inside publication. The one
        # exception is a database that never admitted a message: the bridge
        # itself grants `floor_seq == highSeq+1` there (see protect_recovery)
        # and the published dedup floor collapses onto 1, so it stays coverable.
        headroom = window['highSeq'] == 0
        if floor < window['retainedFloor'] or (not headroom and floor > window['highSeq']):
            fail('migration_window_invalid')
        epoch = window['epoch'] or os.urandom(16).hex()
        if window['epoch'] is not None and (epoch != window['epoch'] or floor < window['floorSeq']):
            fail('migration_window_invalid')
        # The bridge owns its own retention invariants (retained floor bounds, the
        # 4096 admission gate and the eviction ledger); never hand-write the row.
        try:
            opened.protect_recovery(floor, epoch)
        except Exception as error:
            if str(error) == 'recovery_checkpoint_required':
                fail('migration_window_overflow')
            fail('migration_window_invalid')
    return dict(report, epoch=epoch, floorSeq=floor)


def capture(report):
    """A private consistent copy the manifest can be published from."""
    scratch = tempfile.mkdtemp(prefix='migration-capture.', dir=core.owner.STATE)
    try:
        with checked().bridge(report['live']) as opened:
            checked().backup(opened, Path(scratch) / 'bridge.sqlite')
        spool = checked().Spool(Path(scratch) / 'bridge.sqlite')
    except Exception:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    try:
        window = spool.recovery_coverage()
        if window['epoch'] != report['epoch'] or window['floorSeq'] is None:
            fail('migration_window_invalid')
        # Install the exact canonical anchor before the checkpoint is read.
        spool.cursor(report['checkpoint'],
                     json.dumps(report['boundary'], separators=(',', ':')))
    except Exception:
        spool.close()
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return spool, window, scratch


def apply(stamp=None):
    with contextlib.ExitStack() as stack:
        stack.callback(os.close, bridge_lock())
        if core.owner.owner_active():
            fail('migration_owner_active')
        try:
            stack.enter_context(checked().locked(core.owner.STATE))
        except BlockingIOError:
            fail('migration_recovery_active')
        report = observe()
        saved = backup(report, stamp or time.strftime('%Y%m%d-%H%M%S', time.gmtime()))
        report = install_retention(report)
        try:
            legacy = core.recovery_dispositions.import_legacy(core, report['archive'],
                                                              saved / 'legacy')
        except Exception:
            fail('migration_disposition')
        ledger = core.recovery_dispositions.ledger_for(
            core, report['observed']['profileSha256']).read()
        spool, window, scratch = capture(report)
        try:
            metadata = dict(
                epoch=report['epoch'], profileSha256=report['observed']['profileSha256'],
                ledgerRevision=ledger['revision'], bridgeBoundary=report['boundary'],
                dedup=dict(floorSeq=max(1, window['highSeq']), highSeq=window['highSeq']),
                selfProof=report['seed'],
                createdAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                capture=dict(spool=spool, checkpointName=report['checkpoint']))
            try:
                published = store().publish(report['archive'], metadata)
            except Exception:
                fail('migration_manifest')
        finally:
            spool.close()
            shutil.rmtree(scratch, ignore_errors=True)
        # Advance live retention only after the pointer commit.
        pairs = [pair for pair in (store().current(), store().previous()) if pair is not None]
        try:
            with checked().bridge(report['live']) as opened:
                opened.protect_recovery(min(pair[0]['dedup']['floorSeq'] for pair in pairs),
                                        report['epoch'])
        except Exception:
            fail('migration_retention')
        return dict(stage='recovery_migrated', backup=str(saved),
                    imported=legacy['imported'], matched=legacy['matched'],
                    replayed=report['published'] == report['observed']['snapshotSha256'],
                    manifest=hashlib.sha256(
                        core.recovery_dispositions.admission._encoded(published[0])).hexdigest(),
                    generation=report['generation'], checkpointName=report['checkpoint'],
                    bridgeBoundary=published[0]['bridgeBoundary'],
                    dedup=published[0]['dedup'], ledgerRevision=ledger['revision'])


def check():
    """Read-only: prove the exact apply plan resolves and would not refuse."""
    return plan(observe())


def main():
    if sys.platform != 'linux' or os.geteuid() != 0:
        print(json.dumps(dict(stage='recovery_migration', code='linux_root_required')))
        return 1
    if sys.argv[1:] not in (['check'], ['apply']):
        print(json.dumps(dict(stage='recovery_migration', code='usage',
                              usage=['check', 'apply'])))
        return 2
    bind(module('run_private_forwarder'))
    write = sys.argv[1:] == ['apply']
    try:
        report = apply() if write else check()
    except Migration as error:
        print(json.dumps(dict(stage='recovery_migration', code=error.code, write=write)))
        return 1
    except Exception as error:  # Never leak paths or raw records.
        print(json.dumps(dict(stage='recovery_migration', write=write,
                              code=str(getattr(error, 'code', None) or 'migration_failed')[:64])))
        return 1
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
