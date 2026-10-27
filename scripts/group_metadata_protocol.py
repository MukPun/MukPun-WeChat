"""Refresh metadata for one already bound group, without sending or logging in.

The caller supplies the private binding and owns process/snapshot lifecycle.
Exactly one POST is permitted; all further requests read its bound operation or
the same group's local catalog record. Failure never resubmits or requests QR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
import math
import re
import time
from typing import Callable, Mapping
from urllib.parse import quote

JSON_LIMIT = 64 * 1024
MAX_SECONDS = 150.0
MAX_POLLS = 160
ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
OPERATION_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.ASCII)
STATES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
ACTIVE_STATES = frozenset({"queued", "running"})
RESULT_CODES = frozenset({
    "ok", "invalid_arguments", "client_already_used", "request_not_allowed", "deadline_exceeded",
    "transport_failed", "transport_timeout", "transport_connection_failed", "transport_protocol_failed",
    "transport_encoding_failed", "body_too_large", "invalid_response", "invalid_content_type",
    "content_encoding_rejected", "redirect_rejected", "request_failed", "invalid_operation",
    "metadata_failed", "metadata_cancelled", "metadata_incomplete", "name_mismatch",
    "invalid_group", "auth_required", "account_identity_mismatch", "callback_failed",
})


class MetadataError(Exception):
    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in RESULT_CODES else "invalid_response"
        super().__init__(self.code)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


def _text(value, maximum):
    try:
        return (isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= maximum
                and not any(ord(character) < 32 or ord(character) == 127 for character in value))
    except UnicodeError:
        return False


def _header(headers, name):
    if not isinstance(headers, Mapping):
        raise MetadataError("invalid_response")
    values = [value for key, value in headers.items() if isinstance(key, str) and key.lower() == name]
    if len(values) > 1 or any(not isinstance(value, str) for value in values):
        raise MetadataError("invalid_response")
    return values[0] if values else ""


def _object(body):
    if not isinstance(body, bytes) or len(body) > JSON_LIMIT:
        raise MetadataError("body_too_large")
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("constant")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise MetadataError("invalid_response") from None
    if not isinstance(value, dict):
        raise MetadataError("invalid_response")
    return value


class RequestPolicy:
    def __init__(self, account, room_id):
        if (not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account)
                or not _text(room_id, 256) or room_id == "@chatroom" or not room_id.endswith("@chatroom")):
            raise MetadataError("invalid_arguments")
        self.account, self.room_id = account, room_id
        self.prefix = "/v1/accounts/" + account
        self.group_path = self.prefix + "/groups/" + quote(room_id, safe="")
        self.refresh_path = self.group_path + "/refresh"
        self.operation_path = None
        self.posted = False

    def bind_operation(self, identity):
        if (not isinstance(identity, str) or not OPERATION_RE.fullmatch(identity)
                or self.operation_path is not None):
            raise MetadataError("invalid_operation")
        self.operation_path = self.prefix + "/operations/" + identity

    def authorize(self, method, path):
        if method == "POST" and path == self.refresh_path and not self.posted:
            # Consume before I/O: even a timeout cannot admit a second POST.
            self.posted = True
            return
        if method == "GET" and self.posted and path in {self.operation_path, self.group_path}:
            return
        raise MetadataError("request_not_allowed")


class LoopbackMetadataTransport:
    def __init__(self, policy, token, *, port=26174):
        if (not isinstance(policy, RequestPolicy) or not isinstance(token, str)
                or not 8 <= len(token) <= 1024 or any(not 33 <= ord(char) <= 126 for char in token)
                or type(port) is not int or not 1 <= port <= 65535):
            raise MetadataError("invalid_arguments")
        self.policy, self.token, self.port = policy, token, port
        self._post_attempted = False

    def request(self, method, path, *, timeout):
        # The client consumes policy admission. Transport checks only the fixed
        # route surface, so a caller cannot bypass it with arbitrary HTTP.
        if not isinstance(path, str) or not (method == "POST" and path == self.policy.refresh_path and not self._post_attempted
                or method == "GET" and self._post_attempted and path in {self.policy.operation_path, self.policy.group_path}):
            raise MetadataError("request_not_allowed")
        if method == "POST":
            self._post_attempted = True
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise MetadataError("invalid_arguments")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            # Body is deliberately omitted; no idempotency key or login scope.
            connection.request(method, path, headers={"Authorization": "Bearer " + self.token,
                               "Accept": "application/json", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > JSON_LIMIT):
                raise MetadataError("body_too_large")
            body = response.read(JSON_LIMIT + 1)
            if len(body) > JSON_LIMIT:
                raise MetadataError("body_too_large")
            headers = {}
            for key, value in response.getheaders():
                lowered = key.lower()
                if lowered in headers and lowered in {"content-type", "content-length", "content-encoding"}:
                    raise MetadataError("invalid_response")
                headers[lowered] = value
            return Response(response.status, headers, body)
        except MetadataError:
            raise
        except TimeoutError:
            raise MetadataError("transport_timeout") from None
        except (ConnectionError, OSError):
            raise MetadataError("transport_connection_failed") from None
        except http.client.HTTPException:
            raise MetadataError("transport_protocol_failed") from None
        except UnicodeError:
            raise MetadataError("transport_encoding_failed") from None
        except Exception:
            raise MetadataError("transport_failed") from None
        finally:
            connection.close()


class GroupMetadataClient:
    def __init__(self, account, token, room_id, target_name, *, port=26174, transport=None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.policy = RequestPolicy(account, room_id)
        if not _text(target_name, 256) or not callable(clock) or not callable(sleep):
            raise MetadataError("invalid_arguments")
        real_transport = LoopbackMetadataTransport(self.policy, token, port=port)
        self.transport = real_transport if transport is None else transport
        self.target_name = target_name
        self.clock, self.sleep = clock, sleep
        self._used = False

    def _request(self, method, path, deadline, *, status, timeout=1.0):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise MetadataError("deadline_exceeded")
        self.policy.authorize(method, path)
        try:
            response = self.transport.request(method, path, timeout=min(timeout, remaining))
        except MetadataError:
            raise
        except Exception:
            raise MetadataError("transport_failed") from None
        if self.clock() >= deadline:
            raise MetadataError("deadline_exceeded")
        if (not isinstance(response, Response) or type(response.status) is not int
                or not 100 <= response.status <= 599):
            raise MetadataError("invalid_response")
        if 300 <= response.status <= 399:
            raise MetadataError("redirect_rejected")
        if _header(response.headers, "content-encoding").lower() not in {"", "identity"}:
            raise MetadataError("content_encoding_rejected")
        if _header(response.headers, "content-type").split(";", 1)[0].strip().lower() != "application/json":
            raise MetadataError("invalid_content_type")
        value = _object(response.body)
        if response.status != status:
            error = value.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if code in {"unauthorized", "forbidden", "auth_required"}:
                raise MetadataError("auth_required")
            if code == "account_identity_mismatch":
                raise MetadataError(code)
            raise MetadataError("request_failed")
        return value

    def _operation(self, value, identity=None):
        fields = {"id", "accountId", "kind", "state", "result", "createdAt", "updatedAt"}
        if (set(value) != fields or not isinstance(value["id"], str) or not OPERATION_RE.fullmatch(value["id"])
                or identity is not None and value["id"] != identity
                or value["accountId"] != self.policy.account or value["kind"] != "metadata"
                or not isinstance(value["state"], str) or value["state"] not in STATES
                or not isinstance(value["result"], dict)
                or not _text(value["createdAt"], 128) or not _text(value["updatedAt"], 128)):
            raise MetadataError("invalid_operation")
        return value["state"]

    def _emit(self, emit, record, deadline):
        if self.clock() >= deadline:
            raise MetadataError("deadline_exceeded")
        try:
            emit(record)
        except Exception:
            raise MetadataError("callback_failed") from None
        if self.clock() >= deadline:
            raise MetadataError("deadline_exceeded")

    def run(self, deadline, emit, *, on_operation=None):
        if self._used:
            raise MetadataError("client_already_used")
        self._used = True
        if (type(deadline) not in {int, float} or not math.isfinite(deadline)
                or not callable(emit) or on_operation is not None and not callable(on_operation)):
            raise MetadataError("invalid_arguments")
        work = min(float(deadline) - 1.0, self.clock() + MAX_SECONDS)
        operation = self._request("POST", self.policy.refresh_path, work, status=202)
        state = self._operation(operation)
        identity = operation["id"]
        self.policy.bind_operation(identity)
        if on_operation is not None:
            # The private callback can persist this ticket outside the public
            # progress channel. Failure never retries admission or cancels it.
            self._emit(on_operation, identity, work)
        self._emit(emit, {"stage": "group_metadata_state", "state": "submitted"}, work)
        previous = None
        running_seen = False
        for iteration in range(MAX_POLLS):
            if state == "queued" and running_seen:
                raise MetadataError("invalid_operation")
            running_seen = running_seen or state == "running"
            if state != previous and state in ACTIVE_STATES | {"succeeded"}:
                self._emit(emit, {"stage": "group_metadata_state", "state": state}, work)
                previous = state
            if state == "failed":
                code = operation["result"].get("code")
                raise MetadataError(code if code in {"auth_required", "account_identity_mismatch"} else "metadata_failed")
            if state == "cancelled":
                raise MetadataError("metadata_cancelled")
            if state == "succeeded":
                result = operation["result"]
                if (set(result) != {"recordCount", "failedItems", "profileProjection", "coverage"}
                        or type(result["recordCount"]) is not int or result["recordCount"] != 1
                        or type(result["failedItems"]) is not int or result["failedItems"] != 0
                        or result["profileProjection"] != "complete" or result["coverage"] != "known_identity"):
                    raise MetadataError("metadata_incomplete")
                value = self._request("GET", self.policy.group_path, work, status=200, timeout=30)
                if value.get("id") != self.policy.room_id or value.get("roomId") != self.policy.room_id:
                    raise MetadataError("invalid_group")
                if value.get("displayName") != self.target_name:
                    raise MetadataError("name_mismatch")
                self._emit(emit, {"stage": "group_metadata_check", "ok": True,
                                 "projection_complete": True, "name_matches": True}, work)
                return "ok"
            remaining = work - self.clock()
            if remaining <= 0 or iteration + 1 >= MAX_POLLS:
                raise MetadataError("deadline_exceeded")
            self.sleep(min(1.0, remaining))
            operation = self._request("GET", self.policy.operation_path, work, status=200)
            state = self._operation(operation, identity)
        raise MetadataError("deadline_exceeded")
