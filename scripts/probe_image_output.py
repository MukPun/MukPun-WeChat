#!/usr/bin/env python3
"""One fixed, separately approved image per scope; no model or fresh login."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
IMAGE_SHA256 = '264a52467f2becaec18c7373782130a967d33651ee981f2bcd7376f879fb37e2'


def main():
    if os.geteuid() != 0 or sys.argv[1:] not in (['private'], ['group']):
        return 2
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    scope = sys.argv[1]
    spec = importlib.util.spec_from_file_location('_image_probe_formal', ROOT / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    formal.load_config()
    core = formal.core
    raw = (ROOT / 'image-output-test.png').read_bytes()
    core.require(hashlib.sha256(raw).hexdigest() == IMAGE_SHA256, 'fixed_image_changed')
    image = core.output_images.payload(raw, 'image/png')
    attempt_path = core.owner.STATE / ('image-output-poc-' + scope + '.json')
    core.require(not attempt_path.exists(), 'image_poc_already_attempted')
    core.require(core.owner.owner_active(), 'existing_session_required')
    before = core.owner.read_json(core.STATUS)
    core.require(before.get('ready') is True and before.get('uncertain') == 0
        and not before.get('image_egress_active')
        and not any(before.get('queue', {}).get(k) for k in ('queued', 'publishing', 'uncertain')), 'bridge_not_quiet')
    native = core.groups.PinnedNative()
    native.open()
    host = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    _, binding = core.groups.load_send_binding(native, formal.core.SETTINGS['groupName'])
    unit = core.groups.READER_PREFIX + secrets.token_hex(6) + '.service'
    bundle = capture = lease = None
    lock = None
    submitted = False
    restored = False
    outcome = None
    audit = None
    stopped = False
    success = False
    try:
        formal.command(['/usr/bin/systemctl', 'stop', core.UNIT])
        stopped = True
        formal.stopped_receipt()
        lock = os.open(core.owner.STATE / 'bridge.lock', os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bundle = Path(tempfile.mkdtemp(prefix='astrbot-wechat-image-output.', dir='/var/tmp'))
        for name in core.FILES:
            core.groups.legacy.write_public(bundle, name, core.groups.legacy.source_text(ROOT / name))
        os.chmod(bundle, 0o755)
        seconds = 90
        core.require(native.remaining_seconds() > 150, 'session_expiring')
        config = {'account': 'wechat-primary', 'token': native.token(), 'runtimeSeconds': seconds,
            'privateName': core.SETTINGS['privateContact'], 'groupName': core.SETTINGS['groupName'],
            'roomId': binding['roomId'], 'groupPrefix': core.SETTINGS['groupPrefix'],
            'sendInterval': core.SETTINGS['sendIntervalSeconds'], 'groupWakeMode': core.SETTINGS['groupWakeMode'],
            'selfProof': core.self_proof(), 'imageInput': False, 'groupContext': False, 'imageOutput': True}
        args = ['/usr/bin/systemd-run', '--system', '--quiet', '--collect', '--wait', '--pipe', '--unit=' + unit,
            *core.groups.reader_properties(bundle, 'awio-' + secrets.token_hex(4), native.host.parent, native.netfd, seconds),
            '/usr/bin/python3', '-I', '-B', '/opt/forward_native_worker.py']
        capture = core.Duplex(args, config)
        del config
        lease = core.media_egress.Lease(core, native)
        request = None
        isolated = False
        end = time.monotonic() + 85
        audit_path = core.owner.STATE / 'forward-image-send.json'
        while time.monotonic() < end and outcome is None:
            core.require(native.unchanged(), 'native_changed')
            for record in capture.tick():
                kind = record.get('kind')
                if kind == 'isolated':
                    core.require(record.get('ok') is True and all(record.get('checks', {}).values()), 'worker_isolation')
                    core.groups.verify_reader(unit, native, 'forward_native_worker.py')
                    isolated = True
                    capture.acknowledge()
                elif kind == 'native_ready':
                    core.require(isolated and record.get('selfSenderId'), 'worker_readiness')
                    destination = record['peer'] if scope == 'private' else binding['roomId']
                    request = {'kind': 'send_image', 'requestId': secrets.token_hex(16),
                        'key': secrets.token_hex(16), 'destination': destination, 'image': image}
                    capture.send({'kind': 'start', 'after': record['boundary']})
                elif kind == 'native_armed':
                    core.require(request is not None and core.reserve_image_upload(native, image), 'image_budget_exhausted')
                    audit = core.image_audit_value(request)
                    core.owner.write_json(attempt_path, {**audit, 'scope': scope})
                    core.owner.write_json(audit_path, audit)
                    submitted = True
                    capture.send(request)
                elif kind == 'image_send_intent':
                    core.require(request is not None and record.get('requestId') == request['requestId']
                        and core.output_images.job_id(record.get('jobId')), 'image_intent')
                    audit['jobId'] = record['jobId']
                    core.owner.write_json(audit_path, audit)
                elif kind in {'media_wait', 'media_done'}:
                    if kind == 'media_wait':
                        lease.wait(record['jobId'])
                    else:
                        lease.done(record['jobId'])
                elif kind in {'event', 'checkpoint'}:
                    # Only this temporary subscriber ACKs. The production
                    # durable cursor is unchanged and no model is contacted.
                    capture.send({'kind': 'ack', 'cursor': record['cursor']})
                elif kind == 'native_image_result':
                    core.require(record.get('requestId') == request['requestId'], 'image_result_binding')
                    receipt = record.get('receipt', {})
                    accepted = (record.get('ok') is True and record.get('server_confirmed') is True
                        and receipt.get('job_id') == audit.get('jobId')
                        and core.output_images.accepted(receipt, 'wechat-primary', request['destination'], IMAGE_SHA256))
                    unsubmitted = record.get('submitted') is False and record.get('server_confirmed') is False
                    audit.update(state='accepted' if accepted else 'rejected' if unsubmitted else 'unknown',
                                 code=record.get('code', 'image_response'))
                    core.owner.write_json(audit_path, audit)
                    core.owner.write_json(attempt_path, {**audit, 'scope': scope})
                    outcome = {'stage': 'image_output_poc', 'scope': scope, 'ok': accepted,
                        'code': audit['code'], 'state': audit['state'], 'bytes': image['bytes'],
                        'source_sha256': IMAGE_SHA256, 'server_confirmed': record.get('server_confirmed') is True,
                        'projection_verified': receipt.get('projection_verified') is True}
                    success = accepted
                elif kind == 'report':
                    core.require(record.get('stage') in {'sse_connection', 'loop_guard', 'unsupported_incoming'}, 'probe_report')
                else:
                    raise ValueError('probe_worker_failed')
            if capture.process.poll() is not None and outcome is None:
                raise ValueError('probe_worker_exited')
        core.require(outcome is not None, 'image_probe_timeout')
        core.owner.wire(outcome)
    finally:
        if lease:
            lease.close()
        core.tap.stop_unit(unit, True)
        if capture:
            capture.close()
        if bundle:
            for name in core.FILES:
                (bundle / name).unlink(missing_ok=True)
            bundle.rmdir()
        if lock is not None:
            os.close(lock)
        unchanged = native.unchanged()
        native.close()
        flags, failures = core.tap.compare_baselines(host, network, business)
        safe = not submitted or audit is not None and audit.get('state') in {'accepted', 'rejected'}
        if stopped and safe and unchanged and not failures:
            formal.command(['/usr/bin/systemctl', 'start', core.UNIT], 10)
            until = time.monotonic() + 35
            while time.monotonic() < until:
                state = core.owner.read_json(core.STATUS)
                if state.get('ready') is True:
                    restored = True
                    break
                time.sleep(.5)
        core.owner.wire({'stage': 'image_output_poc_cleanup', 'ok': unchanged and not failures,
            'native_session_unchanged': unchanged, 'bridge_restored': restored, **flags})
    return 0 if success and restored and not failures else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'stage': 'image_output_poc_failed', 'ok': False,
                          'code': getattr(error, 'stage', 'probe_failed')}))
        raise SystemExit(1)
