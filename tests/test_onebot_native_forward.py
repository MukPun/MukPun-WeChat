"""Real Native-worker IPC/HTTP wiring with a synthetic local API only."""
import asyncio
import base64
from datetime import datetime, timezone
import json
import importlib.util
import hashlib
import os
from pathlib import Path
import sys
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from wechat_bridge.forward_wire import MAX_FRAME, encode, decode
from wechat_bridge import image_input as input_images, image_input_wire as input_wire
from wechat_bridge import image_output as output_images
import test_onebot_image_output_native as image_fixture
import test_onebot_voice_native as voice_fixture
from test_onebot_voice_output import wav, segment
from wechat_bridge import voice_output as output_voice


class MediaLockDeadlines(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('forward_worker_deadline_test',
            Path(__file__).resolve().parents[1] / 'scripts/forward_native_worker.py')
        self.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.worker)

    def test_image_output_wait_for_media_slot_expires_without_submission(self):
        self.output_timeout('image')

    def test_voice_output_wait_for_media_slot_expires_without_submission(self):
        self.output_timeout('voice')

    def output_timeout(self, kind):
        lock = threading.Lock()
        lock.acquire()
        reports = []
        worker = self.worker
        target = worker.send_image_command if kind == 'image' else worker.send_voice_command
        sender = worker.image_sender if kind == 'image' else worker.voice_sender
        command = {'requestId': 'a' * 32, 'destination': 'synthetic-peer',
                   'key': 'b' * 32, kind: {'sha256': 'c' * 64}}
        with patch.object(worker, 'emit', reports.append), patch.object(sender, 'Client') as client:
            thread = threading.Thread(target=target, args=({'account': 'wechat-primary', 'token': 't' * 32},
                'synthetic-self', command, time.monotonic() + .03, lock), daemon=True)
            thread.start()
            try:
                thread.join(.3)
                self.assertFalse(thread.is_alive(), 'Media lock wait exceeded admitted command deadline')
                client.assert_not_called()
                self.assertEqual(len(reports), 1)
                self.assertEqual(reports[0]['kind'], 'native_' + kind + '_result')
                self.assertEqual(reports[0]['requestId'], 'a' * 32)
                self.assertEqual(reports[0]['code'], kind + '_timeout')
                self.assertIs(reports[0]['submitted'], False)
                self.assertIs(reports[0]['server_confirmed'], False)
                self.assertIs(reports[0]['ok'], False)
            finally:
                lock.release()
                thread.join(1)

    def test_acquisition_lock_wait_consumes_deadline_without_native_read(self):
        lock = threading.Lock()
        lock.acquire()
        results = queue.Queue(maxsize=1)
        media = Mock()
        thread = threading.Thread(target=self.worker.acquire_input_image,
            args=(media, 'source-1', 'synthetic-peer', False, True,
                  time.monotonic() + .03, lock, results), daemon=True)
        thread.start()
        try:
            thread.join(.3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(str(results.get_nowait()['error']), 'image_timeout')
            media.read.assert_not_called()
            media.quoted_source.assert_not_called()
        finally:
            lock.release()
            thread.join(1)


@unittest.skipUnless(os.name == 'posix', 'Native worker uses POSIX pipe select')
class NativeRoundTrip(unittest.IsolatedAsyncioTestCase):
    async def test_worker_reads_sse_full_text_and_returns_bound_receipt(self):
        await self.exercise(False)

    async def test_group_worker_preserves_text_and_verifies_outgoing_projection(self):
        await self.exercise(True)

    async def test_accepted_text_waits_for_delayed_projection_without_second_post(self):
        await self.exercise(True, text_projection_delay=True)

    async def test_native_at_metadata_without_prefix_is_forwarded(self):
        await self.exercise(True, mention=True)

    async def test_own_and_outgoing_messages_do_not_reach_host_or_hide_next_user_message(self):
        await self.exercise(True, loop_noise=True)

    async def test_private_image_bytes_cross_real_worker_pipe(self):
        await self.exercise(False, image_mode='private')

    async def test_text_send_completes_while_inbound_image_download_is_held(self):
        await self.exercise(False, image_mode='private', inbound_hold=True)

    async def test_full_25_mib_inbound_image_crosses_acknowledged_bounded_pipe(self):
        await self.exercise(False, image_mode='private', inbound_bytes=input_images.MAX_IMAGE)

    async def test_quoted_image_failure_keeps_question_with_fixed_report(self):
        await self.exercise(False, image_mode='quote', inbound_failure=True)

    async def test_group_image_waits_for_same_sender_question(self):
        await self.exercise(True, image_mode='group')

    async def test_quoted_image_and_question_cross_worker_with_same_conversation_binding(self):
        await self.exercise(False, image_mode='quote')

    async def test_ordinary_group_text_crosses_native_pipe_with_context_enabled(self):
        await self.exercise(True, context=True)

    async def test_context_does_not_consume_pending_same_member_image(self):
        await self.exercise(True, image_mode='group', context=True)

    async def test_own_loop_noise_stays_filtered_in_context_mode(self):
        await self.exercise(True, loop_noise=True, context=True)

    async def test_private_image_output_does_not_block_next_sse_message(self):
        await self.exercise(False, image_output=True)

    async def test_group_image_output_has_a_separate_correlated_receipt(self):
        await self.exercise(True, image_output=True)

    async def test_chunked_one_megabyte_image_waits_for_commit_and_keeps_sse_alive(self):
        await self.exercise(True, image_output='chunked')

    async def test_new_group_crosses_native_worker_without_binding_or_wakeup(self):
        await self.exercise(True, context=True, all_groups=True)

    async def test_new_group_native_mentions_and_loop_guard(self):
        await self.exercise(True, mention=True, loop_noise=True, all_groups=True)

    async def test_new_group_image_without_wakeup_is_forwarded(self):
        await self.exercise(True, image_mode='plain_group', all_groups=True)

    async def test_storage_pressure_skips_media_fetch_and_keeps_following_text(self):
        await self.exercise(False, image_mode='private', capacity_blocked=True)

    async def test_private_native_voice_output_crosses_real_worker_pipe(self):
        await self.exercise(False, voice_output=True)

    async def test_group_long_voice_chunks_cross_pipe_and_keep_sse_alive(self):
        await self.exercise(True, voice_output='chunked')

    async def exercise(self, group, mention=False, loop_noise=False, image_mode=None, context=False, image_output=False, all_groups=False, capacity_blocked=False, voice_output=False, text_projection_delay=False, inbound_hold=False, inbound_bytes=None, inbound_failure=False):
        output_enabled = bool(image_output or voice_output)
        contract = output_voice if voice_output else output_images
        output_kind = 'voice' if voice_output else 'image'
        body = ' 原始消息\r\n中文🙂\t '
        if group:
            body = '/ai ' + body + ('模拟长文本入站，逐字验证中文与emoji🙂\n' * 180)
        if context:
            body = '普通聊天\r\n完整中文🙂\t '
        if mention:
            body = '@机器人\u2005请回复：AT成功'
        if image_mode:
            body = '/ai 图片里是什么？' if group else '用一句话描述这张图。' if image_mode == 'quote' else ''
        if image_mode == 'plain_group':
            body = ''
        answer = ' 完整答复\r\n中文🙂\t '
        if group:
            answer += '模拟出站长文本逐字校验🙂\n' * 100
        destination = 'synthetic-room@chatroom' if group else 'synthetic-peer'
        room = 'synthetic-room@chatroom'
        if all_groups:
            destination = 'new-unbound-room@chatroom'
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5P8AAAAASUVORK5CYII=')
        if inbound_bytes:
            png = await asyncio.to_thread(lambda raw=png: raw[:-12]
                + image_fixture.chunk(b'tEXt', b'x' * (inbound_bytes - len(raw) - 12)) + raw[-12:])
            self.assertEqual(len(png), inbound_bytes)
        asset_id = 'asset_' + 'a' * 40
        requests, posts = [], []
        text_send_id = str(uuid.uuid4())
        projection_polls = []
        dropped = []
        seed_id = '11111111-2222-4333-8444-555555555555'
        seed_text = '此前已确认的答复'
        source_cursor = '5' if loop_noise else '4' if image_mode == 'group' and context else '3' if image_mode == 'group' else '2'
        created = datetime.now(timezone.utc).isoformat()
        output_started, release_image = asyncio.Event(), asyncio.Event()
        input_started, release_input = asyncio.Event(), asyncio.Event()
        input_state = None
        input_frames, input_chunks = [], []
        input_errors = []
        receipt = None
        image_api = image_fixture.Fake(jobs=[image_fixture.job(destination=destination),
                                           image_fixture.job('accepted', destination=destination)], destination=destination)
        outgoing = image_fixture.IMAGE
        if image_output == 'chunked':
            raw = image_fixture.PNG
            raw = raw[:-12] + image_fixture.chunk(b'tEXt', b'x' * (output_images.MAX_IMAGE - len(raw) - 12)) + raw[-12:]
            outgoing = output_images.payload(raw)
            image_api.source.update(byteLength=outgoing['bytes'], sha256=outgoing['sha256'])
            for job in image_api.jobs:
                job['result']['imagePreparation']['sourceSha256'] = outgoing['sha256']
        image_api.message['senderId'] = 'synthetic-self'
        if voice_output:
            outgoing = output_voice.from_segment(segment(wav(8000, frames=8000 * (61 if voice_output == 'chunked' else 1))))
            image_api = voice_fixture.Fake(outgoing, destination)
            image_api.sender = 'synthetic-self'

        async def server(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                first = head.split(b'\r\n')[0].decode()
                requests.append(first)
                headers = dict(line.lower().split(b': ', 1) for line in head.split(b'\r\n')[1:-2])
                if 'events/stream?after=1 ' in first:
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n')
                    events = [('2', 'outgoing', 'not-fetched'), ('3', 'incoming', 'own-echo'),
                              ('4', 'incoming', 'outgoing-projection')] if loop_noise else []
                    if image_mode == 'group':
                        events.append(('2', 'incoming', 'image-source'))
                        if context:
                            events.append(('3', 'incoming', 'context-1'))
                    events.append((source_cursor, 'incoming', 'source-1'))
                    for cursor, direction, identity in events:
                        value = {'cursor': cursor, 'kind': 'message.committed', 'createdAt': created,
                                 'data': {'threadId': destination, 'direction': direction, 'origin': 'live', 'messageId': identity}}
                        writer.write(('id: ' + cursor + '\ndata: ' + json.dumps(value) + '\n\n').encode())
                    await writer.drain()
                    if output_enabled or capacity_blocked or inbound_hold or inbound_bytes:
                        if output_enabled:
                            await output_started.wait()
                        if inbound_hold:
                            await input_started.wait()
                        value = {'cursor': '3', 'kind': 'message.committed', 'createdAt': created,
                                 'data': {'threadId': destination, 'direction': 'incoming', 'origin': 'live',
                                          'messageId': 'source-2'}}
                        writer.write(('id: 3\ndata: ' + json.dumps(value) + '\n\n').encode())
                        await writer.drain()
                    await reader.read()
                    return
                if '/contacts?search=synthetic-peer&' in first:
                    value = {'items': [{'id': 'synthetic-peer', 'username': 'synthetic-peer', 'nickname': '真实昵称🙂',
                                        'displayName': '好友备注'}], 'nextCursor': None}
                elif '/members?search=synthetic-peer&' in first:
                    value = {'items': [{'id': 'synthetic-peer', 'username': 'synthetic-peer', 'displayName': '群展示名',
                                        'roomDisplayName': '本群名片'}], 'nextCursor': None}
                elif '/contacts?' in first:
                    value = {'items': [{'id': 'synthetic-peer', 'username': 'synthetic-peer', 'displayName': '测试联系人'}],
                             'coverage': {'partial': False}, 'asOfEventCursor': '1', 'nextCursor': None}
                elif '/groups?' in first:
                    value = {'items': [{'id': room, 'roomId': room, 'displayName': '测试群'}],
                             'coverage': {'partial': False}, 'asOfEventCursor': '1', 'nextCursor': None}
                elif '/messages/source-1 ' in first or (output_enabled or capacity_blocked or inbound_hold or inbound_bytes) and '/messages/source-2 ' in first:
                    value = {'id': 'source-1', 'accountId': 'wechat-primary', 'threadId': destination,
                             'senderId': 'synthetic-peer', 'direction': 'incoming',
                             'type': 49 if image_mode == 'quote' else 3 if image_mode in {'private', 'plain_group'} else 1,
                             'body': None if image_mode in {'private', 'plain_group'} else body, 'createdAt': created}
                    if '/messages/source-2 ' in first:
                        value.update(id='source-2', body=body + '2', type=1)
                    if mention:
                        value['mentions'] = {'state': 'known', 'source': 'msgsource.atuserlist',
                                             'self': True, 'all': None, 'userIds': ['synthetic-self']}
                    if image_mode == 'quote':
                        value.update(appMessageType=57, quote={'type': 3, 'serverId': '12345',
                                     'state': 'embedded', 'embeddedImage': True})
                elif '/messages/image-source ' in first:
                    value = {'id': 'image-source', 'accountId': 'wechat-primary', 'threadId': destination,
                             'senderId': 'synthetic-peer', 'direction': 'incoming', 'type': 3, 'body': None, 'createdAt': created}
                elif '/messages/context-1 ' in first:
                    value = {'id': 'context-1', 'accountId': 'wechat-primary', 'threadId': destination,
                             'senderId': 'synthetic-peer', 'direction': 'incoming', 'type': 1,
                             'body': '普通聊天不应吃掉图片🙂', 'createdAt': created}
                elif output_enabled and ('/jobs/' in first or '/messages/projected-' in first):
                    if '/jobs/' in first:
                        await release_image.wait()
                    value = json.loads(image_api.request('GET', first.split()[1], timeout=3).body)
                elif output_enabled and first.startswith('POST /v1/accounts/wechat-primary/uploads '):
                    raw = await reader.readexactly(int(headers[b'content-length']))
                    value = json.loads(image_api.request('POST', image_fixture.PREFIX + '/uploads', timeout=3,
                        headers={'Content-Type': headers[b'content-type'].decode()}, body=raw).body)
                elif '/messages/' in first and '/media ' in first:
                    value = {'items': [{'id': asset_id, 'accountId': 'wechat-primary',
                             'messageId': 'image-source' if image_mode == 'group' else 'source-1', 'kind': 'image', 'variant': 'original',
                             'actualVariant': 'original', 'state': 'expired' if inbound_failure else 'available', 'contentType': 'image/png',
                             'byteLength': len(png), 'sha256': hashlib.sha256(png).hexdigest()}]}
                elif '/media/' + asset_id + '/content ' in first:
                    input_started.set()
                    if inbound_hold:
                        await release_input.wait()
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: '
                                 + str(len(png)).encode() + b'\r\nConnection: close\r\n\r\n' + png)
                    await writer.drain()
                    return
                elif '/messages/own-echo ' in first or '/messages/outgoing-projection ' in first:
                    own_echo = '/messages/own-echo ' in first
                    value = {'id': 'own-echo' if own_echo else 'outgoing-projection', 'accountId': 'wechat-primary',
                             'threadId': destination, 'senderId': 'synthetic-self' if own_echo else 'synthetic-other',
                             'direction': 'incoming' if own_echo else 'outgoing', 'type': 1, 'body': body, 'createdAt': created}
                elif '/sends/' + seed_id + ' ' in first:
                    value = {'id': seed_id, 'accountId': 'wechat-primary', 'destination': destination,
                             'state': 'accepted', 'localMessageId': 'seed-message'}
                elif text_projection_delay and '/sends/' + text_send_id + ' ' in first:
                    projection_polls.append(first)
                    value = {'id': text_send_id, 'accountId': 'wechat-primary', 'destination': destination,
                             'state': 'accepted', 'clientId': '123', 'serverId': '456',
                             'localMessageId': 'sent-1' if len(projection_polls) > 1 else None}
                elif '/messages/seed-message ' in first:
                    value = {'id': 'seed-message', 'accountId': 'wechat-primary', 'threadId': destination,
                             'senderId': 'synthetic-self', 'direction': 'outgoing', 'type': 1, 'body': seed_text}
                elif '/messages/sent-1 ' in first:
                    value = {'id': 'sent-1', 'accountId': 'wechat-primary', 'threadId': destination,
                             'direction': 'outgoing', 'senderId': 'synthetic-self', 'type': 1, 'body': answer, 'serverId': '456'}
                elif first.startswith('POST /v1/accounts/wechat-primary/sends '):
                    raw = await reader.readexactly(int(headers[b'content-length']))
                    posts.append((json.loads(raw), headers[b'idempotency-key']))
                    if output_enabled:
                        value = json.loads(image_api.request('POST', image_fixture.PREFIX + '/sends', timeout=3,
                            headers={'Content-Type': 'application/json', 'Idempotency-Key': headers[b'idempotency-key'].decode()},
                            body=raw).body)
                        if voice_output:
                            value = {**value, 'state': 'queued', 'phase': 'queued', 'serverMessageId': None, 'result': {}}
                        output_started.set()
                    else:
                        value = {'id': text_send_id, 'accountId': 'wechat-primary', 'destination': destination,
                                 'state': 'accepted', 'clientId': '123', 'serverId': '456',
                                 'localMessageId': None if text_projection_delay else 'sent-1'}
                else:
                    self.fail('Unexpected Native API request: ' + first)
                raw = json.dumps(value).encode()
                code = (b'201 Created' if '/uploads ' in first else b'202 Accepted') if first.startswith('POST ') else b'200 OK'
                writer.write(b'HTTP/1.1 ' + code + b'\r\nContent-Type: application/json\r\nContent-Length: '
                             + str(len(raw)).encode() + b'\r\nConnection: close\r\n\r\n' + raw)
                await writer.drain()
            except ConnectionError:
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
        listener = await asyncio.start_server(server, '127.0.0.1', 26174)
        root = Path(__file__).resolve().parents[1]
        # The fixture replaces only isolation inspection. Production entrypoint
        # never exposes this test hook and actual sandbox checks run separately.
        code = ("import importlib.util,sys; p=" + repr(str(root / 'scripts/forward_native_worker.py')) + ";"
                "s=importlib.util.spec_from_file_location('fixture_worker',p);m=importlib.util.module_from_spec(s);"
                "s.loader.exec_module(m);m.reader.isolation_checks=lambda:{k:True for k in m.reader.CHECK_NAMES};"
                "sys.exit(m.main())")
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-B', '-c', code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_FRAME)
        async def send(value):
            raw = encode(value)
            self.assertLessEqual(len(raw), MAX_FRAME)
            process.stdin.write(raw)
            await process.stdin.drain()
        async def until(kind):
            nonlocal input_state
            for _ in range(1024):
                raw = await asyncio.wait_for(process.stdout.readline(), 5)
                self.assertTrue(raw, 'Worker ended before ' + kind)
                self.assertLessEqual(len(raw), MAX_FRAME)
                value = decode(raw)
                if value['kind'].startswith('input_image_'):
                    input_frames.append(len(raw))
                    if value['kind'] == 'input_image_begin':
                        self.assertIsNone(input_state)
                        event = input_wire.begin_header(value)
                        input_state = {'event': event, 'id': value['transferId'], 'index': -1,
                                       'bytes': 0, 'hash': hashlib.sha256()}
                    elif value['kind'] == 'input_image_chunk':
                        self.assertEqual(value['transferId'], input_state['id'])
                        input_state['index'] += 1
                        chunk = input_wire.chunk_bytes(value, input_state['index'],
                            input_state['event']['data']['image']['bytes'] - input_state['bytes'])
                        self.assertLessEqual(len(chunk), 64 * 1024)
                        input_chunks.append(len(chunk))
                        input_state['bytes'] += len(chunk)
                        input_state['hash'].update(chunk)
                    else:
                        self.assertEqual(value, {'kind': 'input_image_commit', 'transferId': input_state['id']})
                        meta = input_state['event']['data']['image']
                        self.assertEqual(input_state['bytes'], len(png))
                        self.assertEqual(input_state['hash'].hexdigest(), hashlib.sha256(png).hexdigest())
                        self.assertEqual(meta['sha256'], hashlib.sha256(png).hexdigest())
                        value = input_state['event']
                        input_state = None
                    if input_state is not None:
                        if input_state['index'] in {-1, 0}:
                            with self.assertRaises(asyncio.TimeoutError):
                                await asyncio.wait_for(process.stdout.readline(), .03)
                        await send({'kind': 'input_image_ack', 'transferId': input_state['id'],
                                    'index': input_state['index']})
                        continue
                self.assertNotIn(value['kind'], {'native_error'}, 'Native fixture rejected: '+str(value.get('code','unknown')))
                if value.get('stage') == 'image_input' and value.get('outcome') == 'unavailable':
                    input_errors.append(value['code'])
                if value.get('stage') == 'loop_guard':
                    dropped.append(value['reason'])
                if (loop_noise or image_mode == 'group') and value['kind'] == 'checkpoint':
                    await send({'kind': 'ack', 'cursor': value['cursor']})
                if value['kind'] == kind:
                    return value
            self.fail('Expected frame missing: ' + kind)
        try:
            config = {'account': 'wechat-primary', 'token': 't' * 32, 'runtimeSeconds': 60}
            if group or image_mode or output_enabled:
                config.update(privateName='测试联系人', groupName='测试群', roomId=room, groupPrefix='/ai ', sendInterval=5)
                config['selfProof'] = {'sendId': seed_id, 'destinationSha256': hashlib.sha256(destination.encode()).hexdigest(),
                                       'textSha256': hashlib.sha256(seed_text.encode()).hexdigest(), 'bytes': len(seed_text.encode())}
            if image_mode:
                config['imageInput'] = True
            if output_enabled:
                config['voiceOutput' if voice_output else 'imageOutput'] = True
            if context:
                config['groupContext'] = True
            if all_groups:
                config.update(allGroups=True, roomId=None, groupContext=False)
            if mention:
                config['groupWakeMode'] = 'mention_or_prefix'
            await send(config)
            self.assertTrue((await until('isolated'))['ok'])
            process.stdin.write(b'AWG-READY\n')
            await process.stdin.drain()
            ready = await until('native_ready')
            self.assertEqual(ready['peer'], 'synthetic-peer')
            if group or image_mode or output_enabled:
                self.assertEqual(ready['selfSenderId'], 'synthetic-self')
            if capacity_blocked:
                await send({'kind': 'media_capacity', 'allow': False})
            await send({'kind': 'start', 'after': '1'})
            await until('native_armed')
            if capacity_blocked:
                self.assertEqual((await until('checkpoint'))['cursor'], '2')
                self.assertFalse(any('/media' in request for request in requests))
                await send({'kind': 'ack', 'cursor': '2'})
                source_cursor = '3'
            if inbound_hold:
                await asyncio.wait_for(input_started.wait(), 3)
                await send({'kind': 'send', 'destination': destination, 'requestId': 'a' * 32,
                            'key': 'b' * 32, 'text': answer})
                try:
                    async with asyncio.timeout(3):
                        await until('send_intent')
                        receipt = await until('native_result')
                except asyncio.TimeoutError:
                    self.fail('Text intent/result was blocked by held inbound image acquisition')
                self.assertTrue(receipt['ok'] and receipt['server_confirmed'])
                self.assertFalse(release_input.is_set())
                self.assertEqual(len(posts), 1)
                self.assertFalse(any('/messages/source-2 ' in request for request in requests))
                release_input.set()
            message = await until('event')
            if group:
                self.assertEqual(message['data']['sender_nickname'], '真实昵称🙂')
                self.assertEqual(message['data']['sender_card'], '本群名片')
            if image_mode == 'group' and context:
                self.assertEqual(message['data']['body'], '普通聊天不应吃掉图片🙂')
                self.assertNotIn('image', message['data'])
                self.assertFalse(any('/media' in request for request in requests))
                await send({'kind': 'ack', 'cursor': '3'})
                message = await until('event')
            self.assertEqual(message['data']['body'], body + '2' if capacity_blocked else body)
            if inbound_failure:
                self.assertNotIn('image', message['data'])
                self.assertEqual(input_errors, ['image_unavailable'])
            if image_mode and not capacity_blocked and not inbound_failure:
                self.assertEqual(message['data']['image']['quality'], 'original')
                if inbound_bytes:
                    self.assertEqual(sum(input_chunks), input_images.MAX_IMAGE)
                    self.assertEqual(len(input_chunks), (len(png) + input_wire.CHUNK_BYTES - 1) // input_wire.CHUNK_BYTES)
                    self.assertEqual(len(input_frames), len(input_chunks) + 2)
                else:
                    self.assertEqual(base64.b64decode(message['data']['image']['base64']), png)
            if capacity_blocked:
                self.assertNotIn('image', message['data'])
            if mention:
                self.assertIs(message['data']['mentioned'], True)
            if loop_noise:
                self.assertEqual(dropped, ['outgoing_event', 'own_sender', 'outgoing_message'])
            if inbound_hold or inbound_bytes:
                self.assertFalse(any('/messages/source-2 ' in request for request in requests))
            await send({'kind': 'ack', 'cursor': source_cursor})
            if inbound_hold or inbound_bytes:
                second = await until('event')
                self.assertEqual(second['data']['id'], 'source-2')
                await send({'kind': 'ack', 'cursor': '3'})
            if output_enabled:
                if image_output == 'chunked' or voice_output == 'chunked':
                    await send({'kind': output_kind + '_begin', 'destination': destination, 'requestId': 'a' * 32,
                                'key': 'b' * 32, output_kind: contract.metadata(outgoing)})
                    self.assertEqual((await until(output_kind + '_chunk_ack'))['index'], -1)
                    raw = base64.b64decode(outgoing['base64'])
                    for index, offset in enumerate(range(0, len(raw), contract.CHUNK_BYTES)):
                        await send({'kind': output_kind + '_chunk', 'requestId': 'a' * 32, 'index': index,
                                    'data': base64.b64encode(raw[offset:offset + contract.CHUNK_BYTES]).decode()})
                        self.assertEqual((await until(output_kind + '_chunk_ack'))['index'], index)
                        self.assertFalse(posts)
                    self.assertFalse(any('/uploads ' in request for request in requests))
                    await send({'kind': output_kind + '_commit', 'requestId': 'a' * 32})
                else:
                    await send({'kind': 'send_' + output_kind, 'destination': destination, 'requestId': 'a' * 32,
                                'key': 'b' * 32, output_kind: outgoing})
                await until(output_kind + '_send_intent')
                second = await until('event')
                self.assertEqual(second['data']['body'], body + '2')
                await send({'kind': 'ack', 'cursor': '3'})
                release_image.set()
                receipt = await until('native_' + output_kind + '_result')
            elif receipt is None:
                await send({'kind': 'send', 'destination': destination, 'requestId': 'a' * 32,
                            'key': 'b' * 32, 'text': answer})
                await until('send_intent')
                receipt = await until('native_result')
            self.assertTrue(receipt['ok'] and receipt['server_confirmed'])
            if output_enabled:
                self.assertTrue(receipt['receipt']['projection_verified'])
                self.assertEqual(receipt['receipt']['source_sha256'], outgoing['sha256'])
                self.assertEqual(posts, [({'kind': output_kind, 'threadId': destination, 'sourceId': voice_fixture.SOURCE if voice_output else image_fixture.SOURCE}, b'b' * 32)])
                upload = image_api.calls[0][3 if voice_output else 4]
                self.assertIn(base64.b64decode(outgoing['base64']), upload)
            elif group or image_mode:
                self.assertTrue(receipt['projection_verified'])
            if not output_enabled:
                self.assertEqual(posts, [({'threadId': destination, 'text': answer}, b'b' * 32)])
            if text_projection_delay:
                self.assertEqual(len(projection_polls), 2)
            self.assertTrue(any('/events/stream?' in request for request in requests))
            self.assertFalse(any('/events?' in request for request in requests))
            if all_groups:
                self.assertFalse(any('/groups?' in request for request in requests))
            await send({'kind': 'stop'})
            self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
        finally:
            release_input.set()
            release_image.set()
            if process.returncode is None:
                process.kill()
                await process.wait()
            listener.close()
            await listener.wait_closed()
