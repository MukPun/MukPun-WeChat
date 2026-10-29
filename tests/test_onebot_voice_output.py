"""Synthetic PCM WAV and typed OneBot voice output; no live messages."""
import asyncio
import base64
from dataclasses import asdict
import io
from pathlib import Path
import struct
import tempfile
import unittest
import uuid
import wave
from unittest.mock import patch

from wechat_bridge import voice_output as voice
from wechat_bridge.continuous import TextBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.protocol import Config, NativeMessage


def wav(rate=32000, channels=1, width=2, frames=None):
    frames = rate if frames is None else frames
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as out:
        out.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        out.writeframes(b'\0' * frames * channels * width)
    return stream.getvalue()


def segment(raw):
    return {'type': 'record', 'data': {'file': 'base64://' + base64.b64encode(raw).decode()}}


def receipt(audio, **changes):
    return {'account': 'account', 'destination': 'peer', 'job_id': 'job_' + str(uuid.uuid4()),
            'state': 'accepted', 'server_id': '123', 'source_sha256': audio['sha256'],
            'projection_verified': True, **changes}


class VoiceContract(unittest.TestCase):
    def test_sovits_pcm32k_normalizes_to_native_fast_path(self):
        value = voice.from_segment(segment(wav()))
        raw = base64.b64decode(value['base64'])
        with wave.open(io.BytesIO(raw), 'rb') as out:
            self.assertEqual((out.getframerate(), out.getnchannels(), out.getsampwidth()), (16000, 1, 2))
            self.assertEqual(out.getnframes(), 16000)
        self.assertEqual(value['duration_ms'], 1000)
        self.assertIs(voice.validate(value), value)

    def test_pcm_profiles_and_long_clip(self):
        for rate, channels, width in ((8000, 1, 1), (44100, 2, 2), (48000, 1, 3), (96000, 2, 4)):
            with self.subTest(rate=rate):
                value = voice.from_segment(segment(wav(rate, channels, width)))
                self.assertLessEqual(abs(value['duration_ms'] - 1000), 1)
        value = voice.from_segment(segment(wav(8000, frames=8000 * 61)))
        self.assertEqual(value['duration_ms'], 61000)

    def test_resampling_preserves_nonzero_signal_and_stereo_mix(self):
        for rate, channels, values in ((32000, 1, (4000,)), (44100, 2, (8000, 4000))):
            raw = wav(rate, channels)
            raw = raw[:44] + struct.pack('<' + 'h' * channels, *values) * rate
            result = voice.from_segment(segment(raw))
            pcm = base64.b64decode(result['base64'])[44:]
            samples = struct.unpack('<' + 'h' * (len(pcm) // 2), pcm)
            self.assertTrue(all(value == sum(values) // channels for value in samples))

    def test_five_minute_low_rate_source_fits_normalized_contract(self):
        value = voice.from_segment(segment(wav(8000, frames=8000 * 300)))
        self.assertEqual(value['bytes'], voice.MAX_AUDIO)
        self.assertEqual(value['duration_ms'], 300000)
        self.assertIs(voice.validate(value), value)

    def test_no_file_network_or_extra_options(self):
        for source in ('file:///AstrBot/data/audio.wav', '/private/audio.wav', 'https://example.invalid/audio.wav'):
            with patch('builtins.open', side_effect=AssertionError('file access')), self.assertRaises(voice.OutputError):
                voice.from_segment({'type': 'record', 'data': {'file': source}})
        for data in ({'file': segment(wav())['data']['file'], 'url': 'secret'}, {'file': []}, {'file': 'base64://!!!'}):
            with self.assertRaises(voice.OutputError):
                voice.from_segment({'type': 'record', 'data': data})

    def test_malformed_truncated_empty_float_and_overlong_fail(self):
        floating = bytearray(wav()); floating[20:22] = struct.pack('<H', 3)
        for raw in (b'not audio', wav()[:-1], wav() + b'trailer', wav(frames=0), bytes(floating), wav(8000, frames=8000 * 301)):
            with self.subTest(size=len(raw)), self.assertRaises(voice.OutputError):
                voice.from_segment(segment(raw))

    def test_metadata_and_chunk_integrity(self):
        audio = voice.from_segment(segment(wav(frames=32000 * 5)))
        raw = base64.b64decode(audio['base64'])
        assembly = voice.Assembly(voice.metadata(audio))
        with self.assertRaises(voice.OutputError):
            assembly.add(1, 'YQ==')
        with self.assertRaises(voice.OutputError):
            assembly.finish()
        for index, offset in enumerate(range(0, len(raw), voice.CHUNK_BYTES)):
            assembly.add(index, base64.b64encode(raw[offset:offset + voice.CHUNK_BYTES]).decode())
        self.assertEqual(assembly.finish(), audio)
        with self.assertRaises(voice.OutputError):
            voice.validate({**audio, 'duration_ms': 1})
        with self.assertRaises(voice.OutputError):
            voice.validate({**audio, 'sha256': '0' * 64})


class Backend:
    online = True
    def __init__(self):
        self.calls = []
        self.reports = []
    def report(self, stage, **fields):
        self.reports.append((stage, fields))
    async def send_voice(self, destination, audio, key):
        self.calls.append((destination, audio, key))
        return receipt(audio, destination=destination)


class VoiceBridge(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Spool(Path(self.directory.name) / 'state.sqlite')
        self.backend = Backend()
        self.bridge = TextBridge(Config('account', 'self', 'room@chatroom', 'test', send_interval=0),
            'peer', self.state, self.backend, voice_output=True, self_sender_id='self-user')
        self.bridge.connected()
        incoming = NativeMessage('account', 'incoming-voice-test', 'peer', 'peer', 'hello', 100)
        self.state.admit(incoming.id, asdict(incoming))
        self.state.next_pending()
        self.bridge.ingest(incoming)
        self.request = {'action': 'send_private_msg', 'params': {'user_id': self.bridge.peer_id,
            'message': [segment(wav())]}, 'echo': 51}
    def tearDown(self):
        self.state.close(); self.directory.cleanup()
    async def test_record_action_and_persistent_dedup(self):
        first = await self.bridge.action(self.request)
        self.assertEqual(first['retcode'], 0)
        self.assertEqual(await self.bridge.action(self.request), first)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual((await self.bridge.action({'action': 'can_send_record'}))['data'], {'yes': True})
    async def test_disabled_and_mixed_segments_reject_without_submission(self):
        self.bridge.voice_output = False
        self.assertEqual((await self.bridge.action(self.request))['retcode'], 1400)
        self.bridge.voice_output = True
        self.request['params']['message'].append({'type': 'text', 'data': {'text': 'caption'}})
        self.assertEqual((await self.bridge.action(self.request))['retcode'], 1400)
        self.assertFalse(self.backend.calls)
    async def test_unknown_receipt_persists_and_is_not_retried(self):
        async def unknown(destination, audio, key):
            self.backend.calls.append(key)
            return receipt(audio, state='needs_review', projection_verified=False)
        self.backend.send_voice = unknown
        result = await self.bridge.action(self.request)
        self.assertEqual(result['retcode'], 1408)
        self.assertEqual(await self.bridge.action(self.request), result)
        self.assertEqual(len(self.backend.calls), 1)
    async def test_gateway_chunked_transfer_and_bound_receipt(self):
        frames = []
        gateway = Gateway('account', 'peer', self.state, 'generation', frames.append, voice_output=True)
        audio = voice.from_segment(segment(wav(frames=32000 * 5)))
        task = asyncio.create_task(gateway.send_voice('peer', audio, 'c' * 32))
        processed = 0
        while not task.done():
            await asyncio.sleep(0)
            while processed < len(frames):
                frame = frames[processed]; processed += 1
                if frame['kind'] in ('voice_begin', 'voice_chunk'):
                    gateway.feed({'kind': 'voice_chunk_ack', 'requestId': frame['requestId'], 'index': frame.get('index', -1)})
                elif frame['kind'] == 'voice_commit':
                    gateway.feed({'kind': 'voice_receipt', 'requestId': frame['requestId'], 'receipt': receipt(audio)})
        self.assertEqual((await task)['state'], 'accepted')
        self.assertEqual(frames[0]['kind'], 'voice_begin')
