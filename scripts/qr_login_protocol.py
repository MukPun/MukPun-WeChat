"""One fresh beta.8 QR login, with a deliberately small HTTP surface.

This library starts no process, reads no environment or files, and stores no QR
or credentials. The outer controller supplies the in-memory token and handles
the QR callback. Native login itself starts background workers on success; by
default the first subsequent action is disconnect. An optional authenticated
callback can do bounded work before that disconnect.
An explicit keep_connected handoff retains a successful connection only after
all success callbacks return; failures still use the ordinary cleanup path.

The deadline is cooperative: injected transports must honor ``timeout`` and
callbacks must return. A blocking callback cannot be preempted by this library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
import math
import re
import time
import uuid
from typing import Callable, Mapping, Protocol


JSON_LIMIT = 64 * 1024
PNG_LIMIT = 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
OPERATION_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z",
    re.ASCII,
)
SAFE_CODE_RE = re.compile(r"[a-z0-9_]{1,80}\Z", re.ASCII)
UPSTREAM_CODES = frozenset({
    "unauthorized", "forbidden", "account_not_found", "not_found", "auth_required",
    "idempotency_conflict", "operation_in_progress", "login_challenge_unavailable",
    "login_challenge_superseded", "invalid_request", "operation_failed", "worker_failed",
    "invalid_server_response", "account_alias_conflict", "account_identity_mismatch",
    "account_database_password_mismatch", "account_device_mismatch",
    "profile_identity_unavailable", "profile_changed_during_login", "profile_resource_limit",
    "profile_keys_unavailable", "profile_database_unavailable", "profile_cipher_unavailable",
    "profile_io_failed", "unsupported_profile",
    "profile_database_password_required", "profile_saved_key_info_unavailable",
    "profile_network_routing_unavailable", "profile_unavailable", "login_poll_failed",
    "login_failed",
})
KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
ACTIVE_STATES = frozenset(
    {"starting", "resuming", "awaiting_scan", "awaiting_approval", "authenticating"}
)
PROFILE_STATES = frozenset({"created", "existing", "pending"})
PROFILE_REASONS = frozenset({"login_database_password_required", "profile_format_key_required"})
FAILED_STATES = frozenset({"failed", "cancelled", "expired", "rejected", "needs_login"})
STATES = ACTIVE_STATES | FAILED_STATES | {"succeeded"}
LOGIN_BODY = {
    "strategy": "qr",
    "qrRefresh": {"mode": "manual", "maxRefreshes": 0},
    "allowQr": True,
    "forceQr": True,
    "timeoutSeconds": 120,
    "computerName": "AstrBot login probe",
}
# The policy uses an immutable snapshot, so caller mutation of the exported
# example dictionary cannot broaden the request contract.
_LOGIN_JSON = json.dumps(LOGIN_BODY, separators=(",", ":"), sort_keys=True).encode("ascii")
_EXTENDED_LOGIN_JSON = json.dumps(
    {**LOGIN_BODY, "timeoutSeconds": 300}, separators=(",", ":"), sort_keys=True
).encode("ascii")


class ProtocolError(Exception):
    """A safe code only; never includes upstream content, token, URL or QR."""

    def __init__(self, code: str, *, status: int | None = None):
        self.code = code if SAFE_CODE_RE.fullmatch(code) else "protocol_failed"
        self.status = status
        self.cleanup_failed = False
        super().__init__(self.code)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class QrImage:
    generation: int
    png: bytes = field(repr=False)


@dataclass(frozen=True)
class LoginResult:
    state: str = "succeeded"
    disconnected: bool = True


@dataclass(frozen=True)
class AuthenticatedProfile:
    """Sanitized readiness and absolute monotonic deadline for cooperative work."""

    state: str
    reason: str
    deadline: float


class Transport(Protocol):
    def bind_operation(self, operation_id: str) -> None: ...

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        max_bytes: int,
        timeout: float,
    ) -> Response: ...


def _json_bytes(value: dict) -> bytes:
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(data) > JSON_LIMIT:
        raise ProtocolError("body_too_large")
    return data


def _json_object(data: bytes) -> dict:
    if not isinstance(data, bytes) or len(data) > JSON_LIMIT:
        raise ProtocolError("body_too_large")

    def unique(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError()

    try:
        result = json.loads(
            data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid_json") from None
    if not isinstance(result, dict):
        raise ProtocolError("invalid_json")
    return result


def _header(headers: Mapping[str, str], name: str) -> str:
    values = [value for key, value in headers.items() if key.lower() == name]
    if len(values) > 1 or any(not isinstance(value, str) for value in values):
        raise ProtocolError("invalid_headers")
    return values[0] if values else ""


class RequestPolicy:
    """Reject every route outside one fixed account and one bound operation."""

    def __init__(self, account_id: str, *, extended_window: bool = False):
        if not isinstance(account_id, str) or not ACCOUNT_RE.fullmatch(account_id):
            raise ProtocolError("invalid_account_id")
        if type(extended_window) is not bool:
            raise ProtocolError("invalid_mode")
        self._extended_window = extended_window
        self.account_id = account_id
        self.account_path = f"/v1/accounts/{account_id}"
        self.operation_id: str | None = None
        self.idempotency_key: str | None = None

    @property
    def extended_window(self) -> bool:
        return self._extended_window

    @property
    def login_body(self) -> bytes:
        """The fixed immutable request snapshot for this policy's window."""
        return _EXTENDED_LOGIN_JSON if self.extended_window else _LOGIN_JSON

    def bind_operation(self, operation_id: str) -> None:
        if not isinstance(operation_id, str) or not OPERATION_RE.fullmatch(operation_id):
            raise ProtocolError("invalid_operation_id")
        if self.operation_id is not None and self.operation_id != operation_id:
            raise ProtocolError("operation_changed")
        self.operation_id = operation_id

    def authorize(
        self, method: str, path: str, body: bytes | None, headers: Mapping[str, str]
    ) -> int:
        if not isinstance(body, (bytes, type(None))) or (body and len(body) > JSON_LIMIT):
            raise ProtocolError("body_too_large")
        if any(key != "Idempotency-Key" for key in headers):
            raise ProtocolError("request_not_allowed")
        if method == "POST" and path == "/v1/accounts":
            if headers or _json_object(body or b"") != {"id": self.account_id}:
                raise ProtocolError("request_not_allowed")
            return JSON_LIMIT
        if method == "POST" and path == self.account_path + "/auth/login":
            key = headers.get("Idempotency-Key", "")
            if not isinstance(key, str) or not KEY_RE.fullmatch(key):
                raise ProtocolError("invalid_idempotency_key")
            if self.idempotency_key is not None and key != self.idempotency_key:
                raise ProtocolError("idempotency_key_changed")
            canonical = json.dumps(_json_object(body or b""), separators=(",", ":"), sort_keys=True).encode("ascii")
            if canonical != self.login_body:
                raise ProtocolError("request_not_allowed")
            self.idempotency_key = key
            return JSON_LIMIT
        if headers:
            raise ProtocolError("request_not_allowed")
        if method == "POST" and path == self.account_path + "/auth/disconnect":
            if body is not None:
                raise ProtocolError("request_not_allowed")
            return JSON_LIMIT
        if self.operation_id is not None and body is None:
            operation_path = self.account_path + "/operations/" + self.operation_id
            if method in {"GET", "DELETE"} and path == operation_path:
                return JSON_LIMIT
            if method == "GET" and re.fullmatch(
                re.escape(operation_path) + r"/qr\?generation=[1-9][0-9]{0,9}", path,
                flags=re.ASCII,
            ):
                return PNG_LIMIT
        raise ProtocolError("request_not_allowed")


