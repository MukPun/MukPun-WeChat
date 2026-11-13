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
            tool.delivery.PUBLIC_CODE=('scripts/run_onebot_tests.py','wechat_bridge/example.py')
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


class PackageDelivery(unittest.TestCase):
    def setUp(self):
        self.tool=load('package_sync',Path(__file__).resolve().parents[1]/'tools/sync_runtime.py')
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.tool.ROOT=self.root
        for name in ('scripts/run_onebot_tests.py','wechat_bridge/__init__.py',
                     'wechat_bridge/core/__init__.py','wechat_bridge/core/contracts.py'):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# synthetic\n')
        self.files=tuple(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*.py')))

    def configure(self):
        self.tool.delivery.PUBLIC_CODE=self.files

    def test_packages_and_root_runner_are_delivered(self):
        self.configure()
        self.tool.sync(True)
        target=self.root/'.runtime/verification'
        self.assertEqual((target/'wechat_bridge/core/contracts.py').read_text(),'# synthetic\n')
        self.assertEqual((target/'run_onebot_tests.py').read_bytes(),(target/'scripts/run_onebot_tests.py').read_bytes())
        self.assertTrue(self.tool.sync()['ok'])

    def test_stale_managed_source_removed_dependencies_and_data_retained(self):
        self.configure();self.tool.sync(True)
        target=self.root/'.runtime/verification'
        for relative in ('onebot-env/keep','data/keep'):
            path=target/relative;path.parent.mkdir(parents=True);path.write_text('keep')
        self.tool.delivery.PUBLIC_CODE=tuple(p for p in self.files if p!='wechat_bridge/core/contracts.py')
        self.assertFalse(self.tool.sync()['ok'])
        self.tool.sync(True)
        self.assertFalse((target/'wechat_bridge/core/contracts.py').exists())
        self.assertTrue((target/'onebot-env/keep').exists());self.assertTrue((target/'data/keep').exists())

    def test_source_directory_symlink_rejected(self):
        self.configure()
        package=self.root/'wechat_bridge/core'
        package.rename(self.root/'outside');package.symlink_to(self.root/'outside',target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'source_symlink'):self.tool.sync(True)

    def test_destination_directory_symlink_rejected_without_writing_outside(self):
        self.configure()
        outside=self.root/'outside';outside.mkdir()
        target=self.root/'.runtime/verification';target.mkdir(parents=True)
        (target/'wechat_bridge').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'runtime_symlink'):self.tool.sync(True)
        self.assertEqual(list(outside.iterdir()),[])

    def test_staging_private_and_traversal_entries_rejected(self):
        self.configure()
        for relative in ('.runtime/private.py','acceptance/release-staging/private.py',
                         'wechat_bridge/.publish/private.py','wechat_bridge/data/private.py',
                         'scripts/../private.py','/tmp/private.py'):
            with self.subTest(relative=relative):
                self.tool.delivery.PUBLIC_CODE=(relative,)
                with self.assertRaisesRegex(ValueError,'public_code_path'):self.tool.sync(True)

    def test_stale_manifest_cannot_delete_private_runtime(self):
        self.configure();self.tool.sync(True)
        private=self.root/'.runtime/verification/onebot-env/keep.py'
        private.parent.mkdir();private.write_text('keep')
        manifest=self.root/'.runtime/worker-manifest.json'
        value=json.loads(manifest.read_text());value['onebot-env/keep.py']='0'*64
        manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'public_code_path'):self.tool.sync(True)
        self.assertEqual(private.read_text(),'keep')

    def test_publication_ignores_scratch_reports(self):
        audit=load('scratch_audit',Path(__file__).resolve().parents[1]/'tools/audit_publication.py')
        scratch=self.root/'.superpowers/sdd';scratch.mkdir(parents=True)
        (scratch/'report.md').write_text('scratch only')
        findings,files=audit.audit(self.root)
        self.assertFalse(findings)
        self.assertFalse(any('.superpowers' in item['path'] for item in files))

    def test_unlisted_staging_and_private_sources_are_not_delivered(self):
        self.configure()
        for relative in ('scripts/private_extra.py','wechat_bridge/staging/unreviewed.py',
                         '.superpowers/sdd/helper.py','.runtime/private.py'):
            path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# excluded\n')
        self.tool.sync(True)
        target=self.root/'.runtime/verification'
        for relative in ('scripts/private_extra.py','wechat_bridge/staging/unreviewed.py',
                         '.superpowers/sdd/helper.py','.runtime/private.py'):
            self.assertFalse((target/relative).exists(),relative)


