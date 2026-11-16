"""Private, bounded send diagnostics. Never accepts message bodies or exceptions."""
import json
import fcntl
import os
import re
import stat
import sys
import time

NAME = 'send-outcomes.jsonl'
MAX_BYTES = 1024 * 1024
BACKUPS = 3
HASH_FIELDS = {'destinationSha256', 'textSha256', 'sourceSha256'}
ID_FIELDS = {'requestId', 'key', 'sendId', 'jobId'}
NUMBER_FIELDS = {'bytes', 'durationMs', 'segmentBytes'}
ACTION_CODES = frozenset({'invalid_request', 'unsupported_message_segment', 'send_requires_review',
    'no_active_private_conversation', 'recovery_capacity', 'destination_not_allowed',
    'outcome_unknown', 'state_unavailable', 'action_not_supported'})


def action_code(record, fallback):
    return record['code'] if record.get('code') in ACTION_CODES else fallback


def _file(directory, name):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('send_outcome_log')
    return info


def record(core, media, outcome, code, audit=None, *, retcode=None, max_bytes=MAX_BYTES):
    """Serialized root/controller writers; four complete JSONL files maximum."""
    if media not in {'text', 'image', 'voice', 'action'} or outcome not in {
            'accepted', 'rejected', 'unknown', 'reconciled', 'busy'}:
        raise ValueError('send_outcome_log')
    value = dict(schemaVersion=1, timestamp=int(time.time()), stage='send_outcome',
                 media=media, outcome=outcome,
                 code=code if isinstance(code, str) and re.fullmatch('[a-z][a-z0-9_]{0,63}', code) else 'unclassified')
    for name, item in (audit or {}).items():
        if name in HASH_FIELDS and isinstance(item, str) and re.fullmatch('[0-9a-f]{64}', item):
            value[name] = item
        elif name in ID_FIELDS and isinstance(item, str) and re.fullmatch('[a-zA-Z0-9_-]{1,100}', item):
            value[name] = item
        elif name in NUMBER_FIELDS and type(item) is int and 0 <= item <= 1024 * 1024 * 1024:
            value[name] = item
    marker = (audit or {}).get('recoveryDisposition', {})
    if marker.get('classification') in {'unconfirmed_quarantined', 'rejected_before_submit', 'reviewed_not_delivered'}:
        value['disposition'] = marker['classification']
    if type(retcode) is int and 0 <= retcode <= 9999:
        value['retcode'] = retcode
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    if not 512 <= max_bytes <= MAX_BYTES or len(raw) > max_bytes:
        raise ValueError('send_outcome_log')
    directory = os.open(core.owner.STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    lock = None
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError('send_outcome_log')
        lock = os.open(NAME + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        _file(directory, NAME + '.lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        files = {name: _file(directory, name) for name in [NAME] + [NAME + '.' + str(i) for i in range(1, BACKUPS + 1)]}
        if files[NAME] and files[NAME].st_size + len(raw) > max_bytes:
            for index in range(BACKUPS, 0, -1):
                source = NAME if index == 1 else NAME + '.' + str(index - 1)
                if files[source]:
                    os.replace(source, NAME + '.' + str(index), src_dir_fd=directory, dst_dir_fd=directory)
        fd = os.open(NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('send_outcome_log')
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)
    sys.stderr.write(raw.decode())
    sys.stderr.flush()
    return value
