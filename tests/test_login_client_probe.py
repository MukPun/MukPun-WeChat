"""Offline-only controller tests; no binary, sockets, or real isolation changes."""
from __future__ import annotations

import base64
from contextlib import ExitStack, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / "scripts" / "login_client_probe.py"
SPEC = importlib.util.spec_from_file_location("login_client_probe_test", PATH)
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def metadata():
    return {"schemaVersion": 1, "netns": {"dev": 4, "ino": 10},
            "hostUserns": {"dev": 4, "ino": 20}, "hostNetns": {"dev": 4, "ino": 30},
            "keeperOwnerUid": 61000, "loginUnit": "astrbot-wechat-login-012345abcdef.service",
            "ipv4": "10.0.2.100/24", "gateway": "10.0.2.2", "allowedIPv4": ["1.1.1.1"]}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.process = Mock()
        self.client = Mock()
        self.client.run.return_value = probe.qr.LoginResult("qr_available", True)
        self.popen = self.stack.enter_context(patch.object(probe.subprocess, "Popen", return_value=self.process))
        self.stop = self.stack.enter_context(patch.object(probe.empty, "stop_child", return_value=True))
        self.metadata = self.stack.enter_context(patch.object(probe, "read_metadata", return_value=metadata()))
        self.isolation = self.stack.enter_context(patch.object(probe, "validate_isolation", return_value=[("guest_network", True)]))
        self.directories = self.stack.enter_context(patch.object(probe.empty, "prepare_directories", return_value={"TOKIO_WORKER_THREADS": "1"}))
        self.health = self.stack.enter_context(patch.object(probe.empty, "wait_for_health"))
        self.token = self.stack.enter_context(patch.object(probe.empty, "read_token", return_value="PRIVATE_TOKEN_NEVER_PRINT"))
        self.fresh = self.stack.enter_context(patch.object(probe, "check_fresh_state"))
        self.transport = self.stack.enter_context(patch.object(probe.qr, "LoopbackHTTPTransport", return_value=Mock()))
        self.constructor = self.stack.enter_context(patch.object(probe.qr, "LoginClient", return_value=self.client))
        self.signal = self.stack.enter_context(patch.object(probe.signal, "signal", return_value=None))
        self.clock = self.stack.enter_context(patch.object(probe.time, "monotonic", return_value=0.0))

    def run_main(self, args, *, allow_fresh_login=True):
        # Existing lifecycle tests explicitly opt into the legacy fresh flow.
        # Leave malformed argument lists untouched so rejection tests stay valid.
        if allow_fresh_login and args in ([mode] for mode in probe.FRESH_LOGIN_MODES):
            args = [*args, "--allow-fresh-login"]
        output = io.StringIO()
        with redirect_stdout(output):
            code = probe.main(args)
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_isolation_failure_never_spawns(self):
        self.isolation.return_value = [("guest_network", False)]
        code, records = self.run_main(["--login"])
        self.assertEqual(code, 1)
        self.popen.assert_not_called()
        self.assertEqual(records[-1]["code"], "isolation_failed")

    def test_unknown_arguments_never_spawn(self):
        for args in ([], ["--unknown"], ["--login", "--send"],
                     ["--allow-fresh-login"], ["--allow-fresh-login", "--login"],
                     ["--login", "--allow-fresh-login", "--allow-fresh-login"],
                     ["--login", "--allow-fresh-login", "--send"],
                     ["--unknown", "--allow-fresh-login"]):
            with self.subTest(args=args):
                code, records = self.run_main(args)
                self.assertEqual(code, 2)
                self.assertEqual(records, [{"stage": "login_result", "ok": False,
                                           "state": "failed", "code": "arguments"}])
        self.popen.assert_not_called()

    def test_bare_modes_block_before_metadata_directories_native_or_network(self):
        with ExitStack() as stack:
            extra = [
                stack.enter_context(patch.object(target, name, side_effect=AssertionError(name)))
                for target, name in ((probe.socket, "socket"), (probe.socket, "getaddrinfo"),
                                     (probe.empty, "get_json"), (probe, "_companion"),
                                     (probe, "LoginDNSDiagnostic"))
            ]
            for mode in probe.FRESH_LOGIN_MODES:
                with self.subTest(mode=mode):
                    code, records = self.run_main([mode], allow_fresh_login=False)
                    self.assertEqual(code, 1)
                    self.assertEqual(records, [{"stage": "login_result", "ok": False,
                                               "state": "failed", "code": "fresh_login_blocked"}])
            for untouched in [self.metadata, self.isolation, self.directories, self.popen,
                              self.health, self.token, self.fresh, self.transport, self.constructor,
                              self.signal, self.clock, self.stop, *extra]:
                untouched.assert_not_called()

    def test_qr_only_passes_mode_deadline_and_stops_own_child(self):
        code, records = self.run_main(["--qr-only"])
        self.assertEqual(code, 0)
        self.assertTrue(self.client.run.call_args.kwargs["qr_only"])
        self.assertEqual(self.constructor.call_args.kwargs["total_timeout"], 26.0)
        self.stop.assert_called_once_with(self.process)
        options = self.popen.call_args.kwargs
        self.assertIs(options["close_fds"], True)
        self.assertEqual(options["stdout"], probe.subprocess.DEVNULL)
        self.assertEqual(options["stderr"], probe.subprocess.DEVNULL)
        self.assertNotIn("pass_fds", options)
        self.assertNotIn("LD_PRELOAD", options["env"])
        self.assertNotIn("AW_LOGIN_DIAG_FD", options["env"])
        self.assertEqual(records[-1], {"stage": "login_result", "ok": True, "state": "qr_available", "code": "ok"})

    def test_login_qr_exact_schema_and_no_private_identifiers(self):
        png = probe.qr.PNG_MAGIC + b"offline QR"
        def run(on_qr, _on_state, *, qr_only):
            self.assertFalse(qr_only)
            on_qr(probe.qr.QrImage(1, png))
            return probe.qr.LoginResult("qr_available", True)
        self.client.run.side_effect = run
        _, records = self.run_main(["--login"])
        record = next(item for item in records if item["stage"] == "login_qr")
        self.assertEqual(set(record), {"stage", "png_base64"})
        self.assertEqual(base64.b64decode(record["png_base64"]), png)
        self.assertNotIn("PRIVATE_TOKEN_NEVER_PRINT", json.dumps(records))
        self.assertNotIn("login-probe-", json.dumps(records))

    def test_success_stops_before_success_state_is_emitted(self):
        order = []
        original_emit = probe.emit
        def run(_on_qr, on_state, *, qr_only):
            on_state("succeeded")  # Library has already disconnected here.
            return probe.qr.LoginResult("succeeded", True)
        self.client.run.side_effect = run
        self.stop.side_effect = lambda child: order.append("stop") or True
        with patch.object(probe, "emit", side_effect=lambda value: (order.append(value.get("state")), original_emit(value))):
            code, records = self.run_main(["--login"])
        self.assertEqual(code, 0)
        self.assertLess(order.index("stop"), order.index("succeeded"))
        self.assertEqual(records[-1]["state"], "succeeded")
        self.stop.assert_called_once_with(self.process)

    def test_startup_failure_stops_only_owned_child(self):
        self.health.side_effect = OSError("PRIVATE_TOKEN_NEVER_PRINT")
        code, records = self.run_main(["--login"])
        self.assertEqual(code, 1)
        self.assertEqual(records[-1]["code"], "startup_failed")
        self.stop.assert_called_once_with(self.process)
        self.assertNotIn("PRIVATE_TOKEN_NEVER_PRINT", json.dumps(records))

    def test_spawn_failure_does_not_stop_unowned_process(self):
        self.popen.side_effect = PermissionError("private path")
        _, records = self.run_main(["--login"])
        self.stop.assert_not_called()
        self.assertEqual(records[-1]["code"], "native_spawn_failed")

    def test_stop_failure_cannot_report_success(self):
        self.stop.return_value = False
        code, records = self.run_main(["--qr-only"])
        self.assertEqual(code, 1)
        self.assertEqual(records[-1]["code"], "cleanup_failed")
        self.assertFalse(records[-1]["ok"])

    def test_untrusted_error_code_is_not_relayed(self):
        self.client.run.side_effect = probe.qr.ProtocolError("token_like_secret")
        _, records = self.run_main(["--login"])
        self.assertEqual(records[-1]["code"], "protocol_failed")
        self.stop.assert_called_once_with(self.process)

    def test_invalid_qr_callback_still_stops_child(self):
        def run(on_qr, _on_state, **_kwargs):
            on_qr(probe.qr.QrImage(1, b"not png"))
        self.client.run.side_effect = run
        code, records = self.run_main(["--login"])
        self.assertEqual(code, 1)
        self.assertFalse(any(item["stage"] == "login_qr" for item in records))
        self.stop.assert_called_once_with(self.process)

    def test_contact_pending_never_queries_directory(self):
        profile = probe.qr.AuthenticatedProfile("pending", "login_database_password_required", 100.0)
        with patch.object(probe, "_companion") as companion, patch.object(probe, "emit") as emit:
            probe.receive_contact(profile, "a", "PRIVATE_TOKEN_NEVER_PRINT")
        companion.assert_not_called()
        self.assertEqual(emit.call_args_list[-1].args[0],
                         {"stage": "contact_result", "ok": False, "code": "profile_pending"})

    def test_contact_mode_only_enters_read_phase_before_success_cleanup(self):
        order = []
        self.client.run.return_value = probe.qr.LoginResult("succeeded", True)
        def run(_on_qr, on_state, *, qr_only, on_authenticated):
            self.assertFalse(qr_only)
            on_authenticated(probe.qr.AuthenticatedProfile("created", "", 100.0))
            on_state("succeeded")
            return probe.qr.LoginResult("succeeded", True)
        self.client.run.side_effect = run
        with patch.object(probe, "receive_contact", side_effect=lambda *args: order.append("receive")) as receive:
            self.stop.side_effect = lambda _process: order.append("stop") or True
            code, _ = self.run_main(["--receive-contact"])
        self.assertEqual(code, 0)
        self.assertEqual(order, ["receive", "stop"])
        self.assertEqual(receive.call_args.args[0].state, "created")
        self.assertIs(self.constructor.call_args.kwargs["extended_window"], True)
        self.assertEqual(self.constructor.call_args.kwargs["total_timeout"], 896.0)

    def test_contact_work_failure_still_returns_to_login_cleanup(self):
        fake = Mock()
        fake.ContactReceiveClient.return_value.run.side_effect = ValueError("PRIVATE_TOKEN_NEVER_PRINT")
        fake.ContactError = type("ContactError", (Exception,), {})
        with patch.object(probe, "_companion", return_value=fake), patch.object(probe, "emit") as emit:
            probe.receive_contact(probe.qr.AuthenticatedProfile("created", "", 100), "a", "secret")
        self.assertEqual(emit.call_args.args[0], {"stage": "contact_result", "ok": False, "code": "receive_failed"})

    def test_send_mode_only_runs_selected_phase_before_cleanup(self):
        order = []
        def run(_on_qr, on_state, *, qr_only, on_authenticated):
            on_authenticated(probe.qr.AuthenticatedProfile("created", "", 100))
            on_state("succeeded")
            return probe.qr.LoginResult("succeeded", True)
        self.client.run.side_effect = run
        with patch.object(probe, "send_contact", side_effect=lambda *args: order.append("send")), \
                patch.object(probe, "receive_contact") as receive:
            self.stop.side_effect = lambda _process: order.append("stop") or True
            code, _ = self.run_main(["--send-contact"])
        self.assertEqual(code, 0)
        self.assertEqual(order, ["send", "stop"])
        self.assertEqual(self.constructor.call_args.kwargs["total_timeout"], 326.0)
        self.assertIs(self.constructor.call_args.kwargs["extended_window"], True)
        receive.assert_not_called()

    def test_send_profile_pending_never_constructs_sender(self):
        with patch.object(probe, "_companion") as companion, patch.object(probe, "emit") as emit:
            probe.send_contact(probe.qr.AuthenticatedProfile("pending", "login_database_password_required", 100), "a", "secret")
        companion.assert_not_called()
        self.assertEqual(emit.call_args.args[0], {"stage": "send_result", "ok": False,
                         "code": "profile_pending", "submitted": False, "server_confirmed": False})

    def test_sender_exception_after_run_is_conservatively_unknown(self):
        sender = Mock()
        sender.run.side_effect = RuntimeError("PRIVATE_TOKEN_NEVER_PRINT")
        fake = SimpleNamespace(ContactSendClient=Mock(return_value=sender))
        with patch.object(probe, "_companion", return_value=fake), patch.object(probe, "emit") as emit:
            probe.send_contact(probe.qr.AuthenticatedProfile("created", "", 100), "a", "secret")
        self.assertEqual(emit.call_args.args[0], {"stage": "send_result", "ok": False,
                         "code": "outcome_unknown", "submitted": True, "server_confirmed": False})


    def test_diagnostic_mode_inherits_only_writer_and_closes_on_spawn_failure(self):
        with ExitStack() as stack:
            for name, value in (("O_NOFOLLOW", 0x100000), ("O_NONBLOCK", 0x200000),
                                ("O_CLOEXEC", 0x400000)):
                stack.enter_context(patch.object(probe.os, name, value, create=True))
            stack.enter_context(patch.object(probe.os, "open", return_value=39))
            file_info = Mock(st_mode=probe.stat.S_IFREG | 0o444, st_uid=0, st_size=1024)
            stack.enter_context(patch.object(probe.os, "fstat", return_value=file_info))
            stack.enter_context(patch.object(Path, "read_text", return_value=""))
            stack.enter_context(patch.object(probe.empty, "parse_mounts", return_value=[]))
            stack.enter_context(patch.object(probe.empty, "_readonly", return_value=True))
            pipe = stack.enter_context(patch.object(probe.os, "pipe2", return_value=(40, 41), create=True))
            close = stack.enter_context(patch.object(probe.os, "close"))
            stack.enter_context(patch.object(probe.os, "read", side_effect=BlockingIOError))
            for failed in (False, True):
                with self.subTest(spawn_failed=failed):
                    close.reset_mock()
                    self.stop.reset_mock()
                    def spawn(*_args, **_options):
                        self.assertNotIn(unittest.mock.call(41), close.call_args_list)
                        if failed:
                            raise OSError("PRIVATE_TOKEN_NEVER_PRINT")
                        return self.process
                    self.popen.side_effect = spawn
                    code, records = self.run_main(["--login-diagnostic"])
                    self.assertEqual(code, int(failed))
                    self.assertFalse(self.client.run.call_args.kwargs["qr_only"])
                    options = self.popen.call_args.kwargs
                    self.assertEqual(options["pass_fds"], (41,))
                    self.assertEqual(options["env"], {"TOKIO_WORKER_THREADS": "1",
                                     "LD_PRELOAD": "/opt/login-dns-probe.so", "AW_LOGIN_DIAG_FD": "41"})
                    self.assertEqual(options["stdout"], probe.subprocess.DEVNULL)
                    self.assertEqual(options["stderr"], probe.subprocess.DEVNULL)
                    self.assertEqual(close.call_args_list, [unittest.mock.call(39),
                                                           unittest.mock.call(41), unittest.mock.call(40)])
                    self.assertNotIn("PRIVATE_TOKEN_NEVER_PRINT", json.dumps(records))
                    if failed:
                        self.stop.assert_not_called()
                    else:
                        self.stop.assert_called_once_with(self.process)
            pipe.assert_called_with(probe.os.O_NONBLOCK | probe.os.O_CLOEXEC)


