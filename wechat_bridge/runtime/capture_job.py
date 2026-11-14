"""Single owned, bounded root job. Broker turns never wait for archive I/O."""
import os
import secrets
import time


class CaptureJob:
    LIMIT_SECONDS=300

    def __init__(self,core,state,token):
        self.core,self.state,self.token=core,state,token
        self.generation=state.workers.generation
        self.unit='astrbot-wechat-capture-'+secrets.token_hex(6)+'.service'
        self.phase='copying'
        self.started=time.monotonic()
        self.committed=False
        self.finished=False
        self.closed=False
        self.core.tap.require_unit_absent(self.unit)
        args=['/usr/bin/systemd-run','--system','--quiet','--collect','--wait','--pipe','--unit='+self.unit,
            # Stop order reverses start order: let the broker receive SIGTERM
            # and revoke/cancel its job before BindsTo stops the job itself.
            '--property=BindsTo='+core.UNIT,'--property=Before='+core.UNIT,
            '--property=MemoryMax=1024M','--property=MemoryHigh=768M','--property=MemorySwapMax=0',
            '--property=CPUQuota=100%','--property=TasksMax=16','--property=Nice=19',
            '--property=RuntimeMaxSec=300','--property=TimeoutStopSec=5','--property=KillMode=control-group',
            '--property=UMask=0077','--property=LimitCORE=0','--property=LimitFSIZE=1101004800',
            '--property=ProtectKernelModules=yes','--property=PrivateNetwork=yes',
            '/usr/bin/python3','-I','-B',str(core.HERE/'recovery_capture_worker.py')]
        config=dict(token=token,checkpointName=state.generation,seed=state.recovery_seed,
            nativePid=state.pinned.host.client_pid,nativeTicks=state.pinned.host.client_ticks,
            profile=state.recovery_adapter.profile(),selfSender=state.self_sender_id,
            parentPid=os.getpid(),parentTicks=core.tap.start_ticks(os.getpid()),
            hostDigest=state.host_digest)
        try:
            self.capture=core.Duplex(args,config)
        except BaseException:
            core.tap.stop_unit(self.unit,True,cgroup='/system.slice/'+self.unit,signal_first=True)
            raise

    def poll(self):
        self.core.require(time.monotonic()-self.started < self.LIMIT_SECONDS,'recovery_checkpoint_timeout')
        self.core.require(self.generation==self.state.workers.generation
            and self.state.pinned.unchanged(),'recovery_owner')
        records=self.capture.tick()
        for record in records:
            self.core.require(record.get('token')==self.token,'capture_result_binding')
            phase=record.get('phase')
            fields={'copied':{'token','phase','barrier'},'prepared':{'token','phase'},
                'done':{'token','phase','floorSeq','epoch','protectedCount','barrier'},
                'failed':{'token','phase','code'}}
            self.core.require(phase in fields and set(record)==fields[phase],'capture_result_binding')
            if phase in {'copied','done'}:
                self.core.require(type(record['barrier']) is bool,'capture_result_binding')
            if phase=='done':
                self.core.wire.recovery_protection({k:record[k] for k in ('floorSeq','epoch')})
                self.core.require(type(record['protectedCount']) is int and record['protectedCount']>=0,
                    'capture_result_binding')
            allowed={'copying':{'copied'},'copied':{'prepared'},'prepared':set(),
                'committing':{'done'},'done':set()}
            if phase=='failed':
                self.core.require(self.phase=='copying' and record.get('code')=='busy','recovery_checkpoint')
                self.finished=True
            else:
                self.core.require(phase in allowed[self.phase],'capture_phase')
                self.phase=phase
                if phase=='done':
                    self.finished=self.committed=True
        if self.capture.process.poll() is not None and not self.finished:
            self.core.require(False,'recovery_checkpoint')
        return records

    def commit(self):
        self.core.require(self.phase=='prepared','capture_phase')
        self.capture.send({'kind':'commit','token':self.token})
        self.phase='committing'

    def close(self):
        if self.closed:
            return
        # Revoke commit authority even if the service cannot immediately stop
        # during filesystem I/O. The child checks EOF at its final commit CAS.
        if not self.capture.process.stdin.closed:
            self.capture.process.stdin.close()
        # Never lose ownership until both unit and its cgroup are proved empty.
        self.core.tap.stop_unit(self.unit,True,cgroup='/system.slice/'+self.unit,signal_first=True)
        self.capture.close()
        self.closed=True
