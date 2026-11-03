"""Resumable root startup, under bridge exclusion; no sends or fresh login.

The startup lock ends before the forwarding loop. The owner consumes a separate
single-use ticket without acquiring this lock. Every interrupted launch is
resolved by real unit/PID/cgroup identity, never by a cached readiness file.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import threading
import time

# Support the formal controller's explicit loader and python -I owner entry.
if not __package__:
    name='_wechat_recovery_private'
    if name not in sys.modules:
        spec=importlib.util.spec_from_loader(name,loader=None,is_package=True)
        package=importlib.util.module_from_spec(spec)
        package.__path__=[str(Path(__file__).resolve().parent)]
        sys.modules[name]=package
    __package__=name
cp=importlib.import_module(__package__+'.recovery_checkpoint')
diag=importlib.import_module(__package__+'.recovery_diagnostics')
owner_module=importlib.import_module(__package__+'.session_runtime')
admission=cp.snapshot.admission
PHASES=('validate','restore','online','rebind','receive_ready','ready')
DELAYS=(1,2,4,8,16,30)
COOLDOWN=300
MAX_ATTEMPTS=128


class Failure(Exception):
    def __init__(self, value):
        self.code=diag.code(value,'recovery_integrity')
        self.stage=self.code
        super().__init__(self.code)


class Journal:
    def __init__(self,state_dir):
        self.directory=Path(state_dir)
        self.path=self.directory/'recovery-startup.json'

    @contextmanager
    def lock(self,name='recovery-startup.lock'):
        with cp.snapshot.state._directory(self.directory,private=True) as parent:
            fd=os.open(name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
            try:
                cp.snapshot.state._check_object(os.fstat(fd),stat.S_ISREG,uid=os.geteuid(),mode=0o600)
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                yield
            finally:
                os.close(fd)

    def read(self):
        try: return owner_module.read_json(self.path)
        except FileNotFoundError: return None

    def write(self,value,path=None,*,exclusive=False):
        path=Path(path or self.path)
        raw=admission._encoded(value)
        if len(raw)>65536: raise Failure('recovery_integrity')
        with cp.snapshot.state._directory(path.parent,private=True) as parent:
            if path.exists() or path.is_symlink():
                cp.snapshot.state._check_object(path.lstat(),stat.S_ISREG,uid=os.geteuid(),mode=0o600)
                if exclusive: raise Failure('recovery_integrity')
            temporary='.'+path.name+'.'+secrets.token_hex(12)
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
            try:
                with os.fdopen(fd,'wb',closefd=False) as output:
                    output.write(raw); output.flush(); os.fsync(fd)
                os.replace(temporary,path.name,src_dir_fd=parent,dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(fd)
                try: os.unlink(temporary,dir_fd=parent)
                except FileNotFoundError: pass


def _save(ops,state,phase=None):
    if phase is not None: state['phase']=phase
    ops.journal.write(state)
    ops.publish(dict(state))


def recover(ops):
    """One serialized bounded attempt. Adapter supplies lifecycle/GET operations.

    Returns durable status. awaiting_network/user_action can be checked after
    retryAt without another restore or another resume. Permanent failure waits
    for explicit repair/review of recovery-startup.json, not systemd restarts.
    """
    locked=False
    try:
        with ops.journal.lock():
            locked=True
            state=ops.journal.read()
            if state is not None:
                if (state.get('schemaVersion')!=1 or state.get('phase') not in PHASES
                        or not re.fullmatch('[0-9a-f]{32}',state.get('attempt',''))):
                    raise Failure('recovery_integrity')
                if state.get('status')=='requires_review': return state
                if state.get('retryAt',0)>ops.clock(): return state
            try:
                binding=ops.validate(state)
                try:
                    live=ops.inspect_owner(state)
                except Failure as error:
                    if error.code not in {'awaiting_network','awaiting_user_action'}: raise
                    # A pre-decision observation hiccup must never fabricate or
                    # degrade durable state: without a journal the next call
                    # re-derives cold cleanly; a ready state stays ready.
                    if state is None:
                        return dict(schemaVersion=1,status=error.code,phase='validate',
                            code=error.code,retryAt=ops.clock()+1)
                    return state
                # A completed prior process may be gone after reboot. Only the
                # adapter's verified absence authorizes a fresh single-use ticket.
                fresh=(state is None or state.get('status')=='ready' or live is None and state.get('launched') is True
                       or state.get('status')=='ready' and live!=state.get('owner'))
                if fresh:
                    state=dict(schemaVersion=1,attempt=secrets.token_hex(16),phase='validate',
                        status='recovering',code='recovering',resumeRequested=False,launched=False,
                        cold=live is None,owner=live,attempts=0,retryAt=0,**binding)
                    _save(ops,state,'validate')
                elif any(state.get(k)!=v for k,v in binding.items()):
                    raise Failure('recovery_admission')
                # An absent owner at entry means this attempt stages a restore
                # launch: the snapshot is restored, so the rebase barrier must be
                # built even when the journal was written by a warm start. The
                # recorded owner is stale by definition, so a changed owner after
                # our own launch is expected exactly here.
                replacement=live is None and state.get('launched') is not True
                if live is None: state['cold']=True
                if state.get('retryAt',0): state['attempts']=0
                state.update(status='recovering',retryAt=0)
                # Revalidate live identity and account even after a ready receipt.
                for number in range(state.get('attempts',0),len(DELAYS)):
                    delay=DELAYS[number]
                    try:
                        live=ops.inspect_owner(state)
                        if live is None:
                            if state.get('launched'):
                                raise Failure('recovery_owner')
                            _save(ops,state,'restore')
                            ops.prepare(state)
                            # Persist before command dispatch: ambiguous command
                            # completion is resolved only through inspect_owner.
                            state['launched']=True
                            _save(ops,state)
                            ops.launch(state)
                            live=ops.inspect_owner(state)
                            if live is None: raise Failure('awaiting_network')
                        if state.get('owner') not in (None,live) and not replacement:
                            raise Failure('recovery_owner')
                        state['owner']=live
                        ops.online(state)
                        completed=state['phase']
                        if PHASES.index(completed)<PHASES.index('online'): _save(ops,state,'online')
                        if state['cold'] and PHASES.index(completed)<PHASES.index('rebind'):
                            ops.rebind(state)
                            _save(ops,state,'rebind')
                        ops.receive_ready(state)
                        state.update(status='recovering',code='recovering')
                        _save(ops,state,'receive_ready')
                        if getattr(ops,'defer_activation',False):
                            return state
                        state.update(status='ready',code='ok',attempts=0,retryAt=0)
                        _save(ops,state,'ready')
                        return state
                    except Failure as error:
                        if error.code not in {'awaiting_network','awaiting_user_action','onebot_not_connected'}: raise
                        state.update(status='awaiting_user_action' if error.code=='awaiting_user_action'
                                     else 'awaiting_network',code=error.code,attempts=number+1)
                        if number==len(DELAYS)-1: state['retryAt']=ops.clock()+delay+COOLDOWN
                        _save(ops,state)
                        ops.sleep(delay)
                state['retryAt']=ops.clock()+COOLDOWN
                _save(ops,state)
                return state
            except Exception as error:
                transient=getattr(error,'code',None) in {'awaiting_network','awaiting_user_action'}
                state=state or dict(schemaVersion=1,attempt=secrets.token_hex(16),phase='validate',
                    cold=False,launched=False,resumeRequested=False,owner=None,attempts=0,
                    **(binding if 'binding' in locals() else {}))
                state.update(status=error.code if transient else 'requires_review',
                    code=diag.code(getattr(error,'code',None),'recovery_integrity'))
                if transient: state['retryAt']=ops.clock()+COOLDOWN
                _save(ops,state)
                return state
            finally:
                ops.close()
    except BlockingIOError:
        # Only the startup lock proves a peer coordinator; a nested blocking
        # error from an adapter must surface as itself.
        if locked: raise
        return dict(status='recovering',phase='validate',code='recovering',retryAt=ops.clock()+1)


def _manifest_hash(pair):
    return hashlib.sha256(admission._encoded(pair[0])).hexdigest()


def selected_pair(state_dir,digest=None):
    store=cp.manifests.Store(Path(state_dir)/'recovery')
    pairs=[store.current()]
    if digest is not None and (pairs[0] is None or _manifest_hash(pairs[0])!=digest):
        pairs.append(store.previous())
    for pair in pairs:
        if pair is not None and (digest is None or _manifest_hash(pair)==digest): return pair
    raise Failure('recovery_integrity')


def _admit(core,pair):
    ledger=core.recovery_dispositions.ledger_for(core,pair[0]['profileSha256'])
    with cp.ledger_locked(ledger), cp.snapshot.copied_database(pair[1]) as (db,meta):
        value=cp.dispositions.admission_for(db,meta['account'],ledger)
        if value['profileSha256']!=meta['profileSha256'] or value['ledgerRevision']<pair[0]['ledgerRevision']:
            raise Failure('recovery_admission')
        admission.validate_database(db,meta['account'],value)
        return value


def consume_ticket(owner):
    """Called only by exact session_runtime.py --serve-recovery owner entry."""
    journal=Journal(owner.STATE)
    with journal.lock('recovery-ticket.lock'):
        ticket=owner.read_json(owner.STATE/'recovery-ticket.json')
        keys={'schemaVersion','phase','attempt','manifestSha256','snapshotSha256','profileSha256','ledgerRevision','admission'}
        if (set(ticket)!=keys or ticket['schemaVersion']!=2 or ticket['phase']!='prepared'
                or not re.fullmatch('[0-9a-f]{32}',ticket['attempt'])):
            raise Failure('recovery_admission')
        pair=selected_pair(owner.STATE,ticket['manifestSha256'])
        value=admission.validate_admission(ticket['admission'])
        if (ticket['snapshotSha256']!=pair[0]['snapshotSha256'] or
                ticket['profileSha256']!=pair[0]['profileSha256'] or
                ticket['ledgerRevision']!=value['ledgerRevision'] or value['profileSha256']!=ticket['profileSha256']
                or ticket['ledgerRevision']<pair[0]['ledgerRevision']):
            raise Failure('recovery_admission')
        ledger=cp.dispositions.Ledger(owner.STATE/'send-dispositions.json',ticket['profileSha256'])
        with cp.ledger_locked(ledger), cp.snapshot.copied_database(pair[1]) as (db,meta):
            if cp.dispositions.admission_for(db,meta['account'],ledger)!=value: raise Failure('recovery_admission')
            admission.validate_database(db,meta['account'],value)
            with cp.snapshot.archive_file(owner.STATE/'checkpoints/private/current.zip') as raw:
                if cp.snapshot._digest(raw)!=ticket['snapshotSha256']: raise Failure('recovery_integrity')
            # A durable immutable consumed receipt is the single-use fence. A
            # crash between receipt and mutable ticket update cannot consume twice.
            history=owner.STATE/'recovery-attempts'
            owner.private_directory(history)
            if len(list(history.iterdir()))>=MAX_ATTEMPTS: raise Failure('recovery_admission')
            ticks=int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
            receipt={**ticket,'phase':'consumed','pid':os.getpid(),'ticks':ticks}
            journal.write(receipt,history/(ticket['attempt']+'.json'),exclusive=True)
            journal.write({**ticket,'phase':'consumed'},owner.STATE/'recovery-ticket.json')
            journal.write(dict(attempt=ticket['attempt'],pid=os.getpid(),ticks=ticks),owner.CONTROL/'recovery-owner.json')
        return ticket


class Production:
    """Actual production adapter. Construct ONLY while holding bridge.lock.

    replacement_snapshot is maintenance's validated cleaned archive. It is
    published as an immutable generation before any restore ticket is prepared.
    """
    def __init__(self,core,*,bridge_path=None,replacement_snapshot=None,replacement_source=None):
        self.core,self.owner=core,core.owner
        self.state_dir=self.owner.STATE
        self.bridge_path=Path(bridge_path or cp.BRIDGE_PUBLIC)
        self.journal=Journal(self.state_dir)
        self.defer_activation=True
        self.replacement_snapshot=replacement_snapshot
        self.replacement_source=replacement_source
        self.pinned=None
        self.read_thread=None
        self.pair=None
        self.admitted=None
        self.generation=None
        self.seed=None
        self.clock=time.time
        self.sleep=time.sleep

    def publish(self,state):
        self.core.status(dict(active=False,ready=False,recovery=state['status'],code=state['code'],
            quarantined=len(self.admitted['entries']) if self.admitted else 0,new_qr=False))
        diag.emit(state['phase'],state['code'],counts={'attempts':state.get('attempts',0)})

    def validate(self,state):
        digest=state.get('manifestSha256') if state and state.get('status')!='ready' else None
        self.pair=selected_pair(self.state_dir,digest)
        self.admitted=_admit(self.core,self.pair)
        self.seed=self.pair[0]['selfProof']
        with cp.snapshot.archive_file(self.pair[1]) as raw, cp.snapshot.state._checked_zip(raw) as archive:
            token=archive.read('api-token').decode('ascii').strip()
        self.generation='profile:'+hashlib.sha256(token.encode()).hexdigest()
        self.no_workers()
        with cp.bridge(self.bridge_path) as live:
            self.checkpoint_name(live)  # Refuse unproved receive state before restoring Native.
        if self.replacement_snapshot is not None:
            if self.replacement_source is None: raise Failure('recovery_integrity')
            cp.validate_maintenance_snapshot(self.replacement_source,self.replacement_snapshot,self.state_dir)
            ledger=self.core.recovery_dispositions.ledger_for(self.core,self.pair[0]['profileSha256'])
            with cp.locked(self.state_dir), cp.ledger_locked(ledger), cp.bridge(self.bridge_path) as live:
                with cp.snapshot.copied_database(self.replacement_snapshot) as (db,meta):
                    self.admitted=cp.dispositions.admission_for(db,meta['account'],ledger)
                    admission.validate_database(db,meta['account'],self.admitted)
                name=self.checkpoint_name(live)
                coverage=live.recovery_coverage()
                metadata={k:v for k,v in self.pair[0].items() if k not in {'schemaVersion','snapshotSha256','nativeBoundary'}}
                metadata.update(ledgerRevision=self.admitted['ledgerRevision'],
                    bridgeBoundary=json.loads(live.cursor(name)),
                    dedup=dict(floorSeq=coverage['floorSeq'],highSeq=coverage['highSeq']),
                    createdAt=datetime.now(timezone.utc).isoformat(),capture=dict(spool=live,checkpointName=name))
                self.pair=cp.manifests.Store(self.state_dir/'recovery').publish(self.replacement_snapshot,metadata)
            self.replacement_snapshot=None
        return dict(manifestSha256=_manifest_hash(self.pair),profileSha256=self.pair[0]['profileSha256'],
                    ledgerRevision=self.admitted['ledgerRevision'],quarantined=len(self.admitted['entries']))

    def inspect_owner(self,state):
        c=self.core
        result,unit=c.tap.load_state(self.owner.OWNER)
        if result and unit.get('LoadState')!='not-found': raise Failure('recovery_owner')
        pid=int(unit.get('MainPID','0'))
        group='/system.slice/'+self.owner.OWNER
        if (unit.get('LoadState')=='not-found' or unit.get('ActiveState') in {'inactive','failed'}) and pid==0:
            control=unit.get('ControlGroup')
            if control and control!=group: raise Failure('recovery_owner')
            path=Path('/sys/fs/cgroup')/group.lstrip('/')/'cgroup.procs'
            if path.exists() and path.read_text().strip(): raise Failure('recovery_owner')
            self.no_workers(native=True)
            return None
        if pid<=1 or unit.get('ControlGroup')!=group: raise Failure('awaiting_network')
        ticks=c.tap.start_ticks(pid)
        c.groups.root_identity(pid,group,'/usr/bin/python3')
        argv=c.groups.argv(pid)
        base=[b'/usr/bin/python3',b'-I',b'-B']
        runtime=os.fsencode(Path(__file__).with_name('session_runtime.py'))
        allowed=[base+[runtime,flag] for flag in (b'--serve-online',b'--serve-long',b'--serve-recovery')]
        allowed.append(base+[os.fsencode(Path(__file__).with_name('resume_quarantined_native.py')),b'--serve'])
        if argv not in allowed: raise Failure('recovery_owner')
        attempt=None
        if argv[-1]==b'--serve-recovery':
            try: marker=self.owner.read_json(self.owner.CONTROL/'recovery-owner.json')
            except FileNotFoundError: raise Failure('awaiting_network') from None
            if (set(marker)!={'attempt','pid','ticks'} or marker['pid']!=pid or marker['ticks']!=ticks
                    or not re.fullmatch('[0-9a-f]{32}',marker['attempt'])): raise Failure('recovery_owner')
            receipt=self.owner.read_json(self.state_dir/'recovery-attempts'/(marker['attempt']+'.json'))
            if (receipt.get('phase')!='consumed' or receipt.get('pid')!=pid or receipt.get('ticks')!=ticks
                    or receipt.get('attempt')!=marker['attempt']): raise Failure('recovery_owner')
            attempt=marker['attempt']
            if state and state.get('cold') and state.get('status')!='ready':
                if attempt!=state['attempt'] or any(receipt.get(k)!=state.get(k)
                        for k in ('manifestSha256','profileSha256','ledgerRevision')):
                    raise Failure('recovery_owner')
        if c.tap.start_ticks(pid)!=ticks or c.tap.details(self.owner.OWNER).get('MainPID')!=str(pid):
            raise Failure('recovery_owner')
        return dict(pid=pid,ticks=ticks,attempt=attempt)

    def no_workers(self,*,native=False):
        # Read-only enumeration; never stop an unproved unit or another service.
        result=self.core.subprocess.run(['/usr/bin/systemctl','list-units','--all','--type=service',
            '--plain','--no-legend','--no-pager'],capture_output=True,timeout=5)
        if result.returncode: raise Failure('recovery_owner')
        if len(result.stdout)>262144: raise Failure('recovery_owner')
        prefixes=['astrbot-wechat-private-host-',self.core.groups.READER_PREFIX]
        if native: prefixes.append('astrbot-wechat-login-')
        for line in result.stdout.decode('ascii').splitlines():
            name=line.split()[0] if line.split() else ''
            if any(re.fullmatch(re.escape(prefix)+r'[0-9a-f]{12}\.service',name) for prefix in prefixes):
                detail=self.core.tap.details(name)
                if detail.get('ActiveState') not in {'inactive','failed'} or detail.get('MainPID')!='0':
                    raise Failure('recovery_owner')
                group=detail.get('ControlGroup')
                if group:
                    if not group.startswith('/') or '..' in Path(group).parts: raise Failure('recovery_owner')
                    path=Path('/sys/fs/cgroup')/group.lstrip('/')/'cgroup.procs'
                    if path.exists() and path.read_text().strip(): raise Failure('recovery_owner')

    def prepare(self,state):
        self.no_workers(native=True)
        if self.inspect_owner(state) is not None: raise Failure('recovery_owner')
        cp.snapshot.state.SnapshotStore(self.state_dir/'checkpoints').prepare()
        current=self.state_dir/'checkpoints/private/current.zip'
        with cp.snapshot.state._directory(current.parent,create=True,private=True) as directory:
            if current.exists() or current.is_symlink():
                cp.snapshot.state._check_object(current.lstat(),stat.S_ISREG,uid=os.geteuid(),mode=0o600)
            temporary='.next.zip'
            try:
                cp.snapshot.state._check_object(os.stat(temporary,dir_fd=directory,follow_symlinks=False),
                    stat.S_ISREG,uid=os.geteuid(),mode=0o600)
                os.unlink(temporary,dir_fd=directory)
            except FileNotFoundError: pass
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            try:
                with cp.snapshot.archive_file(self.pair[1]) as src, os.fdopen(fd,'wb',closefd=False) as dst:
                    shutil.copyfileobj(src,dst,65536); dst.flush(); os.fsync(fd)
                os.replace(temporary,current.name,src_dir_fd=directory,dst_dir_fd=directory); os.fsync(directory)
            finally:
                os.close(fd)
                try: os.unlink(temporary,dir_fd=directory)
                except FileNotFoundError: pass
        ticket=dict(schemaVersion=2,phase='prepared',attempt=state['attempt'],manifestSha256=state['manifestSha256'],
            snapshotSha256=self.pair[0]['snapshotSha256'],profileSha256=state['profileSha256'],
            ledgerRevision=state['ledgerRevision'],admission=self.admitted)
        with self.journal.lock('recovery-ticket.lock'):
            self.journal.write(ticket,self.state_dir/'recovery-ticket.json')

    def launch(self,state):
        try: self.owner.start_owner(False,seconds=self.owner.LONG_RUNTIME_SECONDS,recovery=True)
        except Exception as error:
            if getattr(error,'code',None) in {'owner_start_timeout','owner_start_failed'}:
                raise Failure('awaiting_network') from None
            raise

    def _pin(self):
        if self.pinned is None:
            self.pinned=self.core.groups.PinnedNative()
            try: self.pinned.open()
            except Exception:
                self.pinned.close(); self.pinned=None
                raise Failure('awaiting_network') from None
        if not self.pinned.unchanged(): raise Failure('recovery_owner')
        profile=cp.ForwarderCapture(self.owner,self.pinned,'pending',self.seed).profile()
        if profile!=self.pair[0]['profileSha256']: raise Failure('recovery_integrity')
        self.generation='profile:'+hashlib.sha256(self.pinned.token().encode()).hexdigest()

    def read(self,operation,*,deadline=None):
        """One bounded GET-only namespace thread. No additional root process."""
        self._pin()
        if self.read_thread is not None and self.read_thread.is_alive(): raise Failure('awaiting_network')
        deadline=deadline or time.monotonic()+15
        result={}
        netfd=os.dup(self.pinned.netfd)
        token=self.pinned.token()
        def work():
            try:
                os.setns(netfd,os.CLONE_NEWNET)
                lookup=self.core.health.contact.ContactReceiveClient('wechat-primary',token)
                result['value']=operation(lookup,deadline)
            except Exception as error:
                result['error']=error
            finally:
                os.close(netfd)
        thread=threading.Thread(target=work,name='recovery-get',daemon=True)
        self.read_thread=thread
        thread.start(); thread.join(max(0,deadline-time.monotonic())+.1)
        if thread.is_alive(): raise Failure('recovery_integrity')
        if 'error' in result:
            error=result['error']
            if isinstance(error,Failure): raise error
            if getattr(error,'code',None) in {'transport_failed','transport_timeout','transport_connection_failed',
                    'transport_protocol_failed','deadline_exceeded','request_failed'}:
                raise Failure('awaiting_network') from None
            raise Failure('recovery_integrity') from None
        if not self.pinned.unchanged(): raise Failure('recovery_owner')
        return result['value']

    def online(self,state):
        def account(lookup,deadline):
            lookup.policy.path=lookup.policy.prefix
            return lookup._get(lookup.policy.path,deadline,timeout=8)
        value=self.read(account)
        if value.get('id')!='wechat-primary' or type(value.get('authenticated')) is not bool:
            raise Failure('recovery_integrity')
        if value.get('authenticated') is not True or value.get('connection')!='online':
            if not state.get('resumeRequested') and value.get('connection')=='disconnected':
                state['resumeRequested']=True; _save(self,state)
                try: self.owner.client('resume')
                except Exception: pass  # Persisted single action; recheck GET next iteration.
            raise Failure('awaiting_user_action' if not value['authenticated'] else 'awaiting_network')
        def identity(lookup,deadline):
            lookup.policy.path=lookup.policy.prefix+'/sends/'+self.seed['sendId']
            intent=lookup._get(lookup.policy.path,deadline,timeout=8)
            message=lookup._get(lookup.policy.message(intent.get('localMessageId')),deadline,timeout=8)
            try:
                return self.core.loops.verified_sender(self.seed,intent,message,'wechat-primary',{intent.get('destination')})
            except ValueError: raise Failure('self_identity_unverified') from None
        self.self_sender=self.read(identity)

    def get_event(self,cursor):
        def event(lookup,deadline):
            page=lookup._get(lookup.policy.events(str(int(cursor)-1)),deadline,timeout=8)
            rows=page.get('items')
            if not isinstance(rows,list) or not rows or rows[0].get('cursor')!=cursor:
                raise Failure('recovery_anchor_mismatch')
            return rows[0]
        return self.read(event)

    def checkpoint_name(self,live):
        rows=live.db.execute('SELECT name,value FROM checkpoints LIMIT 129').fetchall()
        if len(rows)>128: raise Failure('recovery_rebase')
        names=[]
        for name,raw in rows:
            if name!=self.generation and not re.fullmatch(r'native:[1-9][0-9]*:[1-9][0-9]*',name): continue
            try:
                boundary=json.loads(raw,object_pairs_hook=admission._unique_object)
                cp.snapshot.boundary(boundary)
                cp.manifests.Store(self.state_dir/'recovery').recovery_plan(dict(spool=live,
                    profileSha256=self.pair[0]['profileSha256'],epoch=self.pair[0]['epoch'],checkpointName=name),self.pair)
            except Exception: continue
            names.append(name)
        if self.generation in names: return self.generation
        if len(names)!=1: raise Failure('recovery_rebase')
        return names[0]

    def target(self,name):
        return dict(epoch=self.pair[0]['epoch'],profileSha256=self.pair[0]['profileSha256'],
                    checkpointName=name,get_event=self.get_event)

    def rebind(self,state):
        self.no_workers()
        with cp.bridge(self.bridge_path) as live: old=self.checkpoint_name(live)
        boundary=self.pair[0]['bridgeBoundary']
        actual=self.get_event(boundary['cursor'])
        if cp.snapshot.event_digest(actual)!=boundary['anchor']: raise Failure('recovery_anchor_mismatch')
        target=self.target(old)
        target['get_event']=lambda cursor: actual if cursor==boundary['cursor'] else None
        cp.rebase_bridge(self.bridge_path,self.pair,target)
        # Rebase already durably backed up latest bridge. Copy ONLY the proved
        # anchored boundary to the exact stable name configured for the host.
        with cp.locked(self.state_dir), cp.bridge(self.bridge_path) as live:
            live.db.execute('BEGIN IMMEDIATE')
            try:
                raw=live.cursor(old)
                if json.loads(raw)!=self.pair[0]['bridgeBoundary']: raise Failure('recovery_rebase')
                existing=live.cursor(self.generation)
                if existing is not None and existing!=raw: raise Failure('recovery_rebase')
                live.cursor(self.generation,raw); live.db.commit()
            except Exception:
                live.db.rollback(); raise

    def receive_ready(self,state):
        # A ready receipt is not permission to infer the Native maximum. On an
        # ordinary restart prove the current live checkpoint without rolling back.
        with cp.bridge(self.bridge_path) as live:
            name=self.checkpoint_name(live)
            boundary=json.loads(live.cursor(name))
        if cp.snapshot.event_digest(self.get_event(boundary['cursor']))!=boundary['anchor']:
            raise Failure('recovery_anchor_mismatch')
        if name!=self.generation:
            with cp.locked(self.state_dir), cp.bridge(self.bridge_path) as live:
                if json.loads(live.cursor(name))!=boundary: raise Failure('recovery_rebase')
                live.cursor(self.generation,json.dumps(boundary,separators=(',',':')))

    def get_source(self,source,event,deadline):
        def message(lookup,end):
            value=lookup._get(lookup.policy.message(source),end,timeout=min(8,max(.001,end-time.monotonic())))
            target=event['data'].get('threadId')
            if (value.get('id')!=source or value.get('accountId')!='wechat-primary' or value.get('threadId')!=target
                    or value.get('direction')!='incoming' or not self.core.loops.sender_id(value.get('senderId'))
                    or value.get('senderId')==self.self_sender or type(value.get('type')) is not int
                    or value['type']!=1 or not isinstance(value.get('body'),str) or not 0<len(value['body'].encode())<=32768):
                # Images and quote/associated-image proof requires original
                # normalized metadata unavailable from this message GET. Retain
                # the barrier; never download media to manufacture evidence.
                raise Failure('recovery_reconcile')
            sse=importlib.import_module(__package__+'.forward_sse')
            data=dict(account='wechat-primary',id=source,room_id=target,sender_id=value['senderId'],
                body=value['body'],timestamp=sse.timestamp(value.get('createdAt')),origin='live',direction='incoming',type=1)
            if self.core.trigger.group_id(target) and self.core.trigger.self_mentioned(value.get('mentions')):
                data['mentioned']=True
            return data
        return self.read(message,deadline=deadline)

    def reconcile(self,pair,pinned,generation,seed,self_sender):
        """Called synchronously at the broker's fresh-capture ingress safe point."""
        self.pair,self.pinned,self.generation,self.seed,self.self_sender=pair,pinned,generation,seed,self_sender
        return cp.reconcile_replay(self.bridge_path,pair,self.target(generation),self.get_source)

    def close(self):
        if self.read_thread is not None and self.read_thread.is_alive():
            self.read_thread.join(1)
        if self.pinned is not None:
            self.pinned.close(); self.pinned=None


