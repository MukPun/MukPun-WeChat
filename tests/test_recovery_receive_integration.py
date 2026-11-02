import asyncio
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading
import tempfile
from pathlib import Path
import zipfile
from unittest import mock

from scripts import recovery_checkpoint as recovery
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.forward_wire import WireError, recovery_protection
from wechat_bridge.image_input_files import ImageFiles
from wechat_bridge import image_input_wire, image_input
from test_recovery_checkpoint import CheckpointCase
from test_recovery_snapshot import event
from test_recovery_snapshot import fixture


class ReceiveTests(CheckpointCase):
    def test_repeated_rollback_merges_every_discarded_source_and_disables_old_anchor_shortcut(self):
        one=self.add(1); first=self.capture(); two=self.add(2)
        recovery.rebase_bridge(self.spool.path,first,self.target(first))
        # Cover an old worker's split commit too: receive cursor can already
        # equal target while a later durable source is still being discarded.
        three=self.add(3,commit_cursor=False)
        recovery.rebase_bridge(self.spool.path,first,self.target(first))
        self.assertEqual(recovery.replay_barrier(self.spool),(1,3,None))
        events={n:dict(cursor=str(n),kind='message.committed',createdAt=event(n)['createdAt'],
            data=dict(messageId='source-'+str(n-2),threadId='peer',direction='incoming',origin='live')) for n in (3,4)}
        with tempfile.TemporaryDirectory() as directory:
            self.archive=fixture(Path(directory),last=10)
            source=Path(directory)/'source10/control.sqlite'
            with sqlite3.connect(source) as db:
                for n,ev in events.items():
                    db.execute('UPDATE events SET kind=?,data_json=? WHERE sequence=?',(ev['kind'],json.dumps(ev['data']),n))
            with zipfile.ZipFile(self.archive,'w') as archive:
                for name in ('api-token','storage-key','cursor-key'):
                    archive.writestr(name,('synthetic-'+name).encode())
                archive.write(source,'accounts/wechat-primary/agent/control.sqlite')
            self.spool.cursor(self.generation,json.dumps(dict(cursor='4',anchor=recovery.snapshot.event_digest(events[4]))))
            pair=self.capture(); self.capture()
            # Original checkpoint two still exists and is ACKed through, but it
            # says nothing about source three lost during the second rollback.
            self.assertEqual(recovery.replay_barrier(self.spool),(1,3,None))
            seen=[]
            def first_range_only(source,event,deadline):
                seen.append(source)
                return {'source-1':one,'source-2':two}[source]
            with self.assertRaisesRegex(ValueError,'^recovery_reconcile$'):
                recovery.reconcile_replay(self.spool.path,pair,self.target(pair),first_range_only)
        self.assertEqual(seen,['source-1','source-2'])
        self.assertEqual(recovery.replay_barrier(self.spool),(1,3,None))
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],1)
        self.assertFalse(self.spool.admit(three['id'],three))

    def test_chunked_staged_image_capacity_pauses_after_commit_and_retries_without_restaging(self):
        self.add(1); self.capture()
        with self.spool.db:
            self.spool.db.executemany('INSERT INTO inbox(source,digest,payload,state,created) VALUES(?,?,NULL,?,1)',
                [('completed-'+str(n),'digest','forwarded') for n in range(2,4097)])
        files=self.root/'files'; files.mkdir(mode=0o700)
        storage=ImageFiles(files)
        from test_hd_input_wire import event as image_event
        frame=image_event(300000)
        frame.update(cursor='9',anchor=self.boundary(9)['anchor'])
        frame['data']['id']='pending-image'
        frames=[]
        gateway=Gateway('wechat-primary','peer',self.spool,self.generation,frames.append,image_input=True,image_files=storage)
        producer=image_input_wire.Producer(frame,'a'*32)
        relay=image_input_wire.Relay(clock=lambda:0)
        next_frames=[]
        class Peer:
            def __init__(self,send): self.send=send
        peers={'host':Peer(gateway.feed),'native':Peer(lambda ack:next_frames.append(producer.ack(ack)))}
        with mock.patch.object(storage,'stage',wraps=storage.stage) as stage:
            relay.handle('native',producer.begin(),peers)
            self.assertTrue(relay.transferring)
            relay.forward_begin(peers)
            while relay.transferring:
                relay.handle('host',frames.pop(0),peers)
                relay.handle('native',next_frames.pop(0),peers)
            self.assertTrue(relay.active)
            self.assertEqual(frames,[{'kind':'report','stage':'recovery','code':'recovery_checkpoint_required'}])
            staged=gateway.recovery_pending['data']['image']
            self.assertIn('file',staged); self.assertNotIn('base64',staged)
            self.assertEqual(stage.call_count,1)
            # This is the root safe-point predicate. Committed ACK waiting can
            # capture or remain paused on missing proof without transfer timeout.
            self.assertFalse(relay.transferring)
            with mock.patch.object(self.owner,'checkpoint_copy',side_effect=ValueError('unproved')):
                with self.assertRaises(ValueError): self.capture()
            relay.clock=lambda:181
            relay.check_deadline()
            self.assertTrue(relay.active)
            self.assertEqual(gateway.recovery_pending['data']['image'],staged)
            # A nonadvancing valid floor also retains the exact staged input.
            coverage=self.spool.recovery_coverage()
            gateway.feed(dict(kind='recovery_protection',floorSeq=coverage['floorSeq'],epoch=coverage['epoch']))
            self.assertEqual(stage.call_count,1)
            self.capture(); self.capture()
            coverage=self.spool.recovery_coverage()
            gateway.feed(dict(kind='recovery_protection',floorSeq=coverage['floorSeq'],epoch=coverage['epoch']))
            self.assertEqual(stage.call_count,1)
        self.assertIsNone(gateway.recovery_pending)
        self.assertEqual(frames[-1],{'kind':'ack','cursor':'9'})
        relay.complete('9')
        self.assertFalse(relay.active)
        self.assertEqual(self.spool.next_pending()[1]['image'],staged)
        # Public pipe input still cannot inject even a valid existing file URI.
        with self.assertRaises(image_input.ImageError):
            gateway.feed({**frame,'data':{**frame['data'],'image':staged}})

    def api(self, mutate=False):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                n = int(self.path.rsplit('/',1)[1])
                value = event(n)
                if mutate: value['data']={'id':'reused-event-number'}
                raw = json.dumps(value).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def log_message(self,*_): pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def close():
            server.shutdown(); thread.join(2); server.server_close()
        self.addCleanup(close)
        def get_event(cursor):
            connection = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            try:
                connection.request('GET','/events/'+str(cursor))
                return json.loads(connection.getresponse().read(65537))
            finally: connection.close()
        return get_event,requests

    def gateway(self,frames):
        return Gateway('wechat-primary','peer',self.spool,self.generation,frames.append)

    def test_latest_bridge_survives_rollback_and_same_id_never_leaves_gateway_twice(self):
        for n in range(1,9): self.add(n)
        nine = self.add(9,commit_cursor=False)
        pair = self.capture()
        ten = self.add(10,terminal=False)
        self.spool.publishing(ten['id']); self.spool.handoff(ten['id'],False)
        identity = self.spool.identity('user','newest-user')[0]
        self.spool.reserve('latest-action','digest','native-key')
        before_rows = list(self.spool.db.execute('SELECT * FROM inbox'))
        get_event,requests = self.api()
        target = self.target(pair); target['get_event']=get_event
        result = recovery.rebase_bridge(self.spool.path,pair,target)
        self.assertEqual(result['boundary'],self.boundary(8))
        self.assertEqual(requests,['/events/8'])
        self.assertEqual(list(self.spool.db.execute('SELECT * FROM inbox')),before_rows)
        self.assertEqual(self.spool.raw_identity('user',identity),'newest-user')
        self.assertEqual(self.spool.submission('latest-action','digest')['message'],'outcome_unknown')
        # Capturing while the receive checkpoint is rebased must preserve the
        # original replay floor even though newest highSeq is already ten.
        self.assertEqual(self.capture()[0]['dedup']['floorSeq'],9)
        saved = sqlite3.connect(self.state_dir/'receive-before.sqlite')
        self.addCleanup(saved.close)
        self.assertEqual(json.loads(saved.execute('SELECT value FROM checkpoints WHERE name=?',(self.generation,)).fetchone()[0])['cursor'],'10')
        frames=[]; gateway=self.gateway(frames)
        for n,data in ((9,nine),(10,ten)):
            gateway.feed(dict(kind='event',cursor=str(n),anchor=self.boundary(n)['anchor'],data=data))
        self.assertEqual([v for v in frames if v['kind']=='ack'],[{'kind':'ack','cursor':'9'},{'kind':'ack','cursor':'10'}])
        self.assertIsNone(self.spool.next_pending())
        self.assertEqual(self.spool.counts(),{'forwarded':9,'uncertain':1})
        self.capture(); self.capture()
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],10)
        self.assertIsNone(recovery.replay_barrier(self.spool))

    def test_reused_historical_checkpoint_keeps_rebase_barrier_after_new_capture(self):
        self.add(1); pair=self.capture(); self.add(2)
        recovery.rebase_bridge(self.spool.path,pair,self.target(pair))
        # The restored stream reuses event number two with different content.
        reused={**event(2),'data':{'id':'different'}}
        self.spool.cursor(self.generation,json.dumps({'cursor':'2','anchor':recovery.snapshot.event_digest(reused)}))
        from pathlib import Path
        import tempfile
        from test_recovery_snapshot import fixture
        with tempfile.TemporaryDirectory() as directory:
            self.archive=fixture(Path(directory),last=2,event_json='{"id":"different"}')
            # Only the actual old canonical event can lift the barrier. Also
            # replace first event in this synthetic candidate with its original
            # canonical data, so current manifest verification remains valid.
            import zipfile
            source=Path(directory)/'source2/control.sqlite'
            with sqlite3.connect(source) as db:
                db.execute('UPDATE events SET data_json=? WHERE sequence=1',(json.dumps(event(1)['data']),))
            with zipfile.ZipFile(self.archive,'w') as archive:
                for name in ('api-token','storage-key','cursor-key'):
                    archive.writestr(name,('synthetic-'+name).encode())
                archive.write(source,'accounts/wechat-primary/agent/control.sqlite')
            captured=self.capture()
        self.assertEqual(captured[0]['dedup']['floorSeq'],1)
        self.assertIsNotNone(recovery.replay_barrier(self.spool))

    def test_reconcile_exact_sources_when_old_nonmessage_checkpoint_never_returns(self):
        one=self.add(1); original=self.capture(); two=self.add(2)
        recovery.rebase_bridge(self.spool.path,original,self.target(original))
        replay_events={n:dict(cursor=str(n),kind='message.committed',createdAt=event(n)['createdAt'],
            data=dict(messageId='source-'+str(n-2),threadId='peer',direction='incoming',origin='live')) for n in (3,4)}
        with tempfile.TemporaryDirectory() as directory:
            self.archive=fixture(Path(directory),last=10)
            source=Path(directory)/'source10/control.sqlite'
            with sqlite3.connect(source) as db:
                db.execute('UPDATE events SET data_json=? WHERE sequence=2',('{"old":"auth-checkpoint-reused"}',))
                for n,ev in replay_events.items():
                    db.execute('UPDATE events SET kind=?,data_json=? WHERE sequence=?',(ev['kind'],json.dumps(ev['data']),n))
            with zipfile.ZipFile(self.archive,'w') as archive:
                for name in ('api-token','storage-key','cursor-key'):
                    archive.writestr(name,('synthetic-'+name).encode())
                archive.write(source,'accounts/wechat-primary/agent/control.sqlite')
            self.spool.cursor(self.generation,json.dumps(dict(cursor='4',anchor=recovery.snapshot.event_digest(replay_events[4]))))
            pair=self.capture()
            self.assertIsNotNone(recovery.replay_barrier(self.spool))
            data={'source-1':one,'source-2':two}
            def source_get(source,event,deadline):
                self.assertFalse(self.spool.db.in_transaction)
                self.assertEqual(event['data']['messageId'],source)
                return {**data[source],'sender_nickname':'changed display name'}
            for bad in (lambda *_:True,lambda source,*_:{**data[source],'body':'changed content'}):
                with self.assertRaisesRegex(ValueError,'^recovery_reconcile$'):
                    recovery.reconcile_replay(self.spool.path,pair,self.target(pair),bad)
                self.assertIsNotNone(recovery.replay_barrier(self.spool))
            # The admission normalization itself is unchanged; only nickname
            # and card are irrelevant. No ID-only proof can release the barrier.
            self.assertEqual(recovery.reconcile_replay(self.spool.path,pair,self.target(pair),source_get),{'verified':2})
            self.assertIsNone(recovery.replay_barrier(self.spool))
            self.capture(); self.capture()
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],2)

    def test_reused_event_number_or_boolean_witness_rejects_without_backup_or_mutation(self):
        self.add(1); pair=self.capture(); self.add(2)
        original=self.spool.path.read_bytes()
        bad,requests=self.api(mutate=True)
        for get_event in (bad,lambda _:True):
            target=self.target(pair); target['get_event']=get_event
            with self.assertRaisesRegex(ValueError,'^recovery_rebase$'):
                recovery.rebase_bridge(self.spool.path,pair,target)
            self.assertEqual(self.spool.path.read_bytes(),original)
            self.assertFalse((self.state_dir/'receive-before.sqlite').exists())
        self.assertEqual(requests,['/events/1'])

    def test_backup_failure_preserves_latest_checkpoint_and_unknown_submission(self):
        self.add(1); pair=self.capture(); self.add(2)
        self.spool.reserve('action','digest','key')
        original=self.spool.path.read_bytes()
        with mock.patch.object(recovery,'backup',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(ValueError): recovery.rebase_bridge(self.spool.path,pair,self.target(pair))
        self.assertEqual(self.spool.path.read_bytes(),original)

    def test_capacity_holds_exact_input_before_ack_until_two_captures_advance_floor(self):
        self.add(1); self.capture()
        # Same production SQLite schema/metadata, bulk fixture avoids thousands
        # of irrelevant durable transition fsyncs. IDs remain synthetic.
        with self.spool.db:
            self.spool.db.executemany('INSERT INTO inbox(source,digest,payload,state,created) VALUES(?,?,NULL,?,1)',
                [('completed-'+str(n),'digest','forwarded') for n in range(2,4097)])
        frames=[]; gateway=self.gateway(frames)
        data=dict(account='wechat-primary',id='pending-new',body='same',sender_id='peer',room_id='peer',
                  direction='incoming',origin='live',type=1,timestamp=1)
        frame=dict(kind='event',cursor='9',anchor=self.boundary(9)['anchor'],data=data)
        gateway.feed(frame)
        self.assertEqual(frames,[{'kind':'report','stage':'recovery','code':'recovery_checkpoint_required'}])
        self.assertEqual(json.loads(self.spool.cursor(self.generation))['cursor'],'1')
        self.assertEqual(gateway.recovery_pending,frame)
        first=self.capture()
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],1)
        second=self.capture()
        self.assertNotEqual(first[0]['createdAt'],second[0]['createdAt'])
        coverage=self.spool.recovery_coverage()
        self.assertEqual(coverage['floorSeq'],4096)
        gateway.feed({'kind':'recovery_protection','floorSeq':coverage['floorSeq'],'epoch':coverage['epoch']})
        self.assertEqual(frames[-1],{'kind':'ack','cursor':'9'})
        self.assertIsNone(gateway.recovery_pending)
        self.assertEqual(self.spool.next_pending()[0],'pending-new')
        gateway.feed(frame)
        self.assertEqual(self.spool.db.execute('SELECT count(*) FROM inbox WHERE source=?',('pending-new',)).fetchone()[0],1)

    def test_floor_frames_are_fixed_small_and_raw_publishing_requires_startup_quarantine(self):
        for value in ({'floorSeq':True,'epoch':'a'*32},{'floorSeq':1,'epoch':'bad'},
                      {'floorSeq':1,'epoch':'a'*32,'path':'anything'}):
            with self.assertRaises(WireError): recovery_protection(value)
        self.add(1); pair=self.capture()
        self.add(2,terminal=False); self.spool.publishing('source-2')
        with self.assertRaises(ValueError): recovery.rebase_bridge(self.spool.path,pair,self.target(pair))
        self.assertEqual(self.spool.counts(),{'forwarded':1,'publishing':1})
