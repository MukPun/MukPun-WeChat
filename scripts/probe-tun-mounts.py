#!/usr/bin/env python3
"""Trusted differential mount diagnostics. Explicit root execution is separate.

Four sequential transient units vary only /run masking and the TUN bind/device
allow. The constant child never opens TUN, issues an ioctl or creates a socket.
An exit status such as 226/NAMESPACE is diagnostic data, never a passed case.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time

ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
CAPTURE_LIMIT = 65536
BUSINESS = {"dockerd", "containerd", "docker-proxy", "frpc", "frps"}
CHILD = r'''
import json, os, stat
with open('/proc/self/status', 'r', encoding='ascii') as stream:
    status = dict(line.split(':', 1) for line in stream if ':' in line)
uid = list(map(int, status['Uid'].split()))
gid = list(map(int, status['Gid'].split()))
checks = {
    'nonroot_ids': len(uid) == len(gid) == 4 and min(uid + gid) > 0 and len(set(uid)) == len(set(gid)) == 1,
    'own_groups_only': set(map(int, status['Groups'].split())) <= {gid[0]},
    'zero_caps': all(int(status[key].strip(), 16) == 0 for key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')),
    'no_new_privs': status['NoNewPrivs'].strip() == '1',
}
try:
    device = os.stat('/dev/net/tun')
    tun = {'present': True, 'character': stat.S_ISCHR(device.st_mode),
           'major': os.major(device.st_rdev), 'minor': os.minor(device.st_rdev)}
except OSError as error:
    tun = {'present': False, 'errno': error.errno}
print(json.dumps({'checks': checks, 'tun': tun}, sort_keys=True), flush=True)
raise SystemExit(0 if all(checks.values()) else 2)
'''


class ProbeFailure(Exception):
    pass


def require(value: bool, reason: str) -> None:
    if not value:
        raise ProbeFailure(reason)


def bounded(arguments: list[str], seconds: float = 3, limit: int = CAPTURE_LIMIT) -> tuple[int, bytes]:
    """Capture bounded stdout; signal only the Popen child created here."""
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, close_fds=True, env=ENV)
    selector = selectors.DefaultSelector()
    output = bytearray()
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        end = time.monotonic() + seconds
        while process.poll() is None or selector.get_map():
            require(time.monotonic() < end, "command_deadline")
            for key, _ in selector.select(0.02):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                else:
                    require(len(output) + len(chunk) <= limit, "command_output_bound")
                    output.extend(chunk)
        return process.wait(), bytes(output)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        process.stdout.close()
        selector.close()


def unit_state(unit: str) -> dict[str, str]:
    code, output = bounded([
        "/usr/bin/systemctl", "show", unit, "--no-pager",
        "--property=LoadState,ActiveState,MainPID,Result,ExecMainCode,ExecMainStatus,ControlGroup",
    ])
    result = dict(line.split("=", 1) for line in output.decode("ascii").splitlines() if "=" in line)
    require(code == 0 or result.get("LoadState") == "not-found", "unit_query_failed")
    return result


def stop_owned(unit: str, group: str) -> None:
    if not group:
        group = unit_state(unit).get("ControlGroup", "")
    code, _ = bounded(["/usr/bin/systemctl", "stop", unit], 4)
    state = unit_state(unit)
    if state.get("ActiveState") == "failed" and state.get("MainPID") == "0":
        # Failed transient units retain their 226 diagnostic until reset.
        reset, _ = bounded(["/usr/bin/systemctl", "reset-failed", unit])
        require(reset == 0, "unit_reset_unconfirmed")
        state = unit_state(unit)
    missing = state.get("LoadState") == "not-found"
    require(missing or (code == 0 and state.get("ActiveState") == "inactive"
                       and state.get("MainPID") == "0"), "unit_stop_unconfirmed")
    if group:
        require(group.startswith("/") and ".." not in Path(group).parts
                and Path(group).name == unit, "cleanup_cgroup_path")
        processes = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
        try:
            require(not processes.read_text(encoding="ascii").strip(), "unit_cgroup_not_empty")
        except FileNotFoundError:
            pass


def snapshot() -> tuple[tuple[int, int], list[object], list[tuple[int, str, int]]]:
    namespace = os.stat("/proc/self/ns/net")
    network = []
    for arguments in (["-j", "link", "show"], ["-j", "-4", "route", "show", "table", "all"],
                      ["-j", "-6", "route", "show", "table", "all"]):
        code, output = bounded(["/usr/sbin/ip", *arguments])
        require(code == 0, "network_read_failed")
        value = json.loads(output)
        require(isinstance(value, list), "network_json_invalid")
        if arguments[1] == "-6":
            for route in value:
                if route.get("protocol") == "ra" and type(route.get("expires")) is int and route["expires"] > 0:
                    route["expires"] = "positive_ra_lifetime"
        network.append(value)
    business = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            name = (entry / "comm").read_text(encoding="ascii").strip()
            if name in BUSINESS or name.startswith("containerd-shim"):
                ticks = int((entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[19])
                business.append((int(entry.name), name, ticks))
        except FileNotFoundError:
            pass
    return (namespace.st_dev, namespace.st_ino), network, sorted(business)


def properties(whole_run: bool, tun_bind: bool) -> list[str]:
    run_mask = "-/run" if whole_run else "-/run/docker.sock -/run/containerd -/run/user"
    values = [
        "Type=exec", "DynamicUser=yes", "UMask=0077", "PrivateUsers=no",
        "PrivateNetwork=yes", "PrivateDevices=yes", "PrivateTmp=no",
        "ProtectHome=tmpfs", "ProtectSystem=strict", "ProtectProc=invisible",
        "NoNewPrivileges=yes", "CapabilityBoundingSet=", "AmbientCapabilities=",
        "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
        "RestrictNamespaces=yes", "RestrictSUIDSGID=yes", "RestrictRealtime=yes",
        "LockPersonality=yes", "SystemCallArchitectures=native",
        "RestrictAddressFamilies=AF_INET AF_INET6", "CPUQuota=20%", "CPUQuotaPeriodSec=100ms",
        "MemoryHigh=24M", "MemoryMax=32M", "MemorySwapMax=0", "TasksMax=8",
        "LimitNOFILE=32", "LimitCORE=0", "LimitFSIZE=0", "RuntimeMaxSec=6",
        "TimeoutStartSec=4", "TimeoutStopSec=2", "SendSIGKILL=yes", "Restart=no",
        "KillMode=control-group", "OOMPolicy=stop", "Nice=19", "IOSchedulingClass=idle",
        "DevicePolicy=closed", "InaccessiblePaths=" + run_mask + " -/tmp -/var/tmp",
        "TemporaryFileSystem=/dev/shm:ro,size=4K,nr_inodes=16,mode=000,nosuid,nodev,noexec",
    ]
    if tun_bind:
        values += ["BindPaths=/dev/net/tun", "DeviceAllow=/dev/net/tun rw"]
    return ["--property=" + item for item in values]


def run_case(unit: str, whole_run: bool, tun_bind: bool) -> dict[str, object]:
    # list-units does not synthesize/load an unknown unit the way show can.
    code, existing = bounded(["/usr/bin/systemctl", "list-units", "--all", "--plain",
                              "--no-legend", "--no-pager", "--full", unit])
    require(code == 0 and not existing.strip(), "unit_name_already_exists")
    group = ""
    try:
        code, output = bounded([
            "/usr/bin/systemd-run", "--system", "--unit=" + unit, "--wait", "--pipe", "--quiet",
            *properties(whole_run, tun_bind), "/usr/bin/python3", "-I", "-B", "-c", CHILD,
        ], 12, 4096)
        state = unit_state(unit)
        group = state.get("ControlGroup", "")
        child = None
        if output.strip():
            child = json.loads(output)
            require(isinstance(child, dict) and set(child) == {"checks", "tun"}
                    and isinstance(child["checks"], dict) and isinstance(child["tun"], dict),
                    "child_json_invalid")
        checks_ok = bool(child) and set(child["checks"]) == {
            "nonroot_ids", "own_groups_only", "zero_caps", "no_new_privs"
        } and all(value is True for value in child["checks"].values())
        tun_ok = bool(child) and (child["tun"] == {
            "present": True, "character": True, "major": 10, "minor": 200
        } if tun_bind else child["tun"] == {"present": False, "errno": 2})
        return {
            "case": ("whole_run" if whole_run else "critical_run") + ("_tun" if tun_bind else "_no_tun"),
            "unit": unit, "client_returncode": code,
            "exec_main_status": state.get("ExecMainStatus"), "unit_result": state.get("Result"),
            "child_ran": child is not None, "child": child,
            "case_passed": code == 0 and checks_ok and tun_ok,
        }
    finally:
        # Never use process names, host namespaces or a broad unit pattern.
        stop_owned(unit, group)


def main() -> int:
    try:
        require(sys.platform == "linux" and os.geteuid() == 0 and len(sys.argv) == 1,
                "linux_root_no_arguments_required")
        def interrupted(_number: int, _frame: object) -> None:
            raise ProbeFailure("interrupted")
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, interrupted)
        require(Path("/sys/class/misc/tun").is_dir()
                and Path("/sys/class/misc/tun/dev").read_text(encoding="ascii").strip() == "10:200",
                "tun_driver_not_already_registered")
        device = os.stat("/dev/net/tun")
        require(stat.S_ISCHR(device.st_mode) and (os.major(device.st_rdev), os.minor(device.st_rdev)) == (10, 200),
                "tun_device_mismatch")
        current = os.stat("/proc/self/ns/net")
        initial = os.stat("/proc/1/ns/net")
        require((current.st_dev, current.st_ino) == (initial.st_dev, initial.st_ino),
                "initial_host_network_required")
        before = snapshot()
        completed = 0
        try:
            prefix = "astrbot-wechat-tun-mounts-" + secrets.token_hex(6)
            for index, (whole_run, tun_bind) in enumerate(((False, False), (False, True), (True, False), (True, True))):
                record = run_case(prefix + "-" + str(index) + ".service", whole_run, tun_bind)
                completed += 1
                print(json.dumps(record, sort_keys=True), flush=True)
        finally:
            require(snapshot() == before, "host_baseline_changed")
        require(completed == 4, "diagnostic_incomplete")
        print("TUN_MOUNT_DIAGNOSTIC_COMPLETE", flush=True)
        return 0
    except (ProbeFailure, OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
        print(json.dumps({"diagnostic_complete": False,
                          "reason": str(error) if isinstance(error, ProbeFailure) else "probe_failed",
                          "errno": getattr(error, "errno", None)}, sort_keys=True), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