class AcceptanceStaging(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.accept = load('acceptance_staging', root/'scripts/accept_reboot_recovery.py')
        self.manifest = load('acceptance_manifest', root/'scripts/runtime_manifest.py')
        self.accept.module = lambda name: self.manifest
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.accept.STAGE = str(self.root/'staged-')
        self.source = self.root/'source'
        self.files = ('scripts/run_onebot_tests.py', 'wechat_bridge/core/contracts.py')
        self.manifest.PUBLIC_CODE = self.files
        for relative in self.files:
            path = self.source/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# synthetic public code\n')

    def test_only_reviewed_sources_and_runner_alias_are_staged(self):
        for relative in ('scripts/context-staging/private.py', 'scripts/private/secret.py',
                         'tests/unreviewed.py'):
            path = self.source/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# must not be staged\n')
        (self.source/'scripts/unlisted.py').symlink_to(self.source/self.files[0])
        target = self.accept.stage_tree(self.source)
        staged = {p.relative_to(target).as_posix() for p in target.rglob('*.py')}
        self.assertEqual(staged, {*self.files, 'run_onebot_tests.py'})
        self.assertEqual((target/'run_onebot_tests.py').read_bytes(),
                         (self.source/self.files[0]).read_bytes())
        if os.name == 'posix':
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual((target/self.files[1]).stat().st_mode & 0o777, 0o644)

    def test_registered_file_and_parent_symlinks_are_rejected_and_cleaned(self):
        path = self.source/self.files[1]
        outside = self.root/'outside.py'
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'source_symlink'):
            self.accept.stage_tree(self.source)
        self.assertFalse(list(self.root.glob('staged-*')))
        path.unlink()
        parent = path.parent
        parent.rmdir()
        parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'source_symlink'):
            self.accept.stage_tree(self.source)
        self.assertFalse(list(self.root.glob('staged-*')))

    def test_missing_reviewed_source_rejects_incomplete_stage(self):
        (self.source/self.files[1]).unlink()
        with self.assertRaisesRegex(ValueError, 'public_code_missing'):
            self.accept.stage_tree(self.source)
        self.assertFalse(list(self.root.glob('staged-*')))


class PrivateCanaryPaths(unittest.TestCase):
    def test_symlinked_runtime_is_rejected_before_any_private_write(self):
        spec = importlib.util.spec_from_file_location('canary_deploy_paths', Path(__file__).resolve().parents[1]/'tools/deploy_private_canary.py')
        deploy = importlib.util.module_from_spec(spec); spec.loader.exec_module(deploy)
        self.assertTrue(hasattr(deploy,'check_runtime_paths'))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'project'; root.mkdir()
            outside = Path(temporary)/'outside'; outside.mkdir()
            config = outside/'forwarder.json'; config.write_text('unchanged')
            (root/'.runtime').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError): deploy.check_runtime_paths(root)
            self.assertEqual(config.read_text(),'unchanged')
            (root/'.runtime').unlink(); (root/'.runtime').mkdir()
            deploy.check_runtime_paths(root)

    def test_pause_only_the_known_project_alert_dropin_and_preserve_bytes(self):
        deploy = load('canary_alert_pause',Path(__file__).resolve().parents[1]/'tools/deploy_private_canary.py')
        self.assertTrue(hasattr(deploy,'pause_alert'))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)/'alert.conf'; paused = Path(temporary)/'alert.conf.private-canary-paused'
            original = b'[Unit]\nOnFailure=astrbot-wechat-forwarder-alert.service\n'
            source.write_bytes(original); source.chmod(0o600)
            deploy.pause_alert(source,paused)
            self.assertFalse(source.exists()); self.assertEqual(paused.read_bytes(),original)
            deploy.pause_alert(source,paused)  # Interrupted rollout can re-enter safely.
            paused.unlink(); source.write_text('[Unit]\nOnFailure=another.service\n')
            with self.assertRaises(ValueError): deploy.pause_alert(source,paused)
            self.assertTrue(source.exists()); self.assertFalse(paused.exists())
