#!/usr/bin/env python3
"""Own only this project's bounded session runtime; never retry a fresh login.

The offline retention proof uses disposable empty accounts. Durable SDK files
are root-private, outside the project. No command in this controller sends.
"""
from __future__ import annotations

import base64
try:
    import fcntl
except ImportError:  # Portable offline tests; actual execution requires Linux.
    fcntl = None
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
STATE = Path("/var/lib/astrbot-wechat-session")
CONTROL = Path("/run/astrbot-wechat-session")
OWNER = "astrbot-wechat-session-owner.service"
RUNTIME_SECONDS = 3600
LONG_RUNTIME_SECONDS = 604800
CHECKPOINT_SECONDS = 300
MAX_JSON = 65536
MAX_PNG = 1024 * 1024
ACTIONS = frozenset({"status", "resume", "initialize", "receive-contact", "stop"})
EXIT_CODES = frozenset({"ok", "linux_root_required", "arguments", "state_unsafe",
    "control_unsafe", "control_unavailable", "owner_running", "owner_unavailable",
    "owner_start_failed", "owner_start_timeout", "busy", "request_timeout",
    "request_failed", "runtime_failed", "checkpoint_failed", "cleanup_failed",
    "retention_failed", "fresh_login_blocked", "interrupted"})


class RuntimeErrorCode(Exception):
    def __init__(self, code):
        self.code = code if code in EXIT_CODES else "runtime_failed"
        super().__init__(self.code)


def companion(name):
    spec = importlib.util.spec_from_file_location("_session_owner_" + name, HERE / (name + ".py"))
    if spec is None or spec.loader is None:
        raise RuntimeErrorCode("runtime_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def wire(record):
    raw = json.dumps(record, sort_keys=True, ensure_ascii=True)
    print("AWP-BEGIN")
    for offset in range(0, len(raw), 64):
        print("AWP " + raw[offset:offset + 64])
    print("AWP-END", flush=True)


def private_directory(path):
    # All callers use fixed absolute paths and fixed, verified private parents.
    if not path.is_absolute():
        raise RuntimeErrorCode("state_unsafe")
    for parent in reversed(path.parents):
        if parent.is_symlink() or not parent.is_dir():
            raise RuntimeErrorCode("state_unsafe")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeErrorCode("state_unsafe")


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value


def read_private(path, maximum=MAX_JSON):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or not 0 < info.st_size <= maximum):
            raise RuntimeErrorCode("control_unsafe")
        value = os.read(descriptor, maximum + 1)
        if len(value) != info.st_size:
            raise RuntimeErrorCode("control_unsafe")
        return value
    finally:
        os.close(descriptor)


def read_json(path):
    def reject(_):
        raise ValueError("constant")
    return json.loads(read_private(path).decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=reject)


def atomic_private(path, data):
    if len(data) > MAX_JSON and path.name != "login-qr.png":
        raise RuntimeErrorCode("control_unsafe")
    if path.name == "login-qr.png" and len(data) > MAX_PNG:
        raise RuntimeErrorCode("control_unsafe")
    temporary = path.with_name("." + path.name + ".next")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_private(path, json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))


def owner_active():
    result = subprocess.run(["/usr/bin/systemctl", "show", OWNER, "--property=ActiveState", "--value"],
                            capture_output=True, timeout=4, check=False)
    return result.returncode == 0 and result.stdout.strip() in {b"active", b"activating", b"deactivating"}


def request_value(value):
    if (not isinstance(value, dict) or set(value) != {"sequence", "action", "allowFreshLogin"}
            or type(value["sequence"]) is not int or not 1 <= value["sequence"] <= 1000000
            or not isinstance(value["action"], str) or value["action"] not in ACTIONS
            or type(value["allowFreshLogin"]) is not bool
            or (value["allowFreshLogin"] and value["action"] != "initialize")):
        raise RuntimeErrorCode("control_unsafe")
    return value


