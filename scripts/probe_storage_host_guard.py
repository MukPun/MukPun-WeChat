#!/usr/bin/env python3
"""Exercise the actual staged OneBot guard without any production IPC/API."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys


def main():
    assert os.geteuid()==0
    root=Path(__file__).resolve().parent
    source=root/'context-staging/verification'
    spec=importlib.util.spec_from_file_location('_host_guard_proof',source/'scripts/run_bridge_trial.py')
    trial=importlib.util.module_from_spec(spec);sys.modules[spec.name]=trial;spec.loader.exec_module(trial)
    baseline={k:trial.owned.identity('/proc/self/ns/'+k) for k in ('net','user')}
    network,business=trial.tap.read_network(),trial.tap.business_snapshot()
    native=trial.tap.details('astrbot-wechat-session-owner.service').get('MainPID')
    unit='astrbot-wechat-storage-host-guard-'+secrets.token_hex(6)+'.service'
    # This proof must never mount, allocate, or chown the live OneBot state dir.
    props=[v for v in trial.host_properties(source,30) if not v.startswith('--property=StateDirectory')]
    props=trial.groups.legacy.properties_changed(props,PrivateNetwork='yes')
    code="import sys;sys.path.insert(0,'/opt/bridge/scripts');import bridge_host_worker as g;import json;c=g.isolation();print(json.dumps(c));sys.exit(0 if len(c)==7 and all(c.values()) else 1)"
    capture=None
    try:
        trial.tap.require_unit_absent(unit)
        capture=trial.tap.Capture(['/usr/bin/systemd-run','--quiet','--collect','--wait','--pipe','--unit='+unit,
            *props,'/usr/bin/python3','-I','-B','-c',code])
        rc=capture.finish(15)
        checks=json.loads(bytes(capture.output['stdout']))
        assert rc==0 and len(checks)==7 and all(checks.values())
        flags,failures=trial.tap.compare_baselines(baseline,network,business)
        assert not failures and trial.tap.details('astrbot-wechat-session-owner.service').get('MainPID')==native
        print(json.dumps({'stage':'actual_onebot_guard','ok':True,'checks':checks,'native_unchanged':True,**flags}))
    finally:
        trial.tap.stop_unit(unit,True)
        if capture:capture.close()


if __name__=='__main__':main()
