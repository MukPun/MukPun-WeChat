"""Root-owned Native data, explicit opt-in, one lifetime writer, no live backup.

The root owner must prove its exact guest stopped before sealing. A lost owner
leaves a leased marker requiring offline review, even if flock was released.
Inventory caps and minimum free headroom are admission/poll checks, not a disk
quota. Callers must retain Native's file/resource limits and bounded scratch.
"""
from __future__ import annotations

from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # Legacy offline imports remain portable.
    fcntl = None
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sqlite3

GUEST_DATA = '/var/lib/astrbot-wechat-persistent-data'
MAX_BYTES = 1024 * 1024 * 1024
MAX_FILE_BYTES = 768 * 1024 * 1024
MAX_ENTRIES = 1024
MIN_FREE = 128 * 1024 * 1024
KEYS = ('api-token', 'storage-key', 'cursor-key')
CODES = frozenset({'persistent_unsafe', 'persistent_io', 'persistent_busy',
                   'persistent_requires_review', 'persistent_limits',
                   'persistent_headroom', 'persistent_profile', 'persistent_binding'})


class PersistentError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in CODES else 'persistent_io'
        super().__init__(self.code)


def _safe(function):
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PersistentError:
            raise
        except Exception:
            raise PersistentError('persistent_io') from None
    return wrapped


