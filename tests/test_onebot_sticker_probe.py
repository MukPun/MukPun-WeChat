"""GET-only sticker diagnosis must not become an unverified download path."""
from datetime import datetime, timezone
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

    def test_sticker_probe_cannot_accidentally_download_original(self):
        client = Mock()
        client.assets.return_value = []
        with self.assertRaisesRegex(native.ImageError, 'sticker_unavailable'):
            native.recent_summary(self.lookup(), 'peer', client, time.monotonic() + 5, sticker=True, download=True)
        client.read.assert_not_called()

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
