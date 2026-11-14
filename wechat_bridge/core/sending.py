"""Composed persistent outbound delivery over injected content and proof policies."""
import asyncio
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from .contracts import Receipt


@dataclass(frozen=True)
class PreparedSend:
    """Validated, protocol-independent delivery content and platform proof contract."""
    kind: str
    body: str = field(default='', repr=False)
    parts: tuple[str, ...] = field(default=(), repr=False)
    quote: str | None = None
    payload: dict | None = field(default=None, repr=False)
    proof: object | None = field(default=None, repr=False)
    timeout: float = 45


class DeliveryService:
    def __init__(self, messages, result):
        self.messages, self.result = messages, result
        self.namespace = self.state.cursor('onebot_namespace')
        if self.namespace is None:
            self.namespace = uuid.uuid4().hex
            self.state.cursor('onebot_namespace', self.namespace)
        self.send_lock = asyncio.Lock()
        self.last_sent = -math.inf
        self.late_text = {}
        register = getattr(self.backend,'set_late_text_handler',None)
        if register is not None: register(self.reconcile_text)

    def reconcile_text(self, part_key, destination, receipt):
        now=time.monotonic()
        self.late_text={k:v for k,v in self.late_text.items() if v['expires']>now}
        item=self.late_text.get(part_key)
        if (item is None or item['destination']!=destination or item['parts']!=1
                or not self.messages.policy.accepted(receipt,self.config.account,destination)):
            return False
        mid=self.state.identity('outgoing',receipt.send_id)[0]
        return self.state.finish_unknown(item['key'],item['digest'],item['native_key'],
            self.result(data={'message_id':mid}))

    def _finish_text(self,key,digest,answer):
        # A bound late receipt can CAS NULL to success while a cancelled await
        # is still unwinding. Never replace that proof with the old timeout.
        if answer['retcode']==1408:
            current=self.state.submission(key,digest)
            if current is not None and current.get('retcode')==0: answer=current
        self.state.finish(key,answer)
        if answer['retcode']==1408: self._hold_unknown()
        return answer

    def _hold_unknown(self):
        hold=getattr(self.backend,'hold_sends',None)
        if hold is not None: hold()

    @property
    def config(self):
        return self.messages.config

    @property
    def state(self):
        return self.messages.state

    @property
    def backend(self):
        return self.messages.backend

    @property
    def clock(self):
        return self.messages.clock

    @property
    def online(self):
        return self.messages.online

    async def send(self, destination, content, request):
        if content.kind != 'text':
            return await self._send_media(destination, content, request)
        output, parts = content.body, content.parts
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        echo = json.dumps(request.get('echo', uuid.uuid4().hex), sort_keys=True, ensure_ascii=True)
        # Echo counters may restart with AstrBot. Scope deduplication to the
        # actual source conversation so a later identical answer is not lost.
        trigger = content.quote or self.state.cursor('reply_trigger:' + destination) or 'none'
        key = hashlib.sha256((self.namespace + ':' + destination + ':' + trigger + ':' + echo).encode()).hexdigest()
        async with self.send_lock:
            cached = self.state.submission(key, digest)
            if cached is not None:
                self.backend.report('loop_guard', reason='duplicate_action')
                if cached.get('retcode') == 1408:
                    self._hold_unknown()
                    if hasattr(self.backend,'last_action_binding'):
                        self.backend.last_action_binding=dict(native_key=self.state.submission_native_key(key,digest),
                            destination=destination)
                    self.backend.report('action_pending',native_key=self.state.submission_native_key(key,digest),
                        destination=destination)
                return cached
            if getattr(self.backend,'send_hold',False):
                return self.result(1403,message='send_requires_review')
            if not self.online or not self.messages.reply_active(destination):
                return self.result(1403, message='no_active_private_conversation')
            # Backpressure waits instead of dropping the second reply.
            await asyncio.sleep(max(0, self.config.send_interval - (self.clock() - self.last_sent)))
            native_key = uuid.uuid4().hex
            self.state.reserve(key, digest, native_key)
            if hasattr(self.backend,'last_action_binding'):
                self.backend.last_action_binding=dict(native_key=native_key,destination=destination)
            confirmed = []
            answer = self.result(1408, message='outcome_unknown')
            try:
                for index, part in enumerate(parts):
                    if index:
                        await asyncio.sleep(self.config.send_interval)
                    part_key = hashlib.sha256((native_key + ':' + str(index)).encode()).hexdigest()[:32]
                    now=time.monotonic()
                    self.late_text={k:v for k,v in self.late_text.items() if v['expires']>now}
                    while len(self.late_text)>=32: del self.late_text[next(iter(self.late_text))]
                    self.late_text[part_key]=dict(key=key,digest=digest,native_key=native_key,
                        destination=destination,parts=len(parts),expires=now+300)
                    receipt = await asyncio.wait_for(self.backend.send_text(destination, part, part_key), timeout=45)
                    self.last_sent = self.clock()
                    # The platform policy validates terminal evidence and exact target binding.
                    if not self.messages.policy.accepted(receipt, self.config.account, destination):
                        if (not confirmed and isinstance(receipt, Receipt)
                                and receipt.account == self.config.account and receipt.destination == destination
                                and receipt.state == 'recovery_capacity' and receipt.send_id == ''
                                and receipt.client_id == '' and receipt.server_id == ''):
                            answer = self.result(1403, message='recovery_capacity')
                        break
                    confirmed.append(receipt.send_id)
                if len(confirmed) == len(parts):
                    mid = self.state.identity('outgoing', confirmed[-1])[0]
                    answer = self.result(data={'message_id': mid})
            except asyncio.CancelledError:
                answer=self._finish_text(key,digest,answer)
                self.backend.report('reply', success=answer['retcode']==0, parts=len(parts),
                    accepted=len(confirmed), destination=destination,retcode=answer['retcode'],native_key=native_key)
                raise
            except Exception:
                pass
            answer=self._finish_text(key,digest,answer)
            self.backend.report('reply', success=answer['retcode'] == 0, parts=len(parts), accepted=len(confirmed),
                                bytes=len(output.encode()), sha256=hashlib.sha256(output.encode()).hexdigest(), destination=destination,
                                retcode=answer['retcode'],native_key=native_key)
            return answer

    async def _send_media(self, destination, content, request):
        media, contract, image, quote = content.kind, content.proof, content.payload, content.quote
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        echo = json.dumps(request.get('echo', uuid.uuid4().hex), sort_keys=True, ensure_ascii=True)
        trigger = quote or self.state.cursor('reply_trigger:' + destination) or 'none'
        key = hashlib.sha256((self.namespace + ':' + destination + ':' + trigger + ':' + echo).encode()).hexdigest()
        async with self.send_lock:
            cached = self.state.submission(key, digest)
            if cached is not None:
                self.backend.report('loop_guard', reason='duplicate_action')
                if cached.get('retcode')==1408: self._hold_unknown()
                return cached
            if getattr(self.backend,'send_hold',False):
                return self.result(1403,message='send_requires_review')
            if not self.online or not self.messages.reply_active(destination):
                return self.result(1403, message='no_active_private_conversation')
            await asyncio.sleep(max(0, self.config.send_interval - (self.clock() - self.last_sent)))
            native_key = uuid.uuid4().hex
            self.state.reserve(key, digest, native_key)
            answer = self.result(1408, message='outcome_unknown')
            confirmed = False
            rejected = False
            try:
                sender = self.backend.send_image if media == 'image' else self.backend.send_voice
                receipt = await asyncio.wait_for(sender(destination, image, native_key),
                    content.timeout)
                self.last_sent = self.clock()
                if contract.accepted(receipt, self.config.account, destination, image['sha256']):
                    mid = self.state.identity('outgoing', media + ':' + receipt['job_id'])[0]
                    answer = self.result(data={'message_id': mid})
                    confirmed = True
                elif (contract.receipt_bound(receipt, self.config.account, destination, image['sha256'])
                      and receipt['state'] in {'rejected', 'recovery_capacity'}):
                    answer = (self.result(1403, message='recovery_capacity') if receipt['state'] == 'recovery_capacity'
                              else self.result(1400, message='unsupported_message_segment'))
                    rejected = True
            except asyncio.CancelledError:
                self.state.finish(key, answer)
                if answer['retcode']==1408: self._hold_unknown()
                self.backend.report('reply', success=False, parts=1, accepted=0, destination=destination,
                                    media=media, bytes=image['bytes'], sha256=image['sha256'])
                raise
            except Exception:
                pass
            self.state.finish(key, answer)
            if answer['retcode']==1408: self._hold_unknown()
            if rejected:
                self.backend.report(media + '_output', outcome='rejected')
            else:
                self.backend.report('reply', success=confirmed, parts=1, accepted=int(confirmed), destination=destination,
                                    media=media, bytes=image['bytes'], sha256=image['sha256'])
            return answer
