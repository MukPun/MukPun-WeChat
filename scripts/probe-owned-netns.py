#!/usr/bin/env python3
"""Trusted, offline namespace-ownership probe; no slirp or WeChat execution.

Run explicitly as root on Linux. Only one random transient system unit and one
temporary root-owned Python copy are created. Business-process/route baselines
remain the caller's responsibility. --keeper is only for the isolated unit.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
import time

PREFIX = "astrbot-wechat-owned-netns-"
CAP_KEYS = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
KEEPER_FAILURE_STAGES = frozenset({
    "read_bound", "single_id_map", "probe_exception", "keeper_mapped_root",
    "keeper_caps_zero", "keeper_groups", "keeper_setgroups", "keeper_uid_map",
    "keeper_gid_map", "keeper_lo_only", "keeper_lo_down", "keeper_no_forwarding_routes",
    "keeper_cgroup_path", "keeper_own_unit", "keeper_resource_limits", "route_diagnostic",
})
MAX_CAPTURE = 65536
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}


class ProbeFailure(Exception):
    def __init__(self, stage: str, number: int | None = None):
        self.stage, self.number = stage, number
        super().__init__(stage)


def require(ok: bool, stage: str) -> None:
    if not ok:
        raise ProbeFailure(stage)


def emit(stage: str, ok: bool, **fields: object) -> None:
    print(json.dumps({"stage": stage, "ok": ok, **fields}, sort_keys=True), flush=True)


def read(path: str | Path, limit: int = MAX_CAPTURE) -> str:
    with open(path, "rb") as stream:
        value = stream.read(limit + 1)
    require(len(value) <= limit, "read_bound")
    return value.decode("utf-8", "strict")


def parse_status(text: str) -> dict[str, str]:
    return dict(line.split(":", 1) for line in text.splitlines() if ":" in line)


def id_map(text: str) -> tuple[int, int, int]:
    rows = [tuple(map(int, line.split())) for line in text.splitlines() if line.strip()]
    require(len(rows) == 1 and len(rows[0]) == 3, "single_id_map")
    return rows[0]


def identity(path: str | Path) -> dict[str, int]:
    item = os.stat(path)
    return {"dev": item.st_dev, "ino": item.st_ino}


def zero_caps(status: dict[str, str]) -> bool:
    return all(int(status[key].strip(), 16) == 0 for key in CAP_KEYS)


def ipv4_routes_safe(text: str) -> bool:
    header = ["Iface", "Destination", "Gateway", "Flags", "RefCnt", "Use", "Metric",
              "Mask", "MTU", "Window", "IRTT"]
    rows = [line.split() for line in text.splitlines() if line.strip()]
    return not rows or rows == [header]


def ipv6_routes_safe(text: str) -> bool:
    """Only the kernel's non-forwarding, next-hop-free ::/0 reject is allowed."""
    for line in text.splitlines():
        row = line.split()
        if (len(row) != 10 or row[0] != "0" * 32 or row[1] != "00" or
                row[2] != "0" * 32 or row[3] != "00" or row[4] != "0" * 32 or
                row[9] != "lo"):
            return False
        try:
            if not int(row[8], 16) & 0x200:  # RTF_REJECT: cannot forward traffic.
                return False
        except ValueError:
            return False
    return True


def route_diagnostic(ipv4: str, ipv6: str) -> dict[str, object]:
    rows = []
    lines4, lines6 = ipv4.splitlines(), ipv6.splitlines()
    for line in lines6[:8]:
        fields = line.split()
        def field(index: int) -> str:
            return fields[index] if index < len(fields) else ""
        def hexadecimal(index: int, maximum: int) -> int | None:
            value = field(index)
            if not value or any(char not in "0123456789abcdefABCDEF" for char in value):
                return None
            parsed = int(value, 16)
            return parsed if parsed <= maximum else None
        rows.append({"field_count": len(fields), "dest_allzero": field(0) == "0" * 32,
                     "source_allzero": field(2) == "0" * 32,
                     "gateway_allzero": field(4) == "0" * 32,
                     "prefix_bits": hexadecimal(1, 128),
                     "source_prefix_bits": hexadecimal(3, 128),
                     "flags": hexadecimal(8, 0xFFFFFFFF), "device_lo": field(9) == "lo"})
    return {"ipv4_splitlines_count": len(lines4),
            "ipv4_nonempty_lines_count": sum(bool(line.strip()) for line in lines4),
            "ipv6_splitlines_count": len(lines6),
            "ipv6_nonempty_lines_count": sum(bool(line.strip()) for line in lines6),
            "ipv6_truncated": len(lines6) > 8, "ipv6_records": rows}


