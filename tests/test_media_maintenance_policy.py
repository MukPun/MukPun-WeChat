import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('maintenance_policy', Path(__file__).resolve().parents[1] / 'scripts/media_maintenance_policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class MaintenancePolicy(unittest.TestCase):
    def setUp(self):
        self.now = 2000000000
        self.status = dict(updated=self.now-2, active=True, ready=True, sse=True,
                           astrbot_connected=True, outbound_pending=False,
                           image_egress_active=False, uncertain=0,
                           last_activity_at=self.now-60, queue={})
        self.capacity = dict(storage_free=40*1024*1024, storage_used=50*1024*1024)

    def decide(self, previous=None):
        return policy.decision(self.status, self.capacity, previous, now=self.now)

    def test_idle_pressure_triggers_but_exact_threshold_does_not(self):
        self.assertEqual(self.decide(), 'maintain')
        self.capacity['storage_free'] = 48*1024*1024
        self.assertEqual(self.decide(), 'capacity_ok')
        self.capacity['storage_used'] = 500*1024*1024
        self.assertEqual(self.decide(), 'maintain')

    def test_pending_activity_connection_and_lease_block(self):
        for key, value in [('outbound_pending', True), ('image_egress_active', True),
                           ('uncertain', 1), ('ready', False), ('sse', False),
                           ('astrbot_connected', False), ('last_activity_at', self.now-59)]:
            with self.subTest(key=key):
                before = self.status[key]
                self.status[key] = value
                self.assertNotEqual(self.decide(), 'maintain')
                self.status[key] = before
        for key in ('queued', 'publishing', 'uncertain'):
            self.status['queue'] = {key: 1}
            self.assertEqual(self.decide(), 'busy')

    def test_stale_future_missing_and_malformed_health_fail_closed(self):
        for key, value in [('updated', self.now-16), ('updated', self.now+1),
                           ('last_activity_at', self.now+1), ('uncertain', False),
                           ('queue', []), ('outbound_pending', None)]:
            with self.subTest(key=key, value=value):
                before = self.status[key]
                self.status[key] = value
                self.assertNotEqual(self.decide(), 'maintain')
                self.status[key] = before
        del self.status['last_activity_at']
        self.assertNotEqual(self.decide(), 'maintain')
        self.capacity['storage_free'] = True
        self.assertNotEqual(self.decide(), 'maintain')

    def test_cooldown_and_interrupted_maintenance_are_not_retried(self):
        for outcome, seconds in [('success', 300), ('no_gain', 3600), ('failed', 3600)]:
            previous = dict(outcome=outcome, completed_at=self.now-seconds+1)
            self.assertEqual(self.decide(previous), 'cooldown')
            previous['completed_at'] -= 1
            self.assertEqual(self.decide(previous), 'maintain')
        for previous in ({'outcome':'in_progress'}, {'outcome':'unexpected'}, {'outcome': []}, {'outcome': {}},
                         {'outcome':'success', 'completed_at': self.now+1}):
            self.assertEqual(self.decide(previous), 'requires_review')

    def test_preflight_minimum_gain(self):
        self.assertFalse(policy.worthwhile({'sources_removed':0, 'snapshot_bytes_before':3000000, 'snapshot_bytes_after':1}))
        self.assertFalse(policy.worthwhile({'sources_removed':2, 'snapshot_bytes_before':1048576, 'snapshot_bytes_after':1}))
        self.assertTrue(policy.worthwhile({'sources_removed':1, 'snapshot_bytes_before':2097152, 'snapshot_bytes_after':1048576}))
        self.assertFalse(policy.worthwhile({'sources_removed':True, 'snapshot_bytes_before':2097152, 'snapshot_bytes_after':1}))


if __name__ == '__main__':
    unittest.main()
