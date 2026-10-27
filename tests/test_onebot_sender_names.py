"""Nickname binding, bounded optional lookups, persistence and message dedup."""
from dataclasses import asdict
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace

from wechat_bridge.continuous import RoutedMessage, TextBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.protocol import Config
from wechat_bridge.sender_names import Resolver


class NameLookup(unittest.TestCase):
    def test_exact_identity_and_group_card_are_preserved_and_cached(self):
        paths = []
        def get(path, deadline, timeout):
            paths.append(path)
            self.assertLessEqual(timeout, .4)
            return {'items': [{'id': 'member', 'username': 'member', 'nickname': '真实昵称🙂',
                               'displayName': '好友备注', 'roomDisplayName': '本群名片'}]}
        resolver = Resolver(SimpleNamespace(policy=SimpleNamespace(prefix='/v1/accounts/account'), _get=get))
        expected = {'sender_nickname': '真实昵称🙂', 'sender_card': '本群名片'}
        self.assertEqual(resolver.resolve('room@chatroom', 'member'), expected)
        self.assertEqual(resolver.resolve('room@chatroom', 'member'), expected)
        self.assertEqual(len(paths), 2)
        self.assertIn('/groups/room%40chatroom/members?search=member', paths[0])

    def test_timeout_is_optional_and_negative_cache_is_bounded(self):
        calls, now = [], [0.]
        def get(path, deadline, timeout):
            calls.append(path)
            raise TimeoutError('upstream details must not escape')
        resolver = Resolver(SimpleNamespace(policy=SimpleNamespace(prefix='/v1/accounts/account'), _get=get),
                            clock=lambda: now[0], capacity=2)
        self.assertEqual(resolver.resolve('room@chatroom', 'member'), {})
        resolver.resolve('room@chatroom', 'member')
        self.assertEqual(len(calls), 2)
        now[0] = 31
        resolver.resolve('room@chatroom', 'member')
        self.assertEqual(len(calls), 4)
        for user in ('another', 'third'):
            resolver.resolve('room@chatroom', user)
        self.assertEqual(len(resolver.cache), 2)

    def test_foreign_ids_deleted_records_and_invalid_names_are_ignored(self):
        for item in ({'id': 'other', 'username': 'other', 'nickname': '不应串人'},
                     {'id': 'member', 'username': 'member', 'nickname': '已删除', 'deleted': True},
                     {'id': 'member', 'username': 'member', 'nickname': '恶意\n名字', 'displayName': 'x' * 257}):
            client = SimpleNamespace(policy=SimpleNamespace(prefix='/v1/accounts/account'),
                                     _get=lambda *args, **kw: {'items': [item]})
            result = Resolver(client).resolve('room@chatroom', 'member')
            self.assertNotIn('sender_nickname', result)


class NameForwarding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'names.sqlite'
        self.state = Spool(self.path)
        self.bind()

    def bind(self):
        self.gateway = Gateway('account', 'peer', self.state, 'profile', lambda _: None, all_groups=True)
        self.core = TextBridge(Config('account', 'self', None, 'legacy'), 'peer', self.state, self.gateway, all_groups=True)
        self.core.connected()

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def deliver(self, room='one@chatroom', identity='one', **names):
        source = RoutedMessage('account', identity, room, 'member', ' 原始正文🙂 ', int(time.time()), **names)
        frame = {'kind': 'event', 'cursor': '2', 'data': asdict(source)}
        self.gateway.feed(frame)
        received = await asyncio.wait_for(self.gateway.next_message(), 1)
        event = self.core.ingest(received)
        await self.core.event_delivered(received, True)
        return event, frame

    async def test_real_names_cross_ipc_and_member_queries_survive_restart(self):
        first, _ = await self.deliver(sender_nickname='微信昵称🙂', sender_card='一群名片')
        second, _ = await self.deliver('two@chatroom', 'two', sender_nickname='微信昵称🙂', sender_card='二群名片')
        self.assertEqual(first['user_id'], second['user_id'])
        self.assertEqual(first['message'][0]['data']['text'], ' 原始正文🙂 ')
        self.assertEqual(first['sender']['nickname'], '微信昵称🙂')
        self.assertEqual(first['sender']['card'], '一群名片')
        self.state.close()
        self.state = Spool(self.path)
        self.bind()
        for event, card in ((first, '一群名片'), (second, '二群名片')):
            answer = await self.core.action({'action': 'get_group_member_info', 'params': {
                'group_id': event['group_id'], 'user_id': event['user_id']}})
            self.assertEqual(answer['data']['card'], card)
            self.assertEqual(answer['data']['nickname'], '微信昵称🙂')
        stranger = await self.core.action({'action': 'get_stranger_info', 'params': {'user_id': first['user_id']}})
        self.assertEqual(stranger['data']['nickname'], '微信昵称🙂')
        fallback, _ = await self.deliver(identity='three')
        self.assertEqual(fallback['sender']['card'], '一群名片')

    async def test_name_changes_do_not_replay_same_source(self):
        _, frame = await self.deliver(sender_nickname='旧昵称')
        frame['data']['sender_nickname'] = '新昵称'
        self.gateway.feed(frame)
        self.assertIsNone(self.state.next_pending())
        self.assertEqual(self.state.counts(), {'forwarded': 1})

    async def test_missing_or_invalid_metadata_preserves_message_and_numeric_identity(self):
        first, _ = await self.deliver(sender_nickname='bad\nname', sender_card='x' * 257)
        self.assertEqual(first['sender']['nickname'], '微信成员' + str(first['user_id']))
        self.assertEqual(first['sender']['card'], '')
        self.assertEqual(first['raw_message'], ' 原始正文🙂 ')
