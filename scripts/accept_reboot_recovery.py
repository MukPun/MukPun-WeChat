#!/usr/bin/env python3
"""Isolated lifecycle acceptance for reboot recovery; root only, read-only live.

    isolated   two genuine constrained lifecycles across the real recovery stack
    baseline   record the current live acceptance baseline (private, root only)
    check      compare live status and process/network baselines against it

`isolated` starts real recovery child processes and a local synthetic Native
API, then terminates them at controlled durable boundaries. It never reads a
real account, never sends a message and never invokes a model. `baseline` and
`check` only inspect the running system; the sole write is the private
baseline document under the session state directory.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
# scripts/ holds the entrypoints and the reviewed staging manifest; the
# sandbox needs the repository root so tools/ and tests/ are visible.
SOURCE = ROOT.parent
STEP = 'startup'
MAX_OUTPUT = 1024 * 1024


def emit(record):
    print(json.dumps(record, sort_keys=True, ensure_ascii=True), flush=True)


def module(name):
    path = ROOT / (name + '.py')
    spec = importlib.util.spec_from_file_location('_accept_reboot_' + name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


STAGE = '/var/tmp/reboot-accept-'
OWNER = 'astrbot-wechat-session-owner.service'
OWNER_GROUP = '/system.slice/' + OWNER
OWNER_SCRIPTS = {'session_runtime.py': ('--serve-online', '--serve-long', '--serve-recovery'),
                 'resume_quarantined_native.py': ('--serve',)}


def pin_native(tap, group):
    """Resolve the live Native unit without assuming this checkout's path.

    `run_group_probe.find_native_unit()` compares absolute script paths against
    its own directory, which is right for a probe but wrong for a staged
    acceptance run from a sibling tree. Every identity requirement is kept; only
    the directory prefix is relaxed to the script basename plus its action.
    """
    owner = tap.details(OWNER)
    if owner.get('ActiveState') != 'active' or owner.get('ControlGroup') != OWNER_GROUP:
        raise ValueError('owner_unavailable')
    pid = int(owner.get('MainPID', '0'))
    if pid <= 1:
        raise ValueError('owner_identity')
    group.root_identity(pid, OWNER_GROUP, '/usr/bin/python3')
    arguments = [item.decode('utf-8', 'surrogateescape') for item in group.argv(pid)]
    if not (len(arguments) == 5 and arguments[0] == '/usr/bin/python3'
            and arguments[1] == '-I' and arguments[2] == '-B'
            and Path(arguments[3]).name in OWNER_SCRIPTS
            and arguments[4] in OWNER_SCRIPTS[Path(arguments[3]).name]):
        raise ValueError('owner_identity')
    for value in group.owned.read(
            Path('/sys/fs/cgroup') / OWNER_GROUP.lstrip('/') / 'cgroup.procs', 4096).split():
        candidate = int(value)
        if candidate == pid:
            continue
        try:
            found = [item[len(b'--unit='):].decode('ascii') for item in group.argv(candidate)
                     if item.startswith(b'--unit=astrbot-wechat-login-')]
        except (FileNotFoundError, ProcessLookupError):
            continue
        if not found:
            continue
        group.root_identity(candidate, OWNER_GROUP, '/usr/bin/systemd-run')
        return found[0]
    raise ValueError('owner_capture')


def stage_tree(source):
    """Copy the reviewed tree where a DynamicUser child can read it."""
    target = Path(STAGE + os.urandom(6).hex())
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    try:
        for name in ('scripts', 'wechat_bridge', 'tests', 'tools'):
            origin = Path(source) / name
            if not origin.is_dir():
                continue
            for parent, directories, files in os.walk(origin):
                directories[:] = [item for item in directories if item != '__pycache__']
                held = target / Path(parent).relative_to(source)
                held.mkdir(parents=True, exist_ok=True)
                os.chmod(held, 0o755)
                for item in files:
                    if not item.endswith('.py'):
                        continue
                    copy = held / item
                    shutil.copyfile(Path(parent) / item, copy)
                    os.chmod(copy, 0o644)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def isolated():
    """Run the lifecycle suites in a bounded, read-only, private-network child."""
    global STEP
    tap = module('probe-slirp-tap')
    group = module('run_group_probe')
    state = module('session_runtime')
    unit = 'astrbot-wechat-reboot-accept-' + os.urandom(6).hex() + '.service'
    source = None
    records = []
    capture = None
    clean = False
    ok = False
    diagnostic = {'unit': unit}
    host = {kind: tap.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = tap.read_network(), tap.business_snapshot()
    native = None
    if state.owner_active():
        item = pin_native(tap, group)
        details = tap.details(item)
        native = (item, details.get('MainPID'), tap.start_ticks(int(details['MainPID'])))
    try:
        tap.require_unit_absent(unit)
        source = stage_tree(SOURCE)
        properties = [
            'Type=exec', 'DynamicUser=yes', 'PrivateNetwork=yes', 'PrivateDevices=yes',
            'ProtectSystem=strict', 'ProtectHome=tmpfs', 'NoNewPrivileges=yes',
            'CapabilityBoundingSet=', 'AmbientCapabilities=', 'ProtectKernelModules=yes',
            'ProtectKernelTunables=yes', 'ProtectControlGroups=yes', 'ProtectKernelLogs=yes',
            'ProtectProc=invisible', 'RestrictNamespaces=yes', 'RestrictRealtime=yes',
            'RestrictSUIDSGID=yes', 'LockPersonality=yes', 'MemoryMax=256M',
            'MemoryHigh=192M', 'MemorySwapMax=0', 'CPUQuota=10%',
            'CPUQuotaPeriodSec=100ms', 'TasksMax=16', 'LimitNOFILE=128',
            'RuntimeMaxSec=180', 'TimeoutStartSec=5', 'TimeoutStopSec=5', 'Restart=no',
            'OOMPolicy=stop', 'KillMode=control-group', 'Nice=19',
            'IOSchedulingClass=idle', 'LimitCORE=0', 'LimitFSIZE=16777216', 'UMask=0077',
            'RestrictAddressFamilies=AF_UNIX AF_INET',
            'BindReadOnlyPaths=' + str(source) + ':/opt/reboot-accept',
            'TemporaryFileSystem=/tmp:rw,size=16M,mode=1777 /var/tmp:rw,size=4M,mode=1777',
            'InaccessiblePaths=-/run/docker.sock -/run/docker -/run/containerd -/var/lib/docker '
            '-/var/lib/containerd -/run/frpc -/run/frp -/run/frps -/run/user -/run/dbus '
            '-/run/systemd/private -/var/lib/astrbot-wechat-session -/run/astrbot-wechat-session',
            'UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT PYTHONPATH PYTHONHOME '
            'HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy',
        ]
        arguments = ['/usr/bin/systemd-run', '--system', '--quiet', '--collect', '--wait',
                     '--pipe', '--unit=' + unit,
                     *['--property=' + value for value in properties],
                     '/usr/bin/python3', '-I', '-B', '/opt/reboot-accept/tools/run_isolated_lifecycles.py']
        capture = tap.Capture(arguments)
        emit({'stage': 'reboot_acceptance_started', 'unit': unit, 'maximum_seconds': 180})
        code = capture.finish(185)
        diagnostic['exit_code'] = code
        for line in bytes(capture.output['stdout']).decode('utf-8').splitlines():
            item = json.loads(line)
            if item.get('stage') not in {'reboot_lifecycle_isolation', 'reboot_lifecycle_tests'}:
                raise ValueError('unexpected_record')
            records.append(item)
        ok = (code == 0 and len(records) == 2
              and all(item.get('ok') is True for item in records))
    except Exception as error:
        diagnostic['error_class'] = type(error).__name__
        diagnostic['code'] = str(getattr(error, 'stage', 'inspection_failed'))[:64]
        ok = False
    finally:
        try:
            tap.stop_unit(unit, True)
            clean = True
        except Exception:
            pass
        if capture is not None:
            capture.close()
        if source is not None:
            shutil.rmtree(source, ignore_errors=True)
        flags, failures = tap.compare_baselines(host, network, business)
        same_native = True
        if native is not None:
            try:
                details = tap.details(native[0])
                same_native = (details.get('MainPID') == native[1]
                               and tap.start_ticks(int(native[1])) == native[2])
            except Exception:
                same_native = False
        for item in records:
            emit(item)
        if not ok:
            if capture is not None:
                lines = bytes(capture.output['stderr']).decode('utf-8', 'replace').splitlines()
                diagnostic['test_summary'] = [line[:240] for line in lines if
                    any(word in line for word in ('Error:', 'FAILED', 'Ran ',
                                                  'Service runtime', 'Failed with result'))][-8:]
            emit({'stage': 'reboot_lifecycle_failure', **diagnostic})
        emit({'stage': 'reboot_lifecycle_cleanup',
                    'ok': clean and not failures and same_native,
                    'unit_removed': clean, 'native_session_unchanged': same_native,
                    **flags})
    good = ok and clean and not failures and same_native
    emit({'stage': 'reboot_lifecycle_result', 'ok': good,
                'code': 'ok' if good else 'lifecycle_failed'})
    return 0 if good else 1


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, timeout=5)
    if result.returncode or len(result.stdout) > MAX_OUTPUT:
        raise ValueError('inspection_command_failed')
    return result.stdout.decode()


def unit(name):
    if not re.fullmatch(r'astrbot-wechat-[a-z0-9-]+\.service', name):
        raise ValueError('unit_name')
    props = 'ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp,Result,ControlGroup'
    return dict(line.split('=', 1) for line in command(
        ['/usr/bin/systemctl', 'show', name, '--property=' + props]).splitlines()
        if '=' in line)


def limits(group):
    if not re.fullmatch(r'/[a-zA-Z0-9_.\-/]+', group) or '..' in group.split('/'):
        raise ValueError('cgroup_shape')
    root = Path('/sys/fs/cgroup') / group.lstrip('/')
    keys = ('memory.max', 'memory.high', 'memory.swap.max', 'pids.max', 'cpu.max')
    out = {k: (root / k).read_text().strip() for k in keys}
    out['memory.events'] = dict(
        line.split() for line in (root / 'memory.events').read_text().splitlines())
    return out


LIFECYCLE_UNITS = ('astrbot-wechat-session-owner.service',
                   'astrbot-wechat-forwarder.service')


def live_checks(core, state, pinned, services, caps):
    """Status and resource checks that must hold before and after recovery."""
    queue = state.get('queue', {})
    return {
        'fresh_status': 0 <= time.time() - state.get('updated', 0) <= 10,
        'ready': all(state.get(k) is True for k in
                     ('active', 'ready', 'sse', 'astrbot_connected',
                      'self_identity_verified')),
        'expected_features': all(state.get(k) is True for k in
                                 ('image_input_enabled', 'group_context_enabled')),
        # Approved quarantined rows are an accepted count, not a health signal.
        'quarantined_bounded': 0 <= state.get('uncertain', 0) <= 128,
        'queue_not_stuck': not queue.get('publishing'),
        'no_active_cdn_lease': state.get('image_egress_active') is False,
        'no_new_qr': state.get('new_qr') is False,
        'resource_caps': all(
            tuple(caps[k][p] for p in ('memory.max', 'memory.high', 'pids.max', 'cpu.max')) == v
            and caps[k]['memory.swap.max'] == '0'
            for k, v in {
                'native_parent': ('3221225472', '2952790016', '32', '20000 100000'),
                'controller': ('67108864', '50331648', '16', '5000 100000'),
                'onebot': ('536870912', '402653184', '8', '10000 100000'),
            }.items()),
        'no_oom_kills': all(int(c['memory.events'].get('oom_kill', '-1')) == 0
                            for c in caps.values()),
        'storage_bounded': state.get('resources', {}).get('native_storage', {}).get(
            'total_bytes') == 2147483648,
    }


def deployment_checks(core):
    """Every deployed module must match the reviewed staging manifest."""
    manifest_path = ROOT / 'context-staging/manifest.json'
    if manifest_path.is_symlink() or not 0 < manifest_path.stat().st_size <= 16384:
        raise ValueError('manifest_shape')
    manifest = json.loads(manifest_path.read_text())
    if (not isinstance(manifest, dict) or len(manifest) > 48
            or any(not re.fullmatch(r'(verification/(scripts|tests|wechat_bridge)/)?[a-z_]+\.py', name)
                   or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                   for name, digest in manifest.items())):
        raise ValueError('manifest_shape')
    changed = [name for name, digest in manifest.items()
               if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    return {'deployed_code_matches_review': not changed}, changed


def observe():
    """Load the formal entrypoints and take one read-only live observation."""
    global STEP
    formal = module('run_forwarder')
    STEP = 'configuration'
    formal.load_config()
    core = formal.core
    STEP = 'status'
    state = core.owner.read_json(core.STATUS)
    pinned = core.groups.PinnedNative()
    STEP = 'native_identity'
    pinned.open()
    try:
        STEP = 'service_inspection'
        names = command(['/usr/bin/systemctl', 'list-units', '--all', '--plain',
                         '--no-legend', 'astrbot-wechat-private-host-*.service']).splitlines()
        hosts = [name for name in (line.split()[0] for line in names if line.split())
                 if unit(name).get('ActiveState') == 'active']
        if len(hosts) != 1:
            raise ValueError('host_unit_count')
        services = {name: unit(name) for name in LIFECYCLE_UNITS + (hosts[0],)}
        caps = {'native_parent': limits('/' + pinned.host.parent),
                'controller': limits(services[core.UNIT]['ControlGroup']),
                'onebot': limits(services[hosts[0]]['ControlGroup'])}
        return {'core': core, 'state': state, 'pinned': pinned,
                'services': services, 'caps': caps, 'host_unit': hosts[0],
                'identity': {'pid': pinned.host.client_pid, 'ticks': pinned.host.client_ticks,
                             'netns': pinned.netns},
                'host': {kind: core.owned.identity('/proc/self/ns/' + kind)
                         for kind in ('net', 'user')}}
    finally:
        pinned.close()


def privatise(core, name):
    return core.owner.STATE / name


def baseline():
    """Record the accepted live baseline; the only documented write."""
    global STEP
    view = observe()
    core, state, services = view['core'], view['state'], view['services']
    STEP = 'reviewed_manifest'
    checks, changed = deployment_checks(core)
    checks.update(live_checks(core, state, view['pinned'], services, view['caps']))
    private = privatise(core, 'reboot-recovery-baseline.json')
    STEP = 'baseline_write'
    if private.exists():
        archived = privatise(
            core, 'reboot-recovery-before-' + str(int(time.time())) + '-' + os.urandom(4).hex() + '.json')
        core.owner.write_json(archived, core.owner.read_json(private))
    core.owner.write_json(private, {
        'at': int(time.time()), 'native': view['identity'], 'services': services,
        'host': view['host'], 'network': core.tap.read_network(),
        'business': core.tap.business_snapshot(), 'status': state})
    previous = privatise(core, 'last-forwarder-completion.json')
    previous = {k: core.owner.read_json(previous).get(k) for k in
                ('code', 'uptime_seconds', 'received', 'forwarded', 'completed_replies',
                 'uncertain', 'cleanup_ok', 'native_session_unchanged',
                 'business_processes_unchanged', 'host_network_unchanged')
                } if previous.exists() else {}
    print(json.dumps({'stage': 'reboot_recovery_baseline', 'ok': all(checks.values()),
                      'checks': checks, 'changed_reviewed_files': changed,
                      'host_unit': view['host_unit'],
                      'services': {n: {k: v.get(k) for k in
                                       ('ActiveState', 'SubState', 'MainPID', 'NRestarts',
                                        'ExecMainStartTimestamp', 'Result')}
                                   for n, v in services.items()},
                      'previous': previous}, sort_keys=True))
    return 0 if all(checks.values()) else 1


def check():
    """Compare the live system against the accepted baseline."""
    global STEP
    view = observe()
    core, state, services = view['core'], view['state'], view['services']
    STEP = 'reviewed_manifest'
    checks, changed = deployment_checks(core)
    checks.update(live_checks(core, state, view['pinned'], services, view['caps']))
    STEP = 'baseline_compare'
    saved = core.owner.read_json(privatise(core, 'reboot-recovery-baseline.json'))
    checks['native_session_unchanged'] = (view['identity'] == saved['native']
                                          and view['pinned'].unchanged())
    checks['bridge_workers_unchanged'] = (
        set(services) == set(saved['services'])
        and all(all(v.get(k) == saved['services'][n].get(k)
                    for k in ('MainPID', 'NRestarts', 'ExecMainStartTimestamp'))
                for n, v in services.items()))
    # JSON persists each (pid, comm, start_ticks) tuple as a list.
    business = [tuple(row) for row in saved['business']]
    flags, failures = core.tap.compare_baselines(saved['host'], saved['network'], business)
    checks.update({k: v for k, v in flags.items() if k.endswith('_unchanged')})
    checks['existing_service_baselines'] = not failures
    window = {'window_seconds': int(time.time()) - saved['at'],
              'business_process_count': len(business),
              'counters_delta': {k: state.get(k, 0) - saved['status'].get(k, 0) for k in
                                 ('received', 'forwarded', 'accepted_parts',
                                  'completed_replies', 'uncertain')},
              'image_delta': {k: state.get('images', {}).get(k, 0)
                              - saved['status'].get('images', {}).get(k, 0) for k in
                              ('prepared', 'forwarded', 'waiting', 'expired', 'unavailable')}}
    info = {k: state.get(k) for k in
            ('started_at', 'uptime_seconds', 'remaining_seconds', 'received', 'forwarded',
             'unsupported_actions', 'image_output_enabled', 'image_output_requests',
             'image_output_sent', 'image_output_rejected', 'onebot_link',
             'last_output_error', 'completed_replies', 'accepted_parts', 'uncertain',
             'queue', 'routes', 'group_traffic', 'images', 'last_image_error',
             'loop_guard', 'resources')}
    mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    print(json.dumps({'stage': 'reboot_recovery_check', 'ok': all(checks.values()),
                      'checks': checks, 'changed_reviewed_files': changed,
                      'services': {n: {k: v.get(k) for k in
                                       ('ActiveState', 'SubState', 'MainPID', 'NRestarts',
                                        'ExecMainStartTimestamp', 'Result')}
                                   for n, v in services.items()},
                      'host_unit': view['host_unit'], 'window': window, 'status': info,
                      'host_available_mib': int(mem['MemAvailable'].split()[0]) // 1024},
                     sort_keys=True))
    return 0 if all(checks.values()) else 1


def main():
    global STEP
    if sys.platform != 'linux' or os.geteuid() != 0:
        return 2
    if sys.argv[1:] not in (['isolated'], ['baseline'], ['check']):
        return 2
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    STEP = 'lifecycle_isolation'
    if sys.argv[1:] == ['isolated']:
        return isolated()
    STEP = 'observation'
    return baseline() if sys.argv[1:] == ['baseline'] else check()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:
        print(json.dumps({'stage': 'reboot_recovery_failed', 'ok': False, 'step': STEP,
                          'code': str(getattr(error, 'stage', 'inspection_failed'))[:64],
                          'error_class': type(error).__name__}))
        raise SystemExit(1)
