"""Root recovery capture. The single broker loop calls capture synchronously.

No broker tick/forward/ACK occurs from before checkpoint_copy through backup.
Native worker has ONE ACK-gated pending source. Thus the copied checkpoint is
snapshot-contained and only the last admitted row can precede its cursor commit
(including an already running old worker). Retaining that row is conservative;
highSeq+1 would be unsafe. Native maximum is never used as receive progress.
Lock order: recovery directory, ledger/manifest, short SQLite transactions. The
owner only produces raw archives; it never waits for this coordination lock.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import tempfile
import time

if not __package__:
    name = '_wechat_recovery_private'
    if name not in sys.modules:
        spec = importlib.util.spec_from_loader(name, loader=None, is_package=True)
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(Path(__file__).resolve().parent)]
        sys.modules[name] = package
    __package__ = name
try:
    import wechat_bridge
except ImportError:
    spec = importlib.util.spec_from_file_location('wechat_bridge',
        Path(__file__).resolve().parents[1]/'wechat_bridge/__init__.py',
        submodule_search_locations=[str(Path(__file__).resolve().parents[1]/'wechat_bridge')])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
snapshot = importlib.import_module(__package__+'.recovery_snapshot')
manifests = importlib.import_module(__package__+'.recovery_manifest')
dispositions = importlib.import_module(__package__+'.send_dispositions')
Spool = manifests.Spool
BRIDGE_PUBLIC = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite')
BRIDGE_PRIVATE = Path('/var/lib/private/astrbot-wechat-bridge/private-forward.sqlite')


@contextmanager
def locked(state_dir):
    with snapshot.state._directory(Path(state_dir), private=True) as directory:
        fd = os.open('recovery.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC, 0o600, dir_fd=directory)
        try:
            snapshot.state._check_object(os.fstat(fd), stat.S_ISREG, uid=os.geteuid(), mode=0o600)
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


@contextmanager
def bridge(path):
    """Existing DB only, no constructors, schema migration or inbox repairs."""
    try:
        with _bridge_connection(path) as spool:
            yield spool
    except Exception:
        raise ValueError('recovery_bridge') from None


@contextmanager
def _bridge_connection(path):
    path = Path(path).absolute()
    with bridge_directory(path) as (path,directory,uid):
        fd = os.open(path.name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            snapshot.state._check_object(before, stat.S_ISREG, uid=uid, mode=0o600)
            if before.st_size > 8*1024*1024:
                raise ValueError()
            for suffix in ('-wal','-shm','-journal'):
                try: side = os.stat(path.name+suffix,dir_fd=directory,follow_symlinks=False)
                except FileNotFoundError: continue
                snapshot.state._check_object(side,stat.S_ISREG,uid=uid,mode=0o600)
                if side.st_size > 8*1024*1024: raise ValueError()
            db = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=1)
            try:
                db.execute('PRAGMA trusted_schema=OFF')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA cache_size=-2048')
                db.execute('PRAGMA mmap_size=0')
                now = path.stat(follow_symlinks=False)
                if (before.st_dev,before.st_ino)!=(now.st_dev,now.st_ino): raise ValueError()
                # Reuse only recovery methods; never call Spool.__init__.
                spool = object.__new__(Spool)
                spool.db, spool.path = db, path
                yield spool
            finally:
                db.close()
        finally:
            os.close(fd)


@contextmanager
def bridge_directory(path):
    """Only systemd's exact service alias can name a service-owned directory.

    DynamicUser StateDirectory may be owned by the service UID or nobody outside
    its idmapped mount. Do not chown it. All ancestors of this fixed host path
    are root-owned/no-follow; final directory and SQLite files share one owner.
    Every other path retains ordinary caller-owned private-directory validation.
    """
    if path not in (BRIDGE_PUBLIC,BRIDGE_PRIVATE):
        with snapshot.state._directory(path.parent,private=True) as fd:
            yield path,fd,os.geteuid()
        return
    if os.geteuid()!=0: raise ValueError()
    if path==BRIDGE_PUBLIC and path.parent.is_symlink():
        link=path.parent.lstat()
        if link.st_uid!=0: raise ValueError()
        target=os.readlink(path.parent)
        if not os.path.isabs(target): target=os.path.join(path.parent.parent,target)
        if os.path.normpath(target)!=str(BRIDGE_PRIVATE.parent): raise ValueError()
        path=BRIDGE_PRIVATE
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        for index,part in enumerate(path.parent.parts[1:]):
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            os.close(fd); fd=child
            info=os.fstat(fd)
            if index<len(path.parent.parts)-2:
                snapshot.state._check_object(info,stat.S_ISDIR,uid=0)
        snapshot.state._check_object(info,stat.S_ISDIR,mode=0o700)
        yield path,fd,info.st_uid
    finally:
        os.close(fd)


def backup(spool, destination):
    fd = os.open(destination, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    os.close(fd)
    target = sqlite3.connect(destination)
    try:
        spool.db.backup(target)
    finally:
        target.close()
    with open(destination, 'rb') as raw: os.fsync(raw.fileno())
    with snapshot.state._directory(Path(destination).parent, private=True) as directory: os.fsync(directory)


@contextmanager
def ledger_locked(ledger):
    with ledger._directory() as directory:
        fd = os.open(ledger.path.name+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory)
        try:
            dispositions._check_file(fd)
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def replay_barrier(spool):
    if not spool.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_rebase'").fetchone():
        return None
    rows = spool.db.execute('SELECT floor_seq,high_seq,checkpoint FROM recovery_rebase').fetchmany(2)
    if not rows: return None
    if len(rows)!=1: raise ValueError()
    floor, high, raw = rows[0]
    if type(floor) is not int or type(high) is not int or not 1<=floor<=high+1 or not isinstance(raw,str) or len(raw)>256:
        raise ValueError()
    boundary = json.loads(raw,object_pairs_hook=snapshot.admission._unique_object)
    # A merged discarded range has no single canonical checkpoint witness.
    # JSON null requires exact source reconciliation of the entire range.
    if boundary is not None: snapshot.boundary(boundary)
    return floor,high,boundary


class ForwarderCapture:
    """Adapter owned by the actual synchronous root forwarding loop.

    Construct after native_ready verified the accepted seed against Native GET.
    Call only between broker iterations with no active outgoing request or input
    image transfer. No caller-supplied 'paused' flag substitutes for this callflow.
    """
    def __init__(self, owner, pinned, checkpoint_name, seed):
        if not isinstance(checkpoint_name,str) or not 0<len(checkpoint_name)<=128: raise ValueError('recovery_checkpoint')
        self.owner, self.pinned = owner, pinned
        self.checkpoint_name, self.seed = checkpoint_name, snapshot.proof(seed)

    def profile(self):
        keys = {}
        for name in ('api-token','storage-key','cursor-key'):
            fd = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=self.pinned.host.data_fd)
            try:
                info = os.fstat(fd)
                snapshot.state._check_object(info,stat.S_ISREG,uid=self.pinned.host.client_uid,mode=0o600)
                raw = os.read(fd,8193)
                if not 0<len(raw)<=8192 or len(raw)!=info.st_size: raise ValueError()
                keys[name] = hashlib.sha256(raw).hexdigest()
            finally:
                os.close(fd)
        return snapshot.admission.profile_sha256(keys)

    @contextmanager
    def copied(self, bridge_path, directory):
        # No SQLite write transaction is open while waiting on owner mailbox.
        profile = self.profile()
        with self.owner.checkpoint_copy(self.pinned) as archive:
            with bridge(bridge_path) as live:
                backup(live, Path(directory)/'capture.sqlite')
            if not self.pinned.unchanged() or self.profile()!=profile: raise ValueError()
            yield archive, Path(directory)/'capture.sqlite'


def capture(owner_host, bridge_path, state_dir):
    """Publish a causally witnessed generation; return verified (manifest,zip).

    Fresh capture can bootstrap a pruned legacy spool: root pause and sole pending
    frame prove replay starts no earlier than its last row, independently of old
    row counts. A numeric legacy cursor needs its real stored canonical anchor.
    Missing/expired anchor fails; no latest-event fallback is permitted.
    """
    try:
        if not isinstance(owner_host, ForwarderCapture): raise ValueError()
        store = manifests.Store(Path(state_dir)/'recovery')
        with locked(state_dir):
            # Install retention before the raw checkpoint. Existing metadata may
            # only advance AFTER pointer commit, never before capture.
            with bridge(bridge_path) as live:
                coverage = live.recovery_coverage()
                epoch = coverage['epoch'] or secrets.token_hex(16)
                if coverage['floorSeq'] is None:
                    live.protect_recovery(coverage['retainedFloor'], epoch)
            with tempfile.TemporaryDirectory(prefix='wechat-recovery-capture.') as directory:
                with owner_host.copied(bridge_path, directory) as (archive, copied):
                    spool = Spool(copied)  # PRIVATE copy; live publishing is untouched.
                    try:
                        raw = spool.cursor(owner_host.checkpoint_name)
                        if raw is None: raise ValueError()
                        checkpoint = json.loads(raw) if raw.startswith('{') else {'cursor':raw,'anchor':None}
                        if not isinstance(checkpoint,dict) or set(checkpoint)!={'cursor','anchor'}: raise ValueError()
                        snapshot.cursor(checkpoint['cursor'])
                        if checkpoint['anchor'] is None:
                            # A profile-token generation survives Native rollback:
                            # its bare number cannot prove which event it meant.
                            current_native = 'native:'+str(owner_host.pinned.host.client_pid)+':'+str(owner_host.pinned.host.client_ticks)
                            if owner_host.checkpoint_name != current_native: raise ValueError()
                        with snapshot.copied_database(archive) as (native, info):
                            if info['profileSha256']!=owner_host.profile(): raise ValueError()
                            canonical = snapshot._event(native, int(checkpoint['cursor']))
                            if canonical is None or checkpoint['anchor'] not in (None,canonical['anchor']): raise ValueError()
                            barrier = replay_barrier(spool)
                            barrier_complete = (barrier is None or
                                barrier[2] is not None and
                                int(canonical['cursor'])>=int(barrier[2]['cursor']) and
                                snapshot._event(native,int(barrier[2]['cursor']))==barrier[2])
                        spool.cursor(owner_host.checkpoint_name, json.dumps(canonical,separators=(',',':')))
                        coverage = spool.recovery_coverage()
                        floor = max(1, coverage['highSeq'])
                        # Pre-rebase source IDs can belong to the discarded Native
                        # suffix. A fresh root pause proves no containment for
                        # those historical rows. Keep their floor until the old
                        # canonical checkpoint is present AND acknowledged again;
                        # reused event numbers alone never release the barrier.
                        if not barrier_complete:
                            floor = min(floor,barrier[0])
                        ledger = dispositions.Ledger(Path(state_dir)/'send-dispositions.json',info['profileSha256'])
                        # Bind exact pending-only inventory to immutable selected
                        # ledger entries; never bless queued/running Native sends.
                        with ledger_locked(ledger):
                            with snapshot.copied_database(archive) as (native, _):
                                ticket = dispositions.admission_for(native,info['account'],ledger)
                            pair = store.publish(archive, dict(epoch=epoch, profileSha256=info['profileSha256'],
                                ledgerRevision=ticket['ledgerRevision'], bridgeBoundary=canonical,
                                dedup=dict(floorSeq=floor,highSeq=coverage['highSeq']), selfProof=owner_host.seed,
                                createdAt=datetime.now(timezone.utc).isoformat(),
                                capture=dict(spool=spool,checkpointName=owner_host.checkpoint_name)))
                    finally:
                        spool.close()
            # Read both committed generations even after an idempotent publish.
            floors = [p[0]['dedup']['floorSeq'] for p in (store.current(),store.previous()) if p]
            with bridge(bridge_path) as live:
                live.protect_recovery(min(floors),epoch)
                if barrier is not None and barrier_complete:
                    with live.db:
                        live.db.execute('DELETE FROM recovery_rebase')
            return pair
    except Exception:
        raise ValueError('recovery_checkpoint') from None


def rebase_bridge(bridge_path, manifest, expected_generation):
    """Backup latest bridge, then change ONLY its named receive checkpoint.

    expected_generation is root-only {epoch,profileSha256,checkpointName,get_event}.
    get_event(cursor) must return the actual restored API's canonical event, not
    a boolean assertion. Caller excludes broker/maintenance admission throughout.
    """
    try:
        if set(expected_generation)!={'epoch','profileSha256','checkpointName','get_event'}: raise ValueError()
        value, archive = manifest
        store = manifests.Store(Path(archive).parent)
        state_dir = Path(archive).parent.parent
        with locked(state_dir), bridge(bridge_path) as live:
            plan = store.recovery_plan(dict(spool=live,epoch=expected_generation['epoch'],
                profileSha256=expected_generation['profileSha256'],checkpointName=expected_generation['checkpointName']),manifest)
            boundary = plan['boundary']
            prior = json.loads(live.cursor(expected_generation['checkpointName']),object_pairs_hook=snapshot.admission._unique_object)
            snapshot.boundary(prior)
            event = expected_generation['get_event'](boundary['cursor'])
            if (not isinstance(event,dict) or event.get('cursor')!=boundary['cursor']
                    or snapshot.event_digest(event)!=boundary['anchor']): raise ValueError()
            # One bounded fixed backup replaces the previous recovery backup.
            with tempfile.TemporaryDirectory(prefix='rebase.',dir=state_dir) as directory:
                saved = Path(directory)/'bridge.sqlite'
                backup(live,saved)
                previous = Path(state_dir)/'receive-before.sqlite'
                if previous.exists() or previous.is_symlink():
                    snapshot.state._check_object(previous.lstat(),stat.S_ISREG,uid=os.geteuid(),mode=0o600)
                os.replace(saved,previous)
                with snapshot.state._directory(Path(state_dir),private=True) as fd: os.fsync(fd)
            live.db.execute('BEGIN IMMEDIATE')
            try:
                # Recheck current coverage after backup; no writes to inbox,
                # submissions, identities, uncertain or publishing states.
                if not manifests._covered(live.recovery_coverage(),value): raise ValueError()
                coverage = live.recovery_coverage()
                if (int(prior['cursor'])>int(boundary['cursor']) or
                        coverage['highSeq']>value['dedup']['highSeq']):
                    live.db.execute('CREATE TABLE IF NOT EXISTS recovery_rebase('
                        'singleton INTEGER PRIMARY KEY CHECK(singleton=1),floor_seq INTEGER NOT NULL,'
                        'high_seq INTEGER NOT NULL,checkpoint TEXT NOT NULL)')
                    previous=replay_barrier(live)
                    floor,high,anchor=coverage['floorSeq'],coverage['highSeq'],prior
                    if previous is not None:
                        floor,high=min(floor,previous[0]),max(high,previous[1])
                        # An old checkpoint cannot prove containment of new
                        # sources discarded by a subsequent rollback.
                        anchor=previous[2] if (floor,high)==previous[:2] else None
                    if high-floor+1>4096: raise ValueError()
                    live.db.execute('INSERT INTO recovery_rebase VALUES(1,?,?,?) '
                        'ON CONFLICT(singleton) DO UPDATE SET floor_seq=excluded.floor_seq,'
                        'high_seq=excluded.high_seq,checkpoint=excluded.checkpoint',
                        (floor,high,json.dumps(anchor,separators=(',',':'))))
                live.cursor(expected_generation['checkpointName'],json.dumps(boundary,separators=(',',':')))
                live.db.commit()
            except Exception:
                live.db.rollback()
                raise
            return plan
    except Exception:
        raise ValueError('recovery_rebase') from None


def _source_event(native, source, through):
    # SDK events have plaintext source IDs; message GET supplies decrypted data.
    # No event query-by-message-ID API, media transfer, or send is needed.
    row = native.execute("SELECT sequence FROM events WHERE sequence<=? AND kind='message.committed' AND "
        "CASE WHEN length(cast(data_json AS BLOB))<=? AND json_valid(data_json) "
        "THEN json_extract(data_json,'$.messageId') END=? ORDER BY sequence DESC LIMIT 1",
        (int(through),snapshot.MAX_EVENT_BYTES,source)).fetchone()
    if row is None: raise ValueError()
    snapshot._event(native,row[0])  # Check all field sizes/types before reading.
    kind,raw,created=native.execute('SELECT kind,data_json,created_at FROM events WHERE sequence=?',row).fetchone()
    return dict(cursor=str(row[0]),kind=kind,data=json.loads(raw),createdAt=created)


def reconcile_replay(bridge_path, manifest, expected_generation, get_source):
    """Root-only bounded alternative when old nonmessage anchors cannot return.

    get_source(source, snapshot_event, deadline) returns the normalized admission
    DATA from actual authenticated Native message GET, never a boolean. The
    supplied event is reconstructed from this committed immutable Native copy;
    no query-by-source events API is assumed. Caller excludes bridge ingress and
    enforces the absolute deadline on each GET. Total budget is fifteen seconds.

    Every original seq in [barrier.floorSeq,barrier.highSeq] is required, including
    sources already in the selected snapshot. All must exist in this fresh copy
    before its acknowledged bridgeBoundary and match the exact durable digest.
    Nickname/card are ignored as at admission. Images/quoted images require the
    original normalized metadata/content digest; never download media to invent
    missing proof. Missing/unavailable proof retains protection and requires_review;
    retry only with a later fresh capture. No partial reconciliation is committed.
    """
    try:
        if set(expected_generation)!={'epoch','profileSha256','checkpointName','get_event'}: raise ValueError()
        value,archive=manifest
        store=manifests.Store(Path(archive).parent)
        with locked(Path(archive).parent.parent), bridge(bridge_path) as live:
            store.recovery_plan(dict(spool=live,epoch=expected_generation['epoch'],
                profileSha256=expected_generation['profileSha256'],checkpointName=expected_generation['checkpointName']),manifest)
            barrier=replay_barrier(live)
            if barrier is None: return {'verified':0}
            floor,high,_=barrier
            if high-floor+1>4096: raise ValueError()
            rows=live.db.execute('SELECT seq,source,digest FROM inbox WHERE seq BETWEEN ? AND ? ORDER BY seq',(floor,high)).fetchall()
            if [row[0] for row in rows]!=list(range(floor,high+1)): raise ValueError()
            checkpoint=live.cursor(expected_generation['checkpointName'])
            deadline=time.monotonic()+15
            with snapshot.copied_database(archive) as (native,metadata):
                if metadata['profileSha256']!=expected_generation['profileSha256']: raise ValueError()
                native.set_progress_handler(lambda: int(time.monotonic()>=deadline),1000)
                for _,source,digest in rows:
                    if time.monotonic()>=deadline: raise ValueError()
                    event=_source_event(native,source,value['bridgeBoundary']['cursor'])
                    data=get_source(source,event,deadline)
                    if (time.monotonic()>=deadline or not isinstance(data,dict) or data.get('id')!=source
                            or event['data'].get('direction')!='incoming' or event['data'].get('origin')!='live'
                            or event['data'].get('threadId')!=data.get('room_id')
                            or data.get('account')!=metadata['account'] or Spool.source_digest(data)!=digest):
                        raise ValueError()
            live.db.execute('BEGIN IMMEDIATE')
            try:
                if (replay_barrier(live)!=barrier or live.cursor(expected_generation['checkpointName'])!=checkpoint
                        or not manifests._covered(live.recovery_coverage(),value)): raise ValueError()
                live.db.execute('DELETE FROM recovery_rebase')
                live.db.commit()
            except Exception:
                live.db.rollback(); raise
            return {'verified':len(rows)}
    except Exception:
        raise ValueError('recovery_reconcile') from None


def validate_maintenance_snapshot(source, candidate, state_dir):
    """Media cleanup preserves canonical history, pending rows and both archives."""
    directory = Path(state_dir)/'recovery'
    if not directory.exists(): return
    store = manifests.Store(directory)
    for pair in (store.current(),store.previous()):
        if pair is None: continue
        value = pair[0]
        boundaries = [value['nativeBoundary'],value['bridgeBoundary']]
        before = snapshot.inspect(source,self_proof_candidates=[value['selfProof']],boundaries=boundaries)
        after = snapshot.inspect(candidate,self_proof_candidates=[value['selfProof']],boundaries=boundaries)
        for key in ('keys','profileSha256','nativeBoundary','pending','selfProof'):
            if before[key]!=after[key]: raise ValueError('recovery_maintenance')
        if after['profileSha256']!=value['profileSha256'] or after['selfProof']!=value['selfProof']:
            raise ValueError('recovery_maintenance')
