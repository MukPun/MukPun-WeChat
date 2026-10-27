"""Synthetic offline snapshots only; never read host SDK state or credentials."""
import importlib.util
import io
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile


SOURCE = Path(__file__).resolve().parents[1] / "scripts/session_state_store.py"
SPEC = importlib.util.spec_from_file_location("session_state_store_tested", SOURCE)
state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state)


class ZipRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "synthetic.zip"
        self.destination = self.root / "restored"

    def write_zip(self, entries, compression=zipfile.ZIP_STORED):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.archive, "w", compression=compression) as archive:
                for name, data in entries:
                    archive.writestr(name, data)
        self.archive.chmod(0o600)

    def rejected(self, code=None):
        with self.assertRaises(state.StateError) as caught:
            state.restore_archive(self.archive, self.destination)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), caught.exception.code)
        self.assertFalse(self.destination.exists(), "must validate before destination creation")

    def test_full_synthetic_sdk_state_and_empty_directories_restored(self):
        entries = [("registry", b"synthetic-registry"), ("storage-key", b"synthetic-key"),
                   ("api-token", b"synthetic-token"), ("control/", b""),
                   ("control/state", b"synthetic-control"), ("profile/db", b"db"),
                   ("profile/db-wal", b"wal"), ("profile/db-shm", b"shm"),
                   ("empty/", b""), ("extra-future-sdk-file", b"preserved")]
        self.write_zip(entries)
        result = state.restore_archive(self.archive, self.destination)
        self.assertEqual(result.files, 8)
        self.assertEqual(result.bytes, sum(len(data) for _, data in entries))
        self.assertEqual(result.archive_bytes, self.archive.stat().st_size)
        for name, data in entries:
            restored = self.destination / name
            if name.endswith("/"):
                self.assertTrue(restored.is_dir())
            else:
                self.assertEqual(restored.read_bytes(), data)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(restored.stat().st_mode),
                                 0o700 if name.endswith("/") else 0o600)

    def test_accepts_existing_empty_private_target(self):
        self.destination.mkdir(mode=0o700)
        self.write_zip([("state", b"synthetic")])
        state.restore_archive(self.archive, self.destination)
        self.assertEqual((self.destination / "state").read_bytes(), b"synthetic")

    def test_nonempty_target_is_untouched(self):
        self.destination.mkdir(mode=0o700)
        marker = self.destination / "marker"
        marker.write_bytes(b"existing")
        self.write_zip([("state", b"synthetic")])
        with self.assertRaises(state.StateError) as caught:
            state.restore_archive(self.archive, self.destination)
        self.assertEqual(caught.exception.code, "state_destination")
        self.assertEqual(list(self.destination.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"existing")

    def test_rejects_traversal_absolute_backslash_controls_and_drive_paths(self):
        for name in ("../escaped", "/absolute", "a/../escaped", "a//b", "a/./b",
                     "a\\b", "C:/escaped", "C:escaped", "a\nb", "a\x7fb", "a\x00b"):
            with self.subTest(name=repr(name)):
                self.write_zip([(name, b"synthetic")])
                # ZipInfo truncates NUL and normalizes backslashes on Windows.
                # Patch both local and central names without changing lengths.
                if "\x00" in name or "\\" in name:
                    placeholder = "x" * len(name)
                    self.write_zip([(placeholder, b"synthetic")])
                    self.archive.write_bytes(self.archive.read_bytes().replace(
                        placeholder.encode(), name.encode()))
                self.rejected()
        self.assertFalse((self.root / "escaped").exists())

    def test_rejects_duplicate_and_file_parent_conflicts(self):
        for entries in ([('same', b'a'), ('same', b'b')],
                        [('parent', b'a'), ('parent/child', b'b')],
                        [('parent/', b''), ('parent', b'b')]):
            with self.subTest(entries=entries):
                self.write_zip(entries)
                self.rejected("state_archive")

    def test_rejects_symlink_fifo_and_mismatched_directory_types(self):
        for mode, name in ((stat.S_IFLNK | 0o777, "link"),
                           (stat.S_IFIFO | 0o600, "fifo"),
                           (stat.S_IFREG | 0o600, "directory/")):
            with self.subTest(mode=mode):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                self.write_zip([(info, b"")])
                self.rejected("state_archive")

    def test_rejects_compressed_zipbomb_without_decompression(self):
        self.write_zip([("bomb", b"a" * 100_000)], compression=zipfile.ZIP_DEFLATED)
        with mock.patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("decompressed")):
            self.rejected("state_archive")

    def test_declared_oversized_member_rejected_before_read(self):
        self.write_zip([("bomb", b"x")])
        raw = bytearray(self.archive.read_bytes())
        central = raw.index(b"PK\x01\x02")
        for offset in (18, 22):
            struct.pack_into("<L", raw, offset, state.MAX_SOURCE_BYTES + 1)
        for offset in (20, 24):
            struct.pack_into("<L", raw, central + offset, state.MAX_SOURCE_BYTES + 1)
        self.archive.write_bytes(raw)
        with mock.patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("read oversized")):
            self.rejected("state_limits")

    def test_total_bytes_count_path_depth_and_archive_caps(self):
        self.write_zip([("one", b"123"), ("two", b"456")])
        with mock.patch.object(state, "MAX_SOURCE_BYTES", 5):
            self.rejected("state_limits")
        self.write_zip([("a", b""), ("b", b"")])
        with mock.patch.object(state, "MAX_ENTRIES", 1):
            self.rejected("state_limits")
        self.write_zip([("a/b/c", b"")])
        with mock.patch.object(state, "MAX_ENTRIES", 2):
            self.rejected("state_limits")
        self.write_zip([("a/" * 24 + "x", b"")])
        self.rejected("state_limits")
        self.write_zip([("x" * 2049, b"")])
        self.rejected("state_limits")
        self.write_zip([("x", b"")])
        with mock.patch.object(state, "MAX_ARCHIVE_BYTES", self.archive.stat().st_size - 1):
            self.rejected("state_limits")

    def test_encrypted_member_rejected(self):
        self.write_zip([("state", b"synthetic")])
        raw = bytearray(self.archive.read_bytes())
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<H", raw, 6, 1)
        struct.pack_into("<H", raw, central + 8, 1)
        self.archive.write_bytes(raw)
        self.rejected("state_archive")

    def test_crc_checked_for_all_members_before_any_write(self):
        self.write_zip([("good", b"good"), ("late-corrupt", b"bad")])
        raw = bytearray(self.archive.read_bytes())
        second = raw.index(b"PK\x03\x04", 4)
        name_length, extra_length = struct.unpack_from("<2H", raw, second + 26)
        raw[second + 30 + name_length + extra_length] ^= 1
        self.archive.write_bytes(raw)
        self.rejected("state_archive")

    def test_central_directory_lie_cannot_allocate_unbounded_entries(self):
        self.write_zip([("a", b""), ("b", b"")])
        raw = bytearray(self.archive.read_bytes())
        end = raw.rindex(b"PK\x05\x06")
        struct.pack_into("<2H", raw, end + 8, 1, 1)
        self.archive.write_bytes(raw)
        with mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError("parsed unbounded CD")):
            self.rejected("state_archive")

    def test_capped_writer_counts_header_rewrites(self):
        raw = io.BytesIO()
        writer = state._CappedWriter(raw)
        with mock.patch.object(state, "MAX_ARCHIVE_BYTES", 8):
            writer.write(b"123456")
            writer.seek(0)
            with self.assertRaises(state.StateError) as caught:
                writer.write(b"abc")
        self.assertEqual(caught.exception.code, "state_limits")
        self.assertEqual(raw.getvalue(), b"123456")

    def test_copy_handles_short_writes_without_truncating_state(self):
        self.write_zip([("state", b"synthetic-full-content")])

        class ShortWriter(io.BytesIO):
            def write(self, data):
                return super().write(data[:2])

        output = ShortWriter()
        with zipfile.ZipFile(self.archive) as archive:
            state._copy_member(archive, archive.infolist()[0], output)
        self.assertEqual(output.getvalue(), b"synthetic-full-content")

    def test_directory_enumeration_stops_at_object_cap(self):
        consumed = []

        def hostile_directory():
            for index in range(100_000):
                consumed.append(index)
                entry = mock.Mock()
                entry.name = str(index)
                yield entry

        with mock.patch.object(state.os, "scandir") as scandir:
            scandir.return_value.__enter__.return_value = hostile_directory()
            with self.assertRaises(state.StateError) as caught:
                state._bounded_names(123, 2)
        self.assertEqual(caught.exception.code, "state_limits")
        self.assertEqual(consumed, [0, 1, 2])
        scandir.return_value.__exit__.assert_called_once()

    def test_error_message_never_echoes_sensitive_input(self):
        self.write_zip([("../SYNTHETIC_PRIVATE_NAME", b"SYNTHETIC_PRIVATE_CONTENT")])
        with self.assertRaises(state.StateError) as caught:
            state.restore_archive(self.archive, self.destination)
        self.assertEqual(str(caught.exception), "state_archive")
        self.assertEqual(str(state.StateError("SYNTHETIC_PRIVATE_NAME")), "state_io")


