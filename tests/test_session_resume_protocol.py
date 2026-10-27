"""Offline saved-session protocol tests; no server or profile is accessed."""

from __future__ import annotations

import http.client
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


PATH = Path(__file__).resolve().parents[1] / "scripts" / "session_resume_protocol.py"
SPEC = importlib.util.spec_from_file_location("session_resume_protocol", PATH)
resume = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resume
SPEC.loader.exec_module(resume)
ACCOUNT = "wechat-primary"
PREFIX = "/v1/accounts/" + ACCOUNT
OP = "a1234567-1234-4234-8234-123456789abc"
OTHER_OP = "b1234567-1234-4234-8234-123456789abc"
OP_PATH = PREFIX + "/operations/" + OP


def response(value, status=200):
    return resume.Response(status, {"content-type": "application/json"}, json.dumps(value).encode())


def status(connected=False):
    return {"id": ACCOUNT, "authenticated": connected,
            "connection": "online" if connected else "disconnected"}


def accounts(*values):
    return response({"items": list(values)})


def operation(state="starting", **metadata):
    return {"id": OP, "accountId": ACCOUNT, "kind": "login", "state": state, "result": metadata}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Transport:
    def __init__(self, replies):
        self.replies, self.requests, self.bound = list(replies), [], None

    def bind_operation(self, operation_id):
        self.bound = operation_id

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if not self.replies:
            raise AssertionError("unexpected request")
        value = self.replies.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ResumeClientTests(unittest.TestCase):
    def client(self, replies, **kwargs):
        clock, transport = Clock(), Transport(replies)
        client = resume.SessionResumeClient(ACCOUNT, "secret-token", transport=transport,
                                            clock=clock, sleep=clock.sleep, **kwargs)
        return client, transport, clock

    def test_active_validated_connection_is_reused_without_post(self):
        client, transport, _ = self.client([accounts(status()), response(status(True))])
        result = client.run(30, lambda _: self.fail("no operation"))
        self.assertEqual(result, resume.ResumeResult(True, "connected", connected=True))
        self.assertEqual([request[:2] for request in transport.requests],
                         [("GET", "/v1/accounts"), ("GET", PREFIX)])

    def test_empty_store_requests_initialization_without_creating_account(self):
        client, transport, _ = self.client([accounts()])
        self.assertEqual(client.run(30, lambda _: None),
                         resume.ResumeResult(False, "needs_initialization", needs_initialization=True))
        self.assertEqual(len(transport.requests), 1)

    def test_missing_or_duplicate_accounts_stop_without_creation(self):
        for values in ([{"id": "other"}], [status(), status()],
                       [status(), {"id": "other"}, {"id": "other"}]):
            with self.subTest(values=values):
                client, transport, _ = self.client([accounts(*values)])
                self.assertEqual(client.run(30, lambda _: None).code, "existing_account_mismatch")
                self.assertEqual(len(transport.requests), 1)

    def test_other_unique_accounts_do_not_change_selected_account(self):
        client, transport, _ = self.client([
            accounts({"id": "other"}, status()), response(status(True)),
        ])
        self.assertTrue(client.run(30, lambda _: None).ok)
        self.assertEqual(transport.requests[-1][1], PREFIX)

    def test_restore_posts_connect_once_without_body_key_or_qr_and_retains_connection(self):
        client, transport, _ = self.client([
            accounts(status()), response(status()), response(operation(), 202),
            response(operation("resuming", method="PRIVATE")),
            response(operation("awaiting_approval", qrAvailable=True, reason="PRIVATE")),
            response(operation("awaiting_approval", qrAvailable=True)),
            response(operation("authenticating")), response(operation("succeeded")),
            response(status(True)),
        ])
        emitted = []
        result = client.run(30, emitted.append)
        self.assertTrue(result.connected)
        self.assertEqual(emitted, [{"stage": "resume_state", "state": state}
                                   for state in ("starting", "resuming", "awaiting_approval", "authenticating")])
        self.assertNotIn("PRIVATE", repr(emitted))
        self.assertEqual(transport.bound, OP)
        self.assertEqual([request[:2] for request in transport.requests if request[0] == "POST"],
                         [("POST", PREFIX + "/connect")])
        self.assertTrue(all(set(request[2]) == {"timeout"} for request in transport.requests))
        self.assertTrue(all(0 < request[2]["timeout"] <= 1 for request in transport.requests))
        self.assertTrue(all("/qr" not in request[1] and "/auth/" not in request[1]
                            for request in transport.requests))

    def test_online_without_current_validation_is_not_reused(self):
        for value in ({"id": ACCOUNT, "authenticated": False, "connection": "online"},
                      {"id": ACCOUNT, "authenticated": True, "connection": "reconnecting"},
                      {"id": ACCOUNT, "authenticated": True, "connection": "disconnected"}):
            client, transport, _ = self.client([
                accounts(status()), response(value), response(operation("needs_login"), 202),
            ])
            self.assertEqual(client.run(30, lambda _: None).code, "needs_attention")
            self.assertEqual(transport.requests[-1][:2], ("POST", PREFIX + "/connect"))

    def test_no_saved_trust_and_needs_login_are_attention_without_reinitialization(self):
        for state in ("needs_login", "failed"):
            client, transport, _ = self.client([
                accounts(status()), response(status()),
                response(operation(state, reason="no_usable_saved_trust", private="PRIVATE"), 202),
            ])
            result = client.run(30, lambda _: None)
            self.assertEqual(result, resume.ResumeResult(False, "needs_attention"))
            self.assertEqual(len(transport.requests), 3)
            self.assertNotIn("PRIVATE", repr(result))

    def test_fixed_network_storage_and_auth_failure_classification(self):
        cases = [("profile_io_failed", "storage_failed"),
                 ("profile_cipher_unavailable", "storage_failed"),
                 ("account_database_password_mismatch", "storage_failed"),
                 ("auth_required", "authentication_failed"),
                 ("profile_network_routing_unavailable", "network_failed"),
                 ("operation_in_progress", "operation_in_progress"),
                 ("PRIVATE", "resume_failed")]
        for code, expected in cases:
            with self.subTest(code=code):
                client, _, _ = self.client([
                    accounts(status()), response(status()), response(operation("failed", code=code), 202),
                ])
                result = client.run(30, lambda _: None)
                self.assertEqual(result.code, expected)
                self.assertFalse(result.needs_initialization)

    def test_transport_timeout_never_means_missing_session(self):
        cases = [(TimeoutError("PRIVATE"), "transport_timeout"),
                 (ConnectionRefusedError("PRIVATE"), "transport_connection_failed"),
                 (http.client.BadStatusLine("PRIVATE"), "transport_protocol_failed"),
                 (RuntimeError("PRIVATE"), "transport_failed")]
        for error, expected in cases:
            client, _, _ = self.client([error])
            result = client.run(30, lambda _: None)
            self.assertEqual(result.code, expected)
            self.assertFalse(result.needs_initialization)
            self.assertNotIn("PRIVATE", repr(result))

    def test_http_error_codes_are_sanitized(self):
        for value, http_status, expected in [({"error": {"code": "forbidden"}}, 403, "authentication_failed"),
                                           ({"error": {"code": "profile_io_failed"}}, 503, "storage_failed"),
                                           ({"error": {"code": "PRIVATE"}}, 500, "resume_failed")]:
            client, _, _ = self.client([response(value, http_status)])
            self.assertEqual(client.run(30, lambda _: None).code, expected)

    def test_operation_identity_kind_uuid_and_state_cannot_change(self):
        for update in ({"id": OTHER_OP}, {"accountId": "other"}, {"kind": "send"},
                       {"id": "../qr"}, {"state": "PRIVATE"}, {"result": None}):
            value = operation("resuming")
            value.update(update)
            client, transport, _ = self.client([
                accounts(status()), response(status()), response(operation(), 202), response(value),
            ])
            self.assertEqual(client.run(30, lambda _: None).code, "invalid_response")
            self.assertEqual(len(transport.requests), 4)
            self.assertEqual(transport.requests[-1][:2], ("GET", OP_PATH))

    def test_success_receipt_requires_current_connection_confirmation(self):
        client, _, _ = self.client([
            accounts(status()), response(status()), response(operation("succeeded"), 202), response(status()),
        ])
        self.assertEqual(client.run(30, lambda _: None).code, "resume_failed")

    def test_callback_failure_does_not_cancel_or_disconnect_operation(self):
        client, transport, _ = self.client([
            accounts(status()), response(status()), response(operation("awaiting_approval"), 202),
        ])
        result = client.run(30, lambda _: (_ for _ in ()).throw(ValueError("PRIVATE")))
        self.assertEqual(result.code, "callback_failed")
        self.assertEqual(len(transport.requests), 3)

    def test_deadline_and_poll_count_are_bounded_without_cleanup_mutations(self):
        client, transport, clock = self.client([
            accounts(status()), response(status()), response(operation(), 202),
        ])
        self.assertEqual(client.run(0.25, lambda _: None).code, "deadline_exceeded")
        self.assertEqual(clock.now, 0.25)
        self.assertEqual(len(transport.requests), 3)
        client, transport, clock = self.client([
            accounts(status()), response(status()), response(operation(), 202),
            response(operation()), response(operation()),
        ])
        with patch.object(resume, "MAX_POLLS", 1):
            self.assertEqual(client.run(1000, lambda _: None).code, "deadline_exceeded")
        self.assertLessEqual(len(transport.requests), 5)

    def test_malformed_or_oversize_responses_are_rejected(self):
        cases = [resume.Response(200, {"content-type": "application/json"}, b'{"items":[],"items":[]}'),
                 resume.Response(200, {"content-type": "application/json"}, b'{"items":NaN}'),
                 resume.Response(200, {"content-type": "application/json"}, b"x" * (resume.JSON_LIMIT + 1)),
                 response({"items": [None]}), response({"items": "PRIVATE"}),
                 resume.Response(302, {}, b""),
                 resume.Response(200, {"content-type": "application/json", "content-encoding": "gzip"}, b"{}")]
        for item in cases:
            client, _, _ = self.client([item])
            self.assertFalse(client.run(30, lambda _: None).ok)

    def test_invalid_arguments_and_single_use(self):
        for account in ("../other", "", "x/qr", "http://host", None):
            with self.assertRaises(resume.ResumeError):
                resume.SessionResumeClient(account, "token", transport=Transport([]))
        for deadline in (None, True, float("nan"), float("inf")):
            client, transport, _ = self.client([])
            self.assertEqual(client.run(deadline, lambda _: None).code, "invalid_arguments")
            self.assertFalse(transport.requests)
        client, _, _ = self.client([accounts()])
        client.run(30, lambda _: None)
        self.assertEqual(client.run(30, lambda _: None).code, "client_already_used")


