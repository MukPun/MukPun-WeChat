#!/usr/bin/env python3
"""Read current health and systemd lifecycle evidence without chat contents."""
import importlib.util
import json
import os
from pathlib import Path
import resource
import subprocess
import time

assert os.geteuid() == 0
resource.setrlimit(resource.RLIMIT_AS,(134217728,134217728))
resource.setrlimit(resource.RLIMIT_CPU,(8,8))
root=Path(__file__).resolve().parent
s=importlib.util.spec_from_file_location('_outage_health',root/'run_forwarder.py')
f=importlib.util.module_from_spec(s);s.loader.exec_module(f)
c=f.core
v=c.owner.read_json(c.STATUS)
keys=('active','ready','code','updated','new_qr','sse','astrbot_connected','received','forwarded','completed_replies',
      'uncertain','queue','resources','routes','onebot_link','all_groups_enabled')
out={'health':{k:v.get(k) for k in keys},'status_age_seconds':int(time.time())-v.get('updated',0)}
p=c.groups.PinnedNative();p.open()
try:
    base=Path('/sys/fs/cgroup')/p.host.parent
    out['native_parent_memory_events']=dict(line.split() for line in (base/'memory.events').read_text().splitlines())
    out['native_unchanged']=p.unchanged()
finally:p.close()
run=subprocess.run(['/usr/bin/journalctl','--no-pager','-o','json','-n','160',
    '--since=2026-10-28 04:25:00 UTC','--until=2026-10-28 04:30:00 UTC',
    '-u','astrbot-wechat-forwarder.service','-u','astrbot-wechat-group-read-*.service',
    '-u','astrbot-wechat-private-host-*.service'],capture_output=True,timeout=5)
assert len(run.stdout)<2*1024*1024
events=[]
for line in run.stdout.splitlines():
    item=json.loads(line)
    msg=item.get('MESSAGE','')
    if item.get('_COMM')=='systemd' and any(x in msg for x in ('Main process exited','Failed with result','oom','OOM','restart counter','CPU time')):
        events.append({'at':item.get('__REALTIME_TIMESTAMP'),'unit':item.get('UNIT'),
                       'manager_message':msg[:350]})
out['lifecycle']=events
print(json.dumps(out,sort_keys=True))
