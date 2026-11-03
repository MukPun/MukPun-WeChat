"""Two genuine isolated process lifecycles across the real recovery stack.

No real account, message send or model invocation happens here: the Native side
is a real constrained child process plus a local synthetic HTTP API serving
fixture events, and the OneBot side is the real Gateway over the real Spool.
Both cold restore and online owner reuse are exercised; volatile state is
emptied between lifecycles and the worker is terminated at controlled durable
boundaries. Uncertain WS outcomes and metadata pressure beyond the protected
4096 window are covered without any network peer beyond loopback.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from urllib.request import urlopen

from scripts import recovery_coordinator as coordination
from scripts import recovery_checkpoint as cp
from scripts import send_dispositions
from scripts.forward_sse import event_digest
from wechat_bridge.durable import Spool, StateError
from wechat_bridge.forward_gateway import Gateway
from test_recovery_snapshot import fixture, event, SEED

MARKER = '#lifecycle-native-owner'

CHILD = r'''
import json,os,sys,time
state_dir,attempt=sys.argv[1],sys.argv[2]
ticket=json.loads(open(os.path.join(state_dir,'recovery-ticket.json')).read())
if ticket['phase']!='prepared' or ticket['attempt']!=attempt: raise SystemExit(2)
ticks=int(open('/proc/self/stat').read().rsplit(')',1)[1].split()[19])
fd=os.open(os.path.join(state_dir,'recovery-owner.json'),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
os.write(fd,json.dumps(dict(schemaVersion=1,pid=os.getpid(),ticks=ticks,attempt=attempt)).encode())
os.close(fd)
while True: time.sleep(1)
'''


def _ticks(pid):
    stat = Path('/proc')/str(pid)/'stat'
    try:
        raw = stat.read_text()
    except OSError:
        return None
    return int(raw.rsplit(')',1)[1].split()[19])


def _argv(pid):
    try:
        raw = (Path('/proc')/str(pid)/'cmdline').read_bytes()
    except OSError:
        return None
    return [item.decode('utf-8','replace') for item in raw.split(b'\0') if item]


def _owner_processes(state_dir):
    """Genuine /proc verification, like the systemd MainPID path in production."""
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        argv = _argv(entry.name)
        if (argv is None or len(argv) < 4 or argv[0] != sys.executable or argv[1] != '-c'
                or not argv[2].startswith(MARKER) or argv[3] != str(state_dir)):
            continue
        ticks = _ticks(entry.name)
        if ticks is not None:
            found.append(dict(pid=int(entry.name),ticks=ticks))
    return found


class NativeAPI(BaseHTTPRequestHandler):
    """Synthetic Native HTTP API: account, resume, events, self identity."""

    def log_message(self, *args):
        pass

    def _send(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        server = self.server
        if server.delay:
            time.sleep(server.delay)
        path = self.path
        if path == '/account':
            self._send(dict(id='wechat-primary',authenticated=True,connection=server.connection))
            return
        if path.startswith('/events/'):
            self._send(dict(items=[event(int(path.rsplit('/',1)[1]))]))
            return
        if path == '/sends/'+SEED['sendId']:
            self._send(dict(localMessageId='seed-message',destination='peer'))
            return
        if path == '/messages/seed-message':
            self._send(dict(text='accepted text',bytes=SEED['bytes']))
            return
        self._send({}, status=404)

    def do_POST(self):
        if self.path == '/resume':
            self.server.resumed += 1
            self.server.connection = 'online'
            self._send(dict(ok=True))
            return
        self._send({}, status=404)


class LifecycleHarness:
    """One isolated lifecycle environment: real bridge, manifests, journal."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.state_dir = self.root/'private'
        self.state_dir.mkdir(mode=0o700)
        self.spool = Spool(self.root/'bridge.sqlite')
        self.spool.db.execute('PRAGMA synchronous=OFF')  # synthetic bulk fixture only
        self.generation = 'profile:'+'b'*64
        self.children = []
        server = ThreadingHTTPServer(('127.0.0.1',0), NativeAPI)
        server.connection = 'online'
        server.resumed = 0
        server.delay = 0
        self.server = server
        threading.Thread(target=server.serve_forever,name='synthetic-native',daemon=True).start()
        self.api = 'http://127.0.0.1:%d' % server.server_address[1]

    def close(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        self.server.shutdown()
        self.server.server_close()
        self.spool.close()
        self.temp.cleanup()

    @staticmethod
    def boundary(n):
        return {'cursor':str(n),'anchor':event_digest(event(n))}

    def add(self, n, *, terminal=True, commit=True, state='queued'):
        source = 'source-'+str(n)
        data = dict(account='wechat-primary',id=source,body='same',sender_id='peer',room_id='peer',
                    direction='incoming',origin='live',type=1,timestamp=1)
        if commit:
            self.spool.admit_checkpoint(source,data,self.generation,json.dumps(self.boundary(n)))
        else:
            self.spool.admit(source,data)
        if state == 'publishing':
            self.spool.publishing(source)
        elif terminal:
            self.spool.publishing(source)
            self.spool.handoff(source,True)
        return data

    def delivered(self, source):
        data = dict(account='wechat-primary',id=source,body='same',sender_id='peer',room_id='peer',
                    direction='incoming',origin='live',type=1,timestamp=1)
        self.spool.admit(data['id'],data)
        self.spool.publishing(data['id'])
        self.spool.handoff(data['id'],True)

    def capture(self):
        stale = self.root/('source'+str(8))
        if stale.exists():
            shutil.rmtree(stale)
        archive = fixture(self.root,last=8)
        keydir = self.root/'keys'
        keydir.mkdir(mode=0o700,exist_ok=True)
        for name in ('api-token','storage-key','cursor-key'):
            (keydir/name).write_bytes(('synthetic-'+name).encode())
            (keydir/name).chmod(0o600)
        keyfd = os.open(keydir,os.O_RDONLY|os.O_DIRECTORY)
        pinned = SimpleNamespace(unchanged=lambda:True,host=SimpleNamespace(client_pid=123,
            client_ticks=456,data_fd=keyfd,client_uid=os.geteuid()))
        @contextmanager
        def checkpoint_copy(owner):
            # No SQLite write transaction may span the owner mailbox wait.
            assert not self.spool.db.in_transaction
            yield archive
        owner = SimpleNamespace(checkpoint_copy=checkpoint_copy)
        adapter = cp.ForwarderCapture(owner,pinned,self.generation,SEED)
        pair = cp.capture(adapter,self.spool.path,self.state_dir)
        os.close(keyfd)
        return pair

    def ops(self, pair, *, crash=None):
        return LifecycleOps(self, pair, crash)

    def wipe_volatile(self):
        # A bridge restart lost volatile control evidence while the Native
        # owner process may still be alive; durable state always remains.
        for name in ('recovery-startup.json','recovery-owner.json','recovery-ticket.json'):
            path = self.state_dir/name
            if path.exists():
                path.unlink()


class LifecycleOps:
    """Adapter the real engine drives: real journal, ticket, /proc owner checks,
    real HTTP API reads, real rebase over the real bridge database."""

    def __init__(self, harness, pair, crash=None):
        self.harness = harness
        self.pair = pair
        self.crash = crash
        self.journal = coordination.Journal(harness.state_dir)
        self.events = []
        self._crashed = False
        self._pending_mismatch = False

    # -- engine plumbing ---------------------------------------------------
    clock = lambda self: 0
    sleep = lambda self, seconds: None

    def publish(self, state):
        self.events.append((state['phase'],state['status']))
        if self.crash == state['phase'] and not self._crashed:
            self._crashed = True
            raise KeyboardInterrupt()

    def close(self):
        pass

    def _get(self, path):
        return json.loads(urlopen(self.harness.api+path,timeout=10).read())

    # -- admission ---------------------------------------------------------
    def validate(self, state):
        pair = coordination.selected_pair(self.harness.state_dir)
        ledger = send_dispositions.Ledger(self.harness.state_dir/'send-dispositions.json',
                                          pair[0]['profileSha256'])
        with cp.ledger_locked(ledger), cp.snapshot.copied_database(pair[1]) as (db,meta):
            value = send_dispositions.admission_for(db,meta['account'],ledger)
            if (value['profileSha256']!=meta['profileSha256']
                    or value['ledgerRevision']<pair[0]['ledgerRevision']):
                raise coordination.Failure('recovery_admission')
            if self._pending_mismatch:
                # A NEW pending send nobody dispositioned must not be
                # inherited from the old exact admission evidence.
                raise coordination.Failure('recovery_admission')
        self.pair = pair
        return dict(manifestSha256=coordination._manifest_hash(pair),
                    profileSha256=pair[0]['profileSha256'],
                    ledgerRevision=pair[0]['ledgerRevision'])

    # -- owner lifecycle ---------------------------------------------------
    def inspect_owner(self, state):
        found = _owner_processes(self.harness.state_dir)
        if len(found) > 1:
            raise coordination.Failure('recovery_owner')
        return found[0] if found else None

    def no_workers(self):
        if len(_owner_processes(self.harness.state_dir)) > 1:
            raise coordination.Failure('recovery_owner')

    def prepare(self, state):
        target = self.harness.state_dir/'checkpoints/private'
        target.mkdir(parents=True,exist_ok=True)
        current = target/'current.zip'
        shutil.copyfile(self.pair[1],current)
        current.chmod(0o600)
        ticket = dict(schemaVersion=2,phase='prepared',attempt=state['attempt'],
            manifestSha256=state['manifestSha256'],snapshotSha256=self.pair[0]['snapshotSha256'],
            profileSha256=state['profileSha256'],ledgerRevision=state['ledgerRevision'],
            admission=dict(schemaVersion=1,profileSha256=self.pair[0]['profileSha256'],
                           ledgerRevision=state['ledgerRevision'],entries=[]))
        with self.journal.lock('recovery-ticket.lock'):
            self.journal.write(ticket,self.harness.state_dir/'recovery-ticket.json')
        if self.crash == 'prepare' and not self._crashed:
            self._crashed = True
            raise KeyboardInterrupt()

    def launch(self, state):
        marker = self.harness.state_dir/'recovery-owner.json'
        if marker.exists():
            marker.unlink()
        child = subprocess.Popen([sys.executable,'-c',MARKER+'\n'+CHILD,
            str(self.harness.state_dir),state['attempt']],
            start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.harness.children.append(child)
        for _ in range(200):
            if marker.exists() and marker.read_text().find(state['attempt'])>=0:
                break
            if child.poll() is not None:
                raise coordination.Failure('awaiting_network')
            time.sleep(0.05)
        if self.crash == 'launch' and not self._crashed:
            self._crashed = True
            child.kill()
            child.wait(timeout=10)
            raise KeyboardInterrupt()

    # -- real API reads ------------------------------------------------------
    def online(self, state):
        value = self._get('/account')
        if value.get('id')!='wechat-primary' or type(value.get('authenticated')) is not bool:
            raise coordination.Failure('recovery_integrity')
        if value.get('connection')=='disconnected':
            if not state.get('resumeRequested'):
                state['resumeRequested']=True
                coordination._save(self,state)
                self._post_resume()
            raise coordination.Failure('awaiting_network')
        intent = self._get('/sends/'+SEED['sendId'])
        message = self._get('/messages/'+intent['localMessageId'])
        if (hashlib.sha256(message['text'].encode()).hexdigest()!=SEED['textSha256']
                or hashlib.sha256(intent['destination'].encode()).hexdigest()!=SEED['destinationSha256']
                or message['bytes']!=SEED['bytes']):
            raise coordination.Failure('self_identity_unverified')

    def _post_resume(self):
        from urllib.request import Request
        request = Request(self.harness.api+'/resume',data=b'{}',
                          headers={'Content-Type':'application/json'})
        urlopen(request,timeout=10).read()

    def get_event(self, cursor):
        rows = self._get('/events/'+str(int(cursor))).get('items')
        if not isinstance(rows,list) or not rows or rows[0].get('cursor')!=str(cursor):
            raise coordination.Failure('recovery_anchor_mismatch')
        return rows[0]

    # -- rebind over the real bridge ---------------------------------------
    def checkpoint_name(self, live):
        rows = [row[0] for row in live.db.execute('SELECT name FROM checkpoints')]
        if self.harness.generation in rows:
            return self.harness.generation
        raise coordination.Failure('recovery_rebase')

    def target(self, name):
        return dict(epoch=self.pair[0]['epoch'],profileSha256=self.pair[0]['profileSha256'],
                    checkpointName=name,get_event=self.get_event)

    def rebind(self, state):
        self.no_workers()
        with cp.bridge(self.harness.spool.path) as live:
            old = self.checkpoint_name(live)
        boundary = self.pair[0]['bridgeBoundary']
        actual = self.get_event(boundary['cursor'])
        if event_digest(actual)!=boundary['anchor']:
            raise coordination.Failure('recovery_anchor_mismatch')
        target = self.target(old)
        target['get_event'] = lambda cursor: actual if str(cursor)==boundary['cursor'] else None
        cp.rebase_bridge(self.harness.spool.path,self.pair,target)

    def receive_ready(self, state):
        coverage = self.harness.spool.recovery_coverage()
        if coverage['floorSeq'] is None:
            raise coordination.Failure('recovery_rebase')
        if self.harness.spool.counts().get('publishing',0):
            raise coordination.Failure('onebot_not_connected')


class LifecycleCase(unittest.TestCase):
    def setUp(self):
        self.harness = LifecycleHarness()
        self.addCleanup(self.harness.close)

    add = lambda self, n, **kw: self.harness.add(n,**kw)
    capture = lambda self: self.harness.capture()
    ops = lambda self, pair, **kw: self.harness.ops(pair,**kw)
    wipe_volatile = lambda self: self.harness.wipe_volatile()
    spool = property(lambda self: self.harness.spool)
    state_dir = property(lambda self: self.harness.state_dir)
    server = property(lambda self: self.harness.server)


class ColdRestoreAndReuseTests(LifecycleCase):
    def test_cold_restore_then_online_reuse_then_owner_death_relaunch(self):
        for n in range(1,9):
            self.add(n)
        pair = self.capture()
        # Lifecycle 1: cold restore through the real engine.
        first = coordination.recover(self.ops(pair))
        self.assertEqual(first['status'],'ready')
        self.assertTrue(first['cold'])
        self.assertEqual(len(_owner_processes(self.state_dir)),1)
        self.assertTrue((self.state_dir/'receive-before.sqlite').exists())
        # The pre-boundary delivered inputs were never replayed to the model.
        self.assertEqual(self.spool.counts()['forwarded'],8)
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],8)

        # Lifecycle 2: bridge restart with volatile evidence gone, owner alive.
        self.wipe_volatile()
        second = coordination.recover(self.ops(pair))
        self.assertEqual(second['status'],'ready')
        self.assertFalse(second['cold'])
        self.assertEqual(len(_owner_processes(self.state_dir)),1)
        self.assertEqual(self.spool.counts()['forwarded'],8)

        # Lifecycle 3: owner died. Verified absence authorizes one relaunch.
        for child in self.harness.children:
            child.kill()
            child.wait(timeout=10)
        self.wipe_volatile()
        third = coordination.recover(self.ops(pair))
        self.assertEqual(third['status'],'ready')
        self.assertTrue(third['cold'])
        self.assertEqual(len(_owner_processes(self.state_dir)),1)
        self.assertEqual(self.spool.counts()['forwarded'],8)
        # Same durable evidence: no new model-side redelivery anywhere.
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],8)

    def test_transient_first_observation_does_not_fabricate_cold(self):
        for n in range(1,9):
            self.add(n)
        pair = self.capture()
        ops = self.ops(pair)
        original = ops.inspect_owner
        def transient(state):
            if not getattr(ops,'_observed',False):
                ops._observed = True
                raise coordination.Failure('awaiting_network')
            return original(state)
        ops.inspect_owner = transient
        first = coordination.recover(ops)
        self.assertEqual(first['status'],'awaiting_network')
        self.assertIsNone(ops.journal.read())
        second = coordination.recover(self.ops(pair))
        self.assertEqual(second['status'],'ready')
        self.assertTrue(second['cold'])


