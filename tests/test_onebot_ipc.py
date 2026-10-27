"""Synthetic framing, Native allowlist and asynchronous IPC receipt tests."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock
import uuid

from wechat_bridge import ipc
from wechat_bridge.gateway import Gateway
from wechat_bridge.protocol import Receipt

SPEC=importlib.util.spec_from_file_location('tested_bridge_native',Path(__file__).resolve().parents[1]/'scripts/bridge_native_protocol.py')
native=importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name]=native
SPEC.loader.exec_module(native)


def message(**changes):
    return {'account':'wechat-primary','id':'synthetic-message','room_id':'synthetic@chatroom',
            'sender_id':'synthetic-user','body':'/ai hello','timestamp':int(time.time()),
            'origin':'live','direction':'incoming','type':1,**changes}


class FrameTests(unittest.TestCase):
    def test_fragmented_and_combined_private_frames(self):
        lines=ipc.Lines()
        raw=ipc.encode({'kind':'x','text':'中文'})+ipc.encode({'kind':'y'})
        for byte in raw[:5]:
            lines.feed(bytes([byte]))
            self.assertIsNone(lines.pop())
        lines.feed(raw[5:])
        self.assertEqual(lines.pop(),{'kind':'x','text':'中文'})
        self.assertEqual(lines.pop(),{'kind':'y'})
        self.assertIsNone(lines.pop())

    def test_oversize_duplicate_and_nonfinite_reject(self):
        for raw in (b'{"kind":1,"kind":2}',b'{"x":NaN}',b'[]',b'x'*(ipc.MAX_FRAME+1)):
            with self.subTest(raw=raw[:20]),self.assertRaises(ipc.IPCError):
                ipc.decode(raw)
        with self.assertRaises(ipc.IPCError):
            ipc.encode({'x':'x'*ipc.MAX_FRAME})

    def test_unterminated_frame_and_backpressure_bound(self):
        lines=ipc.Lines()
        lines.feed(b'x'*ipc.MAX_FRAME)
        with self.assertRaises(ipc.IPCError):
            lines.pop()
        with self.assertRaises(ipc.IPCError):
            lines.feed(b'x'*(ipc.MAX_FRAME*2))


class TrialTests(unittest.TestCase):
    def trial(self,**kwargs):
        return ipc.Trial('wechat-primary','synthetic@chatroom','live',**kwargs)

    def command(self,trial,**changes):
        return {'kind':'send','requestId':'1'*32,'key':'2'*32,'sourceId':trial.source['id'],
                'permit':trial.permit,'text':'synthetic answer',**changes}

    def test_live_permit_is_exact_single_use(self):
        trial=self.trial()
        frame=trial.admit(message())
        self.assertEqual(frame['data']['id'],'synthetic-message')
        command=self.command(trial)
        self.assertEqual(trial.authorize(command),command)
        with self.assertRaises(ipc.IPCError):
            trial.authorize(command)

    def test_wrong_room_history_private_or_untriggered_reject(self):
        for changes in ({'room_id':'private-user'},{'account':'other'},{'origin':'history'},
                        {'direction':'outgoing'},{'body':'ordinary'},{'body':'/ai '},{'type':True},
                        {'timestamp':0},{'body':'/ai \ud800'}):
            with self.subTest(changes=changes),self.assertRaises(ipc.IPCError):
                self.trial().admit(message(**changes))

    def test_no_permit_rebinding_or_altered_fields(self):
        for changes in ({'permit':'0'*32},{'sourceId':'different'},{'requestId':'bad'},
                        {'key':'bad'},{'text':'x'*8193},{'text':'bad\x00'}, {'extra':1}):
            trial=self.trial()
            trial.admit(message())
            with self.subTest(changes=changes),self.assertRaises(ipc.IPCError):
                trial.authorize(self.command(trial,**changes))

    def test_observe_can_never_authorize_send(self):
        marker='AWIPC-'+'a'*16
        trial=ipc.Trial('wechat-primary','synthetic@chatroom','observe',marker)
        trial.admit(message(body=marker))
        with self.assertRaises(ipc.IPCError):
            trial.authorize(self.command(trial))

    def test_expiry_blocks_native_submission(self):
        now=[1000.]
        trial=self.trial(clock=lambda:now[0])
        trial.admit(message(timestamp=1000))
        now[0]=1181.
        with self.assertRaises(ipc.IPCError):
            trial.authorize(self.command(trial))


class NativeTests(unittest.TestCase):
    def source(self,replies):
        transport=Mock()
        transport.request.side_effect=[native.groups.Response(200,{'content-type':'application/json'},json.dumps(value).encode()) for value in replies]
        source=native.Source('wechat-primary','synthetic-token','synthetic@chatroom','test-group','live',transport=transport)
        return source,transport

    def catalog(self):
        return {'items':[{'id':'synthetic@chatroom','roomId':'synthetic@chatroom','displayName':'test-group'}],
                'nextCursor':None,'asOfEventCursor':'10','coverage':{'partial':False}}

    def event(self,**changes):
        return {'cursor':'11','kind':'message.committed','data':{'messageId':'mid','threadId':'synthetic@chatroom',
                'origin':'live','direction':'incoming',**changes}}

    def test_source_only_fetches_new_target_live_message(self):
        detail={'id':'mid','accountId':'wechat-primary','threadId':'synthetic@chatroom','senderId':'synthetic-user',
                'direction':'incoming','type':1,'body':'/ai hi'}
        source,transport=self.source([self.catalog(),{'items':[self.event()],'nextCursor':'11'},detail])
        source.start(time.monotonic()+30)
        value=source.poll(time.monotonic()+30)
        self.assertTrue(ipc.message_valid(value,'wechat-primary','synthetic@chatroom'))
        self.assertIsNone(source.poll(time.monotonic()+30))
        self.assertEqual([call.args[0] for call in transport.request.call_args_list],['GET','GET','GET'])

    def test_history_or_other_group_event_does_not_fetch_body(self):
        for change in ({'origin':'history'},{'threadId':'other@chatroom'},{'direction':'outgoing'}):
            source,transport=self.source([self.catalog(),{'items':[self.event(**change)],'nextCursor':'11'}])
            source.start(time.monotonic()+30)
            self.assertIsNone(source.poll(time.monotonic()+30))
            self.assertEqual(transport.request.call_count,2)

    def test_source_rejects_wrong_binding_and_cursor_regression(self):
        catalog=self.catalog()
        catalog['items'][0].update(id='other@chatroom',roomId='other@chatroom')
        source,_=self.source([catalog])
        with self.assertRaises(native.groups.GroupError):
            source.start(time.monotonic()+30)
        source,_=self.source([self.catalog(),{'items':[self.event()],'nextCursor':'9'}])
        source.start(time.monotonic()+30)
        with self.assertRaises(native.groups.GroupError):
            source.poll(time.monotonic()+30)

    def test_dynamic_reply_uses_exact_bound_text_and_key_once(self):
        identity=str(uuid.uuid4())
        value={'id':identity,'accountId':'wechat-primary','destination':'synthetic@chatroom','state':'accepted',
               'clientId':'12','serverId':'34'}
        transport=Mock()
        transport.request.return_value=native.sender.Response(202,{'content-type':'application/json'},json.dumps(value).encode())
        ticket=Mock()
        client=native.TextSendClient('wechat-primary','synthetic-token','synthetic@chatroom','模型回复','a'*32,
                                    on_intent=ticket,transport=transport)
        result=client.run(time.monotonic()+30,lambda _:None)
        self.assertTrue(result.server_confirmed)
        self.assertEqual(json.loads(transport.request.call_args.kwargs['body']),{'threadId':'synthetic@chatroom','text':'模型回复'})
        self.assertEqual(transport.request.call_args.kwargs['headers']['Idempotency-Key'],'a'*32)
        ticket.assert_called_once_with(identity)
        self.assertEqual(client.receipt['server_id'],'34')
        self.assertEqual(client.run(time.monotonic()+30,lambda _:None).code,'client_already_used')
        transport.request.assert_called_once()

    def test_dynamic_post_timeout_is_unknown_and_not_retried(self):
        transport=Mock()
        transport.request.side_effect=TimeoutError()
        client=native.TextSendClient('wechat-primary','synthetic-token','synthetic@chatroom','answer','a'*32,transport=transport)
        result=client.run(time.monotonic()+30,lambda _:None)
        self.assertTrue(result.submitted)
        self.assertEqual(result.code,'outcome_unknown')
        transport.request.assert_called_once()


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_serialized_message_to_send_and_receipt(self):
        frames=[]
        gateway=Gateway('wechat-primary','synthetic@chatroom',lambda value:frames.append(ipc.decode(ipc.encode(value))))
        trial=ipc.Trial('wechat-primary','synthetic@chatroom','live')
        gateway.feed(ipc.decode(ipc.encode(trial.admit(message()))))
        value=await gateway.next_message()
        gateway.bridge=Mock(pending={'native_id':value.id})
        task=asyncio.create_task(gateway.send_text(value.room_id,'synthetic answer','a'*32))
        await asyncio.sleep(0)
        command=trial.authorize(frames[0])
        receipt={'account':'wechat-primary','destination':value.room_id,'send_id':str(uuid.uuid4()),
                 'state':'accepted','client_id':'12','server_id':'34'}
        gateway.feed(ipc.decode(ipc.encode({'kind':'receipt','requestId':command['requestId'],'receipt':receipt})))
        result=await task
        self.assertIsInstance(result,Receipt)
        self.assertEqual(result.server_id,'34')
        with self.assertRaises(ipc.IPCError):
            await gateway.send_text(value.room_id,'second','b'*32)

    async def test_receipt_without_waiter_or_outside_group_rejects(self):
        gateway=Gateway('wechat-primary','synthetic@chatroom',lambda _:None)
        with self.assertRaises(ipc.IPCError):
            gateway.feed({'kind':'receipt','requestId':'a'*32,'receipt':{}})
        value={'kind':'message','data':message(room_id='other@chatroom'),'permit':'a'*32}
        with self.assertRaises(ipc.IPCError):
            gateway.feed(value)


if __name__=='__main__':
    unittest.main()
