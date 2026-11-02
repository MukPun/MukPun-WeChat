"""Private immutable send dispositions. Stores only hashes and fixed classes.

One ledger belongs to one profile lineage. A disposition is evidence for human
review, never authorization to edit a Native row or submit a send again.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat

try:
    from . import restore_admission as admission
except ImportError:
    import restore_admission as admission


def entry(account, kind, row, disposition, evidence_kind, evidence_sha256):
    try:
        result = dict(kind=kind, recordSha256=admission.fingerprint(account, kind, row),
                      disposition=disposition, evidenceKind=evidence_kind, evidenceSha256=evidence_sha256)
        admission._validate_entry(result)
        return result
    except (ValueError, TypeError, RecursionError):
        raise ValueError('send_disposition') from None


def _check_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > admission.MAX_BYTES):
        raise ValueError('send_ledger')


class Ledger:
    def __init__(self, path, profile_sha256):
        self.path = Path(path)
        self.profile_sha256 = profile_sha256
        if not admission._sha(profile_sha256) or self.path.name in {'', '.', '..'}:
            raise ValueError('send_ledger')

    @contextmanager
    def _directory(self):
        """Pin the private parent through no-follow directory descriptors."""
        fd = None
        try:
            absolute = self.path.absolute()
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            for component in absolute.parent.parts[1:]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('send_ledger')
            yield fd
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            raise ValueError('send_ledger') from None
        finally:
            if fd is not None:
                os.close(fd)

    def _empty(self):
        return dict(schemaVersion=1, profileSha256=self.profile_sha256, revision=0, entries=[])

    def _validate(self, value):
        if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'profileSha256', 'revision', 'entries'}
                or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
                or value['profileSha256'] != self.profile_sha256
                or type(value['revision']) is not int or value['revision'] < 0):
            raise ValueError('send_ledger')
        admission._validate_entries(value['entries'])
        if len(admission._encoded(value)) > admission.MAX_BYTES:
            raise ValueError('send_ledger')
        return value

    def _read(self, directory):
        try:
            fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        except FileNotFoundError:
            return self._empty()
        try:
            _check_file(fd)
            with os.fdopen(fd, 'rb', closefd=False) as source:
                raw = source.read(admission.MAX_BYTES + 1)
            if len(raw) > admission.MAX_BYTES:
                raise ValueError('send_ledger')
            return self._validate(json.loads(raw, object_pairs_hook=admission._unique_object))
        finally:
            os.close(fd)

    def read(self):
        with self._directory() as directory:
            return self._read(directory)

    def record(self, value):
        """Serialize writers, reject conflicting evidence, atomically persist once."""
        try:
            admission._validate_entry(value)
            value = dict(value)
        except (ValueError, TypeError):
            raise ValueError('send_ledger') from None
        with self._directory() as directory:
            lock = os.open(self.path.name+'.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                           os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory)
            try:
                _check_file(lock)
                fcntl.flock(lock, fcntl.LOCK_EX)
                current = self._read(directory)
                for previous in current['entries']:
                    if previous['recordSha256'] == value['recordSha256']:
                        if previous != value:
                            raise ValueError('send_ledger')
                        return current
                updated = {**current, 'revision': current['revision'] + 1,
                           'entries': [*current['entries'], value]}
                self._validate(updated)
                self._write(directory, admission._encoded(updated))
                return updated
            finally:
                os.close(lock)

    def _write(self, directory, raw):
        temporary = '.'+self.path.name+'.'+secrets.token_hex(12)
        fd = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                         os.O_CLOEXEC, 0o600, dir_fd=directory)
            with os.fdopen(fd, 'wb', closefd=False) as target:
                target.write(raw)
                target.flush()
                os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def admission_for(db, account, ledger):
    """Select only exact safe pending entries; historical terminal entries stay private."""
    value = ledger.read()
    available = {(v['kind'], v['recordSha256']): v for v in value['entries']}
    selected = []
    try:
        for kind, row in admission.pending_records(db):
            key = (kind, admission.fingerprint(account, kind, row))
            if key not in available or not admission._safe_pending(kind, row, available[key]):
                raise ValueError('send_admission')
            selected.append(available[key])
        result = dict(schemaVersion=1, profileSha256=value['profileSha256'],
                      ledgerRevision=value['revision'], entries=selected)
        admission.validate_admission(result)
    except (ValueError, TypeError, RecursionError):
        raise ValueError('send_admission') from None
    return result
