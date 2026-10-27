#!/usr/bin/env python3
"""Offline TAP-only probe. Explicit root execution on Linux is a separate step.

Re-runs the owned-namespace gate before starting slirp. No guest application,
interface configuration, DNS, firewall mutation, host route mutation or external
connection is performed. Only the exact random units created here are stopped.
The companion probe-owned-netns.py is imported unchanged and copied as keeper.py.
"""
from __future__ import annotations

import importlib.util
import copy
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time

MODULE_PATH = Path(__file__).resolve().with_name("probe-owned-netns.py")
SPEC = importlib.util.spec_from_file_location("tap_probe_owned_namespace", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("owned_namespace_module_unavailable")
owned = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owned)
require, ProbeFailure = owned.require, owned.ProbeFailure
ENV = owned.ENV
MAX_CAPTURE = owned.MAX_CAPTURE
HELPER_PREFIX = "astrbot-wechat-slirp-tap-helper-"
IP = "/usr/sbin/ip"
LIMITS = {"cpu.max": "20000 100000", "memory.high": "100663296",
          "memory.max": "134217728", "memory.swap.max": "0", "pids.max": "32"}
BUSINESS_NAMES = {"dockerd", "containerd", "containerd-shim", "docker-proxy", "frpc", "frps"}
HELPER_MASKS = ("/run/docker.sock", "/run/docker", "/run/containerd", "/run/user",
                "/run/dbus", "/run/systemd/private", "/run/frp", "/run/frpc", "/run/frps",
                "/tmp", "/var/tmp")
# Comparison-only identity sentinel: cannot collide with any JSON field value.
POSITIVE_RA_EXPIRY = object()
LAUNCHER_FAILURE_STAGES = frozenset({"filesystem_ro", "docker_mask", "containerd_mask",
    "mask_access", "home_hidden", "root_hidden", "identity", "cgroup", "keeper_tick",
    "namespace_open_user", "namespace_open_net", "namespace_identity", "pin_inherit", "exec"})

