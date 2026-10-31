import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from wechat_bridge import image_input as images
from wechat_bridge.image_input_files import ImageFiles


def image(extra=140000):
    return images.payload(b'\x89PNG\r\n\x1a\n'+b'\0'*4+b'IHDR'+struct.pack('>II',2,2)+b'x'*extra,'image/png','original')


class HDImageFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 10000
        self.files = ImageFiles(self.root, clock=lambda:self.now)

    def test_large_preserves_bytes_and_reuses_verified_file(self):
        import base64
        value = image()
        result = self.files.stage(value)
        target = self.root / (value['sha256']+'.png')
        self.assertEqual(target.read_bytes(),base64.b64decode(value['base64']))
        self.assertEqual(result,dict(images.metadata(value),file=images.export_uri(images.metadata(value))))
        self.assertEqual(target.stat().st_mode & 0o777,0o600)
        self.now += 10
        self.assertEqual(self.files.stage(value),result)
        self.assertEqual(target.stat().st_mtime,self.now)
        target.write_bytes(b'bad')
        with self.assertRaises(images.ImageError):self.files.stage(value)

    def test_small_images_are_exported_like_large_ones(self):
        import base64
        value = image(0)
        result = self.files.stage(value)
        meta = images.metadata(value)
        self.assertEqual(result, dict(meta, file=images.export_uri(meta)))
        self.assertEqual((self.root/(meta['sha256']+'.png')).read_bytes(), base64.b64decode(value['base64']))

    def test_inline_survives_only_when_the_file_cache_is_full(self):
        value = image(0)
        with patch('wechat_bridge.image_input_files.MAX_ENTRIES', 0):
            self.assertIs(self.files.stage(value), value)
        with patch('wechat_bridge.image_input_files.MAX_ENTRIES', 0):
            with self.assertRaisesRegex(images.ImageError, 'image_capacity'):
                self.files.stage(image(140001))

    def test_no_incoming_descriptor(self):
        value = image(0)
        with self.assertRaises(images.ImageError):self.files.stage(dict(images.metadata(value),file=images.export_uri(images.metadata(value))))

    def test_cleanup_ttl_keeps_unrelated(self):
        value=image();self.files.stage(value)
        unrelated=self.root/'notes';unrelated.write_text('keep')
        self.now+=599;self.files.cleanup()
        self.assertEqual(len(list(self.root.iterdir())),2)
        self.now+=2;self.files.cleanup()
        self.assertEqual(list(self.root.iterdir()),[unrelated])

    def test_capacity_never_evicts_live_files(self):
        self.files.stage(image())
        with patch('wechat_bridge.image_input_files.MAX_ENTRIES',1):
            with self.assertRaisesRegex(images.ImageError,'image_capacity'):self.files.stage(image(140001))
        with patch('wechat_bridge.image_input_files.MAX_TOTAL_BYTES',140025):
            with self.assertRaisesRegex(images.ImageError,'image_capacity'):self.files.stage(image(140001))
        self.assertEqual(len(list(self.root.iterdir())),1)

    def test_unsafe_links_and_directory_rejected(self):
        value=image();name=value['sha256']+'.png'
        target=self.root/'other';target.write_bytes(b'private')
        (self.root/name).symlink_to(target)
        with self.assertRaises(images.ImageError):self.files.stage(value)
        (self.root/name).unlink();os.link(target,self.root/name)
        with self.assertRaises(images.ImageError):self.files.stage(value)
        os.chmod(self.root,0o755)
        with self.assertRaises(images.ImageError):ImageFiles(self.root)

    def test_name_digest_binding_and_expired_orphan(self):
        value=image();self.files.stage(value)
        path=self.root/(value['sha256']+'.png')
        self.assertEqual(path.name,value['sha256']+'.png')
        path.write_bytes(b'corrupt')
        with self.assertRaises(images.ImageError):self.files.stage(value)
        path.unlink()
        orphan=self.root/('.hd-input-'+'a'*32+'.tmp');orphan.write_bytes(b'x');orphan.chmod(0o600)
        os.utime(orphan,(self.now-601,self.now-601));self.files.cleanup()
        self.assertFalse(orphan.exists())

    def test_wrong_hash_and_unsafe_managed_modes_are_rejected(self):
        value=image();bad=dict(value,sha256='0'*64)
        with self.assertRaises(images.ImageError):self.files.stage(bad)
        self.assertEqual(list(self.root.iterdir()),[])
        self.files.stage(value)
        path=self.root/(value['sha256']+'.png');path.chmod(0o644)
        self.now+=601
        with self.assertRaises(images.ImageError):self.files.cleanup()
        self.assertTrue(path.exists())

    def test_symlink_directory_is_rejected(self):
        link=self.root/'directory-link';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(images.ImageError):ImageFiles(link)

    def test_expired_orphan_releases_capacity(self):
        orphan=self.root/('.hd-input-'+'b'*32+'.tmp');orphan.write_bytes(b'x');orphan.chmod(0o600)
        os.utime(orphan,(self.now,self.now))
        with patch('wechat_bridge.image_input_files.MAX_ENTRIES',1):
            with self.assertRaisesRegex(images.ImageError,'image_capacity'):self.files.stage(image())
            self.now+=601
            self.files.stage(image())
        self.assertFalse(orphan.exists())
        self.assertEqual(len(list(self.root.iterdir())),1)

    def test_cleanup_and_unrelated_stage_do_not_read_file_contents(self):
        value=image();self.files.stage(value)
        with patch('wechat_bridge.image_input_files.os.read',side_effect=AssertionError('inventory read image bytes')):
            self.files.cleanup()
            self.files.stage(image(140001))
        path=self.root/(value['sha256']+'.png')
        with path.open('r+b') as stream:
            stream.seek(-1,2);stream.write(b'z')
        with self.assertRaises(images.ImageError):self.files.stage(value)
