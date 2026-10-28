#!/usr/bin/env python3
"""Read-only image budget and Native capacity; no source data or credentials."""
import importlib.util
import json
import os
import resource
from pathlib import Path

assert os.geteuid()==0
root=Path(__file__).resolve().parent
s=importlib.util.spec_from_file_location('_capacity',root/'run_forwarder.py')
f=importlib.util.module_from_spec(s);s.loader.exec_module(f)
c=f.core
p=c.groups.PinnedNative();p.open()
fds=[]
try:
    budget=c.owner.STATE/'image-output-budget.json'
    out={'budget':c.owner.read_json(budget) if budget.exists() else None}
    st=os.fstatvfs(p.host.data_fd)
    out['storage_free']=st.f_bavail*st.f_frsize
    memory=Path('/sys/fs/cgroup')/p.host.parent
    out['memory_free']=int((memory/'memory.max').read_text())-int((memory/'memory.current').read_text())
    out['file_size_limits']=[line.split()[3:5] for line in (Path('/proc')/str(p.host.client_pid)/'limits').read_text().splitlines() if line.startswith('Max file size')]
    out['guard_file_limit']=c.output_budget.FILE_LIMIT
    a=os.open('accounts',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=p.host.data_fd);fds.append(a)
    names=os.listdir(a);assert len(names)==1
    a2=os.open(names[0],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=a);fds.append(a2)
    a3=os.open('agent',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=a2);fds.append(a3)
    out['database_parts']={}
    for suffix in ('','-wal','-journal'):
        try:out['database_parts'][suffix or 'main']=os.stat('control.sqlite'+suffix,dir_fd=a3,follow_symlinks=False).st_size
        except FileNotFoundError:pass
    out['small_upload_allowed']=c.output_budget.reserve(out['budget'],65536,
        storage_free=out['storage_free'],memory_free=out['memory_free'],database_bytes=sum(out['database_parts'].values()),
        file_limit=min(resource.prlimit(p.host.client_pid,resource.RLIMIT_FSIZE))) is not None
    v=c.owner.read_json(c.STATUS)
    out['health']={k:v.get(k) for k in ('active','ready','sse','astrbot_connected','uncertain','new_qr','queue')}
    print(json.dumps(out,sort_keys=True))
finally:
    for fd in reversed(fds):os.close(fd)
    p.close()