# This trusted launcher pins its own namespace descriptors: root's /proc/PID/fd
# paths would not be accessible to DynamicUser. fd >= 10 avoids ready-fd 3.
LAUNCHER = r'''
import fcntl, json, os, pathlib, re, stat, sys
current_stage = 'identity'
mask_diagnostics = {}
def stage(label):
    global current_stage
    current_stage = label
def check(ok, label):
    stage(label)
    if not ok:
        raise RuntimeError("helper_preflight")
def ident(path):
    s = os.stat(path)
    return (s.st_dev, s.st_ino)
def ticks(pid):
    return int(pathlib.Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[19])
def inaccessible(path, label):
    stage(label)
    try:
        pathlib.Path(path).stat()
    except (FileNotFoundError, PermissionError):
        return True
    return False
def read_mountinfo():
    with open('/proc/self/mountinfo', 'r', encoding='utf-8') as stream:
        text = stream.read(65537)
    check(len(text) <= 65536, 'filesystem_ro')
    mounts = []
    for line in text.splitlines():
        fields = line.split(' - ', 1)[0].split()
        check(len(fields) >= 6 and ' - ' in line, 'filesystem_ro')
        decode = lambda value: re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1),8)), value)
        mounts.append((decode(fields[3]), decode(fields[4]), set(fields[5].split(','))))
    return mounts
def masked_mount(mounts, path):
    matches = [mount for mount in mounts if mount[1] == path]
    return bool(matches and '/systemd/inaccessible/' in matches[-1][0] and 'ro' in matches[-1][2])
def mode_type(mode):
    for label, predicate in (('socket',stat.S_ISSOCK), ('directory',stat.S_ISDIR),
                            ('regular',stat.S_ISREG), ('character',stat.S_ISCHR),
                            ('block',stat.S_ISBLK), ('fifo',stat.S_ISFIFO)):
        if predicate(mode):
            return label
    return 'other'
def check_filesystem():
    stage('filesystem_ro')
    mounts = read_mountinfo()
    for path in ('/', '/usr', '/etc'):
        check(bool(os.statvfs(path).f_flag & os.ST_RDONLY), 'filesystem_ro')
    stage('docker_mask')
    docker = os.stat('/run/docker.sock')
    docker_masked = masked_mount(mounts, '/run/docker.sock')
    mask_diagnostics['docker_mask'] = {'mode':stat.S_IMODE(docker.st_mode),
        'type':mode_type(docker.st_mode), 'mask':docker_masked}
    check(stat.S_IMODE(docker.st_mode) == 0 and docker_masked, 'docker_mask')
    stage('containerd_mask')
    containerd = os.stat('/run/containerd')
    containerd_masked = masked_mount(mounts, '/run/containerd')
    mask_diagnostics['containerd_mask'] = {'mode':stat.S_IMODE(containerd.st_mode),
        'type':mode_type(containerd.st_mode), 'mask':containerd_masked}
    check(stat.S_IMODE(containerd.st_mode) == 0 and stat.S_ISDIR(containerd.st_mode) and
          containerd_masked, 'containerd_mask')
    stage('mask_access')
    for path in ('/run/docker.sock', '/run/containerd'):
        check(all(not os.access(path, mode) for mode in (os.R_OK, os.W_OK, os.X_OK)), 'mask_access')
    check(inaccessible('/home/bridgeuser/.ssh', 'home_hidden'), 'home_hidden')
    check(inaccessible('/root/.ssh', 'root_hidden'), 'root_hidden')
try:
    check(len(sys.argv) == 14, 'identity')
    pid, start, uid, gid, ud, ui, nd, ni, hud, hui, hnd, hni = map(int, sys.argv[1:13])
    unit = sys.argv[13]
    check(unit.startswith('astrbot-wechat-slirp-tap-helper-') and unit.endswith('.service'), 'identity')
    status = dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
    check(list(map(int,status['Uid'].split())) == [uid]*4 and uid > 0, 'identity')
    check(list(map(int,status['Gid'].split())) == [gid]*4 and gid > 0, 'identity')
    check(set(map(int,status['Groups'].split())) <= {gid}, 'identity')
    check(all(int(status[k].strip(),16) == 0 for k in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')), 'identity')
    check(status['NoNewPrivs'].strip() == '1', 'identity')
    check(ident('/proc/self/ns/user') == (hud,hui) and ident('/proc/self/ns/net') == (hnd,hni), 'identity')
    check((ud,ui) != (hud,hui) and (nd,ni) != (hnd,hni), 'identity')
    stage('cgroup')
    group = pathlib.Path('/proc/self/cgroup').read_text().strip()
    check(group.startswith('0::/') and '\n' not in group and group.endswith('/'+unit), 'cgroup')
    base = pathlib.Path('/sys/fs/cgroup') / group[3:].lstrip('/')
    expected = {'cpu.max':'20000 100000','memory.max':'134217728','memory.high':'100663296','memory.swap.max':'0','pids.max':'16'}
    check(all((base/name).read_text().strip() == value for name,value in expected.items()), 'cgroup')
    check_filesystem()
    stage('keeper_tick')
    check(ticks(pid) == start, 'keeper_tick')
    descriptors = []
    for kind, expected_id in (('user',(ud,ui)), ('net',(nd,ni))):
        stage('namespace_open_user' if kind == 'user' else 'namespace_open_net')
        original = os.open('/proc/'+str(pid)+'/ns/'+kind, os.O_RDONLY | os.O_CLOEXEC)
        try:
            stage('namespace_identity')
            item = os.fstat(original)
            check((item.st_dev,item.st_ino) == expected_id, 'namespace_identity')
            stage('pin_inherit')
            descriptor = fcntl.fcntl(original, fcntl.F_DUPFD_CLOEXEC, 10)
        finally:
            os.close(original)
        os.set_inheritable(descriptor, True)
        descriptors.append(descriptor)
    stage('keeper_tick')
    check(ticks(pid) == start, 'keeper_tick')
    command = ('exec /usr/bin/slirp4netns --netns-type=path '
               '--userns-path=/proc/self/fd/%d --disable-host-loopback '
               '--disable-dns --enable-seccomp --ready-fd=3 '
               '/proc/self/fd/%d tap0 3>&1 1>/dev/null') % tuple(descriptors)
    stage('exec')
    os.execve('/bin/sh', ['/bin/sh','-c',command],
              {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C'})
except Exception as error:
    number = getattr(error, 'errno', None)
    record = {'stage':current_stage, 'errno':number if type(number) is int and 0 < number < 4096 else None}
    if current_stage in ('docker_mask', 'containerd_mask') and current_stage in mask_diagnostics:
        record['mask_diagnostic'] = mask_diagnostics[current_stage]
    try:
        os.write(2,b'HELPER_PREFLIGHT_FAILED '+json.dumps(record,sort_keys=True).encode('ascii')+b'\n')
    except OSError:
        pass
    raise SystemExit(1)
'''


