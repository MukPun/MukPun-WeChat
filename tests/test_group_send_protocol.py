"""Synthetic one-group send checks; no credentials or external requests."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('tested_group_sender', Path(__file__).resolve().parents[1] / 'scripts/group_send_protocol.py')
sender = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sender
SPEC.loader.exec_module(sender)
ACCOUNT, TOKEN, ROOM, NAME = 'wechat-primary', 'synthetic-token-only', 'test-room@chatroom', '测试群'
KEY = '1' * 32
IDENTITY = '12345678-1234-4123-8123-123456789abc'
POST = '/v1/accounts/' + ACCOUNT + '/sends'


def response(state='accepted', **changes):
    value = {'id': IDENTITY, 'accountId': ACCOUNT, 'destination': ROOM, 'state': state,
             'clientId': '42', 'serverId': '1001' if state == 'accepted' else None}
    value.update(changes)
    return sender.Response(202 if state == 'queued' else 200, {'content-type': 'application/json'}, json.dumps(value).encode())


class Clock:
    def __init__(self):
        self.now = 0.
    def __call__(self):
        return self.now
    def sleep(self, value):
        self.now += value


class GroupSendTests(unittest.TestCase):
    def client(self, replies=None, lookup=None, **kwargs):
        clock = Clock()
        transport = Mock()
        transport.request.side_effect = replies or [response('queued'), response()]
        client = sender.GroupSendClient(ACCOUNT, TOKEN, ROOM, NAME, KEY, transport=transport,
            lookup=lookup or (lambda deadline: (ROOM, '10')), clock=clock, sleep=clock.sleep, **kwargs)
        return client, transport

    def test_fixed_group_text_key_one_post_and_bound_poll(self):
        ticket = Mock()
        client, transport = self.client(on_intent=ticket)
        output = []
        receipt = client.run(100, output.append)
        self.assertEqual(receipt, sender.SendResult(True, 'ok', True, True))
        self.assertEqual([call.args for call in transport.request.call_args_list], [
            ('POST', POST), ('GET', POST + '/' + IDENTITY)])
        options = transport.request.call_args_list[0].kwargs
        self.assertEqual(json.loads(options['body']), {'threadId': ROOM, 'text': sender.TEST_TEXT})
        self.assertEqual(options['headers'], {'Content-Type': 'application/json', 'Idempotency-Key': KEY})
        ticket.assert_called_once_with(IDENTITY)
        self.assertNotIn(ROOM, json.dumps(output))
        self.assertNotIn(TOKEN, json.dumps(output))
        self.assertNotIn(KEY, json.dumps(output))
        self.assertEqual(client.run(100, output.append).code, 'client_already_used')
        self.assertEqual(transport.request.call_count, 2)

    def test_wrong_or_ambiguous_group_prevents_post(self):
        for lookup in (lambda _: ('different@chatroom', '10'), lambda _: (ROOM,),
                       Mock(side_effect=sender.groups.GroupError('target_ambiguous'))):
            with self.subTest(lookup=lookup):
                client, transport = self.client(lookup=lookup)
                result = client.run(100, lambda _: None)
                self.assertFalse(result.submitted)
                self.assertFalse(result.ok)
                transport.request.assert_not_called()

    def test_private_targets_empty_rooms_and_invalid_keys_reject(self):
        for room, key in (('wxid_direct', KEY), ('@chatroom', KEY), ('bad\n@chatroom', KEY),
                          (ROOM, 'short'), (ROOM, 'A' * 32)):
            with self.subTest(room=room), self.assertRaises(sender.SendError):
                sender.GroupSendClient(ACCOUNT, TOKEN, room, NAME, key)

    def test_timeout_after_post_is_unknown_and_never_retried(self):
        client, transport = self.client([TimeoutError()])
        result = client.run(100, lambda _: None)
        self.assertEqual(result, sender.SendResult(False, 'outcome_unknown', True, False))
        self.assertEqual(transport.request.call_count, 1)
        self.assertEqual(client.run(100, lambda _: None).code, 'client_already_used')
        self.assertEqual(transport.request.call_count, 1)

    def test_receipt_must_match_group_and_server_identity(self):
        for changed in ({'destination': 'other@chatroom'}, {'accountId': 'other'}, {'serverId': None},
                        {'clientId': None}, {'id': '12345678-1234-4123-8123-123456789abd'}):
            with self.subTest(changed=changed):
                client, transport = self.client([response('queued'), response(**changed)])
                result = client.run(100, lambda _: None)
                self.assertEqual(result.code, 'outcome_unknown')
                self.assertFalse(result.server_confirmed)
                self.assertEqual(sum(call.args[0] == 'POST' for call in transport.request.call_args_list), 1)

    def test_failed_private_ticket_callback_stops_without_resubmission(self):
        client, transport = self.client(on_intent=Mock(side_effect=RuntimeError('private')))
        self.assertEqual(client.run(100, lambda _: None).code, 'outcome_unknown')
        self.assertEqual(transport.request.call_count, 1)

    def test_fixed_policy_cannot_send_other_text_or_use_other_routes(self):
        policy = sender.RequestPolicy(ACCOUNT)
        policy.bind(ROOM, KEY)
        for method, path, headers, body in (
            ('POST', POST, policy._headers, b'{}'),
            ('POST', POST + '?extra=1', policy._headers, policy._body),
            ('POST', '/v1/accounts/' + ACCOUNT + '/connect', policy._headers, policy._body),
            ('GET', '/v1/accounts/' + ACCOUNT + '/events', None, None)):
            with self.subTest(path=path), self.assertRaises(sender.SendError):
                policy.authorize(method, path, headers=headers, body=body)

    def test_default_lookup_uses_group_catalog_not_private_contacts(self):
        with patch.object(sender.groups, 'GroupReceiveClient') as group_reader:
            group_reader.return_value._target.return_value = (ROOM, '10')
            transport = Mock(request=Mock(side_effect=[response('queued'), response()]))
            client = sender.GroupSendClient(ACCOUNT, TOKEN, ROOM, NAME, KEY, transport=transport)
            self.assertTrue(client.run(sender.time.monotonic() + 90, lambda _: None).ok)
            group_reader.assert_called_once()
            group_reader.return_value._target.assert_called_once()


if __name__ == '__main__':
    unittest.main()
