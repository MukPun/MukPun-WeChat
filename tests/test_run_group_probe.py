"""Sidecar identity, bounded output and cleanup tests; synthetic state only."""
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "scripts/run_group_probe.py"
SPEC = importlib.util.spec_from_file_location("run_group_tested", SOURCE)
group = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = group
SPEC.loader.exec_module(group)


class AdmissionTests(unittest.TestCase):
    def test_names_are_bounded_without_controls(self):
        for value in ("", "x" * 257, "a\n", "a\x00", "\ud800", None):
            self.assertFalse(group.valid_name(value))
        self.assertTrue(group.valid_name("测试群"))

    def test_only_current_owner_capture_selects_native(self):
        unit = "astrbot-wechat-login-123456abcdef.service"
        owner = {"MainPID": "10", "ActiveState": "active", "ControlGroup": group.OWNER_GROUP}
        def arguments(pid):
            if pid == 10:
                return [b"/usr/bin/python3", b"-I", b"-B", bytes(str(group.HERE / "session_runtime.py"), "utf-8"), b"--serve-online"]
            return [b"/usr/bin/systemd-run", b"--pipe", ("--unit=" + unit).encode()]
        with patch.object(group.tap, "details", return_value=owner), \
             patch.object(group.tap, "start_ticks", return_value=100), \
             patch.object(group, "root_identity") as identity, \
             patch.object(group, "argv", side_effect=arguments), \
             patch.object(group.owned, "read", return_value="10\n20\n"):
            self.assertEqual(group.find_native_unit(), unit)
        self.assertEqual(identity.call_args_list[-1].args, (20, group.OWNER_GROUP, "/usr/bin/systemd-run"))

    def test_another_owner_or_multiple_native_captures_reject(self):
        with patch.object(group.tap, "details", return_value={"MainPID": "10", "ActiveState": "active",
                                                           "ControlGroup": "/other.service"}):
            with self.assertRaises(Exception):
                group.find_native_unit()
        owner = {"MainPID": "10", "ActiveState": "active", "ControlGroup": group.OWNER_GROUP}
        def arguments(pid):
            if pid == 10:
                return [b"/usr/bin/python3", b"-I", b"-B", bytes(str(group.HERE / "session_runtime.py"), "utf-8"), b"--serve-online"]
            return [b"/usr/bin/systemd-run", ("--unit=astrbot-wechat-login-" + ("a" if pid == 20 else "b") * 12 + ".service").encode()]
        with patch.object(group.tap, "details", return_value=owner), \
             patch.object(group.tap, "start_ticks", return_value=100), \
             patch.object(group, "root_identity"), patch.object(group, "argv", side_effect=arguments), \
             patch.object(group.owned, "read", return_value="10\n20\n30\n"):
            with self.assertRaises(Exception):
                group.find_native_unit()

    def test_reader_only_joins_pinned_network_without_capabilities(self):
        values = group.reader_properties(Path("/fixed"), "reader", "own.slice", 9, 620)
        joined = "\n".join(values)
        self.assertIn(f"--property=NetworkNamespacePath=/proc/{group.os.getpid()}/fd/9", values)
        for value in ("CapabilityBoundingSet=", "AmbientCapabilities=", "MemoryMax=32M", "MemoryHigh=24M",
                      "MemorySwapMax=0", "TasksMax=4", "RestrictNamespaces=yes", "Slice=own.slice"):
            self.assertIn("--property=" + value, values)
        self.assertNotIn("CAP_SYS_ADMIN", joined)
        self.assertNotIn("IPAddressDeny", joined)
        self.assertNotIn("DeviceAllow", joined)
        self.assertNotIn("BindPaths=", joined)
        self.assertIn("-/run/docker.sock", joined)
        self.assertIn("-/run/frpc", joined)

    def test_token_read_is_fd_relative_private_and_never_logged(self):
        pinned = group.PinnedNative()
        pinned.host = SimpleNamespace(data_fd=77, client_uid=2000, data_dev=13, _client_alive=Mock())
        raw = b"x" * 40
        good = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=2000, st_dev=13,
                               st_nlink=1, st_size=40)
        with ExitStack() as stack:
            stack.enter_context(patch.object(group.os, "O_NOFOLLOW", 0, create=True))
            stack.enter_context(patch.object(group.os, "O_NONBLOCK", 0, create=True))
            stack.enter_context(patch.object(group.os, "O_CLOEXEC", 0, create=True))
            opened = stack.enter_context(patch.object(group.os, "open", return_value=9))
            stack.enter_context(patch.object(group.os, "close"))
            stack.enter_context(patch.object(group.os, "fstat", return_value=good))
            stack.enter_context(patch.object(group.os, "read", return_value=raw))
            self.assertEqual(pinned.token(), raw.decode())
            self.assertEqual(opened.call_args.kwargs, {"dir_fd": 77})
            good.st_nlink = 2
            with self.assertRaises(Exception):
                pinned.token()

    def test_reader_pid_and_network_must_match_before_acknowledgement(self):
        unit = "astrbot-wechat-group-read-123456abcdef.service"
        parent = "astrbotwechatprobeabcdef123456.slice"
        cgroup = "/" + parent + "/" + unit
        pinned = SimpleNamespace(netns={"dev": 1, "ino": 99},
                                 host=SimpleNamespace(parent=parent, client_uid=3000, _client_alive=Mock()))
        state = {"MainPID": "55", "ActiveState": "active", "ControlGroup": cgroup}
        status = "Uid:\t4000 4000 4000 4000\nGid:\t4000 4000 4000 4000\nNoNewPrivs:\t1\n" + "".join(
            name + ":\t0000000000000000\n" for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))
        def read(path, *args):
            return status if Path(path).name == "status" else "0::" + cgroup
        def identity(path):
            return pinned.netns if str(path).replace("\\", "/").endswith("/ns/net") else {"dev": 1, "ino": 8}
        with patch.object(group.tap, "details", return_value=state), \
             patch.object(group.tap, "start_ticks", return_value=111), \
             patch.object(group.owned, "read", side_effect=read), \
             patch.object(group.owned, "identity", side_effect=identity), \
             patch.object(group, "argv", return_value=[b"/usr/bin/python3", b"-I", b"-B", b"/opt/group_reader_client.py"]), \
             patch.object(group.tap, "cgroup_limits") as limits:
            group.verify_reader(unit, pinned)
            self.assertEqual(limits.call_args.args[0], unit)
            self.assertEqual(limits.call_args.args[1]["memory.max"], "33554432")
            pinned.netns = {"dev": 1, "ino": 100}
            with patch.object(group.owned, "identity", side_effect=lambda path: {"dev": 1, "ino": 8}):
                with self.assertRaises(Exception):
                    group.verify_reader(unit, pinned)


