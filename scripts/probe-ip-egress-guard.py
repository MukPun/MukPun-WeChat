#!/usr/bin/env python3
"""Trusted, short systemd IP-filter test against newly created local sockets only.

No Docker commands, namespace changes, firewall commands, DNS or external traffic.
The positive control is required: a timeout alone does not prove a working filter.
"""

import errno
import json
import os
import pathlib
import secrets
import socket
import subprocess
import sys


CHILD = r'''
import errno, json, os, pathlib, socket, sys
status = dict(line.split(':', 1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
control = pathlib.Path('/proc/self/cgroup').read_text().strip().split(':')[-1]
base = pathlib.Path('/sys/fs/cgroup') / control.lstrip('/')
checks = {
    'host_non_root': os.geteuid() != 0 and os.getegid() != 0,
    'zero_caps': all(int(status[k].strip(), 16) == 0 for k in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')),
    'no_new_privs': status['NoNewPrivs'].strip() == '1',
    'memory_32mb': (base/'memory.max').read_text().strip() == '33554432',
    'no_swap': (base/'memory.swap.max').read_text().strip() == '0',
    'tasks_8': (base/'pids.max').read_text().strip() == '8',
    'cpu_20_percent': (base/'cpu.max').read_text().strip() == '20000 100000',
    'root_readonly': bool(os.statvfs('/').f_flag & os.ST_RDONLY),
    'home_hidden': not pathlib.Path('/home/bridgeuser/.ssh').exists(),
    'host_net_for_trusted_control': os.readlink('/proc/self/ns/net') == sys.argv[3],
    'own_cgroup': 'astrbot-wechat-ipguard-' in control,
}
if not all(checks.values()):
    print(json.dumps({'checks':checks}), flush=True)
    raise SystemExit(2)
outcomes = []
for family, address, port in ((socket.AF_INET,'127.0.0.1',int(sys.argv[1])), (socket.AF_INET6,'::1',int(sys.argv[2]))):
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(1.5)
        try:
            client.connect((address, port))
            outcome = 'connected'
        except TimeoutError:
            outcome = 'timeout'
        except OSError as error:
            outcome = 'denied' if error.errno in (errno.EPERM, errno.EACCES) else 'unexpected_error'
        outcomes.append(outcome)
print(json.dumps({'checks':checks,'outcomes':outcomes}), flush=True)
'''


def run_probe(unit, ports, network, deny):
    properties = [
        'Type=exec', 'DynamicUser=yes', 'UMask=0077',
        'CPUQuota=20%', 'CPUQuotaPeriodSec=100ms',
        'MemoryHigh=24M', 'MemoryMax=32M', 'MemorySwapMax=0', 'TasksMax=8',
        'LimitNOFILE=32', 'LimitCORE=0', 'LimitFSIZE=0',
        'RuntimeMaxSec=8', 'TimeoutStartSec=5', 'TimeoutStopSec=2',
        'Restart=no', 'KillMode=control-group', 'OOMPolicy=stop',
        'Nice=19', 'IOSchedulingClass=idle', 'NoNewPrivileges=yes',
        'PrivateNetwork=no', 'PrivateUsers=no', 'PrivateDevices=yes',
        'ProtectHome=tmpfs', 'ProtectSystem=strict', 'ProtectProc=invisible',
        'ProtectKernelTunables=yes', 'ProtectKernelModules=yes',
        'ProtectControlGroups=yes', 'RestrictNamespaces=yes',
        'RestrictSUIDSGID=yes', 'RestrictRealtime=yes', 'LockPersonality=yes',
        'SystemCallArchitectures=native', 'CapabilityBoundingSet=',
        'RestrictAddressFamilies=AF_INET AF_INET6',
        'InaccessiblePaths=-/run/docker.sock -/run/containerd -/run/user -/tmp -/var/tmp',
    ]
    if deny:
        properties += ['IPAddressDeny=any', 'IPAccounting=yes']
    args = ['/usr/bin/systemd-run', '--system', '--unit='+unit,
            '--collect', '--wait', '--pipe', '--quiet']
    args += ['--property='+p for p in properties]
    args += ['/usr/bin/python3', '-I', '-B', '-c', CHILD,
             str(ports[0]), str(ports[1]), network]
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=14, check=False,
                                env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
        if result.returncode or len(result.stdout) > 8192:
            raise RuntimeError('unit_failed')
        parsed = json.loads(result.stdout)
        if not isinstance(parsed.get('checks'), dict) or not all(parsed['checks'].values()):
            raise RuntimeError('child_preflight_failed')
        return parsed
    finally:
        # Exact random unit created by this invocation only, never process-name cleanup.
        subprocess.run(['/usr/bin/systemctl', 'stop', unit],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5, check=False)
        state = subprocess.run(['/usr/bin/systemctl', 'show', unit, '--no-pager',
                                '--property=LoadState,ActiveState,MainPID'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=3, check=False)
        if len(state.stdout) > 1024:
            raise RuntimeError('cleanup_unconfirmed')
        fields = dict(line.split('=', 1) for line in state.stdout.decode('ascii').splitlines() if '=' in line)
        if not (fields.get('MainPID') == '0' and
                (fields.get('LoadState') == 'not-found' or
                 fields.get('ActiveState') in {'inactive', 'failed'})):
            raise RuntimeError('cleanup_unconfirmed')


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        print('Run with sudo, with no arguments; never put credentials in scripts.', file=sys.stderr)
        return 2
    prefix = 'astrbot-wechat-ipguard-' + secrets.token_hex(6)
    print('PROBE_UNIT_PREFIX='+prefix, flush=True)
    network = os.readlink('/proc/self/ns/net')
    listeners = []
    stage = 'prepare_local_listeners'
    try:
        for family, address in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
            listener = socket.socket(family, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind((address, 0))
            listener.listen(2)
            listener.settimeout(0.3)
        ports = [item.getsockname()[1] for item in listeners]
        stage = 'positive_control'
        control = run_probe(prefix+'-control.service', ports, network, False)
        if control.get('outcomes') != ['connected', 'connected']:
            raise RuntimeError('positive_control_failed')
        for listener in listeners:
            connection, _ = listener.accept()
            connection.close()
        stage = 'deny_all_control'
        guarded = run_probe(prefix+'-deny.service', ports, network, True)
        if guarded.get('outcomes') not in ([a,b] for a in ('denied','timeout') for b in ('denied','timeout')):
            raise RuntimeError('deny_filter_ineffective')
        for listener in listeners:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            connection.close()
            raise RuntimeError('unexpected_inbound_connection')
        if os.readlink('/proc/self/ns/net') != network:
            raise RuntimeError('host_namespace_changed')
        print(json.dumps({'unit_prefix':prefix,'control':control,'guarded':guarded,
                          'host_network_unchanged':True}, indent=2))
        print('IP_EGRESS_DENY_VERIFIED: only newly created IPv4/IPv6 loopback listeners were tested.')
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        safe_reasons = {'unit_failed', 'child_preflight_failed', 'positive_control_failed',
                        'deny_filter_ineffective', 'unexpected_inbound_connection',
                        'host_namespace_changed', 'cleanup_unconfirmed'}
        print(json.dumps({'stage':stage, 'error':type(error).__name__,
                          'reason':str(error) if str(error) in safe_reasons else 'probe_failed',
                          'errno':getattr(error,'errno',None)}))
        return 1
    finally:
        for listener in listeners:
            listener.close()


if __name__ == '__main__':
    raise SystemExit(main())
