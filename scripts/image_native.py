"""Native media reads and bounded original/standard jobs; never sends a message."""
import http.client
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import time
from urllib.parse import quote

_path = Path(__file__).with_name('image_input.py')
if not _path.exists():
    _path = Path(__file__).resolve().parents[1] / 'wechat_bridge/image_input.py'
_spec = importlib.util.spec_from_file_location('_image_native_contract', _path)
_contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_contract)
MAX_IMAGE, MIMES, ImageError, payload = _contract.MAX_IMAGE, _contract.MIMES, _contract.ImageError, _contract.payload
ERROR_CODES = _contract.ERROR_CODES


def recent_summary(lookup, peer, client, deadline, *, download=False, sticker=False):
    path = lookup.policy.prefix + '/threads/' + quote(peer, safe='') + '/messages?limit=20'
    lookup.policy.path = path
    page = lookup._get(path, deadline, timeout=8)
    items = page.get('items')
    if not isinstance(items, list) or len(items) > 20:
        raise ImageError('image_probe_shape')
    candidates = [m for m in items if isinstance(m, dict) and m.get('accountId') == 'wechat-primary'
                  and m.get('threadId') == peer and m.get('senderId') == peer and m.get('direction') == 'incoming'
                  and type(m.get('type')) is int and m['type'] == (47 if sticker else 3) and isinstance(m.get('id'), str)]
    def recent(m):
        try:
            stamp = datetime.fromisoformat(m['createdAt'].replace('Z', '+00:00'))
            return stamp.tzinfo is not None and -5 <= time.time() - stamp.timestamp() <= 7200
        except (KeyError, TypeError, ValueError):
            return False
    candidates = [m for m in candidates if recent(m)]
    if not candidates:
        return {'found': False, 'variants': [], 'message_type': 47 if sticker else 3}
    candidate = max(candidates, key=lambda m: m.get('createdAt') or '')
    try:
        assets = client.assets(candidate['id'], deadline)
    except ImageError as error:
        if not sticker:
            raise
        return {'found': True, 'message_type': 47, 'variants': [],
                'metadata_error': str(error) if str(error) in ERROR_CODES else 'image_failed',
                'native_code': getattr(error, 'native_code', None), 'native_http_status': getattr(error, 'native_http_status', None),
                'body_text': isinstance(candidate.get('body'), str),
                'body_bytes': len(candidate['body'].encode()) if isinstance(candidate.get('body'), str) else None}
    result = {'found': True, 'message_type': 47 if sticker else 3, 'variants': [
        {'quality': a.get('variant') if a.get('variant') in {'original', 'standard', 'thumbnail'} else 'unknown',
         'state': a.get('state') if a.get('state') in {'available', 'pending', 'expired', 'unsupported', 'failed'} else 'unknown',
         'bytes': a.get('byteLength') if type(a.get('byteLength')) is int else None,
         'mime': a.get('contentType') if a.get('contentType') in MIMES else None}
        for a in assets if isinstance(a, dict) and a.get('kind') == ('sticker' if sticker else 'image')]}
    def label(value):
        return value if isinstance(value, str) and re.fullmatch('[a-z][a-z0-9_]{0,63}', value) else None
    for asset, summary in zip((a for a in assets if isinstance(a, dict) and a.get('kind') == ('sticker' if sticker else 'image')), result['variants']):
        job_id = asset.get('jobId')
        if isinstance(job_id, str) and re.fullmatch('job_[0-9a-f-]{36}', job_id):
            job = client.request('GET', client.prefix + '/jobs/' + job_id, deadline)
            summary['job'] = {'state': label(job.get('state')), 'phase': label(job.get('phase')), 'reason': label(job.get('reason')),
                              'completed_bytes': job.get('completedBytes') if type(job.get('completedBytes')) is int else None,
                              'total_bytes': job.get('totalBytes') if type(job.get('totalBytes')) is int else None}
    if download:
        if sticker:
            raise ImageError('sticker_unavailable')
        image = client.read(candidate['id'], deadline, retry=True)
        result['verified'] = {k: image[k] for k in ('bytes', 'sha256', 'mime', 'quality')}
    return result


