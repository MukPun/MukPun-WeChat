"""Fixed SDK media job contracts with synthetic transports only."""
import base64
from copy import deepcopy
import importlib.util
import json
import hashlib
import http.server
import os
from pathlib import Path
import struct
import threading
import time
import unittest
import uuid
import zlib

from wechat_bridge.image_output import payload

spec = importlib.util.spec_from_file_location('image_output_native_test',
    Path(__file__).resolve().parents[1] / 'scripts/image_send_native.py')
native = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = native
spec.loader.exec_module(native)

def chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)


PNG = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b''))
IMAGE = payload(PNG, 'image/png')
SOURCE = 'upload_' + str(uuid.uuid4())
JOB = 'job_' + str(uuid.uuid4())
PREFIX = '/v1/accounts/wechat-primary'
ORIGINAL_HASH = 'a' * 64


def response(value, status=200):
    return native.Response(status, {'content-type': 'application/json'}, json.dumps(value).encode())


def job(state='queued', destination='peer'):
    return {'id': JOB, 'accountId': 'wechat-primary', 'kind': 'send', 'threadId': destination,
            'sourceId': SOURCE, 'state': state, 'phase': 'completed' if state == 'accepted' else 'preparing',
            'serverMessageId': '101' if state == 'accepted' else None,
            'result': {'nativeReceipt': {'status': 'server_accepted', 'serviceCode': 0,
                       'serverMessageId': '101', 'clientIdMatches': True},
                       'imagePreparation': {'sourceSha256': IMAGE['sha256'], 'sentOriginalSha256': ORIGINAL_HASH,
                                            'sourcePreserved': False, 'width': 1, 'height': 1},
                       'uploadReadbackVerified': True, 'verifiedSha256': [ORIGINAL_HASH, 'b' * 64, 'c' * 64],
                       'profileProjection': 'complete', 'messageIds': ['projected-image']}}


class Clock:
    now = 100.
    def __call__(self):
        return self.now
    def sleep(self, delay):
        self.now += delay


class Fake:
    def __init__(self, jobs=None, destination='peer'):
        self.destination = destination
        self.jobs = deepcopy(jobs if jobs is not None else [job(), job('accepted')])
        self.calls = []
        self.source = {'id': SOURCE, 'name': 'bridge-image.png', 'mediaType': 'image/png',
                       'byteLength': len(PNG), 'sha256': IMAGE['sha256']}
        self.message = {'id': 'projected-image', 'accountId': 'wechat-primary', 'threadId': destination,
                        'senderId': 'self-user', 'direction': 'outgoing', 'type': 3, 'serverId': '101'}
        self.assets = {'items': [{'id': 'asset_' + 'e' * 40, 'accountId': 'wechat-primary',
                        'messageId': 'projected-image', 'kind': 'image', 'variant': 'original',
                        'actualVariant': 'original', 'state': 'available', 'contentType': 'image/jpeg',
                        'byteLength': 55, 'sha256': ORIGINAL_HASH}]}

    def request(self, method, path, *, timeout, headers=None, body=None):
        self.calls.append((method, path, timeout, deepcopy(headers), body))
        if method == 'POST' and path == PREFIX + '/uploads':
            return response(self.source, 201)
        if method == 'POST' and path == PREFIX + '/sends':
            return response(self.jobs[0], 202)
        if method == 'GET' and path == PREFIX + '/jobs/' + JOB:
            if len(self.jobs) > 1:
                self.jobs.pop(0)
            return response(self.jobs[0])
        if method == 'GET' and path == PREFIX + '/messages/projected-image':
            return response(self.message)
        if method == 'GET' and path == PREFIX + '/messages/projected-image/media':
            return response(self.assets)
        raise AssertionError('unbound_native_request')