class LoopbackHTTPTransport:
    """Raw loopback HTTP: no proxy lookup, redirects, URL inputs or file I/O."""

    def __init__(
        self, account_id: str, token: str, *, port: int = 26174,
        extended_window: bool = False,
    ):
        self.policy = RequestPolicy(account_id, extended_window=extended_window)
        if (
            not isinstance(token, str) or not 1 <= len(token) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise ProtocolError("invalid_token")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ProtocolError("invalid_port")
        self._token = token
        self.port = port

    def bind_operation(self, operation_id: str) -> None:
        self.policy.bind_operation(operation_id)

    def request(
        self, method: str, path: str, *, body: bytes | None,
        headers: Mapping[str, str], max_bytes: int, timeout: float,
    ) -> Response:
        allowed_limit = self.policy.authorize(method, path, body, headers)
        if type(max_bytes) is not int or not 1 <= max_bytes <= allowed_limit:
            raise ProtocolError("invalid_body_limit")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 1:
            raise ProtocolError("invalid_timeout")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            outgoing = {"Authorization": "Bearer " + self._token, "Accept-Encoding": "identity"}
            outgoing.update(headers)
            if body is not None:
                outgoing["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=outgoing)
            response = connection.getresponse()
            # A failed QR response is still a small JSON error, never a 1 MiB error.
            limit = max_bytes if 200 <= response.status < 300 else min(max_bytes, JSON_LIMIT)
            length = response.getheader("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]+", length) or int(length) > limit):
                raise ProtocolError("body_too_large")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ProtocolError("body_too_large")
            if 300 <= response.status < 400:
                raise ProtocolError("redirect_rejected", status=response.status)
            return Response(response.status, {
                "content-type": response.getheader("Content-Type", ""),
                "content-encoding": response.getheader("Content-Encoding", ""),
            }, data)
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError("transport_failed") from None
        finally:
            connection.close()


class LoginClient:
    """Single-use fresh-account login. All callbacks receive sanitized values."""

    def __init__(
        self, account_id: str, transport: Transport, *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        total_timeout: float = 150, poll_interval: float = 0.5,
        idempotency_key: str | None = None,
        extended_window: bool = False,
    ):
        self.policy = RequestPolicy(account_id, extended_window=extended_window)
        transport_policy = getattr(transport, "policy", None)
        if (isinstance(transport_policy, RequestPolicy) and
                transport_policy.extended_window != extended_window):
            raise ProtocolError("mode_mismatch")
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        maximum_timeout = 900 if extended_window else 180
        if (not isinstance(total_timeout, (int, float)) or not math.isfinite(total_timeout)
                or not 2 < total_timeout <= maximum_timeout):
            raise ProtocolError("invalid_timeout")
        if not isinstance(poll_interval, (int, float)) or not math.isfinite(poll_interval) or not 0.05 <= poll_interval <= 5:
            raise ProtocolError("invalid_poll_interval")
        self.total_timeout = total_timeout
        self.poll_interval = poll_interval
        self.key = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
        if not isinstance(self.key, str) or not KEY_RE.fullmatch(self.key):
            raise ProtocolError("invalid_idempotency_key")
        self._used = False

    def _request(
        self, method: str, path: str, deadline: float, *, value: dict | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        body = _json_bytes(value) if value is not None else None
        request_headers = headers or {}
        limit = self.policy.authorize(method, path, body, request_headers)
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ProtocolError("deadline_exceeded")
        try:
            response = self.transport.request(
                method, path, body=body, headers=request_headers,
                max_bytes=limit, timeout=min(1.0, remaining),
            )
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError("transport_failed") from None
        if self.clock() > deadline:
            raise ProtocolError("deadline_exceeded")
        if not isinstance(response, Response) or type(response.status) is not int or not 100 <= response.status <= 599:
            raise ProtocolError("invalid_response")
        if not isinstance(response.body, bytes) or len(response.body) > limit:
            raise ProtocolError("body_too_large")
        if 300 <= response.status < 400:
            raise ProtocolError("redirect_rejected", status=response.status)
        if not 200 <= response.status < 300:
            if len(response.body) > JSON_LIMIT:
                raise ProtocolError("body_too_large", status=response.status)
            code = "request_failed"
            try:
                error = _json_object(response.body).get("error")
                candidate = error.get("code") if isinstance(error, dict) else None
                if isinstance(candidate, str) and candidate in UPSTREAM_CODES:
                    code = candidate
            except ProtocolError:
                pass
            raise ProtocolError(code, status=response.status)
        if _header(response.headers, "content-encoding").lower() not in {"", "identity"}:
            raise ProtocolError("content_encoding_rejected")
        return response

    def _object(self, response: Response, status: int) -> dict:
        if response.status != status:
            raise ProtocolError("unexpected_status", status=response.status)
        if _header(response.headers, "content-type").split(";", 1)[0].strip().lower() != "application/json":
            raise ProtocolError("invalid_content_type")
        return _json_object(response.body)

    def _operation(self, value: dict, *, bind: bool = False) -> tuple[str, dict]:
        operation_id = value.get("id")
        if (
            not isinstance(operation_id, str) or not OPERATION_RE.fullmatch(operation_id)
            or value.get("accountId") != self.policy.account_id
            or value.get("kind") != "login" or value.get("state") not in STATES
            or "result" not in value
            or (value["result"] is not None and not isinstance(value["result"], dict))
        ):
            raise ProtocolError("invalid_operation")
        if bind:
            self.policy.bind_operation(operation_id)
            self.transport.bind_operation(operation_id)
        elif operation_id != self.policy.operation_id:
            raise ProtocolError("operation_changed")
        return value["state"], value["result"] or {}

    def _disconnect(self, deadline: float) -> None:
        result = self._object(self._request(
            "POST", self.policy.account_path + "/auth/disconnect", deadline
        ), 200)
        if result.get("state") != "disconnected" or result.get("trustRetained") is not True:
            raise ProtocolError("disconnect_unconfirmed")

    def _callback(self, callback: Callable | None, value: object, deadline: float) -> None:
        if self.clock() >= deadline:
            raise ProtocolError("deadline_exceeded")
        if callback is not None:
            try:
                callback(value)
            except BaseException:
                raise ProtocolError("callback_failed") from None
        if self.clock() >= deadline:
            raise ProtocolError("deadline_exceeded")

    def run(
        self, on_qr: Callable[[QrImage], None],
        on_state: Callable[[str], None] | None = None,
        *, qr_only: bool = False,
        on_authenticated: Callable[[AuthenticatedProfile], None] | None = None,
        keep_connected: bool = False,
    ) -> LoginResult:
        if self._used:
            raise ProtocolError("client_already_used")
        self._used = True
        if (not callable(on_qr) or (on_state is not None and not callable(on_state)) or
                (on_authenticated is not None and not callable(on_authenticated))):
            raise ProtocolError("invalid_callback")
        if type(qr_only) is not bool or type(keep_connected) is not bool:
            raise ProtocolError("invalid_mode")
        deadline = self.clock() + self.total_timeout
        work_deadline = deadline - 2.0  # Reserve two bounded HTTP calls for cleanup.
        created = False
        disconnected = False
        handed_off = False
        state: str | None = None
        last_state: str | None = None
        shown: set[int] = set()
        failure: ProtocolError | None = None
        result_state = "succeeded"
        try:
            account = self._object(self._request(
                "POST", "/v1/accounts", work_deadline, value={"id": self.policy.account_id}
            ), 201)
            if account.get("id") != self.policy.account_id:
                raise ProtocolError("account_changed")
            created = True
            operation = self._object(self._request(
                "POST", self.policy.account_path + "/auth/login", work_deadline,
                value=_json_object(self.policy.login_body), headers={"Idempotency-Key": self.key},
            ), 202)
            state, metadata = self._operation(operation, bind=True)
            operation_path = self.policy.account_path + "/operations/" + str(self.policy.operation_id)
            while True:
                if state == "succeeded":
                    if on_authenticated is not None:
                        profile = metadata.get("profile")
                        profile_state = profile.get("state") if isinstance(profile, dict) else None
                        profile_reason = profile.get("reason") if isinstance(profile, dict) else None
                        if not isinstance(profile_state, str) or profile_state not in PROFILE_STATES:
                            profile_state = "unavailable"
                        if (profile_state != "pending" or not isinstance(profile_reason, str) or
                                profile_reason not in PROFILE_REASONS):
                            profile_reason = ""
                        self._callback(on_authenticated, AuthenticatedProfile(
                            profile_state, profile_reason, work_deadline
                        ), work_deadline)
                    if not keep_connected:
                        self._disconnect(deadline)
                        disconnected = True
                    self._callback(on_state, state, work_deadline if keep_connected else deadline)
                    # Handoff is committed only after all success callbacks return.
                    # Any failure before this point still disconnects in finally.
                    handed_off = keep_connected
                    break
                if state in FAILED_STATES:
                    detail = metadata.get("code")
                    raise ProtocolError(detail if state == "failed" and isinstance(detail, str)
                                        and detail in UPSTREAM_CODES else "login_" + state)
                if state != last_state:
                    self._callback(on_state, state, work_deadline)
                    last_state = state
                if state in {"awaiting_scan", "awaiting_approval"} and metadata.get("qrAvailable") is True:
                    generation = metadata.get("qrGeneration")
                    if type(generation) is not int or not 1 <= generation <= 2147483647:
                        raise ProtocolError("invalid_qr_generation")
                    if generation not in shown:
                        image = self._request(
                            "GET", operation_path + f"/qr?generation={generation}", work_deadline
                        )
                        content_type = _header(image.headers, "content-type").split(";", 1)[0].strip().lower()
                        if image.status != 200 or content_type != "image/png" or not image.body.startswith(PNG_MAGIC):
                            raise ProtocolError("invalid_qr_image")
                        shown.add(generation)
                        self._callback(on_qr, QrImage(generation, image.body), work_deadline)
                        if qr_only:
                            result_state = "qr_available"
                            break
                remaining = work_deadline - self.clock()
                if remaining <= 0:
                    raise ProtocolError("deadline_exceeded")
                self.sleep(min(self.poll_interval, remaining))
                operation = self._object(self._request("GET", operation_path, work_deadline), 200)
                state, metadata = self._operation(operation)
        except ProtocolError as error:
            failure = error
        except BaseException:
            failure = ProtocolError("protocol_failed")
        finally:
            cleanup_failed = False
            if self.policy.operation_id is not None and state != "succeeded":
                try:
                    cancelled = self._object(self._request(
                        "DELETE", self.policy.account_path + "/operations/" + self.policy.operation_id, deadline
                    ), 200)
                    cancelled_state, _ = self._operation(cancelled)
                    if cancelled_state not in FAILED_STATES | {"succeeded"}:
                        raise ProtocolError("cancel_unconfirmed")
                except BaseException:
                    cleanup_failed = True
            if created and not disconnected and not handed_off:
                try:
                    self._disconnect(deadline)
                    disconnected = True
                except BaseException:
                    cleanup_failed = True
            if cleanup_failed:
                if failure is None:
                    failure = ProtocolError("cleanup_failed")
                failure.cleanup_failed = True
        if failure is not None:
            raise failure from None
        return LoginResult(state=result_state, disconnected=disconnected)
