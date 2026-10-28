import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


class ProjectLayout(unittest.TestCase):
    def test_instance_paths_are_checkout_relative_and_private(self):
        root=Path(__file__).resolve().parents[1]
        paths=load('layout_paths',root/'scripts/project_paths.py')
        self.assertEqual(paths.PROJECT_ROOT,root)
        self.assertEqual(paths.CONFIG,root/'.runtime/forwarder.json')
        self.assertEqual(paths.VERIFICATION,root/'.runtime/verification')
        self.assertEqual(paths.NATIVE_ASSET.parent,root/'.runtime/assets')
        self.assertEqual(paths.ASTRBOT_DATA,root.parent/'AstrBot/data')

    def test_worker_sync_preserves_dependencies_and_detects_modified_code(self):
        tool=load('layout_sync',Path(__file__).resolve().parents[1]/'tools/sync_runtime.py')
        with tempfile.TemporaryDirectory() as directory:
            tool.ROOT=Path(directory)
            for name in ('scripts','tests','wechat_bridge','tools'):(tool.ROOT/name).mkdir()
            (tool.ROOT/'scripts/run_onebot_tests.py').write_text('# synthetic runner\n')
            source=tool.ROOT/'wechat_bridge/example.py';source.write_text('answer=42\n')
            environment=tool.ROOT/'.runtime/verification/onebot-env'
            environment.mkdir(parents=True);(environment/'keep').write_bytes(b'fixture-dependency')
            self.assertFalse(tool.sync()['ok'])
            self.assertTrue(tool.sync(True)['ok']);self.assertTrue(tool.sync()['ok'])
            self.assertEqual((environment/'keep').read_bytes(),b'fixture-dependency')
            target=tool.ROOT/'.runtime/verification/wechat_bridge/example.py'
            if os.name=='posix':
                self.assertEqual(target.stat().st_mode&0o777,0o644)
                self.assertEqual(target.parent.stat().st_mode&0o777,0o755)
            target.write_text('answer=0\n')
            self.assertFalse(tool.sync()['ok'])
            tool.sync(True)
            self.assertEqual(target.read_bytes(),source.read_bytes())
            manifest=json.loads((tool.ROOT/'.runtime/worker-manifest.json').read_text())
            self.assertEqual(len(manifest),3)
