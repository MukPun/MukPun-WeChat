"""One fixed-contact text submission, with no automatic submission retries.

The caller owns explicit user approval, login admission and process cleanup.
This module reads only the existing contact catalog before submitting the exact
test text once. A confirmed result means server acceptance, never recipient read.
It reads no credentials, environment or profile files and starts no processes.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import time
from typing import Callable
import uuid


def _receive_companion():
    path = Path(__file__).resolve().with_name("contact_receive_protocol.py")
    name = "_contact_send_receive"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("companion_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


receive = _receive_companion()
TARGET_NAME = receive.TARGET_NAME
TEST_TEXT = "微信接入测试：这是一条私聊发送验证消息，无需回复。"
JSON_LIMIT = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_POLL_SECONDS = 45.0
STATES = frozenset({"queued", "submitting", "accepted", "failed", "needs_review", "cancelled"})
EMITTED_STATES = STATES | {"target_matched"}
TERMINAL_CODES = {"failed": "send_failed", "needs_review": "send_needs_review",
                  "cancelled": "send_cancelled"}
RESULT_CODES = frozenset(receive.RESULT_CODES | {
    "outcome_unknown", "send_failed", "send_needs_review", "send_cancelled",
})
DECIMAL_RE = re.compile(r"[1-9][0-9]{0,19}\Z", re.ASCII)
KEY_RE = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)


class SendError(Exception):
    """A public fixed code only, without upstream content or private IDs."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in RESULT_CODES else "invalid_response"
        super().__init__(self.code)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    code: str
    submitted: bool
    server_confirmed: bool


Response = receive.Response


def _uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and parsed.variant == uuid.RFC_4122 and str(parsed) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _decimal(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(DECIMAL_RE.fullmatch(value)) and int(value) <= maximum


def _target(value: object) -> bool:
    return (receive._text(value, 256) and not value.endswith("@chatroom")
            and value != "filehelper" and not value.startswith("weixin"))


def _body(target: str) -> bytes:
    return json.dumps({"threadId": target, "text": TEST_TEXT}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


class RequestPolicy:
    """Exact method, path, body and header allowlist for one bound submission."""

    def __init__(self, account: str):
        if not isinstance(account, str) or not receive.ACCOUNT_RE.fullmatch(account):
            raise SendError("invalid_arguments")
        self._account = account
        self._post_path = "/v1/accounts/" + account + "/sends"
        self._body = None
        self._headers = None
        self._poll_path = None
        self._post_attempted = False

    def bind(self, target: str, key: str) -> None:
        if self._body is not None or not _target(target) or not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise SendError("request_not_allowed")
        self._body = _body(target)
        self._headers = {"Content-Type": "application/json", "Idempotency-Key": key}

    def authorize(self, method: str, path: str, *, headers=None, body=None) -> None:
        if (method == "POST" and path == self._post_path and self._body is not None
                and body == self._body and type(body) is bytes and headers == self._headers
                and type(headers) is dict):
            return
        if (method == "GET" and self._poll_path is not None and path == self._poll_path
                and headers is None and body is None):
            return
        raise SendError("request_not_allowed")

    def start_submission(self) -> None:
        if self._post_attempted or self._body is None:
            raise SendError("request_not_allowed")
        self._post_attempted = True

    def bind_send(self, send_id: str) -> str:
        if not self._post_attempted or self._poll_path is not None or not _uuid4(send_id):
            raise SendError("request_not_allowed")
        self._poll_path = self._post_path + "/" + send_id
        return self._poll_path


class LoopbackSendTransport:
    """Raw loopback HTTP: no proxies, redirects, extra APIs or POST retries."""

    def __init__(self, policy: RequestPolicy, token: str, *, port: int = 26174):
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or type(port) is not int or not 1 <= port <= 65535):
            raise SendError("invalid_arguments")
        self._policy, self._token, self._port = policy, token, port
        self._post_attempted = False

    def request(self, method: str, path: str, *, timeout: float, headers=None, body=None) -> Response:
        self._policy.authorize(method, path, headers=headers, body=body)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= REQUEST_TIMEOUT_SECONDS):
            raise SendError("invalid_arguments")
        if method == "POST":
            if self._post_attempted:
                raise SendError("request_not_allowed")
            self._post_attempted = True
        connection = None
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=timeout)
            request_headers = {"Authorization": "Bearer " + self._token,
                               "Accept": "application/json", "Accept-Encoding": "identity"}
            if headers is not None:
                request_headers.update(headers)
            if method == "POST":
                connection.request(method, path, body=body, headers=request_headers)
            else:
                connection.request(method, path, headers=request_headers)
            incoming = connection.getresponse()
            response_headers = {}
            for key, value in incoming.getheaders():
                key = key.lower()
                if key in {"content-length", "content-type", "content-encoding"}:
                    if key in response_headers or not isinstance(value, str):
                        raise SendError("invalid_response")
                    response_headers[key] = value
            length = response_headers.get("content-length")
            if length is not None and (not re.fullmatch(r"[0-9]+", length)
                                       or len(length) > 10 or int(length) > JSON_LIMIT):
                raise SendError("body_too_large")
            if 300 <= incoming.status < 400:
                raise SendError("redirect_rejected")
            if response_headers.get("content-encoding", "").lower() not in {"", "identity"}:
                raise SendError("content_encoding_rejected")
            response_body = incoming.read(JSON_LIMIT + 1)
            if not isinstance(response_body, bytes) or len(response_body) > JSON_LIMIT:
                raise SendError("body_too_large")
            if length is not None and len(response_body) != int(length):
                raise SendError("invalid_response")
            return Response(incoming.status, response_headers, response_body)
        except SendError:
            raise
        except TimeoutError:
            raise SendError("transport_timeout") from None
        except ConnectionError:
            raise SendError("transport_connection_failed") from None
        except http.client.HTTPException:
            raise SendError("transport_protocol_failed") from None
        except UnicodeError:
            raise SendError("transport_encoding_failed") from None
        except Exception:
            raise SendError("transport_failed") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


