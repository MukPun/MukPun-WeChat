#!/usr/bin/env python3
"""Read fixed image-job receipt metadata only; never submits or retries."""
import importlib.util
import json
import os
from pathlib import Path
import resource
import sqlite3
import stat
import sys
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent


def main():
    if os.geteuid() != 0 or sys.argv[1:] not in ([], ['--bridge']):
        return 2
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    spec = importlib.util.spec_from_file_location('_job_inspection', ROOT / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core, owner = formal.core, formal.core.owner
    bridge = sys.argv[1:] == ['--bridge']
    audit = owner.read_json(owner.STATE / ('forward-image-send.json' if bridge else 'image-output-poc-private.json'))
    core.require((bridge or audit.get('scope') == 'private') and core.output_images.job_id(audit.get('jobId')), 'job_binding')
    pinned = core.groups.PinnedNative()
    pinned.open()
    work = Path(tempfile.mkdtemp(prefix='astrbot-wechat-image-job-review.', dir='/var/tmp'))
    try:
        core.trial.fresh_status()
        store = owner.companion('session_state_store')
        latest = store.SnapshotStore(owner.STATE / 'checkpoints').latest()
        with zipfile.ZipFile(latest, 'r') as archive:
            store._archive_manifest(archive)
            names = [i.filename for i in archive.infolist() if i.filename.endswith('/agent/control.sqlite')]
            core.require(len(names) == 1, 'database_layout')
            for suffix in ('', '-wal', '-shm', '-journal'):
                if names[0] + suffix in archive.namelist():
                    with archive.open(names[0] + suffix) as source, (work / ('control.sqlite' + suffix)).open('wb') as target:
                        shutil.copyfileobj(source, target, 65536)
        path = work / 'control.sqlite'
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            row = connection.execute('SELECT state,phase,data_json FROM media_jobs WHERE id=?', (audit['jobId'],)).fetchone()
            core.require(row is not None and len(row[2]) <= 65536, 'job_missing')
            data = json.loads(row[2])
            columns = [item[1] for item in connection.execute('PRAGMA table_info(media_jobs)')]
            error_columns = {}
            for col in columns:
                if 'error' in col.lower() and col.replace('_','').isalnum():
                    value = connection.execute('SELECT "'+col+'" FROM media_jobs WHERE id=?',(audit['jobId'],)).fetchone()[0]
                    if value is not None and len(str(value)) <= 4096:
                        error_columns[col] = str(value)
            recent = []
            for state,phase,payload in connection.execute('SELECT state,phase,data_json FROM media_jobs ORDER BY rowid DESC LIMIT 12'):
                item=json.loads(payload)
                preparation=item.get('result',{}).get('imagePreparation')
                if isinstance(preparation,dict):
                    recent.append({'state':state,'phase':phase,'width':preparation.get('width'),'height':preparation.get('height'),
                        'same_source':preparation.get('sourceSha256')==audit.get('sourceSha256'),
                        'server_receipt':item.get('result',{}).get('nativeReceipt',{}).get('status')})
        finally:
            connection.close()
        result = data.get('result', {})
        prepared = result.get('imagePreparation', {})
        receipt = result.get('nativeReceipt', {})
        hashes = result.get('verifiedSha256')
        public = {'state': row[0], 'phase': row[1],
            'audit_state': audit.get('state'), 'audit_code': audit.get('code'), 'source_bytes': audit.get('bytes'),
            'receipt_status': receipt.get('status'), 'service_code': receipt.get('serviceCode'),
            'client_id_matches': receipt.get('clientIdMatches'),
            'server_id_matches': receipt.get('serverMessageId') == data.get('serverMessageId'),
            'readback_verified': result.get('uploadReadbackVerified'),
            'source_hash_matches': prepared.get('sourceSha256') == audit.get('sourceSha256'),
            'source_preserved': prepared.get('sourcePreserved'),
            'prepared_width': prepared.get('width'), 'prepared_height': prepared.get('height'),
            'verified_hash_count': len(hashes) if isinstance(hashes, list) else None,
            'original_hash_matches': bool(hashes and hashes[0] == prepared.get('sentOriginalSha256')),
            'profile_projection': result.get('profileProjection'),
            'projected_message_count': len(result.get('messageIds', [])),
            'native_session_unchanged': pinned.unchanged()}
        error = data.get('error') or {}
        public['job_columns'] = columns
        public['data_keys'] = sorted(data)
        public['recent_images'] = recent
        public['stored_error_codes'] = {}
        import re
        for field,message in error_columns.items():
            public['stored_error_codes'][field] = sorted(set(re.findall(r'\b(?:unsupported|invalid|image|media|cdn|profile)_[a-z0-9_]{1,80}\b',message.lower())))
            public['stored_error_categories'] = {word:word in message.lower() for word in ('variant','size','dimensions','thumbnail','original','upload','network','timeout','decode','permission','space')}
        public['error_keys'] = sorted(error) if isinstance(error,dict) else []
        if isinstance(error,dict):
            code = error.get('code')
            import re
            public['error_code'] = code if isinstance(code,str) and re.fullmatch('[a-zA-Z0-9_:-]{1,128}',code) else None
            message = str(error.get('message','')).lower()
            public['error_categories'] = {word:word in message for word in ('decode','webp','unsupported','timeout','upload','denied','image','jpeg')}
        if bridge:
            spool = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve(strict=True)
            db = sqlite3.connect(spool.as_uri()+'?mode=ro', uri=True, timeout=.2)
            try:
                public['queue'] = dict(db.execute('SELECT state,count(*) FROM inbox GROUP BY state'))
                submissions = db.execute('SELECT result FROM submissions WHERE native_key=?', (audit['key'],)).fetchall()
                public['bound_submission_count'] = len(submissions)
                public['submission_retcodes'] = [json.loads(item[0]).get('retcode') if item[0] else None for item in submissions]
            finally:
                db.close()
        print(json.dumps({'stage': 'image_job_receipt_inspection', **public}, sort_keys=True))
    finally:
        pinned.close()
        for suffix in ('', '-wal', '-shm', '-journal'):
            (work / ('control.sqlite' + suffix)).unlink(missing_ok=True)
        work.rmdir()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'stage':'image_job_inspection_failed','error_class':type(error).__name__}))
        raise SystemExit(1)