class Capture(owned.Capture):
    def __init__(self, arguments: list[str], pass_fds: tuple[int, ...] = ()):
        self.process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        close_fds=True, pass_fds=pass_fds, env=ENV)
        self.selector = selectors.DefaultSelector()
        self.output = {"stdout": bytearray(), "stderr": bytearray()}
        for name in self.output:
            pipe = getattr(self.process, name)
            os.set_blocking(pipe.fileno(), False)
            self.selector.register(pipe, selectors.EVENT_READ, name)

    def drain(self, delay: float = 0.01) -> None:
        super().drain(delay)


def command_result(arguments: list[str], seconds: float = 2,
                   pass_fds: tuple[int, ...] = ()) -> tuple[int, str]:
    capture = Capture(arguments, pass_fds)
    try:
        code = capture.finish(seconds)
        return code, bytes(capture.output["stdout"]).decode("utf-8", "strict")
    finally:
        capture.close()


def command(arguments: list[str], seconds: float = 2,
            pass_fds: tuple[int, ...] = ()) -> str:
    code, output = command_result(arguments, seconds, pass_fds)
    require(code == 0, "read_command_failed")
    return output


def load_state(unit: str) -> tuple[int, dict[str, str]]:
    code, output = command_result(["/usr/bin/systemctl", "show", unit, "--no-pager",
                                   "--property=MainPID,ActiveState,LoadState,ControlGroup"])
    return code, dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def require_unit_absent(unit: str) -> None:
    # show/GetUnitProperties can implicitly load and synthesize an unknown
    # slice. list-units only enumerates already loaded units and is read-only.
    output = command(["/usr/bin/systemctl", "list-units", "--all", "--full",
                      "--plain", "--no-legend", "--no-pager", unit])
    require(not output.strip(), "unit_name_already_exists")


def details(unit: str) -> dict[str, str]:
    output = command(["/usr/bin/systemctl", "show", unit, "--no-pager",
                      "--property=MainPID,ActiveState,Result,ControlGroup,IPIngressBytes,IPEgressBytes"])
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def start_slice(name: str) -> None:
    require(name.startswith("astrbotwechatprobe") and name.endswith(".slice") and
            "-" not in name and "/" not in name, "slice_name")
    command(["/usr/bin/busctl", "--system", "call", "org.freedesktop.systemd1",
             "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
             "StartTransientUnit", "ssa(sv)a(sa(sv))", name, "fail", "6",
             "CPUQuotaPerSecUSec", "t", "200000", "CPUQuotaPeriodUSec", "t", "100000",
             "MemoryHigh", "t", "100663296", "MemoryMax", "t", "134217728",
             "MemorySwapMax", "t", "0", "TasksMax", "t", "32", "0"])


def cgroup_limits(unit: str, expected: dict[str, str]) -> str:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = details(unit)
        path = state.get("ControlGroup", "")
        if state.get("ActiveState") != "active" or not path:
            time.sleep(0.02)
            continue
        require(path.startswith("/") and ".." not in Path(path).parts and
                Path(path).name == unit, "exact_cgroup_path")
        base = Path("/sys/fs/cgroup") / path.lstrip("/")
        require(all(owned.read(base / key).strip() == value for key, value in expected.items()),
                "cgroup_limits")
        return path
    raise ProbeFailure("slice_ready_deadline")


def start_ticks(pid: int) -> int:
    return int(owned.read(f"/proc/{pid}/stat", 4096).rsplit(")", 1)[1].split()[19])


def business_snapshot() -> list[tuple[int, str, int]]:
    values = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            name = owned.read(entry / "comm", 256).strip()
            # Linux comm truncates containerd-shim variants at 15 characters.
            if name in BUSINESS_NAMES or name.startswith("containerd-shim"):
                values.append((int(entry.name), name, start_ticks(int(entry.name))))
        except FileNotFoundError:
            continue
    return sorted(values)


