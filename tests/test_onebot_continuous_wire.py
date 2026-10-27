"""Actual SSE HTTP and aiocqhttp WebSocket round trips using synthetic peers."""
import asyncio
import base64
from dataclasses import asdict
import importlib.util
import json
import logging
from pathlib import Path
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from wechat_bridge.continuous import PrivateBridge, ImageMessage, TextBridge, RoutedMessage
from wechat_bridge.image_input import payload
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.protocol import Config, NativeMessage, Receipt
from wechat_bridge.transport import Endpoint, Runner

spec = importlib.util.spec_from_file_location('forward_sse_wire_test', Path(__file__).resolve().parents[1] / 'scripts/forward_sse.py')
sse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sse)
spec = importlib.util.spec_from_file_location('wire_host_worker', Path(__file__).resolve().parents[1] / 'scripts/forward_host_worker.py')
host_worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_worker)


class RoundTrips(unittest.IsolatedAsyncioTestCase):
    async def test_sse_reconnect_uses_durable_cursor(self):
        requests = []
        async def server(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                requests.append(request)
                current = '11' if b'after=10 ' in request else '12'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n')
                raw = ('id: ' + current + '\ndata: ' + json.dumps({'cursor': current, 'kind': 'message.committed'}) + '\n\n').encode()
                for index in range(0, len(raw), 7):
                    writer.write(raw[index:index + 7])
                    await writer.drain()
                    await asyncio.sleep(.001)
            finally:
                writer.close()
                await writer.wait_closed()
        listener = await asyncio.start_server(server, '127.0.0.1', 0)
        port = listener.sockets[0].getsockname()[1]
        pump = sse.Pump('wechat-primary', 't' * 32, '10', time.monotonic() + 6, port=port)
        try:
            pump.start()
            async def read_item():
                for _ in range(300):
                    if not pump.items.empty():
                        return pump.items.get_nowait()
                    await asyncio.sleep(.01)
                self.fail('SSE delivery timed out')
            first = await read_item()
            self.assertEqual(first['cursor'], '11')
            await asyncio.sleep(.1)
            self.assertEqual(len(requests), 1)  # Waits for durable ACK.
            pump.ack('11')
            second = await read_item()
            self.assertEqual(second['cursor'], '12')
            self.assertIn(b'/events/stream?after=11 ', requests[1])
            self.assertTrue(all(b'/events?' not in request for request in requests))
            pump.ack('12')
        finally:
            pump.close()
            listener.close()
            await listener.wait_closed()

    async def test_actual_aiocqhttp_private_burst_and_two_replies_each(self):
        await self.exercise()

    async def test_actual_aiocqhttp_image_payload_and_text_reply(self):
        await self.exercise(image=True)

    async def test_actual_aiocqhttp_group_context_then_wakeup_reply(self):
        await self.exercise(group=True)

    async def test_actual_aiocqhttp_private_image_failure_then_text(self):
        await self.exercise(unsupported=True)

    async def test_actual_aiocqhttp_group_image_failure_then_text(self):
        await self.exercise(group=True, unsupported=True)

    async def exercise(self, image=False, group=False, unsupported=False):
        from aiocqhttp import CQHttp
        from aiocqhttp.exceptions import ActionFailed
        from hypercorn.config import Config as HyperConfig
        from hypercorn.asyncio.run import worker_serve
        from hypercorn.utils import wrap_app
        token = secrets.token_urlsafe(32)
        bot = CQHttp('private_wire_test', access_token=token, api_timeout_sec=4)
        bot.logger.setLevel(logging.CRITICAL)
        config = HyperConfig()
        config.bind, config.accesslog, config.errorlog, config.graceful_timeout = ['127.0.0.1:0'], None, None, 1
        sockets = config.create_sockets()
        port = sockets.insecure_sockets[0].getsockname()[1]
        shutdown = asyncio.Event()
        serving = asyncio.create_task(worker_serve(wrap_app(bot.server_app, config.wsgi_max_body_size, 'asgi'),
            config, sockets=sockets, shutdown_trigger=shutdown.wait))
        events, sent, actions, reports = [], [], [], []
        complete = asyncio.Queue()
        @bot.on_message('group' if group else 'private')
        async def private(event):
            try:
                events.append(dict(event))
                if group and not any(part['type'] == 'at' for part in event.message):
                    await complete.put(True)
                    return
                if image:
                    data = event.message[0]['data']['file']
                    if not data.startswith('base64://') or base64.b64decode(data[9:]) != picture:
                        raise ValueError('image_bytes_changed')
                if unsupported:
                    try:
                        await bot.call_action('send_group_msg' if group else 'send_private_msg', self_id=event.self_id,
                            **({'group_id': event.group_id} if group else {'user_id': event.user_id}),
                            message=[{'type': 'image', 'data': {'file': 'file:///AstrBot/data/meme.gif'}}])
                    except ActionFailed as error:
                        if error.retcode != 1400 or error.result.get('message') != 'unsupported_message_segment':
                            raise
                    else:
                        raise ValueError('image_was_falsely_accepted')
                for index in range(2):
                    await bot.call_action('send_group_msg' if group else 'send_private_msg', self_id=event.self_id,
                        **({'group_id': event.group_id} if group else {'user_id': event.user_id}),
                        message=[{'type': 'reply', 'data': {'id': str(event.message_id)}},
                                 {'type': 'text', 'data': {'text': f'回复{event.message_id}:{index}\r\n🙂'}}])
                await complete.put(True)
            except Exception:
                await complete.put(False)
        task = runner = state = None
        directory = tempfile.TemporaryDirectory()
        try:
            state = Spool(Path(directory.name) / 'spool.sqlite')
            gateway = Gateway('account', 'peer', state, 'generation', lambda _: None, image_input=image,
                              group='room@chatroom' if group else None, group_context=group)
            async def send(destination, body, key):
                sent.append((destination, body, key))
                return Receipt('account', destination, str(uuid.uuid4()), 'accepted', '12', '45')
            gateway.send_text = send
            core = PrivateBridge(Config('account', 'self', None, 'tester', send_interval=0), 'peer', state, gateway, image_input=image)
            if group:
                core = TextBridge(Config('account', 'self', 'room@chatroom', 'tester', send_interval=0),
                                  'peer', state, gateway, group_context=True)
            def action_result(answer):
                actions.append(answer)
                with patch.object(host_worker, 'emit', reports.append):
                    host_worker.action_result(answer)
            runner = Runner(core, Endpoint('ws://127.0.0.1:' + str(port) + '/ws', token), on_reply=action_result)
            task = asyncio.create_task(runner.run(maximum_seconds=12))
            await asyncio.wait_for(runner.ready.wait(), 4)
            count = 3 if group else 1 if image else 4
            picture = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5P8AAAAASUVORK5CYII=')
            for index in range(count):
                message = NativeMessage('account', str(index), 'peer', 'peer', f' 输入{index}\r\n🙂 ', int(time.time()))
                if group:
                    message = RoutedMessage('account', str(index), 'room@chatroom', 'member-' + str(index),
                        '/ai 唤醒🙂' if index == 2 else f' 输入{index}\r\n🙂 ', int(time.time()))
                if image:
                    message = ImageMessage('account', str(index), 'peer', 'peer', '', int(time.time()), type=3,
                                           image=payload(picture, 'image/png', 'thumbnail'))
                gateway.feed({'kind': 'event', 'cursor': str(index + 1), 'data': asdict(message)})
            for _ in range(count):
                self.assertTrue(await asyncio.wait_for(complete.get(), 5))
            expected = [f' 输入{i}\r\n🙂 ' for i in range(count)]
            if group:
                expected[-1] = '/ai 唤醒🙂'
                self.assertEqual([[p['type'] for p in event['message']] for event in events], [['text'], ['text'], ['at', 'text']])
                self.assertEqual(len({event['user_id'] for event in events}), count)
            replies = 2 if group else count * 2
            self.assertEqual([event['raw_message'] for event in events], [''] if image else expected)
            self.assertEqual(len(sent), replies)
            rejected = 1 if group else count
            self.assertEqual(len(actions), replies + (rejected if unsupported else 0))
            self.assertEqual(sum(answer['retcode'] == 0 for answer in actions), replies)
            self.assertEqual(reports, [{'kind': 'report', 'stage': 'action_unsupported', 'retcode': 1400}] * rejected
                             if unsupported else [])
            # A connect attempt before Hypercorn has started listening may
            # legitimately fail, especially under the simulation's CPU cap.
            # One successful connection proves the rejection did not reconnect.
            self.assertEqual(runner.connections, 1)
            self.assertTrue(runner.ready.is_set())
            self.assertEqual(len({item[2] for item in sent}), replies)
            self.assertEqual(state.counts(), {'forwarded': count})
        finally:
            if runner:
                runner.stop.set()
            if task:
                await asyncio.wait_for(task, 4)
            shutdown.set()
            await asyncio.wait_for(serving, 4)
            if state:
                state.close()
            directory.cleanup()
