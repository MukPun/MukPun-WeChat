#!/usr/bin/env python3
"""Resolve one proven pre-submission SDK failure; never sends or replays it."""
import ast
import hashlib
import importlib.util
import json
import os
import resource
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile

HERE = Path(__file__).resolve().parent


def load(name):
    spec=importlib.util.spec_from_file_location('_image_review_'+name,HERE/(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module


def main():
    assert os.geteuid()==0 and not sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_AS,(134217728,134217728))
    resource.setrlimit(resource.RLIMIT_CPU,(10,10))
    formal=load('run_forwarder')
    core,owner=formal.core,formal.core.owner
    require=core.require
    info=core.tap.details(core.UNIT)
    require(info.get('MainPID')=='0' and info.get('ActiveState')=='failed','bridge_must_be_stopped')
    audit_path=owner.STATE/'forward-image-send.json'
    audit=owner.read_json(audit_path)
    require(audit.get('state')=='unknown' and audit.get('code')=='image_failed'
            and core.output_images.job_id(audit.get('jobId')),'audit_binding')
    pinned=core.groups.PinnedNative()
    pinned.open()
    host={k:core.owned.identity('/proc/self/ns/'+k) for k in ('net','user')}
    network,business=core.tap.read_network(),core.tap.business_snapshot()
    work=Path(tempfile.mkdtemp(prefix='astrbot-wechat-preparation-review.',dir='/var/tmp'))
    try:
        core.trial.fresh_status()
        store=owner.companion('session_state_store')
        archive_path=store.SnapshotStore(owner.STATE/'checkpoints').latest()
        with zipfile.ZipFile(archive_path) as archive:
            store._archive_manifest(archive)
            names=[n for n in archive.namelist() if n.endswith('/agent/control.sqlite')]
            require(len(names)==1,'database_binding')
            for suffix in ('','-wal','-shm','-journal'):
                name=names[0]+suffix
                if name in archive.namelist():
                    with archive.open(name) as source,(work/('control.sqlite'+suffix)).open('wb') as out:
                        shutil.copyfileobj(source,out,65536)
        db=sqlite3.connect((work/'control.sqlite').as_uri()+'?mode=ro',uri=True,timeout=.2)
        try:
            row=db.execute('SELECT state,phase,attempts,error_code,data_json FROM media_jobs WHERE id=?',(audit['jobId'],)).fetchone()
        finally:
            db.close()
        require(row is not None and row[0]=='failed' and row[1]=='preparing' and 0<=row[2]<=1
                and row[3]=='media_processing_failed','not_a_known_preparation_failure')
        job=json.loads(row[4]); result=job.get('result',{})
        require(result.get('nativeReceipt') is None and result.get('uploadReadbackVerified') is not True
            and result.get('profileProjection')=='not_applicable'
            and result.get('imagePreparation',{}).get('sourceSha256')==audit['sourceSha256']
            and hashlib.sha256(job['threadId'].encode()).hexdigest()==audit['destinationSha256'],'job_binding')
        spool=Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve(strict=True)
        journal=sqlite3.connect(spool,timeout=.2)
        try:
            counts=dict(journal.execute('SELECT state,count(*) FROM inbox GROUP BY state'))
            require(not any(counts.get(k) for k in ('queued','publishing','uncertain')),'pending_input_requires_review')
            submissions=journal.execute('SELECT key,result FROM submissions WHERE native_key=?',(audit['key'],)).fetchall()
            require(len(submissions)==1 and (submissions[0][1] is None or json.loads(submissions[0][1]).get('retcode')==1408),'submission_binding')
            backup=owner.STATE/('review-preparing-'+audit['jobId'])
            backup.mkdir(mode=0o700,exist_ok=False)
            owner.write_json(backup/'audit-before.json',audit)
            owner.write_json(backup/'status-before.json',owner.read_json(core.STATUS))
            copy=sqlite3.connect(backup/'bridge-before.sqlite')
            try: journal.backup(copy)
            finally: copy.close()
            (backup/'bridge-before.sqlite').chmod(0o600)
            old=json.loads((HERE/'context-staging/previous-manifest.json').read_text())
            new=json.loads((HERE/'context-staging/manifest.json').read_text())
            require(set(old)==set(new),'manifest_binding')
            changed={n for n in new if old[n]!=new[n]}
            require(changed=={'image_send_native.py','verification/scripts/image_send_native.py',
                              'verification/tests/test_onebot_image_output_native.py'},'unexpected_code_changes')
            deploy=load('deploy_group_context')
            for name in sorted(new):
                prior,metadata=deploy.read(HERE/name)
                require(hashlib.sha256(prior).hexdigest()==old[name],'live_code_changed')
                if name in changed:
                    raw,_=deploy.read(HERE/'context-staging'/name)
                    require(hashlib.sha256(raw).hexdigest()==new[name],'stage_code_changed')
                    ast.parse(raw)
                    save=backup/name
                    save.parent.mkdir(parents=True,exist_ok=True)
                    save.write_bytes(prior);save.chmod(0o600)
                    deploy.replace(HERE/name,raw,metadata)
            answer={'status':'failed','retcode':1400,'data':None,'message':'unsupported_message_segment',
                    'wording':'image_preparation_failed'}
            with journal:
                update=journal.execute('UPDATE submissions SET result=? WHERE key=? AND native_key=?',
                    (json.dumps(answer,separators=(',',':')),submissions[0][0],audit['key']))
                require(update.rowcount==1,'submission_update_failed')
        finally:
            journal.close()
        owner.write_json(audit_path,{**audit,'state':'rejected','code':'image_preparation_failed',
            'verification':'sdk_failed_preparing_before_wechat_submission'})
        code,_=core.tap.command_result(['/usr/bin/systemctl','reset-failed',core.UNIT],5)
        require(code==0,'owned_unit_reset_failed')
        formal.start()
        until=time.monotonic()+35
        while time.monotonic()<until:
            status=owner.read_json(core.STATUS)
            if status.get('ready') is True:
                flags,failures=core.tap.compare_baselines(host,network,business)
                require(not failures and pinned.unchanged() and status.get('uncertain')==0,'restored_bridge_requires_review')
                print(json.dumps({'stage':'preparing_failure_reconciled','ok':True,'bridge_ready':True,
                    'known_not_sent':True,'resubmitted':False,'native_session_unchanged':True,'new_qr':False,**flags}))
                return
            time.sleep(.5)
        raise ValueError('bridge_not_ready')
    finally:
        pinned.close()
        for suffix in ('','-wal','-shm','-journal'):
            (work/('control.sqlite'+suffix)).unlink(missing_ok=True)
        work.rmdir()


if __name__=='__main__':
    main()
