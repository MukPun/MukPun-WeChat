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


if __name__ == '__main__':
    unittest.main()
