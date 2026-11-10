"""Root-only disposition integration. Never mutates Native SQL or retries sends."""
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

# Controllers use explicit file loading under -I. Give root helpers a private
# package for their relative imports without adding writable directories to sys.path.
if __package__:
    from . import restore_admission as admission, send_dispositions as dispositions, recovery_snapshot as snapshots
else:
    name = '_wechat_recovery_private'
    if name not in sys.modules:
        spec = importlib.util.spec_from_loader(name, loader=None, is_package=True)
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(Path(__file__).resolve().parent)]
        sys.modules[name] = package
    admission = importlib.import_module(name+'.restore_admission')
    dispositions = importlib.import_module(name+'.send_dispositions')
    snapshots = importlib.import_module(name+'.recovery_snapshot')

LEDGER = 'send-dispositions.json'
AUDITS = (('forward-send.json','text'), ('private-forward-send.json','text'),
          ('forward-image-send.json','media'), ('forward-image-live-send.json','media'),
          ('forward-voice-send.json','media'))


def ledger_for(core, profile):
    return dispositions.Ledger(core.owner.STATE / LEDGER, profile)


def output_available(core):
    """Reserve room for a serialized send's possible exception before submission."""
    path = core.owner.STATE / LEDGER
    try:
        if not path.exists():
            return not path.is_symlink()
        raw = core.owner.read_private(path, admission.MAX_BYTES)
        value = json.loads(raw, object_pairs_hook=admission._unique_object)
        ledger = ledger_for(core, value['profileSha256']).read()
        return len(ledger['entries']) < admission.MAX_ENTRIES
    except Exception:
        return False


def _evidence(audit):
    value = {k:v for k,v in audit.items() if k != 'recoveryDisposition'}
    raw = admission._encoded(value)
    if len(raw) > 4096:
        raise ValueError('recovery_disposition')
    return hashlib.sha256(raw).hexdigest()


def _matching(db, audit, kind):
    if (kind not in {'text','media'} or not isinstance(audit,dict)
            or not isinstance(audit.get('key'),str) or re.fullmatch('[0-9a-f]{32}',audit['key']) is None
            or not admission._sha(audit.get('destinationSha256'))):
        raise ValueError('recovery_disposition')
    # Also validates exact schema and the bounded pending inventory.
    admission.pending_records(db)
    columns = admission.TEXT_COLUMNS if kind=='text' else admission.MEDIA_COLUMNS
    table = 'outbox' if kind=='text' else 'media_jobs'
    identity = audit.get('sendId' if kind=='text' else 'jobId')
    key = audit['key'] if kind=='text' else 'send:'+audit['key']
    where, args = 'idempotency_key=?', (key,)
    if identity is not None:
        if not isinstance(identity,str) or not 0 < len(identity) <= 100:
            raise ValueError('recovery_disposition')
        where, args = '(id=? OR idempotency_key=?)', (identity,key)
    sizes = '+'.join('coalesce(length(CAST('+c+' AS BLOB)),0)' for c in columns.split(','))
    bounds = db.execute('SELECT '+sizes+' FROM '+table+' WHERE '+where+' LIMIT 2',args).fetchall()
    if len(bounds)>1 or bounds and bounds[0][0]>admission.MAX_ROW_BYTES-1024:
        raise ValueError('recovery_disposition')
    if not bounds:
        return None
    row = db.execute('SELECT '+columns+' FROM '+table+' WHERE '+where,args).fetchone()
    if row[1]!=key or identity is not None and row[0]!=identity or not admission._row_shape(kind,row):
        raise ValueError('recovery_disposition')
    if kind=='text':
        destination=row[3]
    else:
        data=json.loads(row[10],object_pairs_hook=admission._unique_object)
        destination=data.get('threadId')
        if row[3]!='send' or not isinstance(data.get('result',{}),dict):
            raise ValueError('recovery_disposition')
        prepared=data.get('result',{}).get('imagePreparation')
        if prepared is not None and (not isinstance(prepared,dict)
                or prepared.get('sourceSha256')!=audit.get('sourceSha256')):
            raise ValueError('recovery_disposition')
    if not isinstance(destination,str) or hashlib.sha256(destination.encode()).hexdigest()!=audit['destinationSha256']:
        raise ValueError('recovery_disposition')
    return row


