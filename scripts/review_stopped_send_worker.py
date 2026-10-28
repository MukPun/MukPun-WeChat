"""GET-only reconciliation of one bound send and its outgoing projection."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

s = importlib.util.spec_from_file_location('_receipt_reader', Path(__file__).with_name('group_reader_client.py'))
r = importlib.util.module_from_spec(s)
s.loader.exec_module(r)
r.CODE_FILES += ('bridge_delivery_worker.py',)


def main():
    v = json.loads(sys.stdin.buffer.readline(4097))
    checks = r.isolation_checks()
    good = set(checks) == r.CHECK_NAMES and all(checks.values())
    print(json.dumps({'kind':'isolated','ok':good}), flush=True)
    if not good or not r.await_ready(): return 1
    client = r.group.contact.ContactReceiveClient(v['account'], v['token'])
    def get(path):
        client.policy.path = path
        return client._get(path, time.monotonic()+10, timeout=8)
    intent = get('/v1/accounts/'+v['account']+'/sends/'+v['sendId'])
    valid = (intent.get('id') == v['sendId'] and intent.get('accountId') == v['account']
             and intent.get('state') == 'accepted' and intent.get('destination') == v['destination']
             and bool(intent.get('serverId')) and bool(intent.get('localMessageId')))
    if valid:
        msg = get(client.policy.message(intent['localMessageId']))
        body = msg.get('body')
        valid = (msg.get('accountId') == v['account'] and msg.get('threadId') == v['destination']
                 and msg.get('direction') == 'outgoing' and msg.get('type') == 1 and isinstance(body,str)
                 and len(body.encode()) == v['bytes'] and hashlib.sha256(body.encode()).hexdigest() == v['textSha256'])
    print(json.dumps({'kind':'delivery_check','verified':valid}),flush=True)
    return 0 if valid else 1


if __name__ == '__main__':
    sys.exit(main())
