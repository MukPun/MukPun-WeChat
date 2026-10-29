"""Offline-only GC for a private copy of beta.8 sidecar SQLite.

Never call on a running Native database. Callers must retain the original
snapshot and install a reviewed copy only while the Native owner is stopped.
Login keys, profiles, outbox, events and job records are never modified.
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import copy
import os
import shutil
import tempfile
import zipfile
import stat

SOURCE_TTL = 86400
PRESSURE_AGE = 60
KEEP_BYTES = 32 * 1024 * 1024
MAX_ROWS = 20000


def _digest_tables(db):
    result={}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if name == 'media_sources':continue
        if not re.fullmatch('[a-zA-Z_][a-zA-Z_0-9]*',name):raise ValueError('cache_schema')
        h=hashlib.sha256()
        for row in db.execute('SELECT * FROM "'+name+'" ORDER BY rowid'):
            for item in row:
                raw = (b'B'+item if isinstance(item,bytes) else b'J'+json.dumps(item,ensure_ascii=True).encode())
                h.update(len(raw).to_bytes(8,'big'));h.update(raw)
        result[name]=h.hexdigest()
    return result


def _accepted(job,sha):
    state,phase,data=job
    result=data.get('result') or {}
    receipt=result.get('nativeReceipt') or {}
    preparation=result.get('imagePreparation') or {}
    return (state=='accepted' and phase=='completed' and result.get('profileProjection')=='complete'
            and receipt.get('status')=='server_accepted' and type(receipt.get('serviceCode')) is int
            and receipt.get('serviceCode')==0
            and receipt.get('clientIdMatches') is True and bool(receipt.get('serverMessageId'))
            and preparation.get('sourceSha256')==sha and result.get('uploadReadbackVerified') is True)


def _server_id(value):
    return (isinstance(value,str) and re.fullmatch(r'[1-9][0-9]{0,18}',value) is not None
            and int(value) <= 9223372036854775807)


def _voice_accepted(job):
    state,phase,data=job
    result=data.get('result')
    if not isinstance(result,dict):return False
    receipt=result.get('nativeReceipt')
    chunks=result.get('chunks')
    ids=result.get('messageIds')
    if (state!='accepted' or phase!='completed' or result.get('profileProjection')!='complete'
            or result.get('officialMediaWriteback')!='complete' or not isinstance(receipt,dict)
            or receipt.get('status')!='server_accepted' or type(receipt.get('serviceCode')) is not int
            or receipt['serviceCode']!=0 or receipt.get('clientIdMatches') is not True
            or not _server_id(data.get('serverMessageId'))
            or receipt.get('serverMessageId')!=data['serverMessageId']
            or not isinstance(chunks,list) or not 1<=len(chunks)<=5
            or not isinstance(ids,list) or len(ids)!=len(chunks)
            or any(not isinstance(item,str) or not item for item in ids)
            or len(set(ids))!=len(ids)):return False
    servers=[]
    total=0
    for index,chunk in enumerate(chunks):
        if (not isinstance(chunk,dict) or type(chunk.get('index')) is not int or chunk['index']!=index
                or chunk.get('state')!='accepted' or type(chunk.get('durationMs')) is not int
                or not 1<=chunk['durationMs']<=60000 or not _server_id(chunk.get('serverMessageId'))):return False
        servers.append(chunk['serverMessageId'])
        total+=chunk['durationMs']
    return total<=300000 and len(set(servers))==len(servers) and servers[-1]==data['serverMessageId']


def _keep_bytes(value):
    if type(value) is not int or not 0<=value<=KEEP_BYTES:raise ValueError('cache_keep_bytes')


def prune_database(path, *, offline=False, now=None, pressure=False, protected_sources=(), keep_bytes=KEEP_BYTES):
    """Mutate only an offline copy; return aggregate counters, never IDs/data."""
    if offline is not True:raise ValueError('cache_offline_copy_required')
    _keep_bytes(keep_bytes)
    now=time.time() if now is None else now
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('cache_database_path')
    db=sqlite3.connect(path,timeout=.2)
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA journal_mode=DELETE')
        if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('cache_database_integrity')
        before=_digest_tables(db)
        sources=db.execute('SELECT id,name,media_type,byte_length,sha256,created_at FROM media_sources').fetchmany(MAX_ROWS+1)
        if len(sources)>MAX_ROWS:raise ValueError('cache_inventory_bound')
        jobs={}
        job_bytes=0
        for count,(identity,kind,state,phase,raw) in enumerate(db.execute('SELECT id,kind,state,phase,data_json FROM media_jobs')):
            if count>=MAX_ROWS or not isinstance(raw,str) or len(raw)>65536:raise ValueError('cache_job_bound')
            job_bytes+=len(raw.encode())
            if job_bytes>16*1024*1024:raise ValueError('cache_job_bound')
            data=json.loads(raw)
            source=data.get('sourceId')
            if isinstance(source,str):jobs.setdefault(source,[]).append((state if kind=='send' else 'foreign',phase,data))
        from datetime import datetime
        eligible=[]
        protected=set(protected_sources)
        for identity,name,mime,size,sha,created in sources:
            image=(name in {'bridge-image.png','bridge-image.jpg','bridge-image.webp'}
                   and mime in {'image/png','image/jpeg','image/webp'})
            voice=name=='bridge-voice.wav' and mime=='audio/wav'
            if (identity in protected or not (image or voice) or not jobs.get(identity)
                    or not all(_voice_accepted(job) if voice else _accepted(job,sha)
                               for job in jobs[identity])):continue
            try:
                stamp=datetime.fromisoformat(created.replace('Z','+00:00'))
                if stamp.tzinfo is None:continue
                age=now-stamp.timestamp()
            except (ValueError,AttributeError):continue
            if age < (PRESSURE_AGE if pressure else SOURCE_TTL):continue
            eligible.append((created,identity,size,sha))
        total=sum(row[3] for row in sources)
        removed=asset_bytes=0
        with db:
            for _,identity,size,sha in sorted(eligible):
                if pressure and total<=keep_bytes:break
                # Preserve job/outbox metadata; only discard completed cache.
                db.execute('DELETE FROM media_sources WHERE id=?',(identity,))
                # Preserve SDK asset rows and managed profile files verbatim.
                # Their lifecycle is distinct from our completed upload cache.
                total-=size;removed+=1
            if _digest_tables(db)!=before:raise ValueError('cache_protected_data_changed')
        if removed:
            db.execute('VACUUM')
        if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)] or _digest_tables(db)!=before:
            raise ValueError('cache_database_integrity')
        return {'sources_removed':removed,'source_bytes_retained':total,'asset_bytes_released':asset_bytes}
    finally:db.close()


def rewrite_snapshot(source, destination, state_store, *, offline=False, pressure=False, keep_bytes=KEEP_BYTES):
    """Create a candidate only; caller owns stopped-state checks and install."""
    if offline is not True:raise ValueError('cache_offline_copy_required')
    _keep_bytes(keep_bytes)
    source,destination=Path(source),Path(destination)
    if destination.exists() or destination.is_symlink():raise ValueError('cache_candidate_exists')
    work=Path(tempfile.mkdtemp(prefix='astrbot-wechat-cache-copy.',dir='/var/tmp' if os.name=='posix' else None))
    os.chmod(work,0o700)
    try:
        if shutil.disk_usage(work).free < 4*state_store.MAX_SOURCE_BYTES:
            raise ValueError('cache_workspace_capacity')
        with zipfile.ZipFile(source) as archive:
            entries,_,_=state_store._archive_manifest(archive)
            databases=[e.filename for e in entries if e.filename.endswith('/agent/control.sqlite')]
            if len(databases)!=1:raise ValueError('cache_database_layout')
            name=databases[0]
            for suffix in ('','-wal','-shm','-journal'):
                if name+suffix in archive.namelist():
                    with archive.open(name+suffix) as src,(work/('control.sqlite'+suffix)).open('wb') as dst:
                        shutil.copyfileobj(src,dst,65536)
                    (work/('control.sqlite'+suffix)).chmod(0o600)
            result=prune_database(work/'control.sqlite',offline=True,pressure=pressure,keep_bytes=keep_bytes)
            with destination.open('xb') as raw:
                os.chmod(destination,0o600)
                with zipfile.ZipFile(raw,'w',compression=zipfile.ZIP_STORED,allowZip64=False) as target:
                    for entry in entries:
                        if entry.filename in {name+s for s in ('-wal','-shm','-journal')}:continue
                        info=copy.copy(entry)
                        if entry.is_dir():target.writestr(info,b'');continue
                        with target.open(info,'w') as dst:
                            if entry.filename==name:
                                with (work/'control.sqlite').open('rb') as src:shutil.copyfileobj(src,dst,65536)
                            else:
                                with archive.open(entry) as src:shutil.copyfileobj(src,dst,65536)
                raw.flush();os.fsync(raw.fileno())
        if destination.stat().st_size>state_store.MAX_ARCHIVE_BYTES:raise ValueError('cache_archive_bound')
        with zipfile.ZipFile(destination) as check:
            checked,_,_=state_store._archive_manifest(check)
            old={e.filename:(e.file_size,e.CRC) for e in entries if e.filename!=name and e.filename not in {name+s for s in ('-wal','-shm','-journal')}}
            new={e.filename:(e.file_size,e.CRC) for e in checked if e.filename!=name}
            if old!=new:raise ValueError('cache_protected_files_changed')
        return {**result,'snapshot_bytes_before':source.stat().st_size,'snapshot_bytes_after':destination.stat().st_size,
                'protected_files_verified':len(old)}
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        for suffix in ('','-wal','-shm','-journal'):(work/('control.sqlite'+suffix)).unlink(missing_ok=True)
        work.rmdir()


def maintain_store(snapshot_store, state_store, *, native_stopped=False):
    """Run at owner startup only, before creating its Native process.

    The existing current snapshot becomes previous only after the candidate is
    fully validated. Failure never overwrites the sole recoverable original.
    """
    if native_stopped is not True:
        raise ValueError('cache_native_must_be_stopped')
    import fcntl
    candidate = snapshot_store.path / '.media-clean.zip'
    with snapshot_store._private() as private:
        fcntl.flock(private, fcntl.LOCK_EX)
        original = snapshot_store._slot(private, 'current.zip')
        snapshot_store._slot(private, 'previous.zip')
        if original is None:
            return {'sources_removed': 0}
        if candidate.exists() or candidate.is_symlink():
            raise ValueError('cache_candidate_exists')
        source = snapshot_store.path / 'private/current.zip'
        try:
            result = rewrite_snapshot(source, candidate, state_store, offline=True,
                                      pressure=original.st_size >= 64*1024*1024)
            if not result['sources_removed']:
                return result
            current = snapshot_store._slot(private, 'current.zip')
            if (current.st_ino, current.st_size, current.st_mtime_ns) != (original.st_ino, original.st_size, original.st_mtime_ns):
                raise ValueError('cache_original_changed')
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise ValueError('cache_candidate_unsafe')
            os.replace('current.zip', 'previous.zip', src_dir_fd=private, dst_dir_fd=private)
            try:
                os.replace(candidate, 'current.zip', dst_dir_fd=private)
                os.fsync(private)
            except Exception:
                os.replace('previous.zip', 'current.zip', src_dir_fd=private, dst_dir_fd=private)
                raise
            return result
        finally:
            candidate.unlink(missing_ok=True)
