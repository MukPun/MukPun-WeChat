"""Real SQLite + synthetic IPC; no Native service, network or model."""
import asyncio
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from wechat_bridge.continuous import TextBridge
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.forward_wire import WireError
from wechat_bridge.protocol import Config, NativeMessage
from wechat_bridge.storage import Spool


class LateText(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state=Spool(Path(self.temp.name)/'bridge.sqlite'); self.addCleanup(self.state.close)
        self.frames=[]
        self.gateway=Gateway('account','peer',self.state,'generation',self.frames.append)
        self.gateway.TEXT_TIMEOUT=.01
        self.bridge=TextBridge(Config('account','self','room@chatroom','synthetic',send_interval=0),
            'peer',self.state,self.gateway)
        self.bridge.connected()
        self.gateway.feed(dict(kind='event',cursor='1',data=asdict(
            NativeMessage('account','incoming','peer','peer','question',int(time.time())))))
        self.event=self.bridge.ingest(await self.gateway.next_message())
        await self.bridge.event_delivered(NativeMessage('account','incoming','peer','peer','question',1),True)

    def action(self,body='reply'):
        return dict(action='send_private_msg',params=dict(user_id=self.event['user_id'],message=body),echo='same')

    def receipt(self,frame,**change):
        return dict(kind='receipt',requestId=frame['requestId'],receipt=dict(
            account='account',destination='peer',send_id=str(uuid.uuid4()),state='accepted',
            client_id='12',server_id='34',**change))

    async def timed_out(self, body='reply'):
        answer=await asyncio.wait_for(self.bridge.action(self.action(body)),1)
        self.assertEqual(answer['retcode'],1408)
        return next(frame for frame in self.frames if frame.get('kind')=='send')

    async def test_late_accepted_receipt_updates_only_original_unknown_without_resending(self):
        frame=await self.timed_out()
        late=self.receipt(frame)
        self.gateway.feed(late)
        answer=await self.bridge.action(self.action())
        self.assertEqual(answer['retcode'],0)
        self.gateway.feed(late)
        self.assertEqual(len([f for f in self.frames if f.get('kind')=='send']),1)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM submissions').fetchone()[0],1)
        self.assertTrue(any(f.get('stage')=='late_text_receipt' and f['reconciled'] for f in self.frames))

    async def test_late_receipt_wrong_target_and_unknown_request_are_rejected(self):
        frame=await self.timed_out()
        late=self.receipt(frame)
        late['receipt']['destination']='other'
        with self.assertRaisesRegex(WireError,'receipt_binding'): self.gateway.feed(late)
        late=self.receipt(frame); late['requestId']='0'*32
        with self.assertRaisesRegex(WireError,'receipt_binding'): self.gateway.feed(late)
        self.assertEqual((await self.bridge.action(self.action()))['retcode'],1408)

    async def test_late_receipt_cannot_overwrite_conflicting_result(self):
        frame=await self.timed_out()
        key=self.state.db.execute('SELECT key FROM submissions').fetchone()[0]
        self.state.finish(key,{'status':'failed','retcode':1400,'data':None})
        self.gateway.feed(self.receipt(frame))
        self.assertEqual((await self.bridge.action(self.action()))['retcode'],1400)

    async def test_timeout_keeps_local_delivery_open(self):
        await self.timed_out()
        self.assertFalse(self.gateway.send_hold)
        other=self.action();other['echo']='another-action'
        self.assertEqual((await self.bridge.action(other))['retcode'],1408)
        self.assertEqual(len([f for f in self.frames if f.get('kind')=='send']),2)
        self.assertTrue(self.gateway.activation.is_set())

    async def test_late_first_part_does_not_claim_multisegment_success(self):
        frame=await self.timed_out('a'*20000)
        self.gateway.feed(self.receipt(frame))
        self.assertEqual((await self.bridge.action(self.action('a'*20000)))['retcode'],1408)
        self.assertEqual(len([f for f in self.frames if f.get('kind')=='send']),1)

    def test_state_cas_is_bound_to_digest_native_key_and_unknown_result(self):
        self.state.reserve('key','digest','native')
        result={'status':'ok','retcode':0,'data':{'message_id':1}}
        self.assertFalse(self.state.finish_unknown('key','other','native',result))
        self.assertFalse(self.state.finish_unknown('key','digest','other',result))
        self.assertTrue(self.state.finish_unknown('key','digest','native',result))
        self.assertTrue(self.state.finish_unknown('key','digest','native',result))


class AdmissionBarrier(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=LateText.asyncSetUp
    action=LateText.action
    receipt=LateText.receipt
    timed_out=LateText.timed_out

    async def test_receipt_during_cancellation_cannot_overwrite_verified_success(self):
        self.gateway.TEXT_TIMEOUT=1
        task=asyncio.create_task(self.bridge.action(self.action()))
        while not any(f.get('kind')=='send' for f in self.frames): await asyncio.sleep(0)
        frame=next(f for f in self.frames if f.get('kind')=='send')
        self.gateway.futures[frame['requestId']].cancel()
        self.gateway.feed(self.receipt(frame))
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual((await self.bridge.action(self.action()))['retcode'],0)
        self.assertEqual(len([f for f in self.frames if f.get('kind')=='send']),1)

    async def test_capture_pause_precedes_reservation_and_send_timer(self):
        token='a'*32
        self.gateway.feed({'kind':'capture_pause','token':token})
        task=asyncio.create_task(self.bridge.action(self.action()))
        await asyncio.sleep(.03)
        self.assertFalse(task.done())
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM submissions').fetchone()[0],0)
        self.assertFalse(any(f.get('kind')=='send' for f in self.frames))
        self.assertIn({'kind':'capture_paused','token':token},self.frames)
        self.gateway.feed({'kind':'capture_resume','token':token})
        self.assertEqual((await task)['retcode'],1408)

    async def test_active_action_defers_capture_without_closing_admission(self):
        self.gateway.TEXT_TIMEOUT=1
        task=asyncio.create_task(self.bridge.action(self.action()))
        while not any(f.get('kind')=='send' for f in self.frames): await asyncio.sleep(0)
        token='b'*32
        self.gateway.feed({'kind':'capture_pause','token':token})
        self.assertNotIn({'kind':'capture_paused','token':token},self.frames)
        self.assertIn({'kind':'capture_deferred','token':token},self.frames)
        self.assertIsNone(self.gateway.capture_token)
        frame=next(f for f in self.frames if f.get('kind')=='send')
        self.gateway.feed(self.receipt(frame))
        self.assertEqual((await task)['retcode'],0)
        self.assertNotIn({'kind':'capture_paused','token':token},self.frames)

    async def test_review_hold_blocks_send_but_status_is_available(self):
        self.gateway.feed({'kind':'send_hold','hold':True})
        self.assertEqual((await self.bridge.action(self.action()))['retcode'],1403)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM submissions').fetchone()[0],0)
        self.assertEqual((await self.bridge.action({'action':'get_status','params':{}}))['retcode'],0)

    async def test_expired_receipt_is_not_admitted(self):
        frame=await self.timed_out()
        self.gateway.retired_text[frame['requestId']]['expires']=0
        with self.assertRaisesRegex(WireError,'receipt_binding'): self.gateway.feed(self.receipt(frame))

    async def test_retired_receipt_cache_has_a_fixed_capacity(self):
        self.gateway.LATE_LIMIT=2
        for _ in range(3):
            with self.assertRaises(TimeoutError):
                await self.gateway.send_text('peer','reply',uuid.uuid4().hex)
        self.assertEqual(len(self.gateway.retired_text),2)
        first=next(f for f in self.frames if f.get('kind')=='send')
        with self.assertRaisesRegex(WireError,'receipt_binding'): self.gateway.feed(self.receipt(first))
