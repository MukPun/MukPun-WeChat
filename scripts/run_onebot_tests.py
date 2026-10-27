#!/usr/bin/env python3
"""Trusted entrypoint: verify sandbox before importing simulation dependencies."""
import json
import os
from pathlib import Path
import sys
import unittest


def isolation():
    checks = {}
    try:
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        checks['non_root'] = os.geteuid() != 0 and os.getegid() != 0
        checks['zero_caps'] = all(int(status[key].strip(), 16) == 0 for key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))
        checks['no_new_privileges'] = status['NoNewPrivs'].strip() == '1'
        interfaces = {line.split(':',1)[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:] if ':' in line}
        checks['private_loopback_only'] = interfaces == {'lo'}
        checks['readonly_code_and_root'] = all(os.statvfs(path).f_flag & os.ST_RDONLY for path in ('/','/usr','/etc','/opt/onebot'))
        mounts = Path('/proc/self/mountinfo').read_text().splitlines()
        checks['docker_masked'] = all(any(line.split()[4] == path and 'inaccessible' in line.split()[3] for line in mounts)
                                      for path in ('/run/docker.sock','/run/containerd'))
        checks['home_hidden'] = all(any(line.split()[4] == path and ' - tmpfs ' in line for line in mounts) for path in ('/home','/root'))
        group = Path('/proc/self/cgroup').read_text().strip().removeprefix('0::')
        base = Path('/sys/fs/cgroup') / group.lstrip('/')
        expected = {'memory.max':'134217728','memory.high':'100663296','memory.swap.max':'0',
                    'pids.max':'16','cpu.max':'10000 100000'}
        checks['resource_limits'] = all((base/name).read_text().strip() == value for name,value in expected.items())
    except Exception:
        checks['inspection_complete'] = False
    return checks


def main():
    checks = isolation()
    good = len(checks) == 8 and all(checks.values())
    print(json.dumps({'stage':'onebot_simulation_isolation','ok':good,'checks':checks}), flush=True)
    if not good:
        return 1
    root = Path('/opt/onebot')
    sys.path[:0] = [str(root), str(root/'onebot-env/lib/python3.12/site-packages')]
    suite = unittest.defaultTestLoader.discover(str(root/'tests'), pattern='test_onebot_*.py')
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    failures = [test.id() for test,_ in (result.failures + result.errors)][:8]
    # These tests run with synthetic data and no live credentials or network.
    failure_details = {test.id(): trace.splitlines()[-1][:320]
                       for test, trace in (result.failures + result.errors)[:8]}
    print(json.dumps({'stage':'onebot_linux_tests','ok':result.wasSuccessful(),
                      'tests':result.testsRun,'skipped':len(result.skipped),'failures':failures,
                      'failure_details':failure_details}), flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
