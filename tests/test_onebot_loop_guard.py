from dataclasses import asdict
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from wechat_bridge.continuous import TextBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.forward_wire import WireError
from wechat_bridge.loop_guard import proof, verified_sender
from wechat_bridge.protocol import Config, NativeMessage, Receipt


class IdentityProof(unittest.TestCase):
    def setUp(self):
        self.seed = {'sendId': '11111111-2222-4333-8444-555555555555',
                     'destinationSha256': hashlib.sha256(b'room@chatroom').hexdigest(),
                     'textSha256': hashlib.sha256('答复🙂'.encode()).hexdigest(), 'bytes': len('答复🙂'.encode())}
        self.intent = {'id': self.seed['sendId'], 'accountId': 'account', 'state': 'accepted',
                       'destination': 'room@chatroom', 'localMessageId': 'local'}
        self.message = {'id': 'local', 'accountId': 'account', 'threadId': 'room@chatroom', 'senderId': 'actual-self',
                        'direction': 'outgoing', 'type': 1, 'body': '答复🙂'}

    def verify(self, seed=None, intent=None, message=None):
        return verified_sender(seed or self.seed, intent or self.intent, message or self.message,
                               'account', {'peer', 'room@chatroom'})

    def test_real_sender_is_bound_from_exact_accepted_projection(self):
        self.assertEqual(self.verify(), 'actual-self')

    def test_intent_changes_cannot_supply_self_identity(self):
        for change in ({'id': 'other'}, {'accountId': 'other'}, {'state': 'submitted'},
                       {'destination': 'other@chatroom'}, {'localMessageId': 'other'}, {'localMessageId': None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(intent={**self.intent, **change})

    def test_projection_changes_cannot_supply_self_identity(self):
        for change in ({'id': 'other'}, {'accountId': 'other'}, {'threadId': 'other@chatroom'}, {'senderId': 'peer'},
                       {'senderId': ''}, {'senderId': 'room@chatroom'}, {'direction': 'incoming'},
                       {'type': True}, {'type': 3}, {'body': '不同正文'}, {'body': '答复🙂 '}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(message={**self.message, **change})

    def test_proof_is_bounded_and_has_no_unapproved_fields(self):
        for change in ({'sendId': '../bad'}, {'bytes': True}, {'bytes': 8193}, {'bytes': 0},
                       {'textSha256': 'x' * 64}, {'destinationSha256': ''}, {'token': 'fixture-not-a-real-token'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                proof({**self.seed, **change})


class LoopBoundaries(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'loop.sqlite'
        self.state = Spool(self.path)
        self.reports, self.sent = [], []
        self.bind()

    def bind(self):
        self.gateway = Gateway('account', 'peer', self.state, 'profile', self.reports.append,
                               group='room@chatroom', self_sender_id='actual-self')
        self.core = TextBridge(Config('account', 'stable-onebot-id', 'room@chatroom', 'test', send_interval=0),
                               'peer', self.state, self.gateway, self_sender_id='actual-self')
        self.core.connected()
        async def send(destination, body, key):
            self.sent.append((destination, body))
            return Receipt('account', destination, str(uuid.uuid4()), 'accepted', '1', '2')
        self.gateway.send_text = send

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    def message(self, identity='source', sender='peer', room='room@chatroom', body='/ai 相同正文'):
        return NativeMessage('account', identity, room, sender, body, int(time.time()))

    def feed(self, message, cursor='1'):
        self.gateway.feed({'kind': 'event', 'cursor': cursor, 'data': asdict(message)})

    async def deliver(self, message, cursor='1'):
        self.feed(message, cursor)
        message = await self.gateway.next_message()
        event = self.core.ingest(message)
        await self.core.event_delivered(message, True)
        return event

    async def test_mislabeled_own_group_message_never_enters_inbox(self):
        with self.assertRaises(WireError):
            self.feed(self.message(sender='actual-self'))
        self.assertEqual(self.state.counts(), {})
        self.assertFalse(self.reports)

    async def test_second_layer_rejects_own_sender_before_onebot(self):
        with self.assertRaises(ValueError):
            self.core.ingest(self.message(sender='actual-self'))
        self.assertIsNone(self.state.cursor('reply_trigger:room@chatroom'))

    async def test_outgoing_and_history_are_rejected_without_ack(self):
        for changes in ({'direction': 'outgoing'}, {'origin': 'history'}):
            data = {**asdict(self.message()), **changes}
            with self.assertRaises(WireError):
                self.gateway.feed({'kind': 'event', 'cursor': '1', 'data': data})
        self.assertFalse(self.reports)

    async def test_repeated_source_id_does_not_create_another_model_event(self):
        message = self.message()
        await self.deliver(message)
        self.feed(message, '2')
        self.assertIsNone(self.state.next_pending())
        self.assertIn({'kind': 'report', 'stage': 'loop_guard', 'reason': 'duplicate_input'}, self.reports)

    async def test_new_identical_text_from_same_or_other_member_is_not_discarded(self):
        for index, sender in enumerate(('peer', 'peer', 'other-member')):
            event = await self.deliver(self.message(str(index), sender=sender), str(index + 1))
            self.assertEqual(event['raw_message'], '/ai 相同正文')
        self.assertEqual(self.state.counts(), {'forwarded': 3})

    async def test_same_text_private_and_group_stay_separate(self):
        first = await self.deliver(self.message('p', room='peer', body='/ai 相同正文'))
        second = await self.deliver(self.message('g'), '2')
        self.assertEqual((first['message_type'], second['message_type']), ('private', 'group'))
        self.assertEqual(self.state.message_route(first['message_id']), 'peer')
        self.assertEqual(self.state.message_route(second['message_id']), 'room@chatroom')

    async def test_repeated_reply_action_after_restart_is_not_sent_twice(self):
        message = self.message()
        event = await self.deliver(message)
        request = {'action': 'send_group_msg', 'params': {'group_id': event['group_id'], 'message': '/ai 相同正文'}, 'echo': 7}
        self.assertEqual((await self.core.action(request))['retcode'], 0)
        stable_id = self.core.self_id
        self.state.close()
        self.state = Spool(self.path)
        self.bind()
        self.assertEqual(self.core.self_id, stable_id)
        self.assertEqual((await self.core.action(request))['retcode'], 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn({'kind': 'report', 'stage': 'loop_guard', 'reason': 'duplicate_action'}, self.reports)
        self.feed(message, '2')
        self.assertIsNone(self.state.next_pending())

    async def test_another_user_may_repeat_our_reply_verbatim(self):
        event = await self.deliver(self.message())
        await self.core.action({'action': 'send_group_msg', 'params': {'group_id': event['group_id'],
                               'message': '/ai 相同正文'}, 'echo': 7})
        event2 = await self.deliver(self.message('another-source', sender='other-member'), '2')
        self.assertEqual(event2['raw_message'], self.sent[0][1])
        self.assertEqual(self.state.counts(), {'forwarded': 2})

    async def test_self_identity_does_not_change_established_onebot_id(self):
        self.assertEqual(self.core.self_id, self.state.identity('bot', 'stable-onebot-id')[0])
