"""Offline parser/policy tests only. Never run the root/TAP orchestrator."""
import ast
import copy
import importlib.util
from pathlib import Path
import stat
import re
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "probe-slirp-tap.py"
SPEC = importlib.util.spec_from_file_location("slirp_tap_probe", SOURCE)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def network(tap=True):
    names = ["lo", "tap0"] if tap else ["lo"]
    return {"links": [{"ifname": name, "flags": ["BROADCAST"]} for name in names],
            "addresses": [{"ifname": name, "addr_info": []} for name in names],
            "routes4": [], "routes6": []}


class SlirpTapProbeTests(unittest.TestCase):
    def test_script_and_embedded_launcher_parse_without_execution(self):
        ast.parse(SOURCE.read_text(encoding="utf-8"))
        ast.parse(probe.LAUNCHER)

    def test_launcher_only_runs_fixed_slirp_with_separate_ready_fd(self):
        self.assertIn("fcntl.F_DUPFD_CLOEXEC, 10", probe.LAUNCHER)
        self.assertIn("os.set_inheritable(descriptor, True)", probe.LAUNCHER)
        self.assertIn("3>&1 1>/dev/null", probe.LAUNCHER)
        self.assertIn("--ready-fd=3", probe.LAUNCHER)
        self.assertIn("--disable-host-loopback", probe.LAUNCHER)
        self.assertIn("--disable-dns", probe.LAUNCHER)
        self.assertIn("--enable-seccomp", probe.LAUNCHER)
        self.assertNotIn("--configure", probe.LAUNCHER)
        self.assertNotIn("--enable-sandbox", probe.LAUNCHER)
        self.assertNotIn("--enable-ipv6", probe.LAUNCHER)

    def test_owned_keeper_chain_is_imported_unchanged(self):
        props = probe.owned.properties("/var/tmp/bundle", "awpt-test")
        self.assertIn("--property=TasksMax=16", props)
        self.assertIn("--property=RuntimeMaxSec=20", props)

    def test_helper_has_no_host_capabilities_or_network_exceptions(self):
        props = probe.helper_properties("/var/tmp/bundle", "awpt-test", "astrbotwechatprobetest.slice")
        self.assertIn("--property=CapabilityBoundingSet=", props)
        self.assertIn("--property=AmbientCapabilities=", props)
        self.assertNotIn("--property=CapabilityBoundingSet=CAP_SYS_ADMIN", props)
        self.assertNotIn("--property=AmbientCapabilities=CAP_SYS_ADMIN", props)
        self.assertIn("--property=IPAddressDeny=any", props)
        self.assertIn("--property=IPAccounting=yes", props)
        self.assertFalse(any("IPAddressAllow=" in item for item in props))
        self.assertIn("--property=PrivateDevices=yes", props)
        self.assertIn("--property=BindPaths=/dev/net/tun", props)
        self.assertIn("--property=DeviceAllow=/dev/net/tun rw", props)
        self.assertIn("--property=DevicePolicy=closed", props)
        masked = next(item.split("=", 2)[2].split() for item in props
                      if item.startswith("--property=InaccessiblePaths="))
        self.assertEqual(set(masked), {"-/run/docker.sock", "-/run/docker", "-/run/containerd", "-/run/user",
                                      "-/run/dbus", "-/run/systemd/private", "-/run/frp", "-/run/frpc",
                                      "-/run/frps", "-/tmp", "-/var/tmp"})
        self.assertNotIn("-/run", masked)
        self.assertNotIn("-/run/systemd", masked)
        self.assertIn("--property=RuntimeMaxSec=8", props)
        self.assertIn("--property=TasksMax=16", props)

    def test_slice_uses_correct_typed_values_and_no_set_property(self):
        with mock.patch.object(probe, "command", return_value="") as command:
            probe.start_slice("astrbotwechatprobe123.slice")
        args = command.call_args.args[0]
        self.assertEqual(args[7:11], ["ssa(sv)a(sa(sv))", "astrbotwechatprobe123.slice", "fail", "6"])
        self.assertEqual(args[11:], ["CPUQuotaPerSecUSec", "t", "200000", "CPUQuotaPeriodUSec", "t", "100000",
                                   "MemoryHigh", "t", "100663296", "MemoryMax", "t", "134217728",
                                   "MemorySwapMax", "t", "0", "TasksMax", "t", "32", "0"])

    @staticmethod
    def launcher_filesystem_check():
        # Compile only the pure-check definitions, not the launcher body.
        definitions = [node for node in ast.parse(probe.LAUNCHER).body
                       if isinstance(node, ast.FunctionDef) and node.name in {
                           "stage", "check", "inaccessible", "read_mountinfo", "masked_mount", "mode_type", "check_filesystem"}]
        fake_os = mock.Mock(ST_RDONLY=1, R_OK=4, W_OK=2, X_OK=1)
        fake_os.statvfs.return_value = SimpleNamespace(f_flag=1)
        fake_os.stat.side_effect = lambda path: SimpleNamespace(
            st_mode=stat.S_IFREG if path == "/run/docker.sock" else stat.S_IFDIR)
        fake_os.access.return_value = False
        fake_pathlib = mock.Mock()
        fake_pathlib.Path.return_value.stat.side_effect = FileNotFoundError(2, "hidden")
        mounts = ('1 0 0:1 / / ro - ext4 /dev/root ro\n'
                  '2 1 0:2 /systemd/inaccessible/sock /run/docker.sock ro - tmpfs tmpfs rw\n'
                  '3 1 0:2 /systemd/inaccessible/dir /run/containerd ro - tmpfs tmpfs rw\n')
        scope = {"os": fake_os, "stat": stat, "pathlib": fake_pathlib, "re": re,
                 "mask_diagnostics": {}, "open": mock.mock_open(read_data=mounts)}
        exec(compile(ast.Module(body=definitions, type_ignores=[]), "<launcher-checks>", "exec"), scope)
        return scope["check_filesystem"], fake_os, fake_pathlib

    def test_launcher_checks_actual_masks_and_readonly_paths_without_connecting(self):
        check, fake_os, _ = self.launcher_filesystem_check()
        check()
        self.assertEqual([call.args[0] for call in fake_os.statvfs.call_args_list], ["/", "/usr", "/etc"])
        self.assertEqual([call.args[0] for call in fake_os.stat.call_args_list], ["/run/docker.sock", "/run/containerd"])
        self.assertEqual(fake_os.access.call_count, 6)

    def test_launcher_rejects_live_socket_unmasked_permissions_or_access(self):
        for mode in (stat.S_IFSOCK | 0o660, stat.S_IFREG | 0o660):
            check, fake_os, _ = self.launcher_filesystem_check()
            fake_os.stat.side_effect = lambda _path, mode=mode: SimpleNamespace(st_mode=mode)
            with self.assertRaises(RuntimeError):
                check()
        check, fake_os, _ = self.launcher_filesystem_check()
        fake_os.access.return_value = True
        with self.assertRaises(RuntimeError):
            check()

    def test_launcher_accepts_mode_zero_dummy_socket_on_actual_readonly_mask(self):
        check, fake_os, _ = self.launcher_filesystem_check()
        fake_os.stat.side_effect = lambda path: SimpleNamespace(
            st_mode=stat.S_IFSOCK if path == "/run/docker.sock" else stat.S_IFDIR)
        check()
        self.assertEqual(check.__globals__["mask_diagnostics"]["docker_mask"],
                         {"mode": 0, "type": "socket", "mask": True})

    def test_launcher_rejects_mode_zero_socket_without_exact_readonly_inaccessible_mount(self):
        for mount in (
            '2 1 0:2 /docker.sock /run/docker.sock ro - tmpfs tmpfs rw\n',
            '2 1 0:2 /systemd/inaccessible/sock /run/docker.sock rw - tmpfs tmpfs rw\n',
            '2 1 0:2 /systemd/inaccessible/dir /run ro - tmpfs tmpfs rw\n'):
            check, fake_os, _ = self.launcher_filesystem_check()
            fake_os.stat.side_effect = lambda path: SimpleNamespace(
                st_mode=stat.S_IFSOCK if path == "/run/docker.sock" else stat.S_IFDIR)
            check.__globals__["open"] = mock.mock_open(read_data=mount)
            with self.assertRaises(RuntimeError):
                check()
            self.assertFalse(check.__globals__["mask_diagnostics"]["docker_mask"]["mask"])

    def test_launcher_rejects_visible_home_or_writable_system_mount(self):
        check, _, fake_pathlib = self.launcher_filesystem_check()
        fake_pathlib.Path.return_value.stat.side_effect = None
        with self.assertRaises(RuntimeError):
            check()
        check, fake_os, _ = self.launcher_filesystem_check()
        fake_os.statvfs.side_effect = lambda path: SimpleNamespace(f_flag=0 if path == "/usr" else 1)
        with self.assertRaises(RuntimeError):
            check()

    def test_launcher_accepts_permission_denied_home_but_rejects_other_oserror(self):
        check, _, fake_pathlib = self.launcher_filesystem_check()
        fake_pathlib.Path.return_value.stat.side_effect = PermissionError(13, "hidden")
        check()
        fake_pathlib.Path.return_value.stat.side_effect = OSError(5, "unrelated I/O failure")
        with self.assertRaises(OSError):
            check()

    def test_every_launcher_check_has_an_explicit_allowlisted_stage(self):
        for node in ast.walk(ast.parse(probe.LAUNCHER)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "check":
                self.assertEqual(len(node.args), 2)
                self.assertIsInstance(node.args[1], ast.Constant)
                self.assertIn(node.args[1].value, probe.LAUNCHER_FAILURE_STAGES)

    def test_slice_name_cannot_create_implicit_parent_slices(self):
        with mock.patch.object(probe, "command") as command:
            for name in ("astrbot-wechat.slice", "../astrbotwechatprobe.slice", "system.slice"):
                with self.assertRaises(probe.ProbeFailure):
                    probe.start_slice(name)
            command.assert_not_called()

    def test_unit_absence_enumerates_without_loading_unknown_slice(self):
        unit = "astrbotwechatprobe123.slice"
        with mock.patch.object(probe, "command", return_value="\n") as command, \
                mock.patch.object(probe, "load_state") as load:
            probe.require_unit_absent(unit)
        self.assertEqual(command.call_args.args[0], ["/usr/bin/systemctl", "list-units", "--all", "--full",
                                                   "--plain", "--no-legend", "--no-pager", unit])
        load.assert_not_called()

    def test_unit_absence_rejects_any_existing_unit_output(self):
        with mock.patch.object(probe, "command", return_value="astrbotwechatprobe123.slice loaded active active\n"):
            with self.assertRaises(probe.ProbeFailure) as error:
                probe.require_unit_absent("astrbotwechatprobe123.slice")
        self.assertEqual(error.exception.stage, "unit_name_already_exists")

    def test_pinned_target_must_match_expected_and_differ_from_host(self):
        actual = {"dev": 4, "ino": 42}
        with mock.patch.object(probe.os, "fstat", return_value=SimpleNamespace(st_dev=4, st_ino=42)), \
                mock.patch.object(probe, "command") as command:
            for expected, host in (({"dev": 4, "ino": 43}, {"dev": 4, "ino": 1}), (actual, actual)):
                with self.assertRaises(probe.ProbeFailure):
                    probe.read_network(10, expected, host)
            command.assert_not_called()

    def test_target_queries_only_use_pinned_fd_and_read_only_ip_arguments(self):
        ident = {"dev": 4, "ino": 42}
        with mock.patch.object(probe.os, "fstat", return_value=SimpleNamespace(st_dev=4, st_ino=42)), \
                mock.patch.object(probe, "command", return_value="[]") as command:
            probe.read_network(10, ident, {"dev": 4, "ino": 1})
        self.assertEqual(command.call_count, 4)
        for call in command.call_args_list:
            args, _seconds, fds = call.args
            self.assertEqual(args[:4], ["/usr/bin/nsenter", "--net=/proc/self/fd/10", "--", probe.IP])
            self.assertIn("show", args)
            self.assertEqual(fds, (10,))
            self.assertFalse({"add", "del", "delete", "replace", "set", "flush"} & set(args))

    def test_down_tap_without_addresses_or_routes_passes(self):
        probe.check_target(network(), True)
        probe.check_target(network(False), False)

    def test_up_tap_unexpected_interface_or_address_fails(self):
        variants = []
        value = network()
        value["links"][1]["flags"].append("UP")
        variants.append(value)
        value = network()
        value["links"].append({"ifname": "eth0", "flags": []})
        variants.append(value)
        value = network()
        value["addresses"][1]["addr_info"] = [{"local": "10.0.2.100"}]
        variants.append(value)
        for value in variants:
            with self.assertRaises(probe.ProbeFailure):
                probe.check_target(value, True)

    def test_only_nonforwarding_loopback_null_route_is_allowed(self):
        value = network()
        value["routes6"] = [{"type": "unreachable", "dst": "default", "dev": "lo"}]
        probe.check_target(value, True)
        for route in ({"dst": "default", "dev": "tap0", "gateway": "10.0.2.2"},
                      {"type": "unreachable", "dst": "10.0.0.0/8", "dev": "lo"},
                      {"type": "unreachable", "dst": "default", "dev": "lo", "nexthops": []}):
            value["routes6"] = [route]
            with self.assertRaises(probe.ProbeFailure):
                probe.check_target(value, True)

    def test_stop_checks_inactive_mainpid_and_empty_cgroup(self):
        before = {"LoadState": "loaded", "ControlGroup": "/test/own.service"}
        after = {"LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0"}
        with mock.patch.object(probe, "load_state", side_effect=[(0, before), (0, after)]), \
                mock.patch.object(probe, "command_result", return_value=(0, "")), \
                mock.patch.object(probe.owned, "read", return_value=""):
            probe.stop_unit("own.service", True)

    def test_stop_rejects_leftover_processes_or_active_mainpid(self):
        before = {"LoadState": "loaded", "ControlGroup": "/test/own.service"}
        after = {"LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0"}
        with mock.patch.object(probe, "load_state", side_effect=[(0, before), (0, after)]), \
                mock.patch.object(probe, "command_result", return_value=(0, "")), \
                mock.patch.object(probe.owned, "read", return_value="999\n"):
            with self.assertRaises(probe.ProbeFailure):
                probe.stop_unit("own.service", True)
        with mock.patch.object(probe, "load_state", side_effect=[(0, before), (0, dict(after, MainPID="999"))]), \
                mock.patch.object(probe, "command_result", return_value=(0, "")):
            with self.assertRaises(probe.ProbeFailure):
                probe.stop_unit("own.service", True)

    def test_collected_unit_not_found_is_accepted_after_exact_stop(self):
        missing = {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}
        with mock.patch.object(probe, "load_state", side_effect=[(1, missing), (1, missing)]), \
                mock.patch.object(probe, "command_result", return_value=(5, "")) as command:
            probe.stop_unit("own.service", True)
            self.assertEqual(command.call_args.args[0], ["/usr/bin/systemctl", "stop", "own.service"])

    def test_proc_starttime_parser_handles_parentheses_in_comm(self):
        fields = ["S"] + ["0"] * 18 + ["12345"] + ["0"] * 4
        with mock.patch.object(probe.owned, "read", return_value="42 (a(b)c) " + " ".join(fields)):
            self.assertEqual(probe.start_ticks(42), 12345)

    def test_exited_helper_is_fully_drained_before_failure(self):
        capture = mock.Mock()
        capture.process.poll.return_value = 1
        with self.assertRaises(probe.ProbeFailure) as error:
            probe.require_helper_running(capture)
        self.assertEqual(error.exception.stage, "helper_start_failed")
        capture.finish.assert_called_once_with(2)

    def test_capture_diagnostics_emit_only_fixed_labels_and_errno(self):
        capture = mock.Mock()
        capture.process.poll.return_value = 1
        capture.output = {"stdout": bytearray(), "stderr": bytearray(
            b'private-token /home/private-user: Permission denied\n'
            b'HELPER_PREFLIGHT_FAILED\nsetns(CLONE_NEWNET): Operation not permitted\n'
            b'ioctl(TUNSETIFF): Invalid argument\n')}
        summary = probe.capture_summary(capture)
        self.assertTrue(summary["helper_preflight_failed"])
        self.assertEqual(summary["initialization_labels"], ["setns_net", "tun_setiff"])
        self.assertEqual(summary["stderr"]["errno_candidates"], [1, 13, 22])
        self.assertNotIn("private-token", str(summary))
        self.assertNotIn("private-user", str(summary))

    def test_namespace_diagnostics_use_fixed_mount_labels_and_empty_stderr_count(self):
        capture = mock.Mock()
        capture.process.poll.return_value = 226
        capture.output = {"stdout": bytearray(), "stderr": bytearray()}
        summary = probe.capture_summary(capture)
        self.assertTrue(summary["namespace_failure"])
        self.assertEqual(summary["stderr_bytes_count"], 0)
        self.assertEqual(summary["mount_path_labels"], [])
        capture.output["stderr"] = bytearray(b'Failed at step NAMESPACE: /var/tmp/private-token /run/private.sock /dev/net/tun /opt\n')
        summary = probe.capture_summary(capture)
        self.assertEqual(summary["mount_path_labels"], ["run", "dev_net_tun", "opt", "var_tmp"])
        self.assertNotIn("private-token", str(summary))
        self.assertNotIn("private.sock", str(summary))

    def test_launcher_failure_parser_allows_only_fixed_stages_and_numeric_errno(self):
        capture = mock.Mock()
        capture.process.poll.return_value = 1
        capture.output = {"stdout": bytearray(), "stderr": bytearray(
            b'HELPER_PREFLIGHT_FAILED {"stage":"namespace_open_user","errno":13,"message":"private-token"}\n'
            b'HELPER_PREFLIGHT_FAILED {"stage":"/private-path","errno":5}\n'
            b'HELPER_PREFLIGHT_FAILED {"stage":[],"errno":5}\n'
            b'HELPER_PREFLIGHT_FAILED {"stage":"root_hidden","errno":"private-token"}\n')}
        summary = probe.capture_summary(capture)
        self.assertEqual(summary["launcher_failures"], [
            {"stage": "namespace_open_user", "errno": 13}, {"stage": "root_hidden", "errno": None}])
        self.assertNotIn("private-token", str(summary))
        self.assertNotIn("/private-path", str(summary))

    def test_mask_failure_diagnostics_allow_only_mode_type_and_mask_boolean(self):
        capture = mock.Mock()
        capture.process.poll.return_value = 1
        capture.output = {"stdout": bytearray(), "stderr": bytearray(
            b'HELPER_PREFLIGHT_FAILED {"stage":"docker_mask","errno":null,"mask_diagnostic":'
            b'{"mode":432,"type":"socket","mask":true,"source":"private-token"}}\n'
            b'HELPER_PREFLIGHT_FAILED {"stage":"containerd_mask","errno":null,"mask_diagnostic":'
            b'{"mode":0,"type":"private-token","mask":true}}\n')}
        summary = probe.capture_summary(capture)
        self.assertEqual(summary["launcher_failures"], [
            {"stage": "docker_mask", "errno": None,
             "mask_diagnostic": {"mode": 432, "type": "socket", "mask": True}},
            {"stage": "containerd_mask", "errno": None}])
        self.assertNotIn("private-token", str(summary))

    def test_keeper_stdout_diagnostics_use_existing_safe_parser(self):
        capture = mock.Mock()
        capture.process.poll.return_value = 1
        capture.output = {"stdout": bytearray(b'private raw stdout'), "stderr": bytearray()}
        with mock.patch.object(probe.owned, "keeper_failure_summary", return_value=[{"stage": "known"}]) as safe:
            summary = probe.capture_summary(capture, keeper=True)
        safe.assert_called_once_with(b'private raw stdout')
        self.assertEqual(summary["keeper_failures"], [{"stage": "known"}])
        self.assertNotIn("private raw stdout", str(summary))

    def test_baseline_flags_report_actual_comparisons_independently(self):
        host = {"user": {"dev": 1, "ino": 2}, "net": {"dev": 1, "ino": 3}}
        with mock.patch.object(probe.owned, "identity", side_effect=[host["user"], host["net"]]), \
                mock.patch.object(probe, "read_network", return_value={"links": []}), \
                mock.patch.object(probe, "business_snapshot", return_value=[(42, "frpc", 7)]):
            flags, failures = probe.compare_baselines(host, {"links": []}, [])
        self.assertEqual(flags, {"host_namespace_unchanged": True, "host_network_unchanged": True,
                                 "business_processes_unchanged": False, "host_ra_timer_changed": False})
        self.assertEqual(failures, ["business_processes_changed"])

    def test_baseline_read_error_is_unknown_not_changed_and_other_checks_run(self):
        host = {"user": {"dev": 1, "ino": 2}, "net": {"dev": 1, "ino": 3}}
        with mock.patch.object(probe.owned, "identity", side_effect=[host["user"], host["net"]]), \
                mock.patch.object(probe, "read_network", side_effect=OSError(13, "private diagnostic")), \
                mock.patch.object(probe, "business_snapshot", return_value=[]):
            flags, failures = probe.compare_baselines(host, {}, [])
        self.assertIsNone(flags["host_network_unchanged"])
        self.assertTrue(flags["host_namespace_unchanged"])
        self.assertTrue(flags["business_processes_unchanged"])
        self.assertIsNone(flags["host_ra_timer_changed"])
        self.assertEqual(failures, ["host_network_baseline_unavailable"])

    @staticmethod
    def ra_network(expires=7040):
        return {"links": [{"ifname": "eth0"}], "routes4": [], "routes6": [
            {"dst": "2001:db8:1::/64", "gateway": "fe80::1", "dev": "eth0",
             "protocol": "ra", "metric": 100, "expires": expires}]}

    def test_only_positive_ra_countdown_or_refresh_is_normalized(self):
        before = self.ra_network()
        for value in (7039, 1, 8000):
            with self.subTest(expires=value):
                after = self.ra_network(value)
                self.assertEqual(probe.normalize_host_network(before), probe.normalize_host_network(after))
        self.assertEqual(before["routes6"][0]["expires"], 7040)
        self.assertIs(probe.normalize_host_network(before)["routes6"][0]["expires"], probe.POSITIVE_RA_EXPIRY)

    def test_ra_normalization_preserves_all_other_fields_and_route_existence(self):
        before = self.ra_network()
        variants = []
        for key, value in (("gateway", "fe80::2"), ("dst", "2001:db8:2::/64"),
                           ("dev", "eth1"), ("metric", 101), ("protocol", "static"),
                           ("expires", 0), ("expires", -1), ("expires", True), ("expires", "7040")):
            after = self.ra_network(7039)
            after["routes6"][0][key] = value
            variants.append(after)
        after = self.ra_network()
        del after["routes6"][0]["expires"]
        variants.append(after)
        after = self.ra_network()
        after["routes6"] = []
        variants.append(after)
        after = self.ra_network()
        after["routes6"].append(copy.deepcopy(after["routes6"][0]))
        variants.append(after)
        for after in variants:
            with self.subTest(after=after):
                self.assertNotEqual(probe.normalize_host_network(before), probe.normalize_host_network(after))

    def test_non_ra_expiry_changes_remain_visible(self):
        before, after = self.ra_network(7040), self.ra_network(7039)
        before["routes6"][0]["protocol"] = after["routes6"][0]["protocol"] = "static"
        self.assertNotEqual(probe.normalize_host_network(before), probe.normalize_host_network(after))

    def test_ra_timer_change_flag_is_independent_and_target_checks_stay_strict(self):
        before, after = self.ra_network(7040), self.ra_network(7039)
        host = {"user": {"dev": 1, "ino": 2}, "net": {"dev": 1, "ino": 3}}
        with mock.patch.object(probe.owned, "identity", side_effect=[host["user"], host["net"]]), \
                mock.patch.object(probe, "read_network", return_value=after), \
                mock.patch.object(probe, "business_snapshot", return_value=[]):
            flags, failures = probe.compare_baselines(host, before, [])
        self.assertTrue(flags["host_network_unchanged"])
        self.assertTrue(flags["host_ra_timer_changed"])
        self.assertEqual(failures, [])
        target = network()
        target["routes6"] = before["routes6"]
        with self.assertRaises(probe.ProbeFailure):
            probe.check_target(target, True)

    def test_nonlinux_root_orchestrator_rejects_before_any_command(self):
        with mock.patch.object(probe.sys, "platform", "win32"), \
                mock.patch.object(probe, "command") as command:
            with self.assertRaises(probe.ProbeFailure):
                probe.orchestrator()
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
