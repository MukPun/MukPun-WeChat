"""Ordinary group context stays distinct from wakeups and media admission."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from wechat_bridge.continuous import ImageMessage, RoutedMessage, TextBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_config import DEFAULT, read, validate
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.forward_wire import WireError
from wechat_bridge.group_trigger import admitted
from wechat_bridge.protocol import Config, Receipt
from wechat_bridge.state import StateError
from test_onebot_images import PNG


class ContextPolicy(unittest.TestCase):
    def test_old_configs_do_not_silently_broaden_admission(self):
        for missing in (('groupContextEnabled',), ('groupContextEnabled', 'imageInputEnabled'),
                        ('groupContextEnabled', 'imageInputEnabled', 'groupWakeMode')):
            value = read(json.dumps({k: v for k, v in DEFAULT.items() if k not in missing}).encode())
            self.assertIs(value['groupContextEnabled'], False)
            if 'groupWakeMode' in missing:
                self.assertEqual(value['groupWakeMode'], 'prefix')
        for bad in (1, 'true', None):
            with self.assertRaises(ValueError):
                validate({**DEFAULT, 'groupContextEnabled': bad})

    def test_context_never_broadens_image_or_invalid_wake_policy(self):
        self.assertTrue(admitted('普通聊天', False, context=True))
        self.assertFalse(admitted('普通聊天', False, context=False))
        self.assertFalse(admitted('普通聊天', False, context=True, image=True))
        self.assertFalse(admitted('普通聊天', False, 'invalid', context=True))
        self.assertFalse(admitted('普通聊天', 1, context=True))
        self.assertFalse(admitted('   ', False, context=True))


class ContextRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'context.sqlite'
        self.state = Spool(self.path)
        self.frames, self.sent = [], []
        self.bind()

    def bind(self):
        self.gateway = Gateway('account', 'peer', self.state, 'profile', self.frames.append,
            group='room@chatroom', wake_mode='mention_or_prefix', self_sender_id='actual-self',
            image_input=True, group_context=True)
        self.core = TextBridge(Config('account', 'stable-self', 'room@chatroom', '测试群', send_interval=0),
            'peer', self.state, self.gateway, wake_mode='mention_or_prefix', self_sender_id='actual-self',
            image_input=True, group_context=True)
        self.core.connected()
        async def send(destination, body, key):
            self.sent.append((destination, body, key))
            return Receipt('account', destination, str(uuid.uuid4()), 'accepted', '1', '2')
        self.gateway.send_text = send

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    def message(self, identity='one', body=' 普通聊天\r\n中文🙂\t ', sender='member', **kw):
        return RoutedMessage('account', identity, 'room@chatroom', sender, body, int(time.time()), **kw)

    def feed(self, message, cursor='1'):
        self.gateway.feed({'kind': 'event', 'cursor': cursor, 'anchor': 'a' * 64, 'data': asdict(message)})

    async def deliver(self, message, cursor='1'):
        self.feed(message, cursor)
        received = await self.gateway.next_message()
        event = self.core.ingest(received)
        await self.core.event_delivered(received, True)
        return event

    async def test_ordinary_text_keeps_every_byte_without_inventing_at(self):
        message = self.message()
        event = await self.deliver(message)
        self.assertEqual(event['message'], [{'type': 'text', 'data': {'text': message.body}}])
        self.assertEqual(event['raw_message'], message.body)
        self.assertEqual(self.state.message_route(event['message_id']), 'room@chatroom')
        self.assertIs(self.frames[-1]['group_wakeup'], False)
        self.assertEqual(self.state.counts(), {'forwarded': 1})
        self.assertFalse(self.sent)  # Admission never invokes a model/sender.

    async def test_native_at_and_ai_prefix_still_wake(self):
        for i, (body, mentioned) in enumerate((('@机器人\u2005问题', True), ('/ai 问题', False))):
            event = await self.deliver(self.message(str(i), body, mentioned=mentioned), str(i + 1))
            self.assertEqual(event['message'][0], {'type': 'at', 'data': {'qq': str(self.core.self_id)}})
            self.assertEqual(event['message'][1]['data']['text'], '问题' if body.startswith('/ai ') else body)
            self.assertIs(self.frames[-1]['group_wakeup'], True)

    async def test_plain_at_text_does_not_become_a_real_mention(self):
        event = await self.deliver(self.message(body='@机器人 仅有文字，不是真实提及'))
        self.assertEqual([p['type'] for p in event['message']], ['text'])

    async def test_members_and_private_group_interleaving_remain_distinct(self):
        a = await self.deliver(self.message('a', sender='member-a'))
        private = RoutedMessage('account', 'p', 'peer', 'peer', '私聊🙂', int(time.time()))
        p = await self.deliver(private, '2')
        b = await self.deliver(self.message('b', sender='member-b'), '3')
        self.assertNotEqual(a['user_id'], b['user_id'])
        self.assertEqual(a['group_id'], b['group_id'])
        self.assertEqual(p['message_type'], 'private')
        self.assertEqual(self.state.counts(), {'forwarded': 3})

    async def test_astrbot_can_choose_to_reply_to_ordinary_text(self):
        event = await self.deliver(self.message())
        result = await self.core.action({'action': 'send_group_msg', 'echo': 'plugin-decision',
            'params': {'group_id': event['group_id'], 'message': '由AstrBot决定的答复🙂'}})
        self.assertEqual(result['retcode'], 0)
        self.assertEqual([s[:2] for s in self.sent], [('room@chatroom', '由AstrBot决定的答复🙂')])

    async def test_context_does_not_change_wakeup_reply_dedup_scope(self):
        event = await self.deliver(self.message('wake', '/ai 问题'))
        trigger = self.state.cursor('reply_trigger:room@chatroom')
        request = {'action': 'send_group_msg', 'echo': 42, 'params': {'group_id': event['group_id'], 'message': '答复'}}
        self.assertEqual((await self.core.action(request))['retcode'], 0)
        await self.deliver(self.message('context'), '2')
        self.assertEqual(self.state.cursor('reply_trigger:room@chatroom'), trigger)
        self.assertEqual((await self.core.action(request))['retcode'], 0)
        self.assertEqual(len(self.sent), 1)

    async def test_same_id_remains_deduplicated_across_restart(self):
        message = self.message()
        event = await self.deliver(message)
        self.state.close()
        self.state = Spool(self.path)
        self.bind()
        self.feed(message, '2')
        self.assertIsNone(self.state.next_pending())
        self.assertEqual(self.core.group_id, event['group_id'])

    async def test_own_history_and_other_group_still_rejected_without_ack(self):
        source = asdict(self.message())
        for change in ({'sender_id': 'actual-self'}, {'origin': 'history'}, {'direction': 'outgoing'}, {'room_id': 'other@chatroom'}):
            with self.assertRaises(WireError):
                self.gateway.feed({'kind': 'event', 'cursor': '1', 'data': {**source, **change}})
        self.assertFalse(self.frames)
        self.assertEqual(self.state.counts(), {})

    async def test_image_without_wakeup_is_not_upgraded_to_context(self):
        from wechat_bridge.image_input import payload
        message = ImageMessage('account', 'image', 'room@chatroom', 'member', '普通文字', int(time.time()),
            image=payload(PNG, 'image/png', 'thumbnail'))
        with self.assertRaises(WireError):
            self.feed(message)
        self.assertEqual(self.state.counts(), {})

    async def test_context_queue_remains_bounded_and_preserves_pending_text(self):
        for i in range(self.state.LIMIT):
            self.feed(self.message(str(i)), str(i + 1))
        count = len(self.frames)
        with self.assertRaises(StateError):
            self.feed(self.message('overflow'), '33')
        self.assertEqual(len(self.frames), count)
        self.assertEqual(self.state.counts(), {'queued': self.state.LIMIT})
        self.assertEqual(self.state.next_pending()[0], '0')
