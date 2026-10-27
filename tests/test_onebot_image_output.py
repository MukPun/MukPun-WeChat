"""Bounded static image output and real typed IPC receipt semantics."""
import asyncio
import base64
from dataclasses import asdict
import hashlib
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import zlib

from wechat_bridge.continuous import TextBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.forward_wire import WireError, encode, decode, MAX_FRAME
from wechat_bridge.protocol import Config, NativeMessage
from wechat_bridge import image_output as images


def chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)


def png(width=2, height=2, *, extra=b'', compressed=None):
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    pixels = b''.join(b'\0' + b'\xff\0\0\xff' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + extra
            + chunk(b'IDAT', zlib.compress(pixels) if compressed is None else compressed) + chunk(b'IEND', b''))


JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/'
    '2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAACAAIDASIAAhEBAxEB/'
    '8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2Jy'
    'ggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5'
    'usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcF'
    'BAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hp'
    'anN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMB'
    'AAIRAxEAPwD80/iF4h1XUvH3iW7u9TvLq7uNTuZZp57h3kkdpWLMzE5JJJJJ65ooor+m8B/ulH/DH8kefjf96q/4n+Z//9k=')


def receipt(image, destination='peer', **changes):
    return {'account': 'account', 'destination': destination, 'job_id': 'job_' + str(uuid.uuid4()),
            'state': 'accepted', 'server_id': '123456', 'source_sha256': image['sha256'],
            'projection_verified': True, **changes}


