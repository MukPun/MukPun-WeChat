"""Bounded metadata-only source reuse, bound to one Native lifetime."""
import re
import uuid

MAX_ENTRIES, TTL = 64, 86400


def valid_id(value):
    try:
        return (isinstance(value,str) and value.startswith('upload_')
                and str(uuid.UUID(value[7:]))==value[7:] and uuid.UUID(value[7:]).version==4)
    except ValueError:
        return False


class Cache:
    def __init__(self,value,epoch,now):
        if not isinstance(epoch,str) or not re.fullmatch('[0-9]+:[0-9]+',epoch):
            raise ValueError('source_cache_epoch')
        self.epoch,self.now,self.entries=epoch,now,{}
        if value is None:return
        if (not isinstance(value,dict) or set(value)!={'schemaVersion','epoch','entries'} or value['schemaVersion']!=1
                or not isinstance(value['entries'],dict) or len(value['entries'])>MAX_ENTRIES):
            raise ValueError('source_cache_shape')
        for sha,entry in value['entries'].items():self.validate(sha,entry)
        if value['epoch']==epoch:
            self.entries={k:dict(v) for k,v in value['entries'].items() if 0<=now()-v['used']<=TTL}

    @staticmethod
    def validate(sha,entry):
        if (not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha)
                or not isinstance(entry,dict) or set(entry)!={'sourceId','bytes','mime','used'}
                or not valid_id(entry['sourceId']) or type(entry['bytes']) is not int or not 0<entry['bytes']<=8*1024*1024
                or entry['mime'] not in {'image/png','image/jpeg','image/webp'}
                or type(entry['used']) is not int or entry['used']<0):raise ValueError('source_cache_entry')

    def lookup(self,image):
        v=self.entries.get(image['sha256'])
        if v and v['bytes']==image['bytes'] and v['mime']==image['mime'] and 0<=self.now()-v['used']<=TTL:
            v['used']=int(self.now());return v['sourceId']
        return None

    def put(self,image,identity):
        v={'sourceId':identity,'bytes':image['bytes'],'mime':image['mime'],'used':int(self.now())}
        self.validate(image['sha256'],v);self.entries[image['sha256']]=v
        while len(self.entries)>MAX_ENTRIES:del self.entries[min(self.entries,key=lambda k:self.entries[k]['used'])]

    def discard(self,sha):self.entries.pop(sha,None)

    def value(self):return {'schemaVersion':1,'epoch':self.epoch,'entries':self.entries}
