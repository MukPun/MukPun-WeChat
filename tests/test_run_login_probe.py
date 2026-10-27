"""Pure offline policy tests; never execute the root orchestrator or networking."""
import ast
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts/run_login_probe.py"
SPEC = importlib.util.spec_from_file_location("run_login_probe_tested", SOURCE)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class LoginOrchestrationTests(unittest.TestCase):
    def test_source_parses_without_running_root_entrypoint(self):
        ast.parse(SOURCE.read_text())

    def test_bare_modes_block_before_preflight_dns_directories_or_processes(self):
        with contextlib.ExitStack() as stack:
            emit = stack.enter_context(mock.patch.object(probe, "emit"))
            untouched = [
                stack.enter_context(mock.patch.object(target, name, side_effect=AssertionError(name)))
                for target, name in ((probe, "orchestrator"), (probe.signal, "signal"),
                                     (probe.owned, "identity"), (probe.tap, "read_network"),
                                     (probe.tap, "command"), (probe, "resolve_hosts"),
                                     (probe.subprocess, "Popen"), (tempfile, "mkdtemp"))
            ]
            for mode in probe.FRESH_LOGIN_MODES:
                with self.subTest(mode=mode):
                    emit.reset_mock()
                    self.assertEqual(probe.main([mode]), 1)
                    emit.assert_called_once_with("login_probe_fresh_login_blocked", ok=False)
            for operation in untouched:
                operation.assert_not_called()

    def test_direct_orchestrator_also_defaults_to_blocked_before_preflight(self):
        with contextlib.ExitStack() as stack:
            emit = stack.enter_context(mock.patch.object(probe, "emit"))
            untouched = [
                stack.enter_context(mock.patch.object(target, name, side_effect=AssertionError(name)))
                for target, name in ((probe, "require"), (probe.owned, "identity"),
                                     (probe.tap, "read_network"), (probe.tap, "command"),
                                     (probe, "resolve_hosts"), (probe.subprocess, "Popen"),
                                     (tempfile, "mkdtemp"))
            ]
            for mode in probe.FRESH_LOGIN_MODES:
                with self.subTest(mode=mode):
                    emit.reset_mock()
                    self.assertEqual(probe.orchestrator(mode), 1)
                    emit.assert_called_once_with("login_probe_fresh_login_blocked", ok=False)
            for operation in untouched:
                operation.assert_not_called()

    def test_exact_flagged_mode_reaches_orchestrator_with_explicit_opt_in(self):
        with mock.patch.object(probe, "orchestrator", return_value=0) as orchestrator, \
                mock.patch.object(probe.signal, "signal"), \
                mock.patch.object(probe.signal, "SIGHUP", 1, create=True):
            for mode in probe.FRESH_LOGIN_MODES:
                with self.subTest(mode=mode):
                    orchestrator.reset_mock()
                    self.assertEqual(probe.main([mode, "--allow-fresh-login"]), 0)
                    orchestrator.assert_called_once_with(mode, allow_fresh_login=True)

    def test_malformed_arguments_are_rejected_without_fresh_login_or_preflight(self):
        with mock.patch.object(probe, "orchestrator") as orchestrator, \
                mock.patch.object(probe.signal, "signal") as signal, \
                mock.patch.object(probe, "emit") as emit:
            for args in ([], ["--unknown"], ["--login", "--send"],
                         ["--allow-fresh-login"], ["--allow-fresh-login", "--login"],
                         ["--login", "--allow-fresh-login", "--allow-fresh-login"],
                         ["--login", "--allow-fresh-login", "--send"],
                         ["--unknown", "--allow-fresh-login"]):
                with self.subTest(args=args):
                    emit.reset_mock()
                    self.assertEqual(probe.main(args), 2)
                    emit.assert_called_once_with("login_probe_arguments", ok=False)
            orchestrator.assert_not_called()
            signal.assert_not_called()

    def test_public_ipv4_rejects_host_frp_private_reserved_multicast_and_ipv6(self):
        self.assertTrue(probe.public_ipv4("101.226.142.171", set()))
        for address in ("101.226.142.171", "203.0.113.10", "127.0.0.1", "10.0.0.1", "169.254.1.1",
                        "224.0.0.1", "240.0.0.1", "192.0.2.1", "0.0.0.0", "::1", "invalid"):
            with self.subTest(address=address):
                self.assertFalse(probe.public_ipv4(address, {"101.226.142.171"}))

    def test_dns_only_requests_fixed_domains_and_rejects_any_bad_result(self):
        with mock.patch.object(probe.tap, "command", return_value='["101.226.142.171"]') as command:
            hosts = probe.resolve_hosts(set())
        self.assertEqual(set(hosts), set(probe.DOMAINS))
        self.assertEqual([call.args[0][-1] for call in command.call_args_list], list(probe.DOMAINS))
        with mock.patch.object(probe.tap, "command", return_value='["101.226.142.171","10.0.0.1"]'):
            with self.assertRaises(probe.Failure):
                probe.resolve_hosts(set())

    def test_dns_retains_eight_per_domain_within_total_allowlist_bound(self):
        answers = {
            name: [f"101.226.{10 + index}.{last}" for last in range(16, 0, -1)]
            for index, name in enumerate(probe.DOMAINS)
        }
        with mock.patch.object(probe.tap, "command",
                               side_effect=lambda args, _timeout: json.dumps(answers[args[-1]])):
            hosts = probe.resolve_hosts(set())
        self.assertEqual(set(hosts), set(probe.DOMAINS))
        for name in probe.DOMAINS:
            self.assertEqual(hosts[name], sorted(answers[name])[:8])
            self.assertEqual(len(hosts[name]), 8)
            self.assertTrue(all(probe.public_ipv4(address, set()) for address in hosts[name]))
        allowed = {address for addresses in hosts.values() for address in addresses}
        self.assertEqual(len(allowed), 32)

    def test_dns_rejects_private_tail_before_reducing_each_domain(self):
        for invalid_domain in probe.DOMAINS:
            for position in (8, 15):
                with self.subTest(domain=invalid_domain, position=position):
                    answers = {
                        name: [f"101.226.{10 + index}.{last}" for last in range(1, 17)]
                        for index, name in enumerate(probe.DOMAINS)
                    }
                    # This private address also sorts after every valid answer,
                    # so validating only a selected sorted prefix would miss it.
                    answers[invalid_domain][position] = "192.168.1.1"
                    with mock.patch.object(probe.tap, "command",
                                           side_effect=lambda args, _timeout: json.dumps(answers[args[-1]])):
                        with self.assertRaises(probe.Failure) as error:
                            probe.resolve_hosts(set())
                    self.assertEqual(error.exception.stage, "dns_public_ipv4_only")

    def test_client_only_joins_network_keeps_zero_caps_and_has_one_combined_bind_property(self):
        with mock.patch.object(probe.os, "getpid", return_value=42):
            values = probe.client_properties(Path("/var/tmp/bundle"), "awl-client", "own.slice", 11, 150)
        self.assertIn("--property=NetworkNamespacePath=/proc/42/fd/11", values)
        self.assertIn("--property=PrivateUsers=no", values)
        self.assertIn("--property=CapabilityBoundingSet=", values)
        self.assertIn("--property=AmbientCapabilities=", values)
        self.assertIn("--property=RestrictAddressFamilies=AF_INET AF_INET6", values)
        self.assertIn("--property=RuntimeMaxSec=150", values)
        self.assertIn("--property=TasksMax=32", values)
        for key in ("BindPaths", "DeviceAllow", "IPAddressDeny"):
            self.assertFalse(any(value.startswith("--property=" + key + "=") for value in values))
        self.assertIn("--property=PrivateDevices=yes", values)
        self.assertIn("--property=DevicePolicy=closed", values)
        bindings = [value for value in values if value.startswith("--property=BindReadOnlyPaths=")]
        self.assertEqual(len(bindings), 1)
        for target in (":/opt", ":/etc/hosts", ":/etc/nsswitch.conf", ":/etc/resolv.conf"):
            self.assertIn(target, bindings[0])

    def test_helper_allows_only_exact_ipv4s_and_preserves_secured_original_properties(self):
        values = probe.helper_properties(Path("/var/tmp/bundle"), "owner", "own.slice", 45, ["101.226.142.171"])
        self.assertIn("--property=IPAddressDeny=any", values)
        self.assertIn("--property=IPAddressAllow=101.226.142.171/32", values)
        self.assertIn("--property=CapabilityBoundingSet=", values)
        self.assertIn("--property=RuntimeMaxSec=45", values)
        self.assertNotIn("--property=RuntimeMaxSec=8", values)

    def test_guest_mutation_never_runs_when_fd_is_host_or_wrong_target(self):
        actual = {"dev": 1, "ino": 2}
        with mock.patch.object(probe.os, "fstat", return_value=SimpleNamespace(st_dev=1, st_ino=2)), \
                mock.patch.object(probe.tap, "command") as command:
            for expected, host in ((actual, actual), ({"dev": 1, "ino": 3}, {"dev": 1, "ino": 4})):
                with self.assertRaises(probe.Failure):
                    probe.target_command(11, expected, host, ["link", "set", "lo", "up"])
            command.assert_not_called()

    def test_guest_mutation_only_uses_fixed_commands_and_pinned_fd(self):
        actual, host = {"dev": 1, "ino": 2}, {"dev": 1, "ino": 4}
        with mock.patch.object(probe.os, "fstat", return_value=SimpleNamespace(st_dev=1, st_ino=2)), \
                mock.patch.object(probe.tap, "command") as command:
            probe.target_command(11, actual, host, ["link", "set", "lo", "up"])
            args, _timeout, fds = command.call_args.args
            self.assertEqual(args[:4], ["/usr/bin/nsenter", "--net=/proc/self/fd/11", "--", probe.tap.IP])
            self.assertEqual(fds, (11,))
            with self.assertRaises(probe.Failure):
                probe.target_command(11, actual, host, ["route", "flush", "table", "all"])

    def test_keeper_only_changes_bounded_temporary_copy_lifetime(self):
        with mock.patch.object(probe, "source_text", return_value="before\ntime.sleep(12)\nafter"):
            self.assertEqual(probe.keeper_source(45), "before\ntime.sleep(45)\nafter")
            self.assertEqual(probe.keeper_source(150), "before\ntime.sleep(150)\nafter")
            self.assertEqual(probe.keeper_source(930), "before\ntime.sleep(930)\nafter")
            with self.assertRaises(probe.Failure):
                probe.keeper_source(999)

    def test_valid_large_qr_line_is_not_truncated_to_64k(self):
        png = probe.PNG_MAGIC + b"x" * 900000
        encoded = base64.b64encode(png).decode("ascii")
        line = json.dumps({"stage": "login_qr", "png_base64": encoded}).encode()
        self.assertGreater(len(line), 65536)
        self.assertEqual(probe.parse_client_line(line, {"ok"}, set())["png_base64"], encoded)

    def test_relay_rejects_extra_fields_unknown_enums_and_non_png(self):
        for record in (
            {"stage": "login_result", "ok": True, "state": "succeeded", "code": "ok", "token": "secret"},
            {"stage": "isolation_check", "ok": True, "name": "unknown"},
            {"stage": "login_state", "state": "arbitrary-secret"},
            {"stage": "login_qr", "png_base64": base64.b64encode(b"not png").decode()},
        ):
            with self.subTest(record=record), self.assertRaises(probe.Failure):
                probe.parse_client_line(json.dumps(record).encode(), {"ok"}, {"known"})

    def test_contact_records_expose_only_profile_enums_and_temporary_marker(self):
        records = [
            {"stage": "contact_profile", "state": "created", "reason": ""},
            {"stage": "contact_state", "state": "target_matched"},
            {"stage": "contact_waiting", "marker": "AWTEST-1234abcd", "seconds": 45},
            {"stage": "contact_result", "ok": True, "code": "ok"},
        ]
        for record in records:
            self.assertEqual(probe.parse_client_line(json.dumps(record).encode(), {"ok"}, set()), record)
        for change in ({"marker": "private text"}, {"seconds": True}, {"seconds": 601}, {"username": "private"}):
            with self.assertRaises(probe.Failure):
                probe.parse_client_line(json.dumps({**records[2], **change}).encode(), {"ok"}, set())
        with self.assertRaises(probe.Failure):
            probe.parse_client_line(json.dumps({**records[-1], "ok": False}).encode(), {"ok"}, set())

    def test_receive_observation_rejects_private_fields_and_inconsistent_counts(self):
        record = {"stage": "contact_observation", "event_count": 8, "target_incoming_count": 2,
                  "target_live_count": 1, "text_checked": 1, "marker_matched": True}
        self.assertEqual(probe.parse_client_line(json.dumps(record).encode(), {"ok"}, set()), record)
        for change in ({"text_checked": True}, {"event_count": 20001}, {"target_live_count": 3},
                       {"marker_matched": "yes"}, {"text_checked": 0}, {"body": "private"}):
            with self.subTest(change=change), self.assertRaises(probe.Failure):
                probe.parse_client_line(json.dumps({**record, **change}).encode(), {"ok"}, set())

    def test_send_failure_is_not_hidden_by_login_success(self):
        status = {}
        record = {"stage": "send_result", "ok": False, "code": "outcome_unknown",
                  "submitted": True, "server_confirmed": False}
        capture = SimpleNamespace(output={"stdout": bytearray(json.dumps(record).encode()+b"\n")})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(probe.relay(capture, {"ok", "outcome_unknown"}, set(), send_status=status))
        self.assertEqual(status, {key: record[key] for key in ("ok", "code", "submitted", "server_confirmed")})
        for change in ({"body": "private"}, {"server_confirmed": True}, {"submitted": "yes"}):
            with self.assertRaises(probe.Failure):
                probe.parse_client_line(json.dumps({**record, **change}).encode(), {"ok", "outcome_unknown"}, set())

    def test_contact_failure_is_kept_separate_from_login_success_across_frames(self):
        status = {}
        records = [{"stage": "contact_result", "ok": False, "code": "profile_pending"},
                   {"stage": "login_result", "ok": True, "state": "succeeded", "code": "ok"}]
        for index, record in enumerate(records):
            capture = SimpleNamespace(output={"stdout": bytearray(json.dumps(record).encode()+b"\n")})
            with contextlib.redirect_stdout(io.StringIO()):
                complete = probe.relay(capture, {"ok", "profile_pending"}, set(), contact_status=status)
            self.assertEqual(complete, index == 1)
        self.assertEqual(status, {"ok": False, "code": "profile_pending"})

    def test_relay_emits_only_contract_and_clears_consumed_qr_buffer(self):
        records = [{"stage": "isolation_check", "name": "known", "ok": True},
                   {"stage": "login_state", "state": "awaiting_scan"},
                   {"stage": "login_result", "ok": True, "state": "succeeded", "code": "ok"}]
        capture = SimpleNamespace(output={"stdout": bytearray(b"".join(json.dumps(item).encode()+b"\n" for item in records))})
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertTrue(probe.relay(capture, {"ok"}, {"known"}))
        frames, current = [], []
        for line in stream.getvalue().splitlines():
            self.assertLess(len(line), 80)
            if line == "AWP-BEGIN":
                current = []
            elif line == "AWP-END":
                frames.append(json.loads("".join(current)))
            else:
                self.assertTrue(line.startswith("AWP "))
                current.append(line[4:])
        self.assertEqual(frames, records)
        self.assertFalse(capture.output["stdout"])

    def test_nonlinux_orchestrator_stops_before_commands(self):
        with mock.patch.object(probe.sys, "platform", "win32"), mock.patch.object(probe.tap, "command") as command:
            with self.assertRaises(probe.Failure):
                probe.orchestrator("--login", allow_fresh_login=True)
            command.assert_not_called()

    def test_dns_diagnostics_reject_unknown_fields_hosts_and_unbounded_results(self):
        valid = {"stage": "login_dns", "host": "szlong.weixin.qq.com", "result": -2}
        self.assertEqual(probe.parse_client_line(json.dumps(valid).encode(), set(), set()), valid)
        for change in ({"host": "example.com"}, {"host": "private token.wechat.com"},
                       {"host": "BAD.wechat.com"}, {"result": True}, {"result": -99999},
                       {"token": "private"}):
            with self.subTest(change=change), self.assertRaises(probe.Failure):
                probe.parse_client_line(json.dumps({**valid, **change}).encode(), set(), set())

    def test_client_diagnostics_are_fixed_and_never_echo_raw_stderr_or_qr(self):
        raw = (b'Failed to parse IPAddressDeny: Invalid argument /etc/resolv.conf private-token\n'
               b'ModuleNotFoundError private-account /opt/login_client_probe.py\n')
        capture = SimpleNamespace(process=SimpleNamespace(poll=lambda: 226),
                                  output={"stderr": bytearray(raw), "stdout": bytearray(b"private QR")})
        summary = probe.client_diagnostic(capture)
        self.assertEqual(summary["exit_code"], 226)
        self.assertTrue(summary["namespace_failure"])
        self.assertIn("IPAddressDeny", summary["property_labels"])
        self.assertIn("python_module_not_found", summary["labels"])
        self.assertIn("/etc/resolv.conf", summary["path_labels"])
        self.assertNotIn("private-token", str(summary))
        self.assertNotIn("private-account", str(summary))
        self.assertNotIn("private QR", str(summary))

    def test_short_startup_error_is_visible_only_before_any_controller_stdout(self):
        raw = b"Failed to create transient unit: duplicate assignment\n"
        capture = SimpleNamespace(process=SimpleNamespace(poll=lambda: 1),
                                  stdout_total=0, stderr_total=len(raw),
                                  output={"stderr": bytearray(raw), "stdout": bytearray()})
        self.assertEqual(probe.client_diagnostic(capture)["startup_message"], raw.decode().strip())
        capture.stdout_total = 1  # Consumed output still prevents disclosure.
        self.assertIsNone(probe.client_diagnostic(capture)["startup_message"])


if __name__ == "__main__":
    unittest.main()