def _admission_module():
    spec = importlib.util.spec_from_file_location('_persistent_admission', Path(__file__).with_name('restore_admission.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require(ok, code='persistent_unsafe'):
    if not ok:
        raise PersistentError(code)


@contextmanager
def _directory(path):
    """No-follow every component, never resolve a user-controlled ancestor."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _private(fd):
    info = os.fstat(fd)
    _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)


def _file(fd, *, uid, dev=None):
    return _file_info(os.fstat(fd), uid=uid, dev=dev)


def _file_info(info, *, uid, dev=None, volatile=False):
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink in ((0, 1) if volatile else (1,)) and info.st_uid == uid
             and stat.S_IMODE(info.st_mode) == 0o600 and (dev is None or info.st_dev == dev))
    return info


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _read(parent):
    fd = os.open('mode.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    try:
        info = _file(fd, uid=os.geteuid(), dev=os.fstat(parent).st_dev)
        _require(0 < info.st_size <= 4096)
        raw = os.read(fd, 4097)
        _require(len(raw) == info.st_size)
        value = json.loads(raw, object_pairs_hook=_unique)
        _require(isinstance(value, dict) and set(value) == {
            'schemaVersion', 'mode', 'state', 'dev', 'ino', 'profileSha256', 'uid'})
        _require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
                 and value['mode'] == 'persistent' and value['state'] in {'sealed', 'leased'}
                 and all(type(value[k]) is int and value[k] >= 0 for k in ('dev', 'ino', 'uid'))
                 and isinstance(value['profileSha256'], str) and len(value['profileSha256']) == 64
                 and all(c in '0123456789abcdef' for c in value['profileSha256']))
        return value
    finally:
        os.close(fd)


@_safe
def storage_status(state_dir: Path) -> dict:
    """Read opt-in metadata only; leased mode stays enabled. No writer lock/ZIP."""
    with _directory(state_dir) as state:
        try:
            fd = os.open('persistent', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=state)
        except FileNotFoundError:
            return {'mode': 'snapshot', 'backup': 'legacy'}
        try:
            _private(state)
            _private(fd)
            try:
                _read(fd)
            except FileNotFoundError:
                return {'mode': 'snapshot', 'backup': 'legacy'}
            return {'mode': 'persistent', 'backup': 'disabled'}
        finally:
            os.close(fd)


@_safe
def storage_metadata(state_dir: Path) -> dict:
    """Root read-only marker binding, including opaque profileSha256; no lock."""
    with _directory(state_dir) as state:
        _private(state)
        fd = os.open('persistent', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=state)
        try:
            _private(fd)
            return _read(fd)
        finally:
            os.close(fd)


def profile_fingerprint(state_dir: Path) -> str:
    """Opaque full-profile binding from validated root metadata; no writer lock."""
    return storage_metadata(state_dir)['profileSha256']


def enabled(state_dir: Path) -> bool:
    return storage_status(state_dir)['mode'] == 'persistent'


def _inventory(fd, uid, *, mutate=None, copy_to=None, volatile=False):
    """Bounded full-tree no-follow traversal. Only stopped trees may mutate."""
    device = os.fstat(fd).st_dev
    count = total = 0

    def visit(directory, relative='', depth=0, target=None):
        nonlocal count, total
        info = os.fstat(directory)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and info.st_dev == device
                 and stat.S_IMODE(info.st_mode) == 0o700)
        _require(depth <= 24, 'persistent_limits')
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                _require(count <= MAX_ENTRIES, 'persistent_limits')
                name = entry.name
                path = relative + '/' + name
                _require(len(path.encode()) <= 2048 and name not in {'.', '..'})
                try:
                    before = entry.stat(follow_symlinks=False)
                    _require(stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode))
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                    if stat.S_ISDIR(before.st_mode): flags |= os.O_DIRECTORY
                    child = os.open(name, flags, dir_fd=directory)
                except FileNotFoundError:
                    if volatile: continue
                    raise
                try:
                    after = os.fstat(child)
                    if volatile and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                        continue
                    _require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino))
                    if stat.S_ISDIR(after.st_mode):
                        dst = None
                        if target is not None:
                            os.mkdir(name, 0o700, dir_fd=target)
                            dst = os.open(name, flags | os.O_DIRECTORY, dir_fd=target)
                        try:
                            visit(child, path, depth + 1, dst)
                        finally:
                            if dst is not None: os.close(dst)
                    else:
                        # Unlinked open cache/WAL files are normal live churn.
                        # Account their checked size conservatively; keep strict
                        # ownership/mode/device and reject multiple hardlinks.
                        info = _file_info(after, uid=uid, dev=device, volatile=volatile)
                        total += info.st_size
                        _require(info.st_size <= MAX_FILE_BYTES and total <= MAX_BYTES, 'persistent_limits')
                        if target is not None:
                            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                             0o600, dir_fd=target)
                            try:
                                copied = 0
                                while chunk := os.read(child, 65536):
                                    copied += len(chunk)
                                    _require(copied <= info.st_size, 'persistent_binding')
                                    view = memoryview(chunk)
                                    while view:
                                        n = os.write(output, view)
                                        _require(n > 0)
                                        view = view[n:]
                                end = os.fstat(child)
                                _require(copied == info.st_size and (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                                         == (end.st_size, end.st_mtime_ns, end.st_ctime_ns), 'persistent_binding')
                                os.fsync(output)
                            finally:
                                os.close(output)
                        if mutate is not None:
                            os.fchown(child, mutate, mutate)
                            os.fsync(child)
                finally:
                    os.close(child)
        if target is not None: os.fsync(target)
        if mutate is not None:
            os.fchown(directory, mutate, mutate)
            os.fsync(directory)
    visit(fd, target=copy_to)
    return {'entries': count, 'bytes': total}


@_safe
def storage_headroom(data_fd: int, uid: int) -> int:
    """Read-only online media budget from an already pinned Native directory.

    No writer lock, ownership mutation or private paths. The caller must reserve
    media overhead from this value and pause new media on errors. Definitive
    unsafe objects raise persistent_unsafe; uncertain IO raises persistent_io.
    """
    usage = _inventory(data_fd, uid, volatile=True)
    info = os.fstatvfs(data_fd)
    return max(0, min(MAX_BYTES - usage['bytes'], info.f_bavail * info.f_frsize - MIN_FREE))


def _profile(fd, uid):
    digest = hashlib.sha256(b'wechat-persistent-profile-v1')
    for name in KEYS:
        key = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        try:
            info = _file(key, uid=uid, dev=os.fstat(fd).st_dev)
            _require(0 < info.st_size <= 8192, 'persistent_profile')
            raw = os.read(key, 8193)
            _require(len(raw) == info.st_size, 'persistent_profile')
            digest.update(name.encode() + b'\0' + hashlib.sha256(raw).digest())
        finally:
            os.close(key)
    registry = os.open('registry.sqlite', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        _require(_file(registry, uid=uid).st_size > 0, 'persistent_profile')
    finally:
        os.close(registry)
    accounts = os.open('accounts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        with os.scandir(accounts) as entries:
            names = []
            for entry in entries:
                names.append(entry.name)
                _require(len(names) <= 1, 'persistent_profile')
        _require(len(names) == 1, 'persistent_profile')
        digest.update(names[0].encode())
    finally:
        os.close(accounts)
    return digest.hexdigest()


def _validate_candidate(fd, admission):
    """Recover only the private candidate; validate keys and every pending row."""
    module = _admission_module()
    hashes = {}
    for name in KEYS:
        key = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = _file(key, uid=os.geteuid())
            _require(0 < info.st_size <= 8192, 'persistent_profile')
            raw = os.read(key, 8193)
            _require(len(raw) == info.st_size, 'persistent_profile')
            hashes[name] = hashlib.sha256(raw).hexdigest()
        finally:
            os.close(key)
    profile = module.profile_sha256(hashes)
    if admission is None:
        admission = dict(schemaVersion=1, profileSha256=profile, ledgerRevision=0, entries=[])
    module.validate_admission(admission)
    _require(admission['profileSha256'] == profile, 'persistent_profile')
    accounts = os.open('accounts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        names = os.listdir(accounts)  # Full bounded inventory was already checked.
        _require(len(names) == 1, 'persistent_profile')
        account = names[0]
        path = Path('/proc/self/fd') / str(fd) / 'accounts' / account / 'agent/control.sqlite'
        # Do not let SQLite create an absent profile DB or ignore its WAL.
        control = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            _file(control, uid=os.geteuid(), dev=os.fstat(fd).st_dev)
        finally:
            os.close(control)
        db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.2)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA schema_version').fetchone()
            db.execute('PRAGMA query_only=ON')
            _require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'persistent_profile')
            module.validate_database(db, account, admission)
        finally:
            db.close()
    finally:
        os.close(accounts)


class PersistentStore:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir) / 'persistent'
        self.path = self.root / 'data'
        self.parent_fd = self.data_fd = self.lock_fd = None
        self.metadata = None

    def _open(self, create=False):
        _require(self.parent_fd is None)
        with _directory(self.root.parent) as state:
            _private(state)
            if create:
                try: os.mkdir('persistent', 0o700, dir_fd=state)
                except FileExistsError: pass
                os.fsync(state)
            self.parent_fd = os.open('persistent', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=state)
        _private(self.parent_fd)
        self.lock_fd = os.open('writer.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                               0o600, dir_fd=self.parent_fd)
        _file(self.lock_fd, uid=os.geteuid(), dev=os.fstat(self.parent_fd).st_dev)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PersistentError('persistent_busy') from None

    def _publish(self, value):
        fd = os.open('.mode.next', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.parent_fd)
        try:
            raw = json.dumps(value, separators=(',', ':')).encode()
            view = memoryview(raw)
            while view:
                n = os.write(fd, view)
                _require(n > 0)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace('.mode.next', 'mode.json', src_dir_fd=self.parent_fd, dst_dir_fd=self.parent_fd)
        os.fsync(self.parent_fd)
        self.metadata = value

    def _headroom(self, extra=0):
        info = os.fstatvfs(self.parent_fd)
        _require(info.f_bavail * info.f_frsize >= MIN_FREE + extra, 'persistent_headroom')

    @_safe
    def initialize_from_stopped(self, source: Path, *, expected_uid: int, stopped, restore_admission=None):
        """Explicit migration. Callback must prove exact source writer retired.

        Caller owns source quiescence across this call. Full files/sidecars are
        copied; source is untouched. Failed candidates remain private for review.
        Marker publishes only after durable copy, inventory and profile binding.
        """
        try:
            self._open(create=True)
            try:
                _read(self.parent_fd)
            except FileNotFoundError:
                pass
            else:
                raise PersistentError('persistent_requires_review')
            _require(stopped() is True, 'persistent_binding')
            with _directory(source) as source_fd:
                before = os.fstat(source_fd)
                inventory = _inventory(source_fd, expected_uid)
                profile = _profile(source_fd, expected_uid)
                self._headroom(inventory['bytes'])
                os.mkdir('data', 0o700, dir_fd=self.parent_fd)
                self.data_fd = os.open('data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.parent_fd)
                _inventory(source_fd, expected_uid, copy_to=self.data_fd)
                _require(stopped() is True and (before.st_dev, before.st_ino) ==
                         (os.fstat(source_fd).st_dev, os.fstat(source_fd).st_ino), 'persistent_binding')
                _inventory(self.data_fd, os.geteuid())
                _require(_profile(self.data_fd, os.geteuid()) == profile, 'persistent_profile')
                _validate_candidate(self.data_fd, restore_admission)
                _inventory(self.data_fd, os.geteuid())
                if restore_admission is not None:
                    _admission_module().validate_admission(restore_admission)
                    raw = json.dumps(restore_admission, sort_keys=True, separators=(',', ':')).encode()
                    admission_fd = os.open('admission.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                           0o600, dir_fd=self.parent_fd)
                    try:
                        view = memoryview(raw)
                        while view:
                            n = os.write(admission_fd, view)
                            _require(n > 0)
                            view = view[n:]
                        os.fsync(admission_fd)
                    finally:
                        os.close(admission_fd)
                info = os.fstat(self.data_fd)
                self._publish(dict(schemaVersion=1, mode='persistent', state='sealed',
                                   dev=info.st_dev, ino=info.st_ino, uid=os.geteuid(), profileSha256=profile))
        finally:
            self.close()
        return self

    @_safe
    def acquire(self):
        try:
            self._open()
            self.metadata = _read(self.parent_fd)
            _require(self.metadata['state'] == 'sealed' and self.metadata['uid'] == os.geteuid(),
                     'persistent_requires_review')
            self.data_fd = os.open('data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.parent_fd)
            info = os.fstat(self.data_fd)
            _require((info.st_dev, info.st_ino) == self.identity, 'persistent_binding')
            self.validate_capacity()
            _require(_profile(self.data_fd, os.geteuid()) == self.metadata['profileSha256'], 'persistent_profile')
            return self
        except BaseException:
            self.close()
            raise

    @_safe
    def restore_admission(self):
        """Immutable migration admission. Every guest still validates current DB."""
        try:
            fd = os.open('admission.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=self.parent_fd)
        except FileNotFoundError:
            return None
        try:
            info = _file(fd, uid=os.geteuid(), dev=os.fstat(self.parent_fd).st_dev)
            _require(0 < info.st_size <= 65536)
            raw = os.read(fd, 65537)
            _require(len(raw) == info.st_size)
            return _admission_module().validate_admission(json.loads(raw, object_pairs_hook=_unique))
        finally:
            os.close(fd)

    @property
    def identity(self):
        return self.metadata['dev'], self.metadata['ino']

    @_safe
    def validate_capacity(self, *, live=False):
        _require(self.data_fd is not None)
        result = _inventory(self.data_fd, self.metadata['uid'], volatile=live)
        self._headroom()
        return result

    @_safe
    def handoff(self, uid: int, *, source_identity, process_alive):
        _require(type(uid) is int and uid > 0 and tuple(source_identity) == self.identity
                 and process_alive() is True, 'persistent_binding')
        _require(self.metadata['state'] == 'sealed', 'persistent_requires_review')
        self.validate_capacity()
        # Publish leased first: any interruption is fail-closed, never a second writer.
        self._publish({**self.metadata, 'state': 'leased', 'uid': uid})
        _inventory(self.data_fd, os.geteuid(), mutate=uid)
        _require(process_alive() is True, 'persistent_binding')
        os.fsync(self.data_fd)

    @_safe
    def seal(self, *, stopped):
        _require(stopped() is True, 'persistent_binding')
        _inventory(self.data_fd, self.metadata['uid'], mutate=os.geteuid())
        _require(stopped() is True, 'persistent_binding')
        _require(_profile(self.data_fd, os.geteuid()) == self.metadata['profileSha256'], 'persistent_profile')
        os.fsync(self.data_fd)
        self._publish({**self.metadata, 'state': 'sealed', 'uid': os.geteuid()})

    def close(self):
        for name in ('data_fd', 'lock_fd', 'parent_fd'):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def __enter__(self): return self.acquire()
    def __exit__(self, *_): self.close()
