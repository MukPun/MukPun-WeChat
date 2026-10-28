import importlib.util
import json
import os
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from unittest import mock

spec=importlib.util.spec_from_file_location('gc_contract',Path(__file__).resolve().parents[1]/'scripts/media_cache_maintenance.py')
gc=importlib.util.module_from_spec(spec);spec.loader.exec_module(gc)


class MediaCacheMaintenance(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'Private snapshot locking uses POSIX')
    def test_startup_collection_keeps_original_and_protected_files(self):
        ss=importlib.util.spec_from_file_location('startup_store',Path(__file__).resolve().parents[1]/'scripts/session_state_store.py')
        state=importlib.util.module_from_spec(ss);ss.loader.exec_module(state)
        store=state.SnapshotStore(Path(self.directory.name)/'snapshots').prepare()
        current=store.path/'private/current.zip'
        with zipfile.ZipFile(current,'w') as z:
            z.writestr('accounts/example/agent/control.sqlite',self.path.read_bytes())
            z.writestr('storage-key',b'synthetic-key-must-be-retained')
        current.chmod(0o600)
        original=hashlib.sha256(current.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'native_must_be_stopped'):
            gc.maintain_store(store,state)
        result=gc.maintain_store(store,state,native_stopped=True)
        self.assertEqual(result['sources_removed'],1)
        self.assertEqual(hashlib.sha256((store.path/'private/previous.zip').read_bytes()).hexdigest(),original)
        with zipfile.ZipFile(current) as z:
            self.assertEqual(z.read('storage-key'),b'synthetic-key-must-be-retained')
        # A failing rewrite cannot change the current recoverable snapshot.
        before=current.read_bytes()
        with mock.patch.object(gc,'rewrite_snapshot',side_effect=ValueError('injected')):
            with self.assertRaisesRegex(ValueError,'injected'):
                gc.maintain_store(store,state,native_stopped=True)
        self.assertEqual(current.read_bytes(),before)
        self.assertFalse((store.path/'.media-clean.zip').exists())

    def test_snapshot_rewrite_preserves_other_files_and_requires_offline_copy(self):
        ss=importlib.util.spec_from_file_location('gc_store',Path(__file__).resolve().parents[1]/'scripts/session_state_store.py')
        state=importlib.util.module_from_spec(ss);ss.loader.exec_module(state)
        source=Path(self.directory.name)/'old.zip';dest=Path(self.directory.name)/'candidate.zip'
        with zipfile.ZipFile(source,'w') as z:
            z.writestr('accounts/example/agent/control.sqlite',self.path.read_bytes())
            z.writestr('storage-key',b'private-test-key-must-not-change')
            z.writestr('profiles/opaque.bin',b'opaque-profile-must-not-change')
        with self.assertRaisesRegex(ValueError,'offline_copy'):gc.rewrite_snapshot(source,dest,state)
        result=gc.rewrite_snapshot(source,dest,state,offline=True)
        self.assertEqual(result['sources_removed'],1)
        self.assertEqual(result['protected_files_verified'],2)
        with zipfile.ZipFile(dest) as z:
            self.assertEqual(z.read('storage-key'),b'private-test-key-must-not-change')
            self.assertEqual(z.read('profiles/opaque.bin'),b'opaque-profile-must-not-change')

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'control.sqlite'
        db=sqlite3.connect(self.path)
        db.executescript('''
        CREATE TABLE media_sources(id TEXT,name TEXT,media_type TEXT,byte_length INTEGER,sha256 TEXT,created_at TEXT,payload BLOB);
        CREATE TABLE media_jobs(id TEXT,kind TEXT,state TEXT,phase TEXT,data_json TEXT);
        CREATE TABLE media_assets(id TEXT,message_id TEXT,kind TEXT,metadata_json TEXT,payload BLOB);
        CREATE TABLE outbox(id TEXT,payload BLOB,state TEXT);
        CREATE TABLE events(sequence INTEGER,data_json TEXT);
        CREATE TABLE runtime(key TEXT,value BLOB);
        INSERT INTO outbox VALUES('must-retain',X'010203','unknown');
        INSERT INTO events VALUES(17,'anchor-must-retain');
        INSERT INTO runtime VALUES('credential-fingerprint',X'0102');
        ''')
        data={'sourceId':'upload_source','result':{'nativeReceipt':{'status':'server_accepted','serviceCode':0,
            'clientIdMatches':True,'serverMessageId':'123'},'profileProjection':'complete','uploadReadbackVerified':True,
            'imagePreparation':{'sourceSha256':'a'*64},'messageIds':['message']}}
        db.execute('INSERT INTO media_sources VALUES(?,?,?,?,?,?,?)',('upload_source','bridge-image.png','image/png',
            1048576,'a'*64,'2020-01-01T00:00:00+00:00',b'x'*1048576))
        db.execute('INSERT INTO media_jobs VALUES(?,?,?,?,?)',('job','send','accepted','completed',json.dumps(data)))
        db.execute('INSERT INTO media_assets VALUES(?,?,?,?,?)',('asset','message','image',json.dumps({'state':'available'}),b'x'*65536))
        db.commit();self.before=gc._digest_tables(db);db.close()

    def tearDown(self):self.directory.cleanup()

    def test_only_verified_completed_cache_is_removed_and_database_compacts(self):
        old_size=self.path.stat().st_size
        result=gc.prune_database(self.path,offline=True)
        self.assertEqual(result['sources_removed'],1)
        self.assertEqual(result['asset_bytes_released'],0)
        db=sqlite3.connect(self.path)
        self.assertEqual(gc._digest_tables(db),self.before)
        metadata,payload=db.execute('SELECT metadata_json,payload FROM media_assets').fetchone()
        self.assertEqual(json.loads(metadata)['state'],'available');self.assertEqual(payload,b'x'*65536)
        db.close()
        self.assertLess(self.path.stat().st_size,old_size)

    def test_requires_explicit_offline_copy(self):
        with self.assertRaisesRegex(ValueError,'offline_copy'):gc.prune_database(self.path)

    def test_any_unfinished_reference_blocks_source_cleanup(self):
        db=sqlite3.connect(self.path)
        db.execute('INSERT INTO media_jobs VALUES(?,?,?,?,?)',('pending','send','running','submitting',json.dumps({'sourceId':'upload_source'})))
        db.commit();db.close()
        self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],0)

    def test_incomplete_projection_and_protected_source_are_retained(self):
        self.assertEqual(gc.prune_database(self.path,offline=True,protected_sources={'upload_source'})['sources_removed'],0)
        db=sqlite3.connect(self.path)
        raw=json.loads(db.execute('SELECT data_json FROM media_jobs').fetchone()[0])
        raw['result']['profileProjection']='pending'
        db.execute('UPDATE media_jobs SET data_json=?',(json.dumps(raw),));db.commit();db.close()
        self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],0)

    def test_fresh_cache_is_not_expired(self):
        self.assertEqual(gc.prune_database(self.path,offline=True,now=1577836801)['sources_removed'],0)
