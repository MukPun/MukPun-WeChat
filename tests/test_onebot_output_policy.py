"""Unsupported output is rejected before submission and keeps text usable."""
from dataclasses import asdict
import asyncio
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from wechat_bridge.continuous import TextBridge
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.protocol import Config, NativeMessage, Receipt

spec = importlib.util.spec_from_file_location('output_policy_worker',
    Path(__file__).resolve().parents[1] / 'scripts/forward_host_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class OutputPolicy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Spool(Path(self.directory.name) / 'spool.sqlite')
        self.sent, self.reports = [], []
        self.gateway = Gateway('account', 'peer', self.state, 'profile', lambda _: None, group='room@chatroom')
        self.core = TextBridge(Config('account', 'self', 'room@chatroom', 'test', send_interval=0),
            'peer', self.state, self.gateway, image_input=True)
        self.core.connected()
        async def send(destination, body, key):
            self.sent.append((destination, body))
            return Receipt('account', destination, str(uuid.uuid4()), 'accepted', '12', '45')
        self.gateway.send_text = send

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def exercise(self, group, message):
        source = NativeMessage('account', 'source', 'room@chatroom' if group else 'peer',
            'member' if group else 'peer', '/ai test' if group else 'test', int(time.time()))
        self.gateway.feed({'kind': 'event', 'cursor': '1', 'anchor': 'a' * 64, 'data': asdict(source)})
        source = await asyncio.wait_for(self.gateway.next_message(), 3)
        event = self.core.ingest(source)
        await self.core.event_delivered(source, True)
        params = {'group_id': event['group_id']} if group else {'user_id': event['user_id']}
        request = {'action': 'send_group_msg' if group else 'send_private_msg',
                   'params': {**params, 'message': message}, 'echo': {'seq': 86}}
        answer = await asyncio.wait_for(self.core.action(request), 3)
        self.assertEqual(answer, {'status': 'failed', 'retcode': 1400, 'data': None,
            'message': 'unsupported_message_segment', 'wording': 'unsupported_message_segment', 'echo': {'seq': 86}})
        self.assertFalse(self.sent)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0], 0)
        with patch.object(worker, 'emit', self.reports.append):
            worker.action_result(answer)
        self.assertEqual(self.reports, [{'kind': 'report', 'stage': 'action_unsupported', 'retcode': 1400}])
        # The same active route and echo remain usable: no reservation or
        # partial text was consumed by the rejected image.
        request['params']['message'] = '完整文本🙂'
        self.assertEqual((await asyncio.wait_for(self.core.action(request), 3))['retcode'], 0)
        self.assertEqual(self.sent, [(source.room_id, '完整文本🙂')])
        self.assertTrue(self.core.online)

    async def test_private_plugin_image_rejected_without_native_io(self):
        await self.exercise(False, [{'type': 'image', 'data': {'file': 'file:///AstrBot/data/meme.gif'}}])

    async def test_group_plugin_image_rejected_without_native_io(self):
        await self.exercise(True, [{'type': 'image', 'data': {'file': 'base64://not-downloaded'}}])

    async def test_mixed_text_and_image_never_sends_partial_text(self):
        await self.exercise(False, [{'type': 'text', 'data': {'text': '不可部分发送'}},
                                   {'type': 'image', 'data': {'file': 'https://example.invalid/meme.png'}}])

    async def test_cq_image_string_is_also_rejected_before_send(self):
        await self.exercise(True, '[CQ:image,file=file:///AstrBot/data/meme.png]')

    async def test_image_input_does_not_advertise_image_output(self):
        answer = await self.core.action({'action': 'can_send_image'})
        self.assertEqual(answer, {'status': 'ok', 'retcode': 0, 'data': {'yes': False}})


class ActionReports(unittest.TestCase):
    def test_unknown_or_unverified_failures_keep_existing_stop_policy(self):
        for code, message in ((1408, 'outcome_unknown'), (1500, 'state_unavailable'),
                              (1400, 'invalid_request'), (1408, 'unsupported_message_segment')):
            with self.subTest(code=code, message=message), patch.object(worker, 'emit') as emit:
                worker.action_result({'retcode': code, 'message': message})
                emit.assert_called_once_with({'kind': 'report', 'stage': 'action_rejected', 'retcode': code})

    def test_success_does_not_emit_rejection(self):
        with patch.object(worker, 'emit') as emit:
            worker.action_result({'retcode': 0, 'data': {'message_id': 123}})
            emit.assert_not_called()
