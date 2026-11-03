#!/usr/bin/env python3
"""Trusted entrypoint for the isolated reboot-recovery lifecycles.

Verify the sandbox before importing anything that touches the recovery stack,
then run the recovery suites: two genuine isolated process lifecycles against
synthetic fixtures and a loopback-only Native API, with no real account, no
message send and no model invocation.
"""
import json
import os
from pathlib import Path
import sys
import unittest

CODE = '/opt/reboot-accept'


def isolation():
    checks = {}
    try:
        status = dict(line.split(':', 1) for line in
                      Path('/proc/self/status').read_text().splitlines() if ':' in line)
        checks['non_root'] = os.geteuid() != 0 and os.getegid() != 0
        checks['zero_caps'] = all(int(status[key].strip(), 16) == 0
                                  for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))
        checks['no_new_privileges'] = status['NoNewPrivs'].strip() == '1'
        interfaces = {line.split(':', 1)[0].strip()
                      for line in Path('/proc/net/dev').read_text().splitlines()[2:] if ':' in line}
        checks['private_loopback_only'] = interfaces == {'lo'}
        checks['readonly_code'] = all(os.statvfs(path).f_flag & os.ST_RDONLY
                                      for path in ('/', '/usr', '/etc', CODE))
        mounts = Path('/proc/self/mountinfo').read_text().splitlines()
        checks['docker_masked'] = all(
            any(line.split()[4] == path and 'inaccessible' in line.split()[3] for line in mounts)
            for path in ('/run/docker.sock', '/run/containerd'))
        checks['home_hidden'] = all(
            any(line.split()[4] == path and ' - tmpfs ' in line for line in mounts)
            for path in ('/home', '/root'))
        group = Path('/proc/self/cgroup').read_text().strip().removeprefix('0::')
        base = Path('/sys/fs/cgroup') / group.lstrip('/')
        expected = {'memory.max': '268435456', 'memory.high': '201326592',
                    'memory.swap.max': '0', 'pids.max': '16', 'cpu.max': '10000 100000'}
        checks['resource_limits'] = all((base / name).read_text().strip() == value
                                        for name, value in expected.items())
    except Exception:
        checks['inspection_complete'] = False
    return checks


def main():
    checks = isolation()
    good = len(checks) == 8 and all(checks.values())
    print(json.dumps({'stage': 'reboot_lifecycle_isolation', 'ok': good,
                      'checks': checks}), flush=True)
    if not good:
        return 1
    root = Path(CODE)
    # tests/ first: the recovery suites import their shared fixtures by name.
    sys.path[:0] = [str(root), str(root / 'tests')]
    suite = unittest.TestSuite()
    missing = []
    for name in ('test_recovery_lifecycle', 'test_recovery_coordinator',
                 'test_recovery_checkpoint', 'test_recovery_manifest',
                 'test_recovery_snapshot'):
        try:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
        except Exception as error:
            # A suite that cannot even be imported is a failure, not a smaller
            # green run: name it and fail the acceptance step.
            missing.append(name + ': ' + type(error).__name__)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    failures = [test.id() for test, _ in (result.failures + result.errors)][:8]
    details = {test.id(): trace.splitlines()[-1][:320]
               for test, trace in (result.failures + result.errors)[:8]}
    ok = result.wasSuccessful() and not missing
    print(json.dumps({'stage': 'reboot_lifecycle_tests', 'ok': ok,
                      'tests': result.testsRun, 'skipped': len(result.skipped),
                      'failures': failures, 'failure_details': details,
                      'unloadable': missing}), flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
