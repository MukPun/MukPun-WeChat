"""Pure, bounded admission of unchanged non-replayable sends in a copied DB.

The caller recovers journals in a private copy first and independently compares
profile_sha256(restored_key_hashes) with the admission's profileSha256. This
module never edits Native jobs or interprets an approval as permission to retry.
"""
import hashlib
import json
import re
import sqlite3

TEXT_COLUMNS = 'id,idempotency_key,payload_hash,destination,payload,state,client_id,server_id,error_code,created_at'
MEDIA_COLUMNS = ('id,idempotency_key,request_hash,kind,state,phase,attempts,'
                 'cancel_requested,retryable,error_code,data_json,payload,created_at,updated_at')
MAX_ENTRIES = 128
MAX_BYTES = 65536
MAX_ROW_BYTES = 16 * 1024 * 1024
ENTRY_FIELDS = {'kind', 'recordSha256', 'disposition', 'evidenceKind', 'evidenceSha256'}
EVIDENCE = {'accepted_verified': {'native_receipt'},
            'rejected_before_submit': {'native_pre_submit'},
            'unconfirmed_quarantined': {'legacy_hold', 'unconfirmed_result', 'operator_review'},
            'reviewed_not_delivered': {'operator_review'}}


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def profile_sha256(key_hashes):
    """Canonical SHA256 of the exact named key digests, never the keys themselves."""
    if (not isinstance(key_hashes, dict)
            or set(key_hashes) != {'api-token', 'storage-key', 'cursor-key'}
            or not all(_sha(v) for v in key_hashes.values())):
        raise ValueError('restore_profile')
    return hashlib.sha256(_encoded(key_hashes)).hexdigest()


def fingerprint(account, kind, row):
    """Hash account and all typed columns; retain the legacy media byte framing."""
    columns = {'text': TEXT_COLUMNS, 'media': MEDIA_COLUMNS}
    if (not isinstance(account, str) or not account or not isinstance(kind, str) or kind not in columns
            or not isinstance(row, (tuple, list)) or len(row) != len(columns[kind].split(','))):
        raise ValueError('restore_fingerprint')
    digest = hashlib.sha256(b'native-quarantined-send-v1' if kind == 'media'
                            else b'native-text-send-v1')
    total = 0
    for item in (account, *row):
        if item is None:
            raw = b'N'
        elif type(item) is int:
            raw = b'I' + str(item).encode('ascii')
        elif isinstance(item, str):
            raw = b'S' + item.encode('utf-8')
        elif isinstance(item, bytes):
            raw = b'B' + item
        else:
            raise ValueError('restore_fingerprint')
        total += len(raw)
        if total > MAX_ROW_BYTES:
            raise ValueError('restore_fingerprint')
        digest.update(len(raw).to_bytes(8, 'big'))
        digest.update(raw)
    return digest.hexdigest()


def _validate_entry(value):
    if (not isinstance(value, dict) or set(value) != ENTRY_FIELDS
            or not isinstance(value['kind'], str) or value['kind'] not in {'text', 'media'}
            or not _sha(value['recordSha256']) or not _sha(value['evidenceSha256'])
            or not isinstance(value['disposition'], str) or value['disposition'] not in EVIDENCE
            or not isinstance(value['evidenceKind'], str)
            or value['evidenceKind'] not in EVIDENCE[value['disposition']]):
        raise ValueError('restore_admission')


def _validate_entries(entries):
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ValueError('restore_admission')
    seen = set()
    for value in entries:
        _validate_entry(value)
        # No record digest may be repeated, including under another kind.
        if value['recordSha256'] in seen:
            raise ValueError('restore_admission')
        seen.add(value['recordSha256'])


def validate_admission(value):
    """Validate an already decoded object; JSON decoders must reject duplicate keys."""
    try:
        if (not isinstance(value, dict)
                or set(value) != {'schemaVersion', 'profileSha256', 'ledgerRevision', 'entries'}
                or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
                or not _sha(value['profileSha256'])
                or type(value['ledgerRevision']) is not int or value['ledgerRevision'] < 0):
            raise ValueError('restore_admission')
        _validate_entries(value['entries'])
        if len(_encoded(value)) > MAX_BYTES:
            raise ValueError('restore_admission')
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError('restore_admission') from None
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('restore_json')
        result[key] = value
    return result


