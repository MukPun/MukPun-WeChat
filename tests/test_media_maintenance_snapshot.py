import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location('_proof_test_'+name, ROOT/'scripts'/(name+'.py'))
    value=importlib.util.module_from_spec(spec);sys.modules[spec.name]=value;spec.loader.exec_module(value);return value
proofs=load('media_maintenance_snapshot');guest=load('session_runtime_client')


class SnapshotProof(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);self.db=self.root/'control.sqlite'
        self.audit=dict(jobId='synthetic-job',key='synthetic-key',destinationSha256=hashlib.sha256(b'thread').hexdigest(),sourceSha256='a'*64,bytes=123)
        data=dict(threadId='thread',sourceId='source',completedBytes=123,totalBytes=123,
                  result=dict(imagePreparation={'sourceSha256':'a'*64},uploadReadbackVerified=True))
        self.row=('synthetic-job','send:synthetic-key','request','send','needs_review','submitting',1,0,0,None,json.dumps(data),None,'created','updated')
        columns=guest.QUARANTINED_COLUMNS.split(',')
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE outbox(state TEXT)')
            db.execute('CREATE TABLE media_sources(id TEXT,sha256 TEXT,byte_length INTEGER)')
            db.execute('CREATE TABLE media_jobs('+','.join(n+(' INTEGER' if n in ('attempts','cancel_requested','retryable') else ' BLOB' if n=='payload' else ' TEXT') for n in columns)+')')
            db.execute('INSERT INTO media_sources VALUES(?,?,?)',('source','a'*64,123))
            db.execute('INSERT INTO media_jobs VALUES('+','.join('?' for _ in self.row)+')',self.row)
        self.approved=dict(schemaVersion=1,jobSha256=guest.quarantined_send_fingerprint('example',self.row))

    def snapshot(self):
        path=self.root/'snapshot.zip'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('accounts/example/agent/control.sqlite',self.db.read_bytes())
            for name in ('api-token','storage-key','cursor-key'):archive.writestr(name,b'synthetic-'+name.encode())
        return path

    def test_original_exact_unknown_and_keys_proved_without_mutation(self):
        source=self.snapshot();before=source.read_bytes()
        result=proofs.analyze(source,guest,audit=self.audit,approved=self.approved)
        self.assertEqual(result['quarantinedSend'],self.approved)
        self.assertEqual(result['keys']['storage-key'],hashlib.sha256(b'synthetic-storage-key').hexdigest())
        self.assertEqual(source.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'unknown_send'):proofs.analyze(source,guest)

    def test_changed_unknown_or_source_binding_cannot_inherit_approval(self):
        with sqlite3.connect(self.db) as db:db.execute("UPDATE media_jobs SET updated_at='changed'")
        with self.assertRaisesRegex(ValueError,'changed'):proofs.analyze(self.snapshot(),guest,audit=self.audit,approved=self.approved)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE media_jobs SET updated_at='updated'")
            db.execute('UPDATE media_sources SET byte_length=124')
        with self.assertRaisesRegex(ValueError,'binding'):proofs.analyze(self.snapshot(),guest,audit=self.audit,approved=self.approved)

    def test_extra_unknown_pending_outbox_and_no_hold_case(self):
        with sqlite3.connect(self.db) as db:db.execute("INSERT INTO outbox VALUES('queued')")
        with self.assertRaisesRegex(ValueError,'pending_outbox'):proofs.analyze(self.snapshot(),guest,audit=self.audit,approved=self.approved)
        with sqlite3.connect(self.db) as db:
            db.execute('DELETE FROM outbox');db.execute('INSERT INTO media_jobs SELECT * FROM media_jobs')
        with self.assertRaisesRegex(ValueError,'unknown_send'):proofs.analyze(self.snapshot(),guest,audit=self.audit,approved=self.approved)
        with sqlite3.connect(self.db) as db:db.execute('DELETE FROM media_jobs')
        self.assertIsNone(proofs.analyze(self.snapshot(),guest)['quarantinedSend'])


if __name__=='__main__':unittest.main()
