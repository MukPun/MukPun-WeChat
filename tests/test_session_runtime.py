"""Owner control/lifecycle tests use synthetic records and fake sandbox hosts."""
import base64
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("runtime_owner_tested", Path(__file__).resolve().parents[1] /
                                             "scripts/session_runtime.py")
owner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner)


class CommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        control = mock.patch.object(owner, "CONTROL", Path(temporary.name))
        control.start()
        self.addCleanup(control.stop)

    def test_initialization_requires_explicit_exact_flag(self):
        for value in (["initialize"], ["initialize", "--allow-fresh-login", "extra"],
                      ["resume", "--allow-fresh-login"], ["send"], []):
            with self.subTest(value=value), self.assertRaises(owner.RuntimeErrorCode):
                owner.parse_arguments(value)
        self.assertEqual(owner.parse_arguments(["initialize", "--allow-fresh-login"]), ("initialize", True))

    def test_mailbox_rejects_extra_keys_booleans_and_login_on_resume(self):
        good = {"sequence": 1, "action": "resume", "allowFreshLogin": False}
        self.assertEqual(owner.request_value(good), good)
        for mutation in ({"sequence": True}, {"sequence": 0}, {"action": "send"},
                         {"allowFreshLogin": True}, {"extra": 1}):
            with self.subTest(mutation=mutation), self.assertRaises(owner.RuntimeErrorCode):
                owner.request_value({**good, **mutation})

    def test_no_qr_bytes_enter_status_or_journal(self):
        status = owner.PublicState()
        status.begin({"sequence": 1})
        data = b"\x89PNG\r\n\x1a\n" + b"synthetic"
        with mock.patch.object(owner, "atomic_private") as write:
            status.append({"sequence": 1, "record": {"stage": "login_qr",
                          "png_base64": base64.b64encode(data).decode()}})
        write.assert_called_once_with(owner.CONTROL / "login-qr.png", data)
        self.assertNotIn("synthetic", str(status.value))
        self.assertNotIn("png_base64", str(status.value))
        self.assertEqual(status.value["qrVersion"], 1)
        self.assertEqual(status.value["records"], [])

    def test_new_command_drops_the_previous_connection_claim(self):
        status = owner.PublicState()
        status.begin({"sequence": 1})
        status.append({"sequence": 1, "record": {"stage": "runtime_status", "accounts": 1,
                                                "connected": True}})
        self.assertIs(status.value["connected"], True)
        status.begin({"sequence": 2})
        # A resume that fails never reports a fresh value, so the old claim must
        # not survive the command that could not prove it.
        self.assertIs(status.value["connected"], False)
        self.assertIs(status.value["busy"], True)

    def test_old_envelopes_cannot_complete_new_command(self):
        status = owner.PublicState()
        status.begin({"sequence": 3})
        status.append({"sequence": 2, "record": {"stage": "runtime_result", "ok": True, "code": "ok"}})
        self.assertTrue(status.value["busy"])
        self.assertFalse(status.value["done"])

    def test_tail_is_bounded_and_records_indexed(self):
        status = owner.PublicState()
        for _ in range(100):
            status.append({"sequence": 0, "record": {"stage": "runtime_status", "accounts": 0,
                                                    "connected": False}})
        self.assertEqual(status.value["recordCount"], 100)
        self.assertEqual(len(status.value["records"]), 32)
        self.assertEqual(status.value["records"][0]["index"], 69)

    def test_success_requires_all_baselines(self):
        receipt = {"ok": True, "host_namespace_unchanged": True,
                   "host_network_unchanged": True, "business_processes_unchanged": True}
        self.assertTrue(owner.cleanup_ok(receipt))
        for key in receipt:
            for value in (False, None, 1):
                with self.subTest(key=key, value=value):
                    self.assertFalse(owner.cleanup_ok({**receipt, key: value}))

    def test_stop_waits_until_owner_has_fully_exited(self):
        current = {"sequence": 1, "records": [], "qrVersion": 0, "done": True,
                   "ok": True, "ready": False, "cleanup": {"ok": True}}
        info = mock.Mock(st_mode=0o100600, st_uid=123, st_nlink=1)
        with mock.patch.object(owner, "private_directory"), \
             mock.patch.object(owner, "owner_active", side_effect=[True, True, False]) as active, \
             mock.patch.object(owner.os, "open", return_value=9), \
             mock.patch.object(owner.os, "close"), \
             mock.patch.object(owner.os, "fstat", return_value=info), \
             mock.patch.object(owner.os, "O_NOFOLLOW", 0, create=True), \
             mock.patch.object(owner.os, "O_CLOEXEC", 0, create=True), \
             mock.patch.object(owner.os, "geteuid", return_value=123, create=True), \
             mock.patch.object(owner, "fcntl", mock.Mock(LOCK_EX=1, LOCK_NB=2)), \
             mock.patch.object(owner, "read_json", side_effect=[{"sequence": 0, "busy": False}, current, current]), \
             mock.patch.object(owner, "write_json"), \
             mock.patch.object(owner.time, "sleep") as sleep, \
             mock.patch.object(owner, "wire"):
            self.assertEqual(owner.client("stop"), 0)
        self.assertEqual(active.call_count, 3)
        sleep.assert_called_once_with(0.2)