def safe_route_diagnostic(record: dict[str, object]) -> dict[str, object] | None:
    """Rebuild the diagnostic from scalar allowlists; discard all other fields."""
    count_keys = ("ipv4_splitlines_count", "ipv4_nonempty_lines_count",
                  "ipv6_splitlines_count", "ipv6_nonempty_lines_count")
    if not all(type(record.get(key)) is int and 0 <= record[key] <= MAX_CAPTURE for key in count_keys):
        return None
    rows = record.get("ipv6_records")
    if not isinstance(rows, list) or len(rows) > 8 or type(record.get("ipv6_truncated")) is not bool:
        return None
    safe_rows = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("field_count")) is not int or not 0 <= row["field_count"] <= MAX_CAPTURE:
            return None
        bool_keys = ("dest_allzero", "source_allzero", "gateway_allzero", "device_lo")
        if not all(type(row.get(key)) is bool for key in bool_keys):
            return None
        safe = {"field_count": row["field_count"], **{key: row[key] for key in bool_keys}}
        for key, maximum in (("prefix_bits", 128), ("source_prefix_bits", 128), ("flags", 0xFFFFFFFF)):
            value = row.get(key)
            if value is not None and (type(value) is not int or not 0 <= value <= maximum):
                return None
            safe[key] = value
        safe_rows.append(safe)
    return {"stage": "route_diagnostic", **{key: record[key] for key in count_keys},
            "ipv6_truncated": record["ipv6_truncated"], "ipv6_records": safe_rows}


def stderr_summary(value: bytes) -> dict[str, object]:
    """Expose fixed diagnostics only, never command output or path contents."""
    text = value[-4096:].decode("utf-8", "replace")
    source = "unshare" if "unshare:" in text else "setpriv" if "setpriv:" in text else "systemd"
    messages = {"Operation not permitted": 1, "Permission denied": 13,
                "No such file or directory": 2, "Invalid argument": 22,
                "Read-only file system": 30, "Too many open files": 24,
                "Resource temporarily unavailable": 11}
    return {"source": source, "errno_candidates": sorted({code for message, code
            in messages.items() if message in text})}


def keeper_failure_summary(value: bytes) -> list[dict[str, object]]:
    failures: list[dict[str, object]] = []
    for line in value[-MAX_CAPTURE:].split(b"\n")[:-1]:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(record, dict) or record.get("ok") is not False:
            continue
        stage = record.get("stage")
        if not isinstance(stage, str) or stage not in KEEPER_FAILURE_STAGES:
            continue
        if stage == "route_diagnostic":
            safe = safe_route_diagnostic(record)
            if safe is None:
                continue
        else:
            number = record.get("errno")
            safe = {"stage": stage, "errno": number if type(number) is int and 0 < number < 4096 else None}
        if safe not in failures:
            failures.append(safe)
        if len(failures) == 8:
            break
    return failures


def cleanup_confirmed(state: dict[str, str]) -> bool:
    return state.get("LoadState") == "not-found" or (
        state.get("MainPID") == "0" and state.get("ActiveState") in {"inactive", "failed"})


class Capture:
    """Drain both pipes without ever buffering more than MAX_CAPTURE bytes."""
    def __init__(self, arguments: list[str]):
        self.process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        close_fds=True, env=ENV)
        self.selector = selectors.DefaultSelector()
        self.output = {"stdout": bytearray(), "stderr": bytearray()}
        for name in self.output:
            pipe = getattr(self.process, name)
            os.set_blocking(pipe.fileno(), False)
            self.selector.register(pipe, selectors.EVENT_READ, name)

    def drain(self, delay: float = 0.05) -> None:
        for key, _ in self.selector.select(delay):
            chunk = os.read(key.fileobj.fileno(), 4096)
            if not chunk:
                self.selector.unregister(key.fileobj)
                key.fileobj.close()
            else:
                require(sum(map(len, self.output.values())) + len(chunk) <= MAX_CAPTURE,
                        "command_output_bound")
                self.output[key.data].extend(chunk)

    def finish(self, seconds: float) -> int:
        end = time.monotonic() + seconds
        while self.process.poll() is None or self.selector.get_map():
            require(time.monotonic() < end, "command_deadline")
            self.drain()
        return self.process.wait()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for key in list(self.selector.get_map().values()):
            self.selector.unregister(key.fileobj)
            key.fileobj.close()
        self.selector.close()


