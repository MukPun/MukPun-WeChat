#!/usr/bin/env python3
"""Restart the reviewed core only; never restart or initialize Native."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

assert os.geteuid()==0
root=Path(__file__).resolve().parent
s=importlib.util.spec_from_file_location('_restore_storage_core',root/'run_forwarder.py')
f=importlib.util.module_from_spec(s);sys.modules[s.name]=f;s.loader.exec_module(f)
c=f.core
p=c.groups.PinnedNative();p.open()
try:
    v=c.owner.read_json(c.STATUS)
    assert v.get('uncertain')==0 and not any(v.get('queue',{}).get(k) for k in ('queued','publishing','uncertain'))
    for name,allowed in (('forward-send.json',{'accepted'}),('forward-image-send.json',{'accepted','rejected'})):
        path=c.owner.STATE/name
        if path.exists():assert c.owner.read_json(path).get('state') in allowed
    assert p.unchanged()
    subprocess.run(['/usr/bin/systemctl','reset-failed',c.UNIT],check=True,capture_output=True,timeout=5)
    subprocess.run(['/usr/bin/python3','-I','-B',str(root/'run_forwarder.py'),'start'],check=True,capture_output=True,timeout=20)
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        v=c.owner.read_json(c.STATUS)
        if v.get('ready') is True and time.time()-v.get('updated',0)<10:break
        time.sleep(1)
    ok=v.get('ready') is True and v.get('uncertain')==0 and p.unchanged()
    print(json.dumps({'stage':'storage_core_restored','ok':ok,'ready':v.get('ready'),'code':v.get('code'),
        'uncertain':v.get('uncertain'),'native_unchanged':p.unchanged(),'new_qr':v.get('new_qr')}),flush=True)
    raise SystemExit(0 if ok else 1)
finally:p.close()
