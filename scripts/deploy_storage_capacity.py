#!/usr/bin/env python3
"""Instance-specific, bounded storage release with no fresh-login fallback.

Only reviewed code paths and this project's services are changed. Root-private
code/snapshot backups are retained; no message or media is sent by this tool.
"""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parent
STEP='preflight'


def mark(stage):
    global STEP
    STEP=stage
    print(json.dumps({'stage':'storage_release_progress','step':stage}),flush=True)


def load(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);sys.modules[name]=value;spec.loader.exec_module(value)
    return value


def run(script, action, timeout=100):
    result=subprocess.run(['/usr/bin/python3','-I','-B',str(ROOT/script),action],
        capture_output=True,timeout=timeout)
    if result.returncode:raise ValueError('owned_command_failed')


def healthy(c):
    value=c.owner.read_json(c.STATUS)
    if not(value.get('active') is True and value.get('ready') is True
        and value.get('uncertain')==0 and not value.get('outbound_pending')
        and not value.get('image_egress_active')
        and not any(value.get('queue',{}).get(k) for k in ('queued','publishing','uncertain'))):
        raise ValueError('core_not_quiescent')
    for name,allowed in (('forward-send.json',{'accepted'}),('forward-image-send.json',{'accepted','rejected'})):
        path=c.owner.STATE/name
        if path.exists() and c.owner.read_json(path).get('state') not in allowed:
            raise ValueError('send_requires_review')
    return value


def replace_file(target, source, previous=None):
    info=previous or (target.stat() if target.exists() else None)
    temporary=target.with_name('.'+target.name+'.storage-next')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
    try:
        with os.fdopen(fd,'wb',closefd=False) as stream:
            with source.open('rb') as src:shutil.copyfileobj(src,stream,65536)
            stream.flush();os.fsync(fd)
        if info:
            os.fchmod(fd,stat.S_IMODE(info.st_mode));os.fchown(fd,info.st_uid,info.st_gid)
        os.replace(temporary,target)
    finally:
        os.close(fd);temporary.unlink(missing_ok=True)


def key_fingerprints(pinned):
    # These three immutable key files are bounded; no database freeze is needed.
    result={}
    for name in ('api-token','storage-key','cursor-key'):
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=pinned.host.data_fd)
        try:
            info=os.fstat(fd)
            assert stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==pinned.host.client_uid
            assert info.st_dev==pinned.host.data_dev and 0<info.st_size<=8192
            result[name]=hashlib.sha256(os.read(fd,8193)).digest()
        finally:os.close(fd)
    return result


