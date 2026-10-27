#!/usr/bin/env python3
"""GET-only receipt/projection inspection. Emits no identifiers or bodies."""
import hashlib
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import re
import sys
import time


def module(name):
    spec=importlib.util.spec_from_file_location('_delivery_'+name,Path(__file__).with_name(name+'.py'))
    value=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=value
    spec.loader.exec_module(value)
    return value


r=module('group_reader_client')
r.CODE_FILES=r.CODE_FILES+('bridge_delivery_worker.py',)


def emit(value):
    print(json.dumps(value,separators=(',',':'),ensure_ascii=True),flush=True)


def main():
    try:
        value=json.loads(sys.stdin.buffer.readline(4097),object_pairs_hook=r.unique,parse_constant=r.invalid)
        if (set(value)!={'account','token','roomId','targetName','sendId','previousSendId','bodyHash','sourceId','sentAt'}
                or value['account']!='wechat-primary' or not re.fullmatch(r'[0-9a-f-]{36}',value['sendId'])
                or not re.fullmatch(r'[0-9a-f]{64}',value['bodyHash'])):
            return 1
        checks=r.isolation_checks()
        good=set(checks)==r.CHECK_NAMES and all(checks.values())
        emit({'kind':'isolated','ok':good})
        if not good or not r.await_ready():
            return 1
        reader=r.group.GroupReceiveClient(value['account'],value['token'],value['targetName'])
        prefix='/v1/accounts/'+value['account']+'/sends/'
        def get(path):
            reader.policy.path=path
            return reader._get(path,time.monotonic()+8,timeout=5)
        intent=get(prefix+value['sendId'])
        report={'kind':'delivery_check','account_matches':intent.get('accountId')==value['account'],
                'destination_matches':intent.get('destination')==value['roomId'],
                'send_id_matches':intent.get('id')==value['sendId'],'state_accepted':intent.get('state')=='accepted',
                'has_server_id':bool(intent.get('serverId')),'has_local_projection':bool(intent.get('localMessageId')),
                'previous_server_id_distinct':None,'projection_loaded':False}
        previous=value['previousSendId']
        if previous is not None:
            if not isinstance(previous,str) or not re.fullmatch(r'[0-9a-f-]{36}',previous):
                return 1
            old=get(prefix+previous)
            report['previous_server_id_distinct']=bool(intent.get('serverId')) and intent.get('serverId')!=old.get('serverId')
            report['previous_client_id_distinct']=intent.get('clientId')!=old.get('clientId')
        local=intent.get('localMessageId')
        if isinstance(local,str) and local:
            message=get(reader.policy.message(local))
            body=message.get('body')
            report.update(projection_loaded=True,projected_group_matches=message.get('threadId')==value['roomId'],
                          projected_outgoing=message.get('direction')=='outgoing',projected_text_type=message.get('type')==1,
                          body_hash_matches=isinstance(body,str) and hashlib.sha256(body.encode()).hexdigest()==value['bodyHash'],
                          body_bytes=len(body.encode()) if isinstance(body,str) else 0,
                          expected_test_phrase=isinstance(body,str) and '微信群AI桥接测试成功' in body)
            if (isinstance(body,str) and len(body.encode())<=512 and report['body_hash_matches']
                    and report['projected_group_matches'] and report['projected_outgoing']):
                # Only this already-approved outgoing reply, never other chat.
                report['outgoing_text']=body
                report['visible_characters']=sum(c.isprintable() and not c.isspace() for c in body)
        source=get(reader.policy.message(value['sourceId']))
        question=source.get('body')
        report['source_is_test_prompt']=question=='/ai 请只回复：微信群AI桥接测试成功。'
        report['source_is_hello']=isinstance(question,str) and question.strip() in {'/ai 你好','/ai 你好！','/ai 你好。'}
        report['source_body_bytes']=len(question.encode()) if isinstance(question,str) else 0
        report['source_time_valid']=False
        created=source.get('createdAt')
        if isinstance(created,str):
            try:
                timestamp=datetime.fromisoformat(created.replace('Z','+00:00'))
                if timestamp.tzinfo is not None:
                    report['source_time_valid']=True
                    report['source_age_at_send_seconds']=int(value['sentAt']-timestamp.timestamp())
            except ValueError:
                pass
        emit(report)
        return 0
    except Exception:
        emit({'kind':'delivery_error','code':'receipt_inspection_failed'})
        return 1


if __name__=='__main__':
    raise SystemExit(main())
