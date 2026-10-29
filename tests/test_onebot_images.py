import base64
from dataclasses import asdict
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from wechat_bridge import image_input as images
from wechat_bridge.continuous import ImageMessage, TextBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.forward_wire import encode, decode, WireError
from wechat_bridge.forward_config import DEFAULT, validate
from wechat_bridge.protocol import Config

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5P8AAAAASUVORK5CYII=')
SPEC = importlib.util.spec_from_file_location('test_image_native', Path(__file__).resolve().parents[1] / 'scripts/image_native.py')
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class ImageContract(unittest.TestCase):
    def test_bytes_are_preserved_in_onebot_and_pipe(self):
        image = images.payload(PNG, 'image/png', 'thumbnail')
        self.assertEqual(images.validate(image), image)
        segment = images.segment(image)
        self.assertEqual(base64.b64decode(segment['data']['file'][9:]), PNG)
        self.assertEqual(decode(encode({'image': image}))['image'], image)

    def test_bad_mime_size_dimensions_and_digest_are_rejected(self):
        image = images.payload(PNG, 'image/png', 'thumbnail')
        for changes in ({'mime': 'text/html'}, {'bytes': True}, {'bytes': images.MAX_IMAGE + 1},
                        {'sha256': 'a' * 64}, {'base64': image['base64'][:-1] + '!'},
                        {'base64': 'https://not-permitted'}, {'quality': 'best'}, {'path': '/etc/passwd'}):
            with self.subTest(changes=changes), self.assertRaises(images.ImageError):
                images.validate({**image, **changes})
        huge = PNG[:16] + (65535).to_bytes(4, 'big') * 2 + PNG[24:]
        for raw, mime in ((huge, 'image/png'), (PNG, 'image/jpeg'), (b'', 'image/png'), (b'x' * (images.MAX_IMAGE + 1), 'image/png')):
            with self.assertRaises(images.ImageError):
                images.payload(raw, mime, 'standard')

    def test_old_configuration_keeps_images_disabled(self):
        old = {k: v for k, v in DEFAULT.items() if k != 'imageInputEnabled'}
        self.assertFalse(validate(old)['imageInputEnabled'])
        with self.assertRaises(ValueError):
            validate({**DEFAULT, 'imageInputEnabled': 1})

    def test_full_bounded_image_and_question_fit_existing_frame(self):
        raw = PNG + b'\0' * (images.MAX_IMAGE - len(PNG))
        value = {'image': images.payload(raw, 'image/png', 'standard'), 'body': '\n' * 32768}
        self.assertEqual(decode(encode(value)), value)


