"""GET-only recovery of one image job that provably never reached WeChat.

Called under bridge.lock before workers start. Never creates a Native send,
changes its lifecycle, or clears an unrelated unknown outcome.

The gap this closes: a bridge that wrote ``state='submitted'`` and then died
before the Native job settled used to stop every later start with
``previous_image_send_requires_review`` - even when the job itself proves that
zero bytes left this host. The runtime already knew how to read that proof
(``image_send_native.Client._known_failure``), but the startup gate never asked
the same question, because that check only ran while a Native client was alive.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
HEX32 = re.compile(r'[0-9a-f]{32}\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
REVIEW = 'native_job_proved_before_submission'
REVIEWED = ('state', 'review', 'recoveryVerification', 'recoveryDisposition')
# Phases the pinned SDK reaches before it hands a body to WeChat. "submitting"
# and "readback" are deliberately absent: past that point the bridge may not
# claim the image was undelivered.
EARLY = {'preparing', 'uploading'}


def companion(name, path):
    spec = importlib.util.spec_from_file_location('_image_recovery_' + name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


native = companion('image_send_native', HERE / 'image_send_native.py')
output_images = native.images


def require(value):
    if not value:
        raise ValueError('image_recovery_unverified')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def subject(audit):
    """The immutable facts an operator or this module may never rewrite."""
    return encoded({key: value for key, value in audit.items() if key not in REVIEWED})


def destination_matches(audit, job):
    """The audit stores only the digest; never log or compare the raw peer."""
    peer = job.get('threadId')
    return (isinstance(peer, str) and 0 < len(peer.encode('utf-8')) <= 256
            and hashlib.sha256(peer.encode()).hexdigest() == audit['destinationSha256'])


def known_failure(audit, job):
    """Every condition must hold; one miss leaves the audit for a human."""
    if not isinstance(job, dict):
        return False
    state, phase, result = job.get('state'), job.get('phase'), job.get('result')
    if state == 'failed':
        phases = {'preparing', 'uploading', 'readback'}
    elif state == 'needs_review':
        phases = EARLY
    else:
        return False
    return (job.get('id') == audit['jobId'] and job.get('accountId') == 'wechat-primary'
            and job.get('kind') == 'send' and phase in phases
            and destination_matches(audit, job)
            and job.get('serverMessageId') in (None, '')
            and isinstance(result, dict) and result.get('nativeReceipt') is None
            and not result.get('messageIds')
            # A byte that crossed the socket is history the bridge cannot
            # recall; only a job that sent nothing is a proved non-delivery.
            and type(job.get('completedBytes')) is int and job['completedBytes'] == 0)


def read_job(pinned, audit):
    """The token crosses an anonymous stdin pipe, never argv, env or a file."""
    value = subprocess.run(['/usr/bin/nsenter', '--net=/proc/' + str(pinned.host.client_pid) + '/ns/net',
        '--', '/usr/bin/python3', '-I', '-B', str(Path(__file__).resolve()), '--read'],
        input=json.dumps({'token': pinned.token(), 'audit': audit}).encode(),
        capture_output=True, timeout=22)
    try:
        answer = json.loads(value.stdout)
    except ValueError:
        return None
    return answer.get('job') if value.returncode == 0 and isinstance(answer, dict) else None


def workers_stopped(core):
    value = subprocess.run(['/usr/bin/systemctl', 'list-units', '--all', '--type=service',
        '--plain', '--no-legend', '--no-pager'], capture_output=True, text=True, timeout=10)
    if value.returncode:
        return False
    for line in value.stdout.splitlines():
        name = line.split()[0] if line.split() else ''
        if not any(re.fullmatch(re.escape(prefix) + r'[0-9a-f]{12}\.service', name)
                   for prefix in ('astrbot-wechat-private-host-', core.groups.READER_PREFIX)):
            continue
        code, state = core.tap.load_state(name)
        if state.get('LoadState') == 'not-found':
            continue
        if (code or state.get('ActiveState') not in ('inactive', 'failed')
                or state.get('MainPID') != '0'):
            return False
        group = state.get('ControlGroup')
        if group:
            if not group.startswith('/') or '..' in Path(group).parts:
                return False
            procs = Path('/sys/fs/cgroup') / group.lstrip('/') / 'cgroup.procs'
            if procs.exists() and procs.read_text().strip():
                return False
    return True


def verify(audit, job, *, at):
    """The marker this module writes, and the only shape it will accept back."""
    return {'classification': 'reviewed_not_delivered', 'jobState': job['state'],
            'jobPhase': job['phase'], 'completedBytes': job['completedBytes'],
            'auditSha256': hashlib.sha256(subject(audit)).hexdigest(), 'verifiedAt': at}


def frozen(audit, job):
    """What an operator needs to re-derive the decision without the live job."""
    return {'schemaVersion': 1, 'reviewedAt': int(time.time()),
            'audit': {key: audit[key] for key in
                      ('state', 'key', 'requestId', 'destinationSha256', 'sourceSha256',
                       'bytes', 'jobId')},
            'job': {key: job.get(key) for key in
                    ('id', 'state', 'phase', 'reason', 'completedBytes', 'serverMessageId',
                     'attempts', 'cancelRequested', 'retryable', 'totalBytes')},
            'nativeReceipt': None, 'messageIds': []}


def cleared(audit):
    """Terminal only for this module's own reviewed rejection, fully re-checked.

    A bare ``rejected`` label is not evidence, and neither is a marker somebody
    edited afterwards: every start re-derives the decision from the immutable
    facts, so a tampered marker reopens the gate instead of passing it.
    """
    if not isinstance(audit, dict):
        return False
    if audit.get('state') == 'accepted':
        return True
    if audit.get('state') != 'rejected' or audit.get('review') != REVIEW:
        return False
    marker = audit.get('recoveryVerification')
    return (isinstance(marker, dict)
            and set(marker) == {'classification', 'jobState', 'jobPhase', 'completedBytes',
                                'auditSha256', 'verifiedAt'}
            and marker['classification'] == 'reviewed_not_delivered'
            and marker['jobState'] in ('failed', 'needs_review')
            and marker['jobPhase'] in (EARLY | {'readback'})
            and type(marker['completedBytes']) is int and marker['completedBytes'] == 0
            and type(marker['verifiedAt']) is int and marker['verifiedAt'] > 0
            and isinstance(marker['auditSha256'], str) and HEX64.fullmatch(marker['auditSha256'])
            and marker['auditSha256'] == hashlib.sha256(subject(audit)).hexdigest())


def reconcile(core, audit_path, settings):
    """Prove the audited job never submitted; a miss leaves the gate closed."""
    pinned = None
    try:
        raw = audit_path.read_bytes()
        require(len(raw) <= 4096 and settings is not None)
        audit = json.loads(raw)
        if cleared(audit):
            # Already settled and still self-consistent: never re-ask Native.
            return True
        require(audit.get('state') == 'submitted')
        require(all(isinstance(audit.get(key), str) and HEX32.fullmatch(audit[key])
                    for key in ('key', 'requestId'))
                and output_images.job_id(audit.get('jobId'))
                and isinstance(audit.get('destinationSha256'), str)
                and HEX64.fullmatch(audit['destinationSha256'])
                and isinstance(audit.get('sourceSha256'), str) and HEX64.fullmatch(audit['sourceSha256'])
                and type(audit.get('bytes')) is int and 0 < audit['bytes'] <= 25 * 1024 * 1024
                and workers_stopped(core))
        pinned = core.groups.PinnedNative()
        pinned.open()
        job = read_job(pinned, audit)
        require(known_failure(audit, job) and pinned.unchanged() and audit_path.read_bytes() == raw)
        parent = core.owner.STATE / 'image-recovery'
        core.owner.private_directory(parent)
        target = parent / ('job-' + audit['jobId'][-12:])
        core.owner.write_json(target, frozen(audit, job))
        require(workers_stopped(core) and pinned.unchanged() and audit_path.read_bytes() == raw)
        settled = dict(audit)
        settled.update(state='rejected', review=REVIEW,
                       recoveryVerification=verify(audit, job, at=int(time.time())))
        core.owner.write_json(audit_path, settled)
        return True
    except Exception:
        return False
    finally:
        if pinned is not None:
            pinned.close()


def main():
    if sys.argv[1:] != ['--read']:
        return 2
    try:
        value = json.loads(sys.stdin.buffer.read(8193))
        require(set(value) == {'token', 'audit'} and isinstance(value['token'], str)
                and 1 <= len(value['token']) <= 4096)
        audit = value['audit']
        require(output_images.job_id(audit.get('jobId')))
        policy = native.Policy('wechat-primary', b'', {})
        path = policy.prefix + '/sends/' + audit['jobId']
        policy.gets.add(path)
        response = native.Transport(policy, value['token'], 26174).request('GET', path, timeout=8)
        require(response.status == 200)
        print(json.dumps({'job': json.loads(response.body)}, ensure_ascii=False), flush=True)
        return 0
    except Exception:
        print('{"job":null}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
