"""Bounded private pending-message spool. Payloads are deleted after handoff.

WebSocket has no application acknowledgement: interrupted writes are retained
as uncertain and are never silently replayed into an LLM.
"""
from contextlib import contextmanager
import hashlib
import json
import time
import re

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
            CREATE TABLE IF NOT EXISTS recovery_window(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                floor_seq INTEGER, epoch TEXT, lost_through INTEGER NOT NULL);
            INSERT OR IGNORE INTO recovery_window
                SELECT 1,NULL,NULL,max(
                    coalesce((SELECT max(i.seq-1) FROM inbox i WHERE i.seq>1 AND
                        NOT EXISTS(SELECT 1 FROM inbox p WHERE p.seq=i.seq-1)),0),
                    CASE WHEN coalesce((SELECT seq FROM sqlite_sequence WHERE name='inbox'),0)>
                        coalesce((SELECT max(seq) FROM inbox),0)
                        THEN coalesce((SELECT seq FROM sqlite_sequence WHERE name='inbox'),0) ELSE 0 END);
        ''')
        # A process could have died after writing a frame but before recording
        # its completion. Automatic replay would risk a second AI invocation.
        with self.db:
            self.db.execute("UPDATE inbox SET state='uncertain' WHERE state='publishing'")

    def admit(self, source, data):
        raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        digest = self.source_digest(data)
        with self._recovery_transaction():
            row = self.db.execute('SELECT digest FROM inbox WHERE source=?', (source,)).fetchone()
            if row:
                if row[0] != digest:
                    raise StateError('source_changed')
                return False
            count, size = self.db.execute(
                'SELECT count(*),coalesce(sum(length(cast(payload AS blob))),0) FROM inbox WHERE payload IS NOT NULL').fetchone()
            if count >= self.LIMIT or size + len(raw.encode()) > self.BYTES:
                raise StateError('inbox_full')
            floor = self.db.execute('SELECT floor_seq FROM recovery_window').fetchone()[0]
            if floor is not None:
                # Reserve metadata capacity now, before ACK. Every admitted row
                # could complete before another checkpoint is published.
                protected = self.db.execute('SELECT count(*) FROM inbox WHERE seq>=?', (floor,)).fetchone()[0]
                if protected >= 4096:
                    raise StateError('recovery_checkpoint_required')
            self.db.execute('INSERT INTO inbox(source,digest,payload,state,created) VALUES(?,?,?,?,?)',
                            (source, digest, raw, 'queued', int(time.time())))
            self._prune_recovery(floor)
        return True

    @staticmethod
    def source_digest(data):
        identity_payload = {key: value for key, value in data.items() if key not in {'sender_nickname', 'sender_card'}}
        return hashlib.sha256(json.dumps(identity_payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()

    def admit_checkpoint(self, source, data, name, value):
        """Commit input and its receive cursor together before acknowledging it."""
        with self._recovery_transaction():
            inserted = self.admit(source, data)
            self.cursor(name, value)
        return inserted

    @contextmanager
    def _recovery_transaction(self):
        own = not self.db.in_transaction
        if own:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            if own:
                self.db.commit()
        except Exception:
            if own:
                self.db.rollback()
            raise

    def _prune_recovery(self, floor):
        if floor is None:
            condition = "state='forwarded'"
            params = ()
        else:
            # Only payload-free rows are reclaimable. An uncertain row written by
            # handoff(False) keeps its payload on purpose, so it is never pruned
            # here; LIMIT keeps that bounded.
            condition = "payload IS NULL AND state IN ('forwarded','expired','uncertain')"
            params = (floor,)
        # Record every eviction in the same transaction, including legacy mode.
        query = 'SELECT seq FROM inbox WHERE '+condition+' ORDER BY seq DESC LIMIT -1 OFFSET 4096'
        if floor is not None:
            query = 'SELECT seq FROM inbox WHERE seq<? AND seq IN ('+query+')'
        removed = self.db.execute('SELECT max(seq) FROM ('+query+')', params).fetchone()[0]
        if removed is not None:
            self.db.execute('UPDATE recovery_window SET lost_through=max(lost_through,?)', (removed,))
            self.db.execute('DELETE FROM inbox WHERE seq IN ('+query+')', params)

    def protect_recovery(self, floor_seq, epoch):
        """Protect a replay window; the epoch identifies one profile lineage.

        Activate before capture. Advance only AFTER publishing, to no more than
        the minimum floor of BOTH committed manifests. Publishing failures must
        never advance this value. No implicit reset or epoch migration is allowed.
        """
        if (type(floor_seq) is not int or floor_seq < 1 or not isinstance(epoch, str)
                or re.fullmatch('[0-9a-f]{32}', epoch) is None):
            raise StateError('recovery_coverage')
        with self._recovery_transaction():
            coverage = self.recovery_coverage()
            if (floor_seq < coverage['retainedFloor'] or floor_seq > coverage['highSeq']+1
                    or coverage['epoch'] is not None and
                    (epoch != coverage['epoch'] or floor_seq < coverage['floorSeq'])):
                raise StateError('recovery_coverage')
            if self.db.execute('SELECT count(*) FROM inbox WHERE seq>=?', (floor_seq,)).fetchone()[0] > 4096:
                raise StateError('recovery_checkpoint_required')
            self.db.execute('UPDATE recovery_window SET floor_seq=?,epoch=?', (floor_seq,epoch))
            self._prune_recovery(floor_seq)
        return self.recovery_coverage()

    def recovery_coverage(self):
        """Payload-free durable proof of retained suffix, not a row-count guess."""
        with self._recovery_transaction():
            floor, epoch, lost = self.db.execute('SELECT floor_seq,epoch,lost_through FROM recovery_window').fetchone()
            first = self.db.execute('SELECT min(seq) FROM inbox').fetchone()[0]
            high = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='inbox'").fetchone()
            return dict(floorSeq=floor, retainedFloor=lost+1, firstSeq=first,
                        highSeq=high[0] if high else 0, epoch=epoch)

    def next_pending(self):
        row = self.db.execute("SELECT source,payload FROM inbox WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def expire(self, source):
        with self.db:
            result = self.db.execute("UPDATE inbox SET state='expired',payload=NULL WHERE source=? AND state='queued'", (source,))
            if result.rowcount != 1:
                raise StateError('inbox_transition')
            self._prune_recovery(self.db.execute('SELECT floor_seq FROM recovery_window').fetchone()[0])

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
            self._prune_recovery(self.db.execute('SELECT floor_seq FROM recovery_window').fetchone()[0])

    def counts(self):
        return dict(self.db.execute('SELECT state,count(*) FROM inbox GROUP BY state'))

    def cursor(self, name, value=None):
        if value is not None:
            with self._recovery_transaction():
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
