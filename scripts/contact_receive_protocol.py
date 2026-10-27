"""One bounded, GET-only private-message receipt test for a fixed contact.

The caller owns login/profile admission and all disconnect/process cleanup. This
module never starts a process, reads files/environment, sends, syncs or marks read.
Only a matching one-time marker is accepted; no message body or private ID is
passed to the output callback. The outer process TTL remains the hard deadline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
import math
import re
import secrets
import time
from typing import Callable, Mapping
from urllib.parse import quote


TARGET_NAME = "测试联系人"
JSON_LIMIT = 64 * 1024
PAGE_LIMIT = 20
MAX_CONTACT_PAGES = 3
MAX_WAIT_SECONDS = 600
# The native catalog shares an account lock with login's initial sync worker.
# Leave time for that lock and a cold profile read, within the existing deadline.
READ_TIMEOUT_SECONDS = 30.0
ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z", re.ASCII)
MARKER_RE = re.compile(r"AWTEST-[0-9a-f]{8}\Z", re.ASCII)
RESULT_CODES = frozenset({
    "ok", "invalid_arguments", "client_already_used", "deadline_exceeded",
    "request_not_allowed", "transport_failed", "transport_timeout", "transport_connection_failed",
    "transport_protocol_failed", "transport_encoding_failed", "body_too_large", "invalid_response",
    "invalid_content_type", "content_encoding_rejected", "redirect_rejected",
    "request_failed", "directory_unavailable", "target_not_found", "target_ambiguous",
    "catalog_incomplete", "invalid_contacts", "invalid_cursor", "cursor_changed",
    "event_cursor_expired", "invalid_events", "invalid_message", "message_not_received",
    "callback_failed",
})


class ContactError(Exception):
    """Only a fixed, public code; never includes upstream content or identifiers."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in RESULT_CODES else "invalid_response"
        super().__init__(self.code)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


def _text(value: object, maximum: int) -> bool:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def _cursor(value: object) -> str:
    if (not isinstance(value, str) or not DECIMAL_RE.fullmatch(value)
            or int(value) > 2**63 - 1):
        raise ContactError("invalid_cursor")
    return value


def _object(body: bytes) -> dict:
    if not isinstance(body, bytes) or len(body) > JSON_LIMIT:
        raise ContactError("body_too_large")

    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError()

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise ContactError("invalid_response") from None
    if not isinstance(value, dict):
        raise ContactError("invalid_response")
    return value


def _header(headers: Mapping[str, str], name: str) -> str:
    values = [value for key, value in headers.items() if key.lower() == name]
    if len(values) > 1 or any(not isinstance(value, str) for value in values):
        raise ContactError("invalid_response")
    return values[0] if values else ""


class RequestPolicy:
    """Allow exactly the current catalog page, event cursor or bound message."""

    def __init__(self, account: str):
        if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
            raise ContactError("invalid_arguments")
        self.account = account
        self.prefix = "/v1/accounts/" + account
        self.catalog_path = (self.prefix + "/contacts?search=" + quote(TARGET_NAME, safe="")
                             + "&limit=20")
        self.path = self.catalog_path

    def authorize(self, method: str, path: str) -> None:
        if method != "GET" or path != self.path:
            raise ContactError("request_not_allowed")

    def contact_page(self, cursor: str) -> str:
        if not _text(cursor, 8192):
            raise ContactError("invalid_cursor")
        self.path = self.catalog_path + "&cursor=" + quote(cursor, safe="")
        return self.path

    def events(self, cursor: str) -> str:
        self.path = self.prefix + "/events?after=" + _cursor(cursor) + "&limit=20"
        return self.path

    def message(self, message_id: str) -> str:
        if not _text(message_id, 512):
            raise ContactError("invalid_events")
        self.path = self.prefix + "/messages/" + quote(message_id, safe="")
        return self.path


