"""Synthetic owned subprocess and broker turns; no Native/WS/model or real unit."""
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_runtime_lifecycle as lifecycle
from wechat_bridge.runtime.capture_job import CaptureJob
from wechat_bridge.runtime.reports import report


class CaptureRuntime(unittest.TestCase):
    fixture=lifecycle.RuntimeIntegration.fixture
    runtime=lifecycle.RuntimeIntegration.runtime
    peer=lifecycle.RuntimeIntegration.peer
    attach=lifecycle.RuntimeIntegration.attach
    handshake_fixture=lifecycle.RuntimeIntegration.handshake_fixture

    def setup_capture(self):
        c,s,d,db=self.handshake_fixture()
        from scripts import run_private_forwarder as core
        d.owner.CONTROL=d.owner.STATE;d.owner.CHECKPOINT_SECONDS=60
        d.owner.read_json=lambda _:dict(busy=False,ready=True)
        d.recovery_checkpoints=core.recovery_checkpoints
        d.Duplex=Mock();s.recovery_adapter=SimpleNamespace(profile=lambda:'a'*64)
        s.armed=True;s.recovery_refresh=True;s.host_wait['ready']=True
        s.recovery_seed={};s.pinned.host=SimpleNamespace(client_pid=123,client_ticks=456)
        for name in ('host','native'):
            peer=self.peer();peer.tick.side_effect=None;peer.tick.return_value=[];peer.output={'stdout':bytearray()}
            self.attach(c,name,peer)
        return c,s,d,db

    def paused(self,c,s):
        c.recovery.capture_if_safe()
        self.assertEqual(s.capture_phase,'quiescing')
        c.recovery.barrier('host',{'kind':'capture_paused','token':s.capture_token})
        c.recovery.capture_if_safe()
        c.recovery.barrier('native',{'kind':'capture_paused','token':s.capture_token})
        c.recovery.capture_if_safe()
        c.recovery.barrier('host',{'kind':'capture_fenced','token':s.capture_token})

    def test_formal_capture_waits_for_both_barriers_and_partial_frame(self):
        c,s,d,db=self.setup_capture()
        with patch('wechat_bridge.runtime.capture_job.CaptureJob') as job:
            c.recovery.capture_if_safe();c.recovery.capture_if_safe()
            job.assert_not_called()
            c.recovery.barrier('host',{'kind':'capture_paused','token':s.capture_token})
            c.recovery.capture_if_safe()
            c.recovery.barrier('native',{'kind':'capture_paused','token':s.capture_token})
            c.recovery.capture_if_safe()
            c.recovery.barrier('host',{'kind':'capture_fenced','token':s.capture_token})
            s.workers.peers['native'].output['stdout'].extend(b'{')
            c.recovery.capture_if_safe();job.assert_not_called()

    def test_active_host_defers_capture_before_native_pause(self):
        c,s,d,db=self.setup_capture()
        c.recovery.capture_if_safe()
        s.workers.peers['native'].send.assert_not_called()
        c.recovery.barrier('host',{'kind':'capture_deferred','token':s.capture_token})
        self.assertIsNone(s.capture_phase);self.assertFalse(s.capture_paused)
        self.assertIsNone(s.capture_job)
        s.workers.peers['native'].send.assert_not_called()

    def test_copy_done_releases_barriers_while_slow_proof_keeps_broker_live(self):
        c,s,d,db=self.setup_capture()
        with patch('wechat_bridge.runtime.capture_job.CaptureJob') as factory:
            factory.LIMIT_SECONDS=300
            job=factory.return_value
            job.poll.side_effect=[[{'phase':'copied','barrier':False}],[],[{'phase':'prepared'}],
                [{'phase':'done','floorSeq':1,'epoch':'b'*32,'protectedCount':1,'barrier':False}]]
            self.paused(c,s);c.recovery.capture_if_safe()
            self.assertEqual(s.capture_phase,'proving');self.assertFalse(s.capture_paused)
            # A real broker turn handles current IPC while proof has not returned.
            s.workers.peers['native'].tick.return_value=[{'kind':'report','stage':'sse_connection','connected':True}]
            self.assertFalse(c.poll());self.assertTrue(s.sse_connected)
            c.recovery.capture_if_safe();self.assertEqual(s.capture_phase,'proving')
            c.recovery.capture_if_safe();job.commit.assert_called_once()
            c.recovery.capture_if_safe()
            self.assertEqual(s.recovery_state,'ready');self.assertIsNone(s.capture_job)
            job.close.assert_called_once()

    def test_wrong_barrier_and_changed_generation_never_publish(self):
        c,s,d,db=self.setup_capture();self.paused(c,s)
        with self.assertRaises(Exception):
            c.recovery.barrier('host',{'kind':'capture_paused','token':'0'*32})
        s.workers.generation+=1
        with patch('wechat_bridge.runtime.capture_job.CaptureJob') as job:
            c.recovery.capture_if_safe();job.assert_not_called()
        self.assertEqual(s.recovery_state,'requires_review')

    def test_new_refresh_and_capacity_request_survive_an_inflight_capture(self):
        c,s,d,db=self.setup_capture()
        with patch('wechat_bridge.runtime.capture_job.CaptureJob') as factory:
            factory.LIMIT_SECONDS=300
            factory.return_value.poll.side_effect=[[{'phase':'copied','barrier':False}],
                [{'phase':'prepared'}],
                [{'phase':'done','floorSeq':1,'epoch':'b'*32,'protectedCount':1,'barrier':False}]]
            self.paused(c,s);c.recovery.capture_if_safe()
            s.recovery_refresh=s.recovery_capacity=True
            c.recovery.capture_if_safe();c.recovery.capture_if_safe()
            self.assertTrue(s.recovery_refresh and s.recovery_capacity)
            self.assertEqual(s.capture_cycles,0)
            c.recovery.capture_if_safe()
            self.assertEqual(s.capture_cycles,2)

    def test_rebuilt_host_receives_hold_before_activation(self):
        from wechat_bridge.runtime.session import activate_if_ready
        c,s,d,db=self.setup_capture()
        s.sse_connected=s.astrbot_connected=True
        s.send_reviews['unbound']=None
        activate_if_ready(d,s)
        sent=[call.args[0] for call in s.workers.peers['host'].send.call_args_list]
        self.assertEqual(sent,[{'kind':'send_hold','hold':True},{'kind':'activate'}])

    def test_cancel_owned_job_precedes_worker_stop(self):
        c,s,d,db=self.setup_capture();order=[]
        job=SimpleNamespace(close=lambda:order.append('capture'))
        s.capture_job=job
        d.tap.stop_unit.side_effect=lambda *a:order.append('worker')
        c.restart({'host':'worker_transport_failed'})
        self.assertEqual(order[0],'capture');self.assertIsNone(s.capture_job)

    def test_busy_before_copy_defers_and_keeps_barriers_until_owner_is_idle(self):
        c,s,d,db=self.setup_capture()
        d.owner.read_json=lambda _:dict(busy=True,ready=True)
        with patch('wechat_bridge.runtime.capture_job.CaptureJob') as factory:
            factory.LIMIT_SECONDS=300
            factory.return_value.poll.return_value=[{'phase':'failed','code':'busy'}]
            self.paused(c,s);c.recovery.capture_if_safe()
            self.assertEqual(s.capture_phase,'awaiting_owner');self.assertTrue(s.capture_paused)
            c.recovery.capture_if_safe();self.assertTrue(s.capture_paused)
            d.owner.read_json=lambda _:dict(busy=False,ready=True)
            c.recovery.capture_if_safe()
            self.assertFalse(s.capture_paused);self.assertIsNone(s.capture_phase)

    def test_action_failure_and_pending_text_do_not_retire_workers(self):
        c,s,d,db=self.setup_capture()
        report(d,s,'host',{'kind':'report','stage':'action_failed','retcode':1403})
        self.assertFalse(c.review());self.assertEqual(s.counters['rejected_actions'],1)
        key='d'*32
        report(d,s,'host',{'kind':'report','stage':'action_pending','native_key':key,'destination':'peer'})
        report(d,s,'host',{'kind':'report','stage':'action_pending','native_key':key,'destination':'peer'})
        self.assertFalse(c.review());self.assertEqual(len(s.send_reviews),1)
        part=hashlib.sha256((key+':0').encode()).hexdigest()[:32]
        record={'kind':'report','stage':'late_text_receipt','reconciled':True,'requestId':'e'*32,
            'part_key':part,'destination':'peer','send_id':'bound-send'}
        s.audit=dict(state='accepted',requestId='e'*32,key=part,sendId='other',
            destinationSha256=hashlib.sha256(b'peer').hexdigest())
        report(d,s,'host',record);self.assertEqual(len(s.send_reviews),1)
        s.audit['sendId']='bound-send'
        report(d,s,'host',record);self.assertFalse(s.send_reviews)
        s.workers.peers['host'].send.assert_called_with({'kind':'send_hold','hold':False})
        # The old failed WS response can finish writing after reconciliation.
        report(d,s,'host',{'kind':'report','stage':'action_pending','native_key':key,'destination':'peer'})
        self.assertFalse(s.send_reviews)

    def test_unbound_unknown_cannot_be_cleared_by_late_text(self):
        c,s,d,db=self.setup_capture()
        report(d,s,'host',{'kind':'report','stage':'action_rejected','retcode':1408})
        self.assertEqual(s.send_reviews,{'unbound':None})
        with self.assertRaises(Exception):
            report(d,s,'host',{'kind':'report','stage':'action_rejected','retcode':1500})

    def test_real_subprocess_waits_for_commit_while_parent_polls(self):
        c,s,d,db=self.setup_capture()
        from scripts import run_private_forwarder as core
        d.HERE=Path(core.HERE);d.UNIT='synthetic-forwarder.service'
        d.tap.require_unit_absent=Mock();d.tap.start_ticks=lambda _:1
        children=[];commands=[]
        code='''import json,sys
cfg=json.loads(sys.stdin.readline()); token=cfg['token']
def out(phase,**fields):
 print(json.dumps(dict(token=token,phase=phase,**fields)),flush=True)
out('copied',barrier=False);out('prepared')
permit=json.loads(sys.stdin.readline());assert permit==dict(kind='commit',token=token)
out('done',floorSeq=1,epoch='b'*32,protectedCount=1,barrier=False)
'''
        def spawn(args,config):
            commands.append(args)
            child=core.Duplex([sys.executable,'-I','-B','-c',code],config)
            children.append(child);return child
        d.Duplex=spawn
        def stop(*args,**kwargs):
            for child in children:
                if child.process.poll() is None: child.process.terminate()
                child.process.wait(timeout=3)
        d.tap.stop_unit.side_effect=stop
        job=CaptureJob(d,s,'a'*32);self.addCleanup(job.close)
        # systemd reverses ordering on stop. The parent must receive SIGTERM
        # before BindsTo stops its job so the parent can revoke/cancel it.
        self.assertIn('--property=Before='+d.UNIT,commands[0])
        self.assertNotIn('--property=After='+d.UNIT,commands[0])
        end=time.monotonic()+3
        while job.phase!='prepared' and time.monotonic()<end: job.poll()
        self.assertEqual(job.phase,'prepared');self.assertIsNone(children[0].process.poll())
        before=time.monotonic()
        for _ in range(5): self.assertEqual(job.poll(),[])
        self.assertLess(time.monotonic()-before,.2)
        job.commit()
        while not job.finished and time.monotonic()<end: job.poll()
        self.assertTrue(job.committed)
        job.close();job.close()  # cleanup may be reached again after retirement


