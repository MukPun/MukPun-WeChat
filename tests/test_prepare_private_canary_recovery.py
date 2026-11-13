"""Real synthetic saved-session preparation; never Native/network/model calls."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

from scripts import recovery_checkpoint as cp, recovery_dispositions as rd
from scripts import recovery_manifest as manifests, recovery_snapshot as snapshot
from scripts import recovery_coordinator
from scripts import session_runtime as owner
from test_recovery_snapshot import fixture, SEED, event


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root/'state'; self.state.mkdir(mode=0o700)
        raw = fixture(self.root)
        self.live = dict(state='submitted', key='b'*32, destinationSha256=hashlib.sha256(b'peer').hexdigest(),
                         sourceSha256='c'*64, bytes=100, requestId='d'*32)
        self.rows = [('text', ('old-text','old-key','hash','peer',b'cipher','needs_review',None,None,None,'time'))]
        for i in range(3):
            data = {'threadId':'peer','result':{'imagePreparation':{'sourceSha256':'c'*64}}}
            self.rows.append(('media', (f'media-{i}','send:'+('b'*32 if i==2 else str(i)*32),'hash',
                'send','needs_review','uploading',1,0,0,'unknown',json.dumps(data),b'cipher','time','time')))
        db = sqlite3.connect(self.root/'source8/control.sqlite')
        for kind,row in self.rows:
            db.execute('INSERT INTO '+('outbox' if kind=='text' else 'media_jobs')+' VALUES('+','.join('?'*len(row))+')',row)
        db.commit(); db.close()
        raw.unlink()
        with zipfile.ZipFile(raw,'w') as bundle:
            for key in ('api-token','storage-key','cursor-key'): bundle.writestr(key,('synthetic-'+key).encode())
            bundle.write(self.root/'source8/control.sqlite','accounts/wechat-primary/agent/control.sqlite')
        raw.chmod(0o600)
        archive_dir=self.state/'checkpoints/private'; archive_dir.mkdir(parents=True,mode=0o700)
        (self.state/'checkpoints').chmod(0o700)
        self.archive=archive_dir/'current.zip'; shutil.copyfile(raw,self.archive); self.archive.chmod(0o600)
        self.profile=snapshot.inspect(self.archive)['profileSha256']
        self.ledger=rd.dispositions.Ledger(self.state/rd.LEDGER,self.profile)
        for kind,row in self.rows[:3]:
            self.ledger.record(rd.dispositions.entry('wechat-primary',kind,row,'unconfirmed_quarantined','operator_review','a'*64))
        self.prior_ledger=self.ledger.read()
        self.core=SimpleNamespace(owner=SimpleNamespace(STATE=self.state,read_json=owner.read_json,
            read_private=owner.read_private,write_json=owner.write_json,private_directory=owner.private_directory),
            SETTINGS={'privateOnly':True, **{k:False for k in ('imageInputEnabled','imageOutputEnabled',
                'voiceOutputEnabled','stickerInputEnabled','linkInputEnabled','groupContextEnabled')}},
            self_proof=lambda:SEED, audit_cleared=lambda audit:rd.audit_cleared(self.core,audit),
            recovery_dispositions=rd,recovery_checkpoints=cp,recovery_coordinator=recovery_coordinator)
        for name,value in [('forward-image-live-send.json',self.live),('forward-send.json',dict(state='accepted',**SEED)),
                           ('private-forward-send.json',{'state':'accepted','destinationSha256':hashlib.sha256(b'peer').hexdigest()}),
                           ('recovery-startup.json',{'status':'requires_review','phase':'validate','attempt':'e'*32}),
                           ('media-maintenance.json',{'status':'failed'}),('recovery-ticket.json',{'preserve':True})]:
            owner.write_json(self.state/name,value)
        history=self.state/'recovery-attempts'; history.mkdir(mode=0o700)
        owner.write_json(history/'old.json',{'phase':'consumed'})
        self.bridge=self.root/'bridge.sqlite'
        spool=cp.Spool(self.bridge); spool.protect_recovery(1,'a'*32)
        spool.identity('user','peer'); spool.reserve('prior-action','prior-digest','native')
        spool.admit('prior-source',{'body':'synthetic'}); spool.publishing('prior-source'); spool.handoff('prior-source',True)
        self.generation='profile:'+hashlib.sha256(b'synthetic-api-token').hexdigest()
        self.boundary={'cursor':'8','anchor':snapshot.event_digest(event(8))}
        spool.cursor(self.generation,json.dumps(self.boundary))
        spool.db.execute('CREATE TABLE recovery_rebase(singleton INTEGER PRIMARY KEY,floor_seq,high_seq,checkpoint)')
        spool.db.execute('INSERT INTO recovery_rebase VALUES(1,1,1,?)',(json.dumps(self.boundary),)); spool.db.commit()
        self.store=manifests.Store(self.state/'recovery')
        self.old_pair=self.store.publish(raw,dict(epoch='a'*32,profileSha256=self.profile,ledgerRevision=3,
            bridgeBoundary=self.boundary,dedup={'floorSeq':1,'highSeq':1},selfProof=SEED,
            createdAt='2026-01-01T00:00:00Z',capture={'spool':spool,'checkpointName':self.generation}))
        spool.close()
        self.timer_paused=False
        class Environment:
            def absent(inner): pass
            def timer_state(inner): return {'ActiveState':'active','SubState':'waiting'}
            def pause_timer(inner): self.timer_paused=True
        self.environment=Environment()

    def module(self):
        path=Path(__file__).resolve().parents[1]/'tools/prepare_private_canary_recovery.py'
        self.assertTrue(path.is_file(), 'limited recovery preparation helper missing')
        spec=importlib.util.spec_from_file_location('prepare_canary_test',path)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def inventory(self):
        return {str(p.relative_to(self.state)):p.read_bytes() for p in self.state.rglob('*')
                if p.is_file() and not p.name.endswith('.lock')}

    def test_exact_unknown_quarantined_without_rewriting_bridge_or_audit(self):
        module=self.module(); before=self.inventory(); bridge_before=self.bridge.read_bytes()
        result=module.prepare(self.core,self.bridge,self.environment)
        self.assertEqual(result,{'status':'prepared','pending':4,'ledger_entries':4})
        self.assertTrue(self.timer_paused)
        audit=owner.read_json(self.state/'forward-image-live-send.json')
        self.assertEqual({k:v for k,v in audit.items() if k!='recoveryDisposition'},self.live)
        self.assertTrue(self.core.audit_cleared(audit)); self.assertEqual(audit['state'],'submitted')
        self.assertEqual(self.ledger.read()['entries'][:3],self.prior_ledger['entries'])
        self.assertEqual(self.bridge.read_bytes(),bridge_before)
        self.assertEqual(self.archive.read_bytes(),before['checkpoints/private/current.zip'])
        self.assertFalse((self.state/'recovery-startup.json').exists())
        self.assertEqual((self.state/'private-canary-recovery/recovery-startup.json').read_bytes(),before['recovery-startup.json'])
        for name in ('recovery-ticket.json','recovery-attempts/old.json','media-maintenance.json'):
            self.assertEqual((self.state/name).read_bytes(),before[name])
        current=self.store.current(); self.assertEqual(self.store.previous(),self.old_pair)
        self.assertEqual(current[0]['dedup'],self.old_pair[0]['dedup'])
        self.assertEqual(current[0]['bridgeBoundary'],self.boundary)
        self.assertEqual(current[0]['ledgerRevision'],4)
        with snapshot.copied_database(current[1]) as (db,meta):
            admission=rd.dispositions.admission_for(db,meta['account'],self.ledger)
            rd.admission.validate_database(db,meta['account'],admission)
            self.assertEqual(len(admission['entries']),4)
        completed=self.inventory()
        self.assertEqual(module.prepare(self.core,self.bridge,self.environment),result)
        self.assertEqual(self.inventory(),completed)

    def test_changed_pending_refuses_without_private_state_writes(self):
        module=self.module()
        db=sqlite3.connect(self.root/'source8/control.sqlite')
        extra=('extra-text','extra-key','hash','peer',b'cipher','needs_review',None,None,None,'time')
        db.execute('INSERT INTO outbox VALUES('+','.join('?'*10)+')',extra)
        db.commit(); db.close()
        with zipfile.ZipFile(self.archive,'w') as bundle:
            for key in ('api-token','storage-key','cursor-key'): bundle.writestr(key,('synthetic-'+key).encode())
            bundle.write(self.root/'source8/control.sqlite','accounts/wechat-primary/agent/control.sqlite')
        before=self.inventory(); bridge_before=self.bridge.read_bytes()
        with self.assertRaises(ValueError): module.prepare(self.core,self.bridge,self.environment)
        self.assertEqual(self.inventory(),before); self.assertEqual(self.bridge.read_bytes(),bridge_before)
        self.assertFalse(self.timer_paused)
