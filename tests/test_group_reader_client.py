"""Synthetic stdin and mocked isolation only; no real runtime/credential access."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("group_reader_tested", Path(__file__).resolve().parents[1] / "scripts/group_reader_client.py")
reader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reader
SPEC.loader.exec_module(reader)


def configuration(**changes):
    return {"schemaVersion": 1, "account": "wechat-primary", "targetName": "synthetic-group",
            "action": "identify", "token": "synthetic-api-token-that-is-not-real", "runtimeSeconds": 60, **changes}


class ReaderTests(unittest.TestCase):
    def read(self, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode() + b"\n"
        with patch.object(reader.sys, "stdin", Mock(buffer=io.BytesIO(raw))):
            return reader.read_config()

    def test_parent_resource_limits_accept_exact_new_and_legacy_pairs(self):
        from types import SimpleNamespace
        mib = 1024*1024
        parent_path = '/sys/fs/cgroup/astrbotwechatprobe' + 'a'*12 + '.slice'
        child_path = parent_path + '/astrbot-wechat-group-read-' + 'b'*12 + '.service'
        for maximum, high, accepted in ((128,96,True),(384,288,True),(3072,2816,True),
                                         (3072,288,False),(384,2816,False),(3073,2816,False)):
            def read_text(path):
                name = str(path)
                if name == '/proc/self/status':
                    return '\n'.join(key + ':0' for key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')) + '\nNoNewPrivs:1'
                if name == '/proc/self/cgroup':
                    return '0::/' + child_path.removeprefix('/sys/fs/cgroup/')
                if name == '/proc/net/dev':
                    return 'header\nheader\nlo:0\ntap0:0'
                root, field = name.rsplit('/',1)
                values = {'memory.max':str((maximum if root==parent_path else 96)*mib),
                          'memory.high':str((high if root==parent_path else 72)*mib),
                          'memory.swap.max':'0', 'pids.max':'32' if root==parent_path else '4',
                          'cpu.max':'20000 100000'}
                return values[field]
            with self.subTest(maximum=maximum,high=high), \
                 patch.object(reader.Path, 'read_text', read_text), \
                 patch.object(reader, '_mounts', return_value=[]), \
                 patch.object(reader, '_readonly', return_value=True), \
                 patch.object(reader, '_masked', return_value=True), \
                 patch.object(reader, '_containing', return_value={'fstype':'tmpfs'}), \
                 patch.object(reader.Path, 'lstat', return_value=SimpleNamespace(st_mode=0o100444,st_uid=0,st_nlink=1,st_size=100)):
                self.assertEqual(reader.isolation_checks()['resource_limits'], accepted)

    def test_only_exact_schema_and_bounded_actions(self):
        self.assertEqual(self.read(configuration()), configuration())
        for changes in ({"action": "send"}, {"action": "initialize"}, {"token": "short"},
                        {"token": "x" * 31 + "\n"}, {"targetName": "a\n"}, {"targetName": "中" * 86},
                        {"runtimeSeconds": True}, {"runtimeSeconds": 620}, {"runtimeSeconds": 29}, {"extra": 1}):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                self.read(configuration(**changes))

    def test_duplicate_keys_constants_and_oversized_stdin_reject(self):
        for value in (b'{"schemaVersion":1,"schemaVersion":1}', b'{"value":NaN}', b"x" * 4097):
            with self.subTest(value=value[:35]), self.assertRaises(Exception):
                self.read(value)

    def test_refresh_requires_private_bound_room_field(self):
        value = configuration(action="refresh", runtimeSeconds=200, roomId="synthetic-room@chatroom")
        self.assertEqual(self.read(value), value)
        for changes in ({"roomId": "@chatroom"}, {"roomId": "direct-user"}, {"roomId": "bad\n@chatroom"}):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                self.read({**value, **changes})
        with self.assertRaises(Exception):
            self.read(configuration(action="refresh", runtimeSeconds=200))
        with self.assertRaises(Exception):
            self.read(configuration(roomId="synthetic-room@chatroom"))

    def test_send_requires_private_room_and_fixed_key(self):
        value = configuration(action="send", runtimeSeconds=120,
                              roomId="synthetic-room@chatroom", idempotencyKey="1" * 32)
        self.assertEqual(self.read(value), value)
        for key in (None, "x" * 32, "1" * 31, "1" * 33):
            with self.subTest(key=key), self.assertRaises(Exception):
                self.read({**value, "idempotencyKey": key})
        with self.assertRaises(Exception):
            self.read({key: item for key, item in value.items() if key != "idempotencyKey"})
        with self.assertRaises(Exception):
            self.read({**value, "text": "arbitrary text"})

    def invoke(self, value, *, isolated=True, action_result="ok"):
        output = io.StringIO()
        client = Mock()
        client.identify.return_value = client.run.return_value = action_result
        checks = {name: isolated for name in reader.CHECK_NAMES}
        with patch.object(reader, "read_config", return_value=value), \
             patch.object(reader, "isolation_checks", return_value=checks), \
             patch.object(reader, "await_ready", return_value=True), \
             patch.object(reader.group, "GroupReceiveClient", return_value=client), redirect_stdout(output):
            status = reader.main([])
        return status, [json.loads(line) for line in output.getvalue().splitlines()], client

    def test_isolation_failure_blocks_all_native_requests(self):
        code, records, client = self.invoke(configuration(), isolated=False)
        self.assertEqual(code, 1)
        client.identify.assert_not_called()
        client.run.assert_not_called()
        self.assertEqual(records[-1]["code"], "reader_isolation_failed")

    def test_identification_does_not_start_receive_or_expose_token(self):
        value = configuration()
        code, records, client = self.invoke(value)
        self.assertEqual(code, 0)
        client.identify.assert_called_once()
        client.run.assert_not_called()
        self.assertNotIn(value["token"], json.dumps(records))
        self.assertNotIn(value["targetName"], json.dumps(records))

    def test_receive_only_uses_existing_group_client(self):
        code, records, client = self.invoke(configuration(action="receive", runtimeSeconds=620))
        self.assertEqual(code, 0)
        client.run.assert_called_once()
        client.identify.assert_not_called()
        self.assertEqual(records[-1], {"stage": "group_result", "ok": True, "code": "ok"})

    def test_no_native_requests_without_parent_acknowledgement(self):
        output = io.StringIO()
        with patch.object(reader, "read_config", return_value=configuration()), \
             patch.object(reader, "isolation_checks", return_value={name: True for name in reader.CHECK_NAMES}), \
             patch.object(reader, "await_ready", return_value=False), \
             patch.object(reader.group, "GroupReceiveClient") as client, redirect_stdout(output):
            self.assertEqual(reader.main([]), 1)
        client.assert_not_called()
        self.assertIn("reader_handshake_failed", output.getvalue())

    def test_binding_returns_id_only_in_private_pipe_record(self):
        output = io.StringIO()
        client = Mock()
        client.bind.return_value = "synthetic-room@chatroom"
        with patch.object(reader, "read_config", return_value=configuration(action="bind", runtimeSeconds=200)), \
             patch.object(reader, "isolation_checks", return_value={name: True for name in reader.CHECK_NAMES}), \
             patch.object(reader, "await_ready", return_value=True), \
             patch.object(reader.group, "GroupReceiveClient", return_value=client), redirect_stdout(output):
            self.assertEqual(reader.main([]), 0)
        client.run.assert_not_called()
        client.identify.assert_not_called()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        private = [record for record in records if record["stage"] == "group_binding"]
        self.assertEqual(private, [{"stage": "group_binding", "roomId": "synthetic-room@chatroom"}])

    def test_metadata_operation_ticket_uses_private_pipe_record(self):
        operation = "12345678-1234-4123-8123-123456789abc"
        output = io.StringIO()
        worker = Mock()
        def work(deadline, emit, *, on_operation):
            on_operation(operation)
            emit({"stage": "group_metadata_check", "ok": True, "projection_complete": True, "name_matches": True})
            return "ok"
        worker.run.side_effect = work
        with patch.object(reader, "read_config", return_value=configuration(action="refresh", runtimeSeconds=200,
                                                                          roomId="synthetic-room@chatroom")), \
             patch.object(reader, "isolation_checks", return_value={name: True for name in reader.CHECK_NAMES}), \
             patch.object(reader, "await_ready", return_value=True), \
             patch.object(reader.metadata, "GroupMetadataClient", return_value=worker), redirect_stdout(output):
            self.assertEqual(reader.main([]), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record for record in records if record["stage"] == "group_metadata_operation"],
                         [{"stage": "group_metadata_operation", "operationId": operation}])
        self.assertNotIn("synthetic-room@chatroom", output.getvalue())
        self.assertNotIn(configuration()["token"], output.getvalue())

    def test_send_emits_private_intent_and_public_receipt_only(self):
        value = configuration(action="send", runtimeSeconds=120,
                              roomId="synthetic-room@chatroom", idempotencyKey="1" * 32)
        identity = "12345678-1234-4123-8123-123456789abc"
        output = io.StringIO()
        def create(*args, on_intent):
            def run(deadline, emit):
                on_intent(identity)
                emit({"stage": "send_state", "state": "accepted"})
                return reader.sender.SendResult(True, "ok", True, True)
            return Mock(run=run)
        with patch.object(reader, "read_config", return_value=value), \
             patch.object(reader, "isolation_checks", return_value={name: True for name in reader.CHECK_NAMES}), \
             patch.object(reader, "await_ready", return_value=True), \
             patch.object(reader.sender, "GroupSendClient", side_effect=create), redirect_stdout(output):
            self.assertEqual(reader.main([]), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item for item in records if item["stage"] == "group_send_intent"],
                         [{"stage": "group_send_intent", "sendId": identity}])
        self.assertEqual(records[-2], {"stage": "group_send_result", "ok": True, "code": "ok",
                                      "submitted": True, "server_confirmed": True})
        self.assertNotIn(value["roomId"], output.getvalue())
        self.assertNotIn(value["idempotencyKey"], output.getvalue())


if __name__ == "__main__":
    unittest.main()
