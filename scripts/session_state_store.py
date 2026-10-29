"""Bounded, private snapshots of a complete, quiescent SDK data directory.

The caller owns native-process quiescence and the 32 MiB tmpfs mount.  This
module never selects individual credentials, logs names, or opens a network.
Linux is required for snapshot creation; ZIP validation/restoration also has a
portable implementation for offline tests.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import struct
from typing import NamedTuple
import unicodedata
import zipfile


MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1028 * 1024 * 1024
MAX_ENTRIES = 1024
MAX_PATH_BYTES = 2048
MAX_DEPTH = 24
CHUNK = 64 * 1024
SLOTS = ("current.zip", "previous.zip", ".next.zip")
_FD_API = (os.name == "posix" and hasattr(os, "O_NOFOLLOW")
           and os.open in os.supports_dir_fd and os.listdir in os.supports_fd)
_CODES = frozenset({"state_unsafe", "state_limits", "state_archive", "state_io",
                    "state_platform", "state_destination", "state_rotation"})


class StateError(RuntimeError):
    """Only a fixed, non-sensitive error code is exposed to the caller."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else "state_io"
        super().__init__(self.code)


class SnapshotInfo(NamedTuple):
    files: int
    bytes: int
    archive_bytes: int


def _safe_api(function):
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except StateError:
            raise
        except Exception:
            raise StateError("state_io") from None
    call.__name__ = function.__name__
    call.__doc__ = function.__doc__
    return call


def _uid():
    return os.geteuid() if os.name == "posix" else None


def _check_object(info, kind, uid=None, dev=None, mode=None):
    if not kind(info.st_mode):
        raise StateError("state_unsafe")
    if uid is not None and info.st_uid != uid:
        raise StateError("state_unsafe")
    if dev is not None and info.st_dev != dev:
        raise StateError("state_unsafe")
    if mode is not None and os.name == "posix" and stat.S_IMODE(info.st_mode) != mode:
        raise StateError("state_unsafe")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise StateError("state_unsafe")
    # Windows reparse points include junctions as well as symbolic links.
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise StateError("state_unsafe")


def _same_object(before, after):
    if (before.st_dev, before.st_ino, before.st_mode, before.st_uid) != (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid):
        raise StateError("state_unsafe")


@contextmanager
def _directory(path, *, create=False, private=False):
    """Open without following any path component, including ancestor links."""
    path = Path(os.path.abspath(os.fspath(path)))
    if not _FD_API:
        for part in [*reversed(path.parents), path]:
            if part.exists() or part.is_symlink():
                _check_object(part.lstat(), stat.S_ISDIR)
        if create:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        _check_object(path.lstat(), stat.S_ISDIR, uid=_uid(),
                      mode=0o700 if private else None)
        yield path
        return
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            if create and index == len(parts) - 1:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        _check_object(os.fstat(fd), stat.S_ISDIR, uid=_uid() if private else None,
                      mode=0o700 if private else None)
        yield fd
    finally:
        os.close(fd)


def _name(name, directory):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise StateError("state_archive")
    if any(unicodedata.category(char) == "Cc" for char in name):
        raise StateError("state_archive")
    normalized = name[:-1] if directory and name.endswith("/") else name
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise StateError("state_archive")
    try:
        length = len(name.encode("utf-8"))
    except UnicodeError:
        raise StateError("state_archive") from None
    if length > MAX_PATH_BYTES or len(parts) > MAX_DEPTH:
        raise StateError("state_limits")
    return normalized, parts


class _CappedWriter:
    """Limit cumulative writes, including ZIP header seek-and-rewrite traffic."""

    def __init__(self, raw):
        self.raw = raw
        self.written = 0

    def write(self, data):
        if self.written + len(data) > MAX_ARCHIVE_BYTES:
            raise StateError("state_limits")
        self.written += len(data)
        return self.raw.write(data)

    def seek(self, *args):
        return self.raw.seek(*args)

    def tell(self):
        return self.raw.tell()

    def flush(self):
        return self.raw.flush()


def _bounded_names(directory_fd, remaining):
    """Stop enumeration at the object cap, before collecting hostile filenames."""
    names = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if len(names) >= remaining:
                raise StateError("state_limits")
            names.append(entry.name)
    return sorted(names)