class LoopbackGETTransport:
    """Fixed loopback host, no supplied headers/body, proxy lookup or redirects."""

    def __init__(self, policy: RequestPolicy, token: str, *, port: int = 26174):
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or type(port) is not int or not 1 <= port <= 65535):
            raise ContactError("invalid_arguments")
        self.policy = policy
        self._token = token
        self.port = port

    def request(self, method: str, path: str, *, timeout: float) -> Response:
        self.policy.authorize(method, path)
        if (not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
                or not 0 < timeout <= READ_TIMEOUT_SECONDS):
            raise ContactError("invalid_arguments")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request("GET", path, headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/json", "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            headers = {}
            for key, value in response.getheaders():
                key = key.lower()
                if key in {"content-length", "content-type", "content-encoding"}:
                    if key in headers:
                        raise ContactError("invalid_response")
                    headers[key] = value
            length = headers.get("content-length")
            if length is not None and (not re.fullmatch(r"[0-9]+", length)
                                       or len(length) > 10 or int(length) > JSON_LIMIT):
                raise ContactError("body_too_large")
            if 300 <= response.status < 400:
                raise ContactError("redirect_rejected")
            if headers.get("content-encoding", "").lower() not in {"", "identity"}:
                raise ContactError("content_encoding_rejected")
            body = response.read(JSON_LIMIT + 1)
            if len(body) > JSON_LIMIT:
                raise ContactError("body_too_large")
            return Response(response.status, headers, body)
        except ContactError:
            raise
        except TimeoutError:
            raise ContactError("transport_timeout") from None
        except ConnectionError:
            raise ContactError("transport_connection_failed") from None
        except http.client.HTTPException:
            raise ContactError("transport_protocol_failed") from None
        except UnicodeError:
            raise ContactError("transport_encoding_failed") from None
        except Exception:
            raise ContactError("transport_failed") from None
        finally:
            connection.close()


class ContactReceiveClient:
    def __init__(self, account: str, token: str, *, port: int = 26174,
                 transport=None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 marker_factory: Callable[[], str] | None = None):
        self.policy = RequestPolicy(account)
        # Validate even with an injected offline transport.
        native_transport = LoopbackGETTransport(self.policy, token, port=port)
        self.transport = native_transport if transport is None else transport
        self.clock, self.sleep = clock, sleep
        self.marker_factory = marker_factory or (lambda: "AWTEST-" + secrets.token_hex(4))
        self._used = False

    def _get(self, path: str, deadline: float, *, timeout: float = 1.0) -> dict:
        self.policy.authorize("GET", path)
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ContactError("deadline_exceeded")
        try:
            response = self.transport.request("GET", path, timeout=min(timeout, remaining))
        except ContactError:
            raise
        except Exception:
            raise ContactError("transport_failed") from None
        if self.clock() >= deadline:
            raise ContactError("deadline_exceeded")
        if (not isinstance(response, Response) or type(response.status) is not int
                or not 100 <= response.status <= 599):
            raise ContactError("invalid_response")
        if 300 <= response.status < 400:
            raise ContactError("redirect_rejected")
        if _header(response.headers, "content-encoding").lower() not in {"", "identity"}:
            raise ContactError("content_encoding_rejected")
        if _header(response.headers, "content-type").split(";", 1)[0].strip().lower() != "application/json":
            raise ContactError("invalid_content_type")
        value = _object(response.body)
        if response.status != 200:
            error = value.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if code in {"profile_keys_missing", "profile_unavailable", "unsupported_profile"}:
                raise ContactError("directory_unavailable")
            if code == "event_cursor_expired":
                raise ContactError("event_cursor_expired")
            if code == "pagination_cursor_expired":
                raise ContactError("invalid_cursor")
            raise ContactError("request_failed")
        return value

    def _target(self, deadline: float) -> tuple[str, str]:
        matches = []
        boundary = None
        cursors = set()
        path = self.policy.catalog_path
        for _ in range(MAX_CONTACT_PAGES):
            page = self._get(path, deadline, timeout=READ_TIMEOUT_SECONDS)
            coverage = page.get("coverage")
            if not isinstance(coverage, dict) or coverage.get("partial") is not False:
                raise ContactError("catalog_incomplete")
            current = _cursor(page.get("asOfEventCursor"))
            if boundary is not None and current != boundary:
                raise ContactError("cursor_changed")
            boundary = current
            items = page.get("items")
            if not isinstance(items, list) or len(items) > PAGE_LIMIT:
                raise ContactError("invalid_contacts")
            for item in items:
                if not isinstance(item, dict):
                    raise ContactError("invalid_contacts")
                if item.get("deleted") is True:
                    continue
                name = item.get("displayName")
                identity = item.get("id")
                if (not _text(identity, 256) or item.get("username") != identity
                        or (name is not None and not isinstance(name, str))):
                    raise ContactError("invalid_contacts")
                if name == TARGET_NAME:
                    if identity.endswith("@chatroom") or identity == "filehelper" or identity.startswith("weixin"):
                        raise ContactError("invalid_contacts")
                    matches.append(identity)
            if "nextCursor" not in page:
                raise ContactError("invalid_contacts")
            cursor = page["nextCursor"]
            if cursor is None:
                if not matches:
                    raise ContactError("target_not_found")
                if len(matches) != 1:
                    raise ContactError("target_ambiguous")
                return matches[0], boundary
            if not _text(cursor, 8192) or cursor in cursors:
                raise ContactError("invalid_cursor")
            cursors.add(cursor)
            path = self.policy.contact_page(cursor)
        raise ContactError("catalog_incomplete")

    def _emit(self, emit: Callable[[dict], None], value: dict, deadline: float) -> None:
        if self.clock() >= deadline:
            raise ContactError("deadline_exceeded")
        try:
            emit(value)
        except Exception:
            raise ContactError("callback_failed") from None
        if self.clock() >= deadline:
            raise ContactError("deadline_exceeded")

    def _valid_message(self, message: dict, message_id: str, target: str) -> bool:
        """Bind a private message to the exact contact; group subclasses differ."""
        return (message.get("id") == message_id
                and message.get("accountId") == self.policy.account
                and message.get("threadId") == target and message.get("senderId") == target
                and type(message.get("type")) is int and message["type"] == 1
                and message.get("direction") == "incoming"
                and isinstance(message.get("body"), str))

    def run(self, deadline: float, emit: Callable[[dict], None]) -> str:
        if self._used:
            raise ContactError("client_already_used")
        self._used = True
        observation = None
        try:
            if (not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
                    or not callable(emit)):
                raise ContactError("invalid_arguments")
            work_deadline = deadline - 1.0
            target, after = self._target(work_deadline)
            self._emit(emit, {"stage": "contact_state", "state": "target_matched"}, work_deadline)
            window = min(float(MAX_WAIT_SECONDS), work_deadline - self.clock())
            if window <= 0:
                raise ContactError("deadline_exceeded")
            wait_deadline = self.clock() + window
            marker = self.marker_factory()
            if not isinstance(marker, str) or not MARKER_RE.fullmatch(marker):
                raise ContactError("invalid_arguments")
            observation = {
                "stage": "contact_observation", "event_count": 0,
                "target_incoming_count": 0, "target_live_count": 0,
                "text_checked": 0, "marker_matched": False,
            }
            self._emit(emit, {"stage": "contact_waiting", "marker": marker,
                              "seconds": min(MAX_WAIT_SECONDS, math.ceil(window))}, wait_deadline)
            while self.clock() < wait_deadline:
                page = self._get(self.policy.events(after), wait_deadline)
                items = page.get("items")
                if not isinstance(items, list) or len(items) > PAGE_LIMIT:
                    raise ContactError("invalid_events")
                next_cursor = _cursor(page.get("nextCursor"))
                previous = int(after)
                for item in items:
                    if not isinstance(item, dict):
                        raise ContactError("invalid_events")
                    cursor = _cursor(item.get("cursor"))
                    if int(cursor) <= previous:
                        raise ContactError("invalid_events")
                    previous = int(cursor)
                if previous != int(next_cursor):
                    raise ContactError("invalid_events")
                observation["event_count"] = min(20000, observation["event_count"] + len(items))
                for item in items:
                    data = item.get("data")
                    if (item.get("kind") == "message.committed" and isinstance(data, dict)
                            and data.get("direction") == "incoming" and data.get("threadId") == target):
                        observation["target_incoming_count"] = min(
                            20000, observation["target_incoming_count"] + 1)
                        if data.get("origin") == "live":
                            observation["target_live_count"] = min(
                                20000, observation["target_live_count"] + 1)
                for item in items:
                    if item.get("kind") != "message.committed":
                        continue
                    data = item.get("data")
                    if not isinstance(data, dict):
                        raise ContactError("invalid_events")
                    if (data.get("origin") != "live" or data.get("direction") != "incoming"
                            or data.get("threadId") != target):
                        continue
                    message_id = data.get("messageId")
                    message = self._get(self.policy.message(message_id), wait_deadline,
                                        timeout=READ_TIMEOUT_SECONDS)
                    if not self._valid_message(message, message_id, target):
                        raise ContactError("invalid_message")
                    observation["text_checked"] = min(20000, observation["text_checked"] + 1)
                    if message["body"] == marker:
                        observation["marker_matched"] = True
                        return "ok"
                after = next_cursor
                remaining = wait_deadline - self.clock()
                if remaining > 0:
                    self.sleep(min(1.0, remaining))
            raise ContactError("message_not_received")
        except ContactError:
            raise
        except Exception:
            raise ContactError("invalid_response") from None
        finally:
            if observation is not None:
                # Emit aggregate counters even after the work deadline. A failed
                # diagnostic callback must not change the receipt result.
                try:
                    emit(observation)
                except Exception:
                    pass
