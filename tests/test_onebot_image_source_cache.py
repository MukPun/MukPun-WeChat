import importlib.util
from pathlib import Path
import unittest
import uuid

spec=importlib.util.spec_from_file_location('cache_contract',Path(__file__).resolve().parents[1]/'scripts/image_source_cache.py')
cache=importlib.util.module_from_spec(spec);spec.loader.exec_module(cache)


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.now=100000
        self.image={'sha256':'a'*64,'bytes':1234,'mime':'image/png'}
        self.source='upload_'+str(uuid.uuid4())
        self.c=cache.Cache(None,'123:456',lambda:self.now)

    def test_same_native_and_exact_content_reuses_persisted_source(self):
        self.c.put(self.image,self.source)
        restored=cache.Cache(self.c.value(),'123:456',lambda:self.now)
        self.assertEqual(restored.lookup(self.image),self.source)
        for change in ({'sha256':'b'*64},{'bytes':1235},{'mime':'image/jpeg'}):
            self.assertIsNone(restored.lookup({**self.image,**change}))
        self.assertNotIn('base64',str(self.c.value()))

    def test_new_native_or_expiry_never_reuses_old_source(self):
        self.c.put(self.image,self.source)
        self.assertIsNone(cache.Cache(self.c.value(),'124:457',lambda:self.now).lookup(self.image))
        self.now+=cache.TTL+1
        self.assertIsNone(self.c.lookup(self.image))

    def test_lru_index_is_bounded_and_invalid_identity_is_rejected(self):
        for i in range(70):
            self.c.put({**self.image,'sha256':format(i,'064x')},self.source)
            self.now+=1
        self.assertEqual(len(self.c.entries),64)
        self.assertNotIn(format(0,'064x'),self.c.entries)
        with self.assertRaises(ValueError):self.c.put(self.image,'/private/source')

    def test_failed_source_is_evicted(self):
        self.c.put(self.image,self.source)
        self.c.discard(self.image['sha256'])
        self.assertIsNone(self.c.lookup(self.image))
