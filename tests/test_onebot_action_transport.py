"""Real OneBot API futures survive large actions and determinate parse errors."""
import asyncio
import base64
from contextlib import asynccontextmanager, ExitStack
from dataclasses import asdict
import json
import logging
from pathlib import Path
import secrets
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from aiocqhttp import CQHttp
from aiocqhttp.api_impl import ResultStore
from aiocqhttp.exceptions import ActionFailed
from hypercorn.asyncio.run import worker_serve
from hypercorn.config import Config as HyperConfig
from hypercorn.utils import wrap_app

from wechat_bridge.continuous import PrivateBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge import forward_wire as wire, protocol, transport
from wechat_bridge.protocol import Config, NativeMessage, Receipt
from wechat_bridge import image_output as images
from test_onebot_image_output import png, chunk, receipt


@asynccontextmanager
async def runtime():
    token = secrets.token_urlsafe(32)
    bot = CQHttp('action_transport_test', access_token=token, api_timeout_sec=10)
    bot.logger.setLevel(logging.CRITICAL)
    server_config = HyperConfig()
    server_config.bind = ['127.0.0.1:0']
    server_config.accesslog = server_config.errorlog = None
    server_config.graceful_timeout = 1
    sockets = server_config.create_sockets()
    port = sockets.insecure_sockets[0].getsockname()[1]
    shutdown = asyncio.Event()
    serving = asyncio.create_task(worker_serve(
        wrap_app(bot.server_app, server_config.wsgi_max_body_size, 'asgi'), server_config,
        sockets=sockets, shutdown_trigger=shutdown.wait))
    running = runner = state = None
    sent, answers = [], []
    with tempfile.TemporaryDirectory() as directory, ExitStack() as legacy:
        try:
            state = Spool(Path(directory) / 'spool.sqlite')
            gateway = Gateway('account', 'peer', state, 'generation', lambda _: None)
            async def send(destination, body, key):
                sent.append((destination, body, key))
                return Receipt('account', destination, str(uuid.uuid4()), 'accepted', '12', '45')
            gateway.send_text = send
            core = PrivateBridge(Config('account', 'self', None, 'tester', send_interval=0),
                                 'peer', state, gateway)
            core.connected()
            message = NativeMessage('account', 'source', 'peer', 'peer', 'test', int(time.time()))
            gateway.feed({'kind': 'event', 'cursor': '1', 'data': asdict(message)})
            message = await asyncio.wait_for(gateway.next_message(), 3)
            core.ingest(message)
            await core.event_delivered(message, True)
            if not hasattr(transport, 'MAX_ACTION_BYTES'):
                # Reproduce the currently deployed worker's two monkeypatches.
                # A transport with its own action budget must no longer use them.
                legacy.enter_context(patch.object(transport, 'MAX_JSON', wire.MAX_FRAME))
                legacy.enter_context(patch.object(transport, 'decode', lambda raw: wire.decode(raw.encode())))
            runner = transport.Runner(core, transport.Endpoint('ws://127.0.0.1:' + str(port) + '/ws', token),
                                      on_reply=answers.append)
            running = asyncio.create_task(runner.run(maximum_seconds=20))
            await asyncio.wait_for(runner.ready.wait(), 5)
            yield SimpleNamespace(bot=bot, core=core, runner=runner, sent=sent, answers=answers,
                                  socket=bot._wsr_api_clients[str(core.self_id)])
        finally:
            if runner:
                runner.stop.set()
            if running:
                await asyncio.wait_for(running, 4)
            shutdown.set()
            await asyncio.wait_for(serving, 4)
            if state:
                state.close()


