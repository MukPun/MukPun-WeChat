#!/usr/bin/env python3
"""Non-root OneBot-side IPC worker. Never receives a Native API credential."""
import asyncio
import json
import os
from pathlib import Path
import select
import sys

ROOT=Path('/opt/bridge')
STEP='configuration'
sys.path[:0]=[str(ROOT),str(ROOT/'onebot-env/lib/python3.12/site-packages')]
from wechat_bridge import ipc


def emit(value):
    sys.stdout.buffer.write(ipc.encode(value))
    sys.stdout.buffer.flush()


def isolation():
    checks={}
    try:
        status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        checks['non_root']=os.geteuid()>0 and os.getegid()>0
        checks['zero_caps']=all(int(status[key].strip(),16)==0 for key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))
        checks['no_new_privileges']=status['NoNewPrivs'].strip()=='1'
        checks['readonly_code']=all(os.statvfs(path).f_flag&os.ST_RDONLY for path in ('/','/usr','/etc','/opt/bridge'))
        mounts=Path('/proc/self/mountinfo').read_text().splitlines()
        checks['docker_masked']=all(any(line.split()[4]==path and 'inaccessible' in line.split()[3] for line in mounts)
                                    for path in ('/run/docker.sock','/run/containerd'))
        checks['home_hidden']=all(any(line.split()[4]==path and ' - tmpfs ' in line for line in mounts) for path in ('/home','/root'))
        group=Path('/proc/self/cgroup').read_text().strip().removeprefix('0::')
        base=Path('/sys/fs/cgroup')/group.lstrip('/')
        checks['resource_limits']=all((base/name).read_text().strip()==expected for name,expected in {
            'memory.max':'67108864','memory.high':'50331648','memory.swap.max':'0','pids.max':'8','cpu.max':'10000 100000'}.items())
    except Exception:
        checks['complete']=False
    return checks


async def run(config):
    global STEP
    from wechat_bridge.gateway import Gateway
    from wechat_bridge.protocol import Bridge,Config
    from wechat_bridge.state import State
    loop=asyncio.get_running_loop()
    incoming=asyncio.StreamReader(limit=ipc.MAX_FRAME)
    transport,_=await loop.connect_read_pipe(lambda:asyncio.StreamReaderProtocol(incoming),sys.stdin.buffer)
    gateway=Gateway(config['account'],config['roomId'],emit)
    state=runner=task=None
    received=False
    async def consume():
        nonlocal received
        while True:
            raw=await incoming.readline()
            if not raw:
                if runner:
                    runner.stop.set()
                return
            value=ipc.decode(raw)
            gateway.feed(value)
            if value.get('kind')=='stop':
                if runner:
                    runner.stop.set()
                return
            if config['mode']=='observe' and value.get('kind')=='message':
                message=await gateway.next_message()
                emit({'kind':'observed','sourceId':message.id})
                received=True
                return
    try:
        if config['mode']=='observe':
            emit({'kind':'host_ready','connected':False})
            await asyncio.wait_for(consume(),timeout=config['runtimeSeconds'])
            return 0 if received else 1
        from wechat_bridge.transport import Endpoint,Runner
        STEP='state_open'
        path=Path('/var/lib/astrbot-wechat-bridge').resolve(strict=True)/'state.sqlite'
        state=State(path)
        core=Bridge(Config(config['account'],'account:'+config['account'],config['roomId'],config['targetName']),state,gateway)
        gateway.bridge=core
        def replied(answer):
            if gateway.last_request and not gateway.reported:
                gateway.reported=True
                emit({'kind':'reply_done','requestId':gateway.last_request,'ok':answer.get('retcode')==0})
            elif not gateway.last_request and answer.get('retcode')!=0:
                emit({'kind':'reply_rejected','code':answer.get('retcode')})
        runner=Runner(core,Endpoint('ws://127.0.0.1:6199/ws',config['onebotToken']),on_reply=replied)
        STEP='websocket_open'
        task=asyncio.create_task(runner.run(maximum_seconds=config['runtimeSeconds']))
        await asyncio.wait_for(runner.ready.wait(),timeout=12)
        emit({'kind':'host_ready','connected':True})
        STEP='ipc_read'
        reading=asyncio.create_task(consume())
        try:
            done,_=await asyncio.wait([task,reading],return_when=asyncio.FIRST_COMPLETED)
            for item in done:
                item.result()
        finally:
            reading.cancel()
            await asyncio.gather(reading,return_exceptions=True)
        return 0
    finally:
        if runner:
            runner.stop.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        if state:
            state.close()
        transport.close()


def main():
    try:
        config=ipc.decode(sys.stdin.buffer.readline(4097))
        if (set(config)!={'schemaVersion','account','roomId','targetName','mode','runtimeSeconds','onebotToken'}
                or config['schemaVersion']!=1 or config['account']!='wechat-primary'
                or config['mode'] not in {'observe','live'} or type(config['runtimeSeconds']) is not int
                or not 30<=config['runtimeSeconds']<=620):
            raise ipc.IPCError('config')
        checks=isolation()
        good=len(checks)==7 and all(checks.values())
        emit({'kind':'isolated','ok':good,'checks':checks})
        if not good:
            return 1
        ready,_,_=select.select([sys.stdin.buffer],[],[],5)
        if not ready or sys.stdin.buffer.readline(32)!=b'AWG-READY\n':
            return 1
        return asyncio.run(run(config))
    except Exception:
        emit({'kind':'host_error','code':'host_trial_failed','phase':STEP})
        return 1


if __name__=='__main__':
    raise SystemExit(main())
