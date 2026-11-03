"""Isolated startup lifecycle tests; never use real units or saved profiles."""
from contextlib import contextmanager
import importlib.util
import json
import tempfile
from pathlib import Path
import unittest


class CoordinatorContract(unittest.TestCase):
    def test_production_coordinator_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.recovery_coordinator'),
                             'production resumable startup coordinator is missing')


class EngineTests(unittest.TestCase):
    def setUp(self):
        from scripts import recovery_coordinator as recovery
        self.r = recovery
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def ops(self):
        r = self.r
        class Ops:
            state_dir = self.root
            now = 1000
            sleep = staticmethod(lambda seconds: None)
            owner = None
            launches = resumes = 0
            failures = 0
            crash = None
            events = []
            journal = r.Journal(self.root)
            def clock(s): return s.now
            def publish(s, state):
                s.events.append(state['phase'])
                if s.crash == state['phase']:
                    s.crash = None
                    raise KeyboardInterrupt()
            def validate(s, state):
                return dict(manifestSha256='a'*64, profileSha256='b'*64, ledgerRevision=1)
            def inspect_owner(s, state): return s.owner
            def prepare(s, state): s.ticket = dict(state)
            def launch(s, state):
                s.launches += 1
                s.owner = dict(pid=22, ticks=33, attempt=state['attempt'])
            def online(s, state):
                if s.failures:
                    s.failures -= 1
                    raise r.Failure('awaiting_network')
            def rebind(s, state): pass
            def receive_ready(s, state): pass
            def close(s): pass
        return Ops()

    def test_restart_after_each_durable_stage_reuses_live_attempt(self):
        for phase in ('validate','restore','online','rebind','receive_ready','ready'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                ops = self.ops(); ops.state_dir = Path(directory); ops.journal = self.r.Journal(ops.state_dir)
                ops.crash = phase
                with self.assertRaises(KeyboardInterrupt): self.r.recover(ops)
                result = self.r.recover(ops)
                self.assertEqual(result['status'],'ready')
                self.assertEqual(ops.launches,1)

    def test_timeout_does_not_authorize_second_launch(self):
        ops = self.ops()
        launch = ops.launch
        def timeout(state):
            launch(state)
            raise self.r.Failure('awaiting_network')
        ops.launch = timeout
        self.assertEqual(self.r.recover(ops)['status'],'ready')
        self.assertEqual(ops.launches,1)

    def test_network_budget_cooldown_and_later_safe_check(self):
        ops = self.ops(); ops.failures = 8; delays=[]; ops.sleep=delays.append
        result = self.r.recover(ops)
        self.assertEqual(result['status'],'awaiting_network')
        self.assertEqual(delays,[1,2,4,8,16,30])
        self.assertEqual(result['retryAt'],1300)
        self.r.recover(ops)
        self.assertEqual(ops.failures,2)
        ops.now=1301; ops.failures=0
        self.assertEqual(self.r.recover(ops)['status'],'ready')
        self.assertEqual(ops.launches,1)

    def test_two_coordinators_are_serialized(self):
        ops = self.ops()
        with ops.journal.lock():
            result = self.r.recover(ops)
        self.assertEqual(result['status'],'recovering')
        self.assertEqual(ops.launches,0)

    def test_existing_online_owner_ignores_stale_status(self):
        ops=self.ops(); ops.owner=dict(pid=22,ticks=33,attempt=None)
        self.assertEqual(self.r.recover(ops)['status'],'ready')
        self.assertEqual(ops.launches,0)

    def test_starting_live_owner_observation_timeout_remains_resumable(self):
        ops=self.ops(); calls=[]
        def inspect(state):
            calls.append(1)
            if len(calls)==1: raise self.r.Failure('awaiting_network')
            return dict(pid=22,ticks=33,attempt=None)
        ops.inspect_owner=inspect
        self.assertEqual(self.r.recover(ops)['status'],'awaiting_network')
        ops.now=1301
        self.assertEqual(self.r.recover(ops)['status'],'ready')
        self.assertEqual(ops.launches,0)

    def test_transient_first_observation_keeps_true_cold_restore(self):
        ops=self.ops(); rebinds=[]
        ops.rebind=lambda state: rebinds.append(1)
        def hiccup(state): raise self.r.Failure('awaiting_network')
        ops.inspect_owner=hiccup
        self.assertEqual(self.r.recover(ops)['status'],'awaiting_network')
        self.assertIsNone(ops.journal.read())
        ops.inspect_owner=lambda state: ops.owner
        self.assertEqual(self.r.recover(ops)['status'],'ready')
        self.assertEqual((ops.launches,len(rebinds)),(1,1))
        self.assertIs(ops.journal.read()['cold'],True)

    def test_ready_state_is_not_degraded_by_transient_observation(self):
        ops=self.ops()
        self.assertEqual(self.r.recover(ops)['status'],'ready')
        def hiccup(state): raise self.r.Failure('awaiting_network')
        ops.inspect_owner=hiccup
        result=self.r.recover(ops)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(ops.journal.read()['status'],'ready')

    def test_owner_death_after_warm_start_still_rebases_the_restore(self):
        # Regression: `cold` used to be derived only on a fresh attempt, so a
        # warm journal plus a dead owner reused cold=False, staged an older
        # snapshot and skipped the rebase barrier entirely.
        ops=self.ops(); rebinds=[]
        ops.rebind=lambda state: rebinds.append(1)
        # A warm start reuses the running owner: nothing is launched or rebased.
        ops.owner=dict(pid=22,ticks=33,attempt=None)
        self.assertEqual(self.r.recover(ops)['status'],'ready')
        self.assertEqual((ops.launches,len(rebinds)),(0,0))
        self.assertIs(ops.journal.read()['cold'],False)
        # The coordinator was down; while it was down the owner died too.
        state=ops.journal.read()
        state.update(status='recovering',phase='online',launched=False,code='recovering')
        ops.journal.write(state)
        ops.owner=None
        result=self.r.recover(ops)
        self.assertEqual(result['status'],'ready')
        self.assertEqual((ops.launches,len(rebinds)),(1,1))
        self.assertIs(ops.journal.read()['cold'],True)

    def test_permanent_failure_is_durable_and_does_not_retry_start(self):
        ops=self.ops()
        def rejected(state): raise self.r.Failure('recovery_admission')
        ops.validate=rejected
        self.assertEqual(self.r.recover(ops)['status'],'requires_review')
        self.assertEqual(self.r.recover(ops)['status'],'requires_review')
        self.assertEqual(ops.launches,0)

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import os
import sqlite3
import threading
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse, parse_qs
import zipfile
from test_recovery_checkpoint import CheckpointCase
from test_recovery_snapshot import SEED, event
from scripts import recovery_checkpoint as cp
from scripts import recovery_coordinator as recovery
from scripts import session_runtime, recovery_dispositions, native_health_probe
from wechat_bridge import loop_guard, group_trigger


class ProductionTests(CheckpointCase):
    @staticmethod
    def source_event(n):
        return dict(cursor=str(n),kind='message.committed',createdAt=event(n)['createdAt'],
            data=dict(messageId='source-'+str(n),threadId='peer',direction='incoming',origin='live'))

    @staticmethod
    def boundary(n):
        return dict(cursor=str(n),anchor=cp.snapshot.event_digest(ProductionTests.source_event(n)))

    def setUp(self):
        super().setUp()
        self.generation='native:123:456'
        self.adapter.checkpoint_name=self.generation
        self.control=self.root/'volatile'; self.control.mkdir(mode=0o700)
        self.native_row=('held','held-key','hash','send','needs_review','submitting',1,0,0,None,'{}',b'encrypted','time','time')
        source=self.root/'source10/control.sqlite'
        with sqlite3.connect(source) as db:
            db.execute('INSERT INTO media_jobs VALUES('+','.join('?'*14)+')',self.native_row)
            for n in range(1,11):
                ev=self.source_event(n)
                db.execute('UPDATE events SET kind=?,data_json=? WHERE sequence=?',(ev['kind'],json.dumps(ev['data']),n))
        with zipfile.ZipFile(self.archive,'w') as archive:
            for name in ('api-token','storage-key','cursor-key'):
                archive.writestr(name,('synthetic-'+name).encode())
            archive.write(source,'accounts/wechat-primary/agent/control.sqlite')
        self.archive.chmod(0o600)
        self.profile=self.adapter.profile()
        self.audit={'state':'unknown','code':'image_needs_review','jobId':'held'}
        evidence=recovery_dispositions._evidence(self.audit)
        entry=cp.dispositions.entry('wechat-primary','media',self.native_row,'reviewed_not_delivered','operator_review',evidence)
        cp.dispositions.Ledger(self.state_dir/'send-dispositions.json',self.profile).record(entry)
        self.audit['recoveryDisposition']=dict(classification='reviewed_not_delivered',profileSha256=self.profile,
            evidenceSha256=evidence,matched=True,recordSha256=entry['recordSha256'],admissionRequired=True,
            snapshotSha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())
        session_runtime.write_json(self.state_dir/'forward-image-send.json',self.audit)
        session_runtime.write_json(self.state_dir/'forward-image-hold.json',{'nativeClientPid':999999,'nativeClientTicks':1})
        self.original_audit=(self.state_dir/'forward-image-send.json').read_bytes()
        self.add(1)
        self.pair=self.capture()
        self.add(2,terminal=False)
        self.spool.publishing('source-2'); self.spool.handoff('source-2',False)
        self.add(3)
        self.calls=[]; self.accounts=[]; self.resume_calls=[]
        self.responses={}
        case=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                case.calls.append((self.command,self.path,self.headers.get('Authorization')))
                prefix='/v1/accounts/wechat-primary'
                parsed=urlparse(self.path)
                if parsed.path==prefix:
                    value=case.accounts.pop(0) if case.accounts else dict(id='wechat-primary',authenticated=True,connection='online')
                elif parsed.path==prefix+'/sends/'+SEED['sendId']:
                    value=dict(id=SEED['sendId'],accountId='wechat-primary',state='accepted',destination='peer',localMessageId='accepted-local')
                elif parsed.path==prefix+'/messages/accepted-local':
                    value=dict(id='accepted-local',accountId='wechat-primary',threadId='peer',direction='outgoing',type=1,senderId='self',body='accepted text')
                elif parsed.path==prefix+'/events':
                    n=int(parse_qs(parsed.query)['after'][0])+1
                    value=dict(items=[case.source_event(n)])
                elif parsed.path.startswith(prefix+'/messages/source-'):
                    source=parsed.path.rsplit('/',1)[1]
                    value=case.responses.get(source,dict(id=source,accountId='wechat-primary',threadId='peer',direction='incoming',type=1,
                        senderId='peer',body='same',createdAt='1970-01-01T00:00:01+00:00'))
                else:
                    self.send_error(404); return
                raw=json.dumps(value).encode()
                self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server_thread=threading.Thread(target=self.http.serve_forever,daemon=True); self.server_thread.start()
        self.addCleanup(self.http.server_close); self.addCleanup(self.http.shutdown)
        self.owner=SimpleNamespace(STATE=self.state_dir,CONTROL=self.control,LONG_RUNTIME_SECONDS=604800,
            read_json=session_runtime.read_json,read_private=session_runtime.read_private,write_json=session_runtime.write_json,
            private_directory=session_runtime.private_directory,client=lambda action:self.resume_calls.append(action))
        self.core=SimpleNamespace(owner=self.owner,recovery_dispositions=recovery_dispositions,loops=loop_guard,
            trigger=group_trigger,status=lambda value:None,health=SimpleNamespace(contact=SimpleNamespace(
                ContactReceiveClient=lambda account,token:native_health_probe.contact.ContactReceiveClient(account,token,port=self.http.server_port))))
        self.native=self.pinned
        self.native.netfd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        self.addCleanup(os.close,self.native.netfd)
        self.native.token=lambda:'synthetic-api-token'
        self.native.close=lambda:None
        self.live=None; self.launches=0
        class Ops(recovery.Production):
            def no_workers(s,**kwargs): pass
            def inspect_owner(s,state): return case.live
            def launch(s,state):
                ticket=recovery.consume_ticket(case.owner)
                case.launches+=1
                case.live=dict(pid=123,ticks=456,attempt=ticket['attempt'])
            def _pin(s):
                s.pinned=case.native
                s.generation='profile:'+hashlib.sha256(case.native.token().encode()).hexdigest()
        self.ops=Ops(self.core,bridge_path=self.spool.path)
        self.ops.sleep=lambda _:None
        self.ops.clock=lambda:1000
        self.namespace=mock.patch.object(recovery.os,'setns',create=True)
        self.namespace.start(); self.addCleanup(self.namespace.stop)
        self.clone=mock.patch.object(recovery.os,'CLONE_NEWNET',0,create=True)
        self.clone.start(); self.addCleanup(self.clone.stop)

    def finish(self):
        result=recovery.recover(self.ops)
        self.assertEqual(result['phase'],'receive_ready',result)
        self.assertEqual(result['status'],'recovering',result)
        recovery.mark_ready(self.core)
        self.assertEqual(self.ops.journal.read()['status'],'ready')
        return result

    def test_cold_empty_run_stale_hold_exact_quarantine_behind_cursor_recovers(self):
        self.assertEqual(list(self.control.iterdir()),[])
        before=self.spool.db.execute('SELECT seq,source,digest,payload,state FROM inbox').fetchall()
        self.finish()
        self.assertEqual(self.launches,1)
        self.assertEqual(self.resume_calls,[])
        self.assertEqual(self.spool.db.execute('SELECT seq,source,digest,payload,state FROM inbox').fetchall(),before)
        self.assertEqual(json.loads(self.spool.cursor(self.ops.generation)),self.boundary(1))
        self.assertEqual(cp.replay_barrier(self.spool),(1,3,None))
        self.assertFalse(self.spool.admit('source-3',self.add_data(3)))
        self.assertEqual((self.state_dir/'forward-image-send.json').read_bytes(),self.original_audit)
        with cp.snapshot.copied_database(self.state_dir/'checkpoints/private/current.zip') as (db,_):
            self.assertEqual(db.execute('SELECT * FROM media_jobs').fetchone(),self.native_row)
        self.assertTrue(recovery_dispositions.audit_cleared(self.core,self.audit))
        self.assertTrue(all(method=='GET' and auth=='Bearer synthetic-api-token' for method,_,auth in self.calls))
        from scripts import run_private_forwarder as broker
        with mock.patch.object(broker,'owner',self.owner), mock.patch.object(broker,'audit_cleared',side_effect=lambda audit:recovery_dispositions.audit_cleared(self.core,audit)):
            self.owner.owner_active=lambda:True
            path,hold,_=broker.image_send_preflight({'imageOutputEnabled':True})
            self.assertEqual(path.name,'forward-image-live-send.json'); self.assertIsNone(hold)

    def add(self,n,*,commit_cursor=True,terminal=True):
        # Match actual forward_native_worker admission field order. The existing
        # digest is deliberately exact, including serialization order.
        data=self.add_data(n); source=data['id']
        if commit_cursor:self.spool.admit_checkpoint(source,data,self.generation,json.dumps(self.boundary(n)))
        else:self.spool.admit(source,data)
        if terminal:
            self.spool.publishing(source);self.spool.handoff(source,True)
        return data

    @staticmethod
    def add_data(n):
        return dict(account='wechat-primary',id='source-'+str(n),room_id='peer',sender_id='peer',body='same',
                    timestamp=1,origin='live',direction='incoming',type=1)

    def test_production_actual_get_reconciles_all_fresh_sources_and_retains_unprovable_image(self):
        self.finish()
        self.spool.cursor(self.ops.generation,json.dumps(self.boundary(3)))
        self.adapter.checkpoint_name=self.ops.generation
        pair=self.capture()
        self.responses['source-2']=dict(id='source-2',accountId='wechat-primary',threadId='peer',direction='incoming',type=3,
            senderId='peer',body='',createdAt='1970-01-01T00:00:01+00:00')
        with self.assertRaisesRegex(ValueError,'recovery_reconcile'):
            self.ops.reconcile(pair,self.native,self.ops.generation,SEED,'self')
        self.assertIsNotNone(cp.replay_barrier(self.spool))
        self.responses.clear()
        self.assertEqual(self.ops.reconcile(pair,self.native,self.ops.generation,SEED,'self'),{'verified':3})
        self.assertIsNone(cp.replay_barrier(self.spool))
        self.assertEqual(self.spool.counts().get('uncertain'),1)

    def test_no_spurious_resume_online_and_transient_approval_resolves(self):
        self.accounts=[dict(id='wechat-primary',authenticated=False,connection='disconnected'),
                       dict(id='wechat-primary',authenticated=False,connection='reconnecting')]
        self.finish()
        self.assertEqual(self.resume_calls,['resume'])
        self.finish()
        self.assertEqual(self.resume_calls,['resume'])
        self.assertEqual(self.launches,1)

    def test_approval_exhaustion_keeps_single_attempt_and_durable_user_action(self):
        self.accounts=[dict(id='wechat-primary',authenticated=False,connection='disconnected')]*6
        result=recovery.recover(self.ops)
        self.assertEqual(result['status'],'awaiting_user_action')
        self.assertEqual(result['retryAt'],1300)
        self.assertEqual(self.resume_calls,['resume'])
        self.assertEqual(self.launches,1)
        self.assertEqual(len(list((self.state_dir/'recovery-attempts').iterdir())),1)
        self.ops.clock=lambda:1301
        self.finish()
        self.assertEqual(self.resume_calls,['resume'])

    def test_consumed_ticket_cannot_be_replayed_even_if_mutable_ticket_rewound(self):
        self.finish()
        path=self.state_dir/'recovery-ticket.json'
        value=session_runtime.read_json(path); value['phase']='prepared'
        self.ops.journal.write(value,path)
        with self.assertRaises(recovery.Failure): recovery.consume_ticket(self.owner)
        self.assertEqual(len(list((self.state_dir/'recovery-attempts').iterdir())),1)

    def test_mismatched_api_anchor_is_permanent_and_never_overwrites_latest_spool(self):
        old=self.ops.get_event
        self.ops.get_event=lambda cursor:{**old(cursor),'kind':'different'}
        before=self.spool.cursor(self.generation)
        result=recovery.recover(self.ops)
        self.assertEqual(result['status'],'requires_review')
        self.assertEqual(self.spool.cursor(self.generation),before)
        self.assertEqual(self.launches,1)
        self.assertEqual(recovery.recover(self.ops)['status'],'requires_review')
        self.assertEqual(self.launches,1)

    def test_unanchored_stable_name_never_falls_back_to_native_maximum(self):
        stable='profile:'+hashlib.sha256(b'synthetic-api-token').hexdigest()
        self.spool.db.execute('DELETE FROM checkpoints'); self.spool.db.commit()
        self.spool.cursor(stable,'3')
        self.assertEqual(recovery.recover(self.ops)['status'],'requires_review')
        self.assertEqual(self.spool.cursor(stable),'3')
        self.assertEqual(self.launches,0)

    def test_latest_ledger_revision_is_explicitly_bound_and_additional_pending_refuses(self):
        entry=cp.dispositions.entry('wechat-primary','text',('terminal','k','h','peer',b'x','accepted','c','s',None,'t'),
            'accepted_verified','native_receipt','f'*64)
        cp.dispositions.Ledger(self.state_dir/'send-dispositions.json',self.profile).record(entry)
        result=self.finish()
        self.assertEqual(result['ledgerRevision'],2)
        self.assertEqual(self.pair[0]['ledgerRevision'],1)
        ticket=session_runtime.read_json(self.state_dir/'recovery-ticket.json')
        self.assertEqual(ticket['ledgerRevision'],2)
        self.assertEqual(len(ticket['admission']['entries']),1)

    def test_exact_new_owner_entry_has_no_config_or_argument_escape(self):
        self.assertEqual(session_runtime.parse_arguments(['--serve-recovery']),('--serve-recovery',False))
        for args in (['--serve-recovery','--allow-fresh-login'],['--serve-recovery','/tmp/ticket'],['--serve-recovery=foo']):
            with self.assertRaises(session_runtime.RuntimeErrorCode):session_runtime.parse_arguments(args)

    def test_maintenance_publishes_cleaned_candidate_and_latest_boundary_before_ticket(self):
        # A source-only cache file changes archive bytes without changing any
        # required Native row, key, proof or canonical event.
        candidate=self.root/'cleaned.zip'
        with zipfile.ZipFile(self.archive) as original,zipfile.ZipFile(candidate,'w') as cleaned:
            for info in original.infolist():cleaned.writestr(info.filename,original.read(info.filename))
            cleaned.writestr('accounts/wechat-primary/agent/cache-note',b'synthetic-maintenance-candidate')
        candidate.chmod(0o600)
        self.ops.replacement_snapshot=candidate
        self.ops.replacement_source=self.archive
        result=self.finish()
        ticket=session_runtime.read_json(self.state_dir/'recovery-ticket.json')
        self.assertEqual(ticket['snapshotSha256'],hashlib.sha256(candidate.read_bytes()).hexdigest())
        self.assertNotEqual(ticket['snapshotSha256'],self.pair[0]['snapshotSha256'])
        current=recovery.selected_pair(self.state_dir)
        self.assertEqual(ticket['manifestSha256'],recovery._manifest_hash(current))
        self.assertEqual(current[0]['bridgeBoundary'],self.boundary(3))
        self.assertEqual(json.loads(self.spool.cursor(self.ops.generation)),self.boundary(3))


    def test_worker_integrity_failure_is_durable_without_another_start(self):
        self.finish()
        recovery.block(self.core,'recovery_anchor_mismatch')
        self.assertEqual(recovery.recover(self.ops)['status'],'requires_review')
        self.assertEqual(self.launches,1)
