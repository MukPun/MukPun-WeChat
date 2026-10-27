"""Resume one existing beta.8 account without creating an account or QR login.

The outer runtime owns the complete persistent data directory. This module reads
no files or environment, starts no processes, and never sends, cancels, logs out,
forgets credentials or disconnects. It emits fixed states and returns safe codes.

Pinned API evidence: wechat-api/src/actions.rs connect accepts no body/key and
returns a login operation; wechat-core/src/actions.rs uses auto with allowQr=false
and forceQr=false. runtime.rs account_status reports the current daemon's validated
connection as authenticated=true and connection="online".
"""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
import math
import re
import time
from typing import Callable, Mapping


LOGICAL_ACCOUNT = "wechat-primary"
JSON_LIMIT = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 1.0
MAX_RESUME_SECONDS = 330.0
MAX_POLLS = 660
ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
OPERATION_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z",
    re.ASCII,
)
ACTIVE_STATES = frozenset({"starting", "resuming", "awaiting_approval", "authenticating"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "expired", "rejected", "needs_login"})
STATES = ACTIVE_STATES | TERMINAL_STATES | {"awaiting_scan"}
RESULT_CODES = frozenset({
    "connected", "needs_initialization", "needs_attention", "existing_account_mismatch",
    "invalid_arguments", "client_already_used", "request_not_allowed", "invalid_response",
    "body_too_large", "redirect_rejected", "content_encoding_rejected", "callback_failed",
    "deadline_exceeded", "transport_timeout", "transport_connection_failed",
    "transport_protocol_failed", "transport_encoding_failed", "transport_failed",
    "storage_failed", "authentication_failed", "network_failed", "operation_in_progress",
    "resume_failed",
})
STORAGE_CODES = frozenset({
    "profile_identity_unavailable", "profile_database_password_required",
    "profile_saved_key_info_unavailable", "profile_keys_unavailable", "profile_keys_missing",
    "profile_database_unavailable", "profile_changed_during_login", "profile_resource_limit",
    "profile_cipher_unavailable", "profile_io_failed", "profile_unavailable", "unsupported_profile",
    "account_database_password_mismatch", "official_profile_not_found",
    "official_profile_ambiguous", "official_profile_in_use", "official_profile_overlaps_store",
    "official_profile_inventory_limit", "account_profile_mismatch",
})
AUTH_CODES = frozenset({
    "unauthorized", "forbidden", "auth_required", "account_identity_mismatch", "account_device_mismatch",
})


