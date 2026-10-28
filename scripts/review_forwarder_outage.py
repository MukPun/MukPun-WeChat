#!/usr/bin/env python3
"""Inspect a stopped bridge and its last send without resubmitting messages."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import sys
import tempfile
import zipfile
import secrets
import time
import subprocess

ROOT = Path(__file__).resolve().parent


def main():
    assert os.geteuid() == 0 and sys.argv[1:] in ([], ['--recover'])
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    spec = importlib.util.spec_from_file_location('_outage_formal', ROOT / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core, owner = formal.core, formal.core.owner
    audit = owner.read_json(owner.STATE / 'forward-send.json')
    result = {'audit_state': audit.get('state'), 'audit_fields': sorted(audit),
              'audit_modified': (owner.STATE / 'forward-send.json').stat().st_mtime}
    spool = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite').resolve()
    sc = sqlite3.connect(spool.as_uri() + '?mode=ro', uri=True, timeout=.2)
    try:
        result['inbox'] = dict(sc.execute('SELECT state,count(*) FROM inbox GROUP BY state'))
        result['action_results'] = dict(sc.execute("SELECT coalesce(json_extract(result,'$.retcode'),-1),count(*) FROM submissions GROUP BY 1"))
        result['unresolved_actions'] = sc.execute("SELECT count(*) FROM submissions WHERE result IS NULL OR json_extract(result,'$.retcode')=1408").fetchone()[0]
    finally:
        sc.close()
    pinned = core.groups.PinnedNative()
    pinned.open()
    work = Path(tempfile.mkdtemp(prefix='astrbot-wechat-outage-review.', dir='/var/tmp'))
    try:
        status = core.trial.fresh_status()
        result['session'] = {k: status.get(k) for k in ('ok','accounts','connected','ready')}
        store = owner.companion('session_state_store')
        latest = store.SnapshotStore(owner.STATE / 'checkpoints').latest()
        with zipfile.ZipFile(latest) as archive:
            store._archive_manifest(archive)
            names = [i.filename for i in archive.infolist() if i.filename.endswith('/agent/control.sqlite')]
            assert len(names) == 1
            for suffix in ('', '-wal', '-shm', '-journal'):
                if names[0] + suffix in archive.namelist():
                    with archive.open(names[0] + suffix) as src, (work / ('control.sqlite' + suffix)).open('wb') as dst:
                        shutil.copyfileobj(src, dst, 65536)
        connection = sqlite3.connect((work / 'control.sqlite').as_uri() + '?mode=ro', uri=True, timeout=.2)
        connection.row_factory = sqlite3.Row
        try:
            result['outbox_columns'] = [r[1] for r in connection.execute('PRAGMA table_info(outbox)')]
            row = connection.execute('SELECT * FROM outbox WHERE id=?', (audit.get('sendId'),)).fetchone()
            result['send_present'] = row is not None
            if row:
                row = dict(row)
                result['native_state'] = row.get('state')
                result['key_matches'] = row.get('idempotency_key') == audit.get('key')
                result['destination_matches'] = hashlib.sha256(row['destination'].encode()).hexdigest() == audit.get('destinationSha256')
                result['json_shapes'] = {}
                for k,v in row.items():
                    if isinstance(v,str) and v.startswith('{'):
                        try:
                            data=json.loads(v)
                            result['json_shapes'][k]=sorted(data)
                            if 'state' in data: result['data_state']=data['state']
                        except ValueError: pass
        finally:
            connection.close()
        result['native_session_unchanged'] = pinned.unchanged()
        if sys.argv[1:] == ['--recover']:
            assert row and row['state'] == 'accepted' and result['key_matches'] and result['destination_matches']
            assert not any(result['inbox'].get(k) for k in ('queued','publishing','uncertain'))
            g = core.groups
            host = {k:core.owned.identity('/proc/self/ns/'+k) for k in ('net','user')}
            network, business = core.tap.read_network(), core.tap.business_snapshot()
            unit = g.READER_PREFIX + secrets.token_hex(6) + '.service'
            bundle = Path(tempfile.mkdtemp(prefix='astrbot-wechat-receipt.',dir='/var/tmp'))
            capture = None
            files = g.FILES + ('bridge_delivery_worker.py',)
            verified = False
            try:
                for name in files:
                    source = ROOT / ('review_stopped_send_worker.py' if name == 'bridge_delivery_worker.py' else name)
                    g.legacy.write_public(bundle,name,g.legacy.source_text(source))
                os.chmod(bundle,0o755)
                config = {'account':'wechat-primary','token':pinned.token(),'sendId':audit['sendId'],
                          'destination':row['destination'],'bytes':audit['bytes'],'textSha256':audit['textSha256']}
                args = ['/usr/bin/systemd-run','--system','--quiet','--collect','--wait','--pipe','--unit='+unit,
                        *g.reader_properties(bundle,'awrr-'+secrets.token_hex(4),pinned.host.parent,pinned.netfd,60),
                        '/usr/bin/python3','-I','-B','/opt/bridge_delivery_worker.py']
                capture = g.InputCapture(args,config)
                del config
                end = time.monotonic()+55
                while capture.process.poll() is None or capture.selector.get_map():
                    assert time.monotonic() < end
                    capture.drain(.05)
                    while b'\n' in capture.output['stdout']:
                        line,_,rest=capture.output['stdout'].partition(b'\n');capture.output['stdout'][:]=rest
                        value=json.loads(line)
                        if value.get('kind')=='isolated':
                            assert value.get('ok') is True
                            g.verify_reader(unit,pinned,'bridge_delivery_worker.py')
                            capture.acknowledge()
                        else:
                            assert value == {'kind':'delivery_check','verified':True}
                            verified=True
                assert verified and capture.process.wait()==0
            finally:
                g.tap.stop_unit(unit,True)
                if capture: capture.close()
                for name in files: (bundle/name).unlink(missing_ok=True)
                bundle.rmdir()
            flags, failures = core.tap.compare_baselines(host,network,business)
            assert not failures and pinned.unchanged()
            backup = owner.STATE / ('outage-recovery-'+str(int(time.time())))
            backup.mkdir(mode=0o700)
            owner.write_json(backup/'forward-send.json',audit)
            db = sqlite3.connect(spool,timeout=.2)
            try:
                with db:
                    rows = db.execute("SELECT key,native_key,result FROM submissions WHERE result IS NULL OR json_extract(result,'$.retcode')=1408").fetchall()
                    matching = [i for i in rows if hashlib.sha256((i[1]+':0').encode()).hexdigest()[:32] == audit['key']]
                    assert len(matching)==1
                    owner.write_json(backup/'submission.json',{'key':matching[0][0],'native_key':matching[0][1],'result':matching[0][2]})
                    db.execute("INSERT OR IGNORE INTO identities(kind,raw) VALUES('outgoing',?)",(audit['sendId'],))
                    mid=db.execute("SELECT id FROM identities WHERE kind='outgoing' AND raw=?",(audit['sendId'],)).fetchone()[0]
                    answer={'status':'ok','retcode':0,'data':{'message_id':mid},'message':'','wording':''}
                    db.execute('UPDATE submissions SET result=? WHERE key=?',(json.dumps(answer),matching[0][0]))
            finally: db.close()
            audit.update(state='accepted',review='native_receipt_and_projection_verified')
            owner.write_json(owner.STATE/'forward-send.json',audit)
            subprocess.run(['/usr/bin/systemctl','reset-failed',core.UNIT],check=True,capture_output=True,timeout=5)
            subprocess.run(['/usr/bin/python3','-I','-B',str(ROOT/'run_forwarder.py'),'start'],check=True,capture_output=True,timeout=15)
            result.update(reconciled=True,no_resend=True,**flags)
        print(json.dumps(result, sort_keys=True))
    finally:
        pinned.close()
        for suffix in ('', '-wal', '-shm', '-journal'):
            (work / ('control.sqlite' + suffix)).unlink(missing_ok=True)
        work.rmdir()


if __name__ == '__main__':
    main()