class RetentionTests(unittest.TestCase):
    def test_owner_failure_stays_failed_after_successful_cleanup(self):
        receipt = {"ok": True, "host_namespace_unchanged": True,
                   "host_network_unchanged": True, "business_processes_unchanged": True}
        host = mock.Mock(ready=True, stopped=False)
        host.pump.return_value = [{"sequence": 0, "record": {"stage": "runtime_ready", "ok": True,
                                  "restored": False, "accounts": 0, "connected": False}}]
        host.checkpoint.side_effect = RuntimeError("sensitive-not-logged")
        host.close.return_value = receipt
        status = owner.PublicState()
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(owner, "CONTROL", Path(temporary)), \
             mock.patch.object(owner, "private_directory"), \
             mock.patch.object(owner, "PublicState", return_value=status), \
             mock.patch.object(status, "publish"), \
             mock.patch.object(owner.signal, "signal"), \
             mock.patch.object(owner, "companion", return_value=mock.Mock(SessionHost=mock.Mock(return_value=host))):
            (Path(temporary) / "request.json").write_text("stale-initialize", encoding="utf-8")
            with self.assertRaises(owner.RuntimeErrorCode) as caught:
                owner.serve(True)
            self.assertEqual(caught.exception.code, "checkpoint_failed")
            self.assertFalse((Path(temporary) / "request.json").exists())
        self.assertFalse(status.value["ok"])
        self.assertEqual(status.value["code"], "checkpoint_failed")
        self.assertTrue(status.value["cleanup"]["ok"])

    def test_two_boots_retain_keys_without_authentication(self):
        instances = []
        receipt = {"ok": True, "host_namespace_unchanged": True,
                   "host_network_unchanged": True, "business_processes_unchanged": True}
        class Host:
            def __init__(self, path, *, offline, seconds):
                self.ready, self.stopped = False, False
                self.path = path
                self.ordinal = len(instances)
                instances.append(self)
                self.checkpoints = 0
                self.closed = False
                assert offline is True
            def start(self):
                pass
            def pump(self, delay):
                self.ready = True
                return [{"sequence": 0, "record": {"stage": "runtime_ready", "ok": True,
                         "restored": bool(self.ordinal), "accounts": 0, "connected": False}}]
            def fingerprints(self):
                return {name: b"x" * 32 for name in ("api_token", "storage_key", "cursor_key", "registry")}
            def checkpoint(self):
                self.checkpoints += 1
                return type("Info", (), {"files": 4, "bytes": 200, "archive_bytes": 500})()
            def close(self):
                self.closed = True
                return receipt
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(owner, "STATE", Path(temporary) / "state"), \
             mock.patch.object(owner, "private_directory", side_effect=lambda p: p.mkdir(exist_ok=True)), \
             mock.patch.object(owner, "owner_active", return_value=False), \
             mock.patch.object(owner, "companion", return_value=type("Module", (), {"SessionHost": Host})), \
             mock.patch.object(owner, "wire") as wire:
            self.assertEqual(owner.retention_proof(), 0)
        self.assertEqual(len(instances), 2)
        self.assertEqual(instances[0].path, instances[1].path)
        self.assertTrue(all(item.closed and item.checkpoints == 1 for item in instances))
        result = next(call.args[0] for call in wire.call_args_list
                      if call.args[0]["stage"] == "retention_result")
        self.assertTrue(result["ok"])
        self.assertTrue(all(result["checks"].values()))


if __name__ == "__main__":
    unittest.main()
