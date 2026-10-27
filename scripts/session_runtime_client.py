#!/usr/bin/env python3
"""An isolated, persistent native client controlled by a root-owned mailbox.

Only the parent owns durable snapshots. This guest keeps its private tmpfs and
native process alive across explicitly requested actions, and never sends.
"""
from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import time


def _companion(name: str):
    # Explicit paths also work under python -I, without adding a writable cwd.
    path = Path(__file__).resolve().parent / (name + ".py")
    spec = importlib.util.spec_from_file_location("_session_runtime_" + name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("companion_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


login = _companion("login_client_probe")
empty, qr = login.empty, login.qr
resume = _companion("session_resume_protocol")
receive = _companion("contact_receive_protocol")
state_store = _companion("session_state_store")
CONTROLLER = Path("/opt/session_runtime_client.py")
CONFIG = Path("/opt/session_config.json")
COMMAND = Path("/opt/command.json")
RESTORE = Path("/opt/restore.zip")
ACCOUNT = "wechat-primary"
POLL_SECONDS = 0.25
HOLD_SECONDS = 15.0
JSON_LIMIT = 4096
empty.CONTROLLER = CONTROLLER
# login's existing cgroup prefix and exact TAP/unit checks stay unchanged.
RUNTIME_CODES = frozenset({
    "ok", "arguments", "config_failed", "metadata_failed", "isolation_failed",
    "restore_failed", "pending_outbox_review", "outbox_review_failed",
    "native_spawn_failed", "startup_failed", "accounts_failed", "status_failed",
    "fresh_login_blocked", "existing_account_requires_resume", "account_unavailable",
    "offline_operation_blocked", "connection_required",
    "login_failed", "resume_failed", "receive_failed", "command_failed",
    "runtime_deadline", "native_exited", "interrupted", "cleanup_failed", "runtime_stopped",
} | resume.RESULT_CODES | receive.RESULT_CODES | login.RELAY_CODES)


class RuntimeFailure(Exception):
    def __init__(self, code: str):
        self.code = code if code in RUNTIME_CODES else "startup_failed"
        super().__init__(self.code)


class Interrupted(BaseException):
    pass


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("constant")


def _readonly_file(path: Path, *, maximum: int | None = None,
                   owner_uid: int = 0, mode: int | None = None) -> int:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                         getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(descriptor)
        mounts = empty.parse_mounts(Path("/proc/self/mountinfo").read_text())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_nlink != 1
                or info.st_mode & 0o222 or info.st_size <= 0
                or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
                or (maximum is not None and info.st_size > maximum)
                or not empty._readonly(mounts, str(path))):
            raise ValueError("public_file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_json(path: Path) -> dict:
    descriptor = _readonly_file(path, maximum=JSON_LIMIT)
    try:
        raw = os.read(descriptor, JSON_LIMIT + 1)
        if len(raw) > JSON_LIMIT:
            raise ValueError("public_file")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError("public_file")
        return value
    finally:
        os.close(descriptor)


def validate_config(value: object) -> dict:
    keys = {"schemaVersion", "restore", "offline", "runtimeSeconds", "account"}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
            or type(value["restore"]) is not bool or type(value["offline"]) is not bool
            or type(value["runtimeSeconds"]) is not int or not 1 <= value["runtimeSeconds"] <= 604800
            or value["offline"] and value["runtimeSeconds"] > 3600
            or value["account"] != ACCOUNT):
        raise ValueError("config")
    return value


def validate_command(value: object) -> dict:
    keys = {"schemaVersion", "sequence", "action", "allowFreshLogin"}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
            or type(value["sequence"]) is not int or value["sequence"] < 0
            or not isinstance(value["action"], str)
            or value["action"] not in {"status", "resume", "initialize", "receive-contact", "stop", "release"}
            or type(value["allowFreshLogin"]) is not bool):
        raise ValueError("command")
    return value


def read_config() -> dict:
    return validate_config(_read_json(CONFIG))


def read_command() -> dict:
    return validate_command(_read_json(COMMAND))


def emit(sequence: int, record: dict) -> None:
    print(json.dumps({"sequence": sequence, "record": record},
                     separators=(",", ":"), ensure_ascii=True), flush=True)


def result(sequence: int, ok: bool, code: str) -> None:
    emit(sequence, {"stage": "runtime_result", "ok": ok,
                    "code": code if code in RUNTIME_CODES else "command_failed"})


