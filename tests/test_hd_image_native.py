import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('hd_native', Path(__file__).resolve().parents[1] / 'scripts/image_native.py')
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5P8AAAAASUVORK5CYII=')
STANDARD_PNG = PNG + b'standard-fixture'
JOB = 'job_11111111-2222-4333-8444-555555555555'

def asset(quality, state='available', **changes):
    raw = STANDARD_PNG if quality == 'standard' else PNG
    value = dict(id='asset_' + {'original':'a','standard':'b','thumbnail':'c'}[quality] * 40,
                accountId='wechat-primary', messageId='source', kind='image', variant=quality,
                actualVariant=quality, state=state, byteLength=len(raw), contentType='image/png',
                sha256=hashlib.sha256(raw).hexdigest())
    value.update(changes)
    return value

class HTTPReadRecovery(unittest.TestCase):
    def connection(self, body=b'', mime='application/json', *, failure=None, status=200):
        response=Mock(status=status)
        headers={'Content-Type':mime,'Content-Length':str(len(body))}
        response.getheader.side_effect=lambda name,default=None:headers.get(name,default)
        response.read.return_value=body
        connection=Mock()
        if failure:connection.getresponse.side_effect=failure
        else:connection.getresponse.return_value=response
        return connection

    def test_transient_metadata_read_recovers_original_under_same_deadline(self):
        connections=[self.connection(failure=TimeoutError()),
                     self.connection(json.dumps({'items':[asset('original')]}).encode()),
                     self.connection(PNG,'image/png')]
        client=native.Client('wechat-primary','synthetic')
        with patch.object(native.http.client,'HTTPConnection',side_effect=connections) as connect, patch.object(native.time,'sleep'):
            image=client.read('source',time.monotonic()+60)
        self.assertEqual(image['quality'],'original')
        self.assertEqual(image['sha256'],hashlib.sha256(PNG).hexdigest())
        self.assertEqual(connect.call_count,3)
        for c in connections:
            c.close.assert_called_once()
            self.assertEqual(c.request.call_args.args[0],'GET')
        self.assertGreater(connect.call_args_list[0].kwargs['timeout'],8)

    def test_post_is_single_attempt_even_on_transport_failure(self):
        connection=self.connection(failure=TimeoutError())
        with patch.object(native.http.client,'HTTPConnection',return_value=connection) as connect:
            with self.assertRaisesRegex(native.ImageError,'image_transport'):
                native.Client('wechat-primary','synthetic').request('POST','/retry',time.monotonic()+60,{})
        connect.assert_called_once();connection.close.assert_called_once()

    def test_get_retry_is_bounded_and_never_renews_the_deadline(self):
        for expires in (False,True):
            with self.subTest(expires=expires):
                now=[100.0]
                connection=self.connection(failure=ConnectionResetError())
                def fail():
                    if expires:now[0]=102.0
                    raise ConnectionResetError()
                connection.getresponse.side_effect=fail
                with patch.object(native.time,'monotonic',side_effect=lambda:now[0]), patch.object(native.time,'sleep'), patch.object(native.http.client,'HTTPConnection',return_value=connection) as connect:
                    with self.assertRaises(native.ImageError):
                        native.Client('wechat-primary','synthetic').request('GET','/media',101.0)
                self.assertEqual(connect.call_count,1 if expires else 3)
                self.assertTrue(all(0<c.kwargs['timeout']<=1 for c in connect.call_args_list))

    def test_exhausted_read_budget_reserves_one_bound_cancellation(self):
        client=native.Client('wechat-primary','synthetic');calls=[];polls=[0]
        original=asset('original','pending')
        def request(method,path,deadline,body=None,*,binary=False):
            calls.append((method,path))
            if path.endswith('/media'):return {'items':[original]}
            if path.endswith('/media-transfers'):
                return dict(id=JOB,accountId='wechat-primary',kind='download',assetId=original['id'],state='running',completedBytes=0,totalBytes=len(PNG))
            if path.endswith('/cancel'):return {}
            polls[0]+=1
            if polls[0]%3==0:
                return dict(id=JOB,accountId='wechat-primary',kind='download',assetId=original['id'],state='running',completedBytes=0,totalBytes=len(PNG))
            failure=native.ImageError('image_transport');failure.transient=True;raise failure
        client._request=request;client.on_done=Mock()
        with patch.object(native.time,'sleep'):
            with self.assertRaises(native.ImageError):client.read('source',time.monotonic()+60)
        self.assertEqual(len(calls),120)
        self.assertEqual(calls[-1],('POST',client.prefix+'/jobs/'+JOB+'/cancel'))
        self.assertEqual(sum(path.endswith('/cancel') for _,path in calls),1)
        self.assertIsNone(client._read_requests_left)
        client.on_done.assert_called_once_with(JOB)

    def test_native_rejection_and_invalid_json_are_not_retried(self):
        for body,status in ((b'{bad',200),(b'{"error":{"code":"media_source_changed"}}',409)):
            with self.subTest(status=status):
                connection=self.connection(body,status=status)
                with patch.object(native.http.client,'HTTPConnection',return_value=connection) as connect:
                    with self.assertRaises(native.ImageError):
                        native.Client('wechat-primary','synthetic').request('GET','/media',time.monotonic()+60)
                connect.assert_called_once()