class PublicState:
    """Bounded, non-sensitive progress. QR bytes live in a separate private file."""
    def __init__(self):
        self.value = {"schemaVersion": 1, "sequence": 0, "done": False, "busy": True,
                      "ready": False, "connected": False, "accounts": 0, "ok": False,
                      "code": "starting", "recordCount": 0, "records": [], "qrVersion": 0}

    def append(self, envelope):
        sequence, record = envelope["sequence"], envelope["record"]
        if sequence != self.value["sequence"]:
            return
        stage = record["stage"]
        if stage == "login_qr":
            # Never put base64 in status JSON or systemd's journal.
            raw = base64.b64decode(record["png_base64"], validate=True)
            if not 0 < len(raw) <= MAX_PNG or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeErrorCode("control_unsafe")
            atomic_private(CONTROL / "login-qr.png", raw)
            self.value["qrVersion"] += 1
            return
        if stage in {"runtime_ready", "runtime_status"}:
            self.value.update(accounts=record["accounts"], connected=record["connected"])
            if stage == "runtime_ready":
                self.value.update(ready=True, busy=False, done=True, ok=True, code="ready")
        if stage == "runtime_result":
            self.value.update(done=True, busy=False, ok=record["ok"], code=record["code"])
        if stage == "runtime_stopped":
            self.value.update(ready=False, connected=False)
        self.value["recordCount"] += 1
        self.value["records"].append({"index": self.value["recordCount"], "record": record})
        self.value["records"] = self.value["records"][-32:]

    def begin(self, request):
        self.value.update(sequence=request["sequence"], busy=True, done=False, ok=False,
                          code="working", recordCount=0, records=[], qrVersion=0)
        (CONTROL / "login-qr.png").unlink(missing_ok=True)

    def publish(self):
        write_json(CONTROL / "status.json", self.value)


def wait_ready(host, maximum=25):
    deadline = time.monotonic() + maximum
    ready_record = None
    while not host.ready and time.monotonic() < deadline:
        for envelope in host.pump(0.1):
            record = envelope["record"]
            if record["stage"] == "runtime_ready":
                ready_record = record
            elif record["stage"] == "runtime_result" and not record["ok"]:
                raise RuntimeErrorCode("runtime_failed")
        if host.stopped:
            raise RuntimeErrorCode("runtime_failed")
    if not host.ready or ready_record is None:
        raise RuntimeErrorCode("owner_start_timeout")
    return ready_record


def cleanup_ok(receipt):
    """Accept only explicit success and all three host-baseline confirmations."""
    return (isinstance(receipt, dict) and receipt.get("ok") is True
            and all(receipt.get(key) is True for key in (
                "host_namespace_unchanged", "host_network_unchanged", "business_processes_unchanged")))


def remove_test_state(path):
    # Fixed private selftest directory, fixed slots only; never recursively delete.
    private = path / "private"
    if private.exists():
        for slot in ("current.zip", "previous.zip", ".next.zip"):
            candidate = private / slot
            if candidate.exists() or candidate.is_symlink():
                info = candidate.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                    raise RuntimeErrorCode("state_unsafe")
                candidate.unlink()
        private.rmdir()
    path.rmdir()


def retention_proof():
    if owner_active():
        raise RuntimeErrorCode("owner_running")
    private_directory(STATE)
    private_directory(STATE / "selftests")
    state_path = STATE / "selftests" / ("empty-" + secrets.token_hex(8))
    private_directory(state_path)
    host = None
    passed = False
    receipts = []
    try:
        module = companion("session_runtime_host")
        host = module.SessionHost(state_path, offline=True, seconds=90)
        host.start()
        first = wait_ready(host)
        if first["accounts"] != 0 or first["restored"]:
            raise RuntimeErrorCode("retention_failed")
        before = host.fingerprints()
        info = host.checkpoint()
        wire({"stage": "retention_checkpoint", "ok": True, "files": info.files,
              "bytes": info.bytes, "archive_bytes": info.archive_bytes})
        receipt = host.close()
        host = None
        receipts.append(receipt)
        if not cleanup_ok(receipt):
            raise RuntimeErrorCode("cleanup_failed")
        wire({"stage": "retention_first_stop", **receipt})
        host = module.SessionHost(state_path, offline=True, seconds=90)
        host.start()
        second = wait_ready(host)
        after = host.fingerprints()
        checks = {name + "_retained": isinstance(before.get(name), bytes)
                  and len(before[name]) == 32 and before[name] == after.get(name)
                  for name in ("api_token", "storage_key", "cursor_key")}
        checks.update(restored=second["restored"] is True, accounts_empty=second["accounts"] == 0,
                      registry_present=isinstance(after.get("registry"), bytes), no_qr=True, no_login=True)
        if not all(checks.values()):
            raise RuntimeErrorCode("retention_failed")
        # Exercise the live freeze/snapshot/thaw path again on restored files.
        host.checkpoint()
        receipt = host.close()
        host = None
        receipts.append(receipt)
        if not cleanup_ok(receipt):
            raise RuntimeErrorCode("cleanup_failed")
        wire({"stage": "retention_second_stop", **receipt})
        wire({"stage": "retention_result", "ok": True, "checks": checks})
        passed = True
        return 0
    finally:
        if host is not None:
            receipt = host.close()
            wire({"stage": "retention_abort_cleanup", **receipt})
        # These are empty-account synthetic files, never production session data.
        remove_test_state(state_path)
        if not passed:
            wire({"stage": "retention_result", "ok": False})


