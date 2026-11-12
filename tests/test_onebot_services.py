"""Composed OneBot path, synthetic backend and opaque platform routing only."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from wechat_bridge.continuous import PrivateBridge, TextBridge
from wechat_bridge.protocol import Bridge, Config, NativeMessage, Receipt
from wechat_bridge.storage.durable import Spool
from wechat_bridge.core.contracts import Conversation, ConversationKind


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Spool(Path(self.directory.name) / 'test.sqlite')
        self.addCleanup(self.state.close)
        self.sent = []
        async def send(destination, body, key):
            self.sent.append((destination, body, key))
            return Receipt('account', destination, str(uuid.uuid4()), 'accepted', '1', '2')
        self.backend = SimpleNamespace(online=True, send_text=send, report=lambda *a, **kw: None)

    def private(self):
        result = PrivateBridge(Config('account', 'self', None, 'peer', send_interval=0),
                               'peer', self.state, self.backend)
        result.connected()
        return result

    async def test_every_handler_validates_whole_request_before_dispatch(self):
        core = self.private()
        for action in ('get_friend_list', 'send_private_msg', 'send_group_msg', 'can_send_image'):
            for fields in ({'unexpected': True}, {'params': []}, {'params': {'self_id': core.self_id + 1}},
                           {'echo': 'x' * 4097}):
                with self.subTest(action=action, fields=fields):
                    answer = await core.action({'action': action, **fields})
                    self.assertEqual(answer['retcode'], 1400)
        self.assertFalse(self.sent)

    def test_compatibility_assembly_uses_composed_engine(self):
        core = self.private()
        self.assertFalse(isinstance(core, Bridge))
        self.assertIsInstance(core, PrivateBridge)
        self.assertEqual(type(core.messages).__module__, 'wechat_bridge.core.messages')
        self.assertEqual(type(core.delivery).__module__, 'wechat_bridge.core.sending')
        self.assertEqual(type(core.registry).__module__, 'wechat_bridge.onebot.registry')

    async def test_registry_handlers_and_opaque_echo(self):
        core = self.private()
        self.assertTrue(hasattr(core, 'registry'), 'formal service needs an action registry')
        for action in ('get_status', 'get_friend_list', 'can_send_image'):
            echo = {'nested': [None, False, 23, {'value': 'opaque'}]}
            answer = await core.action({'action': action, 'echo': echo})
            self.assertEqual(answer['echo'], echo)
            self.assertEqual(answer['retcode'], 0)
            self.assertIn(action, core.registry.handlers)
        self.assertEqual((await core.action({'action': 'delete_msg'}))['retcode'], 1501)

    async def test_complete_chain_and_ambiguous_target_never_submit(self):
        core = self.private()
        self.state.admit('one', {})
        event = core.ingest(NativeMessage('account', 'one', 'peer', 'peer', 'hello', 1000))
        for params in ({'user_id': event['user_id'], 'group_id': 99, 'message': 'bad'},
                       {'user_id': event['user_id'], 'message': [{'type': 'text', 'data': {'text': 'valid'}},
                                                                 {'type': 'video', 'data': {}}]}):
            answer = await core.action({'action': 'send_msg', 'params': params, 'echo': 'opaque'})
            self.assertEqual(answer['retcode'], 1400)
        self.assertFalse(self.sent)

    async def test_formal_service_accepts_non_wechat_group_policy(self):
        self.assertIsNotNone(importlib.util.find_spec('wechat_bridge.onebot'), 'OneBot package must exist')
        from wechat_bridge.onebot.service import OneBotService
        class Policy:
            def conversation(self, account, raw):
                return Conversation(account, raw, ConversationKind.GROUP if raw == 'topic:blue' else ConversationKind.PRIVATE)
            def is_self(self, message):
                return message.sender_id == 'robot' or message.direction == 'outgoing'
            def validate_message(self, message, image_input):
                return message.account == 'account' and message.room_id in {'topic:blue', 'peer'} and not self.is_self(message)
            def wakeup(self, message):
                return False
            def body(self, message):
                return message.body
            def accepted(self, receipt, account, destination):
                return receipt.account == account and receipt.destination == destination and receipt.state == 'accepted'
        config = SimpleNamespace(account='account', self_native_id='robot', room_id='topic:blue',
                                 room_name='Blue', send_interval=0)
        core = OneBotService(config, 'peer', self.state, self.backend, policy=Policy(), groups=True, all_groups=True)
        core.connected()
        message = NativeMessage('account', 'one', 'topic:blue', 'member:red', 'hello', 1000)
        self.state.admit(message.id, {})
        event = core.ingest(message)
        self.assertEqual(event['message_type'], 'group')
        request = {'action': 'send_group_msg', 'params': {'group_id': event['group_id'], 'message': 'response'}, 'echo': [1, None]}
        self.assertEqual((await core.action(request))['retcode'], 0)
        self.assertEqual(self.sent[0][:2], ('topic:blue', 'response'))
        self.assertEqual((await core.action(request))['retcode'], 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual((await core.action({'action': 'get_group_list'}))['data'][0]['group_id'], event['group_id'])
        await core.event_delivered(message, True)
        self.assertEqual(self.state.counts(), {'forwarded': 1})

    def test_formal_host_uses_service_assembly_and_inventory(self):
        root = Path(__file__).resolve().parents[1]
        host = (root / 'scripts/forward_host_worker.py').read_text()
        self.assertIn('from wechat_bridge.adapters.wechat.assembly import create_service', host)
        self.assertNotIn('from wechat_bridge.continuous import', host)
        from scripts.runtime_manifest import PUBLIC_CODE, role_files
        for package in ('onebot', 'core'):
            for path in (root / 'wechat_bridge' / package).glob('*.py'):
                relative = path.relative_to(root).as_posix()
                self.assertIn(relative, PUBLIC_CODE)
                self.assertIn(relative, role_files('host'))

    def test_new_group_identity_allocation_preserves_legacy_order(self):
        core = TextBridge(Config('account', 'self', None, 'group', send_interval=0),
                          'peer', self.state, self.backend, all_groups=True)
        core.connected()
        self.state.admit('one', {})
        event = core.ingest(NativeMessage('account', 'one', 'blue@chatroom', 'member', 'hi', 1000))
        self.assertEqual((core.self_id, core.peer_id, event['group_id'], event['message_id'], event['user_id']),
                         (1, 2, 3, 4, 5))

    async def test_private_constructor_keeps_configured_group_directory(self):
        core = PrivateBridge(Config('account', 'self', 'blue@chatroom', 'Blue', send_interval=0),
                             'peer', self.state, self.backend)
        core.connected()
        expected = [{'group_id': core.group_id, 'group_name': 'Blue'}]
        self.assertEqual((await core.action({'action': 'get_group_list'}))['data'], expected)
        answer = await core.action({'action': 'get_group_info', 'params': {'group_id': core.group_id}})
        self.assertEqual(answer['data'], expected[0])
        self.assertEqual((await core.action({'action': 'send_group_msg'}))['retcode'], 1403)

    def test_continuous_retains_legacy_import_aliases(self):
        from wechat_bridge import continuous, protocol
        for name in ('Bridge', 'NativeMessage', 'Receipt', 'UnsupportedMessageSegment', 'number'):
            self.assertTrue(hasattr(continuous, name), name)
            self.assertIs(getattr(continuous, name), getattr(protocol, name))
