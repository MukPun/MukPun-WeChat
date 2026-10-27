"""Temporary CDN access for this project's existing slirp cgroup only.

Learn a single public :443 destination from the pinned wx process's own socket.
Never accept an address supplied by an IPC peer. Restore the original cgroup
allowlist after the media job; no firewall, routes, modules or service restart.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time

ERROR_CODES = frozenset({'media_egress_policy', 'media_egress_ambiguous', 'media_egress_unit', 'media_egress_apply',
    'media_egress_identity', 'media_egress_sockets', 'media_egress_job', 'media_egress_expired',
    'media_egress_capacity', 'media_egress_conflict'})


def addresses(raw):
    result = set()
    for item in raw.split():
        network = ipaddress.ip_network(item, strict=True)
        if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
            raise ValueError('media_egress_policy')
        result.add(str(network.network_address))
    if not 0 < len(result) <= 32:
        raise ValueError('media_egress_policy')
    return result


def destination(rows, known, inodes):
    candidates = set()
    for line in rows.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[9] not in inodes or fields[3] not in {'01', '02'}:
            continue
        address, port = fields[2].split(':')
        if port != '01BB':
            continue
        ip = ipaddress.IPv4Address(bytes.fromhex(address)[::-1])
        if ip.is_global and str(ip) not in known:
            candidates.add(str(ip))
    if len(candidates) > 1:
        raise ValueError('media_egress_ambiguous')
    return next(iter(candidates), None)


class Lease:
    def __init__(self, core, pinned):
        self.core, self.pinned = core, pinned
        suffix = core.groups.UNIT_RE.fullmatch(pinned.host.unit).group(1)
        self.unit = core.tap.HELPER_PREFIX + suffix + '.service'
        if not re.fullmatch('astrbot-wechat-slirp-tap-helper-[0-9a-f]{12}\\.service', self.unit):
            raise ValueError('media_egress_unit')
        self.original = self.expected = None
        self.job = self.lock = None
        self.changed = False
        self.grants = 0
        self.until = 0

    def policy(self):
        result = subprocess.run(['/usr/bin/systemctl', 'show', self.unit, '--property=IPAddressAllow', '--value'],
                                capture_output=True, timeout=4)
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError('media_egress_policy')
        return addresses(result.stdout.decode('ascii'))

    def set_policy(self, values):
        result = subprocess.run(['/usr/bin/systemctl', 'set-property', '--runtime', self.unit,
                                 'IPAddressAllow=',
                                 'IPAddressAllow=' + ' '.join(ip + '/32' for ip in sorted(values))],
                                capture_output=True, timeout=4)
        if result.returncode or self.policy() != values:
            raise ValueError('media_egress_apply')

    def helper(self):
        core = self.core
        info = core.tap.details(self.unit)
        pid = int(info.get('MainPID', '0'))
        if (pid <= 1 or info.get('ActiveState') != 'active'
                or info.get('ControlGroup') != '/' + self.pinned.host.parent + '/' + self.unit
                or core.owned.identity('/proc/' + str(pid) + '/exe') != core.owned.identity('/usr/bin/slirp4netns')):
            raise ValueError('media_egress_identity')
        return pid, core.tap.start_ticks(pid)

    def base_policy(self):
        path = '/proc/' + str(self.pinned.host.client_pid) + '/root/opt/network.json'
        metadata = json.loads(self.core.owned.read(Path(path), 16384))
        values = metadata.get('allowedIPv4')
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError('media_egress_policy')
        return addresses(' '.join(value + '/32' for value in values))

    def sdk_destination(self):
        core, pinned = self.core, self.pinned
        process_file = Path('/sys/fs/cgroup') / pinned.host.parent / pinned.host.unit / 'cgroup.procs'
        numbers = core.owned.read(process_file, 4096).split()
        if not 0 < len(numbers) <= 32:
            raise ValueError('media_egress_identity')
        expected = core.owned.identity('/proc/' + str(pinned.host.client_pid) + '/root/opt/wx')
        matches = []
        for number in numbers:
            base = Path('/proc') / str(int(number))
            try:
                if core.owned.identity(base / 'exe') != expected:
                    continue
                ticks = core.tap.start_ticks(int(number))
                status = core.owned.parse_status(core.owned.read(base / 'status'))
                if set(map(int, status['Uid'].split())) != {pinned.host.client_uid}:
                    raise ValueError('media_egress_identity')
                entries = list((base / 'fd').iterdir())
                if len(entries) > 256:
                    raise ValueError('media_egress_sockets')
                inodes = set()
                for entry in entries:
                    try:
                        link = os.readlink(entry)
                    except FileNotFoundError:
                        continue
                    found = re.fullmatch(r'socket:\[([0-9]+)\]', link)
                    if found:
                        inodes.add(found[1])
                candidate = destination(core.owned.read(base / 'net/tcp', 65536), self.original, inodes)
                if core.tap.start_ticks(int(number)) != ticks:
                    raise ValueError('media_egress_identity')
                matches.append(candidate)
            except FileNotFoundError:
                continue
        if len(matches) != 1:
            raise ValueError('media_egress_identity')
        return matches[0]

    def wait(self, job):
        if not re.fullmatch('job_[0-9a-f-]{36}', job):
            raise ValueError('media_egress_job')
        if self.job is None:
            import fcntl
            path = self.core.owner.CONTROL / 'media-egress.lock'
            lock = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                os.close(lock)
                raise
            self.lock, self.job = lock, job
            self.original = self.expected = self.policy()
            if self.original != self.base_policy():
                raise ValueError('media_egress_policy')
            self.identity = self.helper()
            self.until = time.monotonic() + 45
        if self.job != job or self.helper() != self.identity or not self.pinned.unchanged():
            raise ValueError('media_egress_identity')
        if time.monotonic() >= self.until:
            self.close()
            raise ValueError('media_egress_expired')
        if self.changed:
            return False
        candidate = self.sdk_destination()
        if candidate is None:
            return False
        values = self.original | {candidate}
        if len(values) > 32:
            raise ValueError('media_egress_capacity')
        self.expected = values
        # Mark before I/O: cleanup restores even if a post-update check fails.
        self.changed = True
        self.set_policy(values)
        self.grants += 1
        if self.helper() != self.identity:
            raise ValueError('media_egress_identity')
        return True

    def done(self, job):
        if self.job is not None and self.job != job:
            raise ValueError('media_egress_job')
        self.close()

    def close(self):
        restored = not self.changed
        try:
            if self.changed:
                if self.helper() != self.identity:
                    raise ValueError('media_egress_identity')
                current = self.policy()
                if current not in (self.expected, self.original):
                    raise ValueError('media_egress_conflict')
                self.set_policy(self.original)
                if self.helper() != self.identity:
                    raise ValueError('media_egress_identity')
                restored = True
        finally:
            if self.lock is not None:
                os.close(self.lock)
            self.lock = self.job = None
            self.changed = not restored
