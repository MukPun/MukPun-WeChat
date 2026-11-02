"""Offline bounded proof over a verified saved archive, including copied WAL.

self_proof_candidates is a root-only input of already accepted seeds previously
checked with loop_guard.verified_sender against Native GET results. Offline we
bind each candidate to the snapshot's accepted send, server receipt, destination
and completed SDK projection marker. This is NOT decryption or independent
verification of text: encrypted payload_hash is never used as textSha256. The
restored Native must again pass verified_sender before owner startup proceeds.
No raw key, message, send row or event data is returned or logged.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

try:
    from . import session_state_store as state
    from . import restore_admission as admission
    from .forward_sse import event_digest, cursor
except ImportError:
    import session_state_store as state
    import restore_admission as admission
    from forward_sse import event_digest, cursor
from wechat_bridge.loop_guard import proof

MAX_EVENT_BYTES = 65536
MAX_CANDIDATES = 128


def boundary(value):
    if (not isinstance(value, dict) or set(value) != {'cursor','anchor'}
            or not admission._sha(value['anchor']) or cursor(value['cursor']) == '0'):
        raise ValueError('recovery_snapshot')
    return value


@contextmanager
def archive_file(path):
    """Pin ancestors and file; detect mutation across all streaming reads."""
    path = Path(path)
    with state._directory(path.parent, private=True) as parent:
        fd = os.open(path.name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC, dir_fd=parent)
    with os.fdopen(fd, 'rb') as raw:
        before = os.fstat(raw.fileno())
        state._check_object(before, stat.S_ISREG, uid=os.geteuid(), mode=0o600)
        if before.st_size > state.MAX_ARCHIVE_BYTES:
            raise ValueError('recovery_snapshot')
        yield raw
        after = os.fstat(raw.fileno())
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ValueError('recovery_snapshot')


def _digest(raw):
    raw.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := raw.read(state.CHUNK):
        size += len(chunk)
        if size > state.MAX_ARCHIVE_BYTES:
            raise ValueError('recovery_snapshot')
        digest.update(chunk)
    raw.seek(0)
    return digest.hexdigest()


def _event(db, sequence=None):
    where, args = (' WHERE sequence=?', (sequence,)) if sequence is not None else (' ORDER BY sequence DESC LIMIT 1', ())
    sizes = db.execute('SELECT length(cast(kind AS BLOB)),length(cast(data_json AS BLOB)),length(cast(created_at AS BLOB)) FROM events'+where,args).fetchone()
    if sizes is None:
        return None
    if any(type(v) is not int or v < 0 for v in sizes) or sum(sizes)>MAX_EVENT_BYTES-128:
        raise ValueError('recovery_snapshot')
    row=db.execute('SELECT sequence,kind,data_json,created_at FROM events'+where,args).fetchone()
    if (type(row[0]) is not int or row[0]<=0 or not isinstance(row[1],str)
            or not isinstance(row[2],str) or not isinstance(row[3],str)):
        raise ValueError('recovery_snapshot')
    # SDK Event is camelCase; created_at is passed through verbatim, not parsed.
    # SDK maps invalid stored JSON to null. JSON constants are invalid in serde.
    try:
        data=json.loads(row[2],parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except ValueError:
        data=None
    value=dict(cursor=str(row[0]),kind=row[1],data=data,createdAt=row[3])
    return boundary(dict(cursor=value['cursor'],anchor=event_digest(value)))


def _seed(db, candidates):
    for seed in candidates:
        proof(seed)
        # Bound every selected field before SQLite materializes it.
        row=db.execute("SELECT destination,state,server_id FROM outbox WHERE id=? AND "
                       "length(cast(destination AS BLOB))<=8192 AND length(state)<=32 AND "
                       "length(cast(server_id AS BLOB)) BETWEEN 1 AND 8192",(seed['sendId'],)).fetchone()
        if not row or row[1]!='accepted' or not isinstance(row[0],str) or not isinstance(row[2],str):
            continue
        if hashlib.sha256(row[0].encode()).hexdigest()!=seed['destinationSha256']:
            continue
        marker=db.execute('SELECT value FROM runtime WHERE key=? AND length(value) BETWEEN 1 AND 8192',
                          ('send-projected:'+seed['sendId'],)).fetchone()
        if marker and isinstance(marker[0],bytes):
            marker[0].decode('utf-8')  # Same UTF-8 projection receipt requirement as read_send.
            return dict(seed)
    return None


def inspect(snapshot, *, self_proof_candidates=(), boundaries=(), lineage_validator=None):
    """Return hashes, exact event anchors, pending fingerprints and accepted seed.

    boundaries supplies at most two {cursor,anchor} values to prove in the same
    SQLite read transaction as nativeBoundary. No event means nativeBoundary=None;
    callers may inspect lineage with lineage_validator(profileSha256)->True but
    publication always requires a real event. Temporary copied DB/WAL stays private.
    """
    try:
        if (not isinstance(self_proof_candidates,(list,tuple)) or len(self_proof_candidates)>MAX_CANDIDATES
                or not isinstance(boundaries,(list,tuple)) or len(boundaries)>2):
            raise ValueError()
        for value in self_proof_candidates:
            proof(value)
        for value in boundaries:
            boundary(value)
        with archive_file(snapshot) as raw, state._checked_zip(raw) as archive:
            state._archive_manifest(archive)
            digest=_digest(raw)
            names=archive.namelist()
            databases=[n for n in names if n.endswith('/agent/control.sqlite')]
            if len(databases)!=1:
                raise ValueError()
            name=databases[0]
            parts=name.split('/')
            if len(parts)!=4 or parts[0]!='accounts':
                raise ValueError()
            account=parts[1]
            keys={}
            for key in ('api-token','storage-key','cursor-key'):
                entry=archive.getinfo(key)
                if not 0<entry.file_size<=8192:
                    raise ValueError()
                keys[key]=hashlib.sha256(archive.read(key)).hexdigest()
            profile=admission.profile_sha256(keys)
            if lineage_validator is not None and lineage_validator(profile) is not True:
                raise ValueError()
            with tempfile.TemporaryDirectory(prefix='wechat-recovery-proof.') as directory:
                work=Path(directory)
                for suffix in ('','-wal','-shm','-journal'):
                    if name+suffix in names:
                        fd=os.open(work/('control.sqlite'+suffix),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                        with os.fdopen(fd,'wb') as target:
                            state._copy_member(archive,archive.getinfo(name+suffix),target)
                db=sqlite3.connect(work/'control.sqlite',timeout=.2)
                try:
                    db.execute('PRAGMA trusted_schema=OFF')
                    db.execute('PRAGMA cache_size=-2048')
                    db.execute('PRAGMA mmap_size=0')
                    if db.execute('PRAGMA quick_check').fetchmany(2)!=[('ok',)]:
                        raise ValueError()
                    db.execute('BEGIN')
                    tables=dict(db.execute("SELECT name,type FROM sqlite_master WHERE name IN ('events','runtime')"))
                    if tables!={'events':'table','runtime':'table'}:
                        raise ValueError()
                    native=_event(db)
                    for value in boundaries:
                        if _event(db,int(value['cursor']))!=value:
                            raise ValueError()
                    pending=[dict(kind=kind,recordSha256=admission.fingerprint(account,kind,row))
                             for kind,row in admission.pending_records(db)]
                    selected=_seed(db,self_proof_candidates)
                finally:
                    db.close()
            return dict(keys=keys,profileSha256=profile,snapshotSha256=digest,
                        nativeBoundary=native,pending=pending,selfProof=selected)
    except Exception:
        raise ValueError('recovery_snapshot') from None
