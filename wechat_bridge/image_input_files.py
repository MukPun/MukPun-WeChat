"""Private, bounded, short-lived original image staging for AstrBot."""
import base64
import fcntl
import hashlib
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager

from . import image_input as images

MAX_ENTRIES = 256
MAX_TOTAL_BYTES = 512 * 1024 * 1024
TTL = 600
_MANAGED = re.compile(r'([0-9a-f]{64})\.(png|jpg|gif|webp)\Z')
_TEMP = re.compile(r'\.hd-input-[0-9a-f]{32}\.tmp\Z')


def _unsafe():
    raise images.ImageError('image_integrity')


class ImageFiles:
    def __init__(self, directory, *, clock=time.time):
        self.directory = os.fspath(directory)
        self.clock = clock
        with self._directory():
            pass

    @contextmanager
    def _directory(self):
        try:
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            _unsafe()
        try:
            info = os.fstat(fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                _unsafe()
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(fd)

    def _verify(self, directory, name, *, verify_digest=False):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError:
            _unsafe()
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or not 0 <= info.st_size <= images.MAX_IMAGE):
                _unsafe()
            if verify_digest:
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    digest.update(chunk)
                if digest.hexdigest() != _MANAGED.fullmatch(name).group(1):
                    _unsafe()
            return info
        finally:
            os.close(fd)

    def _inventory(self, directory, now):
        entries = os.listdir(directory)
        total = 0
        live = {}
        for name in entries:
            managed = _MANAGED.fullmatch(name)
            temporary = _TEMP.fullmatch(name)
            if not (managed or temporary):
                continue
            info = self._verify(directory, name)
            if now - info.st_mtime >= TTL:
                os.unlink(name, dir_fd=directory)
                continue
            live[name] = info
            total += info.st_size
        # Unrelated files consume entry capacity but are never deleted.
        count = len(entries) - sum(1 for name in entries
            if (_MANAGED.fullmatch(name) or _TEMP.fullmatch(name)) and name not in live)
        return live, count, total

    def cleanup(self):
        with self._directory() as directory:
            self._inventory(directory, self.clock())

    def stage(self, image):
        # Incoming file descriptors are deliberately rejected by default validation.
        images.validate(image)
        meta = images.metadata(image)
        with self._directory() as directory:
            now = self.clock()
            live, count, total = self._inventory(directory, now)
            if count > MAX_ENTRIES or total > MAX_TOTAL_BYTES:
                raise images.ImageError('image_capacity')
            if image['bytes'] <= images.INLINE_BYTES:
                return image
            raw = base64.b64decode(image['base64'], validate=True)
            name = meta['sha256'] + '.' + images.EXTENSIONS[meta['mime']]
            if name in live:
                verified = self._verify(directory, name, verify_digest=True)
                if verified.st_size != len(raw):
                    _unsafe()
                os.utime(name, (now, now), dir_fd=directory, follow_symlinks=False)
            else:
                if count + 1 > MAX_ENTRIES or total + len(raw) > MAX_TOTAL_BYTES:
                    raise images.ImageError('image_capacity')
                temporary = '.hd-input-' + uuid.uuid4().hex + '.tmp'
                fd = None
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
                    os.fchmod(fd, 0o600)
                    view = memoryview(raw)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError('short image write')
                        view = view[written:]
                    os.utime(fd, (now, now))
                    os.fsync(fd)
                    os.close(fd)
                    fd = None
                    os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    if fd is not None:
                        os.close(fd)
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            return dict(meta, file=images.export_uri(meta))