class BoundaryTerminationTests(unittest.TestCase):
    def test_termination_at_each_durable_boundary_never_duplicates_or_replays(self):
        for crash in ('validate','prepare','launch','online','rebind','receive_ready','ready'):
            with self.subTest(crash=crash):
                harness = LifecycleHarness()
                try:
                    for n in range(1,9):
                        harness.add(n)
                    pair = harness.capture()
                    ops = harness.ops(pair,crash=crash)
                    with self.assertRaises(KeyboardInterrupt):
                        coordination.recover(ops)
                    state = ops.journal.read()
                    self.assertIsNotNone(state)
                    launches = len(harness.children)
                    result = coordination.recover(harness.ops(pair))
                    self.assertEqual(result['status'],'ready')
                    alive = _owner_processes(harness.state_dir)
                    # A live owner is never launched twice; only verified
                    # absence (crash == 'launch' killed the child) relaunches.
                    if crash in ('prepare','online','rebind','receive_ready','ready','validate'):
                        self.assertEqual(len(harness.children),max(1,launches))
                    else:
                        self.assertEqual(len(harness.children),launches+1)
                    self.assertEqual(len(alive),1)
                    # No replay: delivered inputs stay terminal and protected.
                    self.assertEqual(harness.spool.counts()['forwarded'],8)
                    self.assertGreaterEqual(
                        harness.spool.recovery_coverage()['floorSeq'],8)
                finally:
                    harness.close()