@unittest.skipUnless(state._FD_API, "requires Linux/POSIX nofollow and dir_fd")
class PosixSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir(mode=0o700)
        self.source_fd = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.source_fd)
        self.store = state.SnapshotStore(self.root / "snapshots").prepare()

    def commit(self):
        return self.store.commit(self.source_fd, expected_uid=os.geteuid(),
                                 expected_dev=os.fstat(self.source_fd).st_dev)

    def test_complete_tree_round_trip_and_two_generations(self):
        contents = {"registry": b"synthetic-registry", "storage-key": b"synthetic-key",
                    "api-token": b"synthetic-token", "control/state": b"control",
                    "profile/db": b"db", "profile/db-wal": b"wal",
                    "profile/db-shm": b"shm", "future-sdk-state/unknown": b"future"}
        for name, content in contents.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.source / "empty").mkdir()
        first = self.commit()
        self.assertEqual(first.files, len(contents))
        self.assertEqual(first.bytes, sum(map(len, contents.values())))
        old = self.store.latest().read_bytes()
        restored = self.root / "restored"
        state.restore_archive(self.store.latest(), restored)
        self.assertTrue((restored / "empty").is_dir())
        self.assertEqual({name: (restored / name).read_bytes() for name in contents}, contents)
        (self.source / "registry").write_bytes(b"synthetic-second")
        self.commit()
        self.assertEqual((self.store.path / "private/previous.zip").read_bytes(), old)
        self.assertEqual(set(path.name for path in (self.store.path / "private").iterdir()),
                         {"current.zip", "previous.zip"})
        for path in (self.store.path, self.store.path / "private"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.store.path / "private").iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_latest_prefers_current_then_previous(self):
        self.assertIsNone(self.store.latest())
        (self.source / "file").write_bytes(b"synthetic")
        self.commit()
        self.commit()
        current = self.store.latest()
        self.assertEqual(current.name, "current.zip")
        current.unlink()
        self.assertEqual(self.store.latest().name, "previous.zip")

    def test_rejects_source_symlink_hardlink_and_special_file(self):
        outside = self.root / "outside"
        outside.write_bytes(b"synthetic")
        entry = self.source / "entry"
        for make in (lambda: entry.symlink_to(outside), lambda: os.link(outside, entry),
                     lambda: os.mkfifo(entry)):
            with self.subTest(make=make):
                make()
                try:
                    with self.assertRaises(state.StateError) as caught:
                        self.commit()
                    self.assertEqual(caught.exception.code, "state_unsafe")
                    self.assertIsNone(self.store.latest())
                finally:
                    entry.unlink()

    def test_rejects_source_owner_device_and_entry_limits(self):
        for uid, dev in ((os.geteuid() + 1, os.fstat(self.source_fd).st_dev),
                         (os.geteuid(), os.fstat(self.source_fd).st_dev + 1)):
            with self.assertRaises(state.StateError) as caught:
                self.store.commit(self.source_fd, expected_uid=uid, expected_dev=dev)
            self.assertEqual(caught.exception.code, "state_unsafe")
        (self.source / "one").write_bytes(b"123")
        with mock.patch.object(state, "MAX_SOURCE_BYTES", 2):
            with self.assertRaises(state.StateError) as caught:
                self.commit()
        self.assertEqual(caught.exception.code, "state_limits")
        (self.source / "two").mkdir()
        with mock.patch.object(state, "MAX_ENTRIES", 1):
            with self.assertRaises(state.StateError) as caught:
                self.commit()
        self.assertEqual(caught.exception.code, "state_limits")

    def test_rotation_failure_preserves_prior_snapshot_and_removes_staging(self):
        (self.source / "state").write_bytes(b"old-synthetic")
        self.commit()
        old = self.store.latest().read_bytes()
        (self.source / "state").write_bytes(b"new-synthetic")
        replace = os.replace

        def failing_replace(source, destination, **kwargs):
            if source == ".next.zip":
                raise OSError("SYNTHETIC_PRIVATE_ERROR")
            return replace(source, destination, **kwargs)

        with mock.patch.object(state.os, "replace", side_effect=failing_replace):
            with self.assertRaises(state.StateError) as caught:
                self.commit()
        self.assertEqual(str(caught.exception), "state_rotation")
        self.assertEqual(self.store.latest().read_bytes(), old)
        self.assertFalse((self.store.path / "private/.next.zip").exists())

    def test_failed_write_and_hard_archive_cap_preserve_prior_snapshot(self):
        (self.source / "state").write_bytes(b"old-synthetic")
        self.commit()
        old = self.store.latest().read_bytes()
        (self.source / "state").write_bytes(b"new-synthetic" * 50)
        with mock.patch.object(state, "MAX_ARCHIVE_BYTES", len(old) + 1):
            with self.assertRaises(state.StateError) as caught:
                self.commit()
        self.assertEqual(str(caught.exception), "state_limits")
        self.assertEqual(self.store.latest().read_bytes(), old)
        self.assertFalse((self.store.path / "private/.next.zip").exists())

    def test_slot_symlink_hardlink_and_permissions_rejected(self):
        target = self.root / "outside.zip"
        target.write_bytes(b"synthetic")
        target.chmod(0o600)
        slot = self.store.path / "private/current.zip"
        for make in (lambda: slot.symlink_to(target), lambda: os.link(target, slot)):
            make()
            try:
                with self.assertRaises(state.StateError):
                    self.store.latest()
            finally:
                slot.unlink()
        slot.write_bytes(b"synthetic")
        slot.chmod(0o644)
        with self.assertRaises(state.StateError):
            self.store.prepare()

    def test_root_and_private_symlink_or_wrong_permissions_rejected(self):
        self.store.path.chmod(0o755)
        with self.assertRaises(state.StateError):
            self.store.prepare()
        self.store.path.chmod(0o700)
        private = self.store.path / "private"
        private.rmdir()
        private.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(state.StateError):
            self.store.prepare()

    def test_restore_rejects_symlink_archive_and_target(self):
        (self.source / "file").write_bytes(b"synthetic")
        self.commit()
        linked_archive = self.root / "link.zip"
        linked_archive.symlink_to(self.store.latest())
        with self.assertRaises(state.StateError):
            state.restore_archive(linked_archive, self.root / "restored")
        destination = self.root / "linked-target"
        destination.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(state.StateError):
            state.restore_archive(self.store.latest(), destination)
        self.assertEqual(list(self.source.iterdir()), [self.source / "file"])

    def test_explicit_guest_readonly_mode_and_owner(self):
        (self.source / "file").write_bytes(b"synthetic")
        self.commit()
        archive = self.root / "guest.zip"
        archive.write_bytes(self.store.latest().read_bytes())
        archive.chmod(0o400)
        result = state.restore_archive(archive, self.root / "restored",
                                       archive_owner_uid=os.geteuid(), archive_mode=0o400)
        self.assertEqual(result.files, 1)
        self.assertEqual((self.root / "restored/file").read_bytes(), b"synthetic")
        with self.assertRaises(state.StateError):
            state.restore_archive(archive, self.root / "bad-mode", archive_mode=0o444)


if __name__ == "__main__":
    unittest.main()
