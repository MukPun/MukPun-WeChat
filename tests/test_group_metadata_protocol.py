"""Synthetic bounded metadata admission/polling; never contact a real server."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

SPEC = importlib.util.spec_from_file_location("group_metadata_tested", Path(__file__).resolve().parents[1] / "scripts/group_metadata_protocol.py")
metadata = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = metadata
SPEC.loader.exec_module(metadata)
ACCOUNT, TOKEN, ROOM, NAME = "wechat-primary", "synthetic-token-only", "1234@chatroom", "测试群"
OP = "12345678-1234-4234-8234-123456789abc"
PREFIX = "/v1/accounts/" + ACCOUNT
GROUP = PREFIX + "/groups/" + quote(ROOM, safe="")
SUCCESS = {"recordCount": 1, "failedItems": 0, "profileProjection": "complete", "coverage": "known_identity"}


def response(value, status=200, **headers):
    return metadata.Response(status, {"content-type": "application/json", **headers}, json.dumps(value).encode())


def operation(state="queued", result=None, **changes):
    return {"id": OP, "accountId": ACCOUNT, "kind": "metadata", "state": state,
            "result": (SUCCESS if state == "succeeded" else {}) if result is None else result,
            "createdAt": "2026-10-25T00:00:00Z", "updatedAt": "2026-10-25T00:00:01Z", **changes}


def group(**changes):
    return {"id": ROOM, "roomId": ROOM, "displayName": NAME, "ownerId": None,
            "announcement": None, **changes}


class Clock:
    def __init__(self):
        self.value = 0.0
    def __call__(self):
        return self.value
    def sleep(self, value):
        self.value += value


class Transport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
    def request(self, method, path, **options):
        self.calls.append((method, path, options))
        if not self.replies:
            raise AssertionError("unexpected request")
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class ProtocolTests(unittest.TestCase):
    def client(self, replies):
        transport, clock, records = Transport(replies), Clock(), []
        client = metadata.GroupMetadataClient(ACCOUNT, TOKEN, ROOM, NAME, transport=transport,
                                              clock=clock, sleep=clock.sleep)
        return client, transport, clock, records

    def test_one_bound_post_then_own_operation_and_exact_group_get(self):
        client, transport, _, records = self.client([
            response(operation(), 202), response(operation("running")),
            response(operation("succeeded")), response(group())])
        self.assertEqual(client.run(200, records.append), "ok")
        self.assertEqual([call[:2] for call in transport.calls], [
            ("POST", GROUP + "/refresh"), ("GET", PREFIX + "/operations/" + OP),
            ("GET", PREFIX + "/operations/" + OP), ("GET", GROUP)])
        self.assertTrue(all(set(call[2]) == {"timeout"} for call in transport.calls))
        self.assertEqual(records[-1], {"stage": "group_metadata_check", "ok": True,
                                       "projection_complete": True, "name_matches": True})
        for sensitive in (TOKEN, ROOM, OP, NAME):
            self.assertNotIn(sensitive, json.dumps(records))

    def test_ambiguous_admission_failure_never_resubmits(self):
        client, transport, _, records = self.client([metadata.MetadataError("transport_timeout")])
        with self.assertRaises(metadata.MetadataError) as caught:
            client.run(200, records.append)
        self.assertEqual(caught.exception.code, "transport_timeout")
        self.assertEqual(len(transport.calls), 1)
        with self.assertRaises(metadata.MetadataError) as reused:
            client.run(200, records.append)
        self.assertEqual(reused.exception.code, "client_already_used")
        self.assertEqual(len(transport.calls), 1)

    def test_private_operation_callback_precedes_poll_without_public_ticket(self):
        client, transport, _, records = self.client([
            response(operation(), 202), response(operation("succeeded")), response(group())])
        tickets = []
        self.assertEqual(client.run(200, records.append,
                         on_operation=lambda identity: tickets.append((identity, len(transport.calls)))), "ok")
        self.assertEqual(tickets, [(OP, 1)])
        self.assertNotIn(OP, json.dumps(records))

    def test_private_callback_failure_does_not_cancel_or_resubmit(self):
        client, transport, _, records = self.client([response(operation(), 202)])
        def failed(_):
            raise RuntimeError("not-exported-sensitive-error")
        with self.assertRaises(metadata.MetadataError) as caught:
            client.run(200, records.append, on_operation=failed)
        self.assertEqual(caught.exception.code, "callback_failed")
        self.assertEqual([call[0] for call in transport.calls], ["POST"])

    def test_invalid_private_callback_is_rejected_before_admission(self):
        client, transport, _, records = self.client([])
        with self.assertRaises(metadata.MetadataError) as caught:
            client.run(200, records.append, on_operation=True)
        self.assertEqual(caught.exception.code, "invalid_arguments")
        self.assertEqual(transport.calls, [])

    def test_unknown_operation_ids_accounts_kinds_and_states_stop(self):
        cases = ({"id": "secret"}, {"id": OP.upper()}, {"accountId": "other"},
                 {"kind": "login"}, {"state": "awaiting_scan"})
        for changes in cases:
            client, transport, _, records = self.client([response(operation(**changes), 202)])
            with self.subTest(changes=changes), self.assertRaises(metadata.MetadataError) as caught:
                client.run(200, records.append)
            self.assertEqual(caught.exception.code, "invalid_operation")
            self.assertEqual(len(transport.calls), 1)

    def test_poll_cannot_switch_operation_or_regress_to_queued(self):
        for replies in ([response(operation(), 202), response(operation(id="12345678-1234-4234-8234-123456789abd"))],
                        [response(operation("running"), 202), response(operation())]):
            client, transport, _, records = self.client(replies)
            with self.assertRaises(metadata.MetadataError) as caught:
                client.run(200, records.append)
            self.assertEqual(caught.exception.code, "invalid_operation")
            self.assertEqual(sum(call[0] == "POST" for call in transport.calls), 1)

    def test_success_state_requires_full_projection_and_counts(self):
        for change in ({"recordCount": 0}, {"recordCount": True}, {"failedItems": 1},
                       {"profileProjection": "partial"}, {"coverage": "all"}):
            client, transport, _, records = self.client([response(operation("succeeded", {**SUCCESS, **change}), 202)])
            with self.subTest(change=change), self.assertRaises(metadata.MetadataError) as caught:
                client.run(200, records.append)
            self.assertEqual(caught.exception.code, "metadata_incomplete")
            self.assertEqual(len(transport.calls), 1)

    def test_exact_name_and_group_identity_checked_after_projection(self):
        for changes, code in (({"displayName": "different-name"}, "name_mismatch"),
                              ({"id": "another@chatroom"}, "invalid_group"),
                              ({"roomId": "another@chatroom"}, "invalid_group")):
            client, _, _, records = self.client([response(operation("succeeded"), 202), response(group(**changes))])
            with self.subTest(changes=changes), self.assertRaises(metadata.MetadataError) as caught:
                client.run(200, records.append)
            self.assertEqual(caught.exception.code, code)
            self.assertFalse(any(record["stage"] == "group_metadata_check" for record in records))

    def test_auth_failure_stops_without_qr_connect_or_retry(self):
        client, transport, _, records = self.client([
            response(operation(), 202), response(operation("failed", {"code": "auth_required"}))])
        with self.assertRaises(metadata.MetadataError) as caught:
            client.run(200, records.append)
        self.assertEqual(caught.exception.code, "auth_required")
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])
        self.assertFalse(any("/auth/" in call[1] or "/qr" in call[1] for call in transport.calls))

    def test_http_auth_error_never_retries_or_reads_operation(self):
        client, transport, _, records = self.client([response({"error": {"code": "auth_required"}}, 409)])
        with self.assertRaises(metadata.MetadataError) as caught:
            client.run(200, records.append)
        self.assertEqual(caught.exception.code, "auth_required")
        self.assertEqual(len(transport.calls), 1)

    def test_total_deadline_caps_polling_without_second_post(self):
        client, transport, clock, records = self.client([response(operation(), 202)] + [response(operation())] * 160)
        with self.assertRaises(metadata.MetadataError) as caught:
            client.run(1000, records.append)
        self.assertEqual(caught.exception.code, "deadline_exceeded")
        self.assertLessEqual(clock.value, 150)
        self.assertLessEqual(len(transport.calls), 161)
        self.assertEqual(sum(call[0] == "POST" for call in transport.calls), 1)

    def test_redirect_encoding_duplicates_and_huge_body_fail_without_retry(self):
        cases = [response({}, 302), response({}, 202, **{"content-encoding": "gzip"}),
                 metadata.Response(202, {"content-type": "application/json"}, b'{"id":1,"id":2}'),
                 metadata.Response(202, {"content-type": "application/json"}, b"x" * (metadata.JSON_LIMIT + 1))]
        for reply in cases:
            client, transport, _, records = self.client([reply])
            with self.subTest(status=reply.status), self.assertRaises(metadata.MetadataError):
                client.run(200, records.append)
            self.assertEqual(len(transport.calls), 1)

    def test_policy_has_no_send_sync_lookup_login_or_cancel_surface(self):
        policy = metadata.RequestPolicy(ACCOUNT, ROOM)
        for method, path in (("POST", PREFIX + "/connect"), ("POST", PREFIX + "/sends"),
                             ("POST", PREFIX + "/sync"), ("GET", GROUP + "/lookup"),
                             ("GET", GROUP + "/members"), ("DELETE", PREFIX + "/operations/" + OP)):
            with self.subTest(method=method, path=path), self.assertRaises(metadata.MetadataError):
                policy.authorize(method, path)
        policy.authorize("POST", policy.refresh_path)
        with self.assertRaises(metadata.MetadataError):
            policy.authorize("POST", policy.refresh_path)

    def test_transport_omits_body_and_key_and_uses_loopback(self):
        policy = metadata.RequestPolicy(ACCOUNT, ROOM)
        transport = metadata.LoopbackMetadataTransport(policy, TOKEN)
        reply = Mock(status=202)
        reply.getheader.return_value = None
        reply.getheaders.return_value = [("Content-Type", "application/json")]
        reply.read.return_value = b"{}"
        connection = Mock()
        connection.getresponse.return_value = reply
        with patch.object(metadata.http.client, "HTTPConnection", return_value=connection) as make:
            transport.request("POST", policy.refresh_path, timeout=1)
        make.assert_called_once_with("127.0.0.1", 26174, timeout=1)
        args, keywords = connection.request.call_args
        self.assertEqual(args, ("POST", policy.refresh_path))
        self.assertEqual(set(keywords), {"headers"})
        self.assertNotIn("Idempotency-Key", keywords["headers"])
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