def read_network(namespace_fd: int | None = None,
                 expected: dict[str, int] | None = None,
                 host_net: dict[str, int] | None = None,
                 seconds: float = 2) -> dict[str, object]:
    prefix: list[str] = []
    passed: tuple[int, ...] = ()
    if namespace_fd is not None:
        item = os.fstat(namespace_fd)
        actual = {"dev": item.st_dev, "ino": item.st_ino}
        require(actual == expected and actual != host_net, "target_namespace_fd_gate")
        prefix = ["/usr/bin/nsenter", f"--net=/proc/self/fd/{namespace_fd}", "--"]
        passed = (namespace_fd,)
    queries = {"links": ["-j", "link", "show"],
               "routes4": ["-j", "-4", "route", "show", "table", "all"],
               "routes6": ["-j", "-6", "route", "show", "table", "all"]}
    if namespace_fd is not None:
        queries["addresses"] = ["-j", "addr", "show"]
    output = {}
    deadline = time.monotonic() + seconds
    for label, query in queries.items():
        remaining = deadline - time.monotonic()
        require(remaining > 0, "network_read_deadline")
        parsed = json.loads(command(prefix + [IP, *query], remaining, passed))
        require(isinstance(parsed, list), "network_json_list")
        output[label] = parsed
    return output


def check_target(state: dict[str, object], tap_expected: bool) -> None:
    links = state["links"]
    names = {item["ifname"] for item in links}
    require(names == ({"lo", "tap0"} if tap_expected else {"lo"}), "target_interfaces")
    require(all("UP" not in item.get("flags", []) for item in links), "target_interfaces_down")
    require(all(not item.get("addr_info") for item in state["addresses"]), "target_no_ip")
    for version in ("routes4", "routes6"):
        for route in state[version]:
            require(route.get("type") == "unreachable" and route.get("dev") == "lo" and
                    route.get("dst") == "default" and "gateway" not in route and
                    "nexthops" not in route, "target_no_forwarding_routes")


def helper_properties(bundle: str, user: str, parent: str) -> list[str]:
    removed = {"CapabilityBoundingSet", "AmbientCapabilities", "RestrictAddressFamilies",
               "RuntimeMaxSec", "InaccessiblePaths"}
    values = [value for value in owned.properties(bundle, user)
              if value.removeprefix("--property=").split("=", 1)[0] not in removed]
    values += ["--property=" + value for value in (
        "Slice=" + parent, "CapabilityBoundingSet=", "AmbientCapabilities=",
        "RuntimeMaxSec=8", "IPAddressDeny=any", "IPAccounting=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "InaccessiblePaths=" + " ".join("-" + path for path in HELPER_MASKS),
        "BindPaths=/dev/net/tun", "DevicePolicy=closed", "DeviceAllow=/dev/net/tun rw")]
    return values


def verify_helper(pid: int, unit: str, keeper_info: dict[str, object],
                  host: dict[str, dict[str, int]], parent: str) -> None:
    before = start_ticks(pid)
    base = Path("/proc") / str(pid)
    require(owned.identity(base / "exe") == owned.identity("/usr/bin/slirp4netns"), "helper_exe")
    argv = owned.read(base / "cmdline", 4096).encode().split(b"\0")[:-1]
    require(len(argv) == 9 and argv[0] == b"/usr/bin/slirp4netns" and
            argv[1] == b"--netns-type=path" and
            argv[2].startswith(b"--userns-path=/proc/self/fd/") and
            argv[3:7] == [b"--disable-host-loopback", b"--disable-dns", b"--enable-seccomp", b"--ready-fd=3"] and
            argv[7].startswith(b"/proc/self/fd/") and argv[8] == b"tap0", "helper_argv")
    fd_numbers = [int(argv[2].rsplit(b"/", 1)[1]), int(argv[7].rsplit(b"/", 1)[1])]
    require(len(set(fd_numbers)) == 2 and min(fd_numbers) >= 10, "helper_namespace_descriptors")
    require(all(owned.identity(base / "fd" / str(number)) == keeper_info[kind + "ns"]
                for number, kind in zip(fd_numbers, ("user", "net"))), "helper_pinned_namespaces")
    status = owned.parse_status(owned.read(base / "status"))
    require(list(map(int, status["Uid"].split())) == [keeper_info["host_uid"]] * 4 and
            list(map(int, status["Gid"].split())) == [keeper_info["host_gid"]] * 4,
            "helper_same_nonroot_identity")
    require(set(map(int, status["Groups"].split())) <= {keeper_info["host_gid"]}, "helper_groups")
    require(owned.zero_caps(status) and status["NoNewPrivs"].strip() == "1", "helper_caps_zero")
    require(owned.identity(base / "ns/net") == host["net"] and
            owned.identity(base / "ns/user") == host["user"], "helper_host_namespaces")
    group = owned.read(base / "cgroup").strip()
    require(group.startswith("0::/") and "\n" not in group and
            group.endswith("/" + parent + "/" + unit), "helper_exact_cgroup")
    require(start_ticks(pid) == before, "helper_process_unchanged")
    unit_state = details(unit)
    require(unit_state.get("MainPID") == str(pid) and unit_state.get("ActiveState") == "active",
            "helper_still_active")
    require(unit_state.get("IPIngressBytes") == "0" and unit_state.get("IPEgressBytes") == "0",
            "helper_zero_ip_accounting")


