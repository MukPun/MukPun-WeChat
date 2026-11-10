#!/usr/bin/env python3
"""Report a dead forwarder through the Native daemon's own send path.

The forwarder is the only process that talks to AstrBot, so when it dies the
AstrBot side simply goes quiet and nobody notices: on 2026-11-09 it stayed dead
for 37 hours. systemd already retries (Restart=on-failure, StartLimitBurst=3);
what was missing is the notice after the retries run out.

The Native daemon is the one component that survives, because it lives in its
own transient unit and keeps serving the WeChat account while the forwarder is
down. It is reachable only from inside its network namespace, so every call goes
through nsenter and its own bearer token, which never leaves this process.

Delivery is best effort. A notification that fails must not turn one failed unit
into two, so every outcome exits 0 and the journal line carries the evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid

HERE = Path(__file__).resolve()
CONFIG = HERE.parents[1] / '.runtime' / 'forwarder.json'
CONTROL = Path('/run/astrbot-wechat-session')
STATE = Path('/var/lib/astrbot-wechat-session')
TARGET_UNIT = 'astrbot-wechat-forwarder.service'
ALERT_UNIT = 'astrbot-wechat-forwarder-alert.service'
NATIVE_UNIT = 'astrbot-wechat-login-'
MAX_TEXT = 1800
TIMEOUT = 20
# Native authenticates reads and writes with this bearer token. It lives inside
# the daemon's mount namespace, so it is read through the daemon's own /proc.
TOKEN_PATH = '/var/lib/astrbot-wechat-poc/data/api-token'


def record(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def setting(name, default=None):
    try:
        return json.loads(CONFIG.read_text(encoding='utf-8')).get(name, default)
    except Exception:
        return default


def native_pid():
    """Return the pid of the WeChat daemon that owns the account.

    The daemon is a child of the transient login unit, not its main process, so
    the handle is the cgroup: read every pid the unit holds and keep the one
    whose argv is the daemon itself. Trusting the unit prefix alone would also
    accept a half-dead unit that merely still carries it.
    """
    result = subprocess.run(['/usr/bin/systemctl', 'list-units', '--all', '--type=service',
                             '--state=running', '--plain', '--no-legend', '--no-pager'],
                            capture_output=True, timeout=TIMEOUT)
    for line in result.stdout.decode('utf-8', 'replace').splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith(NATIVE_UNIT):
            continue
        shown = subprocess.run(['/usr/bin/systemctl', 'show', '-p', 'ControlGroup', '--value',
                                fields[0]], capture_output=True, timeout=TIMEOUT)
        group = shown.stdout.decode('utf-8', 'replace').strip()
        if not group.startswith('/'):
            continue
        try:
            pids = Path('/sys/fs/cgroup' + group + '/cgroup.procs').read_text().split()
        except OSError:
            continue
        for entry in pids:
            try:
                pid = int(entry)
            except ValueError:
                continue
            if pid <= 1:
                continue
            try:
                argv = Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0')
            except OSError:
                continue
            if b'/opt/wx' in argv:
                return pid
    return None


def token(pid):
    result = subprocess.run(['/usr/bin/nsenter', '-t', str(pid), '-m', '--',
                             '/usr/bin/cat', TOKEN_PATH], capture_output=True, timeout=TIMEOUT)
    if result.returncode != 0:
        return None
    value = result.stdout.decode('ascii', 'replace').strip()
    return value if 0 < len(value) <= 256 else None


def active():
    """Read the unit state from systemd, not from the bridge's own last word.

    A killed process leaves its status file saying "active", so the self-report
    cannot decide whether to shout; only systemd knows the unit is gone.
    """
    result = subprocess.run(['/usr/bin/systemctl', 'show', '-p', 'ActiveState', '--value',
                             TARGET_UNIT], capture_output=True, timeout=TIMEOUT)
    return result.stdout.decode('ascii', 'replace').strip()


def resolve(pid, secret, peer):
    """Find the direct thread whose display name is exactly the contact.

    The catalog search is a contains-match, so the exact name is checked here
    instead of trusted; two contacts sharing a substring would otherwise route
    the alert to whichever the daemon happened to order first.
    """
    path = ('/v1/accounts/wechat-primary/contacts?search='
            + urllib.parse.quote(peer, safe=''))
    result = subprocess.run(['/usr/bin/nsenter', '-t', str(pid), '-n', '--', '/usr/bin/curl',
                             '-s', '-H', 'Authorization: Bearer ' + secret, '--get',
                             'http://127.0.0.1:26174' + path], capture_output=True, timeout=TIMEOUT)
    if result.returncode != 0:
        return None
    try:
        page = json.loads(result.stdout.decode('utf-8'))
    except Exception:
        return None
    items = page.get('items') if isinstance(page, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get('deleted') is True:
            continue
        if item.get('displayName') != peer:
            continue
        identity = item.get('id')
        if not isinstance(identity, str) or not identity or identity != item.get('username'):
            continue
        if identity.endswith('@chatroom') or identity.startswith('gh_'):
            continue
        return identity
    return None


def send(pid, secret, thread, text):
    payload = json.dumps({'threadId': thread, 'kind': 'text', 'text': text},
                         ensure_ascii=False).encode('utf-8')
    # The idempotency key travels in the header, not the body: Native reads
    # "idempotency-key" and rejects the request without it. A fresh uuid keeps
    # two genuine outages from collapsing into one message.
    return subprocess.run(['/usr/bin/nsenter', '-t', str(pid), '-n', '--', '/usr/bin/curl',
                           '-s', '-X', 'POST',
                           '-H', 'Authorization: Bearer ' + secret,
                           '-H', 'Content-Type: application/json',
                           '-H', 'idempotency-key: ' + uuid.uuid4().hex,
                           '--data-binary', '@-',
                           'http://127.0.0.1:26174/v1/accounts/wechat-primary/sends'],
                          input=payload, capture_output=True, timeout=TIMEOUT)


def reason():
    """Collect the codes a reader needs to know why the unit gave up."""
    value = {'unit': TARGET_UNIT}
    for name, path in (('status', CONTROL / 'forwarder.json'),
                       ('failure', STATE / 'last-forwarder-failure.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if name == 'status':
            for field in ('code', 'recovery', 'requires_review', 'updated', 'active', 'ready'):
                if field in data:
                    value[field] = data[field]
        else:
            value['failureCode'] = data.get('code')
            if type(data.get('updated')) is int:
                value['failedAt'] = data['updated']
    return value


def text(value):
    lines = ['[中转层停摆] ' + TARGET_UNIT]
    if value.get('failureCode'):
        lines.append('原因码：' + str(value['failureCode']))
    if value.get('code') and value['code'] != value.get('failureCode'):
        lines.append('状态码：' + str(value['code']))
    if type(value.get('failedAt')) is int:
        lines.append('最后一次失败：' + time.strftime('%Y-%m-%d %H:%M:%S',
                                                   time.localtime(value['failedAt'])))
    if value.get('recovery'):
        lines.append('恢复状态：' + str(value['recovery']))
    lines.append('systemd 已耗尽重启次数，需要人工处理。')
    return '\n'.join(lines)[:MAX_TEXT]


def unit_texts(entry=HERE):
    """Return the alert unit and the drop-in that points the forwarder at it.

    A drop-in is used instead of editing the forwarder unit because the
    forwarder's own installer treats any byte change to its unit file as
    tampering; the drop-in layers the trigger on top without disturbing it.
    """
    return ('''# Managed only by astrbot-wechat
[Unit]
Description=Report a stopped AstrBot WeChat forwarder
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I -B "@ENTRY@"
MemoryMax=128M
MemoryHigh=96M
MemorySwapMax=0
CPUQuota=25%
TasksMax=8
Nice=19
UMask=0077
LimitCORE=0
ProtectKernelModules=yes
TimeoutStartSec=90
TimeoutStopSec=10
StandardOutput=journal
StandardError=journal
'''.replace('@ENTRY@', str(entry)), '''# Managed only by astrbot-wechat
[Unit]
OnFailure=@ALERT@
'''.replace('@ALERT@', ALERT_UNIT))


def write_unit(path, content):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or path.read_text() != content):
            raise ValueError('alert_unit_changed')
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(descriptor, content.encode())
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install():
    base = Path('/etc/systemd/system')
    if not (base / TARGET_UNIT).exists():
        raise ValueError('forwarder_unit_missing')
    service, dropin = unit_texts()
    dropin_dir = base / TARGET_UNIT.replace('.service', '.service.d')
    if not dropin_dir.exists():
        dropin_dir.mkdir(mode=0o755)
    write_unit(base / ALERT_UNIT, service)
    write_unit(dropin_dir / 'alert.conf', dropin)
    subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], capture_output=True, timeout=TIMEOUT,
                   check=True)
    return {'stage': 'forwarder_alert_installed', 'ok': True, 'unit': ALERT_UNIT}


def main():
    if sys.argv[1:] == ['install']:
        if os.geteuid() != 0:
            record({'stage': 'forwarder_alert_installed', 'ok': False, 'code': 'root_required'})
            return 2
        try:
            record(install())
        except ValueError as error:
            record({'stage': 'forwarder_alert_installed', 'ok': False, 'code': str(error)})
            return 1
        except Exception as error:
            record({'stage': 'forwarder_alert_installed', 'ok': False, 'code': type(error).__name__})
            return 1
        return 0
    if sys.argv[1:] == ['--render']:
        record(reason())
        return 0
    try:
        state = active()
        if state == 'active':
            # The drop-in also fires for units that never actually stopped, and
            # an operator restarting the service by hand must not page anyone.
            record({'stage': 'alert', 'ok': True, 'code': 'already_healthy'})
            return 0
        peer = setting('privateContact')
        if not isinstance(peer, str) or not peer:
            record({'stage': 'alert', 'ok': False, 'code': 'no_contact'})
            return 0
        thread = setting('alertThread')
        if not isinstance(thread, str) or not thread:
            thread = None
        value = reason()
        pid = native_pid()
        if pid is None:
            record({'stage': 'alert', 'ok': False, 'code': 'native_absent', 'reason': value})
            return 0
        secret = token(pid)
        if not secret:
            record({'stage': 'alert', 'ok': False, 'code': 'token_unavailable', 'reason': value})
            return 0
        if not thread:
            thread = resolve(pid, secret, peer)
        if not thread:
            record({'stage': 'alert', 'ok': False, 'code': 'thread_required',
                    'contact': peer, 'reason': value})
            return 0
        result = send(pid, secret, thread, text(value))
        payload = json.loads(result.stdout.decode('utf-8', 'replace') or '{}')
        if not isinstance(payload, dict):
            payload = {}
        record({'stage': 'alert', 'ok': payload.get('state') in ('queued', 'accepted'),
                'state': payload.get('state'), 'sendId': payload.get('id'),
                'serverId': payload.get('serverId'), 'reason': value})
    except Exception as error:
        record({'stage': 'alert', 'ok': False, 'code': type(error).__name__})
    return 0


if __name__ == '__main__':
    sys.exit(main())
