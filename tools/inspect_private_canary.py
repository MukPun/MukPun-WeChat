#!/usr/bin/env python3
"""Read-only, redacted rollout inspection. No service/config/session mutations."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


STATES = frozenset({'accepted', 'unknown', 'submitted', 'preparing', 'uploading', 'readback',
                    'rejected', 'not_submitted', 'cleared', 'cancelled', 'failed'})
QUEUES = frozenset({'queued', 'publishing', 'forwarded', 'uncertain', 'expired'})


def inspect_audits(core):
    result = {}
    for name, kind in core.recovery_dispositions.AUDITS:
        path = core.owner.STATE/name
        if not path.exists():
            result[name] = {'present': False}
            continue
        audit = core.owner.read_json(path)
        result[name] = {'present': True,
            'state': audit.get('state') if audit.get('state') in STATES else 'unclassified',
            'cleared': core.audit_cleared(audit)}
    return result


def inspect(project, *, recovery_proof=False, image_jobs=False, existing_image_snapshot=False, receive_backlog=False):
    spec = importlib.util.spec_from_file_location('_private_canary_formal', project/'scripts/run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = formal
    spec.loader.exec_module(formal)
    core = formal.core
    result = {'stage': 'private_canary_inspection', 'read_only': True}
    units = {}
    for unit in (core.UNIT, core.owner.OWNER):
        command = subprocess.run(['/usr/bin/systemctl', 'show', '--no-pager',
            '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result', '-p', 'ExecMainStatus', unit],
            capture_output=True, timeout=8, check=True)
        units[unit] = dict(line.split('=',1) for line in command.stdout.decode('ascii').splitlines() if '=' in line)
    result['units'] = units
    formal.load_config()
    result['configuration'] = {key: core.SETTINGS.get(key, False) for key in (
        'privateOnly', 'allGroupsEnabled', 'imageInputEnabled', 'imageOutputEnabled',
        'voiceOutputEnabled', 'stickerInputEnabled', 'linkInputEnabled', 'groupContextEnabled')}
    status = core.owner.read_json(core.STATUS)
    # Only fixed enums, booleans and integer counts leave this process.
    result['bridge'] = {key: status.get(key) is True for key in (
        'active', 'ready', 'sse', 'astrbot_connected', 'native_connected', 'self_identity_verified',
        'private_only', 'all_groups_enabled', 'cleanup_ok', 'native_session_unchanged', 'requires_review')}
    status_codes = core.recovery_diagnostics.CODES | {'preflight_failed','forwarder_failed',
        'forwarder_preflight_failed','previous_send_requires_review','previous_image_send_requires_review',
        'previous_voice_send_requires_review','native_changed','held_connection_unavailable',
        'held_native_instance_unavailable','onebot_token_missing'}
    result['bridge']['code'] = status.get('code') if status.get('code') in status_codes else 'unclassified'
    result['bridge']['status_age_seconds'] = max(0, int(time.time())-status['updated']) if type(status.get('updated')) is int else None
    result['routes'] = {route:{key:count for key,count in status.get('routes',{}).get(route,{}).items()
        if key in {'received','forwarded','replies'} and type(count) is int and 0<=count<=1000000}
        for route in ('private','group')}
    result['audits'] = inspect_audits(core)
    result['owner_status'] = {key: core.owner.read_json(core.owner.CONTROL/'status.json').get(key) is True
                              for key in ('ready', 'connected', 'ok')}
    result['image_hold_present'] = (core.owner.STATE/'forward-image-hold.json').exists()
    # Never instantiate Spool: its constructor can upgrade tables/retire rows.
    path = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve(strict=True)
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1) as connection:
        connection.execute('PRAGMA query_only=ON')
        rows = connection.execute('SELECT state,count(*) FROM inbox GROUP BY state').fetchall()
        result['queue'] = {state: count for state,count in rows if state in QUEUES}
        result['queue_has_unclassified_state'] = any(state not in QUEUES for state,count in rows)
        result['action_results'] = {str(code) if code in (-1,0,1400,1403,1408,1500,1501) else 'other':count
            for code,count in connection.execute(
                "SELECT coalesce(json_extract(result,'$.retcode'),-1),count(*) FROM submissions GROUP BY 1")}
        result['unresolved_actions'] = connection.execute(
            "SELECT count(*) FROM submissions WHERE result IS NULL OR json_extract(result,'$.retcode')=1408").fetchone()[0]
    pinned = core.groups.PinnedNative()
    try:
        pinned.open()
        result['native'] = {'remaining_seconds': pinned.remaining_seconds(), 'unchanged': pinned.unchanged()}
        health = core.health.probe_once(pinned)
        allowed_codes = core.health.contact.RESULT_CODES | {'probe_failed','probe_timeout',
            'probe_namespace_failed','probe_token_unavailable'}
        result['native'].update(health_ok=health.get('ok') is True,
            connection=health.get('connection') if health.get('connection') in {'online','reconnecting','disconnected'} else 'unchecked',
            authenticated=health.get('authenticated') if type(health.get('authenticated')) is bool else None,
            probe_code=health.get('code') if health.get('code') in allowed_codes else 'ok' if health.get('ok') is True else 'unclassified',
            account_online=health.get('ok') is True and health.get('connected') is True)
        if receive_backlog:
            result['receive_backlog'] = inspect_receive_backlog(core, pinned)
        if image_jobs:
            path = core.owner.STATE/'forward-image-live-send.json'
            audit = core.owner.read_json(path)
            job = core.image_recovery.read_job(pinned,audit)
            detail = {'read_ok':isinstance(job,dict),'audit_has_job_id': bool(audit.get('jobId')),
                'audit_job_id_valid':core.output_images.job_id(audit.get('jobId'))}
            if isinstance(job,dict):
                detail.update(state=job.get('state') if job.get('state') in
                    {'queued','running','completed','succeeded','failed','needs_review','cancelled'} else 'unclassified',
                    phase=job.get('phase') if job.get('phase') in
                    {'queued','preparing','uploading','submitting','readback','completed','done'} else 'unclassified',
                    completed_bytes=job.get('completedBytes') if type(job.get('completedBytes')) is int else None,
                    job_bound=job.get('id')==audit.get('jobId'),
                    destination_bound=core.image_recovery.destination_matches(audit,job),
                    has_server_id=bool(job.get('serverMessageId')),
                    has_native_receipt=bool((job.get('result') or {}).get('nativeReceipt')),
                    has_message_ids=bool((job.get('result') or {}).get('messageIds')),
                    proved_not_submitted=core.image_recovery.known_failure(audit,job))
            result['live_image_job']=detail
        if existing_image_snapshot:
            detail={'verified':False,'phase':'existing_archive'}
            result['live_image_snapshot']=detail
            try:
                if units[core.UNIT]['ActiveState'] not in {'inactive','failed'}:
                    raise ValueError('inspection_requires_review')
                audit_path = core.owner.STATE/'forward-image-live-send.json'
                audit = core.owner.read_json(audit_path)
                archive = core.owner.STATE/'checkpoints/private/current.zip'
                archive_age = max(0,int(time.time()-archive.stat().st_mtime))
                detail['archive_age_seconds']=archive_age
                detail['phase']='archive_freshness'
                if archive_age>900 or archive.stat().st_mtime<audit_path.stat().st_mtime:
                    raise ValueError('inspection_requires_review')
                dispositions = core.recovery_dispositions
                detail['phase']='database_copy'
                with dispositions.snapshots.copied_database(archive) as (db,metadata):
                    detail['phase']='audit_matching'
                    row = dispositions._matching(db,audit,'media')
                    detail['matched']=row is not None
                    if row is not None:
                        detail['job_state']=row[4] if row[4] in STATES|{'queued','running','completed'} else 'unclassified'
                        detail['job_phase']=row[5] if row[5] in {'queued','preparing','uploading','submitting','readback','completed'} else 'unclassified'
                        detail['snapshot_proves_pre_submit']=dispositions._pre_submit(row)
                        candidate={**audit,'jobId':row[0]}
                        live_job=core.image_recovery.read_job(pinned,candidate)
                        detail['bound_job_read_ok']=isinstance(live_job,dict)
                        detail['live_proves_pre_submit']=core.image_recovery.known_failure(candidate,live_job)
                    detail['phase']='current_admission'
                    admission = dispositions.dispositions.admission_for(db,metadata['account'],
                        dispositions.ledger_for(core,metadata['profileSha256']))
                    dispositions.admission.validate_database(db,metadata['account'],admission)
                    result['live_image_snapshot']={'verified':True,'matched':row is not None,
                        'current_admission_verified':True,'quarantined':len(admission['entries']),
                        'state':row[4] if row is not None and row[4] in STATES else None,
                        'phase':row[5] if row is not None and row[5] in {'preparing','uploading','submitting','readback'} else None,
                        'native_unchanged':pinned.unchanged(),'archive_age_seconds':archive_age}
            except Exception as error:
                allowed = {'restore_database','restore_admission','send_disposition','send_ledger','recovery_admission'}
                detail['code']=str(error) if str(error) in allowed else 'inspection_requires_review'
                # Never expose an unrecognized exception string.
    finally:
        # close() only releases our FDs; never call SessionHost.close().
        pinned.close()
    if recovery_proof:
        pair = core.recovery_coordinator.selected_pair(core.owner.STATE)
        admitted = core.recovery_coordinator._admit(core, pair)
        result['recovery'] = {'manifest_verified': True, 'admission_verified': True,
            'quarantined':len(admitted['entries']), 'ledger_revision': admitted['ledgerRevision']}
    result['inspection_complete'] = True
    result['deployment_performed'] = False
    return result



def inspect_receive_backlog(core, pinned):
    """GET-only bounded scan from the durable anchor; never ACK or move it."""
    coordinator = core.recovery_coordinator
    # Match formal Native: the client uses this name both for GET and exact filtering.
    core.health.contact.TARGET_NAME = core.SETTINGS['privateContact']
    cp = core.recovery_checkpoints
    generation = 'profile:'+hashlib.sha256(pinned.token().encode()).hexdigest()
    path = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve(strict=True)
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT value FROM checkpoints WHERE name=?',(generation,)).fetchone()
        if row is None: raise ValueError('inspection_requires_review')
        boundary = json.loads(row[0]); cp.snapshot.boundary(boundary)
        private = core.owner.read_json(core.owner.STATE/'private-forward-send.json')
        peers = [raw for raw, in db.execute("SELECT raw FROM identities WHERE kind='user'")
                 if hashlib.sha256(raw.encode()).hexdigest()==private.get('destinationSha256')]
        if len(peers)!=1: raise ValueError('inspection_requires_review')
    ops = coordinator.Production(core)
    ops.pinned = pinned
    ops.pair = coordinator.selected_pair(core.owner.STATE)
    ops.seed = ops.pair[0]['selfProof']
    if cp.snapshot.event_digest(ops.get_event(boundary['cursor']))!=boundary['anchor']:
        raise ValueError('inspection_requires_review')
    def scan(lookup, deadline):
        selected_peer, _ = lookup._target(deadline)
        if selected_peer != peers[0]: raise ValueError()
        after = boundary['cursor']
        total = private_count = 0
        for _ in range(200):
            page = lookup._get(lookup.policy.events(after),deadline,timeout=5)
            items = page.get('items')
            if not isinstance(items,list) or len(items)>20: raise ValueError()
            previous = int(after)
            for item in items:
                cp.snapshot.event_digest(item)
                cp.snapshot.cursor(item.get('cursor'))
                if int(item['cursor'])<=previous: raise ValueError()
                previous = int(item['cursor'])
                data = item.get('data')
                if (item.get('kind')=='message.committed' and isinstance(data,dict)
                        and data.get('threadId')==peers[0] and data.get('direction')=='incoming'
                        and data.get('origin')=='live'):
                    private_count += 1
            after = cp.snapshot.cursor(page.get('nextCursor'))
            if int(after)!=previous: raise ValueError()
            total += len(items)
            if not items:
                return dict(complete=True,events_checked=total,private_incoming=private_count,
                            receive_anchor_verified=True,private_target_verified=True,cursor_changed=False)
        return dict(complete=False,events_checked=total,private_incoming=private_count,
                    receive_anchor_verified=True,private_target_verified=True,cursor_changed=False)
    return ops.read(scan,deadline=time.monotonic()+20)


def inspect_offline(project):
    """Only existing files/units; no Native owner, API, mailbox or lifecycle."""
    spec=importlib.util.spec_from_file_location('_offline_canary_formal',project/'scripts/run_forwarder.py')
    formal=importlib.util.module_from_spec(spec); sys.modules[spec.name]=formal; spec.loader.exec_module(formal)
    core=formal.core
    formal.load_config()
    result={'stage':'private_canary_offline_evidence','read_only':True,
            'private_only_configured':core.SETTINGS.get('privateOnly') is True,
            'audits':inspect_audits(core)}
    owner=core.owner.read_json(core.owner.CONTROL/'status.json')
    result['saved_owner_status']={key:owner.get(key) is True for key in ('ready','connected','ok','done')}
    result['saved_owner_status']['code']=owner.get('code') if owner.get('code') in core.owner.EXIT_CODES else 'unclassified'
    cleanup=owner.get('cleanup') or {}
    result['saved_owner_status']['cleanup']={key:value for key,value in cleanup.items()
        if key in {'ok','native_stop_ok','final_snapshot_saved','business_processes_unchanged',
                   'host_namespace_unchanged','host_network_unchanged','host_ra_timer_changed'} and type(value) is bool}
    for name in ('media-maintenance.json','recovery-startup.json'):
        path=core.owner.STATE/name
        if path.exists():
            value=core.owner.read_json(path)
            allowed={'in_progress','requires_review','failed','success','no_gain','recovering','ready',
                     'stop_native','restore','clean','stop_bridge','validate','online','rebind','receive_ready'}
            result[name]={key:value.get(key) if value.get(key) in allowed else 'unclassified'
                          for key in ('status','outcome','phase')}
    archive=core.owner.STATE/'checkpoints/private/current.zip'
    result['snapshot']={'age_seconds':max(0,int(time.time()-archive.stat().st_mtime)),
                        'bytes':archive.stat().st_size}
    audit=core.owner.read_json(core.owner.STATE/'forward-image-live-send.json')
    dispositions=core.recovery_dispositions
    with dispositions.snapshots.copied_database(archive) as (db,metadata):
        row=dispositions._matching(db,audit,'media')
        result['live_image']={'matched':row is not None,'audit_has_job_id':bool(audit.get('jobId'))}
        if row is not None:
            data=json.loads(row[10]); native_result=data.get('result') or {}
            proposed=dispositions.dispositions.entry(metadata['account'],'media',row,
                'unconfirmed_quarantined','operator_review',dispositions._evidence(audit))
            result['live_image']['safe_to_quarantine']=dispositions.admission._safe_pending('media',row,proposed)
            result['live_image'].update(state=row[4] if row[4] in STATES|{'queued','running','completed','needs_review'} else 'unclassified',
                phase=row[5] if row[5] in {'queued','preparing','uploading','submitting','readback','completed'} else 'unclassified',
                proved_pre_submit=dispositions._pre_submit(row),
                has_native_receipt=bool(native_result.get('nativeReceipt')),
                has_server_id=bool(data.get('serverMessageId')),
                completed_bytes=row[7] if type(row[7]) is int else None)
        ledger=dispositions.ledger_for(core,metadata['profileSha256']).read()
        approved={(entry['kind'],entry['recordSha256']):entry for entry in ledger['entries']}
        pending=dispositions.admission.pending_records(db)
        uncovered=[]
        for kind,record in pending:
            key=(kind,dispositions.admission.fingerprint(metadata['account'],kind,record))
            entry=approved.get(key)
            if entry is None or not dispositions.admission._safe_pending(kind,record,entry):
                uncovered.append({'kind':kind,'matches_live_image':kind=='media' and row is not None and record==row})
        result['pending']={'count':len(pending),'ledger_entries':len(approved),'uncovered':uncovered}
        seed=core.self_proof()
        result['snapshot']['accepted_self_proof_present']=dispositions.snapshots._seed(db,[seed])==seed
        with dispositions.snapshots.archive_file(archive) as saved, dispositions.snapshots.state._checked_zip(saved) as bundle:
            generation='profile:'+hashlib.sha256(bundle.read('api-token').strip()).hexdigest()
        bridge=Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve(strict=True)
        with sqlite3.connect(bridge.as_uri()+'?mode=ro',uri=True,timeout=1) as live:
            live.execute('PRAGMA query_only=ON')
            checkpoint=live.execute('SELECT value FROM checkpoints WHERE name=?',(generation,)).fetchone()
            if checkpoint is None: raise ValueError('inspection_requires_review')
            boundary=json.loads(checkpoint[0]); dispositions.snapshots.boundary(boundary)
            result['snapshot']['receive_anchor_matches']=dispositions.snapshots._event(db,int(boundary['cursor']))==boundary
            result['snapshot']['bridge_queue_clear']=not live.execute(
                "SELECT 1 FROM inbox WHERE state IN ('queued','publishing','uncertain') LIMIT 1").fetchone()
            private_audit=core.owner.read_json(core.owner.STATE/'private-forward-send.json')
            peers=[raw for raw, in live.execute("SELECT raw FROM identities WHERE kind='user'")
                   if hashlib.sha256(raw.encode()).hexdigest()==private_audit.get('destinationSha256')]
            if len(peers)==1:
                result['snapshot']['private_events_after_checkpoint']=db.execute(
                    "SELECT count(*) FROM events WHERE sequence>? AND kind='message.committed' "
                    "AND json_valid(data_json) AND json_extract(data_json,'$.threadId')=? "
                    "AND json_extract(data_json,'$.direction')='incoming' "
                    "AND json_extract(data_json,'$.origin')='live'",(int(boundary['cursor']),peers[0])).fetchone()[0]
            else:
                result['snapshot']['private_events_after_checkpoint']=None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path, help='existing deployed checkout (not a new worktree)')
    parser.add_argument('--recovery-proof', action='store_true', help='also verify the selected snapshot and disposition admission; uses a private temporary copy')
    parser.add_argument('--image-jobs',action='store_true',help='GET the exact live audited image job; never resubmit')
    parser.add_argument('--existing-image-snapshot',action='store_true',help='read existing automatic archive only; no owner request or snapshot generation')
    parser.add_argument('--receive-backlog',action='store_true',help='GET-only bounded scan after durable anchor; never moves it or calls a model')
    parser.add_argument('--offline-evidence',action='store_true',help='read only existing snapshot/status/ledger; no Native owner or API required')
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        print(json.dumps({'stage':'private_canary_inspection','ok':False,'code':'linux_root_required'}))
        return 1
    try:
        result = inspect_offline(arguments.project.resolve(strict=True)) if arguments.offline_evidence else inspect(arguments.project.resolve(strict=True), recovery_proof=arguments.recovery_proof, image_jobs=arguments.image_jobs, existing_image_snapshot=arguments.existing_image_snapshot, receive_backlog=arguments.receive_backlog)
    except Exception:
        # Native/SQLite exceptions may contain tokens, paths, IDs or payloads.
        print(json.dumps({'stage':'private_canary_inspection','ok':False,'code':'inspection_requires_review'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
