#!/usr/bin/env python3
"""Isolated QR login, with explicitly selected private-chat test phases."""
from __future__ import annotations

import base64
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid


def _companion(name: str):
    path = Path(__file__).resolve().parent / (name + ".py")
    spec = importlib.util.spec_from_file_location("_login_probe_" + name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("companion_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


empty = _companion("empty_client_probe")
qr = _companion("qr_login_protocol")
empty.CONTROLLER = Path("/opt/login_client_probe.py")
empty.CGROUP_PREFIX = "astrbot-wechat-login-"
NETWORK = Path("/opt/network.json")
DNS_PROBE = "/opt/login-dns-probe.so"
DNS_BUFFER_LIMIT = 8192
DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
LOGIN_DOMAINS = frozenset({"long.weixin.qq.com", "short.weixin.qq.com",
                           "szlong.weixin.qq.com", "szshort.weixin.qq.com"})
FRESH_LOGIN_MODES = ("--login", "--login-diagnostic", "--qr-only", "--receive-contact", "--send-contact")
ALLOW_FRESH_LOGIN = "--allow-fresh-login"
UNIT_RE = re.compile(r"astrbot-wechat-login-[0-9a-f]{12}\.service\Z", re.ASCII)
RELAY_STATES = frozenset(qr.ACTIVE_STATES | {"succeeded"})
RELAY_CODES = frozenset({
    "ok", "isolation_failed", "native_spawn_failed", "startup_failed", "fresh_state_failed",
    "login_failed", "login_cancelled", "login_expired", "login_rejected", "login_needs_login",
    "cleanup_failed", "arguments", "interrupted", "protocol_failed", "deadline_exceeded", "metadata_failed",
    "fresh_login_blocked",
} | qr.UPSTREAM_CODES)
ISOLATION_CHECKS = frozenset({
    "nonroot_identity", "supplementary_groups", "capabilities_zero", "no_new_privileges",
    "root_readonly", "usr_readonly", "etc_readonly", "binary_readonly", "config_readonly",
    "controller_readonly", "docker_socket_masked", "containerd_masked", "guest_network",
    "unix_sockets_forbidden", "dedicated_cgroup", "cpu_limit", "memory_max", "memory_high",
    "memory_swap_zero", "tasks_limit", "cgroup_inventory", "work_tmpfs_32m_bounded",
    "tmp_masked_blocked", "var_tmp_masked_blocked", "dev_shm_readonly_bounded",
    "work_write_delete", "binary_sha256", "binary_executable_mount", "minimal_config",
    "network_metadata", "guest_namespace", "host_user_namespace", "distinct_dynamic_uid",
    "exact_login_cgroup", "companions_readonly",
})


def emit(record: dict) -> None:
    print(json.dumps(record, separators=(",", ":"), ensure_ascii=True), flush=True)


def _result(ok: bool, state: str, code: str) -> None:
    emit({"stage": "login_result", "ok": ok, "state": state,
          "code": code if code in RELAY_CODES else "protocol_failed"})


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value


class LoginDNSDiagnostic:
    """Bounded, best-effort relay of public DNS results from the native child."""

    def __init__(self):
        descriptor = os.open(DNS_PROBE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            mounts = empty.parse_mounts(Path("/proc/self/mountinfo").read_text())
            if not (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
                    not info.st_mode & 0o222 and 0 < info.st_size <= 1024 * 1024 and
                    empty._readonly(mounts, DNS_PROBE)):
                raise ValueError("diagnostic_library")
        finally:
            os.close(descriptor)
        self.read_fd, self.write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.buffer = b""
        self.discarding = False

    def close_write(self):
        if self.write_fd is not None:
            try:
                os.close(self.write_fd)
            except OSError:
                pass
            self.write_fd = None

    def close(self):
        self.close_write()
        if self.read_fd is not None:
            try:
                os.close(self.read_fd)
            except OSError:
                pass
            self.read_fd = None
        self.buffer = b""

    @staticmethod
    def relay(line: bytes):
        try:
            value = json.loads(line.decode("ascii"), object_pairs_hook=_unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
            if not isinstance(value, dict) or set(value) != {"host", "result"}:
                return
            host, result = value["host"], value["result"]
            if (not isinstance(host, str) or len(host) > 253 or
                    not host.endswith((".weixin.qq.com", ".wechat.com")) or
                    not all(DNS_LABEL_RE.fullmatch(label) for label in host.split(".")) or
                    type(result) is not int or not -1024 <= result <= 1024):
                return
            emit({"stage": "login_dns", "host": host, "result": result})
        except Exception:
            pass  # Malformed diagnostics must not affect authentication or reveal raw data.

    def drain(self):
        if self.read_fd is None:
            return
        # Bound each callback's work as well as the retained partial line.
        remaining = DNS_BUFFER_LIMIT
        while remaining:
            try:
                chunk = os.read(self.read_fd, min(4096, remaining))
            except OSError:
                return
            if not chunk:
                return
            remaining -= len(chunk)
            parts = chunk.split(b"\n")
            for index, part in enumerate(parts):
                if not self.discarding:
                    if len(self.buffer) + len(part) > DNS_BUFFER_LIMIT:
                        self.buffer = b""
                        self.discarding = True
                    else:
                        self.buffer += part
                if index < len(parts) - 1:
                    if not self.discarding:
                        self.relay(self.buffer)
                    self.buffer = b""
                    self.discarding = False


def validate_metadata(value: object) -> dict:
    keys = {"schemaVersion", "netns", "hostUserns", "hostNetns", "keeperOwnerUid",
            "loginUnit", "ipv4", "gateway", "allowedIPv4"}
    if not isinstance(value, dict) or set(value) != keys or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValueError("metadata")
    for key in ("netns", "hostUserns", "hostNetns"):
        identity = value[key]
        if (not isinstance(identity, dict) or set(identity) != {"dev", "ino"} or
                any(type(identity[field]) is not int or identity[field] <= 0 for field in identity)):
            raise ValueError("identity")
    if (type(value["keeperOwnerUid"]) is not int or value["keeperOwnerUid"] <= 0 or
            not isinstance(value["loginUnit"], str) or not UNIT_RE.fullmatch(value["loginUnit"]) or
            value["ipv4"] != "10.0.2.100/24" or value["gateway"] != "10.0.2.2" or
            value["netns"] == value["hostNetns"]):
        raise ValueError("layout")
    addresses = value["allowedIPv4"]
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 32:
        raise ValueError("addresses")
    if any(not isinstance(item, str) for item in addresses) or addresses != sorted(set(addresses)):
        raise ValueError("addresses")
    for address in addresses:
        parsed = ipaddress.IPv4Address(address)
        if not parsed.is_global or str(parsed) != address:
            raise ValueError("addresses")
    return value


def read_metadata() -> dict:
    descriptor = os.open(NETWORK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
                not info.st_mode & 0o022 and 0 < info.st_size <= 8192):
            raise ValueError("metadata_file")
        raw = os.read(descriptor, 8193)
        if len(raw) > 8192:
            raise ValueError("metadata_file")
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
        return validate_metadata(value)
    finally:
        os.close(descriptor)


def _identity(path: str) -> dict:
    info = os.stat(path)
    return {"dev": info.st_dev, "ino": info.st_ino}


def guest_network() -> bool:
    """Read interface configuration only; never connect or send a packet."""
    import fcntl
    names = {line.split(":", 1)[0].strip() for line in
             Path("/proc/net/dev").read_text().splitlines() if ":" in line}
    if names != {"lo", "tap0"} or set(os.listdir("/sys/class/net")) != names:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        for name, expected_ip, expected_mask in (
                ("lo", "127.0.0.1", "255.0.0.0"), ("tap0", "10.0.2.100", "255.255.255.0")):
            request = struct.pack("256s", name.encode("ascii"))
            flags = struct.unpack_from("H", fcntl.ioctl(channel.fileno(), 0x8913, request), 16)[0]
            address = socket.inet_ntoa(fcntl.ioctl(channel.fileno(), 0x8915, request)[20:24])
            netmask = socket.inet_ntoa(fcntl.ioctl(channel.fileno(), 0x891b, request)[20:24])
            if not flags & 1 or address != expected_ip or netmask != expected_mask:
                return False
    routes = []
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            return False
        routes.append((fields[0], fields[1].upper(), fields[2].upper(), fields[7].upper()))
    return sorted(routes) == sorted([
        ("tap0", "00000000", "0202000A", "00000000"),
        ("tap0", "0002000A", "00000000", "00FFFFFF"),
    ])


def validate_isolation(metadata: dict) -> list[tuple[str, bool]]:
    checks = [("guest_network", guest_network() if name == "loopback_only" else passed)
              if name == "loopback_only" else (name, passed)
              for name, passed in empty.validate_isolation()]

    def check(name, work):
        try:
            checks.append((name, bool(work())))
        except Exception:
            checks.append((name, False))

    check("network_metadata", lambda: validate_metadata(metadata) == metadata)
    check("guest_namespace", lambda: _identity("/proc/self/ns/net") == metadata["netns"] and
          _identity("/proc/self/ns/net") != metadata["hostNetns"])
    check("host_user_namespace", lambda: _identity("/proc/self/ns/user") == metadata["hostUserns"])
    check("distinct_dynamic_uid", lambda: os.getuid() > 0 and os.geteuid() > 0 and
          os.getuid() != metadata["keeperOwnerUid"] and os.geteuid() != metadata["keeperOwnerUid"])
    suffix = metadata["loginUnit"][len("astrbot-wechat-login-"):-len(".service")]
    check("exact_login_cgroup", lambda: Path("/proc/self/cgroup").read_text().splitlines() ==
          ["0::/astrbotwechatprobe" + suffix + ".slice/" + metadata["loginUnit"]])
    def readonly_companions():
        mounts = empty.parse_mounts(Path("/proc/self/mountinfo").read_text())
        if not all(empty._readonly(mounts, path) for path in (
                "/opt/network.json", "/opt/empty_client_probe.py", "/opt/qr_login_protocol.py",
                "/opt/contact_receive_protocol.py",
                "/opt/contact_send_protocol.py",
                "/etc/hosts", "/etc/nsswitch.conf", "/etc/resolv.conf")):
            return False
        if (Path("/etc/resolv.conf").read_bytes() != b"" or
                Path("/etc/nsswitch.conf").read_text() != "passwd: files\ngroup: files\nhosts: files\n"):
            return False
        hosts = [line.split() for line in Path("/etc/hosts").read_text().splitlines()]
        if hosts[:2] != [["127.0.0.1", "localhost"], ["::1", "localhost"]]:
            return False
        pairs = hosts[2:]
        return (all(len(pair) == 2 and pair[0] in metadata["allowedIPv4"] and
                    pair[1] in LOGIN_DOMAINS for pair in pairs) and
                {pair[0] for pair in pairs} == set(metadata["allowedIPv4"]) and
                {pair[1] for pair in pairs} == LOGIN_DOMAINS)
    check("companions_readonly", readonly_companions)
    return checks


def check_fresh_state(token: str) -> None:
    status, _ = empty.get_json("/v1/accounts")
    if status not in (401, 403):
        raise empty.ProbeFailure("anonymous_accounts_denied")
    status, accounts = empty.get_json("/v1/accounts", token)
    if status != 200 or accounts.get("items") != []:
        raise empty.ProbeFailure("empty_accounts")
    status, value = empty.get_json("/v1/status", token)
    version = value.get("version")
    if (status != 200 or value.get("state") != "ready" or not isinstance(version, dict) or
            version.get("backend") != "native" or version.get("version") != empty.EXPECTED_VERSION):
        raise empty.ProbeFailure("native_ready_version")


def receive_contact(profile, account: str, token: str) -> None:
    """A separate, explicitly selected read-only phase before disconnect."""
    emit({"stage": "contact_profile", "state": profile.state, "reason": profile.reason})
    if profile.state not in {"created", "existing"}:
        emit({"stage": "contact_result", "ok": False,
              "code": "profile_pending" if profile.state == "pending" else "profile_unavailable"})
        return
    receive = _companion("contact_receive_protocol")
    try:
        result = receive.ContactReceiveClient(account, token, port=empty.PORT).run(profile.deadline, emit)
        code = "ok" if result == "ok" else "receive_failed"
    except receive.ContactError as error:
        code = error.code if error.code in receive.RESULT_CODES else "receive_failed"
    except Exception:
        code = "receive_failed"
    emit({"stage": "contact_result", "ok": code == "ok", "code": code})


def send_contact(profile, account: str, token: str) -> None:
    """One fixed test send; invoke only after the human approves its text."""
    emit({"stage": "contact_profile", "state": profile.state, "reason": profile.reason})
    if profile.state not in {"created", "existing"}:
        emit({"stage": "send_result", "ok": False,
              "code": "profile_pending" if profile.state == "pending" else "profile_unavailable",
              "submitted": False, "server_confirmed": False})
        return
    send = _companion("contact_send_protocol")
    try:
        client = send.ContactSendClient(account, token, port=empty.PORT)
    except Exception:
        emit({"stage": "send_result", "ok": False, "code": "send_failed",
              "submitted": False, "server_confirmed": False})
        return
    try:
        result = client.run(profile.deadline, emit)
        if (not isinstance(result, send.SendResult) or result.code not in send.RESULT_CODES or
                any(type(value) is not bool for value in (result.ok, result.submitted, result.server_confirmed)) or
                result.ok != (result.code == "ok") or
                result.server_confirmed != result.ok or (result.ok and not result.submitted)):
            raise ValueError("invalid_result")
        fields = {"ok": result.ok, "code": result.code, "submitted": result.submitted,
                  "server_confirmed": result.server_confirmed}
    except Exception:
        # An unexpected error after run began cannot establish that no POST left.
        fields = {"ok": False, "code": "outcome_unknown", "submitted": True, "server_confirmed": False}
    emit({"stage": "send_result", **fields})


class Interrupted(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    # This flag only prevents accidental fresh login; it does not grant user
    # authorization or replace checking for an existing recoverable session.
    if arguments in ([mode] for mode in FRESH_LOGIN_MODES):
        _result(False, "failed", "fresh_login_blocked")
        return 1
    if arguments not in ([mode, ALLOW_FRESH_LOGIN] for mode in FRESH_LOGIN_MODES):
        _result(False, "failed", "arguments")
        return 2
    mode = arguments[0]
    qr_only = mode == "--qr-only"
    receive_mode = mode == "--receive-contact"
    send_mode = mode == "--send-contact"
    deadline = time.monotonic() + (30.0 if qr_only else 900.0 if receive_mode else 330.0 if send_mode else 120.0)
    process = None
    diagnostic = None
    stopped = False
    success = False
    state = "failed"
    code = "isolation_failed"
    interrupted = False
    old_handlers = {}

    def interrupt(_number, _frame):
        nonlocal interrupted
        interrupted = True
        raise Interrupted()

    try:
        for number in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
            if number is None:
                continue
            old_handlers[number] = signal.signal(number, interrupt)
        code = "metadata_failed"
        metadata = read_metadata()
        code = "isolation_failed"
        checks = validate_isolation(metadata)
        if not checks or any(name not in ISOLATION_CHECKS for name, _ in checks):
            raise ValueError("checks")
        for name, passed in checks:
            emit({"stage": "isolation_check", "name": name, "ok": bool(passed)})
        if not all(passed for _, passed in checks):
            raise ValueError("isolation")
        if mode == "--login-diagnostic":
            diagnostic = LoginDNSDiagnostic()
        code = "startup_failed"
        environment = empty.prepare_directories()
        spawn_options = {}
        if diagnostic is not None:
            environment = {**environment, "LD_PRELOAD": DNS_PROBE,
                           "AW_LOGIN_DIAG_FD": str(diagnostic.write_fd)}
            spawn_options["pass_fds"] = (diagnostic.write_fd,)
        code = "native_spawn_failed"
        try:
            process = subprocess.Popen(
                [str(empty.BINARY), "--server-config", str(empty.CONFIG), "--data-dir", str(empty.DATA),
                 "--listen", "127.0.0.1:26174", "serve"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, env=environment, cwd=str(empty.BASE), **spawn_options)
        finally:
            if diagnostic is not None:
                diagnostic.close_write()
        code = "startup_failed"
        empty.wait_for_health(process)
        token = empty.read_token()
        code = "fresh_state_failed"
        check_fresh_state(token)
        remaining = deadline - time.monotonic() - 4.0
        if remaining <= 2:
            raise qr.ProtocolError("deadline_exceeded")
        account = "login-probe-" + uuid.uuid4().hex
        window_options = {"extended_window": True} if receive_mode or send_mode else {}
        transport = qr.LoopbackHTTPTransport(account, token, port=empty.PORT, **window_options)
        client = qr.LoginClient(account, transport, total_timeout=remaining, **window_options)
        code = "protocol_failed"

        def show_qr(image):
            if diagnostic is not None:
                diagnostic.drain()
            if (not isinstance(image.png, bytes) or len(image.png) > qr.PNG_LIMIT or
                    not image.png.startswith(qr.PNG_MAGIC)):
                raise ValueError("qr")
            emit({"stage": "login_qr", "png_base64": base64.b64encode(image.png).decode("ascii")})

        def show_state(value):
            nonlocal stopped
            if diagnostic is not None:
                diagnostic.drain()
            if value not in RELAY_STATES:
                raise ValueError("state")
            if value == "succeeded":
                # The library has already confirmed disconnect before this callback.
                stopped = empty.stop_child(process)
                if not stopped:
                    raise qr.ProtocolError("cleanup_failed")
            emit({"stage": "login_state", "state": value})

        work = {}
        if receive_mode:
            work["on_authenticated"] = lambda profile: receive_contact(profile, account, token)
        elif send_mode:
            work["on_authenticated"] = lambda profile: send_contact(profile, account, token)
        result = client.run(show_qr, show_state, qr_only=qr_only, **work)
        if result.state not in {"succeeded", "qr_available"} or result.disconnected is not True:
            raise qr.ProtocolError("cleanup_failed")
        success, state, code = True, result.state, "ok"
    except qr.ProtocolError as error:
        code = "cleanup_failed" if error.cleanup_failed else (
            error.code if error.code in RELAY_CODES else "protocol_failed")
    except Interrupted:
        code = "interrupted"
    except Exception:
        pass  # Never print arbitrary exceptions, upstream bodies or private identifiers.
    finally:
        if diagnostic is not None:
            diagnostic.drain()
        if process is not None and not stopped:
            try:
                stopped = empty.stop_child(process)
            except BaseException:
                stopped = False
            if not stopped:
                success, state, code = False, "failed", "cleanup_failed"
        if diagnostic is not None:
            diagnostic.drain()
            diagnostic.close()
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
    if interrupted:
        success, state, code = False, "failed", "interrupted"
    _result(success, state if success else "failed", code)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
