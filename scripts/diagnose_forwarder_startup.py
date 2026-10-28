#!/usr/bin/env python3
"""Reproduce Native startup in the existing sandbox; GET only, no SSE or send."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import time


def main():
    assert os.geteuid() == 0 and not sys.argv[1:]
    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('_startup_diagnostic', root / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    formal.load_config()
    core, settings = formal.core, formal.core.SETTINGS
    pinned = core.groups.PinnedNative()
    unit = core.groups.READER_PREFIX + secrets.token_hex(6) + '.service'
    bundle, capture = None, None
    result = {'stage': 'native_startup_diagnostic', 'ok': False, 'model_called': False, 'sent': False}
    step = 'pin_native'
    baseline = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    try:
        pinned.open()
        lifecycle = core.owner.read_json(core.owner.CONTROL / 'status.json')
        result['lifecycle'] = {key: lifecycle.get(key) for key in ('ready', 'connected', 'ok', 'busy', 'code')}
        children = Path('/proc') / str(pinned.host.client_pid) / 'task' / str(pinned.host.client_pid) / 'children'
        result['native_processes'] = []
        for child in children.read_text().split():
            process = Path('/proc') / child
            result['native_processes'].append({'pid': int(child), 'comm': (process / 'comm').read_text().strip(),
                'threads': [{'comm': (task / 'comm').read_text().strip(), 'wchan': (task / 'wchan').read_text().strip()}
                            for task in (process / 'task').iterdir()]})
        step = 'inspect_native_outbox'
        account_directories = list((Path('/proc/self/fd') / str(pinned.host.data_fd) / 'accounts').iterdir())
        assert len(account_directories) == 1
        database = account_directories[0] / 'agent/control.sqlite'
        result['native_database_exists'] = database.is_file()
        try:
            native_connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.2)
        except sqlite3.Error as error:
            result['native_database_error'] = getattr(error, 'sqlite_errorname', type(error).__name__)
            native_connection = None
        if native_connection is not None:
            connection = native_connection
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            result['native_outbox'] = dict(connection.execute('SELECT state,COUNT(*) FROM outbox GROUP BY state'))
            result['native_media'] = [list(row) for row in connection.execute('SELECT kind,state,COUNT(*) FROM media_jobs GROUP BY kind,state')]
        step = 'inspect_bridge_queue'
        bridge_database = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve(strict=True)
        try:
            bridge_connection = sqlite3.connect(bridge_database.as_uri() + '?mode=ro', uri=True, timeout=.2)
        except sqlite3.Error as error:
            result['bridge_database_error'] = getattr(error, 'sqlite_errorname', type(error).__name__)
            bridge_connection = None
        if bridge_connection is not None:
            connection = bridge_connection
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            result['bridge_queue'] = dict(connection.execute('SELECT state,COUNT(*) FROM inbox GROUP BY state'))
        step = 'load_identity_proof'
        proof = core.self_proof()
        room = None
        if not settings['allGroupsEnabled']:
            _, binding = core.groups.load_send_binding(pinned, settings['groupName'])
            room = binding['roomId']
        bundle = Path(tempfile.mkdtemp(prefix='astrbot-wechat-startup-diagnostic.', dir='/var/tmp'))
        for name in core.FILES:
            source = core.HERE / name
            if not source.exists():
                source = core.HERE.parent / 'wechat_bridge' / name
            text = core.groups.legacy.source_text(source)
            if name == 'forward_native_worker.py':
                changes = {
                    "        peer, boundary = lookup._target": "        diagnostic_step = 'private_contact'\n        peer, boundary = lookup._target",
                    "        own = resolve_self(": "        diagnostic_step = 'self_identity'\n        own = resolve_self(",
                    "else 'native_worker_failed')": "else str(error) if isinstance(error, contact.ContactError) else 'native_worker_failed')",
                    "emit({'kind': 'native_error', 'code': code})": "emit({'kind': 'native_error', 'code': code, 'step': locals().get('diagnostic_step', 'configuration')})",
                }
                for before, after in changes.items():
                    assert text.count(before) == 1, 'diagnostic_source_changed'
                    text = text.replace(before, after)
                before = "        diagnostic_step = 'private_contact'"
                probes = """        original_path = lookup.policy.path
        for probe_name, probe_path in [('status', '/v1/status'), ('accounts', '/v1/accounts'), ('account', '/v1/accounts/wechat-primary')]:
            probe_start = time.monotonic()
            lookup.policy.path = probe_path
            try:
                probe_value = lookup._get(probe_path, time.monotonic() + 3, timeout=2)
                probe_result = {'ok': True}
                if probe_name == 'account':
                    probe_result.update({key: probe_value.get(key) for key in ('connection', 'authenticated')})
            except contact.ContactError as error:
                probe_result = {'ok': False, 'code': error.code}
            emit({'kind': 'diagnostic_probe', 'probe': probe_name, 'seconds': round(time.monotonic() - probe_start, 2), **probe_result})
        lookup.policy.path = original_path
"""
                text = text.replace(before, probes + before)
                ast.parse(text)
            core.groups.legacy.write_public(bundle, name, text)
        bundle.chmod(0o755)
        core.tap.require_unit_absent(unit)
        properties = core.groups.reader_properties(bundle, 'awdg-' + secrets.token_hex(4),
                                                   pinned.host.parent, pinned.netfd, 120)
        config = {'account': 'wechat-primary', 'token': pinned.token(), 'runtimeSeconds': 120,
                  'privateName': settings['privateContact'], 'groupName': settings['groupName'], 'roomId': room,
                  'groupPrefix': settings['groupPrefix'], 'groupWakeMode': settings['groupWakeMode'],
                  'sendInterval': settings['sendIntervalSeconds'], 'selfProof': proof,
                  'allGroups': settings['allGroupsEnabled']}
        args = ['/usr/bin/systemd-run', '--system', '--quiet', '--collect', '--wait', '--pipe', '--unit=' + unit,
                *properties, '/usr/bin/python3', '-I', '-B', '/opt/forward_native_worker.py']
        step = 'worker_startup'
        capture = core.Duplex(args, config)
        del config
        isolated, finished = False, False
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline and not finished:
            for record in capture.tick():
                if record.get('kind') == 'isolated' and record.get('ok') is True and not isolated:
                    core.groups.verify_reader(unit, pinned, 'forward_native_worker.py')
                    isolated = True
                    capture.acknowledge()
                elif record.get('kind') == 'native_ready' and isolated:
                    result.update(ok=True, step='native_ready', self_identity_verified=True)
                    finished = True
                elif record.get('kind') == 'native_error':
                    result.update(step=record.get('step'), code=record.get('code'))
                    finished = True
                elif record.get('kind') == 'diagnostic_probe':
                    result.setdefault('api_probes', []).append({key: value for key, value in record.items() if key != 'kind'})
                else:
                    raise RuntimeError('unexpected_diagnostic_frame')
            if capture.process.poll() is not None and not finished:
                raise RuntimeError('diagnostic_worker_exited')
        assert finished, 'diagnostic_timeout'
    except Exception as error:
        result.update(step=step, error_class=type(error).__name__)
    finally:
        core.tap.stop_unit(unit, True)
        if capture:
            capture.close()
        result['native_session_unchanged'] = pinned.host is not None and pinned.unchanged()
        pinned.close()
        if bundle:
            for name in core.FILES:
                (bundle / name).unlink(missing_ok=True)
            bundle.rmdir()
        flags, failures = core.tap.compare_baselines(baseline, network, business)
        result.update(flags)
        result['baseline_ok'] = not failures
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
