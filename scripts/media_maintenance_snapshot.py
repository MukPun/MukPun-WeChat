"""Read-only, copied-snapshot proof for saved-login media maintenance."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile


def analyze(snapshot, guest, *, audit=None, approved=None):
    """Return key hashes and exact pre-approved unknown row; never infer approval."""
    with tempfile.TemporaryDirectory(prefix='wechat-media-proof.') as directory:
        work = Path(directory)
        with zipfile.ZipFile(snapshot) as archive:
            guest.state_store._archive_manifest(archive)
            names = [n for n in archive.namelist() if n.endswith('/agent/control.sqlite')]
            if len(names) != 1:
                raise ValueError('maintenance_database_layout')
            name = names[0]
            account = name.split('/')[1]
            keys = {}
            for key in ('api-token', 'storage-key', 'cursor-key'):
                entry = archive.getinfo(key)
                if not 0 < entry.file_size <= 8192:
                    raise ValueError('maintenance_key_bound')
                keys[key] = hashlib.sha256(archive.read(key)).hexdigest()
            for suffix in ('', '-wal', '-shm', '-journal'):
                if name+suffix in archive.namelist():
                    with archive.open(name+suffix) as src, (work/('control.sqlite'+suffix)).open('xb') as dst:
                        shutil.copyfileobj(src, dst, 65536)
        with sqlite3.connect(work/'control.sqlite', timeout=.2) as db:
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('maintenance_database_integrity')
            if db.execute("SELECT count(*) FROM outbox WHERE state IS NULL OR state NOT IN ('accepted','failed','cancelled')").fetchone() != (0,):
                raise ValueError('maintenance_pending_outbox')
            rows = db.execute('SELECT '+guest.QUARANTINED_COLUMNS+" FROM media_jobs WHERE kind IS NULL OR kind NOT IN ('send','download') OR (kind='send' AND (state IS NULL OR state NOT IN ('accepted','failed','cancelled','unsupported','expired')))").fetchmany(2)
            if approved is None:
                if rows or audit is not None:
                    raise ValueError('maintenance_unknown_send')
                return {'keys': keys, 'quarantinedSend': None}
            guest.validate_quarantined_send(approved)
            if len(rows) != 1 or not isinstance(audit, dict):
                raise ValueError('maintenance_unknown_send')
            row = rows[0]
            if (row[3:9] != ('send','needs_review','submitting',1,0,0)
                    or row[0] != audit.get('jobId') or row[1] != 'send:'+audit.get('key', '')
                    or guest.quarantined_send_fingerprint(account, row) != approved['jobSha256']):
                raise ValueError('maintenance_unknown_send_changed')
            data = json.loads(row[10])
            result = data.get('result') or {}
            prep = result.get('imagePreparation') or {}
            source = db.execute('SELECT sha256,byte_length FROM media_sources WHERE id=?', (data.get('sourceId'),)).fetchone()
            if (hashlib.sha256(data['threadId'].encode()).hexdigest() != audit.get('destinationSha256')
                    or prep.get('sourceSha256') != audit.get('sourceSha256')
                    or data.get('completedBytes') != data.get('totalBytes')
                    or result.get('uploadReadbackVerified') is not True or result.get('nativeReceipt')
                    or source != (audit.get('sourceSha256'), audit.get('bytes'))):
                raise ValueError('maintenance_unknown_binding')
            return {'keys': keys, 'quarantinedSend': approved}
