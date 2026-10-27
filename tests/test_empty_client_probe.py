"""Offline controller tests: every process and HTTP operation is mocked."""
import contextlib
import errno
import importlib.util
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location(
    "empty_client_probe", Path(__file__).resolve().parents[1] / "scripts" / "empty_client_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        # A forgotten mock fails locally instead of executing or contacting anything.
        self.stack.enter_context(patch.object(probe.subprocess, "Popen", side_effect=AssertionError("real process forbidden")))
        self.stack.enter_context(patch.object(probe.http.client, "HTTPConnection", side_effect=AssertionError("real HTTP forbidden")))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))

    def run_main(self, responses=None, prepare_error=None, spawn_error=None):
        child = MagicMock()
        child.poll.return_value = None
        child.wait.return_value = 0
        defaults = [(401, {}), (200, {"items": []}),
                    (200, {"state": "ready", "version": {"backend": "native", "version": probe.EXPECTED_VERSION}})]
        with patch.object(probe, "validate_isolation", return_value=[("sandbox", True)]), \
             patch.object(probe, "prepare_directories", return_value={"TOKIO_WORKER_THREADS": "1"}, side_effect=prepare_error), \
             patch.object(probe, "wait_for_health"), \
             patch.object(probe, "read_token", return_value="not-printed-secret"), \
             patch.object(probe, "get_json", side_effect=responses or defaults), \
             patch.object(probe.subprocess, "Popen", return_value=child, side_effect=spawn_error) as spawn:
            code = probe.main([])
        return code, child, spawn

    def test_private_directory_error_reports_only_stage_and_numeric_errno(self):
        code, child, spawn = self.run_main(prepare_error=PermissionError(errno.EACCES, "secret-path-and-content"))
        self.assertEqual(code, 1)
        spawn.assert_not_called()
        child.terminate.assert_not_called()
        output = self.output.getvalue()
        self.assertIn('"check": "private_directories"', output)
        self.assertIn('"errno": 13', output)
        self.assertNotIn("secret-path-and-content", output)
        self.assertNotIn("private_directories_ready", output)
        self.assertNotIn("EMPTY_CLIENT_STARTUP_VERIFIED", output)

    def test_spawn_error_reports_only_stage_and_numeric_errno(self):
        code, child, spawn = self.run_main(spawn_error=PermissionError(errno.EACCES, "secret-executable-path"))
        self.assertEqual(code, 1)
        spawn.assert_called_once()
        child.terminate.assert_not_called()
        output = self.output.getvalue()
        self.assertIn('"check": "private_directories_ready"', output)
        self.assertIn('"check": "native_spawn"', output)
        self.assertIn('"errno": 13', output)
        self.assertNotIn("secret-executable-path", output)
        self.assertNotIn("child_stopped", output)
        self.assertNotIn("EMPTY_CLIENT_STARTUP_VERIFIED", output)

    def test_binary_execution_requires_permission_and_exec_mount(self):
        for permitted, flags, expected in ((True, 0, True), (True, 8, False), (False, 0, False)):
            with self.subTest(permitted=permitted, flags=flags), \
                 patch.object(probe.os, "ST_NOEXEC", 8, create=True), \
                 patch.object(probe.os, "access", return_value=permitted), \
                 patch.object(probe.os, "statvfs", return_value=SimpleNamespace(f_flag=flags), create=True):
                self.assertEqual(probe._binary_executable_mount(), expected)

    def test_isolation_diagnostic_includes_opt_mount_flags(self):
        with patch.object(probe.Path, "read_text", return_value="1 0 0:1 / / ro - ext4 root rw\n"), \
             patch.object(probe.os, "statvfs", return_value=SimpleNamespace(f_blocks=1, f_frsize=4096, f_files=32), create=True), \
             patch.object(probe.os, "stat", return_value=SimpleNamespace(st_mode=0o40555)):
            self.assertTrue(probe.emit_temp_mount_diagnostics())
        self.assertIn('"path": "/opt"', self.output.getvalue())
        self.assertIn('"options": ["ro"]', self.output.getvalue())

    def test_failed_isolation_does_not_start_binary(self):
        with patch.object(probe, "validate_isolation", return_value=[("capabilities_zero", False)]), \
             patch.object(probe.subprocess, "Popen") as spawn:
            self.assertEqual(probe.main([]), 1)
            spawn.assert_not_called()
        self.assertNotIn("EMPTY_CLIENT_STARTUP_VERIFIED", self.output.getvalue())

    def test_isolation_only_never_starts_binary_even_when_every_check_passes(self):
        with patch.object(probe, "validate_isolation", return_value=[("sandbox", True)]), \
             patch.object(probe, "emit_temp_mount_diagnostics", return_value=True) as diagnostic, \
             patch.object(probe, "prepare_directories") as prepare, \
             patch.object(probe.subprocess, "Popen") as spawn, \
             patch.object(probe, "get_json") as http:
            self.assertEqual(probe.main(["--isolation-only"]), 0)
            diagnostic.assert_called_once_with()
            prepare.assert_not_called()
            spawn.assert_not_called()
            http.assert_not_called()
        self.assertNotIn("EMPTY_CLIENT_STARTUP_VERIFIED", self.output.getvalue())

    def test_isolation_only_reports_mounts_even_after_failed_protection(self):
        with patch.object(probe, "validate_isolation", return_value=[("sandbox", False)]), \
             patch.object(probe, "emit_temp_mount_diagnostics", return_value=True) as diagnostic, \
             patch.object(probe.subprocess, "Popen") as spawn:
            self.assertEqual(probe.main(["--isolation-only"]), 1)
            diagnostic.assert_called_once_with()
            spawn.assert_not_called()

    def test_unknown_arguments_are_rejected_before_any_probe(self):
        for arguments in (["--unknown"], ["--isolation-only", "--unknown"],
                          ["--isolation-only", "--isolation-only"]):
            with self.subTest(arguments=arguments), \
                 patch.object(probe, "validate_isolation") as validate, \
                 patch.object(probe.subprocess, "Popen") as spawn:
                self.assertEqual(probe.main(arguments), 2)
                validate.assert_not_called()
                spawn.assert_not_called()

    def test_empty_client_success_and_owned_child_cleanup(self):
        code, child, spawn = self.run_main()
        self.assertEqual(code, 0)
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=3)
        child.kill.assert_not_called()
        self.assertEqual(spawn.call_args.args[0][0], str(probe.BINARY))
        self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(spawn.call_args.kwargs["close_fds"])
        self.assertNotIn("not-printed-secret", self.output.getvalue())
        self.assertIn("EMPTY_CLIENT_STARTUP_VERIFIED", self.output.getvalue())

    def test_nonempty_accounts_fail_and_still_stop_child(self):
        code, child, _ = self.run_main([(401, {}), (200, {"items": [{"id": "sensitive-account"}]})])
        self.assertEqual(code, 1)
        child.terminate.assert_called_once()
        self.assertNotIn("sensitive-account", self.output.getvalue())
        self.assertNotIn("EMPTY_CLIENT_STARTUP_VERIFIED", self.output.getvalue())

    def test_wrong_version_or_not_ready_cannot_succeed(self):
        for state, version, backend in (("starting", probe.EXPECTED_VERSION, "native"),
                                        ("ready", "0.16.0", "native"),
                                        ("ready", probe.EXPECTED_VERSION, "legacy")):
            with self.subTest(state=state, version=version, backend=backend):
                code, child, _ = self.run_main([(401, {}), (200, {"items": []}),
                    (200, {"state": state, "version": {"version": version, "backend": backend}})])
                self.assertEqual(code, 1)
                child.terminate.assert_called_once()

    def test_unauthenticated_accounts_success_is_rejected(self):
        code, _, _ = self.run_main([(200, {"items": []})])
        self.assertEqual(code, 1)

    def test_oversized_body_is_rejected_and_connection_closed(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.getheader.return_value = None
        response.read.return_value = b"x" * (probe.MAX_BODY + 1)
        with patch.object(probe.http.client, "HTTPConnection", return_value=connection) as constructor:
            with self.assertRaises(probe.ProbeFailure) as failure:
                probe.get_json("/health")
        self.assertEqual(failure.exception.check, "response_body_bound")
        response.read.assert_called_once_with(probe.MAX_BODY + 1)
        connection.close.assert_called_once()
        constructor.assert_called_once_with("127.0.0.1", probe.PORT, timeout=0.8)

    def test_oversized_content_length_is_rejected_before_read(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.getheader.return_value = str(probe.MAX_BODY + 1)
        with patch.object(probe.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(probe.ProbeFailure):
                probe.get_json("/health")
        response.read.assert_not_called()
        connection.close.assert_called_once()

    def test_cleanup_kills_only_its_child_after_term_timeout(self):
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("owned-child", 3), 0]
        self.assertTrue(probe.stop_child(child))
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list[0].kwargs, {"timeout": 3})
        self.assertEqual(child.wait.call_args_list[1].kwargs, {"timeout": 1})

    def test_cleanup_failure_prevents_success(self):
        with patch.object(probe, "stop_child", return_value=False):
            code, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertNotIn("EMPTY_CLIENT_STARTUP_VERIFIED", self.output.getvalue())

    def test_request_paths_are_fixed(self):
        with self.assertRaises(probe.ProbeFailure):
            probe.get_json("/v1/accounts/create")

    def test_mountinfo_escape_and_most_specific_mount(self):
        mounts = probe.parse_mounts(
            "1 0 0:1 / / ro - ext4 root rw\n"
            "2 1 0:2 / /tmp rw,nosuid,nodev,noexec - tmpfs tmpfs rw\n"
            "3 1 0:2 /systemd/inaccessible/sock /run/docker.sock ro - tmpfs tmpfs rw\n")
        self.assertEqual(probe.containing_mount(mounts, "/tmp/example")["fstype"], "tmpfs")
        self.assertIn("inaccessible", probe.containing_mount(mounts, "/run/docker.sock")["root"])

    def test_all_private_directories_use_bounded_var_lib(self):
        self.assertEqual(probe.WORK_MOUNT, "/var/lib")
        self.assertEqual(probe.BASE, Path("/var/lib/astrbot-wechat-poc"))
        with patch.object(probe.Path, "mkdir") as mkdir:
            environment = probe.prepare_directories()
        self.assertEqual(probe.DATA, probe.BASE / "data")
        self.assertEqual(mkdir.call_count, 6)
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
            self.assertTrue(Path(environment[key]).is_relative_to(probe.BASE))
        self.assertNotIn("/tmp/", environment["TMPDIR"].replace("\\", "/"))
        self.assertEqual(environment["TOKIO_WORKER_THREADS"], "1")

    def test_masked_temporary_path_requires_both_access_denials(self):
        with patch.object(probe, "_masked", return_value=True), \
             patch.object(probe.os, "scandir", side_effect=PermissionError(errno.EACCES, "denied")), \
             patch.object(probe.tempfile, "mkstemp", side_effect=PermissionError(errno.EACCES, "denied")):
            self.assertTrue(probe._blocked_temporary_path([], "/tmp"))

    def test_unexpected_temp_creation_cleans_only_new_probe_and_fails(self):
        with patch.object(probe, "_masked", return_value=True), \
             patch.object(probe.os, "scandir", side_effect=PermissionError(errno.EACCES, "denied")), \
             patch.object(probe.tempfile, "mkstemp", return_value=(73, "/tmp/empty-probe-denied-owned")), \
             patch.object(probe.os, "close") as close, \
             patch.object(probe.os, "unlink") as unlink:
            self.assertFalse(probe._blocked_temporary_path([], "/tmp"))
        close.assert_called_once_with(73)
        unlink.assert_called_once_with("/tmp/empty-probe-denied-owned")

    def test_visible_temp_directory_or_missing_mask_cannot_pass(self):
        with patch.object(probe, "_masked", return_value=True), \
             patch.object(probe.os, "scandir", return_value=MagicMock()), \
             patch.object(probe.tempfile, "mkstemp", side_effect=PermissionError(errno.EACCES, "denied")):
            self.assertFalse(probe._blocked_temporary_path([], "/var/tmp"))
        with patch.object(probe, "_masked", return_value=False), \
             patch.object(probe.tempfile, "mkstemp") as create:
            self.assertFalse(probe._blocked_temporary_path([], "/tmp"))
            create.assert_not_called()

    def test_disk_full_is_not_evidence_of_a_mask(self):
        with patch.object(probe, "_masked", return_value=True), \
             patch.object(probe.os, "scandir", side_effect=PermissionError(errno.EACCES, "denied")), \
             patch.object(probe.tempfile, "mkstemp", side_effect=OSError(errno.ENOSPC, "full")):
            self.assertFalse(probe._blocked_temporary_path([], "/tmp"))


if __name__ == "__main__":
    unittest.main()