class RelayTests(unittest.TestCase):
    def test_send_without_explicit_approval_never_reaches_preflight(self):
        with patch.object(group.tap, "read_network") as network, patch.object(group, "PinnedNative") as native:
            with self.assertRaises(group.legacy.Failure) as caught:
                group.orchestrator("send", "测试群")
        self.assertEqual(caught.exception.stage, "send_approval_required")
        network.assert_not_called()
        native.assert_not_called()
        with patch.object(group, "orchestrator") as run, patch.object(group.legacy, "emit"):
            self.assertEqual(group.main(["--send-group", "测试群"]), 2)
        run.assert_not_called()

    def test_existing_send_attempt_blocks_readmission(self):
        controller = Mock(STATE=Path("/synthetic-state"))
        controller.fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        controller.read_json.return_value = {"status": "accepted"}
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
        with patch.object(group.os, "open", return_value=9), \
             patch.object(group.os, "O_NOFOLLOW", 0, create=True), \
             patch.object(group.os, "fstat", return_value=info), patch.object(group.os, "close") as close:
            with self.assertRaises(group.legacy.Failure) as caught:
                group.lock_once(controller, send=True)
        self.assertEqual(caught.exception.stage, "send_already_attempted")
        controller.read_json.assert_called_once_with(controller.STATE / "group-send-attempt.json")
        close.assert_called_once_with(9)

    def test_send_requires_saved_metadata_proof_matching_binding(self):
        binding = {"roomId": "synthetic-room@chatroom"}
        controller = Mock(STATE=Path("/synthetic-state"))
        with patch.object(group, "load_binding", return_value=(controller, binding)):
            for value in ({**binding, "metadataVerified": False}, {"metadataVerified": True},
                          {"roomId": "other@chatroom", "metadataVerified": True}):
                controller.read_json.return_value = value
                with self.assertRaises(group.legacy.Failure):
                    group.load_send_binding(Mock(), "测试群")
            controller.read_json.return_value = {**binding, "metadataVerified": True}
            self.assertEqual(group.load_send_binding(Mock(), "测试群"), (controller, binding))

    def test_accepted_send_saved_only_after_checkpoint(self):
        controller = Mock(STATE=Path("/synthetic-state"))
        audit = {"status": "operation", "sendId": "synthetic"}
        receipt = {"ok": True, "code": "ok", "submitted": True, "server_confirmed": True}
        controller.read_json.return_value = audit
        with patch.object(group, "checkpoint_current", side_effect=group.legacy.Failure("checkpoint_failed")):
            with self.assertRaises(group.legacy.Failure):
                group.save_send_result(controller, audit, receipt, Mock())
        controller.write_json.assert_not_called()
        with patch.object(group, "checkpoint_current") as checkpoint:
            group.save_send_result(controller, audit, receipt, Mock())
        checkpoint.assert_called_once()
        self.assertEqual(controller.write_json.call_args.args[1]["status"], "accepted")

    def test_send_receipt_requires_consistent_acceptance_flags(self):
        receipt = {"stage": "group_send_result", "ok": True, "code": "ok",
                   "submitted": True, "server_confirmed": True}
        self.assertEqual(group.parse_record(json.dumps(receipt).encode(), {"ok"}), receipt)
        for change in ({"submitted": False}, {"server_confirmed": False}, {"ok": False}, {"sendId": "private"}):
            with self.subTest(change=change), self.assertRaises(group.legacy.Failure):
                group.parse_record(json.dumps({**receipt, **change}).encode(), {"ok"})

    def test_existing_metadata_attempt_blocks_readmission(self):
        controller = Mock(STATE=Path("/synthetic-state"))
        controller.fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        controller.read_json.return_value = {"status": "operation"}
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
        with patch.object(group.os, "open", return_value=9), \
             patch.object(group.os, "O_NOFOLLOW", 0, create=True), \
             patch.object(group.os, "fstat", return_value=info), patch.object(group.os, "close") as close:
            with self.assertRaises(group.legacy.Failure) as caught:
                group.lock_metadata(controller)
        self.assertEqual(caught.exception.stage, "metadata_already_attempted")
        close.assert_called_once_with(9)
        controller.write_json.assert_not_called()

    def test_operation_ticket_is_saved_once_to_private_audit(self):
        binding = {"schemaVersion": 1, "account": "wechat-primary", "targetName": "synthetic-group",
                   "roomId": "synthetic-room@chatroom", "apiTokenFingerprint": "a" * 64}
        controller = Mock(STATE=Path("/synthetic-state"))
        controller.read_json.return_value = {**binding, "status": "admitted"}
        operation = "12345678-1234-4123-8123-123456789abc"
        group.write_metadata_audit(controller, binding, operation=operation)
        controller.write_json.assert_called_once_with(controller.STATE / "group-refresh-attempt.json",
                         {**binding, "status": "operation", "operationId": operation})
        controller.read_json.return_value = {**binding, "status": "operation", "operationId": operation}
        with self.assertRaises(group.legacy.Failure):
            group.write_metadata_audit(controller, binding, operation=operation)
    def test_records_never_accept_messages_or_identifiers(self):
        for record in ({"stage": "group_state", "state": "target_matched", "roomId": "secret"},
                       {"stage": "group_result", "ok": True, "code": "token"},
                       {"stage": "login_qr", "png_base64": "secret"},
                       {"stage": "send_result", "ok": True},
                       {"stage": "group_waiting", "marker": "AWTEST-12345678", "seconds": True}):
            with self.subTest(record=record), self.assertRaises(Exception):
                group.parse_record(json.dumps(record).encode(), {"ok"})
        with self.assertRaises(Exception):
            group.parse_record(b'{"stage":"group_result","ok":true,"ok":false,"code":"ok"}', {"ok"})

    def test_isolation_requires_all_fixed_minimum_checks(self):
        record = {"stage": "group_reader_isolated", "ok": True,
                  "checks": dict.fromkeys(group.ISOLATION_CHECKS, True)}
        self.assertEqual(group.parse_record(json.dumps(record).encode(), {"ok"}), record)
        record["checks"]["private_network"] = False
        record["ok"] = False
        self.assertEqual(group.parse_record(json.dumps(record).encode(), {"ok"}), record)
        record["ok"] = True
        with self.assertRaises(Exception):
            group.parse_record(json.dumps(record).encode(), {"ok"})

    def _exercise_reader(self, isolation_ok):
        records = [{"stage": "group_reader_isolated", "ok": isolation_ok,
                    "checks": dict.fromkeys(group.ISOLATION_CHECKS, True)},
            {"stage": "group_state", "state": "target_matched"},
            {"stage": "group_result", "ok": True, "code": "ok"}]
        records[0]["checks"]["private_network"] = isolation_ok
        captured = []
        class FakeCapture:
            def __init__(self, arguments, configuration):
                captured.append(self)
                self.arguments = arguments
                self.configuration = configuration
                self.acknowledged = False
                self.process = Mock()
                self.process.poll.return_value = self.process.wait.return_value = 0
                self.selector = Mock()
                self.selector.get_map.return_value = {1: True}
                self.output = {"stdout": bytearray(b"\n".join(json.dumps(x).encode() for x in records) + b"\n"), "stderr": bytearray()}
            def drain(self, delay):
                self.selector.get_map.return_value = {}
            def close(self):
                pass
            def acknowledge(self):
                self.acknowledged = True
        pinned = Mock()
        pinned.host = SimpleNamespace(parent="existing.slice")
        pinned.netfd = 9
        pinned.token.return_value = "synthetic-token-" + "x" * 32
        pinned.remaining_seconds.return_value = 1800
        pinned.unchanged.return_value = True
        baseline = {"host_namespace_unchanged": True, "host_network_unchanged": True,
                    "business_processes_unchanged": True}
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            stack.enter_context(patch.object(group.sys, "platform", "linux"))
            stack.enter_context(patch.object(group.os, "geteuid", return_value=0, create=True))
            stack.enter_context(patch.object(group, "companion", return_value=SimpleNamespace(RESULT_CODES={"ok"})))
            stack.enter_context(patch.object(group, "PinnedNative", return_value=pinned))
            stack.enter_context(patch.object(group.owned, "identity", return_value={"dev": 1, "ino": 2}))
            stack.enter_context(patch.object(group.tap, "read_network", return_value={}))
            stack.enter_context(patch.object(group.tap, "business_snapshot", return_value=[]))
            stack.enter_context(patch.object(group.tap, "compare_baselines", return_value=(baseline, [])))
            stack.enter_context(patch.object(group.tempfile, "mkdtemp", return_value=temporary))
            stack.enter_context(patch.object(group.legacy, "source_text", return_value="synthetic"))
            stack.enter_context(patch.object(group.legacy, "write_public"))
            stack.enter_context(patch.object(group.tap, "require_unit_absent"))
            stopped = stack.enter_context(patch.object(group.tap, "stop_unit"))
            stack.enter_context(patch.object(group, "InputCapture", FakeCapture))
            verified = stack.enter_context(patch.object(group, "verify_reader"))
            emitted = stack.enter_context(patch.object(group.legacy, "wire_record"))
            self.assertEqual(group.orchestrator("identify", "测试群"), 0 if isolation_ok else 1)
        stopped.assert_called_once()
        self.assertTrue(stopped.call_args.args[0].startswith(group.READER_PREFIX))
        self.assertIs(stopped.call_args.args[1], True)
        pinned.close.assert_called_once()
        self.assertNotIn("synthetic-token", str(emitted.call_args_list))
        self.assertIs(captured[0].acknowledged, isolation_ok)
        self.assertEqual(verified.call_count, int(isolation_ok))

    def test_cleanup_stops_only_new_reader_and_preserves_native(self):
        self._exercise_reader(True)

    def test_send_audit_precedes_ack_private_ticket_is_not_relayed(self):
        identity = "12345678-1234-4123-8123-123456789abc"
        room = "synthetic-room@chatroom"
        records = [{"stage": "group_reader_isolated", "ok": True,
                    "checks": dict.fromkeys(group.ISOLATION_CHECKS, True)},
                   {"stage": "group_send_state", "state": "target_matched"},
                   {"stage": "group_send_state", "state": "submitting"},
                   {"stage": "group_send_intent", "sendId": identity},
                   {"stage": "group_send_state", "state": "accepted"},
                   {"stage": "group_send_result", "ok": True, "code": "ok", "submitted": True, "server_confirmed": True},
                   {"stage": "group_result", "ok": True, "code": "ok"}]
        controller = Mock(STATE=Path("/synthetic-state"))
        saved, events = {}, []
        controller.write_json.side_effect = lambda path, value: saved.update({path: value})
        controller.read_json.side_effect = lambda path: saved[path]
        captured = []
        class Capture:
            def __init__(self, arguments, configuration):
                self.configuration = configuration
                captured.append(self)
                self.process = Mock()
                self.process.poll.return_value = self.process.wait.return_value = 0
                self.selector = Mock()
                self.selector.get_map.return_value = {1: True}
                self.output = {"stdout": bytearray(b"\n".join(json.dumps(x).encode() for x in records) + b"\n"), "stderr": bytearray()}
            def drain(self, delay):
                self.selector.get_map.return_value = {}
            def acknowledge(self):
                audit = saved[controller.STATE / "group-send-attempt.json"]
                if audit["status"] != "admitted" or audit["idempotencyKey"] != self.configuration["idempotencyKey"]:
                    raise AssertionError("audit missing before admission")
                events.append("ack")
            def close(self):
                pass
        pinned = Mock()
        pinned.host = SimpleNamespace(parent="existing.slice")
        pinned.netfd = 9
        pinned.token.return_value = "synthetic-token-" + "x" * 32
        pinned.remaining_seconds.return_value = 1800
        pinned.unchanged.return_value = True
        baseline = dict.fromkeys(("host_namespace_unchanged", "host_network_unchanged", "business_processes_unchanged"), True)
        binding = {"roomId": room, "targetName": "测试群"}
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            for target, name, value in (
                (group.sys, "platform", "linux"), (group, "PinnedNative", Mock(return_value=pinned)),
                (group.owned, "identity", Mock(return_value={"dev": 1, "ino": 2})),
                (group.tap, "read_network", Mock(return_value={})), (group.tap, "business_snapshot", Mock(return_value=[])),
                (group.tap, "compare_baselines", Mock(return_value=(baseline, []))),
                (group.tempfile, "mkdtemp", Mock(return_value=temporary)),
                (group.legacy, "source_text", Mock(return_value="synthetic")), (group.legacy, "write_public", Mock()),
                (group.tap, "require_unit_absent", Mock()), (group, "verify_reader", Mock()),
                (group, "InputCapture", Capture), (group, "load_send_binding", Mock(return_value=(controller, binding))),
                (group, "lock_once", Mock(return_value=None)),
                (group, "companion", Mock(return_value=SimpleNamespace(RESULT_CODES={"ok"}, TEST_TEXT="fixed")))):
                stack.enter_context(patch.object(target, name, value))
            stack.enter_context(patch.object(group.os, "geteuid", return_value=0, create=True))
            checkpoint = stack.enter_context(patch.object(group, "checkpoint_current"))
            stopped = stack.enter_context(patch.object(group.tap, "stop_unit"))
            relay = stack.enter_context(patch.object(group.legacy, "wire_record"))
            stack.enter_context(patch.object(group.legacy, "emit"))
            self.assertEqual(group.orchestrator("send", "测试群", approved=True), 0)
        self.assertEqual(events, ["ack"])
        checkpoint.assert_called_once_with(controller, pinned)
        audit = saved[controller.STATE / "group-send-attempt.json"]
        self.assertEqual(audit["status"], "accepted")
        self.assertEqual(audit["sendId"], identity)
        self.assertNotIn(identity, str(relay.call_args_list))
        self.assertNotIn(room, str(relay.call_args_list))
        self.assertNotIn(captured[0].configuration["idempotencyKey"], str(relay.call_args_list))
        stopped.assert_called_once()
        self.assertTrue(stopped.call_args.args[0].startswith(group.READER_PREFIX))

    def test_failed_isolation_emits_only_fixed_checks_without_ack(self):
        self._exercise_reader(False)


if __name__ == "__main__":
    unittest.main()