class ImageContract(unittest.TestCase):
    def test_rejection_describes_oversized_png_without_decoding_pixels(self):
        raw = png(1025, 1, compressed=b'not-a-zlib-stream')
        raw += b'x' * (images.MAX_IMAGE + 3 - len(raw))
        segments = [{'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(raw).decode()}}]
        with patch.object(images.zlib, 'decompressobj', side_effect=AssertionError('pixel decoding')):
            value = images.rejection('size', segments)
        self.assertEqual(value, {'reason': 'size', 'format': 'png', 'source_bytes': len(raw),
                                'source_sha256': hashlib.sha256(raw).hexdigest(), 'width': 1025, 'height': 1})
        self.assertIs(images.validate_rejection(value), value)
        with self.assertRaisesRegex(images.OutputError, '^output_image_size$'):
            images.from_segment(segments[0])

    def test_rejection_invalid_base64_and_external_sources_do_not_leak(self):
        empty = {'reason': 'source', 'format': 'unknown', 'source_bytes': 0,
                 'source_sha256': '', 'width': 0, 'height': 0}
        for source in ('base64://private-invalid-!!!', 'base64://\N{SNOWMAN}',
                       'file:///private/meme.png', 'https://private.invalid/meme.png?token=secret'):
            with self.subTest(source=source[:20]), patch('builtins.open', side_effect=AssertionError('file access')):
                value = images.rejection('source', [
                    {'type': 'text', 'data': {'text': 'private text'}},
                    {'type': 'image', 'data': {'file': source, 'private': 'private metadata'}}])
            self.assertEqual(value, empty)

    def test_rejection_stops_at_first_image_and_never_copies_unknown_fields(self):
        segment = {'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(png()).decode()}}
        value = images.rejection('segment', [
            {'type': 'image', 'data': {'file': 'file:///private/path'}}, segment])
        self.assertEqual(value['source_sha256'], '')
        self.assertEqual(value['source_bytes'], 0)
        for segments in (None, 'private string', [{'type': 'text', 'data': {'text': 'private text'}}],
                         [{'type': 'image', 'data': None}]):
            with self.subTest(segments=segments):
                self.assertEqual(images.rejection('segment', segments), value)

    def test_rejection_refuses_to_decode_beyond_diagnostic_budget(self):
        source = 'base64://' + 'A' * (4 * ((images.MAX_DIAGNOSTIC_IMAGE + 2) // 3) + 4)
        with patch.object(images.base64, 'b64decode', side_effect=AssertionError('over-budget decode')):
            value = images.rejection('size', [{'type': 'image', 'data': {'file': source}}])
        self.assertEqual(value['format'], 'unknown')
        self.assertEqual(value['source_bytes'], 0)
        self.assertEqual(value['source_sha256'], '')
        # The last base64 quartet can still decode beyond the raw-byte limit.
        raw = b'x' * (images.MAX_DIAGNOSTIC_IMAGE + 1)
        value = images.rejection('size', [{'type': 'image', 'data': {
            'file': 'base64://' + base64.b64encode(raw).decode()}}])
        self.assertEqual(value['source_bytes'], 0)

    def test_rejection_reads_jpeg_webp_gif_and_unknown_headers(self):
        webp = (b'RIFF' + struct.pack('<I', 18) + b'WEBPVP8X' + struct.pack('<I', 10)
                + b'\0' * 4 + (2).to_bytes(3, 'little') + (3).to_bytes(3, 'little'))
        for raw, image_format, shape in ((JPEG, 'jpeg', (2, 2)), (webp, 'webp', (3, 4)),
                                         (b'GIF89a' + struct.pack('<HH', 5, 6), 'gif', (5, 6)),
                                         (b'private unknown bytes', 'unknown', (0, 0))):
            with self.subTest(format=image_format):
                value = images.rejection('format', [{'type': 'image', 'data': {
                    'file': 'base64://' + base64.b64encode(raw).decode()}}])
                self.assertEqual(value['format'], image_format)
                self.assertEqual((value['width'], value['height']), shape)
                self.assertEqual(value['source_bytes'], len(raw))
                self.assertEqual(value['source_sha256'], hashlib.sha256(raw).hexdigest())

    def test_rejection_bounds_header_dimensions_and_strict_schema(self):
        raw = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 65536, 1, 8, 6, 0, 0, 0)))
        value = images.rejection('dimensions', [{'type': 'image', 'data': {
            'file': 'base64://' + base64.b64encode(raw).decode()}}])
        self.assertEqual((value['width'], value['height']), (0, 1))
        for changes in ({'extra': 'private'}, {'reason': 'private'}, {'reason': []}, {'format': 'bmp'},
                        {'format': []}, {'source_bytes': True}, {'source_bytes': -1},
                        {'source_bytes': images.MAX_DIAGNOSTIC_IMAGE + 1}, {'source_sha256': 'A' * 64},
                        {'source_sha256': 'private'}, {'width': True}, {'width': 65536}, {'height': -1}):
            with self.subTest(changes=changes), self.assertRaises(images.OutputError):
                images.validate_rejection({**value, **changes})
        with self.assertRaises(images.OutputError):
            images.validate_rejection({key: item for key, item in value.items() if key != 'reason'})
        with self.assertRaises(images.OutputError):
            images.rejection('private', [])

    def test_png_interlace_has_a_distinct_reason_without_relaxing_limits(self):
        raw = bytearray(png(1, 1))
        raw[28] = 1
        raw[29:33] = struct.pack('>I', zlib.crc32(raw[12:29]) & 0xffffffff)
        with self.assertRaisesRegex(images.OutputError, '^output_image_interlace$'):
            images.payload(bytes(raw))
        with self.assertRaisesRegex(images.OutputError, '^output_image_limits$'):
            images.payload(png(images.MAX_EDGE + 1, 1))

    def test_png_and_jpeg_are_preserved_and_fit_existing_ipc(self):
        for raw, mime in ((png(), 'image/png'), (JPEG, 'image/jpeg')):
            value = images.payload(raw)
            self.assertEqual(value['mime'], mime)
            self.assertEqual(base64.b64decode(images.validate(value)['base64']), raw)
            frame = encode({'kind': 'send_image', 'requestId': 'a' * 32, 'key': 'b' * 32,
                            'destination': 'peer', 'image': value})
            self.assertLessEqual(len(frame), MAX_FRAME)
            self.assertEqual(decode(frame)['image'], value)

    def test_maximum_image_uses_bounded_ordered_chunks_and_checks_full_hash(self):
        raw = png()
        padding = images.MAX_IMAGE - len(raw) - 12
        raw = png(extra=chunk(b'tEXt', b'Comment\0' + b'x' * (padding - 8)))
        self.assertEqual(len(raw), images.MAX_IMAGE)
        value = images.payload(raw)
        assembly = images.Assembly(images.metadata(value))
        for index, offset in enumerate(range(0, len(raw), images.CHUNK_BYTES)):
            data = base64.b64encode(raw[offset:offset + images.CHUNK_BYTES]).decode()
            frame = {'kind': 'image_chunk', 'requestId': 'a' * 32, 'index': index, 'data': data}
            self.assertLess(len(encode(frame)), MAX_FRAME)
            assembly.add(index, decode(encode(frame))['data'])
        self.assertEqual(assembly.finish(), value)

    def test_chunk_reorder_duplicate_truncation_and_hash_mismatch_are_rejected(self):
        value = images.payload(png())
        assembly = images.Assembly(images.metadata(value))
        for index, data in ((1, value['base64']), (0, 'YQ==')):
            with self.assertRaises(images.OutputError):
                assembly.add(index, data)
        with self.assertRaises(images.OutputError):
            assembly.finish()
        assembly.add(0, value['base64'])
        with self.assertRaises(images.OutputError):
            assembly.add(0, value['base64'])
        wrong = images.Assembly({**images.metadata(value), 'sha256': '0' * 64})
        wrong.add(0, value['base64'])
        with self.assertRaises(images.OutputError):
            wrong.finish()

    def test_static_webp_headers_and_animation_rejection(self):
        def webp(kind, body):
            content = b'WEBP' + kind + struct.pack('<I', len(body)) + body + (b'\0' if len(body) % 2 else b'')
            return b'RIFF' + struct.pack('<I', len(content)) + content
        lossy = webp(b'VP8 ', b'\x10\0\0\x9d\x01\x2a\x02\0\x02\0')
        lossless = webp(b'VP8L', b'\x2f' + struct.pack('<I', 1 | (1 << 14)))
        for raw in (lossy, lossless):
            self.assertEqual(images.dimensions(raw, 'image/webp'), (2, 2))
            self.assertEqual(images.payload(raw)['mime'], 'image/webp')
        for raw in (webp(b'ANIM', b'\0' * 6), lossy[:-1],
                    webp(b'VP8L', b'\x2f' + struct.pack('<I', images.MAX_EDGE))):
            with self.assertRaises(images.OutputError):
                images.payload(raw)

    def test_paths_urls_gif_webp_and_unbounded_base64_are_rejected(self):
        values = ['file:///AstrBot/data/meme.png', 'https://example.invalid/meme.png',
                  'base64://' + base64.b64encode(b'GIF89a' + b'\1\0\1\0').decode(),
                  'base64://' + base64.b64encode(b'RIFF' + b'\0' * 4 + b'WEBP').decode(),
                  'base64://' + base64.b64encode(b'x' * (images.MAX_IMAGE + 1)).decode()]
        for source in values:
            with self.subTest(source=source[:40]), self.assertRaises(images.OutputError):
                images.from_segment({'type': 'image', 'data': {'file': source}})

    def test_png_crc_animation_dimension_and_decompression_limits(self):
        bad_crc = bytearray(png())
        bad_crc[-1] ^= 1
        samples = [bytes(bad_crc), png(images.MAX_EDGE + 1, 1), png(extra=chunk(b'acTL', struct.pack('>II', 2, 0))),
                   png(1, 1, compressed=zlib.compress(b'\0' * 1000000))]
        for raw in samples:
            with self.subTest(length=len(raw)), self.assertRaises(images.OutputError):
                images.payload(raw)

    def test_sha_mime_count_quality_and_canonical_base64_bind_payload(self):
        value = images.payload(png())
        for changes in ({'sha256': 'a' * 64}, {'mime': 'image/jpeg'}, {'bytes': True},
                        {'quality': 'thumbnail'}, {'base64': value['base64'] + '\n'}):
            with self.subTest(changes=changes), self.assertRaises(images.OutputError):
                images.validate({**value, **changes})

    def test_common_meme_dimensions_are_valid_and_pixel_budget_is_still_enforced(self):
        for width, height in ((960, 1280), (1254, 1254), (1280, 1214)):
            raw = png(width, height)
            self.assertEqual(images.dimensions(raw, 'image/png'), (width, height))
        with self.assertRaises(images.OutputError):
            images.payload(png(2048, 2048))

    def test_streamed_png_checks_row_filters_across_output_chunk_boundaries(self):
        width, height = 1254, 1254
        rows = (b'\0' + b'\xff\0\0\xff' * width) * height
        header = chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
        packed = zlib.compress(rows)
        raw = b'\x89PNG\r\n\x1a\n' + header + chunk(b'IDAT', packed[:9]) + chunk(b'IDAT', packed[9:]) + chunk(b'IEND', b'')
        self.assertEqual(images.dimensions(raw, 'image/png'), (width, height))
        bad = bytearray(rows)
        bad[700 * (width * 4 + 1)] = 5
        raw = b'\x89PNG\r\n\x1a\n' + header + chunk(b'IDAT', zlib.compress(bad)) + chunk(b'IEND', b'')
        with self.assertRaises(images.OutputError):
            images.payload(raw)

    def test_receipt_requires_media_job_projection_and_bound_hash(self):
        image = images.payload(png())
        good = receipt(image)
        self.assertTrue(images.accepted(good, 'account', 'peer', image['sha256']))
        for changes in ({'account': 'other'}, {'destination': 'other'}, {'source_sha256': 'b' * 64},
                        {'job_id': str(uuid.uuid4())}, {'server_id': '0'}, {'server_id': str(2**64)},
                        {'projection_verified': False}, {'projection_verified': 1}, {'state': 'needs_review'}):
            self.assertFalse(images.accepted({**good, **changes}, 'account', 'peer', image['sha256']))


class ImageActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'state.sqlite'
        self.state = Spool(self.path)
        self.frames, self.sent = [], []
        self.gateway = Gateway('account', 'peer', self.state, 'profile', self.frames.append,
                               group='room@chatroom', image_output=True)
        self.core = TextBridge(Config('account', 'self', 'room@chatroom', 'test', send_interval=0),
            'peer', self.state, self.gateway, image_output=True)
        self.core.connected()
        async def send(destination, image, key):
            self.sent.append((destination, image, key))
            return receipt(image, destination)
        self.gateway.send_image = send
        self.value = images.payload(png())
        self.segment = {'type': 'image', 'data': {'file': 'base64://' + self.value['base64']}}

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def event(self, group=False):
        message = NativeMessage('account', 'group-source' if group else 'private-source',
            'room@chatroom' if group else 'peer', 'member' if group else 'peer',
            '/ai test' if group else 'test', int(time.time()))
        self.gateway.feed({'kind': 'event', 'cursor': '1', 'anchor': 'a' * 64, 'data': asdict(message)})
        message = await asyncio.wait_for(self.gateway.next_message(), 2)
        event = self.core.ingest(message)
        await self.core.event_delivered(message, True)
        return event

    def action(self, event, segments=None, echo=86):
        group = event['message_type'] == 'group'
        params = {'group_id': event['group_id']} if group else {'user_id': event['user_id']}
        return {'action': 'send_group_msg' if group else 'send_private_msg',
                'params': {**params, 'message': [self.segment] if segments is None else segments}, 'echo': {'seq': echo}}

    async def test_private_image_returns_only_verified_media_receipt(self):
        event = await self.event()
        result = await self.core.action(self.action(event))
        self.assertEqual(result['retcode'], 0)
        self.assertEqual(result['echo'], {'seq': 86})
        self.assertEqual(self.sent[0][:2], ('peer', self.value))

    async def test_group_image_quote_is_bound_to_same_group(self):
        event = await self.event(True)
        result = await self.core.action(self.action(event, [
            {'type': 'reply', 'data': {'id': str(event['message_id'])}}, self.segment]))
        self.assertEqual(result['retcode'], 0)
        self.assertEqual(self.sent[0][0], 'room@chatroom')

    async def test_disabled_output_does_not_advertise_capability(self):
        self.assertEqual((await self.core.action({'action': 'can_send_image'}))['data'], {'yes': True})
        self.core.image_output = False
        self.assertEqual((await self.core.action({'action': 'can_send_image'}))['data'], {'yes': False})
        event = await self.event()
        self.assertEqual((await self.core.action(self.action(event)))['message'], 'unsupported_message_segment')
        self.assertFalse(self.sent)

    async def test_text_mix_multiple_images_and_foreign_quote_never_partial_send(self):
        group = await self.event(True)
        private = await self.event()
        values = [[{'type': 'text', 'data': {'text': 'must-not-send'}}, self.segment],
                  [self.segment, self.segment],
                  [{'type': 'reply', 'data': {'id': group['message_id']}}, self.segment]]
        for index, segments in enumerate(values):
            result = await self.core.action(self.action(private, segments, index))
            self.assertEqual(result['message'], 'unsupported_message_segment')
        self.assertFalse(self.sent)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM submissions').fetchone()[0], 0)

    async def test_identical_action_after_reopen_is_not_resent(self):
        event = await self.event()
        request = self.action(event)
        first = await self.core.action(request)
        self.state.close()
        self.state = Spool(self.path)
        self.core.state = self.state
        self.assertEqual(await self.core.action(request), first)
        self.assertEqual(len(self.sent), 1)

    async def test_unknown_and_wrong_binding_are_never_accepted_or_retried(self):
        event = await self.event()
        for index, changes in enumerate(({'state': 'needs_review', 'projection_verified': False},
                                         {'destination': 'other'}, {'source_sha256': 'a' * 64})):
            async def send(destination, image, key, changes=changes):
                self.sent.append((destination, image, key))
                return receipt(image, destination, **changes)
            self.gateway.send_image = send
            request = self.action(event, echo=index)
            result = await self.core.action(request)
            self.assertEqual(result['retcode'], 1408)
            self.assertEqual(await self.core.action(request), result)
        self.assertEqual(len(self.sent), 3)

    async def test_known_unsubmitted_budget_rejection_is_not_an_unknown_reply(self):
        event = await self.event()
        async def send(destination, image, key):
            return receipt(image, destination, state='rejected', job_id='', server_id='', projection_verified=False)
        self.gateway.send_image = send
        result = await self.core.action(self.action(event))
        self.assertEqual(result['message'], 'unsupported_message_segment')
        self.assertEqual(self.frames[-1], {'kind': 'report', 'stage': 'image_output', 'outcome': 'rejected'})
        self.assertFalse(any(f.get('stage') == 'reply' for f in self.frames))

    async def test_invalid_image_records_only_bounded_reason_and_keeps_following_action(self):
        event = await self.event()
        raw = png(images.MAX_EDGE + 1, 1)
        segment = {'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(raw).decode()}}
        result = await self.core.action(self.action(event, [segment]))
        self.assertEqual(result['message'], 'unsupported_message_segment')
        detail = [f['detail'] for f in self.frames if f.get('stage') == 'image_output_validation'][-1]
        self.assertEqual(detail['reason'], 'dimensions')
        self.assertEqual(detail['source_bytes'], len(raw))
        self.assertEqual(set(detail), images.REJECTION_KEYS)
        self.assertFalse(self.sent)
        result = await self.core.action(self.action(event, echo=87))
        self.assertEqual(result['retcode'], 0)


class ImagePipe(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Spool(Path(self.directory.name) / 'state.sqlite')
        self.frames = []
        self.gateway = Gateway('account', 'peer', self.state, 'profile', self.frames.append, image_output=True)
        self.image = images.payload(png())

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def test_image_frame_and_receipt_roundtrip_are_separate_from_text(self):
        task = asyncio.create_task(self.gateway.send_image('peer', self.image, 'a' * 32))
        try:
            await asyncio.sleep(0)
            frame = self.frames[-1]
            self.assertEqual(set(frame), {'kind', 'requestId', 'destination', 'image', 'key'})
            self.assertEqual(frame['kind'], 'send_image')
            self.assertEqual(decode(encode(frame))['image'], self.image)
            bad = {'kind': 'receipt', 'requestId': frame['requestId'], 'receipt': {
                'account': 'account', 'destination': 'peer', 'send_id': str(uuid.uuid4()),
                'state': 'accepted', 'client_id': '12', 'server_id': '45'}}
            with self.assertRaises(WireError):
                self.gateway.feed(bad)
            wrong = receipt(self.image, 'other')
            with self.assertRaises(WireError):
                self.gateway.feed({'kind': 'image_receipt', 'requestId': frame['requestId'], 'receipt': wrong})
            good = receipt(self.image)
            self.gateway.feed({'kind': 'image_receipt', 'requestId': frame['requestId'], 'receipt': good})
            self.assertEqual(await task, good)
            self.assertFalse(self.gateway.futures)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_disabled_gateway_and_bad_keys_do_not_emit_frames(self):
        self.gateway.image_output = False
        with self.assertRaises(WireError):
            await self.gateway.send_image('peer', self.image, 'a' * 32)
        self.gateway.image_output = True
        with self.assertRaises(WireError):
            await self.gateway.send_image('peer', self.image, '../other')
        self.assertFalse(self.frames)

    async def test_large_transfer_waits_for_each_ack_and_full_receipt(self):
        self.image = images.payload(png(extra=chunk(b'tEXt', b'x' * 300000)))
        task = asyncio.create_task(self.gateway.send_image('peer', self.image, 'a' * 32))
        try:
            await asyncio.sleep(0)
            first = self.frames[-1]
            self.assertEqual(first['kind'], 'image_begin')
            assembly = images.Assembly(first['image'])
            for index in range(-1, 5):
                before = len(self.frames)
                await asyncio.sleep(0)
                self.assertEqual(len(self.frames), before)
                self.gateway.feed({'kind': 'image_chunk_ack', 'requestId': first['requestId'], 'index': index})
                for _ in range(3):
                    await asyncio.sleep(0)
                current = self.frames[-1]
                if current['kind'] == 'image_chunk':
                    self.assertLess(len(encode(current)), MAX_FRAME)
                    assembly.add(current['index'], current['data'])
            self.assertEqual(self.frames[-1]['kind'], 'image_commit')
            self.assertEqual(assembly.finish(), self.image)
            answer = receipt(self.image)
            self.gateway.feed({'kind': 'image_receipt', 'requestId': first['requestId'], 'receipt': answer})
            self.assertEqual(await task, answer)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_budget_rejection_at_begin_does_not_wait_for_chunk_ack(self):
        value = images.payload(png(extra=chunk(b'tEXt', b'x' * 150000)))
        task = asyncio.create_task(self.gateway.send_image('peer', value, 'a' * 32))
        await asyncio.sleep(0)
        first = self.frames[-1]
        answer = receipt(value, state='rejected', job_id='', server_id='', projection_verified=False)
        self.gateway.feed({'kind': 'image_receipt', 'requestId': first['requestId'], 'receipt': answer})
        self.assertEqual(await asyncio.wait_for(task, 1), answer)
        self.assertEqual(len(self.frames), 1)
