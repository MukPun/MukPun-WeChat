"""Offline lifecycle and synthetic SQLite tests; never start the native SDK."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


PATH = Path(__file__).resolve().parents[1] / "scripts/session_runtime_client.py"
SPEC = importlib.util.spec_from_file_location("session_runtime_client_tested", PATH)
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


def config(**changes):
    return {"schemaVersion": 1, "restore": False, "offline": False,
            "runtimeSeconds": 1800, "account": runtime.ACCOUNT, **changes}


def command(sequence=1, action="status", allow=False):
    return {"schemaVersion": 1, "sequence": sequence, "action": action,
            "allowFreshLogin": allow}


class ValidationTests(unittest.TestCase):
    def test_exact_config_and_command(self):
        self.assertEqual(runtime.validate_config(config()), config())
        self.assertEqual(runtime.validate_command(command()), command())
        self.assertEqual(runtime.validate_config(config(runtimeSeconds=604800))['runtimeSeconds'], 604800)

    def test_config_rejects_wrong_types_limits_and_extra_fields(self):
        values = [None, [], config(schemaVersion=True), config(schemaVersion=2),
                  config(restore=1), config(offline=0), config(runtimeSeconds=True),
                  config(runtimeSeconds=0), config(runtimeSeconds=604801), config(offline=True, runtimeSeconds=3601),
                  config(account="other"), config(extra="private")]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime.validate_config(value)

    def test_command_rejects_send_and_unsafe_sequence_or_flags(self):
        values = [None, [], command(action="send-contact"), command(action="login"),
                  command(sequence=-1), command(sequence=True), command(allow=1),
                  {**command(), "extra": True}, {**command(), "schemaVersion": True}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime.validate_command(value)

    def test_duplicate_json_keys_and_nan_are_rejected(self):
        for raw in (b'{"sequence":1,"sequence":2}', b'{"sequence":NaN}'):
            with patch.object(runtime, "_readonly_file", return_value=9), \
                    patch.object(runtime.os, "read", return_value=raw), \
                    patch.object(runtime.os, "close"), self.assertRaises(ValueError):
                runtime._read_json(Path("unused"))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = self.stack.enter_context(patch.object(runtime.time, "monotonic", return_value=0.0))
        self.output = []
        self.emit = self.stack.enter_context(patch.object(runtime, "emit", side_effect=lambda seq, rec: self.output.append((seq, rec))))
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen = self.stack.enter_context(patch.object(runtime.subprocess, "Popen", return_value=self.process))
        self.stop = self.stack.enter_context(patch.object(runtime.empty, "stop_child", return_value=True))
        self.metadata = self.stack.enter_context(patch.object(runtime.login, "read_metadata", return_value={}))
        self.isolation = self.stack.enter_context(patch.object(runtime.login, "validate_isolation", return_value=[("guest_network", True)]))
        self.offline_isolation = self.stack.enter_context(patch.object(runtime.empty, "validate_isolation", return_value=[("loopback_only", True)]))
        self.prepare = self.stack.enter_context(patch.object(runtime.empty, "prepare_directories", return_value={"PATH": "/usr/bin:/bin"}))
        self.health = self.stack.enter_context(patch.object(runtime.empty, "wait_for_health"))
        self.token = self.stack.enter_context(patch.object(runtime.empty, "read_token", return_value="PRIVATE_NEVER_PRINT"))
        self.accounts = self.stack.enter_context(patch.object(runtime.empty, "get_json", return_value=(200, {"items": []})))
        self.detail = self.stack.enter_context(patch.object(runtime, "_account_get", return_value={"authenticated": True, "connection": "online"}))
        self.resume_client = self.stack.enter_context(patch.object(runtime.resume, "SessionResumeClient"))
        self.qr_client = self.stack.enter_context(patch.object(runtime.qr, "LoginClient"))
        self.receive_client = self.stack.enter_context(patch.object(runtime.receive, "ContactReceiveClient"))

    def controller(self, **changes):
        return runtime.RuntimeController(config(**changes))

    def last_result(self):
        return [record for _, record in self.output if record["stage"] == "runtime_result"][-1]

    def make_account(self, connected=True):
        self.accounts.return_value = (200, {"items": [{"id": runtime.ACCOUNT}]})
        self.detail.return_value = {"authenticated": connected,
                                    "connection": "online" if connected else "disconnected"}

    def test_start_offline_validates_before_any_native_or_local_api(self):
        controller = self.controller(offline=True)
        controller.start()
        self.metadata.assert_not_called()
        self.isolation.assert_not_called()
        self.offline_isolation.assert_called_once()
        self.assertEqual([rec["stage"] for _, rec in self.output], ["runtime_isolated", "runtime_ready"])
        self.assertEqual(self.output[-1][1], {"stage": "runtime_ready", "ok": True,
                                           "restored": False, "accounts": 0, "connected": False})
        self.assertEqual(self.popen.call_args.kwargs["env"]["TOKIO_WORKER_THREADS"], "1")
        self.assertIs(self.popen.call_args.kwargs["stdout"], runtime.subprocess.DEVNULL)
        self.resume_client.assert_not_called()
        self.qr_client.assert_not_called()

    def test_isolation_failure_prevents_directory_and_native_creation(self):
        self.isolation.return_value = [("guest_network", False)]
        with self.assertRaises(runtime.RuntimeFailure) as caught:
            self.controller().start()
        self.assertEqual(caught.exception.code, "isolation_failed")
        self.prepare.assert_not_called()
        self.popen.assert_not_called()

    def test_restore_review_happens_before_native_start(self):
        calls = []
        with patch.object(runtime, "wait_restore_archive", side_effect=lambda: calls.append("handoff")), \
                patch.object(runtime.state_store, "restore_archive", side_effect=lambda *a, **k: calls.append("restore")) as restore, \
                patch.object(runtime, "review_restored_outbox", side_effect=lambda: calls.append("review")), \
                patch.object(runtime.os, "geteuid", return_value=61000, create=True), \
                patch.object(runtime.subprocess, "Popen", side_effect=lambda *a, **k: calls.append("native") or self.process):
            self.controller(restore=True).start()
        self.assertEqual(calls, ["handoff", "restore", "review", "native"])
        restore.assert_called_once_with(runtime.RESTORE, runtime.empty.DATA,
                                        archive_owner_uid=61000, archive_mode=0o400)

    def test_pending_outbox_prevents_native_and_auth(self):
        with patch.object(runtime, "wait_restore_archive"), \
                patch.object(runtime.state_store, "restore_archive"), \
                patch.object(runtime, "review_restored_outbox", side_effect=runtime.RuntimeFailure("pending_outbox_review")), \
                patch.object(runtime.os, "geteuid", return_value=61000, create=True):
            with self.assertRaises(runtime.RuntimeFailure) as caught:
                self.controller(restore=True).start()
        self.assertEqual(caught.exception.code, "pending_outbox_review")
        self.popen.assert_not_called()
        self.resume_client.assert_not_called()
        self.qr_client.assert_not_called()

    def test_restore_failure_does_not_start_native(self):
        with patch.object(runtime, "wait_restore_archive", side_effect=OSError("PRIVATE_PATH")):
            with self.assertRaises(runtime.RuntimeFailure) as caught:
                self.controller(restore=True).start()
        self.assertEqual(str(caught.exception), "restore_failed")
        self.popen.assert_not_called()

    def test_native_spawn_failure_has_fixed_code(self):
        self.popen.side_effect = OSError("PRIVATE_TOKEN_NEVER_PRINT")
        with self.assertRaises(runtime.RuntimeFailure) as caught:
            self.controller().start()
        self.assertEqual(str(caught.exception), "native_spawn_failed")

    def test_inventory_rejects_other_account_instead_of_creating_alias(self):
        self.accounts.return_value = (200, {"items": [{"id": "other"}]})
        with self.assertRaises(runtime.RuntimeFailure) as caught:
            self.controller().inventory()
        self.assertEqual(caught.exception.code, "existing_account_mismatch")
        self.detail.assert_not_called()

    def test_inventory_network_and_shape_errors_are_fixed(self):
        for answer in ((500, {}), (200, []), (200, {"items": [None]}),
                       (200, {"items": [{"id": runtime.ACCOUNT}] * 2})):
            self.accounts.return_value = answer
            with self.subTest(answer=answer), self.assertRaises(runtime.RuntimeFailure) as caught:
                self.controller().inventory()
            self.assertEqual(caught.exception.code, "accounts_failed")
        self.accounts.side_effect = TimeoutError("PRIVATE_BODY")
        with self.assertRaises(runtime.RuntimeFailure) as caught:
            self.controller().inventory()
        self.assertEqual(str(caught.exception), "accounts_failed")

    def test_status_and_duplicate_mailbox_are_read_only(self):
        controller = self.controller()
        self.make_account()
        controller.execute(command())
        count = self.accounts.call_count
        controller.execute(command())
        controller.execute(command(sequence=0, action="initialize", allow=True))
        self.assertEqual(self.accounts.call_count, count)
        self.assertEqual(self.output, [(1, {"stage": "runtime_status", "accounts": 1, "connected": True}),
                                       (1, {"stage": "runtime_result", "ok": True, "code": "ok"})])
        self.resume_client.assert_not_called()
        self.qr_client.assert_not_called()
        self.stop.assert_not_called()

    def test_ordinary_status_failure_consumes_sequence_and_retains_native(self):
        controller = self.controller()
        self.accounts.side_effect = TimeoutError("private")
        controller.execute(command(sequence=7))
        controller.execute(command(sequence=7))
        self.assertEqual(controller.last_sequence, 7)
        self.accounts.assert_called_once()
        self.assertEqual(self.last_result(), {"stage": "runtime_result", "ok": False, "code": "accounts_failed"})
        self.stop.assert_not_called()
        self.accounts.side_effect = None
        controller.execute(command(sequence=8))
        self.assertTrue(self.last_result()["ok"])

    def test_offline_proof_blocks_all_auth_and_receipt_actions(self):
        controller = self.controller(offline=True)
        for seq, action in enumerate(("resume", "initialize", "receive-contact"), 1):
            controller.execute(command(sequence=seq, action=action, allow=True))
            self.assertEqual(self.last_result()["code"], "offline_operation_blocked")
        self.qr_client.assert_not_called()
        self.resume_client.assert_not_called()
        self.receive_client.assert_not_called()
        self.accounts.assert_not_called()

    def test_initialization_is_explicit_and_existing_account_requires_resume(self):
        controller = self.controller()
        controller.execute(command(action="initialize"))
        self.assertEqual(self.last_result()["code"], "fresh_login_blocked")
        self.accounts.assert_not_called()
        self.make_account()
        controller.execute(command(sequence=2, action="initialize", allow=True))
        self.assertEqual(self.last_result()["code"], "existing_account_requires_resume")
        self.qr_client.assert_not_called()
        self.resume_client.assert_not_called()

    def test_initialize_hands_off_connected_session_without_stop(self):
        client = self.qr_client.return_value
        client.run.return_value = runtime.qr.LoginResult("succeeded", False)
        with patch.object(runtime.qr, "LoopbackHTTPTransport"):
            self.controller().execute(command(action="initialize", allow=True))
        self.assertTrue(client.run.call_args.kwargs["keep_connected"])
        self.assertEqual(self.last_result()["code"], "ok")
        self.assertTrue(self.last_result()["ok"])
        self.assertEqual(self.output[-2], (1, {"stage": "runtime_status", "accounts": 1, "connected": True}))
        self.stop.assert_not_called()

    def test_initialize_disconnected_result_is_failure_but_no_retry(self):
        self.qr_client.return_value.run.return_value = runtime.qr.LoginResult("succeeded", True)
        with patch.object(runtime.qr, "LoopbackHTTPTransport"):
            controller = self.controller()
            controller.execute(command(action="initialize", allow=True))
            controller.execute(command(action="initialize", allow=True))
        self.assertEqual(self.last_result()["code"], "login_failed")
        self.qr_client.assert_called_once()
        self.stop.assert_not_called()

    def test_resume_uses_no_qr_client_and_preserves_result(self):
        self.resume_client.return_value.run.return_value = runtime.resume.ResumeResult(False, "needs_attention")
        self.controller().execute(command(action="resume"))
        self.assertEqual(self.last_result()["code"], "needs_attention")
        self.qr_client.assert_not_called()
        self.stop.assert_not_called()

    def test_successful_resume_updates_cached_status_before_result(self):
        self.resume_client.return_value.run.return_value = runtime.resume.ResumeResult(True, "connected", connected=True)
        self.controller().execute(command(sequence=8, action="resume"))
        self.assertEqual(self.output, [(8, {"stage": "runtime_status", "accounts": 1, "connected": True}),
                                       (8, {"stage": "runtime_result", "ok": True, "code": "connected"})])
        self.accounts.assert_not_called()
        self.qr_client.assert_not_called()
        self.stop.assert_not_called()

    def test_receipt_requires_online_existing_account_and_never_resumes(self):
        controller = self.controller()
        controller.execute(command(action="receive-contact"))
        self.assertEqual(self.last_result()["code"], "account_unavailable")
        self.make_account(False)
        controller.execute(command(sequence=2, action="receive-contact"))
        self.assertEqual(self.last_result()["code"], "connection_required")
        self.receive_client.assert_not_called()
        self.resume_client.assert_not_called()
        self.qr_client.assert_not_called()

    def test_receipt_success_keeps_connection_and_never_sends(self):
        self.make_account()
        self.receive_client.return_value.run.return_value = "ok"
        self.controller().execute(command(action="receive-contact"))
        self.assertTrue(self.last_result()["ok"])
        self.stop.assert_not_called()
        self.resume_client.assert_not_called()
        self.qr_client.assert_not_called()

    def test_stop_is_idempotent_and_announces_snapshot_hold(self):
        controller = self.controller()
        controller.process = self.process
        self.assertEqual(controller.execute(command(action="stop")), "stop")
        self.assertEqual(self.output[-1][1], {"stage": "runtime_stopped", "ok": True})
        self.assertTrue(controller.stop())
        controller.stopped()
        self.stop.assert_called_once_with(self.process)
        self.assertEqual(sum(rec["stage"] == "runtime_stopped" for _, rec in self.output), 1)

    def test_hold_only_release_exits_and_rejects_new_work(self):
        controller = self.controller()
        controller.last_sequence = 1
        with patch.object(runtime, "read_command", side_effect=[command(1), command(2, "resume"), command(3, "release")]), \
                patch.object(runtime.time, "sleep"):
            controller.hold()
        results = [rec for _, rec in self.output]
        self.assertEqual(results, [{"stage": "runtime_result", "ok": False, "code": "runtime_stopped"},
                                   {"stage": "runtime_result", "ok": True, "code": "ok"}])
        self.resume_client.assert_not_called()

    def test_restore_handoff_accepts_only_guest_private_read_permission(self):
        states = [SimpleNamespace(st_mode=stat.S_IFREG, st_uid=0, st_nlink=1, st_size=30),
                  SimpleNamespace(st_mode=stat.S_IFREG, st_uid=61000, st_nlink=1, st_size=30),
                  SimpleNamespace(st_mode=stat.S_IFREG | 0o400, st_uid=61000, st_nlink=1, st_size=30)]
        with patch.object(runtime, "RESTORE") as path, \
                patch.object(runtime.os, "geteuid", return_value=61000, create=True), \
                patch.object(runtime, "_readonly_file", return_value=11) as checked, \
                patch.object(runtime.os, "close") as close, \
                patch.object(runtime.time, "sleep"):
            path.lstat.side_effect = states
            runtime.wait_restore_archive()
            checked.assert_called_once_with(path, owner_uid=61000, mode=0o400)
            close.assert_called_once_with(11)

    def test_world_readable_restore_is_rejected_before_open(self):
        with patch.object(runtime, "RESTORE") as path, \
                patch.object(runtime.os, "geteuid", return_value=61000, create=True), \
                patch.object(runtime, "_readonly_file") as checked:
            path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=0, st_nlink=1, st_size=30)
            with self.assertRaises(runtime.RuntimeFailure) as caught:
                runtime.wait_restore_archive()
        self.assertEqual(caught.exception.code, "restore_failed")
        checked.assert_not_called()

    def test_main_malformed_mailbox_stops_and_holds_without_exception_content(self):
        with patch.object(runtime, "read_config", return_value=config()), \
                patch.object(runtime, "read_command", side_effect=[ValueError("PRIVATE_DETAILS"), command(1, "release")]), \
                patch.object(runtime.signal, "signal", return_value=None):
            code = runtime.main([])
        self.assertEqual(code, 1)
        self.stop.assert_called_once_with(self.process)
        self.assertIn((0, {"stage": "runtime_result", "ok": False, "code": "command_failed"}), self.output)
        self.assertNotIn("PRIVATE_DETAILS", repr(self.output))


class RestoredOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.database = self.data / "accounts" / runtime.ACCOUNT / "agent" / "control.sqlite"
        self.database.parent.mkdir(parents=True)
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.executescript("CREATE TABLE outbox(id TEXT,state TEXT); CREATE TABLE media_jobs(kind TEXT,state TEXT);")
        self.connection.commit()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(runtime.empty, "DATA", self.data))
        self.stack.enter_context(patch.object(runtime.os, "geteuid", return_value=self.database.stat().st_uid, create=True))

    def test_terminal_outbox_and_media_are_allowed_without_modification(self):
        self.connection.executemany("INSERT INTO outbox VALUES (?,?)", [("1", "accepted"), ("2", "failed"), ("3", "cancelled")])
        self.connection.executemany("INSERT INTO media_jobs VALUES (?,?)", [("send", state) for state in ("accepted", "failed", "cancelled", "unsupported", "expired")] + [("download", "queued")])
        self.connection.commit()
        before = self.database.read_bytes()
        runtime.review_restored_outbox()
        self.assertEqual(self.database.read_bytes(), before)

    def test_pending_or_unknown_outbox_blocks_replay(self):
        for state in ("queued", "submitting", "needs_review", "future_state", None):
            self.connection.execute("DELETE FROM outbox")
            self.connection.execute("INSERT INTO outbox VALUES ('1',?)", (state,))
            self.connection.commit()
            with self.subTest(state=state), self.assertRaises(runtime.RuntimeFailure) as caught:
                runtime.review_restored_outbox()
            self.assertEqual(caught.exception.code, "pending_outbox_review")

    def test_pending_media_send_and_unknown_kind_block_replay(self):
        for kind, state in (("send", "queued"), ("send", "running"), ("send", "needs_review"),
                            ("send", None), (None, "accepted"), ("future", "accepted")):
            self.connection.execute("DELETE FROM media_jobs")
            self.connection.execute("INSERT INTO media_jobs VALUES (?,?)", (kind, state))
            self.connection.commit()
            with self.subTest(kind=kind, state=state), self.assertRaises(runtime.RuntimeFailure) as caught:
                runtime.review_restored_outbox()
            self.assertEqual(caught.exception.code, "pending_outbox_review")

    def test_wal_pending_row_is_seen_before_native_starts(self):
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA wal_autocheckpoint=0")
        self.connection.execute("INSERT INTO outbox VALUES ('1','queued')")
        self.connection.commit()
        self.assertTrue(Path(str(self.database) + "-wal").exists())
        with self.assertRaises(runtime.RuntimeFailure) as caught:
            runtime.review_restored_outbox()
        self.assertEqual(caught.exception.code, "pending_outbox_review")

    def test_wrong_schema_is_not_assumed_safe(self):
        self.connection.execute("DROP TABLE outbox")
        self.connection.execute("CREATE VIEW outbox AS SELECT 'accepted' AS state")
        self.connection.commit()
        with self.assertRaises(runtime.RuntimeFailure) as caught:
            runtime.review_restored_outbox()
        self.assertEqual(caught.exception.code, "outbox_review_failed")


if __name__ == "__main__":
    unittest.main()
