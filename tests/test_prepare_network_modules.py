"""Purely offline tests: every subprocess and Linux state read is mocked."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "prepare-network-modules.py"
SPEC = importlib.util.spec_from_file_location("prepare_network_modules_under_test", SOURCE)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class PlanTests(unittest.TestCase):
    def plan(self, *extra: str) -> str:
        return "\n".join((*extra, "insmod " + module.MODULES[0].path.as_posix())) + "\n"

    def validate(self, text: str, *, verified=frozenset(), live=True, already_live=False):
        with patch.object(module, "capture", return_value=(0, text)), patch.object(module, "live", return_value=live):
            module.check_plan(module.MODULES[0], already_live, verified)

    def test_only_two_proved_builtin_names_and_existing_lib_are_accepted(self):
        text = self.plan("builtin crc32c_intel", "builtin crc32c_generic",
                         "insmod " + module.DEPENDENCY_FILES["libcrc32c"].as_posix())
        self.validate(text, verified=frozenset(module.BUILTINS))

    def test_known_builtin_without_evidence_is_rejected(self):
        with self.assertRaisesRegex(module.Failure, "builtin_not_verified"):
            self.validate(self.plan("builtin crc32c_intel"))

    def test_other_builtin_insmod_install_parameters_and_duplicates_are_rejected(self):
        forbidden = ("builtin crc32", "builtin crc32c-intel", "builtin crc32c_intel parameter=1",
                     "insmod /wrong/extra.ko.zst", "install /bin/false",
                     "insmod " + module.DEPENDENCY_FILES["libcrc32c"].as_posix() + " parameter=1",
                     "builtin crc32c_intel\nbuiltin crc32c_intel")
        for entry in forbidden:
            with self.subTest(entry=entry), self.assertRaises(module.Failure):
                self.validate(self.plan(entry), verified=frozenset(module.BUILTINS))

    def test_libcrc32c_must_already_be_live(self):
        with self.assertRaisesRegex(module.Failure, "dependency_not_live"):
            self.validate(self.plan("insmod " + module.DEPENDENCY_FILES["libcrc32c"].as_posix()), live=False)

    def test_blacklisted_unloaded_target_fails_and_live_target_is_skipped(self):
        with self.assertRaisesRegex(module.Failure, "target_missing"):
            self.validate("")
        self.validate("", already_live=True)


class EvidenceTests(unittest.TestCase):
    CONFIG = "CONFIG_CRYPTO_CRC32C=y\nCONFIG_CRYPTO_CRC32C_INTEL=y\n"
    FILE = b"kernel/arch/x86/crypto/crc32c-intel.ko\nkernel/crypto/crc32c_generic.ko\n"

    def builtin(self, config=CONFIG, data=FILE):
        with patch.object(module, "read_regular", return_value=data), \
             patch.object(module, "BUILTIN_SIZE", len(data)), \
             patch.object(module, "BUILTIN_SHA256", hashlib.sha256(data).hexdigest()):
            return module.check_builtins(config)

    def test_exact_config_and_modules_builtin_lines_prove_builtins_without_sys_module(self):
        with patch.object(module, "live", side_effect=AssertionError("builtin sysfs must not be queried")):
            self.assertEqual(self.builtin(), frozenset(module.BUILTINS))

    def test_builtin_file_hash_and_size_are_required(self):
        with patch.object(module, "read_regular", return_value=self.FILE):
            with self.assertRaisesRegex(module.Failure, "hash_size_mismatch"):
                module.check_builtins(self.CONFIG)

    def test_nonbuiltin_config_missing_or_duplicate_exact_line_fails(self):
        for config, data in ((self.CONFIG.replace("=y", "=m", 1), self.FILE),
                             (self.CONFIG, self.FILE.replace(b"kernel/crypto/crc32c_generic.ko\n", b"")),
                             (self.CONFIG, self.FILE + b"kernel/crypto/crc32c_generic.ko\n")):
            with self.subTest(config=config, data=data), self.assertRaisesRegex(module.Failure, "evidence_mismatch"):
                self.builtin(config, data)

    def test_libcrc32c_hash_and_metadata_are_fixed(self):
        data = b"mock compressed module"
        def metadata(field, path):
            return {"name": "libcrc32c", "depends": "", "vermagic": "fixed magic"}[field]
        with patch.object(module, "read_regular", return_value=data), \
             patch.object(module, "LIBCRC32C_SIZE", len(data)), \
             patch.object(module, "LIBCRC32C_SHA256", hashlib.sha256(data).hexdigest()), \
             patch.object(module, "modinfo", side_effect=metadata), patch.object(module, "live", return_value=True):
            module.check_libcrc32c("fixed magic")
            with self.assertRaisesRegex(module.Failure, "metadata_mismatch"):
                module.check_libcrc32c("wrong magic")
        with patch.object(module, "read_regular", return_value=data):
            with self.assertRaisesRegex(module.Failure, "hash_size_mismatch"):
                module.check_libcrc32c("fixed magic")


class FailureProgressTests(unittest.TestCase):
    def run_main(self, *, live_states, loader):
        output = io.StringIO()
        records = [{"name": item.name, "live": False} for item in module.MODULES]
        with patch.object(module, "check_all", return_value=records), \
             patch.object(module.os, "geteuid", return_value=0, create=True), \
             patch.object(module, "safe_live_snapshot", side_effect=live_states), \
             patch.object(module, "load_one", side_effect=loader), contextlib.redirect_stdout(output):
            code = module.main(["--load-if-approved"])
        return code, json.loads(output.getvalue().splitlines()[-1])

    def test_successful_insertion_is_recorded_before_failed_unit_cleanup(self):
        progress = module.LoadProgress()
        with patch.object(module.os, "geteuid", return_value=0, create=True), \
             patch.object(module, "capture", side_effect=[(0, ""), (0, ""), (0, "")]), \
             patch.object(module, "live", return_value=True), \
             patch.object(module, "stop_owned", side_effect=module.Failure("unit_cleanup_unconfirmed")):
            with self.assertRaisesRegex(module.Failure, "cleanup_unconfirmed"):
                module.load_one(module.MODULES[0], progress)
        self.assertEqual(progress.attempted, ["nft_ct"])
        self.assertEqual(progress.loaded, ["nft_ct"])
        self.assertTrue(progress.cleanup_unconfirmed)

    def test_failed_output_retains_initial_current_and_partial_progress(self):
        initial = {item.name: "not_live" for item in module.MODULES}
        current = dict(initial, nft_ct="live")
        def failed(item, progress):
            progress.attempted.append(item.name)
            progress.loaded.append(item.name)
            progress.cleanup_unconfirmed = True
            raise module.Failure("unit_cleanup_unconfirmed")
        code, record = self.run_main(live_states=[initial, current], loader=failed)
        self.assertEqual(code, 1)
        self.assertEqual(record["initial_live"], initial)
        self.assertEqual(record["current_live"], current)
        self.assertEqual(record["loaded_by_this_run"], ["nft_ct"])
        self.assertTrue(record["possible_global_kernel_state_change"])
        self.assertTrue(record["cleanup_unconfirmed"])

    def test_uncertain_attempt_and_failed_state_read_do_not_erase_primary_error(self):
        initial = {item.name: "not_live" for item in module.MODULES}
        unknown = {item.name: "unavailable" for item in module.MODULES}
        def failed(item, progress):
            progress.attempted.append(item.name)
            raise module.Failure("module_load_unconfirmed")
        code, record = self.run_main(live_states=[initial, unknown], loader=failed)
        self.assertEqual(code, 1)
        self.assertEqual(record["reason"], "module_load_unconfirmed")
        self.assertEqual(record["current_live"], unknown)
        self.assertTrue(record["possible_global_kernel_state_change"])

    def test_safe_snapshot_handles_oserror(self):
        with patch.object(module, "live", side_effect=OSError("private diagnostic")):
            self.assertEqual(set(module.safe_live_snapshot().values()), {"unavailable"})

    def test_primary_load_error_survives_additional_cleanup_error(self):
        progress = module.LoadProgress()
        with patch.object(module.os, "geteuid", return_value=0, create=True), \
             patch.object(module, "capture", side_effect=[(0, ""), (0, ""), (1, "")]), \
             patch.object(module, "stop_owned", side_effect=module.Failure("cleanup_failed")):
            with self.assertRaisesRegex(module.Failure, "module_load_unconfirmed"):
                module.load_one(module.MODULES[0], progress)
        self.assertTrue(progress.cleanup_unconfirmed)
        self.assertEqual(progress.loaded, [])

    def test_default_check_never_loads_and_all_live_load_mode_reports_no_change(self):
        records = [{"name": item.name, "live": True} for item in module.MODULES]
        for args in ([], ["--check"], ["--load-if-approved"]):
            output = io.StringIO()
            with self.subTest(args=args), patch.object(module, "check_all", return_value=records), \
                 patch.object(module.os, "geteuid", return_value=0, create=True), \
                 patch.object(module, "live", return_value=True), patch.object(module, "load_one") as loader, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(module.main(args), 0)
                loader.assert_not_called()
            record = json.loads(output.getvalue().splitlines()[-1])
            if args == ["--load-if-approved"]:
                self.assertEqual(record["loaded_by_this_run"], [])
                self.assertFalse(record["global_kernel_state_changed"])


if __name__ == "__main__":
    unittest.main()
