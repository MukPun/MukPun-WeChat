import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('test_output_budget',
    Path(__file__).resolve().parents[1] / 'scripts/image_output_budget.py')
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


class OutputBudget(unittest.TestCase):
    def test_counter_survives_serialization_and_does_not_reset_at_capacity(self):
        import json
        state = None
        for _ in range(16):
            state = budget.reserve(state, 1000, storage_free=20*1024*1024, memory_free=40*1024*1024)
            state = json.loads(json.dumps(state))
        self.assertEqual(state['attempts'], 16)
        self.assertIsNone(budget.reserve(state, 1, storage_free=20*1024*1024, memory_free=40*1024*1024))
        self.assertEqual(state['sourceBytes'], 16000)

    def test_low_space_or_memory_rejects_without_reserving(self):
        state = {'schemaVersion': 1, 'attempts': 2, 'sourceBytes': 2000}
        for storage, memory in ((8*1024*1024, 40*1024*1024), (20*1024*1024, 31*1024*1024)):
            self.assertIsNone(budget.reserve(state, 1000, storage_free=storage, memory_free=memory))
        self.assertEqual(state['attempts'], 2)

    def test_corrupt_or_negative_budget_is_not_reinitialized(self):
        for state in ({}, {'schemaVersion': 1, 'attempts': -1, 'sourceBytes': 0},
                      {'schemaVersion': 1, 'attempts': True, 'sourceBytes': 0}):
            with self.assertRaises(ValueError):
                budget.reserve(state, 1000, storage_free=20*1024*1024, memory_free=40*1024*1024)

    def test_large_source_reserves_growth_and_rejects_near_file_limit(self):
        self.assertIsNone(budget.reserve(None, 1024*1024, storage_free=10*1024*1024,
                                        memory_free=40*1024*1024))
        self.assertIsNone(budget.reserve(None, 1024*1024, storage_free=20*1024*1024,
                                        memory_free=40*1024*1024, database_bytes=14*1024*1024))
        self.assertEqual(budget.reserve(None, 1024*1024, storage_free=20*1024*1024,
                         memory_free=40*1024*1024, database_bytes=8*1024*1024)['attempts'], 1)
