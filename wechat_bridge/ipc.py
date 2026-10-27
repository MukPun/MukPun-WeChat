"""Bounded private pipe frames and one-question trial permissions."""
import json
import re
import secrets
import time

MAX_FRAME = 32768
HEX = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)
MESSAGE_FIELDS = frozenset({'account','id','room_id','sender_id','body','timestamp','origin','direction','type'})


class IPCError(Exception):
    pass


def text(value, maximum=8192, *, body=False):
    try:
        return (isinstance(value,str) and 0<len(value.encode('utf-8'))<=maximum
                and not any((ord(c)<32 and (not body or c not in '\n\t')) or ord(c)==127 for c in value))
    except UnicodeError:
        return False


def encode(value):
    raw = json.dumps(value,ensure_ascii=True,separators=(',',':'),allow_nan=False).encode('ascii') + b'\n'
    if len(raw)>MAX_FRAME:
        raise IPCError('frame_too_large')
    return raw


def decode(raw):
    if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_FRAME:
        raise IPCError('frame_size')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:
                raise IPCError('duplicate_key')
            result[key]=value
        return result
    def invalid(_):
        raise IPCError('invalid_constant')
    try:
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid)
    except (ValueError,UnicodeError,RecursionError):
        raise IPCError('invalid_json') from None
    if not isinstance(value,dict):
        raise IPCError('invalid_object')
    return value


class Lines:
    def __init__(self):
        self.buffer=bytearray()

    def feed(self,raw):
        if not isinstance(raw,bytes) or len(self.buffer)+len(raw)>MAX_FRAME*2:
            raise IPCError('buffer_limit')
        self.buffer.extend(raw)

    def pop(self):
        position=self.buffer.find(b'\n')
        if position<0:
            if len(self.buffer)>=MAX_FRAME:
                raise IPCError('frame_too_large')
            return None
        raw=bytes(self.buffer[:position+1])
        del self.buffer[:position+1]
        return decode(raw)


def message_valid(value,account,room):
    return (isinstance(value,dict) and set(value)==MESSAGE_FIELDS and value['account']==account
            and value['room_id']==room and text(value['id'],512) and text(value['sender_id'],256)
            and not value['sender_id'].endswith('@chatroom') and text(value['body'],body=True)
            and value['origin']=='live' and value['direction']=='incoming'
            and type(value['type']) is int and value['type']==1 and type(value['timestamp']) is int)


class Trial:
    """Root-side admission. A new permit never authorizes a different group."""
    def __init__(self,account,room,mode,marker=None,*,clock=time.time):
        if mode not in {'observe','live'} or not text(account,64) or not text(room,256) or not room.endswith('@chatroom'):
            raise IPCError('trial_config')
        if mode=='observe' and (not isinstance(marker,str) or not re.fullmatch(r'AWIPC-[0-9a-f]{16}',marker)):
            raise IPCError('trial_marker')
        self.account,self.room,self.mode,self.marker,self.clock=account,room,mode,marker,clock
        self.started=clock()
        self.source=None
        self.permit=None
        self.consumed=False

    def admit(self,value):
        if self.source is not None or not message_valid(value,self.account,self.room):
            raise IPCError('message_rejected')
        if not self.started-1<=value['timestamp']<=self.clock()+5:
            raise IPCError('message_expired')
        if (self.mode=='observe' and value['body']!=self.marker or self.mode=='live'
                and (not value['body'].startswith('/ai ') or not value['body'][4:].strip())):
            raise IPCError('trigger_rejected')
        self.source=dict(value)
        self.permit=secrets.token_hex(16)
        return {'kind':'message','data':dict(value),'permit':self.permit}

    def authorize(self,value):
        keys={'kind','requestId','sourceId','permit','text','key'}
        if (self.mode!='live' or self.source is None or self.consumed or not isinstance(value,dict)
                or set(value)!=keys or value['kind']!='send' or value['sourceId']!=self.source['id']
                or value['permit']!=self.permit or not isinstance(value['requestId'],str)
                or not HEX.fullmatch(value['requestId']) or not isinstance(value['key'],str)
                or not HEX.fullmatch(value['key']) or not text(value['text'],body=True)
                or self.clock()>self.source['timestamp']+180):
            raise IPCError('send_rejected')
        self.consumed=True  # Before persistence, IPC or HTTP; failures cannot retry.
        return dict(value)
