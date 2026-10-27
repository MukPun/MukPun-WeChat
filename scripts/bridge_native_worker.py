#!/usr/bin/env python3
"""One bounded Native-side IPC trial; no login, logout or session lifecycle."""
import importlib.util
import os
from pathlib import Path
import select
import sys
import time


def module(name):
    spec=importlib.util.spec_from_file_location('_native_worker_'+name,Path(__file__).with_name(name+'.py'))
    value=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=value
    spec.loader.exec_module(value)
    return value


ipc=module('bridge_ipc')
reader=module('group_reader_client')
native=module('bridge_native_protocol')
reader.CODE_FILES=reader.CODE_FILES+('bridge_native_worker.py','bridge_native_protocol.py','bridge_ipc.py')


def emit(value):
    sys.stdout.buffer.write(ipc.encode(value))
    sys.stdout.buffer.flush()


def config():
    value=ipc.decode(sys.stdin.buffer.readline(4097))
    if (set(value)!={'schemaVersion','account','token','roomId','targetName','mode','marker','runtimeSeconds'}
            or value['schemaVersion']!=1 or value['account']!='wechat-primary'
            or not ipc.text(value['token'],1024) or len(value['token'])<32
            or not ipc.text(value['targetName'],256) or type(value['runtimeSeconds']) is not int
            or not 30<=value['runtimeSeconds']<=620):
        raise ipc.IPCError('config')
    ipc.Trial(value['account'],value['roomId'],value['mode'],value['marker'])
    return value


def main():
    try:
        value=config()
        checks=reader.isolation_checks()
        good=set(checks)==reader.CHECK_NAMES and all(checks.values())
        emit({'kind':'isolated','ok':good,'checks':checks})
        if not good or not reader.await_ready():
            return 1
        source=native.Source(value['account'],value['token'],value['roomId'],value['targetName'],value['mode'],value['marker'])
        trial=ipc.Trial(value['account'],value['roomId'],value['mode'],value['marker'])
        emit({'kind':'native_ready'})
        deadline=time.monotonic()+value['runtimeSeconds']-3
        lines=ipc.Lines()
        os.set_blocking(sys.stdin.fileno(),False)
        started=permitted=False
        next_poll=0.
        while time.monotonic()<deadline:
            readable,_,_=select.select([sys.stdin.fileno()],[],[],.1)
            if readable:
                raw=os.read(sys.stdin.fileno(),4096)
                if not raw:
                    return 0
                lines.feed(raw)
            while True:
                command=lines.pop()
                if command is None:
                    break
                if command=={'kind':'stop'}:
                    return 0
                if command=={'kind':'start'} and not started:
                    source.start(min(deadline,time.monotonic()+30))
                    started=True
                    emit({'kind':'native_armed'})
                elif (command.get('kind')=='permit' and set(command)=={'kind','sourceId','permit'}
                      and trial.source is not None and not permitted and command['sourceId']==trial.source['id']
                      and isinstance(command['permit'],str) and ipc.HEX.fullmatch(command['permit'])):
                    trial.permit=command['permit']
                    permitted=True
                elif command.get('kind')=='send' and permitted:
                    trial.authorize(command)
                    worker=native.TextSendClient(value['account'],value['token'],value['roomId'],command['text'],command['key'],
                        on_intent=lambda identity:emit({'kind':'send_intent','requestId':command['requestId'],'sendId':identity}))
                    result=worker.run(min(deadline,time.monotonic()+40),lambda _:None)
                    emit({'kind':'native_result','requestId':command['requestId'],'ok':result.ok,'code':result.code,
                          'submitted':result.submitted,'server_confirmed':result.server_confirmed,'receipt':worker.receipt})
                else:
                    raise ipc.IPCError('command_rejected')
            if started and not source.emitted and time.monotonic()>=next_poll:
                message=source.poll(min(deadline,time.monotonic()+4))
                next_poll=time.monotonic()+1
                if message is not None:
                    trial.admit(message)
                    emit({'kind':'message','data':message})
        return 0
    except Exception:
        emit({'kind':'native_error','code':'native_trial_failed'})
        return 1


if __name__=='__main__':
    raise SystemExit(main())
