import asyncio
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
import unittest

from wechat_bridge.continuous import RoutedMessage, TextBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_config import DEFAULT, validate
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.forward_wire import WireError
from wechat_bridge.group_trigger import self_mentioned, triggered
from wechat_bridge.protocol import Config


class MentionMetadata(unittest.TestCase):
    def test_only_verified_direct_self_mention(self):
        value = {'state': 'known', 'userIds': ['synthetic-self'], 'self': True,
                 'all': None, 'source': 'msgsource.atuserlist'}
        self.assertTrue(self_mentioned(value))
        for changes in ({'state': 'unknown'}, {'self': False}, {'self': None}, {'self': 1},
                        {'source': None}, {'source': 'body-text'}, {'userIds': []}, {'userIds': ['bad\n']}):
            self.assertFalse(self_mentioned({**value, **changes}))

    def test_plain_at_text_and_at_everyone_do_not_wake(self):
        self.assertFalse(triggered('@机器人 普通文本', False, 'mention_or_prefix'))
        self.assertFalse(self_mentioned({'state': 'known', 'userIds': ['notify@all'], 'self': False,
                                      'all': True, 'source': 'msgsource.atuserlist'}))

    def test_selected_policy_and_prefix_compatibility(self):
        self.assertTrue(triggered('@机器人 问题', True, 'mention_or_prefix'))
        self.assertTrue(triggered('/ai 问题', False, 'mention_or_prefix'))
        self.assertFalse(triggered('/ai 问题', False, 'mention'))
        self.assertFalse(triggered('@机器人 问题', True, 'prefix'))
        self.assertEqual(validate({k: v for k, v in DEFAULT.items() if k != 'groupWakeMode'})['groupWakeMode'], 'prefix')


class MentionRoute(unittest.IsolatedAsyncioTestCase):
    async def test_native_at_maps_to_onebot_without_changing_question(self):
        directory = tempfile.TemporaryDirectory()
        state = Spool(Path(directory.name) / 'spool.sqlite')
        try:
            gateway = Gateway('account', 'peer', state, 'profile', lambda _: None, group='room@chatroom', wake_mode='mention_or_prefix')
            core = TextBridge(Config('account', 'self', 'room@chatroom', '测试群', send_interval=0),
                              'peer', state, gateway, wake_mode='mention_or_prefix')
            core.connected()
            message = RoutedMessage('account', 'at-1', 'room@chatroom', 'member', '@机器人\u2005请回复🙂\n完整问题 ',
                                    int(time.time()), mentioned=True)
            gateway.feed({'kind': 'event', 'cursor': '1', 'data': asdict(message)})
            incoming = await gateway.next_message()
            event = core.ingest(incoming)
            self.assertEqual(event['raw_message'], message.body)
            self.assertEqual(event['message'], [{'type': 'at', 'data': {'qq': str(core.self_id)}},
                                               {'type': 'text', 'data': {'text': message.body}}])
            await core.event_delivered(incoming, True)
            # Replayed SSE does not create another AI input.
            gateway.feed({'kind': 'event', 'cursor': '1', 'data': asdict(message)})
            self.assertIsNone(state.next_pending())
            fake = asdict(message)
            fake.update(id='at-2', mentioned=False)
            with self.assertRaises(WireError):
                gateway.feed({'kind': 'event', 'cursor': '2', 'data': fake})
        finally:
            state.close()
            directory.cleanup()
