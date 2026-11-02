"""Root-private immutable recovery generations. No live owner integration.

publish's capture={spool,checkpointName} MUST refer to a private consistent bridge
copy made while ingress/ACKs are frozen with the Native snapshot. The owner must
establish that manifest.floorSeq protects every source after bridgeBoundary; a
numeric cursor or a recent 4096-row suffix is not that causal proof. Here we check
the actual protected coverage and durable checkpoint in one read transaction,
and check both event anchors in the recovered Native archive. Never open a live
bridge through a second Spool (its constructor repairs publishing rows).

epoch identifies a profile lineage, not a generation; refreshes retain it. Only
AFTER successful pointer publication may the owner advance Spool protection, to
at most min(current.dedup.floorSeq, previous.dedup.floorSeq). A failed fsync may
mean the pointer committed: re-read both verified generations before advancing.
"""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import recovery_snapshot as snapshot
from . import restore_admission as admission
from . import session_state_store as state
from wechat_bridge.durable import Spool
from wechat_bridge.loop_guard import proof

MAX_JSON = 65536
FIELDS = {'schemaVersion','epoch','profileSha256','snapshotSha256','ledgerRevision',
          'nativeBoundary','bridgeBoundary','dedup','selfProof','createdAt'}


def _epoch(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{32}',value) is not None


def _validate(value):
    if (not isinstance(value,dict) or set(value)!=FIELDS or
            type(value['schemaVersion']) is not int or value['schemaVersion']!=1 or
            not _epoch(value['epoch']) or not admission._sha(value['profileSha256']) or
            not admission._sha(value['snapshotSha256']) or
            type(value['ledgerRevision']) is not int or value['ledgerRevision']<0):
        raise ValueError()
    native=snapshot.boundary(value['nativeBoundary'])
    bridge=value['bridgeBoundary']
    if bridge is not None:
        snapshot.boundary(bridge)
        if int(bridge['cursor'])>int(native['cursor']): raise ValueError()
    dedup=value['dedup']
    if (not isinstance(dedup,dict) or set(dedup)!={'floorSeq','highSeq'} or
            any(type(n) is not int for n in dedup.values()) or
            not 1<=dedup['floorSeq']<=dedup['highSeq']+1<=2**63):
        raise ValueError()
    proof(value['selfProof'])
    created=value['createdAt']
    if not isinstance(created,str) or len(created)>64 or datetime.fromisoformat(created.replace('Z','+00:00')).tzinfo is None:
        raise ValueError()
    if len(admission._encoded(value))>MAX_JSON: raise ValueError()
    return value


@contextmanager
def _bridge(value):
    if (not isinstance(value,dict) or set(value)!={'spool','checkpointName'} or
            not isinstance(value['spool'],Spool) or
            not isinstance(value['checkpointName'],str) or not 0<len(value['checkpointName'])<=128):
        raise ValueError()
    spool=value['spool']
    # Do not commit, repair, or mutate a caller transaction.
    if spool.db.in_transaction: raise ValueError()
    spool.db.execute('BEGIN')
    try:
        coverage=spool.recovery_coverage()
        raw=spool.cursor(value['checkpointName'])
        if raw is not None:
            if not isinstance(raw,str) or len(raw.encode())>MAX_JSON: raise ValueError()
            checkpoint=json.loads(raw,object_pairs_hook=admission._unique_object)
            snapshot.boundary(checkpoint)
        else:
            checkpoint=None
        # Retained uncertain rows are already quarantined: next_pending selects
        # only queued work and admit dedups their source IDs. Keep them intact,
        # including below the protected floor. Raw publishing must first pass
        # through Spool's existing restart quarantine; unknown states fail closed.
        if spool.db.execute("SELECT 1 FROM inbox WHERE typeof(state)!='text' OR "
                            "state NOT IN ('queued','forwarded','expired','uncertain') LIMIT 1").fetchone():
            raise ValueError()
        yield spool,coverage,checkpoint
    finally:
        spool.db.rollback()


def _covered(coverage,manifest):
    floor=manifest['dedup']['floorSeq']
    return (coverage['epoch']==manifest['epoch'] and coverage['floorSeq'] is not None and
            coverage['floorSeq']<=floor and coverage['retainedFloor']<=floor and
            coverage['highSeq']>=manifest['dedup']['highSeq'])


class Store:
    """At most two committed archives and one bounded uncommitted candidate.

    current.json atomically names BOTH manifests by their content digest. Readers
    never discover candidates by scanning. Archive and manifest data are synced
    before pointer replacement; directory fsync is the durable commit boundary.
    current()/previous()/publish() return (verified manifest dict, archive Path).
    All methods fail with a fixed code; contents stay in the calling owner's state.
    """
    def __init__(self,directory):
        self.directory=Path(os.path.abspath(directory))

    @contextmanager
    def _locked(self,write=False):
        try:
            with state._directory(self.directory,create=write,private=True) as directory:
                fcntl.flock(directory,fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                try:
                    names=os.listdir(directory)
                    if len(names)>12: raise ValueError()
                    for name in names:
                        if not (name in {'current.json','.next.zip','.next.manifest','.next.pointer'} or
                                re.fullmatch('[0-9a-f]{64}\\.(zip|json)',name)):
                            raise ValueError()
                        info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                        state._check_object(info,stat.S_ISREG,uid=os.geteuid(),mode=0o600,dev=os.fstat(directory).st_dev)
                        if info.st_size>(state.MAX_ARCHIVE_BYTES if name.endswith('.zip') else MAX_JSON):
                            raise ValueError()
                    yield directory
                finally:
                    fcntl.flock(directory,fcntl.LOCK_UN)
        except Exception:
            raise ValueError('recovery_manifest') from None

    def _read(self,directory,name):
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        with os.fdopen(fd,'rb') as raw:
            state._check_object(os.fstat(raw.fileno()),stat.S_ISREG,uid=os.geteuid(),mode=0o600)
            encoded=raw.read(MAX_JSON+1)
            if len(encoded)>MAX_JSON: raise ValueError()
        value=json.loads(encoded,object_pairs_hook=admission._unique_object,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return value,encoded

    def _pointer(self,directory):
        try: value,_=self._read(directory,'current.json')
        except FileNotFoundError: return {'current':None,'previous':None}
        if (not isinstance(value,dict) or set(value)!={'current','previous'} or
                not admission._sha(value['current']) or
                value['previous'] is not None and not admission._sha(value['previous']) or
                value['current']==value['previous']):
            raise ValueError()
        return value

    def _verify(self,directory,digest):
        if digest is None: return None
        manifest,encoded=self._read(directory,digest+'.json')
        if hashlib.sha256(encoded).hexdigest()!=digest: raise ValueError()
        _validate(manifest)
        if encoded!=admission._encoded(manifest): raise ValueError()
        archive=self.directory/(manifest['snapshotSha256']+'.zip')
        boundaries=[manifest['nativeBoundary']]
        if manifest['bridgeBoundary'] is not None: boundaries.append(manifest['bridgeBoundary'])
        observed=snapshot.inspect(archive,self_proof_candidates=[manifest['selfProof']],boundaries=boundaries)
        if (observed['snapshotSha256']!=manifest['snapshotSha256'] or
                observed['profileSha256']!=manifest['profileSha256'] or
                observed['nativeBoundary']!=manifest['nativeBoundary'] or
                observed['selfProof']!=manifest['selfProof']): raise ValueError()
        return manifest,archive

    def _pairs(self,directory):
        pointer=self._pointer(directory)
        pairs=[self._verify(directory,pointer[key]) for key in ('current','previous')]
        if pairs[1] is not None and (pairs[0][0]['epoch']!=pairs[1][0]['epoch'] or
                pairs[0][0]['profileSha256']!=pairs[1][0]['profileSha256']): raise ValueError()
        return pointer,pairs

    def current(self):
        with self._locked() as directory: return self._pairs(directory)[1][0]

    def previous(self):
        with self._locked() as directory: return self._pairs(directory)[1][1]

    def _sync(self,fd):
        os.fsync(fd)

    def _write(self,directory,name,encoded):
        if len(encoded)>MAX_JSON: raise ValueError()
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb') as raw:
            raw.write(encoded); raw.flush(); self._sync(raw.fileno())

    def _copy_archive(self,directory,source):
        with snapshot.archive_file(source) as raw:
            fd=os.open('.next.zip',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            with os.fdopen(fd,'wb') as target:
                digest=hashlib.sha256(); size=0
                while chunk:=raw.read(state.CHUNK):
                    size+=len(chunk)
                    if size>state.MAX_ARCHIVE_BYTES: raise ValueError()
                    target.write(chunk); digest.update(chunk)
                target.flush(); self._sync(target.fileno())
        return digest.hexdigest()

    def _write_manifest(self,directory,encoded):
        self._write(directory,'.next.manifest',encoded)

    def _commit_pointer(self,directory,encoded):
        self._write(directory,'.next.pointer',encoded)
        os.replace('.next.pointer','current.json',src_dir_fd=directory,dst_dir_fd=directory)
        self._sync(directory)

    def _clean(self,directory,pairs):
        keep={'current.json'}
        for pair in pairs:
            if pair is not None:
                keep.add(pair[0]['snapshotSha256']+'.zip')
                keep.add(hashlib.sha256(admission._encoded(pair[0])).hexdigest()+'.json')
        for name in os.listdir(directory):
            if name not in keep: os.unlink(name,dir_fd=directory)
        self._sync(directory)

    def publish(self,archive,metadata):
        with self._locked(write=True) as directory:
            if not isinstance(metadata,dict) or set(metadata)!=(FIELDS-{'schemaVersion','snapshotSha256','nativeBoundary'})|{'capture'}:
                raise ValueError()
            pointer,pairs=self._pairs(directory)
            self._clean(directory,pairs)
            try:
                with _bridge(metadata['capture']) as (spool,coverage,checkpoint):
                    if checkpoint!=metadata['bridgeBoundary']: raise ValueError()
                    observed=snapshot.inspect(archive,self_proof_candidates=[metadata['selfProof']],
                                              boundaries=[] if checkpoint is None else [checkpoint])
                    value={k:v for k,v in metadata.items() if k!='capture'}
                    value.update(schemaVersion=1,snapshotSha256=observed['snapshotSha256'],nativeBoundary=observed['nativeBoundary'])
                    _validate(value)
                    if (observed['profileSha256']!=value['profileSha256'] or observed['selfProof']!=value['selfProof'] or
                            not _covered(coverage,value) or coverage['highSeq']!=value['dedup']['highSeq']):
                        raise ValueError()
                    if pairs[0] is not None and (value['epoch']!=pairs[0][0]['epoch'] or
                            value['profileSha256']!=pairs[0][0]['profileSha256']): raise ValueError()
                    encoded=admission._encoded(value); digest=hashlib.sha256(encoded).hexdigest()
                    if digest==pointer['current']: return pairs[0]
                    if self._copy_archive(directory,archive)!=value['snapshotSha256']: raise ValueError()
                    # Verify the immutable COPY, not just the mutable source.
                    copied=snapshot.inspect(self.directory/'.next.zip',self_proof_candidates=[value['selfProof']],
                                            boundaries=[value['nativeBoundary']]+([] if checkpoint is None else [checkpoint]))
                    if copied!=observed: raise ValueError()
                    name=value['snapshotSha256']+'.zip'
                    if name in os.listdir(directory): os.unlink('.next.zip',dir_fd=directory)
                    else: os.rename('.next.zip',name,src_dir_fd=directory,dst_dir_fd=directory)
                    self._write_manifest(directory,encoded)
                    os.replace('.next.manifest',digest+'.json',src_dir_fd=directory,dst_dir_fd=directory)
                    self._sync(directory)
                    self._commit_pointer(directory,admission._encoded({'current':digest,'previous':pointer['current']}))
                result=self._pairs(directory)[1]
                self._clean(directory,result)
                return result[0]
            except Exception:
                # May have committed at a failed final fsync. Never roll back a
                # pointer or assume failure permits protection advancement.
                raise ValueError('recovery_manifest') from None

    def recovery_plan(self,current_bridge,manifest):
        """Read-only plan from a verified committed pair; never restore bridge DB.

        Receive from bridgeBoundary, possibly earlier than the Native snapshot's
        maximum event. Null bootstrap has no proven receive start and is rejected.
        The live durable checkpoint can be later; protected source IDs dedup that
        interval, including retained uncertain inputs that must never be retried.
        Caller must freeze before applying and re-verify Native GET anchor.
        """
        with self._locked() as directory:
            if (not isinstance(current_bridge,dict) or
                    set(current_bridge)!={'spool','profileSha256','epoch','checkpointName'}): raise ValueError()
            _,pairs=self._pairs(directory)
            if not isinstance(manifest,(tuple,list)) or len(manifest)!=2 or not any(manifest==pair for pair in pairs if pair is not None):
                raise ValueError()
            value=manifest[0]
            if (value['bridgeBoundary'] is None or current_bridge['profileSha256']!=value['profileSha256'] or
                    current_bridge['epoch']!=value['epoch']): raise ValueError()
            with _bridge({key:current_bridge[key] for key in ('spool','checkpointName')}) as (spool,coverage,checkpoint):
                if (not _covered(coverage,value) or checkpoint is None or
                        int(checkpoint['cursor'])<int(value['bridgeBoundary']['cursor']) or
                        checkpoint['cursor']==value['bridgeBoundary']['cursor'] and checkpoint!=value['bridgeBoundary']):
                    raise ValueError()
                return {'boundary':dict(value['bridgeBoundary']),'epoch':value['epoch']}