class ContactSendClient:
    POLICY_TYPE = RequestPolicy
    valid_target = staticmethod(_target)

    def __init__(self, account: str, token: str, *, port: int = 26174, transport=None,
                 lookup=None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._policy = self.POLICY_TYPE(account)
        native = LoopbackSendTransport(self._policy, token, port=port)
        if not callable(clock) or not callable(sleep) or (lookup is not None and not callable(lookup)):
            raise SendError("invalid_arguments")
        self._account = account
        self._transport = native if transport is None else transport
        self._clock, self._sleep = clock, sleep
        # Only _target is reused: run() would enter the receive/event workflow.
        reader = receive.ContactReceiveClient(account, token, port=port, transport=transport,
                                              clock=clock, sleep=sleep)
        self._lookup = reader._target if lookup is None else lookup
        self._used = self._submitted = self._server_confirmed = False
        self._send_id = self._client_id = None
        self._last_state = None

    def _emit(self, emit, state: str) -> None:
        if state not in EMITTED_STATES:
            raise SendError("invalid_response")
        if state != self._last_state:
            try:
                emit({"stage": "send_state", "state": state})
            except Exception:
                raise SendError("callback_failed") from None
            self._last_state = state

    def _new_key(self):
        return uuid.uuid4().hex

    def _request(self, method: str, path: str, deadline: float, *, headers=None, body=None) -> dict:
        self._policy.authorize(method, path, headers=headers, body=body)
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise SendError("deadline_exceeded")
        if method == "POST":
            self._policy.start_submission()
            # Set before calling the transport: failures do not prove non-delivery.
            self._submitted = True
        try:
            options = {"timeout": min(REQUEST_TIMEOUT_SECONDS, remaining)}
            if method == "POST":
                options.update(headers=headers, body=body)
            response = self._transport.request(method, path, **options)
        except (SendError, receive.ContactError):
            raise
        except Exception:
            raise SendError("transport_failed") from None
        if self._clock() >= deadline:
            raise SendError("deadline_exceeded")
        if (not isinstance(response, Response) or type(response.status) is not int
                or response.status != (202 if method == "POST" else 200)):
            raise SendError("invalid_response")
        if receive._header(response.headers, "content-encoding").lower() not in {"", "identity"}:
            raise SendError("content_encoding_rejected")
        if receive._header(response.headers, "content-type").split(";", 1)[0].strip().lower() != "application/json":
            raise SendError("invalid_content_type")
        return receive._object(response.body)

    def _intent(self, value: dict, target: str) -> str:
        essential = {"id", "accountId", "destination", "state", "clientId", "serverId"}
        allowed = essential | {"localMessageId", "errorCode", "createdAt"}
        if not essential <= value.keys() or not value.keys() <= allowed:
            raise SendError("invalid_response")
        send_id, client_id, state = value["id"], value["clientId"], value["state"]
        if (not _uuid4(send_id) or (self._send_id is not None and send_id != self._send_id)
                or value["accountId"] != self._account or value["destination"] != target
                or not isinstance(state, str) or state not in STATES):
            raise SendError("invalid_response")
        if client_id not in (None, "") and not _decimal(client_id, 2**32 - 1):
            raise SendError("invalid_response")
        if self._client_id is not None and client_id != self._client_id:
            raise SendError("invalid_response")
        server_id = value["serverId"]
        if server_id is not None and not _decimal(server_id, 2**64 - 1):
            raise SendError("invalid_response")
        if state == "accepted" and (not _decimal(client_id, 2**32 - 1)
                                    or not _decimal(server_id, 2**64 - 1)):
            raise SendError("invalid_response")
        for key, maximum in (("localMessageId", 512), ("errorCode", 256), ("createdAt", 128)):
            if key in value and value[key] is not None and not receive._text(value[key], maximum):
                raise SendError("invalid_response")
        if self._send_id is None:
            self._send_id = send_id
            self._policy.bind_send(send_id)
        if client_id not in (None, ""):
            self._client_id = client_id
        return state

    def _terminal(self, state: str, emit) -> SendResult | None:
        if state == "accepted":
            self._server_confirmed = True
            result = SendResult(True, "ok", True, True)
        elif state in TERMINAL_CODES:
            result = SendResult(False, TERMINAL_CODES[state], True, False)
        else:
            self._emit(emit, state)
            return None
        # Notification failures cannot erase a validated terminal server result.
        try:
            self._emit(emit, state)
        except SendError:
            pass
        return result

    def run(self, deadline: float, emit: Callable[[dict], None]) -> SendResult:
        if self._used:
            return SendResult(False, "client_already_used", self._submitted, self._server_confirmed)
        self._used = True
        try:
            if (type(deadline) not in (int, float) or not math.isfinite(deadline) or not callable(emit)):
                raise SendError("invalid_arguments")
            if self._clock() >= deadline:
                raise SendError("deadline_exceeded")
            resolved = self._lookup(deadline)
            if not isinstance(resolved, tuple) or len(resolved) != 2 or not self.valid_target(resolved[0]):
                raise SendError("invalid_contacts")
            target = resolved[0]
            self._emit(emit, "target_matched")
            if self._clock() >= deadline:
                raise SendError("deadline_exceeded")
            key = self._new_key()
            self._policy.bind(target, key)
            self._emit(emit, "submitting")
            value = self._request("POST", self._policy._post_path, deadline,
                                  headers=dict(self._policy._headers), body=self._policy._body)
            state = self._intent(value, target)
            result = self._terminal(state, emit)
            if result is not None:
                return result
            poll_deadline = min(deadline, self._clock() + MAX_POLL_SECONDS)
            while self._clock() < poll_deadline:
                value = self._request("GET", self._policy._poll_path, poll_deadline)
                state = self._intent(value, target)
                result = self._terminal(state, emit)
                if result is not None:
                    return result
                remaining = poll_deadline - self._clock()
                if remaining > 0:
                    self._sleep(min(1.0, remaining))
            raise SendError("deadline_exceeded")
        except (SendError, receive.ContactError) as error:
            code = "outcome_unknown" if self._submitted else error.code
            if code not in RESULT_CODES:
                code = "invalid_response"
            return SendResult(False, code, self._submitted, self._server_confirmed)
        except Exception:
            return SendResult(False, "outcome_unknown" if self._submitted else "invalid_response",
                              self._submitted, self._server_confirmed)
