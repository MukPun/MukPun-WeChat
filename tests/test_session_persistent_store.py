"""Real, disposable SQLite storage; no production profile or Native process."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'scripts/session_persistent_store.py'


@unittest.skipUnless(sys.platform == 'linux', 'Linux fd ownership lifecycle')
class PersistentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.source = self.root / 'source'
        self.source.mkdir(mode=0o700)
        for name in ('api-token', 'storage-key', 'cursor-key'):
            (self.source / name).write_bytes(('synthetic-' + name).encode())
            (self.source / name).chmod(0o600)
        self.database = self.source / 'registry.sqlite'
        with sqlite3.connect(self.database) as db:
            db.execute('create table identity (value text)')
            db.execute("insert into identity values ('synthetic-account')")
        self.database.chmod(0o600)
        (self.source / 'accounts').mkdir(mode=0o700)
        (self.source / 'accounts' / 'synthetic-account').mkdir(mode=0o700)
        agent = self.source / 'accounts/synthetic-account/agent'
        agent.mkdir(mode=0o700)
        control = agent / 'control.sqlite'
        with sqlite3.connect(control) as db:
            db.executescript('''CREATE TABLE outbox(id,idempotency_key,payload_hash,destination,payload,state,client_id,server_id,error_code,created_at);
                CREATE TABLE media_jobs(id,idempotency_key,request_hash,kind,state,phase,attempts,cancel_requested,retryable,error_code,data_json,payload,created_at,updated_at);''')
        control.chmod(0o600)
        self.spec = importlib.util.spec_from_file_location('persistent_store_tested', PATH)
        self.assertTrue(PATH.exists(), 'persistent Native store has not been implemented')
        self.module = importlib.util.module_from_spec(self.spec)
        sys.modules[self.spec.name] = self.module
        self.spec.loader.exec_module(self.module)

    def initialize(self):
        store = self.module.PersistentStore(self.state)
        store.initialize_from_stopped(self.source, expected_uid=os.geteuid(),
                                      stopped=lambda: True)
        return store

    def test_two_sqlite_lifecycles_need_no_archive(self):
        self.assertEqual(self.module.storage_status(self.state)['mode'], 'snapshot')
        store = self.initialize()
        self.assertEqual(self.module.storage_status(self.state),
                         {'mode': 'persistent', 'backup': 'disabled'})
        inode = store.path.stat().st_ino
        for index in (1, 2):
            with self.module.PersistentStore(self.state) as lease:
                with sqlite3.connect(lease.path / 'registry.sqlite') as db:
                    self.assertEqual(db.execute('select count(*) from identity').fetchone()[0], index)
                    db.execute('insert into identity values (?)', ('run-' + str(index),))
                lease.seal(stopped=lambda: True)
        self.assertEqual(store.path.stat().st_ino, inode)
        self.assertFalse(list(self.state.rglob('*.zip')))

    def test_profile_accessor_matches_published_binding(self):
        self.initialize()
        marker = self.module.storage_metadata(self.state)
        self.assertEqual(self.module.profile_fingerprint(self.state), marker['profileSha256'])

    def test_same_uid_handoff_and_seal_are_real_fd_operations(self):
        self.initialize()
        with self.module.PersistentStore(self.state) as lease:
            info = lease.path.stat()
            lease.handoff(os.geteuid(), source_identity=(info.st_dev, info.st_ino), process_alive=lambda: True)
            self.assertTrue(self.module.enabled(self.state))
            self.assertEqual(self.module.storage_metadata(self.state)['state'], 'leased')
            lease.seal(stopped=lambda: True)
        self.assertEqual(self.module.storage_metadata(self.state)['state'], 'sealed')

    def test_admission_is_private_retained_and_not_in_guest_data(self):
        import hashlib
        import json
        hashes = {k: hashlib.sha256((self.source / k).read_bytes()).hexdigest() for k in self.module.KEYS}
        profile = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        admission = dict(schemaVersion=1, profileSha256=profile, ledgerRevision=5, entries=[])
        store = self.module.PersistentStore(self.state)
        store.initialize_from_stopped(self.source, expected_uid=os.geteuid(),
                                      stopped=lambda: True, restore_admission=admission)
        with self.module.PersistentStore(self.state) as lease:
            self.assertEqual(lease.restore_admission(), admission)
        self.assertFalse((store.path / 'admission.json').exists())
        self.assertEqual((store.root / 'admission.json').stat().st_mode & 0o777, 0o600)

    def test_live_inventory_tolerates_disappearing_cache_file(self):
        self.initialize()
        with self.module.PersistentStore(self.state) as lease:
            volatile = lease.path / 'volatile'
            volatile.write_bytes(b'temporary')
            volatile.chmod(0o600)
            real_open = self.module.os.open
            def opening(name, *args, **kwargs):
                if name == 'volatile':
                    volatile.unlink()
                    raise FileNotFoundError()
                return real_open(name, *args, **kwargs)
            with patch.object(self.module.os, 'open', side_effect=opening):
                lease.validate_capacity(live=True)

    def test_unapproved_pending_row_cannot_enable_storage(self):
        with sqlite3.connect(self.source / 'accounts/synthetic-account/agent/control.sqlite') as db:
            db.execute("insert into outbox(id,state) values ('synthetic-unknown','needs_review')")
        with self.assertRaises(self.module.PersistentError):
            self.initialize()
        self.assertFalse(self.module.enabled(self.state))

    def test_online_media_headroom_clamps_disk_free_to_inventory_budget(self):
        self.initialize()
        with self.module.PersistentStore(self.state) as lease:
            usage = sum(path.stat().st_size for path in lease.path.rglob('*') if path.is_file())
            FS = type('FS', (), {'f_bavail': 4 * self.module.MAX_BYTES, 'f_frsize': 1})
            with patch.object(self.module.os, 'fstatvfs', return_value=FS()):
                self.assertEqual(self.module.storage_headroom(lease.data_fd, os.geteuid()),
                                 self.module.MAX_BYTES - usage)
            FS.f_bavail = self.module.MIN_FREE + 4096
            with patch.object(self.module.os, 'fstatvfs', return_value=FS()):
                self.assertEqual(self.module.storage_headroom(lease.data_fd, os.geteuid()), 4096)
            FS.f_bavail = self.module.MIN_FREE - 1
            with patch.object(self.module.os, 'fstatvfs', return_value=FS()):
                self.assertEqual(self.module.storage_headroom(lease.data_fd, os.geteuid()), 0)

    def test_lifetime_lock_rejects_second_writer(self):
        self.initialize()
        with self.module.PersistentStore(self.state):
            with self.assertRaisesRegex(self.module.PersistentError, 'persistent_busy'):
                with self.module.PersistentStore(self.state):
                    pass

    @unittest.skipUnless(os.geteuid() == 0, 'real UID handoff requires root')
    def test_handoff_mount_binding_and_sealing(self):
        self.initialize()
        with self.module.PersistentStore(self.state) as lease:
            with self.assertRaises(self.module.PersistentError):
                lease.handoff(63121, source_identity=(0, 0), process_alive=lambda: True)
            info = lease.path.stat()
            lease.handoff(63121, source_identity=(info.st_dev, info.st_ino), process_alive=lambda: True)
            self.assertEqual((lease.path / 'registry.sqlite').stat().st_uid, 63121)
            self.assertEqual(lease.path.stat().st_uid, 63121)
            with self.assertRaises(self.module.PersistentError):
                lease.seal(stopped=lambda: False)
            lease.seal(stopped=lambda: True)
            self.assertEqual(lease.path.stat().st_uid, 0)
            self.assertEqual((lease.path / 'registry.sqlite').stat().st_uid, 0)

    def test_unsafe_objects_and_unstopped_source_do_not_publish_mode(self):
        for kind in ('symlink', 'hardlink', 'fifo', 'unstopped'):
            with self.subTest(kind=kind):
                item = self.source / 'bad'
                if kind == 'symlink': item.symlink_to(self.database)
                if kind == 'hardlink': os.link(self.database, item)
                if kind == 'fifo': os.mkfifo(item)
                target = self.root / kind
                target.mkdir(mode=0o700)
                with self.assertRaises(self.module.PersistentError):
                    self.module.PersistentStore(target).initialize_from_stopped(
                        self.source, expected_uid=os.geteuid(), stopped=lambda: kind != 'unstopped')
                self.assertEqual(self.module.storage_status(target)['mode'], 'snapshot')
                item.unlink(missing_ok=True)

    def test_parent_symlink_and_replaced_data_inode_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(self.module.PersistentError):
            self.module.storage_status(alias)
        store = self.initialize()
        store.path.rename(store.path.with_name('old-data'))
        store.path.mkdir(mode=0o700)
        with self.assertRaises(self.module.PersistentError):
            with self.module.PersistentStore(self.state): pass

    def test_profile_mutation_and_disk_headroom_fail_closed(self):
        self.initialize()
        (self.state / 'persistent/data/api-token').write_bytes(b'changed')
        with self.assertRaises(self.module.PersistentError):
            with self.module.PersistentStore(self.state): pass
        other = self.root / 'low-space'
        other.mkdir(mode=0o700)
        with patch.object(self.module.os, 'fstatvfs', return_value=type('FS', (), {'f_bavail': 0, 'f_frsize': 4096})()):
            with self.assertRaisesRegex(self.module.PersistentError, 'persistent_headroom'):
                self.module.PersistentStore(other).initialize_from_stopped(
                    self.source, expected_uid=os.geteuid(), stopped=lambda: True)

    @unittest.skipUnless(os.geteuid() == 0, 'real UID handoff requires root')
    def test_unclean_lease_requires_offline_review(self):
        self.initialize()
        with self.module.PersistentStore(self.state) as lease:
            info = lease.path.stat()
            lease.handoff(63121, source_identity=(info.st_dev, info.st_ino), process_alive=lambda: True)
        with self.assertRaisesRegex(self.module.PersistentError, 'persistent_requires_review'):
            with self.module.PersistentStore(self.state): pass
        # Only disposable test state cleanup; no runtime recovery convenience.
        for base, dirs, files in os.walk(self.state):
            os.chown(base, 0, 0)
            for name in files: os.chown(Path(base) / name, 0, 0)


if __name__ == '__main__': unittest.main()