def command(arguments: list[str], seconds: float = 2) -> tuple[int, str]:
    capture = Capture(arguments)
    try:
        code = capture.finish(seconds)
        return code, bytes(capture.output["stdout"]).decode("utf-8", "replace")
    finally:
        capture.close()


def unit_show(unit: str) -> dict[str, str]:
    code, text = command(["/usr/bin/systemctl", "show", unit, "--no-pager",
                          "--property=LoadState,MainPID,Result,ExecMainCode,ExecMainStatus,ActiveState"])
    state = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if state.get("LoadState") == "not-found":
        return {"LoadState": "not-found"}
    if code:
        return {"Result": "unavailable"}
    return state


def await_owned_keeper(capture: Capture, unit: str,
                       host: dict[str, dict[str, int]], seconds: float = 8) -> dict[str, object]:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        capture.drain()
        if capture.process.poll() is not None:
            # Reap/drain both pipes before reporting a real startup failure.
            capture.finish(2)
            raise ProbeFailure("keeper_start_failed")
        records = [json.loads(line) for line in bytes(capture.output["stdout"]).split(b"\n")[:-1]
                   if line.startswith(b'{"')]
        if not any(item.get("stage") == "keeper_verified" and item.get("ok") is True
                   for item in records):
            continue
        pid = int(unit_show(unit).get("MainPID", "0"))
        if not pid:
            continue
        try:
            return verify_owned_keeper(pid, unit, host)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
            # MainPID/procfs may change during service initialization/exit.
    raise ProbeFailure("keeper_ready_deadline")


def verify_owned_keeper(pid: int, unit: str, host: dict[str, dict[str, int]]) -> dict[str, object]:
    """Read-only verifier. Returned ns identities can be compared in later probes."""
    import fcntl

    base = Path("/proc") / str(pid)
    require(identity(base / "exe") == identity("/usr/bin/python3"), "keeper_python_exe")
    require(read(base / "cmdline", 4096).encode().split(b"\0")[:-1] ==
            [b"/usr/bin/python3", b"-I", b"-B", b"/opt/keeper.py", b"--keeper"], "keeper_argv")
    status = parse_status(read(base / "status"))
    uid = list(map(int, status["Uid"].split()))
    gid = list(map(int, status["Gid"].split()))
    require(len(uid) == len(gid) == 4 and min(uid + gid) > 0 and
            len(set(uid)) == len(set(gid)) == 1, "host_nonroot_identity")
    require(set(map(int, status["Groups"].split())) <= {gid[0]}, "host_groups")
    require(zero_caps(status) and status["NoNewPrivs"].strip() == "1", "host_caps_zero")
    require(id_map(read(base / "uid_map")) == (0, uid[0], 1), "host_uid_map")
    require(id_map(read(base / "gid_map")) == (0, gid[0], 1), "host_gid_map")
    require(read(base / "setgroups").strip() == "deny", "setgroups_denied")
    user = identity(base / "ns/user")
    net = identity(base / "ns/net")
    require(user != host["user"] and net != host["net"], "namespaces_distinct")
    with open(base / "ns/net", "rb") as net_file, open(base / "ns/user", "rb") as user_file:
        owner_fd = fcntl.ioctl(net_file.fileno(), 0xB701)  # NS_GET_USERNS
        try:
            owner_stat = os.fstat(owner_fd)
            require({"dev": owner_stat.st_dev, "ino": owner_stat.st_ino} == user,
                    "network_owner_userns")
        finally:
            os.close(owner_fd)
        owner_uid = bytearray(4)
        fcntl.ioctl(user_file.fileno(), 0xB704, owner_uid, True)  # NS_GET_OWNER_UID
        require(struct.unpack("=I", owner_uid)[0] == uid[0], "userns_owner_uid")
        parent_fd = fcntl.ioctl(user_file.fileno(), 0xB702)  # NS_GET_PARENT
        try:
            parent = os.fstat(parent_fd)
            require({"dev": parent.st_dev, "ino": parent.st_ino} == host["user"],
                    "userns_parent_initial")
        finally:
            os.close(parent_fd)
    cgroup = read(base / "cgroup").strip()
    require(cgroup.startswith("0::/") and "\n" not in cgroup and
            ".." not in Path(cgroup[3:]).parts and
            cgroup.endswith("/" + unit), "own_unit_cgroup")
    return {"pid": pid, "host_uid": uid[0], "host_gid": gid[0], "userns": user,
            "netns": net, "owner_uid": uid[0], "parent_userns": host["user"]}


