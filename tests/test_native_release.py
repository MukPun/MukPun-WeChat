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
        expected_sha = '6f4ac76232f48744fbf015f677b255a0162170252023ccf2ab677f5fac2555f7'
        self.assertEqual(guest.EXPECTED_VERSION, '1.2.0')
        self.assertEqual(paths.NATIVE_ASSET, paths.RUNTIME_ROOT / 'assets' / 'wx-v1.2.0-voice-format-linux-x64-gnu')
        self.assertEqual(host.ASSET, paths.NATIVE_ASSET)
        self.assertEqual(host.ASSET_SIZE, 34886144)
        self.assertEqual(host.ASSET_SHA, expected_sha)
        self.assertEqual(guest.EXPECTED_SHA256, expected_sha)
        # The standalone shell launcher must allow the same exact asset copy.
        shell = (ROOT / 'scripts/run-empty-client-probe.sh').read_text()
        self.assertIn(paths.NATIVE_ASSET.name + '\"', shell)
        self.assertIn('task_sha=' + expected_sha + '\n', shell)
        self.assertIn("= '" + str(host.ASSET_SIZE) + "' ]", shell)
        self.assertIn('--fsize=' + str(host.ASSET_SIZE) + ':' + str(host.ASSET_SIZE), shell)


if __name__ == '__main__':
    unittest.main()