class ActionTransport(unittest.IsolatedAsyncioTestCase):
    async def test_one_megabyte_image_through_real_cqhttp_and_chunked_gateway(self):
        async with runtime() as live:
            gateway = live.core.backend
            gateway.image_output = live.core.image_output = True
            value = images.payload(png(extra=chunk(b'tEXt', b'x' * (images.MAX_IMAGE - len(png()) - 12))))
            assembly, frames = None, []
            def emit(frame):
                nonlocal assembly
                if frame['kind'] not in {'image_begin', 'image_chunk', 'image_commit'}:
                    return
                frames.append(frame['kind'])
                self.assertLess(len(wire.encode(frame)), wire.MAX_FRAME)
                if frame['kind'] == 'image_begin':
                    assembly = images.Assembly(frame['image'])
                    index = -1
                elif frame['kind'] == 'image_chunk':
                    assembly.add(frame['index'], frame['data'])
                    index = frame['index']
                else:
                    self.assertEqual(assembly.finish(), value)
                    gateway.feed({'kind': 'image_receipt', 'requestId': frame['requestId'], 'receipt': receipt(value)})
                    return
                gateway.feed({'kind': 'image_chunk_ack', 'requestId': frame['requestId'], 'index': index})
            gateway.emit = emit
            answer = await live.bot.call_action('send_private_msg', self_id=live.core.self_id,
                user_id=live.core.peer_id, message=[{'type': 'image', 'data': {'file': 'base64://' + value['base64']}}])
            self.assertIs(type(answer['message_id']), int)
            self.assertEqual(frames.count('image_chunk'), images.MAX_IMAGE // images.CHUNK_BYTES)
            self.assertEqual(frames[-1], 'image_commit')
            self.assertEqual(live.runner.connections, 1)

    async def assert_text_continues(self, live):
        result = await live.bot.call_action('send_private_msg', self_id=live.core.self_id,
            user_id=live.core.peer_id, message='完整文本🙂')
        self.assertIs(type(result['message_id']), int)
        self.assertEqual([item[:2] for item in live.sent], [('peer', '完整文本🙂')])
        self.assertEqual(live.runner.connections, 1)
        self.assertTrue(live.runner.ready.is_set())

    async def test_real_base64_image_over_ipc_limit_gets_api_result_not_timeout(self):
        async with runtime() as live:
            file_value = 'base64://' + base64.b64encode(b'x' * (192 * 1024)).decode()
            params = {'self_id': live.core.self_id, 'user_id': live.core.peer_id,
                      'message': [{'type': 'image', 'data': {'file': file_value}}]}
            self.assertGreater(len(json.dumps({'action': 'send_private_msg', 'params': params,
                                              'echo': {'seq': 86}}).encode()), wire.MAX_FRAME)
            with self.assertRaises(ActionFailed) as failed:
                await live.bot.call_action('send_private_msg', **params)
            self.assertEqual(failed.exception.retcode, 1400)
            self.assertEqual(failed.exception.result['message'], 'unsupported_message_segment')
            self.assertIs(type(failed.exception.result['echo']['seq']), int)
            self.assertFalse(live.sent)
            await self.assert_text_continues(live)

    async def test_large_invalid_text_echo_is_returned_and_connection_survives(self):
        async with runtime() as live:
            with self.assertRaises(ActionFailed) as failed:
                await live.bot.call_action('send_private_msg', self_id=live.core.self_id,
                    user_id=live.core.peer_id, message='x' * (wire.MAX_FRAME + 1))
            self.assertEqual(failed.exception.retcode, 1400)
            self.assertEqual(failed.exception.result['message'], 'invalid_request')
            self.assertIs(type(failed.exception.result['echo']['seq']), int)
            self.assertFalse(live.sent)
            await self.assert_text_continues(live)

    async def test_duplicate_parameter_keeps_unique_top_level_echo(self):
        async with runtime() as live:
            seq = 900001
            future = asyncio.create_task(ResultStore.fetch(seq, 3))
            await asyncio.sleep(0)
            await live.socket.send('{"action":"send_private_msg","params":{"user_id":1,"user_id":2,'
                                   '"message":"must not send"},"echo":{"seq":900001}}')
            answer = await future
            self.assertEqual(answer['retcode'], 1400)
            self.assertEqual(answer['message'], 'invalid_json')
            self.assertEqual(answer['echo'], {'seq': seq})
            self.assertFalse(live.sent)
            self.assertFalse(live.answers)
            self.assertEqual(live.runner.decode_rejections, 1)
            await self.assert_text_continues(live)

    async def test_unparseable_message_does_not_poison_next_action(self):
        async with runtime() as live:
            await live.socket.send('{not-json')
            await self.assert_text_continues(live)
            self.assertEqual([answer['retcode'] for answer in live.answers], [0])
            self.assertEqual(live.runner.decode_rejections, 1)

    async def test_ambiguous_echo_is_not_guessed_or_executed(self):
        async with runtime() as live:
            await live.socket.send('{"action":"send_private_msg","params":{"user_id":1,"message":"bad"},'
                                   '"echo":{"seq":900002},"echo":{"seq":900003}}')
            await self.assert_text_continues(live)
            self.assertEqual([answer['retcode'] for answer in live.answers], [0])
            self.assertEqual(live.runner.decode_rejections, 1)


class DecoderBudget(unittest.TestCase):
    def test_ipc_budget_is_not_relaxed_by_action_budget(self):
        self.assertEqual(wire.MAX_FRAME, 262144)
        self.assertEqual(transport.MAX_ACTION_BYTES, 4 * 1024 * 1024)
        with self.assertRaises(wire.WireError):
            wire.decode(b'{"text":"' + b'x' * wire.MAX_FRAME + b'"}')

    def test_legacy_worker_decoder_override_cannot_reenter_action_path(self):
        with patch.object(transport, 'decode', side_effect=wire.WireError('must_not_use_ipc_decoder')):
            self.assertEqual(transport.decode_action('{"action":"get_status","echo":{"seq":6}}')['echo'], {'seq': 6})

    def test_strict_decoder_rejects_duplicate_keys_and_excessive_structure(self):
        for raw in ('{"a":1,"a":2}', '{"value":' + '[' * 64 + '0' + ']' * 64 + '}',
                    '{"value":[' + ','.join('{}' for _ in range(5000)) + ']}'):
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                protocol.decode(raw)

    def test_action_budget_accepts_long_opaque_string_and_rejects_hard_limit(self):
        prefix, suffix = '{"value":"', '","echo":{"seq":4}}'
        raw = prefix + 'x' * (transport.MAX_ACTION_BYTES - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(raw.encode()), transport.MAX_ACTION_BYTES)
        self.assertEqual(transport.decode_action(raw)['echo'], {'seq': 4})
        with self.assertRaises(protocol.DecodeError) as failure:
            transport.decode_action(raw + ' ')
        self.assertFalse(failure.exception.has_echo)

    def test_duplicate_echo_or_nested_echo_key_is_never_selected(self):
        for raw in ('{"params":{"a":1,"a":2},"echo":{"seq":1,"seq":2}}',
                    '{"echo":{"seq":1},"echo":{"seq":2}}'):
            with self.subTest(raw=raw), self.assertRaises(protocol.DecodeError) as failure:
                transport.decode_action(raw)
            self.assertFalse(failure.exception.has_echo)

    def test_delimiters_inside_escaped_text_do_not_spend_structure_budget(self):
        value = '[{"item": "\\\\"},]' * 5000
        raw = json.dumps({'params': {'value': value}, 'echo': {'seq': 5}})
        self.assertEqual(transport.decode_action(raw)['params']['value'], value)
