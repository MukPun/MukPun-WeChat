#!/usr/bin/env python3
"""Bounded switch diagnostics; never print contact IDs, tokens or messages."""
import json
from pathlib import Path
import os

assert os.geteuid()==0
base=Path('/var/lib/astrbot-wechat-session')
control=Path('/run/astrbot-wechat-session')
keys=('updated','active','ready','code','uncertain','queue','cleanup_ok','native_session_unchanged',
      'accounts','connected','done','busy','ok','cacheMaintenance','self_identity_verified',
      'image_egress_active','outbound_pending','resources')
def safe(path):
    try:
        value=json.loads(path.read_text())
        return {k:value[k] for k in keys if k in value}
    except Exception:return {'unreadable':True}
result={'forwarder':safe(control/'forwarder.json'),'owner':safe(control/'status.json')}
paths=sorted((base/'releases').glob('storage128-*'))[-2:]
result['releases']=[{'began_at':int(p.name.rsplit('-',1)[1]),'result':safe(p/'result.json'),
                     'failed':safe(p/'failed-forwarder-state.json')} for p in paths]
print(json.dumps(result,sort_keys=True))