def require_helper_running(capture: Capture) -> None:
    if capture.process.poll() is not None:
        # Reap and drain both complete pipes before constructing safe diagnostics.
        capture.finish(2)
        raise ProbeFailure("helper_start_failed")


def capture_summary(capture: Capture, keeper: bool = False) -> dict[str, object]:
    stderr = bytes(capture.output["stderr"])
    bounded = stderr[-MAX_CAPTURE:]
    labels = {
        "setns_user": b"setns(CLONE_NEWUSER)",
        "setns_net": b"setns(CLONE_NEWNET)",
        "namespace_fd_path": b"/proc/self/fd/",
        "open_tun": b'open("/dev/net/tun")',
        "tun_setiff": b"ioctl(TUNSETIFF)",
        "device_up": b"cannot set device up",
        "device_address": b"cannot set device address",
        "slirp_new_failed": b"slirp_new failed",
        "create_slirp_failed": b"create_slirp failed",
        "seccomp_failed": b"enable_seccomp failed",
        "seccomp_load_failed": b"seccomp_load()",
        "sent_tapfd": b"sent tapfd=",
        "received_tapfd": b"received tapfd=",
    }
    value: dict[str, object] = {
        "stderr": owned.stderr_summary(stderr),
        "stderr_bytes_count": len(stderr),
        "helper_preflight_failed": b"HELPER_PREFLIGHT_FAILED" in bounded,
        "initialization_labels": sorted(label for label, marker in labels.items() if marker in bounded),
        "mount_path_labels": [label for label, path in
            (("run", b"/run"), ("dev_net_tun", b"/dev/net/tun"), ("opt", b"/opt"),
             ("tmp", b"/tmp"), ("var_tmp", b"/var/tmp"))
            if re.search(rb"(?<![A-Za-z0-9_./-])" + re.escape(path) + rb"(?=[/\s:'\"]|$)", bounded)],
        "namespace_failure": capture.process.poll() == 226 or b"NAMESPACE" in bounded,
        "exit_code": capture.process.poll(),
    }
    launcher_failures = []
    for line in bounded.split(b"\n"):
        if not line.startswith(b"HELPER_PREFLIGHT_FAILED "):
            continue
        try:
            record = json.loads(line[len(b"HELPER_PREFLIGHT_FAILED "):])
        except (ValueError, UnicodeError):
            continue
        if (not isinstance(record, dict) or not isinstance(record.get("stage"), str) or
                record["stage"] not in LAUNCHER_FAILURE_STAGES):
            continue
        number = record.get("errno")
        safe = {"stage": record["stage"], "errno": number if type(number) is int and 0 < number < 4096 else None}
        detail = record.get("mask_diagnostic")
        if record["stage"] in {"docker_mask", "containerd_mask"} and isinstance(detail, dict):
            mode, kind, masked = detail.get("mode"), detail.get("type"), detail.get("mask")
            if (type(mode) is int and 0 <= mode <= 0o7777 and
                    isinstance(kind, str) and kind in {"socket", "directory", "regular", "character", "block", "fifo", "other"} and
                    type(masked) is bool):
                safe["mask_diagnostic"] = {"mode": mode, "type": kind, "mask": masked}
        if safe not in launcher_failures:
            launcher_failures.append(safe)
        if len(launcher_failures) == 8:
            break
    value["launcher_failures"] = launcher_failures
    if keeper:
        value["keeper_failures"] = owned.keeper_failure_summary(bytes(capture.output["stdout"]))
    else:
        value["ready_exact"] = bytes(capture.output["stdout"]) == b"1"
    return value


