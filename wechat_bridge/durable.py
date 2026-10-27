"""Bounded private pending-message spool. Payloads are deleted after handoff.

WebSocket has no application acknowledgement: interrupted writes are retained
as uncertain and are never silently replayed into an LLM.
"""
import hashlib
import json
import time

from .state import State, StateError


class Spool(State):
    LIMIT = 32
    BYTES = 2 * 1024 * 1024

    def __init__(self, path):
        super().__init__(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS inbox(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT UNIQUE NOT NULL,
                digest TEXT NOT NULL, payload TEXT, state TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints(name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS message_routes(id INTEGER PRIMARY KEY, destination TEXT NOT NULL);
        ''')
        # A process could have died after writing a frame but before recording
        # its completion. Automatic replay would risk a second AI invocation.
        with self.db:
            self.db.execute("UPDATE inbox SET state='uncertain' WHERE state='publishing'")

    def admit(self, source, data):
        raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        identity_payload = {key: value for key, value in data.items() if key not in {'sender_nickname', 'sender_card'}}
        digest = hashlib.sha256(json.dumps(identity_payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        row = self.db.execute('SELECT digest FROM inbox WHERE source=?', (source,)).fetchone()
        if row:
            if row[0] != digest:
                raise StateError('source_changed')
            return False
        count, size = self.db.execute(
            'SELECT count(*),coalesce(sum(length(cast(payload AS blob))),0) FROM inbox WHERE payload IS NOT NULL').fetchone()
        if count >= self.LIMIT or size + len(raw.encode()) > self.BYTES:
            raise StateError('inbox_full')
        with self.db:
            self.db.execute('INSERT INTO inbox(source,digest,payload,state,created) VALUES(?,?,?,?,?)',
                            (source, digest, raw, 'queued', int(time.time())))
            # Only completed metadata is pruned; pending content is never evicted.
            self.db.execute("DELETE FROM inbox WHERE seq IN (SELECT seq FROM inbox WHERE state='forwarded' ORDER BY seq DESC LIMIT -1 OFFSET 4096)")
        return True

    def next_pending(self):
        row = self.db.execute("SELECT source,payload FROM inbox WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def expire(self, source):
        with self.db:
            result = self.db.execute("UPDATE inbox SET state='expired',payload=NULL WHERE source=? AND state='queued'", (source,))
            if result.rowcount != 1:
                raise StateError('inbox_transition')

    def publishing(self, source):
        with self.db:
            result = self.db.execute("UPDATE inbox SET state='publishing' WHERE source=? AND state='queued'", (source,))
            if result.rowcount != 1:
                raise StateError('inbox_transition')

    def handoff(self, source, success):
        with self.db:
            if success:
                self.db.execute("UPDATE inbox SET state='forwarded',payload=NULL WHERE source=? AND state='publishing'", (source,))
            else:
                self.db.execute("UPDATE inbox SET state='uncertain' WHERE source=? AND state='publishing'", (source,))

    def counts(self):
        return dict(self.db.execute('SELECT state,count(*) FROM inbox GROUP BY state'))

    def cursor(self, name, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', (name, value))
        row = self.db.execute('SELECT value FROM checkpoints WHERE name=?', (name,)).fetchone()
        return row[0] if row else None

    def bind_route(self, identity, destination):
        with self.db:
            self.db.execute('INSERT INTO message_routes VALUES(?,?) ON CONFLICT(id) DO NOTHING', (identity, destination))
            self.db.execute('DELETE FROM message_routes WHERE id IN (SELECT id FROM message_routes ORDER BY id DESC LIMIT -1 OFFSET 4096)')

    def message_route(self, identity):
        row = self.db.execute('SELECT destination FROM message_routes WHERE id=?', (identity,)).fetchone()
        return row[0] if row else None
