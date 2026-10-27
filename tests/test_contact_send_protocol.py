"""Offline send contract checks: no server, credentials or real submissions."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote


PATH = Path(__file__).resolve().parents[1] / "scripts" / "contact_send_protocol.py"
SPEC = importlib.util.spec_from_file_location("contact_send_protocol", PATH)
sender = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sender
SPEC.loader.exec_module(sender)

ACCOUNT = "private_account"
TOKEN = "private-token-never-output"
TARGET = "wxid_private_target"
SEND_ID = "36400924-7844-48a3-987d-d063a9f6bc56"
OTHER_ID = "87d2db41-42b8-4640-b8b6-9303a510aa93"
CLIENT_ID = "4294967295"
SERVER_ID = "18446744073709551615"
PREFIX = "/v1/accounts/" + ACCOUNT
CONTACTS = PREFIX + "/contacts?search=" + quote(sender.TARGET_NAME, safe="") + "&limit=20"
POST_PATH = PREFIX + "/sends"
POLL_PATH = POST_PATH + "/" + SEND_ID


def response(value, status=200, headers=None):
    return sender.Response(status, headers or {"content-type": "application/json"},
                           json.dumps(value).encode("utf-8"))


def intent(state="queued", **changes):
    value = {"id": SEND_ID, "accountId": ACCOUNT, "destination": TARGET, "state": state,
             "clientId": CLIENT_ID, "serverId": SERVER_ID if state == "accepted" else None,
             "errorCode": None, "createdAt": "2026-10-25T00:00:00Z"}
    value.update(changes)
    return value


def contact(identity=TARGET, name=sender.TARGET_NAME):
    return {"id": identity, "username": identity, "displayName": name}


def catalog(items=None, cursor=None):
    return {"items": [contact()] if items is None else items, "nextCursor": cursor,
             "asOfEventCursor": "10", "coverage": {"partial": False}}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Transport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def request(self, method, path, **options):
        self.requests.append((method, path, options))
        if not self.replies:
            raise AssertionError("unexpected-request-with-private-data")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class SendTests(unittest.TestCase):
    def client(self, replies, *, lookup=None):
        clock, output = Clock(), []
        transport = Transport(replies)
        client = sender.ContactSendClient(ACCOUNT, TOKEN, transport=transport, lookup=lookup,
                                          clock=clock, sleep=clock.sleep)
        return client, transport, clock, output

    def assert_unknown(self, replies, *, deadline=100):
        client, transport, clock, output = self.client(replies)
        result = client.run(deadline, output.append)
        self.assertEqual(result, sender.SendResult(False, "outcome_unknown", True, False))
        self.assertEqual(sum(method == "POST" for method, _, _ in transport.requests), 1)
        return client, transport, clock, output

    def test_default_path_only_contacts_one_fixed_post_and_bound_poll(self):
        client, transport, _, output = self.client([
            response(catalog()), response(intent(), 202), response(intent("submitting")),
            response(intent("accepted"))])
        with patch.object(sender.receive.ContactReceiveClient, "run", side_effect=AssertionError("must-not-receive")):
            result = client.run(100, output.append)
        self.assertEqual(result, sender.SendResult(True, "ok", True, True))
        self.assertEqual([request[:2] for request in transport.requests], [
            ("GET", CONTACTS), ("POST", POST_PATH), ("GET", POLL_PATH), ("GET", POLL_PATH)])
        post = transport.requests[1][2]
        self.assertEqual(set(post), {"timeout", "headers", "body"})
        self.assertEqual(json.loads(post["body"]), {"threadId": TARGET, "text": sender.TEST_TEXT})
        self.assertEqual(post["body"], json.dumps({"threadId": TARGET, "text": sender.TEST_TEXT},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(set(post["headers"]), {"Content-Type", "Idempotency-Key"})
        key = post["headers"]["Idempotency-Key"]
        self.assertRegex(key, r"^[0-9a-f]{32}$")
        self.assertEqual(sender.uuid.UUID(hex=key).version, 4)
        self.assertEqual(post["headers"]["Content-Type"], "application/json")
        self.assertEqual([record["state"] for record in output],
                         ["target_matched", "submitting", "queued", "submitting", "accepted"])
        self.assertTrue(all(set(record) == {"stage", "state"} and record["stage"] == "send_state"
                            for record in output))

    def test_duplicate_contact_on_later_page_never_posts(self):
        client, transport, _, output = self.client([
            response(catalog(cursor="next")), response(catalog([contact("wxid_second")]))])
        self.assertEqual(client.run(100, output.append),
                         sender.SendResult(False, "target_ambiguous", False, False))
        self.assertEqual(len(transport.requests), 2)
        self.assertTrue(all(method == "GET" for method, _, _ in transport.requests))
        self.assertEqual(output, [])

    def test_lookup_unavailable_absent_partial_group_and_cursor_fail_before_post(self):
        cases = [(response(catalog([])), "target_not_found"),
                 (response(catalog([contact("room@chatroom")])), "invalid_contacts"),
                 (response({"error": {"code": "profile_unavailable", "message": TOKEN}}, 409),
                  "directory_unavailable")]
        partial = catalog()
        partial["coverage"]["partial"] = True
        cases.append((response(partial), "catalog_incomplete"))
        for reply, code in cases:
            with self.subTest(code=code):
                client, transport, _, output = self.client([reply])
                self.assertEqual(client.run(100, output.append), sender.SendResult(False, code, False, False))
                self.assertEqual([request[:2] for request in transport.requests], [("GET", CONTACTS)])
                self.assertEqual(output, [])

    def test_every_post_transport_failure_is_unknown_and_never_polled_or_retried(self):
        failures = [TimeoutError(TOKEN), ConnectionRefusedError(TARGET), RuntimeError(SEND_ID),
                    sender.SendError("transport_timeout"), sender.receive.ContactError("transport_failed")]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                _, transport, _, _ = self.assert_unknown([response(catalog()), error])
                self.assertEqual([request[:2] for request in transport.requests],
                                 [("GET", CONTACTS), ("POST", POST_PATH)])

    def test_every_unusable_post_response_is_unknown_and_no_poll_id_is_bound(self):
        invalid = [response(intent(), 200), response(intent(), 302), response(intent(), 400),
                   response({"items": [intent()]}, 202), response({}, 202),
                   response(intent(id="not-a-uuid"), 202), response(intent(state="unknown"), 202),
                   response(intent(destination="somebody_else"), 202),
                   response(intent(), 202, {"content-type": "text/html"}),
                   response(intent(), 202, {"content-type": "application/json", "content-encoding": "gzip"}),
                   sender.Response(202, {"content-type": "application/json"}, b'{"id":1,"id":2}'),
                   sender.Response(202, {"content-type": "application/json"}, b"x" * (sender.JSON_LIMIT + 1))]
        for reply in invalid:
            with self.subTest(reply=repr(reply)):
                client, transport, _, _ = self.assert_unknown([response(catalog()), reply])
                self.assertIsNone(client._policy._poll_path)
                self.assertEqual(len(transport.requests), 2)

    def test_poll_transport_failure_or_bad_status_never_reposts(self):
        for reply in (TimeoutError(TOKEN), RuntimeError(TARGET), response(intent(), 404),
                      response(intent(), 202), response(intent(state="future_state"))):
            with self.subTest(reply=type(reply).__name__):
                _, transport, _, _ = self.assert_unknown([response(catalog()), response(intent(), 202), reply])
                self.assertEqual([request[:2] for request in transport.requests],
                                 [("GET", CONTACTS), ("POST", POST_PATH), ("GET", POLL_PATH)])

    def test_polling_caps_at_45_seconds_or_remaining_deadline(self):
        for deadline, expected in ((100, 45), (3.25, 3.25)):
            with self.subTest(deadline=deadline):
                _, transport, clock, output = self.assert_unknown([
                    response(catalog()), response(intent(), 202), *[response(intent())] * 50], deadline=deadline)
                self.assertEqual(clock.now, expected)
                self.assertEqual(len(transport.requests), 2 + int(sender.math.ceil(expected)))
                self.assertTrue(all(0 < request[2]["timeout"] <= 30 for request in transport.requests))
                self.assertEqual(output[-1], {"stage": "send_state", "state": "queued"})
                self.assertLessEqual(transport.requests[-1][2]["timeout"], expected % 1 or 1)

    def test_lookup_and_post_time_reduce_total_budget(self):
        client, transport, clock, output = self.client([
            response(catalog()), response(intent(), 202), *[response(intent())] * 10])
        original = transport.request

        def request(method, path, **options):
            reply = original(method, path, **options)
            if path == CONTACTS:
                clock.now += 4
            elif method == "POST":
                clock.now += 2
            return reply

        transport.request = request
        self.assertEqual(client.run(8, output.append).code, "outcome_unknown")
        self.assertEqual(clock.now, 8)
        self.assertEqual([request[2]["timeout"] for request in transport.requests], [8, 4, 2, 1])

    def test_deadline_during_post_leaves_unknown_without_poll(self):
        client, transport, clock, output = self.client([response(catalog()), response(intent("accepted"), 202)])
        original = transport.request

        def request(method, path, **options):
            result = original(method, path, **options)
            if method == "POST":
                clock.now = 10
            return result

        transport.request = request
        self.assertEqual(client.run(10, output.append), sender.SendResult(False, "outcome_unknown", True, False))
        self.assertEqual(len(transport.requests), 2)

    def test_accepted_initial_response_is_sufficient_without_extra_api(self):
        client, transport, _, output = self.client([response(catalog()), response(intent("accepted"), 202)])
        self.assertEqual(client.run(100, output.append), sender.SendResult(True, "ok", True, True))
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(output[-1]["state"], "accepted")

    def test_accepted_requires_all_bound_identities_and_exact_unsigned_ids(self):
        changes = [{"id": OTHER_ID}, {"id": "36400924-7844-18a3-987d-d063a9f6bc56"},
                   {"accountId": "other_account"}, {"destination": "other_target"},
                   {"destination": {"threadId": TARGET}}, {"clientId": "1"}, {"clientId": None},
                   {"clientId": ""}, {"clientId": "0"}, {"clientId": 1}, {"clientId": True},
                   {"clientId": "4294967296"}, {"clientId": "01"},
                   {"serverId": None}, {"serverId": ""}, {"serverId": "0"},
                   {"serverId": "01"}, {"serverId": 1}, {"serverId": True},
                   {"serverId": "18446744073709551616"}, {"serverId": "-1"},
                   {"extra": TOKEN}]
        for change in changes:
            with self.subTest(change=list(change)):
                _, transport, _, output = self.assert_unknown([
                    response(catalog()), response(intent(), 202), response(intent("accepted", **change))])
                self.assertEqual(len(transport.requests), 3)
                self.assertNotIn("accepted", [record["state"] for record in output])

    def test_client_id_can_bind_later_but_cannot_change_or_disappear(self):
        client, _, _, output = self.client([
            response(catalog()), response(intent(clientId=None), 202),
            response(intent("submitting", clientId="")), response(intent("accepted"))])
        self.assertTrue(client.run(100, output.append).server_confirmed)
        for changed in ("1", None, ""):
            with self.subTest(changed=changed):
                self.assert_unknown([response(catalog()), response(intent(clientId=None), 202),
                                     response(intent("submitting")), response(intent("accepted", clientId=changed))])

    def test_all_terminal_failures_are_fixed_and_stop_immediately(self):
        for state, code in sender.TERMINAL_CODES.items():
            for initial in (True, False):
                with self.subTest(state=state, initial=initial):
                    replies = [response(catalog()), response(intent(state if initial else "queued"), 202)]
                    if not initial:
                        replies.append(response(intent(state, errorCode=TOKEN)))
                    client, transport, _, output = self.client(replies)
                    self.assertEqual(client.run(100, output.append), sender.SendResult(False, code, True, False))
                    self.assertEqual(len(transport.requests), 2 if initial else 3)
                    self.assertEqual(output[-1], {"stage": "send_state", "state": state})

    def test_reusing_client_never_repeats_any_request(self):
        client, transport, _, output = self.client([response(catalog()), response(intent("accepted"), 202)])
        self.assertTrue(client.run(100, output.append).ok)
        self.assertEqual(client.run(100, output.append), sender.SendResult(False, "client_already_used", True, True))
        self.assertEqual(len(transport.requests), 2)

    def test_callback_failure_before_submission_prevents_post(self):
        for fail_state in ("target_matched", "submitting"):
            client, transport, _, output = self.client([response(catalog())])

            def emit(record):
                output.append(record)
                if record["state"] == fail_state:
                    raise RuntimeError(TOKEN)

            self.assertEqual(client.run(100, emit), sender.SendResult(False, "callback_failed", False, False))
            self.assertEqual(len(transport.requests), 1)

    def test_callback_failure_after_submission_preserves_uncertainty_or_confirmed_terminal(self):
        for state in ("queued", "accepted", "failed"):
            client, transport, _, _ = self.client([response(catalog()), response(intent(state), 202)])

            def emit(record):
                if record["state"] == state:
                    raise RuntimeError(TARGET)

            result = client.run(100, emit)
            expected = {"queued": "outcome_unknown", "accepted": "ok", "failed": "send_failed"}[state]
            self.assertEqual(result.code, expected)
            self.assertEqual(result.server_confirmed, state == "accepted")
            self.assertEqual(len(transport.requests), 2)

    def test_invalid_args_and_expired_deadline_never_lookup(self):
        for deadline, callback in ((True, [].append), (float("nan"), [].append),
                                   (0, [].append), (10, None)):
            with self.subTest(deadline=deadline):
                lookup = Mock(side_effect=AssertionError("must-not-lookup"))
                client, transport, _, _ = self.client([], lookup=lookup)
                result = client.run(deadline, callback)
                self.assertFalse(result.submitted)
                lookup.assert_not_called()
                self.assertEqual(transport.requests, [])
        for kwargs in ({"account": "../../other", "token": TOKEN}, {"account": ACCOUNT, "token": "bad\n"},
                       {"account": ACCOUNT, "token": TOKEN, "port": True},
                       {"account": ACCOUNT, "token": TOKEN, "lookup": object()}):
            with self.subTest(argument=list(kwargs)):
                with self.assertRaises(sender.SendError) as caught:
                    sender.ContactSendClient(**kwargs)
                self.assertEqual(str(caught.exception), "invalid_arguments")

    def test_policy_denies_unbound_polls_changed_payload_headers_and_extra_apis(self):
        policy = sender.RequestPolicy(ACCOUNT)
        with self.assertRaises(sender.SendError):
            policy.authorize("GET", POLL_PATH)
        policy.bind(TARGET, "0" * 32)
        headers, body = dict(policy._headers), policy._body
        policy.authorize("POST", POST_PATH, headers=headers, body=body)
        forbidden = [("POST", POST_PATH, headers, b'{}'),
                     ("POST", POST_PATH, {**headers, "X-Extra": "value"}, body),
                     ("POST", POST_PATH, {**headers, "Idempotency-Key": "1" * 32}, body),
                     ("POST", POST_PATH, headers, body + b" "),
                     ("POST", PREFIX + "/sync", headers, body),
                     ("POST", PREFIX + "/messages/read", headers, body),
                     ("GET", POST_PATH + "/by-idempotency-key?key=secret", None, None),
                     ("GET", CONTACTS, None, None),
                     ("GET", PREFIX + "/events?after=0&limit=20", None, None)]
        for method, path, request_headers, request_body in forbidden:
            with self.subTest(method=method, path=path):
                with self.assertRaises(sender.SendError):
                    policy.authorize(method, path, headers=request_headers, body=request_body)
        with self.assertRaises(sender.SendError):
            policy.bind_send(SEND_ID)
        policy.start_submission()
        policy.bind_send(SEND_ID)
        policy.authorize("GET", POLL_PATH)
        for path in (POST_PATH + "/" + OTHER_ID, POLL_PATH + "?extra=1"):
            with self.assertRaises(sender.SendError):
                policy.authorize("GET", path)
        with self.assertRaises(sender.SendError):
            policy.start_submission()

    def test_native_transport_is_loopback_exact_body_headers_and_single_post(self):
        policy = sender.RequestPolicy(ACCOUNT)
        policy.bind(TARGET, "0" * 32)
        transport = sender.LoopbackSendTransport(policy, TOKEN, port=12345)
        incoming = Mock(status=202)
        incoming.getheaders.return_value = [("Content-Type", "application/json")]
        incoming.read.return_value = b"{}"
        connection = Mock()
        connection.getresponse.return_value = incoming
        with patch.object(sender.http.client, "HTTPConnection", return_value=connection) as create:
            transport.request("POST", POST_PATH, headers=policy._headers, body=policy._body, timeout=0.5)
            with self.assertRaises(sender.SendError):
                transport.request("POST", POST_PATH, headers=policy._headers, body=policy._body, timeout=0.5)
        create.assert_called_once_with("127.0.0.1", 12345, timeout=0.5)
        connection.request.assert_called_once_with("POST", POST_PATH, body=policy._body, headers={
            "Authorization": "Bearer " + TOKEN, "Accept": "application/json", "Accept-Encoding": "identity",
            "Content-Type": "application/json", "Idempotency-Key": "0" * 32})
        incoming.read.assert_called_once_with(sender.JSON_LIMIT + 1)
        connection.close.assert_called_once()

    def test_native_transport_rejects_redirect_encoding_size_duplicate_and_length(self):
        cases = [(302, [("Content-Type", "application/json")], b"{}", "redirect_rejected"),
                 (202, [("Content-Encoding", "gzip")], b"{}", "content_encoding_rejected"),
                 (202, [("Content-Length", "65537")], b"{}", "body_too_large"),
                 (202, [("Content-Length", "bad")], b"{}", "body_too_large"),
                 (202, [("Content-Type", "application/json"), ("content-type", "text/html")],
                  b"{}", "invalid_response"),
                 (202, [], b"x" * (sender.JSON_LIMIT + 1), "body_too_large"),
                 (202, [("Content-Length", "10")], b"{}", "invalid_response")]
        for status, headers, body, code in cases:
            with self.subTest(code=code):
                policy = sender.RequestPolicy(ACCOUNT)
                policy.bind(TARGET, "0" * 32)
                transport = sender.LoopbackSendTransport(policy, TOKEN)
                incoming = Mock(status=status)
                incoming.getheaders.return_value = headers
                incoming.read.return_value = body
                connection = Mock()
                connection.getresponse.return_value = incoming
                with patch.object(sender.http.client, "HTTPConnection", return_value=connection):
                    with self.assertRaises(sender.SendError) as caught:
                        transport.request("POST", POST_PATH, headers=policy._headers, body=policy._body, timeout=1)
                self.assertEqual(str(caught.exception), code)
                connection.close.assert_called_once()

    def test_private_values_never_appear_in_records_results_errors_or_object_repr(self):
        client, transport, _, output = self.client([
            response(catalog()), response(intent(), 202), response(intent("accepted"))])
        result = client.run(100, output.append)
        safe = json.dumps(output) + repr(result) + repr(client) + repr(client._policy) + repr(client._transport)
        safe += repr(response(intent("accepted")))
        for bad in (ACCOUNT, TOKEN, TARGET, SEND_ID, CLIENT_ID, SERVER_ID, sender.TEST_TEXT):
            self.assertNotIn(bad, safe)
        key = transport.requests[1][2]["headers"]["Idempotency-Key"]
        self.assertNotIn(key, safe)
        error = sender.SendError(TOKEN)
        self.assertEqual(str(error), "invalid_response")
        self.assertNotIn(TOKEN, repr(error))
        _, _, _, failed_output = self.assert_unknown([response(catalog()), RuntimeError(TOKEN)])
        self.assertNotIn(TOKEN, json.dumps(failed_output))


if __name__ == "__main__":
    unittest.main()