class DiagnosticTests(unittest.TestCase):
    def test_relay_accepts_only_exact_public_dns_schema(self):
        valid = {"host": "long.weixin.qq.com", "result": -2}
        invalid = [
            {**valid, "extra": "PRIVATE_TOKEN_NEVER_PRINT"}, {**valid, "result": True},
            {**valid, "result": 1025}, {**valid, "result": -1025}, {**valid, "result": 1.0},
            {**valid, "host": "Long.weixin.qq.com"}, {**valid, "host": "secret.example.com"},
            {**valid, "host": "-a.wechat.com"}, {**valid, "host": "a-.wechat.com"},
            {**valid, "host": "a..wechat.com"}, {**valid, "host": "a" * 64 + ".wechat.com"},
            {**valid, "host": ".".join(["a" * 63] * 4) + ".wechat.com"},
            {**valid, "host": "\u00e9.wechat.com"}, {**valid, "host": "wechat.com"},
        ]
        with patch.object(probe, "emit") as emit:
            probe.LoginDNSDiagnostic.relay(json.dumps(valid).encode())
            for value in invalid:
                probe.LoginDNSDiagnostic.relay(json.dumps(value).encode())
            for raw in (b"PRIVATE_TOKEN_NEVER_PRINT", b"\xff", b"[]",
                        b'{"host":"long.weixin.qq.com","result":0,"result":1}'):
                probe.LoginDNSDiagnostic.relay(raw)
        emit.assert_called_once_with({"stage": "login_dns", **valid})

    def test_oversize_line_is_dropped_and_next_record_recovers(self):
        diagnostic = probe.LoginDNSDiagnostic.__new__(probe.LoginDNSDiagnostic)
        diagnostic.read_fd, diagnostic.write_fd = 40, None
        diagnostic.buffer, diagnostic.discarding = b"", False
        record = b'{"host":"dns.wechat.com","result":0}\n'
        with patch.object(probe.os, "read", side_effect=[b"x" * 4096, b"x" * 4096,
                                                        b"x\n" + record, BlockingIOError]) as read, \
                patch.object(probe, "emit") as emit:
            diagnostic.drain()
            self.assertEqual(len(diagnostic.buffer), probe.DNS_BUFFER_LIMIT)
            emit.assert_not_called()
            diagnostic.drain()
            self.assertEqual(diagnostic.buffer, b"")
            self.assertFalse(diagnostic.discarding)
            self.assertTrue(all(call.args[1] <= 4096 for call in read.call_args_list))
        emit.assert_called_once_with({"stage": "login_dns", "host": "dns.wechat.com", "result": 0})