class ResumeError(Exception):
    """A fixed code only; upstream messages and reasons are never exposed."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in RESULT_CODES else "invalid_response"
        super().__init__(self.code)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class ResumeResult:
    ok: bool
    code: str
    connected: bool = False
    needs_initialization: bool = False


def _object(body: bytes) -> dict:
    if not isinstance(body, bytes) or len(body) > JSON_LIMIT:
        raise ResumeError("body_too_large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError()

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise ResumeError("invalid_response") from None
    if not isinstance(value, dict):
        raise ResumeError("invalid_response")
    return value


def _header(headers: Mapping[str, str], name: str) -> str:
    if not isinstance(headers, Mapping):
        raise ResumeError("invalid_response")
    values = []
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ResumeError("invalid_response")
        if key.lower() == name:
            values.append(value)
    if len(values) > 1:
        raise ResumeError("invalid_response")
    return values[0] if values else ""


def _failure_code(value: object, *, status: int | None = None) -> str:
    # Do not retain or emit raw public error bodies, operation reasons or IDs.
    code = value if isinstance(value, str) else ""
    if status in {401, 403} or code in AUTH_CODES:
        return "authentication_failed"
    if code in STORAGE_CODES:
        return "storage_failed"
    if code == "profile_network_routing_unavailable":
        return "network_failed"
    if code in {"needs_login", "no_usable_saved_trust"}:
        return "needs_attention"
    if code in {"account_not_found", "not_found"}:
        return "existing_account_mismatch"
    if code == "operation_in_progress":
        return "operation_in_progress"
    return "resume_failed"


class RequestPolicy:
    """Four exact routes; one connect and one immutable UUID binding."""

    def __init__(self, account: str):
        if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
            raise ResumeError("invalid_arguments")
        self.account = account
        self.prefix = "/v1/accounts/" + account
        self.operation_path: str | None = None

    def bind_operation(self, operation_id: str) -> str:
        if not isinstance(operation_id, str) or not OPERATION_RE.fullmatch(operation_id):
            raise ResumeError("invalid_response")
        path = self.prefix + "/operations/" + operation_id
        if self.operation_path is not None and self.operation_path != path:
            raise ResumeError("invalid_response")
        self.operation_path = path
        return path

    def authorize(self, method: str, path: str, *, body=None, headers=None) -> None:
        if body is not None or headers is not None:
            raise ResumeError("request_not_allowed")
        if method == "GET" and path in {"/v1/accounts", self.prefix}:
            return
        if method == "POST" and path == self.prefix + "/connect":
            return
        if method == "GET" and self.operation_path is not None and path == self.operation_path:
            return
        raise ResumeError("request_not_allowed")


class LoopbackHTTPTransport:
    """Raw 127.0.0.1 HTTP only: no environment proxies or redirect following."""

    def __init__(self, policy: RequestPolicy, token: str, *, port: int = 26174):
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in token)
                or type(port) is not int or not 1 <= port <= 65535):
            raise ResumeError("invalid_arguments")
        self._policy, self._token, self._port = policy, token, port
        self._connect_attempted = False

    def request(self, method: str, path: str, *, timeout: float, body=None, headers=None) -> Response:
        self._policy.authorize(method, path, body=body, headers=headers)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= REQUEST_TIMEOUT_SECONDS):
            raise ResumeError("invalid_arguments")
        if method == "POST":
            if self._connect_attempted:
                raise ResumeError("request_not_allowed")
            self._connect_attempted = True
        connection = None
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=timeout)
            connection.request(method, path, headers={
                "Authorization": "Bearer " + self._token, "Accept": "application/json",
                "Accept-Encoding": "identity",
            })
            incoming = connection.getresponse()
            response_headers = {}
            for key, value in incoming.getheaders():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ResumeError("invalid_response")
                key = key.lower()
                if key in {"content-length", "content-type", "content-encoding"}:
                    if key in response_headers:
                        raise ResumeError("invalid_response")
                    response_headers[key] = value
            length = response_headers.get("content-length")
            if length is not None and (not re.fullmatch(r"[0-9]+", length)
                                       or len(length) > 10 or int(length) > JSON_LIMIT):
                raise ResumeError("body_too_large")
            if 300 <= incoming.status < 400:
                raise ResumeError("redirect_rejected")
            if response_headers.get("content-encoding", "").lower() not in {"", "identity"}:
                raise ResumeError("content_encoding_rejected")
            data = incoming.read(JSON_LIMIT + 1)
            if not isinstance(data, bytes) or len(data) > JSON_LIMIT:
                raise ResumeError("body_too_large")
            if length is not None and len(data) != int(length):
                raise ResumeError("invalid_response")
            return Response(incoming.status, response_headers, data)
        except ResumeError:
            raise
        except TimeoutError:
            raise ResumeError("transport_timeout") from None
        except ConnectionError:
            raise ResumeError("transport_connection_failed") from None
        except http.client.HTTPException:
            raise ResumeError("transport_protocol_failed") from None
        except UnicodeError:
            raise ResumeError("transport_encoding_failed") from None
        except Exception:
            raise ResumeError("transport_failed") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


class SessionResumeClient:
    def __init__(self, account: str, token: str, *, port: int = 26174, transport=None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, poll_interval: float = 0.5):
        self.policy = RequestPolicy(account)
        native = LoopbackHTTPTransport(self.policy, token, port=port)
        if (not callable(clock) or not callable(sleep) or type(poll_interval) not in (int, float)
                or not math.isfinite(poll_interval) or not 0.05 <= poll_interval <= 5):
            raise ResumeError("invalid_arguments")
        self.transport = native if transport is None else transport
        self.clock, self.sleep, self.poll_interval = clock, sleep, poll_interval
        self._used = False

    def _request(self, method: str, path: str, deadline: float, status: int) -> dict:
        self.policy.authorize(method, path)
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ResumeError("deadline_exceeded")
        try:
            response = self.transport.request(method, path, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining))
        except ResumeError:
            raise
        except TimeoutError:
            raise ResumeError("transport_timeout") from None
        except ConnectionError:
            raise ResumeError("transport_connection_failed") from None
        except http.client.HTTPException:
            raise ResumeError("transport_protocol_failed") from None
        except UnicodeError:
            raise ResumeError("transport_encoding_failed") from None
        except Exception:
            raise ResumeError("transport_failed") from None
        if self.clock() >= deadline:
            raise ResumeError("deadline_exceeded")
        if (not isinstance(response, Response) or type(response.status) is not int
                or not 100 <= response.status <= 599):
            raise ResumeError("invalid_response")
        if not isinstance(response.body, bytes) or len(response.body) > JSON_LIMIT:
            raise ResumeError("body_too_large")
        if 300 <= response.status < 400:
            raise ResumeError("redirect_rejected")
        if _header(response.headers, "content-encoding").lower() not in {"", "identity"}:
            raise ResumeError("content_encoding_rejected")
        if response.status in {401, 403}:
            raise ResumeError("authentication_failed")
        if _header(response.headers, "content-type").split(";", 1)[0].strip().lower() != "application/json":
            raise ResumeError("invalid_response")
        value = _object(response.body)
        if response.status != status:
            error = value.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise ResumeError(_failure_code(code, status=response.status))
        return value

    def _status(self, value: dict) -> bool:
        if value.get("id") != self.policy.account:
            raise ResumeError("existing_account_mismatch")
        if (type(value.get("authenticated")) is not bool
                or value.get("connection") not in {"online", "reconnecting", "disconnected"}):
            raise ResumeError("invalid_response")
        return value["authenticated"] is True and value["connection"] == "online"

    def _operation(self, value: dict, *, bind: bool = False) -> tuple[str, dict]:
        operation_id = value.get("id")
        if (not isinstance(operation_id, str) or not OPERATION_RE.fullmatch(operation_id)
                or value.get("accountId") != self.policy.account or value.get("kind") != "login"
                or not isinstance(value.get("state"), str) or value["state"] not in STATES
                or not isinstance(value.get("result"), dict)):
            raise ResumeError("invalid_response")
        path = self.policy.prefix + "/operations/" + operation_id
        if bind:
            self.policy.bind_operation(operation_id)
            # Injected transports may enforce their own binding too.
            binder = getattr(self.transport, "bind_operation", None)
            if binder is not None:
                binder(operation_id)
        elif path != self.policy.operation_path:
            raise ResumeError("invalid_response")
        return value["state"], value["result"]

    def run(self, deadline: float, emit: Callable[[dict], None]) -> ResumeResult:
        if self._used:
            return ResumeResult(False, "client_already_used")
        self._used = True
        try:
            if type(deadline) not in (int, float) or not math.isfinite(deadline) or not callable(emit):
                raise ResumeError("invalid_arguments")
            deadline = min(deadline, self.clock() + MAX_RESUME_SECONDS)
            accounts = self._request("GET", "/v1/accounts", deadline, 200).get("items")
            if not isinstance(accounts, list):
                raise ResumeError("invalid_response")
            if not accounts:
                return ResumeResult(False, "needs_initialization", needs_initialization=True)
            ids = []
            for account in accounts:
                if (not isinstance(account, dict) or not isinstance(account.get("id"), str)
                        or not ACCOUNT_RE.fullmatch(account["id"])):
                    raise ResumeError("invalid_response")
                ids.append(account["id"])
            if self.policy.account not in ids or len(set(ids)) != len(ids):
                raise ResumeError("existing_account_mismatch")
            if self._status(self._request("GET", self.policy.prefix, deadline, 200)):
                return ResumeResult(True, "connected", connected=True)
            value = self._request("POST", self.policy.prefix + "/connect", deadline, 202)
            state, metadata = self._operation(value, bind=True)
            previous_state = None
            for _ in range(MAX_POLLS):
                if state == "succeeded":
                    # Confirm current-daemon validation instead of trusting a stale receipt.
                    if not self._status(self._request("GET", self.policy.prefix, deadline, 200)):
                        raise ResumeError("resume_failed")
                    return ResumeResult(True, "connected", connected=True)
                if state in {"needs_login", "awaiting_scan", "rejected"}:
                    return ResumeResult(False, "needs_attention")
                if state in TERMINAL_STATES:
                    if metadata.get("reason") == "no_usable_saved_trust":
                        return ResumeResult(False, "needs_attention")
                    raise ResumeError(_failure_code(metadata.get("code")))
                if state != previous_state:
                    try:
                        emit({"stage": "resume_state", "state": state})
                    except BaseException:
                        raise ResumeError("callback_failed") from None
                    previous_state = state
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise ResumeError("deadline_exceeded")
                self.sleep(min(self.poll_interval, remaining))
                value = self._request("GET", self.policy.operation_path, deadline, 200)
                state, metadata = self._operation(value)
            raise ResumeError("deadline_exceeded")
        except ResumeError as error:
            return ResumeResult(False, error.code)
        except Exception:
            return ResumeResult(False, "invalid_response")