def _archive_manifest(archive):
    """Validate every member and CRC before any destination is created."""
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES:
        raise StateError("state_limits")
    seen = {}
    files = total = 0
    for entry in entries:
        directory = entry.is_dir()
        name, parts = _name(entry.orig_filename, directory)
        if entry.filename != entry.orig_filename or name in seen:
            raise StateError("state_archive")
        unix_mode = entry.external_attr >> 16
        kind = stat.S_IFMT(unix_mode)
        if kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG):
            raise StateError("state_archive")
        if entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1:
            raise StateError("state_archive")
        if entry.file_size != entry.compress_size or entry.file_size < 0:
            raise StateError("state_archive")
        if directory and entry.file_size:
            raise StateError("state_archive")
        seen[name] = directory
        if not directory:
            files += 1
            total += entry.file_size
            if total > MAX_SOURCE_BYTES:
                raise StateError("state_limits")
    # Count implicit directories too: a small central directory must not create
    # more than the source limit's number of filesystem objects on restore.
    objects = set(seen)
    for name in seen:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in seen and not seen[parent]:
                raise StateError("state_archive")
            objects.add(parent)
    if len(objects) > MAX_ENTRIES:
        raise StateError("state_limits")
    actual = 0
    for entry in entries:
        with archive.open(entry, "r") as stream:
            count = 0
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                count += len(chunk)
                actual += len(chunk)
                if count > entry.file_size or actual > MAX_SOURCE_BYTES:
                    raise StateError("state_limits")
            if count != entry.file_size:
                raise StateError("state_archive")
    return entries, files, total


@contextmanager
def _checked_zip(raw):
    try:
        _preflight_zip(raw)
        with zipfile.ZipFile(raw, "r", allowZip64=False) as archive:
            yield archive
    except StateError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, EOFError,
            NotImplementedError, ValueError, UnicodeError):
        raise StateError("state_archive") from None


def _preflight_zip(raw):
    """Bound central-directory parsing before ZipFile allocates ZipInfo objects."""
    raw.seek(0, os.SEEK_END)
    length = raw.tell()
    if length > MAX_ARCHIVE_BYTES:
        raise StateError("state_limits")
    raw.seek(max(0, length - 65557))
    tail = raw.read(65557)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        raise StateError("state_archive")
    end = struct.unpack("<4s4H2LH", tail[at:at + 22])
    if (end[1] or end[2] or end[3] != end[4] or
            len(tail) - at != 22 + end[7] or
            end[5] == 0xffffffff or end[6] == 0xffffffff):
        raise StateError("state_archive")
    if end[4] > MAX_ENTRIES:
        raise StateError("state_limits")
    start = length - len(tail) + at
    if end[6] + end[5] != start:
        raise StateError("state_archive")
    raw.seek(end[6])
    count = 0
    while raw.tell() < start:
        header = raw.read(46)
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            raise StateError("state_archive")
        name_length, extra_length, comment_length = struct.unpack_from("<3H", header, 28)
        if name_length > MAX_PATH_BYTES:
            raise StateError("state_limits")
        raw.seek(name_length + extra_length + comment_length, os.SEEK_CUR)
        count += 1
        if count > MAX_ENTRIES:
            raise StateError("state_limits")
        if raw.tell() > start:
            raise StateError("state_archive")
    if count != end[4]:
        raise StateError("state_archive")
    raw.seek(0)


