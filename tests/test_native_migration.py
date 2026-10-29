"""Synthetic migration gates; no account, Native, network or sends."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("migration_guest_tests",
    Path(__file__).resolve().parents[1] / "scripts/session_runtime_client.py")
guest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guest
SPEC.loader.exec_module(guest)


@unittest.skipUnless(os.name == "posix", "POSIX private restore")
class MigrationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "accounts/wechat-primary/agent/control.sqlite"
        self.database.parent.mkdir(parents=True)
        self.db = sqlite3.connect(self.database)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE outbox(id TEXT,state TEXT);
            CREATE TABLE media_jobs(id TEXT,idempotency_key TEXT,request_hash TEXT,
                kind TEXT,state TEXT,phase TEXT,attempts INTEGER,cancel_requested INTEGER,
                retryable INTEGER,error_code TEXT,data_json TEXT,payload BLOB,
                created_at TEXT,updated_at TEXT);
        """)
        data = {"sourceId": "source-one", "threadId": "room-one",
                "result": {"imagePreparation": {"sourceSha256": "a"*64},
                           "uploadReadbackVerified": True}}
        self.row = ("job-one", "send-key", "b"*64, "send", "needs_review",
                    "submitting", 1, 0, 0, "media_processing_failed",
                    json.dumps(data), b"private-encrypted-payload", "before", "after")
        self.db.execute("INSERT INTO media_jobs VALUES (" + ",".join("?"*14) + ")", self.row)
        self.db.commit()
        self.data_patch = patch.object(guest.empty, "DATA", self.root)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def approval(self):
        return {"schemaVersion": 1,
                "jobSha256": guest.quarantined_send_fingerprint("wechat-primary", self.row)}

    def test_only_explicit_migration_config_accepts_approval(self):
        value = {"schemaVersion": 1, "restore": True, "offline": True,
                 "runtimeSeconds": 90, "account": guest.ACCOUNT,
                 "quarantinedSend": {"schemaVersion": 1, "jobSha256": "a"*64}}
        try:
            actual = guest.validate_config(value)
        except ValueError:
            self.fail("explicit migration restore is still rejected")
        self.assertEqual(actual, value)
        for approval in (None, {}, {"schemaVersion": True, "jobSha256": "a"*64},
                         {"schemaVersion": 1, "jobSha256": "invalid"},
                         {**value["quarantinedSend"], "extra": 1}):
            with self.subTest(approval=approval), self.assertRaises(ValueError):
                guest.validate_config({**value, "quarantinedSend": approval})
        with self.assertRaises(ValueError):
            guest.validate_config({**value, "restore": False})

    def test_normal_restore_still_blocks_and_explicit_restore_preserves_row(self):
        with self.assertRaises(guest.RuntimeFailure):
            guest.review_restored_outbox()
        before = self.database.read_bytes()
        guest.review_restored_outbox(self.approval())
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.db.execute("SELECT * FROM media_jobs").fetchone(), self.row)

    def test_any_changed_binding_or_replayable_state_rejects(self):
        approval = self.approval()
        for column, value in (("id","different"), ("idempotency_key","other"),
            ("payload",b"changed"), ("state","queued"), ("phase","uploading"),
            ("retryable",1), ("attempts",2), ("cancel_requested",1),
            ("data_json","{}"), ("error_code","changed")):
            with self.subTest(column=column):
                with self.db:
                    self.db.execute("UPDATE media_jobs SET " + column + "=?", (value,))
                with self.assertRaises(guest.RuntimeFailure):
                    guest.review_restored_outbox(approval)
                self.db.execute("DELETE FROM media_jobs")
                self.db.execute("INSERT INTO media_jobs VALUES (" + ",".join("?"*14) + ")", self.row)
                self.db.commit()

    def test_even_fresh_fingerprint_cannot_approve_a_replayable_job(self):
        row = list(self.row)
        row[4] = "queued"
        self.db.execute("UPDATE media_jobs SET state='queued'")
        self.db.commit()
        approval = {"schemaVersion": 1,
                    "jobSha256": guest.quarantined_send_fingerprint("wechat-primary", row)}
        with self.assertRaises(guest.RuntimeFailure):
            guest.review_restored_outbox(approval)

    def test_additional_unknown_or_pending_text_is_never_covered(self):
        approval = self.approval()
        other = ("job-two", *self.row[1:])
        self.db.execute("INSERT INTO media_jobs VALUES (" + ",".join("?"*14) + ")", other)
        self.db.commit()
        with self.assertRaises(guest.RuntimeFailure):
            guest.review_restored_outbox(approval)
        self.db.execute("DELETE FROM media_jobs WHERE id='job-two'")
        self.db.execute("INSERT INTO outbox VALUES ('text','submitting')")
        self.db.commit()
        with self.assertRaises(guest.RuntimeFailure):
            guest.review_restored_outbox(approval)

    def test_missing_approved_job_or_wrong_account_rejects(self):
        approval = self.approval()
        self.db.execute("DELETE FROM media_jobs")
        self.db.commit()
        with self.assertRaises(guest.RuntimeFailure):
            guest.review_restored_outbox(approval)
        self.db.execute("INSERT INTO media_jobs VALUES (" + ",".join("?"*14) + ")", self.row)
        self.db.commit()
        other = self.database.parent.parent.with_name("another-account")
        self.database.parent.parent.rename(other)
        with self.assertRaises(guest.RuntimeFailure):
            guest.review_restored_outbox(approval)

    def test_wal_extra_send_is_seen_with_approval(self):
        approval = self.approval()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute("INSERT INTO outbox VALUES ('new-text','queued')")
        self.db.commit()
        with self.assertRaises(guest.RuntimeFailure):
            guest.review_restored_outbox(approval)


