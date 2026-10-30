"""A restart may reconcile a proved send, but must never replay it."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wechat_bridge.durable import Spool

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('text_recovery_test', ROOT / 'scripts/text_send_recovery.py')
recovery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recovery
if Path(spec.origin).exists():
    spec.loader.exec_module(recovery)

ID = '12345678-1234-4123-8123-123456789abc'
BODY, PEER = 'synthetic text', 'synthetic-peer'
NATIVE_KEY = 'a' * 32


def audit():
    return {'state': 'submitted', 'key': hashlib.sha256((NATIVE_KEY + ':0').encode()).hexdigest()[:32],
        'requestId': 'b' * 32, 'destinationSha256': hashlib.sha256(PEER.encode()).hexdigest(),
        'textSha256': hashlib.sha256(BODY.encode()).hexdigest(), 'bytes': len(BODY.encode()),
        'sendId': ID, 'nativeClientPid': 100, 'nativeClientTicks': 200, 'segmentBytes': 8192}


def intent():
    return {'id': ID, 'accountId': 'wechat-primary', 'destination': PEER, 'state': 'accepted',
            'serverId': '123', 'clientId': '456', 'localMessageId': 'sent-1'}


def message():
    return {'id': 'sent-1', 'accountId': 'wechat-primary', 'threadId': PEER,
            'direction': 'outgoing', 'type': 1, 'senderId': 'synthetic-self', 'body': BODY, 'serverId': '123'}


class ProofTests(unittest.TestCase):
    def test_full_bound_native_proof(self):
        self.assertTrue(recovery.verify_readback(audit(), intent(), message()))

    def test_each_identity_and_content_mismatch_is_rejected(self):
        changes = [('intent', key, value) for key, value in {
            'id': '87654321-1234-4123-8123-123456789abc', 'accountId': 'other',
            'destination': 'other', 'state': 'submitting', 'serverId': '', 'clientId': None,
            'localMessageId': 'other'}.items()]
        changes += [('message', key, value) for key, value in {
            'id': 'other', 'accountId': 'other', 'threadId': 'other', 'direction': 'incoming',
            'type': True, 'senderId': PEER, 'body': 'other', 'serverId': '789'}.items()]
        for target, key, value in changes:
            with self.subTest(target=target, key=key):
                i, m = intent(), message()
                (i if target == 'intent' else m)[key] = value
                self.assertFalse(recovery.verify_readback(audit(), i, m))

    def test_readback_uses_only_the_exact_send_and_its_named_projection(self):
        lookup = Mock()
        lookup.policy.prefix = '/v1/accounts/wechat-primary'
        lookup.policy.message.return_value = '/v1/accounts/wechat-primary/messages/sent-1'
        lookup._get.side_effect = [intent(), message()]
        self.assertTrue(recovery.read_bound(lookup, audit(), 20))
        self.assertEqual([call.args for call in lookup._get.call_args_list], [
            ('/v1/accounts/wechat-primary/sends/' + ID, 20),
            ('/v1/accounts/wechat-primary/messages/sent-1', 20)])

    def test_unaccepted_or_different_send_never_reads_a_projection(self):
        for changes in ({'state': 'submitting'}, {'id': 'other'}, {'accountId': 'other'}, {'localMessageId': None}):
            lookup = Mock()
            lookup.policy.prefix = '/v1/accounts/wechat-primary'
            lookup._get.return_value = {**intent(), **changes}
            self.assertFalse(recovery.read_bound(lookup, audit(), 20))
            self.assertEqual(lookup._get.call_count, 1)

    def test_active_worker_prevents_recovery(self):
        core = SimpleNamespace(groups=SimpleNamespace(READER_PREFIX='astrbot-wechat-group-read-'), tap=Mock())
        core.tap.load_state.return_value = (0, {'LoadState': 'loaded', 'ActiveState': 'active', 'MainPID': '123'})
        process = SimpleNamespace(returncode=0, stdout='astrbot-wechat-private-host-123456abcdef.service loaded active running')
        with patch.object(recovery.subprocess, 'run', return_value=process):
            self.assertFalse(recovery.workers_stopped(core))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.database = self.root / 'private-forward.sqlite'
        spool = Spool(self.database)
        spool.reserve('c' * 64, 'd' * 64, NATIVE_KEY)
        spool.reserve('e' * 64, 'f' * 64, '9' * 32)
        spool.close()
        self.audit_path = self.root / 'forward-send.json'
        self.audit_path.write_text(json.dumps(audit()))
        self.audit_path.chmod(0o600)
        self.frozen = {}
        for name in ('forward-image-send.json', 'forward-voice-send.json', 'forward-image-hold.json'):
            path = self.root / name
            path.write_text(json.dumps({'state': 'unknown', 'name': name}))
            self.frozen[name] = path.read_bytes()
        self.pinned = Mock()
        self.pinned.host.client_pid, self.pinned.host.client_ticks = 100, 200
        self.pinned.unchanged.return_value = True
        def private_json(path, value):
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        self.owner = SimpleNamespace(STATE=self.root,
            read_json=lambda p: json.loads(p.read_text()),
            write_json=private_json,
            private_directory=lambda p: p.mkdir(mode=0o700, parents=True, exist_ok=True))
        self.core = SimpleNamespace(owner=self.owner, groups=SimpleNamespace(PinnedNative=lambda: self.pinned))

    def run_recovery(self, **patches):
        with patch.object(recovery, 'DATABASE', self.database), \
                patch.object(recovery, 'workers_stopped', return_value=patches.get('stopped', True)), \
                patch.object(recovery, 'readback', return_value=patches.get('proved', True)) as read:
            result = recovery.reconcile(self.core, self.audit_path, {'segmentBytes': 8192})
        self.assertTrue(all((self.root / name).read_bytes() == raw for name, raw in self.frozen.items()))
        return result, read

    def rows(self):
        with sqlite3.connect(self.database) as db:
            return db.execute('SELECT key,native_key,result FROM submissions ORDER BY key').fetchall()

    def test_confirmed_short_text_is_finalized_with_backup_without_replay(self):
        self.assertTrue(self.run_recovery()[0])
        rows = self.rows()
        answer = json.loads(rows[0][2])
        self.assertEqual(answer['retcode'], 0)
        self.assertEqual(rows[1][2], None)
        self.assertEqual(json.loads(self.audit_path.read_text())['state'], 'accepted')
        backups = list((self.root / 'text-recovery').glob('receipt-*/bridge.sqlite'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as db:
            self.assertIsNone(db.execute('SELECT result FROM submissions WHERE key=?', ('c' * 64,)).fetchone()[0])
        self.pinned.close.assert_called_once()

    def test_unknown_result_can_be_reconciled(self):
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE submissions SET result=? WHERE key=?',
                (json.dumps({'status': 'failed', 'retcode': 1408, 'data': None,
                    'message': 'outcome_unknown', 'wording': 'outcome_unknown'}), 'c' * 64))
        self.assertTrue(self.run_recovery()[0])
        self.assertEqual(json.loads(self.rows()[0][2])['retcode'], 0)

    def test_unknown_proof_or_active_worker_never_changes_any_state(self):
        for options in ({'proved': False}, {'stopped': False}):
            with self.subTest(options=options):
                before = self.rows(), self.audit_path.read_bytes()
                self.assertFalse(self.run_recovery(**options)[0])
                self.assertEqual((self.rows(), self.audit_path.read_bytes()), before)

    def test_different_native_instance_cannot_reconcile(self):
        self.pinned.host.client_ticks = 201
        before = self.rows(), self.audit_path.read_bytes()
        result, read = self.run_recovery()
        self.assertFalse(result)
        read.assert_not_called()
        self.assertEqual((self.rows(), self.audit_path.read_bytes()), before)

    def test_missing_or_nonfirst_or_segmented_submission_is_not_recovered(self):
        for change in ({'key': '0' * 32},
                       {'key': hashlib.sha256((NATIVE_KEY + ':1').encode()).hexdigest()[:32]},
                       {'bytes': 8188}, {'nativeClientPid': None}, {'segmentBytes': 4096}):
            with self.subTest(change=change):
                current = {**audit(), **change}
                self.audit_path.write_text(json.dumps(current))
                before = self.rows(), self.audit_path.read_bytes()
                self.assertFalse(self.run_recovery()[0])
                self.assertEqual((self.rows(), self.audit_path.read_bytes()), before)

    def test_conflicting_completed_result_is_not_overwritten(self):
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE submissions SET result=? WHERE key=?',
                       (json.dumps({'status': 'failed', 'retcode': 1400}), 'c' * 64))
        before = self.rows(), self.audit_path.read_bytes()
        for _ in range(5):
            self.assertFalse(self.run_recovery()[0])
        self.assertEqual((self.rows(), self.audit_path.read_bytes()), before)
        self.assertFalse((self.root / 'text-recovery').exists())

    def test_commit_then_audit_write_crash_recovers_idempotently(self):
        write = self.owner.write_json
        def fail_audit(path, value):
            if path == self.audit_path:
                raise OSError('synthetic write interruption')
            write(path, value)
        self.owner.write_json = fail_audit
        self.assertFalse(self.run_recovery()[0])
        first = self.rows()
        self.assertEqual(json.loads(first[0][2])['retcode'], 0)
        self.assertEqual(json.loads(self.audit_path.read_text())['state'], 'submitted')
        self.assertFalse(self.run_recovery()[0])
        self.assertEqual(len(list((self.root / 'text-recovery').glob('receipt-*'))), 1)
        self.owner.write_json = write
        self.assertTrue(self.run_recovery()[0])
        self.assertEqual(self.rows(), first)

    def test_existing_success_from_onebot_is_idempotent(self):
        with sqlite3.connect(self.database) as db:
            mid = db.execute("INSERT INTO identities(kind,raw) VALUES('outgoing',?)", (ID,)).lastrowid
            result = {'status': 'ok', 'retcode': 0, 'data': {'message_id': mid}}
            db.execute('UPDATE submissions SET result=? WHERE key=?', (json.dumps(result), 'c' * 64))
        self.assertTrue(self.run_recovery()[0])
        self.assertEqual(json.loads(self.rows()[0][2]), result)

    def test_completed_backups_are_bounded(self):
        for index in range(5):
            self.audit_path.write_text(json.dumps({**audit(), 'requestId': f'{index:032x}'}))
            self.assertTrue(self.run_recovery()[0])
        self.assertEqual(len(list((self.root / 'text-recovery').glob('receipt-*'))), 3)

    def test_interrupted_empty_backup_is_rebuilt_before_recovery(self):
        raw = self.audit_path.read_bytes()
        target = self.root / 'text-recovery' / ('receipt-' + hashlib.sha256(raw).hexdigest()[:12])
        target.mkdir(mode=0o700, parents=True)
        (target / 'audit.json').write_bytes(raw)
        (target / 'audit.json').chmod(0o600)
        (target / 'bridge.sqlite').touch(mode=0o600)
        (target / '.backup-ready.json.next').write_bytes(b'{"interrupted":')
        (target / '.backup-ready.json.next').chmod(0o600)
        self.assertTrue(self.run_recovery()[0])
        with sqlite3.connect(target / 'bridge.sqlite') as saved:
            self.assertIsNone(saved.execute('SELECT result FROM submissions WHERE key=?', ('c' * 64,)).fetchone()[0])

    def test_systemd_state_directory_alias_resolves_to_canonical_database(self):
        private = self.root / 'private'
        private.mkdir(mode=0o700)
        self.database.rename(private / self.database.name)
        alias = self.root / 'state-alias'
        alias.symlink_to(private, target_is_directory=True)
        self.database = alias / self.database.name
        self.assertTrue(self.run_recovery()[0])
        self.assertEqual(json.loads(self.rows()[0][2])['retcode'], 0)

    def test_database_connections_close_before_worker_resume(self):
        connect = recovery.sqlite3.connect
        connections = []
        def remember(*args, **kwargs):
            db = connect(*args, **kwargs)
            connections.append(db)
            return db
        with patch.object(recovery.sqlite3, 'connect', side_effect=remember):
            self.assertTrue(self.run_recovery()[0])
        self.assertGreaterEqual(len(connections), 2)
        for db in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')

    def test_owned_partial_audit_write_does_not_block_proved_recovery(self):
        temporary = self.audit_path.with_name('.' + self.audit_path.name + '.next')
        temporary.write_bytes(b'{"interrupted":')
        temporary.chmod(0o600)
        write = self.owner.write_json
        def exclusive(path, value):
            if path.with_name('.' + path.name + '.next').exists():
                raise FileExistsError('synthetic interrupted atomic write')
            write(path, value)
        self.owner.write_json = exclusive
        self.assertTrue(self.run_recovery()[0])
        self.assertFalse(temporary.exists())

    def test_native_or_audit_changes_after_readback_prevent_mutation(self):
        self.pinned.unchanged.return_value = False
        before = self.rows(), self.audit_path.read_bytes()
        self.assertFalse(self.run_recovery()[0])
        self.assertEqual((self.rows(), self.audit_path.read_bytes()), before)

    def test_database_symlink_is_rejected(self):
        other = self.root / 'other.sqlite'
        self.database.rename(other)
        self.database.symlink_to(other)
        self.assertFalse(self.run_recovery()[0])


if __name__ == '__main__':
    unittest.main()
