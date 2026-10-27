#!/usr/bin/env python3
"""Real static assets, actual Native client validation, no API calls or tokens."""
import base64
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import secrets
import sys
import time


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def worker():
    assert os.geteuid() != 0
    group = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().removeprefix('0::').lstrip('/')
    limits = {'memory.max': '33554432', 'memory.high': '25165824', 'memory.swap.max': '0',
              'cpu.max': '10000 100000', 'pids.max': '4'}
    actual = {k: (group / k).read_text().strip() for k in limits}
    print(json.dumps({'stage': 'meme_memory_limits', 'actual': actual}), flush=True)
    assert actual == limits, str(actual)
    root = Path('/opt/onebot')
    native = module('_memory_native', root / 'scripts/forward_native_worker.py')
    image = native.image_sender.images
    names = {'0075_sticker_bf_107.jpg', '0024_sticker_bf_032.jpg', '0019_sticker_bf_025.png',
             '0109_sticker_bf_156.png', '0165_sticker_issue_07_013.png', '0081_sticker_bf_120.png'}
    results = []
    for path in Path('/opt/assets').rglob('*'):
        if path.name not in names or 'happy' not in path.parts or not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        started = time.monotonic()
        shape = image._rejection_header(raw)[1]
        meta = {'mime': 'image/png' if path.suffix == '.png' else 'image/jpeg', 'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(), 'quality': 'original'}
        assembly = image.Assembly(meta)
        for index, offset in enumerate(range(0, len(raw), image.CHUNK_BYTES)):
            assembly.add(index, base64.b64encode(raw[offset:offset+image.CHUNK_BYTES]).decode())
        # Native receives chunks, never the source-side full file plus payload.
        del raw
        complete = assembly.finish()
        del assembly
        sender = native.image_sender.Client('wechat-primary', 'x'*32, 'fixture-peer', complete,
                                            'a'*32, self_sender_id='fixture-self')
        assert sender.image['sha256'] == complete['sha256']
        results.append({'name': path.name, 'bytes': complete['bytes'], 'width': shape[0], 'height': shape[1],
                        'seconds': round(time.monotonic()-started, 3)})
        del sender, complete
        gc.collect()
    assert len(results) == len(names)
    print(json.dumps({'stage': 'meme_memory_worker', 'ok': True, 'assets': results,
                      'limits_verified': True,
                      'charged_peak_bytes': int((group / 'memory.peak').read_text()) if (group/'memory.peak').exists() else None,
                      'charged_current_bytes': int((group / 'memory.current').read_text()),
                      'oom_kills': int(dict(line.split() for line in (group/'memory.events').read_text().splitlines())['oom_kill']),
                      'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), flush=True)


def main():
    if sys.argv[1:] == ['--worker']:
        return worker()
    assert os.geteuid() == 0 and not sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_AS, (96*1024*1024, 96*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    here = Path(__file__).resolve().parent
    shared = module('_memory_host', here / 'session_runtime_host.py')
    tap, owned = shared.tap, shared.owned
    host = {k: owned.identity('/proc/self/ns/'+k) for k in ('net','user')}
    network, business = tap.read_network(), tap.business_snapshot()
    unit = 'astrbot-wechat-meme-memory-' + secrets.token_hex(6) + '.service'
    properties = ['DynamicUser=yes','PrivateNetwork=yes','PrivateDevices=yes','ProtectHome=tmpfs',
        'ProtectSystem=strict','NoNewPrivileges=yes','CapabilityBoundingSet=','AmbientCapabilities=',
        'ProtectKernelModules=yes','ProtectKernelTunables=yes','ProtectControlGroups=yes',
        'MemoryMax=32M','MemoryHigh=24M','MemorySwapMax=0','CPUQuota=10%','CPUQuotaPeriodSec=100ms','TasksMax=4',
        'Nice=19','RuntimeMaxSec=120','TimeoutStopSec=5','Restart=no','KillMode=control-group',
        'LimitCORE=0','LimitFSIZE=0','UMask=0077','WorkingDirectory=/opt/onebot',
        'TemporaryFileSystem=/tmp:rw,size=1M,mode=1777',
        'BindReadOnlyPaths='+str(here/'context-staging/verification')+':/opt/onebot '
            '/home/bridgeuser/AstrBot/data/plugin_data/meme_manager/packs:/opt/assets',
        'InaccessiblePaths=-/run/docker.sock -/run/containerd -/var/lib/docker -/run/frpc -/run/frp '
            '-/var/lib/astrbot-wechat-session -/run/astrbot-wechat-session',
        'UnsetEnvironment=HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy PYTHONPATH PYTHONHOME']
    capture = None
    try:
        tap.require_unit_absent(unit)
        capture = tap.Capture(['/usr/bin/systemd-run','--system','--quiet','--wait','--pipe',
            '--unit='+unit,*['--property='+p for p in properties],'/usr/bin/python3','-I','-B',
            '/opt/onebot/probe_meme_memory.py','--worker'])
        code = capture.finish(127)
        records = [json.loads(row) for row in bytes(capture.output['stdout']).decode().splitlines()]
        if code != 0:
            lines = bytes(capture.output['stderr']).decode(errors='replace').splitlines()
            details = tap.details(unit)
            print(json.dumps({'stage': 'meme_memory_failed', 'unit': unit, 'exit_code': code, 'detail': lines[-5:],
                'manager': {k: details.get(k) for k in ('ActiveState','SubState','Result','ExecMainCode','ExecMainStatus')},
                'records': records}), flush=True)
        assert code == 0 and len(records) == 2 and records[-1].get('stage') == 'meme_memory_worker'
        print(json.dumps(records[-1]), flush=True)
    finally:
        details = tap.details(unit)
        if details.get('ActiveState') == 'failed' and details.get('MainPID') == '0':
            status, _ = tap.command_result(['/usr/bin/systemctl','reset-failed',unit], 4)
            assert status == 0
        tap.stop_unit(unit, True)
        if capture:
            capture.close()
        flags, failures = tap.compare_baselines(host, network, business)
        print(json.dumps({'stage': 'meme_memory_cleanup', 'ok': not failures, **flags}), flush=True)


if __name__ == '__main__':
    main()
