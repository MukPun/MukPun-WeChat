import asyncio
from dataclasses import asdict
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from wechat_bridge.continuous import PrivateBridge, split_text
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.forward_wire import decode, encode, Lines, WireError
from wechat_bridge.protocol import Config, NativeMessage, Receipt
from wechat_bridge.state import StateError

spec = importlib.util.spec_from_file_location('forward_sse_test', Path(__file__).resolve().parents[1] / 'scripts/forward_sse.py')
sse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sse)


class TextTests(unittest.TestCase):
    def test_lossless_utf8_parts(self):
        body = ' 前缀\r\n' + '中文🙂e\u0301\t' * 1800 + '\n后缀 '
        parts = split_text(body)
        self.assertEqual(''.join(parts), body)
        self.assertTrue(all(len(part.encode()) <= 8192 for part in parts))
        self.assertGreater(len(parts), 1)

    def test_reject_oversize_without_truncating(self):
        with self.assertRaises(ValueError):
            split_text('中' * 11000)

    def test_ipc_fragmentation_unicode(self):
        value = {'text': '🙂汉字\r\n' * 2000}
        raw = encode(value)
        parser = Lines()
        for index in range(0, len(raw), 137):
            parser.feed(raw[index:index + 137])
        self.assertEqual(parser.pop(), value)
        self.assertIsNone(parser.pop())

    def test_sse_comments_crlf_and_final_incomplete(self):
        raw = b': heartbeat\r\n\r\nid: 12\r\ndata: {"cursor":"12",\r\ndata: "kind":"message.committed"}\r\n\r\nid: 13\ndata: {"cursor":"13"}'
        result = list(sse.frames(io.BytesIO(raw)))
        # Incomplete frame is never acknowledged or delivered.
        self.assertEqual(result, [('12', 'message', {'cursor': '12', 'kind': 'message.committed'})])

    def test_real_created_at(self):
        self.assertEqual(sse.timestamp('2026-10-26T00:00:00Z'), 1792972800)
        with self.assertRaises(sse.StreamError):
            sse.timestamp('2026-10-26T00:00:00')

    def test_duplicate_ipc_keys(self):
        with self.assertRaises(WireError):
            decode(b'{"kind":"event","kind":"send"}')


class PrivateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'spool.sqlite'
        self.state = Spool(self.path)
        self.emitted = []
        self.backend = Gateway('account', 'peer', self.state, 'generation', self.emitted.append)
        self.core = PrivateBridge(Config('account', 'self', None, 'tester', send_interval=0), 'peer', self.state, self.backend)
        self.core.connected()
        self.sent = []
        async def send(destination, body, key):
            self.sent.append((destination, body, key))
            return Receipt('account', destination, str(uuid.uuid4()), 'accepted', '123', '456')
        self.backend.send_text = send

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    def message(self, identity='one', body=' 测试\r\n🙂\t '):
        return NativeMessage('account', identity, 'peer', 'peer', body, int(time.time()))

    def feed(self, message, cursor='1'):
        self.backend.feed({'kind': 'event', 'data': asdict(message), 'cursor': cursor})

    async def ingest(self, message):
        self.feed(message)
        message = await self.backend.next_message()
        event = self.core.ingest(message)
        await self.core.event_delivered(message, True)
        return event

    async def test_whitespace_and_unicode_exact(self):
        message = self.message()
        event = await self.ingest(message)
        self.assertEqual(event['message'][0]['data']['text'], message.body)
        self.assertEqual(event['raw_message'], message.body)
        self.assertEqual(event['message_type'], 'private')
        self.assertEqual(self.state.counts(), {'forwarded': 1})

    async def test_burst_fifo_not_busy_drop(self):
        for index in range(24):
            self.feed(self.message(str(index), f'消息{index}'), str(index + 1))
        for index in range(24):
            message = await self.backend.next_message()
            self.assertEqual(message.body, f'消息{index}')
            self.core.ingest(message)
            await self.core.event_delivered(message, True)
        self.assertEqual(self.state.counts(), {'forwarded': 24})

    async def test_queue_full_keeps_existing_and_does_not_ack(self):
        for index in range(32):
            self.feed(self.message(str(index)), str(index + 1))
        before = len(self.emitted)
        with self.assertRaises(StateError):
            self.feed(self.message('overflow'), '33')
        self.assertEqual(len(self.emitted), before)
        self.assertEqual(self.state.counts(), {'queued': 32})

    async def test_durable_dedup_after_reopen(self):
        message = self.message()
        await self.ingest(message)
        self.state.close()
        self.state = Spool(self.path)
        self.assertFalse(self.state.admit(message.id, asdict(message)))
        self.assertIsNone(self.state.next_pending())
        self.assertIsNone(self.state.db.execute('SELECT payload FROM inbox').fetchone()[0])

    async def test_write_interruption_retained_uncertain(self):
        self.feed(self.message())
        message = await self.backend.next_message()
        self.core.ingest(message)
        self.state.close()
        self.state = Spool(self.path)
        self.assertEqual(self.state.counts(), {'uncertain': 1})
        self.assertIsNone(self.state.next_pending())
        self.assertIsNotNone(self.state.db.execute('SELECT payload FROM inbox').fetchone()[0])

    async def test_long_multi_action_replies_complete(self):
        event = await self.ingest(self.message())
        body = ' 答复\r\n' + '中文🙂' * 2300 + '\n结尾 '
        params = {'user_id': event['user_id'], 'message': [{'type': 'text', 'data': {'text': body}}]}
        first = await self.core.action({'action': 'send_private_msg', 'params': params, 'echo': 'first'})
        self.assertEqual(first['retcode'], 0)
        self.assertEqual(''.join(item[1] for item in self.sent), body)
        second = await self.core.action({'action': 'send_msg', 'params': {'user_id': event['user_id'],
            'message_type': 'private', 'message': '第二条回复'}, 'echo': 'second'})
        self.assertEqual(second['retcode'], 0)
        self.assertEqual(self.sent[-1][1], '第二条回复')
        self.assertEqual(len({item[2] for item in self.sent}), len(self.sent))

    async def test_same_echo_no_duplicate_send(self):
        event = await self.ingest(self.message())
        request = {'action': 'send_private_msg', 'params': {'user_id': event['user_id'], 'message': '答复'}, 'echo': 'same'}
        first = await self.core.action(request)
        self.core.disconnected()
        self.core.connected()
        second = await self.core.action(request)
        self.assertEqual(first, second)
        self.assertEqual(len(self.sent), 1)

    async def test_astrbot_echo_counter_reset_does_not_lose_a_new_reply(self):
        event = await self.ingest(self.message('first'))
        request = {'action': 'send_private_msg', 'params': {'user_id': event['user_id'], 'message': '相同答复'}, 'echo': 17}
        self.assertEqual((await self.core.action(request))['retcode'], 0)
        await self.ingest(self.message('second'))
        self.assertEqual((await self.core.action(request))['retcode'], 0)
        self.assertEqual(len(self.sent), 2)

    async def test_reset_echo_with_different_new_reply_is_not_a_conflict(self):
        event = await self.ingest(self.message('first'))
        request = {'action': 'send_private_msg', 'params': {'user_id': event['user_id'], 'message': '第一次答复'}, 'echo': 17}
        self.assertEqual((await self.core.action(request))['retcode'], 0)
        await self.ingest(self.message('second'))
        request['params']['message'] = '新的答复'
        self.assertEqual((await self.core.action(request))['retcode'], 0)
        self.assertEqual([item[1] for item in self.sent], ['第一次答复', '新的答复'])

    async def test_partial_unknown_not_success_or_retried(self):
        event = await self.ingest(self.message())
        count = 0
        async def send(destination, body, key):
            nonlocal count
            count += 1
            return Receipt('account', destination, str(uuid.uuid4()), 'accepted' if count == 1 else 'unknown', '12', '45')
        self.backend.send_text = send
        request = {'action': 'send_private_msg', 'params': {'user_id': event['user_id'], 'message': 'a' * 19000}, 'echo': 'partial'}
        self.assertEqual((await self.core.action(request))['retcode'], 1408)
        self.assertEqual((await self.core.action(request))['retcode'], 1408)
        self.assertEqual(count, 2)

    async def test_unbound_destination_and_unsupported_segment_no_partial(self):
        event = await self.ingest(self.message())
        for params in ({'user_id': event['user_id'] + 1, 'message': 'bad'},
                       {'user_id': event['user_id'], 'message': [{'type': 'text', 'data': {'text': '前文'}},
                            {'type': 'image', 'data': {'file': 'bad'}}]}):
            self.assertNotEqual((await self.core.action({'action': 'send_private_msg', 'params': params}))['retcode'], 0)
        self.assertFalse(self.sent)

    async def test_outgoing_echo_rejected(self):
        message = asdict(self.message())
        message['direction'] = 'outgoing'
        with self.assertRaises(WireError):
            self.backend.feed({'kind': 'event', 'cursor': '1', 'data': message})

    async def test_receipt_wrong_destination_no_success(self):
        event = await self.ingest(self.message())
        async def send(*args):
            return Receipt('account', 'someone_else', str(uuid.uuid4()), 'accepted', '12', '45')
        self.backend.send_text = send
        answer = await self.core.action({'action': 'send_private_msg', 'params': {'user_id': event['user_id'], 'message': '正文'}})
        self.assertNotEqual(answer['retcode'], 0)