def _pending(db):
    db.execute('PRAGMA trusted_schema=OFF')
    tables = dict(db.execute("SELECT name,type FROM sqlite_master WHERE name IN ('outbox','media_jobs')"))
    if tables != {'outbox': 'table', 'media_jobs': 'table'}:
        raise ValueError('restore_database')
    records = []
    total_bytes = 0
    for table, kind, columns, condition in [
        ('outbox', 'text', TEXT_COLUMNS, "state IS NULL OR state NOT IN ('accepted','failed','cancelled')"),
        ('media_jobs', 'media', MEDIA_COLUMNS, "kind IS NULL OR kind NOT IN ('send','download') OR "
         "(kind='send' AND (state IS NULL OR state NOT IN ('accepted','failed','cancelled','unsupported','expired')))")]:
        info = db.execute('PRAGMA table_xinfo('+table+')').fetchall()
        if [r[1] for r in info] != columns.split(',') or any(r[6] != 0 for r in info):
            raise ValueError('restore_database')
        # Inspect only bounded size metadata before materializing blobs. The
        # shared budget also prevents 128 individually bounded rows exhausting
        # the restore worker's memory.
        sizes = '+'.join('coalesce(length(CAST('+c+' AS BLOB)),0)' for c in columns.split(','))
        size_rows = db.execute('SELECT '+sizes+' FROM '+table+' WHERE '+condition+
                               ' LIMIT ?', (MAX_ENTRIES + 1,)).fetchall()
        total_bytes += sum(r[0] + 9 * len(columns.split(',')) for r in size_rows)
        if len(records) + len(size_rows) > MAX_ENTRIES or total_bytes > MAX_ROW_BYTES:
            raise ValueError('restore_database')
        rows = db.execute('SELECT '+columns+' FROM '+table+' WHERE '+condition+
                          ' LIMIT ?', (MAX_ENTRIES + 1,)).fetchall()
        records.extend((kind, tuple(row)) for row in rows)
        if len(records) > MAX_ENTRIES:
            raise ValueError('restore_database')
    return records


def pending_records(db):
    """Read a bounded, consistent inventory from a caller-owned sqlite connection.

    WAL is read normally; immutable SQLite mode is intentionally never used.
    Existing caller transactions are preserved, including uncommitted fixture data.
    """
    if not isinstance(db, sqlite3.Connection):
        raise ValueError('restore_database')
    own_transaction = not db.in_transaction
    try:
        if own_transaction:
            db.execute('BEGIN')
        return _pending(db)
    except (sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError('restore_database') from None
    finally:
        if own_transaction and db.in_transaction:
            db.rollback()


def _has_receipt(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'serverId', 'server_id', 'serverMessageId', 'nativeReceipt', 'native_receipt'} and item is not None:
                return True
            if _has_receipt(item):
                return True
    elif isinstance(value, list):
        return any(_has_receipt(item) for item in value)
    return False


def _row_shape(kind, row):
    required = ((0, 1, 2, 3, 5, 9) if kind == 'text'
                else (0, 1, 2, 3, 4, 5, 10, 12, 13))
    if any(not isinstance(row[i], str) or not row[i] for i in required):
        return False
    if kind == 'text':
        return (isinstance(row[4], bytes) and
                all(row[i] is None or isinstance(row[i], str) for i in (6, 7, 8)))
    return ((row[11] is None or isinstance(row[11], bytes)) and
            (row[9] is None or isinstance(row[9], str)))


def _safe_pending(kind, row, entry):
    if not _row_shape(kind, row) or entry['disposition'] == 'accepted_verified':
        return False
    if kind == 'text':
        return (row[5] == 'needs_review' and row[7] is None and
                entry['disposition'] in {'unconfirmed_quarantined', 'reviewed_not_delivered'})
    if (row[3:5] != ('send', 'needs_review') or row[5] not in {'preparing', 'uploading', 'submitting', 'readback'}
            or any(type(row[i]) is not int for i in (6, 7, 8)) or row[6:9] != (1, 0, 0)):
        return False
    if entry['disposition'] == 'rejected_before_submit':
        if row[5] not in {'preparing', 'uploading'} or not isinstance(row[10], str):
            return False
        try:
            data = json.loads(row[10], object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            return isinstance(data, dict) and not _has_receipt(data)
        except (ValueError, RecursionError):
            return False
    return entry['disposition'] in {'unconfirmed_quarantined', 'reviewed_not_delivered'}


def validate_database(db, account, admission):
    """Require the exact approved pending inventory without changing any row."""
    validate_admission(admission)
    approved = {(v['kind'], v['recordSha256']): v for v in admission['entries']}
    seen = set()
    try:
        for kind, row in pending_records(db):
            key = (kind, fingerprint(account, kind, row))
            if key in seen or key not in approved or not _safe_pending(kind, row, approved[key]):
                raise ValueError('restore_database')
            seen.add(key)
        if seen != set(approved):
            raise ValueError('restore_database')
    except (ValueError, TypeError, RecursionError):
        raise ValueError('restore_database') from None
    return admission
