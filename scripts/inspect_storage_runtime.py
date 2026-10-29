#!/usr/bin/env python3
"""Read-only live capacity acceptance; no message bodies or key material."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import sys


def main():
    assert os.geteuid()==0
    root=Path(__file__).resolve().parent
    spec=importlib.util.spec_from_file_location('_storage_inspection',root/'run_forwarder.py')
    formal=importlib.util.module_from_spec(spec);sys.modules[spec.name]=formal;spec.loader.exec_module(formal)
    c=formal.core
    p=c.groups.PinnedNative();p.open()
    try:
        state=c.owner.read_json(c.STATUS)
        session=c.owner.read_json(c.owner.CONTROL/'status.json')
        manifest=json.loads((root/'context-staging/storage-manifest.json').read_text())
        checks={'ready':all(state.get(k) is True for k in ('active','ready','sse','astrbot_connected')),
            'no_uncertain':state.get('uncertain')==0,'no_qr':state.get('new_qr') is False,
            'queue_drained':not any(state.get('queue',{}).get(k) for k in ('queued','publishing','uncertain')),
            'code_verified':all(hashlib.sha256((root/n).read_bytes()).hexdigest()==v['sha256'] for n,v in manifest.items()),
            'source_limit':c.output_images.MAX_IMAGE==8*1024*1024,
            'session_ready':session.get('ready') is True and session.get('accounts')==1 and session.get('connected') is True}
        groups={'native_parent':'/'+p.host.parent,'native_sdk':'/'+p.host.parent+'/'+p.host.unit,
                'controller':'/system.slice/'+c.UNIT,'owner':'/system.slice/'+c.owner.OWNER}
        for prefix,label in (('astrbot-wechat-private-host-','onebot'),(c.groups.READER_PREFIX,'native_reader')):
            result=c.tap.command(['/usr/bin/systemctl','list-units','--plain','--no-legend','--state=active',prefix+'*.service'])
            if isinstance(result,bytes):result=result.decode()
            # tap.command returns stdout text in the current helper contract.
            names=[line.split()[0] for line in result.splitlines() if line.split()]
            assert len(names)==1 and re.fullmatch(re.escape(prefix)+r'[0-9a-f]{12}\.service',names[0])
            groups[label]=c.tap.details(names[0])['ControlGroup']
        caps={}
        for label,group in groups.items():
            base=Path('/sys/fs/cgroup')/group.lstrip('/')
            values={k:(base/k).read_text().strip() for k in ('memory.max','memory.high','memory.current','memory.swap.max','pids.max','cpu.max')}
            values['oom_kills']=int(dict(line.split() for line in (base/'memory.events').read_text().splitlines())['oom_kill'])
            caps[label]=values
        expected={'native_parent':3072,'native_sdk':2560,'native_reader':96,'onebot':128,'controller':64,'owner':64}
        checks['memory_limits']=all(caps[k]['memory.max']==str(v*1024*1024) for k,v in expected.items())
        checks['no_oom']=all(v['oom_kills']==0 for v in caps.values())
        checks['no_swap']=all(v['memory.swap.max']=='0' for v in caps.values())
        fs=os.fstatvfs(p.host.data_fd)
        storage={'total_bytes':fs.f_blocks*fs.f_frsize,'free_bytes':fs.f_bavail*fs.f_frsize,
                 'file_limit':min(resource.prlimit(p.host.client_pid,resource.RLIMIT_FSIZE))}
        checks['storage_limits']=storage['total_bytes']==2048*1024*1024 and storage['file_limit']==768*1024*1024
        checks['media_headroom']=c.output_budget.allow_input(**c.media_headroom(p))
        print(json.dumps({'stage':'storage_capacity_acceptance','ok':all(checks.values()),'checks':checks,
            'storage':storage,'cgroups':caps,'cache_maintenance':session.get('cacheMaintenance'),
            'traffic':{k:state.get(k) for k in ('received','forwarded','completed_replies','image_output_requests','image_output_sent','uncertain')},
            'budget':c.owner.read_json(c.owner.STATE/'image-output-budget.json')},sort_keys=True))
        return 0 if all(checks.values()) else 1
    finally:p.close()


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print(json.dumps({'stage':'storage_capacity_acceptance','ok':False,'code':'inspection_failed'}))
        raise SystemExit(1)