def serve(offline, seconds=RUNTIME_SECONDS):
    private_directory(STATE)
    private_directory(CONTROL)
    # Every daemon boot starts with an empty command mailbox. Even a direct
    # internal invocation cannot replay an initialize left by a previous boot.
    (CONTROL / "request.json").unlink(missing_ok=True)
    (CONTROL / "login-qr.png").unlink(missing_ok=True)
    status = PublicState()
    status.publish()
    host = companion("session_runtime_host").SessionHost(STATE / "checkpoints", offline=offline,
                                                        seconds=seconds)
    interrupted = False
    def stop_signal(_number, _frame):
        nonlocal interrupted
        interrupted = True
    handlers = {number: signal.signal(number, stop_signal) for number in (signal.SIGTERM, signal.SIGINT)}
    next_checkpoint = time.monotonic() + CHECKPOINT_SECONDS
    deadline = time.monotonic() + seconds - 15
    previous_done = False
    operational_failure = None
    try:
        host.start()
        status.value['cacheMaintenance'] = host.cache_maintenance
        while time.monotonic() < deadline and not interrupted:
            for envelope in host.pump(0.15):
                status.append(envelope)
            # No snapshot of a partial or rejected restoration.
            completed = status.value["done"] and not previous_done
            if host.ready and not host.stopped and (completed or time.monotonic() >= next_checkpoint):
                try:
                    host.checkpoint()
                except Exception:
                    status.value.update(ok=False, code="checkpoint_failed", done=True, busy=False)
                    raise RuntimeErrorCode("checkpoint_failed") from None
                next_checkpoint = time.monotonic() + CHECKPOINT_SECONDS
            previous_done = status.value["done"]
            status.publish()
            if host.stopped:
                if status.value["ok"] is False:
                    operational_failure = status.value["code"]
                break
            if not status.value["busy"]:
                try:
                    request = request_value(read_json(CONTROL / "request.json"))
                except FileNotFoundError:
                    request = None
                if request is not None and request["sequence"] > status.value["sequence"]:
                    status.begin(request)
                    status.publish()
                    previous_done = False
                    host.request(request["sequence"], request["action"], allow_fresh=request["allowFreshLogin"])
        return 0 if operational_failure is None else 1
    except RuntimeErrorCode as error:
        operational_failure = error.code
        raise
    except Exception:
        operational_failure = "runtime_failed"
        raise
    finally:
        receipt = host.close()
        clean = cleanup_ok(receipt)
        status.value.update(ready=False, connected=False, done=True, busy=False,
                            ok=clean and operational_failure is None,
                            code=operational_failure or ("stopped" if clean else "cleanup_failed"))
        status.value["cleanup"] = receipt
        (CONTROL / "login-qr.png").unlink(missing_ok=True)
        status.publish()
        for number, handler in handlers.items():
            signal.signal(number, handler)


def start_owner(offline, seconds=RUNTIME_SECONDS):
    if type(seconds) is not int or seconds not in {RUNTIME_SECONDS, LONG_RUNTIME_SECONDS} or offline and seconds > RUNTIME_SECONDS:
        raise RuntimeErrorCode('arguments')
    if owner_active():
        raise RuntimeErrorCode("owner_running")
    private_directory(STATE)
    private_directory(CONTROL)
    # Stale control files have no account material; production checkpoints stay.
    for name in ("request.json", "status.json", "login-qr.png", ".request.json.next",
                 ".status.json.next", ".login-qr.png.next"):
        (CONTROL / name).unlink(missing_ok=True)
    arguments = ["/usr/bin/systemd-run", "--quiet", "--collect", "--unit=" + OWNER,
                 "--property=MemoryMax=64M", "--property=MemoryHigh=48M", "--property=MemorySwapMax=0",
                 "--property=CPUQuota=5%", "--property=TasksMax=16", "--property=Nice=19",
                 "--property=RuntimeMaxSec=" + str(seconds + 60), "--property=TimeoutStopSec=25",
                 "--property=KillMode=control-group", "--property=UMask=0077",
                 "--property=LimitCORE=0", "--property=LimitFSIZE=150994944",
                 "--property=ProtectKernelModules=yes", "--property=StandardOutput=null",
                 "--property=StandardError=null", "/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve()),
                 "--serve-offline" if offline else "--serve-long" if seconds == LONG_RUNTIME_SECONDS else "--serve-online"]
    result = subprocess.run(arguments, capture_output=True, timeout=5, check=False)
    if result.returncode:
        raise RuntimeErrorCode("owner_start_failed")
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        try:
            value = read_json(CONTROL / "status.json")
            if value.get("ready") is True:
                wire({"stage": "session_owner", "ok": True, "offline": offline,
                      "accounts": value["accounts"], "connected": value["connected"],
                      "runtime_seconds": seconds})
                return 0
            if value.get("done") is True and not value.get("ok"):
                raise RuntimeErrorCode("runtime_failed")
        except FileNotFoundError:
            pass
        time.sleep(0.2)
    raise RuntimeErrorCode("owner_start_timeout")


