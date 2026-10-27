#!/usr/bin/env python3
"""Read existing accepted ticket in a new isolated worker; never resubmit."""
import importlib.util
import json
import os
from pathlib import Path
import resource
import secrets
import tempfile
import sys

HERE=Path(__file__).resolve().parent


def module(name):
    spec=importlib.util.spec_from_file_location('_delivery_host_'+name,HERE/(name+'.py'))
    value=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=value
    spec.loader.exec_module(value)
    return value


def main():
    resource.setrlimit(resource.RLIMIT_AS,(268435456,268435456))
    resource.setrlimit(resource.RLIMIT_CPU,(15,15))
    g=module('run_group_probe')
    owner=module('session_runtime')
    native=g.PinnedNative()
    host={kind:g.owned.identity('/proc/self/ns/'+kind) for kind in ('net','user')}
    network,business=g.tap.read_network(),g.tap.business_snapshot()
    bundle=capture=None
    unit=g.READER_PREFIX+secrets.token_hex(6)+'.service'
    files=g.FILES+('bridge_delivery_worker.py',)
    ok=False
    try:
        native.open()
        controller,binding=g.load_send_binding(native,'\u95f2\u804a-A')
        attempt=owner.read_json(owner.STATE/'last-bridge-send.json')
        previous=owner.read_json(owner.STATE/'group-send-attempt.json')
        g.require(attempt.get('state')=='accepted' and attempt.get('serverConfirmed') is True,'receipt_state')
        bundle=Path(tempfile.mkdtemp(prefix='astrbot-wechat-delivery.',dir='/var/tmp'))
        for name in files:
            g.legacy.write_public(bundle,name,g.legacy.source_text(HERE/name))
        os.chmod(bundle,0o755)
        configuration={'account':'wechat-primary','token':native.token(),'roomId':binding['roomId'],
                       'targetName':binding['targetName'],'sendId':attempt['sendId'],
                       'previousSendId':previous.get('sendId'),'bodyHash':attempt['textSha256'],
                       'sourceId':attempt['sourceId'],'sentAt':(owner.STATE/'last-bridge-send.json').stat().st_mtime}
        g.tap.require_unit_absent(unit)
        args=['/usr/bin/systemd-run','--system','--quiet','--collect','--wait','--pipe','--unit='+unit,
              *g.reader_properties(bundle,'awbd-'+secrets.token_hex(4),native.host.parent,native.netfd,60),
              '/usr/bin/python3','-I','-B','/opt/bridge_delivery_worker.py']
        capture=g.InputCapture(args,configuration)
        isolated=False
        import time
        deadline=time.monotonic()+65
        while capture.process.poll() is None or capture.selector.get_map():
            g.require(time.monotonic()<deadline,'receipt_deadline')
            capture.drain(.1)
            while b'\n' in capture.output['stdout']:
                line,_,rest=capture.output['stdout'].partition(b'\n')
                capture.output['stdout'][:]=rest
                value=json.loads(line)
                if value.get('kind')=='isolated':
                    g.require(value.get('ok') is True and not isolated,'receipt_isolation')
                    g.verify_reader(unit,native,'bridge_delivery_worker.py')
                    capture.acknowledge()
                    isolated=True
                elif value.get('kind')=='delivery_check':
                    g.require(isolated,'receipt_isolation')
                    # Worker output contains booleans/counts only, no text/IDs.
                    g.require(all(key=='kind' or type(item) in (bool,int,type(None))
                        or key=='outgoing_text' and isinstance(item,str) and len(item.encode())<=512
                        for key,item in value.items()),'receipt_output')
                    owner.wire({'stage':'bridge_delivery_inspection',**{k:v for k,v in value.items() if k!='kind'}})
                    ok=True
                else:
                    raise ValueError('receipt_failed')
        ok=ok and capture.process.wait()==0
    except Exception:
        ok=False
        owner.wire({'stage':'bridge_delivery_inspection','ok':False,'code':'read_only_check_failed'})
    finally:
        clean=True
        try:
            g.tap.stop_unit(unit,True)
        except Exception:
            clean=False
        if capture:
            capture.close()
        same=native.unchanged() if native.host is not None else False
        native.close()
        if bundle:
            for name in files:
                (bundle/name).unlink(missing_ok=True)
            bundle.rmdir()
        flags,failures=g.tap.compare_baselines(host,network,business)
        owner.wire({'stage':'bridge_delivery_cleanup','ok':clean and same and not failures,
                    'native_session_unchanged':same,**flags})
    return 0 if ok and clean and same and not failures else 1


if __name__=='__main__':
    raise SystemExit(main())
