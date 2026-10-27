"""Private, bounded identity and submission journal; no message bodies."""
import json
import os
from pathlib import Path
import sqlite3
import stat


class StateError(Exception):
    pass


class State:
    KINDS = {'bot', 'group', 'user', 'incoming', 'outgoing'}

    def __init__(self, path):
        self.path = Path(path).absolute()
        if not self.path.parent.is_dir() or any(part.is_symlink() for part in (self.path, *self.path.parents)):
            raise StateError('state_path')
        if os.name == 'posix':
            parent = self.path.parent.stat()
            if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                raise StateError('state_permissions')
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                os.name == 'posix' and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600)):
            raise StateError('state_permissions')
        self.db = sqlite3.connect(self.path, timeout=.2)
        self.db.execute('PRAGMA trusted_schema=OFF')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA max_page_count=2048')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS identities(
                id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id BETWEEN 1 AND 2147483647),
                kind TEXT NOT NULL, raw TEXT NOT NULL, UNIQUE(kind,raw));
            CREATE TABLE IF NOT EXISTS submissions(
                key TEXT PRIMARY KEY, digest TEXT NOT NULL, native_key TEXT NOT NULL,
                result TEXT);
        ''')

    def identity(self, kind, raw):
        if kind not in self.KINDS or not isinstance(raw, str) or not 0 < len(raw.encode('utf-8')) <= 512:
            raise StateError('identity_invalid')
        row = self.db.execute('SELECT id FROM identities WHERE kind=? AND raw=?', (kind, raw)).fetchone()
        if row:
            return row[0], False
        with self.db:
            if self.db.execute('SELECT COUNT(*) FROM identities').fetchone()[0] >= 8192:
                raise StateError('state_capacity')
            cursor = self.db.execute('INSERT INTO identities(kind,raw) VALUES(?,?)', (kind, raw))
        return cursor.lastrowid, True

    def known(self, kind, number):
        if type(number) is not int:
            return False
        return self.db.execute('SELECT 1 FROM identities WHERE kind=? AND id=?', (kind, number)).fetchone() is not None

    def submission(self, key, digest):
        row = self.db.execute('SELECT digest,result FROM submissions WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise StateError('echo_conflict')
        return json.loads(row[1]) if row[1] is not None else {
            'status': 'failed', 'retcode': 1408, 'data': None, 'message': 'outcome_unknown'}

    def reserve(self, key, digest, native_key):
        with self.db:
            if self.db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] >= 4096:
                raise StateError('state_capacity')
            self.db.execute('INSERT INTO submissions(key,digest,native_key) VALUES(?,?,?)', (key, digest, native_key))

    def finish(self, key, result):
        raw = json.dumps(result, separators=(',', ':'), ensure_ascii=True)
        if len(raw) > 1024:
            raise StateError('result_invalid')
        with self.db:
            self.db.execute('UPDATE submissions SET result=? WHERE key=?', (raw, key))

    def close(self):
        self.db.close()