class SnapshotStore:
    """A dedicated 0700 root with three bounded 0600 slots in private/."""

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _private(self, create=False):
        with _directory(self.path, create=create, private=True) as root:
            if _FD_API:
                if create:
                    try:
                        os.mkdir("private", 0o700, dir_fd=root)
                    except FileExistsError:
                        pass
                fd = os.open("private", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=root)
                try:
                    _check_object(os.fstat(fd), stat.S_ISDIR, uid=_uid(), mode=0o700,
                                  dev=os.fstat(root).st_dev)
                    if set(os.listdir(fd)) - set(SLOTS):
                        raise StateError("state_unsafe")
                    yield fd
                finally:
                    os.close(fd)
            else:
                with _directory(root / "private", create=create, private=True) as private:
                    if set(os.listdir(private)) - set(SLOTS):
                        raise StateError("state_unsafe")
                    yield private

    def _slot(self, private, name):
        try:
            info = (os.stat(name, dir_fd=private, follow_symlinks=False) if _FD_API
                    else (private / name).lstat())
        except FileNotFoundError:
            return None
        dev = os.fstat(private).st_dev if _FD_API else private.stat().st_dev
        _check_object(info, stat.S_ISREG, uid=_uid(), dev=dev, mode=0o600)
        if info.st_size > MAX_ARCHIVE_BYTES:
            raise StateError("state_limits")
        return info

    @_safe_api
    def prepare(self):
        with self._private(create=True) as private:
            for name in SLOTS:
                self._slot(private, name)
        return self

    @_safe_api
    def latest(self):
        with self._private() as private:
            # Validate all fixed slots, including stale staging files.
            found = {name: self._slot(private, name) for name in SLOTS}
            for name in SLOTS[:2]:
                if found[name] is not None:
                    return self.path / "private" / name
        return None

    @_safe_api
    def commit(self, source_fd, *, expected_uid, expected_dev):
        if not _FD_API:
            raise StateError("state_platform")
        import fcntl
        with self._private() as private:
            fcntl.flock(private, fcntl.LOCK_EX)
            for name in SLOTS:
                self._slot(private, name)
            try:
                os.unlink(".next.zip", dir_fd=private)
            except FileNotFoundError:
                pass
            fd = os.open(".next.zip", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=private)
            try:
                with os.fdopen(fd, "w+b", buffering=0) as raw:
                    fd = None
                    writer = _CappedWriter(raw)
                    files, total = self._write(source_fd, writer, expected_uid, expected_dev)
                    raw.flush()
                    os.fsync(raw.fileno())
                    size = os.fstat(raw.fileno()).st_size
                    _check_object(os.fstat(raw.fileno()), stat.S_ISREG, uid=_uid(),
                                  dev=os.fstat(private).st_dev, mode=0o600)
                    raw.seek(0)
                    with _checked_zip(raw) as archive:
                        _, checked_files, checked_total = _archive_manifest(archive)
                    if (files, total) != (checked_files, checked_total):
                        raise StateError("state_archive")
                current = self._slot(private, "current.zip")
                self._slot(private, "previous.zip")
                moved = False
                try:
                    if current is not None:
                        os.replace("current.zip", "previous.zip", src_dir_fd=private,
                                   dst_dir_fd=private)
                        moved = True
                    os.replace(".next.zip", "current.zip", src_dir_fd=private,
                               dst_dir_fd=private)
                except OSError:
                    if moved:
                        try:
                            os.replace("previous.zip", "current.zip", src_dir_fd=private,
                                       dst_dir_fd=private)
                        except OSError:
                            pass  # previous.zip remains the last valid snapshot.
                    raise StateError("state_rotation") from None
                os.fsync(private)
                return SnapshotInfo(files, total, size)
            finally:
                if fd is not None:
                    os.close(fd)
                try:
                    os.unlink(".next.zip", dir_fd=private)
                except FileNotFoundError:
                    pass

    def _write(self, source_fd, writer, uid, dev):
        _check_object(os.fstat(source_fd), stat.S_ISDIR, uid=uid, dev=dev)
        counts = [0, 0, 0]  # entries, files, bytes
        visited = set()

        def walk(parent_fd, prefix):
            parent = os.fstat(parent_fd)
            _check_object(parent, stat.S_ISDIR, uid=uid, dev=dev)
            identity = (parent.st_dev, parent.st_ino)
            if identity in visited:
                raise StateError("state_unsafe")
            visited.add(identity)
            for name in _bounded_names(parent_fd, MAX_ENTRIES - counts[0]):
                before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                directory = stat.S_ISDIR(before.st_mode)
                _check_object(before, stat.S_ISDIR if directory else stat.S_ISREG,
                              uid=uid, dev=dev)
                relative = prefix + name + ("/" if directory else "")
                _name(relative, directory)
                counts[0] += 1
                if counts[0] > MAX_ENTRIES:
                    raise StateError("state_limits")
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if directory:
                    flags |= os.O_DIRECTORY
                child = os.open(name, flags, dir_fd=parent_fd)
                try:
                    opened = os.fstat(child)
                    _same_object(before, opened)
                    _check_object(opened, stat.S_ISDIR if directory else stat.S_ISREG,
                                  uid=uid, dev=dev)
                    entry = zipfile.ZipInfo(relative)
                    entry.create_system = 3
                    entry.compress_type = zipfile.ZIP_STORED
                    entry.external_attr = ((stat.S_IFDIR | 0o700) if directory
                                           else (stat.S_IFREG | 0o600)) << 16
                    if directory:
                        parent_check = os.open("..", os.O_RDONLY | os.O_DIRECTORY |
                                               os.O_NOFOLLOW, dir_fd=child)
                        try:
                            ancestor = os.fstat(parent_check)
                            if (ancestor.st_dev, ancestor.st_ino) != identity:
                                raise StateError("state_unsafe")
                        finally:
                            os.close(parent_check)
                        archive.writestr(entry, b"")
                        walk(child, relative)
                    else:
                        if opened.st_size + counts[2] > MAX_SOURCE_BYTES:
                            raise StateError("state_limits")
                        entry.file_size = opened.st_size
                        size = 0
                        with archive.open(entry, "w") as output:
                            while True:
                                chunk = os.read(child, CHUNK)
                                if not chunk:
                                    break
                                size += len(chunk)
                                counts[2] += len(chunk)
                                if size > opened.st_size or counts[2] > MAX_SOURCE_BYTES:
                                    raise StateError("state_limits")
                                output.write(chunk)
                        after = os.fstat(child)
                        _same_object(opened, after)
                        _check_object(after, stat.S_ISREG, uid=uid, dev=dev)
                        if (size != opened.st_size or opened.st_size != after.st_size or
                                opened.st_mtime_ns != after.st_mtime_ns or
                                opened.st_ctime_ns != after.st_ctime_ns):
                            raise StateError("state_unsafe")
                        counts[1] += 1
                finally:
                    os.close(child)
            after = os.fstat(parent_fd)
            if (parent.st_mtime_ns, parent.st_ctime_ns) != (
                    after.st_mtime_ns, after.st_ctime_ns):
                raise StateError("state_unsafe")

        with zipfile.ZipFile(writer, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=False) as archive:
            walk(source_fd, "")
        return counts[1], counts[2]


