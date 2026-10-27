import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import time
import unittest

SPEC = importlib.util.spec_from_file_location('egress_policy_test', Path(__file__).resolve().parents[1] / 'scripts/media_egress.py')
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


def row(ip, inode='123', port='01BB', state='01'):
    encoded = bytes(map(int, ip.split('.')))[::-1].hex().upper()
    return ' 0: 6402000A:C000 ' + encoded + ':' + port + ' ' + state + ' 0:0 0:0 0 1 0 ' + inode


class Scope(unittest.TestCase):
    def test_only_bounded_public_single_ip_rules_are_accepted(self):
        self.assertEqual(policy.addresses('1.1.1.1/32 8.8.8.8/32'), {'1.1.1.1', '8.8.8.8'})
        for value in ('', '0.0.0.0/0', '127.0.0.1/32', '192.168.1.1/32', '8.8.8.0/24', '::/0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.addresses(value)

    def test_only_a_socket_owned_by_the_pinned_sdk_can_supply_one_public_443_target(self):
        rows = 'header\n' + '\n'.join((row('1.1.1.1'), row('8.8.8.8', inode='other'),
                                      row('9.9.9.9', port='0050'), row('127.0.0.1'), row('4.2.2.2', state='06')))
        self.assertIsNone(policy.destination(rows, {'1.1.1.1'}, {'123'}))
        self.assertEqual(policy.destination(rows + '\n' + row('8.8.8.8', state='02'), {'1.1.1.1'}, {'123'}), '8.8.8.8')

    def test_two_new_targets_are_rejected_instead_of_widening_access(self):
        with self.assertRaisesRegex(ValueError, 'media_egress_ambiguous'):
            policy.destination('header\n' + row('8.8.8.8') + '\n' + row('9.9.9.9'), set(), {'123'})

    def lease(self):
        lease = policy.Lease.__new__(policy.Lease)
        lease.job, lease.lock = 'job_11111111-2222-4333-8444-555555555555', None
        lease.original = lease.expected = {'1.1.1.1'}
        lease.changed, lease.until, lease.identity = False, time.monotonic() + 10, (100, 200)
        lease.grants = 0
        lease.pinned = SimpleNamespace(unchanged=lambda: True)
        lease.helper = lambda: (100, 200)
        lease.sdk_destination = lambda: '8.8.8.8'
        lease.current = {'1.1.1.1'}
        lease.policy = lambda: lease.current
        lease.writes = []
        def update(values):
            lease.writes.append(set(values))
            lease.current = set(values)
        lease.set_policy = update
        return lease

    def test_one_temporary_grant_is_restored_after_job(self):
        lease = self.lease()
        self.assertTrue(lease.wait(lease.job))
        self.assertFalse(lease.wait(lease.job))
        lease.done(lease.job)
        self.assertEqual(lease.writes, [{'1.1.1.1', '8.8.8.8'}, {'1.1.1.1'}])
        self.assertFalse(lease.changed)

    def test_mismatched_job_or_changed_sdk_does_not_change_policy(self):
        lease = self.lease()
        with self.assertRaises(ValueError):
            lease.wait('job_99999999-2222-4333-8444-555555555555')
        lease.pinned.unchanged = lambda: False
        with self.assertRaises(ValueError):
            lease.wait(lease.job)
        self.assertEqual(lease.writes, [])

    def test_cleanup_refuses_to_overwrite_an_unrelated_policy_change(self):
        lease = self.lease()
        lease.wait(lease.job)
        lease.current = {'9.9.9.9'}
        with self.assertRaisesRegex(ValueError, 'media_egress_conflict'):
            lease.close()
        self.assertEqual(len(lease.writes), 1)

    def test_apply_failure_still_restores_original_policy(self):
        lease = self.lease()
        update = lease.set_policy
        def fail(values):
            update(values)
            if len(values) > 1:
                raise ValueError('media_egress_apply')
        lease.set_policy = fail
        with self.assertRaises(ValueError):
            lease.wait(lease.job)
        lease.close()
        self.assertEqual(lease.current, {'1.1.1.1'})

    def test_systemd_append_semantics_are_reset_before_restoring_allowlist(self):
        lease = self.lease()
        lease.unit = 'astrbot-wechat-slirp-tap-helper-' + 'a' * 12 + '.service'
        def systemctl(arguments, **kwargs):
            for argument in arguments:
                if argument.startswith('IPAddressAllow='):
                    value = argument.split('=', 1)[1]
                    if value:
                        lease.current |= policy.addresses(value)
                    else:
                        lease.current = set()
            return SimpleNamespace(returncode=0)
        with patch.object(policy.subprocess, 'run', side_effect=systemctl):
            policy.Lease.set_policy(lease, {'1.1.1.1', '8.8.8.8'})
            policy.Lease.set_policy(lease, {'1.1.1.1'})
        self.assertEqual(lease.current, {'1.1.1.1'})

    def test_failed_restore_is_not_reported_as_closed(self):
        lease = self.lease()
        lease.wait(lease.job)
        def failed(_):
            raise ValueError('media_egress_apply')
        lease.set_policy = failed
        with self.assertRaises(ValueError):
            lease.close()
        self.assertTrue(lease.changed)