def normalize_host_network(network: dict[str, object]) -> dict[str, object]:
    """Ignore only positive RA expiry clock values, preserving field presence.

    This is deliberately not applied to target-namespace reads or checks.
    Zero, missing, bool and other expiry representations retain their exact value.
    """
    normalized = copy.deepcopy(network)
    for route in normalized.get("routes6", []):
        if (isinstance(route, dict) and route.get("protocol") == "ra" and
                type(route.get("expires")) is int and route["expires"] > 0):
            route["expires"] = POSITIVE_RA_EXPIRY
    return normalized


def compare_baselines(host: dict[str, dict[str, int]], network: dict[str, object],
                      business: list[tuple[int, str, int]]) -> tuple[dict[str, bool | None], list[str]]:
    checks = (
        ("host_namespace_unchanged", lambda: {kind: owned.identity("/proc/self/ns/" + kind) for kind in host}, host),
        ("host_network_unchanged", read_network, network),
        ("business_processes_unchanged", business_snapshot, business),
    )
    flags: dict[str, bool | None] = {"host_ra_timer_changed": None}
    failures = []
    for label, snapshot, before in checks:
        try:
            current = snapshot()
            if label == "host_network_unchanged":
                normalized_before = normalize_host_network(before)
                normalized_current = normalize_host_network(current)
                flags[label] = normalized_current == normalized_before
                flags["host_ra_timer_changed"] = (
                    current.get("routes6") != before.get("routes6") and
                    normalized_current.get("routes6") == normalized_before.get("routes6"))
            else:
                flags[label] = current == before
            if not flags[label]:
                failures.append(label.removesuffix("_unchanged") + "_changed")
        except (ProbeFailure, OSError, ValueError, KeyError):
            flags[label] = None
            failures.append(label.removesuffix("_unchanged") + "_baseline_unavailable")
    return flags, failures


def stop_unit(unit: str, service: bool, cgroup: str | None = None) -> None:
    # --collect can remove a successfully stopped service before show. A missing
    # unit is acceptable only when systemctl show reports LoadState=not-found.
    code, before = load_state(unit)
    require(code == 0 or before.get("LoadState") == "not-found", "unit_query_failed")
    group = cgroup or before.get("ControlGroup", "")
    stop_code, _ = command_result(["/usr/bin/systemctl", "stop", unit], 4)
    show_code, state = load_state(unit)
    missing = state.get("LoadState") == "not-found"
    require((missing or stop_code == show_code == 0) and
            (missing or (state.get("ActiveState") == "inactive" and
             (not service or state.get("MainPID") == "0"))), "unit_stop_unconfirmed")
    if group:
        require(group.startswith("/") and ".." not in Path(group).parts and
                Path(group).name == unit, "cleanup_cgroup_path")
        process_file = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
        try:
            require(not owned.read(process_file).strip(), "unit_cgroup_not_empty")
        except FileNotFoundError:
            pass


