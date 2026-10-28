#!/usr/bin/env python3
"""Promote only four reviewed capacity-guard copies; preserve existing services."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

root=Path(__file__).resolve().parent
assert os.geteuid()==0
stage=root/'image-budget-staging'
names={'run_private_forwarder.py','image_output_budget.py',
       'verification/scripts/run_private_forwarder.py','verification/scripts/image_output_budget.py'}
manifest=json.loads((stage/'manifest.json').read_text())
assert set(manifest)==names
spec=importlib.util.spec_from_file_location('_budget_deploy',root/'run_forwarder.py')
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
c=f.core
v=c.owner.read_json(c.STATUS)
assert v.get('ready') is True and v.get('uncertain')==0 and not v.get('image_egress_active')
assert not any(v.get('queue',{}).get(k) for k in ('queued','publishing','uncertain'))
p=c.groups.PinnedNative();p.open()
host={k:c.owned.identity('/proc/self/ns/'+k) for k in ('net','user')}
network,business=c.tap.read_network(),c.tap.business_snapshot()
config=(root.parent/'forwarder.json').read_bytes()
backup=root/('image-budget-backup-'+str(int(time.time())))
backup.mkdir(mode=0o700)
try:
    for n in names:
        raw=(stage/n).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==manifest[n]
        ast.parse(raw)
        dest=backup/n;dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(root/n,dest)
    def run(action):
        subprocess.run(['/usr/bin/python3','-I','-B',str(root/'run_forwarder.py'),action],check=True,capture_output=True,timeout=40)
    run('stop')
    v=c.owner.read_json(c.STATUS)
    assert v.get('cleanup_ok') is True and v.get('native_session_unchanged') is True and v.get('uncertain')==0
    for n in names:
        target=root/n
        info=target.stat()
        temp=target.with_name(target.name+'.budget-next')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,info.st_mode & 0o777)
        with os.fdopen(fd,'wb') as stream:
            stream.write((stage/n).read_bytes());stream.flush();os.fsync(stream.fileno())
        os.chown(temp,info.st_uid,info.st_gid);os.replace(temp,target)
    run('start')
    end=time.monotonic()+30
    while time.monotonic()<end:
        v=c.owner.read_json(c.STATUS)
        if v.get('ready') is True:break
        time.sleep(.5)
    flags,failures=c.tap.compare_baselines(host,network,business)
    assert v.get('ready') is True and v.get('uncertain')==0 and p.unchanged() and not failures
    assert (root.parent/'forwarder.json').read_bytes()==config
    assert all(hashlib.sha256((root/n).read_bytes()).hexdigest()==manifest[n] for n in names)
    print(json.dumps({'stage':'image_budget_guard_deployed','ready':True,'native_unchanged':True,
                      'configuration_unchanged':True,'files_verified':4,**flags}))
finally:p.close()