def _restore_member(root, entry, archive):
    directory = entry.is_dir()
    _, parts = _name(entry.orig_filename, directory)
    if _FD_API:
        parent = os.dup(root)
        try:
            for component in parts[:-1] if not directory else parts:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=parent)
                _check_object(os.fstat(child), stat.S_ISDIR, uid=_uid(), mode=0o700,
                              dev=os.fstat(root).st_dev)
                os.close(parent)
                parent = child
            if not directory:
                fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
                with os.fdopen(fd, "wb", buffering=0) as output:
                    _copy_member(archive, entry, output)
        finally:
            os.close(parent)
    else:
        parent = root
        for component in parts[:-1] if not directory else parts:
            parent = parent / component
            try:
                parent.mkdir(mode=0o700)
            except FileExistsError:
                pass
            _check_object(parent.lstat(), stat.S_ISDIR)
        if not directory:
            fd = os.open(parent / parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600)
            with os.fdopen(fd, "wb", buffering=0) as output:
                _copy_member(archive, entry, output)


def _copy_member(archive, entry, output):
    count = 0
    with archive.open(entry, "r") as stream:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            count += len(chunk)
            if count > entry.file_size or count > MAX_SOURCE_BYTES:
                raise StateError("state_limits")
            remaining = memoryview(chunk)
            while remaining:
                written = output.write(remaining)
                if written is None or written <= 0:
                    raise StateError("state_io")
                remaining = remaining[written:]
    if count != entry.file_size:
        raise StateError("state_archive")


@_safe_api
def restore_archive(archive_path, destination_path, *, archive_owner_uid=None,
                    archive_mode=0o600):
    """Validate all bytes, then restore into an empty private directory.

    A sandbox guest may explicitly select its current uid and mode 0400 for its
    read-only bind-mounted input; the caller must verify that mount is read-only.
    Host snapshot slots always retain the current-euid/0600 policy.
    """
    archive_path = Path(archive_path)
    if archive_mode not in (0o600, 0o400):
        raise StateError("state_unsafe")
    if archive_mode == 0o400 and archive_owner_uid != _uid():
        raise StateError("state_unsafe")
    owner = _uid() if archive_owner_uid is None else archive_owner_uid
    before = archive_path.lstat()
    _check_object(before, stat.S_ISREG, uid=owner, mode=archive_mode)
    if before.st_size > MAX_ARCHIVE_BYTES:
        raise StateError("state_limits")
    if _FD_API:
        with _directory(archive_path.parent) as parent:
            fd = os.open(archive_path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    else:
        fd = os.open(archive_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb", buffering=0) as raw:
        opened = os.fstat(raw.fileno())
        _same_object(before, opened)
        _check_object(opened, stat.S_ISREG, uid=owner, mode=archive_mode)
        if before.st_size != opened.st_size:
            raise StateError("state_unsafe")
        with _checked_zip(raw) as archive:
            entries, files, total = _archive_manifest(archive)
            after = os.fstat(raw.fileno())
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise StateError("state_unsafe")
            with _directory(destination_path, create=True, private=True) as destination:
                if os.listdir(destination):
                    raise StateError("state_destination")
                for entry in entries:
                    _restore_member(destination, entry, archive)
    return SnapshotInfo(files, total, before.st_size)
