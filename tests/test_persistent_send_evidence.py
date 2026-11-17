"""Bounded WAL outcome proofs on synthetic persistent Native data."""
import fcntl
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import recovery_dispositions as dispositions, session_runtime
import test_session_persistent_store as store_tests


class PersistentOutcomes(unittest.TestCase):
    def setUp(self):
        helper = store_tests.PersistentStoreTests()
        helper.addCleanup = self.addCleanup
        helper.setUp()
        helper.initialize()
        self.lease = helper.module.PersistentStore(helper.state).acquire()
        self.addCleanup(self.lease.close)
        self.lease.handoff(os.geteuid(), source_identity=self.lease.identity, process_alive=lambda: True)
        self.path = self.lease.path / 'accounts/synthetic-account/agent/control.sqlite'
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('PRAGMA wal_autocheckpoint=0')
        self.state = helper.state
        self.pinned = SimpleNamespace(unchanged=lambda: True, host=SimpleNamespace(
            storage_mode='persistent', data_fd=self.lease.data_fd, client_uid=os.geteuid(),
            client_pid=os.getpid(), client_ticks=123))
        self.owner = SimpleNamespace(STATE=self.state, checkpoint_copy=Mock(side_effect=OSError('backup unavailable')),
            read_private=session_runtime.read_private, read_json=session_runtime.read_json,
            write_json=session_runtime.write_json)
        self.core = SimpleNamespace(owner=self.owner, recovery_dispositions=dispositions)
        self.audit = dict(state='unknown', key='b'*32, requestId='a'*32,
            destinationSha256=hashlib.sha256(b'peer').hexdigest(), textSha256='c'*64, bytes=1)
        self.audit_path = self.state / 'forward-send.json'
        self.row = ('text', self.audit['key'], 'hash', 'peer', b'cipher', 'needs_review', None, None, None, 'time')

    def insert(self, state='needs_review'):
        row = (*self.row[:5], state, *self.row[6:])
        self.writer.execute('INSERT INTO outbox VALUES('+','.join('?'*10)+')', row)
        self.writer.commit()

    def record(self):
        return dispositions.record_native_outcome(self.core, self.pinned, self.audit, 'text',
            'unconfirmed_quarantined', path=self.audit_path)

    def test_wal_outcome_archived_then_exact_admission_without_snapshot(self):
        self.insert()
        marker = self.record()
        self.assertIn('databaseViewSha256', marker)
        self.assertNotIn('snapshotSha256', marker)
        self.assertTrue(dispositions._valid_marker(marker, dispositions._evidence(self.audit)))
        archived = dispositions.pending.evidence(self.core)['entries'][0]['audit']
        self.assertEqual(archived['recoveryDisposition'], marker)
        admission = self.owner.read_json(self.state / 'persistent/admission.json')
        dispositions.admission.validate_database(self.writer, 'synthetic-account', admission)
        self.assertEqual(admission['ledgerRevision'], 1)
        self.assertEqual(self.writer.execute('SELECT * FROM outbox').fetchone(), self.row)
        self.owner.checkpoint_copy.assert_not_called()

    def test_pending_live_settlement_preserves_reused_slot_and_native_binding(self):
        self.insert('queued')
        with self.assertRaises(dispositions.pending.PendingSend) as error:
            self.record()
        dispositions.pending.preserve(self.core, self.pinned, self.audit_path, self.audit, 'text', error.exception.profile)
        self.assertEqual(dispositions.pending.settle_live(self.core, self.pinned), 1)
        self.assertFalse((self.state / 'persistent/admission.json').exists())
        self.owner.write_json(self.audit_path, dict(state='accepted', key='d'*32))
        self.writer.execute("UPDATE outbox SET state='needs_review'"); self.writer.commit()
        self.assertEqual(dispositions.pending.settle_live(self.core, self.pinned), 0)
        self.assertEqual(self.owner.read_json(self.audit_path)['state'], 'accepted')
        self.assertEqual(len(dispositions.pending.evidence(self.core)['entries']), 1)
        self.assertEqual(self.owner.read_json(self.state / 'persistent/admission.json')['ledgerRevision'], 1)
        self.assertFalse(dispositions.pending.read(self.core)['entries'])

    def test_audit_archive_failure_cannot_publish_admission(self):
        self.insert()
        with patch.object(dispositions.pending, 'archive', side_effect=OSError('disk full')):
            with self.assertRaises(ValueError): self.record()
        self.assertFalse((self.state / 'persistent/admission.json').exists())
        self.assertFalse((self.state / 'send-dispositions.json').exists())

    def test_changed_path_or_profile_cannot_authorize_row(self):
        self.insert()
        original = self.pinned.host.data_fd
        self.pinned.host.data_fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(ValueError): self.record()
        finally:
            os.close(self.pinned.host.data_fd); self.pinned.host.data_fd = original
        (self.lease.path / 'storage-key').write_bytes(b'changed')
        with self.assertRaises(ValueError): self.record()
        self.assertFalse((self.state / 'send-dispositions.json').exists())

    def test_live_safe_point_runs_bounded_settlement_without_capture(self):
        from wechat_bridge.adapters.wechat.recovery import RecoveryBackend
        state = SimpleNamespace(live_only=True, armed=True, request=None, voice_relay=SimpleNamespace(pending=False),
            input_relay=SimpleNamespace(transferring=False), image_transfer=None, image_ack=None,
            pinned=self.pinned, recovery_state='ready')
        with patch.object(dispositions.pending, 'settle_live', create=True, return_value=1) as settle:
            backend = RecoveryBackend(self.core, state)
            backend.capture_if_safe(); backend.capture_if_safe()
            settle.assert_called_once_with(self.core, self.pinned)
        self.assertEqual(state.recovery_state, 'ready')


    def test_untracked_unknown_row_cannot_inherit_current_permission(self):
        self.insert()
        other = ('other', 'e'*32, *self.row[2:])
        self.writer.execute('INSERT INTO outbox VALUES('+','.join('?'*10)+')', other)
        self.writer.commit()
        with self.assertRaises(ValueError): self.record()
        self.assertFalse((self.state / 'persistent/admission.json').exists())

    def test_mutated_old_unknown_row_cannot_inherit_ledger_permission(self):
        self.insert()
        self.record()
        path = self.state / 'persistent/admission.json'
        previous = path.read_bytes()
        self.writer.execute("UPDATE outbox SET error_code='changed'")
        self.writer.commit()
        with self.assertRaises(ValueError): self.record()
        self.assertEqual(path.read_bytes(), previous)
        ledger = self.owner.read_json(self.state / 'send-dispositions.json')
        self.assertEqual(ledger['revision'], 1)

    def test_single_transaction_sees_original_wal_during_concurrent_write(self):
        self.insert()
        with dispositions.persistent.database(self.core, self.pinned, [('text', self.audit)]) as (db, metadata, verify):
            self.writer.execute("UPDATE outbox SET error_code='later'")
            self.writer.commit()
            self.assertIsNone(db.execute('SELECT error_code FROM outbox').fetchone()[0])
            self.assertNotEqual(metadata['profileSha256'], dispositions.persistent.storage.storage_metadata(self.state)['profileSha256'])
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM outbox')
            verify()
        self.assertEqual(self.writer.execute('SELECT error_code FROM outbox').fetchone()[0], 'later')

    def test_native_binding_change_rejects_pending_without_removal(self):
        self.insert('queued')
        with self.assertRaises(dispositions.pending.PendingSend) as raised: self.record()
        dispositions.pending.preserve(self.core, self.pinned, self.audit_path, self.audit, 'text', raised.exception.profile)
        self.pinned.host.client_ticks += 1
        with self.assertRaises(ValueError): dispositions.pending.settle_live(self.core, self.pinned)
        self.assertEqual(len(dispositions.pending.read(self.core)['entries']), 1)

    def test_archive_failure_retains_pending_and_no_admission(self):
        self.insert('queued')
        with self.assertRaises(dispositions.pending.PendingSend) as raised: self.record()
        dispositions.pending.preserve(self.core, self.pinned, self.audit_path, self.audit, 'text', raised.exception.profile)
        self.writer.execute("UPDATE outbox SET state='needs_review'"); self.writer.commit()
        with patch.object(dispositions.pending, 'archive', side_effect=OSError('full')):
            with self.assertRaises(ValueError): dispositions.pending.settle_live(self.core, self.pinned)
        self.assertEqual(len(dispositions.pending.read(self.core)['entries']), 1)
        self.assertFalse((self.state / 'persistent/admission.json').exists())

    def test_sql_budget_defers_without_admission(self):
        self.insert()
        with self.assertRaises(dispositions.pending.PendingSend):
            with dispositions.persistent.database(self.core, self.pinned, [('text', self.audit)]) as (db, _, _verify):
                db.execute('WITH RECURSIVE count(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM count WHERE x<100000000) SELECT sum(x) FROM count').fetchone()
        self.assertFalse((self.state / 'persistent/admission.json').exists())

    def test_ledger_lock_contention_is_pending_and_preserves_archive(self):
        self.insert()
        path = self.state / 'send-dispositions.json.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(dispositions.pending.PendingSend): self.record()
        self.assertEqual(len(dispositions.pending.evidence(self.core)['entries']), 1)
        self.assertFalse((self.state / 'persistent/admission.json').exists())

    def test_missing_wal_shared_index_defers_without_creating_it(self):
        self.insert()
        path = self.path.with_name('control.sqlite-shm')
        path.unlink()
        with self.assertRaises(dispositions.pending.PendingSend): self.record()
        self.assertFalse(path.exists())

    def test_unsafe_idle_evidence_keeps_integrity_protection(self):
        from wechat_bridge.adapters.wechat.recovery import RecoveryBackend
        state = SimpleNamespace(live_only=True, armed=True, request=None, voice_relay=SimpleNamespace(pending=False),
            input_relay=SimpleNamespace(transferring=False), image_transfer=None, image_ack=None,
            pinned=self.pinned, recovery_state='ready')
        with patch.object(dispositions.pending, 'settle_live', side_effect=ValueError('integrity')):
            RecoveryBackend(self.core, state).capture_if_safe()
        self.assertEqual(state.recovery_state, 'requires_review')
