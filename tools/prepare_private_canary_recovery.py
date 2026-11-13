#!/usr/bin/env python3
"""One exact offline quarantine preparation; restore is a separate explicit mode."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SERVICE = 'astrbot-wechat-forwarder.service'
MAINTENANCE = 'astrbot-wechat-media-maintenance.service'
TIMER = 'astrbot-wechat-media-maintenance.timer'
BACKUP = 'private-canary-recovery'
LIVE = 'forward-image-live-send.json'
MEDIA = ('imageInputEnabled','imageOutputEnabled','voiceOutputEnabled',
         'stickerInputEnabled','linkInputEnabled','groupContextEnabled')
PHASE = 'preflight'


def require(value):
    if not value:
        raise ValueError('private_canary_recovery_requires_review')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def command(args):
    result = subprocess.run(args, capture_output=True, timeout=30)
    require(result.returncode == 0)
    return result.stdout


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def config(core):
    require(core.SETTINGS.get('privateOnly') is True)
    require(all(core.SETTINGS.get(key) is False for key in MEDIA))


class Environment:
    """Only fixed project unit reads and pausing the exact maintenance timer."""
    def __init__(self, core, bridge_path):
        require(core.UNIT == SERVICE)
        self.core, self.bridge_path = core, bridge_path

    def absent(self):
        detail = self.core.tap.details(SERVICE)
        require(detail.get('ActiveState') in {'failed','inactive'} and detail.get('MainPID') == '0')
        require(command(['/usr/bin/systemctl','show','--value','-p','Restart',SERVICE]).strip() == b'no')
        require(command(['/usr/bin/systemctl','show','--value','-p','OnFailure',SERVICE]).strip() == b'')
        maintenance = self.core.tap.details(MAINTENANCE)
        require(maintenance.get('ActiveState') in {'failed','inactive'} and maintenance.get('MainPID') == '0')
        production = self.core.recovery_coordinator.Production(self.core, bridge_path=self.bridge_path)
        require(production.inspect_owner(None) is None)
        production.no_workers(native=True)

    def timer_state(self):
        detail = self.core.tap.details(TIMER)
        require(detail.get('ActiveState') in {'active','inactive','failed'})
        return {key:detail.get(key,'') for key in ('ActiveState','SubState')}

    def pause_timer(self):
        command(['/usr/bin/systemctl','stop',TIMER])
        require(self.core.tap.details(TIMER).get('ActiveState') in {'inactive','failed'})


def raw_sources(core, rd):
    names = [name for name,_ in rd.AUDITS] + ['forward-image-hold.json']
    names += [name+'.recovery.json' for name,_ in rd.AUDITS]
    names += [rd.LEDGER,'recovery-startup.json','media-maintenance.json','recovery-ticket.json']
    result = {}
    for name in names:
        path = core.owner.STATE/name
        if path.exists() or path.is_symlink():
            result[name] = core.owner.read_private(path,65536)
    require(LIVE in result and rd.LEDGER in result)
    return result


def bridge_digest(spool):
    # The full consistent DB binds inbox, identities, submissions, cursor,
    # protection and any existing rebase without instantiating a live Spool.
    return sha(spool.db.serialize())


def exclusive(core, cp, path, raw):
    with cp.snapshot.state._directory(path.parent, private=True) as directory:
        try:
            fd = os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        except FileExistsError:
            require(core.owner.read_private(path,max(65536,len(raw))) == raw)
        else:
            with os.fdopen(fd,'wb') as output:
                output.write(raw); output.flush(); os.fsync(output.fileno())
        os.fsync(directory)


def inspect(core, cp, rd, live, archive, store):
    sources = raw_sources(core,rd)
    audit = json.loads(sources[LIVE],object_pairs_hook=rd.admission._unique_object)
    for name,_ in rd.AUDITS:
        if name != LIVE and name in sources:
            other = json.loads(sources[name],object_pairs_hook=rd.admission._unique_object)
            require(other.get('state') == 'accepted' or core.audit_cleared(other))
    current = store.current(); require(current is not None)
    previous = store.previous()
    pointer = core.owner.read_private(store.directory/'current.json',65536)
    old = current[0]
    journal = json.loads(sources['recovery-startup.json'],object_pairs_hook=rd.admission._unique_object) if 'recovery-startup.json' in sources else None
    selected = current
    if journal and journal.get('manifestSha256'):
        candidates = [pair for pair in (current,previous) if pair is not None
                      and sha(rd.admission._encoded(pair[0])) == journal['manifestSha256']]
        require(len(candidates) == 1); selected = candidates[0]
    seed = core.self_proof()
    # Same-profile sequence numbers do not prove continuity after rollback.
    # Preserve the old floor only when every previously committed causal
    # boundary still names its exact event in the saved current archive.
    boundaries = []
    for pair in (current,selected):
        for key in ('nativeBoundary','bridgeBoundary'):
            if pair[0][key] is not None and pair[0][key] not in boundaries:
                boundaries.append(pair[0][key])
    observed = cp.snapshot.inspect(archive,self_proof_candidates=[seed],boundaries=boundaries)
    require(observed['profileSha256'] == old['profileSha256'] and observed['selfProof'] == seed)
    with cp.snapshot.copied_database(archive) as (db,metadata):
        require(metadata['profileSha256'] == old['profileSha256'])
        require(cp.snapshot._seed(db,[seed]) == seed)
        row = rd._matching(db,audit,'media'); require(row is not None)
        require(audit.get('state') == 'submitted')
        require(row[4:6] == ('needs_review','uploading') and type(row[6]) is int and row[6] == 1)
        require(all(type(v) is int and v == 0 for v in row[7:9]))
        data = json.loads(row[10],object_pairs_hook=rd.admission._unique_object)
        require(not rd.admission._has_receipt(data) and rd._pre_submit(row) is False)
        proposed = rd.dispositions.entry(metadata['account'],'media',row,'unconfirmed_quarantined',
                                          'operator_review',rd._evidence(audit))
        require(rd.admission._safe_pending('media',row,proposed))
        ledger = rd.ledger_for(core,metadata['profileSha256']); prior = ledger.read()
        pending = rd.admission.pending_records(db); require(len(pending) == 4)
        approved = {(item['kind'],item['recordSha256']):item for item in prior['entries']}
        missing = []
        for kind,record in pending:
            entry = approved.get((kind,rd.admission.fingerprint(metadata['account'],kind,record)))
            if entry is None: missing.append((kind,record))
            else: require(rd.admission._safe_pending(kind,record,entry))
        coverage = live.recovery_coverage()
        with cp.snapshot.archive_file(archive) as saved, cp.snapshot.state._checked_zip(saved) as bundle:
            generation = 'profile:'+sha(bundle.read('api-token').strip())
        raw_checkpoint = live.cursor(generation); require(raw_checkpoint is not None)
        boundary = json.loads(raw_checkpoint,object_pairs_hook=rd.admission._unique_object)
        cp.snapshot.boundary(boundary)
        require(old['bridgeBoundary'] is not None and int(boundary['cursor']) >= int(old['bridgeBoundary']['cursor']))
        require(cp.snapshot._event(db,int(boundary['cursor'])) == boundary)
        require(cp.snapshot._event(db,int(old['bridgeBoundary']['cursor'])) == old['bridgeBoundary'])
        require(cp.manifests._covered(coverage,old) and cp.manifests._covered(coverage,selected[0]))
        require(not live.db.execute("SELECT 1 FROM inbox WHERE state IS NULL OR state NOT IN ('forwarded','expired') LIMIT 1").fetchone())
        private = json.loads(sources['private-forward-send.json'],object_pairs_hook=rd.admission._unique_object)
        peers = [raw for raw, in live.db.execute("SELECT raw FROM identities WHERE kind='user'")
                 if sha(raw.encode()) == private.get('destinationSha256')]
        require(len(peers) == 1)
        require(db.execute("SELECT count(*) FROM events WHERE sequence>? AND kind='message.committed' "
            "AND json_valid(data_json) AND json_extract(data_json,'$.threadId')=? "
            "AND json_extract(data_json,'$.direction')='incoming' AND json_extract(data_json,'$.origin')='live'",
            (int(boundary['cursor']),peers[0])).fetchone()[0] == 0)
    return dict(sources=sources,audit=audit,current=current,selected=selected,pointer=pointer,metadata=metadata,
        prior=prior,missing=missing,row=row,ledger=ledger,proposed=proposed,seed=seed,coverage=coverage,
        generation=generation,boundary=boundary,bridge=bridge_digest(live))


def unchanged(core, cp, rd, live, archive, store, proof, *, sources=None, pointer=None):
    require(raw_sources(core,rd) == (proof['sources'] if sources is None else sources))
    require(core.owner.read_private(store.directory/'current.json',65536) == (proof['pointer'] if pointer is None else pointer))
    require(bridge_digest(live) == proof['bridge'] and live.recovery_coverage() == proof['coverage'])
    require(core.self_proof() == proof['seed'])
    with cp.snapshot.archive_file(archive) as saved:
        require(cp.snapshot._digest(saved) == proof['metadata']['snapshotSha256'])


def completed(core, cp, rd, live, archive, store, proof, backup):
    path = backup/'complete.json'
    require(path.exists())
    marker = core.owner.read_json(path)
    require(set(marker) == {'sources','pointer','bridge','snapshot','binding'})
    require(marker['sources'] == {name:sha(raw) for name,raw in proof['sources'].items()})
    require(marker['pointer'] == sha(proof['pointer']) and marker['bridge'] == proof['bridge']
            and marker['snapshot'] == proof['metadata']['snapshotSha256'])
    require(marker['binding'] == sha(core.owner.read_private(backup/'binding.json',65536)))
    old_ledger = json.loads(core.owner.read_private(backup/rd.LEDGER,65536),object_pairs_hook=rd.admission._unique_object)
    old_audit = json.loads(core.owner.read_private(backup/LIVE,4096),object_pairs_hook=rd.admission._unique_object)
    require({k:v for k,v in proof['audit'].items() if k != 'recoveryDisposition'} == old_audit)
    require(core.audit_cleared(proof['audit']) and proof['missing'] == [] and len(proof['prior']['entries']) == 4
            and proof['prior']['revision'] == 4 and old_ledger['entries'] == proof['prior']['entries'][:3])
    require('recovery-startup.json' not in proof['sources'])
    with cp.snapshot.copied_database(archive) as (db,metadata):
        ticket = rd.dispositions.admission_for(db,metadata['account'],proof['ledger'])
        rd.admission.validate_database(db,metadata['account'],ticket)
    require(proof['current'][0]['ledgerRevision'] == 4)


def _prepare(core, bridge_path, environment):
    """Prepare only; injectable environment exposes no Native or send operation."""
    global PHASE
    cp, rd = core.recovery_checkpoints, core.recovery_dispositions
    config(core); environment.absent()
    state = core.owner.STATE; backup = state/BACKUP
    store = cp.manifests.Store(state/'recovery')
    archive = state/'checkpoints/private/current.zip'
    journal = core.recovery_coordinator.Journal(state)
    with ExitStack() as stack:
        # Same order as maintenance, then coordinator, then manifest capture.
        for name in ('media-maintenance.lock','bridge.lock','recovery-startup.lock','recovery.lock'):
            stack.enter_context(journal.lock(name))
        environment.absent()
        live = stack.enter_context(cp.bridge(bridge_path))
        PHASE = 'validate'
        proof = inspect(core,cp,rd,live,archive,store)
        if (backup/'complete.json').exists():
            completed(core,cp,rd,live,archive,store,proof,backup)
            return {'status':'prepared','pending':4,'ledger_entries':4}
        require(proof['prior']['revision'] == 3 and len(proof['prior']['entries']) == 3)
        require(proof['missing'] == [('media',proof['row'])] and 'recoveryDisposition' not in proof['audit'])
        require(not backup.exists() and not backup.is_symlink())
        # All proof validation precedes the first backup or timer mutation.
        timer = environment.timer_state()
        binding = dict(snapshot=proof['metadata']['snapshotSha256'],profile=proof['metadata']['profileSha256'],
            sources={name:sha(raw) for name,raw in proof['sources'].items()},pointer=sha(proof['pointer']),
            bridge=proof['bridge'],coverage=proof['coverage'],checkpoint=proof['boundary'],
            current=proof['current'][0],selected=proof['selected'][0],timer=timer)
        PHASE = 'backup'
        core.owner.private_directory(backup)
        for name,raw in {**proof['sources'],'recovery-pointer.json':proof['pointer'],
                         'binding.json':rd.admission._encoded(binding)}.items():
            exclusive(core,cp,backup/name,raw)
        cp.backup(live,backup/'bridge.sqlite')
        # Both old immutable generations can otherwise be cleaned by publish.
        # Only a distinct selected previous pair needs a full archive backup.
        if proof['selected'] != proof['current']:
            with cp.snapshot.archive_file(proof['selected'][1]) as saved:
                target=backup/'selected.zip'
                fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as output:
                    while chunk:=saved.read(cp.snapshot.state.CHUNK): output.write(chunk)
                    output.flush(); os.fsync(output.fileno())
        for directory in (backup,state):
            with cp.snapshot.state._directory(directory,private=True) as fd: os.fsync(fd)
        unchanged(core,cp,rd,live,archive,store,proof)
        environment.absent()
        PHASE = 'pause_timer'
        environment.pause_timer()
        environment.absent()
        unchanged(core,cp,rd,live,archive,store,proof)
        PHASE = 'quarantine'
        # _record owns Ledger.record's writer lock. Never hold it here.
        with cp.snapshot.copied_database(archive) as (db,metadata):
            require(metadata == proof['metadata'])
            marker=rd._record(core,db,metadata,proof['audit'],'media','unconfirmed_quarantined',
                              evidence_kind='operator_review')
            require(marker['matched'] is True and marker['admissionRequired'] is True
                    and marker['recordSha256'] == proof['proposed']['recordSha256']
                    and marker['classification'] == 'unconfirmed_quarantined')
        audit={**proof['audit'],'recoveryDisposition':marker}
        rd.write_evidence(core,state/LIVE,audit)
        updated=raw_sources(core,rd)
        require({name:raw for name,raw in updated.items() if name not in {LIVE,rd.LEDGER}} ==
                {name:raw for name,raw in proof['sources'].items() if name not in {LIVE,rd.LEDGER}})
        PHASE = 'publish'
        with cp.ledger_locked(proof['ledger']):
            now=proof['ledger'].read()
            require(now['entries'] == [*proof['prior']['entries'],proof['proposed']] and now['revision'] == 4)
            with cp.snapshot.copied_database(archive) as (db,metadata):
                ticket=rd.dispositions.admission_for(db,metadata['account'],proof['ledger'])
                rd.admission.validate_database(db,metadata['account'],ticket)
                require(len(ticket['entries']) == 4 and ticket['ledgerRevision'] == 4)
            unchanged(core,cp,rd,live,archive,store,proof,sources=updated)
            environment.absent()
            old=proof['current'][0]
            # No protection/cursor/rebase writes: inherit the conservative floor.
            pair=store.publish(archive,dict(epoch=old['epoch'],profileSha256=old['profileSha256'],
                ledgerRevision=4,bridgeBoundary=proof['boundary'],
                dedup=dict(floorSeq=old['dedup']['floorSeq'],highSeq=proof['coverage']['highSeq']),
                selfProof=proof['seed'],createdAt=datetime.now(timezone.utc).isoformat(),
                capture=dict(spool=live,checkpointName=proof['generation'])))
            require(store.previous() == proof['current'])
        new_pointer=core.owner.read_private(store.directory/'current.json',65536)
        unchanged(core,cp,rd,live,archive,store,proof,sources=updated,pointer=new_pointer)
        require(pair[0]['ledgerRevision'] == 4 and core.audit_cleared(audit))
        PHASE = 'archive_journal'
        old_journal=state/'recovery-startup.json'
        if 'recovery-startup.json' in proof['sources']:
            # Backup already contains exact original bytes. Move the live file
            # atomically only AFTER durable publication; never touch tickets.
            require(core.owner.read_private(old_journal,65536) == proof['sources']['recovery-startup.json'])
            os.replace(old_journal,backup/'startup-moved.json')
            for directory in (backup,state):
                with cp.snapshot.state._directory(directory,private=True) as fd: os.fsync(fd)
        final=raw_sources(core,rd)
        require(final == {name:raw for name,raw in updated.items() if name != 'recovery-startup.json'})
        exclusive(core,cp,backup/'complete.json',rd.admission._encoded(dict(
            sources={name:sha(raw) for name,raw in final.items()},pointer=sha(new_pointer),
            bridge=proof['bridge'],snapshot=proof['metadata']['snapshotSha256'],
            binding=sha(rd.admission._encoded(binding)))))
        return {'status':'prepared','pending':4,'ledger_entries':4}


def prepare(core, bridge_path, environment):
    try:
        return _prepare(core,bridge_path,environment)
    except Exception:
        raise ValueError('private_canary_recovery_requires_review') from None


def source(expected):
    global PHASE
    PHASE='source'
    require(isinstance(expected,str) and re.fullmatch('[0-9a-f]{40}',expected))
    prefix=['/usr/bin/git','--no-optional-locks','-c','safe.directory='+str(ROOT),'-C',str(ROOT)]
    require((ROOT/'.git').is_dir() and not (ROOT/'.git').is_symlink())
    require(command(prefix+['rev-parse','--show-toplevel']).decode().strip() == str(ROOT))
    command(prefix+['ls-files','--error-unmatch','tools/prepare_private_canary_recovery.py'])
    require(command(prefix+['rev-parse','HEAD']).decode().strip() == expected)
    require(not command(prefix+['status','--porcelain','--untracked-files=no']).strip())
    deploy=load('_canary_recovery_paths',ROOT/'tools/deploy_private_canary.py')
    deploy.check_runtime_paths(ROOT)
    sync=load('_canary_recovery_sync',ROOT/'tools/sync_runtime.py')
    require(sync.sync(False)['ok'] is True)
    formal=load('_canary_recovery_formal',ROOT/'scripts/run_forwarder.py')
    formal.load_config(); config(formal.core)
    return formal.core


def restore(core, bridge_path, environment):
    """Formal saved-session startup only; forwarder workers remain stopped."""
    global PHASE
    config(core)
    journal=core.recovery_coordinator.Journal(core.owner.STATE)
    with journal.lock('media-maintenance.lock'), journal.lock('bridge.lock'):
        environment.absent()
        # Verify this exact completed preparation before Native is restored.
        cp,rd=core.recovery_checkpoints,core.recovery_dispositions
        with cp.bridge(bridge_path) as live:
            archive=core.owner.STATE/'checkpoints/private/current.zip'
            store=cp.manifests.Store(core.owner.STATE/'recovery')
            proof=inspect(core,cp,rd,live,archive,store)
            completed(core,cp,rd,live,archive,store,proof,core.owner.STATE/BACKUP)
        require(core.tap.details(TIMER).get('ActiveState') in {'inactive','failed'})
        PHASE='restore'
        # Coordinator acquires its own startup/recovery/ledger locks.
        result=core.recovery_coordinator.startup(core)
        require(result.get('status') == 'recovering' and result.get('phase') == 'receive_ready')
        return {'status':'recovering','phase':'receive_ready'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--apply',action='store_true')
    modes.add_argument('--restore',action='store_true')
    parser.add_argument('--expected-commit',required=True)
    args=parser.parse_args()
    if os.geteuid() != 0 or not sys.platform.startswith('linux'):
        print(json.dumps({'stage':'private_canary_recovery','ok':False,'code':'linux_root_required'})); return 1
    try:
        core=source(args.expected_commit)
        bridge_path=core.recovery_checkpoints.BRIDGE_PUBLIC
        environment=Environment(core,bridge_path)
        result=restore(core,bridge_path,environment) if args.restore else prepare(core,bridge_path,environment)
        print(json.dumps({'stage':'private_canary_recovery','ok':True,**result},sort_keys=True)); return 0
    except Exception:
        # Exception text can contain private paths, credentials or message data.
        print(json.dumps({'stage':'private_canary_recovery','ok':False,
                          'code':'preparation_requires_review','phase':PHASE})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