def main():
    global STEP
    assert os.geteuid()==0 and sys.argv[1:]==['--apply']
    c=load('_storage_old_core',ROOT/'run_forwarder.py').core
    stage=ROOT/'context-staging'
    prepared=load('_storage_prepared',ROOT/'prepare_storage_release.py')
    manifest=json.loads((stage/'storage-manifest.json').read_text())
    assert set(manifest)==set(prepared.mapping())
    for target,item in manifest.items():
        assert item['source']==prepared.mapping()[target]
        raw=(stage/item['source']).read_bytes();ast.parse(raw)
        assert hashlib.sha256(raw).hexdigest()==item['sha256']
        old=ROOT/target
        assert (hashlib.sha256(old.read_bytes()).hexdigest() if old.exists() else None)==item['previous']
        assert not old.is_symlink()
    available=int(dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())['MemAvailable'].split()[0])*1024
    assert available>=768*1024*1024 and shutil.disk_usage(c.owner.STATE).free>=1024*1024*1024
    baseline={k:c.owned.identity('/proc/self/ns/'+k) for k in ('net','user')}
    network,business=c.tap.read_network(),c.tap.business_snapshot()
    config=(ROOT.parent/'forwarder.json').read_bytes()
    pinned=c.groups.PinnedNative();pinned.open()
    fingerprints=key_fingerprints(pinned)
    pinned.close()
    first=healthy(c)
    # Permit already-triggered model work to settle before the handoff.
    end=time.monotonic()+120;quiet=time.monotonic()
    key=lambda v:tuple(v.get(k) for k in ('received','forwarded','accepted_parts','completed_replies','image_output_requests'))
    previous=key(first)
    while time.monotonic()-quiet<20:
        if time.monotonic()>=end:raise ValueError('traffic_did_not_settle')
        time.sleep(2);value=healthy(c)
        if key(value)!=previous:quiet=time.monotonic();previous=key(value)
    c.owner.private_directory(c.owner.STATE/'releases')
    backup=c.owner.STATE/'releases'/('storage128-'+str(int(time.time())))
    c.owner.private_directory(backup)
    changed={n:item for n,item in manifest.items() if item['sha256']!=item['previous']}
    for name in changed:
        path=ROOT/name
        if path.exists():
            dest=backup/'code'/name;dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            shutil.copy2(path,dest)
    c.owner.write_json(backup/'manifest.json',manifest)
    stopped=promoted=owner_changed=bridge_started=False
    try:
        mark('bridge_stop')
        run('run_forwarder.py','stop',40);stopped=True
        final=c.owner.read_json(c.STATUS)
        assert final.get('cleanup_ok') is True and final.get('native_session_unchanged') is True and final.get('uncertain')==0
        assert not any(final.get('queue',{}).get(k) for k in ('queued','publishing','uncertain'))
        mark('session_save')
        run('session_runtime.py','stop',100);owner_changed=True
        assert not c.owner.owner_active()
        saved=c.owner.read_json(c.owner.CONTROL/'status.json')
        assert saved.get('ok') is True and c.owner.cleanup_ok(saved.get('cleanup'))
        private=c.owner.STATE/'checkpoints/private'
        for name in ('current.zip','previous.zip'):
            if (private/name).exists():shutil.copy2(private/name,backup/name)
        mark('code_install')
        for name,item in changed.items():
            # Detect concurrent source edits again immediately before replacement.
            path=ROOT/name
            assert (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None)==item['previous']
        promoted=True
        for name,item in changed.items():replace_file(ROOT/name,stage/item['source'])
        mark('session_restore')
        run('session_runtime.py','start-long',110)
        new=load('_storage_new_core',ROOT/'run_forwarder.py').core
        session_status=new.owner.read_json(new.owner.CONTROL/'status.json')
        if not session_status.get('connected'):
            # Reconnect only the preserved account; resume has no fresh QR path.
            run('session_runtime.py','resume',390)
        restored=new.groups.PinnedNative();restored.open()
        try:
            current=key_fingerprints(restored)
            assert current==fingerprints
            fs=os.fstatvfs(restored.host.data_fd)
            assert fs.f_blocks*fs.f_frsize==128*1024*1024
            native_parent=Path('/sys/fs/cgroup')/restored.host.parent
            assert (native_parent/'memory.max').read_text().strip()==str(384*1024*1024)
        finally:restored.close()
        mark('bridge_start')
        started_after=int(time.time())
        subprocess.run(['/usr/bin/systemctl','reset-failed',new.UNIT],check=True,capture_output=True,timeout=5)
        run('run_forwarder.py','start',20);bridge_started=True
        end=time.monotonic()+90
        while time.monotonic()<end:
            value=new.owner.read_json(new.STATUS)
            fresh=value.get('updated',0)>=started_after
            if fresh and value.get('ready') is True:break
            if fresh and value.get('active') is False and value.get('code')!='starting':raise ValueError('new_bridge_failed')
            time.sleep(1)
        assert fresh and value.get('ready') is True and value.get('uncertain')==0 and not value.get('new_qr')
        flags,failures=c.tap.compare_baselines(baseline,network,business)
        assert not failures and (ROOT.parent/'forwarder.json').read_bytes()==config
        assert all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==item['sha256'] for n,item in manifest.items())
        result={'stage':'storage_capacity_deployed','ok':True,'source_max_bytes':8*1024*1024,
            'storage_bytes':128*1024*1024,'native_parent_memory_max':384*1024*1024,
            'ready':True,'new_qr':False,'session_keys_retained':True,'configuration_unchanged':True,
            'files_verified':len(manifest),**flags}
        c.owner.write_json(backup/'result.json',result);print(json.dumps(result),flush=True)
        return 0
    except Exception:
        try:
            c.owner.write_json(backup/'failed-forwarder-state.json',c.owner.read_json(c.STATUS))
        except Exception:
            pass
        rollback='not_needed'
        if stopped:
            try:
                if bridge_started:
                    run('run_forwarder.py','stop',40)
                    state=c.owner.read_json(c.STATUS)
                    assert state.get('uncertain')==0 and state.get('cleanup_ok') is True
                if owner_changed and c.owner.owner_active():run('session_runtime.py','stop',100)
                # Never discard newer saved messages just to fit an old sandbox.
                if owner_changed:
                    with zipfile.ZipFile(c.owner.STATE/'checkpoints/private/current.zip') as z:
                        assert sum(i.file_size for i in z.infolist())<30*1024*1024
                if promoted:
                    for name,item in changed.items():
                        old=backup/'code'/name
                        if old.exists():replace_file(ROOT/name,old)
                        elif item['previous'] is None:(ROOT/name).unlink(missing_ok=True)
                if owner_changed:
                    run('session_runtime.py','start-long',110)
                    if not c.owner.read_json(c.owner.CONTROL/'status.json').get('connected'):
                        run('session_runtime.py','resume',390)
                run('run_forwarder.py','start',20)
                rollback='core_restart_requested'
            except Exception:rollback='requires_review'
        result={'stage':'storage_capacity_deploy_failed','ok':False,'step':STEP,'rollback':rollback}
        c.owner.write_json(backup/'result.json',result);print(json.dumps(result),flush=True)
        return 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print(json.dumps({'stage':'storage_capacity_preflight_failed','ok':False,'step':STEP}),flush=True)
        raise SystemExit(1)