def _pre_submit(row):
    """Positive SDK failure, full row and every voice chunk must prove no submit."""
    data=json.loads(row[10],object_pairs_hook=admission._unique_object)
    result=data.get('result',{})
    chunks=result.get('chunks',[])
    return (row[3]=='send' and row[4] in {'failed','needs_review'} and row[5] in {'preparing','uploading'}
        and type(row[6]) is int and row[6] in (0,1) and type(row[7]) is int and row[7]==0
        and type(row[8]) is int and row[8]==0 and row[9]=='media_processing_failed'
        and result.get('profileProjection')=='not_applicable' and result.get('uploadReadbackVerified') is not True
        and not admission._has_receipt(data)
        and isinstance(chunks,list) and all(isinstance(c,dict) and c.get('state')=='prepared' for c in chunks))


def _record(core, db, metadata, audit, kind, classification, evidence_kind=None, evidence_sha=None):
    if classification not in {'unconfirmed_quarantined','rejected_before_submit','reviewed_not_delivered'}:
        raise ValueError('recovery_disposition')
    row=_matching(db,audit,kind)
    evidence_kind=evidence_kind or {'rejected_before_submit':'native_pre_submit',
                                     'reviewed_not_delivered':'operator_review'}.get(classification,'unconfirmed_result')
    evidence_sha=evidence_sha or _evidence(audit)
    result=dict(classification=classification,profileSha256=metadata['profileSha256'],
                evidenceSha256=evidence_sha,matched=row is not None,recordSha256=None,
                admissionRequired=False,snapshotSha256=metadata['snapshotSha256'])
    if row is None:
        # An absent job is uncertainty evidence, never a rejection proof.
        result['classification']='unconfirmed_quarantined'
        return result
    if classification=='rejected_before_submit' and (kind!='media' or not _pre_submit(row)):
        raise ValueError('recovery_disposition')
    entry=dispositions.entry(metadata['account'],kind,row,classification,evidence_kind,evidence_sha)
    # Queued/running/retryable rows cannot become an admission exception.
    pending={(k,admission.fingerprint(metadata['account'],k,r)) for k,r in admission.pending_records(db)}
    result['admissionRequired']=(kind,entry['recordSha256']) in pending
    if result['admissionRequired']:
        if not admission._safe_pending(kind,row,entry):raise ValueError('recovery_disposition')
        ledger_for(core,metadata['profileSha256']).record(entry)
    result['recordSha256']=entry['recordSha256']
    return result


def record_native_outcome(core, pinned, audit, kind, classification):
    if classification=='accepted_verified':
        return None  # Ordinary accepted history must not fill exception capacity.
    try:
        if not pinned.unchanged():
            raise ValueError()
        with core.owner.checkpoint_copy(pinned) as snapshot:
            with snapshots.copied_database(snapshot) as (db,metadata):
                if not pinned.unchanged():
                    raise ValueError()
                return _record(core,db,metadata,audit,kind,classification)
    except Exception:
        raise ValueError('recovery_disposition') from None


def write_evidence(core, path, value):
    """Publish recovery evidence durably; ordinary owner status writes are unchanged."""
    try:
        with snapshots.state._directory(Path(path).parent,private=True) as directory:
            core.owner.write_json(path,value)
            os.fsync(directory)
    except Exception:
        raise ValueError('recovery_disposition') from None


def audit_cleared(core, audit):
    """Gate recognizes only this exact durable audit, never a bare rejected label."""
    try:
        if 'recoveryDisposition' not in audit:
            return _legacy_cleared(core,audit)
        marker=audit['recoveryDisposition']
        if not _valid_marker(marker,_evidence(audit)):return False
        if not marker['admissionRequired']:return True
        ledger=ledger_for(core,marker['profileSha256']).read()
        return any(v['recordSha256']==marker['recordSha256'] and v['evidenceSha256']==marker['evidenceSha256']
                   and v['disposition']==marker['classification'] for v in ledger['entries'])
    except Exception:
        return False


def _valid_marker(marker, evidence_sha):
    if (not isinstance(marker,dict) or set(marker)!={'classification','profileSha256','evidenceSha256',
            'matched','recordSha256','admissionRequired','snapshotSha256'}
            or marker['classification'] not in {'unconfirmed_quarantined','rejected_before_submit','reviewed_not_delivered'}
            or marker['evidenceSha256']!=evidence_sha or type(marker['matched']) is not bool
            or type(marker['admissionRequired']) is not bool
            or not admission._sha(marker['profileSha256']) or not admission._sha(marker['snapshotSha256'])):
        return False
    if not marker['matched']:
        return (not marker['admissionRequired'] and marker['classification']=='unconfirmed_quarantined'
                and marker['recordSha256'] is None)
    return admission._sha(marker['recordSha256'])


