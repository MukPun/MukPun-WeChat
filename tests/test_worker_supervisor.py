"""Crash budget for in-place worker restarts.

The bridge used to treat any worker death as fatal and let systemd arbitrate,
with StartLimitBurst=3 inside StartLimitIntervalSec=600. Three deaths in ten
minutes permanently disabled the unit, which is how a 13.5-crashes-per-day
service once stayed down for 37 hours. These tests pin the narrower window
that distinguishes "unlucky" from "durably broken".
"""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('worker_supervisor_test', ROOT / 'scripts/worker_supervisor.py')
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def make(name='native', **options):
    clock = Clock()
    options.setdefault('window', 10.0)
    options.setdefault('limit', 3)
    return supervisor.Supervisor(name, clock=clock, **options), clock


class Budget(unittest.TestCase):
    def test_first_two_deaths_still_allow_a_restart(self):
        item, clock = make()
        self.assertEqual(item.record(), 1)
        self.assertTrue(item.recoverable())
        clock.advance(1)
        self.assertEqual(item.record(), 2)
        self.assertTrue(item.recoverable())

    def test_third_death_inside_the_window_stops_the_budget(self):
        item, clock = make()
        for _ in range(2):
            item.record()
            clock.advance(1)
        self.assertEqual(item.record(), 3)
        self.assertFalse(item.recoverable())

    def test_deaths_outside_the_window_do_not_accumulate(self):
        item, clock = make()
        # Three deaths spread over ten minutes is the pattern systemd punished
        # and it is not evidence of a durable defect.
        for _ in range(3):
            item.record()
            clock.advance(200)
        self.assertEqual(item.record(), 1)
        self.assertTrue(item.recoverable())

    def test_window_slides_rather_than_resetting(self):
        item, clock = make()
        item.record()
        clock.advance(9)
        item.record()
        clock.advance(9)
        # The first death aged out exactly when the third arrived.
        self.assertEqual(item.record(), 2)
        self.assertTrue(item.recoverable())

    def test_clock_going_backwards_does_not_wedge_the_window(self):
        item, clock = make()
        item.record()
        item.record()
        clock.advance(-500)
        self.assertEqual(item.record(), 1)
        self.assertTrue(item.recoverable())

    def test_recoverable_itself_never_records(self):
        item, _ = make()
        item.record()
        self.assertTrue(item.recoverable())
        self.assertTrue(item.recoverable())
        self.assertEqual(len(item.crashes), 1)

    def test_restarts_are_counted_separately_from_crashes(self):
        item, _ = make()
        item.record()
        self.assertEqual(item.restarted(), 1)
        self.assertEqual(item.restarted(), 2)
        self.assertEqual(item.state()['crashes'], 1)
        self.assertEqual(item.state()['restarts'], 2)


class Shapes(unittest.TestCase):
    def test_state_is_bounded_and_json_safe(self):
        item, _ = make()
        item.record()
        value = item.state()
        self.assertEqual(set(value), {'name', 'crashes', 'restarts', 'window', 'limit'})
        self.assertEqual(value['name'], 'native')
        self.assertEqual(value['window'], 10.0)
        self.assertEqual(value['limit'], 3)

    def test_empty_name_is_rejected(self):
        for bad in ('', None, 1):
            with self.assertRaises(ValueError):
                supervisor.Supervisor(bad)

    def test_non_positive_bounds_are_rejected(self):
        for window, limit in ((0, 3), (10, 0), (-1, 3), (10, -2)):
            with self.assertRaises(ValueError):
                supervisor.Supervisor('native', window=window, limit=limit)

    def test_defaults_match_napcat(self):
        self.assertEqual(supervisor.WINDOW, 10.0)
        self.assertEqual(supervisor.LIMIT, 3)


if __name__ == '__main__':
    unittest.main()
