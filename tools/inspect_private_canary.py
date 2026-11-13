#!/usr/bin/env python3
"""Read-only, redacted rollout inspection. No service/config/session mutations."""
import argparse
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


def inspect(project, *, recovery_proof=False, image_jobs=False):
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
        if image_jobs:
            path = core.owner.STATE/'forward-image-live-send.json'
            audit = core.owner.read_json(path)
            job = core.image_recovery.read_job(pinned,audit)
            detail = {'read_ok':isinstance(job,dict)}
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
    finally:
        # close() only releases our FDs; never call SessionHost.close().
        pinned.close()
    if recovery_proof:
        pair = core.recovery_coordinator.selected_pair(core.owner.STATE)
        admitted = core.recovery_coordinator._admit(core, pair)
        result['recovery'] = {'manifest_verified': True, 'admission_verified': True,
            'quarantined':len(admitted['entries']), 'ledger_revision': admitted['ledgerRevision']}
    result['inspection_complete'] = True
    result['deployed'] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path, help='existing deployed checkout (not a new worktree)')
    parser.add_argument('--recovery-proof', action='store_true', help='also verify the selected snapshot and disposition admission; uses a private temporary copy')
    parser.add_argument('--image-jobs',action='store_true',help='GET the exact live audited image job; never resubmit')
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        print(json.dumps({'stage':'private_canary_inspection','ok':False,'code':'linux_root_required'}))
        return 1
    try:
        result = inspect(arguments.project.resolve(strict=True), recovery_proof=arguments.recovery_proof, image_jobs=arguments.image_jobs)
    except Exception:
        # Native/SQLite exceptions may contain tokens, paths, IDs or payloads.
        print(json.dumps({'stage':'private_canary_inspection','ok':False,'code':'inspection_requires_review'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
