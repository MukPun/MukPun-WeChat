"""Production broker framing with synthetic media and bounded polling turns."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix', 'Production broker uses POSIX descriptors')
class BrokerPipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('forwarder_pipe_test', ROOT / 'scripts/run_private_forwarder.py')
        cls.forwarder = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.forwarder
        spec.loader.exec_module(cls.forwarder)

    def image_hold(self):
        audit = {'state': 'unknown', 'code': 'image_needs_review', 'key': 'a' * 32,
                 'requestId': 'b' * 32, 'destinationSha256': 'c' * 64,
                 'sourceSha256': 'd' * 64, 'bytes': 27186,
                 'jobId': 'job_11111111-2222-4333-8444-555555555555'}
        digest = hashlib.sha256(json.dumps(audit, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        hold = {'schemaVersion': 1, 'mode': 'text_only', 'auditSha256': digest,
                'jobId': audit['jobId'], 'nativeClientPid': 123, 'nativeClientTicks': 456,
                'approvedAt': 1700000000}
        return {'imageOutputEnabled': False}, audit, hold

    def test_image_hold_allows_only_disabled_output_and_exact_native_instance(self):
        settings, audit, hold = self.image_hold()
        self.assertTrue(self.forwarder.held_image_allowed(settings, audit, hold, native_identity=(123, 456)))
        for identity in ((124, 456), (123, 457)):
            self.assertFalse(self.forwarder.held_image_allowed(settings, audit, hold, native_identity=identity))
        self.assertFalse(self.forwarder.held_image_allowed({'imageOutputEnabled': True}, audit, hold))
        self.assertFalse(self.forwarder.held_image_allowed(None, audit, hold))

    def test_explicit_image_resume_keeps_exact_audit_and_native_binding(self):
        settings, audit, hold = self.image_hold()
        resumed = {**hold, 'mode': 'new_images'}
        for enabled in (False, True):
            current = {**settings, 'imageOutputEnabled': enabled}
            self.assertTrue(self.forwarder.held_image_allowed(current, audit, resumed, native_identity=(123, 456)))
            self.assertFalse(self.forwarder.held_image_allowed(current, audit, resumed, native_identity=(124, 456)))
            self.assertFalse(self.forwarder.held_image_allowed(current, {**audit, 'key': 'e' * 32}, resumed))

    def test_resumed_image_audit_never_overwrites_the_held_audit(self):
        settings, audit, hold = self.image_hold()
        settings['imageOutputEnabled'] = True
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            original = state / 'forward-image-send.json'
            original.write_text(json.dumps(audit))
            (state / 'forward-image-hold.json').write_text(json.dumps({**hold, 'mode': 'new_images'}))
            before = original.read_bytes()
            with patch.object(self.forwarder.owner, 'STATE', state), \
                    patch.object(self.forwarder.owner, 'read_json', side_effect=lambda path: json.loads(path.read_text())), \
                    patch.object(self.forwarder.owner, 'owner_active', return_value=True):
                path, approved, frozen = self.forwarder.image_send_preflight(settings)
                self.assertNotEqual(path, original)
                self.assertFalse(path.exists())
                self.assertEqual(frozen, audit)
                path.write_text(json.dumps({'state': 'accepted'}))
                self.assertEqual(self.forwarder.image_send_preflight(settings), (path, approved, frozen))
            self.assertEqual(original.read_bytes(), before)

    def test_resume_permission_does_not_cover_another_unresolved_image(self):
        settings, audit, hold = self.image_hold()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'forward-image-send.json').write_text(json.dumps(audit))
            (state / 'forward-image-hold.json').write_text(json.dumps({**hold, 'mode': 'new_images'}))
            current = state / 'forward-image-live-send.json'
            with patch.object(self.forwarder.owner, 'STATE', state), \
                    patch.object(self.forwarder.owner, 'read_json', side_effect=lambda path: json.loads(path.read_text())), \
                    patch.object(self.forwarder.owner, 'owner_active', return_value=True):
                for enabled in (False, True):
                    for status in ('submitted', 'unknown', 'needs_review'):
                        current.write_text(json.dumps({'state': status}))
                        with self.subTest(enabled=enabled, state=status), self.assertRaises(Exception) as caught:
                            self.forwarder.image_send_preflight({**settings, 'imageOutputEnabled': enabled})
                        self.assertEqual(getattr(caught.exception, 'stage', None), 'previous_image_send_requires_review')

    def test_new_unknown_image_cannot_be_hidden_by_an_old_text_only_hold(self):
        settings, audit, hold = self.image_hold()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'forward-image-send.json').write_text(json.dumps(audit))
            hold_path = state / 'forward-image-hold.json'
            hold_path.write_text(json.dumps(hold))
            (state / 'forward-image-live-send.json').write_text(json.dumps({'state': 'unknown'}))
            with patch.object(self.forwarder.owner, 'STATE', state), \
                    patch.object(self.forwarder.owner, 'read_json', side_effect=lambda path: json.loads(path.read_text())), \
                    patch.object(self.forwarder.owner, 'owner_active', return_value=True):
                with self.assertRaises(Exception) as caught:
                    self.forwarder.image_send_preflight(settings)
                self.assertEqual(getattr(caught.exception, 'stage', None), 'previous_image_send_requires_review')
                # Even after a future review settles the old image and removes
                # its hold, a newer unknown image must still stop startup.
                (state / 'forward-image-send.json').write_text(json.dumps({'state': 'accepted'}))
                hold_path.unlink()
                with self.assertRaises(Exception) as caught:
                    self.forwarder.image_send_preflight(settings)
                self.assertEqual(getattr(caught.exception, 'stage', None), 'previous_image_send_requires_review')

    def test_image_output_enable_preserves_an_explicit_resume_hold(self):
        spec = importlib.util.spec_from_file_location('resumed_image_formal', ROOT / 'scripts/run_forwarder.py')
        formal = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(formal)
        _, audit, hold = self.image_hold()
        settings = formal.configuration.validate({'schemaVersion': 1, 'privateContact': '测试联系人',
            'groupName': '测试群', 'groupPrefix': '/ai ', 'groupWakeMode': 'mention_or_prefix',
            'runtimeSeconds': 600, 'sendIntervalSeconds': 5, 'segmentBytes': 8192,
            'imageInputEnabled': False, 'groupContextEnabled': False,
            'imageOutputEnabled': False, 'allGroupsEnabled': True})
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            original = state / 'forward-image-send.json'
            original.write_text(json.dumps(audit))
            hold_path = state / 'forward-image-hold.json'
            hold_path.write_text(json.dumps({**hold, 'mode': 'new_images'}))
            config = state / 'config.json'
            config.write_text(json.dumps(settings))
            before = (original.read_bytes(), hold_path.read_bytes())
            with patch.object(formal, 'load_config'), patch.object(formal.os, 'geteuid', return_value=0), \
                    patch.object(formal, 'CONFIG', config), patch.object(formal.core, 'SETTINGS', settings), \
                    patch.object(formal.core.owner, 'STATE', state), \
                    patch.object(formal.core.owner, 'read_json', side_effect=lambda path: json.loads(path.read_text())), \
                    patch.object(formal.core.owner, 'write_json', side_effect=lambda path, value: path.write_text(json.dumps(value))), \
                    patch.object(formal.core.owner, 'owner_active', return_value=True), \
                    patch.object(formal.core.owner, 'wire'), patch.object(formal.os, 'chown'), \
                    patch.object(sys, 'argv', ['run_forwarder.py', 'image-output', 'enable']):
                self.assertEqual(formal.main(), 0)
            self.assertTrue(json.loads(config.read_text())['imageOutputEnabled'])
            self.assertEqual((original.read_bytes(), hold_path.read_bytes()), before)

    def test_image_hold_does_not_cover_another_action_source_or_destination(self):
        settings, audit, hold = self.image_hold()
        for key, value in (('key', 'e' * 32), ('sourceSha256', 'e' * 64),
                           ('destinationSha256', 'e' * 64), ('requestId', 'e' * 32),
                           ('state', 'submitted'), ('code', 'image_timeout')):
            with self.subTest(field=key):
                self.assertFalse(self.forwarder.held_image_allowed(settings, {**audit, key: value}, hold))

    def test_image_hold_rejects_missing_malformed_and_stale_permissions(self):
        settings, audit, hold = self.image_hold()
        for candidate in (None, {}, {**hold, 'extra': True}, {**hold, 'schemaVersion': True},
                          {**hold, 'mode': 'all_sends'}, {**hold, 'mode': []}, {**hold, 'nativeClientPid': 0},
                          {**hold, 'nativeClientTicks': True}, {**hold, 'approvedAt': 0},
                          {**hold, 'jobId': 'job_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'}):
            with self.subTest(hold=candidate):
                self.assertFalse(self.forwarder.held_image_allowed(settings, audit, candidate))

    def test_image_output_cannot_be_enabled_while_hold_exists(self):
        spec = importlib.util.spec_from_file_location('held_image_formal', ROOT / 'scripts/run_forwarder.py')
        formal = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(formal)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'forward-image-hold.json').write_text('{}')
            with patch.object(formal, 'load_config'), patch.object(formal.os, 'geteuid', return_value=0), \
                    patch.object(formal.core.owner, 'STATE', state), \
                    patch.object(sys, 'argv', ['run_forwarder.py', 'image-output', 'enable']), \
                    patch.object(formal.core.owner, 'write_json', side_effect=AssertionError('must not change config')):
                with self.assertRaises(Exception) as caught:
                    formal.main()
            self.assertEqual(getattr(caught.exception, 'stage', None), 'quarantined_image_requires_review')

    def test_held_session_never_restarts_native_if_owner_exits(self):
        with patch.object(self.forwarder.owner, 'owner_active', side_effect=[True, False]), \
                patch.object(self.forwarder.owner, 'start_owner', side_effect=AssertionError('no restart')) as start, \
                patch.object(self.forwarder.trial, 'ensure_session', side_effect=AssertionError('no restore')), \
                patch.object(self.forwarder.trial, 'fresh_status', return_value={'connected': True}):
            with self.assertRaises(Exception) as caught:
                self.forwarder.ensure_saved_session({'imageOutputEnabled': False}, image_hold=True)
        self.assertEqual(getattr(caught.exception, 'stage', None), 'held_native_instance_unavailable')
        start.assert_not_called()

    def test_promote_cannot_touch_services_while_hold_exists(self):
        spec = importlib.util.spec_from_file_location('held_image_promote', ROOT / 'scripts/run_forwarder.py')
        formal = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(formal)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'forward-image-hold.json').write_text('{}')
            with patch.object(formal, 'load_config'), patch.object(formal.os, 'geteuid', return_value=0), \
                    patch.object(formal.core.owner, 'STATE', state), \
                    patch.object(sys, 'argv', ['run_forwarder.py', 'promote']), \
                    patch.object(formal, 'command', side_effect=AssertionError('must not touch service')) as command:
                with self.assertRaises(Exception) as caught:
                    formal.main()
            self.assertEqual(getattr(caught.exception, 'stage', None), 'quarantined_image_requires_review')
            command.assert_not_called()

    def test_large_image_chunks_finish_within_bounded_polling_budget(self):
        # Model a throttled broker getting ten polling turns per second. One
        # acknowledged chunk is in flight; its full frame must cross the broker
        # without spending the existing 40-second assembly deadline on reads.
        read_fd, sender = os.pipe()
        receiver = os.fdopen(read_fd, 'rb', buffering=0)
        os.set_blocking(sender, False)
        if hasattr(self.forwarder.fcntl, 'F_SETPIPE_SZ'):
            self.forwarder.fcntl.fcntl(sender, self.forwarder.fcntl.F_SETPIPE_SZ, 8192)
        selector = selectors.DefaultSelector()
        selector.register(receiver, selectors.EVENT_READ, 'stdout')
        forwarder = self.forwarder
        broker = forwarder.Duplex.__new__(forwarder.Duplex)
        broker.pending = bytearray()
        broker.selector = selector
        broker.output = {'stdout': bytearray(), 'stderr': bytearray()}
        turns = 0
        image_bytes = 1812155
        chunk_bytes = 64 * 1024
        try:
            for index, offset in enumerate(range(0, image_bytes, chunk_bytes)):
                size = min(chunk_bytes, image_bytes - offset)
                frame = {'kind': 'image_chunk', 'requestId': 'a' * 32, 'index': index,
                         'data': base64.b64encode(b'x' * size).decode('ascii')}
                raw = forwarder.wire.encode(frame)
                sent = 0
                received = []
                while not received and turns < 400:
                    if sent < len(raw):
                        try:
                            sent += os.write(sender, raw[sent:])
                        except BlockingIOError:
                            pass
                    received = broker.tick()
                    turns += 1
                self.assertTrue(received, 'Image transfer exhausted its polling budget')
                self.assertEqual(received, [frame])
            self.assertLess(turns, 400)
            self.assertEqual(broker.output['stdout'], bytearray())
        finally:
            selector.close()
            receiver.close()
            os.close(sender)


if __name__ == '__main__':
    unittest.main()
