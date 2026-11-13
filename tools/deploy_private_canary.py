#!/usr/bin/env python3
"""One controlled private/text rollout; never restores Native or sends messages."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE = Path('/run/systemd/system/astrbot-wechat-forwarder.service.d/zzzz-private-canary.conf')
OVERRIDE_TEXT = '[Service]\nRestart=no\n'
LEGACY_OVERRIDE_TEXT = '[Unit]\nOnFailure=\n[Service]\nRestart=no\n'
ALERT_CONFIG = Path('/etc/systemd/system/astrbot-wechat-forwarder.service.d/alert.conf')
PAUSED_ALERT = ALERT_CONFIG.with_name('alert.conf.private-canary-paused')
PHASE = 'preflight'


def require(value):
    if not value:
        raise ValueError('private_canary_precondition')


def command(args, timeout=20):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    require(result.returncode == 0)
    return result.stdout


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def replace(path, raw, mode):
    temporary = path.with_name('.'+path.name+'.canary-'+secrets.token_hex(6))
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as output:
            output.write(raw); output.flush(); os.fsync(fd)
        os.fchmod(fd, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY|os.O_RDONLY|os.O_NOFOLLOW)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)


def check_runtime_paths(root):
    for path in (root/'.runtime', root/'.runtime/forwarder.json',
                 root/'.runtime/verification', root/'.runtime/worker-manifest.json'):
        require(not any(item.is_symlink() for item in (path,*path.parents)))


def check_alert_paths(source, paused):
    require(not (source.exists() and paused.exists()))
    for path in (source,paused):
        require(not any(item.is_symlink() for item in (path,*path.parents)))
        if path.exists():
            info = path.stat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1 and not info.st_mode & 0o022)
            lines = [line.strip() for line in path.read_text().splitlines()
                     if line.strip() and not line.lstrip().startswith('#')]
            require(lines==['[Unit]','OnFailure=astrbot-wechat-forwarder-alert.service'])


def pause_alert(source, paused):
    check_alert_paths(source,paused)
    if source.exists():
        # systemd dependencies cannot be reset by an empty drop-in assignment.
        # Preserve this exact project's file under a suffix systemd ignores.
        os.replace(source,paused)
        directory = os.open(source.parent,os.O_DIRECTORY|os.O_RDONLY|os.O_NOFOLLOW)
        try: os.fsync(directory)
        finally: os.close(directory)


def apply(expected):
    global PHASE
    PHASE='source'

    head = command(['/usr/bin/git','--no-optional-locks','-c','safe.directory='+str(ROOT),'-C',str(ROOT),'rev-parse','HEAD']).decode().strip()
    require(head == expected)
    require(not command(['/usr/bin/git','--no-optional-locks','-c','safe.directory='+str(ROOT),'-C',str(ROOT),
                         'status','--porcelain','--untracked-files=no']).strip())
    PHASE='paths'
    check_runtime_paths(ROOT)
    sync_tool = load('_private_rollout_sync',ROOT/'tools/sync_runtime.py')
    sync_tool.sync(False)  # Complete source/destination/stale-path preflight before writes.
    PHASE='service_configuration'
    formal = load('_private_rollout_formal', ROOT/'scripts/run_forwarder.py')
    core = formal.core
    current = core.tap.details(core.UNIT)
    require(current.get('ActiveState') in {'failed','inactive'} and current.get('MainPID') == '0')
    formal.load_config()
    updated = formal.configuration.validate({**core.SETTINGS, 'privateOnly':True,
        **{key:False for key in ('imageInputEnabled','imageOutputEnabled','voiceOutputEnabled',
                                'stickerInputEnabled','linkInputEnabled','groupContextEnabled')}})
    PHASE='account_queue_audits'
    inspection = load('_private_rollout_inspect', ROOT/'tools/inspect_private_canary.py').inspect(ROOT)
    require(inspection['native']['account_online'] and inspection['native']['unchanged']
            and inspection['native']['remaining_seconds'] > 900)
    require(not any(inspection['queue'].get(key) for key in ('queued','publishing','uncertain'))
            and not inspection['queue_has_unclassified_state'])
    require(all(not item['present'] or item['state']=='accepted' or item['cleared']
                for item in inspection['audits'].values()))
    # Full snapshot/admission proof remains mandatory in formal startup. Never
    # delete/rewrite an action reservation or disposition to make this pass.
    PHASE='owned_unit'
    require(formal.UNIT_FILE.is_file() and not formal.UNIT_FILE.is_symlink())
    info = formal.UNIT_FILE.stat()
    unit_raw = formal.UNIT_FILE.read_bytes()
    require(info.st_uid == 0 and not info.st_mode & 0o022 and
            b'# Managed only by astrbot-wechat' in unit_raw and
            os.fsencode(core.ENTRY) in unit_raw)
    require(not OVERRIDE.is_symlink())
    require(not OVERRIDE.exists() or OVERRIDE.read_text() in (OVERRIDE_TEXT,LEGACY_OVERRIDE_TEXT))
    check_alert_paths(ALERT_CONFIG,PAUSED_ALERT)
    require(not OVERRIDE.parent.is_symlink())
    if OVERRIDE.parent.exists():
        info = OVERRIDE.parent.stat()
        require(info.st_uid == 0 and not info.st_mode & 0o022)
    PHASE='backup'
    backup = core.owner.STATE/('private-canary-'+time.strftime('%Y%m%d-%H%M%S',time.gmtime())+'-'+secrets.token_hex(3))
    core.owner.private_directory(backup)
    core.owner.write_json(backup/'metadata.json', {'sourceCommit':head,'createdAt':int(time.time()),'privateOnly':True})
    replace(backup/'forwarder.json',formal.CONFIG.read_bytes(),0o600)
    replace(backup/'forwarder.service',unit_raw,0o600)
    # Configuration is closed to groups before any new process can start.
    PHASE='configure'
    core.owner.write_json(formal.CONFIG,updated)
    PHASE='code_view'
    synced = sync_tool.sync(True)
    require(synced['ok'])
    PHASE='unit_configuration'
    replace(formal.UNIT_FILE,formal.unit_text().encode(),0o644)
    OVERRIDE.parent.mkdir(mode=0o755,exist_ok=True)
    replace(OVERRIDE,OVERRIDE_TEXT.encode(),0o644)
    pause_alert(ALERT_CONFIG,PAUSED_ALERT)
    command(['/usr/bin/systemctl','daemon-reload'])
    PHASE='effective_unit'
    require(command(['/usr/bin/systemctl','show','--value','-p','OnFailure',core.UNIT]).strip()==b'')
    require(command(['/usr/bin/systemctl','show','--value','-p','Restart',core.UNIT]).strip()==b'no')
    PHASE='start'
    command(['/usr/bin/systemctl','reset-failed',core.UNIT])
    command(['/usr/bin/systemctl','start',core.UNIT],10)
    print(json.dumps({'stage':'private_canary_started','ok':True,'sourceCommit':head,
                      'privateOnly':True,'mediaEnabled':False,'automaticRetry':False,
                      'backup':str(backup),'runtimeFiles':synced['files']}),flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',required=True)
    parser.add_argument('--expected-commit',required=True)
    args = parser.parse_args()
    if os.geteuid()!=0:
        print(json.dumps({'stage':'private_canary_deploy','ok':False,'code':'linux_root_required'})); return 1
    try:
        return apply(args.expected_commit)
    except Exception:
        # Keep privateOnly/config/audits and failure evidence. No rollback or
        # automatic restart can accidentally reopen group forwarding.
        print(json.dumps({'stage':'private_canary_deploy','ok':False,'code':'deployment_requires_review','phase':PHASE})); return 1


if __name__=='__main__':
    raise SystemExit(main())
