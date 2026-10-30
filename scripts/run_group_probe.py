#!/usr/bin/env python3
"""Check one named group through the existing bounded Native session.

Only the new short-lived reader is stopped. Native, its owner, transport and
slice are neither started nor stopped. Credentials travel in an anonymous pipe.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import stat
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent


def companion(name):
    spec = importlib.util.spec_from_file_location("_group_host_" + name, HERE / (name + ".py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("companion_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


session = companion("session_runtime_host")
legacy, tap, owned, require = session.legacy, session.tap, session.owned, session.require
OWNER = "astrbot-wechat-session-owner.service"
OWNER_GROUP = "/system.slice/" + OWNER
UNIT_RE = re.compile(r"astrbot-wechat-login-([0-9a-f]{12})\.service\Z", re.ASCII)
READER_PREFIX = "astrbot-wechat-group-read-"
FILES = ("group_reader_client.py", "group_receive_protocol.py", "contact_receive_protocol.py", "group_metadata_protocol.py",
         "contact_send_protocol.py", "group_send_protocol.py")
MAX_LINE = 4096
ISOLATION_CHECKS = frozenset({"non_root", "zero_caps", "private_network", "readonly_code",
                            "no_new_privileges", "own_cgroup", "filesystem_readonly",
                            "docker_masked", "home_hidden", "resource_limits"})
FIXED_CODES = frozenset({"ok", "arguments", "linux_root_required", "owner_unavailable",
    "owner_identity", "owner_capture", "native_changed", "token_unsafe", "reader_failed",
    "reader_deadline", "reader_record", "reader_isolation", "reader_result_missing",
    "cleanup_failed", "host_baseline_failed", "interrupted", "group_probe_failed", "native_trial_expiring", "binding_conflict",
    "metadata_already_attempted", "metadata_busy", "metadata_audit", "checkpoint_failed",
    "send_approval_required", "send_already_attempted", "send_busy", "send_audit", "group_not_verified"})
BINDING_KEYS = frozenset({"schemaVersion", "account", "targetName", "roomId", "apiTokenFingerprint"})
OPERATION_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.ASCII)


def valid_name(value):
    try:
        return (isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 256
                and not any(ord(char) < 32 or ord(char) == 127 for char in value))
    except UnicodeError:
        return False


def argv(pid):
    return owned.read(Path("/proc") / str(pid) / "cmdline", 16384).encode().split(b"\0")[:-1]


def root_identity(pid, group, executable):
    base = Path("/proc") / str(pid)
    status = owned.parse_status(owned.read(base / "status"))
    require(status.get("Uid", "").split() == ["0"] * 4
            and status.get("Gid", "").split() == ["0"] * 4
            and owned.read(base / "cgroup").strip() == "0::" + group
            and owned.identity(base / "exe") == owned.identity(executable), "owner_identity")


def find_native_unit():
    owner = tap.details(OWNER)
    require(owner.get("ActiveState") == "active" and owner.get("ControlGroup") == OWNER_GROUP,
            "owner_unavailable")
    pid = int(owner.get("MainPID", "0"))
    require(pid > 1, "owner_identity")
    ticks = tap.start_ticks(pid)
    root_identity(pid, OWNER_GROUP, "/usr/bin/python3")
    require(argv(pid) in ([b"/usr/bin/python3", b"-I", b"-B", os.fsencode(HERE / "session_runtime.py"), b"--serve-online"],
                         [b"/usr/bin/python3", b"-I", b"-B", os.fsencode(HERE / "session_runtime.py"), b"--serve-long"],
                         [b"/usr/bin/python3", b"-I", b"-B", os.fsencode(HERE / "resume_quarantined_native.py"), b"--serve"]), "owner_identity")
    numbers = owned.read(Path("/sys/fs/cgroup") / OWNER_GROUP.lstrip("/") / "cgroup.procs", 4096).split()
    require(0 < len(numbers) <= 16 and all(value.isdecimal() for value in numbers), "owner_capture")
    matches = []
    for value in numbers:
        candidate = int(value)
        if candidate == pid:
            continue
        try:
            arguments = argv(candidate)
            found = [item[len(b"--unit="):].decode("ascii") for item in arguments
                     if item.startswith(b"--unit=astrbot-wechat-login-")]
            if not found:
                continue
            require(arguments and arguments[0] == b"/usr/bin/systemd-run" and len(found) == 1
                    and UNIT_RE.fullmatch(found[0]), "owner_capture")
            root_identity(candidate, OWNER_GROUP, "/usr/bin/systemd-run")
            matches.append(found[0])
        except (FileNotFoundError, ProcessLookupError):
            continue
    require(len(matches) == 1 and tap.start_ticks(pid) == ticks
            and tap.details(OWNER).get("MainPID") == str(pid), "owner_capture")
    return matches[0]


class PinnedNative:
    """Use the existing identity gate without invoking any lifecycle methods."""
    def __init__(self):
        self.host = None
        self.netfd = None
        self.netns = None

    def open(self):
        unit = find_native_unit()
        suffix = UNIT_RE.fullmatch(unit).group(1)
        self.host = session.SessionHost(Path("/var/lib/astrbot-wechat-session/checkpoints"),
                                        offline=False, seconds=30)
        self.host.unit = unit
        self.host.parent = "astrbotwechatprobe" + suffix + ".slice"
        self.host._pin_client()
        self.netfd = os.open(f"/proc/{self.host.client_pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
        self.host.fds.append(self.netfd)
        info = os.fstat(self.netfd)
        self.netns = {"dev": info.st_dev, "ino": info.st_ino}
        require(self.netns != owned.identity("/proc/self/ns/net"), "native_changed")
        self.host._client_alive()
        base = Path('/sys/fs/cgroup') / self.host.parent
        profile = session.RUNTIME_LIMITS if (base/'memory.max').read_text().strip() == session.RUNTIME_LIMITS['memory.max'] else tap.LIMITS
        tap.cgroup_limits(self.host.parent, profile)
        return self

    def token(self):
        self.host._client_alive()
        descriptor = os.open("api-token", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=self.host.data_fd)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == self.host.client_uid
                    and info.st_dev == self.host.data_dev and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o600 and 32 <= info.st_size <= 1024,
                    "token_unsafe")
            raw = os.read(descriptor, 1025)
            require(len(raw) == info.st_size, "token_unsafe")
            token = raw.decode("ascii").strip()
            require(32 <= len(token) <= 1024 and all(33 <= ord(char) <= 126 for char in token), "token_unsafe")
            return token
        finally:
            os.close(descriptor)

    def unchanged(self):
        try:
            self.host._client_alive()
            return owned.identity(f"/proc/{self.host.client_pid}/ns/net") == self.netns
        except Exception:
            return False

    def remaining_seconds(self):
        self.host._client_alive()
        opt = os.open("opt", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.host.root_fd)
        try:
            descriptor = os.open("session_config.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=opt)
            try:
                info = os.fstat(descriptor)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                        and stat.S_IMODE(info.st_mode) == 0o444 and 0 < info.st_size <= 4096, "native_changed")
                configuration = session.guest.validate_config(json.loads(os.read(descriptor, 4097),
                    object_pairs_hook=session.guest._unique, parse_constant=session.guest._invalid_constant))
                require(configuration["offline"] is False, "native_changed")
                duration = configuration["runtimeSeconds"]
            finally:
                os.close(descriptor)
        finally:
            os.close(opt)
        started = self.host.client_ticks / os.sysconf("SC_CLK_TCK")
        remaining = duration - (time.clock_gettime(time.CLOCK_BOOTTIME) - started)
        owner_pid = int(tap.details(OWNER).get('MainPID', '0'))
        require(owner_pid > 1, 'owner_unavailable')
        owner_started = tap.start_ticks(owner_pid) / os.sysconf('SC_CLK_TCK')
        owner_remaining = duration - (time.clock_gettime(time.CLOCK_BOOTTIME) - owner_started) - 30
        remaining = min(remaining, owner_remaining)
        require(0 < remaining <= duration, "native_trial_expiring")
        return int(remaining)

    def close(self):
        # Deliberately no SessionHost.close(): it would stop the real session.
        if self.host is not None:
            for descriptor in reversed(self.host.fds):
                os.close(descriptor)
            self.host.fds.clear()


def reader_properties(bundle, user, parent, netfd, seconds, *, large_images=False):
    values = owned.properties(str(bundle), user)
    return legacy.properties_changed(values, Slice=parent,
        CapabilityBoundingSet="", AmbientCapabilities="", RestrictNamespaces="yes",
        NetworkNamespacePath=f"/proc/{os.getpid()}/fd/{netfd}",
        RestrictAddressFamilies="AF_INET AF_INET6", DevicePolicy="closed",
        InaccessiblePaths=" ".join("-" + path for path in tap.HELPER_MASKS),
        MemoryMax="256M" if large_images else "32M", MemoryHigh="192M" if large_images else "24M", MemorySwapMax="0", TasksMax="4", LimitNOFILE="32",
        RuntimeMaxSec=str(seconds + 10),
        TemporaryFileSystem="/var/lib:ro,size=4K,nr_inodes=16,mode=000,nosuid,nodev,noexec "
                            "/dev/shm:ro,size=4K,nr_inodes=16,mode=000,nosuid,nodev,noexec")


class InputCapture(owned.Capture):
    def __init__(self, arguments, configuration):
        payload = json.dumps(configuration, separators=(",", ":"), ensure_ascii=True).encode()
        require(0 < len(payload) <= 4096, "token_unsafe")
        self.process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, close_fds=True, env=owned.ENV)
        self.selector = selectors.DefaultSelector()
        self.output = {"stdout": bytearray(), "stderr": bytearray()}
        for name in self.output:
            pipe = getattr(self.process, name)
            os.set_blocking(pipe.fileno(), False)
            self.selector.register(pipe, selectors.EVENT_READ, name)
        self.process.stdin.write(payload + b"\n")
        self.process.stdin.flush()

    def acknowledge(self):
        self.process.stdin.write(b"AWG-READY\n")
        self.process.stdin.flush()
        self.process.stdin.close()

    def close(self):
        try:
            super().close()
        finally:
            if not self.process.stdin.closed:
                self.process.stdin.close()


def verify_reader(unit, pinned, entry="group_reader_client.py", *, large_images=False):
    require(entry in {"group_reader_client.py", "bridge_native_worker.py", "bridge_delivery_worker.py", "forward_native_worker.py"}, "reader_isolation")
    state = tap.details(unit)
    pid = int(state.get("MainPID", "0"))
    group = "/" + pinned.host.parent + "/" + unit
    require(pid > 1 and state.get("ActiveState") == "active"
            and state.get("ControlGroup") == group, "reader_isolation")
    ticks = tap.start_ticks(pid)
    base = Path("/proc") / str(pid)
    require(owned.identity(base / "exe") == owned.identity("/usr/bin/python3")
            and argv(pid) == [b"/usr/bin/python3", b"-I", b"-B", os.fsencode("/opt/" + entry)],
            "reader_isolation")
    status = owned.parse_status(owned.read(base / "status"))
    uids, gids = status.get("Uid", "").split(), status.get("Gid", "").split()
    require(len(uids) == len(gids) == 4 and len(set(uids)) == len(set(gids)) == 1
            and all(value.isdecimal() and int(value) > 0 for value in uids + gids)
            and int(uids[0]) != pinned.host.client_uid and owned.zero_caps(status)
            and status.get("NoNewPrivs", "").strip() == "1"
            and owned.read(base / "cgroup").strip() == "0::" + group
            and owned.identity(base / "ns/net") == pinned.netns, "reader_isolation")
    tap.cgroup_limits(unit, {"memory.max": "268435456" if large_images else "33554432", "memory.high": "201326592" if large_images else "25165824",
                             "memory.swap.max": "0", "pids.max": "4", "cpu.max": "20000 100000"})
    require(tap.start_ticks(pid) == ticks and tap.details(unit).get("MainPID") == str(pid),
            "reader_isolation")
    pinned.host._client_alive()


def parse_record(line, codes):
    require(isinstance(line, bytes) and 0 < len(line) <= MAX_LINE, "reader_record")
    value = json.loads(line, object_pairs_hook=session.guest._unique,
                       parse_constant=session.guest._invalid_constant)
    require(isinstance(value, dict) and isinstance(value.get("stage"), str), "reader_record")
    stage = value["stage"]
    if stage == "group_reader_isolated":
        checks = value.get("checks")
        good = (set(value) == {"stage", "ok", "checks"} and type(value["ok"]) is bool
                and isinstance(checks, dict) and set(checks) == ISOLATION_CHECKS
                and all(type(flag) is bool for flag in checks.values())
                and value["ok"] is all(checks.values()))
    elif stage == "group_state":
        good = value == {"stage": stage, "state": "target_matched"}
    elif stage == "group_waiting":
        good = (set(value) == {"stage", "marker", "seconds"}
                and isinstance(value["marker"], str)
                and re.fullmatch(r"(?:AWTEST-[0-9a-f]{8}|AWGROUP-[0-9a-f]{16})", value["marker"])
                and type(value["seconds"]) is int and 1 <= value["seconds"] <= 600)
    elif stage == "group_observation":
        keys = {"event_count", "target_incoming_count", "target_live_count", "text_checked"}
        good = (set(value) == {"stage", "marker_matched"} | keys
                and type(value["marker_matched"]) is bool
                and all(type(value[key]) is int and 0 <= value[key] <= 20000 for key in keys))
    elif stage == "group_result":
        good = (set(value) == {"stage", "ok", "code"} and type(value["ok"]) is bool
                and isinstance(value["code"], str) and value["code"] in codes)
    elif stage == "group_bind_state":
        good = value == {"stage": stage, "state": "awaiting_marker"}
    elif stage == "group_binding_observation":
        counters = {"event_count", "candidate_group_events", "text_checked"}
        good = (set(value) == {"stage", "marker_matched"} | counters
                and type(value["marker_matched"]) is bool
                and all(type(value[key]) is int and 0 <= value[key] <= 20000 for key in counters))
    elif stage == "group_binding":
        room = value.get("roomId")
        good = (set(value) == {"stage", "roomId"} and isinstance(room, str)
                and 0 < len(room.encode("utf-8")) <= 256 and room.endswith("@chatroom")
                and not any(ord(char) < 32 or ord(char) == 127 for char in room))
    elif stage == "group_catalog_item":
        name = value.get("name")
        good = (set(value) == {"stage", "index", "name"} and type(value["index"]) is int
                and 1 <= value["index"] <= 400 and isinstance(name, str)
                and len(name.encode("utf-8")) <= 512
                and not any(ord(char) < 32 or ord(char) == 127 for char in name)
                and not name.endswith("@chatroom"))
    elif stage == "group_catalog_summary":
        good = (set(value) == {"stage", "count", "named_count", "complete"}
                and type(value["count"]) is type(value["named_count"]) is int
                and 0 <= value["named_count"] <= value["count"] <= 400
                and type(value["complete"]) is bool)
    elif stage == "group_metadata_operation":
        good = (set(value) == {"stage", "operationId"} and isinstance(value["operationId"], str)
                and OPERATION_RE.fullmatch(value["operationId"]) is not None)
    elif stage == "group_metadata_state":
        good = (set(value) == {"stage", "state"} and value["state"] in {"submitted", "queued", "running", "succeeded"})
    elif stage == "group_metadata_check":
        good = (set(value) == {"stage", "ok", "projection_complete", "name_matches"}
                and all(value[key] is True for key in ("ok", "projection_complete", "name_matches")))
    elif stage == "group_send_intent":
        good = (set(value) == {"stage", "sendId"} and isinstance(value["sendId"], str)
                and OPERATION_RE.fullmatch(value["sendId"]) is not None)
    elif stage == "group_send_state":
        good = (set(value) == {"stage", "state"} and value["state"] in {
            "target_matched", "submitting", "queued", "accepted", "failed", "needs_review", "cancelled"})
    elif stage == "group_send_result":
        good = (set(value) == {"stage", "ok", "code", "submitted", "server_confirmed"}
                and all(type(value[key]) is bool for key in ("ok", "submitted", "server_confirmed"))
                and isinstance(value["code"], str) and value["code"] in codes
                and value["ok"] is (value["code"] == "ok")
                and value["server_confirmed"] is value["ok"]
                and (not value["server_confirmed"] or value["submitted"]))
    else:
        good = False
    require(good, "reader_record")
    return value


def save_binding(pinned, target_name, room):
    controller = companion("session_runtime")
    controller.private_directory(controller.STATE)
    path = controller.STATE / "group-binding.json"
    value = {"schemaVersion": 1, "account": "wechat-primary", "targetName": target_name,
             "roomId": room, "apiTokenFingerprint": hashlib.sha256(pinned.token().encode("ascii")).hexdigest()}
    require(pinned.unchanged(), "native_changed")
    try:
        before = controller.read_json(path)
    except FileNotFoundError:
        before = None
    require(before is None or before == value, "binding_conflict")
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    require(len(raw) <= 4096, "reader_record")
    controller.atomic_private(path, raw)


def load_binding(pinned, target_name):
    controller = companion("session_runtime")
    controller.private_directory(controller.STATE)
    value = controller.read_json(controller.STATE / "group-binding.json")
    require(isinstance(value, dict) and set(value) == BINDING_KEYS
            and type(value["schemaVersion"]) is int and value["schemaVersion"] == 1
            and value["account"] == "wechat-primary" and value["targetName"] == target_name
            and value["apiTokenFingerprint"] == hashlib.sha256(pinned.token().encode("ascii")).hexdigest(), "binding_conflict")
    parse_record(json.dumps({"stage": "group_binding", "roomId": value["roomId"]}).encode(), set())
    require(pinned.unchanged(), "native_changed")
    return controller, value


def lock_once(controller, *, send=False):
    label = "send" if send else "metadata"
    prefix = "group-send" if send else "group-refresh"
    descriptor = os.open(controller.STATE / (prefix + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, label + "_audit")
        try:
            controller.fcntl.flock(descriptor, controller.fcntl.LOCK_EX | controller.fcntl.LOCK_NB)
        except BlockingIOError:
            raise legacy.Failure(label + "_busy") from None
        try:
            controller.read_json(controller.STATE / (prefix + "-attempt.json"))
        except FileNotFoundError:
            return descriptor
        raise legacy.Failure(label + "_already_attempted")
    except BaseException:
        os.close(descriptor)
        raise


def lock_metadata(controller):
    return lock_once(controller)


def write_metadata_audit(controller, binding, *, operation=None):
    path = controller.STATE / "group-refresh-attempt.json"
    value = {**binding, "status": "admitted"}
    if operation is not None:
        previous = controller.read_json(path)
        require(isinstance(previous, dict) and previous == value, "metadata_audit")
        value.update(status="operation", operationId=operation)
    controller.write_json(path, value)


def checkpoint_current(controller, pinned):
    require(pinned.unchanged(), "native_changed")
    # A completed status command also commits a full SDK snapshot in the owner.
    before = controller.read_json(controller.CONTROL / "status.json")
    require(before.get("ready") is True and not before.get("busy"), "checkpoint_failed")
    sequence = before["sequence"] + 1
    controller.write_json(controller.CONTROL / "request.json", controller.request_value({
        "sequence": sequence, "action": "status", "allowFreshLogin": False}))
    deadline = time.monotonic() + 10
    saved = False
    while time.monotonic() < deadline:
        status = controller.read_json(controller.CONTROL / "status.json")
        if status.get("sequence") == sequence and status.get("done"):
            saved = status.get("ok") is True and status.get("accounts") == 1
            break
        time.sleep(.15)
    require(saved and pinned.unchanged(), "checkpoint_failed")


def verify_metadata_saved(controller, binding, pinned):
    audit = controller.read_json(controller.STATE / "group-refresh-attempt.json")
    require(isinstance(audit, dict) and all(audit.get(key) == value for key, value in binding.items())
            and audit.get("status") == "operation" and isinstance(audit.get("operationId"), str)
            and OPERATION_RE.fullmatch(audit["operationId"]) is not None, "metadata_audit")
    checkpoint_current(controller, pinned)
    controller.write_json(controller.STATE / "group-verification.json", {**binding, "metadataVerified": True})
    controller.write_json(controller.STATE / "group-refresh-attempt.json", {**audit, "status": "verified"})


def load_send_binding(pinned, target_name):
    controller, binding = load_binding(pinned, target_name)
    proof = controller.read_json(controller.STATE / "group-verification.json")
    require(isinstance(proof, dict) and proof == {**binding, "metadataVerified": True}, "group_not_verified")
    return controller, binding


def save_send_intent(controller, audit, identity):
    path = controller.STATE / "group-send-attempt.json"
    require(controller.read_json(path) == audit and audit.get("status") == "admitted"
            and isinstance(identity, str) and OPERATION_RE.fullmatch(identity), "send_audit")
    updated = {**audit, "status": "operation", "sendId": identity}
    controller.write_json(path, updated)
    return updated


def save_send_result(controller, audit, receipt, pinned):
    require(controller.read_json(controller.STATE / "group-send-attempt.json") == audit, "send_audit")
    if receipt["server_confirmed"]:
        require(audit.get("status") == "operation", "send_audit")
    # Also preserve unknown/failed outbox outcomes; startup will block pending
    # sends instead of replaying this test. A receipt never authorizes a retry.
    checkpoint_current(controller, pinned)
    controller.write_json(controller.STATE / "group-send-attempt.json", {
        **audit, "status": "accepted" if receipt["server_confirmed"] else "stopped",
        "result": {key: receipt[key] for key in ("ok", "code", "submitted", "server_confirmed")}})


def orchestrator(action, target_name, *, approved=False):
    require(action != "send" or approved is True, "send_approval_required")
    require(sys.platform == "linux" and os.geteuid() == 0, "linux_root_required")
    require(action in {"identify", "receive", "bind", "list", "refresh", "send"} and valid_name(target_name), "arguments")
    protocol = companion("group_receive_protocol")
    metadata = companion("group_metadata_protocol")
    sender = companion("group_send_protocol")
    codes = set(protocol.RESULT_CODES) | set(metadata.RESULT_CODES) | set(sender.RESULT_CODES) | {"ok", "reader_config_failed", "reader_isolation_failed",
                                          "reader_handshake_failed", "reader_failed"}
    host = {kind: owned.identity("/proc/self/ns/" + kind) for kind in ("user", "net")}
    network, business = tap.read_network(), tap.business_snapshot()
    pinned = PinnedNative()
    bundle = capture = None
    unit = READER_PREFIX + secrets.token_hex(6) + ".service"
    attempted = False
    result = None
    isolated = matched = waiting = binding_armed = binding_observed = False
    listed_count = named_count = 0
    listed_complete = False
    metadata_lock = None
    metadata_controller = metadata_binding = None
    metadata_admitted = operation_seen = metadata_verified = False
    send_controller = send_binding = send_audit = send_receipt = None
    send_seen = False
    cleanup_errors = []
    primary = None
    try:
        pinned.open()
        seconds = min({"identify": 60, "receive": 620, "bind": 200, "list": 60, "refresh": 200, "send": 120}[action], pinned.remaining_seconds() - 20)
        require(seconds >= 30, "native_trial_expiring")
        if action == "refresh":
            metadata_controller, metadata_binding = load_binding(pinned, target_name)
            metadata_lock = lock_metadata(metadata_controller)
        elif action == "send":
            send_controller, send_binding = load_send_binding(pinned, target_name)
            metadata_lock = lock_once(send_controller, send=True)
            send_audit = {**send_binding, "status": "admitted", "idempotencyKey": uuid.uuid4().hex,
                          "textSha256": hashlib.sha256(sender.TEST_TEXT.encode("utf-8")).hexdigest()}
        token = pinned.token()
        bundle = Path(tempfile.mkdtemp(prefix="astrbot-wechat-group.", dir="/var/tmp"))
        for name in FILES:
            legacy.write_public(bundle, name, legacy.source_text(HERE / name))
        os.chmod(bundle, 0o755)
        tap.require_unit_absent(unit)
        configuration = {"schemaVersion": 1, "account": "wechat-primary", "targetName": target_name,
                         "action": action, "token": token, "runtimeSeconds": seconds}
        if action == "refresh":
            configuration["roomId"] = metadata_binding["roomId"]
        elif action == "send":
            configuration.update(roomId=send_binding["roomId"], idempotencyKey=send_audit["idempotencyKey"])
        arguments = ["/usr/bin/systemd-run", "--system", "--unit=" + unit, "--collect", "--wait", "--pipe", "--quiet",
                     *reader_properties(bundle, "awgr-" + unit[len(READER_PREFIX):-8],
                                        pinned.host.parent, pinned.netfd, seconds),
                     "/usr/bin/python3", "-I", "-B", "/opt/group_reader_client.py"]
        attempted = True
        capture = InputCapture(arguments, configuration)
        del configuration, token
        deadline = time.monotonic() + seconds + 8
        while capture.process.poll() is None or capture.selector.get_map():
            require(time.monotonic() < deadline, "reader_deadline")
            capture.drain(.1)
            while b"\n" in capture.output["stdout"]:
                line, _, rest = capture.output["stdout"].partition(b"\n")
                capture.output["stdout"][:] = rest
                record = parse_record(bytes(line), codes)
                stage = record["stage"]
                if stage == "group_reader_isolated":
                    require(not isolated, "reader_isolation")
                    if not record["ok"]:
                        legacy.wire_record(record)
                        raise legacy.Failure("reader_isolation")
                    verify_reader(unit, pinned)
                    if action == "refresh":
                        write_metadata_audit(metadata_controller, metadata_binding)
                        metadata_admitted = True
                    elif action == "send":
                        send_controller.write_json(send_controller.STATE / "group-send-attempt.json", send_audit)
                    capture.acknowledge()
                    isolated = True
                else:
                    require(isolated, "reader_isolation")
                if stage == "group_state":
                    require(not matched, "reader_record")
                    matched = True
                elif stage == "group_bind_state":
                    require(action == "bind" and not binding_armed and not matched, "reader_record")
                    binding_armed = True
                elif stage == "group_waiting":
                    require((action == "receive" and matched or action == "bind" and binding_armed) and not waiting,
                            "reader_record")
                    waiting = True
                elif stage == "group_binding":
                    require(action == "bind" and binding_armed and waiting and binding_observed and not matched, "reader_record")
                    save_binding(pinned, target_name, record["roomId"])
                    matched = True
                    legacy.emit("group_binding_saved", ok=True)
                    continue  # Never relay the private room identifier.
                elif stage == "group_binding_observation":
                    require(action == "bind" and binding_armed and waiting and not matched, "reader_record")
                    binding_observed = record["marker_matched"]
                elif stage == "group_result":
                    require(result is None, "reader_record")
                    result = record
                elif stage == "group_catalog_item":
                    require(action == "list" and record["index"] == listed_count + 1 and not listed_complete,
                            "reader_record")
                    listed_count += 1
                    named_count += bool(record["name"])
                elif stage == "group_catalog_summary":
                    require(action == "list" and not listed_complete and record["count"] == listed_count
                            and record["named_count"] == named_count, "reader_record")
                    listed_complete = record["complete"]
                elif stage == "group_metadata_operation":
                    require(action == "refresh" and metadata_admitted and not operation_seen, "metadata_audit")
                    write_metadata_audit(metadata_controller, metadata_binding, operation=record["operationId"])
                    operation_seen = True
                    continue  # Private operation identity never reaches public progress.
                elif stage == "group_metadata_state":
                    require(action == "refresh" and operation_seen, "metadata_audit")
                elif stage == "group_metadata_check":
                    require(action == "refresh" and operation_seen and not metadata_verified, "metadata_audit")
                    verify_metadata_saved(metadata_controller, metadata_binding, pinned)
                    metadata_verified = True
                elif stage == "group_send_intent":
                    require(action == "send" and not send_seen, "send_audit")
                    send_audit = save_send_intent(send_controller, send_audit, record["sendId"])
                    send_seen = True
                    continue  # Private send identity never reaches progress.
                elif stage == "group_send_state":
                    require(action == "send", "send_audit")
                elif stage == "group_send_result":
                    require(action == "send" and send_receipt is None
                            and (not record["server_confirmed"] or send_seen), "send_audit")
                    save_send_result(send_controller, send_audit, record, pinned)
                    send_receipt = record
                legacy.wire_record(record)
            require(len(capture.output["stdout"]) <= MAX_LINE, "reader_record")
        require(not capture.output["stdout"] and result is not None, "reader_result_missing")
        require(capture.process.wait() == (0 if result["ok"] else 1), "reader_failed")
        require(not result["ok"] or action == "list" and listed_complete
                or action == "refresh" and metadata_verified
                or action == "send" and send_receipt is not None and send_receipt["server_confirmed"]
                or matched and (action == "identify" or waiting), "reader_record")
    except KeyboardInterrupt:
        primary = "interrupted"
    except Exception as error:
        label = getattr(error, "stage", None)
        primary = label if label in FIXED_CODES else "group_probe_failed"
    finally:
        if attempted:
            try:
                tap.stop_unit(unit, True)
            except Exception:
                cleanup_errors.append("reader_unit_cleanup_failed")
        if capture is not None:
            try:
                capture.close()
            except Exception:
                cleanup_errors.append("reader_capture_cleanup_failed")
        same_native = pinned.unchanged() if pinned.host is not None else False
        try:
            pinned.close()
        except Exception:
            cleanup_errors.append("reader_fd_cleanup_failed")
        if metadata_lock is not None:
            os.close(metadata_lock)
        if bundle is not None:
            try:
                for name in FILES:
                    (bundle / name).unlink(missing_ok=True)
                bundle.rmdir()
            except OSError:
                cleanup_errors.append("reader_bundle_cleanup_failed")
        baseline, failures = tap.compare_baselines(host, network, business)
        clean = not cleanup_errors and not failures and same_native
        legacy.emit("group_reader_cleanup", ok=clean, native_session_unchanged=same_native,
                    reader_unit_removed=not cleanup_errors, **baseline)
    good = primary is None and result is not None and result["ok"] and clean
    legacy.emit("group_probe_result", ok=good,
                code="ok" if good else primary or ("cleanup_failed" if not clean else result["code"]))
    return 0 if good else 1


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) >= 1 and arguments[0] == "--send-group":
        if len(arguments) != 3 or arguments[2] != "--approved-single-message" or not valid_name(arguments[1]):
            legacy.emit("group_probe_result", ok=False, code="send_approval_required")
            return 2
        try:
            return orchestrator("send", arguments[1], approved=True)
        except Exception:
            legacy.emit("group_probe_result", ok=False, code="group_probe_failed")
            return 1
    if len(arguments) != 2 or arguments[0] not in {"--identify-group", "--receive-group", "--bind-group", "--list-groups", "--refresh-group"} or not valid_name(arguments[1]):
        legacy.emit("group_probe_result", ok=False, code="arguments")
        return 2
    try:
        return orchestrator({"--identify-group": "identify", "--receive-group": "receive", "--bind-group": "bind", "--list-groups": "list", "--refresh-group": "refresh"}[arguments[0]], arguments[1])
    except Exception:
        legacy.emit("group_probe_result", ok=False, code="group_probe_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
