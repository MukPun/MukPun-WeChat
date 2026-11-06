"""GET-only sticker diagnosis must not become an unverified download path."""
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from test_onebot_images import native


class StickerProbe(unittest.TestCase):
    def lookup(self, **changes):
        message = {'id': 'sample', 'accountId': 'wechat-primary', 'threadId': 'peer', 'senderId': 'peer',
                   'direction': 'incoming', 'type': 47, 'body': None, 'createdAt': datetime.now(timezone.utc).isoformat()}
        return SimpleNamespace(policy=SimpleNamespace(prefix='/v1/accounts/wechat-primary'),
                               _get=Mock(return_value={'items': [{**message, **changes}]}))

    def test_sticker_metadata_is_get_only_and_separate_from_images(self):
        client = Mock()
        client.assets.return_value = [{'kind': 'sticker', 'variant': 'original', 'state': 'pending',
                                       'signedUrl': 'must-not-leave-native'}]
        result = native.recent_summary(self.lookup(), 'peer', client, time.monotonic() + 5, sticker=True)
        self.assertTrue(result['found'])
        self.assertEqual(result['message_type'], 47)
        self.assertEqual(result['variants'][0]['quality'], 'original')
        self.assertNotIn('must-not-leave-native', json.dumps(result))
        client.read.assert_not_called()
        client.request.assert_not_called()

    def test_foreign_sender_conversation_or_outgoing_is_never_probed(self):
        for change in ({'senderId': 'other'}, {'threadId': 'other'}, {'direction': 'outgoing'}, {'type': 3}):
            client = Mock()
            result = native.recent_summary(self.lookup(**change), 'peer', client, time.monotonic() + 5, sticker=True)
            self.assertFalse(result['found'])
            client.assets.assert_not_called()

    def test_upstream_operation_failed_is_reported_without_private_metadata(self):
        error = native.ImageError('image_native_rejected')
        error.native_code, error.native_http_status = 'operation_failed', 500
        client = Mock()
        client.assets.side_effect = error
        result = native.recent_summary(self.lookup(), 'peer', client, time.monotonic() + 5, sticker=True)
        self.assertTrue(result['found'])
        self.assertEqual(result['native_code'], 'operation_failed')
        self.assertEqual(result['native_http_status'], 500)
        self.assertIsNone(result['body_bytes'])
        client.read.assert_not_called()

    def test_sticker_download_routes_through_read_sticker(self):
        client = Mock()
        client.assets.return_value = []
        client.read_sticker.return_value = {'bytes': 12, 'sha256': 'a' * 64, 'mime': 'image/png', 'quality': 'original'}
        result = native.recent_summary(self.lookup(), 'peer', client, time.monotonic() + 5, sticker=True, download=True)
        self.assertEqual(result['verified'], {'bytes': 12, 'sha256': 'a' * 64, 'mime': 'image/png', 'quality': 'original'})
        client.read_sticker.assert_called_once()
        client.read.assert_not_called()

    def connection(self, responses):
        def reply(status, headers, body):
            served = Mock(status=status)
            served.getheader.side_effect = lambda key, default=None: headers.get(key, default)
            served.read.return_value = body
            return served
        connection = Mock()
        connection.getresponse.side_effect = [reply(*item) for item in responses]
        return connection

    def test_wxgf_preview_integrity_uses_preview_metadata(self):
        raw = (b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0d' + b'IHDR'
               + (1).to_bytes(4, 'big') + (1).to_bytes(4, 'big') + b'\x08\x06\x00\x00\x00')
        asset = {'id': 'asset_' + 'a' * 40, 'accountId': 'wechat-primary', 'messageId': 'sample',
                 'kind': 'sticker', 'variant': 'original', 'state': 'available',
                 'contentType': 'image/x-wxgf', 'byteLength': 900, 'sha256': 'b' * 64,
                 'preview': {'contentType': 'image/png', 'byteLength': len(raw),
                             'sha256': hashlib.sha256(raw).hexdigest()}}
        listing = json.dumps({'items': [asset]}).encode()
        connection = self.connection([
            (200, {'Content-Type': 'application/json', 'Content-Length': str(len(listing))}, listing),
            (200, {'Content-Type': 'image/png', 'Content-Length': str(len(raw))}, raw)])
        with patch.object(native.http.client, 'HTTPConnection', return_value=connection):
            result = native.Client('wechat-primary', 't' * 32).read_sticker('sample', time.monotonic() + 5)
        self.assertEqual(result['mime'], 'image/png')
        self.assertEqual(result['sha256'], hashlib.sha256(raw).hexdigest())

    def test_plain_gif_sticker_serves_original_bytes(self):
        raw = b'GIF89a' + (1).to_bytes(2, 'little') + (1).to_bytes(2, 'little') + b'\x00\xf0\x00'
        asset = {'id': 'asset_' + 'b' * 40, 'accountId': 'wechat-primary', 'messageId': 'sample',
                 'kind': 'sticker', 'variant': 'original', 'state': 'available',
                 'contentType': 'image/gif', 'byteLength': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        listing = json.dumps({'items': [asset]}).encode()
        connection = self.connection([
            (200, {'Content-Type': 'application/json', 'Content-Length': str(len(listing))}, listing),
            (200, {'Content-Type': 'image/gif', 'Content-Length': str(len(raw))}, raw)])
        with patch.object(native.http.client, 'HTTPConnection', return_value=connection):
            result = native.Client('wechat-primary', 't' * 32).read_sticker('sample', time.monotonic() + 5)
        self.assertEqual(result['mime'], 'image/gif')
        self.assertEqual(result['sha256'], hashlib.sha256(raw).hexdigest())

    def test_wxgf_preview_digest_mismatch_is_rejected(self):
        raw = (b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0d' + b'IHDR'
               + (1).to_bytes(4, 'big') + (1).to_bytes(4, 'big') + b'\x08\x06\x00\x00\x00')
        asset = {'id': 'asset_' + 'c' * 40, 'accountId': 'wechat-primary', 'messageId': 'sample',
                 'kind': 'sticker', 'variant': 'original', 'state': 'available',
                 'contentType': 'image/x-wxgf', 'byteLength': 900, 'sha256': 'b' * 64,
                 'preview': {'contentType': 'image/png', 'byteLength': len(raw), 'sha256': 'c' * 64}}
        listing = json.dumps({'items': [asset]}).encode()
        connection = self.connection([
            (200, {'Content-Type': 'application/json', 'Content-Length': str(len(listing))}, listing),
            (200, {'Content-Type': 'image/png', 'Content-Length': str(len(raw))}, raw)])
        with patch.object(native.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(native.ImageError, 'image_asset_integrity'):
                native.Client('wechat-primary', 't' * 32).read_sticker('sample', time.monotonic() + 5)

    def test_http_failure_exposes_only_public_code_and_status(self):
        response = Mock(status=500)
        response.getheader.side_effect = lambda k, default=None: {'Content-Type': 'application/json'}.get(k, default)
        response.read.return_value = b'{"error":{"code":"operation_failed","details":"https://private.invalid/signed"}}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(native.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaises(native.ImageError) as caught:
                native.Client('wechat-primary', 't' * 32).assets('sample', time.monotonic() + 5)
        self.assertEqual(str(caught.exception), 'image_native_rejected')
        self.assertEqual(caught.exception.native_code, 'operation_failed')
        self.assertEqual(caught.exception.native_http_status, 500)
        self.assertNotIn('private.invalid', str(vars(caught.exception)))


if __name__ == '__main__':
    unittest.main()
