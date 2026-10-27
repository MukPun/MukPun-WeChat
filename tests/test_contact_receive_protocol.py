"""Offline contact receiver checks; no process or real network is used."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote


PATH = Path(__file__).resolve().parents[1] / "scripts" / "contact_receive_protocol.py"
SPEC = importlib.util.spec_from_file_location("contact_receive_protocol", PATH)
receiver = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = receiver
SPEC.loader.exec_module(receiver)

ACCOUNT = "contact_probe_123"
TARGET = "wxid_private_target"
MARKER = "AWTEST-0123abcd"
PREFIX = "/v1/accounts/" + ACCOUNT
CONTACTS = PREFIX + "/contacts?search=" + quote(receiver.TARGET_NAME, safe="") + "&limit=20"


def response(value, status=200, headers=None):
    return receiver.Response(status, headers or {"content-type": "application/json"},
                             json.dumps(value).encode("utf-8"))


def contact(identity=TARGET, name=receiver.TARGET_NAME):
    return {"id": identity, "username": identity, "displayName": name,
            "nickname": name, "remark": None, "alias": None, "relation": "unknown"}


def catalog(items=None, cursor=None, boundary="10"):
    return {"items": [contact()] if items is None else items, "nextCursor": cursor,
            "asOfEventCursor": boundary, "sourceGeneration": "generation",
            "coverage": {"partial": False, "databases": []}}


def event(cursor="11", identity=TARGET, origin="live", direction="incoming", message_id="message_1"):
    return {"cursor": cursor, "kind": "message.committed", "data": {
        "threadId": identity, "origin": origin, "direction": direction, "messageId": message_id}}


def events(items=None, cursor="11"):
    return {"items": [event()] if items is None else items, "nextCursor": cursor}


def message(**changes):
    result = {"id": "message_1", "accountId": ACCOUNT, "threadId": TARGET, "senderId": TARGET,
              "type": 1, "direction": "incoming", "body": MARKER}
    result.update(changes)
    return result


def observation(**changes):
    result = {"stage": "contact_observation", "event_count": 0,
              "target_incoming_count": 0, "target_live_count": 0,
              "text_checked": 0, "marker_matched": False}
    result.update(changes)
    return result


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
            raise AssertionError("unexpected request")
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ReceiveTests(unittest.TestCase):
    def client(self, replies):
        clock, output = Clock(), []
        transport = Transport(replies)
        client = receiver.ContactReceiveClient(ACCOUNT, "private-token", transport=transport,
                                               clock=clock, sleep=clock.sleep,
                                               marker_factory=lambda: MARKER)
        return client, transport, clock, output

    def assert_error(self, replies, code, deadline=8):
        client, transport, clock, output = self.client(replies)
        with self.assertRaises(receiver.ContactError) as caught:
            client.run(deadline, output.append)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(TARGET, str(caught.exception))
        return client, transport, clock, output

    def test_accepts_only_marker_and_outputs_no_ids_or_bodies(self):
        client, transport, _, output = self.client([
            response(catalog()), response(events()), response(message())])
        self.assertEqual(client.run(100, output.append), "ok")
        self.assertEqual(output, [{"stage": "contact_state", "state": "target_matched"},
                                  {"stage": "contact_waiting", "marker": MARKER, "seconds": 99},
                                  observation(event_count=1, target_incoming_count=1,
                                              target_live_count=1, text_checked=1,
                                              marker_matched=True)])
        self.assertEqual([r[:2] for r in transport.requests], [
            ("GET", CONTACTS), ("GET", PREFIX + "/events?after=10&limit=20"),
            ("GET", PREFIX + "/messages/message_1")])
        self.assertTrue(all(0 < r[2]["timeout"] <= receiver.READ_TIMEOUT_SECONDS for r in transport.requests))
        self.assertNotIn(TARGET, json.dumps(output))

    def test_pagination_is_canonical_and_finishes_before_announcing_match(self):
        cursor = "opaque+/=?&value"
        client, transport, _, output = self.client([
            response(catalog(cursor=cursor)), response(catalog(items=[])),
            response(events()), response(message())])
        self.assertEqual(client.run(100, output.append), "ok")
        self.assertEqual(transport.requests[1][1], CONTACTS + "&cursor=" + quote(cursor, safe=""))

    def test_duplicate_name_on_later_page_is_ambiguous(self):
        _, transport, _, output = self.assert_error([
            response(catalog(cursor="next")), response(catalog([contact("another_target")]))
        ], "target_ambiguous")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(output, [])

    def test_three_incomplete_pages_never_claim_unique(self):
        _, transport, _, output = self.assert_error([
            response(catalog(cursor="one")), response(catalog([], "two")),
            response(catalog([], "three"))], "catalog_incomplete")
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(output, [])

    def test_missing_name_does_not_sync_or_lookup(self):
        _, transport, _, output = self.assert_error([
            response(catalog([contact("other", "测试联系人的朋友")]))], "target_not_found")
        self.assertEqual([r[:2] for r in transport.requests], [("GET", CONTACTS)])
        self.assertEqual(output, [])

    def test_partial_or_unknown_coverage_never_claims_unique(self):
        for coverage in ({"partial": True}, {}, None, {"partial": 0}, {"partial": "false"}):
            with self.subTest(coverage=coverage):
                page = catalog()
                page["coverage"] = coverage
                _, transport, _, output = self.assert_error([response(page)], "catalog_incomplete")
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(output, [])
        page = catalog([])
        page["coverage"]["partial"] = True
        _, _, _, output = self.assert_error([
            response(catalog(cursor="next")), response(page)], "catalog_incomplete")
        self.assertEqual(output, [])

    def test_pending_profile_error_is_safe(self):
        self.assert_error([response({"error": {"code": "profile_keys_missing",
                                              "message": "private-data"}}, 409)],
                          "directory_unavailable")

    def test_changed_missing_numeric_and_repeated_cursors_are_rejected(self):
        for boundary in (None, 10, "-1", "01", "9223372036854775808"):
            with self.subTest(boundary=boundary):
                self.assert_error([response(catalog(boundary=boundary))], "invalid_cursor")
        self.assert_error([response(catalog(cursor="one")),
                           response(catalog([], boundary="11"))], "cursor_changed")
        self.assert_error([response(catalog(cursor="one")),
                           response(catalog([], "one"))], "invalid_cursor")

    def test_rejects_group_and_inconsistent_target_identity(self):
        self.assert_error([response(catalog([contact("room@chatroom")]))], "invalid_contacts")
        wrong = contact()
        wrong["username"] = "different"
        self.assert_error([response(catalog([wrong]))], "invalid_contacts")

    def test_unrelated_historical_outgoing_and_group_events_never_fetch_body(self):
        ignored = [event("11", identity="unrelated"), event("12", origin="catchup"),
                   event("13", direction="outgoing"), event("14", identity="room@chatroom")]
        _, transport, clock, output = self.assert_error([
            response(catalog()), response(events(ignored, "14")),
            response(events([], "14"))], "message_not_received", deadline=3)
        self.assertEqual(clock.now, 2)
        self.assertTrue(all("/messages/" not in r[1] for r in transport.requests))
        self.assertEqual(output[-1], observation(event_count=4, target_incoming_count=1))
        self.assertNotIn("unrelated", json.dumps(output))
        self.assertNotIn("room@chatroom", json.dumps(output))

    def test_marker_must_match_exactly_and_can_arrive_after_other_text(self):
        client, transport, _, output = self.client([
            response(catalog()), response(events()), response(message(body=MARKER + " ")),
            response(events([event("12", message_id="message_2")], "12")),
            response(message(id="message_2"))])
        self.assertEqual(client.run(100, output.append), "ok")
        self.assertEqual(len(transport.requests), 5)
        self.assertEqual(output[-1], observation(event_count=2, target_incoming_count=2,
                                               target_live_count=2, text_checked=2,
                                               marker_matched=True))

    def test_single_message_must_match_every_bound_identity(self):
        changes = [{"id": "different"}, {"accountId": "other"}, {"threadId": "other"},
                   {"senderId": "other"}, {"direction": "outgoing"}, {"type": 3}, {"type": True}]
        for change in changes:
            with self.subTest(change=change):
                _, _, _, output = self.assert_error([
                    response(catalog()), response(events()), response(message(**change))],
                    "invalid_message")
                self.assertEqual(output[-1], observation(event_count=1, target_incoming_count=1,
                                                        target_live_count=1))

    def test_invalid_event_order_and_cursor_cannot_skip_messages(self):
        for page in (events([], "11"), events([event("10")], "10"),
                     events([event("12"), event("11")], "11"), events(cursor=11)):
            with self.subTest(page=page):
                expected = "invalid_cursor" if page["nextCursor"] == 11 else "invalid_events"
                self.assert_error([response(catalog()), response(page)], expected)

    def test_expired_events_do_not_restart_from_zero(self):
        _, transport, _, _ = self.assert_error([
            response(catalog()), response({"error": {"code": "event_cursor_expired"}}, 410)],
            "event_cursor_expired")
        self.assertEqual(len(transport.requests), 2)

    def test_ten_minute_wait_cap_and_one_second_cleanup_reserve(self):
        self.assertEqual(receiver.MAX_WAIT_SECONDS, 600)
        for deadline, waits in ((900, 600), (601, 600), (4, 3)):
            with self.subTest(deadline=deadline):
                _, transport, clock, output = self.assert_error([
                    response(catalog()), *[response(events([], "10")) for _ in range(waits)]],
                    "message_not_received", deadline=deadline)
                self.assertEqual(clock.now, waits)
                self.assertEqual(len(transport.requests), waits + 1)
                self.assertEqual(output[1]["seconds"], waits)
                self.assertEqual(output[-1], observation())
                self.assertEqual(sum(item["stage"] == "contact_observation" for item in output), 1)

    def test_wait_uses_remaining_budget_after_catalog_and_fractional_sleep(self):
        client, transport, clock, output = self.client([
            response(catalog()), *[response(events([], "10")) for _ in range(3)]])
        original_request = transport.request

        def request(method, path, **options):
            reply = original_request(method, path, **options)
            if path == CONTACTS:
                clock.now += 4.25
            return reply

        transport.request = request
        with self.assertRaises(receiver.ContactError) as caught:
            client.run(8, output.append)
        self.assertEqual(caught.exception.code, "message_not_received")
        self.assertEqual(clock.now, 7)
        self.assertEqual(output[1]["seconds"], 3)
        self.assertEqual(transport.requests[-1][2]["timeout"], 0.75)
        self.assertEqual(output[-1], observation())

    def test_observation_filters_metadata_and_never_exposes_upstream_fields(self):
        ignored = [event("11", identity="private-other", message_id="private-message"),
                   event("12", origin="catchup"), event("13", origin="history"),
                   event("14", direction="outgoing"),
                   event("15", identity="private-room@chatroom")]
        for item in ignored:
            item["data"].update(body="private-body", token="private-token", raw="private-raw")
        _, transport, _, output = self.assert_error([
            response(catalog()), response(events(ignored, "15")),
            response(events([], "15"))], "message_not_received", deadline=3)
        self.assertEqual(output[-1], observation(event_count=5, target_incoming_count=2))
        self.assertTrue(all("/messages/" not in request[1] for request in transport.requests))
        self.assertEqual(set(output[-1]), set(observation()))
        for value in (TARGET, ACCOUNT, "private-other", "private-message", "private-body",
                      "private-token", "private-room@chatroom", "private-raw", "cursor", "origin"):
            self.assertNotIn(value, json.dumps(output))

    def test_observation_counters_are_capped_integers(self):
        replies = [response(catalog())]
        wrong_text = response(message(body="other text"))
        for page in range(1001):
            first = 11 + page * receiver.PAGE_LIMIT
            last = first + receiver.PAGE_LIMIT - 1
            replies.append(response(events([event(str(cursor)) for cursor in range(first, last + 1)],
                                           str(last))))
            replies.extend([wrong_text] * receiver.PAGE_LIMIT)
        replies[-1] = response(message())
        client, _, _, output = self.client(replies)
        client.sleep = lambda _seconds: None
        self.assertEqual(client.run(900, output.append), "ok")
        self.assertEqual(output[-1], observation(event_count=20000, target_incoming_count=20000,
                                               target_live_count=20000, text_checked=20000,
                                               marker_matched=True))
        for field in ("event_count", "target_incoming_count", "target_live_count", "text_checked"):
            self.assertIs(type(output[-1][field]), int)
        self.assertIs(type(output[-1]["marker_matched"]), bool)

    def test_success_counts_all_validated_page_metadata_without_extra_body_reads(self):
        page = [event("11"), event("12", origin="catchup"),
                event("13", identity="private-other"), event("14", message_id="unread-body")]
        client, transport, _, output = self.client([
            response(catalog()), response(events(page, "14")), response(message())])
        self.assertEqual(client.run(100, output.append), "ok")
        self.assertEqual(output[-1], observation(event_count=4, target_incoming_count=3,
                                               target_live_count=2, text_checked=1,
                                               marker_matched=True))
        self.assertEqual(len(transport.requests), 3)
        self.assertNotIn("private-other", json.dumps(output))
        self.assertNotIn("unread-body", json.dumps(output))

    def test_observation_callback_failure_does_not_replace_success_or_failure(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                replies = [response(catalog()), response(events()),
                           response(message()) if succeeds else RuntimeError("private-error")]
                client, _, _, output = self.client(replies)

                def emit(value):
                    output.append(value)
                    if value["stage"] == "contact_observation":
                        raise RuntimeError("private-callback-error")

                if succeeds:
                    self.assertEqual(client.run(100, emit), "ok")
                else:
                    with self.assertRaises(receiver.ContactError) as caught:
                        client.run(100, emit)
                    self.assertEqual(str(caught.exception), "transport_failed")
                self.assertEqual(sum(item["stage"] == "contact_observation" for item in output), 1)
                self.assertEqual(output[-1]["marker_matched"], succeeds)
                self.assertEqual(output[-1]["text_checked"], int(succeeds))

    def test_timeout_still_emits_observation_once_after_work_deadline(self):
        client, transport, clock, output = self.client([response(catalog()), response(events())])
        original_request = transport.request

        def request(method, path, **options):
            reply = original_request(method, path, **options)
            if "/events?" in path:
                clock.now = 7
            return reply

        transport.request = request
        with self.assertRaises(receiver.ContactError) as caught:
            client.run(8, output.append)
        self.assertEqual(str(caught.exception), "deadline_exceeded")
        self.assertEqual(output[-1], observation())
        self.assertEqual(sum(item["stage"] == "contact_observation" for item in output), 1)

    def test_observation_requires_bound_target_and_started_wait(self):
        for stage in ("contact_state", "contact_waiting"):
            with self.subTest(stage=stage):
                client, _, _, output = self.client([response(catalog())])

                def emit(value):
                    output.append(value)
                    if value["stage"] == stage:
                        raise RuntimeError("private-callback-error")

                with self.assertRaises(receiver.ContactError) as caught:
                    client.run(8, emit)
                self.assertEqual(str(caught.exception), "callback_failed")
                self.assertEqual(sum(item["stage"] == "contact_observation" for item in output),
                                 int(stage == "contact_waiting"))
        client, _, _, output = self.client([response(catalog())])
        client.marker_factory = lambda: "invalid-marker"
        with self.assertRaises(receiver.ContactError) as caught:
            client.run(8, output.append)
        self.assertEqual(str(caught.exception), "invalid_arguments")
        self.assertEqual(output, [{"stage": "contact_state", "state": "target_matched"}])

    def test_callback_errors_and_transport_errors_hide_private_details(self):
        client, _, _, _ = self.client([response(catalog())])
        with self.assertRaises(receiver.ContactError) as caught:
            client.run(10, Mock(side_effect=RuntimeError("private-body")))
        self.assertEqual(str(caught.exception), "callback_failed")
        self.assert_error([RuntimeError("private-token")], "transport_failed")

    def test_request_policy_cannot_send_sync_mark_read_or_read_other_ids(self):
        policy = receiver.RequestPolicy(ACCOUNT)
        for method, path in (("POST", CONTACTS), ("POST", PREFIX + "/sync"),
                             ("POST", PREFIX + "/sends"), ("GET", PREFIX + "/messages/other"),
                             ("GET", CONTACTS + "&extra=1"), ("GET", "/v1/accounts/other/contacts")):
            with self.subTest(method=method, path=path):
                with self.assertRaises(receiver.ContactError):
                    policy.authorize(method, path)
        policy.events("10")
        with self.assertRaises(receiver.ContactError):
            policy.authorize("GET", CONTACTS)
        with self.assertRaises(receiver.ContactError):
            policy.authorize("GET", PREFIX + "/events?after=0&limit=20")

    def test_safe_response_handling(self):
        for reply, code in ((response({}, 302), "redirect_rejected"),
                            (response({}, headers={"content-type": "text/html"}), "invalid_content_type"),
                            (response({}, headers={"content-type": "application/json",
                                                   "content-encoding": "gzip"}), "content_encoding_rejected"),
                            (receiver.Response(200, {"content-type": "application/json"},
                                               b"x" * (receiver.JSON_LIMIT + 1)), "body_too_large"),
                            (receiver.Response(200, {"content-type": "application/json"},
                                               b'{"items":[],"items":[]}'), "invalid_response")):
            with self.subTest(code=code):
                self.assert_error([reply], code)

    def test_native_transport_is_loopback_get_only_and_closes(self):
        policy = receiver.RequestPolicy(ACCOUNT)
        transport = receiver.LoopbackGETTransport(policy, "private-token", port=12345)
        incoming = Mock(status=200)
        incoming.getheaders.return_value = [("Content-Type", "application/json")]
        incoming.read.return_value = b"{}"
        connection = Mock()
        connection.getresponse.return_value = incoming
        with patch.object(receiver.http.client, "HTTPConnection", return_value=connection) as create:
            transport.request("GET", CONTACTS, timeout=0.5)
        create.assert_called_once_with("127.0.0.1", 12345, timeout=0.5)
        connection.request.assert_called_once_with("GET", CONTACTS, headers={
            "Authorization": "Bearer private-token", "Accept": "application/json",
            "Accept-Encoding": "identity"})
        incoming.read.assert_called_once_with(receiver.JSON_LIMIT + 1)
        connection.close.assert_called_once()

    def test_cold_read_budget_does_not_consume_cleanup_reserve(self):
        client, transport, _, output = self.client([response(catalog()), response(events()), response(message())])
        self.assertEqual(client.run(100, output.append), "ok")
        self.assertEqual([r[2]["timeout"] for r in transport.requests], [30, 1, 30])
        client, transport, _, output = self.client([response(catalog()), response(events()), response(message())])
        self.assertEqual(client.run(4, output.append), "ok")
        self.assertTrue(all(r[2]["timeout"] <= 3 for r in transport.requests))

    def test_transport_failure_kind_is_safe_and_connection_always_closes(self):
        failures = [(TimeoutError("private"), "transport_timeout"),
                    (ConnectionRefusedError("private"), "transport_connection_failed"),
                    (receiver.http.client.BadStatusLine("private"), "transport_protocol_failed"),
                    (UnicodeEncodeError("ascii", "私", 0, 1, "private"), "transport_encoding_failed"),
                    (OSError("private-response"), "transport_failed")]
        for error, code in failures:
            with self.subTest(code=code):
                connection = Mock()
                connection.getresponse.side_effect = error
                transport = receiver.LoopbackGETTransport(receiver.RequestPolicy(ACCOUNT), "private-token")
                with patch.object(receiver.http.client, "HTTPConnection", return_value=connection):
                    with self.assertRaises(receiver.ContactError) as caught:
                        transport.request("GET", CONTACTS, timeout=30)
                self.assertEqual(str(caught.exception), code)
                connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
