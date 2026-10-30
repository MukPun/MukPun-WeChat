"""Synthetic HD resource contracts; no live services or private state."""
import ast
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def load(name):
    spec = importlib.util.spec_from_file_location('_hd_resources_' + name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


group = load('run_group_probe')
bridge = load('run_bridge_trial')
reader = load('group_reader_client')


def properties(values):
    return dict(value.removeprefix('--property=').split('=', 1) for value in values)


class HDResources(unittest.TestCase):
    def test_reader_properties_preserve_default_and_hd_isolation(self):
        for hd, maximum, high in ((False, '32M', '24M'), (True, '256M', '192M')):
            with self.subTest(hd=hd):
                values = properties(group.reader_properties(Path('/synthetic/bundle'), 'synthetic', 'synthetic.slice', 9, 60, large_images=hd))
                self.assertEqual((values['MemoryMax'], values['MemoryHigh']), (maximum, high))
                self.assertEqual(values['MemorySwapMax'], '0')
                self.assertEqual(values['TasksMax'], '4')
                self.assertEqual(values['Slice'], 'synthetic.slice')
                self.assertEqual(values['CapabilityBoundingSet'], '')
                self.assertEqual(values['RestrictNamespaces'], 'yes')

    def test_host_properties_only_bind_dedicated_hd_directory(self):
        for hd, maximum, high in ((False, '128M', '96M'), (True, '512M', '384M')):
            with self.subTest(hd=hd):
                values = properties(bridge.host_properties(Path('/synthetic/source'), 60, hd_images=hd))
                self.assertEqual((values['MemoryMax'], values['MemoryHigh']), (maximum, high))
                self.assertEqual(values['LimitFSIZE'], '33554432' if hd else '8388608')
                self.assertEqual(values['MemorySwapMax'], '0')
                self.assertEqual(values['TasksMax'], '8')
                self.assertEqual(values['BindReadOnlyPaths'], '/synthetic/source:/opt/bridge')
                if hd:
                    self.assertEqual(values['BindPaths'], str(ROOT.parent / 'AstrBot/data/temp/wechat-hd-input') + ':/var/lib/astrbot-wechat-bridge/hd-input')
                else:
                    self.assertNotIn('BindPaths', values)

    def test_verifiers_use_exact_selected_memory_pair(self):
        for module, verify, flag, unit, cgroup, command, pairs in (
            (group, group.verify_reader, 'large_images', 'reader.service', '/synthetic.slice/reader.service', [b'/usr/bin/python3', b'-I', b'-B', b'/opt/group_reader_client.py'], ((32, 24), (256, 192))),
            (bridge, bridge.verify_host, 'hd_images', 'host.service', '/system.slice/host.service', [b'/usr/bin/python3', b'-I', b'-B', b'/opt/bridge/scripts/bridge_host_worker.py'], ((128, 96), (512, 384))),
        ):
            for hd, (maximum, high) in enumerate(pairs):
                with self.subTest(verifier=verify.__name__, hd=hd), ExitStack() as stack:
                    pinned = Mock(netns='synthetic-identity')
                    pinned.host = SimpleNamespace(parent='synthetic.slice', client_uid=100, _client_alive=Mock())
                    stack.enter_context(patch.object(module.tap, 'details', return_value={'MainPID': '123', 'ActiveState': 'active', 'ControlGroup': cgroup}))
                    stack.enter_context(patch.object(module.tap, 'start_ticks', return_value=9))
                    stack.enter_context(patch.object(module.owned, 'identity', return_value='synthetic-identity'))
                    stack.enter_context(patch.object(module.owned, 'read', side_effect=lambda path: '0::' + cgroup if str(path).endswith('/cgroup') else 'status'))
                    stack.enter_context(patch.object(module.owned, 'parse_status', return_value={'Uid': '200 200 200 200', 'Gid': '200 200 200 200', 'NoNewPrivs': '1'}))
                    stack.enter_context(patch.object(module.owned, 'zero_caps', return_value=True))
                    stack.enter_context(patch.object(group if module is group else module.groups, 'argv', return_value=command))
                    caps = stack.enter_context(patch.object(module.tap, 'cgroup_limits'))
                    verify(unit, pinned, **{flag: bool(hd)})
                    actual = caps.call_args.args[1]
                    self.assertEqual(actual['memory.max'], str(maximum * MIB))
                    self.assertEqual(actual['memory.high'], str(high * MIB))
                    self.assertEqual(actual['memory.swap.max'], '0')

    def test_reader_accepts_only_exact_hd_legacy_and_default_pairs(self):
        parent = '/sys/fs/cgroup/astrbotwechatprobe' + 'a' * 12 + '.slice'
        child = parent + '/astrbot-wechat-group-read-' + 'b' * 12 + '.service'
        for maximum, high, accepted in ((256, 192, True), (96, 72, True), (32, 24, True), (256, 72, False), (96, 192, False), (257, 192, False)):
            def read_text(path):
                name = str(path)
                if name == '/proc/self/status':
                    return '\n'.join(key + ':0' for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')) + '\nNoNewPrivs:1'
                if name == '/proc/self/cgroup':
                    return '0::/' + child.removeprefix('/sys/fs/cgroup/')
                if name == '/proc/net/dev':
                    return 'header\nheader\nlo:0\ntap0:0'
                root, field = name.rsplit('/', 1)
                return {'memory.max': str((3072 if root == parent else maximum) * MIB),
                        'memory.high': str((2816 if root == parent else high) * MIB),
                        'memory.swap.max': '0', 'pids.max': '32' if root == parent else '4',
                        'cpu.max': '20000 100000'}[field]
            with self.subTest(maximum=maximum, high=high), \
                 patch.object(reader.Path, 'read_text', read_text), \
                 patch.object(reader, '_mounts', return_value=[]), \
                 patch.object(reader, '_readonly', return_value=True), \
                 patch.object(reader, '_masked', return_value=True), \
                 patch.object(reader, '_containing', return_value={'fstype': 'tmpfs'}), \
                 patch.object(reader.Path, 'lstat', return_value=SimpleNamespace(st_mode=0o100444, st_uid=0, st_nlink=1, st_size=100)):
                self.assertEqual(reader.isolation_checks()['resource_limits'], accepted)

    def test_active_inspections_expect_formal_hd_limits(self):
        # Inspect the literal acceptance tables without invoking their live main().
        expected = {}
        for name in ('accept_forwarder_core', 'inspect_storage_runtime'):
            tree = ast.parse((ROOT / 'scripts' / (name + '.py')).read_text())
            tables = [node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'expected' for target in node.targets)]
            self.assertEqual(len(tables), 1)
            expected[name] = ast.literal_eval(tables[0])
        self.assertEqual(expected['accept_forwarder_core']['onebot'], ('536870912', '402653184', '8', '10000 100000'))
        self.assertEqual(expected['accept_forwarder_core']['native_parent'], ('3221225472', '2952790016', '32', '20000 100000'))
        self.assertEqual(expected['inspect_storage_runtime'], {'native_parent': 3072, 'native_sdk': 2560, 'native_reader': 256, 'onebot': 512, 'controller': 64, 'owner': 64})
