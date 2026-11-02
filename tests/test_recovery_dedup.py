import tempfile
from pathlib import Path
import unittest
from wechat_bridge.durable import Spool
from wechat_bridge.state import StateError

EPOCH = 'a' * 32

class RecoveryDedupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'bridge.sqlite'
        self.spool = Spool(self.path)
        # Thousands of synthetic transitions exercise capacity, not power loss.
        self.spool.db.execute('PRAGMA synchronous=OFF')
        self.addCleanup(lambda: self.spool.close())

    def complete(self, i, state='forwarded'):
        self.spool.admit(str(i), {'body': 'same'})
        if state == 'expired':
            self.spool.expire(str(i))
        else:
            self.spool.publishing(str(i))
            self.spool.handoff(str(i), state == 'forwarded')

    def test_required_api(self):
        self.assertTrue(callable(getattr(self.spool, 'protect_recovery', None)))

    def test_ack_reserves_completion_capacity_and_global_metadata_cap(self):
        self.spool.protect_recovery(1, EPOCH)
        for i in range(4095):
            self.complete(i)
        self.spool.admit('reserved', {'body':'same'})
        with self.assertRaisesRegex(StateError, '^recovery_checkpoint_required$'):
            self.spool.admit('overflow', {'body':'same'})
        self.spool.publishing('reserved')
        self.spool.handoff('reserved', True)
        self.spool.protect_recovery(2, EPOCH)
        self.complete('after-refresh')
        self.assertEqual(sum(self.spool.counts().values()), 4096)

    def test_protection_rejects_before_insert_and_refresh_releases_capacity(self):
        self.spool.protect_recovery(1, EPOCH)
        for i in range(4096):
            self.complete(i, 'expired' if i == 0 else 'forwarded')
        with self.assertRaisesRegex(StateError, '^recovery_checkpoint_required$'):
            self.spool.admit('overflow', {'body': 'same'})
        self.assertEqual(self.spool.recovery_coverage()['highSeq'], 4096)
        self.assertFalse(self.spool.admit('0', {'body': 'same'}))
        with self.assertRaisesRegex(StateError, '^source_changed$'):
            self.spool.admit('0', {'body': 'changed'})
        self.spool.protect_recovery(100, EPOCH)
        for i in range(4096, 4100):
            self.complete(i)
        coverage = self.spool.recovery_coverage()
        self.assertEqual(coverage['epoch'], EPOCH)
        self.assertEqual(coverage['floorSeq'], 100)
        self.assertLessEqual(coverage['retainedFloor'], 100)
        self.assertEqual(coverage['highSeq'], 4100)
        self.spool.close()
        self.spool = Spool(self.path)
        self.assertEqual(self.spool.recovery_coverage(), coverage)

    def test_no_retroactive_protection_over_missing_history(self):
        for i in range(4100):
            self.complete(i)
        with self.assertRaisesRegex(StateError, '^recovery_coverage$'):
            self.spool.protect_recovery(1, EPOCH)
        self.assertGreater(self.spool.recovery_coverage()['retainedFloor'], 1)

    def test_monotonic_lineage_and_uncertain_retention(self):
        self.spool.protect_recovery(1, EPOCH)
        self.complete('uncertain', 'uncertain')
        self.complete('expired', 'expired')
        self.assertFalse(self.spool.admit('uncertain', {'body': 'same'}))
        self.assertFalse(self.spool.admit('expired', {'body': 'same'}))
        self.spool.protect_recovery(2, EPOCH)
        for floor, epoch in [(1,EPOCH),(2,'b'*32),(100,EPOCH),(True,EPOCH)]:
            with self.assertRaises(StateError):
                self.spool.protect_recovery(floor, epoch)
        self.assertEqual(self.spool.counts(), {'uncertain':1, 'expired':1})
