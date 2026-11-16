"""Shared helpers for the formal controller and the historical trial entry."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

HERE=Path(__file__).resolve().parent


def module(name):
    path=HERE/(name+'.py')
    if name=='bridge_ipc' and not path.exists():
        path=HERE.parent/'wechat_bridge/ipc.py'
    spec=importlib.util.spec_from_file_location('_forward_support_'+name,path)
    result=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=result
    spec.loader.exec_module(result)
    return result


groups=module('run_group_probe')
owner=module('session_runtime')
paths=module('project_paths')
ipc=module('bridge_ipc')
tap,owned,require=groups.tap,groups.owned,groups.require
wire=owner.wire


def fresh_status():
    old=owner.read_json(owner.CONTROL/'status.json')
    require(old.get('ready') is True and not old.get('busy'),'session_not_ready')
    sequence=old['sequence']+1
    owner.write_json(owner.CONTROL/'request.json',owner.request_value({
        'sequence':sequence,'action':'status','allowFreshLogin':False}))
    deadline=time.monotonic()+75
    while time.monotonic()<deadline:
        value=owner.read_json(owner.CONTROL/'status.json')
        if value.get('sequence')==sequence and value.get('done'):
            require(value.get('ok') is True and value.get('accounts')==1,'session_status_failed')
            return value
        time.sleep(.15)
    raise groups.legacy.Failure('session_status_timeout')


def ensure_session():
    restored=False
    if not owner.owner_active():
        previous=owner.read_json(owner.CONTROL/'status.json')
        require(previous.get('ok') is True and previous.get('accounts')==1
                and owner.cleanup_ok(previous.get('cleanup')),'saved_session_not_ready')
        owner.start_owner(False)
        restored=True
    end=time.monotonic()+60
    while True:
        value=fresh_status()
        if value.get('connected') is True:
            break
        require(time.monotonic()<end,'saved_connection_unavailable')
        time.sleep(3)
    wire({'stage':'bridge_session','ok':True,'restored':restored,'connected':True,'new_qr':False})


def read_onebot_token():
    value=json.loads(owner.read_private(paths.ASTRBOT_DATA/'cmd_config.json',4*1024*1024).decode('utf-8-sig'))
    selected=[item for item in value.get('platform',[]) if isinstance(item,dict) and item.get('type')=='aiocqhttp']
    require(len(selected)==1 and selected[0].get('enable') is True,'onebot_config')
    item=selected[0]
    require(str(item.get('ws_reverse_port'))=='6199','onebot_config')
    token=item.get('ws_reverse_token')
    require(isinstance(token,str) and 16<=len(token)<=1024 and all(33<=ord(c)<=126 for c in token),'onebot_token')
    return token


def host_properties(source,seconds, *, hd_images=False):
    values=[
        'Type=exec','DynamicUser=yes','PrivateNetwork=no','PrivateDevices=yes',
        'ProtectSystem=strict','ProtectHome=tmpfs','ProtectProc=invisible','NoNewPrivileges=yes',
        'CapabilityBoundingSet=','AmbientCapabilities=','ProtectKernelModules=yes',
        'ProtectKernelTunables=yes','ProtectControlGroups=yes','ProtectKernelLogs=yes',
        'RestrictNamespaces=yes','RestrictRealtime=yes','RestrictSUIDSGID=yes','LockPersonality=yes',
        'MemoryMax='+('512M' if hd_images else '128M'),'MemoryHigh='+('384M' if hd_images else '96M'),'MemorySwapMax=0','CPUQuota=10%','CPUQuotaPeriodSec=100ms',
        'TasksMax=8','LimitNOFILE=64','RuntimeMaxSec='+str(seconds+15),'TimeoutStopSec=5',
        'Restart=no','OOMPolicy=stop','KillMode=control-group','LimitCORE=0','LimitFSIZE='+('33554432' if hd_images else '8388608'),
        'Nice=19','IOSchedulingClass=idle','UMask=0077','RestrictAddressFamilies=AF_UNIX AF_INET',
        'StateDirectory=astrbot-wechat-bridge','StateDirectoryMode=0700',
        'BindReadOnlyPaths='+str(source)+':/opt/bridge','WorkingDirectory=/opt/bridge',
        'TemporaryFileSystem=/tmp:rw,size=4M,mode=1777 /var/tmp:rw,size=4M,mode=1777',
        'InaccessiblePaths=-/run/docker.sock -/run/docker -/run/containerd -/var/lib/docker '
        '-/var/lib/containerd -/run/frp -/run/frpc -/run/frps -/run/user -/run/dbus '
        '-/run/systemd/private -/var/lib/astrbot-wechat-session -/run/astrbot-wechat-session',
        'UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT PYTHONPATH PYTHONHOME '
        'HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy',
    ]
    if hd_images:
        from_path=Path(__file__).resolve().parents[1].parent/'AstrBot/data/temp/wechat-hd-input'
        values.append('BindPaths='+str(from_path)+':/var/lib/astrbot-wechat-bridge/hd-input')
    return ['--property='+value for value in values]


class Duplex(groups.InputCapture):
    def __init__(self,args,config):
        super().__init__(args,config)
        self.pending=bytearray()
        os.set_blocking(self.process.stdin.fileno(),False)

    def send(self,value):
        raw=ipc.encode(value)
        require(len(self.pending)+len(raw)<=ipc.MAX_FRAME*2,'ipc_backpressure')
        self.pending.extend(raw)

    def acknowledge(self):
        self.pending.extend(b'AWG-READY\n')

    def tick(self):
        if self.pending:
            try:
                count=os.write(self.process.stdin.fileno(),self.pending[:4096])
                del self.pending[:count]
            except BlockingIOError:
                pass
        self.drain(.02)
        records=[]
        output=self.output['stdout']
        while b'\n' in output:
            line,_,rest=output.partition(b'\n')
            output[:]=rest
            records.append(ipc.decode(bytes(line)))
        require(len(output)<ipc.MAX_FRAME,'ipc_frame_bound')
        return records


def verify_host(unit,pinned,entry='bridge_host_worker.py', *, hd_images=False):
    info=tap.details(unit)
    pid=int(info.get('MainPID','0'))
    require(pid>1 and info.get('ActiveState')=='active' and info.get('ControlGroup')=='/system.slice/'+unit,'host_identity')
    before=tap.start_ticks(pid)
    require(entry in {'bridge_host_worker.py','forward_host_worker.py'} and
            groups.argv(pid)==[b'/usr/bin/python3',b'-I',b'-B',os.fsencode('/opt/bridge/scripts/'+entry)],'host_identity')
    status=owned.parse_status(owned.read(Path('/proc')/str(pid)/'status'))
    uids=status.get('Uid','').split()
    require(len(uids)==4 and len(set(uids))==1 and int(uids[0])>0
            and int(uids[0])!=pinned.host.client_uid and owned.zero_caps(status)
            and status.get('NoNewPrivs','').strip()=='1','host_identity')
    require(owned.identity('/proc/'+str(pid)+'/ns/net')==owned.identity('/proc/self/ns/net'),'host_network_identity')
    tap.cgroup_limits(unit,{'memory.max':'536870912' if hd_images else '134217728','memory.high':'402653184' if hd_images else '100663296','memory.swap.max':'0',
                            'cpu.max':'10000 100000','pids.max':'8'})
    require(tap.start_ticks(pid)==before,'host_identity')
