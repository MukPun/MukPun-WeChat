"""Synthetic GET-only group receipt; no process or real network is used."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

SOURCE = Path(__file__).resolve().parents[1] / "scripts/group_receive_protocol.py"
SPEC = importlib.util.spec_from_file_location("group_receive_tested", SOURCE)
group = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = group
SPEC.loader.exec_module(group)
ACCOUNT, NAME = "wechat-primary", "测试群"
ROOM, MEMBER = "123456789@chatroom", "wxid_test_member"
MARKER = "AWTEST-0123abcd"
BIND_MARKER = "AWGROUP-0123456789abcdef"
PREFIX = "/v1/accounts/" + ACCOUNT
GROUPS = PREFIX + "/groups?search=" + quote(NAME, safe="") + "&limit=20"


def response(value, status=200):
    return group.Response(status, {"content-type": "application/json"}, json.dumps(value).encode())


def room(identity=ROOM, name=NAME, **extra):
    return {"id": identity, "roomId": identity, "displayName": name,
            "ownerId": "not-exported-owner", "announcement": "not-exported-announcement", **extra}


def catalog(items=None, cursor=None, boundary="10", **extra):
    return {"items": [room()] if items is None else items, "nextCursor": cursor,
            "asOfEventCursor": boundary, "coverage": {"partial": False}, **extra}


def event(cursor="11", target=ROOM, origin="live", direction="incoming", message_id="message_1"):
    return {"cursor": cursor, "kind": "message.committed", "data": {
        "threadId": target, "origin": origin, "direction": direction, "messageId": message_id}}


def events(items=None, cursor="11"):
    return {"items": [event()] if items is None else items, "nextCursor": cursor}


def message(**changes):
    return {"id": "message_1", "accountId": ACCOUNT, "threadId": ROOM, "senderId": MEMBER,
            "type": 1, "direction": "incoming", "body": MARKER, **changes}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Transport:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def request(self, method, path, **options):
        self.requests.append((method, path, options))
        if not self.replies:
            raise AssertionError("unexpected request")
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class GroupTests(unittest.TestCase):
    def client(self, replies, **options):
        clock, transport, output = Clock(), Transport(replies), []
        client = group.GroupReceiveClient(ACCOUNT, "test-only-token", NAME, transport=transport,
                    clock=clock, sleep=clock.sleep, marker_factory=lambda: MARKER, **options)
        return client, transport, clock, output

    def test_list_uses_no_search_and_preserves_chinese_names_without_ids(self):
        other = "999@chatroom"
        client, transport, _, output = self.client([response(catalog([room(), room(other, other)]))])
        self.assertEqual(client.list_groups(100, output.append), "ok")
        self.assertEqual([entry[:2] for entry in transport.requests], [("GET", PREFIX + "/groups?limit=20")])
        self.assertEqual(output, [
            {"stage": "group_catalog_item", "index": 1, "name": NAME},
            {"stage": "group_catalog_item", "index": 2, "name": ""},
            {"stage": "group_catalog_summary", "count": 2, "named_count": 1, "complete": True}])
        self.assertNotIn(ROOM, str(output))
        self.assertNotIn(other, str(output))
        self.assertNotIn("not-exported-owner", str(output))

    def test_empty_unfiltered_catalog_is_reported_as_zero_not_missing_name(self):
        client, transport, _, output = self.client([response(catalog([]))])
        self.assertEqual(client.list_groups(100, output.append), "ok")
        self.assertEqual(output, [{"stage": "group_catalog_summary", "count": 0,
                                   "named_count": 0, "complete": True}])

    def error(self, replies, code, *, identify=False, deadline=8):
        client, transport, clock, output = self.client(replies)
        with self.assertRaises(group.GroupError) as caught:
            (client.identify if identify else client.run)(deadline, output.append)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(ROOM, str(caught.exception))
        self.assertNotIn(MEMBER, str(caught.exception))
        return client, transport, clock, output

    def test_receive_group_member_marker_uses_only_three_gets(self):
        client, transport, _, output = self.client([response(catalog()), response(events()), response(message())])
        self.assertEqual(client.run(100, output.append), "ok")
        self.assertEqual([r[:2] for r in transport.requests], [
            ("GET", GROUPS), ("GET", PREFIX + "/events?after=10&limit=20"),
            ("GET", PREFIX + "/messages/message_1")])
        self.assertEqual(output[:2], [{"stage": "group_state", "state": "target_matched"},
                                     {"stage": "group_waiting", "marker": MARKER, "seconds": 99}])
        self.assertEqual(output[-1], {"stage": "group_observation", "event_count": 1,
            "target_incoming_count": 1, "target_live_count": 1, "text_checked": 1, "marker_matched": True})

    def test_callback_never_receives_group_member_or_message_data(self):
        client, _, _, output = self.client([response(catalog()), response(events()), response(message())])
        client.run(100, output.append)
        serialized = json.dumps(output)
        for secret in (ROOM, MEMBER, NAME, "not-exported-owner", "not-exported-announcement", "message_1"):
            self.assertNotIn(secret, serialized)

    def test_response_and_errors_are_shared_with_base_transport(self):
        self.assertIs(group.Response, group.contact.Response)
        self.assertIs(group.GroupError, group.contact.ContactError)

    def test_identify_is_catalog_only_and_does_not_generate_marker(self):
        client, transport, _, output = self.client([response(catalog())])
        marker = client.marker_factory = Mock(side_effect=AssertionError("not for discovery"))
        self.assertEqual(client.identify(100, output.append), "ok")
        marker.assert_not_called()
        self.assertEqual([r[:2] for r in transport.requests], [("GET", GROUPS)])
        self.assertEqual(output, [{"stage": "group_state", "state": "target_matched"}])

    def test_client_cannot_reuse_identify_then_receive(self):
        client, _, _, output = self.client([response(catalog())])
        client.identify(100, output.append)
        with self.assertRaises(group.GroupError) as caught:
            client.run(100, output.append)
        self.assertEqual(caught.exception.code, "client_already_used")

    def test_client_cannot_reuse_receive_then_identify(self):
        client, _, _, output = self.client([response(catalog()), response(events()), response(message())])
        client.run(100, output.append)
        with self.assertRaises(group.GroupError) as caught:
            client.identify(100, output.append)
        self.assertEqual(caught.exception.code, "client_already_used")

    def test_group_name_is_exact_not_native_substring_match(self):
        self.error([response(catalog([room(name=NAME + "的朋友")]))], "target_not_found", identify=True)

    def test_identify_checks_all_pages_and_escapes_cursor(self):
        cursor = "opaque+/=?&name"
        client, transport, _, output = self.client([
            response(catalog(cursor=cursor)), response(catalog(items=[]))])
        self.assertEqual(client.identify(100, output.append), "ok")
        self.assertEqual(transport.requests[1][1], GROUPS + "&cursor=" + quote(cursor, safe=""))
        self.assertEqual(len(output), 1)

    def test_duplicate_exact_group_names_fail_on_later_page(self):
        _, transport, _, output = self.error([
            response(catalog(cursor="next")), response(catalog([room("other@chatroom")]))],
            "target_ambiguous", identify=True)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(output, [])

    def test_tombstone_is_not_a_matching_group(self):
        self.error([response(catalog([{"id": ROOM, "deleted": True, "version": "2"}]))],
                   "target_not_found", identify=True)

    def test_missing_target_does_not_fall_back_to_lookup_or_sync(self):
        _, transport, _, output = self.error([response(catalog([]))], "target_not_found", identify=True)
        self.assertEqual([r[:2] for r in transport.requests], [("GET", GROUPS)])
        self.assertEqual(output, [])

    def test_group_ids_must_consistently_identify_a_chatroom(self):
        for item in (room("wxid_private"), room(roomId="other@chatroom"), room(id=None),
                     room(displayName=3), room("\n@chatroom")):
            with self.subTest(item=item):
                self.error([response(catalog([item]))], "invalid_response", identify=True)

    def test_catalog_shapes_and_missing_cursor_fail(self):
        values = [catalog(items=[None]), catalog(items="bad"), catalog(items=[room()] * 21)]
        missing = catalog()
        del missing["nextCursor"]
        values.append(missing)
        for value in values:
            with self.subTest(value=value):
                self.error([response(value)], "invalid_response", identify=True)

    def test_partial_or_unknown_catalog_never_claims_group(self):
        for coverage in ({"partial": True}, {}, None, {"partial": 0}, {"partial": "false"}):
            with self.subTest(coverage=coverage):
                self.error([response(catalog(coverage=coverage))], "catalog_incomplete", identify=True)

    def test_catalog_boundary_cannot_change_during_paging(self):
        self.error([response(catalog(cursor="next")), response(catalog([], boundary="11"))],
                   "cursor_changed", identify=True)

    def test_cursor_values_and_paging_loops_are_bounded(self):
        for boundary in (None, 10, "-1", "01", "9223372036854775808"):
            with self.subTest(boundary=boundary):
                self.error([response(catalog(boundary=boundary))], "invalid_cursor", identify=True)
        self.error([response(catalog(cursor="same")), response(catalog([], "same"))],
                   "invalid_cursor", identify=True)
        self.error([response(catalog(cursor="one")), response(catalog([], "two")),
                    response(catalog([], "three"))], "catalog_incomplete", identify=True)

    def test_group_member_is_required_instead_of_room_as_sender(self):
        for sender in (ROOM, "other@chatroom", None, "", 3, "member\n", "a" * 513):
            with self.subTest(sender=sender):
                self.error([response(catalog()), response(events()), response(message(senderId=sender))],
                           "invalid_message")

    def test_message_stays_bound_to_account_group_id_and_type(self):
        changes = ({"id": "other"}, {"accountId": "other"}, {"threadId": "other@chatroom"},
                   {"direction": "outgoing"}, {"type": 3}, {"type": True}, {"body": None})
        for change in changes:
            with self.subTest(change=change):
                self.error([response(catalog()), response(events()), response(message(**change))], "invalid_message")

    def test_private_other_group_outgoing_and_catchup_events_fetch_no_text(self):
        ignored = [event("11", target=MEMBER), event("12", target="other@chatroom"),
                   event("13", origin="catchup"), event("14", direction="outgoing")]
        _, transport, _, output = self.error([response(catalog()), response(events(ignored, "14")),
                                              response(events([], "14"))], "message_not_received", deadline=3)
        self.assertTrue(all("/messages/" not in request[1] for request in transport.requests))
        self.assertFalse(output[-1]["marker_matched"])

    def test_marker_is_exact_and_already_normalized_by_native(self):
        client, _, _, output = self.client([
            response(catalog()), response(events()), response(message(body=MEMBER + ":\n" + MARKER)),
            response(events([event("12", message_id="message_2")], "12")),
            response(message(id="message_2"))])
        self.assertEqual(client.run(100, output.append), "ok")
        self.assertEqual(output[-1]["text_checked"], 2)

    def test_group_name_bounds_are_utf8_bytes_and_controls(self):
        for name in (None, 3, "", "name\n", "测" * 86, "a" * 257, "\ud800"):
            with self.subTest(name=name), self.assertRaises(group.GroupError) as caught:
                group.GroupReceiveClient(ACCOUNT, "token", name)
            self.assertEqual(caught.exception.code, "invalid_arguments")
        self.assertTrue(group.RequestPolicy(ACCOUNT, "测" * 85).catalog_path.startswith(PREFIX + "/groups?"))

    def test_policy_allows_only_exact_current_get_path(self):
        policy = group.RequestPolicy(ACCOUNT, NAME)
        policy.authorize("GET", GROUPS)
        for method, path in (("POST", GROUPS), ("GET", PREFIX + "/groups"),
                             ("GET", PREFIX + "/contacts"), ("GET", PREFIX + "/send"),
                             ("GET", PREFIX + "/groups/x/lookup"), ("GET", PREFIX + "/groups/x/members")):
            with self.subTest(method=method, path=path), self.assertRaises(group.GroupError):
                policy.authorize(method, path)

    def test_profile_and_transport_errors_never_login(self):
        _, transport, _, _ = self.error([response({"error": {"code": "profile_keys_missing"}}, 409)],
                                       "directory_unavailable", identify=True)
        self.assertEqual(len(transport.requests), 1)

    def test_identification_deadline_and_callbacks_are_bounded(self):
        client, _, _, _ = self.client([response(catalog())])
        with self.assertRaises(group.GroupError) as caught:
            client.identify(100, lambda _: (_ for _ in ()).throw(ValueError("sensitive")))
        self.assertEqual(caught.exception.code, "callback_failed")
        for deadline in (float("inf"), float("nan"), "bad"):
            client, transport, _, _ = self.client([])
            with self.assertRaises(group.GroupError) as caught:
                client.identify(deadline, lambda _: None)
            self.assertEqual(caught.exception.code, "invalid_arguments")
            self.assertEqual(transport.requests, [])

    def test_bind_empty_catalog_returns_room_only_on_private_return_channel(self):
        client, transport, _, output = self.client([
            response({"asOfEventCursor": "10"}), response(events()), response(message(body=BIND_MARKER))])
        result = client.bind(100, output.append, marker_factory=lambda: BIND_MARKER)
        self.assertEqual(result, ROOM)
        self.assertEqual(output[:2], [{"stage": "group_bind_state", "state": "awaiting_marker"},
                                     {"stage": "group_waiting", "marker": BIND_MARKER, "seconds": 99}])
        self.assertEqual(output[-1], {"stage": "group_binding_observation", "event_count": 1,
            "candidate_group_events": 1, "text_checked": 1, "marker_matched": True})
        self.assertNotIn(ROOM, json.dumps(output))
        self.assertNotIn(MEMBER, json.dumps(output))
        self.assertEqual([request[:2] for request in transport.requests], [
            ("GET", GROUPS), ("GET", PREFIX + "/events?after=10&limit=20"),
            ("GET", PREFIX + "/messages/message_1")])
        with self.assertRaises(group.GroupError) as caught:
            client.identify(100, output.append)
        self.assertEqual(caught.exception.code, "client_already_used")

    def test_bind_ignores_private_catchup_outgoing_and_nontext(self):
        ignored = [event("11", target=MEMBER), event("12", origin="catchup"),
                   event("13", direction="outgoing"), event("14", target="@chatroom"),
                   event("15", message_id="nontext")]
        client, transport, _, output = self.client([
            response({"asOfEventCursor": "10"}), response(events(ignored, "15")),
            response(message(id="nontext", type=3, senderId=None, body=None)),
            response(events([event("16", message_id="text")], "16")),
            response(message(id="text", body=BIND_MARKER))])
        self.assertEqual(client.bind(100, output.append, marker_factory=lambda: BIND_MARKER), ROOM)
        self.assertEqual(sum("/messages/" in request[1] for request in transport.requests), 2)
        self.assertEqual(output[-1]["candidate_group_events"], 2)
        self.assertEqual(output[-1]["text_checked"], 1)

    def test_bind_duplicate_marker_in_distinct_rooms_is_ambiguous(self):
        other = "other@chatroom"
        client, _, _, output = self.client([
            response({"asOfEventCursor": "10"}),
            response(events([event("11"), event("12", target=other, message_id="message_2")], "12")),
            response(message(body=BIND_MARKER)),
            response(message(id="message_2", threadId=other, body=BIND_MARKER))])
        with self.assertRaises(group.GroupError) as caught:
            client.bind(100, output.append, marker_factory=lambda: BIND_MARKER)
        self.assertEqual(caught.exception.code, "target_ambiguous")
        self.assertFalse(output[-1]["marker_matched"])
        self.assertNotIn(other, json.dumps(output))

    def test_bind_marker_must_match_exactly(self):
        client, _, _, output = self.client([
            response({"asOfEventCursor": "10"}), response(events()),
            response(message(body=BIND_MARKER + " ")), response(events([], "11"))])
        with self.assertRaises(group.GroupError) as caught:
            client.bind(3, output.append, marker_factory=lambda: BIND_MARKER)
        self.assertEqual(caught.exception.code, "message_not_received")
        self.assertEqual(output[-1]["text_checked"], 1)
        self.assertFalse(output[-1]["marker_matched"])

    def test_bind_body_fetch_count_has_hard_100_limit(self):
        replies = [response({"asOfEventCursor": "10"})]
        for page in range(5):
            batch = [event(str(11 + page * 20 + index), message_id="m" + str(page * 20 + index))
                     for index in range(20)]
            replies.append(response(events(batch, batch[-1]["cursor"])))
            replies.extend(response(message(id=item["data"]["messageId"], body="different")) for item in batch)
        replies.append(response(events([event("111", message_id="limit")], "111")))
        client, transport, _, output = self.client(replies)
        with self.assertRaises(group.GroupError) as caught:
            client.bind(100, output.append, marker_factory=lambda: BIND_MARKER)
        self.assertEqual(caught.exception.code, "catalog_incomplete")
        self.assertEqual(sum("/messages/" in request[1] for request in transport.requests), 100)
        self.assertEqual(output[-1]["candidate_group_events"], 101)

    def test_bind_invalid_cursor_never_restarts_from_zero(self):
        for first in ({}, {"asOfEventCursor": None}, {"asOfEventCursor": "01"}):
            client, transport, _, output = self.client([response(first)])
            with self.assertRaises(group.GroupError) as caught:
                client.bind(100, output.append, marker_factory=lambda: BIND_MARKER)
            self.assertEqual(caught.exception.code, "invalid_cursor")
            self.assertEqual(len(transport.requests), 1)
            self.assertEqual(output, [])
        client, transport, _, output = self.client([
            response({"asOfEventCursor": "10"}), response({"error": {"code": "event_cursor_expired"}}, 410)])
        with self.assertRaises(group.GroupError) as caught:
            client.bind(100, output.append, marker_factory=lambda: BIND_MARKER)
        self.assertEqual(caught.exception.code, "event_cursor_expired")
        self.assertEqual(len(transport.requests), 2)

    def test_bind_text_rejects_cross_account_room_sender_and_noninteger_type(self):
        for changes in ({"accountId": "other"}, {"threadId": "other@chatroom"},
                        {"senderId": ROOM}, {"id": "other"}, {"direction": "outgoing"},
                        {"type": True}, {"body": None}):
            with self.subTest(changes=changes):
                client, _, _, output = self.client([response({"asOfEventCursor": "10"}),
                    response(events()), response(message(body=BIND_MARKER, **{k:v for k,v in changes.items() if k != "body"})
                                               if "body" not in changes else message(body=None))])
                with self.assertRaises(group.GroupError) as caught:
                    client.bind(100, output.append, marker_factory=lambda: BIND_MARKER)
                self.assertEqual(caught.exception.code, "invalid_message")

    def test_bind_default_nonce_is_64_bit_and_window_is_at_most_180_seconds(self):
        client, _, _, output = self.client([response({"asOfEventCursor": "10"}),
                                          response(events()), response(message(body=BIND_MARKER))])
        with patch.object(group.secrets, "token_hex", return_value="0123456789abcdef") as token:
            self.assertEqual(client.bind(500, output.append), ROOM)
        token.assert_called_once_with(8)
        self.assertEqual(output[1]["seconds"], 180)

    def test_bind_rejects_short_or_noncanonical_nonce_before_announcing_wait(self):
        for marker in (MARKER, "AWGROUP-0123", "AWGROUP-0123456789ABCDEF", "AWGROUP-0123456789abcdef\n"):
            client, transport, _, output = self.client([response({"asOfEventCursor": "10"})])
            with self.assertRaises(group.GroupError) as caught:
                client.bind(100, output.append, marker_factory=lambda: marker)
            self.assertEqual(caught.exception.code, "invalid_arguments")
            self.assertEqual(output, [])
            self.assertEqual(len(transport.requests), 1)


if __name__ == "__main__":
    unittest.main()
