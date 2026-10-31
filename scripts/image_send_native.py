"""Bounded beta.8 image upload/send jobs; no login, retries or arbitrary URLs.

The caller binds an approved destination and owns the small aggregate storage
budget. An accepted HTTP job is not a successful WeChat send: only a correlated
native receipt plus its outgoing image projection can complete this client.
"""
from dataclasses import dataclass, field
import base64
import http.client
import importlib.util
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import quote
import uuid

def _contract(name):
    path = Path(__file__).with_name(name + '.py')
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / 'wechat_bridge' / (name + '.py')
    spec = importlib.util.spec_from_file_location('_image_send_' + name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


images, input_images = _contract('image_output'), _contract('image_input')

JSON_LIMIT = 65536
MAX_SECONDS = 90
MAX_POLLS = 180
MIMES = frozenset({'image/jpeg', 'image/png', 'image/webp'})
HEX = re.compile('[0-9a-f]{64}')
KEY = re.compile('[0-9a-f]{32}')
ERROR_CODES = frozenset({'image_arguments', 'image_limits', 'image_request_not_allowed', 'image_response',
    'image_transport', 'image_timeout', 'image_upload_binding', 'image_job_binding', 'image_receipt_binding',
    'image_projection_binding', 'image_failed', 'image_needs_review', 'image_cancelled', 'image_callback',
    'image_client_used'})


class ImageSendError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else 'image_response'
        super().__init__(self.code)


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class Result:
    ok: bool
    code: str
    submitted: bool
    server_confirmed: bool
    receipt: dict = field(repr=False)


def _identity(value, maximum=512):
    try:
        return (isinstance(value, str) and 0 < len(value.encode('utf-8')) <= maximum
                and not any(ord(c) < 33 or ord(c) == 127 for c in value))
    except UnicodeError:
        return False


def _uuid(value, prefix):
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    try:
        parsed = uuid.UUID(value[len(prefix):])
        return parsed.version == 4 and str(parsed) == value[len(prefix):]
    except ValueError:
        return False


def _server(value):
    return isinstance(value, str) and re.fullmatch('[1-9][0-9]{0,19}', value) is not None and int(value) <= 2**64 - 1


def _object(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ImageSendError('image_response')
            out[key] = value
        return out
    def invalid(_):
        raise ImageSendError('image_response')
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise ImageSendError('image_response') from None
    if not isinstance(value, dict):
        raise ImageSendError('image_response')
    return value


class Policy:
    """Exact owned upload and send bodies followed only by bound GETs."""
    def __init__(self, account, upload, upload_headers):
        self.prefix = '/v1/accounts/' + account
        self.posts = {self.prefix + '/uploads': (upload_headers, upload)}
        self.gets = set()
        self.attempted = set()

    def authorize(self, method, path, *, headers=None, body=None):
        if method == 'POST' and path in self.posts and (headers, body) == self.posts[path]:
            return
        if method == 'GET' and path in self.gets and headers is None and body is None:
            return
        raise ImageSendError('image_request_not_allowed')

    def begin(self, method, path):
        if method == 'POST':
            if path in self.attempted:
                raise ImageSendError('image_request_not_allowed')
            self.attempted.add(path)


class Transport:
    """Fixed raw loopback HTTP, no proxies, redirects or POST retries."""
    def __init__(self, policy, token, port):
        self.policy, self._token, self.port = policy, token, port

    def request(self, method, path, *, timeout, headers=None, body=None):
        self.policy.authorize(method, path, headers=headers, body=body)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 8:
            raise ImageSendError('image_arguments')
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
        try:
            outgoing = {'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json',
                        'Accept-Encoding': 'identity'}
            if headers:
                outgoing.update(headers)
            connection.request(method, path, body=body, headers=outgoing)
            incoming = connection.getresponse()
            selected = {}
            for key, value in incoming.getheaders():
                key = key.lower()
                if key in {'content-length', 'content-type', 'content-encoding'}:
                    if key in selected:
                        raise ImageSendError('image_response')
                    selected[key] = value
            length = selected.get('content-length')
            if (length is not None and (not re.fullmatch('[0-9]{1,8}', length) or int(length) > JSON_LIMIT)
                    or selected.get('content-encoding', 'identity') != 'identity'):
                raise ImageSendError('image_response')
            raw = incoming.read(JSON_LIMIT + 1)
            if len(raw) > JSON_LIMIT or length is not None and len(raw) != int(length):
                raise ImageSendError('image_response')
            return Response(incoming.status, selected, raw)
        except ImageSendError:
            raise
        except (OSError, http.client.HTTPException, UnicodeError):
            raise ImageSendError('image_transport') from None
        finally:
            connection.close()


class Client:
    media_kind = 'image'
    deadline_seconds = MAX_SECONDS
    max_polls = MAX_POLLS

    def _source(self, image):
        images.validate(image)
        raw = base64.b64decode(image['base64'], validate=True)
        self.width, self.height = images.dimensions(raw, image['mime'])
        if (image['mime'] not in MIMES or self.width > images.MAX_EDGE or self.height > images.MAX_EDGE
                or self.width * self.height > images.MAX_PIXELS):
            raise ImageSendError('image_limits')
        self.filename = 'bridge-image.' + {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[image['mime']]
        return raw

    def _known_failure(self, job):
        state, phase = job.get('state'), job.get('phase')
        # needs_review is the pinned SDK's conservative outcome for an
        # interrupted send. A terminal job that never reached the submitting
        # phase cannot have produced a WeChat message, so the bridge may record
        # a rejection instead of stopping the whole forwarder. Submitting and
        # readback stay uncertain.
        if state == 'failed':
            phases = {'preparing', 'uploading', 'readback'}
        elif state == 'needs_review':
            phases = {'preparing', 'uploading'}
        else:
            return False
        return (phase in phases and job.get('serverMessageId') in (None, '')
                and isinstance(job.get('result'), dict) and job['result'].get('nativeReceipt') is None)

    def __init__(self, account, token, destination, image, key, *, self_sender_id,
                 port=26174, transport=None, clock=time.monotonic, sleep=time.sleep,
                 on_intent=lambda _: None, on_wait=lambda _: None, on_done=lambda _: None,
                 source_id=None, on_source=lambda _: None):
        if (account != 'wechat-primary' or not _identity(destination, 256) or destination == '@chatroom'
                or destination == 'filehelper' or destination.startswith('weixin')
                or not _identity(self_sender_id, 256) or self_sender_id.endswith('@chatroom')
                or destination == self_sender_id or not isinstance(key, str) or not KEY.fullmatch(key)
                or not isinstance(token, str) or not 1 <= len(token) <= 4096
                or any(not 33 <= ord(c) <= 126 for c in token)
                or type(port) is not int or not 1 <= port <= 65535
                or not all(callable(f) for f in (clock, sleep, on_intent, on_wait, on_done))):
            raise ImageSendError('image_arguments')
        if source_id is not None and not _uuid(source_id,'upload_'):
            raise ImageSendError('image_arguments')
        try:
            raw = self._source(image)
        except (ValueError, TypeError, KeyError):
            raise ImageSendError('image_limits') from None
        self.account, self.destination, self.image = account, destination, dict(image)
        self.self_sender_id, self.key = self_sender_id, key
        self.clock, self.sleep = clock, sleep
        self.on_intent, self.on_wait, self.on_done = on_intent, on_wait, on_done
        self.on_source = on_source
        boundary = 'astrbot-image-' + uuid.uuid4().hex
        upload = b'' if source_id else (('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="'
                   + self.filename + '"\r\nContent-Type: ' + image['mime'] + '\r\n\r\n').encode('ascii')
                  + raw + ('\r\n--' + boundary + '--\r\n').encode('ascii'))
        upload_headers = {'Content-Type': 'multipart/form-data; boundary=' + boundary}
        self.policy = Policy(account, upload, upload_headers)
        self.transport = Transport(self.policy, token, port) if transport is None else transport
        self.used = self.submitted = self.upload_attempted = self.server_confirmed = self.projection_verified = False
        self.source_id = source_id or ''
        self.job_id = self.server_id = ''
        self._media_done = False

    def _call(self, callback, job):
        try:
            callback(job)
        except Exception:
            raise ImageSendError('image_callback') from None

    def request(self, method, path, deadline, *, headers=None, body=None):
        self.policy.authorize(method, path, headers=headers, body=body)
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ImageSendError('image_timeout')
        self.policy.begin(method, path)
        if method == 'POST':
            if path.endswith('/uploads'):
                self.upload_attempted = True
            else:
                self.submitted = True
        try:
            response = self.transport.request(method, path, timeout=min(8., remaining), headers=headers, body=body)
        except ImageSendError:
            raise
        except Exception:
            raise ImageSendError('image_transport') from None
        expected = 201 if path.endswith('/uploads') else 202 if method == 'POST' else 200
        if (not isinstance(response, Response) or response.status != expected
                or not isinstance(response.headers, dict) or not isinstance(response.body, bytes)
                or len(response.body) > JSON_LIMIT
                or response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json'
                or response.headers.get('content-encoding', 'identity') != 'identity'):
            raise ImageSendError('image_response')
        return _object(response.body)

    def _upload(self, deadline):
        if self.source_id:
            return
        path = self.policy.prefix + '/uploads'
        headers, body = self.policy.posts[path]
        source = self.request('POST', path, deadline, headers=headers, body=body)
        if (not _uuid(source.get('id'), 'upload_') or source.get('name') != self.filename
                or source.get('mediaType') != self.image['mime'] or source.get('byteLength') != self.image['bytes']
                or type(source.get('byteLength')) is not int or source.get('sha256') != self.image['sha256']):
            raise ImageSendError('image_upload_binding')
        self.source_id = source['id']
        self._call(self.on_source,self.source_id)

    def _job(self, value):
        if (not _uuid(value.get('id'), 'job_') or self.job_id and value['id'] != self.job_id
                or value.get('accountId') != self.account or value.get('kind') != 'send'
                or value.get('threadId') != self.destination or value.get('sourceId') != self.source_id
                or value.get('state') not in {'queued', 'running', 'preparing', 'uploading', 'readback', 'submitting',
                                              'accepted', 'failed', 'needs_review', 'cancelled'}):
            raise ImageSendError('image_job_binding')
        if not self.job_id:
            self.job_id = value['id']
            self.policy.gets.add(self.policy.prefix + '/jobs/' + self.job_id)
            self._call(self.on_intent, self.job_id)
        return value

    def _accepted(self, job):
        result = job.get('result')
        if not isinstance(result, dict):
            raise ImageSendError('image_receipt_binding')
        receipt, prepared, verified = result.get('nativeReceipt'), result.get('imagePreparation'), result.get('verifiedSha256')
        if (job.get('phase') != 'completed' or not _server(job.get('serverMessageId'))
                or not isinstance(receipt, dict) or receipt.get('status') != 'server_accepted'
                or type(receipt.get('serviceCode')) is not int or receipt['serviceCode'] != 0
                or receipt.get('clientIdMatches') is not True or receipt.get('serverMessageId') != job['serverMessageId']
                or result.get('uploadReadbackVerified') is not True or not isinstance(prepared, dict)
                or prepared.get('sourceSha256') != self.image['sha256']
                or not isinstance(prepared.get('sentOriginalSha256'), str) or not HEX.fullmatch(prepared['sentOriginalSha256'])
                or type(prepared.get('sourcePreserved')) is not bool
                or type(prepared.get('width')) is not int or type(prepared.get('height')) is not int
                # beta.8 fit_jpeg may upscale a small PNG to the 1280px
                # variant budget. Bind to that documented output bound and
                # source aspect ratio, not a false downscale-only assumption.
                or not 0 < prepared['width'] <= (images.MAX_EDGE if prepared['sourcePreserved'] else 1280)
                or not 0 < prepared['height'] <= (images.MAX_EDGE if prepared['sourcePreserved'] else 1280)
                or self.width == self.height and prepared['width'] != prepared['height']
                or abs(prepared['width'] * self.height - prepared['height'] * self.width) > max(self.width, self.height)
                or prepared['sourcePreserved'] and (prepared['sentOriginalSha256'] != self.image['sha256']
                    or self.image['mime'] != 'image/jpeg'
                    or (prepared['width'], prepared['height']) != (self.width, self.height))
                or not isinstance(verified, list) or len(verified) != 3
                or any(not isinstance(item, str) or not HEX.fullmatch(item) for item in verified)
                or verified[0] != prepared['sentOriginalSha256']):
            raise ImageSendError('image_receipt_binding')
        self.server_id, self.server_confirmed = job['serverMessageId'], True
        return result, prepared

    def _projection(self, result, prepared, deadline):
        ids = result.get('messageIds')
        if result.get('profileProjection') != 'complete' or not isinstance(ids, list) or len(ids) != 1 or not _identity(ids[0]):
            return False
        path = self.policy.prefix + '/messages/' + quote(ids[0], safe='')
        self.policy.gets.update({path, path + '/media'})
        message = self.request('GET', path, deadline)
        if (message.get('id') != ids[0] or message.get('accountId') != self.account
                or message.get('threadId') != self.destination or message.get('senderId') != self.self_sender_id
                or message.get('direction') != 'outgoing' or type(message.get('type')) is not int or message['type'] != 3
                or message.get('serverId') != self.server_id):
            raise ImageSendError('image_projection_binding')
        assets = self.request('GET', path + '/media', deadline).get('items')
        if not isinstance(assets, list) or not 0 < len(assets) <= 8:
            raise ImageSendError('image_projection_binding')
        originals = [a for a in assets if isinstance(a, dict) and a.get('variant') == 'original']
        if len(originals) != 1:
            raise ImageSendError('image_projection_binding')
        asset = originals[0]
        if (asset.get('accountId') != self.account or asset.get('messageId') != ids[0]
                or asset.get('kind') != 'image' or asset.get('state') != 'available'
                or asset.get('actualVariant') != 'original' or asset.get('contentType') != 'image/jpeg'
                or type(asset.get('byteLength')) is not int or not 0 < asset['byteLength'] <= 65520
                or asset.get('sha256') != prepared['sentOriginalSha256']):
            raise ImageSendError('image_projection_binding')
        self.projection_verified = True
        return True

    def _result(self, ok, code, state):
        # The bridge contract intentionally exposes no independent cancelled
        # state: a cancelled media job is retained as unknown, never replayed.
        if state == 'cancelled':
            state = 'unknown'
        return Result(ok, code, self.submitted, self.server_confirmed,
            {'account': self.account, 'destination': self.destination, 'job_id': self.job_id,
             'state': state, 'server_id': self.server_id, 'source_sha256': self.image['sha256'],
             'projection_verified': self.projection_verified})

    def run(self, deadline):
        if self.used:
            return self._result(False, 'image_client_used', 'unknown')
        self.used = True
        state, code = 'unknown', 'image_response'
        try:
            if type(deadline) not in (int, float) or not math.isfinite(deadline) or not 0 < deadline - self.clock() <= self.deadline_seconds:
                raise ImageSendError('image_arguments')
            self._upload(deadline)
            path = self.policy.prefix + '/sends'
            headers = {'Content-Type': 'application/json', 'Idempotency-Key': self.key}
            body = json.dumps({'kind': self.media_kind, 'threadId': self.destination, 'sourceId': self.source_id},
                              sort_keys=True, separators=(',', ':')).encode('utf-8')
            self.policy.posts[path] = (headers, body)
            job = self._job(self.request('POST', path, deadline, headers=headers, body=body))
            for _ in range(self.max_polls):
                state = job['state']
                if state in {'failed', 'needs_review', 'cancelled'}:
                    # The pinned SDK sets "submitting" before calling WeChat.
                    # A terminal failure in an earlier phase is a known failure,
                    # even though the local job-creation POST was accepted.
                    if self._known_failure(job):
                        self.submitted = False
                    return self._result(False, {'failed': 'image_failed', 'needs_review': 'image_needs_review',
                                               'cancelled': 'image_cancelled'}[state], state)
                if state == 'accepted':
                    result, prepared = self._accepted(job)
                    if not self._media_done:
                        self._call(self.on_done, self.job_id)
                        self._media_done = True
                    if self._projection(result, prepared, deadline):
                        return self._result(True, 'ok', 'accepted')
                else:
                    self._call(self.on_wait, self.job_id)
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise ImageSendError('image_timeout')
                self.sleep(min(.5, remaining))
                job = self._job(self.request('GET', self.policy.prefix + '/jobs/' + self.job_id, deadline))
            raise ImageSendError('image_timeout')
        except ImageSendError as error:
            code = error.code
        except Exception:
            code = 'image_response'
        finally:
            if self.job_id and not self._media_done:
                try:
                    self._call(self.on_done, self.job_id)
                    self._media_done = True
                except ImageSendError:
                    # The caller's lease owner still treats a failed revoke as
                    # unsafe; never silently claim this client completed.
                    pass
        return self._result(False, code, 'needs_review' if self.submitted else 'unknown')
