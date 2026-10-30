"""A server acceptance may precede its outgoing profile projection."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('projection_wait_worker', Path(__file__).resolve().parents[1] / 'scripts/forward_native_worker.py')
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)
ID = '12345678-1234-4123-8123-123456789abc'
PREFIX = '/v1/accounts/wechat-primary/sends'


def intent(projection=None, server='456'):
    return {'id': ID, 'accountId': 'wechat-primary', 'destination': 'synthetic-peer',
            'state': 'accepted', 'clientId': '123', 'serverId': server, 'localMessageId': projection}


def response(value, status=200):
    return worker.native.sender.Response(status, {'content-type': 'application/json'}, json.dumps(value).encode())


class Clock:
    def __init__(self): self.now = 0.
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class ProjectionWait(unittest.TestCase):
    def client(self, replies):
        clock, transport = Clock(), Mock()
        transport.request.side_effect = replies
        client = worker.RouteSender('wechat-primary', 'synthetic-token', 'synthetic-peer',
                                    'synthetic text', 'b' * 32, transport=transport, clock=clock, sleep=clock.sleep)
        return client, transport

    def test_accepted_text_polls_same_send_until_projection_exists(self):
        client, transport = self.client([response(intent(), 202), response(intent()), response(intent('sent-1'))])
        result = client.run(5, lambda _: None)
        self.assertTrue(result.ok and result.server_confirmed)
        self.assertEqual(client.projection_id, 'sent-1')
        self.assertEqual([call.args for call in transport.request.call_args_list],
                         [('POST', PREFIX), ('GET', PREFIX + '/' + ID), ('GET', PREFIX + '/' + ID)])

    def test_missing_projection_keeps_confirmed_receipt_without_resubmitting(self):
        client, transport = self.client(lambda method, *_args, **_kwargs: response(intent(), 202 if method == 'POST' else 200))
        result = client.run(3, lambda _: None)
        self.assertFalse(result.ok)
        self.assertTrue(result.submitted and result.server_confirmed)
        self.assertEqual(sum(call.args[0] == 'POST' for call in transport.request.call_args_list), 1)

    def test_projection_poll_cannot_change_the_accepted_server_identity(self):
        client, transport = self.client([response(intent(), 202), response(intent('sent-1', server='789'))])
        result = client.run(5, lambda _: None)
        self.assertFalse(result.ok)
        self.assertTrue(result.server_confirmed)
        self.assertEqual(sum(call.args[0] == 'POST' for call in transport.request.call_args_list), 1)

    def test_transient_confirmation_poll_retries_get_without_resubmitting(self):
        client, transport = self.client([response(intent(), 202),
            worker.native.sender.SendError('transport_timeout'), response(intent('sent-1'))])
        result = client.run(5, lambda _: None)
        self.assertTrue(result.ok and result.server_confirmed)
        self.assertEqual([call.args[0] for call in transport.request.call_args_list], ['POST', 'GET', 'GET'])

    def projection(self, **changes):
        return {'id': 'sent-1', 'accountId': 'wechat-primary', 'threadId': 'synthetic-peer',
                'direction': 'outgoing', 'type': 1, 'senderId': 'synthetic-self',
                'body': 'synthetic text', 'serverId': '456', **changes}

    def lookup(self, clock, replies):
        transport = Mock()
        def request(*args, **kwargs):
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return worker.contact.Response(200, {'content-type': 'application/json'}, json.dumps(value).encode())
        transport.request.side_effect = request
        return worker.contact.ContactReceiveClient('wechat-primary', 'synthetic-token',
            transport=transport, clock=clock, sleep=clock.sleep), transport

    def accepted(self):
        client, transport = self.client([response(intent('sent-1'), 202)])
        self.assertTrue(client.run(5, lambda _: None).ok)
        return client, transport

    def test_final_projection_read_recovers_from_transient_failure(self):
        client, send_transport = self.accepted()
        lookup, transport = self.lookup(client._clock, iter([
            worker.contact.ContactError('transport_timeout'), self.projection()]))
        self.assertTrue(client.verify_projection(lookup, 'synthetic-self', 5))
        self.assertEqual(transport.request.call_count, 2)
        self.assertTrue(all(call.args == ('GET', '/v1/accounts/wechat-primary/messages/sent-1')
                            for call in transport.request.call_args_list))
        self.assertEqual(send_transport.request.call_count, 1)

    def test_failed_final_read_preserves_receipt_and_returns_a_fixed_reason(self):
        client, _ = self.accepted()
        lookup, transport = self.lookup(client._clock, iter([
            worker.contact.ContactError('transport_timeout') for _ in range(3)]))
        self.assertFalse(client.verify_projection(lookup, 'synthetic-self', 5))
        self.assertEqual(client.confirmation_code, 'transport_timeout')
        self.assertEqual(client.receipt['state'], 'accepted')
        self.assertEqual(transport.request.call_count, 3)

    def test_final_projection_mismatch_does_not_retry_or_report_success(self):
        for changes in ({'serverId': '789'}, {'body': 'another'}, {'senderId': 'another'},
                        {'accountId': 'other'}, {'direction': 'incoming'}, {'type': True}):
            with self.subTest(changes=changes):
                client, _ = self.accepted()
                lookup, transport = self.lookup(client._clock, iter([self.projection(**changes)]))
                self.assertFalse(client.verify_projection(lookup, 'synthetic-self', 5))
                self.assertEqual(client.confirmation_code, 'invalid_message')
                self.assertEqual(transport.request.call_count, 1)

    def test_invalid_final_response_is_terminal_but_does_not_escape(self):
        client, _ = self.accepted()
        lookup, transport = self.lookup(client._clock, iter([worker.contact.ContactError('invalid_response')]))
        self.assertFalse(client.verify_projection(lookup, 'synthetic-self', 5))
        self.assertEqual(client.confirmation_code, 'invalid_response')
        self.assertEqual(transport.request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
