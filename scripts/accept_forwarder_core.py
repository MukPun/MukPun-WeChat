#!/usr/bin/env python3
"""Read-only live acceptance; only the private acceptance baseline is written."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
STEP = 'startup'


def command(args):
    result = subprocess.run(args, capture_output=True, timeout=5)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ValueError('inspection_command_failed')
    return result.stdout.decode()


def unit(name):
    if not re.fullmatch(r'astrbot-wechat-[a-z0-9-]+\.service', name):
        raise ValueError('unit_name')
    props = 'ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp,Result,ControlGroup'
    return dict(line.split('=', 1) for line in command(['/usr/bin/systemctl', 'show', name,
                '--property=' + props]).splitlines() if '=' in line)


def limits(group):
    if not re.fullmatch(r'/[a-zA-Z0-9_.\-/]+', group) or '..' in group.split('/'):
        raise ValueError('cgroup_shape')
    root = Path('/sys/fs/cgroup') / group.lstrip('/')
    keys = ('memory.max', 'memory.high', 'memory.swap.max', 'pids.max', 'cpu.max', 'memory.current')
    out = {k: (root / k).read_text().strip() for k in keys}
    out['memory.events'] = dict(line.split() for line in (root / 'memory.events').read_text().splitlines())
    return out


def history():
    """Read only systemd lifecycle metadata, never application log bodies."""
    raw = command(['/usr/bin/journalctl', '--no-pager', '--output=json', '-n', '200',
                   '--since=2026-10-26 15:00:00 UTC', '--until=2026-10-26 17:10:00 UTC',
                   '-u', 'astrbot-wechat-forwarder.service',
                   '-u', 'astrbot-wechat-private-host-*.service',
                   '-u', 'astrbot-wechat-group-read-*.service'])
    events = []
    for line in raw.splitlines():
        item = json.loads(line)
        if item.get('_COMM') != 'systemd':
            continue
        name = item.get('UNIT', '')
        if not re.fullmatch(r'astrbot-wechat-(forwarder|private-host-[a-f0-9]+|group-read-[a-f0-9]+)\.service', name):
            continue
        event = {k: item[k] for k in ('__REALTIME_TIMESTAMP', 'UNIT', 'MESSAGE_ID',
                                    'EXIT_CODE', 'EXIT_STATUS', 'JOB_RESULT', 'RESULT') if k in item}
        # Only fixed manager messages are allowed; no argv, exceptions or chat data.
        message = item.get('MESSAGE', '')
        match = re.search(r'(Main process exited, code=[a-z-]+, status=[A-Za-z0-9_/]+|'
                          r'Failed with result .[a-z-]+.|Scheduled restart job, restart counter is at [0-9]+)', message)
        if match:
            event['manager_event'] = match.group(0)
        events.append(event)
    print(json.dumps({'stage': 'core_acceptance_history', 'ok': True,
                      'manager_events': events, 'journal_rows': len(raw.splitlines())}, sort_keys=True))
    return 0


def main():
    global STEP
    if os.geteuid() != 0 or sys.argv[1:] not in (['baseline'], ['check'], ['history']):
        return 2
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    if sys.argv[1:] == ['history']:
        STEP = 'lifecycle_history'
        return history()
    spec = importlib.util.spec_from_file_location('_accept_core_formal', ROOT / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    STEP = 'configuration'
    formal.load_config()
    core = formal.core
    state = core.owner.read_json(core.STATUS)
    pinned = core.groups.PinnedNative()
    STEP = 'native_identity'
    pinned.open()
    try:
        STEP = 'service_inspection'
        names = command(['/usr/bin/systemctl', 'list-units', '--all', '--plain', '--no-legend',
                         'astrbot-wechat-private-host-*.service']).splitlines()
        hosts = [line.split()[0] for line in names if line.split()]
        hosts = [name for name in hosts if unit(name).get('ActiveState') == 'active']
        if len(hosts) != 1:
            raise ValueError('host_unit_count')
        services = {name: unit(name) for name in (formal.core.UNIT, 'astrbot-wechat-session-owner.service', hosts[0])}
        caps = {'native_parent': limits('/' + pinned.host.parent),
                'controller': limits(services[core.UNIT]['ControlGroup']),
                'onebot': limits(services[hosts[0]]['ControlGroup'])}
        expected = {'native_parent': ('402653184', '301989888', '32', '20000 100000'),
                    'controller': ('67108864', '50331648', '16', '5000 100000'),
                    'onebot': ('134217728', '100663296', '8', '10000 100000')}
        checks = {'fresh_status': 0 <= time.time() - state.get('updated', 0) <= 10,
                  'ready': all(state.get(k) is True for k in ('active', 'ready', 'sse', 'astrbot_connected', 'self_identity_verified')),
                  'expected_features': all(state.get(k) is True for k in ('image_input_enabled', 'group_context_enabled')),
                  'no_uncertain': state.get('uncertain') == 0,
                  'queue_drained': not any(state.get('queue', {}).get(k) for k in ('queued', 'publishing', 'uncertain')),
                  'no_active_cdn_lease': state.get('image_egress_active') is False,
                  'no_new_qr': state.get('new_qr') is False,
                  'resource_caps': all(tuple(caps[k][p] for p in ('memory.max', 'memory.high', 'pids.max', 'cpu.max')) == v
                                       and caps[k]['memory.swap.max'] == '0' for k, v in expected.items()),
                  'no_oom_kills': all(int(c['memory.events'].get('oom_kill', '-1')) == 0 for c in caps.values()),
                  'storage_bounded': state.get('resources', {}).get('native_storage', {}).get('total_bytes') == 201326592}
        STEP = 'reviewed_manifest'
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
        checks['deployed_code_matches_review'] = not changed
        identity = {'pid': pinned.host.client_pid, 'ticks': pinned.host.client_ticks, 'netns': pinned.netns}
        private = core.owner.STATE / 'core-acceptance-baseline.json'
        host = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
        if sys.argv[1:] == ['baseline']:
            STEP = 'baseline_write'
            if private.exists():
                archived = core.owner.STATE / ('core-acceptance-before-' + str(int(time.time())) + '-' + secrets.token_hex(4) + '.json')
                core.owner.write_json(archived, core.owner.read_json(private))
            core.owner.write_json(private, {'at': int(time.time()), 'native': identity, 'services': services,
                'host': host, 'network': core.tap.read_network(), 'business': core.tap.business_snapshot(), 'status': state})
            prior = core.owner.STATE / 'last-forwarder-completion.json'
            previous = core.owner.read_json(prior) if prior.exists() else {}
            previous = {k: previous.get(k) for k in ('code', 'uptime_seconds', 'received', 'forwarded', 'completed_replies',
                'uncertain', 'cleanup_ok', 'native_session_unchanged', 'business_processes_unchanged', 'host_network_unchanged')}
        else:
            saved = core.owner.read_json(private)
            checks['native_session_unchanged'] = identity == saved['native'] and pinned.unchanged()
            checks['bridge_workers_unchanged'] = (set(services) == set(saved['services']) and all(
                all(v.get(k) == saved['services'][n].get(k) for k in ('MainPID', 'NRestarts', 'ExecMainStartTimestamp'))
                for n, v in services.items()))
            # JSON persists each (pid, comm, start_ticks) tuple as a list.
            # Restore the snapshot's type before exact identity comparison.
            business = [tuple(row) for row in saved['business']]
            flags, failures = core.tap.compare_baselines(saved['host'], saved['network'], business)
            checks.update({k: v for k, v in flags.items() if k.endswith('_unchanged')})
            checks['existing_service_baselines'] = not failures
            previous = {'window_seconds': int(time.time()) - saved['at'],
                        'business_process_count': len(business),
                        'counters_delta': {k: state.get(k, 0) - saved['status'].get(k, 0)
                            for k in ('received', 'forwarded', 'accepted_parts', 'completed_replies', 'uncertain')},
                        'image_delta': {k: state.get('images', {}).get(k, 0) - saved['status'].get('images', {}).get(k, 0)
                            for k in ('prepared', 'forwarded', 'waiting', 'expired', 'unavailable')}}
        info = {k: state.get(k) for k in ('started_at', 'uptime_seconds', 'remaining_seconds', 'received', 'forwarded', 'unsupported_actions',
            'image_output_enabled', 'image_output_requests', 'image_output_sent', 'image_output_rejected', 'onebot_link', 'last_output_error',
            'completed_replies', 'accepted_parts', 'uncertain', 'queue', 'routes', 'group_traffic', 'images',
            'last_image_error', 'loop_guard', 'resources')}
        mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        print(json.dumps({'stage': 'core_acceptance_' + sys.argv[1], 'ok': all(checks.values()), 'checks': checks,
            'services': {n: {k: v.get(k) for k in ('ActiveState', 'SubState', 'MainPID', 'NRestarts', 'ExecMainStartTimestamp', 'Result')}
                         for n, v in services.items()},
            'changed_reviewed_files': changed, 'status': info, 'previous': previous,
            'host_available_mib': int(mem['MemAvailable'].split()[0]) // 1024}, sort_keys=True))
    finally:
        pinned.close()
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'stage': 'core_acceptance_failed', 'ok': False, 'step': STEP,
                          'code': getattr(error, 'stage', None), 'error_class': type(error).__name__}))
        raise SystemExit(1)
