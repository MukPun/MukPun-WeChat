import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from scripts import restore_admission as admission
from scripts.forward_sse import event_digest

SEED = dict(sendId='11111111-1111-1111-1111-111111111111',
            destinationSha256=hashlib.sha256(b'peer').hexdigest(),
            textSha256=hashlib.sha256(b'accepted text').hexdigest(), bytes=13)


def event(n):
    return dict(cursor=str(n), kind='message', data={'id':str(n)}, createdAt='2026-01-01T00:00:00.120+00:00')


def fixture(root, last=8, *, wal=False, pending=False, seed=True, event_json=None):
    source = root / ('source'+str(last))
    source.mkdir(mode=0o700)
    dbpath = source / 'control.sqlite'
    db = sqlite3.connect(dbpath)
    if wal:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
    db.execute('CREATE TABLE outbox('+','.join(admission.TEXT_COLUMNS.split(','))+')')
    db.execute('CREATE TABLE media_jobs('+','.join(admission.MEDIA_COLUMNS.split(','))+')')
    db.execute('CREATE TABLE events(sequence INTEGER PRIMARY KEY,kind TEXT,data_json TEXT,created_at TEXT)')
    db.execute('CREATE TABLE runtime(key TEXT PRIMARY KEY,value BLOB)')
    for i in range(1,last+1):
        ev = event(i)
        db.execute('INSERT INTO events VALUES(?,?,?,?)', (i,ev['kind'],json.dumps(ev['data']) if event_json is None else event_json,ev['createdAt']))
    if seed:
        db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?)', (SEED['sendId'],'key','unrelated-payload-hash','peer',b'encrypted','accepted','client','server',None,'time'))
        db.execute('INSERT INTO runtime VALUES(?,?)', ('send-projected:'+SEED['sendId'],b'local-message'))
    if pending:
        db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?)', ('pending','pending-key','hash','peer',b'private','queued','client',None,None,'time'))
    db.commit()
    path = root / ('snapshot'+str(last)+'.zip')
    with zipfile.ZipFile(path,'w') as z:
        for key in ('api-token','storage-key','cursor-key'):
            z.writestr(key, ('synthetic-'+key).encode())
        for file in source.iterdir():
            z.write(file, 'accounts/wechat-primary/agent/'+file.name)
    path.chmod(0o600)
    db.close()
    return path


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_required_module(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.recovery_snapshot'))

    def test_wal_event_timestamp_pending_and_candidate_selection(self):
        from scripts.recovery_snapshot import inspect
        path = fixture(self.root, wal=True, pending=True)
        missing = {**SEED, 'sendId':'22222222-2222-2222-2222-222222222222'}
        result = inspect(path, self_proof_candidates=[missing, SEED], boundaries=[{'cursor':'7','anchor':event_digest(event(7))}])
        self.assertEqual(result['nativeBoundary'], {'cursor':'8','anchor':event_digest(event(8))})
        self.assertEqual(result['selfProof'], SEED)
        self.assertEqual(result['profileSha256'], admission.profile_sha256(result['keys']))
        self.assertEqual(len(result['pending']),1)
        self.assertEqual(set(result['pending'][0]), {'kind','recordSha256'})
        self.assertEqual(result['snapshotSha256'], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_missing_seed_and_no_event_are_explicit(self):
        from scripts.recovery_snapshot import inspect
        path = fixture(self.root, last=0, seed=False)
        result = inspect(path, lineage_validator=lambda profile: True)
        self.assertIsNone(result['nativeBoundary'])
        self.assertIsNone(result['selfProof'])
        with self.assertRaisesRegex(ValueError,'^recovery_snapshot$'):
            inspect(path, lineage_validator=lambda profile: False)

    def test_sdk_rejects_nontext_event_json_and_invalid_json_maps_to_null(self):
        from scripts.recovery_snapshot import inspect
        path=fixture(self.root,event_json=b'{"id":"8"}')
        with self.assertRaisesRegex(ValueError,'^recovery_snapshot$'): inspect(path)

    def test_sdk_invalid_json_maps_to_null(self):
        from scripts.recovery_snapshot import inspect
        path=fixture(self.root,event_json='not json')
        expected={**event(8),'data':None}
        self.assertEqual(inspect(path)['nativeBoundary']['anchor'],event_digest(expected))

    def test_bad_anchor_links_permissions_and_bounds(self):
        from scripts.recovery_snapshot import inspect
        path = fixture(self.root)
        with self.assertRaises(ValueError):
            inspect(path, boundaries=[{'cursor':'8','anchor':'0'*64}])
        link = self.root/'linked.zip'
        os.link(path,link)
        with self.assertRaises(ValueError):
            inspect(path)
        link.unlink()
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            inspect(path)