class Client:
    def __init__(self, account, token, *, port=26174, on_wait=lambda _: None, on_done=lambda _: None):
        if account != 'wechat-primary':
            raise ImageError('image_account')
        self.prefix, self.token, self.port = '/v1/accounts/' + account, token, port
        self.downloads = 0
        self._read_requests_left = None
        self.on_wait, self.on_done = on_wait, on_done

    def request(self, method, path, deadline, body=None, *, binary=False):
        # Native media discovery can wait for the account lock. Only transient
        # GET failures are repeated; all attempts share the caller's deadline.
        for attempt in range(3 if method == 'GET' else 1):
            if self._read_requests_left is not None:
                # Keep one attempt available to cancel a bound active download.
                reserve = 0 if method == 'POST' and path.endswith('/cancel') else 1
                if self._read_requests_left <= reserve:
                    raise ImageError('image_timeout')
                self._read_requests_left -= 1
            try:
                return self._request(method, path, deadline, body, binary=binary)
            except ImageError as error:
                remaining = deadline - time.monotonic()
                if (method != 'GET' or attempt == 2 or not getattr(error, 'transient', False)
                        or remaining <= 0):
                    raise
                time.sleep(min(.25, remaining))

    def _request(self, method, path, deadline, body=None, *, binary=False):
        remaining = min(20 if binary or method == 'GET' else 8, deadline - time.monotonic())
        if remaining <= 0:
            raise ImageError('image_timeout')
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=remaining)
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept-Encoding': 'identity'}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, encoded, headers)
            response = connection.getresponse()
            length = response.getheader('Content-Length')
            limit = MAX_IMAGE if binary else 65536
            if (length is not None and (not re.fullmatch('[0-9]{1,9}', length) or int(length) > limit)
                    or response.getheader('Content-Encoding', 'identity') != 'identity'):
                raise ImageError('image_response_size')
            raw = response.read(limit + 1)
            if len(raw) > limit or length is not None and len(raw) != int(length):
                raise ImageError('image_response_size')
            if response.status not in {200, 202}:
                # Only fixed public error labels may leave the Native API;
                # never expose XML, CDN URLs or arbitrary server messages.
                for code in ('sticker_source_not_allowed', 'unsupported_sticker_source', 'media_metadata_invalid',
                             'media_metadata_unavailable', 'unsupported_media_transport'):
                    if code.encode() in raw:
                        raise ImageError(code)
                failure = ImageError('image_native_rejected')
                failure.native_http_status = response.status
                try:
                    code = json.loads(raw).get('error', {}).get('code')
                    if isinstance(code, str) and re.fullmatch('[a-z][a-z0-9_]{0,63}', code):
                        failure.native_code = code
                except (ValueError, TypeError, AttributeError):
                    pass
                raise failure
            mime = response.getheader('Content-Type', '').split(';')[0].lower()
            if binary:
                if mime not in MIMES:
                    raise ImageError('image_content_type')
                return raw, mime
            if mime != 'application/json':
                raise ImageError('image_content_type')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ImageError('image_native_shape')
            return value
        except (OSError, http.client.HTTPException, ValueError) as error:
            if isinstance(error, ImageError):
                raise
            failure = ImageError('image_transport')
            failure.transient = isinstance(error, (OSError, http.client.HTTPException))
            raise failure from None
        finally:
            connection.close()

    def assets(self, identity, deadline):
        if not isinstance(identity, str) or not 0 < len(identity.encode()) <= 512:
            raise ImageError('image_identity')
        value = self.request('GET', self.prefix + '/messages/' + quote(identity, safe='') + '/media', deadline)
        items = value.get('items')
        if not isinstance(items, list) or len(items) > 8:
            raise ImageError('image_assets')
        return items

    def quoted_source(self, identity, room, deadline):
        if not isinstance(identity, str) or not 0 < len(identity.encode()) <= 512:
            raise ImageError('image_identity')
        value = self.request('GET', self.prefix + '/messages/' + quote(identity, safe=''), deadline)
        if (not isinstance(value, dict) or value.get('accountId') != 'wechat-primary'
                or value.get('id') != identity or value.get('threadId') != room
                or value.get('type') != 49 or value.get('appMessageType') != 57):
            raise ImageError('image_quote_binding')
        reference = value.get('quote')
        # Native 1.2 exposes only the received parent's embedded quote content.
        # Its media belongs to that parent; never substitute an original row.
        if (isinstance(reference, dict) and type(reference.get('type')) is int
                and reference['type'] == 3 and reference.get('state') == 'embedded'
                and reference.get('embeddedImage') is True):
            return identity
        raise ImageError('image_quote_binding')

    def read(self, identity, deadline, *, retry=False):
        self._read_requests_left = 120
        try:
            return self._read(identity, deadline, retry=retry)
        finally:
            self._read_requests_left = None

    def _read(self, identity, deadline, *, retry=False):
        deadline = min(deadline, time.monotonic() + 60)
        items = self.assets(identity, deadline)
        asset_ids = set()
        for item in items:
            if (not isinstance(item, dict) or item.get('accountId') != 'wechat-primary'
                    or item.get('messageId') != identity or item.get('kind') != 'image'
                    or item.get('variant') not in ('original', 'standard', 'thumbnail')):
                raise ImageError('image_asset_binding')
            self.asset_id(item.get('id'))
            if item['id'] in asset_ids:
                raise ImageError('image_asset_binding')
            asset_ids.add(item['id'])
        # Reserve ten requests for metadata, starts, content, and cancellation.
        poll_budget = [110]
        for quality in ('original', 'standard'):
            candidates = [item for item in items if item.get('variant') == quality]
            if len(candidates) > 1:
                raise ImageError('image_asset_binding')
            if not candidates:
                continue
            asset = candidates[0]
            size = self.asset_metadata(asset, identity)
            state = asset.get('state')
            if state in {'expired', 'unsupported'} or state == 'failed' and not (retry or self.retryable_transport(asset)):
                continue
            try:
                if size is not None and size > MAX_IMAGE:
                    raise ImageError('image_oversize')
                if state == 'available':
                    return self.content(asset, identity, deadline)
                if state != 'pending' and not (state == 'failed' and (retry or self.retryable_transport(asset))):
                    raise ImageError('image_asset_binding')
                return self.transfer(asset, identity, quality, deadline, poll_budget, retry=retry)
            except ImageError as error:
                # Only locally classified download/transport failures permit fallback.
                # Native rejection codes may indicate changed source identity or
                # integrity; unknown codes must retain the original rejection.
                if str(error) not in {'image_download_failed', 'image_transport', 'image_oversize'}:
                    raise
        raise ImageError('image_unavailable')

    @staticmethod
    def retryable_transport(value):
        return value.get('reason') == 'transport_unavailable' and value.get('retryable') is True

    def transfer(self, asset, identity, quality, deadline, poll_budget, *, retry):
        if self.downloads >= 1000000:
            raise ImageError('image_unavailable')
        self.downloads += 1
        old_job = asset.get('jobId')
        recovering = asset.get('state') == 'failed' and (retry or self.retryable_transport(asset))
        if recovering:
            if not isinstance(old_job, str) or not re.fullmatch('job_[0-9a-f-]{36}', old_job):
                raise ImageError('image_job_identity')
            # Automatic Native downloads can fail before the bridge acquires a
            # CDN lease. Rebind the existing download before any retry mutation.
            job = self.request('GET', self.prefix + '/jobs/' + old_job, deadline)
        else:
            job = self.request('POST', self.prefix + '/messages/' + quote(identity, safe='') + '/media-transfers', deadline,
                               {'messageId': identity, 'assetId': asset['id'], 'quality': quality})
        job_id = job.get('id')
        if (not isinstance(job_id, str) or not re.fullmatch('job_[0-9a-f-]{36}', job_id)
                or recovering and job_id != old_job):
            raise ImageError('image_job_identity')
        retried = False
        job_active = False
        try:
            while poll_budget[0] > 0:
                poll_budget[0] -= 1
                if job.get('id') != job_id:
                    raise ImageError('image_job_identity')
                if job.get('accountId') != 'wechat-primary' or job.get('kind') != 'download' or job.get('assetId') != asset['id']:
                    raise ImageError('image_job_binding')
                state = job.get('state')
                if state not in ('available', 'failed', 'cancelled', 'expired', 'unsupported', 'queued', 'running', 'pending'):
                    raise ImageError('image_job_binding')
                job_active = state in ('queued', 'running', 'pending')
                total = job.get('totalBytes')
                done = job.get('completedBytes')
                if (done is not None and (type(done) is not int or done < 0)
                        or job_active and done is None
                        or total is not None and (type(total) is not int or total <= 0)
                        or done is not None and total is not None and done > total):
                    raise ImageError('image_download_size')
                if state == 'available':
                    completed = self.request('GET', self.prefix + '/media/' + asset['id'], deadline)
                    if completed.get('id') != asset['id'] or completed.get('actualVariant') != quality:
                        raise ImageError('image_asset_binding')
                    return self.content(completed, identity, deadline)
                if (state == 'failed' and not retried and job.get('retryable') is True
                        and (retry or self.retryable_transport(job))
                        and type(job.get('attempts')) is int and 0 <= job['attempts'] < 3):
                    retried = True
                    self.on_wait(job_id)
                    job_active = True  # Retry may be accepted even if its response is lost.
                    job = self.request('POST', self.prefix + '/jobs/' + job_id + '/retry', deadline, {})
                    continue
                if state in ('failed', 'cancelled', 'expired', 'unsupported'):
                    raise ImageError('image_download_failed')
                # Transfer totals may include encryption padding. Only a valid
                # announcement beyond that allowance proves the image too large.
                if done > MAX_IMAGE + 32 or total is not None and total > MAX_IMAGE + 32:
                    raise ImageError('image_oversize')
                self.on_wait(job_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ImageError('image_timeout')
                time.sleep(min(.6, remaining))
                job = self.request('GET', self.prefix + '/jobs/' + job_id, deadline)
            raise ImageError('image_timeout')
        except ImageError as error:
            # An available asset needs no cancellation when only its validated
            # byte length prevents use; active oversize jobs still stop promptly.
            if job_active:
                try:
                    self.request('POST', self.prefix + '/jobs/' + job_id + '/cancel', min(deadline, time.monotonic() + 2), {})
                except ImageError:
                    pass
            raise
        finally:
            self.on_done(job_id)

    @staticmethod
    def asset_id(value):
        if not isinstance(value, str) or not re.fullmatch('asset_[0-9a-f]{40}', value):
            raise ImageError('image_asset_identity')

    def asset_metadata(self, asset, identity):
        self.asset_id(asset.get('id'))
        available = asset.get('state') == 'available'
        size, mime, digest = asset.get('byteLength'), asset.get('contentType'), asset.get('sha256')
        if (asset.get('accountId') != 'wechat-primary' or asset.get('messageId') != identity
                or asset.get('kind') != 'image' or asset.get('variant') not in ('thumbnail', 'standard', 'original')
                or asset.get('state') not in ('available', 'pending', 'failed', 'expired', 'unsupported')
                or available and asset.get('actualVariant') != asset['variant']
                or (available or size is not None) and (type(size) is not int or size <= 0)
                or (available or mime is not None) and (not isinstance(mime, str) or mime not in MIMES)
                or (available or digest is not None) and (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest))):
            raise ImageError('image_asset_binding')
        return size

    def content(self, asset, identity, deadline):
        size = self.asset_metadata(asset, identity)
        if asset.get('state') != 'available':
            raise ImageError('image_asset_binding')
        if size > MAX_IMAGE:
            raise ImageError('image_oversize')
        raw, mime = self.request('GET', self.prefix + '/media/' + asset['id'] + '/content', deadline, binary=True)
        result = payload(raw, mime, asset['actualVariant'])
        if result['sha256'] != asset.get('sha256') or result['bytes'] != asset['byteLength'] or mime != asset['contentType']:
            raise ImageError('image_asset_integrity')
        return result
