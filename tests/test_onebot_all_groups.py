"""New groups cross the complete gateway/OneBot path without bridge policy."""
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
from wechat_bridge.image_input import payload
from wechat_bridge.protocol import Config, Receipt
from test_onebot_images import PNG


class AllGroups(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'groups.sqlite'
        self.state = Spool(self.path)
        self.frames, self.sent = [], []
        self.bind()

    def bind(self):
        self.gateway = Gateway('account', 'peer', self.state, 'profile', self.frames.append,
            all_groups=True, self_sender_id='self', image_input=True)
        self.core = TextBridge(Config('account', 'stable-bot', None, 'legacy', send_interval=0),
            'peer', self.state, self.gateway, all_groups=True, self_sender_id='self', image_input=True)
        self.core.connected()
        async def send(destination, body, key):
            self.sent.append((destination, body))
            return Receipt('account', destination, str(uuid.uuid4()), 'accepted', '1', '2')
        self.gateway.send_text = send

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def deliver(self, room='new@chatroom', body='普通消息', **kw):
        message = RoutedMessage('account', uuid.uuid4().hex, room, 'member', body, int(time.time()), **kw)
        return await self.deliver_message(message)

    async def deliver_message(self, message):
        self.gateway.feed({'kind': 'event', 'cursor': '2', 'anchor': 'a' * 64, 'data': asdict(message)})
        received = await self.gateway.next_message()
        event = self.core.ingest(received)
        await self.core.event_delivered(received, True)
        return event

    async def test_new_groups_forward_without_context_or_wakeup_configuration(self):
        a = await self.deliver('first@chatroom', ' 原文\r\n🙂 ')
        b = await self.deliver('later@chatroom', '/ai 不改写')
        self.assertNotEqual(a['group_id'], b['group_id'])
        self.assertEqual(a['message'], [{'type': 'text', 'data': {'text': ' 原文\r\n🙂 '}}])
        self.assertEqual(b['message'], [{'type': 'text', 'data': {'text': '/ai 不改写'}}])
        self.assertFalse(self.sent)
        for event, room in ((a, 'first@chatroom'), (b, 'later@chatroom')):
            result = await self.core.action({'action': 'send_group_msg', 'echo': room,
                'params': {'group_id': event['group_id'], 'message': 'AstrBot答复'}})
            self.assertEqual(result['retcode'], 0)
        self.assertEqual(self.sent, [('first@chatroom', 'AstrBot答复'), ('later@chatroom', 'AstrBot答复')])

    async def test_only_native_mentions_add_at_and_plain_images_are_forwarded(self):
        event = await self.deliver(mentioned=True)
        self.assertEqual(event['message'][0], {'type': 'at', 'data': {'qq': str(self.core.self_id)}})
        message = ImageMessage('account', 'image', 'image@chatroom', 'member', '', int(time.time()),
            type=3, image=payload(PNG, 'image/png', 'thumbnail'))
        image = await self.deliver_message(message)
        self.assertIn('image', [part['type'] for part in image['message']])
        self.assertNotIn('at', [part['type'] for part in image['message']])

    async def test_group_ids_and_queries_survive_restart_and_reply_window_expiry(self):
        event = await self.deliver()
        self.state.close()
        self.state = Spool(self.path)
        self.bind()
        self.core.clock = lambda: time.time() + 3600
        for action, extra in (('get_group_info', {}), ('get_group_member_info', {'user_id': event['user_id']})):
            result = await self.core.action({'action': action, 'params': {'group_id': event['group_id'], **extra}})
            self.assertEqual(result['data']['group_id'], event['group_id'])
        result = await self.core.action({'action': 'get_group_list'})
        self.assertEqual([g['group_id'] for g in result['data']], [event['group_id']])
        result = await self.core.action({'action': 'send_msg', 'params': {'group_id': event['group_id'], 'message': '延迟答复'}})
        self.assertEqual(result['retcode'], 0)
        self.assertEqual((await self.deliver())['group_id'], event['group_id'])

    async def test_loop_guard_and_private_binding_still_apply(self):
        source = asdict(RoutedMessage('account', 'bad', 'new@chatroom', 'member', '消息', int(time.time())))
        for change in ({'sender_id': 'self'}, {'origin': 'history'}, {'direction': 'outgoing'},
                       {'account': 'wrong'}, {'room_id': 'stranger'}, {'room_id': '@chatroom'},
                       {'room_id': 'peer'}, {'room_id': 'bad\n@chatroom'}):
            with self.assertRaises(WireError):
                self.gateway.feed({'kind': 'event', 'cursor': '2', 'data': {**source, **change}})
        self.assertFalse(self.frames)
        result = await self.core.action({'action': 'send_group_msg', 'params': {'group_id': 999, 'message': '错误'}})
        self.assertNotEqual(result['retcode'], 0)
        self.assertFalse(self.sent)

    def test_configuration_upgrades_old_deployments_and_validates_switch(self):
        old = {k: v for k, v in DEFAULT.items() if k != 'allGroupsEnabled'}
        self.assertIs(read(json.dumps(old).encode())['allGroupsEnabled'], True)
        for bad in (1, 'true', None):
            with self.assertRaises(ValueError):
                validate({**DEFAULT, 'allGroupsEnabled': bad})