class CaptureWorkerProcess(unittest.TestCase):
    """Run the actual capture entrypoint on private synthetic archives/SQLite."""
    def setUp(self):
        import test_recovery_checkpoint as fixtures
        self.case=fixtures.CheckpointCase()
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.case.add(9)
        from scripts import send_dispositions
        profile=self.case.adapter.profile()
        ledger=send_dispositions.Ledger(self.case.state_dir/'send-dispositions.json',profile)
        ledger.path.write_text(json.dumps(ledger.read()));ledger.path.chmod(0o600)
        status=self.case.state_dir/'status.json'
        status.write_text(json.dumps(dict(busy=False,ready=True)));status.chmod(0o600)
        self.profile=profile

    def child(self, *, stalled=False):
        from scripts import run_private_forwarder as core
        case=self.case
        root=Path(__file__).resolve().parents[1]
        # Replace only host/Native ownership and mailbox dependencies. The real
        # worker, snapshot proof, manifest publisher and commit callback run.
        code=r'''
import importlib.util,sys,json,os,time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
root,state,bridge,archive,keys,stall=map(Path,sys.argv[1:])
sys.path.insert(0,str(root))
from scripts import recovery_capture_worker as w,recovery_checkpoint as cp
from scripts import session_runtime as owner
from wechat_bridge import forward_wire as wire
cp=SimpleNamespace(**{**vars(cp),'BRIDGE_PUBLIC':bridge})
source=Path(w.__file__).read_text().replace('except Exception:\n        notify',
    'except Exception:\n        import traceback;traceback.print_exc()\n        notify')
exec(compile(source,w.__file__,'exec'),w.__dict__)
fd=os.open(keys,os.O_RDONLY|os.O_DIRECTORY)
p=SimpleNamespace(open=lambda:None,close=lambda:None,unchanged=lambda:True,
 host=SimpleNamespace(client_pid=123,client_ticks=456,data_fd=fd,client_uid=os.geteuid()))
@contextmanager
def checkpoint(_): yield archive
owner.STATE=owner.CONTROL=state
owner.checkpoint_copy=checkpoint
core=SimpleNamespace(owner=owner,wire=wire,recovery_checkpoints=cp,HERE=root/'scripts',
 groups=SimpleNamespace(PinnedNative=lambda:p),tap=SimpleNamespace(start_ticks=lambda _:1),
 delivery=SimpleNamespace(role_digest=lambda *a,**k:'a'*64))
def require(value,code):
 if not value: raise ValueError(code)
core.require=require
class Loader:
 def create_module(self,spec): return None
 def exec_module(self,module): module.core=core
util=importlib.util
w.importlib=SimpleNamespace(util=SimpleNamespace(
 spec_from_file_location=lambda name,path:util.spec_from_loader(name,Loader()),
 module_from_spec=util.module_from_spec))
w.os=SimpleNamespace(geteuid=lambda:0)
if stall.name in {'stall','pointer'}:
 method='_copy_archive' if stall.name=='stall' else '_write'
 original=getattr(cp.manifests.Store,method)
 def slow(self,*args):
  result=original(self,*args)
  if stall.name=='pointer' and args[1]!='.next.pointer': return result
  (state/'copy-waiting').touch()
  deadline=time.monotonic()+5
  while not (state/'release-copy').exists():
   if time.monotonic()>deadline: raise ValueError('fixture deadline')
   time.sleep(.01)
  return result
 setattr(cp.manifests.Store,method,slow)
raise SystemExit(w.run())
'''
        child=core.Duplex([sys.executable,'-I','-B','-c',code,str(root),str(case.state_dir),
            str(case.spool.path),str(case.archive),str(case.root/'keys'),
            stalled if isinstance(stalled,str) else 'stall' if stalled else 'normal'],
            dict(token='a'*32,checkpointName=case.generation,seed=case.adapter.seed,
                nativePid=123,nativeTicks=456,profile=self.profile,selfSender='self',
                parentPid=123,parentTicks=1,hostDigest='a'*64))
        self.addCleanup(child.close)
        child.fixture_records=[]
        return child

    def wait_phase(self,child,phase):
        end=time.monotonic()+5
        while time.monotonic()<end:
            child.fixture_records.extend(child.tick())
            while child.fixture_records:
                record=child.fixture_records.pop(0)
                self.assertNotEqual(record.get('phase'),'failed',bytes(child.output['stderr']).decode())
                if record.get('phase')==phase: return record
            if child.process.poll() is not None: self.fail('capture child ended before '+phase)
        self.fail('capture child stalled before '+phase)

    def test_real_worker_publishes_captured_boundary_after_live_ingress_resumes(self):
        child=self.child()
        self.wait_phase(child,'copied')
        self.case.add(11)
        self.wait_phase(child,'prepared')
        child.send({'kind':'commit','token':'a'*32})
        done=self.wait_phase(child,'done')
        self.assertFalse(done['barrier'])
        from scripts import recovery_manifest
        pair=recovery_manifest.Store(self.case.state_dir/'recovery').current()
        self.assertEqual(pair[0]['bridgeBoundary'],self.case.boundary(9))
        self.assertEqual(json.loads(self.case.spool.cursor(self.case.generation)),self.case.boundary(11))

    def test_control_eof_during_slow_copy_revokes_pointer_commit(self):
        self.revoked_pointer('stall')

    def test_control_eof_after_pointer_fsync_revokes_pointer_commit(self):
        self.revoked_pointer('pointer')

    def revoked_pointer(self,point):
        child=self.child(stalled=point)
        self.wait_phase(child,'copied');self.wait_phase(child,'prepared')
        child.send({'kind':'commit','token':'a'*32})
        end=time.monotonic()+5
        marker=self.case.state_dir/'copy-waiting'
        while not marker.exists() and time.monotonic()<end: child.tick()
        self.assertTrue(marker.exists())
        child.process.stdin.close()
        (self.case.state_dir/'release-copy').touch()
        records=[]
        while child.process.poll() is None and time.monotonic()<end: records.extend(child.tick())
        records.extend(child.tick())
        self.assertTrue(any(r.get('phase')=='failed' for r in records))
        self.assertFalse((self.case.state_dir/'recovery/current.json').exists())
