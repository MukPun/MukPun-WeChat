#!/usr/bin/env python3
"""Fail-closed, empty native-client startup check for an isolated system unit.

This controller makes only local GET requests. It never creates an account,
logs in, or sends a message. The enclosing systemd unit is a prerequisite.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any

BINARY = Path("/opt/wx")
CONFIG = Path("/opt/server.toml")
CONTROLLER = Path("/opt/empty_client_probe.py")
EXPECTED_SHA256 = "fde2920e71e0e20b926498b8c306a5064527afa5abd2b558eff9cefa0cce478e"
EXPECTED_VERSION = "1.0.0-beta.8"
WORK_MOUNT = "/var/lib"
BASE = Path(WORK_MOUNT) / "astrbot-wechat-poc"
DATA = BASE / "data"
PORT = 26174
MAX_BODY = 64 * 1024
STARTUP_SECONDS = 15.0
CGROUP_PREFIX = "astrbot-wechat-empty-probe-"


class ProbeFailure(Exception):
    """Only a fixed, non-sensitive check identifier may leave the controller."""

    def __init__(self, check: str):
        self.check = check
        super().__init__(check)


def emit(check: str, ok: bool, **safe_fields: Any) -> None:
    print(json.dumps({"check": check, "ok": ok, **safe_fields}, sort_keys=True), flush=True)


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def parse_mounts(text: str) -> list[dict[str, Any]]:
    result = []
    for line in text.splitlines():
        fields = line.split()
        split = fields.index("-")
        result.append({
            "root": _unescape_mount(fields[3]),
            "point": _unescape_mount(fields[4]),
            "options": set(fields[5].split(",")),
            "fstype": fields[split + 1],
            "source": _unescape_mount(fields[split + 2]),
        })
    return result


def containing_mount(mounts: list[dict[str, Any]], path: str) -> dict[str, Any]:
    matches = [item for item in mounts if item["point"] == "/" or
               path == item["point"] or path.startswith(item["point"].rstrip("/") + "/")]
    if not matches:
        raise ProbeFailure("mount_inventory")
    return max(matches, key=lambda item: len(item["point"]))


def _readonly(mounts: list[dict[str, Any]], path: str) -> bool:
    return "ro" in containing_mount(mounts, path)["options"] and bool(
        os.statvfs(path).f_flag & os.ST_RDONLY)


def _masked(mounts: list[dict[str, Any]], path: str) -> bool:
    mount = containing_mount(mounts, path)
    # InaccessiblePaths is an actual bind of systemd's mode-000 inaccessible node.
    # A missing path on an otherwise visible /run is not sufficient evidence.
    if "inaccessible" not in mount["root"] or "ro" not in mount["options"]:
        return False
    try:
        return stat.S_IMODE(os.stat(mount["point"]).st_mode) == 0
    except PermissionError:
        return True


def _blocked_temporary_path(mounts: list[dict[str, Any]], path: str) -> bool:
    """Require a real mask and denied directory enumeration and file creation."""
    if not _masked(mounts, path):
        return False
    enumeration_denied = False
    try:
        with os.scandir(path):
            pass
    except PermissionError:
        enumeration_denied = True
    creation_denied = False
    try:
        descriptor, filename = tempfile.mkstemp(prefix="empty-probe-denied-", dir=path)
    except OSError as error:
        import errno
        creation_denied = error.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
    else:
        # If the expected mask was ineffective, remove only our own new file.
        try:
            os.close(descriptor)
        finally:
            os.unlink(filename)
    return enumeration_denied and creation_denied


def _binary_executable_mount() -> bool:
    return os.access(BINARY, os.X_OK) and not bool(os.statvfs(BINARY).f_flag & os.ST_NOEXEC)


def validate_isolation() -> list[tuple[str, bool]]:
    """Recheck effective kernel state immediately before any child starts."""
    checks: list[tuple[str, bool]] = []

    def check(name: str, work: Any) -> None:
        try:
            checks.append((name, bool(work())))
        except Exception:
            checks.append((name, False))

    check("nonroot_identity", lambda: min(os.getuid(), os.geteuid(), os.getgid(), os.getegid()) > 0)
    check("supplementary_groups", lambda: set(os.getgroups()) <= {os.getgid()} and os.getgid() == os.getegid())
    try:
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    except Exception:
        status = {}
    check("capabilities_zero", lambda: all(int(status[key].strip(), 16) == 0 for key in
          ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")))
    check("no_new_privileges", lambda: status["NoNewPrivs"].strip() == "1")
    try:
        mounts = parse_mounts(Path("/proc/self/mountinfo").read_text())
    except Exception:
        mounts = []
    for name, path in (("root_readonly", "/"), ("usr_readonly", "/usr"), ("etc_readonly", "/etc"),
                       ("binary_readonly", str(BINARY)), ("config_readonly", str(CONFIG)),
                       ("controller_readonly", str(CONTROLLER))):
        check(name, lambda path=path: _readonly(mounts, path))
    check("docker_socket_masked", lambda: _masked(mounts, "/run/docker.sock"))
    check("containerd_masked", lambda: _masked(mounts, "/run/containerd"))
    check("loopback_only", lambda: set(os.listdir("/sys/class/net")) == {"lo"} and
          {line.split(":", 1)[0].strip() for line in Path("/proc/net/dev").read_text().splitlines() if ":" in line} == {"lo"})

    def unix_forbidden() -> bool:
        try:
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as error:
            import errno
            return error.errno in (errno.EAFNOSUPPORT, errno.EPERM, errno.EACCES)
        channel.close()
        return False

    check("unix_sockets_forbidden", unix_forbidden)
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
        paths = [line[3:] for line in lines if line.startswith("0::")]
        if len(paths) != 1 or ".." in Path(paths[0]).parts:
            raise ValueError("cgroup")
        relative = paths[0].lstrip("/")
        group = Path("/sys/fs/cgroup") / relative
        checks.append(("dedicated_cgroup", any(part.startswith(CGROUP_PREFIX) for part in Path(relative).parts)))
        def limit(filename: str) -> int:
            return int((group / filename).read_text().strip())
        check("cpu_limit", lambda: (group / "cpu.max").read_text().split() == ["20000", "100000"])
        check("memory_max", lambda: 0 < limit("memory.max") <= 128 * 1024 * 1024)
        check("memory_high", lambda: 0 < limit("memory.high") <= 96 * 1024 * 1024)
        check("memory_swap_zero", lambda: limit("memory.swap.max") == 0)
        check("tasks_limit", lambda: 0 < limit("pids.max") <= 32)
    except Exception:
        checks.append(("cgroup_inventory", False))

    def work_limit() -> bool:
        mount = containing_mount(mounts, WORK_MOUNT)
        fs = os.statvfs(WORK_MOUNT)
        return (mount["point"] == WORK_MOUNT and mount["fstype"] == "tmpfs" and
                {"nodev", "nosuid", "noexec"} <= mount["options"] and
                "rw" in mount["options"] and not fs.f_flag & os.ST_RDONLY and
                fs.f_blocks * fs.f_frsize == 32 * 1024 * 1024 and
                0 < fs.f_files <= 1024 and stat.S_IMODE(os.stat(WORK_MOUNT).st_mode) == 0o1777)

    check("work_tmpfs_32m_bounded", work_limit)
    for name, path in (("tmp_masked_blocked", "/tmp"), ("var_tmp_masked_blocked", "/var/tmp")):
        check(name, lambda path=path: _blocked_temporary_path(mounts, path))
    for name, path in (("dev_shm_readonly_bounded", "/dev/shm"),):
        def small_tmpfs(path: str = path) -> bool:
            mount = containing_mount(mounts, path)
            fs = os.statvfs(path)
            return (mount["point"] == path and mount["fstype"] == "tmpfs" and
                    _readonly(mounts, path) and 0 < fs.f_blocks * fs.f_frsize <= 4096)
        check(name, small_tmpfs)

    def work_write() -> bool:
        descriptor, name = tempfile.mkstemp(prefix="empty-probe-", dir=WORK_MOUNT)
        try:
            os.write(descriptor, b"probe")
        finally:
            os.close(descriptor)
            os.unlink(name)
        return True

    check("work_write_delete", work_write)
    def binary_hash() -> bool:
        with BINARY.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return digest == EXPECTED_SHA256 and BINARY.is_file()
    check("binary_sha256", binary_hash)
    check("binary_executable_mount", _binary_executable_mount)
    check("minimal_config", lambda: CONFIG.stat().st_size <= 4096 and
          tomllib.loads(CONFIG.read_text()) == {"schemaVersion": 1})
    return checks


def prepare_directories() -> dict[str, str]:
    BASE.mkdir(mode=0o700)
    paths = {"HOME": BASE / "home", "XDG_CONFIG_HOME": BASE / "config",
             "XDG_RUNTIME_DIR": BASE / "run", "TMPDIR": BASE / "tmp"}
    DATA.mkdir(mode=0o700)
    for path in paths.values():
        path.mkdir(mode=0o700)
    return {**{key: str(value) for key, value in paths.items()},
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TOKIO_WORKER_THREADS": "1"}


def emit_temp_mount_diagnostics() -> bool:
    """Report only public mount metadata for fixed scratch and binary paths."""
    try:
        mounts = parse_mounts(Path("/proc/self/mountinfo").read_text())
    except Exception:
        emit("temp_mount_inventory", False)
        return False
    complete = True
    for path in (WORK_MOUNT, "/tmp", "/var/tmp", "/dev/shm", "/opt"):
        try:
            mount = containing_mount(mounts, path)
            fs = os.statvfs(path)
            emit("temp_mount_diagnostic", True, path=path,
                 mountpoint=mount["point"], fstype=mount["fstype"],
                 options=sorted(mount["options"]), root=mount["root"],
                 bytes=fs.f_blocks * fs.f_frsize, inodes=fs.f_files,
                 mode=oct(stat.S_IMODE(os.stat(path).st_mode)))
        except Exception:
            emit("temp_mount_diagnostic", False, path=path)
            complete = False
    return complete


def get_json(path: str, token: str | None = None) -> tuple[int, dict[str, Any]]:
    if path not in ("/health", "/v1/accounts", "/v1/status"):
        raise ProbeFailure("request_path")
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=0.8)
    try:
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        size = response.getheader("Content-Length")
        if size is not None and (not size.isdecimal() or int(size) > MAX_BODY):
            raise ProbeFailure("response_body_bound")
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ProbeFailure("response_body_bound")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise ProbeFailure("response_json") from None
        if not isinstance(payload, dict):
            raise ProbeFailure("response_shape")
        return response.status, payload
    finally:
        connection.close()


def wait_for_health(process: Any) -> None:
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ProbeFailure("native_process_alive")
        try:
            status, payload = get_json("/health")
            if status == 200 and payload == {"status": "ok"}:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.1)
    raise ProbeFailure("health_deadline")


def read_token() -> str:
    path = DATA / "api-token"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                stat.S_IMODE(info.st_mode) == 0o600 and 32 <= info.st_size <= 1024):
            raise ProbeFailure("private_token_file")
        raw = os.read(descriptor, 1025)
        if len(raw) > 1024:
            raise ProbeFailure("private_token_file")
        token = raw.decode("ascii").strip()
        if len(token) < 32 or not all(33 <= ord(char) <= 126 for char in token):
            raise ProbeFailure("private_token_file")
        return token
    finally:
        os.close(descriptor)


def stop_child(process: Any) -> bool:
    """Signal only the Popen handle owned by this controller, never a group."""
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in ([], ["--isolation-only"]):
        emit("arguments", False)
        return 2
    isolation_only = arguments == ["--isolation-only"]
    try:
        checks = validate_isolation()
    except Exception:
        emit("isolation_inventory", False)
        if isolation_only:
            emit_temp_mount_diagnostics()
        return 1
    for name, passed in checks:
        emit(name, passed)
    all_passed = bool(checks) and all(passed for _, passed in checks)
    if isolation_only:
        diagnosed = emit_temp_mount_diagnostics()
        emit("isolation_only_complete", all_passed and diagnosed)
        # This mode has no path into directory preparation, Popen or HTTP.
        return 0 if all_passed and diagnosed else 1
    if not all_passed:
        return 1
    process = None
    success = False
    try:
        try:
            child_environment = prepare_directories()
        except OSError as error:
            emit("private_directories", False, errno=error.errno if isinstance(error.errno, int) else 0)
            return 1
        emit("private_directories_ready", True)
        try:
            process = subprocess.Popen(
                [str(BINARY), "--server-config", str(CONFIG), "--data-dir", str(DATA),
                 "--listen", "127.0.0.1:26174", "serve"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, env=child_environment, cwd=str(BASE),
            )
        except OSError as error:
            emit("native_spawn", False, errno=error.errno if isinstance(error.errno, int) else 0)
            return 1
        wait_for_health(process)
        emit("health_ready", True)
        token = read_token()
        emit("private_token_file", True)
        code, _ = get_json("/v1/accounts")
        if code not in (401, 403):
            raise ProbeFailure("anonymous_accounts_denied")
        emit("anonymous_accounts_denied", True)
        code, accounts = get_json("/v1/accounts", token)
        if code != 200 or accounts.get("items") != []:
            raise ProbeFailure("empty_accounts")
        emit("empty_accounts", True)
        code, payload = get_json("/v1/status", token)
        version = payload.get("version")
        if (code != 200 or payload.get("state") != "ready" or not isinstance(version, dict)
                or version.get("backend") != "native" or version.get("version") != EXPECTED_VERSION):
            raise ProbeFailure("native_ready_version")
        emit("native_ready_version", True, version=EXPECTED_VERSION, backend="native")
        success = True
    except ProbeFailure as error:
        emit(error.check, False)
    except Exception:
        # Never serialize arbitrary exceptions: they may contain HTTP headers/paths.
        emit("probe_operation", False)
    finally:
        if process is not None:
            stopped = stop_child(process)
            emit("child_stopped", stopped)
            success = success and stopped
    if success:
        print("EMPTY_CLIENT_STARTUP_VERIFIED", flush=True)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
