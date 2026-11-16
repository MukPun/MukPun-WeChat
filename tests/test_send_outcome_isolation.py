"""Per-action failures with real SQLite and synthetic IPC; no model or account."""
import asyncio
import base64
import importlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import test_late_text_receipts as late_tests
from test_onebot_image_output import png, receipt
from test_onebot_voice_output import wav, segment
import test_runtime_lifecycle as runtime_tests
import test_recovery_disposition_integration as disposition_tests
from wechat_bridge import image_output, voice_output
from wechat_bridge.protocol import Receipt


class SendIsolation(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = late_tests.LateText.asyncSetUp
    action = late_tests.LateText.action
    receipt = late_tests.LateText.receipt
    timed_out = late_tests.LateText.timed_out

    async def next_text_succeeds(self):
        body = 'next-' + uuid.uuid4().hex
        task = asyncio.create_task(self.bridge.action({**self.action(body), 'echo': body}))
        for _ in range(100):
            await asyncio.sleep(0)
            sends = [f for f in self.frames if f.get('kind') == 'send']
            if sends and sends[-1]['text'] == body:
                self.gateway.feed(self.receipt(sends[-1]))
                break
            if task.done():
                break
        answer = await asyncio.wait_for(task, 1)
        self.assertEqual(answer['retcode'], 0)

    async def test_text_timeout_does_not_close_delivery_or_resend_old_action(self):
        await self.timed_out()
        self.assertFalse(self.gateway.send_hold)
        self.assertTrue(self.gateway.activation.is_set())
        await self.next_text_succeeds()
        self.assertEqual((await self.bridge.action(self.action()))['retcode'], 1408)
        self.assertEqual(len([f for f in self.frames if f.get('kind') == 'send']), 2)

    async def test_wrong_late_media_target_remains_rejected(self):
        from wechat_bridge.forward_wire import WireError
        self.gateway.image_output = True
        image = image_output.payload(png())
        task = asyncio.create_task(self.gateway.send_image('peer', image, 'a' * 32))
        while not any(f.get('kind') == 'send_image' for f in self.frames):
            await asyncio.sleep(0)
        frame = next(f for f in self.frames if f.get('kind') == 'send_image')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(WireError):
            self.gateway.feed({'kind': 'image_receipt', 'requestId': frame['requestId'],
                               'receipt': receipt(image, destination='other')})

    async def test_receipt_arriving_between_cancel_and_finally_is_bound_to_old_media(self):
        self.gateway.image_output = self.gateway.voice_output = True
        for media, payload in (('image', image_output.payload(png())), ('voice', voice_output.from_segment(segment(wav())))):
            task = asyncio.create_task(getattr(self.gateway, 'send_' + media)('peer', payload, uuid.uuid4().hex))
            count = len(self.frames)
            while not any(f.get('kind') == 'send_' + media for f in self.frames[count:]):
                await asyncio.sleep(0)
            frame = next(f for f in self.frames[count:] if f.get('kind') == 'send_' + media)
            task.cancel()
            self.gateway.feed({'kind': media + '_receipt', 'requestId': frame['requestId'], 'receipt': receipt(payload)})
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_chunk_ack_after_cancellation_cannot_complete_next_request(self):
        self.gateway.voice_output = True
        audio = voice_output.from_segment(segment(wav(frames=32000 * 5)))
        task = asyncio.create_task(self.gateway.send_voice('peer', audio, 'a' * 32))
        while not any(f.get('kind') == 'voice_begin' for f in self.frames):
            if task.done(): await task
            await asyncio.sleep(0)
        frame = next(f for f in self.frames if f.get('kind') == 'voice_begin')
        task.cancel()
        ack = {'kind': 'voice_chunk_ack', 'requestId': frame['requestId'], 'index': -1}
        self.gateway.feed(ack)
        self.gateway.feed({'kind': 'voice_receipt', 'requestId': frame['requestId'], 'receipt': receipt(audio)})
        with self.assertRaises(asyncio.CancelledError):
            await task
        next_task = asyncio.create_task(self.gateway.send_voice('peer', audio, 'b' * 32))
        while len([f for f in self.frames if f.get('kind') == 'voice_begin']) != 2:
            if next_task.done(): await next_task
            await asyncio.sleep(0)
        self.gateway.feed(ack)
        self.assertFalse(self.gateway.chunk_ack.done())
        next_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await next_task

    async def test_image_and_voice_unknown_leave_next_text_available(self):
        self.bridge.image_output = self.gateway.image_output = True
        self.bridge.voice_output = self.gateway.voice_output = True
        for media, message in (
            ('image', [{'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(png()).decode()}}]),
            ('voice', [segment(wav())]),
        ):
            calls = []
            async def unknown(destination, payload, key):
                calls.append(key)
                return receipt(payload, state='unknown', projection_verified=False)
            setattr(self.gateway, 'send_' + media, unknown)
            action = {**self.action(), 'params': {**self.action()['params'], 'message': message}, 'echo': media}
            answer = await self.bridge.action(action)
            self.assertEqual(answer['retcode'], 1408)
            self.assertFalse(self.gateway.send_hold)
            await self.next_text_succeeds()
            self.assertEqual(await self.bridge.action(action), answer)
            self.assertEqual(len(calls), 1)

    async def test_late_media_receipt_does_not_break_the_next_text_request(self):
        self.gateway.image_output = True
        image = image_output.payload(png())
        first = asyncio.create_task(self.gateway.send_image('peer', image, 'a' * 32))
        while not any(f.get('kind') == 'send_image' for f in self.frames):
            await asyncio.sleep(0)
        frame = next(f for f in self.frames if f.get('kind') == 'send_image')
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(self.gateway.send_text('peer', 'new request', 'b' * 32))
        while not any(f.get('kind') == 'send' for f in self.frames):
            await asyncio.sleep(0)
        self.gateway.feed({'kind': 'image_receipt', 'requestId': frame['requestId'], 'receipt': receipt(image)})
        self.assertFalse(second.done())
        text = next(f for f in self.frames if f.get('kind') == 'send')
        self.gateway.feed(self.receipt(text))
        self.assertEqual((await second).state, 'accepted')


class RuntimeIsolation(unittest.TestCase):
    def fixture(self):
        helper = runtime_tests.RuntimeIntegration()
        helper.addCleanup = self.addCleanup
        c, s, d, db = helper.fixture()
        host = helper.peer()
        helper.attach(c, 'host', host)
        s.counters = {'uncertain': 0, 'unsupported_actions': 0}
        return c, s, d, host

    def test_unknown_and_rejected_action_reports_are_diagnostic_only(self):
        from wechat_bridge.runtime.reports import report
        c, s, d, host = self.fixture()
        d.send_outcomes = importlib.import_module('wechat_bridge.runtime.outcomes')
        for stage, code in (('action_rejected', 1408), ('action_failed', 1403), ('action_unsupported', 1400)):
            report(d, s, 'host', {'kind': 'report', 'stage': stage, 'retcode': code})
        self.assertFalse(s.send_reviews)
        self.assertFalse(any(call.args[0].get('kind') == 'send_hold' for call in host.send.call_args_list))
        log = d.owner.STATE / 'send-outcomes.jsonl'
        self.assertEqual([json.loads(line)['retcode'] for line in log.read_text().splitlines()], [1408, 1403, 1400])

    def test_bound_pending_text_does_not_block_capture_or_activation(self):
        from wechat_bridge.runtime.reports import report
        c, s, d, host = self.fixture()
        report(d, s, 'host', {'kind': 'report', 'stage': 'action_pending', 'native_key': 'a' * 32, 'destination': 'peer'})
        self.assertFalse(any(call.args[0].get('kind') == 'send_hold' for call in host.send.call_args_list))

    def output_fixture(self):
        from scripts import run_private_forwarder as core
        c, s, d, host = self.fixture()
        for name in ('audit_value', 'image_audit_value', 'output_images', 'trigger'):
            setattr(d, name, getattr(core, name))
        d.owner.write_json = lambda path, value: path.write_text(json.dumps(value))
        d.output_available = lambda: True
        d.reserve_image_upload = lambda *args, **kw: True
        d.record_image_review = lambda reviews, request, audit, code: audit.update(state='unknown', code=code) or []
        s.settings = {'imageOutputEnabled': True, 'allGroupsEnabled': False, 'segmentBytes': 8192}
        s.pinned.host = SimpleNamespace(client_pid=1234, client_ticks=1234)
        s.armed = True; s.last_input = {'peer': time.monotonic()}; s.source_cache = None
        s.counters.update(accepted_parts=0, image_output_sent=0, image_output_requests=0)
        return c, s, d, host

    def test_root_busy_rejects_only_new_action_without_overwriting_old_audit(self):
        from wechat_bridge.runtime.output import output
        c, s, d, host = self.output_fixture()
        old_request = s.request = {'kind': 'send_image', 'requestId': 'a' * 32}
        old_audit = s.audit = {'state': 'unknown', 'key': 'b' * 32}
        request = dict(kind='send', requestId='c' * 32, destination='peer', text='next', key='d' * 32)
        output(d, s, 'host', request)
        self.assertIs(s.request, old_request); self.assertIs(s.audit, old_audit)
        self.assertEqual(host.send.call_args.args[0]['receipt']['state'], 'recovery_capacity')

    def test_native_unknown_image_clears_only_its_request_and_allows_new_send(self):
        from wechat_bridge.runtime.output import output
        c, s, d, host = self.output_fixture()
        image = image_output.payload(png())
        request = dict(kind='send_image', requestId='a' * 32, destination='peer', key='b' * 32, image=image)
        native = SimpleNamespace(send=Mock()); c.workers.peers['native'] = native
        output(d, s, 'host', request)
        job = 'job_' + str(uuid.uuid4())
        output(d, s, 'native', dict(kind='image_send_intent', requestId='a' * 32, jobId=job))
        result = dict(kind='native_image_result', requestId='a' * 32, ok=False,
                      submitted=True, server_confirmed=False, code='image_needs_review',
                      receipt=receipt(image, job_id=job, account='wechat-primary', state='needs_review', projection_verified=False))
        output(d, s, 'native', result)
        self.assertIsNone(s.request)
        self.assertEqual(s.image_audit['state'], 'unknown')
        output(d, s, 'host', dict(kind='send', requestId='c' * 32, destination='peer', key='d' * 32, text='next'))
        self.assertEqual(native.send.call_args.args[0]['text'], 'next')
        rows = [json.loads(line) for line in (d.owner.STATE / 'send-outcomes.jsonl').read_text().splitlines()]
        self.assertEqual(rows[0]['jobId'], job); self.assertEqual(rows[0]['outcome'], 'unknown')


class OutcomeLogs(unittest.TestCase):
    def test_private_rotating_logs_allow_only_metadata(self):
        module = importlib.import_module('wechat_bridge.runtime.outcomes')
        with tempfile.TemporaryDirectory() as directory:
            core = SimpleNamespace(owner=SimpleNamespace(STATE=Path(directory)))
            audit = {'requestId': 'a' * 32, 'key': 'b' * 32, 'bytes': 123,
                     'destinationSha256': 'c' * 64, 'textSha256': 'd' * 64,
                     'text': 'PRIVATE CONTENT', 'token': 'fixture-PRIVATE-TOKEN', 'destination': 'PRIVATE USER'}
            for _ in range(20):
                module.record(core, 'image', 'unknown', 'image_needs_review', audit, max_bytes=1024)
            logs = sorted(p for p in Path(directory).glob('send-outcomes.jsonl*') if not p.name.endswith('.lock'))
            self.assertEqual(len(logs), 4)
            for path in logs:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertLessEqual(path.stat().st_size, 1024)
                self.assertNotIn('PRIVATE', path.read_text())
                for line in path.read_text().splitlines():
                    row = json.loads(line)
                    self.assertEqual(row['requestId'], 'a' * 32)
                    self.assertEqual(row['outcome'], 'unknown')

    def test_symlink_log_does_not_overwrite_another_file(self):
        module = importlib.import_module('wechat_bridge.runtime.outcomes')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target'; target.write_text('keep')
            (root / 'send-outcomes.jsonl').symlink_to(target)
            with self.assertRaises((OSError, ValueError)):
                module.record(SimpleNamespace(owner=SimpleNamespace(STATE=root)), 'text', 'unknown', 'outcome_unknown')
            self.assertEqual(target.read_text(), 'keep')


class PendingOutcomes(unittest.TestCase):
    def setUp(self):
        helper = disposition_tests.DispositionTests()
        helper.addCleanup = self.addCleanup
        helper.setUp()
        self.helper = helper
        from scripts import run_private_forwarder as core
        self.subject = core
        self.owner_patch = patch.object(core, 'owner', helper.owner)
        self.owner_patch.start(); self.addCleanup(self.owner_patch.stop)
        helper.pinned.host = SimpleNamespace(client_pid=1234, client_ticks=5678)
        self.pending = core.recovery_dispositions.pending
        self.audit = helper.audit
        self.path = helper.root / 'forward-image-live-send.json'

    def test_running_job_is_preserved_without_restore_permission_and_settled_later(self):
        h, core = self.helper, self.subject
        h.archive(h.row(state='running'))
        self.assertIsNone(core.settle_outcome(h.pinned, self.path, self.audit, 'media', 'unconfirmed_quarantined'))
        self.assertTrue(core.audit_preserved(h.pinned, self.audit))
        self.assertFalse(core.audit_cleared(self.audit))
        self.assertFalse((h.root / 'send-dispositions.json').exists())
        self.assertEqual(self.pending.settle(core, h.pinned, h.snapshot), 1)
        import sqlite3
        db = sqlite3.connect(h.root / 'source8/control.sqlite')
        db.execute('DELETE FROM media_jobs'); db.commit(); db.close()
        h.archive(h.row())
        self.assertEqual(self.pending.settle(core, h.pinned, h.snapshot), 0)
        saved = core.owner.read_json(self.path)
        self.assertEqual(saved['state'], 'unknown')
        self.assertTrue(core.audit_cleared(saved))
        self.assertEqual(len(core.owner.read_json(h.root / 'send-dispositions.json')['entries']), 1)

    def test_queued_text_is_pending_and_a_different_native_cannot_reuse_it(self):
        h, core = self.helper, self.subject
        audit = dict(state='unknown', key='a' * 32, requestId='b' * 32,
                     destinationSha256=self.audit['destinationSha256'], textSha256='c' * 64, bytes=1)
        row = ('text', audit['key'], 'hash', 'peer', b'cipher', 'queued', None, None, None, 'time')
        h.archive(row, 'text')
        path = h.root / 'forward-send.json'
        core.settle_outcome(h.pinned, path, audit, 'text', 'unconfirmed_quarantined')
        self.assertTrue(core.audit_preserved(h.pinned, audit))
        h.pinned.host.client_ticks += 1
        self.assertFalse(core.audit_preserved(h.pinned, audit))
        with self.assertRaises(ValueError):
            self.pending.settle(core, h.pinned, h.snapshot)

    def test_pending_settlement_preserves_full_evidence_after_live_slot_reused(self):
        h, core = self.helper, self.subject
        h.archive(h.row(state='running'))
        core.settle_outcome(h.pinned, self.path, self.audit, 'media', 'unconfirmed_quarantined')
        newer = dict(state='accepted', key='d' * 32)
        core.owner.write_json(self.path, newer)
        import sqlite3
        db = sqlite3.connect(h.root / 'source8/control.sqlite')
        db.execute('DELETE FROM media_jobs'); db.commit(); db.close()
        h.archive(h.row())
        self.pending.settle(core, h.pinned, h.snapshot)
        self.assertEqual(core.owner.read_json(self.path), newer)
        archived = self.pending.evidence(core)['entries'][0]['audit']
        self.assertEqual({k:v for k,v in archived.items() if k != 'recoveryDisposition'}, self.audit)
        self.assertTrue(core.audit_cleared(archived))
        self.assertFalse(self.pending.read(core)['entries'])

    def test_only_valid_append_only_ledger_growth_is_retryable(self):
        core = self.subject
        module = core.recovery_dispositions
        entry = dict(kind='media', recordSha256='a' * 64, disposition='unconfirmed_quarantined',
                     evidenceKind='unconfirmed_result', evidenceSha256='b' * 64)
        old = dict(schemaVersion=1, profileSha256='c' * 64, revision=1, entries=[entry])
        new = {**old, 'revision': 2, 'entries': [entry, {**entry, 'recordSha256': 'd' * 64}]}
        self.assertTrue(module.ledger_appended(json.dumps(old), json.dumps(new)))
        for changed in ({**new, 'revision': 3}, {**new, 'profileSha256': 'e' * 64},
                        {**new, 'entries': [{**entry, 'evidenceSha256': 'f' * 64}, new['entries'][1]]}):
            with self.assertRaises(ValueError):
                module.ledger_appended(json.dumps(old), json.dumps(changed))

    def test_failed_readback_without_pre_submit_proof_is_unknown_not_global_failure(self):
        h, core = self.helper, self.subject
        h.archive(h.row(phase='readback', state='failed'))
        marker = core.settle_outcome(h.pinned, self.path, self.audit, 'media', 'rejected_before_submit')
        self.assertEqual(marker['classification'], 'unconfirmed_quarantined')
        self.assertTrue(core.audit_cleared(core.owner.read_json(self.path)))
        self.assertEqual(self.pending.evidence(core)['entries'][0]['audit']['state'], 'unknown')
