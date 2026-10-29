"""No network, native execution or privileged calls in these owner tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / "scripts" / "session_runtime_host.py"
SPEC = importlib.util.spec_from_file_location("session_runtime_host_tests", PATH)
host = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = host
SPEC.loader.exec_module(host)


def envelope(record, sequence=0):
    return json.dumps({"sequence": sequence, "record": record}).encode()


class ContractTests(unittest.TestCase):
    def test_scalar_records(self):
        for record in (
            {"stage": "runtime_isolated", "ok": True},
            {"stage": "runtime_ready", "ok": True, "restored": False, "accounts": 0, "connected": False},
            {"stage": "runtime_status", "accounts": 1, "connected": True},
            {"stage": "runtime_result", "ok": False, "code": "fresh_login_blocked"},
            {"stage": "runtime_stopped", "ok": True},
            {"stage": "resume_state", "state": next(iter(host.guest.resume.ACTIVE_STATES))},
        ):
            self.assertEqual(host.validate_envelope(envelope(record))["record"], record)

    def test_raw_ids_messages_unknown_fields_never_relay(self):
        records = (
            {"stage": "runtime_status", "accounts": 1, "connected": True, "token": "secret"},
            {"stage": "runtime_result", "ok": False, "code": "secret"},
            {"stage": "runtime_stopped", "ok": "true"},
            {"stage": "runtime_isolated", "ok": 1},
            {"stage": "runtime_status", "accounts": True, "connected": True},
            {"stage": "runtime_status", "accounts": 2, "connected": True},
            {"stage": "send_state", "state": "accepted"},
            {"stage": "arbitrary", "body": "secret"},
        )
        for record in records:
            with self.subTest(record=record), self.assertRaises(Exception):
                host.validate_envelope(envelope(record))

    def test_sequences_are_integers_and_duplicate_keys_reject(self):
        record = {"stage": "runtime_stopped", "ok": True}
        for sequence in (-1, True, 2**53, .5, "1"):
            with self.subTest(sequence=sequence), self.assertRaises(Exception):
                host.validate_envelope(envelope(record, sequence))
        with self.assertRaises(ValueError):
            host.validate_envelope(b'{"sequence":0,"sequence":1,"record":{}}')

    def test_bounds_and_private_offline_network(self):
        for offline, seconds in ((1, 30), (True, 29), (False, 604801), (True, 3601), (True, True)):
            with self.subTest(offline=offline, seconds=seconds), self.assertRaises(ValueError):
                host.SessionHost(Path("unused"), offline=offline, seconds=seconds)
        self.assertEqual(host.SessionHost(Path('unused'), offline=False, seconds=604800).seconds, 604800)
        values = host.offline_properties(Path("/fixed"), "awpl-test", "own.slice", 80)
        joined = "\n".join(values)
        self.assertIn("--property=PrivateNetwork=yes", values)
        self.assertNotIn("NetworkNamespacePath=", joined)
        self.assertIn("--property=CapabilityBoundingSet=", values)
        self.assertIn("--property=RestrictAddressFamilies=AF_INET AF_INET6", values)
        self.assertIn("size=32M", joined)
        self.assertIn("-/run/docker.sock", joined)
        self.assertIn("-/run/frpc", joined)

    def test_offline_actions_and_monotonic_mailbox(self):
        instance = host.SessionHost(Path("unused"), offline=True, seconds=30)
        instance.bundle = Path("/fixed")
        with patch.object(host.legacy, "write_public") as write, patch.object(host.os, "replace"):
            for action in ("initialize", "resume", "receive-contact"):
                with self.assertRaises(Exception):
                    instance.request(0, action, allow_fresh=True)
            write.assert_not_called()
            instance.request(0, "status")
            with self.assertRaises(Exception):
                instance.request(0, "stop")
            instance.request(1, "stop")
            self.assertEqual(json.loads(write.call_args.args[2])["sequence"], 1)


class SnapshotTests(unittest.TestCase):
    def instance(self):
        instance = host.SessionHost(Path("unused"), offline=True, seconds=30)
        instance.can_snapshot = True
        instance.data_fd, instance.client_uid, instance.data_dev = 10, 456, 789
        instance.store = Mock()
        return instance

    def test_checkpoint_freezes_exact_own_unit_and_always_thaws(self):
        instance = self.instance()
        instance.store.commit.side_effect = RuntimeError("sensitive")
        with patch.object(instance, "_freeze") as freeze, patch.object(host.tap, "command") as command:
            with self.assertRaises(RuntimeError):
                instance.checkpoint()
            freeze.assert_called_once_with()
            instance.store.commit.assert_called_once_with(10, expected_uid=456, expected_dev=789)
            command.assert_called_once_with(["/usr/bin/systemctl", "thaw", instance.unit], 3)

    def test_failed_freeze_never_commits(self):
        instance = self.instance()
        with patch.object(instance, "_freeze", side_effect=RuntimeError("failure")), \
                patch.object(host.tap, "command") as command:
            with self.assertRaises(RuntimeError):
                instance.checkpoint()
            instance.store.commit.assert_not_called()
            self.assertEqual(command.call_args.args[0][-1], instance.unit)

    def test_not_ready_cannot_overwrite_last_good_snapshot(self):
        instance = self.instance()
        instance.can_snapshot = False
        with patch.object(instance, "_freeze") as freeze:
            with self.assertRaises(Exception):
                instance.checkpoint()
            freeze.assert_not_called()
            instance.store.commit.assert_not_called()

    def test_freezer_waits_for_confirmed_kernel_state(self):
        instance = self.instance()
        with patch.object(instance, "_client_alive"), patch.object(host.tap, "command", side_effect=("", "frozen\n")) as command:
            instance._freeze()
            self.assertEqual(command.call_args_list[0].args[0], ["/usr/bin/systemctl", "freeze", instance.unit])
            self.assertIn("--property=FreezerState", command.call_args_list[1].args[0])

    def test_close_is_idempotent_and_only_stops_owned_names(self):
        instance = self.instance()
        instance.attempted = [(instance.parent, False), (instance.unit, True)]
        with patch.object(host.tap, "stop_unit") as stop:
            first = instance.close()
            self.assertTrue(first["ok"])
            self.assertIs(instance.close(), first)
            self.assertEqual(stop.call_args_list[0].args, (instance.unit, True))
            self.assertEqual(stop.call_args_list[1].args, (instance.parent, False))


class PumpTests(unittest.TestCase):
    def instance(self, records, offline=True):
        instance = host.SessionHost(Path("unused"), offline=offline, seconds=30)
        instance.bundle = Path("/fixed")
        instance.sequence = 0
        capture = Mock()
        capture.output = {"stdout": bytearray(b"\n".join(envelope(record) for record in records) + b"\n")}
        capture.process.poll.return_value = None
        instance.captures[instance.unit] = capture
        return instance

    def test_offline_cannot_relay_qr_or_other_login_activity(self):
        instance = self.instance([{"stage": "login_state", "state": "starting"}])
        with self.assertRaises(Exception):
            instance.pump(0)

    def test_archive_is_handed_to_verified_uid_then_hidden(self):
        records = [{"stage": "runtime_isolated", "ok": True},
                   {"stage": "runtime_ready", "ok": True, "restored": True, "accounts": 0, "connected": False}]
        instance = self.instance(records)
        instance.restore = True
        def pin():
            instance.client_pid, instance.client_uid = 100, 63123
        with patch.object(instance, "_pin_client", side_effect=pin) as verified, \
                patch.object(host.os, "chown", create=True) as chown, patch.object(host.os, "chmod") as chmod:
            emitted = instance.pump(0)
            verified.assert_called_once()
            chown.assert_called_once_with(instance.bundle / "restore.zip", 63123, -1, follow_symlinks=False)
            self.assertEqual([call.args[1] for call in chmod.call_args_list], [0o400, 0])
            self.assertTrue(instance.ready and instance.can_snapshot)
            self.assertEqual(len(emitted), 2)

    def test_ready_before_isolation_never_permits_snapshot(self):
        instance = self.instance([{"stage": "runtime_ready", "ok": True, "restored": False,
                                   "accounts": 0, "connected": False}])
        with self.assertRaises(Exception):
            instance.pump(0)
        self.assertFalse(instance.can_snapshot)

    def test_partial_restore_failure_never_becomes_checkpointable(self):
        instance = self.instance([{"stage": "runtime_result", "ok": False, "code": "restore_failed"},
                                  {"stage": "runtime_stopped", "ok": True}])
        instance.pump(0)
        self.assertTrue(instance.stopped)
        self.assertFalse(instance.can_snapshot)


class ShutdownTests(unittest.TestCase):
    def instance(self):
        instance = host.SessionHost(Path("unused"), offline=True, seconds=30)
        instance.client_pid, instance.client_pidfd = 100, 99
        instance.can_snapshot = True
        capture = Mock()
        capture.process.poll.return_value = 0
        instance.captures[instance.unit] = capture
        return instance

    def test_close_reports_verified_stop_and_final_snapshot_independently(self):
        for stop_ok, snapshot_ready, checkpoint_error in ((True,True,False),
                (True,True,True),(True,False,False),(False,True,False)):
            instance = self.instance()
            instance.stopped, instance.stop_ok = True, stop_ok
            instance.can_snapshot = snapshot_ready
            instance.attempted = [(instance.parent,False),(instance.unit,True)]
            with self.subTest(stop_ok=stop_ok,ready=snapshot_ready,error=checkpoint_error), \
                 patch.object(instance, 'request'), \
                 patch.object(instance, 'checkpoint', side_effect=RuntimeError('checkpoint') if checkpoint_error else None) as checkpoint, \
                 patch.object(host.tap, 'stop_unit') as stop:
                result = instance.close()
                self.assertIs(result.get('native_stop_ok'), stop_ok)
                self.assertIs(result.get('final_snapshot_saved'), stop_ok and snapshot_ready and not checkpoint_error)
                self.assertEqual(checkpoint.call_count, int(stop_ok and snapshot_ready))
                self.assertEqual(stop.call_count, 2)
                self.assertIs(instance.close(), result)

    def test_baseline_tail_failure_preserves_verified_snapshot_receipt(self):
        for raises in (False,True):
            instance = self.instance()
            instance.stopped, instance.stop_ok = True, True
            instance.before_network = {}
            with self.subTest(raises=raises), \
                 patch.object(instance, 'request'), patch.object(instance, 'checkpoint'), \
                 patch.object(host.tap, 'compare_baselines',
                    side_effect=RuntimeError('baseline') if raises else None,
                    return_value=({'host_network_unchanged':False},['network_changed'])):
                result = instance.close()
                self.assertFalse(result['ok'])
                self.assertTrue(result['native_stop_ok'])
                self.assertTrue(result['final_snapshot_saved'])
                self.assertTrue(instance.closed)
                self.assertIs(instance.close(), result)

    def test_interrupt_uses_only_validated_pinned_controller(self):
        instance = self.instance()
        with patch.object(instance, "_client_alive") as verify, \
                patch.object(host.signal, "pidfd_send_signal", create=True) as send:
            instance._interrupt_controller()
            verify.assert_called_once()
            send.assert_called_once_with(99, host.signal.SIGTERM)

    def test_identity_failure_or_missing_pidfd_never_signals(self):
        instance = self.instance()
        with patch.object(instance, "_client_alive", side_effect=RuntimeError("changed")), \
                patch.object(host.signal, "pidfd_send_signal", create=True) as send:
            with self.assertRaises(RuntimeError):
                instance._interrupt_controller()
            send.assert_not_called()
        instance.client_pidfd = None
        with patch.object(instance, "_client_alive") as verify, \
                patch.object(host.signal, "pidfd_send_signal", create=True) as send:
            with self.assertRaises(Exception):
                instance._interrupt_controller()
            verify.assert_not_called()
            send.assert_not_called()

    def test_idle_mailbox_stop_checkpoints_without_signal(self):
        instance = self.instance()
        def pump(_delay):
            instance.stopped, instance.stop_ok = True, True
            return []
        with patch.object(instance, "request") as request, patch.object(instance, "pump", side_effect=pump), \
                patch.object(instance, "checkpoint") as checkpoint, \
                patch.object(instance, "_interrupt_controller") as interrupt:
            result = instance.close()
            self.assertTrue(result["ok"])
            checkpoint.assert_called_once()
            interrupt.assert_not_called()
            self.assertEqual([call.args[1] for call in request.call_args_list], ["stop", "release"])

    def test_busy_mailbox_interrupts_then_checkpoints(self):
        instance = self.instance()
        order = []
        def interrupt():
            order.append("signal")
        def pump(_delay):
            if order:
                instance.stopped, instance.stop_ok = True, True
            return []
        def checkpoint():
            self.assertTrue(instance.stopped)
            order.append("checkpoint")
        # Two clock reads create overall/mailbox deadlines; the next exits the
        # mailbox window, then one read admits the post-signal completion pump.
        with patch.object(instance, "request") as request, patch.object(instance, "pump", side_effect=pump), \
                patch.object(instance, "checkpoint", side_effect=checkpoint), \
                patch.object(instance, "_interrupt_controller", side_effect=interrupt) as signal_controller, \
                patch.object(host.time, "monotonic", side_effect=[0, 0, 2, 2, 3]):
            result = instance.close()
            self.assertTrue(result["ok"])
            signal_controller.assert_called_once()
            self.assertEqual(order, ["signal", "checkpoint"])
            self.assertEqual([call.args[1] for call in request.call_args_list], ["stop", "release"])

if __name__ == "__main__":
    unittest.main()