def _account_get(token: str) -> dict:
    """A single literal GET route, with no auth-action or caller-supplied path."""
    connection = http.client.HTTPConnection("127.0.0.1", empty.PORT, timeout=0.8)
    try:
        connection.request("GET", "/v1/accounts/" + ACCOUNT, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > empty.MAX_BODY):
            raise RuntimeFailure("status_failed")
        body = response.read(empty.MAX_BODY + 1)
        if (response.status != 200 or len(body) > empty.MAX_BODY
                or response.getheader("Content-Encoding", "").lower() not in {"", "identity"}
                or response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"):
            raise RuntimeFailure("status_failed")
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=_invalid_constant)
        if (not isinstance(value, dict) or value.get("id") != ACCOUNT
                or type(value.get("authenticated")) is not bool
                or value.get("connection") not in {"online", "reconnecting", "disconnected"}):
            raise RuntimeFailure("status_failed")
        return value
    finally:
        connection.close()


def _connected(value: dict) -> bool:
    return value.get("authenticated") is True and value.get("connection") == "online"


def review_restored_outbox() -> None:
    """The pinned native outbox schema is checked read-only before process spawn."""
    try:
        accounts = empty.DATA / "accounts"
        if not accounts.exists():
            return
        entries = list(accounts.iterdir())
        if len(entries) > 1:
            raise RuntimeFailure("accounts_failed")
        for account in entries:
            if account.is_symlink() or not account.is_dir():
                raise RuntimeFailure("outbox_review_failed")
            database = account / "agent" / "control.sqlite"
            info = database.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()):
                raise RuntimeFailure("outbox_review_failed")
            # mode=ro sees WAL contents. immutable=1 would miss queued WAL rows.
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
            try:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                tables = dict(connection.execute(
                    "SELECT name,type FROM sqlite_master WHERE name IN ('outbox','media_jobs')"))
                if tables != {"outbox": "table", "media_jobs": "table"}:
                    raise RuntimeFailure("outbox_review_failed")
                pending = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM outbox WHERE state IS NULL "
                    "OR state NOT IN ('accepted','failed','cancelled'))").fetchone()
                media = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM media_jobs WHERE kind IS NULL "
                    "OR kind NOT IN ('send','download') OR (kind='send' AND "
                    "(state IS NULL OR state NOT IN ('accepted','failed','cancelled','unsupported','expired'))))").fetchone()
                if pending != (0,) or media != (0,):
                    raise RuntimeFailure("pending_outbox_review")
            finally:
                connection.close()
    except RuntimeFailure:
        raise
    except Exception:
        raise RuntimeFailure("outbox_review_failed") from None


def wait_restore_archive() -> None:
    """Root hands this archive only to the validated guest's temporary UID."""
    deadline = time.monotonic() + 10.0
    guest_uid = os.geteuid()
    while time.monotonic() < deadline:
        info = RESTORE.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0
                or (info.st_uid, mode) not in {(0, 0), (guest_uid, 0), (guest_uid, 0o400)}):
            raise RuntimeFailure("restore_failed")
        if (info.st_uid, mode) == (guest_uid, 0o400):
            descriptor = _readonly_file(RESTORE, owner_uid=guest_uid, mode=0o400)
            os.close(descriptor)
            return
        time.sleep(POLL_SECONDS)
    raise RuntimeFailure("restore_failed")


