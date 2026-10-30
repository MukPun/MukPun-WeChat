"""One acknowledged inbound image at a time; controllers never assemble it."""
import base64
import binascii
import hashlib
import importlib.util
from pathlib import Path
import re
import time
try:
    from . import image_input as images
except ImportError:
    spec=importlib.util.spec_from_file_location('_input_wire_images',Path(__file__).with_name('image_input.py'))
    images=importlib.util.module_from_spec(spec);spec.loader.exec_module(images)

CHUNK_BYTES=65535  # Divisible by three: slice canonical base64 without another full decode.
CHUNK_CHARS=CHUNK_BYTES//3*4
TIMEOUT=180
FRAMES=frozenset({'input_image_begin','input_image_chunk','input_image_commit','input_image_ack'})
HEX=re.compile('[0-9a-f]{32}\\Z')


def require(condition):
    if not condition:raise ValueError('input_image_transfer')


def begin_header(frame):
    require(isinstance(frame,dict) and set(frame)=={'kind','transferId','event','image'}
            and frame['kind']=='input_image_begin' and isinstance(frame['transferId'],str)
            and HEX.fullmatch(frame['transferId']))
    images.validate_metadata(frame['image'])
    event=frame['event']
    require(isinstance(event,dict) and set(event) in ({'kind','cursor','data'},{'kind','cursor','data','anchor'})
            and event['kind']=='event' and isinstance(event['data'],dict) and 'image' not in event['data'])
    return {**event,'data':{**event['data'],'image':frame['image']}}


def chunk_bytes(frame,index,remaining):
    require(set(frame)=={'kind','transferId','index','data'} and frame['kind']=='input_image_chunk'
            and type(frame['index']) is int and frame['index']==index and isinstance(frame['data'],str)
            and 0<len(frame['data'])<=CHUNK_CHARS)
    try:raw=base64.b64decode(frame['data'],validate=True)
    except (ValueError,binascii.Error):raise ValueError('input_image_transfer') from None
    require(len(raw)==min(CHUNK_BYTES,remaining) and base64.b64encode(raw).decode()==frame['data'])
    return raw


class Producer:
    def __init__(self,event,identity):
        require(isinstance(identity,str) and HEX.fullmatch(identity))
        self.image=event['data']['image'];images.validate(self.image)
        self.identity=identity;self.index=-1;self.offset=0
        self.event={**event,'data':{k:v for k,v in event['data'].items() if k!='image'}}
        self.started=False;self.done=False
    def begin(self):
        require(not self.started);self.started=True
        return {'kind':'input_image_begin','transferId':self.identity,'event':self.event,'image':images.metadata(self.image)}
    def ack(self,value):
        require(self.started and not self.done and set(value)=={'kind','transferId','index'}
                and value['kind']=='input_image_ack' and value['transferId']==self.identity
                and type(value['index']) is int and value['index']==self.index)
        if self.offset==len(self.image['base64']):
            self.done=True;self.image=None
            return {'kind':'input_image_commit','transferId':self.identity}
        self.index+=1
        data=self.image['base64'][self.offset:self.offset+CHUNK_CHARS];self.offset+=len(data)
        return {'kind':'input_image_chunk','transferId':self.identity,'index':self.index,'data':data}


class Receiver:
    def __init__(self,emit,*,validate=lambda _:None,clock=time.monotonic):
        self.emit,self.validate,self.clock=emit,validate,clock;self.state=None
    @property
    def active(self):return self.state is not None
    def check_deadline(self):
        if self.state is not None:require(self.clock()<self.state['deadline'])
    def feed(self,frame):
        self.check_deadline()
        if frame.get('kind')=='input_image_begin':
            require(not self.active);header=begin_header(frame);self.validate(header)
            self.state={'frame':frame,'header':header,'raw':bytearray(),'hash':hashlib.sha256(),'index':0,'deadline':self.clock()+TIMEOUT}
            self.emit({'kind':'input_image_ack','transferId':frame['transferId'],'index':-1});return None
        require(self.active and frame.get('transferId')==self.state['frame']['transferId'])
        s=self.state;meta=s['frame']['image']
        if frame.get('kind')=='input_image_chunk':
            raw=chunk_bytes(frame,s['index'],meta['bytes']-len(s['raw']))
            s['raw'].extend(raw);s['hash'].update(raw);s['index']+=1
            self.emit({'kind':'input_image_ack','transferId':frame['transferId'],'index':frame['index']});return None
        require(set(frame)=={'kind','transferId'} and frame['kind']=='input_image_commit'
                and len(s['raw'])==meta['bytes'] and s['hash'].hexdigest()==meta['sha256'])
        image=images.payload(bytes(s['raw']),meta['mime'],meta['quality'])
        result={**s['header'],'data':{**s['header']['data'],'image':image}}
        self.state=None
        return result


class Relay:
    def __init__(self,*,clock=time.monotonic):self.state=None;self.clock=clock
    @property
    def active(self):return self.state is not None
    def check_deadline(self):
        if self.active:require(self.clock()<self.state['deadline'])
    def forward_begin(self,peers):
        require(self.active and not self.state['forwarded'])
        self.state['forwarded']=True;peers['host'].send(self.state['frame'])
    def complete(self,cursor):
        if self.active:
            require(self.state['committed'] and cursor==self.state['frame']['event']['cursor'])
            self.state=None
    def handle(self,name,frame,peers):
        self.check_deadline();kind=frame.get('kind')
        if kind=='input_image_begin':
            require(name=='native' and not self.active);header=begin_header(frame)
            self.state={'frame':frame,'index':0,'bytes':0,'hash':hashlib.sha256(),'ack':-1,
                        'forwarded':False,'committed':False,'deadline':self.clock()+TIMEOUT}
            return header
        require(self.active and frame.get('transferId')==self.state['frame']['transferId'])
        s=self.state
        require(s['forwarded'] and not s['committed'])
        if kind=='input_image_ack':
            require(name=='host' and set(frame)=={'kind','transferId','index'}
                    and type(frame['index']) is int and frame['index']==s['ack'])
            s['ack']=None;peers['native'].send(frame);return None
        require(name=='native' and s['ack'] is None)
        if kind=='input_image_chunk':
            raw=chunk_bytes(frame,s['index'],s['frame']['image']['bytes']-s['bytes'])
            s['bytes']+=len(raw);s['hash'].update(raw);s['index']+=1;s['ack']=frame['index']
        else:
            require(kind=='input_image_commit' and set(frame)=={'kind','transferId'}
                    and s['bytes']==s['frame']['image']['bytes'] and s['hash'].hexdigest()==s['frame']['image']['sha256'])
            s['committed']=True
        peers['host'].send(frame)
        return None