class NativeMediaPolicy(unittest.TestCase):
    def setUp(self):
        self.client = native.Client('wechat-primary', 'synthetic-token')
        self.asset = {'id': 'asset_' + 'a' * 40, 'accountId': 'wechat-primary', 'messageId': 'source',
                      'kind': 'image', 'variant': 'thumbnail', 'actualVariant': 'thumbnail', 'state': 'available',
                      'byteLength': len(PNG), 'contentType': 'image/png', 'sha256': hashlib.sha256(PNG).hexdigest()}
        self.calls = []
        def request(method, path, deadline, body=None, *, binary=False):
            self.calls.append((method, path, body))
            if binary:
                return PNG, 'image/png'
            if path.endswith('/media'):
                return {'items': [self.asset]}
            return self.asset
        self.client.request = request

    def test_cached_image_needs_only_fixed_native_gets(self):
        image = self.client.read('source', time.monotonic() + 1)
        self.assertEqual(image['sha256'], self.asset['sha256'])
        self.assertTrue(all(method == 'GET' and path.startswith('/v1/accounts/wechat-primary/') for method, path, _ in self.calls))

    def test_cross_account_message_digest_and_size_cannot_be_substituted(self):
        for changes in ({'accountId': 'other'}, {'messageId': 'other'}, {'sha256': '0' * 64},
                        {'byteLength': 999}, {'byteLength': images.MAX_IMAGE + 1}, {'id': '../../bad'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.client.content({**self.asset, **changes}, 'source', time.monotonic() + 1)

    def test_pending_originals_are_never_downloaded(self):
        self.asset.update(variant='original', state='pending', actualVariant=None, byteLength=None)
        with self.assertRaises(native.ImageError):
            self.client.read('source', time.monotonic() + 1)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))

    def test_thumbnail_job_is_bound_to_exact_asset(self):
        self.asset.update(state='pending', actualVariant=None, byteLength=None)
        job_id = 'job_11111111-2222-4333-8444-555555555555'
        def request(method, path, deadline, body=None, *, binary=False):
            self.calls.append((method, path, body))
            if path.endswith('/media'):
                return {'items': [self.asset]}
            if path.endswith('/media-transfers'):
                self.assertEqual(body, {'messageId': 'source', 'assetId': self.asset['id'], 'quality': 'thumbnail'})
                return {'id': job_id, 'accountId': 'other-account', 'kind': 'download', 'assetId': self.asset['id']}
            return {}
        self.client.request = request
        with self.assertRaisesRegex(native.ImageError, 'image_job_binding'):
            self.client.read('source', time.monotonic() + 1)
        self.assertEqual(self.calls[-1][1], '/v1/accounts/wechat-primary/jobs/' + job_id + '/cancel')

    def test_one_thumbnail_job_downloads_then_verifies_original_bytes(self):
        completed = dict(self.asset)
        self.asset.update(state='pending', actualVariant=None, byteLength=None)
        job = {'id': 'job_11111111-2222-4333-8444-555555555555', 'accountId': 'wechat-primary',
               'kind': 'download', 'assetId': self.asset['id'], 'state': 'queued', 'completedBytes': 0, 'totalBytes': None}
        def request(method, path, deadline, body=None, *, binary=False):
            self.calls.append((method, path, body))
            if binary:
                return PNG, 'image/png'
            if path.endswith('/media'):
                return {'items': [self.asset]}
            if path.endswith('/media-transfers'):
                return job
            if '/jobs/' in path:
                return {**job, 'state': 'available'}
            return completed
        self.client.request = request
        with patch.object(native.time, 'sleep'):
            self.assertEqual(self.client.read('source', time.monotonic() + 1)['sha256'], completed['sha256'])
        self.assertEqual(len([r for r in self.calls if r[0] == 'POST']), 1)
        self.assertEqual(self.calls[1][2]['quality'], 'thumbnail')

    def test_http_size_header_rejects_before_reading_body(self):
        response = Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: str(images.MAX_IMAGE + 1) if name == 'Content-Length' else default
        connection = Mock()
        connection.getresponse.return_value = response
        client = native.Client('wechat-primary', 'synthetic-token')
        with patch.object(native.http.client, 'HTTPConnection', return_value=connection), self.assertRaisesRegex(native.ImageError, 'image_response_size'):
            client.request('GET', '/v1/accounts/wechat-primary/media/asset/content', time.monotonic() + 1, binary=True)
        response.read.assert_not_called()
        connection.close.assert_called_once()

    def test_quoted_image_uses_native_message_embedded_media_without_retired_route(self):
        value = {'id': 'question/1', 'accountId': 'wechat-primary', 'threadId': 'peer',
                 'type': 49, 'appMessageType': 57,
                 'quote': {'type': 3, 'serverId': '12345', 'state': 'embedded', 'embeddedImage': True}}
        calls = []
        def request(method, path, deadline, **kwargs):
            self.assertEqual((method, path), ('GET', '/v1/accounts/wechat-primary/messages/question%2F1'))
            calls.append((method, path))
            return value
        self.client.request = request
        self.assertEqual(self.client.quoted_source('question/1', 'peer', time.monotonic() + 1), 'question/1')
        self.assertEqual(calls, [('GET', '/v1/accounts/wechat-primary/messages/question%2F1')])

    def test_quoted_image_rejects_foreign_missing_recalled_or_nonimage_parent(self):
        value = {'id': 'question', 'accountId': 'wechat-primary', 'threadId': 'peer',
                 'type': 49, 'appMessageType': 57,
                 'quote': {'type': 3, 'serverId': '12345', 'state': 'embedded', 'embeddedImage': True}}
        candidates = [{**value, 'id': 'other'}, {**value, 'accountId': 'other'},
                      {**value, 'threadId': 'other@chatroom'}, {**value, 'type': 3},
                      {**value, 'appMessageType': 1}, {**value, 'quote': None}]
        for change in ({'type': 47}, {'type': True}, {'state': 'missing'}, {'state': 'recalled'},
                       {'state': 'resolved'}, {'embeddedImage': False}, {'embeddedImage': 1}):
            candidates.append({**value, 'quote': {**value['quote'], **change}})
        for candidate in candidates:
            with self.subTest(value=candidate):
                self.client.request = lambda *a, **k: candidate
                with self.assertRaisesRegex(native.ImageError, 'image_quote_binding'):
                    self.client.quoted_source('question', 'peer', time.monotonic() + 1)


class ImageRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Spool(Path(self.directory.name) / 'images.sqlite')
        self.emitted = []
        self.gateway = Gateway('account', 'peer', self.state, 'profile', self.emitted.append,
                               group='room@chatroom', self_sender_id='self', image_input=True)
        self.core = TextBridge(Config('account', 'bot', 'room@chatroom', 'group'), 'peer', self.state, self.gateway,
                               self_sender_id='self', image_input=True)
        self.core.connected()

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    def message(self, **changes):
        value = dict(account='account', id='source', room_id='peer', sender_id='peer', body='',
                     timestamp=int(time.time()), type=3, image=images.payload(PNG, 'image/png', 'thumbnail'))
        return ImageMessage(**{**value, **changes})

    async def deliver(self, message):
        self.gateway.feed({'kind': 'event', 'cursor': '1', 'data': asdict(message)})
        received = await self.gateway.next_message()
        event = self.core.ingest(received)
        await self.core.event_delivered(received, True)
        return event

    async def test_private_image_reaches_onebot_intact_and_payload_is_removed(self):
        event = await self.deliver(self.message())
        self.assertEqual(event['message_type'], 'private')
        self.assertEqual(event['message'][0]['type'], 'image')
        self.assertEqual(base64.b64decode(event['message'][0]['data']['file'][9:]), PNG)
        self.assertEqual(self.state.counts(), {'forwarded': 1})
        self.assertIsNone(self.state.db.execute('SELECT payload FROM inbox').fetchone()[0])
        report = next(r for r in self.emitted if r.get('stage') == 'handoff')
        self.assertEqual(report['image_sha256'], hashlib.sha256(PNG).hexdigest())
        self.assertNotIn('base64', str(report))

    async def test_group_image_and_question_have_one_source_and_route(self):
        event = await self.deliver(self.message(room_id='room@chatroom', body='/ai 图片里是什么？', type=1))
        self.assertEqual([s['type'] for s in event['message']], ['at', 'text', 'image'])
        self.assertEqual(event['message'][1]['data']['text'], '图片里是什么？')
        self.assertEqual(event['raw_message'], '/ai 图片里是什么？')
        self.assertEqual(self.state.message_route(event['message_id']), 'room@chatroom')

    async def test_group_bare_image_does_not_bypass_wake_rule(self):
        with self.assertRaises((WireError, ValueError)):
            self.gateway.feed({'kind': 'event', 'cursor': '1', 'data': asdict(self.message(room_id='room@chatroom'))})
        self.assertEqual(self.state.counts(), {})

    async def test_own_image_and_disabled_policy_do_not_trigger_model(self):
        for message in (self.message(sender_id='self'), self.message(sender_id='other')):
            with self.assertRaises(WireError):
                self.gateway.feed({'kind': 'event', 'cursor': '1', 'data': asdict(message)})
        self.gateway.image_input = False
        with self.assertRaises(WireError):
            self.gateway.feed({'kind': 'event', 'cursor': '1', 'data': asdict(self.message())})
        self.assertEqual(self.state.counts(), {})

    async def test_repeated_image_event_does_not_reinvoke_model(self):
        message = self.message()
        await self.deliver(message)
        self.gateway.feed({'kind': 'event', 'cursor': '2', 'data': asdict(message)})
        self.assertIsNone(self.state.next_pending())

    async def test_invalid_image_is_not_persisted_or_acknowledged(self):
        message = self.message()
        message.image['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.gateway.feed({'kind': 'event', 'cursor': '1', 'data': asdict(message)})
        self.assertEqual(self.state.counts(), {})
        self.assertFalse(self.emitted)

    async def test_expired_queued_image_does_not_call_model_or_block_next_message(self):
        expired = self.message(timestamp=int(time.time()) - 301)
        fresh = self.message(id='fresh')
        for cursor, message in (('1', expired), ('2', fresh)):
            self.gateway.feed({'kind': 'event', 'cursor': cursor, 'data': asdict(message)})
        received = await self.gateway.next_message()
        self.assertEqual(received.id, 'fresh')
        self.assertEqual(self.state.counts(), {'expired': 1, 'queued': 1})
        self.assertIsNone(self.state.db.execute("SELECT payload FROM inbox WHERE source='source'").fetchone()[0])
        self.assertIn({'kind': 'report', 'stage': 'image_input', 'outcome': 'expired'}, self.emitted)