def keeper() -> int:
    import fcntl
    import socket

    status = parse_status(read("/proc/self/status"))
    require(os.getuid() == os.geteuid() == os.getgid() == os.getegid() == 0,
            "keeper_mapped_root")
    require(zero_caps(status) and status["NoNewPrivs"].strip() == "1", "keeper_caps_zero")
    require(set(os.getgroups()) <= {0}, "keeper_groups")
    require(read("/proc/self/setgroups").strip() == "deny", "keeper_setgroups")
    for filename in ("uid_map", "gid_map"):
        mapping = id_map(read("/proc/self/" + filename))
        require(mapping[0] == 0 and mapping[1] > 0 and mapping[2] == 1, "keeper_" + filename)
    interfaces = {line.split(":", 1)[0].strip()
                  for line in read("/proc/self/net/dev").splitlines() if ":" in line}
    require(interfaces == {"lo"}, "keeper_lo_only")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        flags = fcntl.ioctl(channel, 0x8913, struct.pack("16sH14x", b"lo", 0))
        require(not struct.unpack_from("H", flags, 16)[0] & 1, "keeper_lo_down")
    routes4, routes6 = read("/proc/self/net/route"), read("/proc/self/net/ipv6_route")
    routes_safe = ipv4_routes_safe(routes4) and ipv6_routes_safe(routes6)
    if not routes_safe:
        emit("route_diagnostic", False, **route_diagnostic(routes4, routes6))
    require(routes_safe, "keeper_no_forwarding_routes")
    group_text = read("/proc/self/cgroup").strip()
    require(group_text.startswith("0::/") and "\n" not in group_text and
            ".." not in Path(group_text[3:]).parts, "keeper_cgroup_path")
    group = Path("/sys/fs/cgroup") / group_text[3:].lstrip("/")
    require(group.name.startswith(PREFIX) and group.name.endswith(".service"), "keeper_own_unit")
    expected = {"cpu.max": "20000 100000", "memory.max": "134217728",
                "memory.high": "100663296", "memory.swap.max": "0", "pids.max": "16"}
    require(all(read(group / name).strip() == value for name, value in expected.items()),
            "keeper_resource_limits")
    emit("keeper_verified", True, pid=os.getpid(), userns=identity("/proc/self/ns/user"),
         netns=identity("/proc/self/ns/net"), capabilities_zero=True, lo_down=True,
         no_forwarding_routes=True)
    time.sleep(12)
    return 0


def properties(bundle: str, user: str) -> list[str]:
    values = ["Type=exec", "DynamicUser=yes", "User=" + user, "PrivateUsers=no",
              "PrivateNetwork=no", "UMask=0077", "CPUQuota=20%", "CPUQuotaPeriodSec=100ms",
              "MemoryHigh=96M", "MemoryMax=128M", "MemorySwapMax=0", "TasksMax=16",
              "LimitNOFILE=64", "LimitCORE=0", "LimitFSIZE=1M", "RuntimeMaxSec=20",
              "TimeoutStartSec=5", "TimeoutStopSec=2", "SendSIGKILL=yes", "Restart=no",
              "KillMode=control-group", "OOMPolicy=stop", "Nice=19", "IOSchedulingClass=idle",
              "NoNewPrivileges=yes", "PrivateDevices=yes", "PrivateTmp=no",
              "ProtectHome=tmpfs", "ProtectSystem=strict", "ProtectProc=invisible",
              "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
              "RestrictNamespaces=user net", "RestrictSUIDSGID=yes", "RestrictRealtime=yes",
              "LockPersonality=yes", "SystemCallArchitectures=native",
              "CapabilityBoundingSet=CAP_SYS_ADMIN", "AmbientCapabilities=CAP_SYS_ADMIN",
              "UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME",
              "RestrictAddressFamilies=AF_INET AF_INET6",
              "InaccessiblePaths=-/run/docker.sock -/run/containerd -/run/user -/tmp -/var/tmp",
              "BindReadOnlyPaths=" + bundle + ":/opt",
              "TemporaryFileSystem=/dev/shm:ro,size=4K,nr_inodes=16,mode=000,nosuid,nodev,noexec"]
    return ["--property=" + value for value in values]


