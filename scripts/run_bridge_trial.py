#!/usr/bin/env python3
"""Trusted relay between two isolated, separately credentialed IPC workers.

observe: one fresh marker, no AstrBot connection or Native submission.
connect: check both workers and real AstrBot; Native remains observe-only.
live: one /ai question in the verified group and at most one Native submission.
Never initializes, generates QR, changes an existing service, or replays sends.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import secrets
import stat
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent
TARGET='\u95f2\u804a-A'


def module(name):
    path=HERE/(name+'.py')
    if name=='bridge_ipc' and not path.exists():
        path=HERE.parent/'wechat_bridge/ipc.py'
    spec=importlib.util.spec_from_file_location('_bridge_trial_'+name,path)
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
NATIVE_FILES=groups.FILES+('bridge_native_worker.py','bridge_native_protocol.py','bridge_ipc.py')


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


def host_properties(source,seconds):
    values=[
        'Type=exec','DynamicUser=yes','PrivateNetwork=no','PrivateDevices=yes',
        'ProtectSystem=strict','ProtectHome=tmpfs','ProtectProc=invisible','NoNewPrivileges=yes',
        'CapabilityBoundingSet=','AmbientCapabilities=','ProtectKernelModules=yes',
        'ProtectKernelTunables=yes','ProtectControlGroups=yes','ProtectKernelLogs=yes',
        'RestrictNamespaces=yes','RestrictRealtime=yes','RestrictSUIDSGID=yes','LockPersonality=yes',
        'MemoryMax=128M','MemoryHigh=96M','MemorySwapMax=0','CPUQuota=10%','CPUQuotaPeriodSec=100ms',
        'TasksMax=8','LimitNOFILE=64','RuntimeMaxSec='+str(seconds+15),'TimeoutStopSec=5',
        'Restart=no','OOMPolicy=stop','KillMode=control-group','LimitCORE=0','LimitFSIZE=8388608',
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


def verify_host(unit,pinned,entry='bridge_host_worker.py'):
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
    tap.cgroup_limits(unit,{'memory.max':'134217728','memory.high':'100663296','memory.swap.max':'0',
                            'cpu.max':'10000 100000','pids.max':'8'})
    require(tap.start_ticks(pid)==before,'host_identity')


def run(mode):
    require(sys.platform=='linux' and os.geteuid()==0,'linux_root_required')
    resource.setrlimit(resource.RLIMIT_AS,(268435456,268435456))
    resource.setrlimit(resource.RLIMIT_CPU,(30,30))
    owner.private_directory(owner.STATE)
    descriptor=os.open(owner.STATE/'bridge.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise groups.legacy.Failure('bridge_busy') from None
    token=read_onebot_token() if mode in {'live','connect'} else None
    if mode=='live':
        try:
            old=owner.read_json(owner.STATE/'last-bridge-send.json')
        except FileNotFoundError:
            old=None
        require(old is None or old.get('state')=='accepted','previous_send_requires_review')
    ensure_session()
    host={kind:owned.identity('/proc/self/ns/'+kind) for kind in ('net','user')}
    network,business=tap.read_network(),tap.business_snapshot()
    pinned=groups.PinnedNative()
    bundle=None
    peers={}
    started=[]
    good=False
    public_code='trial_failed'
    native_unit=groups.READER_PREFIX+secrets.token_hex(6)+'.service'
    host_unit='astrbot-wechat-bridge-host-'+secrets.token_hex(6)+'.service'
    audit=None
    cleanup=[]
    try:
        pinned.open()
        controller,binding=groups.load_send_binding(pinned,TARGET)
        seconds=min(600,pinned.remaining_seconds()-35)
        require(seconds>=60,'native_trial_expiring')
        native_mode='observe' if mode=='connect' else mode
        host_mode='live' if mode=='connect' else mode
        marker='AWIPC-'+secrets.token_hex(8) if native_mode=='observe' else None
        policy=ipc.Trial('wechat-primary',binding['roomId'],native_mode,marker)
        bundle=Path(tempfile.mkdtemp(prefix='astrbot-wechat-bridge.',dir='/var/tmp'))
        for name in NATIVE_FILES:
            source=(HERE/name) if name!='bridge_ipc.py' or (HERE/name).exists() else HERE.parent/'wechat_bridge/ipc.py'
            groups.legacy.write_public(bundle,name,groups.legacy.source_text(source))
        os.chmod(bundle,0o755)
        native_config={'schemaVersion':1,'account':'wechat-primary','token':pinned.token(),'roomId':binding['roomId'],
                       'targetName':TARGET,'mode':native_mode,'marker':marker,'runtimeSeconds':seconds}
        host_config={'schemaVersion':1,'account':'wechat-primary','roomId':binding['roomId'],'targetName':TARGET,
                     'mode':host_mode,'runtimeSeconds':seconds,'onebotToken':token}
        for name,unit,properties,entry,configuration in (
            ('native',native_unit,groups.reader_properties(bundle,'awbi-'+secrets.token_hex(4),pinned.host.parent,pinned.netfd,seconds),
             '/opt/bridge_native_worker.py',native_config),
            ('host',host_unit,host_properties(paths.VERIFICATION,seconds),'/opt/bridge/scripts/bridge_host_worker.py',host_config)):
            tap.require_unit_absent(unit)
            arguments=['/usr/bin/systemd-run','--system','--quiet','--collect','--wait','--pipe','--unit='+unit,
                       *properties,'/usr/bin/python3','-I','-B',entry]
            started.append(unit)
            peers[name]=Duplex(arguments,configuration)
        del native_config,host_config,token
        isolated=set()
        ready=set()
        armed=False
        request=None
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            for name,peer in peers.items():
                for record in peer.tick():
                    kind=record.get('kind')
                    if kind=='isolated':
                        checks=record.get('checks')
                        require(name not in isolated and record.get('ok') is True and isinstance(checks,dict)
                                and all(flag is True for flag in checks.values()),'worker_isolation')
                        if name=='native':
                            groups.verify_reader(native_unit,pinned,'bridge_native_worker.py')
                        else:
                            verify_host(host_unit,pinned)
                        isolated.add(name)
                        peer.acknowledge()
                        wire({'stage':'bridge_worker_isolated','worker':name,'ok':True})
                    elif kind in {'native_ready','host_ready'}:
                        require(name in isolated and name not in ready,'worker_readiness')
                        if name=='host':
                            require(record.get('connected') is (host_mode=='live'),'onebot_not_connected')
                        ready.add(name)
                        if ready=={'native','host'}:
                            peers['native'].send({'kind':'start'})
                    elif kind=='native_armed':
                        require(name=='native' and ready=={'native','host'} and not armed,'worker_readiness')
                        armed=True
                        if mode=='connect':
                            good=True
                            public_code='ok'
                            wire({'stage':'bridge_link_verified','ok':True,'astrbot_connected':True,
                                  'messages_sent':0,'native_sending_enabled':False})
                        else:
                            wire({'stage':'bridge_waiting','mode':mode,'seconds':max(1,int(deadline-time.monotonic())),
                                  'marker':marker,'connected':mode=='live'})
                    elif kind=='message':
                        require(name=='native' and armed and mode!='connect' and set(record)=={'kind','data'},'ipc_message')
                        forward=policy.admit(record['data'])
                        if mode=='live':
                            peers['native'].send({'kind':'permit','sourceId':policy.source['id'],'permit':policy.permit})
                        peers['host'].send(forward)
                        wire({'stage':'bridge_message_received','ok':True,'forwarded_to_host':True})
                    elif kind=='observed':
                        require(name=='host' and mode=='observe' and policy.source is not None
                                and record.get('sourceId')==policy.source['id'],'ipc_observation')
                        good=True
                        public_code='ok'
                        wire({'stage':'bridge_ipc_verified','ok':True,'messages_sent':0,'astrbot_connected':False})
                    elif kind=='send':
                        require(name=='host' and armed and mode=='live','ipc_send')
                        request=policy.authorize(record)
                        audit={**binding,'state':'submitted','trialId':secrets.token_hex(16),'key':request['key'],
                               'sourceId':request['sourceId'],'textSha256':hashlib.sha256(request['text'].encode()).hexdigest()}
                        owner.write_json(owner.STATE/'last-bridge-send.json',audit)
                        peers['native'].send(request)
                        wire({'stage':'bridge_send_admitted','ok':True,'maximum_submissions':1})
                    elif kind=='send_intent':
                        require(name=='native' and request is not None and record.get('requestId')==request['requestId']
                                and isinstance(record.get('sendId'),str) and groups.OPERATION_RE.fullmatch(record['sendId']),'ipc_receipt')
                        audit={**audit,'sendId':record['sendId']}
                        owner.write_json(owner.STATE/'last-bridge-send.json',audit)
                    elif kind=='native_result':
                        require(name=='native' and request is not None and record.get('requestId')==request['requestId'],'ipc_receipt')
                        receipt=record.get('receipt')
                        confirmed=record.get('ok') is True and record.get('server_confirmed') is True
                        if confirmed:
                            require(isinstance(receipt,dict) and receipt.get('account')=='wechat-primary'
                                    and receipt.get('destination')==binding['roomId'] and receipt.get('state')=='accepted'
                                    and receipt.get('send_id')==audit.get('sendId'),'ipc_receipt')
                        groups.checkpoint_current(owner,pinned)
                        audit={**audit,'state':'accepted' if confirmed else 'unknown','serverConfirmed':confirmed}
                        owner.write_json(owner.STATE/'last-bridge-send.json',audit)
                        if not isinstance(receipt,dict):
                            receipt={'account':'wechat-primary','destination':binding['roomId'],'send_id':'',
                                     'state':'unknown','client_id':'','server_id':''}
                        peers['host'].send({'kind':'receipt','requestId':request['requestId'],'receipt':receipt})
                        wire({'stage':'bridge_native_result','ok':confirmed,'server_confirmed':confirmed})
                    elif kind=='reply_done':
                        require(name=='host' and request is not None and record.get('requestId')==request['requestId'],'ipc_reply')
                        good=record.get('ok') is True and audit.get('state')=='accepted'
                        public_code='ok' if good else 'reply_failed'
                    elif kind=='reply_rejected':
                        require(name=='host' and type(record.get('code')) is int,'ipc_reply')
                        wire({'stage':'bridge_reply_rejected','retcode':record['code']})
                        if policy.source is not None:
                            raise groups.legacy.Failure('onebot_reply_rejected')
                    elif kind in {'native_error','host_error'}:
                        phase=record.get('phase')
                        wire({'stage':'bridge_worker_error','worker':name,
                              'phase':phase if phase in {'configuration','state_open','websocket_open','ipc_read'} else 'worker'})
                        raise groups.legacy.Failure('worker_failed_'+name)
                    else:
                        raise groups.legacy.Failure('ipc_frame_rejected')
            if good or public_code=='reply_failed':
                break
            require(pinned.unchanged(),'native_changed')
            require(all(peer.process.poll() is None for peer in peers.values()),'worker_exited')
        if not good and public_code!='reply_failed':
            public_code='trial_deadline'
    except Exception as error:
        public_code=getattr(error,'stage','trial_failed')
        if public_code not in {'worker_isolation','worker_readiness','onebot_not_connected','ipc_message','ipc_observation',
                'ipc_send','ipc_receipt','ipc_reply','onebot_reply_rejected','worker_failed_native','worker_failed_host',
                'ipc_frame_rejected','native_changed','worker_exited','native_trial_expiring','binding_conflict','checkpoint_failed'}:
            public_code='trial_failed'
    finally:
        for unit in reversed(started):
            try:
                tap.stop_unit(unit,True)
            except Exception:
                cleanup.append('unit_cleanup')
        for peer in peers.values():
            try:
                peer.close()
            except Exception:
                cleanup.append('pipe_cleanup')
        same=pinned.unchanged() if pinned.host is not None else False
        pinned.close()
        if bundle is not None:
            for name in NATIVE_FILES:
                (bundle/name).unlink(missing_ok=True)
            bundle.rmdir()
        flags,failures=tap.compare_baselines(host,network,business)
        clean=not cleanup and not failures and same
        wire({'stage':'bridge_cleanup','ok':clean,'workers_removed':not cleanup,'native_session_unchanged':same,**flags})
        os.close(descriptor)
    result=good and clean
    wire({'stage':'bridge_trial_result','ok':result,'mode':mode,'code':'ok' if result else public_code,
          'server_confirmed':bool(audit and audit.get('state')=='accepted')})
    return 0 if result else 1


def main():
    if sys.argv[1:] not in (['observe'],['connect'],['live']):
        return 2
    try:
        return run(sys.argv[1])
    except Exception:
        wire({'stage':'bridge_trial_result','ok':False,'code':'preflight_failed'})
        return 1


if __name__=='__main__':
    raise SystemExit(main())