class HDNative(unittest.TestCase):
    def client(self, items, *, completed=None, job_changes=None, binary_error=None):
        client = native.Client('wechat-primary', 'synthetic')
        self.calls = []
        self.deadlines = []
        def request(method, path, deadline, body=None, *, binary=False):
            self.calls.append((method, path, body))
            self.deadlines.append(deadline)
            if binary:
                if binary_error and '/media/' + items[0]['id'] + '/' in path:
                    raise native.ImageError(binary_error)
                return (STANDARD_PNG if asset('standard')['id'] in path else PNG), 'image/png'
            if path.endswith('/media'):
                return {'items': items}
            if '/media-transfers' in path or '/jobs/' in path:
                job = dict(id=JOB, accountId='wechat-primary', kind='download', assetId=items[0]['id'], state='available')
                if job_changes:
                    job.update(job_changes(method))
                return job
            return completed or dict(items[0], state='available')
        client.request = request
        return client

    def test_pending_original_beats_cached_lower_variants(self):
        for quality in ('thumbnail', 'standard'):
            with self.subTest(quality=quality):
                original = asset('original', 'pending')
                client = self.client([original, asset(quality)])
                result = client.read('source', time.monotonic()+60)
                self.assertEqual(result['quality'], 'original')
                self.assertEqual(self.calls[1][2], {'messageId':'source','assetId':original['id'],'quality':'original'})
                self.assertEqual(result['sha256'], hashlib.sha256(PNG).hexdigest())

    def test_standard_fallback_for_unavailable_and_download_failure(self):
        for state in ('expired', 'unsupported', 'failed', 'pending'):
            with self.subTest(state=state):
                client = self.client([asset('original', state), asset('standard')], job_changes=lambda _: {'state':'failed'})
                self.assertEqual(client.read('source', time.monotonic()+60)['quality'], 'standard')

    def assert_standard_payload(self, result):
        self.assertEqual(result['quality'], 'standard')
        self.assertEqual(result['sha256'], hashlib.sha256(STANDARD_PNG).hexdigest())
        self.assertEqual(result['bytes'], len(STANDARD_PNG))
        self.assertEqual(base64.b64decode(result['base64']), STANDARD_PNG)
        content_paths = [path for _, path, _ in self.calls if path.endswith('/content')]
        self.assertEqual(content_paths, [native.Client('wechat-primary', 'synthetic').prefix
                                        + '/media/' + asset('standard')['id'] + '/content'])

    def assert_no_standard_request(self):
        standard_id = asset('standard')['id']
        self.assertFalse(any(standard_id in path or body and body.get('assetId') == standard_id
                             for _, path, body in self.calls))

    def test_pending_oversize_total_falls_back_and_releases_bound_job(self):
        for total in (native.MAX_IMAGE + 33, native.MAX_IMAGE * 2):
            for announce_on in ('POST', 'GET'):
                with self.subTest(total=total, announce_on=announce_on):
                    original = asset('original', 'pending')
                    def progress(method):
                        return {'state': 'pending', 'completedBytes': 0,
                                'totalBytes': total if method == announce_on else len(PNG)}
                    client = self.client([original, asset('standard')], job_changes=progress)
                    finished = []
                    client.on_done = finished.append
                    request = client.request
                    def verify_done(method, path, deadline, body=None, *, binary=False):
                        if asset('standard')['id'] in path:
                            self.assertEqual(finished, [JOB])
                        return request(method, path, deadline, body, binary=binary)
                    client.request = verify_done
                    before = time.monotonic()
                    with patch.object(native.time, 'sleep'):
                        result = client.read('source', before + 100)
                    self.assert_standard_payload(result)
                    self.assertEqual(self.calls[1][2], {'messageId': 'source', 'assetId': original['id'], 'quality': 'original'})
                    self.assertEqual([(method, path) for method, path, _ in self.calls if path.endswith('/cancel')],
                                     [('POST', client.prefix + '/jobs/' + JOB + '/cancel')])
                    self.assertTrue(all(deadline <= before + 60.01 for deadline in self.deadlines))
                    self.assertEqual(self.deadlines[-1], self.deadlines[0])
                    self.assertLessEqual(len(self.calls), 120)

    def test_completed_oversize_original_falls_back_without_cancelling_completed_job(self):
        original = asset('original', 'pending')
        completed = asset('original', byteLength=native.MAX_IMAGE + 1)
        client = self.client([original, asset('standard')], completed=completed)
        client.on_done = Mock()
        self.assert_standard_payload(client.read('source', time.monotonic() + 60))
        client.on_done.assert_called_once_with(JOB)
        self.assertFalse(any(path.endswith('/cancel') for _, path, _ in self.calls))

    def test_cached_oversize_original_falls_back_after_metadata_validation(self):
        client = self.client([asset('original', byteLength=native.MAX_IMAGE + 1), asset('standard')])
        self.assert_standard_payload(client.read('source', time.monotonic() + 60))
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))

    def test_encrypted_progress_overhead_does_not_discard_original(self):
        for total in (native.MAX_IMAGE + 1, native.MAX_IMAGE + 16, native.MAX_IMAGE + 32):
            with self.subTest(total=total):
                client = self.client([asset('original', 'pending'), asset('standard')],
                                     job_changes=lambda method: {'state': 'pending', 'completedBytes': 0, 'totalBytes': total}
                                     if method == 'POST' else {'state': 'available'})
                with patch.object(native.time, 'sleep'):
                    result = client.read('source', time.monotonic() + 60)
                self.assertEqual(result['quality'], 'original')
                self.assertEqual(result['sha256'], hashlib.sha256(PNG).hexdigest())
                self.assert_no_standard_request()

    def test_oversize_progress_with_invalid_job_fields_never_falls_back(self):
        changes = ({'completedBytes': True}, {'completedBytes': -1}, {'completedBytes': None},
                   {'completedBytes': []},
                   {'completedBytes': native.MAX_IMAGE + 34},
                   {'totalBytes': True}, {'totalBytes': -1}, {'totalBytes': 0}, {'totalBytes': 'large'},
                   {'totalBytes': {}},
                   {'accountId': 'other'}, {'kind': 'upload'}, {'assetId': asset('standard')['id']},
                   {'id': 'bad-job'}, {'state': 'unknown'})
        for change in changes:
            with self.subTest(change=change):
                progress = dict(state='pending', completedBytes=0, totalBytes=native.MAX_IMAGE + 33)
                progress.update(change)
                client = self.client([asset('original', 'pending'), asset('standard')],
                                     job_changes=lambda _, progress=progress: progress)
                with patch.object(native.time, 'sleep'):
                    with self.assertRaises(native.ImageError):
                        client.read('source', time.monotonic() + 60)
                self.assert_no_standard_request()

    def test_oversize_asset_with_invalid_metadata_never_falls_back(self):
        changes = ({'byteLength': True}, {'byteLength': -1}, {'byteLength': 0},
                   {'byteLength': None}, {'byteLength': 'large'}, {'byteLength': []},
                   {'id': asset('standard')['id']}, {'accountId': 'other'}, {'messageId': 'other'},
                   {'kind': 'sticker'}, {'variant': 'unknown'}, {'actualVariant': 'thumbnail'},
                   {'actualVariant': 'unknown'}, {'actualVariant': None}, {'state': 'unknown'},
                   {'contentType': 'unknown'}, {'contentType': []},
                   {'sha256': None}, {'sha256': True}, {'sha256': 'bad'}, {'sha256': []})
        for cached in (False, True):
            for change in changes:
                with self.subTest(cached=cached, change=change):
                    completed = asset('original', byteLength=native.MAX_IMAGE + 1)
                    completed.update(change)
                    original = completed if cached else asset('original', 'pending')
                    client = self.client([original, asset('standard')], completed=completed)
                    with self.assertRaises(native.ImageError):
                        client.read('source', time.monotonic() + 60)
                    self.assert_no_standard_request()

    def test_pending_asset_oversize_does_not_hide_malformed_metadata(self):
        for change in ({'sha256': True}, {'contentType': []}, {'state': 'unknown'}):
            with self.subTest(change=change):
                original = asset('original', 'pending', byteLength=native.MAX_IMAGE + 1)
                original.update(change)
                client = self.client([original, asset('standard')])
                with self.assertRaises(native.ImageError):
                    client.read('source', time.monotonic() + 60)
                self.assert_no_standard_request()

    def test_oversize_cancel_failure_still_releases_job_before_fallback(self):
        client = self.client([asset('original', 'pending'), asset('standard')],
                             job_changes=lambda _: {'state': 'pending', 'completedBytes': 0,
                                                    'totalBytes': native.MAX_IMAGE + 33})
        client.on_done = Mock()
        request = client.request
        def reject_cancel(method, path, deadline, body=None, *, binary=False):
            if path.endswith('/cancel'):
                raise native.ImageError('image_native_rejected')
            if asset('standard')['id'] in path:
                client.on_done.assert_called_once_with(JOB)
            return request(method, path, deadline, body, binary=binary)
        client.request = reject_cancel
        self.assert_standard_payload(client.read('source', time.monotonic() + 60))
        client.on_done.assert_called_once_with(JOB)

    def test_retryable_transport_failure_recovers_original_once(self):
        for initially_failed in (True, False):
            with self.subTest(initially_failed=initially_failed):
                original = asset('original', 'failed' if initially_failed else 'pending',
                                 reason='transport_unavailable', retryable=True, jobId=JOB)
                client = self.client([original, asset('standard')])
                request = client.request
                def failed_then_retry(method, path, deadline, body=None, *, binary=False):
                    if path.endswith('/retry'):
                        self.calls.append((method, path, body))
                        return dict(id=JOB, accountId='wechat-primary', kind='download',
                                    assetId=original['id'], state='available')
                    if (path.endswith('/jobs/'+JOB) or path.endswith('/media-transfers')):
                        self.calls.append((method, path, body))
                        return dict(id=JOB, accountId='wechat-primary', kind='download',
                                    assetId=original['id'], state='failed', reason='transport_unavailable',
                                    retryable=True, attempts=1)
                    return request(method,path,deadline,body,binary=binary)
                client.request = failed_then_retry
                client.on_done = Mock()
                result = client.read('source', time.monotonic()+60)
                self.assertEqual(result['quality'], 'original')
                self.assertEqual(result['sha256'],hashlib.sha256(PNG).hexdigest())
                self.assertEqual(sum(path.endswith('/retry') for _,path,_ in self.calls),1)
                self.assert_no_standard_request()
                client.on_done.assert_called_once_with(JOB)

    def test_transport_retry_stops_after_one_attempt_and_respects_native_budget(self):
        for attempts in (1, 3, True, None):
            with self.subTest(attempts=attempts):
                original=asset('original','failed',reason='transport_unavailable',retryable=True,jobId=JOB)
                client=self.client([original,asset('standard')],job_changes=lambda _,attempts=attempts: dict(state='failed',reason='transport_unavailable',retryable=True,attempts=attempts))
                self.assertEqual(client.read('source',time.monotonic()+60)['quality'],'standard')
                self.assertEqual(sum(path.endswith('/retry') for _,path,_ in self.calls),int(type(attempts) is int and attempts<3))

    def test_transport_retry_revalidates_download_job_before_post(self):
        for change in ({'accountId':'other'}, {'kind':'send'}, {'assetId':asset('standard')['id']}, {'id':'job_aaaaaaaa-2222-4333-8444-555555555555'}):
            with self.subTest(change=change):
                original=asset('original','failed',reason='transport_unavailable',retryable=True,jobId=JOB)
                client=self.client([original,asset('standard')],job_changes=lambda _,change=change: dict(state='failed',reason='transport_unavailable',retryable=True,attempts=1,**change))
                with self.assertRaisesRegex(native.ImageError,'image_job_'):
                    client.read('source',time.monotonic()+60)
                self.assertFalse(any(method=='POST' for method,_,_ in self.calls))

    def test_lost_retry_response_cancels_only_the_bound_download(self):
        original=asset('original','failed',reason='transport_unavailable',retryable=True,jobId=JOB)
        client=self.client([original,asset('standard')],job_changes=lambda _: dict(state='failed',reason='transport_unavailable',retryable=True,attempts=1))
        request=client.request
        def lost_response(method,path,deadline,body=None,*,binary=False):
            if path.endswith('/retry'):
                self.calls.append((method,path,body))
                raise native.ImageError('image_transport')
            return request(method,path,deadline,body,binary=binary)
        client.request=lost_response
        client.on_done=Mock()
        self.assertEqual(client.read('source',time.monotonic()+60)['quality'],'standard')
        self.assertEqual([(method,path) for method,path,_ in self.calls if path.endswith('/cancel')],
                         [('POST',client.prefix+'/jobs/'+JOB+'/cancel')])
        client.on_done.assert_called_once_with(JOB)

    def test_nonretryable_or_nontransport_failure_is_not_retried(self):
        for reason,retryable in (('transport_unavailable',False),('media_integrity_failed',True),('unknown_failure',True)):
            with self.subTest(reason=reason,retryable=retryable):
                original=asset('original','failed',reason=reason,retryable=retryable,jobId=JOB)
                client=self.client([original,asset('standard')])
                self.assertEqual(client.read('source',time.monotonic()+60)['quality'],'standard')
                self.assertTrue(all(method=='GET' for method,_,_ in self.calls))

    def test_thumbnail_only_is_unavailable(self):
        client = self.client([asset('thumbnail')])
        with self.assertRaises(native.ImageError):
            client.read('source', time.monotonic()+60)

    def test_completed_asset_and_quality_mismatch_never_fall_back(self):
        for changes in ({'id':asset('standard')['id']}, {'actualVariant':'thumbnail'}, {'accountId':'other'}, {'messageId':'other'}, {'sha256':'0'*64}):
            with self.subTest(changes=changes):
                original = asset('original', 'pending')
                client = self.client([original, asset('standard')], completed=dict(original, state='available', **changes))
                with self.assertRaises(native.ImageError):
                    client.read('source', time.monotonic()+60)
                self.assertFalse(any('/'+asset('standard')['id']+'/content' in path for _,path,_ in self.calls))

    def test_job_id_cannot_change_during_poll(self):
        client = self.client([asset('original', 'pending'), asset('standard')], job_changes=lambda method: {'state':'pending', 'completedBytes':0, 'totalBytes':len(PNG)} if method=='POST' else {'id':'job_aaaaaaaa-2222-4333-8444-555555555555'})
        with patch.object(native.time, 'sleep'):
            with self.assertRaisesRegex(native.ImageError, 'image_job'):
                client.read('source', time.monotonic()+60)

    def test_long_lived_client_can_download_after_32_reads(self):
        client = self.client([asset('original', 'pending')])
        client.downloads = 32
        self.assertEqual(client.read('source', time.monotonic()+60)['quality'], 'original')

    def test_download_failure_falls_back_and_lease_is_released(self):
        client = self.client([asset('original', 'pending'), asset('standard')], binary_error='image_transport')
        client.on_done = Mock()
        self.assertEqual(client.read('source', time.monotonic()+60)['quality'], 'standard')
        client.on_done.assert_called_once_with(JOB)

    def test_job_binding_mismatch_does_not_fall_back(self):
        for changes in ({'accountId':'other'}, {'kind':'upload'}, {'assetId':asset('standard')['id']}):
            client = self.client([asset('original', 'pending'), asset('standard')], job_changes=lambda _, changes=changes: changes)
            with self.assertRaisesRegex(native.ImageError, 'image_job_binding'):
                client.read('source', time.monotonic()+60)

    def test_polling_obeys_shared_request_bound(self):
        client = self.client([asset('original', 'pending'), asset('standard', 'pending')], job_changes=lambda _: {'state':'pending', 'completedBytes':0, 'totalBytes':len(PNG)})
        with patch.object(native.time, 'sleep'):
            with self.assertRaises(native.ImageError):
                client.read('source', time.monotonic()+60)
        self.assertLessEqual(len(self.calls), 120)

    def test_native_rejection_never_falls_back_for_unproven_error_codes(self):
        for code in ('media_source_changed', 'media_integrity_failed', 'unknown_failure', None):
            with self.subTest(code=code):
                client = self.client([asset('original'), asset('standard')])
                request = client.request
                def reject(method, path, deadline, body=None, *, binary=False):
                    if binary:
                        error = native.ImageError('image_native_rejected')
                        if code is not None:
                            error.native_code = code
                        raise error
                    return request(method, path, deadline, body, binary=binary)
                client.request = reject
                with self.assertRaisesRegex(native.ImageError, 'image_native_rejected'):
                    client.read('source', time.monotonic()+60)
                self.assertEqual(len(self.calls), 1)

    def test_oversized_available_original_with_wrong_variant_rejects(self):
        original = asset('original')
        original.update(byteLength=native.MAX_IMAGE+1, actualVariant='thumbnail')
        client = self.client([original, asset('standard')])
        with self.assertRaisesRegex(native.ImageError, 'image_asset_binding'):
            client.read('source', time.monotonic()+60)

    def test_binary_header_guard_and_timeout(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.getheader.side_effect = lambda key, default=None: str(native.MAX_IMAGE+1) if key=='Content-Length' else default
        with patch.object(native.http.client, 'HTTPConnection', return_value=connection) as constructor:
            with self.assertRaisesRegex(native.ImageError, 'image_response_size'):
                native.Client('wechat-primary','synthetic').request('GET','/content',time.monotonic()+60,binary=True)
        response.read.assert_not_called()
        self.assertGreater(constructor.call_args.kwargs['timeout'], 19)

if __name__ == '__main__':
    unittest.main()
