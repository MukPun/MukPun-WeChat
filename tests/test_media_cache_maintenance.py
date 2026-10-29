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

    def voice(self):
        data={'sourceId':'upload_source','serverMessageId':'124','result':{
            'nativeReceipt':{'status':'server_accepted','serviceCode':0,'clientIdMatches':True,'serverMessageId':'124'},
            'profileProjection':'complete','officialMediaWriteback':'complete',
            'chunks':[{'index':0,'state':'accepted','durationMs':60000,'serverMessageId':'123'},
                      {'index':1,'state':'accepted','durationMs':1,'serverMessageId':'124'}],
            'messageIds':['one','two']}}
        self.write_voice(data)
        return data

    def write_voice(self,data,kind='send'):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE media_sources SET name='bridge-voice.wav',media_type='audio/wav'")
            db.execute('UPDATE media_jobs SET kind=?,data_json=?',(kind,json.dumps(data)))

    def test_completed_voice_is_removed_preserving_every_other_table(self):
        self.voice()
        with sqlite3.connect(self.path) as db: before=gc._digest_tables(db)
        self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],1)
        with sqlite3.connect(self.path) as db: self.assertEqual(gc._digest_tables(db),before)

    def test_voice_requires_complete_bound_unique_chunk_proof(self):
        mutations=[lambda d:d['result'].update(officialMediaWriteback='pending'),
            lambda d:d['result']['chunks'][1].update(state='failed'),
            lambda d:d.update(serverMessageId='125'),
            lambda d:d['result']['nativeReceipt'].update(serviceCode=False),
            lambda d:d['result']['nativeReceipt'].update(clientIdMatches=False),
            lambda d:d['result']['chunks'][0].update(index=False),
            lambda d:d['result']['chunks'][0].update(durationMs=60001),
            lambda d:d['result']['chunks'][0].update(durationMs=True),
            lambda d:d['result']['chunks'][0].update(serverMessageId='0123'),
            lambda d:d['result']['chunks'][0].update(serverMessageId='9223372036854775808'),
            lambda d:d['result']['chunks'][0].update(serverMessageId='124'),
            lambda d:d['result'].update(chunks=[]),
            lambda d:d['result'].update(messageIds=['one','one']),
            lambda d:d['result'].update(messageIds=['one']),
            lambda d:d['result'].update(messageIds=['one',''])]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                data=self.voice();mutate(data);self.write_voice(data)
                self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],0)
        self.write_voice(self.voice(),kind='receive')
        self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],0)

    def test_pressure_zero_retention_respects_age_and_ttl(self):
        self.voice();stamp=1577836800
        self.assertEqual(gc.prune_database(self.path,offline=True,now=stamp+59,pressure=True,keep_bytes=0)['sources_removed'],0)
        self.assertEqual(gc.prune_database(self.path,offline=True,now=stamp+60)['sources_removed'],0)
        self.assertEqual(gc.prune_database(self.path,offline=True,now=stamp+60,pressure=True)['sources_removed'],0)
        self.assertEqual(gc.prune_database(self.path,offline=True,now=stamp+60,pressure=True,keep_bytes=0)['sources_removed'],1)

    def test_keep_bytes_validation_precedes_mutation(self):
        for value in (True,False,-1,gc.KEEP_BYTES+1,1.5,None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError,'keep_bytes'):
                    gc.prune_database(self.path,offline=True,keep_bytes=value)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM media_sources').fetchone()[0],1)

    def test_snapshot_pressure_zero_retention_preserves_journal_assets(self):
        self.voice()
        ss=importlib.util.spec_from_file_location('gc_store_pressure',Path(__file__).resolve().parents[1]/'scripts/session_state_store.py')
        state=importlib.util.module_from_spec(ss);ss.loader.exec_module(state)
        source=Path(self.directory.name)/'pressure.zip';dest=Path(self.directory.name)/'candidate.zip'
        with zipfile.ZipFile(source,'w') as z:
            z.writestr('accounts/example/agent/control.sqlite',self.path.read_bytes())
            z.writestr('accounts/example/journals/outbox.json',b'opaque-journal')
            z.writestr('accounts/example/assets/voice.wav',b'opaque-sdk-asset')
        result=gc.rewrite_snapshot(source,dest,state,offline=True,pressure=True,keep_bytes=0)
        self.assertEqual(result['sources_removed'],1)
        with zipfile.ZipFile(dest) as z:
            self.assertEqual(z.read('accounts/example/journals/outbox.json'),b'opaque-journal')
            self.assertEqual(z.read('accounts/example/assets/voice.wav'),b'opaque-sdk-asset')

    def test_image_boolean_service_code_is_not_accepted(self):
        with sqlite3.connect(self.path) as db:
            data=json.loads(db.execute('SELECT data_json FROM media_jobs').fetchone()[0])
            data['result']['nativeReceipt']['serviceCode']=False
            db.execute('UPDATE media_jobs SET data_json=?',(json.dumps(data),))
        self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],0)

    def test_voice_any_unfinished_or_foreign_link_blocks_collection(self):
        data=self.voice()
        for kind,state,phase in [('send','running','submitting'),('send','failed','completed'),
                                 ('send','unknown','completed'),('receive','accepted','completed')]:
            with self.subTest(kind=kind,state=state):
                with sqlite3.connect(self.path) as db:
                    db.execute('INSERT INTO media_jobs VALUES(?,?,?,?,?)',('other',kind,state,phase,json.dumps(data)))
                self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],0)
                with sqlite3.connect(self.path) as db: db.execute("DELETE FROM media_jobs WHERE id='other'")

    def test_voice_requires_matching_source_name_and_mime(self):
        self.voice()
        for name,mime in [('bridge-image.png','audio/wav'),('bridge-voice.wav','image/png'),('foreign.wav','audio/wav')]:
            with self.subTest(name=name,mime=mime):
                with sqlite3.connect(self.path) as db:
                    db.execute('UPDATE media_sources SET name=?,media_type=?',(name,mime))
                self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],0)

    def test_voice_five_chunk_limit_and_canonical_server_boundaries(self):
        data=self.voice()
        chunks=[{'index':i,'state':'accepted','durationMs':60000,'serverMessageId':str(i+1)} for i in range(6)]
        for count in (6,5):
            data['result']['chunks']=chunks[:count]
            data['result']['messageIds']=[str(i) for i in range(count)]
            data['serverMessageId']=str(count)
            data['result']['nativeReceipt']['serverMessageId']=str(count)
            self.write_voice(data)
            if count==6:self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],0)
        data['serverMessageId']='9223372036854775807'
        data['result']['nativeReceipt']['serverMessageId']=data['serverMessageId']
        data['result']['chunks'][-1]['serverMessageId']=data['serverMessageId']
        self.write_voice(data)
        self.assertEqual(gc.prune_database(self.path,offline=True)['sources_removed'],1)

    def test_snapshot_invalid_retention_does_not_create_candidate(self):
        dest=Path(self.directory.name)/'candidate.zip'
        for value in (True,-1,gc.KEEP_BYTES+1,0.5):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError,'keep_bytes'):
                    gc.rewrite_snapshot(self.path,dest,None,offline=True,keep_bytes=value)
                self.assertFalse(dest.exists())
