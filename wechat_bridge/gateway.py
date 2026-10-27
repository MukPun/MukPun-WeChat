"""Host-side asynchronous IPC backend. No Native API token or network access."""
import asyncio
import secrets

from .ipc import HEX, IPCError, encode, message_valid
from .protocol import NativeMessage, Receipt


class Gateway:
    online=True

    def __init__(self,account,room,emit):
        self.account,self.room,self.emit=account,room,emit
        self.incoming=asyncio.Queue(maxsize=4)
        self.permits={}
        self.futures={}
        self.bridge=None
        self.last_request=None
        self.reported=False

    def feed(self,value):
        if value.get('kind')=='message' and set(value)=={'kind','data','permit'}:
            data,permit=value['data'],value['permit']
            if (not message_valid(data,self.account,self.room) or not isinstance(permit,str)
                    or not HEX.fullmatch(permit) or len(self.permits)>=4 or self.incoming.full()):
                raise IPCError('message_rejected')
            self.permits[data['id']]=permit
            self.incoming.put_nowait(NativeMessage(**data))
        elif value.get('kind')=='receipt' and set(value)=={'kind','requestId','receipt'}:
            future=self.futures.get(value['requestId'])
            if future is None or future.done():
                raise IPCError('unknown_request')
            receipt=Receipt(**value['receipt'])
            if receipt.account!=self.account or receipt.destination!=self.room:
                raise IPCError('receipt_mismatch')
            future.set_result(receipt)
        elif value=={'kind':'stop'}:
            self.online=False
            for future in self.futures.values():
                if not future.done():
                    future.set_exception(IPCError('parent_stopped'))
        else:
            raise IPCError('frame_rejected')

    async def next_message(self):
        return await self.incoming.get()

    async def send_text(self,room,body,key):
        if not self.online or room!=self.room or self.bridge is None or self.bridge.pending is None:
            raise IPCError('send_rejected')
        source=self.bridge.pending['native_id']
        permit=self.permits.pop(source,None)
        if permit is None:
            raise IPCError('permit_missing')
        identity=secrets.token_hex(16)
        self.last_request=identity
        future=asyncio.get_running_loop().create_future()
        self.futures[identity]=future
        try:
            self.emit({'kind':'send','requestId':identity,'sourceId':source,'permit':permit,'text':body,'key':key})
            return await asyncio.wait_for(future,timeout=43)
        finally:
            self.futures.pop(identity,None)
