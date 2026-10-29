"""Bind every production startup guard to the reviewed Native release."""
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('release_test_' + name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class NativeRelease(unittest.TestCase):
    def test_host_copy_and_guest_startup_bind_the_same_verified_release(self):
        paths, host, guest = (load(name) for name in ('project_paths', 'run_login_probe', 'empty_client_probe'))
        expected_sha = '286b5661582b8d17594b208bf4bec19f7db182f12f9827733cfc0f6fde61efb1'
        self.assertEqual(guest.EXPECTED_VERSION, '1.2.0')
        self.assertEqual(paths.NATIVE_ASSET, paths.RUNTIME_ROOT / 'assets' / 'wx-v1.2.0-linux-x64-gnu')
        self.assertEqual(host.ASSET, paths.NATIVE_ASSET)
        self.assertEqual(host.ASSET_SIZE, 34761720)
        self.assertEqual(host.ASSET_SHA, expected_sha)
        self.assertEqual(guest.EXPECTED_SHA256, expected_sha)


if __name__ == '__main__':
    unittest.main()
