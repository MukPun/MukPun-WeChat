#!/usr/bin/env python3
"""Short group worker beside an already connected, isolated Native.

The API token arrives over an anonymous pipe. This process never reads saved SDK
files, writes credentials, creates accounts, connects or logs out. Only an
explicit parent-approved send action can submit the fixed group test text.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import select
import stat
import sys
import time

SPEC = importlib.util.spec_from_file_location("_group_reader_protocol", Path(__file__).with_name("group_receive_protocol.py"))
group = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = group
SPEC.loader.exec_module(group)
META_SPEC = importlib.util.spec_from_file_location("_group_metadata_protocol", Path(__file__).with_name("group_metadata_protocol.py"))
metadata = importlib.util.module_from_spec(META_SPEC)
sys.modules[META_SPEC.name] = metadata
META_SPEC.loader.exec_module(metadata)
SEND_SPEC = importlib.util.spec_from_file_location("_group_send_protocol", Path(__file__).with_name("group_send_protocol.py"))
sender = importlib.util.module_from_spec(SEND_SPEC)
sys.modules[SEND_SPEC.name] = sender
SEND_SPEC.loader.exec_module(sender)
CONFIG_LIMIT = 4096
CODE_FILES = ("group_reader_client.py", "group_receive_protocol.py", "contact_receive_protocol.py", "group_metadata_protocol.py",
              "contact_send_protocol.py", "group_send_protocol.py")
CHECK_NAMES = frozenset({"non_root", "zero_caps", "private_network", "readonly_code",
    "no_new_privileges", "own_cgroup", "filesystem_readonly", "docker_masked", "home_hidden", "resource_limits"})
RESULT_CODES = group.RESULT_CODES | metadata.RESULT_CODES | sender.RESULT_CODES | {"reader_config_failed", "reader_isolation_failed", "reader_handshake_failed", "reader_failed"}
CGROUP_RE = re.compile(r"0::/(astrbotwechatprobe[0-9a-f]{12}\.slice)/(astrbot-wechat-group-read-[0-9a-f]{12}\.service)\Z")


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("config")
        value[key] = item
    return value


def invalid(_):
    raise ValueError("config")


def read_config():
    raw = sys.stdin.buffer.readline(CONFIG_LIMIT + 1)
    if not 0 < len(raw) <= CONFIG_LIMIT or not raw.endswith(b"\n"):
        raise ValueError("config")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    keys = {"schemaVersion", "account", "targetName", "action", "token", "runtimeSeconds"}
    if isinstance(value, dict) and value.get("action") in {"refresh", "send"}:
        keys.add("roomId")
        if value["action"] == "send":
            keys.add("idempotencyKey")
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
            or value["account"] != "wechat-primary"
            or not isinstance(value["targetName"], str) or not value["targetName"]
            or len(value["targetName"].encode("utf-8")) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value["targetName"])
            or value["action"] not in {"identify", "receive", "bind", "list", "refresh", "send"}
            or not isinstance(value["token"], str) or not 32 <= len(value["token"]) <= 1024
            or any(not 33 <= ord(char) <= 126 for char in value["token"])
            or type(value["runtimeSeconds"]) is not int
            or not 30 <= value["runtimeSeconds"] <= {"identify": 60, "receive": 620, "bind": 200, "list": 60, "refresh": 200, "send": 120}[value["action"]]):
        raise ValueError("config")
    if value["action"] in {"refresh", "send"}:
        room = value["roomId"]
        if (not isinstance(room, str) or room == "@chatroom" or not room.endswith("@chatroom")
                or not 1 <= len(room.encode("utf-8")) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in room)):
            raise ValueError("config")
    if value["action"] == "send" and (not isinstance(value["idempotencyKey"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["idempotencyKey"], re.ASCII)):
        raise ValueError("config")
    return value


def await_ready():
    # No API request is allowed until the trusted parent validates this exact
    # reader PID, namespace and resource controls after our isolation record.
    ready, _, _ = select.select([sys.stdin.buffer], [], [], 5.0)
    return bool(ready) and sys.stdin.buffer.readline(32) == b"AWG-READY\n"


def _mounts():
    result = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        split = fields.index("-")
        unescape = lambda value: re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
        result.append({"point": unescape(fields[4]), "root": unescape(fields[3]),
                       "options": set(fields[5].split(",")), "fstype": fields[split + 1]})
    return result


def _containing(mounts, path):
    return max((entry for entry in mounts if entry["point"] == "/" or path == entry["point"]
                or path.startswith(entry["point"].rstrip("/") + "/")), key=lambda entry: len(entry["point"]))


def _readonly(mounts, path):
    return "ro" in _containing(mounts, path)["options"] and bool(os.statvfs(path).f_flag & os.ST_RDONLY)


def _masked(mounts, path):
    mount = _containing(mounts, path)
    return "inaccessible" in mount["root"] and "ro" in mount["options"]


def isolation_checks():
    checks = {name: False for name in CHECK_NAMES}
    try:
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        mounts = _mounts()
        relative = Path("/proc/self/cgroup").read_text().strip()
        match = CGROUP_RE.fullmatch(relative)
        checks["non_root"] = os.geteuid() > 0 and os.getegid() > 0
        checks["zero_caps"] = all(int(status[name].strip(), 16) == 0 for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))
        checks["no_new_privileges"] = status["NoNewPrivs"].strip() == "1"
        checks["own_cgroup"] = match is not None
        # libc if_nameindex can open AF_NETLINK, which this reader deliberately
        # cannot create. /proc/net/dev reports this same network namespace.
        devices = Path("/proc/net/dev").read_text()
        checks["private_network"] = (len(devices) <= 16384 and
            {line.split(":", 1)[0].strip() for line in devices.splitlines()[2:] if ":" in line} == {"lo", "tap0"})
        checks["filesystem_readonly"] = all(_readonly(mounts, path) for path in ("/", "/usr", "/etc", "/opt", "/var/lib"))
        def code_regular(name):
            path = Path("/opt") / name
            info = path.lstat()
            return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o444 and 0 < info.st_size <= 131072
                    and _readonly(mounts, str(path)))
        checks["readonly_code"] = all(code_regular(name) for name in CODE_FILES)
        checks["docker_masked"] = all(_masked(mounts, path) for path in (
            "/run/docker.sock", "/run/docker", "/run/containerd"))
        checks["home_hidden"] = _containing(mounts, "/home")["fstype"] == "tmpfs" and _containing(mounts, "/root")["fstype"] == "tmpfs"
        if match:
            cgroup = Path("/sys/fs/cgroup") / relative.removeprefix("0::/")
            expected = {"memory.max": "33554432", "memory.high": "25165824", "memory.swap.max": "0",
                        "pids.max": "4", "cpu.max": "20000 100000"}
            parent = {"memory.max": "134217728", "memory.high": "100663296", "memory.swap.max": "0",
                      "pids.max": "32", "cpu.max": "20000 100000"}
            checks["resource_limits"] = (all((cgroup / name).read_text().strip() == value for name, value in expected.items())
                and all((cgroup.parent / name).read_text().strip() == value for name, value in parent.items()))
    except Exception:
        pass  # No exception details, path inventories or tokens leave the reader.
    return checks


def emit(record):
    print(json.dumps(record, separators=(",", ":"), ensure_ascii=True), flush=True)


def main(arguments=None):
    if (sys.argv[1:] if arguments is None else arguments):
        emit({"stage": "group_result", "ok": False, "code": "reader_config_failed"})
        return 2
    try:
        configuration = read_config()
    except Exception:
        emit({"stage": "group_result", "ok": False, "code": "reader_config_failed"})
        return 1
    checks = isolation_checks()
    good = set(checks) == CHECK_NAMES and all(type(value) is bool and value for value in checks.values())
    emit({"stage": "group_reader_isolated", "ok": good, "checks": checks})
    if not good:
        emit({"stage": "group_result", "ok": False, "code": "reader_isolation_failed"})
        return 1
    try:
        if not await_ready():
            raise ValueError("handshake")
    except Exception:
        emit({"stage": "group_result", "ok": False, "code": "reader_handshake_failed"})
        return 1
    try:
        client = group.GroupReceiveClient(configuration["account"], configuration["token"], configuration["targetName"])
        deadline = time.monotonic() + configuration["runtimeSeconds"] - 2
        if configuration["action"] == "send":
            worker = sender.GroupSendClient(configuration["account"], configuration["token"],
                configuration["roomId"], configuration["targetName"], configuration["idempotencyKey"],
                on_intent=lambda identity: emit({"stage": "group_send_intent", "sendId": identity}))
            receipt = worker.run(deadline, lambda record: emit({"stage": "group_send_state", "state": record["state"]}))
            emit({"stage": "group_send_result", "ok": receipt.ok, "code": receipt.code,
                  "submitted": receipt.submitted, "server_confirmed": receipt.server_confirmed})
            outcome = receipt.code
        elif configuration["action"] == "refresh":
            worker = metadata.GroupMetadataClient(configuration["account"], configuration["token"],
                                                  configuration["roomId"], configuration["targetName"])
            outcome = worker.run(deadline, emit, on_operation=lambda operation: emit({
                "stage": "group_metadata_operation", "operationId": operation}))
        elif configuration["action"] == "list":
            outcome = client.list_groups(deadline, emit)
        elif configuration["action"] == "bind":
            room = client.bind(deadline, emit)
            if (not isinstance(room, str) or not room.endswith("@chatroom")
                    or not 1 <= len(room.encode("utf-8")) <= 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in room)):
                raise ValueError("binding")
            # Private anonymous pipe record. The trusted parent validates and
            # persists it; it MUST NOT be relayed to journal/chat/progress.
            emit({"stage": "group_binding", "roomId": room})
            outcome = "ok"
        else:
            outcome = client.identify(deadline, emit) if configuration["action"] == "identify" else client.run(deadline, emit)
        code = outcome if outcome in RESULT_CODES else "reader_failed"
    except group.GroupError as error:
        code = error.code if error.code in RESULT_CODES else "reader_failed"
    except metadata.MetadataError as error:
        code = error.code if error.code in RESULT_CODES else "reader_failed"
    except sender.SendError as error:
        code = error.code if error.code in RESULT_CODES else "reader_failed"
    except Exception:
        code = "reader_failed"
    emit({"stage": "group_result", "ok": code == "ok", "code": code})
    return 0 if code == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
