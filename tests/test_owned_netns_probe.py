"""Pure offline checks; never invoke the root orchestrator or namespace tools."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "probe-owned-netns.py"
SPEC = importlib.util.spec_from_file_location("owned_netns_probe", SOURCE)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class NamespaceProbeTests(unittest.TestCase):
    def test_single_nonroot_mapping_is_preserved(self):
        self.assertEqual(probe.id_map("         0      63635          1\n"), (0, 63635, 1))

    def test_multiple_mappings_are_rejected(self):
        with self.assertRaises(probe.ProbeFailure):
            probe.id_map("0 63635 1\n1 1000 1\n")

    def test_malformed_mapping_is_rejected(self):
        with self.assertRaises(probe.ProbeFailure):
            probe.id_map("0 63635\n")

    def test_every_capability_set_must_be_empty(self):
        status = {key: "0000000000000000" for key in probe.CAP_KEYS}
        self.assertTrue(probe.zero_caps(status))
        for key in probe.CAP_KEYS:
            with self.subTest(key=key):
                altered = dict(status, **{key: "0000000000200000"})
                self.assertFalse(probe.zero_caps(altered))

    def test_missing_capability_set_cannot_pass(self):
        with self.assertRaises(KeyError):
            probe.zero_caps({})

    @staticmethod
    def ipv6_row(destination="0" * 32, prefix="00", gateway="0" * 32,
                 flags="00200200", device="lo"):
        return f"{destination} {prefix} {'0' * 32} 00 {gateway} ffffffff 00000001 00000000 {flags} {device}\n"

    def test_empty_ipv6_table_is_safe(self):
        self.assertTrue(probe.ipv6_routes_safe(""))

    def test_empty_or_whitespace_ipv4_table_is_safe(self):
        for text in ("", "\n", " \t\n\n"):
            with self.subTest(text=text):
                self.assertTrue(probe.ipv4_routes_safe(text))

    def test_exact_ipv4_header_is_safe(self):
        header = "Iface\tDestination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        self.assertTrue(probe.ipv4_routes_safe(header))
        self.assertTrue(probe.ipv4_routes_safe("\n" + header + "  \n"))

    def test_real_ipv4_routes_are_rejected(self):
        route = "tap0 00000000 0202000A 0003 0 0 0 00000000 0 0 0\n"
        header = "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        self.assertFalse(probe.ipv4_routes_safe(route))
        self.assertFalse(probe.ipv4_routes_safe(header + route))

    def test_malformed_ipv4_routes_or_headers_are_rejected(self):
        header = "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        for text in ("garbage\n", header.replace("IRTT", "bad"), header + header,
                     "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window\n"):
            with self.subTest(text=text):
                self.assertFalse(probe.ipv4_routes_safe(text))

    def test_kernel_null_reject_route_is_safe(self):
        self.assertTrue(probe.ipv6_routes_safe(self.ipv6_row()))

    def test_real_ipv6_route_or_interface_is_rejected(self):
        for row in (self.ipv6_row(flags="00000001"), self.ipv6_row(device="eth0"),
                    self.ipv6_row(gateway="0" * 31 + "1"),
                    self.ipv6_row(destination="20010db8" + "0" * 24, prefix="20"),
                    "malformed\n"):
            with self.subTest(row=row):
                self.assertFalse(probe.ipv6_routes_safe(row))

    def test_bootstrap_only_receives_host_sys_admin(self):
        values = probe.properties("/var/tmp/known-bundle", "awpn-known")
        self.assertIn("--property=CapabilityBoundingSet=CAP_SYS_ADMIN", values)
        self.assertIn("--property=AmbientCapabilities=CAP_SYS_ADMIN", values)
        self.assertFalse(any("CAP_SETPCAP" in value for value in values))
        self.assertIn("--property=PrivateUsers=no", values)
        self.assertIn("--property=PrivateNetwork=no", values)
        self.assertIn("--property=RestrictNamespaces=user net", values)
        self.assertIn("--property=RuntimeMaxSec=20", values)
        self.assertIn("--property=TasksMax=16", values)
        self.assertIn("--property=UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME", values)

    def test_stderr_diagnostics_never_echo_paths_or_secrets(self):
        summary = probe.stderr_summary(b"unshare: /home/private-token: Operation not permitted\n")
        self.assertEqual(summary, {"source": "unshare", "errno_candidates": [1]})
        self.assertNotIn("private-token", str(summary))

    def test_read_limit_rejects_oversized_input(self):
        with mock.patch("builtins.open", return_value=io.BytesIO(b"12345")):
            with self.assertRaises(probe.ProbeFailure):
                probe.read("ignored", 4)

    @staticmethod
    def capture_with_marker():
        capture = mock.Mock()
        capture.output = {"stdout": bytearray(), "stderr": bytearray()}
        capture.process.poll.return_value = None
        marker = b'{"stage":"keeper_verified","ok":true}\n'
        chunks = iter([b"", marker])
        capture.drain.side_effect = lambda: capture.output["stdout"].extend(next(chunks, b""))
        return capture

    def test_proc_validation_waits_until_complete_keeper_marker(self):
        capture = self.capture_with_marker()
        def state(_unit):
            self.assertIn(b'"keeper_verified"', capture.output["stdout"])
            return {"MainPID": "42"}
        with mock.patch.object(probe, "unit_show", side_effect=state) as show, \
                mock.patch.object(probe, "verify_owned_keeper", return_value={"pid": 42}) as verify:
            self.assertEqual(probe.await_owned_keeper(capture, "own.service", {}), {"pid": 42})
            self.assertEqual(capture.drain.call_count, 2)
            show.assert_called_once()
            verify.assert_called_once_with(42, "own.service", {})

    def test_proc_enoent_is_retried_after_marker(self):
        capture = self.capture_with_marker()
        with mock.patch.object(probe, "unit_show", return_value={"MainPID": "42"}), \
                mock.patch.object(probe, "verify_owned_keeper", side_effect=[
                    FileNotFoundError(2, "short lived proc entry"), {"pid": 42}]) as verify:
            self.assertEqual(probe.await_owned_keeper(capture, "own.service", {}), {"pid": 42})
            self.assertEqual(verify.call_count, 2)

    def test_other_proc_errors_still_fail_closed(self):
        capture = self.capture_with_marker()
        with mock.patch.object(probe, "unit_show", return_value={"MainPID": "42"}), \
                mock.patch.object(probe, "verify_owned_keeper", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):
                probe.await_owned_keeper(capture, "own.service", {})

    def test_exited_process_is_drained_before_startup_failure(self):
        capture = self.capture_with_marker()
        capture.process.poll.return_value = 1
        with mock.patch.object(probe, "unit_show") as show:
            with self.assertRaises(probe.ProbeFailure) as error:
                probe.await_owned_keeper(capture, "own.service", {})
            self.assertEqual(error.exception.stage, "keeper_start_failed")
            capture.finish.assert_called_once_with(2)
            show.assert_not_called()

    def test_keeper_failure_summary_only_exposes_known_stage_and_errno(self):
        value = (b'{"ok":false,"stage":"keeper_caps_zero","errno":1,"secret":"never output"}\n'
                 b'{"ok":false,"stage":"arbitrary-secret-path","errno":2}\n'
                 b'{"ok":true,"stage":"keeper_lo_down"}\n'
                 b'malformed\n'
                 b'{"ok":false,"stage":"keeper_groups"}')
        self.assertEqual(probe.keeper_failure_summary(value),
                         [{"stage": "keeper_caps_zero", "errno": 1}])

    def test_keeper_failure_summary_rejects_non_numeric_errno_and_deduplicates(self):
        value = b'{"ok":false,"stage":"keeper_groups","errno":"secret"}\n' * 2
        self.assertEqual(probe.keeper_failure_summary(value),
                         [{"stage": "keeper_groups", "errno": None}])

    def test_cleanup_requires_no_pid_or_explicit_unit_not_found(self):
        self.assertTrue(probe.cleanup_confirmed({"LoadState": "not-found"}))
        for state in ("inactive", "failed"):
            self.assertTrue(probe.cleanup_confirmed({"MainPID": "0", "ActiveState": state}))
        self.assertFalse(probe.cleanup_confirmed({"MainPID": "42", "ActiveState": "active"}))
        self.assertFalse(probe.cleanup_confirmed({"MainPID": "0", "ActiveState": "deactivating"}))
        self.assertFalse(probe.cleanup_confirmed({"Result": "unavailable"}))

    def test_collected_unit_does_not_fabricate_success_diagnostics(self):
        with mock.patch.object(probe, "command", return_value=(0,
                "LoadState=not-found\nMainPID=0\nResult=success\nExecMainStatus=0\n")):
            self.assertEqual(probe.unit_show("own.service"), {"LoadState": "not-found"})

    def test_route_diagnostic_reports_structure_without_addresses(self):
        row = self.ipv6_row(destination="20010db8" + "0" * 24, prefix="20", flags="00200200")
        diagnostic = probe.route_diagnostic("header\n\n", row)
        self.assertEqual(diagnostic["ipv4_splitlines_count"], 2)
        self.assertEqual(diagnostic["ipv4_nonempty_lines_count"], 1)
        entry = diagnostic["ipv6_records"][0]
        self.assertEqual(entry["field_count"], 10)
        self.assertFalse(entry["dest_allzero"])
        self.assertEqual(entry["prefix_bits"], 32)
        self.assertEqual(entry["flags"], 0x00200200)
        self.assertNotIn("20010db8", str(diagnostic))

    def test_route_diagnostic_is_bounded_and_strictly_filtered(self):
        diagnostic = probe.route_diagnostic("header\n", self.ipv6_row() * 10)
        self.assertEqual(len(diagnostic["ipv6_records"]), 8)
        self.assertTrue(diagnostic["ipv6_truncated"])
        record = {"stage": "route_diagnostic", "ok": False, "secret": "discard", **diagnostic}
        record["ipv6_records"][0]["address"] = "discard"
        summary = probe.keeper_failure_summary(json.dumps(record).encode() + b"\n")
        self.assertEqual(len(summary), 1)
        self.assertNotIn("discard", str(summary))
        record["ipv6_records"][0]["prefix_bits"] = "arbitrary secret"
        self.assertEqual(probe.keeper_failure_summary(json.dumps(record).encode() + b"\n"), [])


if __name__ == "__main__":
    unittest.main()
