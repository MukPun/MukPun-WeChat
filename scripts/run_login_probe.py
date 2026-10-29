#!/usr/bin/env python3
"""One short native login over the already tested user-mode TAP transport.

Run explicitly as root on Linux after review. No nft, modprobe, package install,
host route change, or existing-service operation. QR bytes are relayed in memory.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("login_tap_helpers", HERE / "probe-slirp-tap.py")
tap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tap
SPEC.loader.exec_module(tap)
owned, require, Failure = tap.owned, tap.require, tap.ProbeFailure
_paths_spec = importlib.util.spec_from_file_location('login_project_paths', HERE / 'project_paths.py')
_paths = importlib.util.module_from_spec(_paths_spec)
_paths_spec.loader.exec_module(_paths)
ASSET = _paths.NATIVE_ASSET
# v1.2.0 + upstream voice-format fix 28f8ea85; exact source-only backport.
ASSET_SIZE = 34886144
ASSET_SHA = "6f4ac76232f48744fbf015f677b255a0162170252023ccf2ab677f5fac2555f7"
DIAG_SIZE = 16528
DIAG_SHA = "e6d908008c1284eb56d4b69a44d66a214881d1593c94685513c0f646a3420654"
# Both szlong and szshort were observed resolving successfully in the completed
# post-approval login. Keep this fixed domain set; never follow arbitrary names.
DOMAINS = ("long.weixin.qq.com", "short.weixin.qq.com", "szlong.weixin.qq.com", "szshort.weixin.qq.com")
MAX_ADDRESSES_PER_DOMAIN = 8  # Four fixed domains must fit the unchanged total of 32.
PREFIX = "astrbot-wechat-login-"
NETWORK = {"ipv4": "10.0.2.100/24", "gateway": "10.0.2.2"}
MAX_CLIENT_LINE = 2 * 1024 * 1024
MAX_PNG = 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
RESOLVER = "import json,socket,sys;print(json.dumps(sorted({x[4][0] for x in socket.getaddrinfo(sys.argv[1],80,socket.AF_INET,socket.SOCK_STREAM)})))"
FRESH_LOGIN_MODES = ("--login", "--login-diagnostic", "--qr-only", "--receive-contact", "--send-contact")
ALLOW_FRESH_LOGIN = "--allow-fresh-login"


def wire_record(record: dict[str, object]) -> None:
    # Windows ConPTY may wrap and repeat a character at column 80. Keep every
    # physical line short and reassemble the JSON in memory at the display end.
    raw = json.dumps(record, sort_keys=True, ensure_ascii=True)
    print("AWP-BEGIN")
    for offset in range(0, len(raw), 64):
        print("AWP " + raw[offset:offset + 64])
    print("AWP-END", flush=True)


def emit(stage: str, **fields: object) -> None:
    wire_record({"stage": stage, **fields})


def properties_changed(values: list[str], **changes: str) -> list[str]:
    result = [item for item in values
              if item.removeprefix("--property=").split("=", 1)[0] not in changes]
    return result + ["--property=" + key + "=" + value for key, value in changes.items()]


def public_ipv4(address: str, host_addresses: set[str]) -> bool:
    try:
        value = ipaddress.IPv4Address(address)
        return (str(value) == address and value.is_global and not value.is_multicast and
                not value.is_reserved and not value.is_private and not value.is_loopback and
                not value.is_link_local and address not in host_addresses and address != "203.0.113.10")
    except ipaddress.AddressValueError:
        return False


def resolve_hosts(host_addresses: set[str]) -> dict[str, list[str]]:
    hosts = {}
    for name in DOMAINS:
        addresses = json.loads(tap.command(["/usr/bin/python3", "-I", "-B", "-c", RESOLVER, name], 4))
        require(isinstance(addresses, list) and 0 < len(addresses) <= 16 and
                all(isinstance(item, str) and public_ipv4(item, host_addresses) for item in addresses),
                "dns_public_ipv4_only")
        # Validate the entire answer before retaining a smaller candidate set.
        # A late private/host address must never be hidden by this restriction.
        hosts[name] = sorted(set(addresses))[:MAX_ADDRESSES_PER_DOMAIN]
    return hosts


def source_text(path: Path) -> str:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_size <= 131072, "source_regular_bounded")
    return owned.read(path, 131072)


def write_public(bundle: Path, name: str, value: str) -> None:
    with open(bundle / name, "x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
    os.chmod(bundle / name, 0o444)


def copy_asset(bundle: Path) -> None:
    source = os.open(ASSET, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(source)
        require(stat.S_ISREG(info.st_mode) and info.st_size == ASSET_SIZE, "asset_size_type")
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(source, "rb", closefd=False) as incoming, open(bundle / "wx", "xb") as outgoing:
            while chunk := incoming.read(65536):
                count += len(chunk)
                require(count <= ASSET_SIZE, "asset_size_type")
                digest.update(chunk)
                outgoing.write(chunk)
        require(count == ASSET_SIZE and digest.hexdigest() == ASSET_SHA, "asset_hash")
    finally:
        os.close(source)
    os.chmod(bundle / "wx", 0o555)
    with open(bundle / "wx", "rb") as stream:
        require(hashlib.file_digest(stream, "sha256").hexdigest() == ASSET_SHA, "staged_asset_hash")


def keeper_source(seconds: int) -> str:
    text = source_text(HERE / "probe-owned-netns.py")
    require(text.count("time.sleep(12)") == 1 and seconds in {45, 150, 360, 930}, "keeper_source_contract")
    return text.replace("time.sleep(12)", f"time.sleep({seconds})")


def copy_diagnostic(bundle: Path) -> None:
    descriptor = os.open(_paths.RUNTIME_ROOT / "assets/login-dns-probe.so", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_size == DIAG_SIZE, "diagnostic_library_type_size")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(DIAG_SIZE + 1)
        require(len(data) == DIAG_SIZE and hashlib.sha256(data).hexdigest() == DIAG_SHA,
                "diagnostic_library_hash")
        with open(bundle / "login-dns-probe.so", "xb") as stream:
            stream.write(data)
        os.chmod(bundle / "login-dns-probe.so", 0o444)
    finally:
        os.close(descriptor)


def diagnostic_hostname(host: object) -> bool:
    if (not isinstance(host, str) or not 0 < len(host) <= 253 or
            not host.endswith((".weixin.qq.com", ".wechat.com"))):
        return False
    labels = host.split(".")
    return all(0 < len(label) <= 63 and label[0].isalnum() and label[-1].isalnum() and
               all("a" <= char <= "z" or "0" <= char <= "9" or char == "-" for char in label)
               for label in labels)


def helper_properties(bundle: Path, user: str, parent: str, seconds: int,
                      allowed: list[str]) -> list[str]:
    values = properties_changed(tap.helper_properties(str(bundle), user, parent), RuntimeMaxSec=str(seconds))
    values += ["--property=IPAddressAllow=" + " ".join(address + "/32" for address in allowed)]
    return values


def client_properties(bundle: Path, user: str, parent: str, netfd: int, seconds: int) -> list[str]:
    bindings = str(bundle) + ":/opt " + " ".join(
        f"{bundle / name}:{target}" for name, target in
        (("hosts", "/etc/hosts"), ("nsswitch.conf", "/etc/nsswitch.conf"), ("resolv.conf", "/etc/resolv.conf")))
    # This is a fresh client unit, so omit helper-only settings entirely. An
    # empty BindPaths assignment can serialize an empty source, which PID 1
    # rejects as non-absolute; there is no existing client mount to reset.
    base = [value for value in tap.helper_properties(str(bundle), user, parent)
            if value.removeprefix("--property=").split("=", 1)[0]
            not in {"BindPaths", "DeviceAllow", "IPAddressDeny"}]
    values = properties_changed(base,
        RuntimeMaxSec=str(seconds), CapabilityBoundingSet="", AmbientCapabilities="",
        TasksMax="32", LimitNOFILE="128", LimitFSIZE="16M", RestrictNamespaces="yes",
        RestrictAddressFamilies="AF_INET AF_INET6", IPAccounting="no",
        BindReadOnlyPaths=bindings,
        NetworkNamespacePath=f"/proc/{os.getpid()}/fd/{netfd}",
        TemporaryFileSystem="/var/lib:rw,size=32M,nr_inodes=1024,mode=1777,nosuid,nodev,noexec /dev/shm:ro,size=4K,nr_inodes=16,mode=000,nosuid,nodev,noexec")
    return values


def target_command(netfd: int, expected: dict[str, int], host: dict[str, int], arguments: list[str]) -> None:
    item = os.fstat(netfd)
    actual = {"dev": item.st_dev, "ino": item.st_ino}
    require(actual == expected and actual != host, "guest_namespace_fd_gate")
    allowed = (["link", "set", "lo", "up"], ["addr", "add", "10.0.2.100/24", "dev", "tap0"],
               ["link", "set", "tap0", "up"], ["route", "add", "default", "via", "10.0.2.2", "dev", "tap0"])
    require(arguments in allowed, "guest_fixed_network_command")
    tap.command(["/usr/bin/nsenter", f"--net=/proc/self/fd/{netfd}", "--", tap.IP, *arguments], 2, (netfd,))


def parse_client_line(line: bytes, codes: set[str], checks: set[str]) -> dict[str, object]:
    require(len(line) <= MAX_CLIENT_LINE, "client_line_bound")
    record = json.loads(line)
    require(isinstance(record, dict) and isinstance(record.get("stage"), str), "client_record_shape")
    if record["stage"] == "login_qr":
        require(set(record) == {"stage", "png_base64"} and isinstance(record["png_base64"], str), "qr_record_shape")
        text = record["png_base64"]
        require(len(text) <= (MAX_PNG + 2) // 3 * 4, "qr_body_bound")
        try:
            image = base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            raise Failure("qr_base64_invalid") from None
        require(0 < len(image) <= MAX_PNG and image.startswith(PNG_MAGIC) and
                base64.b64encode(image).decode("ascii") == text, "qr_png_invalid")
        return {"stage": "login_qr", "png_base64": text}
    if record["stage"] == "isolation_check":
        require(set(record) == {"stage", "name", "ok"} and isinstance(record["name"], str) and
                record["name"] in checks and type(record["ok"]) is bool, "client_isolation_enum")
    elif record["stage"] == "login_state":
        require(set(record) == {"stage", "state"} and isinstance(record["state"], str) and
                record["state"] in {"starting", "resuming", "awaiting_scan", "awaiting_approval", "authenticating", "succeeded"},
                "client_state_enum")
    elif record["stage"] == "login_result":
        require(set(record) == {"stage", "ok", "state", "code"} and type(record["ok"]) is bool and
                isinstance(record["state"], str) and record["state"] in {"succeeded", "qr_available", "failed"} and
                isinstance(record["code"], str) and record["code"] in codes, "client_result_enum")
    elif record["stage"] == "login_dns":
        require(set(record) == {"stage", "host", "result"} and diagnostic_hostname(record["host"])
                and type(record["result"]) is int and -1024 <= record["result"] <= 1024,
                "client_dns_diagnostic")
    elif record["stage"] == "contact_profile":
        require(set(record) == {"stage", "state", "reason"} and
                isinstance(record["state"], str) and record["state"] in {"created", "existing", "pending", "unavailable"} and
                isinstance(record["reason"], str) and record["reason"] in {"", "login_database_password_required", "profile_format_key_required"} and
                (record["state"] == "pending" or record["reason"] == ""), "client_profile_enum")
    elif record["stage"] == "contact_state":
        require(record == {"stage": "contact_state", "state": "target_matched"}, "client_contact_enum")
    elif record["stage"] == "contact_waiting":
        require(set(record) == {"stage", "marker", "seconds"} and isinstance(record["marker"], str) and
                re.fullmatch(r"AWTEST-[0-9a-f]{8}", record["marker"]) and
                type(record["seconds"]) is int and 1 <= record["seconds"] <= 600, "client_contact_waiting")
    elif record["stage"] == "contact_observation":
        counters = {"event_count", "target_incoming_count", "target_live_count", "text_checked"}
        require(set(record) == {"stage", "marker_matched"} | counters and
                all(type(record[name]) is int and 0 <= record[name] <= 20000 for name in counters) and
                type(record["marker_matched"]) is bool and
                record["text_checked"] <= record["target_live_count"] <= record["target_incoming_count"] <= record["event_count"] and
                (not record["marker_matched"] or record["text_checked"] > 0), "client_contact_observation")
    elif record["stage"] == "contact_result":
        require(set(record) == {"stage", "ok", "code"} and type(record["ok"]) is bool and
                isinstance(record["code"], str) and record["code"] in codes and
                record["ok"] == (record["code"] == "ok"), "client_contact_result")
    elif record["stage"] == "send_state":
        require(set(record) == {"stage", "state"} and isinstance(record["state"], str) and
                record["state"] in {"target_matched", "submitting", "queued", "accepted", "failed", "needs_review", "cancelled"},
                "client_send_state")
    elif record["stage"] == "send_result":
        require(set(record) == {"stage", "ok", "code", "submitted", "server_confirmed"} and
                all(type(record[name]) is bool for name in ("ok", "submitted", "server_confirmed")) and
                isinstance(record["code"], str) and record["code"] in codes and
                record["ok"] == (record["code"] == "ok") and record["server_confirmed"] == record["ok"] and
                (not record["ok"] or record["submitted"]), "client_send_result")
    else:
        raise Failure("client_record_not_allowed")
    return record


class ClientCapture(tap.Capture):
    def drain(self, delay: float = 0.02) -> None:
        for key, _ in self.selector.select(delay):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                self.selector.unregister(key.fileobj)
                key.fileobj.close()
            elif key.data == "stdout":
                self.stdout_total = getattr(self, "stdout_total", 0) + len(chunk)
                require(len(self.output["stdout"]) + len(chunk) <= MAX_CLIENT_LINE, "client_line_bound")
                self.output["stdout"].extend(chunk)
            else:
                self.stderr_total = getattr(self, "stderr_total", 0) + len(chunk)
                self.output["stderr"].extend(chunk)
                del self.output["stderr"][:-4096]


def client_diagnostic(capture: ClientCapture) -> dict[str, object]:
    """Only expose short setup errors before the controller has emitted anything.

    The controller emits isolation checks before starting native or creating its
    private token. Once any stdout has arrived, stderr remains labels-only.
    """
    stderr = bytes(capture.output["stderr"])[-4096:]
    markers = {
        "namespace": b"NAMESPACE", "user": b"/USER", "group": b"/GROUP", "exec": b"/EXEC",
        "invalid_argument": b"Invalid argument", "parse_failed": b"Failed to parse",
        "set_properties_failed": b"Failed to set unit properties", "mount_setup_failed": b"Failed to set up mount",
        "dbus_invalid_args": b"InvalidArgs", "unknown_property": b"Unknown property",
        "property_not_found": b"Property", "python_module_not_found": b"ModuleNotFoundError",
        "python_import_error": b"ImportError", "python_syntax_error": b"SyntaxError",
        "permission_denied": b"Permission denied", "operation_not_permitted": b"Operation not permitted",
    }
    properties = ("IPAddressDeny", "DeviceAllow", "BindPaths", "BindReadOnlyPaths", "NetworkNamespacePath",
                  "PrivateNetwork", "RestrictAddressFamilies", "RestrictNamespaces", "TemporaryFileSystem")
    paths = ("/etc/hosts", "/etc/nsswitch.conf", "/etc/resolv.conf", "/opt/login_client_probe.py",
             "/opt/qr_login_protocol.py", "/opt/empty_client_probe.py", "/opt/network.json")
    total = getattr(capture, "stderr_total", len(capture.output["stderr"]))
    startup_message = None
    if (getattr(capture, "stdout_total", len(capture.output.get("stdout", b""))) == 0
            and 0 < total <= 512 and total == len(stderr)
            and all(byte in (9, 10, 13) or 32 <= byte <= 126 for byte in stderr)):
        startup_message = stderr.decode("ascii").strip()
    return {"exit_code": capture.process.poll(), "stderr_bytes_count": total,
            "startup_message": startup_message,
            "stderr": owned.stderr_summary(stderr),
            "namespace_failure": capture.process.poll() == 226 or b"NAMESPACE" in stderr,
            "exec_failure": capture.process.poll() == 203 or b"/EXEC" in stderr,
            "labels": sorted(label for label, marker in markers.items() if marker in stderr),
            "property_labels": [name for name in properties if name.encode("ascii") in stderr],
            "path_labels": [name for name in paths if name.encode("ascii") in stderr]}


def relay(capture: ClientCapture, codes: set[str], checks: set[str], expected_state: str = "succeeded",
          contact_status: dict | None = None, send_status: dict | None = None) -> bool:
    complete = False
    while b"\n" in capture.output["stdout"]:
        line, _, rest = capture.output["stdout"].partition(b"\n")
        capture.output["stdout"][:] = rest
        value = parse_client_line(bytes(line), codes, checks)
        if value.get("stage") == "contact_result" and contact_status is not None:
            require(not contact_status, "contact_result_repeated")
            contact_status.update(ok=value["ok"], code=value["code"])
        if value.get("stage") == "send_result" and send_status is not None:
            require(not send_status, "send_result_repeated")
            send_status.update(ok=value["ok"], code=value["code"], submitted=value["submitted"],
                               server_confirmed=value["server_confirmed"])
        complete = complete or (value.get("stage") == "login_result" and value.get("ok") is True and
                                value.get("state") == expected_state and value.get("code") == "ok")
        wire_record(value)
    return complete


def orchestrator(mode: str, *, allow_fresh_login: bool = False) -> int:
    # This mechanical guard is not user authorization. Check for a recoverable
    # session and obtain authorization before deliberately choosing a fresh QR.
    if allow_fresh_login is not True:
        emit("login_probe_fresh_login_blocked", ok=False)
        return 1
    require(sys.platform == "linux" and os.geteuid() == 0, "linux_root_required")
    seconds = 45 if mode == "--qr-only" else 930 if mode == "--receive-contact" else 360 if mode == "--send-contact" else 150
    host = {kind: owned.identity("/proc/self/ns/" + kind) for kind in ("user", "net")}
    require(host == {kind: owned.identity("/proc/1/ns/" + kind) for kind in host}, "host_initial_namespaces")
    require(Path("/sys/class/misc/tun").exists(), "tun_already_registered")
    tun = os.stat("/dev/net/tun")
    require(stat.S_ISCHR(tun.st_mode) and (os.major(tun.st_rdev), os.minor(tun.st_rdev)) == (10, 200), "tun_device")
    before_network, before_business = tap.read_network(), tap.business_snapshot()
    interfaces = json.loads(tap.command([tap.IP, "-j", "addr", "show"]))
    host_addresses = {item["local"] for interface in interfaces for item in interface.get("addr_info", [])}
    hosts = resolve_hosts(host_addresses)
    allowed = sorted({address for addresses in hosts.values() for address in addresses})
    emit("login_allowlist", address_count=len(allowed))
    require(1 <= len(allowed) <= 32, "login_allowlist_budget")
    suffix = secrets.token_hex(6)
    parent = "astrbotwechatprobe" + suffix + ".slice"
    keeper_unit, helper_unit, client_unit = owned.PREFIX + suffix + ".service", tap.HELPER_PREFIX + suffix + ".service", PREFIX + suffix + ".service"
    owner_user, client_user = "awpo-" + suffix, "awpl-" + suffix
    for unit in (parent, keeper_unit, helper_unit, client_unit):
        tap.require_unit_absent(unit)
    import tempfile
    bundle = Path(tempfile.mkdtemp(prefix="astrbot-wechat-login.", dir="/var/tmp"))
    captures, attempted, stopped, descriptors = {}, [], [], []
    accounting = {"ingress_bytes": None, "egress_bytes": None}
    netfd, info = None, None
    result, failure = 1, "login_probe_failed"
    baseline = {"host_namespace_unchanged": None, "host_network_unchanged": None,
                "business_processes_unchanged": None, "host_ra_timer_changed": None}
    try:
        require(os.statvfs(bundle).f_bavail * os.statvfs(bundle).f_frsize >= 128 * 1024 * 1024 and
                not os.statvfs(bundle).f_flag & os.ST_NOEXEC, "staging_headroom_exec")
        copy_asset(bundle)
        if mode == "--login-diagnostic":
            copy_diagnostic(bundle)
        for name in ("empty_client_probe.py", "qr_login_protocol.py", "login_client_probe.py", "contact_receive_protocol.py", "contact_send_protocol.py"):
            write_public(bundle, name, source_text(HERE / name))
        write_public(bundle, "keeper.py", keeper_source(seconds))
        write_public(bundle, "launcher.py", tap.LAUNCHER)
        write_public(bundle, "server.toml", "schemaVersion = 1\n")
        write_public(bundle, "hosts", "127.0.0.1 localhost\n::1 localhost\n" + "".join(
            f"{address} {name}\n" for name in DOMAINS for address in hosts[name]))
        write_public(bundle, "nsswitch.conf", "passwd: files\ngroup: files\nhosts: files\n")
        write_public(bundle, "resolv.conf", "")
        os.chmod(bundle, 0o755)
        attempted.append((parent, False))
        tap.start_slice(parent)
        tap.cgroup_limits(parent, tap.LIMITS)
        deadline = time.monotonic() + seconds
        keeper_args = ["/usr/bin/systemd-run", "--system", "--unit=" + keeper_unit, "--collect", "--wait", "--pipe", "--quiet",
            *properties_changed(owned.properties(str(bundle), owner_user), Slice=parent, RuntimeMaxSec=str(seconds)),
            "/usr/bin/unshare", "--user", "--map-root-user", "--net", "/usr/bin/setpriv", "--bounding-set=-all",
            "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", "/usr/bin/python3", "-I", "-B", "/opt/keeper.py", "--keeper"]
        attempted.append((keeper_unit, True))
        captures[keeper_unit] = tap.Capture(keeper_args)
        info = owned.await_owned_keeper(captures[keeper_unit], keeper_unit, host, seconds=5)
        pid, ticks = int(info["pid"]), tap.start_ticks(int(info["pid"]))
        pidfd = os.pidfd_open(pid, 0)
        descriptors.append(pidfd)
        for kind in ("user", "net"):
            descriptor = os.open(f"/proc/{pid}/ns/{kind}", os.O_RDONLY | os.O_CLOEXEC)
            descriptors.append(descriptor)
            item = os.fstat(descriptor)
            require({"dev": item.st_dev, "ino": item.st_ino} == info[kind + "ns"], "pinned_namespace_identity")
        netfd = descriptors[-1]
        require(tap.start_ticks(pid) == ticks and owned.verify_owned_keeper(pid, keeper_unit, host) == info, "keeper_identity_stable")
        tap.check_target(tap.read_network(netfd, info["netns"], host["net"]), False)
        helper_args = ["/usr/bin/systemd-run", "--system", "--unit=" + helper_unit, "--collect", "--wait", "--pipe", "--quiet",
            *helper_properties(bundle, owner_user, parent, seconds, allowed), "/usr/bin/python3", "-I", "-B", "/opt/launcher.py"]
        helper_args += list(map(str, (pid, ticks, info["host_uid"], info["host_gid"], info["userns"]["dev"], info["userns"]["ino"],
            info["netns"]["dev"], info["netns"]["ino"], host["user"]["dev"], host["user"]["ino"], host["net"]["dev"], host["net"]["ino"]))) + [helper_unit]
        attempted.append((helper_unit, True))
        captures[helper_unit] = tap.Capture(helper_args)
        helper = captures[helper_unit]
        ready_deadline = time.monotonic() + 4
        while bytes(helper.output["stdout"]) != b"1":
            require(time.monotonic() < ready_deadline, "helper_ready_deadline")
            helper.drain()
            require(bytes(helper.output["stdout"]) in (b"", b"1"), "helper_ready_exact")
            tap.require_helper_running(helper)
        helper_pid = int(tap.details(helper_unit).get("MainPID", "0"))
        tap.verify_helper(helper_pid, helper_unit, info, host, parent)
        tap.check_target(tap.read_network(netfd, info["netns"], host["net"]), True)
        for arguments in (["link", "set", "lo", "up"], ["addr", "add", "10.0.2.100/24", "dev", "tap0"],
                          ["link", "set", "tap0", "up"], ["route", "add", "default", "via", "10.0.2.2", "dev", "tap0"]):
            target_command(netfd, info["netns"], host["net"], arguments)
        metadata = {"schemaVersion": 1, "netns": info["netns"], "hostUserns": host["user"], "hostNetns": host["net"],
                    "keeperOwnerUid": info["host_uid"], "loginUnit": client_unit, **NETWORK, "allowedIPv4": allowed}
        write_public(bundle, "network.json", json.dumps(metadata, sort_keys=True) + "\n")
        # The controller's public constants are loaded only to validate its
        # explicitly documented output enums; its guarded main is never called.
        controller_spec = importlib.util.spec_from_file_location("login_output_contract", HERE / "login_client_probe.py")
        controller = importlib.util.module_from_spec(controller_spec)
        sys.modules[controller_spec.name] = controller
        controller_spec.loader.exec_module(controller)
        codes, checks = set(controller.RELAY_CODES), set(controller.ISOLATION_CHECKS)
        if mode == "--receive-contact":
            receive = controller._companion("contact_receive_protocol")
            codes.update(receive.RESULT_CODES | {"profile_pending", "profile_unavailable", "receive_failed"})
        elif mode == "--send-contact":
            send = controller._companion("contact_send_protocol")
            codes.update(send.RESULT_CODES | {"profile_pending", "profile_unavailable", "send_failed", "outcome_unknown"})
        client_args = ["/usr/bin/systemd-run", "--system", "--unit=" + client_unit, "--collect", "--wait", "--pipe", "--quiet",
            *client_properties(bundle, client_user, parent, netfd, seconds), "/usr/bin/python3", "-I", "-B", "/opt/login_client_probe.py", mode, ALLOW_FRESH_LOGIN]
        attempted.append((client_unit, True))
        captures[client_unit] = ClientCapture(client_args)
        client = captures[client_unit]
        completed = False
        contact_status = {}
        send_status = {}
        emit("login_transport_ready", ok=True)
        while client.process.poll() is None or client.selector.get_map():
            require(time.monotonic() < deadline, "login_outer_deadline")
            require(helper.process.poll() is None and captures[keeper_unit].process.poll() is None, "transport_holder_exited")
            helper.drain(0)
            client.drain()
            completed = relay(client, codes, checks, "qr_available" if mode == "--qr-only" else "succeeded",
                              contact_status if mode == "--receive-contact" else None,
                              send_status if mode == "--send-contact" else None) or completed
        require(not client.output["stdout"] and client.process.wait() == 0 and completed, "login_client_failed")
        require(mode != "--receive-contact" or contact_status.get("ok") is True, "contact_receive_failed")
        require(mode != "--send-contact" or send_status.get("ok") is True, "contact_send_failed")
        result = 0
    except (Failure, OSError, ValueError, KeyError, AttributeError, subprocess.SubprocessError) as error:
        failure = getattr(error, "stage", "login_probe_exception")
    finally:
        if helper_unit in captures:
            try:
                counters = tap.details(helper_unit)
                for label, property_name in (("ingress_bytes", "IPIngressBytes"), ("egress_bytes", "IPEgressBytes")):
                    raw = counters.get(property_name, "")
                    if raw.isdecimal() and 0 <= int(raw) < 2**64 - 1:
                        accounting[label] = int(raw)
            except Exception:
                pass  # Unknown accounting is reported as null, never as zero.
        for unit, service in reversed(attempted):
            try:
                tap.stop_unit(unit, service)
                stopped.append(unit)
                if unit == helper_unit and netfd is not None and info is not None:
                    state = tap.read_network(netfd, info["netns"], host["net"])
                    require({link["ifname"] for link in state["links"]} == {"lo"}, "tap_cleanup_unconfirmed")
            except Exception:
                result, failure = 1, "login_cleanup_unconfirmed"
        for capture in captures.values():
            try:
                capture.finish(2)
            except Exception:
                result, failure = 1, "login_capture_cleanup_failed"
            finally:
                try:
                    capture.close()
                except Exception:
                    result, failure = 1, "login_capture_cleanup_failed"
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                result, failure = 1, "login_fd_cleanup_failed"
        try:
            for name in ("wx", "keeper.py", "launcher.py", "empty_client_probe.py", "qr_login_protocol.py", "login_client_probe.py",
                         "server.toml", "network.json", "hosts", "nsswitch.conf", "resolv.conf", "login-dns-probe.so",
                         "contact_receive_protocol.py", "contact_send_protocol.py"):
                (bundle / name).unlink(missing_ok=True)
            bundle.rmdir()
        except OSError:
            result, failure = 1, "login_bundle_cleanup_failed"
        baseline, baseline_failures = tap.compare_baselines(host, before_network, before_business)
        if baseline_failures:
            result, failure = 1, baseline_failures[0]
    # Counters are not a hard bandwidth or byte quota; no such claim is made.
    diagnostics = {label: tap.capture_summary(captures[unit], keeper=label == "keeper")
                   for label, unit in (("keeper", keeper_unit), ("helper", helper_unit)) if unit in captures}
    if client_unit in captures:
        diagnostics["client"] = client_diagnostic(captures[client_unit])
    emit("login_probe_finished", ok=result == 0, code="ok" if result == 0 else failure,
         explicit_stop=stopped, **baseline, ip_accounting=accounting, transport=diagnostics)
    return result


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if args in ([mode] for mode in FRESH_LOGIN_MODES):
        emit("login_probe_fresh_login_blocked", ok=False)
        return 1
    if args not in ([mode, ALLOW_FRESH_LOGIN] for mode in FRESH_LOGIN_MODES):
        emit("login_probe_arguments", ok=False)
        return 2
    stopping = False
    def interrupted(number: int, _frame: object) -> None:
        nonlocal stopping
        if not stopping:
            stopping = True
            raise Failure("login_interrupted", number)
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, interrupted)
    try:
        return orchestrator(args[0], allow_fresh_login=True)
    except (Failure, OSError, ValueError, KeyError, AttributeError, subprocess.SubprocessError):
        emit("login_probe_preflight", ok=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
