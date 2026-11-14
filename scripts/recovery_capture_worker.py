#!/usr/bin/env python3
"""Root-only capture of quiesced inputs; no sends, login or lifecycle changes."""
import importlib.util
import os
from pathlib import Path
import select
import sys


def run():
    if os.geteuid()!=0: return 1
    spec=importlib.util.spec_from_file_location('_capture_formal',Path(__file__).with_name('run_forwarder.py'))
    formal=importlib.util.module_from_spec(spec);sys.modules[spec.name]=formal;spec.loader.exec_module(formal)
    c=formal.core; cp=c.recovery_checkpoints
    value=c.wire.decode(sys.stdin.buffer.readline(16385))
    expected={'token','checkpointName','seed','nativePid','nativeTicks','profile','selfSender',
        'parentPid','parentTicks','hostDigest'}
    c.require(set(value)==expected and c.wire.HEX.fullmatch(value['token']),'capture_config')
    token=value['token']
    def notify(phase,**fields):
        sys.stdout.buffer.write(c.wire.encode(dict(token=token,phase=phase,**fields)));sys.stdout.buffer.flush()
    p=c.groups.PinnedNative()
    try:
        p.open()
        c.require((p.host.client_pid,p.host.client_ticks)==(value['nativePid'],value['nativeTicks']),
            'recovery_owner')
        c.require(c.delivery.role_digest(c.HERE.parent,'host',source=True)==value['hostDigest'],
            'capture_source_changed')
        adapter=cp.ForwarderCapture(c.owner,p,value['checkpointName'],value['seed'])
        def alive():
            c.require(c.tap.start_ticks(value['parentPid'])==value['parentTicks'] and p.unchanged()
                and adapter.profile()==value['profile'],'recovery_owner')
        alive()
        original_barrier=None
        def copied():
            nonlocal original_barrier
            alive()
            with cp.bridge(cp.BRIDGE_PUBLIC) as live: original_barrier=cp.replay_barrier(live)
            notify('copied',barrier=original_barrier is not None)
        adapter.on_copied=copied
        ledger=c.owner.STATE/'send-dispositions.json'
        ledger_raw=c.owner.read_private(ledger,cp.snapshot.admission.MAX_BYTES)
        def before_publish():
            alive();notify('prepared')
            ready,_,_=select.select([sys.stdin.buffer],[],[],240)
            c.require(bool(ready),'capture_authorization_timeout')
            permit=c.wire.decode(sys.stdin.buffer.readline(256))
            c.require(permit=={'kind':'commit','token':token},'capture_authorization')
            alive()
            c.require(c.owner.read_private(ledger,cp.snapshot.admission.MAX_BYTES)==ledger_raw,
                'capture_ledger_changed')
        adapter.before_publish=before_publish
        def before_commit(manifest):
            # Final revocation/ledger/coverage CAS immediately before pointer
            # replacement, after every large write and fsync.
            alive()
            readable,_,_=select.select([sys.stdin.buffer],[],[],0)
            c.require(not readable,'capture_authorization_revoked')
            c.require(c.owner.read_private(ledger,cp.snapshot.admission.MAX_BYTES)==ledger_raw,
                'capture_ledger_changed')
            with cp.bridge(cp.BRIDGE_PUBLIC) as live:
                c.require(cp.manifests._covered(live.recovery_coverage(),manifest)
                    and cp.replay_barrier(live)==original_barrier,'capture_live_changed')
        adapter.before_commit=before_commit
        # command.lock is acquired nonblocking inside checkpoint_copy. An idle
        # owner is required; mailbox contention is retried by the parent later.
        status=c.owner.read_json(c.owner.CONTROL/'status.json')
        if status.get('busy') is True:
            notify('failed',code='busy');return 0
        c.require(status.get('busy') is False and status.get('ready') is True,'capture_owner_status')
        pair=cp.capture(adapter,cp.BRIDGE_PUBLIC,c.owner.STATE)
        alive()
        with cp.bridge(cp.BRIDGE_PUBLIC) as live: barrier=cp.replay_barrier(live) is not None
        if barrier:
            # Parent kept both workers quiescent for this exceptional proof.
            reconcile=c.recovery_coordinator.Production(c,bridge_path=cp.BRIDGE_PUBLIC)
            try: reconcile.reconcile(pair,p,value['checkpointName'],value['seed'],value['selfSender'])
            finally:
                reconcile.pinned=None;reconcile.close()
        with cp.bridge(cp.BRIDGE_PUBLIC) as live:
            coverage=live.recovery_coverage()
            count=live.db.execute('SELECT count(*) FROM inbox WHERE seq>=?',(coverage['floorSeq'],)).fetchone()[0]
            barrier=cp.replay_barrier(live) is not None
        notify('done',floorSeq=coverage['floorSeq'],epoch=coverage['epoch'],
            protectedCount=count,barrier=barrier)
        return 0
    except c.owner.RuntimeErrorCode as error:
        notify('failed',code='busy' if error.code=='busy' else 'requires_review')
        return 0 if error.code=='busy' else 1
    except Exception:
        notify('failed',code='requires_review');return 1
    finally: p.close()


if __name__=='__main__':
    try: raise SystemExit(run())
    except Exception: raise SystemExit(1)
