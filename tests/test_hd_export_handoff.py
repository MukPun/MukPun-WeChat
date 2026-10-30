"""Synthetic ownership handoffs using real files and modeled UIDs, never root."""
from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER, OLD, FIRST, NEXT = 1000, 61001, 61002, 61003


def load(name):
    spec = importlib.util.spec_from_file_location('_hd_handoff_' + name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export = load('image_input_export')
runtime = load('session_runtime')


class Interrupted(BaseException):
    pass


class HDExportHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'data/temp/wechat-hd-input'
        self.directory.mkdir(parents=True, mode=0o700)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.record = self.state / 'image-export-owner.json'
        self.pending = self.state / '.image-export-owner.json.next'
        self.owners = {}
        self.events = []
        self.interrupt = None
        self.real_lstat, self.real_fstat = os.lstat, os.fstat
        self.real_stat, self.real_unlink = os.stat, os.unlink
        self.paths = {}
        self.files = [self.create('a' * 64 + '.png', b'image-one'),
                      self.create('.hd-input-' + 'b' * 32 + '.tmp', b'partial-image')]
        self.remember(self.directory, OLD)
        self.unrelated = self.root / 'data/other-astrbot-file'
        self.unrelated.write_bytes(b'preserve other AstrBot data')
        runtime.write_json(self.record, {'schemaVersion': 1, 'uid': OLD})
        self.owner = SimpleNamespace(STATE=self.state, read_json=runtime.read_json, write_json=self.write_json)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(os, 'geteuid', return_value=CONTROLLER))
        stack.enter_context(patch.object(os, 'lstat', side_effect=self.lstat))
        stack.enter_context(patch.object(os, 'stat', side_effect=self.stat))
        stack.enter_context(patch.object(os, 'unlink', side_effect=self.unlink))
        stack.enter_context(patch.object(os, 'fstat', side_effect=self.fstat))
        stack.enter_context(patch.object(os, 'chown', side_effect=self.chown))
        stack.enter_context(patch.object(os, 'fchown', side_effect=self.fchown))

    def key(self, info):
        return info.st_dev, info.st_ino

    def remember(self, path, uid):
        key = self.key(self.real_lstat(path))
        self.owners[key] = uid
        self.paths[key] = path.name

    def create(self, name, data=b'preserved', uid=OLD):
        path = self.directory / name
        path.write_bytes(data)
        path.chmod(0o600)
        self.remember(path, uid)
        return path

    def modeled(self, info):
        fields = list(info)
        fields[4] = self.owners.get(self.key(info), CONTROLLER)
        return os.stat_result(fields)

    def lstat(self, path, **kwargs):
        return self.modeled(self.real_lstat(path, **kwargs))

    def stat(self, path, **kwargs):
        return self.modeled(self.real_stat(path, **kwargs))

    def unlink(self, path, **kwargs):
        key = self.key(self.real_lstat(path, **kwargs))
        self.real_unlink(path, **kwargs)
        self.owners.pop(key, None)
        self.paths.pop(key, None)

    def fstat(self, fd):
        return self.modeled(self.real_fstat(fd))

    def change(self, info, uid):
        key = self.key(info)
        self.owners[key] = uid
        self.events.append(('chown', self.paths.get(key), uid))
        if self.interrupt is not None:
            self.interrupt(info, uid)

    def chown(self, path, uid, gid):
        self.change(self.real_lstat(path), uid)

    def fchown(self, fd, uid, gid):
        self.change(self.real_fstat(fd), uid)

    def write_json(self, path, value):
        runtime.write_json(path, value)
        self.events.append(('journal', value['uid']))

    def assert_owners(self, uid):
        for path in self.files:
            self.assertEqual(path.lstat().st_uid, uid)

    def assert_preserved(self):
        self.assertEqual([path.read_bytes() for path in self.files], [b'image-one', b'partial-image'])
        self.assertEqual(self.unrelated.read_bytes(), b'preserve other AstrBot data')

    def interrupt_first_file(self, wanted_uid):
        def interrupt(info, uid):
            if stat.S_ISREG(info.st_mode) and uid == wanted_uid:
                raise Interrupted()
        self.interrupt = interrupt

    def test_prepare_normalizes_all_files_before_worker_launch(self):
        path, previous = export.prepare(self.directory, self.owner)
        self.assertEqual((path, previous), (self.directory, OLD))
        self.assertEqual(path.lstat().st_uid, CONTROLLER)
        self.assert_owners(CONTROLLER)
        self.assertEqual(runtime.read_json(self.record)['uid'], OLD)
        self.assert_preserved()

    def test_interruption_mid_normalization_can_retry(self):
        self.interrupt_first_file(CONTROLLER)
        with self.assertRaises(Interrupted):
            export.prepare(self.directory, self.owner)
        self.assertEqual({path.lstat().st_uid for path in self.files}, {OLD, CONTROLLER})
        self.assertEqual(runtime.read_json(self.record)['uid'], OLD)
        self.interrupt = None
        prepared = export.prepare(self.directory, self.owner)
        self.assert_owners(CONTROLLER)
        export.assign(*prepared, NEXT, self.owner)
        self.assert_owners(NEXT)
        self.assert_preserved()

    def test_interruption_mid_assignment_recovers_with_different_next_uid(self):
        prepared = export.prepare(self.directory, self.owner)
        self.interrupt_first_file(FIRST)
        with self.assertRaises(Interrupted):
            export.assign(*prepared, FIRST, self.owner)
        self.assertEqual(runtime.read_json(self.record)['uid'], FIRST)
        self.assertEqual({path.lstat().st_uid for path in self.files}, {FIRST, CONTROLLER})
        self.assertEqual(self.directory.lstat().st_uid, CONTROLLER)
        self.interrupt = None
        prepared = export.prepare(self.directory, self.owner)
        self.assertEqual(prepared[1], FIRST)
        self.assert_owners(CONTROLLER)
        export.assign(*prepared, NEXT, self.owner)
        self.assert_owners(NEXT)
        self.assertEqual(self.directory.lstat().st_uid, NEXT)
        self.assertEqual(runtime.read_json(self.record)['uid'], NEXT)
        self.assert_preserved()

    def test_new_journal_is_visible_before_first_file_transfer(self):
        prepared = export.prepare(self.directory, self.owner)
        self.events.clear()
        def inspect_journal(info, uid):
            if uid == FIRST:
                self.assertEqual(runtime.read_json(self.record)['uid'], FIRST)
        self.interrupt = inspect_journal
        export.assign(*prepared, FIRST, self.owner)
        self.assertEqual(self.events[0], ('journal', FIRST))
        self.assertEqual(self.events[-1], ('chown', self.directory.name, FIRST))
        self.assert_preserved()

    def test_failed_journal_write_never_transfers_files(self):
        prepared = export.prepare(self.directory, self.owner)
        self.owner.write_json = lambda *args: (_ for _ in ()).throw(Interrupted())
        with self.assertRaises(Interrupted):
            export.assign(*prepared, FIRST, self.owner)
        self.assert_owners(CONTROLLER)
        self.assertEqual(runtime.read_json(self.record)['uid'], OLD)

    def test_stale_pending_write_is_removed_only_after_normalization(self):
        self.pending.write_bytes(b'{"schemaVersion":1,"uid":')
        self.pending.chmod(0o600)
        self.remember(self.pending, 0)
        self.interrupt_first_file(CONTROLLER)
        with self.assertRaises(Interrupted):
            export.prepare(self.directory, self.owner)
        self.assertTrue(self.pending.exists())
        self.interrupt = None
        prepared = export.prepare(self.directory, self.owner)
        self.assertFalse(self.pending.exists())
        self.assert_owners(CONTROLLER)
        export.assign(*prepared, NEXT, self.owner)
        self.assert_preserved()

    def test_controller_owned_empty_pending_write_can_retry(self):
        self.pending.touch(mode=0o600)
        prepared = export.prepare(self.directory, self.owner)
        self.assertFalse(self.pending.exists())
        export.assign(*prepared, FIRST, self.owner)
        self.assert_owners(FIRST)

    def test_unsafe_managed_entries_are_refused_before_normalization(self):
        cases = ('unknown-name', 'symlink', 'hardlink', 'mode', 'owner', 'size', 'directory')
        for case in cases:
            with self.subTest(case=case):
                name = 'c' * 64 + '.jpg'
                path = self.directory / name
                if case == 'unknown-name':
                    path = self.create('unrelated.txt')
                elif case == 'symlink':
                    path.symlink_to(self.unrelated)
                elif case == 'hardlink':
                    os.link(self.unrelated, path)
                elif case == 'directory':
                    path.mkdir(mode=0o600)
                else:
                    path = self.create(name, uid=NEXT if case == 'owner' else OLD)
                    if case == 'mode':
                        path.chmod(0o644)
                    if case == 'size':
                        with path.open('r+b') as stream:
                            stream.truncate(25 * 1024 * 1024 + 1)
                try:
                    with self.assertRaises((ValueError, OSError)):
                        export.prepare(self.directory, self.owner)
                    self.assert_owners(OLD)
                    self.assert_preserved()
                finally:
                    path.rmdir() if case == 'directory' else path.unlink()

    def test_entry_count_is_bounded_before_normalization(self):
        for index in range(511):
            self.create(format(index, '064x') + '.webp')
        with self.assertRaises(ValueError):
            export.prepare(self.directory, self.owner)
        self.assert_owners(OLD)

    def test_unsafe_pending_write_is_refused_without_unlink(self):
        for case in ('symlink', 'hardlink', 'mode', 'owner', 'directory', 'size'):
            with self.subTest(case=case):
                if case == 'symlink':
                    self.pending.symlink_to(self.unrelated)
                elif case == 'hardlink':
                    os.link(self.unrelated, self.pending)
                elif case == 'directory':
                    self.pending.mkdir(mode=0o600)
                else:
                    self.pending.write_bytes(b'partial')
                    self.pending.chmod(0o644 if case == 'mode' else 0o600)
                    self.remember(self.pending, OLD if case == 'owner' else CONTROLLER)
                    if case == 'size':
                        with self.pending.open('r+b') as stream:
                            stream.truncate(65537)
                try:
                    with self.assertRaises((ValueError, OSError)):
                        export.prepare(self.directory, self.owner)
                    self.assertTrue(self.pending.exists() or self.pending.is_symlink())
                    self.assert_preserved()
                finally:
                    self.pending.rmdir() if case == 'directory' else self.pending.unlink()

    def test_assign_revalidates_entries_before_changing_journal(self):
        prepared = export.prepare(self.directory, self.owner)
        path = self.create('unrelated.txt', uid=CONTROLLER)
        with self.assertRaises(ValueError):
            export.assign(*prepared, FIRST, self.owner)
        self.assertEqual(runtime.read_json(self.record)['uid'], OLD)
        self.assert_owners(CONTROLLER)
        self.assertTrue(path.exists())

    def test_completed_handoff_can_be_prepared_again(self):
        prepared = export.prepare(self.directory, self.owner)
        export.assign(*prepared, FIRST, self.owner)
        prepared = export.prepare(self.directory, self.owner)
        self.assertEqual(prepared[1], FIRST)
        self.assert_owners(CONTROLLER)
        self.assert_preserved()


if __name__ == '__main__':
    unittest.main()