class MetadataTests(unittest.TestCase):
    def test_valid_exact_metadata(self):
        self.assertEqual(probe.validate_metadata(metadata()), metadata())

    def test_metadata_rejects_nonpublic_extra_and_boolean_identity(self):
        for change in ({"allowedIPv4": ["127.0.0.1"]}, {"extra": 1},
                       {"netns": {"dev": True, "ino": 10}}, {"netns": metadata()["hostNetns"]}):
            value = metadata()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                probe.validate_metadata(value)

    def test_identity_and_common_slice_checks(self):
        meta = metadata()
        suffix = "012345abcdef"
        text = {"/proc/self/cgroup": "0::/astrbotwechatprobe" + suffix + ".slice/" + meta["loginUnit"] + "\n",
                "/proc/self/mountinfo": "", "/etc/nsswitch.conf": "passwd: files\ngroup: files\nhosts: files\n",
                "/etc/hosts": "127.0.0.1 localhost\n::1 localhost\n" + "".join(
                    "1.1.1.1 " + host + "\n" for host in sorted(probe.LOGIN_DOMAINS))}
        with ExitStack() as stack:
            stack.enter_context(patch.object(probe.empty, "validate_isolation", return_value=[("loopback_only", False)]))
            stack.enter_context(patch.object(probe, "guest_network", return_value=True))
            stack.enter_context(patch.object(probe, "_identity", side_effect=lambda path: meta["netns" if path.endswith("net") else "hostUserns"]))
            stack.enter_context(patch.object(probe.os, "getuid", return_value=62000, create=True))
            stack.enter_context(patch.object(probe.os, "geteuid", return_value=62000, create=True))
            stack.enter_context(patch.object(Path, "read_text", lambda path: text[str(path).replace("\\", "/")]))
            stack.enter_context(patch.object(Path, "read_bytes", return_value=b""))
            stack.enter_context(patch.object(probe.empty, "parse_mounts", return_value=[]))
            stack.enter_context(patch.object(probe.empty, "_readonly", return_value=True))
            checks = dict(probe.validate_isolation(meta))
            self.assertTrue(all(checks.values()), checks)
            with patch.object(probe.os, "getuid", return_value=61000):
                self.assertFalse(dict(probe.validate_isolation(meta))["distinct_dynamic_uid"])

    def test_nonempty_accounts_rejected_without_login_api(self):
        with patch.object(probe.empty, "get_json", side_effect=[(401, {}), (200, {"items": [{"id": "private"}]})]) as get:
            with self.assertRaises(probe.empty.ProbeFailure):
                probe.check_fresh_state("secret")
            self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
