"""Synthetic copied-database restore admission; never accesses a Native service."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import restore_admission as admission
import send_dispositions as dispositions

KEYS = {'api-token': 'a'*64, 'storage-key': 'b'*64, 'cursor-key': 'c'*64}
PROFILE = lambda: admission.profile_sha256(KEYS)
MEDIA = ('synthetic-job', 'synthetic-key', 'd'*64, 'send', 'needs_review',
         'submitting', 1, 0, 0, 'unknown', '{"result":{}}', b'\x00synthetic', 'before', 'after')
TEXT = ('synthetic-text', 'text-key', 'e'*64, 'synthetic-room', b'encrypted',
        'needs_review', 'client', None, 'unknown', 'before')


def create_db(path):
    db = sqlite3.connect(path)
    os.chmod(path, 0o600)
    for table, columns in [('outbox', admission.TEXT_COLUMNS), ('media_jobs', admission.MEDIA_COLUMNS)]:
        db.execute('CREATE TABLE '+table+' ('+','.join(
            c+(' INTEGER' if c in ('attempts','cancel_requested','retryable') else
               ' BLOB' if c == 'payload' else ' TEXT') for c in columns.split(','))+')')
    db.commit()
    return db


def insert(db, kind, row):
    db.execute('INSERT INTO '+('outbox' if kind == 'text' else 'media_jobs')+
               ' VALUES ('+','.join('?' for _ in row)+')', row)
    db.commit()


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'copied.sqlite'
        self.db = create_db(self.path)
        self.addCleanup(self.db.close)
        self.ledger = dispositions.Ledger(self.root/'dispositions.json', PROFILE())

    def approve(self, kind, row, disposition='unconfirmed_quarantined', evidence='operator_review'):
        self.ledger.record(dispositions.entry('example', kind, row, disposition, evidence, 'f'*64))

    def test_lineage_is_canonical_and_strict(self):
        self.assertEqual(PROFILE(), admission.profile_sha256(dict(reversed(list(KEYS.items())))))
        self.assertNotEqual(PROFILE(), admission.profile_sha256({**KEYS, 'api-token':'d'*64}))
        for value in ({}, {**KEYS, 'extra':'d'*64}, {**KEYS,'api-token':'A'*64},
                      {**KEYS, 'storage-key':True}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '^restore_profile$'):
                admission.profile_sha256(value)

    def test_media_fingerprint_is_legacy_byte_compatible_and_typed(self):
        spec = importlib.util.spec_from_file_location('admission_guest', ROOT/'scripts/session_runtime_client.py')
        guest = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = guest
        spec.loader.exec_module(guest)
        self.assertEqual(admission.MEDIA_COLUMNS, guest.QUARANTINED_COLUMNS)
        self.assertEqual(admission.fingerprint('example','media', MEDIA),
                         guest.quarantined_send_fingerprint('example', MEDIA))
        self.assertNotEqual(admission.fingerprint('example','media', MEDIA),
                            admission.fingerprint('another','media', MEDIA))
        for kind,row in [('other',MEDIA),('media',MEDIA[:-1]),('media',(*MEDIA[:6],True,*MEDIA[7:])),
                         ('text',(*TEXT[:4],1.5,*TEXT[5:]))]:
            with self.subTest(kind=kind,row=row), self.assertRaises(ValueError):
                admission.fingerprint('example',kind,row)
        self.assertNotEqual(admission.fingerprint('example','text',TEXT),
            admission.fingerprint('example','text',(*TEXT[:4],'encrypted',*TEXT[5:])))

    def test_multiple_reviewed_rows_and_terminal_rows_are_unchanged(self):
        second = ('synthetic-job-two', *MEDIA[1:])
        for kind,row in [('media',MEDIA),('media',second),('text',TEXT)]:
            insert(self.db,kind,row)
            self.approve(kind,row)
        insert(self.db,'media',('terminal', *MEDIA[1:4], 'accepted', *MEDIA[5:]))
        insert(self.db,'text',('terminal-text',*TEXT[1:5],'accepted',*TEXT[6:]))
        before = self.path.read_bytes()
        rows_before = [self.db.execute('SELECT * FROM '+table).fetchall() for table in ('outbox','media_jobs')]
        value = dispositions.admission_for(self.db,'example',self.ledger)
        self.assertEqual(len(value['entries']),3)
        self.assertEqual(value['ledgerRevision'],3)
        admission.validate_database(self.db,'example',value)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(rows_before,[self.db.execute('SELECT * FROM '+t).fetchall() for t in ('outbox','media_jobs')])
        self.assertEqual(self.db.execute('PRAGMA trusted_schema').fetchone(),(0,))

    def test_changed_extra_missing_and_account_inventory_reject(self):
        insert(self.db,'media',MEDIA)
        self.approve('media',MEDIA)
        value = dispositions.admission_for(self.db,'example',self.ledger)
        for sql,args in [('UPDATE media_jobs SET payload=?',(b'changed',)),
                         ('DELETE FROM media_jobs',()),
                         ('INSERT INTO media_jobs SELECT * FROM media_jobs',())]:
            with self.subTest(sql=sql):
                self.db.execute('SAVEPOINT synthetic')
                self.db.execute(sql,args)
                with self.assertRaises(ValueError):
                    admission.validate_database(self.db,'example',value)
                self.db.execute('ROLLBACK TO synthetic')
                self.db.execute('RELEASE synthetic')
        with self.assertRaises(ValueError):
            admission.validate_database(self.db,'another',value)

    def test_fresh_hash_cannot_admit_replayable_media_or_text(self):
        mutations = [('media',4,'queued'),('media',4,'running'),('media',8,1),
                     ('media',6,2),('media',7,1),('media',3,'download'),
                     ('media',5,'unknown'),('text',5,'queued'),('text',5,'submitting'),
                     ('text',7,'server')]
        for kind,index,item in mutations:
            with self.subTest(kind=kind,index=index,item=item):
                self.db.execute('DELETE FROM outbox'); self.db.execute('DELETE FROM media_jobs'); self.db.commit()
                row = list(MEDIA if kind == 'media' else TEXT); row[index]=item
                insert(self.db,kind,row)
                entry = dispositions.entry('example',kind,row,'reviewed_not_delivered','operator_review','e'*64)
                value = dict(schemaVersion=1,profileSha256=PROFILE(),ledgerRevision=1,entries=[entry])
                with self.assertRaises(ValueError):
                    admission.validate_database(self.db,'example',value)

    def test_accepted_receipt_never_admits_pending_unknown(self):
        insert(self.db,'media',MEDIA)
        self.approve('media',MEDIA,'accepted_verified','native_receipt')
        with self.assertRaises(ValueError):
            dispositions.admission_for(self.db,'example',self.ledger)

    def test_rejected_media_requires_pre_submit_phase_and_absent_receipt(self):
        for phase,data,okay in [('preparing','{}',True),('uploading','{}',True),
              ('submitting','{}',False),('readback','{}',False),
              ('uploading','{"serverMessageId":"123"}',False),
              ('uploading','{"result":{"nativeReceipt":{}}}',False),
              ('uploading','{"result":{},"result":{}}',False)]:
            with self.subTest(phase=phase,data=data):
                self.db.execute('DELETE FROM media_jobs');self.db.commit()
                row=(*MEDIA[:5],phase,*MEDIA[6:10],data,*MEDIA[11:])
                insert(self.db,'media',row)
                value=dict(schemaVersion=1,profileSha256=PROFILE(),ledgerRevision=1,entries=[
                    dispositions.entry('example','media',row,'rejected_before_submit','native_pre_submit','e'*64)])
                if okay:
                    admission.validate_database(self.db,'example',value)
                else:
                    with self.assertRaises(ValueError): admission.validate_database(self.db,'example',value)

    def test_wal_extra_row_is_visible_and_rejected(self):
        insert(self.db,'media',MEDIA); self.approve('media',MEDIA)
        value=dispositions.admission_for(self.db,'example',self.ledger)
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA wal_autocheckpoint=0')
        insert(self.db,'text',(*TEXT[:5],'queued',*TEXT[6:]))
        self.assertTrue(Path(str(self.path)+'-wal').exists())
        with sqlite3.connect(self.path) as reader:
            with self.assertRaises(ValueError): admission.validate_database(reader,'example',value)

    def test_strict_shapes_duplicate_entries_bounds_and_schema(self):
        base=dict(schemaVersion=1,profileSha256=PROFILE(),ledgerRevision=0,entries=[])
        self.assertEqual(admission.validate_admission(base),base)
        item=dispositions.entry('example','text',TEXT,'reviewed_not_delivered','operator_review','e'*64)
        for change in [dict(schemaVersion=True),dict(ledgerRevision=True),dict(extra=1),
                       dict(profileSha256='bad'),dict(entries=[item,item]),dict(entries=[item]*129),
                       dict(entries=[{**item,'payload':'private'}]),dict(entries=[{**item,'evidenceKind':'native_receipt'}])]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError,'^restore_admission$'):
                admission.validate_admission({**base,**change})
        self.db.execute('ALTER TABLE outbox ADD COLUMN unexpected TEXT')
        with self.assertRaises(ValueError): admission.pending_records(self.db)

    def test_total_pending_row_bytes_are_bounded_before_materialization(self):
        for i in range(2):
            row=(str(i),*MEDIA[1:11],b'x'*4500,*MEDIA[12:])
            insert(self.db,'media',row)
        with patch.object(admission,'MAX_ROW_BYTES',8192):
            with self.assertRaises(ValueError): admission.pending_records(self.db)

    def test_malformed_pending_identity_and_column_types_fail_closed(self):
        for kind,index,item in [('media',0,None),('media',10,b'not-json-text'),
                                ('text',0,None),('text',3,None),('text',4,'not-encrypted-bytes')]:
            with self.subTest(kind=kind,index=index):
                self.db.execute('DELETE FROM media_jobs');self.db.execute('DELETE FROM outbox');self.db.commit()
                row=list(MEDIA if kind == 'media' else TEXT);row[index]=item
                insert(self.db,kind,row)
                value=dict(schemaVersion=1,profileSha256=PROFILE(),ledgerRevision=1,entries=[
                    dispositions.entry('example',kind,row,'reviewed_not_delivered','operator_review','a'*64)])
                with self.assertRaises(ValueError): admission.validate_database(self.db,'example',value)

    def test_inventory_bound_and_unexpected_table_type(self):
        for i in range(129): insert(self.db,'media',(str(i),*MEDIA[1:]))
        with self.assertRaises(ValueError): admission.pending_records(self.db)
        self.db.execute('DROP TABLE media_jobs');self.db.execute('CREATE VIEW media_jobs AS SELECT 1')
        with self.assertRaises(ValueError): admission.pending_records(self.db)
