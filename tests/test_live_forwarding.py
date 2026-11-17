"""Live-only receive runs use a real SQLite spool, without Native or models."""
import json
from pathlib import Path
import tempfile
import unittest

from wechat_bridge.durable import Spool, StateError
from scripts import forward_sse as sse


CHECKPOINT = json.dumps({'cursor': '20', 'anchor': 'a' * 64}, separators=(',', ':'))
TOKEN = 'b' * 32


class LiveReceiveRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'bridge.sqlite'
        self.spool = Spool(self.path)

    def tearDown(self):
        self.spool.close()
        self.directory.cleanup()

    def test_full_start_skips_old_queue_but_retains_uncertain_and_identity(self):
        self.spool.admit('old', {'body': 'old message'})
        self.spool.admit('ambiguous', {'body': 'preserve evidence'})
        self.spool.publishing('ambiguous')
        self.spool.quarantine_publishing()
        identity = self.spool.identity('user', 'stable-peer')[0]
        self.spool.protect_recovery(1, 'c' * 32)
        result = self.spool.begin_live_run('profile:fixture', TOKEN, CHECKPOINT)
        self.assertEqual(result, {'checkpoint': CHECKPOINT, 'skipped': 1, 'new': True})
        self.assertIsNone(self.spool.next_pending())
        rows = dict(self.spool.db.execute('SELECT source,state FROM inbox'))
        self.assertEqual(rows, {'old': 'expired', 'ambiguous': 'uncertain'})
        self.assertEqual(self.spool.identity('user', 'stable-peer')[0], identity)
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'], 1)
        payload = self.spool.db.execute("SELECT payload FROM inbox WHERE source='old'").fetchone()[0]
        self.assertEqual(json.loads(payload), {'body': 'old message'})
        self.assertEqual(self.spool.cursor('profile:fixture'), CHECKPOINT)

    def test_same_run_worker_restart_preserves_new_queue_and_cursor(self):
        self.spool.begin_live_run('profile:fixture', TOKEN, CHECKPOINT)
        new_checkpoint = json.dumps({'cursor': '21', 'anchor': 'd' * 64}, separators=(',', ':'))
        self.spool.admit_checkpoint('new', {'body': 'new message'}, 'profile:fixture', new_checkpoint)
        self.spool.close()
        self.spool = Spool(self.path)
        result = self.spool.begin_live_run('profile:fixture', TOKEN, CHECKPOINT)
        self.assertEqual(result, {'checkpoint': new_checkpoint, 'skipped': 0, 'new': False})
        self.assertEqual(self.spool.next_pending()[0], 'new')
        second = self.spool.begin_live_run('profile:fixture', 'e' * 32, new_checkpoint)
        self.assertTrue(second['new'])
        self.assertEqual(second['skipped'], 1)
        self.assertIsNone(self.spool.next_pending())

    def test_invalid_start_is_atomic(self):
        self.spool.admit('old', {'body': 'unchanged'})
        for token, boundary in ((TOKEN, '20'), ('bad', CHECKPOINT),
                                (TOKEN, '{"cursor":"20","anchor":"secret"}')):
            with self.subTest(token=token, boundary=boundary), self.assertRaises(StateError):
                self.spool.begin_live_run('profile:fixture', token, boundary)
        self.assertEqual(self.spool.next_pending()[0], 'old')
        self.assertIsNone(self.spool.cursor('profile:fixture'))


class FreshBoundaryTests(unittest.TestCase):
    def test_anchor_is_from_actual_event_and_next_event_is_not_consumed(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        event = {'cursor': '20', 'kind': 'message.committed',
                 'data': {'id': 'fixture'}, 'createdAt': '2026-01-01T00:00:00Z'}
        reader = SimpleNamespace(policy=SimpleNamespace(events=lambda after: '/events/' + after),
            _get=Mock(return_value={'items': [event, dict(event, cursor='21')]}))
        anchor = sse.checkpoint_anchor(reader, '20', 100)
        self.assertEqual(anchor, sse.event_digest(event))
        reader._get.assert_called_once_with('/events/19', 100, timeout=10)
        reader._get.return_value = {'items': [dict(event, cursor='21')]}
        with self.assertRaises(sse.StreamError):
            sse.checkpoint_anchor(reader, '20', 100)


class LiveStartupTests(unittest.TestCase):
    def test_startup_uses_saved_persistent_account_without_reading_archives(self):
        from scripts import live_forwarding_startup as live
        from types import SimpleNamespace
        from unittest.mock import Mock
        from test_recovery_snapshot import SEED
        marker = {'schemaVersion': 1, 'profileSha256': 'f' * 64, 'selfProof': SEED}
        store = SimpleNamespace(enabled=lambda _: True, profile_fingerprint=lambda _: 'f' * 64)
        owner = SimpleNamespace(STATE=Path('/private'), companion=lambda _: store,
            read_json=Mock(return_value=marker), owner_active=lambda: True,
            write_json=Mock(), start_owner=Mock(), client=Mock())
        core = SimpleNamespace(owner=owner, loops=SimpleNamespace(proof=lambda _: None),
            require=lambda ok, code: None if ok else (_ for _ in ()).throw(ValueError(code)),
            trial=SimpleNamespace(fresh_status=lambda: {'connected': True, 'accounts': 1}))
        result = live.startup(core)
        self.assertEqual(result['phase'], 'receive_ready')
        self.assertEqual(live.read_binding(core)['selfProof'], SEED)
        self.assertTrue(all(call.args[0].name == 'live-forwarding.json' for call in owner.read_json.call_args_list))
        owner.start_owner.assert_not_called()
        owner.client.assert_not_called()
        store.profile_fingerprint = lambda _: 'e' * 64
        with self.assertRaises(ValueError):
            live.read_binding(core)

    def test_persistent_live_mode_never_runs_capture_even_if_capture_is_broken(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from wechat_bridge.adapters.wechat.recovery import RecoveryBackend
        state = SimpleNamespace(live_only=True, recovery_state='ready', settings={'enabled': True})
        backend = RecoveryBackend(SimpleNamespace(Duplex=object), state)
        backend._async_capture = Mock(side_effect=OSError('synthetic backup failure'))
        backend.capture_if_safe()
        backend._async_capture.assert_not_called()
        self.assertEqual(state.recovery_state, 'ready')


if __name__ == '__main__':
    unittest.main()
