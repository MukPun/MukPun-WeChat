"""Offline protocol tests. No native process or network connection is made."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "qr_login_protocol.py"
SPEC = importlib.util.spec_from_file_location("qr_login_protocol", MODULE_PATH)
qr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qr
SPEC.loader.exec_module(qr)

ACCOUNT = "stage2_fresh_123"
OP = "a1234567-1234-4234-8234-123456789abc"
OTHER_OP = "b1234567-1234-4234-8234-123456789abc"
PREFIX = f"/v1/accounts/{ACCOUNT}"
OP_PATH = f"{PREFIX}/operations/{OP}"
PNG = qr.PNG_MAGIC + b"mock PNG payload"


def response(value, status=200):
    return qr.Response(status, {"content-type": "application/json"}, json.dumps(value).encode())


def operation(state="starting", **metadata):
    return {"id": OP, "accountId": ACCOUNT, "kind": "login", "state": state,
            "result": metadata, "createdAt": "", "updatedAt": ""}


def disconnect():
    return response({"state": "disconnected", "trustRetained": True})


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.bound = None

    def bind_operation(self, operation_id):
        self.bound = operation_id

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if not self.replies:
            raise AssertionError("unexpected request")
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class LoginProtocolTests(unittest.TestCase):
    def client(self, replies, **options):
        clock = FakeClock()
        transport = FakeTransport(replies)
        client = qr.LoginClient(ACCOUNT, transport, clock=clock, sleep=clock.sleep,
                                idempotency_key="fixed_attempt_key", **options)
        return client, transport, clock

    def test_success_disconnects_before_success_callback_and_qr_stays_in_memory(self):
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201), response(operation(), 202),
            response(operation("awaiting_scan", qrAvailable=True, qrGeneration=1)),
            qr.Response(200, {"content-type": "image/png"}, PNG),
            response(operation("awaiting_scan", qrAvailable=True, qrGeneration=1)),
            response(operation("succeeded")), disconnect(),
        ])
        images = []
        states = []

        def on_state(state):
            if state == "succeeded":
                self.assertEqual(transport.requests[-1][:2], ("POST", PREFIX + "/auth/disconnect"))
            states.append(state)

        result = client.run(images.append, on_state)
        self.assertEqual(result, qr.LoginResult())
        self.assertEqual(images, [qr.QrImage(1, PNG)])
        self.assertNotIn("mock PNG", repr(images[0]))
        self.assertEqual(states, ["starting", "awaiting_scan", "succeeded"])
        self.assertEqual(sum(path.endswith("/qr?generation=1") for _, path, _ in transport.requests), 1)
        login = transport.requests[1][2]
        self.assertEqual(json.loads(login["body"]), qr.LOGIN_BODY)
        self.assertNotIn("detach", json.loads(login["body"]))
        self.assertEqual(login["headers"], {"Idempotency-Key": "fixed_attempt_key"})
        self.assertIsNone(transport.requests[-1][2]["body"])
        self.assertTrue(all(0 < item[2]["timeout"] <= 1 for item in transport.requests))

    def test_all_terminal_failures_cancel_and_disconnect(self):
        for state in qr.FAILED_STATES:
            with self.subTest(state=state):
                client, transport, _ = self.client([
                    response({"id": ACCOUNT}, 201), response(operation(state), 202),
                    response(operation(state)), disconnect(),
                ])
                with self.assertRaises(qr.ProtocolError) as caught:
                    client.run(lambda image: self.fail("unexpected QR"))
                self.assertEqual(caught.exception.code, "login_" + state)
                self.assertFalse(caught.exception.cleanup_failed)
                self.assertEqual([item[:2] for item in transport.requests[-2:]],
                                 [("DELETE", OP_PATH), ("POST", PREFIX + "/auth/disconnect")])

    def test_qr_callback_exception_is_sanitized_and_cleans_up(self):
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201),
            response(operation("awaiting_scan", qrAvailable=True, qrGeneration=1), 202),
            qr.Response(200, {"content-type": "image/png"}, PNG),
            response(operation("cancelled")), disconnect(),
        ])

        def callback(_image):
            raise RuntimeError("secret callback payload")

        with self.assertRaises(qr.ProtocolError) as caught:
            client.run(callback)
        self.assertEqual(str(caught.exception), "callback_failed")
        self.assertNotIn("secret", repr(caught.exception))
        self.assertEqual([item[0] for item in transport.requests[-2:]], ["DELETE", "POST"])

    def test_failed_operation_preserves_only_known_error_codes(self):
        for detail, expected in (("profile_resource_limit", "profile_resource_limit"),
                                 ("profile_cipher_unavailable", "profile_cipher_unavailable"),
                                 ("private-account-and-token", "login_failed")):
            client, transport, _ = self.client([
                response({"id": ACCOUNT}, 201), response(operation("failed", code=detail), 202),
                response(operation("failed")), disconnect(),
            ])
            with self.assertRaises(qr.ProtocolError) as caught:
                client.run(lambda _: None)
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("private-account", str(caught.exception))
            self.assertEqual(transport.requests[-1][1], PREFIX + "/auth/disconnect")

    def test_state_callback_base_exception_also_cleans_up(self):
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201), response(operation(), 202),
            response(operation("cancelled")), disconnect(),
        ])
        with self.assertRaisesRegex(qr.ProtocolError, "callback_failed"):
            client.run(lambda image: None, lambda state: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(transport.requests[-1][1], PREFIX + "/auth/disconnect")

    def test_success_callback_exception_happens_after_disconnect(self):
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201), response(operation("succeeded"), 202), disconnect(),
        ])
        with self.assertRaisesRegex(qr.ProtocolError, "callback_failed"):
            client.run(lambda image: None, lambda state: (_ for _ in ()).throw(ValueError("secret")))
        self.assertEqual(len(transport.requests), 3)

    def test_explicit_keep_connected_hands_off_only_after_success_callbacks(self):
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201), response(operation("succeeded"), 202),
        ])
        callbacks = []
        result = client.run(lambda _: self.fail("no QR"), callbacks.append,
                            on_authenticated=lambda _: callbacks.append("authenticated"),
                            keep_connected=True)
        self.assertEqual(result, qr.LoginResult(disconnected=False))
        self.assertEqual(callbacks, ["authenticated", "succeeded"])
        self.assertEqual(len(transport.requests), 2)
        self.assertTrue(json.loads(transport.requests[1][2]["body"])["forceQr"])

    def test_keep_connected_callback_failure_still_disconnects(self):
        for callback_name in ("on_state", "on_authenticated"):
            client, transport, _ = self.client([
                response({"id": ACCOUNT}, 201), response(operation("succeeded"), 202), disconnect(),
            ])
            callback = lambda _: (_ for _ in ()).throw(ValueError("PRIVATE"))
            with self.assertRaisesRegex(qr.ProtocolError, "callback_failed"):
                client.run(lambda _: None, keep_connected=True, **{callback_name: callback})
            self.assertEqual(transport.requests[-1][:2], ("POST", PREFIX + "/auth/disconnect"))

    def test_keep_connected_timeout_still_cancels_and_disconnects(self):
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201), response(operation(), 202),
            response(operation("cancelled")), disconnect(),
        ], total_timeout=3, poll_interval=5)
        with self.assertRaisesRegex(qr.ProtocolError, "deadline_exceeded"):
            client.run(lambda _: None, keep_connected=True)
        self.assertEqual([item[:2] for item in transport.requests[-2:]],
                         [("DELETE", OP_PATH), ("POST", PREFIX + "/auth/disconnect")])

    def test_keep_connected_success_callback_timeout_preserves_disconnect_budget(self):
        client, transport, clock = self.client([
            response({"id": ACCOUNT}, 201), response(operation("succeeded"), 202), disconnect(),
        ], total_timeout=3)
        with self.assertRaisesRegex(qr.ProtocolError, "deadline_exceeded") as caught:
            client.run(lambda _: None, lambda _: clock.sleep(1.1), keep_connected=True)
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertEqual(transport.requests[-1][:2], ("POST", PREFIX + "/auth/disconnect"))

    def test_keep_connected_requires_an_actual_boolean_before_requests(self):
        for value in (None, 0, 1, "true", [], {}):
            client, transport, _ = self.client([])
            with self.assertRaisesRegex(qr.ProtocolError, "invalid_mode"):
                client.run(lambda _: None, keep_connected=value)
            self.assertFalse(transport.requests)

    def test_authenticated_callback_receives_only_sanitized_profile_before_disconnect(self):
        cases = [
            ({"state": "created", "reason": "PRIVATE", "accountId": "PRIVATE"}, "created", ""),
            ({"state": "existing", "reason": "login_database_password_required"}, "existing", ""),
            ({"state": "pending", "reason": "login_database_password_required"},
             "pending", "login_database_password_required"),
            ({"state": "pending", "reason": "profile_format_key_required"},
             "pending", "profile_format_key_required"),
            ({"state": "pending", "reason": "PRIVATE"}, "pending", ""),
            ({"state": "PRIVATE", "reason": "profile_format_key_required"}, "unavailable", ""),
            ({"state": []}, "unavailable", ""), (None, "unavailable", ""),
        ]
        for profile, state, reason in cases:
            with self.subTest(profile=profile):
                client, transport, _ = self.client([
                    response({"id": ACCOUNT}, 201),
                    response(operation("succeeded", profile=profile, private="PRIVATE"), 202), disconnect(),
                ])
                callbacks = []
                def authenticated(value):
                    self.assertEqual(len(transport.requests), 2)
                    self.assertEqual(vars(value), {"state": state, "reason": reason,
                                                   "deadline": client.total_timeout - 2.0})
                    self.assertNotIn("PRIVATE", repr(value))
                    callbacks.append("authenticated")
                def on_state(value):
                    self.assertEqual(transport.requests[-1][:2],
                                     ("POST", PREFIX + "/auth/disconnect"))
                    callbacks.append(value)
                self.assertEqual(client.run(lambda _: None, on_state,
                                            on_authenticated=authenticated), qr.LoginResult())
                self.assertEqual(callbacks, ["authenticated", "succeeded"])

    def test_authenticated_callback_exceptions_are_sanitized_and_disconnect(self):
        for error in (RuntimeError("PRIVATE"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                client, transport, _ = self.client([
                    response({"id": ACCOUNT}, 201), response(operation("succeeded"), 202), disconnect(),
                ])
                states = []
                def authenticated(_profile):
                    raise error
                with self.assertRaisesRegex(qr.ProtocolError, "callback_failed") as caught:
                    client.run(lambda _: None, states.append, on_authenticated=authenticated)
                self.assertFalse(caught.exception.cleanup_failed)
                self.assertNotIn("PRIVATE", repr(caught.exception))
                self.assertEqual(states, [])
                self.assertEqual([request[:2] for request in transport.requests[-1:]],
                                 [("POST", PREFIX + "/auth/disconnect")])
                self.assertEqual(len(transport.requests), 3)

    def test_authenticated_callback_work_deadline_preserves_disconnect_budget(self):
        client, transport, clock = self.client([
            response({"id": ACCOUNT}, 201), response(operation("succeeded"), 202), disconnect(),
        ], total_timeout=3)
        def authenticated(profile):
            self.assertEqual(profile.deadline, 1.0)
            clock.sleep(1.1)
        with self.assertRaisesRegex(qr.ProtocolError, "deadline_exceeded") as caught:
            client.run(lambda _: None, on_authenticated=authenticated)
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertEqual(transport.requests[-1][:2], ("POST", PREFIX + "/auth/disconnect"))
        self.assertEqual(len(transport.requests), 3)

    def test_invalid_authenticated_callback_is_rejected_before_login(self):
        client, transport, _ = self.client([])
        with self.assertRaisesRegex(qr.ProtocolError, "invalid_callback"):
            client.run(lambda _: None, on_authenticated=True)
        self.assertEqual(transport.requests, [])

    def test_deadline_reserves_time_for_cancel_and_disconnect(self):
        client, transport, clock = self.client([
            response({"id": ACCOUNT}, 201), response(operation(), 202),
            response(operation("cancelled")), disconnect(),
        ], total_timeout=3, poll_interval=1)
        with self.assertRaisesRegex(qr.ProtocolError, "deadline_exceeded"):
            client.run(lambda image: None)
        self.assertLessEqual(clock.now, 3)
        self.assertEqual([item[0] for item in transport.requests[-2:]], ["DELETE", "POST"])

    def test_extended_deadline_reserves_two_bounded_cleanup_calls(self):
        for total_timeout in (330, 900):
            with self.subTest(total_timeout=total_timeout):
                client, transport, clock = self.client([
                    response({"id": ACCOUNT}, 201), response(operation(), 202),
                    response(operation("cancelled")), disconnect(),
                ], total_timeout=total_timeout, extended_window=True)
                original_request = transport.request

                def bounded_cleanup(method, path, **kwargs):
                    if method == "DELETE" or path.endswith("/auth/disconnect"):
                        self.assertEqual(kwargs["timeout"], 1.0)
                        clock.sleep(1.0)
                    return original_request(method, path, **kwargs)

                transport.request = bounded_cleanup
                with self.assertRaisesRegex(qr.ProtocolError, "deadline_exceeded") as caught:
                    client.run(lambda _: None, lambda _: clock.sleep(total_timeout - 2))
                self.assertFalse(caught.exception.cleanup_failed)
                self.assertEqual(clock.now, total_timeout)
                self.assertEqual([item[:2] for item in transport.requests], [
                    ("POST", "/v1/accounts"), ("POST", PREFIX + "/auth/login"),
                    ("DELETE", OP_PATH), ("POST", PREFIX + "/auth/disconnect"),
                ])
                body = json.loads(transport.requests[1][2]["body"])
                self.assertEqual(body, {**qr.LOGIN_BODY, "timeoutSeconds": 300})

    def test_extended_authenticated_callback_has_same_cleanup_reservation(self):
        client, transport, clock = self.client([
            response({"id": ACCOUNT}, 201), response(operation("succeeded"), 202),
            disconnect(),
        ], total_timeout=330, extended_window=True)

        def authenticated(profile):
            self.assertEqual(profile.deadline, 328)
            clock.sleep(328)

        with self.assertRaisesRegex(qr.ProtocolError, "deadline_exceeded") as caught:
            client.run(lambda _: None, on_authenticated=authenticated)
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertEqual(transport.requests[-1][:2], ("POST", PREFIX + "/auth/disconnect"))
        self.assertFalse(any(item[0] == "DELETE" for item in transport.requests))

    def test_window_timeout_limits_and_default_are_explicit(self):
        self.assertEqual(self.client([])[0].total_timeout, 150)
        self.assertEqual(self.client([], extended_window=True)[0].total_timeout, 150)
        self.client([], total_timeout=180)
        self.client([], total_timeout=330, extended_window=True)
        self.client([], total_timeout=900, extended_window=True)
        for extended_window, total_timeout in ((False, 180.1), (False, 330),
                                               (True, 900.1), (True, float("inf")),
                                               (True, float("nan")), (True, 2)):
            with self.subTest(extended_window=extended_window, total_timeout=total_timeout):
                with self.assertRaisesRegex(qr.ProtocolError, "invalid_timeout"):
                    self.client([], extended_window=extended_window, total_timeout=total_timeout)

    def test_client_and_transport_window_policies_must_match(self):
        with patch.object(qr.http.client, "HTTPConnection") as factory:
            for transport_window in (False, True):
                transport = qr.LoopbackHTTPTransport(ACCOUNT, "token",
                                                     extended_window=transport_window)
                client = qr.LoginClient(ACCOUNT, transport, extended_window=transport_window)
                self.assertEqual(client.policy.extended_window, transport_window)
                with self.assertRaisesRegex(qr.ProtocolError, "mode_mismatch"):
                    qr.LoginClient(ACCOUNT, transport, extended_window=not transport_window)
        factory.assert_not_called()

    def test_window_flag_requires_an_actual_boolean_in_all_constructors(self):
        for value in (None, 0, 1, "true", [], {}):
            for constructor in (
                lambda: qr.RequestPolicy(ACCOUNT, extended_window=value),
                lambda: qr.LoopbackHTTPTransport(ACCOUNT, "token", extended_window=value),
                lambda: qr.LoginClient(ACCOUNT, FakeTransport([]), extended_window=value),
            ):
                with self.subTest(value=value), self.assertRaisesRegex(qr.ProtocolError, "invalid_mode"):
                    constructor()

    def test_callback_consuming_work_budget_still_cleans_up(self):
        client, transport, clock = self.client([
            response({"id": ACCOUNT}, 201), response(operation(), 202),
            response(operation("cancelled")), disconnect(),
        ], total_timeout=3)
        with self.assertRaisesRegex(qr.ProtocolError, "deadline_exceeded"):
            client.run(lambda image: None, lambda state: clock.sleep(1.1))
        self.assertEqual(len(transport.requests), 4)

    def test_unknown_operation_state_and_changed_identity_fail_closed(self):
        for update in ({"state": "connected"}, {"id": OTHER_OP}, {"accountId": "other"}, {"kind": "sync"}):
            with self.subTest(update=update):
                altered = operation()
                altered.update(update)
                client, transport, _ = self.client([
                    response({"id": ACCOUNT}, 201), response(operation(), 202),
                    response(altered), response(operation("cancelled")), disconnect(),
                ])
                with self.assertRaises(qr.ProtocolError):
                    client.run(lambda image: None)
                self.assertEqual(transport.requests[-1][1], PREFIX + "/auth/disconnect")

    def test_invalid_login_operation_does_not_bind_or_poll(self):
        invalid = operation()
        invalid["id"] = "../../messages"
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201), response(invalid, 202), disconnect(),
        ])
        with self.assertRaisesRegex(qr.ProtocolError, "invalid_operation"):
            client.run(lambda image: None)
        self.assertIsNone(transport.bound)
        self.assertEqual(len(transport.requests), 3)

    def test_changed_created_account_never_starts_login(self):
        client, transport, _ = self.client([response({"id": "other"}, 201)])
        with self.assertRaisesRegex(qr.ProtocolError, "account_changed"):
            client.run(lambda image: None)
        self.assertEqual(len(transport.requests), 1)

    def test_login_timeout_uncertainty_disconnects_without_retry(self):
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201), TimeoutError("secret"), disconnect(),
        ])
        with self.assertRaisesRegex(qr.ProtocolError, "transport_failed"):
            client.run(lambda image: None)
        self.assertEqual([item[1] for item in transport.requests],
                         ["/v1/accounts", PREFIX + "/auth/login", PREFIX + "/auth/disconnect"])

    def test_png_magic_content_type_and_size_are_required(self):
        images = [qr.Response(200, {"content-type": "text/html"}, PNG),
                  qr.Response(200, {"content-type": "image/png"}, b"not png"),
                  qr.Response(200, {"content-type": "image/png"}, PNG + b"x" * qr.PNG_LIMIT)]
        for image in images:
            with self.subTest(image_size=len(image.body)):
                client, _, _ = self.client([
                    response({"id": ACCOUNT}, 201),
                    response(operation("awaiting_scan", qrAvailable=True, qrGeneration=1), 202),
                    image, response(operation("cancelled")), disconnect(),
                ])
                with self.assertRaises(qr.ProtocolError):
                    client.run(lambda image: self.fail("unsafe image delivered"))

    def test_boolean_zero_and_large_qr_generation_rejected(self):
        for generation in (True, 0, -1, "1", 2147483648):
            with self.subTest(generation=generation):
                client, transport, _ = self.client([
                    response({"id": ACCOUNT}, 201),
                    response(operation("awaiting_scan", qrAvailable=True, qrGeneration=generation), 202),
                    response(operation("cancelled")), disconnect(),
                ])
                with self.assertRaisesRegex(qr.ProtocolError, "invalid_qr_generation"):
                    client.run(lambda image: self.fail("unexpected QR"))
                self.assertFalse(any("/qr?" in item[1] for item in transport.requests))

    def test_error_exposes_only_safe_code(self):
        for code, expected in (("unauthorized", "unauthorized"),
                               ("tokenlike_secret_abc123", "request_failed"),
                               ("secret/token?x=1", "request_failed")):
            client, _, _ = self.client([response({"error": {"code": code, "message": "private content"}}, 403)])
            with self.assertRaises(qr.ProtocolError) as caught:
                client.run(lambda image: None)
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(caught.exception.status, 403)
            self.assertNotIn("private", str(caught.exception))

    def test_qr_only_cancels_disconnects_without_further_poll(self):
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201),
            response(operation("awaiting_scan", qrAvailable=True, qrGeneration=1), 202),
            qr.Response(200, {"content-type": "image/png"}, PNG),
            response(operation("cancelled")), disconnect(),
        ])
        images = []
        with patch("builtins.open", side_effect=AssertionError("no file I/O")):
            result = client.run(images.append, qr_only=True)
        self.assertEqual(result, qr.LoginResult(state="qr_available"))
        self.assertEqual(images, [qr.QrImage(1, PNG)])
        self.assertEqual([item[:2] for item in transport.requests], [
            ("POST", "/v1/accounts"), ("POST", PREFIX + "/auth/login"),
            ("GET", OP_PATH + "/qr?generation=1"), ("DELETE", OP_PATH),
            ("POST", PREFIX + "/auth/disconnect"),
        ])

    def test_qr_only_requires_confirmed_cancel_and_disconnect(self):
        for cancel_state, shutdown in (("awaiting_scan", disconnect()),
                                       ("cancelled", response({"state": "connected"}))):
            with self.subTest(cancel_state=cancel_state):
                client, transport, _ = self.client([
                    response({"id": ACCOUNT}, 201),
                    response(operation("awaiting_scan", qrAvailable=True, qrGeneration=1), 202),
                    qr.Response(200, {"content-type": "image/png"}, PNG),
                    response(operation(cancel_state)), shutdown,
                ])
                with self.assertRaises(qr.ProtocolError) as caught:
                    client.run(lambda image: None, qr_only=True)
                self.assertTrue(caught.exception.cleanup_failed)
                self.assertEqual(caught.exception.code, "cleanup_failed")
                self.assertEqual(transport.requests[-1][1], PREFIX + "/auth/disconnect")

    def test_qr_only_success_race_still_disconnects_immediately(self):
        client, transport, _ = self.client([
            response({"id": ACCOUNT}, 201), response(operation("succeeded"), 202), disconnect(),
        ])
        result = client.run(lambda image: self.fail("no QR"), qr_only=True)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(transport.requests[-1][1], PREFIX + "/auth/disconnect")

    def test_null_operation_result_is_valid_wire_value(self):
        starting = operation()
        starting["result"] = None
        resuming = operation("resuming")
        resuming["result"] = None
        succeeded = operation("succeeded")
        succeeded["result"] = None
        client, _, _ = self.client([
            response({"id": ACCOUNT}, 201), response(starting, 202),
            response(resuming), response(succeeded), disconnect(),
        ])
        self.assertEqual(client.run(lambda image: None), qr.LoginResult())

    def test_duplicate_json_keys_and_oversize_json_rejected(self):
        for body in (b'{"id":"a","id":"b"}', b" " * (qr.JSON_LIMIT + 1)):
            client, _, _ = self.client([qr.Response(201, {"content-type": "application/json"}, body)])
            with self.assertRaises(qr.ProtocolError):
                client.run(lambda image: None)

    def test_cleanup_failure_cannot_be_success(self):
        client, _, _ = self.client([
            response({"id": ACCOUNT}, 201), response(operation("succeeded"), 202),
            response({"state": "connected", "trustRetained": True}), TimeoutError(),
        ])
        with self.assertRaises(qr.ProtocolError) as caught:
            client.run(lambda image: None)
        self.assertEqual(caught.exception.code, "disconnect_unconfirmed")
        self.assertTrue(caught.exception.cleanup_failed)

    def test_single_use_client_and_strict_identifiers(self):
        client, _, _ = self.client([response({"error": {"code": "conflict"}}, 409)])
        with self.assertRaises(qr.ProtocolError):
            client.run(lambda image: None)
        with self.assertRaisesRegex(qr.ProtocolError, "client_already_used"):
            client.run(lambda image: None)
        for account in ("../x", "a/b", "a%2fb", "a?x", "a\n", "", "a" * 65):
            with self.assertRaises(qr.ProtocolError):
                qr.RequestPolicy(account)


class PolicyTests(unittest.TestCase):
    def test_whitelist_rejects_chat_read_write_sync_and_other_accounts(self):
        forbidden = [
            ("GET", "/v1/accounts"), ("GET", PREFIX), ("PATCH", PREFIX),
            ("GET", PREFIX + "/messages"), ("GET", PREFIX + "/threads"),
            ("GET", PREFIX + "/events"), ("POST", PREFIX + "/sync"),
            ("POST", PREFIX + "/sends"), ("POST", PREFIX + "/connect"),
            ("GET", OP_PATH + "/qr"), ("GET", OP_PATH + "/qr?generation=1&refresh=true"),
            ("GET", OP_PATH.replace(OP, OTHER_OP)), ("GET", "http://evil/"),
            ("GET", OP_PATH.replace(ACCOUNT, "other")), ("GET", OP_PATH + "/../messages"),
        ]
        for extended_window in (False, True):
            policy = qr.RequestPolicy(ACCOUNT, extended_window=extended_window)
            policy.bind_operation(OP)
            for method, path in forbidden:
                with self.subTest(extended_window=extended_window, method=method, path=path):
                    with self.assertRaises(qr.ProtocolError):
                        policy.authorize(method, path, None, {})

    def test_each_window_accepts_only_its_own_fixed_native_timeout(self):
        for extended_window, expected in ((False, 120), (True, 300)):
            policy = qr.RequestPolicy(ACCOUNT, extended_window=extended_window)
            for timeout in (False, 0, 119, 120, 120.0, 121, 180, 299, 300, 300.0, 301, 330):
                body = json.dumps({**qr.LOGIN_BODY, "timeoutSeconds": timeout}).encode()
                with self.subTest(extended_window=extended_window, timeout=timeout):
                    if type(timeout) is int and timeout == expected:
                        self.assertEqual(policy.authorize("POST", PREFIX + "/auth/login", body,
                                                         {"Idempotency-Key": "fixed"}), qr.JSON_LIMIT)
                    else:
                        with self.assertRaisesRegex(qr.ProtocolError, "request_not_allowed"):
                            policy.authorize("POST", PREFIX + "/auth/login", body,
                                             {"Idempotency-Key": "fixed"})

    def test_fixed_snapshots_ignore_exported_body_mutation_and_keep_schema(self):
        original = json.loads(json.dumps(qr.LOGIN_BODY))
        with patch.dict(qr.LOGIN_BODY, {"timeoutSeconds": 999,
                                       "qrRefresh": {"mode": "auto", "maxRefreshes": 99}}):
            for extended_window, expected in ((False, 120), (True, 300)):
                policy = qr.RequestPolicy(ACCOUNT, extended_window=extended_window)
                self.assertIsInstance(policy.login_body, bytes)
                self.assertEqual(json.loads(policy.login_body), {**original, "timeoutSeconds": expected})
                self.assertEqual(policy.authorize("POST", PREFIX + "/auth/login", policy.login_body,
                                                 {"Idempotency-Key": "fixed"}), qr.JSON_LIMIT)
                with self.assertRaisesRegex(qr.ProtocolError, "request_not_allowed"):
                    policy.authorize("POST", PREFIX + "/auth/login", json.dumps(qr.LOGIN_BODY).encode(),
                                     {"Idempotency-Key": "fixed"})
                altered = json.loads(policy.login_body)
                altered["detach"] = True
                with self.assertRaisesRegex(qr.ProtocolError, "request_not_allowed"):
                    policy.authorize("POST", PREFIX + "/auth/login", json.dumps(altered).encode(),
                                     {"Idempotency-Key": "fixed"})

    def test_create_and_login_body_and_idempotency_are_bound(self):
        policy = qr.RequestPolicy(ACCOUNT)
        with self.assertRaises(qr.ProtocolError):
            policy.authorize("POST", "/v1/accounts", b'{"id":"other"}', {})
        body = json.dumps(qr.LOGIN_BODY).encode()
        self.assertEqual(policy.authorize("POST", PREFIX + "/auth/login", body,
                                         {"Idempotency-Key": "first"}), qr.JSON_LIMIT)
        with self.assertRaises(qr.ProtocolError):
            policy.authorize("POST", PREFIX + "/auth/login", body, {"Idempotency-Key": "second"})
        with self.assertRaises(qr.ProtocolError):
            policy.authorize("POST", PREFIX + "/auth/login", b'{"detach":true}', {"Idempotency-Key": "first"})
        wrong_type = json.loads(body)
        wrong_type["qrRefresh"]["maxRefreshes"] = False
        with self.assertRaises(qr.ProtocolError):
            policy.authorize("POST", PREFIX + "/auth/login", json.dumps(wrong_type).encode(),
                             {"Idempotency-Key": "first"})
        with self.assertRaises(qr.ProtocolError):
            policy.bind_operation("not-an-op")
        policy.bind_operation(OP)
        with self.assertRaises(qr.ProtocolError):
            policy.bind_operation(OTHER_OP)


class RawHTTPTransportTests(unittest.TestCase):
    def raw(self, status=201, data=b'{"id":"stage2_fresh_123"}', length=None):
        reply = Mock()
        reply.status = status
        reply.read.return_value = data
        values = {"Content-Type": "application/json", "Content-Length": length}
        reply.getheader.side_effect = lambda key, default=None: values.get(key, default)
        connection = Mock()
        connection.getresponse.return_value = reply
        return connection, reply

    def test_raw_loopback_ignores_proxy_env_and_never_follows_redirect(self):
        connection, reply = self.raw(302, b"", "0")
        with patch.dict("os.environ", {"HTTP_PROXY": "http://must-not-connect.invalid:9999"}), \
             patch.object(qr.http.client, "HTTPConnection", return_value=connection) as factory:
            transport = qr.LoopbackHTTPTransport(ACCOUNT, "in-memory-secret")
            with self.assertRaisesRegex(qr.ProtocolError, "redirect_rejected"):
                transport.request("POST", "/v1/accounts", body=json.dumps({"id": ACCOUNT}).encode(),
                                  headers={}, max_bytes=qr.JSON_LIMIT, timeout=0.5)
        factory.assert_called_once_with("127.0.0.1", 26174, timeout=0.5)
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()
        self.assertNotIn("in-memory-secret", repr(transport))

    def test_raw_size_limit_even_without_content_length(self):
        connection, reply = self.raw(data=b"x" * (qr.JSON_LIMIT + 1))
        with patch.object(qr.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(qr.ProtocolError, "body_too_large"):
                qr.LoopbackHTTPTransport(ACCOUNT, "token").request(
                    "POST", "/v1/accounts", body=json.dumps({"id": ACCOUNT}).encode(),
                    headers={}, max_bytes=qr.JSON_LIMIT, timeout=1,
                )
        reply.read.assert_called_once_with(qr.JSON_LIMIT + 1)
        connection.close.assert_called_once()

    def test_content_length_over_limit_rejected_before_read(self):
        connection, reply = self.raw(length=str(qr.JSON_LIMIT + 1))
        with patch.object(qr.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(qr.ProtocolError, "body_too_large"):
                qr.LoopbackHTTPTransport(ACCOUNT, "token").request(
                    "POST", "/v1/accounts", body=json.dumps({"id": ACCOUNT}).encode(),
                    headers={}, max_bytes=qr.JSON_LIMIT, timeout=1,
                )
        reply.read.assert_not_called()
        connection.close.assert_called_once()

    def test_unapproved_route_rejected_before_connection_creation(self):
        with patch.object(qr.http.client, "HTTPConnection") as factory:
            transport = qr.LoopbackHTTPTransport(ACCOUNT, "token")
            with self.assertRaisesRegex(qr.ProtocolError, "request_not_allowed"):
                transport.request("POST", PREFIX + "/sends", body=b"{}", headers={},
                                  max_bytes=qr.JSON_LIMIT, timeout=1)
        factory.assert_not_called()

    def test_transport_enforces_selected_fixed_window_before_connecting(self):
        for extended_window, expected, rejected in ((False, 120, 300), (True, 300, 120)):
            connection, _ = self.raw(status=202)
            with self.subTest(extended_window=extended_window), \
                 patch.object(qr.http.client, "HTTPConnection", return_value=connection) as factory:
                transport = qr.LoopbackHTTPTransport(ACCOUNT, "token", extended_window=extended_window)
                for timeout in (rejected, 240):
                    with self.assertRaisesRegex(qr.ProtocolError, "request_not_allowed"):
                        transport.request("POST", PREFIX + "/auth/login",
                                          body=json.dumps({**qr.LOGIN_BODY, "timeoutSeconds": timeout}).encode(),
                                          headers={"Idempotency-Key": "fixed"},
                                          max_bytes=qr.JSON_LIMIT, timeout=1)
                factory.assert_not_called()
                transport.request("POST", PREFIX + "/auth/login", body=transport.policy.login_body,
                                  headers={"Idempotency-Key": "fixed"}, max_bytes=qr.JSON_LIMIT, timeout=1)
                self.assertEqual(json.loads(connection.request.call_args.kwargs["body"])["timeoutSeconds"],
                                 expected)
                factory.assert_called_once_with("127.0.0.1", 26174, timeout=1)


if __name__ == "__main__":
    unittest.main()
