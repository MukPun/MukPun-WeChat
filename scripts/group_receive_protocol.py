"""Bounded, GET-only group identification and one-marker receive validation.

Reuse the existing loopback HTTP/event protections. No account login, process,
sync/lookup, group-member enumeration, read marking or send is available here.
The callback receives aggregate progress only, never room/member IDs or text.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Callable
from urllib.parse import quote


def _base_module():
    path = Path(__file__).resolve().with_name("contact_receive_protocol.py")
    spec = importlib.util.spec_from_file_location("_group_receive_contact_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("companion_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contact = _base_module()
Response = contact.Response
GroupError = contact.ContactError
ContactError = contact.ContactError
RESULT_CODES = contact.RESULT_CODES
JSON_LIMIT = contact.JSON_LIMIT
PAGE_LIMIT = contact.PAGE_LIMIT
MAX_GROUP_PAGES = contact.MAX_CONTACT_PAGES
MAX_WAIT_SECONDS = contact.MAX_WAIT_SECONDS
READ_TIMEOUT_SECONDS = contact.READ_TIMEOUT_SECONDS
MARKER_RE = contact.MARKER_RE
BIND_MARKER_RE = re.compile(r"AWGROUP-[0-9a-f]{16}\Z", re.ASCII)
MAX_BIND_WAIT_SECONDS = 180
MAX_BIND_MESSAGES = 100


class RequestPolicy(contact.RequestPolicy):
    def __init__(self, account: str, target_name: str):
        super().__init__(account)
        # The native API bounds search by UTF-8 bytes, not Python characters.
        if not contact._text(target_name, 256):
            raise GroupError("invalid_arguments")
        self.target_name = target_name
        self.catalog_path = (self.prefix + "/groups?search=" + quote(target_name, safe="")
                             + "&limit=20")
        self.path = self.catalog_path


class GroupReceiveClient(contact.ContactReceiveClient):
    def __init__(self, account: str, token: str, target_name: str, *, port: int = 26174,
                 transport=None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 marker_factory: Callable[[], str] | None = None):
        self.policy = RequestPolicy(account, target_name)
        native_transport = contact.LoopbackGETTransport(self.policy, token, port=port)
        self.transport = native_transport if transport is None else transport
        self.clock, self.sleep = clock, sleep
        self.marker_factory = marker_factory or (lambda: "AWTEST-" + secrets.token_hex(4))
        self._used = False

    def list_groups(self, deadline: float, emit: Callable[[dict], None]) -> str:
        """List local catalog names without a search filter or exposing IDs."""
        if self._used:
            raise GroupError("client_already_used")
        self._used = True
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or not callable(emit):
            raise GroupError("invalid_arguments")
        self.policy.catalog_path = self.policy.prefix + "/groups?limit=20"
        self.policy.path = self.policy.catalog_path
        path = self.policy.catalog_path
        boundary = None
        cursors, identities = set(), set()
        count = named = 0
        for _ in range(20):
            page = self._get(path, deadline, timeout=READ_TIMEOUT_SECONDS)
            coverage = page.get("coverage")
            if not isinstance(coverage, dict) or coverage.get("partial") is not False:
                raise GroupError("catalog_incomplete")
            current = contact._cursor(page.get("asOfEventCursor"))
            if boundary is not None and current != boundary:
                raise GroupError("cursor_changed")
            boundary = current
            items = page.get("items")
            if not isinstance(items, list) or len(items) > PAGE_LIMIT:
                raise GroupError("invalid_response")
            for item in items:
                if not isinstance(item, dict):
                    raise GroupError("invalid_response")
                if item.get("deleted") is True:
                    continue
                identity, name = item.get("id"), item.get("displayName")
                if (not contact._text(identity, 256) or item.get("roomId") != identity
                        or not identity.endswith("@chatroom") or identity in identities):
                    raise GroupError("invalid_response")
                identities.add(identity)
                if name is None or name == identity or isinstance(name, str) and name.endswith("@chatroom"):
                    name = ""
                if name != "" and not contact._text(name, 512):
                    raise GroupError("invalid_response")
                count += 1
                named += bool(name)
                self._emit(emit, {"stage": "group_catalog_item", "index": count, "name": name}, deadline)
            if "nextCursor" not in page:
                raise GroupError("invalid_cursor")
            cursor = page["nextCursor"]
            if cursor is None:
                self._emit(emit, {"stage": "group_catalog_summary", "count": count,
                                  "named_count": named, "complete": True}, deadline)
                return "ok"
            if not contact._text(cursor, 8192) or cursor in cursors:
                raise GroupError("invalid_cursor")
            cursors.add(cursor)
            path = self.policy.contact_page(cursor)
        self._emit(emit, {"stage": "group_catalog_summary", "count": count,
                          "named_count": named, "complete": False}, deadline)
        raise GroupError("catalog_incomplete")

    def _target(self, deadline: float) -> tuple[str, str]:
        matches = []
        boundary = None
        cursors = set()
        path = self.policy.catalog_path
        for _ in range(MAX_GROUP_PAGES):
            page = self._get(path, deadline, timeout=READ_TIMEOUT_SECONDS)
            coverage = page.get("coverage")
            if not isinstance(coverage, dict) or coverage.get("partial") is not False:
                raise GroupError("catalog_incomplete")
            current = contact._cursor(page.get("asOfEventCursor"))
            if boundary is not None and current != boundary:
                raise GroupError("cursor_changed")
            boundary = current
            items = page.get("items")
            if not isinstance(items, list) or len(items) > PAGE_LIMIT:
                raise GroupError("invalid_response")
            for item in items:
                if not isinstance(item, dict):
                    raise GroupError("invalid_response")
                if item.get("deleted") is True:
                    continue
                identity, name = item.get("id"), item.get("displayName")
                if (not contact._text(identity, 256) or item.get("roomId") != identity
                        or not identity.endswith("@chatroom")
                        or (name is not None and not isinstance(name, str))):
                    raise GroupError("invalid_response")
                if name == self.policy.target_name:
                    matches.append(identity)
            if "nextCursor" not in page:
                raise GroupError("invalid_response")
            cursor = page["nextCursor"]
            if cursor is None:
                if not matches:
                    raise GroupError("target_not_found")
                if len(matches) != 1:
                    raise GroupError("target_ambiguous")
                return matches[0], boundary
            if not contact._text(cursor, 8192) or cursor in cursors:
                raise GroupError("invalid_cursor")
            cursors.add(cursor)
            path = self.policy.contact_page(cursor)
        raise GroupError("catalog_incomplete")

    def _valid_message(self, message: dict, message_id: str, target: str) -> bool:
        sender = message.get("senderId")
        return (message.get("id") == message_id
                and message.get("accountId") == self.policy.account
                and message.get("threadId") == target
                and contact._text(sender, 512) and not sender.endswith("@chatroom")
                and type(message.get("type")) is int and message["type"] == 1
                and message.get("direction") == "incoming"
                and isinstance(message.get("body"), str))

    @staticmethod
    def _group_emit(emit: Callable[[dict], None]):
        def relay(record: dict):
            # Base records are already restricted to counters and a new marker.
            stage = record.get("stage")
            if stage not in {"contact_state", "contact_waiting", "contact_observation"}:
                raise ValueError("record")
            emit({**record, "stage": "group_" + stage.removeprefix("contact_")})
        return relay

    def run(self, deadline: float, emit: Callable[[dict], None]) -> str:
        # Preserve the base invalid-argument and one-use behavior for bad emit.
        return super().run(deadline, self._group_emit(emit) if callable(emit) else emit)

    def identify(self, deadline: float, emit: Callable[[dict], None]) -> str:
        if self._used:
            raise GroupError("client_already_used")
        self._used = True
        try:
            if (not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
                    or not callable(emit)):
                raise GroupError("invalid_arguments")
            work_deadline = deadline - 1.0
            self._target(work_deadline)
            self._emit(emit, {"stage": "group_state", "state": "target_matched"}, work_deadline)
            return "ok"
        except GroupError:
            raise
        except Exception:
            raise GroupError("invalid_response") from None

    def bind(self, deadline: float, emit: Callable[[dict], None], *,
             marker_factory: Callable[[], str] | None = None) -> str:
        """Return a room only over the caller's private channel after a fresh nonce.

        A cold group catalog can lack names despite incoming messages. Its event
        boundary remains useful. A user supplies the nonce in their target room;
        no catalog completeness or remote group-name verification is claimed.
        """
        if self._used:
            raise GroupError("client_already_used")
        self._used = True
        observation = None
        try:
            if (not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
                    or not callable(emit)
                    or (marker_factory is not None and not callable(marker_factory))):
                raise GroupError("invalid_arguments")
            work_deadline = deadline - 1.0
            page = self._get(self.policy.catalog_path, work_deadline, timeout=READ_TIMEOUT_SECONDS)
            after = contact._cursor(page.get("asOfEventCursor"))
            window = min(float(MAX_BIND_WAIT_SECONDS), work_deadline - self.clock())
            if window <= 0:
                raise GroupError("deadline_exceeded")
            wait_deadline = self.clock() + window
            marker = (marker_factory or (lambda: "AWGROUP-" + secrets.token_hex(8)))()
            if not isinstance(marker, str) or not BIND_MARKER_RE.fullmatch(marker):
                raise GroupError("invalid_arguments")
            observation = {"stage": "group_binding_observation", "event_count": 0,
                           "candidate_group_events": 0, "text_checked": 0,
                           "marker_matched": False}
            self._emit(emit, {"stage": "group_bind_state", "state": "awaiting_marker"}, wait_deadline)
            self._emit(emit, {"stage": "group_waiting", "marker": marker,
                              "seconds": min(MAX_BIND_WAIT_SECONDS, math.ceil(window))}, wait_deadline)
            fetched = 0
            while self.clock() < wait_deadline:
                page = self._get(self.policy.events(after), wait_deadline)
                items = page.get("items")
                if not isinstance(items, list) or len(items) > PAGE_LIMIT:
                    raise GroupError("invalid_events")
                next_cursor = contact._cursor(page.get("nextCursor"))
                previous = int(after)
                for item in items:
                    if not isinstance(item, dict):
                        raise GroupError("invalid_events")
                    cursor = contact._cursor(item.get("cursor"))
                    if int(cursor) <= previous:
                        raise GroupError("invalid_events")
                    previous = int(cursor)
                if previous != int(next_cursor):
                    raise GroupError("invalid_events")
                observation["event_count"] = min(20000, observation["event_count"] + len(items))
                matched = set()
                for item in items:
                    if item.get("kind") != "message.committed":
                        continue
                    data = item.get("data")
                    if not isinstance(data, dict):
                        raise GroupError("invalid_events")
                    room = data.get("threadId")
                    if (data.get("origin") != "live" or data.get("direction") != "incoming"
                            or not contact._text(room, 256) or room == "@chatroom"
                            or not room.endswith("@chatroom")):
                        continue
                    observation["candidate_group_events"] = min(
                        20000, observation["candidate_group_events"] + 1)
                    if fetched >= MAX_BIND_MESSAGES:
                        raise GroupError("catalog_incomplete")
                    message_id = data.get("messageId")
                    value = self._get(self.policy.message(message_id), wait_deadline,
                                      timeout=READ_TIMEOUT_SECONDS)
                    fetched += 1
                    if (value.get("id") != message_id or value.get("accountId") != self.policy.account
                            or value.get("threadId") != room or value.get("direction") != "incoming"
                            or type(value.get("type")) is not int):
                        raise GroupError("invalid_message")
                    if value["type"] != 1:
                        continue
                    if not self._valid_message(value, message_id, room):
                        raise GroupError("invalid_message")
                    observation["text_checked"] = min(20000, observation["text_checked"] + 1)
                    if value["body"] == marker:
                        matched.add(room)
                if len(matched) > 1:
                    raise GroupError("target_ambiguous")
                if matched:
                    observation["marker_matched"] = True
                    return next(iter(matched))
                after = next_cursor
                remaining = wait_deadline - self.clock()
                if remaining > 0:
                    self.sleep(min(1.0, remaining))
            raise GroupError("message_not_received")
        except GroupError:
            raise
        except Exception:
            raise GroupError("invalid_response") from None
        finally:
            if observation is not None:
                try:
                    emit(observation)
                except Exception:
                    pass