class CoordinatorExclusivityTests(LifecycleCase):
    def test_two_coordinators_yield_single_recovery_right(self):
        for n in range(1,9):
            self.add(n)
        pair = self.capture()
        self.server.delay = 0.4
        probed = {}
        def probe():
            time.sleep(0.2)  # first coordinator already holds the journal lock
            probed['status'] = coordination.recover(self.ops(pair))['status']
        thread = threading.Thread(target=probe,name='coordinator-2')
        thread.start()
        first = coordination.recover(self.ops(pair))
        thread.join(timeout=30)
        self.server.delay = 0
        self.assertEqual(probed['status'],'recovering')
        self.assertEqual(first['status'],'ready')
        self.assertEqual(len(self.harness.children),1)


class GatewayRecoverySemanticsTests(LifecycleCase):
    def gateway(self):
        emitted = []
        gateway = Gateway('wechat-primary','peer',self.spool,self.harness.generation,
                          emitted.append)
        return gateway, emitted

    def frame(self, source, body='same'):
        return dict(kind='event',cursor='1',data=dict(account='wechat-primary',id=source,
            body=body,sender_id='peer',room_id='peer',direction='incoming',origin='live',
            type=1,timestamp=1))

    def test_recovered_spool_semantics_after_recovery(self):
        for n in range(1,9):
            self.add(n)
        pair = self.capture()
        self.assertEqual(coordination.recover(self.ops(pair))['status'],'ready')
        gateway,emitted = self.gateway()
        # Same id, same body: deduplicated, never re-emitted to the model.
        gateway.feed(self.frame('source-3'))
        self.assertIn({'kind':'report','stage':'loop_guard','reason':'duplicate_input'},emitted)
        self.assertFalse([item for item in emitted if item.get('kind') not in ('report','ack')])
        # Same body, different id: a genuinely new input is admitted.
        gateway.feed(self.frame('source-new','same'))
        self.assertEqual(self.spool.counts()['queued'],1)
        # Same id, changed body: rejected, never rewritten.
        with self.assertRaises(StateError):
            gateway.feed(self.frame('source-3','changed'))

    def test_uncertain_ws_outcome_is_retained_not_replayed(self):
        self.add(1)
        # The worker died mid-handoff: on reopen the row is uncertain.
        self.add(2,terminal=False,state='publishing')
        reopened = Spool(self.harness.root/'bridge.sqlite')
        self.assertEqual(reopened.counts().get('uncertain'),1)
        reopened.close()
        for n in range(3,9):
            self.add(n)
        pair = self.capture()
        result = coordination.recover(self.ops(pair))
        # Uncertain inputs are nonfatal for readiness but never replayed.
        self.assertEqual(result['status'],'ready')
        gateway,emitted = self.gateway()
        gateway.feed(self.frame('source-2'))
        self.assertIn({'kind':'report','stage':'loop_guard','reason':'duplicate_input'},emitted)
        self.assertEqual(self.spool.counts().get('uncertain'),1)

    def test_receive_pressure_beyond_protected_window_pauses_cleanly(self):
        for n in range(1,9):
            self.add(n)
        pair = self.capture()
        self.assertEqual(coordination.recover(self.ops(pair))['status'],'ready')
        # Fill the protected window exactly: the floor row itself counts.
        for n in range(9,9+4095):
            self.harness.delivered('pressure-'+str(n))
        gateway,emitted = self.gateway()
        gateway.feed(self.frame('overflow-1'))
        self.assertIn({'kind':'report','stage':'recovery','code':'recovery_checkpoint_required'},
                      emitted)
        self.assertIsNone(self.spool.counts().get('queued'))
        # The floor advances only past BOTH committed generations: the old
        # manifest's floor still binds after one capture, so a successive
        # committed capture is required before the retained input flushes.
        self.harness.capture()
        self.harness.capture()
        coverage = self.spool.recovery_coverage()
        gateway.feed(dict(kind='recovery_protection',floorSeq=coverage['floorSeq'],
                          epoch=coverage['epoch']))
        self.assertEqual(self.spool.counts().get('queued'),1)


class AdmissionInheritanceTests(LifecycleCase):
    def test_new_pending_send_is_not_inherited_from_old_admission(self):
        for n in range(1,9):
            self.add(n)
        pair = self.capture()
        ops = self.ops(pair)
        ops._pending_mismatch = True
        result = coordination.recover(ops)
        self.assertEqual(result['status'],'requires_review')
        self.assertEqual(result['code'],'recovery_admission')
        self.assertEqual(self.harness.children,[])
        # Review state is durable: a plain restart does not bypass it.
        self.assertEqual(coordination.recover(self.ops(pair))['status'],'requires_review')
        # Explicit review clears volatile evidence; the exact old admission
        # evidence then recovers cleanly without inheriting the unknown row.
        self.wipe_volatile()
        self.assertEqual(coordination.recover(self.ops(pair))['status'],'ready')


if __name__ == '__main__':
    unittest.main()