class RuntimeController:
    def __init__(self, configuration: dict):
        self.configuration = configuration
        self.deadline = time.monotonic() + configuration["runtimeSeconds"]
        self.process = None
        self.token = None
        self.prepared = False
        self.last_sequence = -1
        self.sequence = 0
        self.stop_attempted = False
        self.stop_ok = True
        self.stopped_emitted = False

    def inventory(self) -> tuple[int, bool, bool]:
        try:
            status, value = empty.get_json("/v1/accounts", self.token)
        except Exception:
            raise RuntimeFailure("accounts_failed") from None
        if not isinstance(value, dict):
            raise RuntimeFailure("accounts_failed")
        items = value.get("items")
        if (status != 200 or not isinstance(items, list) or len(items) > 1
                or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in items)):
            raise RuntimeFailure("accounts_failed")
        if items and items[0]["id"] != ACCOUNT:
            raise RuntimeFailure("existing_account_mismatch")
        available = bool(items) and items[0]["id"] == ACCOUNT
        try:
            connected = _connected(_account_get(self.token)) if available else False
        except RuntimeFailure:
            raise
        except Exception:
            raise RuntimeFailure("status_failed") from None
        return len(items), connected, available

    def start(self) -> None:
        try:
            metadata = None if self.configuration["offline"] else login.read_metadata()
        except Exception:
            raise RuntimeFailure("metadata_failed") from None
        try:
            checks = empty.validate_isolation() if self.configuration["offline"] else login.validate_isolation(metadata)
            if not checks or any(type(ok) is not bool or not ok for _, ok in checks):
                raise ValueError("isolation")
        except Exception:
            raise RuntimeFailure("isolation_failed") from None
        environment = empty.prepare_directories()
        environment["TOKIO_WORKER_THREADS"] = "1"
        self.prepared = True
        emit(0, {"stage": "runtime_isolated", "ok": True})
        if self.configuration["restore"]:
            try:
                wait_restore_archive()
                state_store.restore_archive(RESTORE, empty.DATA, archive_owner_uid=os.geteuid(),
                                            archive_mode=0o400)
                review_restored_outbox()
            except RuntimeFailure:
                raise
            except Exception:
                raise RuntimeFailure("restore_failed") from None
        try:
            self.process = subprocess.Popen(
                [str(empty.BINARY), "--server-config", str(empty.CONFIG), "--data-dir", str(empty.DATA),
                 "--listen", "127.0.0.1:26174", "serve"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, env=environment, cwd=str(empty.BASE))
        except Exception:
            raise RuntimeFailure("native_spawn_failed") from None
        empty.wait_for_health(self.process)
        self.token = empty.read_token()
        accounts, connected, _ = self.inventory()
        emit(0, {"stage": "runtime_ready", "ok": True,
                 "restored": self.configuration["restore"], "accounts": accounts, "connected": connected})

    def stop(self) -> bool:
        if not self.stop_attempted:
            self.stop_attempted = True
            if self.process is not None:
                try:
                    self.stop_ok = empty.stop_child(self.process) is True
                except BaseException:
                    self.stop_ok = False
        return self.stop_ok

    def stopped(self) -> None:
        if not self.stopped_emitted:
            emit(self.sequence, {"stage": "runtime_stopped", "ok": self.stop_ok})
            self.stopped_emitted = True

    def _relay_resume(self, record: dict) -> None:
        if (not isinstance(record, dict) or set(record) != {"stage", "state"}
                or record.get("stage") != "resume_state" or record.get("state") not in resume.ACTIVE_STATES):
            raise ValueError("record")
        emit(self.sequence, record)

    def _relay_contact(self, record: dict) -> None:
        if not isinstance(record, dict):
            raise ValueError("record")
        stage = record.get("stage")
        if stage == "contact_state":
            valid = set(record) == {"stage", "state"} and record["state"] == "target_matched"
        elif stage == "contact_waiting":
            valid = (set(record) == {"stage", "marker", "seconds"}
                     and isinstance(record["marker"], str) and receive.MARKER_RE.fullmatch(record["marker"])
                     and type(record["seconds"]) is int and 1 <= record["seconds"] <= receive.MAX_WAIT_SECONDS)
        elif stage == "contact_observation":
            counters = {"event_count", "target_incoming_count", "target_live_count", "text_checked"}
            valid = (set(record) == {"stage", "marker_matched"} | counters
                     and type(record["marker_matched"]) is bool
                     and all(type(record[key]) is int and 0 <= record[key] <= 20000 for key in counters))
        else:
            valid = False
        if not valid:
            raise ValueError("record")
        emit(self.sequence, record)

    def initialize(self, allow_fresh_login: bool) -> None:
        if not allow_fresh_login:
            result(self.sequence, False, "fresh_login_blocked")
            return
        accounts, _, _ = self.inventory()
        if accounts:
            result(self.sequence, False, "existing_account_requires_resume")
            return
        remaining = min(330.0, self.deadline - time.monotonic())
        if remaining <= 2:
            result(self.sequence, False, "runtime_deadline")
            return
        transport = qr.LoopbackHTTPTransport(ACCOUNT, self.token, port=empty.PORT, extended_window=True)
        client = qr.LoginClient(ACCOUNT, transport, total_timeout=remaining, extended_window=True)

        def on_qr(image):
            if (not isinstance(image.png, bytes) or len(image.png) > qr.PNG_LIMIT
                    or not image.png.startswith(qr.PNG_MAGIC)):
                raise ValueError("qr")
            emit(self.sequence, {"stage": "login_qr", "png_base64": base64.b64encode(image.png).decode("ascii")})

        def on_state(value):
            if value not in login.RELAY_STATES:
                raise ValueError("state")
            emit(self.sequence, {"stage": "login_state", "state": value})

        try:
            outcome = client.run(on_qr, on_state, keep_connected=True)
            good = outcome.state == "succeeded" and outcome.disconnected is False
            if good:
                # LoginClient validated this operation's success before handing
                # the connection back. Publish only public cached status.
                emit(self.sequence, {"stage": "runtime_status", "accounts": 1, "connected": True})
            result(self.sequence, good, "ok" if good else "login_failed")
        except qr.ProtocolError as error:
            code = error.code if error.code in login.RELAY_CODES else "login_failed"
            result(self.sequence, False, "cleanup_failed" if error.cleanup_failed else code)

    def execute(self, command: dict) -> str | None:
        command = validate_command(command)
        if command["sequence"] <= self.last_sequence:
            return None
        # Consume before work, so errors and a repeated mailbox never retry it.
        self.sequence = self.last_sequence = command["sequence"]
        action = command["action"]
        # The retention proof cannot accidentally exercise auth/network actions,
        # even if a valid but unintended command reaches its mailbox.
        if self.configuration["offline"] and action not in {"status", "stop", "release"}:
            result(self.sequence, False, "offline_operation_blocked")
            return None
        try:
            return self._execute_action(action, command)
        except RuntimeFailure as error:
            # A GET failure or rejected ordinary command does not end a valid
            # session. Malformed mailbox input is validated before this block.
            result(self.sequence, False, error.code)
            return None

    def _execute_action(self, action: str, command: dict) -> str | None:
        if action in {"stop", "release"}:
            good = self.stop()
            result(self.sequence, good, "ok" if good else "cleanup_failed")
            self.stopped()
            return action
        if action == "status":
            accounts, connected, _ = self.inventory()
            emit(self.sequence, {"stage": "runtime_status", "accounts": accounts, "connected": connected})
            result(self.sequence, True, "ok")
        elif action == "resume":
            outcome = resume.SessionResumeClient(ACCOUNT, self.token, port=empty.PORT).run(self.deadline, self._relay_resume)
            if (not isinstance(outcome, resume.ResumeResult) or type(outcome.ok) is not bool
                    or type(outcome.connected) is not bool or type(outcome.needs_initialization) is not bool
                    or outcome.code not in resume.RESULT_CODES or outcome.ok != outcome.connected):
                raise RuntimeFailure("resume_failed")
            if outcome.ok:
                # The resume protocol already checked the live account detail;
                # avoid another connect/login action just to update the owner.
                emit(self.sequence, {"stage": "runtime_status", "accounts": 1, "connected": True})
            result(self.sequence, outcome.ok, outcome.code)
        elif action == "initialize":
            self.initialize(command["allowFreshLogin"])
        elif action == "receive-contact":
            _, connected, available = self.inventory()
            if not available:
                result(self.sequence, False, "account_unavailable")
                return None
            if not connected:
                result(self.sequence, False, "connection_required")
                return None
            try:
                outcome = receive.ContactReceiveClient(ACCOUNT, self.token, port=empty.PORT).run(self.deadline, self._relay_contact)
                code = "ok" if outcome == "ok" else "receive_failed"
            except receive.ContactError as error:
                code = error.code if error.code in receive.RESULT_CODES else "receive_failed"
            result(self.sequence, code == "ok", code)
        return None

    def hold(self) -> None:
        deadline = time.monotonic() + HOLD_SECONDS
        while time.monotonic() < deadline:
            try:
                command = read_command()
                if command["sequence"] > self.last_sequence:
                    self.sequence = self.last_sequence = command["sequence"]
                    if command["action"] == "release":
                        result(self.sequence, self.stop_ok, "ok" if self.stop_ok else "cleanup_failed")
                        return
                    result(self.sequence, False, "runtime_stopped")
            except Exception:
                pass
            time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        result(0, False, "arguments")
        return 2
    try:
        configuration = read_config()
    except Exception:
        result(0, False, "config_failed")
        return 1
    runtime = RuntimeController(configuration)
    handlers = {}
    interrupted = False
    holding = False
    success = False
    completed_action = None
    code = "startup_failed"

    def interrupt(_number, _frame):
        nonlocal interrupted
        interrupted = True
        if not holding:
            raise Interrupted()

    try:
        for number in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
            if number is not None:
                handlers[number] = signal.signal(number, interrupt)
        runtime.start()
        code = "command_failed"
        while True:
            if interrupted:
                raise Interrupted()
            if time.monotonic() >= runtime.deadline:
                raise RuntimeFailure("runtime_deadline")
            if runtime.process.poll() is not None:
                raise RuntimeFailure("native_exited")
            action = runtime.execute(read_command())
            if interrupted:
                raise Interrupted()
            if action in {"stop", "release"}:
                completed_action = action
                success = runtime.stop_ok
                holding = True
                if action == "stop":
                    runtime.hold()
                break
            time.sleep(POLL_SECONDS)
    except Interrupted:
        code = "interrupted"
    except RuntimeFailure as error:
        code = error.code
    except Exception:
        pass  # Never serialize exception text, tokens, API bodies or identifiers.
    finally:
        holding = True
        stopped = runtime.stop()
        if not success and completed_action is None:
            result(runtime.sequence, False, code if stopped else "cleanup_failed")
            if runtime.prepared:
                runtime.stopped()
                runtime.hold()
        for number, handler in handlers.items():
            signal.signal(number, handler)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
