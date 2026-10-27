"""Existing Native read protections plus a bound, single dynamic text send."""
import importlib.util
import json
from pathlib import Path
import sys
import time


def companion(name):
    spec=importlib.util.spec_from_file_location('_bridge_native_'+name,Path(__file__).with_name(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module


groups=companion('group_receive_protocol')
sender=companion('contact_send_protocol')


class Source:
    def __init__(self,account,token,room,name,mode,marker=None,*,transport=None,clock=time.monotonic):
        self.account,self.room,self.mode,self.marker,self.clock=account,room,mode,marker,clock
        self.reader=groups.GroupReceiveClient(account,token,name,transport=transport,clock=clock)
        self.after=None
        self.emitted=False
        self.seen=set()

    def start(self,deadline):
        room,self.after=self.reader._target(deadline)
        if room!=self.room:
            raise groups.GroupError('target_not_found')

    def poll(self,deadline):
        if self.emitted:
            return None
        page=self.reader._get(self.reader.policy.events(self.after),deadline)
        items=page.get('items')
        after=groups.contact._cursor(page.get('nextCursor'))
        if not isinstance(items,list) or len(items)>20:
            raise groups.GroupError('invalid_events')
        previous=int(self.after)
        for item in items:
            if not isinstance(item,dict):
                raise groups.GroupError('invalid_events')
            current=int(groups.contact._cursor(item.get('cursor')))
            if current<=previous:
                raise groups.GroupError('invalid_events')
            previous=current
        if previous!=int(after):
            raise groups.GroupError('invalid_events')
        self.after=after
        for item in items:
            data=item.get('data')
            if (item.get('kind')!='message.committed' or not isinstance(data,dict)
                    or data.get('origin')!='live' or data.get('direction')!='incoming'
                    or data.get('threadId')!=self.room):
                continue
            identity=data.get('messageId')
            if identity in self.seen:
                continue
            if len(self.seen)>=100:
                raise groups.GroupError('catalog_incomplete')
            self.seen.add(identity)
            message=self.reader._get(self.reader.policy.message(identity),deadline,timeout=3)
            if type(message.get('type')) is not int or message['type']!=1:
                continue
            if not self.reader._valid_message(message,identity,self.room):
                raise groups.GroupError('invalid_message')
            body=message['body']
            if self.mode=='observe' and body!=self.marker:
                continue
            if self.mode=='live' and (not body.startswith('/ai ') or not body[4:].strip()):
                continue
            self.emitted=True
            return {'account':self.account,'id':identity,'room_id':self.room,'sender_id':message['senderId'],
                    'body':body,'timestamp':int(time.time()),'origin':'live','direction':'incoming','type':1}
        return None


class TextPolicy(sender.RequestPolicy):
    def __init__(self,account,room,body):
        super().__init__(account)
        self.room,self.body=room,body

    def bind(self,target,key):
        if self._body is not None or target!=self.room or not isinstance(key,str) or not sender.KEY_RE.fullmatch(key):
            raise sender.SendError('request_not_allowed')
        self._body=json.dumps({'threadId':target,'text':self.body},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        self._headers={'Content-Type':'application/json','Idempotency-Key':key}


class TextSendClient(sender.ContactSendClient):
    valid_target=staticmethod(lambda room:isinstance(room,str) and room!='@chatroom' and room.endswith('@chatroom'))

    def __init__(self,account,token,room,body,key,*,on_intent=None,transport=None,clock=time.monotonic,sleep=time.sleep):
        if not self.valid_target(room) or not isinstance(body,str) or not 0<len(body.encode())<=8192:
            raise sender.SendError('invalid_arguments')
        self.POLICY_TYPE=lambda account:TextPolicy(account,room,body)
        super().__init__(account,token,lookup=lambda deadline:(room,'0'),transport=transport,clock=clock,sleep=sleep)
        self.fixed_key,self.callback,self.receipt=key,on_intent,None

    def _new_key(self):
        return self.fixed_key

    def _intent(self,value,target):
        first=self._send_id is None
        state=super()._intent(value,target)
        self.receipt={'account':value['accountId'],'destination':value['destination'],'send_id':value['id'],
                      'state':state,'client_id':value['clientId'] or '', 'server_id':value['serverId'] or ''}
        if first and self.callback:
            self.callback(value['id'])
        return state
