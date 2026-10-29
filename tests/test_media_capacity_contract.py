"""Synthetic resource limits: no services, private state or Native execution."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


def load(name):
    spec = importlib.util.spec_from_file_location('capacity_' + name,
        Path(__file__).resolve().parents[1] / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


host = load('session_runtime_host')
owner = load('session_runtime')
forwarder = load('run_private_forwarder')


class CapacityContract(unittest.TestCase):
    def test_actual_image_reservation_accepts_runtime_file_limit(self):
        mib = 1024*1024
        with patch.object(forwarder.owner, 'STATE', Path('/synthetic/state')), \
             patch.object(forwarder.Path, 'exists', return_value=False), \
             patch.object(forwarder, 'media_headroom', return_value=dict(storage_free=512*mib,
                memory_free=128*mib, database_bytes=100*mib, file_limit=768*mib, database_copies=2)), \
             patch.object(forwarder.owner, 'write_json') as write:
            self.assertTrue(forwarder.reserve_image_upload(object(), {'bytes':8*mib}))
        self.assertEqual(write.call_args.args[1]['attempts'], 1)

    def test_actual_guest_and_parent_start_arguments(self):
        with tempfile.TemporaryDirectory() as name:
            instance = host.SessionHost(Path(name), offline=True, seconds=30)
            with patch.object(host.os, 'geteuid', return_value=0), \
                 patch.object(host.owned, 'identity', return_value='same'), \
                 patch.object(host.tap, 'read_network'), patch.object(host.tap, 'business_snapshot'), \
                 patch.object(instance.store, 'prepare'), patch.object(instance.store, 'latest', return_value=None), \
                 patch.object(host.tap, 'require_unit_absent'), \
                 patch.object(host.tempfile, 'mkdtemp', return_value=name), \
                 patch.object(host.os, 'statvfs', return_value=SimpleNamespace(f_bavail=1024**3, f_frsize=1, f_flag=0)), \
                 patch.object(host.legacy, 'copy_asset'), patch.object(host.legacy, 'write_public'), \
                 patch.object(host.os, 'replace'), patch.object(host.tap, 'start_slice'), \
                 patch.object(host.tap, 'command') as command, \
                 patch.object(host.tap, 'cgroup_limits') as cgroup, \
                 patch.object(host.legacy, 'ClientCapture') as capture:
                instance.start()
            arguments = capture.call_args.args[0]
            for value in ('LimitFSIZE=768M', 'MemoryMax=2560M', 'MemoryHigh=2304M'):
                self.assertIn('--property=' + value, arguments)
            self.assertIn('size=2048M', ' '.join(arguments))
            self.assertIn('MemoryMax=3072M', command.call_args.args[0])
            self.assertIn('MemoryHigh=2816M', command.call_args.args[0])
            limits = cgroup.call_args.args[1]
            self.assertEqual(limits['memory.max'], str(3072*1024*1024))
            self.assertEqual(limits['memory.high'], str(2816*1024*1024))
            self.assertEqual(host.guest.empty.WORK_BYTES, 2048*1024*1024)

    def test_owner_can_write_largest_snapshot(self):
        with patch.object(owner, 'owner_active', return_value=False), \
             patch.object(owner, 'private_directory'), patch.object(owner.Path, 'unlink'), \
             patch.object(owner.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
             patch.object(owner, 'read_json', return_value={'ready':True, 'accounts':0, 'connected':False}), \
             patch.object(owner, 'wire'):
            self.assertEqual(owner.start_owner(True), 0)
        self.assertIn('--property=LimitFSIZE=1101004800', run.call_args.args[0])

    def test_headroom_counts_two_copies_for_both_native_volume_sizes(self):
        mib = 1024*1024
        pinned = SimpleNamespace(host=SimpleNamespace(data_fd=4, parent='synthetic', client_pid=7))
        for total, copies in ((192, 2), (2048, 2), (128, 0)):
            with self.subTest(total=total), \
                 patch.object(forwarder.os, 'fstatvfs', return_value=SimpleNamespace(f_blocks=total*mib, f_frsize=1, f_bavail=120*mib)), \
                 patch.object(forwarder.Path, 'read_text', side_effect=[str(1536*mib), str(128*mib)]), \
                 patch.object(forwarder.os, 'open', return_value=10), patch.object(forwarder.os, 'close'), \
                 patch.object(forwarder.os, 'listdir', return_value=['synthetic']), \
                 patch.object(forwarder.os, 'stat', side_effect=[SimpleNamespace(st_mode=0o100600, st_nlink=1, st_size=20*mib), FileNotFoundError(), FileNotFoundError()]), \
                 patch.object(forwarder.resource, 'prlimit', return_value=(768*mib,768*mib)):
                capacity = forwarder.media_headroom(pinned)
            self.assertEqual(capacity['database_copies'], copies)
            self.assertEqual(capacity['storage_free'], 56*mib if copies else 120*mib)


if __name__ == '__main__':
    unittest.main()
