"""Pinned 1.2.0 voice jobs with synthetic HTTP responses only."""
import base64
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
import uuid

from wechat_bridge import voice_output
from test_onebot_voice_output import wav, segment

spec = importlib.util.spec_from_file_location('voice_native_test', Path(__file__).resolve().parents[1] / 'scripts/voice_send_native.py')
native = importlib.util.module_from_spec(spec); sys.modules[spec.name] = native; spec.loader.exec_module(native)
SOURCE, JOB = 'upload_' + str(uuid.uuid4()), 'job_' + str(uuid.uuid4())
PREFIX = '/v1/accounts/wechat-primary'


class Clock:
    now = 100.
    def __call__(self): return self.now
    def sleep(self, delay): self.now += delay


class Fake:
    def __init__(self, audio, destination='peer'):
        self.audio = audio
        self.destination = destination
        self.sender = 'self-user'
        self.calls = []
        samples = (audio['bytes'] - 44) // 2
        notes = (samples + 959999) // 960000
        part = (samples + notes - 1) // notes
        self.chunks = [{'index': i, 'state': 'accepted', 'durationMs': min(part, samples - i * part) * 1000 // 16000,
                        'serverMessageId': str(101 + i)} for i in range(notes)]
        self.job = {'id': JOB, 'kind': 'send', 'accountId': 'wechat-primary', 'threadId': destination, 'sourceId': SOURCE,
            'state': 'accepted', 'phase': 'completed', 'serverMessageId': self.chunks[-1]['serverMessageId'],
            'result': {'chunks': deepcopy(self.chunks), 'nativeReceipt': {'status': 'server_accepted', 'serviceCode': 0,
                'clientIdMatches': True, 'serverMessageId': self.chunks[-1]['serverMessageId']},
                'profileProjection': 'complete', 'officialMediaWriteback': 'complete',
                'messageIds': ['projected-' + str(i) for i in range(notes)]}}
        self.asset_change = {}
    def request(self, method, path, *, timeout, headers=None, body=None):
        self.calls.append((method, path, headers, body))
        status = 200
        if path.endswith('/uploads'):
            status = 201
            value = {'id': SOURCE, 'name': 'bridge-voice.wav', 'mediaType': 'audio/wav',
                     'byteLength': self.audio['bytes'], 'sha256': self.audio['sha256']}
        elif path.endswith('/sends'):
            status = 202
            assert json.loads(body) == {'kind': 'voice', 'threadId': self.destination, 'sourceId': SOURCE}
            value = self.job
        elif '/jobs/' in path:
            value = self.job
        else:
            media = path.endswith('/media')
            index = int(path.split('/projected-')[1].split('/')[0])
            identity = 'projected-' + str(index)
            if media:
                value = {'items': [{'accountId': 'wechat-primary', 'messageId': identity, 'kind': 'voice',
                    'variant': variant, 'actualVariant': variant, 'state': 'available', 'officialWriteback': 'complete',
                    'contentType': mime, 'byteLength': length, 'sha256': 'a' * 64, **self.asset_change}
                    for variant, mime, length in [('encoded', 'audio/silk', 100), ('normal', 'audio/wav', 32044)]]}
            else:
                value = {'id': identity, 'accountId': 'wechat-primary', 'threadId': self.destination, 'senderId': self.sender,
                    'direction': 'outgoing', 'type': 34, 'serverId': str(101 + index)}
        return native.shared.Response(status, {'content-type': 'application/json'}, json.dumps(value).encode())


class VoiceNative(unittest.TestCase):
    def build(self, seconds=1):
        audio = voice_output.from_segment(segment(wav(8000, frames=8000 * seconds)))
        fake, clock = Fake(audio), Clock()
        client = native.Client('wechat-primary', 'synthetic-token', 'peer', audio, 'b' * 32,
            self_sender_id='self-user', transport=fake, clock=clock, sleep=clock.sleep)
        return client, fake
    def test_one_or_multiple_native_voice_notes(self):
        for seconds in (1, 61, 121):
            client, fake = self.build(seconds)
            result = client.run(150.)
            self.assertTrue(result.ok, result.code)
            self.assertTrue(result.server_confirmed)
            self.assertEqual(len([c for c in fake.calls if c[0] == 'POST']), 2)
            self.assertEqual(len([c for c in fake.calls if c[1].endswith('/media')]), len(fake.chunks))
    def test_mismatched_or_partial_receipts_are_never_success(self):
        for mutate in (lambda j: j['result']['chunks'][0].update(state='needs_review'),
                       lambda j: j['result']['chunks'][0].update(durationMs=999),
                       lambda j: j['result']['nativeReceipt'].update(clientIdMatches=False),
                       lambda j: j.update(threadId='other'),
                       lambda j: j['result'].update(messageIds=[])):
            client, fake = self.build()
            mutate(fake.job)
            result = client.run(101.)
            self.assertFalse(result.ok)
            self.assertTrue(result.submitted)
            self.assertFalse(client.run(150.).ok)
            self.assertEqual(len([c for c in fake.calls if c[1].endswith('/sends')]), 1)
    def test_wrong_asset_is_not_accepted(self):
        client, fake = self.build()
        for change in ({'kind': 'image'}, {'officialWriteback': 'failed'}, {'byteLength': 1}):
            client, fake = self.build()
            fake.asset_change = change
            self.assertFalse(client.run(101.).ok)
    def test_early_failure_is_known_but_partial_voice_is_unknown(self):
        client, fake = self.build(61)
        fake.job.update(state='failed', phase='preparing', serverMessageId=None)
        fake.job['result'] = {'chunks': []}
        self.assertFalse(client.run(150.).submitted)
        client, fake = self.build(61)
        fake.job.update(state='failed', phase='preparing', serverMessageId=None)
        fake.job['result']['nativeReceipt'] = None
        self.assertTrue(client.run(150.).submitted)
