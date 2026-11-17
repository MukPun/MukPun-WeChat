"""Root-private live sends not yet eligible for a recovery disposition."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re

NAME = 'pending-send-outcomes.json'
MAX_ENTRIES = 32
MAX_BYTES = 256 * 1024
EVIDENCE = 'send-outcome-evidence.json'
EVIDENCE_LIMIT = 128
EVIDENCE_BYTES = 1024 * 1024
AUDITS = {'forward-send.json', 'private-forward-send.json', 'forward-image-send.json',
          'forward-image-live-send.json', 'forward-voice-send.json'}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('pending_send_outcomes')
        value[key] = item
    return value


class PendingSend(Exception):
    def __init__(self, profile):
        super().__init__('send_pending')
        self.profile = profile


def read(core):
    path = core.owner.STATE / NAME
    try:
        value = json.loads(core.owner.read_private(path, MAX_BYTES), object_pairs_hook=_unique)
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'profileSha256', 'entries'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or not isinstance(value['profileSha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['profileSha256'])
            or not isinstance(value['entries'], list)
            or not len(value['entries']) <= MAX_ENTRIES):
        raise ValueError('pending_send_outcomes')
    keys = set()
    for entry in value['entries']:
        if (not isinstance(entry, dict) or set(entry) != {'auditName', 'kind', 'audit', 'nativePid', 'nativeTicks'}
                or entry['auditName'] not in AUDITS or entry['kind'] not in {'text', 'media'}
                or not isinstance(entry['audit'], dict) or len(json.dumps(entry['audit'])) > 4096
                or any(type(entry[k]) is not int or entry[k] <= 0 for k in ('nativePid', 'nativeTicks'))):
            raise ValueError('pending_send_outcomes')
        key = entry['audit'].get('key')
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{32}', key) or key in keys:
            raise ValueError('pending_send_outcomes')
        keys.add(key)
    return value


@contextmanager
def locked(core, *, nonblocking=False, profile=None):
    # Same no-follow/private-directory primitive as the immutable ledger.
    module = core.recovery_dispositions
    ledger = module.dispositions.Ledger(core.owner.STATE / NAME, '0' * 64)
    with ledger._directory() as directory:
        fd = os.open(NAME + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        try:
            module.dispositions._check_file(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError:
                raise PendingSend(profile) from None
            yield directory
        finally:
            os.close(fd)


def write(core, value, directory):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(raw.encode()) > MAX_BYTES:
        raise ValueError('pending_send_outcomes')
    core.owner.write_json(core.owner.STATE / NAME, value)
    os.fsync(directory)


def evidence(core):
    try:
        value = json.loads(core.owner.read_private(core.owner.STATE / EVIDENCE, EVIDENCE_BYTES), object_pairs_hook=_unique)
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'profileSha256', 'entries'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or not isinstance(value['entries'], list) or len(value['entries']) > EVIDENCE_LIMIT):
        raise ValueError('send_outcome_evidence')
    module = core.recovery_dispositions
    keys = set()
    for entry in value['entries']:
        if (not isinstance(entry, dict) or set(entry) != {'auditName', 'kind', 'audit'}
                or entry['auditName'] not in AUDITS or entry['kind'] not in {'text', 'media'}
                or not isinstance(entry['audit'], dict) or len(json.dumps(entry['audit'])) > 4096):
            raise ValueError('send_outcome_evidence')
        audit = entry['audit']; key = audit.get('key')
        marker = audit.get('recoveryDisposition')
        if (not isinstance(key, str) or not re.fullmatch('[0-9a-f]{32}', key) or key in keys
                or not module._valid_marker(marker, module._evidence(audit))
                or marker['profileSha256'] != value['profileSha256']):
            raise ValueError('send_outcome_evidence')
        keys.add(key)
    return value


def archive(core, path, audit, kind, *, directory=None):
    if directory is None:
        with locked(core) as fd:
            return archive(core, path, audit, kind, directory=fd)
    marker = audit['recoveryDisposition']
    value = evidence(core) or dict(schemaVersion=1, profileSha256=marker['profileSha256'], entries=[])
    entry = dict(auditName=Path(path).name, kind=kind, audit=dict(audit))
    if entry['auditName'] not in AUDITS or value['profileSha256'] != marker['profileSha256']:
        raise ValueError('send_outcome_evidence')
    prior = next((e for e in value['entries'] if e['audit'].get('key') == audit.get('key')), None)
    if prior is not None:
        previous = prior['audit']['recoveryDisposition']
        if (prior['auditName'] != entry['auditName'] or prior['kind'] != kind or previous['evidenceSha256'] != marker['evidenceSha256']
                or previous['recordSha256'] != marker['recordSha256']):
            raise ValueError('send_outcome_evidence')
        return previous
    if len(value['entries']) >= EVIDENCE_LIMIT:
        raise ValueError('send_outcome_evidence')
    value['entries'].append(entry)
    if len(json.dumps(value).encode()) > EVIDENCE_BYTES:
        raise ValueError('send_outcome_evidence')
    core.owner.write_json(core.owner.STATE / EVIDENCE, value)
    os.fsync(directory)
    return marker


def preserve(core, pinned, path, audit, kind, profile):
    if Path(path).name not in AUDITS or not pinned.unchanged():
        raise ValueError('pending_send_outcomes')
    entry = dict(auditName=Path(path).name, kind=kind, audit=dict(audit),
                 nativePid=pinned.host.client_pid, nativeTicks=pinned.host.client_ticks)
    with locked(core) as directory:
        value = read(core) or dict(schemaVersion=1, profileSha256=profile, entries=[])
        if value['profileSha256'] != profile:
            raise ValueError('pending_send_outcomes')
        prior = next((e for e in value['entries'] if e['audit'].get('key') == audit.get('key')), None)
        if prior is not None and prior != entry or prior is None and len(value['entries']) >= MAX_ENTRIES:
            raise ValueError('pending_send_outcomes')
        if prior is None:
            value['entries'].append(entry)
            write(core, value, directory)
        core.recovery_dispositions.write_evidence(core, path, audit)


def covers(core, pinned, audit):
    value = read(core)
    return bool(value and pinned and pinned.unchanged() and any(
        e['audit'] == audit and (e['nativePid'], e['nativeTicks']) ==
        (pinned.host.client_pid, pinned.host.client_ticks) for e in value['entries']))


def settle(core, pinned, snapshot_path):
    """Called only on the fixed snapshot while BOTH worker barriers are held."""
    module = core.recovery_dispositions
    if read(core) is None:
        return 0
    with locked(core) as directory:
        value = read(core)
        if not value or not value['entries']:
            return 0
        if not pinned.unchanged() or any((e['nativePid'], e['nativeTicks']) !=
                (pinned.host.client_pid, pinned.host.client_ticks) for e in value['entries']):
            raise ValueError('pending_send_outcomes')
        remaining = []
        with module.snapshots.copied_database(snapshot_path) as (db, metadata):
            if value['profileSha256'] != metadata['profileSha256'] or not pinned.unchanged():
                raise ValueError('pending_send_outcomes')
            for entry in value['entries']:
                audit = dict(entry['audit'])
                try:
                    marker = module._record(core, db, metadata, audit, entry['kind'], 'unconfirmed_quarantined')
                except PendingSend:
                    remaining.append(entry)
                    continue
                audit['recoveryDisposition'] = marker
                path = core.owner.STATE / entry['auditName']
                archive(core, path, audit, entry['kind'], directory=directory)
                if path.exists() and core.owner.read_json(path) == entry['audit']:
                    module.write_evidence(core, path, audit)
                from wechat_bridge.runtime import outcomes
                outcomes.record(core, 'text' if entry['kind'] == 'text' else 'voice' if 'durationMs' in audit else 'image',
                                'unknown', 'pending_send_settled', audit, retcode=1408)
        if remaining != value['entries']:
            write(core, {**value, 'entries': remaining}, directory)
        return len(remaining)


def settle_live(core, pinned):
    """Bounded safe-point sweep against one live WAL view, without a freezer."""
    module = core.recovery_dispositions
    value = read(core)
    if not value or not value['entries']:
        return 0
    try:
        context = locked(core, nonblocking=True, profile=value['profileSha256'])
        directory = context.__enter__()
    except PendingSend:
        return len(value['entries'])
    try:
        value = read(core)
        if not value or not value['entries']:
            return 0
        if not pinned.unchanged() or any((e['nativePid'], e['nativeTicks']) !=
                (pinned.host.client_pid, pinned.host.client_ticks) for e in value['entries']):
            raise ValueError('pending_send_outcomes')
        remaining = []
        try:
            targets = [(e['kind'], e['audit']) for e in value['entries']]
            with module.persistent.database(core, pinned, targets) as (db, metadata, verify):
                if value['profileSha256'] != metadata['profileSha256']:
                    raise ValueError('pending_send_outcomes')
                for entry in value['entries']:
                    audit = dict(entry['audit'])
                    try:
                        marker = module._record(core, db, metadata, audit, entry['kind'], 'unconfirmed_quarantined', record=False)
                    except PendingSend:
                        remaining.append(entry)
                        continue
                    audit['recoveryDisposition'] = marker
                    path = core.owner.STATE / entry['auditName']
                    audit['recoveryDisposition'] = archive(core, path, audit, entry['kind'], directory=directory)
                    module._record(core, db, metadata, audit, entry['kind'], 'unconfirmed_quarantined')
                    if path.exists() and core.owner.read_json(path) == entry['audit']:
                        module.write_evidence(core, path, audit)
                # Archival precedes both new startup permission and pending removal.
                module.persistent.refresh_admission(core, pinned, db, metadata, verify)
                verify()
                if remaining != value['entries']:
                    write(core, {**value, 'entries': remaining}, directory)
                return len(remaining)
        except PendingSend:
            return len(value['entries'])
    finally:
        context.__exit__(None, None, None)