def _legacy_cleared(core, audit):
    try:
        raw=core.owner.read_private(core.owner.STATE/LEDGER,admission.MAX_BYTES)
    except FileNotFoundError:ledger={'entries':[]}
    else:
        header=json.loads(raw,object_pairs_hook=admission._unique_object)
        ledger=ledger_for(core,header['profileSha256']).read()
    sources=_legacy_sources(core)
    hold=json.loads(sources['forward-image-hold.json'],object_pairs_hook=admission._unique_object) if 'forward-image-hold.json' in sources else None
    for name,kind in AUDITS:
        if name not in sources or json.loads(sources[name],object_pairs_hook=admission._unique_object)!=audit:continue
        classification,evidence=_legacy_classification(name,kind,audit,hold)
        if classification is None:continue
        sha=hashlib.sha256(sources[name]+(sources['forward-image-hold.json'] if evidence=='legacy_hold' else b'')).hexdigest()
        if any(v['kind']==kind and v['evidenceSha256']==sha and v['disposition']==classification
               and v['evidenceKind']==evidence for v in ledger['entries']):return True
        try:raw=core.owner.read_private(core.owner.STATE/(name+'.recovery.json'),4096)
        except FileNotFoundError:continue
        saved=json.loads(raw,object_pairs_hook=admission._unique_object)
        if (set(saved)=={'kind','marker'} and saved['kind']==kind and _valid_marker(saved['marker'],sha)
                and saved['marker']['matched'] and not saved['marker']['admissionRequired']
                and saved['marker']['classification']==classification):return True
    return False


def _legacy_sources(core):
    sources={}
    for name in [n for n,_ in AUDITS]+['forward-image-hold.json']:
        path=core.owner.STATE/name
        if path.exists() or path.is_symlink():
            sources[name]=core.owner.read_private(path,4096)
    return sources


def _legacy_markers(core):
    saved={}
    for name,_ in AUDITS:
        path=core.owner.STATE/(name+'.recovery.json')
        if path.exists() or path.is_symlink():saved[path.name]=core.owner.read_private(path,4096)
    return saved


def _legacy_classification(name, kind, audit, hold):
    classification=evidence=None
    if name=='forward-image-send.json' and hold is not None:
        if (set(hold)!={'schemaVersion','mode','auditSha256','jobId','nativeClientPid','nativeClientTicks','approvedAt'}
                or type(hold['schemaVersion']) is not int or hold['schemaVersion']!=1
                or hold['mode'] not in {'text_only','new_images'} or hold['jobId']!=audit.get('jobId')
                or re.fullmatch(r'job_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',hold['jobId']) is None
                or audit.get('state')!='unknown' or audit.get('code')!='image_needs_review'
                or hold['auditSha256']!=_evidence(audit)
                or any(type(hold[k]) is not int or hold[k]<=n for k,n in [('nativeClientPid',1),('nativeClientTicks',0),('approvedAt',0)])):
            raise ValueError()
        classification,evidence='unconfirmed_quarantined','legacy_hold'
    elif audit.get('state')=='rejected' and audit.get('review')=='reviewed_send_not_delivered':
        classification,evidence='reviewed_not_delivered','operator_review'
    elif kind=='media' and audit.get('state')=='rejected' and audit.get('verification')=='sdk_failed_preparing_before_wechat_submission':
        classification,evidence='rejected_before_submit','native_pre_submit'
    return classification,evidence


