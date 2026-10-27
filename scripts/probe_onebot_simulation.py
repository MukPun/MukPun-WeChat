#!/usr/bin/env python3
"""Bounded, loopback-only Linux simulation, independent of all live services."""
import importlib.util
import json
import os
from pathlib import Path
import resource
import secrets
import sys

ROOT = Path(__file__).resolve().parent


def companion(name):
    spec = importlib.util.spec_from_file_location('_onebot_probe_' + name, ROOT/(name+'.py'))
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def main():
    if sys.platform != 'linux' or os.geteuid() != 0:
        return 2
    if sys.argv[1:] not in ([], ['--context-stage']):
        return 2
    staged = sys.argv[1:] == ['--context-stage']
    resource.setrlimit(resource.RLIMIT_AS, (268435456,268435456))
    resource.setrlimit(resource.RLIMIT_CPU, (20,20))
    shared = companion('session_runtime_host')
    tap, owned, wire = shared.tap, shared.owned, shared.legacy.wire_record
    host = {kind:owned.identity('/proc/self/ns/'+kind) for kind in ('net','user')}
    network, business = tap.read_network(), tap.business_snapshot()
    # Observe existing Native identity only; do not read credentials or start it.
    owner = companion('session_runtime')
    native = None
    if owner.owner_active():
        groups = companion('run_group_probe')
        unit = groups.find_native_unit()
        details = tap.details(unit)
        native = (unit, details.get('MainPID'), tap.start_ticks(int(details['MainPID'])))
    source = ROOT / ('context-staging/verification' if staged else 'verification')
    unit = 'astrbot-wechat-onebot-sim-' + secrets.token_hex(6) + '.service'
    capture = None
    clean = False
    records = []
    diagnostic = {'unit': unit}
    ok = False
    try:
        tap.require_unit_absent(unit)
        properties = [
            'Type=exec','DynamicUser=yes','PrivateNetwork=yes','PrivateDevices=yes',
            'ProtectSystem=strict','ProtectHome=tmpfs','NoNewPrivileges=yes',
            'CapabilityBoundingSet=','AmbientCapabilities=','ProtectKernelModules=yes',
            'ProtectKernelTunables=yes','ProtectControlGroups=yes','ProtectKernelLogs=yes',
            'ProtectProc=invisible','RestrictNamespaces=yes','RestrictRealtime=yes','RestrictSUIDSGID=yes',
            'LockPersonality=yes','MemoryMax=128M','MemoryHigh=96M','MemorySwapMax=0',
            'CPUQuota=10%','CPUQuotaPeriodSec=100ms','TasksMax=16','LimitNOFILE=128',
            'RuntimeMaxSec=150','TimeoutStartSec=5','TimeoutStopSec=5','Restart=no',
            'OOMPolicy=stop','KillMode=control-group','Nice=19','IOSchedulingClass=idle',
            'LimitCORE=0','LimitFSIZE=16777216','UMask=0077',
            'RestrictAddressFamilies=AF_UNIX AF_INET','WorkingDirectory=/opt/onebot',
            'BindReadOnlyPaths='+str(source)+':/opt/onebot'
                + (' '+str(ROOT/'verification/onebot-env')+':/opt/onebot/onebot-env' if staged else ''),
            'TemporaryFileSystem=/tmp:rw,size=16M,mode=1777 /var/tmp:rw,size=4M,mode=1777',
            'InaccessiblePaths=-/run/docker.sock -/run/docker -/run/containerd -/var/lib/docker '
            '-/var/lib/containerd -/run/frpc -/run/frp -/run/frps -/run/user -/run/dbus '
            '-/run/systemd/private -/var/lib/astrbot-wechat-session -/run/astrbot-wechat-session',
            'UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT PYTHONPATH PYTHONHOME '
            'HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy',
        ]
        arguments = ['/usr/bin/systemd-run','--system','--quiet','--collect','--wait','--pipe','--unit='+unit,
                     *['--property='+value for value in properties], '/usr/bin/python3','-I','-B','/opt/onebot/run_onebot_tests.py']
        capture = tap.Capture(arguments)
        wire({'stage': 'onebot_simulation_started', 'unit': unit, 'maximum_seconds': 150})
        code = capture.finish(157)
        diagnostic['exit_code'] = code
        for line in bytes(capture.output['stdout']).decode('utf-8').splitlines():
            item = json.loads(line)
            if item.get('stage') not in {'onebot_simulation_isolation','onebot_linux_tests'}:
                raise ValueError('unexpected_record')
            records.append(item)
        ok = (code == 0 and len(records) == 2 and all(item.get('ok') is True for item in records)
              and records[-1].get('tests',0) >= 28 and records[-1].get('skipped') == 0)
    except Exception as error:
        diagnostic['error_class'] = type(error).__name__
        diagnostic['code'] = getattr(error, 'stage', 'inspection_failed')
        ok = False
    finally:
        try:
            tap.stop_unit(unit, True)
            clean = True
        except Exception:
            pass
        if capture is not None:
            capture.close()
        flags, failures = tap.compare_baselines(host, network, business)
        same_native = True
        if native is not None:
            details = tap.details(native[0])
            try:
                same_native = details.get('MainPID') == native[1] and tap.start_ticks(int(native[1])) == native[2]
            except Exception:
                same_native = False
        for item in records:
            wire(item)
        if not ok:
            if capture is not None:
                lines = bytes(capture.output['stderr']).decode('utf-8', errors='replace').splitlines()
                diagnostic['test_summary'] = [line[:240] for line in lines if
                    any(word in line for word in ('Error:', 'FAILED', 'Ran ', 'Service runtime', 'Failed with result'))][-8:]
            wire({'stage': 'onebot_simulation_failure', **diagnostic})
        wire({'stage':'onebot_simulation_cleanup','ok':clean and not failures and same_native,
              'unit_removed':clean,'native_session_unchanged':same_native,**flags})
    good = ok and clean and not failures and same_native
    wire({'stage':'onebot_simulation_result','ok':good,'code':'ok' if good else 'simulation_failed'})
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
