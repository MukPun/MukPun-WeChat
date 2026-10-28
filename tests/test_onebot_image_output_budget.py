import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('test_output_budget',
    Path(__file__).resolve().parents[1] / 'scripts/image_output_budget.py')
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


class OutputBudget(unittest.TestCase):
    def test_media_input_pressure_reserves_storage_database_and_memory_for_text(self):
        capacity=dict(storage_free=64*1024*1024,memory_free=128*1024*1024,
                      database_bytes=20*1024*1024,file_limit=128*1024*1024)
        self.assertTrue(budget.allow_input(**capacity))
        for update in ({'storage_free':31*1024*1024},{'memory_free':63*1024*1024},
                       {'database_bytes':97*1024*1024}):
            self.assertFalse(budget.allow_input(**{**capacity,**update}))

    def test_reused_source_avoids_upload_growth_and_keeps_counters(self):
        state={'schemaVersion':1,'attempts':8,'sourceBytes':1251058}
        kwargs=dict(storage_free=18*1024*1024,memory_free=128*1024*1024,
                    database_bytes=20*1024*1024,file_limit=128*1024*1024)
        self.assertIsNone(budget.reserve(state,8*1024*1024,**kwargs))
        self.assertEqual(budget.reserve(state,8*1024*1024,**kwargs,reused_source=True),state)

    def test_verified_native_limit_can_be_32mb_without_relaxing_other_guards(self):
        kwargs = dict(storage_free=18*1024*1024, memory_free=80*1024*1024, database_bytes=20*1024*1024)
        self.assertIsNone(budget.reserve(None, 65536, **kwargs))
        self.assertIsNotNone(budget.reserve(None, 65536, **kwargs, file_limit=32*1024*1024))
        self.assertIsNone(budget.reserve(None, 1024*1024, **kwargs, file_limit=32*1024*1024))
        with self.assertRaises(ValueError):
            budget.reserve(None, 65536, **kwargs, file_limit=64*1024*1024)

    def test_counter_survives_serialization_and_does_not_reset_at_capacity(self):
        import json
        state = None
        for _ in range(16):
            state = budget.reserve(state, 1000, storage_free=20*1024*1024, memory_free=80*1024*1024)
            state = json.loads(json.dumps(state))
        self.assertEqual(state['attempts'], 16)
        self.assertIsNotNone(budget.reserve(state, 1, storage_free=20*1024*1024, memory_free=80*1024*1024))
        self.assertIsNone(budget.reserve({**state,'attempts':budget.MAX_ATTEMPTS}, 1,
                        storage_free=20*1024*1024, memory_free=80*1024*1024))
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
                         memory_free=80*1024*1024, database_bytes=8*1024*1024)['attempts'], 1)

    def test_eight_megabyte_source_requires_capacity_and_allocation_reserve(self):
        self.assertIsNotNone(budget.reserve(None, 8*1024*1024, storage_free=64*1024*1024,
            memory_free=128*1024*1024,database_bytes=20*1024*1024,file_limit=128*1024*1024))
        self.assertIsNone(budget.reserve(None, 8*1024*1024, storage_free=24*1024*1024,
            memory_free=128*1024*1024,database_bytes=20*1024*1024,file_limit=128*1024*1024))
        with self.assertRaises(ValueError):
            budget.reserve(None, 8*1024*1024+1, storage_free=64*1024*1024,
                memory_free=128*1024*1024,file_limit=128*1024*1024)