def block(core,code):
    """Keep a permanent worker integrity failure explicit across service starts."""
    journal=Journal(core.owner.STATE)
    with journal.lock():
        state=journal.read()
        if state is None: raise Failure('recovery_integrity')
        state.update(status='requires_review',code=diag.code(code,'recovery_integrity'))
        journal.write(state)
    diag.emit('worker',state['code'])


def mark_ready(core):
    """Final short activation transition, after actual host WS + Native SSE ready."""
    journal=Journal(core.owner.STATE)
    with journal.lock():
        state=journal.read()
        if state is None or state.get('phase')!='receive_ready': raise Failure('recovery_integrity')
        state.update(phase='ready',status='ready',code='ok',attempts=0,retryAt=0)
        journal.write(state)
        diag.emit('ready','ok')


def startup(core,*,replacement_snapshot=None,replacement_source=None):
    """Caller holds bridge.lock. Return a fixed status; never enter serve here."""
    try:
        return recover(Production(core,replacement_snapshot=replacement_snapshot,replacement_source=replacement_source))
    except Exception as error:
        code=diag.code(getattr(error,'code',None),'recovery_integrity')
        result=dict(status='requires_review',phase='validate',code=code)
        core.status(dict(active=False,ready=False,recovery='requires_review',code=code,new_qr=False))
        diag.emit('validate',code)
        return result