def import_legacy(core, snapshot, backup_dir):
    """Caller holds bridge/migration exclusion; CAS raw audits and archive before writes.

    Preserve raw original audits. Backups are bounded by fixed input names and
    one immutable fingerprint per import set; repeated identical imports reuse it.
    No bridge or Native SQL mutation is performed.
    """
    try:
        sources=_legacy_sources(core)
        previous_markers=_legacy_markers(core)
        with snapshots.copied_database(snapshot) as (db,metadata):
            candidates=[];terminal_markers={};matched=0
            hold=json.loads(sources['forward-image-hold.json'],object_pairs_hook=admission._unique_object) if 'forward-image-hold.json' in sources else None
            if hold is not None and 'forward-image-send.json' not in sources:raise ValueError()
            for name,kind in AUDITS:
                if name not in sources: continue
                audit=json.loads(sources[name],object_pairs_hook=admission._unique_object)
                if audit.get('state')=='accepted' and not (name=='forward-image-send.json' and hold is not None): continue
                classification,evidence=_legacy_classification(name,kind,audit,hold)
                if classification:
                    if not isinstance(audit.get('sendId' if kind=='text' else 'jobId'),str):raise ValueError()
                    row=_matching(db,audit,kind)
                    if row is None or classification=='rejected_before_submit' and not _pre_submit(row):raise ValueError()
                    sha=hashlib.sha256(sources[name]+(sources['forward-image-hold.json'] if evidence=='legacy_hold' else b'')).hexdigest()
                    entry=dispositions.entry(metadata['account'],kind,row,classification,evidence,sha)
                    required=(kind,entry['recordSha256']) in {(k,admission.fingerprint(metadata['account'],k,r)) for k,r in admission.pending_records(db)}
                    if required:
                        if not admission._safe_pending(kind,row,entry):raise ValueError()
                        candidates.append(entry)
                    else:
                        terminal_markers[name+'.recovery.json']=dict(kind=kind,marker=dict(
                            classification=classification,profileSha256=metadata['profileSha256'],evidenceSha256=sha,
                            matched=True,recordSha256=entry['recordSha256'],admissionRequired=False,
                            snapshotSha256=metadata['snapshotSha256']))
                    matched+=1
            ledger=ledger_for(core,metadata['profileSha256'])
            previous=ledger.read()
            # Prevalidate all conflicts/capacity, avoiding partial imports for bad sets.
            combined={e['recordSha256']:e for e in previous['entries']}
            for entry in candidates:
                if entry['recordSha256'] in combined and combined[entry['recordSha256']]!=entry:raise ValueError()
                combined[entry['recordSha256']]=entry
            updated={**previous,'revision':previous['revision']+len(combined)-len(previous['entries']),
                     'entries':list(combined.values())}
            ledger._validate(updated)
            binding=dict(snapshotSha256=metadata['snapshotSha256'],audits={n:hashlib.sha256(v).hexdigest() for n,v in sources.items()},
                         markers={n:hashlib.sha256(v).hexdigest() for n,v in previous_markers.items()})
            digest=hashlib.sha256(admission._encoded(binding)).hexdigest()
            backup_dir=Path(backup_dir);core.owner.private_directory(backup_dir)
            target=backup_dir/digest;core.owner.private_directory(target)
            with snapshots.state._directory(target,private=True) as directory:
                if not set(os.listdir(directory)) <= set(sources)|set(previous_markers)|{'binding.json','ledger-before.json'}:raise ValueError()
                for name,raw in {**sources,**previous_markers,'binding.json':admission._encoded(binding), 'ledger-before.json':admission._encoded(previous)}.items():
                    try:
                        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                    except FileExistsError:
                        saved=core.owner.read_private(target/name,admission.MAX_BYTES)
                        if name!='ledger-before.json' and saved!=raw:raise ValueError()
                    else:
                        with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
                os.fsync(directory)
            for parent in (backup_dir,backup_dir.parent):
                with snapshots.state._directory(parent,private=True) as directory:os.fsync(directory)
            # One ledger transaction: compare the baseline and all source bytes
            # under the same writer lock used by ordinary outcome recording.
            with ledger._directory() as directory:
                lock=os.open(ledger.path.name+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,0o600,dir_fd=directory)
                try:
                    dispositions._check_file(lock)
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    if (ledger._read(directory)!=previous or _legacy_sources(core)!=sources
                            or _legacy_markers(core)!=previous_markers):raise ValueError()
                    with snapshots.archive_file(snapshot) as raw:
                        if snapshots._digest(raw)!=metadata['snapshotSha256']:raise ValueError()
                    if updated!=previous:ledger._write(directory,admission._encoded(updated))
                    for name,value in terminal_markers.items():
                        old=previous_markers.get(name)
                        if old is None or json.loads(old,object_pairs_hook=admission._unique_object)!=value:
                            write_evidence(core,core.owner.STATE/name,value)
                finally:os.close(lock)
            return dict(imported=len(combined)-len(previous['entries']),matched=matched)
    except Exception:
        raise ValueError('recovery_disposition') from None
