"""Synthetic recovery integration; no owner, account, network or model calls."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from scripts import restore_admission as admission
from test_native_migration import guest
from test_recovery_snapshot import fixture


class GuestAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for key in ('api-token', 'storage-key', 'cursor-key'):
            (self.root / key).write_bytes(('synthetic-' + key).encode())
            (self.root / key).chmod(0o600)
        self.profile = admission.profile_sha256({k: hashlib.sha256((self.root/k).read_bytes()).hexdigest()
            for k in ('api-token', 'storage-key', 'cursor-key')})
        path = self.root/'accounts/wechat-primary/agent/control.sqlite'
        path.parent.mkdir(parents=True)
        self.db = sqlite3.connect(path)
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE outbox('+admission.TEXT_COLUMNS+')')
        self.db.execute('CREATE TABLE media_jobs('+admission.MEDIA_COLUMNS+')')
        self.text = ('text', 'key', 'hash', 'peer', b'cipher', 'needs_review', None, None, None, 'time')
        self.media = ('media', 'key2', 'hash', 'send', 'needs_review', 'submitting', 1, 0, 0, None, '{}', b'cipher', 'time', 'time')
        self.pre = ('pre', 'key3', 'hash', 'send', 'needs_review', 'preparing', 1, 0, 0, 'media_processing_failed', '{}', b'cipher', 'time', 'time')
        self.db.execute('INSERT INTO outbox VALUES('+','.join('?'*10)+')',self.text)
        for row in (self.media,self.pre):
            self.db.execute('INSERT INTO media_jobs VALUES('+','.join('?'*14)+')',row)
        self.db.commit()
        self.value = dict(schemaVersion=1,profileSha256=self.profile,ledgerRevision=3,entries=[
            dict(kind=k,recordSha256=admission.fingerprint('wechat-primary',k,r),disposition=d,evidenceKind=e,evidenceSha256='a'*64)
            for k,r,d,e in [('text',self.text,'reviewed_not_delivered','operator_review'),
                ('media',self.media,'unconfirmed_quarantined','legacy_hold'),
                ('media',self.pre,'rejected_before_submit','native_pre_submit')]])
        self.patch = patch.object(guest.empty,'DATA',self.root)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_explicit_config_and_unchanged_inventory(self):
        config=dict(schemaVersion=1,restore=True,offline=True,runtimeSeconds=90,account=guest.ACCOUNT,restoreAdmission=self.value)
        try:
            guest.validate_config(config)
        except ValueError:
            self.fail('new explicit admission is not supported')
        guest.review_restored_outbox(restore_admission=self.value)
        self.assertEqual(self.db.execute('SELECT * FROM outbox').fetchone(),self.text)
        with self.assertRaises(guest.RuntimeFailure): guest.review_restored_outbox()
        for bad in ({**config,'restore':False},{**config,'quarantinedSend':dict(schemaVersion=1,jobSha256='b'*64)}):
            with self.assertRaises(ValueError): guest.validate_config(bad)

    def test_lineage_checked_before_database(self):
        self.assertTrue(hasattr(guest,'restore_admission'), 'guest lacks pure admission module')
        with patch.object(guest.restore_admission,'validate_database') as check:
            with self.assertRaises(guest.RuntimeFailure):
                guest.review_restored_outbox(restore_admission={**self.value,'profileSha256':'0'*64})
            check.assert_not_called()

    def test_changed_missing_additional_and_wal_rows_reject(self):
        self.assertTrue(hasattr(guest,'restore_admission'), 'guest lacks pure admission module')
        for sql,undo in (("UPDATE media_jobs SET updated_at='changed' WHERE id='media'", "UPDATE media_jobs SET updated_at='time' WHERE id='media'"),
                         ("DELETE FROM outbox", None),
                         ("INSERT INTO outbox SELECT 'new',idempotency_key,payload_hash,destination,payload,'queued',client_id,server_id,error_code,created_at FROM outbox", "DELETE FROM outbox WHERE id='new'")):
            self.db.execute(sql); self.db.commit()
            with self.assertRaises(guest.RuntimeFailure): guest.review_restored_outbox(restore_admission=self.value)
            if undo: self.db.execute(undo)
            else: self.db.execute('INSERT INTO outbox VALUES('+','.join('?'*10)+')',self.text)
            self.db.commit()
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute("UPDATE media_jobs SET updated_at='time' WHERE id='media'")
        self.db.execute("INSERT INTO outbox SELECT 'new',idempotency_key,payload_hash,destination,payload,'queued',client_id,server_id,error_code,created_at FROM outbox")
        self.db.commit()
        with self.assertRaises(guest.RuntimeFailure): guest.review_restored_outbox(restore_admission=self.value)

    def test_only_config_read_has_larger_limit(self):
        self.assertEqual(getattr(guest,'CONFIG_LIMIT',None),65536)
        self.assertEqual(guest.JSON_LIMIT,4096)
        with patch.object(guest,'_read_json', return_value={}) as read, patch.object(guest,'validate_config'):
            guest.read_config(); read.assert_called_once_with(guest.CONFIG, maximum=65536)
        with patch.object(guest,'_read_json', return_value={}) as read, patch.object(guest,'validate_command'):
            guest.read_command(); read.assert_called_once_with(guest.COMMAND)


class CopiedDatabaseTests(unittest.TestCase):
    def test_private_context_and_unchanged_inspect_contract(self):
        from scripts import recovery_snapshot
        self.assertTrue(hasattr(recovery_snapshot,'copied_database'), 'root-only copied database API missing')
        with tempfile.TemporaryDirectory() as directory:
            path=fixture(Path(directory),wal=True,pending=True)
            before=path.read_bytes()
            with recovery_snapshot.copied_database(path) as (db,meta):
                self.assertEqual(meta['account'],'wechat-primary')
                self.assertEqual(len(admission.pending_records(db)),1)
                with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM outbox')
            result=recovery_snapshot.inspect(path)
            self.assertEqual(set(result),{'keys','profileSha256','snapshotSha256','nativeBoundary','pending','selfProof'})
            self.assertEqual(path.read_bytes(),before)

class DispositionTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from contextlib import contextmanager
        from scripts import session_runtime
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.snapshot=fixture(self.root,seed=False)
        self.pinned=SimpleNamespace(unchanged=lambda:True)
        @contextmanager
        def copied(pinned):
            yield self.snapshot
        self.owner=SimpleNamespace(STATE=self.root,checkpoint_copy=copied,read_private=session_runtime.read_private,
            read_json=session_runtime.read_json,write_json=session_runtime.write_json,private_directory=session_runtime.private_directory)
        self.core=SimpleNamespace(owner=self.owner)
        self.audit=dict(state='unknown',key='b'*32,requestId='a'*32,destinationSha256=hashlib.sha256(b'peer').hexdigest(),
            sourceSha256='c'*64,bytes=100,jobId='job_11111111-2222-4333-8444-555555555555',code='image_response')

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.recovery_dispositions'),'root disposition integration missing')
        from scripts import recovery_dispositions
        return recovery_dispositions

    def row(self, phase='submitting',state='needs_review',error='media_processing_failed',extra=None):
        data={'threadId':'peer','sourceId':'upload-test','result':{'imagePreparation':{'sourceSha256':'c'*64},'profileProjection':'not_applicable'}}
        if extra: data['result'].update(extra)
        return (self.audit['jobId'],'send:'+self.audit['key'],'d'*64,'send',state,phase,1,0,0,error,json.dumps(data),b'cipher','time','time')

    def archive(self,row,kind='media'):
        import zipfile
        db=sqlite3.connect(self.root/'source8/control.sqlite')
        db.execute('INSERT INTO '+('media_jobs' if kind=='media' else 'outbox')+' VALUES('+','.join('?'*len(row))+')',row)
        db.commit();db.close()
        with zipfile.ZipFile(self.snapshot,'w') as z:
            for key in ('api-token','storage-key','cursor-key'):z.writestr(key,('synthetic-'+key).encode())
            z.write(self.root/'source8/control.sqlite','accounts/wechat-primary/agent/control.sqlite')

    def test_unknown_exact_row_idempotent_and_accepted_no_ledger(self):
        m=self.module(); self.archive(self.row())
        first=m.record_native_outcome(self.core,self.pinned,self.audit,'media','unconfirmed_quarantined')
        second=m.record_native_outcome(self.core,self.pinned,self.audit,'media','unconfirmed_quarantined')
        self.assertEqual(first,second); self.assertTrue(first['matched'])
        self.assertEqual(m.ledger_for(self.core,first['profileSha256']).read()['revision'],1)
        with patch.object(self.owner,'checkpoint_copy',side_effect=AssertionError('accepted requires no copy')):
            self.assertIsNone(m.record_native_outcome(self.core,self.pinned,{},'media','accepted_verified'))
        self.assertEqual(self.audit['state'],'unknown')

    def test_missing_job_is_explicit_and_mismatch_never_records(self):
        m=self.module()
        result=m.record_native_outcome(self.core,self.pinned,self.audit,'media','unconfirmed_quarantined')
        self.assertFalse(result['matched']);self.assertFalse((self.root/'send-dispositions.json').exists())
        self.archive(self.row())
        with self.assertRaisesRegex(ValueError,'^recovery_disposition$'):
            m.record_native_outcome(self.core,self.pinned,{**self.audit,'key':'e'*32},'media','unconfirmed_quarantined')

    def test_pre_submit_requires_positive_full_row_evidence(self):
        m=self.module(); self.archive(self.row(phase='preparing'))
        result=m.record_native_outcome(self.core,self.pinned,self.audit,'media','rejected_before_submit')
        self.assertEqual(result['classification'],'rejected_before_submit')
        db=sqlite3.connect(self.root/'source8/control.sqlite');db.execute('DELETE FROM media_jobs');db.commit();db.close()
        self.archive(self.row(phase='preparing',error=None))
        with self.assertRaises(ValueError):m.record_native_outcome(self.core,self.pinned,self.audit,'media','rejected_before_submit')

    def test_unknown_text_and_capacity_fail_closed(self):
        m=self.module();audit={**self.audit,'sendId':'text-test'};audit.pop('jobId')
        row=('text-test',audit['key'],'d'*64,'peer',b'cipher','needs_review',None,None,None,'time')
        self.archive(row,'text')
        result=m.record_native_outcome(self.core,self.pinned,audit,'text','unconfirmed_quarantined')
        self.assertTrue(result['matched'])
        ledger=m.ledger_for(self.core,result['profileSha256'])
        value=ledger.read(); value['entries']=[{**value['entries'][0],'recordSha256':hashlib.sha256(str(i).encode()).hexdigest()} for i in range(128)]
        self.owner.write_json(ledger.path,value)
        self.assertFalse(m.output_available(self.core))
        with self.assertRaises(ValueError):m.record_native_outcome(self.core,self.pinned,audit,'text','unconfirmed_quarantined')

    def test_legacy_import_exact_hold_backup_and_idempotency(self):
        m=self.module();self.archive(self.row())
        audit={**self.audit,'code':'image_needs_review'}
        hold=dict(schemaVersion=1,mode='new_images',auditSha256=hashlib.sha256(admission._encoded(audit)).hexdigest(),
                  jobId=audit['jobId'],nativeClientPid=123,nativeClientTicks=456,approvedAt=1)
        self.owner.write_json(self.root/'forward-image-send.json',audit)
        self.owner.write_json(self.root/'forward-image-hold.json',hold)
        before=self.snapshot.read_bytes()
        first=m.import_legacy(self.core,self.snapshot,self.root/'backup')
        second=m.import_legacy(self.core,self.snapshot,self.root/'backup')
        self.assertEqual(first['imported'],1);self.assertEqual(second['imported'],0)
        self.assertEqual(self.snapshot.read_bytes(),before)
        self.assertEqual(self.owner.read_json(self.root/'forward-image-send.json'),audit)
        self.assertTrue(list((self.root/'backup').glob('*/forward-image-send.json')))
        self.owner.write_json(self.root/'forward-image-send.json',{**audit,'bytes':101})
        with self.assertRaises(ValueError):m.import_legacy(self.core,self.snapshot,self.root/'backup')

    def test_terminal_outcome_uses_bound_marker_at_full_exception_capacity(self):
        m=self.module();row=self.row(phase='preparing',state='failed');self.archive(row)
        with m.snapshots.copied_database(self.snapshot) as (db,meta):
            ledger=m.ledger_for(self.core,meta['profileSha256']);value=ledger.read()
            entry=m.dispositions.entry(meta['account'],'media',row,'rejected_before_submit','native_pre_submit','a'*64)
            value['entries']=[{**entry,'recordSha256':hashlib.sha256(str(i).encode()).hexdigest()} for i in range(128)]
        self.owner.write_json(ledger.path,value);before=ledger.path.read_bytes()
        result=m.record_native_outcome(self.core,self.pinned,self.audit,'media','rejected_before_submit')
        self.assertFalse(result['admissionRequired']);self.assertTrue(result['matched'])
        self.assertEqual(result['snapshotSha256'],hashlib.sha256(self.snapshot.read_bytes()).hexdigest())
        self.assertEqual(result['recordSha256'],admission.fingerprint('wechat-primary','media',row))
        self.assertTrue(m.audit_cleared(self.core,{**self.audit,'recoveryDisposition':result}))
        self.assertEqual(ledger.path.read_bytes(),before)
        self.assertFalse(m.audit_cleared(self.core,{**self.audit,'bytes':101,'recoveryDisposition':result}))
        for retryable in (1,None,'0'):
            db=sqlite3.connect(self.root/'source8/control.sqlite');db.execute('DELETE FROM media_jobs');db.commit();db.close()
            changed=list(row);changed[8]=retryable;self.archive(tuple(changed))
            with self.assertRaises(ValueError):m.record_native_outcome(self.core,self.pinned,self.audit,'media','unconfirmed_quarantined')
        self.assertEqual(ledger.path.read_bytes(),before)

    def test_legacy_terminal_sidecar_preserves_audit_without_exception_entry(self):
        m=self.module();self.archive(self.row(phase='preparing',state='failed'))
        name='forward-image-live-send.json'
        audit={**self.audit,'state':'rejected','verification':'sdk_failed_preparing_before_wechat_submission'}
        self.owner.write_json(self.root/name,audit);before=(self.root/name).read_bytes()
        events=[];sync=os.fsync;write=m.write_evidence
        root_inode=self.root.stat().st_ino
        def observed_sync(fd):
            info=os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino==root_inode:events.append('parent_sync')
            sync(fd)
        def observed_write(*args):
            events.append('sidecar_write');write(*args);events.append('complete')
        with patch.object(m.os,'fsync',side_effect=observed_sync),patch.object(m,'write_evidence',side_effect=observed_write):
            self.assertEqual(m.import_legacy(self.core,self.snapshot,self.root/'backup'),dict(imported=0,matched=1))
        self.assertEqual(events[-3:],['sidecar_write','parent_sync','complete'])
        self.assertFalse((self.root/m.LEDGER).exists());self.assertTrue(m.audit_cleared(self.core,audit))
        self.assertEqual((self.root/name).read_bytes(),before)
        saved=self.owner.read_json(self.root/(name+'.recovery.json'))['marker']
        self.assertFalse(saved['admissionRequired']);self.assertTrue(saved['matched'])
        self.assertEqual(saved['snapshotSha256'],hashlib.sha256(self.snapshot.read_bytes()).hexdigest())
        self.assertEqual(m.import_legacy(self.core,self.snapshot,self.root/'backup'),dict(imported=0,matched=1))
        self.assertEqual(len(list(self.root.glob('*.recovery.json'))),1)
        self.owner.write_json(self.root/name,{**audit,'bytes':101})
        self.assertFalse(m.audit_cleared(self.core,{**audit,'bytes':101}))

    def test_legacy_terminal_directory_sync_failure_blocks_import(self):
        m=self.module();self.archive(self.row(phase='preparing',state='failed'))
        name='forward-image-live-send.json'
        audit={**self.audit,'state':'rejected','verification':'sdk_failed_preparing_before_wechat_submission'}
        self.owner.write_json(self.root/name,audit);before=(self.root/name).read_bytes()
        sync=os.fsync;root_inode=self.root.stat().st_ino
        def fail_parent(fd):
            info=os.fstat(fd)
            if (stat.S_ISDIR(info.st_mode) and info.st_ino==root_inode
                    and (self.root/(name+'.recovery.json')).exists()):raise OSError('private failure detail')
            sync(fd)
        with patch.object(m.os,'fsync',side_effect=fail_parent),self.assertRaisesRegex(ValueError,'^recovery_disposition$'):
            m.import_legacy(self.core,self.snapshot,self.root/'backup')
        self.assertEqual((self.root/name).read_bytes(),before)
        self.assertFalse((self.root/m.LEDGER).exists())

    def test_combined_legacy_inventory_restores_without_native_row_changes(self):
        m=self.module();self.archive(self.row())
        held={**self.audit,'code':'image_needs_review'}
        hold=dict(schemaVersion=1,mode='new_images',auditSha256=m._evidence(held),jobId=held['jobId'],
                  nativeClientPid=123,nativeClientTicks=456,approvedAt=1)
        text={k:v for k,v in self.audit.items() if k not in {'jobId','sourceSha256'}}
        text.update(state='rejected',review='reviewed_send_not_delivered',key='e'*32,sendId='text-reviewed',textSha256='f'*64)
        self.archive((text['sendId'],text['key'],'d'*64,'peer',b'cipher','needs_review',None,None,None,'time'),'text')
        pre={**self.audit,'state':'rejected','key':'f'*32,'jobId':'job-pre',
             'verification':'sdk_failed_preparing_before_wechat_submission'}
        row=list(self.row(phase='preparing'));row[0]=pre['jobId'];row[1]='send:'+pre['key']
        self.archive(tuple(row))
        for name,value in [('forward-image-send.json',held),('forward-image-hold.json',hold),
                           ('forward-send.json',text),('forward-image-live-send.json',pre)]:
            self.owner.write_json(self.root/name,value)
        originals={n:(self.root/n).read_bytes() for n in ('forward-image-send.json','forward-image-hold.json','forward-send.json','forward-image-live-send.json')}
        snapshot=self.snapshot.read_bytes()
        self.assertEqual(m.import_legacy(self.core,self.snapshot,self.root/'backup'),dict(imported=3,matched=3))
        for audit in (held,text,pre):self.assertTrue(m.audit_cleared(self.core,audit))
        with m.snapshots.copied_database(self.snapshot) as (db,metadata):
            approval=m.dispositions.admission_for(db,'wechat-primary',m.ledger_for(self.core,metadata['profileSha256']))
        restored=self.root/'restored';restored.mkdir(mode=0o700)
        m.snapshots.state.restore_archive(self.snapshot,restored)
        database=restored/'accounts/wechat-primary/agent/control.sqlite';before=database.read_bytes()
        with patch.object(guest.empty,'DATA',restored):
            guest.review_restored_outbox(restore_admission=approval)
            with self.assertRaises(guest.RuntimeFailure):guest.review_restored_outbox()
        self.assertEqual(database.read_bytes(),before);self.assertEqual(self.snapshot.read_bytes(),snapshot)
        self.assertEqual({n:(self.root/n).read_bytes() for n in originals},originals)
        self.assertEqual(m.import_legacy(self.core,self.snapshot,self.root/'backup')['imported'],0)

    def test_legacy_import_cas_rejects_changed_audits_snapshot_or_ledger(self):
        m=self.module();self.archive(self.row())
        audit={**self.audit,'state':'rejected','review':'reviewed_send_not_delivered'}
        self.owner.write_json(self.root/'forward-image-live-send.json',audit)
        baseline=m._legacy_sources(self.core)
        with m.snapshots.copied_database(self.snapshot) as (db,metadata):profile=metadata['profileSha256']
        empty=m.ledger_for(self.core,profile).read()
        cases=[patch.object(m,'_legacy_sources',side_effect=[baseline,{**baseline,'forward-voice-send.json':b'{}'}]),
               patch.object(m.snapshots,'_digest',side_effect=[hashlib.sha256(self.snapshot.read_bytes()).hexdigest(),'0'*64]),
               patch.object(m.dispositions.Ledger,'_read',side_effect=[empty,{**empty,'revision':1}])]
        for changed in cases:
            with changed,self.assertRaisesRegex(ValueError,'^recovery_disposition$'):
                m.import_legacy(self.core,self.snapshot,self.root/'backup')
            self.assertFalse((self.root/m.LEDGER).exists())
        self.assertTrue(list((self.root/'backup').glob('*/binding.json')))

    def test_legacy_rejected_label_is_not_proof_and_evidence_stays_bound(self):
        m=self.module();self.archive(self.row())
        self.owner.write_json(self.root/'forward-image-live-send.json',{**self.audit,'state':'rejected'})
        self.assertEqual(m.import_legacy(self.core,self.snapshot,self.root/'backup')['imported'],0)
        result=m.record_native_outcome(self.core,self.pinned,self.audit,'media','unconfirmed_quarantined')
        marked={**self.audit,'recoveryDisposition':result}
        self.assertTrue(m.audit_cleared(self.core,marked))
        self.assertFalse(m.audit_cleared(self.core,{**marked,'bytes':101}))
        self.assertFalse(m.audit_cleared(self.core,{**marked,'recoveryDisposition':{**result,'evidenceSha256':'0'*64}}))


class OwnerCheckpointTests(unittest.TestCase):
    def test_fresh_status_checkpoint_and_pinned_copy(self):
        from types import SimpleNamespace
        from scripts import session_runtime as owner
        self.assertTrue(hasattr(owner,'checkpoint_copy'),'fresh owner checkpoint copy API missing')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);control=root/'control';control.mkdir(mode=0o700)
            private=root/'checkpoints/private';private.mkdir(parents=True,mode=0o700)
            snapshot=fixture(private)
            snapshot.rename(private/'current.zip')
            calls=[]; original=owner.write_json
            old=dict(sequence=4,busy=False,done=True,ok=True,ready=True,accounts=1,connected=False,records=[])
            owner.write_json(control/'status.json',old)
            def write(path,value):
                calls.append(value)
                self.assertEqual(value,dict(sequence=5,action='status',allowFreshLogin=False))
                original(path,value)
                copy=private/'new.zip';shutil.copyfile(private/'current.zip',copy);copy.chmod(0o600);copy.replace(private/'current.zip')
                original(control/'status.json',{**old,'sequence':5,'code':'ok','records':[
                    {'index':1,'record':{'stage':'runtime_status','accounts':1,'connected':False}},
                    {'index':2,'record':{'stage':'runtime_result','ok':True,'code':'ok'}}]})
            with patch.object(owner,'STATE',root),patch.object(owner,'CONTROL',control),patch.object(owner,'owner_active',return_value=True),patch.object(owner,'write_json',side_effect=write):
                with owner.checkpoint_copy(SimpleNamespace(unchanged=lambda:True)) as path:
                    self.assertEqual(path.read_bytes(),(private/'current.zip').read_bytes())
                    self.assertNotEqual(path,private/'current.zip')
                self.assertFalse(path.exists())
            self.assertEqual(len(calls),1)

    def test_stale_checkpoint_is_rejected(self):
        from scripts import session_runtime as owner
        self.assertTrue(hasattr(owner,'checkpoint_copy'),'fresh owner checkpoint copy API missing')
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);control=root/'control';control.mkdir(mode=0o700)
            private=root/'checkpoints/private';private.mkdir(parents=True,mode=0o700)
            fixture(private).rename(private/'current.zip')
            status=dict(sequence=1,busy=False,done=True,ok=True,ready=True,accounts=1,code='ok',records=[])
            with patch.object(owner,'STATE',root),patch.object(owner,'CONTROL',control),patch.object(owner,'owner_active',return_value=True),patch.object(owner,'write_json'),patch.object(owner,'read_json',side_effect=[status,{**status,'sequence':2}]):
                with self.assertRaises(Exception):
                    with owner.checkpoint_copy(SimpleNamespace(unchanged=lambda:True)):pass


class PackagingTests(unittest.TestCase):
    def test_actual_isolated_paths_in_copied_worker_view(self):
        from scripts import session_runtime_host as host
        self.assertIn('restore_admission.py',host.MODULES)
        self.assertNotIn('send_dispositions.py',host.MODULES)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=Path(__file__).resolve().parents[1]
            for folder in ('scripts','wechat_bridge'):
                shutil.copytree(source/folder,root/folder,ignore=shutil.ignore_patterns('__pycache__'))
            code="import importlib.util,sys; p=sys.argv[1]; s=importlib.util.spec_from_file_location('isolated',p); m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m)"
            for name in ('session_runtime.py','session_runtime_host.py','run_private_forwarder.py','recovery_dispositions.py','resume_quarantined_native.py','recovery_snapshot.py'):
                extra=";m.companion('recovery_snapshot')" if name=='session_runtime.py' else ''
                result=subprocess.run([sys.executable,'-I','-B','-c',code+extra,str(root/'scripts'/name)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)


class GateTests(unittest.TestCase):
    def test_disposition_before_gate_and_failure_preserves_uncertainty(self):
        from scripts import run_private_forwarder as core
        self.assertTrue(hasattr(core,'settle_outcome'),'record-before-gate integration missing')
        calls=[];audit={'state':'unknown','key':'a'*32}
        marker={'classification':'unconfirmed_quarantined','matched':False}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(core.recovery_dispositions,'record_native_outcome',side_effect=lambda *a: calls.append('ledger') or marker),patch.object(core.owner,'write_json',side_effect=lambda *a:calls.append('gate')),patch.object(core.recovery_dispositions.os,'fsync',side_effect=lambda fd:calls.append('parent_sync')):
                core.settle_outcome(None,Path(directory)/'audit.json',audit,'text','unconfirmed_quarantined')
        self.assertEqual(calls,['ledger','gate','parent_sync'])
        self.assertEqual(audit['state'],'unknown')
        with patch.object(core.recovery_dispositions,'record_native_outcome',side_effect=ValueError('recovery_disposition')),patch.object(core.owner,'write_json') as write:
            with self.assertRaises(ValueError):core.settle_outcome(None,Path('/synthetic'),{'state':'unknown'},'media','unconfirmed_quarantined')
            write.assert_not_called()

    def test_runtime_marker_parent_sync_failure_is_fixed_and_fails_closed(self):
        from scripts import run_private_forwarder as core
        sync=os.fsync
        def fail_parent(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError('private failure detail')
            sync(fd)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audit.json'
            with patch.object(core.recovery_dispositions,'record_native_outcome',return_value={'matched':False}),patch.object(core.recovery_dispositions.os,'fsync',side_effect=fail_parent):
                with self.assertRaisesRegex(ValueError,'^recovery_disposition$'):
                    core.settle_outcome(None,path,{'state':'unknown'},'text','unconfirmed_quarantined')
            self.assertEqual(core.owner.read_json(path)['state'],'unknown')

    def test_evidence_parent_rejects_symlink_or_nonprivate_directory_before_write(self):
        from scripts import run_private_forwarder as core
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);private=root/'private';private.mkdir(mode=0o700)
            alias=root/'alias';alias.symlink_to(private,target_is_directory=True)
            shared=root/'shared';shared.mkdir(mode=0o755)
            for parent in (alias,shared):
                with patch.object(core.owner,'write_json') as write,self.assertRaisesRegex(ValueError,'^recovery_disposition$'):
                    core.recovery_dispositions.write_evidence(core,parent/'audit.json',{'state':'unknown'})
                write.assert_not_called()


class CapacityPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import time
        from dataclasses import asdict
        from wechat_bridge.durable import Spool
        from wechat_bridge.forward_gateway import Gateway
        from wechat_bridge.continuous import TextBridge
        from wechat_bridge.protocol import Config,NativeMessage
        self.temp=tempfile.TemporaryDirectory()
        self.state=Spool(Path(self.temp.name)/'bridge.sqlite')
        self.gateway=Gateway('account','peer',self.state,'profile',lambda value:None,image_output=True)
        self.bridge=TextBridge(Config('account','self','room@chatroom','synthetic',send_interval=0),'peer',self.state,self.gateway,image_output=True)
        self.bridge.connected()
        incoming=NativeMessage('account','incoming','peer','peer','question',int(time.time()))
        self.gateway.feed(dict(kind='event',cursor='1',anchor='a'*64,data=asdict(incoming)))
        message=await self.gateway.next_message();self.event=self.bridge.ingest(message)
        await self.bridge.event_delivered(message,True)
        self.calls=[]

    async def asyncTearDown(self):
        self.state.close();self.temp.cleanup()

    def action(self,message,echo='capacity'):
        return dict(action='send_private_msg',params=dict(user_id=self.event['user_id'],message=message),echo=echo)

    async def test_text_capacity_is_policy_failure_and_cached_without_retry(self):
        from wechat_bridge.protocol import Receipt
        async def send(destination,body,key):
            self.calls.append(key);return Receipt('account',destination,'','recovery_capacity')
        self.gateway.send_text=send
        action=self.action('answer')
        first=await self.bridge.action(action);second=await self.bridge.action(action)
        self.assertEqual(first['retcode'],1403);self.assertEqual(first['message'],'recovery_capacity')
        self.assertEqual(first,second);self.assertEqual(len(self.calls),1)

    async def test_partial_text_and_forged_capacity_stay_unknown(self):
        from wechat_bridge.protocol import Receipt
        import uuid
        async def send(destination,body,key):
            self.calls.append(key)
            return Receipt('account',destination,str(uuid.uuid4()),'accepted','1','2') if len(self.calls)==1 else Receipt('account',destination,'','recovery_capacity')
        self.gateway.send_text=send
        action=self.action('x'*10000)
        self.assertEqual((await self.bridge.action(action))['retcode'],1408)
        self.assertEqual((await self.bridge.action(action))['retcode'],1408)
        self.assertEqual(len(self.calls),2)
        async def forged(destination,body,key):return Receipt('account',destination,'existing','recovery_capacity','','2')
        self.gateway.send_text=forged
        self.assertEqual((await self.bridge.action(self.action('another','forged')))['retcode'],1408)

    async def test_voice_capacity_uses_same_bound_cached_policy(self):
        from test_onebot_voice_output import wav,segment
        from wechat_bridge import voice_output
        self.bridge.voice_output=True
        async def send(destination,audio,key):
            self.calls.append(key)
            return dict(account='account',destination=destination,job_id='',state='recovery_capacity',
                        server_id='',source_sha256=audio['sha256'],projection_verified=False)
        self.gateway.send_voice=send
        action=self.action([segment(wav())],'voice-capacity')
        answer=await self.bridge.action(action)
        self.assertEqual(answer['retcode'],1403);self.assertEqual(answer['message'],'recovery_capacity')
        self.assertEqual(await self.bridge.action(action),answer);self.assertEqual(len(self.calls),1)
        audio=voice_output.from_segment(segment(wav()))
        self.assertTrue(voice_output.receipt_bound(await send('peer',audio,'unused'),
                        'account','peer',audio['sha256']))

    async def test_media_capacity_and_unknown_keep_distinct_durable_results(self):
        import base64
        from test_onebot_image_output import png
        from wechat_bridge import image_output
        async def send(destination,image,key):
            self.calls.append(key)
            return dict(account='account',destination=destination,job_id='',state='recovery_capacity',server_id='',source_sha256=image['sha256'],projection_verified=False)
        self.gateway.send_image=send
        message=[dict(type='image',data=dict(file='base64://'+base64.b64encode(png()).decode()))]
        action=self.action(message)
        answer=await self.bridge.action(action)
        self.assertEqual(answer['retcode'],1403);self.assertEqual(answer['message'],'recovery_capacity')
        self.assertEqual(await self.bridge.action(action),answer);self.assertEqual(len(self.calls),1)
        receipt=await send('peer',image_output.payload(png()),'unused')
        self.assertTrue(image_output.receipt_bound(receipt,'account','peer',receipt['source_sha256']))
        for changes in ({'job_id':'job_11111111-1111-1111-1111-111111111111'},{'server_id':'1'},{'projection_verified':True}):
            self.assertFalse(image_output.receipt_bound({**receipt,**changes},'account','peer',receipt['source_sha256']))
        async def unknown(destination,image,key):return {**receipt,'state':'unknown'}
        self.gateway.send_image=unknown
        self.assertEqual((await self.bridge.action(self.action(message,'unknown')))['retcode'],1408)
