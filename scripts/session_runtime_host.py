#!/usr/bin/env python3
"""Trusted owner of one bounded guest and its private, quiescent snapshots.

No login is implicit here. Offline startup has only a private loopback network;
online startup uses the previously verified user-mode transport without changing
host networking. Only exact randomly named units created by this object stop.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import sys
import tempfile
import time


def _companion(name):
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("_session_host_" + name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("companion_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


legacy = _companion("run_login_probe")
guest = _companion("session_runtime_client")
store = _companion("session_state_store")
tap, owned, require = legacy.tap, legacy.owned, legacy.require
RUNTIME_LIMITS = {**tap.LIMITS, 'memory.max':str(3072*1024*1024), 'memory.high':str(2816*1024*1024)}
MODULES = ("empty_client_probe.py", "qr_login_protocol.py", "login_client_probe.py",
           "contact_receive_protocol.py", "contact_send_protocol.py", "session_state_store.py",
           "session_resume_protocol.py", "session_runtime_client.py")
FILES = MODULES + ("wx", "server.toml", "hosts", "nsswitch.conf", "resolv.conf",
                   "keeper.py", "launcher.py", "network.json", "session_config.json",
                   "command.json", "command.next", "restore.zip")
DATA_PARTS = ("var", "lib", "astrbot-wechat-poc", "data")
FINGERPRINT_FILES = {"api_token": "api-token", "storage_key": "storage-key",
                     "cursor_key": "cursor-key", "registry": "registry.sqlite"}


def validate_envelope(line):
    require(isinstance(line, bytes) and len(line) <= legacy.MAX_CLIENT_LINE, "runtime_line_bound")
    value = json.loads(line, object_pairs_hook=guest._unique,
                       parse_constant=guest._invalid_constant)
    require(isinstance(value, dict) and set(value) == {"sequence", "record"}
            and type(value["sequence"]) is int and 0 <= value["sequence"] < 2**53,
            "runtime_envelope")
    record = value["record"]
    require(isinstance(record, dict) and isinstance(record.get("stage"), str), "runtime_record")
    stage = record["stage"]
    if stage == "runtime_isolated":
        good = record == {"stage": stage, "ok": True} and type(record["ok"]) is bool
    elif stage == "runtime_ready":
        good = (set(record) == {"stage", "ok", "restored", "accounts", "connected"}
                and record["ok"] is True and type(record["restored"]) is bool
                and type(record["accounts"]) is int and record["accounts"] in (0, 1)
                and type(record["connected"]) is bool)
    elif stage == "runtime_status":
        good = (set(record) == {"stage", "accounts", "connected"}
                and type(record["accounts"]) is int and record["accounts"] in (0, 1)
                and type(record["connected"]) is bool)
    elif stage == "runtime_result":
        good = (set(record) == {"stage", "ok", "code"} and type(record["ok"]) is bool
                and isinstance(record["code"], str) and record["code"] in guest.RUNTIME_CODES)
    elif stage == "runtime_stopped":
        good = set(record) == {"stage", "ok"} and type(record["ok"]) is bool
    elif stage == "resume_state":
        good = (set(record) == {"stage", "state"} and isinstance(record["state"], str)
                and record["state"] in guest.resume.ACTIVE_STATES)
    else:
        # Existing validator limits QR bytes and all contact/login fields. There
        # is deliberately no send relay in the persistent runtime.
        require(stage in {"login_qr", "login_state", "contact_profile", "contact_state",
                          "contact_waiting", "contact_observation", "contact_result"},
                "runtime_record_not_allowed")
        legacy.parse_client_line(json.dumps(record).encode(), set(guest.RUNTIME_CODES),
                                 set(guest.login.ISOLATION_CHECKS))
        good = True
    require(good, "runtime_record_fields")
    return value


def offline_properties(bundle, user, parent, seconds):
    # No namespace holder, TAP, DNS or externally reachable interface is needed.
    values = [value for value in legacy.client_properties(bundle, user, parent, -1, seconds)
              if not value.startswith("--property=NetworkNamespacePath=")]
    return legacy.properties_changed(values, PrivateNetwork="yes")


class SessionHost:
    def __init__(self, state_dir: Path, *, offline: bool, seconds: int = 1800, quarantined_send=None, restore_sha256=None):
        if (type(offline) is not bool or type(seconds) is not int or not 30 <= seconds <= 604800
                or offline and seconds > 3600):
            raise ValueError("runtime_arguments")
        if restore_sha256 is not None and (not isinstance(restore_sha256, str)
                or len(restore_sha256) != 64 or any(c not in "0123456789abcdef" for c in restore_sha256)):
            raise ValueError("restore_sha256")
        self.restore_sha256 = restore_sha256
        self.quarantined_send = (guest.validate_quarantined_send(quarantined_send)
                                 if quarantined_send is not None else None)
        self.store = store.SnapshotStore(Path(state_dir))
        self.offline, self.seconds = offline, seconds
        suffix = secrets.token_hex(6)
        self.unit = legacy.PREFIX + suffix + ".service"
        self.parent = "astrbotwechatprobe" + suffix + ".slice"
        self.keeper_unit = owned.PREFIX + suffix + ".service"
        self.helper_unit = tap.HELPER_PREFIX + suffix + ".service"
        self.user, self.owner_user = "awpl-" + suffix, "awpo-" + suffix
        self.bundle = None
        self.captures, self.attempted, self.fds = {}, [], []
        self.ready = self.stopped = self.can_snapshot = False
        self.stop_ok = None
        self.restore = False
        self.sequence = -1
        self.client_pid = self.client_ticks = self.client_uid = self.data_dev = None
        self.client_pidfd = None
        self.data_fd = self.root_fd = None
        self.host = self.before_network = self.before_business = None
        self.closed = False
        self.close_result = None
        self.deadline = None

    def start(self):
        require(sys.platform == "linux" and os.geteuid() == 0, "linux_root_required")
        require(self.bundle is None and not self.closed, "runtime_already_started")
        self.host = {kind: owned.identity("/proc/self/ns/" + kind) for kind in ("user", "net")}
        require(self.host == {kind: owned.identity("/proc/1/ns/" + kind) for kind in self.host},
                "host_initial_namespaces")
        self.before_network, self.before_business = tap.read_network(), tap.business_snapshot()
        self.store.prepare()
        # The old process is already stopped when its owner starts a replacement.
        # Never mutate a live Native database; collect only a verified snapshot
        # copy. Cache maintenance failure must not block text/session recovery.
        self.cache_maintenance = {'skipped': True}
        if not self.offline and self.quarantined_send is None:
            try:
                self.cache_maintenance = _companion('media_cache_maintenance').maintain_store(
                    self.store, store, native_stopped=True)
            except Exception:
                pass
        latest = self.store.latest()
        self.restore = latest is not None
        require((self.quarantined_send is None and self.restore_sha256 is None) or self.restore, "quarantined_restore_missing")
        for unit in (self.parent, self.unit, self.keeper_unit, self.helper_unit):
            tap.require_unit_absent(unit)
        self.bundle = Path(tempfile.mkdtemp(prefix="astrbot-wechat-session.", dir="/var/tmp"))
        fs = os.statvfs(self.bundle)
        require(fs.f_bavail * fs.f_frsize >= 128 * 1024 * 1024 and
                not fs.f_flag & os.ST_NOEXEC, "staging_headroom_exec")
        legacy.copy_asset(self.bundle)
        for name in MODULES:
            legacy.write_public(self.bundle, name, legacy.source_text(Path(__file__).with_name(name)))
        legacy.write_public(self.bundle, "server.toml", "schemaVersion = 1\n")
        legacy.write_public(self.bundle, "nsswitch.conf", "passwd: files\ngroup: files\nhosts: files\n")
        legacy.write_public(self.bundle, "resolv.conf", "")
        hosts = {} if self.offline else self._resolve_hosts()
        legacy.write_public(self.bundle, "hosts", "127.0.0.1 localhost\n::1 localhost\n" +
                            "".join(f"{address} {name}\n" for name in legacy.DOMAINS
                                    for address in hosts.get(name, [])))
        legacy.write_public(self.bundle, "session_config.json", json.dumps({
            "schemaVersion": 1, "restore": self.restore, "offline": self.offline,
            "runtimeSeconds": self.seconds, "account": "wechat-primary",
            **({"quarantinedSend": self.quarantined_send} if self.quarantined_send is not None else {})}))
        self.request(0, "status")
        if latest is not None:
            self._stage_restore(latest)
        os.chmod(self.bundle, 0o755)
        self.attempted.append((self.parent, False))
        tap.start_slice(self.parent)
        tap.command(['/usr/bin/systemctl','set-property','--runtime',self.parent,'MemoryMax=3072M','MemoryHigh=2816M'])
        tap.cgroup_limits(self.parent, RUNTIME_LIMITS)
        netfd = None if self.offline else self._start_transport(hosts)
        properties = (offline_properties(self.bundle, self.user, self.parent, self.seconds + 20)
                      if self.offline else legacy.client_properties(
                          self.bundle, self.user, self.parent, netfd, self.seconds + 20))
        # Native 1.2.0 admits SQLite via two temporary full copies.
        # Demand-allocated tmpfs holds the cache and both startup admission copies.
        properties = legacy.properties_changed(properties, LimitFSIZE='768M',MemoryMax='2560M',MemoryHigh='2304M',
            TemporaryFileSystem='/var/lib:rw,size=2048M,nr_inodes=1024,mode=1777,nosuid,nodev,noexec /dev/shm:ro,size=4K,nr_inodes=16,mode=000,nosuid,nodev,noexec')
        args = ["/usr/bin/systemd-run", "--system", "--unit=" + self.unit,
                "--collect", "--wait", "--pipe", "--quiet", *properties,
                "/usr/bin/python3", "-I", "-B", "/opt/session_runtime_client.py"]
        self.attempted.append((self.unit, True))
        self.captures[self.unit] = legacy.ClientCapture(args)
        self.deadline = time.monotonic() + self.seconds + 18
        return self

    def _resolve_hosts(self):
        require(Path("/sys/class/misc/tun").exists(), "tun_already_registered")
        tun = os.stat("/dev/net/tun")
        require(stat.S_ISCHR(tun.st_mode) and (os.major(tun.st_rdev), os.minor(tun.st_rdev)) == (10, 200),
                "tun_device")
        interfaces = json.loads(tap.command([tap.IP, "-j", "addr", "show"]))
        addresses = {item["local"] for interface in interfaces for item in interface.get("addr_info", [])}
        hosts = legacy.resolve_hosts(addresses)
        allowed = {address for values in hosts.values() for address in values}
        require(1 <= len(allowed) <= 32, "login_allowlist_budget")
        return hosts

    def _stage_restore(self, source):
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o600
                    and 0 < info.st_size <= store.MAX_ARCHIVE_BYTES, "restore_source")
            target = os.open(self.bundle / "restore.zip", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW | os.O_CLOEXEC, 0)
            try:
                total = 0
                digest = hashlib.sha256()
                while chunk := os.read(descriptor, 65536):
                    digest.update(chunk)
                    total += len(chunk)
                    require(total <= store.MAX_ARCHIVE_BYTES, "restore_source_bound")
                    view = memoryview(chunk)
                    while view:
                        written = os.write(target, view)
                        require(written > 0, "restore_write")
                        view = view[written:]
                require(total == info.st_size, "restore_source_stable")
                require(self.restore_sha256 is None or digest.hexdigest() == self.restore_sha256,
                        "restore_snapshot_binding")
                os.fsync(target)
            finally:
                os.close(target)
        finally:
            os.close(descriptor)

    def _start_transport(self, hosts):
        seconds = self.seconds + 30
        keeper = legacy.source_text(Path(__file__).with_name("probe-owned-netns.py"))
        require(keeper.count("time.sleep(12)") == 1 and 60 <= seconds <= 604830, "keeper_source_contract")
        legacy.write_public(self.bundle, "keeper.py", keeper.replace("time.sleep(12)", f"time.sleep({seconds})"))
        legacy.write_public(self.bundle, "launcher.py", tap.LAUNCHER)
        args = ["/usr/bin/systemd-run", "--system", "--unit=" + self.keeper_unit,
                "--collect", "--wait", "--pipe", "--quiet",
                *legacy.properties_changed(owned.properties(str(self.bundle), self.owner_user),
                                           Slice=self.parent, RuntimeMaxSec=str(seconds)),
                "/usr/bin/unshare", "--user", "--map-root-user", "--net", "/usr/bin/setpriv",
                "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs",
                "/usr/bin/python3", "-I", "-B", "/opt/keeper.py", "--keeper"]
        self.attempted.append((self.keeper_unit, True))
        self.captures[self.keeper_unit] = tap.Capture(args)
        info = owned.await_owned_keeper(self.captures[self.keeper_unit], self.keeper_unit, self.host, seconds=5)
        pid, ticks = int(info["pid"]), tap.start_ticks(int(info["pid"]))
        self.fds.append(os.pidfd_open(pid, 0))
        for kind in ("user", "net"):
            fd = os.open(f"/proc/{pid}/ns/{kind}", os.O_RDONLY | os.O_CLOEXEC)
            self.fds.append(fd)
            actual = os.fstat(fd)
            require({"dev": actual.st_dev, "ino": actual.st_ino} == info[kind + "ns"], "pinned_namespace_identity")
        netfd = self.fds[-1]
        require(tap.start_ticks(pid) == ticks and owned.verify_owned_keeper(pid, self.keeper_unit, self.host) == info,
                "keeper_identity_stable")
        tap.check_target(tap.read_network(netfd, info["netns"], self.host["net"]), False)
        allowed = sorted({address for values in hosts.values() for address in values})
        args = ["/usr/bin/systemd-run", "--system", "--unit=" + self.helper_unit,
                "--collect", "--wait", "--pipe", "--quiet",
                *legacy.helper_properties(self.bundle, self.owner_user, self.parent, seconds, allowed),
                "/usr/bin/python3", "-I", "-B", "/opt/launcher.py"]
        args += list(map(str, (pid, ticks, info["host_uid"], info["host_gid"],
            info["userns"]["dev"], info["userns"]["ino"], info["netns"]["dev"], info["netns"]["ino"],
            self.host["user"]["dev"], self.host["user"]["ino"], self.host["net"]["dev"], self.host["net"]["ino"]))) + [self.helper_unit]
        self.attempted.append((self.helper_unit, True))
        helper = self.captures[self.helper_unit] = tap.Capture(args)
        deadline = time.monotonic() + 4
        while bytes(helper.output["stdout"]) != b"1":
            require(time.monotonic() < deadline, "helper_ready_deadline")
            helper.drain()
            require(bytes(helper.output["stdout"]) in (b"", b"1"), "helper_ready_exact")
            tap.require_helper_running(helper)
        helper_pid = int(tap.details(self.helper_unit).get("MainPID", "0"))
        tap.verify_helper(helper_pid, self.helper_unit, info, self.host, self.parent)
        tap.check_target(tap.read_network(netfd, info["netns"], self.host["net"]), True)
        for command in (["link", "set", "lo", "up"], ["addr", "add", "10.0.2.100/24", "dev", "tap0"],
                        ["link", "set", "tap0", "up"], ["route", "add", "default", "via", "10.0.2.2", "dev", "tap0"]):
            legacy.target_command(netfd, info["netns"], self.host["net"], command)
        metadata = {"schemaVersion": 1, "netns": info["netns"], "hostUserns": self.host["user"],
                    "hostNetns": self.host["net"], "keeperOwnerUid": info["host_uid"],
                    "loginUnit": self.unit, **legacy.NETWORK, "allowedIPv4": allowed}
        legacy.write_public(self.bundle, "network.json", json.dumps(metadata))
        return netfd

    def _pin_client(self):
        state = tap.details(self.unit)
        pid = int(state.get("MainPID", "0"))
        require(pid > 1 and state.get("ActiveState") == "active" and
                state.get("ControlGroup") == "/" + self.parent + "/" + self.unit, "client_own_unit")
        ticks = tap.start_ticks(pid)
        base = Path("/proc") / str(pid)
        require(owned.identity(base / "exe") == owned.identity("/usr/bin/python3"), "client_python_exe")
        require(owned.read(base / "cmdline", 4096).encode().split(b"\0")[:-1] ==
                [b"/usr/bin/python3", b"-I", b"-B", b"/opt/session_runtime_client.py"], "client_argv")
        status = owned.parse_status(owned.read(base / "status"))
        uids, gids = list(map(int, status["Uid"].split())), list(map(int, status["Gid"].split()))
        require(len(uids) == len(gids) == 4 and min(uids + gids) > 0 and
                len(set(uids)) == len(set(gids)) == 1 and owned.zero_caps(status)
                and status["NoNewPrivs"].strip() == "1", "client_nonroot_identity")
        require(owned.read(base / "cgroup").strip() == "0::/" + self.parent + "/" + self.unit,
                "client_exact_cgroup")
        # A pidfd pins this exact controller even if the numeric PID is reused.
        # It is opened only after argv/UID/cgroup checks, then start ticks are
        # checked again below before it can ever be used for a signal.
        pidfd = os.pidfd_open(pid, 0)
        self.fds.append(pidfd)
        rootfd = os.open(base / "root", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self.fds.append(rootfd)
        current = rootfd
        for component in DATA_PARTS:
            current = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=current)
            self.fds.append(current)
        info = os.fstat(current)
        require(info.st_uid == uids[0] and stat.S_IMODE(info.st_mode) == 0o700, "client_data_directory")
        require(tap.start_ticks(pid) == ticks and tap.details(self.unit).get("MainPID") == str(pid),
                "client_process_unchanged")
        self.client_pid, self.client_ticks, self.client_uid = pid, ticks, uids[0]
        self.client_pidfd = pidfd
        self.data_fd, self.root_fd, self.data_dev = current, rootfd, info.st_dev

    def _client_alive(self):
        require(self.client_pid is not None and tap.start_ticks(self.client_pid) == self.client_ticks,
                "client_process_unchanged")
        state = tap.details(self.unit)
        require(state.get("MainPID") == str(self.client_pid) and state.get("ActiveState") == "active"
                and state.get("ControlGroup") == "/" + self.parent + "/" + self.unit, "client_still_owned")

    def _interrupt_controller(self):
        # The mailbox may be unread while the controller waits in an explicitly
        # requested receive/resume operation. Its SIGTERM handler stops native
        # and holds the tmpfs for a final checkpoint. Never signal its native
        # child, a process group, an unpinned numeric PID or an unrelated unit.
        require(type(self.client_pidfd) is int and self.client_pidfd >= 0,
                "client_pidfd_unavailable")
        self._client_alive()
        signal.pidfd_send_signal(self.client_pidfd, signal.SIGTERM)

    def pump(self, delay=.1):
        require(self.unit in self.captures and not self.closed, "runtime_not_started")
        require(0 <= delay <= 1, "runtime_pump_delay")
        capture = self.captures[self.unit]
        for unit, holder in self.captures.items():
            if unit != self.unit:
                holder.drain(0)
                require(holder.process.poll() is None, "transport_holder_exited")
        capture.drain(delay)
        records = []
        while b"\n" in capture.output["stdout"]:
            line, _, rest = capture.output["stdout"].partition(b"\n")
            capture.output["stdout"][:] = rest
            envelope = validate_envelope(bytes(line))
            record = envelope["record"]
            require(envelope["sequence"] <= self.sequence, "runtime_future_sequence")
            if self.offline:
                require(record["stage"] in {"runtime_isolated", "runtime_ready", "runtime_status",
                                            "runtime_result", "runtime_stopped"}, "offline_record_forbidden")
            if record["stage"] == "runtime_isolated":
                require(self.client_pid is None and envelope["sequence"] == 0, "runtime_isolation_repeated")
                self._pin_client()
                if self.restore:
                    archive = self.bundle / "restore.zip"
                    os.chown(archive, self.client_uid, -1, follow_symlinks=False)
                    os.chmod(archive, 0o400, follow_symlinks=False)
            elif record["stage"] == "runtime_ready":
                require(self.client_pid is not None and not self.ready and record["restored"] == self.restore,
                        "runtime_ready_order")
                require(envelope["sequence"] == 0 and (record["accounts"] > 0 or not record["connected"]),
                        "runtime_ready_inventory")
                self.ready = self.can_snapshot = True
                if self.restore:
                    os.chmod(self.bundle / "restore.zip", 0, follow_symlinks=False)
            elif record["stage"] == "runtime_stopped":
                self.stopped = True
                self.stop_ok = record["ok"]
                self.can_snapshot = self.can_snapshot and record["ok"]
            records.append(envelope)
        if capture.process.poll() is not None and not self.stopped:
            raise legacy.Failure("runtime_client_exited")
        return records

    def request(self, sequence, action, allow_fresh=False):
        command = guest.validate_command({"schemaVersion": 1, "sequence": sequence,
                                          "action": action, "allowFreshLogin": allow_fresh})
        require(self.bundle is not None and not self.closed and sequence > self.sequence,
                "runtime_command_order")
        if self.offline:
            require(action in {"status", "stop", "release"}, "offline_action_forbidden")
        target = self.bundle / "command.next"
        legacy.write_public(self.bundle, target.name, json.dumps(command))
        os.replace(target, self.bundle / "command.json")
        self.sequence = sequence

    def _freeze(self):
        self._client_alive()
        tap.command(["/usr/bin/systemctl", "freeze", self.unit], 3)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            text = tap.command(["/usr/bin/systemctl", "show", self.unit, "--property=FreezerState", "--value"])
            if text.strip() == "frozen":
                return
            time.sleep(.02)
        raise legacy.Failure("client_freeze_deadline")

    def checkpoint(self):
        require(self.can_snapshot and self.data_fd is not None and not self.closed, "checkpoint_not_ready")
        frozen = False
        try:
            # Even after native stops, freeze its controller to stabilize the
            # exact fd-pinned directory while SQLite WAL files are captured.
            frozen = True
            self._freeze()
            return self.store.commit(self.data_fd, expected_uid=self.client_uid, expected_dev=self.data_dev)
        finally:
            if frozen:
                tap.command(["/usr/bin/systemctl", "thaw", self.unit], 3)

    def fingerprints(self):
        require(self.can_snapshot and self.data_fd is not None and not self.closed, "checkpoint_not_ready")
        result = {}
        try:
            self._freeze()
            for label, name in FINGERPRINT_FILES.items():
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.data_fd)
                try:
                    info = os.fstat(fd)
                    require(stat.S_ISREG(info.st_mode) and info.st_uid == self.client_uid and info.st_nlink == 1
                            and info.st_dev == self.data_dev and 0 < info.st_size <= 8 * 1024 * 1024,
                            "fingerprint_file")
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        result[label] = hashlib.file_digest(stream, "sha256").digest()
                finally:
                    os.close(fd)
        finally:
            tap.command(["/usr/bin/systemctl", "thaw", self.unit], 3)
        return result

    def close(self):
        if self.closed:
            return self.close_result
        errors, stopped = [], []
        native_stop_ok = final_snapshot_saved = False
        if self.unit in self.captures and self.client_pid is not None:
            try:
                if not self.stopped:
                    self.request(self.sequence + 1, "stop")
                    deadline = time.monotonic() + 8
                    mailbox_deadline = min(deadline, time.monotonic() + 1)
                    while not self.stopped and time.monotonic() < mailbox_deadline:
                        self.pump(.05)
                    if not self.stopped:
                        self._interrupt_controller()
                    while not self.stopped and time.monotonic() < deadline:
                        self.pump(.05)
                    require(self.stopped, "runtime_stop_deadline")
                require(self.stop_ok is True, "runtime_native_stop_failed")
                native_stop_ok = True
                if self.can_snapshot:
                    self.checkpoint()
                    final_snapshot_saved = True
                self.request(self.sequence + 1, "release")
                capture = self.captures[self.unit]
                deadline = time.monotonic() + 3
                while capture.process.poll() is None and time.monotonic() < deadline:
                    self.pump(.05)
            except Exception:
                errors.append("runtime_graceful_cleanup_failed")
        for unit, service in reversed(self.attempted):
            try:
                tap.stop_unit(unit, service)
                stopped.append(unit)
            except Exception:
                errors.append("runtime_unit_cleanup_failed")
        for capture in self.captures.values():
            try:
                capture.close()
            except Exception:
                errors.append("runtime_capture_cleanup_failed")
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError:
                errors.append("runtime_fd_cleanup_failed")
        if self.bundle is not None:
            try:
                for name in FILES:
                    (self.bundle / name).unlink(missing_ok=True)
                self.bundle.rmdir()
            except OSError:
                errors.append("runtime_bundle_cleanup_failed")
        baseline = {}
        if self.before_network is not None:
            try:
                baseline, failures = tap.compare_baselines(self.host, self.before_network, self.before_business)
                errors.extend(failures)
            except Exception:
                errors.append("runtime_baseline_cleanup_failed")
        self.closed = True
        self.close_result = {"ok": not errors, "code": "ok" if not errors else errors[0],
                             "explicit_stop": stopped, **baseline,
                             "native_stop_ok": native_stop_ok, "final_snapshot_saved": final_snapshot_saved}
        return self.close_result

    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_error):
        self.close()