@unittest.skipUnless(os.name == "posix", "POSIX migration lock")
class MigrationTicketTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("migration_entry_tests",
            Path(__file__).resolve().parents[1] / "scripts/resume_quarantined_native.py")
        self.entry = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.entry
        spec.loader.exec_module(self.entry)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(patch.stopall)
        patch.object(self.entry.owner, "STATE", self.root).start()
        (self.root / "checkpoints/private").mkdir(parents=True)
        self.snapshot = self.root / "checkpoints/private/current.zip"
        self.snapshot.write_bytes(b"synthetic snapshot only")
        self.snapshot.chmod(0o600)
        import hashlib
        self.approval = {"schemaVersion": 1, "jobSha256": "a"*64}
        self.ticket = {"schemaVersion": 1, "phase": "prepared",
            "snapshotSha256": hashlib.sha256(self.snapshot.read_bytes()).hexdigest(),
            "quarantinedSend": self.approval}
        self.path = self.root / "native-1.2.0-migration.json"
        self.entry.owner.write_json(self.path, self.ticket)

    def test_ticket_is_consumed_once_without_altering_snapshot(self):
        before = self.snapshot.read_bytes()
        self.assertEqual(self.entry.consume_ticket(), self.approval)
        self.assertEqual(self.snapshot.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.entry.consume_ticket()

    def test_changed_snapshot_or_public_ticket_is_rejected(self):
        self.snapshot.write_bytes(b"changed saved state")
        with self.assertRaises(ValueError):
            self.entry.consume_ticket()
        self.assertEqual(self.entry.owner.read_json(self.path)["phase"], "prepared")
        self.path.chmod(0o644)
        with self.assertRaises(Exception):
            self.entry.consume_ticket()

    def test_concurrent_ticket_consumers_only_one_succeeds(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, BrokenBarrierError, Lock
        barrier = Barrier(2)
        both_read = Barrier(2)
        serialize_write = Lock()
        original_read = self.entry.owner.read_json
        original_write = self.entry.owner.write_json
        def paused_read(path):
            value = original_read(path)
            try:
                both_read.wait(timeout=0.2)
            except BrokenBarrierError:
                pass
            return value
        def serial_write(path, value):
            with serialize_write:
                original_write(path, value)
        def attempt():
            barrier.wait()
            try:
                self.entry.consume_ticket()
                return True
            except Exception:
                return False
        with patch.object(self.entry.owner, "read_json", side_effect=paused_read), \
                patch.object(self.entry.owner, "write_json", side_effect=serial_write), \
                ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sum(outcomes), 1)


@unittest.skipUnless(os.name == "posix", "POSIX snapshot descriptor")
class SnapshotBindingTests(unittest.TestCase):
    def test_runtime_volume_has_bounded_admission_headroom(self):
        self.assertEqual(guest.empty.WORK_BYTES, 2048*1024*1024)
        self.assertEqual(guest.empty.MEMORY_MAX_BYTES, 2560*1024*1024)
        self.assertEqual(guest.state_store.MAX_SOURCE_BYTES, 1024*1024*1024)
        self.assertEqual(guest.empty.MEMORY_HIGH_BYTES, 2304*1024*1024)
        self.assertEqual(guest.state_store.MAX_ARCHIVE_BYTES, 1028*1024*1024)


    def test_changed_archive_is_rejected_by_actual_stage_copy(self):
        spec = importlib.util.spec_from_file_location("migration_host_tests",
            Path(__file__).resolve().parents[1] / "scripts/session_runtime_host.py")
        host = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = host
        spec.loader.exec_module(host)
        import hashlib
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            source = base / "current.zip"
            source.write_bytes(b"old approved snapshot")
            source.chmod(0o600)
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            try:
                instance = host.SessionHost(base/"store", offline=True, seconds=90,
                                           restore_sha256=expected)
            except TypeError:
                self.fail("Host cannot bind the actual staged snapshot")
            instance.bundle = base / "bundle"
            instance.bundle.mkdir()
            source.write_bytes(b"later replacement snapshot")
            original_fstat = host.os.fstat
            from types import SimpleNamespace
            def as_root(fd):
                info = original_fstat(fd)
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                       st_size=info.st_size, st_uid=0)
            with patch.object(host.os, "fstat", side_effect=as_root):
                with self.assertRaises(Exception):
                    instance._stage_restore(source)

if __name__ == "__main__":
    unittest.main()
