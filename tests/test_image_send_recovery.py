"""A provable non-delivery is settled once; a doubtful one still needs a human."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('image_recovery_test', ROOT / 'scripts/image_send_recovery.py')
recovery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recovery
spec.loader.exec_module(recovery)

JOB = 'job_aa890a35-7ed8-4c39-9e52-d3cd07c72b64'
PEER = '19807690022@chatroom'


def audit(**overrides):
    value = {'state': 'submitted', 'key': 'f1558d95099e49679f87f9e281f681b5',
             'requestId': '7f0e89b40f8279bb3d533e6d36fab59f',
             'destinationSha256': hashlib.sha256(PEER.encode()).hexdigest(),
             'sourceSha256': '7df3c4067fdd0c3caf849946ab5f9e04e3d3f3a23b51b391367976596d935e62',
             'bytes': 149184, 'jobId': JOB}
    value.update(overrides)
    return value


def job(**overrides):
    value = {'id': JOB, 'accountId': 'wechat-primary', 'kind': 'send', 'threadId': PEER,
             'state': 'needs_review', 'phase': 'uploading', 'completedBytes': 0,
             'serverMessageId': None, 'result': {'messageIds': [], 'nativeReceipt': None}}
    value.update(overrides)
    return value


def settled(value, marker):
    return {**value, 'state': 'rejected', 'review': recovery.REVIEW, 'recoveryVerification': marker}


def marker(value, **overrides):
    item = recovery.verify(value, job(), at=1794191600)
    item.update(overrides)
    return item


class Proof(unittest.TestCase):
    """The four-way conjunction that lets a job speak for itself."""

    def test_uploading_with_zero_bytes_and_no_receipt_is_a_proved_failure(self):
        self.assertTrue(recovery.known_failure(audit(), job()))

    def test_failed_job_may_still_be_proved_during_readback(self):
        self.assertTrue(recovery.known_failure(audit(), job(state='failed', phase='readback')))

    def test_needs_review_during_readback_stays_uncertain(self):
        # Past 'uploading' the SDK may already have handed a body to WeChat.
        self.assertFalse(recovery.known_failure(audit(), job(phase='readback')))

    def test_needs_review_while_submitting_stays_uncertain(self):
        self.assertFalse(recovery.known_failure(audit(), job(phase='submitting')))

    def test_a_server_message_id_means_wechat_took_the_body(self):
        self.assertFalse(recovery.known_failure(audit(), job(serverMessageId='12345')))

    def test_sent_bytes_disqualify_the_claim(self):
        self.assertFalse(recovery.known_failure(audit(), job(completedBytes=62407)))

    def test_a_native_receipt_disqualifies_the_claim(self):
        self.assertFalse(recovery.known_failure(
            audit(), job(result={'messageIds': [], 'nativeReceipt': {'status': 'server_accepted'}})))

    def test_message_ids_disqualify_the_claim(self):
        self.assertFalse(recovery.known_failure(
            audit(), job(result={'messageIds': ['12345'], 'nativeReceipt': None})))

    def test_a_queued_job_is_not_a_failure_at_all(self):
        self.assertFalse(recovery.known_failure(audit(), job(state='queued')))

    def test_another_destination_cannot_borrow_this_audit(self):
        self.assertFalse(recovery.known_failure(audit(), job(threadId='other@chatroom')))

    def test_another_job_id_cannot_borrow_this_audit(self):
        self.assertFalse(recovery.known_failure(
            audit(), job(id='job_11111111-1111-4111-8111-111111111111')))

    def test_missing_job_is_uncertainty_not_a_proof(self):
        self.assertFalse(recovery.known_failure(audit(), None))


class Settlement(unittest.TestCase):
    """A proved job must survive a restart without asking again."""

    def setUp(self):
        self.value = audit()
        self.saved = settled(self.value, marker(self.value))

    def test_a_reviewed_rejection_is_terminal(self):
        self.assertTrue(recovery.cleared(self.saved))

    def test_an_accepted_audit_is_terminal(self):
        self.assertTrue(recovery.cleared({**self.value, 'state': 'accepted'}))

    def test_a_bare_rejected_label_is_not_enough(self):
        # Only this module may write the review name, and only with its marker.
        self.assertFalse(recovery.cleared({**self.value, 'state': 'rejected'}))
        self.assertFalse(recovery.cleared({**self.value, 'state': 'rejected',
                                           'review': 'someone_else'}))

    def test_an_edited_marker_reopens_the_gate(self):
        edited = settled(self.value, {**marker(self.value), 'completedBytes': 999})
        self.assertFalse(recovery.cleared(edited))

    def test_a_marker_for_a_different_audit_does_not_clear_this_one(self):
        other = settled(audit(bytes=1), marker(self.value))
        self.assertFalse(recovery.cleared(other))

    def test_marker_must_carry_its_own_proof_when_confirmed(self):
        forged = settled(self.value, {**marker(self.value), 'jobPhase': 'submitting'})
        self.assertFalse(recovery.cleared(forged))

    def test_frozen_evidence_names_what_proved_the_decision(self):
        value = recovery.frozen(self.value, job())
        self.assertEqual(value['job']['completedBytes'], 0)
        self.assertIsNone(value['job']['serverMessageId'])
        self.assertIsNone(value['nativeReceipt'])
        self.assertEqual(value['messageIds'], [])

    def test_subject_ignores_only_what_a_review_may_rewrite(self):
        before = recovery.subject(self.value)
        after = recovery.subject(self.saved)
        self.assertEqual(before, after)


class Reconcile(unittest.TestCase):
    """The gate only opens for a job read back from the live Native instance."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'forward-image-live-send.json'
        self.path.write_text(json.dumps(audit()), encoding='utf-8')
        self.pinned = SimpleNamespace(open=lambda: None, close=lambda: None,
                                      unchanged=lambda: True, host=SimpleNamespace(client_pid=2419),
                                      token=lambda: 't' * 64)
        self.core = SimpleNamespace(owner=SimpleNamespace(STATE=Path(self.directory.name),
                                                          private_directory=lambda p: p.mkdir(exist_ok=True),
                                                          write_json=lambda p, v: Path(p).write_text(
                                                              json.dumps(v, sort_keys=True, separators=(',', ':')))),
                                    groups=SimpleNamespace(PinnedNative=lambda: self.pinned),
                                    tap=SimpleNamespace(load_state=lambda n: (False, {})))
        self.settings = {'imageOutputEnabled': True}

    def tearDown(self):
        self.directory.cleanup()

    def reconcile(self, job_value):
        with patch.object(recovery, 'workers_stopped', return_value=True), \
             patch.object(recovery, 'read_job', return_value=job_value):
            return recovery.reconcile(self.core, self.path, self.settings)

    def test_a_proved_job_is_settled_and_written_once(self):
        self.assertTrue(self.reconcile(job()))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['state'], 'rejected')
        self.assertEqual(saved['review'], recovery.REVIEW)
        self.assertEqual(saved['recoveryVerification']['completedBytes'], 0)
        self.assertTrue(recovery.cleared(saved))
        # A second start must not ask Native again.
        with patch.object(recovery, 'read_job', side_effect=AssertionError('reread')):
            self.assertTrue(recovery.reconcile(self.core, self.path, self.settings))

    def test_a_doubtful_job_leaves_the_audit_untouched(self):
        before = self.path.read_bytes()
        self.assertFalse(self.reconcile(job(phase='submitting')))
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_live_worker_blocks_the_review(self):
        with patch.object(recovery, 'workers_stopped', return_value=False):
            self.assertFalse(recovery.reconcile(self.core, self.path, self.settings))
        self.assertEqual(json.loads(self.path.read_text())['state'], 'submitted')

    def test_an_audit_rewritten_mid_read_is_not_settled(self):
        with patch.object(recovery, 'workers_stopped', return_value=True):
            def rewrite(*_):
                self.path.write_text(json.dumps(audit(bytes=1)), encoding='utf-8')
                return job()
            with patch.object(recovery, 'read_job', side_effect=rewrite):
                self.assertFalse(recovery.reconcile(self.core, self.path, self.settings))

    def test_a_job_for_another_destination_is_not_settled(self):
        self.assertFalse(self.reconcile(job(threadId='other@chatroom')))

    def test_an_accepted_audit_never_calls_native(self):
        self.path.write_text(json.dumps({**audit(), 'state': 'accepted'}), encoding='utf-8')
        with patch.object(recovery, 'read_job', side_effect=AssertionError('reread')):
            self.assertTrue(recovery.reconcile(self.core, self.path, self.settings))

    def test_malformed_audits_never_reach_native(self):
        for broken in ({**audit(), 'bytes': 0}, {**audit(), 'jobId': 'job_nope'},
                       {**audit(), 'key': 'zz'}, {**audit(), 'sourceSha256': 'x'}):
            self.path.write_text(json.dumps(broken), encoding='utf-8')
            self.assertFalse(self.reconcile(job()), broken)


if __name__ == '__main__':
    unittest.main()
