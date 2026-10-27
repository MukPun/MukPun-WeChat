"""OneBot policy tests with synthetic identity, clock and backend only."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
import uuid

from wechat_bridge.protocol import Bridge, Config, NativeMessage, Receipt, decode
from wechat_bridge.state import State, StateError
from wechat_bridge.transport import Endpoint, Runner


class Clock:
    now = 1000.
    def __call__(self):
        return self.now


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'state.sqlite'
        self.state = State(self.path)
        self.addCleanup(self.state.close)
        self.clock = Clock()
        self.config = Config('test-account', 'private-self', 'private-room@chatroom', '测试群')
        self.receipt = Receipt('test-account', 'private-room@chatroom', str(uuid.uuid4()), 'accepted', '42', '1000')
        self.backend = type('Backend', (), {'online': True})()
        self.backend.send_text = AsyncMock(return_value=self.receipt)
        self.core = Bridge(self.config, self.state, self.backend, clock=self.clock)
        self.core.connected()

    def incoming(self, identity='incoming-1', **changes):
        values = dict(account='test-account', id=identity, room_id='private-room@chatroom',
                      sender_id='private-user', body='/ai 你好', timestamp=1000)
        return NativeMessage(**{**values, **changes})

    def request(self, **changes):
        value = {'action': 'send_group_msg', 'params': {'group_id': self.core.group_id, 'message': '回复'}, 'echo': 'opaque'}
        return {**value, **changes}

    def test_group_event_has_numeric_ids_array_and_member(self):
        event = self.core.ingest(self.incoming())
        self.assertEqual(event['message_type'], 'group')
        self.assertEqual(event['sender']['user_id'], event['user_id'])
        self.assertEqual(event['message'], [{'type': 'at', 'data': {'qq': str(self.core.self_id)}},
                                          {'type': 'text', 'data': {'text': '你好'}}])
        for key in ('group_id', 'user_id', 'self_id', 'message_id'):
            self.assertIs(type(event[key]), int)
        wire = json.dumps(event)
        for raw in ('private-self', 'private-room@chatroom', 'private-user', 'incoming-1'):
            self.assertNotIn(raw, wire)

    def test_only_live_incoming_text_with_exact_prefix_and_room(self):
        changes = [{'account': 'other'}, {'room_id': 'other@chatroom'}, {'origin': 'history'},
                   {'direction': 'outgoing'}, {'sender_id': 'private-self'}, {'type': 3}, {'type': True},
                   {'body': '普通聊天'}, {'body': '/ai'}, {'body': '/ai '}, {'body': ' /ai hi'},
                   {'timestamp': 995}, {'timestamp': 1010}, {'body': '/ai \ud800'}, {'sender_id': 'x@chatroom'}]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(self.core.ingest(self.incoming(**change)))
        self.backend.send_text.assert_not_called()

    def test_empty_whitelist_denies_all(self):
        core = Bridge(replace(self.config, room_id=None), self.state, self.backend, clock=self.clock)
        core.connected()
        self.assertIsNone(core.ingest(self.incoming()))
        self.assertIsNone(core.group_id)

    def test_no_pending_question_is_replaced(self):
        first = self.core.ingest(self.incoming())
        self.assertIsNone(self.core.ingest(self.incoming('second', sender_id='another')))
        self.assertEqual(self.core.pending['event']['message_id'], first['message_id'])

    async def test_accepted_is_only_success_and_same_destination(self):
        self.core.ingest(self.incoming())
        answer = await self.core.action(self.request())
        self.assertEqual(answer['status'], 'ok')
        self.assertEqual(answer['echo'], 'opaque')
        self.assertIs(type(answer['data']['message_id']), int)
        self.backend.send_text.assert_awaited_once()
        arguments = self.backend.send_text.call_args.args
        self.assertEqual(arguments[:2], ('private-room@chatroom', '回复'))
        self.assertRegex(arguments[2], r'^[0-9a-f]{32}$')
        self.assertNotIn('private-room', json.dumps(answer))

    async def test_send_without_trigger_or_after_expiry_is_rejected(self):
        self.assertEqual((await self.core.action(self.request()))['retcode'], 1403)
        self.core.ingest(self.incoming())
        self.clock.now += 100
        self.assertEqual((await self.core.action(self.request()))['retcode'], 1403)
        self.backend.send_text.assert_not_called()

    async def test_wrong_group_and_private_messages_never_submit(self):
        self.core.ingest(self.incoming())
        for request in (self.request(params={'group_id': self.core.group_id+1, 'message': 'x'}),
                        self.request(action='send_private_msg'),
                        self.request(action='send_msg', params={'message_type': 'private', 'user_id': 1, 'message': 'x'}),
                        self.request(params={'group_id': self.core.group_id, 'message': 'x', 'user_id': 1}),
                        self.request(params={'group_id': self.core.group_id, 'message': 'x', 'self_id': 999})):
            with self.subTest(request=request):
                self.assertEqual((await self.core.action(request))['status'], 'failed')
        self.backend.send_text.assert_not_called()

    async def test_duplicate_echo_after_quoted_reply_returns_saved_result(self):
        event = self.core.ingest(self.incoming())
        request = self.request(params={'group_id': self.core.group_id, 'message': [
            {'type': 'reply', 'data': {'id': str(event['message_id'])}}, {'type': 'text', 'data': {'text': '回复'}}]})
        first = await self.core.action(request)
        second = await self.core.action(request)
        self.assertEqual(first, second)
        self.backend.send_text.assert_awaited_once()

    async def test_same_echo_with_changed_body_fails_without_second_post(self):
        self.core.ingest(self.incoming())
        await self.core.action(self.request())
        changed = self.request(params={'group_id': self.core.group_id, 'message': 'changed'})
        self.assertEqual((await self.core.action(changed))['status'], 'failed')
        self.backend.send_text.assert_awaited_once()

    async def test_concurrent_duplicate_action_is_single_submission(self):
        self.core.ingest(self.incoming())
        first, second = await asyncio.gather(self.core.action(self.request()), self.core.action(self.request()))
        self.assertEqual(first, second)
        self.backend.send_text.assert_awaited_once()

    async def test_unknown_outcome_never_retries(self):
        self.core.ingest(self.incoming())
        self.backend.send_text.side_effect = TimeoutError('private-upstream-content')
        first = await self.core.action(self.request())
        second = await self.core.action(self.request())
        self.assertEqual(first['retcode'], 1408)
        self.assertEqual(first, second)
        self.assertNotIn('private-upstream', json.dumps(first))
        self.backend.send_text.assert_awaited_once()

    async def test_queued_failed_wrong_destination_or_invalid_receipt_cannot_be_success(self):
        for change in ({'state': 'queued'}, {'state': 'needs_review'}, {'state': 'failed'},
                       {'account': 'other'}, {'destination': 'other@chatroom'}, {'client_id': ''},
                       {'server_id': ''}, {'server_id': '0'}, {'send_id': 'not-uuid'}):
            with self.subTest(change=change):
                self.core.disconnected()
                self.core.connected()
                self.clock.now += 10
                self.core.ingest(self.incoming(str(uuid.uuid4()), timestamp=int(self.clock.now)))
                self.backend.send_text.return_value = replace(self.receipt, **change)
                answer = await self.core.action(self.request())
                self.assertEqual(answer['status'], 'failed')

    async def test_native_cancellation_persists_unknown(self):
        self.core.ingest(self.incoming())
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        self.backend.send_text.side_effect = blocked
        task = asyncio.create_task(self.core.action(self.request()))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self.core.action(self.request()))['retcode'], 1408)
        self.backend.send_text.assert_awaited_once()

    async def test_one_reply_per_question_even_with_new_echo(self):
        self.core.ingest(self.incoming())
        await self.core.action(self.request())
        self.assertEqual((await self.core.action(self.request(echo='new')))['retcode'], 1403)
        self.backend.send_text.assert_awaited_once()

    async def test_rate_limit_does_not_create_native_submission(self):
        self.core.ingest(self.incoming())
        await self.core.action(self.request())
        self.core.ingest(self.incoming('second'))
        self.assertEqual((await self.core.action(self.request(echo='second')))['retcode'], 1429)
        self.backend.send_text.assert_awaited_once()

    async def test_segments_are_atomic_and_reply_reference_must_match(self):
        event = self.core.ingest(self.incoming())
        for segment in ({'type': 'image', 'data': {'file': 'x'}},
                        {'type': 'at', 'data': {'qq': 'all'}},
                        {'type': 'reply', 'data': {'id': event['message_id']+1}}):
            request = self.request(params={'group_id': self.core.group_id, 'message': [
                {'type': 'text', 'data': {'text': 'partial'}}, segment]})
            self.assertEqual((await self.core.action(request))['status'], 'failed')
        self.backend.send_text.assert_not_called()

    async def test_literal_cq_requires_auto_escape(self):
        self.core.ingest(self.incoming())
        params = {'group_id': self.core.group_id, 'message': '[CQ:at,qq=all]'}
        self.assertEqual((await self.core.action(self.request(params=params)))['status'], 'failed')
        self.assertEqual((await self.core.action(self.request(params={**params, 'auto_escape': True})))['status'], 'ok')

    async def test_supported_queries_and_unsupported_actions(self):
        event = self.core.ingest(self.incoming())
        for action in ('get_login_info', 'get_status', 'get_version_info', 'get_group_list'):
            self.assertEqual((await self.core.action({'action': action}))['retcode'], 0)
        answer = await self.core.action({'action': 'get_group_member_info', 'params': {
            'group_id': self.core.group_id, 'user_id': event['user_id']}})
        self.assertEqual(answer['data']['user_id'], event['user_id'])
        for action in ('get_group_member_list', 'set_group_kick', 'set_restart', 'send_private_msg'):
            self.assertEqual((await self.core.action({'action': action}))['retcode'], 1501)
        self.backend.send_text.assert_not_called()

    async def test_astrbot_bot_mention_lookup_chain(self):
        self.core.ingest(self.incoming())
        for action, params in (
            ('get_group_member_info', {'group_id': self.core.group_id, 'user_id': self.core.self_id, 'no_cache': False}),
            ('get_stranger_info', {'user_id': self.core.self_id, 'no_cache': False})):
            answer = await self.core.action({'action': action, 'params': {**params, 'self_id': self.core.self_id}})
            self.assertEqual(answer['retcode'], 0)
            self.assertEqual(answer['data']['user_id'], self.core.self_id)
            self.assertTrue(answer['data']['nickname'])

    def test_expired_question_does_not_block_later_questions(self):
        self.core.ingest(self.incoming())
        self.clock.now += self.config.reply_seconds + 1
        event = self.core.ingest(self.incoming('later', timestamp=int(self.clock.now)))
        self.assertIsNotNone(event)
        self.assertEqual(self.core.pending['event']['message_id'], event['message_id'])

    async def test_echo_round_trip_preserves_json_values(self):
        for echo in (None, 0, False, 'x', [1, 'a'], {'nested': 'value'}):
            answer = await self.core.action({'action': 'get_status', 'echo': echo})
            self.assertEqual(answer['echo'], echo)
            self.assertIs(type(answer['echo']), type(echo))

    def test_restart_preserves_identities_and_no_replay(self):
        event = self.core.ingest(self.incoming())
        self.state.close()
        state = State(self.path)
        self.addCleanup(state.close)
        core = Bridge(self.config, state, self.backend, clock=self.clock)
        core.connected()
        self.assertEqual((core.self_id, core.group_id), (self.core.self_id, self.core.group_id))
        self.assertEqual(state.identity('user', 'private-user')[0], event['user_id'])
        self.assertIsNone(core.ingest(self.incoming()))

    async def test_no_plaintext_message_persisted(self):
        self.core.ingest(self.incoming(body='/ai UNIQUE_PRIVATE_QUESTION'))
        await self.core.action(self.request(params={'group_id': self.core.group_id, 'message': 'UNIQUE_PRIVATE_ANSWER'}))
        raw = self.path.read_bytes()
        self.assertNotIn(b'UNIQUE_PRIVATE_QUESTION', raw)
        self.assertNotIn(b'UNIQUE_PRIVATE_ANSWER', raw)

    def test_disconnected_drops_pending(self):
        self.core.ingest(self.incoming())
        self.core.disconnected()
        self.assertIsNone(self.core.pending)
        self.assertFalse(self.core.online)
        self.assertIsNone(self.core.ingest(self.incoming('new')))


class ValidationTests(unittest.TestCase):
    def test_duplicate_keys_nonfinite_and_binary_json_rejected(self):
        for value in ('{"action":"x","action":"y"}', '{"echo":NaN}', '[]', 'x'*65537, b'{}'):
            with self.subTest(value=str(value)[:30]), self.assertRaises((ValueError, TypeError)):
                decode(value)

    def test_endpoint_never_uses_public_or_query_token_urls(self):
        for url in ('ws://example.com/ws', 'ws://localhost/ws', 'ws://127.0.0.1/ws',
                    'ws://127.0.0.1:99/ws?token=secret', 'ws://u:p@127.0.0.1:99/ws',
                    'http://127.0.0.1:99/ws', 'ws://127.0.0.1:99/other'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Endpoint(url, 'synthetic-token-only')
        endpoint = Endpoint('ws://127.0.0.1:12345/ws', 'synthetic-token-only')
        self.assertNotIn('synthetic-token-only', repr(endpoint))

    def test_configuration_bounds(self):
        good = Config('account', 'self', 'group@chatroom', 'group')
        for changes in ({'room_id': '@chatroom'}, {'reply_seconds': 0}, {'send_interval': float('nan')},
                        {'send_interval': -1}, {'room_id': 'private-user'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(good, **changes)


if __name__ == '__main__':
    unittest.main()
