"""Private disposition ledger tests using only synthetic values."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import send_dispositions as dispositions
import restore_admission as admission

ROW=('synthetic-text','key','a'*64,'synthetic-room',b'synthetic-payload',
     'needs_review','client',None,'unknown','created')
PROFILE='a'*64


def item(index=0):
    return dispositions.entry('example','text',(str(index),*ROW[1:]),
                              'reviewed_not_delivered','operator_review','b'*64)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'ledger.json'
        self.ledger=dispositions.Ledger(self.path,PROFILE)

    def test_private_persistence_idempotency_conflicts_and_lineage(self):
        self.assertEqual(self.ledger.read(),dict(schemaVersion=1,profileSha256=PROFILE,revision=0,entries=[]))
        record=item(); self.ledger.record(record); self.ledger.record(record)
        value=self.ledger.read();self.assertEqual(value['revision'],1);self.assertEqual(value['entries'],[record])
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        raw=self.path.read_bytes()
        for secret in (b'synthetic-text',b'synthetic-room',b'synthetic-payload',b'example'):
            self.assertNotIn(secret,raw)
        with self.assertRaises(ValueError): self.ledger.record({**record,'evidenceSha256':'c'*64})
        with self.assertRaises(ValueError): dispositions.Ledger(self.path,'b'*64).read()
        self.assertEqual(self.path.read_bytes(),raw)

    def test_concurrent_writers_keep_every_entry(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: dispositions.Ledger(self.path,PROFILE).record(item(i)),range(24)))
        value=self.ledger.read();self.assertEqual(value['revision'],24);self.assertEqual(len(value['entries']),24)

    def test_capacity_refuses_without_evicting(self):
        for i in range(128): self.ledger.record(item(i))
        before=self.path.read_bytes()
        with self.assertRaises(ValueError): self.ledger.record(item(128))
        self.assertEqual(self.path.read_bytes(),before)
        self.ledger.record(item(0));self.assertEqual(self.ledger.read()['revision'],128)

    def test_interrupted_replace_keeps_previous_valid_ledger(self):
        self.ledger.record(item());before=self.path.read_bytes()
        with patch.object(dispositions.os,'replace',side_effect=OSError('synthetic interruption')):
            with self.assertRaises(ValueError): self.ledger.record(item(1))
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(self.ledger.read()['revision'],1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['ledger.json','ledger.json.lock'])

    def test_links_modes_and_non_owner_are_rejected(self):
        self.ledger.record(item()); raw=self.path.read_bytes()
        other=self.root/'other';other.write_bytes(raw);other.chmod(0o600)
        self.path.unlink();self.path.symlink_to(other)
        with self.assertRaises(ValueError): self.ledger.read()
        self.path.unlink();os.link(other,self.path)
        with self.assertRaises(ValueError): self.ledger.read()
        self.path.unlink();self.path.write_bytes(raw);self.path.chmod(0o644)
        with self.assertRaises(ValueError): self.ledger.read()
        self.path.chmod(0o600)
        with patch.object(dispositions.os,'geteuid',return_value=os.geteuid()+1):
            with self.assertRaises(ValueError): self.ledger.read()
        self.root.chmod(0o755)
        with self.assertRaises(ValueError): self.ledger.read()

    def test_symlink_parent_and_lock_hardlink_reject(self):
        alias=self.root/'alias';private=self.root/'private';private.mkdir(mode=0o700);alias.symlink_to(private)
        with self.assertRaises(ValueError): dispositions.Ledger(alias/'ledger.json',PROFILE).read()
        lock=self.root/'ledger.json.lock';lock.write_bytes(b'');lock.chmod(0o600)
        os.link(lock,self.root/'other-lock')
        with self.assertRaises(ValueError): self.ledger.record(item())

    def test_strict_json_duplicate_keys_and_byte_bounds(self):
        self.ledger.record(item());base=self.ledger.read()
        malformed=[b'{"schemaVersion":1,"schemaVersion":1}',b' '*65537]
        for change in [dict(revision=True),dict(extra=1),dict(schemaVersion=True),
                       dict(entries=[item(),item()]),dict(entries=[{**item(),'evidenceSha256':'bad'}])]:
            malformed.append(json.dumps({**base,**change}).encode())
        for raw in malformed:
            self.path.write_bytes(raw);self.path.chmod(0o600)
            with self.subTest(size=len(raw)), self.assertRaises(ValueError): self.ledger.read()

    def test_fixed_classifications_and_evidence(self):
        for disposition,evidence in [('accepted_verified','native_receipt'),('rejected_before_submit','native_pre_submit'),
                  ('unconfirmed_quarantined','legacy_hold'),('unconfirmed_quarantined','unconfirmed_result'),
                  ('unconfirmed_quarantined','operator_review'),('reviewed_not_delivered','operator_review')]:
            result=dispositions.entry('example','text',ROW,disposition,evidence,'a'*64)
            self.assertEqual(set(result),{'kind','recordSha256','disposition','evidenceKind','evidenceSha256'})
        for disposition,evidence in [('accepted_verified','operator_review'),('other','native_receipt')]:
            with self.assertRaises(ValueError): dispositions.entry('example','text',ROW,disposition,evidence,'a'*64)
