import hashlib
import asyncio
import importlib.util
import json
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from wechat_bridge.continuous import TextBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.forward_wire import WireError
from wechat_bridge.forward_config import DEFAULT, read, validate
from wechat_bridge.protocol import Config, NativeMessage, Receipt

spec = importlib.util.spec_from_file_location('routing_sse_test', Path(__file__).resolve().parents[1] / 'scripts/forward_sse.py')
sse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sse)


class ConfigAndRecovery(unittest.TestCase):
    def test_configuration_cannot_set_endpoints_or_disable_limits(self):
        self.assertEqual(read(json.dumps(DEFAULT).encode()), DEFAULT)
        for changes in ({'endpoint': 'http://example.test'}, {'runtimeSeconds': True}, {'runtimeSeconds': 604801},
                        {'segmentBytes': 8193}, {'sendIntervalSeconds': 0}, {'groupPrefix': ''}, {'groupName': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**DEFAULT, **changes})

    def test_recovery_anchor_must_match_event_content(self):
        event = {'cursor': '8', 'kind': 'message.committed', 'data': {'messageId': 'synthetic'}}
        class Reader:
            class Policy:
                def events(self, after):
                    self.after = after
                    return '/events'
            policy = Policy()
            def _get(self, *args, **kwargs):
                return {'items': [event]}
        reader = Reader()
        sse.verify_checkpoint(reader, '8', sse.event_digest(event), 100)
        self.assertEqual(reader.policy.after, '7')
        # Same decimal cursor after SDK rollback, different event.
        with self.assertRaises(sse.StreamError):
            sse.verify_checkpoint(reader, '8', 'a' * 64, 100)

    def test_cursor_must_be_canonical(self):
        for value in ('01', '-1', str(2**63), '1\n', '１２'):
            with self.assertRaises(sse.StreamError):
                sse.cursor(value)


class Routes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Spool(Path(self.directory.name) / 'spool.sqlite')
        self.frames, self.sent = [], []
        self.backend = Gateway('account', 'peer', self.state, 'profile', self.frames.append, group='room@chatroom')
        self.core = TextBridge(Config('account', 'self', 'room@chatroom', '测试群', send_interval=0),
                               'peer', self.state, self.backend, private_name='测试好友')
        self.core.connected()
        async def send(destination, body, key):
            self.sent.append((destination, body, key))
            return Receipt('account', destination, str(uuid.uuid4()), 'accepted', '123', '456')
        self.backend.send_text = send

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def event(self, identity, group, body):
        message = NativeMessage('account', identity, 'room@chatroom' if group else 'peer',
                                'member' if group else 'peer', body, int(time.time()))
        self.backend.feed({'kind': 'event', 'cursor': str(len(self.frames) + 1), 'anchor': 'a' * 64, 'data': asdict(message)})
        message = await self.backend.next_message()
        event = self.core.ingest(message)
        await self.core.event_delivered(message, True)
        return event

    async def test_group_private_interleaving_and_complete_multiple_replies(self):
        group = await self.event('group', True, '/ai  第一行\r\n🙂\t ')
        private = await self.event('private', False, ' 私聊\r\n中文 ')
        self.assertEqual(group['message'][1]['data']['text'], ' 第一行\r\n🙂\t ')
        self.assertEqual(group['raw_message'], '/ai  第一行\r\n🙂\t ')
        self.assertEqual(private['sender']['nickname'], '测试好友')
        self.assertEqual(private['message_type'], 'private')
        for action, destination, identity in (('send_group_msg', 'group_id', group['group_id']),
                                             ('send_private_msg', 'user_id', private['user_id']),
                                             ('send_group_msg', 'group_id', group['group_id'])):
            result = await self.core.action({'action': action, 'params': {destination: identity, 'message': '完整答复🙂'}, 'echo': uuid.uuid4().hex})
            self.assertEqual(result['retcode'], 0)
        self.assertEqual([item[0] for item in self.sent], ['room@chatroom', 'peer', 'room@chatroom'])

    async def test_quote_cannot_cross_private_and_group(self):
        group = await self.event('group', True, '/ai 问题')
        private = await self.event('private', False, '问题')
        result = await self.core.action({'action': 'send_private_msg', 'params': {'user_id': private['user_id'],
            'message': [{'type': 'reply', 'data': {'id': group['message_id']}}, {'type': 'text', 'data': {'text': '答复'}}]}})
        self.assertNotEqual(result['retcode'], 0)
        self.assertFalse(self.sent)

    async def test_private_input_does_not_authorize_group_send(self):
        await self.event('private', False, '问题')
        result = await self.core.action({'action': 'send_group_msg', 'params': {'group_id': self.core.group_id, 'message': '答复'}})
        self.assertNotEqual(result['retcode'], 0)
        self.assertFalse(self.sent)

    async def test_group_send_msg_inference_and_long_text_complete(self):
        event = await self.event('group', True, '/ai 问题')
        body = '答复\r\n' + '中文🙂\t' * 1600 + '\n结束'
        result = await self.core.action({'action': 'send_msg', 'params': {'group_id': event['group_id'], 'message': body}, 'echo': 'long'})
        self.assertEqual(result['retcode'], 0)
        self.assertEqual(''.join(item[1] for item in self.sent), body)
        self.assertTrue(all(item[0] == 'room@chatroom' and len(item[1].encode()) <= 8192 for item in self.sent))

    async def test_ordinary_group_and_other_destination_rejected_before_ack(self):
        for room, body in (('room@chatroom', '普通聊天'), ('other@chatroom', '/ai 问题')):
            message = NativeMessage('account', 'source', room, 'member', body, int(time.time()))
            with self.assertRaises(WireError):
                self.backend.feed({'kind': 'event', 'cursor': '2', 'anchor': 'a' * 64, 'data': asdict(message)})
        self.assertFalse(self.frames)
        self.assertEqual(self.state.counts(), {})

    async def test_checkpoint_without_text_does_not_invoke_model(self):
        self.backend.feed({'kind': 'checkpoint', 'cursor': '9', 'anchor': 'b' * 64})
        self.assertEqual(self.frames, [{'kind': 'ack', 'cursor': '9'}])
        self.assertIsNone(self.state.next_pending())
        self.assertEqual(json.loads(self.state.cursor('profile')), {'cursor': '9', 'anchor': 'b' * 64})

    async def test_recovered_queue_waits_for_native_cursor_verification(self):
        gateway = Gateway('account', 'peer', self.state, 'profile', self.frames.append,
                          group='room@chatroom', require_activation=True)
        message = NativeMessage('account', 'waiting', 'peer', 'peer', '排队消息', int(time.time()))
        gateway.feed({'kind': 'event', 'cursor': '2', 'anchor': 'a' * 64, 'data': asdict(message)})
        task = asyncio.create_task(gateway.next_message())
        await asyncio.sleep(.02)
        self.assertFalse(task.done())
        self.assertEqual(self.state.counts(), {'queued': 1})
        gateway.feed({'kind': 'activate'})
        self.assertEqual((await asyncio.wait_for(task, 1)).body, '排队消息')

    async def test_declared_group_and_private_types_cannot_be_confused(self):
        await self.event('group', True, '/ai 问题')
        await self.event('private', False, '问题')
        for params in ({'message_type': 'private', 'group_id': self.core.group_id, 'message': 'bad'},
                       {'message_type': 'group', 'user_id': self.core.peer_id, 'message': 'bad'}):
            self.assertNotEqual((await self.core.action({'action': 'send_msg', 'params': params}))['retcode'], 0)
        self.assertFalse(self.sent)