def orchestrator() -> int:
    require(sys.platform == "linux" and os.geteuid() == 0, "linux_root_required")
    for name in (IP, "/usr/bin/slirp4netns", "/usr/bin/nsenter", "/usr/bin/busctl", "/opt", "/var/tmp"):
        require(Path(name).exists(), "required_path_missing")
    # Opening an unregistered misc device can request module autoload. Require
    # an already registered TUN driver instead of changing host module state.
    require(Path("/sys/class/misc/tun").exists(), "tun_driver_already_registered")
    tun = os.stat("/dev/net/tun")
    require(stat.S_ISCHR(tun.st_mode) and (os.major(tun.st_rdev), os.minor(tun.st_rdev)) == (10, 200),
            "expected_tun_device")
    host = {kind: owned.identity("/proc/self/ns/" + kind) for kind in ("user", "net")}
    require(host == {kind: owned.identity("/proc/1/ns/" + kind) for kind in host}, "host_initial_namespaces")
    before_network, before_business = read_network(), business_snapshot()
    suffix = secrets.token_hex(6)
    parent = "astrbotwechatprobe" + suffix + ".slice"
    keeper_unit = owned.PREFIX + suffix + ".service"
    helper_unit = HELPER_PREFIX + suffix + ".service"
    user = "awpt-" + suffix
    for unit in (parent, keeper_unit, helper_unit):
        require_unit_absent(unit)
    bundle = Path(tempfile.mkdtemp(prefix="astrbot-wechat-tap.", dir="/var/tmp"))
    captures: dict[str, Capture] = {}
    attempted: list[tuple[str, bool]] = []
    descriptors: list[int] = []
    failure: dict[str, object] = {}
    result = 1
    explicit_stops: list[str] = []
    unit_cgroups: dict[str, str] = {}
    pinned_net = None
    verified: dict[str, object] = {}
    baseline_flags: dict[str, bool | None] = {"host_namespace_unchanged": None,
                                           "host_network_unchanged": None,
                                           "business_processes_unchanged": None,
                                           "host_ra_timer_changed": None}
    try:
        for name, source in (("keeper.py", owned.read(MODULE_PATH, 65536)), ("launcher.py", LAUNCHER)):
            with open(bundle / name, "x", encoding="utf-8", newline="\n") as stream:
                stream.write(source)
            os.chmod(bundle / name, 0o444)
        os.chmod(bundle, 0o755)
        require(os.stat(bundle).st_uid == 0 and all(os.stat(bundle / name).st_uid == 0
                for name in ("keeper.py", "launcher.py")), "bundle_root_owned")
        attempted.append((parent, False))
        start_slice(parent)
        unit_cgroups[parent] = cgroup_limits(parent, LIMITS)
        keeper_args = ["/usr/bin/systemd-run", "--system", "--unit=" + keeper_unit,
                       "--collect", "--wait", "--pipe", "--quiet", *owned.properties(str(bundle), user),
                       "--property=Slice=" + parent, "/usr/bin/unshare", "--user", "--map-root-user", "--net",
                       "/usr/bin/setpriv", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
                       "--no-new-privs", "/usr/bin/python3", "-I", "-B", "/opt/keeper.py", "--keeper"]
        attempted.append((keeper_unit, True))
        captures[keeper_unit] = Capture(keeper_args)
        verified = owned.await_owned_keeper(captures[keeper_unit], keeper_unit, host, seconds=5)
        unit_cgroups[keeper_unit] = details(keeper_unit).get("ControlGroup", "")
        pid = int(verified["pid"])
        ticks = start_ticks(pid)
        # Conservative: the Python exec starts before the keeper's sleep(12).
        clock_ticks = os.sysconf("SC_CLK_TCK")
        remaining_lifetime = ticks / clock_ticks + 12 - time.clock_gettime(time.CLOCK_BOOTTIME)
        require(remaining_lifetime >= 5, "keeper_window_too_short")
        deadline = time.monotonic() + min(8, remaining_lifetime - 1)
        pidfd = os.pidfd_open(pid, 0)
        descriptors.append(pidfd)
        for kind in ("user", "net"):
            descriptor = os.open(f"/proc/{pid}/ns/{kind}", os.O_RDONLY | os.O_CLOEXEC)
            descriptors.append(descriptor)
            item = os.fstat(descriptor)
            require({"dev": item.st_dev, "ino": item.st_ino} == verified[kind + "ns"], "pinned_namespace_identity")
            if kind == "net":
                pinned_net = descriptor
        require(start_ticks(pid) == ticks and owned.verify_owned_keeper(pid, keeper_unit, host) == verified,
                "keeper_process_unchanged")

        def gate() -> float:
            import select
            poll = select.poll()
            poll.register(pidfd, select.POLLIN)
            require(not poll.poll(0), "keeper_exited")
            remaining = deadline - time.monotonic()
            require(remaining > 0, "keeper_work_deadline")
            require(start_ticks(pid) == ticks, "keeper_process_unchanged")
            return min(2, remaining)

        gate()
        check_target(read_network(pinned_net, verified["netns"], host["net"], gate()), False)
        helper_args = ["/usr/bin/systemd-run", "--system", "--unit=" + helper_unit,
                       "--collect", "--wait", "--pipe", "--quiet",
                       *helper_properties(str(bundle), user, parent),
                       "/usr/bin/python3", "-I", "-B", "/opt/launcher.py"]
        helper_args += list(map(str, (pid, ticks, verified["host_uid"], verified["host_gid"],
                            verified["userns"]["dev"], verified["userns"]["ino"],
                            verified["netns"]["dev"], verified["netns"]["ino"],
                            host["user"]["dev"], host["user"]["ino"], host["net"]["dev"], host["net"]["ino"])))
        helper_args.append(helper_unit)
        attempted.append((helper_unit, True))
        captures[helper_unit] = Capture(helper_args)
        helper_capture = captures[helper_unit]
        ready_deadline = time.monotonic() + min(3, gate())
        while True:
            gate()
            helper_capture.drain()
            ready = bytes(helper_capture.output["stdout"])
            require(ready in (b"", b"1"), "helper_ready_exact_byte")
            require_helper_running(helper_capture)
            # systemd-run itself retains its stdout while waiting for the unit;
            # slirp closes fd 3 at ready, but the outer pipe need not reach EOF.
            if ready == b"1":
                break
            require(time.monotonic() < ready_deadline, "helper_ready_deadline")
        helper_pid = int(details(helper_unit).get("MainPID", "0"))
        require(helper_pid > 0, "helper_main_pid")
        verify_helper(helper_pid, helper_unit, verified, host, parent)
        unit_cgroups[helper_unit] = details(helper_unit).get("ControlGroup", "")
        check_target(read_network(pinned_net, verified["netns"], host["net"], gate()), True)
        gate()
        stop_unit(helper_unit, True, unit_cgroups[helper_unit])
        explicit_stops.append(helper_unit)
        check_target(read_network(pinned_net, verified["netns"], host["net"], gate()), False)
        stop_unit(keeper_unit, True, unit_cgroups[keeper_unit])
        explicit_stops.append(keeper_unit)
        result = 0
    except (ProbeFailure, OSError, ValueError, KeyError, IndexError, AttributeError) as error:
        failure = {"stage": getattr(error, "stage", "probe_exception"), "errno": getattr(error, "errno", None)}
    finally:
        for unit, service in reversed(attempted):
            if unit in explicit_stops:
                continue
            try:
                stop_unit(unit, service, unit_cgroups.get(unit))
                explicit_stops.append(unit)
            except Exception:
                result = 1
                failure = {"stage": "unit_cleanup_unconfirmed"}
        for capture in captures.values():
            try:
                capture.finish(2)
                # Explicit stop may produce a nonzero client exit code; it is
                # deliberately not required to be zero. Drain errors still fail.
            except (ProbeFailure, OSError, subprocess.SubprocessError):
                result, failure = 1, {"stage": "capture_drain_failed"}
            try:
                capture.close()
            except (OSError, subprocess.SubprocessError):
                result, failure = 1, {"stage": "capture_cleanup_failed"}
        if result == 0 and bytes(captures[helper_unit].output["stdout"]) != b"1":
            result, failure = 1, {"stage": "helper_ready_exact_byte"}
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                result, failure = 1, {"stage": "descriptor_cleanup_failed"}
        try:
            for name in ("keeper.py", "launcher.py"):
                (bundle / name).unlink(missing_ok=True)
            bundle.rmdir()
        except OSError:
            result, failure = 1, {"stage": "bundle_cleanup_failed"}
        baseline_flags, baseline_failures = compare_baselines(host, before_network, before_business)
        if baseline_failures:
            result, failure = 1, {"stage": baseline_failures[0]}
    capture_diagnostics = {("keeper" if unit == keeper_unit else "helper"):
                           capture_summary(capture, keeper=unit == keeper_unit)
                           for unit, capture in captures.items()}
    owned.emit("slirp_tap_verified" if result == 0 else str(failure.get("stage", "probe_failed")), result == 0,
               errno=failure.get("errno"), keeper_unit=keeper_unit, helper_unit=helper_unit, slice=parent,
               explicit_stop=explicit_stops, **baseline_flags, captures=capture_diagnostics,
               no_guest_application=True, no_interface_configuration=True)
    if result == 0:
        print("SLIRP_TAP_VERIFIED", flush=True)
    return result


def main() -> int:
    try:
        require(not sys.argv[1:], "unexpected_arguments")
        def interrupted(number: int, _frame: object) -> None:
            raise ProbeFailure("interrupted", number)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, interrupted)
        return orchestrator()
    except (ProbeFailure, OSError, ValueError, KeyError, AttributeError) as error:
        owned.emit(getattr(error, "stage", "probe_exception"), False,
                   errno=getattr(error, "errno", None))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