def orchestrator() -> int:
    require(sys.platform == "linux" and os.geteuid() == 0, "linux_root_required")
    require(Path("/opt").is_dir() and Path("/var/tmp").is_dir(), "existing_mountpoints")
    host = {"net": identity("/proc/self/ns/net"), "user": identity("/proc/self/ns/user")}
    require(host["net"] == identity("/proc/1/ns/net") and
            host["user"] == identity("/proc/1/ns/user"), "host_initial_namespaces")
    suffix = secrets.token_hex(6)
    unit = PREFIX + suffix + ".service"
    user = "awpn-" + suffix
    bundle = Path(tempfile.mkdtemp(prefix="astrbot-wechat-owned-netns.", dir="/var/tmp"))
    capture = None
    started = False
    result = 1
    failure: dict[str, object] = {}
    failure_unit: dict[str, str] = {"Result": "not_observed"}
    verified: dict[str, object] = {}
    try:
        source = read(Path(__file__).resolve(), 65536).encode()
        with open(bundle / "keeper.py", "xb") as stream:
            stream.write(source)
        os.chmod(bundle / "keeper.py", 0o444)
        os.chmod(bundle, 0o755)
        arguments = ["/usr/bin/systemd-run", "--system", "--unit=" + unit,
                     "--collect", "--wait", "--pipe", *properties(str(bundle), user),
                     "/usr/bin/unshare", "--user", "--map-root-user", "--net",
                     "/usr/bin/setpriv", "--bounding-set=-all", "--inh-caps=-all",
                     "--ambient-caps=-all", "--no-new-privs", "/usr/bin/python3",
                     "-I", "-B", "/opt/keeper.py", "--keeper"]
        emit("starting_trusted_keeper", True, unit=unit)
        started = True
        capture = Capture(arguments)
        verified = await_owned_keeper(capture, unit, host)
        require(capture.finish(24) == 0, "keeper_exit_failed")
        result = 0
    except (ProbeFailure, OSError, ValueError, KeyError) as error:
        failure = {"stage": getattr(error, "stage", "probe_exception"),
                   "errno": getattr(error, "errno", getattr(error, "number", None))}
        if started:
            try:
                failure_unit = unit_show(unit)  # Capture before stop can rewrite Result/ExecMainStatus.
            except Exception:
                failure_unit = {"Result": "unavailable_at_failure"}
    finally:
        if started:
            try:
                command(["/usr/bin/systemctl", "stop", unit], 5)
                diagnostics = unit_show(unit)
                if not cleanup_confirmed(diagnostics):
                    result = 1
                    if not failure:
                        failure = {"stage": "cleanup_unconfirmed"}
            except Exception:
                diagnostics = {"Result": "cleanup_unconfirmed"}
                result = 1
                if not failure:
                    failure = {"stage": "cleanup_unconfirmed"}
        else:
            diagnostics = {"Result": "not_started"}
        if capture:
            try:
                capture.finish(3)
            except ProbeFailure:
                pass
            stderr = stderr_summary(bytes(capture.output["stderr"]))
            child_failures = keeper_failure_summary(bytes(capture.output["stdout"]))
            capture.close()
        else:
            stderr = {"source": "not_started", "errno_candidates": []}
            child_failures = []
        try:
            (bundle / "keeper.py").unlink(missing_ok=True)
            bundle.rmdir()
        except OSError:
            result = 1
            failure = {"stage": "bundle_cleanup_failed"}
        if host != {"net": identity("/proc/self/ns/net"), "user": identity("/proc/self/ns/user")}:
            result = 1
            failure = {"stage": "host_namespace_changed"}
    if result:
        emit(str(failure.get("stage", "probe_failed")), False,
             errno=failure.get("errno"), unit=unit, unit_result=failure_unit,
             cleanup_result=diagnostics, cleanup_confirmed=cleanup_confirmed(diagnostics),
             keeper_failures=child_failures, stderr=stderr)
        return 1
    emit("owned_namespace_verified", True, **verified)
    print("OWNED_NETNS_VERIFIED", flush=True)
    return 0


def main() -> int:
    try:
        if sys.argv[1:] == ["--keeper"]:
            return keeper()
        require(not sys.argv[1:], "unexpected_arguments")
        def interrupted(number: int, _frame: object) -> None:
            raise ProbeFailure("interrupted", number)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, interrupted)
        return orchestrator()
    except (ProbeFailure, OSError, ValueError, KeyError) as error:
        emit(getattr(error, "stage", "probe_exception"), False,
             errno=getattr(error, "errno", getattr(error, "number", None)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
