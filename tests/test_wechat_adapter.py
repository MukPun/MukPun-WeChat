"""WeChat contract boundaries, exercised without Native credentials or services."""
import asyncio
from dataclasses import asdict
import importlib
from pathlib import Path
import tempfile
import unittest

from wechat_bridge.core.contracts import NativeMessage, ConversationKind
from wechat_bridge.storage import Spool
from wechat_bridge.forward_wire import WireError, MAX_FRAME, encode

ROOT = Path(__file__).resolve().parents[1]


class AdapterContract(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        self.assertTrue((ROOT/'wechat_bridge/adapters/wechat/proxy.py').is_file(),
                        'Missing production WeChat IPC adapter')
        return importlib.import_module('wechat_bridge.adapters.wechat.proxy').WeChatIPCBackend

    def test_formal_and_legacy_gateway_use_same_backend(self):
        backend = self.adapter()
        from wechat_bridge.forward_gateway import Gateway
        self.assertIs(Gateway, backend)
        self.assertIn('adapters.wechat.proxy', (ROOT/'scripts/forward_host_worker.py').read_text())
        self.assertNotIn('continuous', (ROOT/'wechat_bridge/adapters/wechat/proxy.py').read_text())

    def test_native_entrypoint_assembles_adapter_and_sse_compatibility(self):
        self.adapter()
        self.assertLess(len((ROOT/'scripts/forward_native_worker.py').read_text().splitlines()), 90)
        from wechat_bridge.adapters.wechat import sse
        from scripts import forward_sse
        self.assertIs(forward_sse.Pump, sse.Pump)
        from scripts import runtime_manifest
        self.assertEqual(runtime_manifest.NATIVE_SOURCES['wechat_native.py'],
                         'wechat_bridge/adapters/wechat/native.py')
        self.assertEqual(runtime_manifest.NATIVE_SOURCES['wechat_sse.py'],
                         'wechat_bridge/adapters/wechat/sse.py')

    def test_platform_policy_normalizes_sources_and_own_identity(self):
        self.adapter()
        from wechat_bridge.adapters.wechat.policy import WeChatPolicy
        policy = WeChatPolicy('account', 'peer', self_sender_id='self', all_groups=True)
        for raw, kind in [('peer', ConversationKind.PRIVATE), ('room@chatroom', ConversationKind.GROUP)]:
            self.assertEqual(policy.conversation('account', raw).kind, kind)
            message = NativeMessage('account', 'm', raw, 'peer' if raw=='peer' else 'member', '完整\r\n🙂', 7)
            policy.check_source(asdict(message))
            self.assertFalse(policy.is_self(message))
        for sender, direction in [('self','incoming'), ('member','outgoing')]:
            message = NativeMessage('account','m','room@chatroom',sender,'same body',7,direction=direction)
            self.assertTrue(policy.is_self(message))
            with self.assertRaisesRegex(WireError,'source_binding'):policy.check_source(asdict(message))
        with self.assertRaisesRegex(WireError,'source_binding'):
            policy.check_source(asdict(NativeMessage('account','m','peer','other','body',7)))

    async def test_real_spool_commits_payload_and_opaque_checkpoint_before_ack(self):
        backend = self.adapter()
        with tempfile.TemporaryDirectory() as directory:
            state = Spool(Path(directory)/'state.sqlite')
            try:
                observed = []
                def emit(frame):
                    if frame['kind']=='ack':
                        observed.append((frame,state.cursor('generation'),state.next_pending()))
                proxy = backend('account','peer',state,'generation',emit,all_groups=True)
                data = asdict(NativeMessage('account','message','room@chatroom','member','完整\r\n🙂',7))
                proxy.feed({'kind':'event','cursor':'8','anchor':'a'*64,'data':data})
                self.assertEqual(observed[0][1],'{"cursor":"8","anchor":"'+'a'*64+'"}')
                self.assertEqual(observed[0][2][1],data)
                self.assertEqual((await proxy.next_message()).body,data['body'])
                proxy.feed({'kind':'checkpoint','cursor':'9','anchor':'b'*64})
                self.assertEqual(len(observed),2)
                self.assertIn('"cursor":"9"', observed[1][1])
                self.assertEqual(type(proxy).__mro__[1].__module__, 'wechat_bridge.core.delivery')
            finally:state.close()

    async def test_text_preserved_and_receipt_bound_to_request(self):
        backend = self.adapter()
        frames = []
        proxy = backend('account','peer',None,'generation',frames.append)
        body = ' 完整\r\n中文🙂\t '
        task = asyncio.create_task(proxy.send_text('peer',body,'a'*32))
        await asyncio.sleep(0)
        frame = frames.pop()
        self.assertEqual(frame['text'], body)
        receipt = dict(account='account',destination='peer',send_id='send',state='accepted')
        with self.assertRaisesRegex(WireError,'receipt_binding'):
            proxy.feed({'kind':'receipt','requestId':'b'*32,'receipt':receipt})
        proxy.feed({'kind':'receipt','requestId':frame['requestId'],'receipt':receipt})
        self.assertEqual((await task).state,'accepted')
        self.assertFalse(proxy.futures)
        self.assertLess(len(encode(frame)),MAX_FRAME)

    def test_native_send_evidence_distinguishes_rejection_from_unknown(self):
        self.adapter()
        from wechat_bridge.adapters.wechat.receipts import send_result
        from wechat_bridge.core.contracts import SendOutcome
        self.assertEqual(send_result(False,False,False,'timeout').outcome,SendOutcome.REJECTED)
        self.assertEqual(send_result(False,False,True,'response').outcome,SendOutcome.UNKNOWN)
        self.assertEqual(send_result(True,True,True,'ok').outcome,SendOutcome.ACCEPTED)
        self.assertEqual(send_result(True,False,True,'ok').outcome,SendOutcome.UNKNOWN)

    def test_isolated_runner_includes_wechat_adapter_contract(self):
        self.assertIn("'test_wechat_adapter.py'", (ROOT/'scripts/run_onebot_tests.py').read_text())

    def test_admission_conflict_never_acknowledges_or_advances_checkpoint(self):
        backend = self.adapter()
        from wechat_bridge.storage import StateError
        with tempfile.TemporaryDirectory() as directory:
            state = Spool(Path(directory)/'state.sqlite')
            try:
                frames = []
                proxy = backend('account','peer',state,'generation',frames.append)
                data = asdict(NativeMessage('account','same-id','peer','peer','body',7))
                proxy.feed({'kind':'event','cursor':'8','data':data})
                frames.clear()
                with self.assertRaisesRegex(StateError,'source_changed'):
                    proxy.feed({'kind':'event','cursor':'9','data':{**data,'body':'changed'}})
                self.assertFalse(frames)
                self.assertEqual(state.cursor('generation'),'8')
                self.assertEqual(state.next_pending()[1],data)
                proxy.feed({'kind':'event','cursor':'9','data':data})
                self.assertEqual(state.counts(),{'queued':1})
                self.assertEqual(frames[-1],{'kind':'ack','cursor':'9'})
            finally:state.close()

    async def test_complete_image_and_voice_payloads_keep_bound_outcomes(self):
        backend = self.adapter()
        from test_onebot_image_output import png, receipt
        from test_onebot_voice_output import wav, segment
        from wechat_bridge import image_output, voice_output
        payloads = {'image':image_output.payload(png()),
                    'voice':voice_output.from_segment(segment(wav()))}
        for kind, payload in payloads.items():
            frames = []
            proxy = backend('account','peer',None,'generation',frames.append,
                            image_output=True,voice_output=True)
            for outcome in ('accepted','rejected','unknown'):
                with self.subTest(kind=kind,outcome=outcome):
                    task = asyncio.create_task(getattr(proxy,'send_'+kind)('peer',payload,'a'*32))
                    await asyncio.sleep(0)
                    frame = frames.pop()
                    self.assertEqual(frame[kind],payload)
                    value = receipt(payload,state=outcome)
                    if outcome=='rejected':
                        value.update(job_id='',server_id='',projection_verified=False)
                    bad = {**value,'source_sha256':'b'*64}
                    with self.assertRaisesRegex(WireError,'image_receipt_binding'):
                        proxy.feed({'kind':kind+'_receipt','requestId':frame['requestId'],'receipt':bad})
                    proxy.feed({'kind':kind+'_receipt','requestId':frame['requestId'],'receipt':value})
                    self.assertEqual(await task,value)
                    self.assertFalse(proxy.futures)

    def test_native_compatibility_helpers_accept_legacy_keyword_arguments(self):
        from scripts import forward_native_worker as worker
        import threading
        import time
        from unittest.mock import patch
        for kind in ('image','voice'):
            frames=[]
            with patch.object(worker,'emit',frames.append):
                try:
                    getattr(worker,'send_'+kind+'_command')(
                        config={'account':'account'},own='self',
                        command={'requestId':'a'*32,'destination':'peer',kind:{'sha256':'b'*64}},
                        deadline=time.monotonic()-1,media_lock=threading.Lock())
                except TypeError as error:
                    self.fail('Legacy Native helper keyword contract: '+str(error))
            self.assertIs(frames[0]['submitted'],False)
            self.assertEqual(frames[0]['code'],kind+'_timeout')
