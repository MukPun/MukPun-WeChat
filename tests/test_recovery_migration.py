"""Migration tests: check stays read-only, apply is exact and repeatable."""

import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import types
import unittest

from scripts import recovery_checkpoint as cp
from scripts import recovery_dispositions as dispositions
from scripts import recovery_manifest as manifests
from scripts import recovery_snapshot as snapshot
from scripts import session_runtime as runtime
from scripts.forward_sse import event_digest
from wechat_bridge import loop_guard
from wechat_bridge.durable import Spool
from test_recovery_snapshot import fixture, SEED

# The fixture archive carries this exact api-token; real hosts derive the same way.
GENERATION = 'profile:' + hashlib.sha256(b'synthetic-api-token').hexdigest()


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def boundary(n):
    value = dict(cursor=str(n), kind='message', data={'id': str(n)},
                 createdAt='2026-01-01T00:00:00.120+00:00')
    return dict(cursor=str(n), anchor=event_digest(value))


def data(source):
    return dict(account='wechat-primary', id=source, body='same', sender_id='peer',
                room_id='peer', direction='incoming', origin='live', type=1, timestamp=1)


class Harness:
    """A legacy host: saved snapshot, old bridge, audits, no manifest tree."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.control = self.root / 'control'
        self.control.mkdir(mode=0o700)
        self.bridge = self.root / 'bridge.sqlite'
        self.snapshot = fixture(self.root, last=8)
        store = snapshot.state.SnapshotStore(self.state / 'checkpoints')
        store.prepare()
        target = self.state / 'checkpoints/private/current.zip'
        shutil.copyfile(self.snapshot, target)
        target.chmod(0o600)
        self.audit = dict(state='accepted', sendId=SEED['sendId'],
                          destinationSha256=SEED['destinationSha256'],
                          textSha256=SEED['textSha256'], bytes=SEED['bytes'])
        self.write('forward-send.json', json.dumps(self.audit).encode())
        # A legacy broker: receive cursor, inbox row, and no recovery_window.
        self.spool = Spool(self.bridge)
        self.spool.db.execute('PRAGMA synchronous=OFF')  # synthetic fixture only
        self.spool.admit_checkpoint('source-8', data('source-8'), GENERATION,
                                    json.dumps(boundary(8)))
        # The legacy image predates recovery retention entirely.
        self.spool.db.execute('DROP TABLE recovery_window')
        self.spool.db.commit()
        self.migration = load(Path(__file__).resolve().parents[1]
                              / 'scripts/migrate_recovery_state.py', '_test_migrate')
        self.migration.bind(self.core())

    def core(self):
        checks = types.ModuleType('_test_checks')
        checks.__dict__.update(cp.__dict__)
        # Only the shim names the synthetic host; cp.bridge() keeps its real globals.
        checks.BRIDGE_PUBLIC = self.bridge
        owner = types.SimpleNamespace(STATE=self.state, CONTROL=self.control,
                                      read_private=runtime.read_private,
                                      private_directory=runtime.private_directory,
                                      owner_active=lambda: False)
        return types.SimpleNamespace(owner=owner, recovery_checkpoints=checks,
                                     recovery_dispositions=dispositions,
                                     recovery_manifest=manifests, loops=loop_guard)

    def write(self, name, raw):
        path = self.state / name
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def tables(self, path=None):
        with sqlite3.connect(path or self.bridge) as db:
            return {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}

    def window(self):
        with sqlite3.connect(self.bridge) as db:
            return db.execute('SELECT floor_seq,epoch,lost_through FROM recovery_window').fetchone()

    def close(self):
        self.spool.close()
        self.temp.cleanup()


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.migration = self.harness.migration

    def check(self):
        return self.migration.check()

    def apply(self, **kw):
        return self.migration.apply(**kw)

    def refusal(self, code):
        with self.assertRaises(self.migration.Migration) as caught:
            self.check()
        self.assertEqual(caught.exception.code, code)


class CheckTests(MigrationCase):
    def test_check_is_read_only_and_resolves_the_exact_plan(self):
        before = sorted(os.listdir(self.harness.state))
        raw = self.harness.bridge.read_bytes()
        plan = self.check()
        self.assertEqual(plan['stage'], 'recovery_migration_plan')
        self.assertFalse(plan['published'])
        self.assertEqual(plan['generation'], GENERATION)
        self.assertEqual(plan['checkpointName'], GENERATION)
        self.assertEqual(plan['bridgeBoundary'], boundary(8))
        self.assertEqual(plan['nativeBoundary'], boundary(8))
        self.assertEqual(plan['selfProof'], SEED)
        self.assertEqual(plan['pending'], 0)
        self.assertEqual(plan['ledgerRevision'], 0)
        self.assertEqual(plan['coverage']['floorSeq'], None)
        self.assertEqual(plan['coverage']['highSeq'], 1)
        self.assertEqual(plan['coverage']['retainedFloor'], 1)
        self.assertEqual(sorted(os.listdir(self.harness.state)), before)
        self.assertFalse((self.harness.state / 'recovery').exists())
        self.assertFalse((self.harness.state / 'recovery-migration-backups').exists())
        self.assertEqual(self.harness.bridge.read_bytes(), raw)
        self.assertNotIn('recovery_window', self.harness.tables())

    def test_check_refuses_an_ambiguous_checkpoint(self):
        self.harness.spool.cursor('profile:' + 'c' * 64, '8')
        self.harness.spool.db.commit()
        self.refusal('migration_checkpoint_ambiguous')

    def test_check_refuses_a_missing_canonical_anchor(self):
        self.harness.spool.cursor(GENERATION,
                                  json.dumps(dict(cursor='7', anchor='0' * 64)))
        self.harness.spool.db.commit()
        self.refusal('migration_anchor_missing')

    def test_check_refuses_an_unaccepted_forward(self):
        self.harness.write('forward-send.json',
                           json.dumps(dict(self.harness.audit, state='unknown')).encode())
        self.refusal('migration_forward_review')

    def test_check_refuses_a_snapshot_behind_the_receive_boundary(self):
        import unittest.mock as mock
        original = snapshot.inspect

        def stale(archive, **kw):
            value = original(archive, **kw)
            value['nativeBoundary'] = boundary(7)
            return value

        with mock.patch.object(snapshot, 'inspect', stale):
            self.refusal('migration_snapshot_behind')

    def test_check_refuses_a_publishing_row(self):
        with self.harness.spool.db:
            self.harness.spool.db.execute(
                "INSERT INTO inbox(source,digest,payload,state,created) "
                "VALUES('source-7','d','p','publishing',1)")
        self.refusal('migration_bridge_publishing')


class ApplyTests(MigrationCase):
    def test_apply_publishes_one_verified_pair_and_advances_retention(self):
        report = self.apply()
        self.assertEqual(report['stage'], 'recovery_migrated')
        self.assertEqual(report['imported'], 0)
        self.assertEqual(report['matched'], 0)
        self.assertFalse(report['replayed'])
        self.assertEqual(report['checkpointName'], GENERATION)
        self.assertEqual(report['bridgeBoundary'], boundary(8))
        self.assertEqual(report['dedup'], dict(floorSeq=1, highSeq=1))
        store = manifests.Store(self.harness.state / 'recovery')
        current, archive = store.current()
        self.assertEqual(hashlib.sha256(
            dispositions.admission._encoded(current)).hexdigest(), report['manifest'])
        self.assertIsNone(store.previous())
        self.assertEqual(current['nativeBoundary'], boundary(8))
        self.assertEqual(current['bridgeBoundary'], boundary(8))
        self.assertEqual(current['selfProof'], SEED)
        self.assertEqual(current['dedup'], dict(floorSeq=1, highSeq=1))
        self.assertEqual(int(current['bridgeBoundary']['cursor']),
                         int(current['nativeBoundary']['cursor']))
        self.assertTrue(archive.name.endswith('.zip'))
        # Retention is installed and advanced only after the pointer commit.
        floor, epoch, _ = self.harness.window()
        self.assertEqual(floor, 1)
        self.assertRegex(epoch, r'^[0-9a-f]{32}$')
        self.assertEqual(current['epoch'], epoch)
        # The backup holds the source audit and a consistent bridge copy.
        saved = Path(report['backup'])
        self.assertEqual(saved.parent.name, 'recovery-migration-backups')
        self.assertEqual((saved / 'forward-send.json').read_bytes(),
                         (self.harness.state / 'forward-send.json').read_bytes())
        self.assertIn('inbox', self.harness.tables(saved / 'bridge.sqlite'))
        self.assertEqual((saved / 'snapshot.zip').read_bytes(),
                         (self.harness.state / 'checkpoints/private/current.zip').read_bytes())
        for name in os.listdir(saved):
            entry = saved / name
            if entry.is_dir():
                self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o700)
            else:
                self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)

    def test_apply_is_idempotent(self):
        first = self.apply()
        second = self.apply()
        self.assertEqual(first['manifest'], second['manifest'])
        self.assertEqual(second['imported'], 0)
        store = manifests.Store(self.harness.state / 'recovery')
        self.assertIsNone(store.previous())
        self.assertEqual(store.current()[0]['createdAt'],
                         store.current()[0]['createdAt'])
        self.assertEqual(self.harness.window()[0], 1)

    def test_apply_refuses_while_the_broker_holds_the_bridge(self):
        import fcntl
        handle = os.open(self.harness.state / 'bridge.lock',
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(self.migration.Migration) as caught:
                self.apply()
            self.assertEqual(caught.exception.code, 'migration_bridge_active')
        finally:
            os.close(handle)
        self.assertFalse((self.harness.state / 'recovery').exists())

    def test_apply_refuses_while_the_native_owner_runs(self):
        self.harness.migration.core.owner.owner_active = lambda: True
        with self.assertRaises(self.migration.Migration) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, 'migration_owner_active')
        self.assertFalse((self.harness.state / 'recovery').exists())

    def test_apply_refuses_a_concurrent_recovery_lock(self):
        import fcntl
        handle = os.open(self.harness.state / 'recovery.lock',
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(self.migration.Migration) as caught:
                self.apply()
            self.assertEqual(caught.exception.code, 'migration_recovery_active')
        finally:
            os.close(handle)

    def test_apply_keeps_only_the_bounded_backup_set(self):
        for stamp in ('20260101-000001', '20260101-000002', '20260101-000003',
                      '20260101-000004', '20260101-000005'):
            self.apply(stamp=stamp)
        saved = sorted(os.listdir(self.harness.state / 'recovery-migration-backups'))
        self.assertEqual(saved, ['20260101-000002', '20260101-000003',
                                 '20260101-000004', '20260101-000005'])

    def test_apply_imports_a_held_legacy_image_and_keeps_the_original(self):
        raw = json.dumps(dict(state='unknown', code='image_needs_review',
                              jobId='job_11111111-1111-4111-8111-111111111111',
                              text='hi')).encode()
        self.harness.write('forward-image-send.json', raw)
        hold = dict(schemaVersion=1, mode='text_only',
                    jobId='job_11111111-1111-4111-8111-111111111111',
                    code='image_needs_review', auditSha256='0' * 64,
                    created=1, updated=2, attempts=3)
        self.harness.write('forward-image-hold.json', json.dumps(hold).encode())
        # A mismatched audit digest is refused, never guessed, and nothing is lost.
        with self.assertRaises(self.migration.Migration) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, 'migration_disposition')
        self.assertEqual((self.harness.state / 'forward-image-send.json').read_bytes(), raw)
        self.assertFalse((self.harness.state / 'recovery').exists())
        self.assertEqual((self.harness.state / 'forward-image-send.json').read_bytes(), raw)