class ResumeTransportTests(unittest.TestCase):
    def transport(self, incoming):
        connection = Mock()
        connection.getresponse.return_value = incoming
        return resume.LoopbackHTTPTransport(resume.RequestPolicy(ACCOUNT), "secret-token"), connection

    def incoming(self, body=b"{}", headers=None, status=200):
        value = Mock()
        value.status = status
        value.getheaders.return_value = headers if headers is not None else [
            ("Content-Type", "application/json"), ("Content-Length", str(len(body))),
        ]
        value.read.return_value = body
        return value

    def test_fixed_loopback_no_body_key_proxy_or_redirect(self):
        transport, connection = self.transport(self.incoming())
        with patch.object(resume.http.client, "HTTPConnection", return_value=connection) as factory:
            transport.request("POST", PREFIX + "/connect", timeout=1)
            factory.assert_called_once_with("127.0.0.1", 26174, timeout=1)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("POST", PREFIX + "/connect"))
        self.assertEqual(kwargs, {"headers": {"Authorization": "Bearer secret-token",
                                              "Accept": "application/json", "Accept-Encoding": "identity"}})
        connection.close.assert_called_once()
        with self.assertRaisesRegex(resume.ResumeError, "request_not_allowed"):
            transport.request("POST", PREFIX + "/connect", timeout=1)

    def test_forbidden_routes_headers_bodies_fail_before_network(self):
        policy = resume.RequestPolicy(ACCOUNT)
        transport = resume.LoopbackHTTPTransport(policy, "token")
        routes = [("POST", "/v1/accounts"), ("POST", PREFIX + "/auth/login"),
                  ("POST", PREFIX + "/auth/disconnect"), ("DELETE", OP_PATH),
                  ("GET", OP_PATH), ("GET", OP_PATH + "/qr"),
                  ("POST", PREFIX + "/sends"), ("GET", "http://example.com")]
        with patch.object(resume.http.client, "HTTPConnection") as factory:
            for method, path in routes:
                with self.assertRaises(resume.ResumeError):
                    transport.request(method, path, timeout=1)
            for kwargs in ({"body": b"{}"}, {"headers": {"Idempotency-Key": "key"}}):
                with self.assertRaises(resume.ResumeError):
                    transport.request("POST", PREFIX + "/connect", timeout=1, **kwargs)
            factory.assert_not_called()
        policy.bind_operation(OP)
        policy.authorize("GET", OP_PATH)
        with self.assertRaises(resume.ResumeError):
            policy.bind_operation(OTHER_OP)

    def test_response_size_redirect_compression_and_duplicate_headers(self):
        cases = [(self.incoming(headers=[("Content-Length", "65537")]), "body_too_large"),
                 (self.incoming(body=b"x" * 65537, headers=[]), "body_too_large"),
                 (self.incoming(status=302), "redirect_rejected"),
                 (self.incoming(headers=[("Content-Encoding", "gzip")]), "content_encoding_rejected"),
                 (self.incoming(headers=[("Content-Type", "application/json"),
                                         ("content-type", "application/json")]), "invalid_response")]
        for incoming, code in cases:
            transport, connection = self.transport(incoming)
            with patch.object(resume.http.client, "HTTPConnection", return_value=connection):
                with self.assertRaisesRegex(resume.ResumeError, code):
                    transport.request("GET", "/v1/accounts", timeout=1)
            connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