def client(action, allow_fresh=False):
    private_directory(CONTROL)
    if action == "status":
        try:
            value = read_json(CONTROL / "status.json")
        except FileNotFoundError:
            value = {"ready": False, "connected": False, "accounts": 0, "code": "not_started"}
        wire({"stage": "session_status", "owner_active": owner_active(), **value})
        return 0
    if action == "initialize" and not allow_fresh:
        raise RuntimeErrorCode("fresh_login_blocked")
    if not owner_active():
        raise RuntimeErrorCode("owner_unavailable")
    lock_path = CONTROL / "command.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RuntimeErrorCode("control_unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeErrorCode("busy") from None
        previous = read_json(CONTROL / "status.json")
        if previous.get("busy"):
            raise RuntimeErrorCode("busy")
        sequence = previous["sequence"] + 1
        request = request_value({"sequence": sequence, "action": action, "allowFreshLogin": allow_fresh})
        write_json(CONTROL / "request.json", request)
        maximum = 650 if action == "receive-contact" else 360 if action in {"resume", "initialize"} else 90
        deadline, seen, qr_seen = time.monotonic() + maximum, 0, 0
        while time.monotonic() < deadline:
            current = read_json(CONTROL / "status.json")
            if current["sequence"] == sequence:
                for item in current["records"]:
                    if item["index"] > seen:
                        wire(item["record"])
                        seen = item["index"]
                if action == "initialize" and current["qrVersion"] > qr_seen:
                    try:
                        raw = read_private(CONTROL / "login-qr.png", MAX_PNG)
                    except FileNotFoundError:
                        raw = None
                    if raw is not None:
                        wire({"stage": "login_qr", "png_base64": base64.b64encode(raw).decode("ascii")})
                        qr_seen = current["qrVersion"]
                if current["done"]:
                    if action == "stop" and ("cleanup" not in current or owner_active()):
                        time.sleep(0.2)
                        continue
                    return 0 if current["ok"] else 1
            if not owner_active():
                raise RuntimeErrorCode("owner_unavailable")
            time.sleep(0.2)
        # A CLI timeout never retries or cancels a native action, nor starts login.
        raise RuntimeErrorCode("request_timeout")
    finally:
        os.close(descriptor)


def parse_arguments(arguments):
    if arguments in (["--retention-proof"], ["--serve-offline"], ["--serve-online"], ["--serve-long"], ["start-long"],
                     ["start"], ["start-online"], ["status"], ["resume"], ["receive-contact"], ["stop"]):
        return arguments[0], False
    if arguments == ["initialize", "--allow-fresh-login"]:
        return "initialize", True
    if arguments == ["initialize"]:
        raise RuntimeErrorCode("fresh_login_blocked")
    raise RuntimeErrorCode("arguments")


def main(arguments=None):
    try:
        action, allow = parse_arguments(sys.argv[1:] if arguments is None else arguments)
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeErrorCode("linux_root_required")
        os.umask(0o077)
        if action == "--retention-proof":
            return retention_proof()
        if action.startswith("--serve-"):
            return serve(action == "--serve-offline", seconds=LONG_RUNTIME_SECONDS if action == '--serve-long' else RUNTIME_SECONDS)
        if action == 'start-long':
            return start_owner(False, seconds=LONG_RUNTIME_SECONDS)
        if action in {"start", "start-online"}:
            return start_owner(action == "start")
        return client(action, allow)
    except RuntimeErrorCode as error:
        code = error.code
    except KeyboardInterrupt:
        code = "interrupted"
    except Exception:
        code = "runtime_failed"
    wire({"stage": "session_owner_result", "ok": False, "code": code})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