class NativeOutput(unittest.TestCase):
    def client(self, transport=None, **changes):
        self.clock = Clock()
        self.frames = []
        self.transport = transport or Fake()
        fields = dict(account='wechat-primary', token='t' * 32, destination='peer', image=IMAGE,
                      key='1' * 32, self_sender_id='self-user', transport=self.transport,
                      clock=self.clock, sleep=self.clock.sleep,
                      on_intent=lambda x: self.frames.append(('intent', x)),
                      on_wait=lambda x: self.frames.append(('wait', x)),
                      on_done=lambda x: self.frames.append(('done', x)))
        fields.update(changes)
        return native.Client(**fields)

    def test_upload_multipart_bytes_and_bound_media_request_are_complete(self):
        client = self.client()
        result = client.run(110)
        self.assertTrue(result.ok)
        self.assertTrue(result.server_confirmed)
        method, path, timeout, headers, body = self.transport.calls[0]
        boundary = headers['Content-Type'].split('boundary=')[1]
        prefix, content = body.split(b'\r\n\r\n', 1)
        self.assertIn(b'name="file"; filename="bridge-image.png"', prefix)
        self.assertIn(b'Content-Type: image/png', prefix)
        suffix = ('\r\n--' + boundary + '--\r\n').encode()
        self.assertTrue(content.endswith(suffix))
        self.assertEqual(content[:-len(suffix)], PNG)
        send = self.transport.calls[1]
        self.assertEqual(send[3], {'Content-Type': 'application/json', 'Idempotency-Key': '1' * 32})
        self.assertEqual(json.loads(send[4]), {'kind': 'image', 'threadId': 'peer', 'sourceId': SOURCE})
        self.assertTrue(all(0 < call[2] <= 8 for call in self.transport.calls))
        self.assertEqual(result.receipt, {'account': 'wechat-primary', 'destination': 'peer', 'job_id': JOB,
            'state': 'accepted', 'server_id': '101', 'source_sha256': IMAGE['sha256'], 'projection_verified': True})
        self.assertEqual(self.frames, [('intent', JOB), ('wait', JOB), ('done', JOB)])

    def test_same_client_cannot_upload_or_submit_again(self):
        client = self.client()
        self.assertTrue(client.run(110).ok)
        calls = len(self.transport.calls)
        self.assertEqual(client.run(110).code, 'image_client_used')
        self.assertEqual(len(self.transport.calls), calls)

    def test_group_destination_uses_the_same_exact_binding(self):
        target = 'room@chatroom'
        fake = Fake([job(destination=target), job('accepted', target)], target)
        result = self.client(fake, destination=target).run(110)
        self.assertTrue(result.ok)
        self.assertEqual(result.receipt['destination'], target)

    def test_upload_identity_mime_size_and_source_hash_must_match(self):
        for field, changed in (('id', 'upload_bad'), ('name', '../../bad.png'), ('mediaType', 'image/jpeg'),
                               ('byteLength', True), ('byteLength', len(PNG) + 1), ('sha256', 'f' * 64)):
            with self.subTest(field=field, changed=changed):
                fake = Fake()
                fake.source[field] = changed
                result = self.client(fake).run(110)
                self.assertFalse(result.ok)
                self.assertEqual(result.code, 'image_upload_binding')
                self.assertFalse(result.submitted)
                self.assertEqual([call[0] for call in fake.calls], ['POST'])

    def test_job_account_kind_target_source_and_id_cannot_change(self):
        for field, changed in (('id', 'job_' + str(uuid.uuid1())), ('accountId', 'other'), ('kind', 'download'),
                               ('threadId', 'other'), ('sourceId', 'upload_' + str(uuid.uuid4()))):
            with self.subTest(field=field):
                first = job()
                first[field] = changed
                fake = Fake([first])
                result = self.client(fake).run(110)
                self.assertEqual(result.code, 'image_job_binding')
                self.assertTrue(result.submitted)
                self.assertFalse(result.server_confirmed)

    def test_job_identity_change_on_poll_is_rejected(self):
        second = job('accepted')
        second['id'] = 'job_' + str(uuid.uuid4())
        result = self.client(Fake([job(), second])).run(110)
        self.assertEqual(result.code, 'image_job_binding')

    def test_accepted_job_needs_correlated_native_receipt_and_verified_source(self):
        mutations = [lambda j: j['result']['nativeReceipt'].update(clientIdMatches=False),
                     lambda j: j['result']['nativeReceipt'].update(serverMessageId='102'),
                     lambda j: j['result']['nativeReceipt'].update(serviceCode=True),
                     lambda j: j['result'].update(uploadReadbackVerified=False),
                     lambda j: j['result']['imagePreparation'].update(sourceSha256='f' * 64),
                     lambda j: j['result']['imagePreparation'].update(sourcePreserved=True),
                     lambda j: j['result'].update(verifiedSha256=['f' * 64] * 3),
                     lambda j: j.update(serverMessageId='0')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = job('accepted')
                mutate(value)
                result = self.client(Fake([value])).run(110)
                self.assertFalse(result.ok)
                self.assertEqual(result.code, 'image_receipt_binding')
                self.assertFalse(result.server_confirmed)

    def test_outgoing_projection_must_bind_server_self_destination_and_type(self):
        for field, changed in (('id', 'other'), ('threadId', 'other'), ('senderId', 'other'),
                               ('direction', 'incoming'), ('type', 47), ('serverId', '102')):
            with self.subTest(field=field):
                fake = Fake([job('accepted')])
                fake.message[field] = changed
                result = self.client(fake).run(110)
                self.assertEqual(result.code, 'image_projection_binding')
                self.assertTrue(result.server_confirmed)
                self.assertFalse(result.receipt['projection_verified'])

    def test_small_png_can_be_upscaled_by_the_pinned_sdk_jpeg_preparation(self):
        raw = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 256, 192, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * 256) * 192)) + chunk(b'IEND', b''))
        image = payload(raw, 'image/png')
        accepted = job('accepted')
        accepted['result']['imagePreparation'].update(sourceSha256=image['sha256'], width=1280, height=960)
        fake = Fake([accepted])
        fake.source.update(byteLength=len(raw), sha256=image['sha256'])
        result = self.client(fake, image=image).run(110)
        self.assertTrue(result.ok)
        self.assertTrue(result.receipt['projection_verified'])

    def test_upscaled_dimensions_still_require_bounds_and_source_aspect_ratio(self):
        for width, height in ((1281, 1281), (1280, 960), (0, 0)):
            with self.subTest(width=width, height=height):
                accepted = job('accepted')
                accepted['result']['imagePreparation'].update(width=width, height=height)
                result = self.client(Fake([accepted])).run(110)
                self.assertEqual(result.code, 'image_receipt_binding')
                self.assertFalse(result.ok)

    def test_original_asset_hash_must_match_sent_jpeg_not_the_png_source(self):
        fake = Fake([job('accepted')])
        fake.assets['items'][0]['sha256'] = IMAGE['sha256']
        result = self.client(fake).run(110)
        self.assertEqual(result.code, 'image_projection_binding')
        self.assertFalse(result.ok)

    def test_preserved_jpeg_receipt_uses_validated_source_shape_instead_of_resize_limit(self):
        # Isolate receipt binding with already-validated source metadata. Image
        # decoding is covered separately at both sides of the IPC boundary.
        client = self.client()
        client.width, client.height = 1600, 900
        client.image['mime'] = 'image/jpeg'
        accepted = job('accepted')
        prepared = accepted['result']['imagePreparation']
        prepared.update(sourcePreserved=True, width=1600, height=900,
                        sentOriginalSha256=client.image['sha256'])
        accepted['result']['verifiedSha256'][0] = client.image['sha256']
        self.assertEqual(client._accepted(accepted)[1]['width'], 1600)
        for change in ({'sourcePreserved': False}, {'width': 1280, 'height': 720},
                       {'sentOriginalSha256': 'f'*64}):
            with self.subTest(change=change), self.assertRaises(native.ImageSendError):
                client._accepted({**accepted, 'result': {**accepted['result'],
                    'imagePreparation': {**prepared, **change}}})

    def test_projection_pending_is_polled_until_complete_without_resubmission(self):
        pending = job('accepted')
        pending['result']['profileProjection'] = 'pending'
        pending['result']['messageIds'] = []
        fake = Fake([pending, job('accepted')])
        result = self.client(fake).run(110)
        self.assertTrue(result.ok)
        self.assertEqual(sum(method == 'POST' for method, *_ in fake.calls), 2)

    def test_failed_needs_review_and_cancelled_never_retry_or_return_success(self):
        for state, code in (('failed', 'image_failed'), ('needs_review', 'image_needs_review'), ('cancelled', 'image_cancelled')):
            with self.subTest(state=state):
                fake = Fake([job(state)])
                result = self.client(fake).run(110)
                self.assertEqual(result.code, code)
                self.assertFalse(result.ok)
                self.assertEqual(result.receipt['state'], 'unknown' if state == 'cancelled' else state)
                self.assertEqual(len(fake.calls), 2)
                self.assertEqual(self.frames, [('intent', JOB), ('done', JOB)])

    def test_failure_before_wechat_submission_is_known_but_submitting_stays_uncertain(self):
        for phase in ('preparing', 'uploading', 'readback', 'submitting', 'completed'):
            failed = job('failed')
            failed['phase'] = phase
            failed['result'].pop('nativeReceipt')
            fake = Fake([failed])
            result = self.client(fake).run(110)
            self.assertFalse(result.ok)
            self.assertEqual(result.code, 'image_failed')
            self.assertEqual(result.submitted, phase in {'submitting', 'completed'})
            self.assertFalse(result.server_confirmed)
            self.assertEqual(sum(c[0] == 'POST' and c[1].endswith('/sends') for c in fake.calls), 1)

    def test_timeout_keeps_job_for_review_without_retry_or_cancel_post(self):
        fake = Fake([job()])
        result = self.client(fake).run(101)
        self.assertEqual(result.code, 'image_timeout')
        self.assertTrue(result.submitted)
        self.assertEqual(result.receipt['state'], 'needs_review')
        self.assertEqual(sum(method == 'POST' for method, *_ in fake.calls), 2)
        self.assertEqual(self.frames[-1], ('done', JOB))

    def test_post_transport_failure_is_never_retried(self):
        fake = Fake()
        original = fake.request
        def request(method, path, **kwargs):
            if path.endswith('/sends'):
                fake.calls.append((method, path, kwargs['timeout'], kwargs.get('headers'), kwargs.get('body')))
                raise TimeoutError()
            return original(method, path, **kwargs)
        fake.request = request
        client = self.client(fake)
        result = client.run(110)
        self.assertEqual(result.code, 'image_transport')
        self.assertTrue(result.submitted)
        self.assertEqual(len(fake.calls), 2)
        client.run(110)
        self.assertEqual(len(fake.calls), 2)

    def test_arbitrary_native_routes_and_repeated_posts_are_blocked(self):
        client = self.client()
        self.assertTrue(client.run(110).ok)
        count = len(self.transport.calls)
        for path in (PREFIX + '/logout', PREFIX + '/jobs/' + JOB + '/retry', '/v1/status'):
            with self.subTest(path=path), self.assertRaises(native.ImageSendError):
                client.request('POST', path, 110, headers={}, body=b'{}')
        upload_path = PREFIX + '/uploads'
        headers, body = client.policy.posts[upload_path]
        with self.assertRaises(native.ImageSendError):
            client.request('POST', upload_path, 110, headers=headers, body=body)
        self.assertEqual(len(self.transport.calls), count)

    def test_invalid_image_targets_and_large_decode_dimensions_are_preflight_errors(self):
        huge = bytearray(PNG)
        huge[16:24] = struct.pack('>II', native.images.MAX_EDGE + 1, 1)
        large_image = {**IMAGE, 'base64': base64.b64encode(huge).decode(), 'sha256': hashlib.sha256(huge).hexdigest()}
        invalid = [('destination', 'filehelper'), ('destination', 'peer\n'), ('key', '../bad'),
                   ('self_sender_id', 'room@chatroom'), ('account', 'other'),
                   ('image', large_image)]
        for name, value in invalid:
            with self.subTest(name=name), self.assertRaises(native.ImageSendError):
                self.client(**{name: value})

    def test_callback_failure_does_not_create_a_successful_receipt(self):
        def failed(_):
            raise RuntimeError('private callback detail')
        result = self.client(on_done=failed).run(110)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'image_callback')
        self.assertNotIn('private', repr(result))

    def test_png_crc_is_revalidated_at_native_boundary(self):
        corrupt = bytearray(PNG)
        corrupt[-1] ^= 1
        bad = {**IMAGE, 'base64': base64.b64encode(corrupt).decode(), 'sha256': hashlib.sha256(corrupt).hexdigest()}
        with self.assertRaises(native.ImageSendError):
            self.client(image=bad)

    @unittest.skipUnless(os.name == 'posix', 'real loopback HTTP is checked in the Linux private-network sandbox')
    def test_real_loopback_http_uses_owned_upload_and_bound_get_routes(self):
        fake, tokens = Fake(), []
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def handle_request(self):
                tokens.append(self.headers.get('Authorization'))
                size = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(size) if self.command == 'POST' else None
                headers = None
                if self.command == 'POST':
                    headers = {'Content-Type': self.headers['Content-Type']}
                    if 'Idempotency-Key' in self.headers:
                        headers['Idempotency-Key'] = self.headers['Idempotency-Key']
                item = fake.request(self.command, self.path, timeout=1, headers=headers, body=body)
                self.send_response(item.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(item.body)))
                self.end_headers()
                self.wfile.write(item.body)
            do_POST = handle_request
            do_GET = handle_request
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = native.Client('wechat-primary', 'synthetic-token', 'peer', IMAGE, '1' * 32,
                self_sender_id='self-user', port=server.server_port)
            result = client.run(time.monotonic() + 5)
            self.assertTrue(result.ok, result.code)
            self.assertEqual(tokens, ['Bearer synthetic-token'] * len(fake.calls))
            self.assertEqual([call[0] for call in fake.calls], ['POST', 'POST', 'GET', 'GET', 'GET'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
