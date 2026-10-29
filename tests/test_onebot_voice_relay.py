"""Root relay invariants with fake ownership and no services."""
import base64
import json
import os
from unittest.mock import patch
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from wechat_bridge import voice_output
from test_onebot_voice_output import wav, segment

spec = importlib.util.spec_from_file_location('voice_relay_test', Path(__file__).resolve().parents[1] / 'scripts/voice_forwarding.py')
relay = importlib.util.module_from_spec(spec); sys.modules[spec.name] = relay; spec.loader.exec_module(relay)


class VoiceRelay(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.saved = {}
        def require(ok, stage):
            if not ok: raise RuntimeError(stage)
        self.core = SimpleNamespace(require=require, owner=SimpleNamespace(STATE=Path(self.directory.name),
            write_json=lambda p,v: self.saved.update({str(p): dict(v)}), read_json=lambda p: self.saved[str(p)]),
            reserve_voice_upload=lambda pinned,audio: True)
        self.counters = dict(voice_output_requests=0, voice_output_sent=0, uncertain=0, accepted_parts=0)
        self.subject = relay.Relay(self.core, None, {'voiceOutputEnabled': True}, self.counters)
        self.audio = voice_output.from_segment(segment(wav()))
        self.frames = {'host': [], 'native': []}
        self.peers = {name: SimpleNamespace(send=items.append) for name,items in self.frames.items()}
        self.command = {'kind': 'send_voice', 'requestId': 'a' * 32, 'destination': 'peer', 'key': 'b' * 32, 'voice': self.audio}
    def tearDown(self): self.directory.cleanup()
    def feed(self, name, value, allowed=True):
        return self.subject.handle(name, value, self.peers, allowed=allowed)
    def test_audit_before_forward_and_strict_full_result(self):
        self.feed('host', self.command)
        self.assertEqual(self.subject.audit['state'], 'submitted')
        self.assertTrue(self.subject.pending)
        identity = 'job_' + str(uuid.uuid4())
        self.feed('native', {'kind': 'voice_send_intent', 'requestId': 'a' * 32, 'jobId': identity})
        receipt = {'account': 'wechat-primary', 'destination': 'peer', 'job_id': identity, 'state': 'accepted',
            'server_id': '123', 'source_sha256': self.audio['sha256'], 'projection_verified': True}
        self.feed('native', {'kind': 'native_voice_result', 'requestId': 'a' * 32, 'receipt': receipt,
            'ok': True, 'server_confirmed': True, 'submitted': True, 'code': 'ok'})
        self.assertFalse(self.subject.pending)
        self.assertEqual(self.subject.audit['state'], 'accepted')
        self.assertEqual(self.counters['voice_output_sent'], 1)
    def test_capacity_rejects_before_native_and_route_denied(self):
        self.core.reserve_voice_upload = lambda p,a: False
        self.feed('host', self.command)
        self.assertFalse(self.frames['native'])
        self.assertEqual(self.frames['host'][0]['receipt']['state'], 'rejected')
        with self.assertRaises(RuntimeError): self.feed('host', self.command, allowed=False)
    def test_unknown_stays_audited_and_stops(self):
        self.feed('host', self.command)
        receipt = {'account': 'wechat-primary', 'destination': 'peer', 'job_id': '', 'state': 'unknown',
            'server_id': '', 'source_sha256': self.audio['sha256'], 'projection_verified': False}
        with self.assertRaisesRegex(RuntimeError, 'voice_send_requires_review'):
            self.feed('native', {'kind': 'native_voice_result', 'requestId': 'a' * 32, 'receipt': receipt,
                'ok': False, 'server_confirmed': False, 'submitted': True, 'code': 'voice_response'})
        self.assertEqual(self.subject.audit['state'], 'unknown')
        self.assertEqual(self.counters['uncertain'], 1)
    def test_chunk_ack_order_and_precommit_integrity(self):
        self.command.update(kind='voice_begin', voice=voice_output.metadata(self.audio))
        self.feed('host', self.command)
        with self.assertRaises(RuntimeError):
            self.feed('host', {'kind': 'voice_chunk', 'requestId': 'a'*32, 'index': 0, 'data': self.audio['base64']})
        self.feed('native', {'kind': 'voice_chunk_ack', 'requestId': 'a'*32, 'index': -1})
        self.feed('host', {'kind': 'voice_chunk', 'requestId': 'a'*32, 'index': 0, 'data': self.audio['base64']})
        self.feed('native', {'kind': 'voice_chunk_ack', 'requestId': 'a'*32, 'index': 0})
        self.feed('host', {'kind': 'voice_commit', 'requestId': 'a'*32})
        self.assertEqual(self.frames['native'][-1]['kind'], 'voice_commit')

    def test_unresolved_audit_preflight_is_independent_of_enabled_flag(self):
        path = self.core.owner.STATE / 'forward-voice-send.json'
        path.touch()
        for state in ('submitted', 'unknown', 'needs_review'):
            self.saved[str(path)] = {'state': state}
            with self.assertRaisesRegex(RuntimeError, 'previous_voice_send_requires_review'):
                relay.preflight(self.core)
        for state in ('accepted', 'rejected'):
            self.saved[str(path)] = {'state': state}
            self.assertEqual(relay.preflight(self.core), path)

    def test_serve_refuses_unknown_voice_before_login_or_token_access(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('voice_preflight_core_test', root / 'scripts/run_private_forwarder.py')
        core = importlib.util.module_from_spec(spec); sys.modules[spec.name] = core; spec.loader.exec_module(core)
        directory = self.core.owner.STATE
        (directory / 'forward-voice-send.json').write_text(json.dumps({'state': 'unknown'}))
        descriptors = []
        original_open = os.open
        def opened(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        try:
            with patch.object(core.os, 'geteuid', return_value=0), patch.object(core.owner, 'STATE', directory), \
                    patch.object(core.owner, 'CONTROL', directory), patch.object(core.owner, 'private_directory'), \
                    patch.object(core.owner, 'read_json', side_effect=lambda path: json.loads(path.read_text())), \
                    patch.object(core, 'image_send_preflight', return_value=(directory / 'image.json', None, None)), \
                    patch.object(core, 'ensure_saved_session') as restore, patch.object(core.trial, 'read_onebot_token') as token, \
                    patch.object(core.os, 'open', side_effect=opened):
                with self.assertRaises(Exception) as caught:
                    core.serve()
                self.assertEqual(getattr(caught.exception, 'stage', None), 'previous_voice_send_requires_review', repr(caught.exception))
                restore.assert_not_called()
                token.assert_not_called()
        finally:
            for descriptor in descriptors:
                try: os.close(descriptor)
                except OSError: pass
