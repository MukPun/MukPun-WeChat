#!/usr/bin/env python3
"""Read-only by default. Loading changes global kernel state and needs approval.

No package installation, boot configuration, firewall commands or module unload.
Only the three pinned module names below can reach the loader's fixed argv.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time

KERNEL = "6.8.0-139-generic"
BASE = Path("/lib/modules") / KERNEL / "kernel"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
MODPROBE = "/usr/sbin/modprobe"
MODINFO = "/usr/sbin/modinfo"
UNIT_PREFIX = "astrbot-wechat-module-"
DEPENDENCY_FILES = {
    "nf_tables": BASE / "net/netfilter/nf_tables.ko.zst",
    "nfnetlink": BASE / "net/netfilter/nfnetlink.ko.zst",
    "nf_conntrack": BASE / "net/netfilter/nf_conntrack.ko.zst",
    "nf_defrag_ipv4": BASE / "net/ipv4/netfilter/nf_defrag_ipv4.ko.zst",
    "nf_defrag_ipv6": BASE / "net/ipv6/netfilter/nf_defrag_ipv6.ko.zst",
    "libcrc32c": BASE / "lib/libcrc32c.ko.zst",
}
LIBCRC32C_SIZE = 2531
LIBCRC32C_SHA256 = "2fa4edc78171d4bb93cf9d0ee70f1d51a25b599c18f6e41824339bf2b23e4a25"
BUILTIN_FILE = BASE.parent / "modules.builtin"
BUILTIN_SIZE = 9714
BUILTIN_SHA256 = "508ae45959f569e45b1e6f1888b254cdc02da5c4bb8b3edc043539b6dd2d735d"
BUILTINS = {
    "crc32c_intel": ("CONFIG_CRYPTO_CRC32C_INTEL", "kernel/arch/x86/crypto/crc32c-intel.ko"),
    "crc32c_generic": ("CONFIG_CRYPTO_CRC32C", "kernel/crypto/crc32c_generic.ko"),
}


@dataclass(frozen=True)
class Module:
    name: str
    size: int
    sha256: str
    depends: frozenset[str]

    @property
    def path(self) -> Path:
        return BASE / "net/netfilter" / (self.name + ".ko.zst")


MODULES = (
    Module("nft_ct", 16137, "691ce7ddf140203cc4c5593d56ab780f2d354f0a909baa0d81d7f0907e72de7f",
           frozenset({"nf_conntrack", "nf_tables"})),
    Module("nft_limit", 8030, "aaa3943386a0a4ec3f0b353fcfde676b7abd7b7af6cdee852b6ed6e2e3143153",
           frozenset({"nf_tables"})),
    Module("nft_quota", 6733, "de3c9b9e84c5b75e5b8b211881c28f081e5abd0eb9d646c5e13ae236eec0bf33",
           frozenset({"nf_tables"})),
)


class Failure(Exception):
    """Only fixed, non-sensitive reason labels may leave this program."""


@dataclass
class LoadProgress:
    attempted: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    cleanup_unconfirmed: bool = False


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise Failure(reason)


def emit(stage: str, **fields: object) -> None:
    print(json.dumps({"stage": stage, **fields}, sort_keys=True), flush=True)


def capture(arguments: list[str], seconds: float = 3, limit: int = 65536) -> tuple[int, str]:
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
                data = os.read(key.fileobj.fileno(), 4096)
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                else:
                    require(len(output) + len(data) <= limit, "command_output_bound")
                    output.extend(data)
        return process.wait(), output.decode("utf-8", "strict")
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


def read_regular(path: Path, maximum: int) -> bytes:
    """Final component must be a root-owned, non-symlink regular file."""
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid == before.st_gid == 0
            and not before.st_mode & 0o022 and before.st_size <= maximum, "file_metadata_mismatch")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        require((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                 opened.st_mode, opened.st_uid, opened.st_gid) ==
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                 before.st_mode, before.st_uid, before.st_gid), "file_changed")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        require(len(value) == opened.st_size and
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns), "file_changed")
        return value
    finally:
        os.close(descriptor)


def live(name: str) -> bool:
    try:
        return (Path("/sys/module") / name / "initstate").read_text(encoding="ascii").strip() == "live"
    except FileNotFoundError:
        return False


def modinfo(field: str, path: Path) -> str:
    code, output = capture([MODINFO, "--field", field, str(path)], limit=4096)
    require(code == 0, "modinfo_failed")
    return output.strip()


def check_builtins(config: str) -> frozenset[str]:
    data = read_regular(BUILTIN_FILE, BUILTIN_SIZE)
    require(len(data) == BUILTIN_SIZE and hashlib.sha256(data).hexdigest() == BUILTIN_SHA256,
            "builtin_file_hash_size_mismatch")
    entries = data.decode("ascii").splitlines()
    for option, entry in BUILTINS.values():
        require(re.search(r"^" + option + r"=y$", config, re.MULTILINE) is not None
                and entries.count(entry) == 1, "builtin_evidence_mismatch")
    return frozenset(BUILTINS)


def check_libcrc32c(reference: str) -> None:
    path = DEPENDENCY_FILES["libcrc32c"]
    data = read_regular(path, LIBCRC32C_SIZE)
    require(len(data) == LIBCRC32C_SIZE and hashlib.sha256(data).hexdigest() == LIBCRC32C_SHA256,
            "libcrc32c_hash_size_mismatch")
    require(live("libcrc32c") and modinfo("name", path) == "libcrc32c"
            and modinfo("depends", path) == "" and modinfo("vermagic", path) == reference,
            "libcrc32c_metadata_mismatch")


def check_plan(module: Module, already_live: bool,
               verified_builtins: frozenset[str] = frozenset()) -> None:
    # --show-depends never inserts modules or executes install commands.
    code, output = capture([MODPROBE, "--show-depends", "--use-blacklist",
                            "--ignore-install", module.name])
    require(code == 0, "load_plan_failed")
    allowed = {path.resolve(): name for name, path in DEPENDENCY_FILES.items()}
    allowed[module.path.resolve()] = module.name
    targets = 0
    seen: set[tuple[str, str]] = set()
    for line in output.splitlines():
        parts = shlex.split(line)
        require(len(parts) == 2 and parts[0] in {"insmod", "builtin"}, "load_plan_not_allowed")
        entry = (parts[0], parts[1])
        require(entry not in seen, "load_plan_duplicate")
        seen.add(entry)
        if parts[0] == "builtin":
            require(parts[1] in BUILTINS and parts[1] in verified_builtins, "load_plan_builtin_not_verified")
            continue
        name = allowed.get(Path(parts[1]).resolve())
        require(name is not None, "load_plan_extra_module")
        if name == module.name:
            targets += 1
        else:
            require(live(name), "load_plan_dependency_not_live")
    # -b may silently omit a blacklisted explicit module. Never bypass it.
    require(targets == 1 or (already_live and targets == 0), "load_plan_target_missing")


def check_all() -> list[dict[str, object]]:
    require(sys.platform == "linux" and os.uname().release == KERNEL
            and os.uname().machine == "x86_64", "kernel_mismatch")
    for tool in (MODPROBE, MODINFO):
        resolved = Path(tool).resolve(strict=True)  # Installed kmod symlinks are normal.
        item = resolved.stat()
        require(stat.S_ISREG(item.st_mode) and item.st_uid == item.st_gid == 0
                and not item.st_mode & 0o022 and os.access(resolved, os.X_OK), "tool_metadata_mismatch")
    config = read_regular(Path("/boot") / ("config-" + KERNEL), 2 * 1024 * 1024).decode("ascii")
    for module in MODULES:
        require(re.search(r"^CONFIG_" + module.name.upper() + r"=m$", config, re.MULTILINE) is not None,
                "kernel_config_mismatch")
    require(re.search(r"^CONFIG_LIBCRC32C=m$", config, re.MULTILINE) is not None, "kernel_config_mismatch")
    verified_builtins = check_builtins(config)
    require(Path("/proc/sys/kernel/modules_disabled").read_text(encoding="ascii").strip() == "0",
            "module_loading_disabled")
    for name, path in DEPENDENCY_FILES.items():
        require(live(name), "dependency_not_live")
        read_regular(path, 2 * 1024 * 1024)
    reference = modinfo("vermagic", DEPENDENCY_FILES["nf_tables"])
    require(reference.split() and reference.split()[0] == KERNEL
            and re.fullmatch(r"[A-Za-z0-9_. -]{1,512}", reference) is not None, "vermagic_mismatch")
    check_libcrc32c(reference)
    records = []
    for module in MODULES:
        contents = read_regular(module.path, module.size)
        require(len(contents) == module.size and hashlib.sha256(contents).hexdigest() == module.sha256,
                "module_hash_size_mismatch")
        require(modinfo("name", module.path) == module.name, "module_name_mismatch")
        require(modinfo("vermagic", module.path) == reference, "vermagic_mismatch")
        dependencies = frozenset(filter(None, modinfo("depends", module.path).split(",")))
        require(dependencies == module.depends and all(live(name) for name in dependencies),
                "dependency_mismatch")
        loaded = live(module.name)
        check_plan(module, loaded, verified_builtins)
        records.append({"name": module.name, "path": str(module.path), "size": module.size,
                        "sha256": module.sha256, "vermagic": reference, "live": loaded})
    return records


def properties() -> list[str]:
    values = [
        "Type=exec", "User=root", "Group=root", "PrivateUsers=no", "UMask=0077",
        "CapabilityBoundingSet=CAP_SYS_MODULE", "AmbientCapabilities=", "NoNewPrivileges=yes",
        "UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GLIBC_TUNABLES PYTHONPATH PYTHONHOME MODPROBE_OPTIONS",
        "PrivateNetwork=yes", "PrivateDevices=yes", "PrivateTmp=no", "ProtectSystem=strict",
        "ProtectHome=tmpfs", "ProtectProc=invisible", "ProtectKernelTunables=yes",
        "ProtectControlGroups=yes", "RestrictNamespaces=yes", "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes", "LockPersonality=yes", "SystemCallArchitectures=native",
        "RestrictAddressFamilies=AF_UNIX", "CPUQuota=20%", "CPUQuotaPeriodSec=100ms",
        "MemoryHigh=24M", "MemoryMax=32M", "MemorySwapMax=0", "TasksMax=8",
        "LimitNOFILE=32", "LimitCORE=0", "LimitFSIZE=0", "RuntimeMaxSec=10",
        "TimeoutStartSec=4", "TimeoutStopSec=2", "SendSIGKILL=yes", "Restart=no",
        "KillMode=control-group", "OOMPolicy=stop", "Nice=19", "IOSchedulingClass=idle",
        "DevicePolicy=closed", "InaccessiblePaths=-/run/docker.sock -/run/containerd -/run/user -/tmp -/var/tmp",
        "TemporaryFileSystem=/dev/shm:ro,size=4K,nr_inodes=16,mode=000,nosuid,nodev,noexec",
    ]
    # ProtectKernelModules=yes would intentionally prevent this approved operation.
    return ["--property=" + item for item in values]


def unit_state(unit: str) -> dict[str, str]:
    code, output = capture(["/usr/bin/systemctl", "show", unit, "--no-pager",
                            "--property=LoadState,ActiveState,MainPID,Result,ExecMainStatus,ControlGroup"])
    state = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    require(code == 0 or state.get("LoadState") == "not-found", "unit_query_failed")
    return state


def stop_owned(unit: str) -> None:
    before = unit_state(unit)
    group = before.get("ControlGroup", "")
    code, _ = capture(["/usr/bin/systemctl", "stop", unit], 4)
    state = unit_state(unit)
    if state.get("ActiveState") == "failed" and state.get("MainPID") == "0":
        reset, _ = capture(["/usr/bin/systemctl", "reset-failed", unit])
        require(reset == 0, "unit_reset_unconfirmed")
        state = unit_state(unit)
    require(state.get("LoadState") == "not-found" or
            (code == 0 and state.get("ActiveState") == "inactive" and state.get("MainPID") == "0"),
            "unit_cleanup_unconfirmed")
    if group:
        require(group.startswith("/") and ".." not in Path(group).parts
                and Path(group).name == unit, "cleanup_cgroup_path")
        try:
            require(not (Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs").read_text().strip(),
                    "unit_cgroup_not_empty")
        except FileNotFoundError:
            pass


def load_one(module: Module, progress: LoadProgress | None = None) -> None:
    require(module in MODULES and os.geteuid() == 0, "approved_root_mode_required")
    progress = LoadProgress() if progress is None else progress
    unit = UNIT_PREFIX + secrets.token_hex(6) + "-" + module.name.replace("_", "-") + ".service"
    for command in ("list-units", "list-unit-files"):
        options = ["--all", "--plain", "--full"] if command == "list-units" else ["--full"]
        code, output = capture(["/usr/bin/systemctl", command, *options, "--no-legend", "--no-pager", unit])
        require(code == 0 and not output.strip(), "unit_name_already_exists")
    original_failure = None
    try:
        progress.attempted.append(module.name)
        # env clears MODPROBE_OPTIONS and all inherited SSH/proxy/loader variables.
        code, _ = capture([
            "/usr/bin/systemd-run", "--system", "--unit=" + unit, "--wait", "--pipe", "--quiet",
            *properties(), "/usr/bin/env", "-i", "PATH=" + ENV["PATH"], "LANG=C", "LC_ALL=C",
            MODPROBE, "--use-blacklist", "--ignore-install", module.name,
        ], 16)
        require(code == 0 and live(module.name), "module_load_unconfirmed")
        # Kernel mutation precedes unit cleanup. Preserve this evidence even if
        # stop_owned later fails and the overall command reports failure.
        progress.loaded.append(module.name)
    except BaseException as error:
        original_failure = error
        raise
    finally:
        # Only our exact random transient unit is stopped; modules remain loaded.
        try:
            stop_owned(unit)
        except Exception:
            progress.cleanup_unconfirmed = True
            if original_failure is None:
                raise


def safe_live_snapshot() -> dict[str, str]:
    states = {}
    for module in MODULES:
        try:
            states[module.name] = "live" if live(module.name) else "not_live"
        except Exception:
            states[module.name] = "unavailable"
    return states


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if args not in ([], ["--check"], ["--load-if-approved"]):
        emit("failed", reason="invalid_arguments", ok=False)
        return 2
    interrupted = False
    def on_signal(_number: int, _frame: object) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True  # Repeated signals must not interrupt finally cleanup.
            raise Failure("interrupted")
    previous = {}
    progress = LoadProgress()
    initial_live = {module.name: "unavailable" for module in MODULES}
    try:
        for number in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
            if number is not None:
                previous[number] = signal.signal(number, on_signal)
        initial_live = safe_live_snapshot()
        records = check_all()
        if args != ["--load-if-approved"]:
            emit("check_complete", ok=True, kernel=KERNEL, modules=records, changed=False,
                 required_live_dependencies=sorted(DEPENDENCY_FILES), verified_builtins=sorted(BUILTINS))
            return 0
        require(os.geteuid() == 0, "approved_root_mode_required")
        for module in MODULES:
            # Recheck files, config, blacklist-aware plan and all live dependencies.
            current = check_all()
            if next(record["live"] for record in current if record["name"] == module.name):
                emit("already_live", module=module.name, changed_by_this_step=False)
                continue
            load_one(module, progress)
            require(live(module.name), "module_load_unconfirmed")
            emit("module_live", module=module.name)
        require(all(live(module.name) for module in MODULES), "modules_not_all_live")
        emit("approved_load_complete", ok=True, modules=[module.name for module in MODULES],
             loaded_by_this_run=progress.loaded, global_kernel_state_changed=bool(progress.loaded), automatic_unload=False)
        return 0
    except (Failure, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        current_live = safe_live_snapshot()
        emit("failed", ok=False, reason=str(error) if isinstance(error, Failure) else "operation_failed",
             errno=getattr(error, "errno", None), automatic_unload=False,
             initial_live=initial_live, current_live=current_live,
             attempted=progress.attempted, loaded_by_this_run=progress.loaded,
             possible_global_kernel_state_change=bool(progress.attempted) or initial_live != current_live,
             cleanup_unconfirmed=progress.cleanup_unconfirmed)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
