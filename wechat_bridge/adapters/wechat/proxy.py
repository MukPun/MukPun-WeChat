"""Durable private input and bounded anonymous-pipe Native sender."""
import asyncio
from contextlib import asynccontextmanager
import base64
import secrets
import time

from ...core.contracts import Receipt
from ...forward_wire import WireError, recovery_protection
from .messages import text, native_message, valid_content
from .policy import WeChatPolicy
from ...core.delivery import DurableInbox
from ... import image_output as output_images
from ... import voice_output as output_voice
from ... import image_input as input_images
from ...image_input_wire import Receiver as InputReceiver
import re


class WeChatIPCBackend(DurableInbox):
    online = True
    TEXT_TIMEOUT = 43
    LATE_SECONDS = 300
    LATE_LIMIT = 32

    def __init__(self, account, peer, state, generation, emit, *, group=None, prefix='/ai ',
                 require_activation=False, wake_mode='prefix', self_sender_id=None, image_input=False,
                 group_context=False, image_output=False, all_groups=False, voice_output=False, image_files=None):
        self.account, self.peer, self.state, self.generation, self.emit = account, peer, state, generation, emit
        self.stopped_error = WireError
        self.wake = asyncio.Event()
        self.futures = {}
        self.retired_text = {}
        self.active_text = None
        self.late_text_handler = None
        self.last_action_binding = None
        self.claimed = None
        self.recovery_pending = None
        self.group, self.prefix = group, prefix
        self.wake_mode = wake_mode
        if self_sender_id is not None and (not text(self_sender_id, 256) or self_sender_id == peer or self_sender_id.endswith('@chatroom')):
            raise WireError('self_identity')
        self.self_sender_id = self_sender_id
        if type(image_input) is not bool:
            raise WireError('image_policy')
        self.image_input = image_input
        if type(image_output) is not bool:
            raise WireError('image_output_policy')
        self.image_output = image_output
        if type(voice_output) is not bool:
            raise WireError("voice_output_policy")
        self.voice_output = voice_output
        if type(group_context) is not bool:
            raise WireError('group_context_policy')
        self.group_context = group_context
        if type(all_groups) is not bool:
            raise WireError('all_groups_policy')
        self.all_groups = all_groups
        self.destinations = {peer} | ({group} if group else set())
        self.policy = WeChatPolicy(account, peer, group=group, prefix=prefix, wake_mode=wake_mode,
                                  self_sender_id=self_sender_id, group_context=group_context,
                                  all_groups=all_groups)
        self.waiting_destination = None
        self.waiting_kind = None
        self.waiting_image_sha256 = None
        self.chunk_ack = None
        self.chunk_index = None
        self.image_files = image_files
        self.input_receiver = InputReceiver(self.emit, validate=lambda header:self._check_source(header["data"]))
        self.capture_token = None
        self.capture_ack = False
        self.actions_inflight = 0
        self.send_hold = False
        self.action_admission = asyncio.Event()
        self.action_admission.set()
        self.activation_requested = not require_activation
        self.activation = asyncio.Event()
        if not require_activation:
            self.activation.set()

    def report(self, stage, **fields):
        self.emit({'kind': 'report', 'stage': stage, **fields})

    def _gates(self):
        if self.activation_requested and not self.capture_token and not self.send_hold:
            self.activation.set()
            self.wake.set()
        else:
            self.activation.clear()
        if not self.capture_token or not self.online:
            self.action_admission.set()
        else:
            self.action_admission.clear()

    def hold_sends(self):
        # Close delivery locally before any timeout response/report can yield
        # to the WS consumer. Only a bound root decision may release the hold.
        self.send_hold=True
        self._gates()

    def publication_finished(self):
        if (self.capture_token and not self.capture_ack and not self.actions_inflight
                and not self.futures and not self.input_receiver.active
                and self.state.db.execute("SELECT 1 FROM inbox WHERE state='publishing' LIMIT 1").fetchone() is None):
            self.capture_ack = True
            self.emit({'kind':'capture_paused','token':self.capture_token})

    @asynccontextmanager
    async def action_slot(self):
        await self.action_admission.wait()
        self.actions_inflight += 1
        try:
            yield
        finally:
            self.actions_inflight -= 1
            self.publication_finished()

    def feed(self, value):
        if value.get('kind')=='capture_fence':
            if (value!={'kind':'capture_fence','token':self.capture_token}
                    or self.capture_token is None or not self.capture_ack
                    or self.actions_inflight or self.futures or self.input_receiver.active):
                raise WireError('capture_binding')
            # FIFO fence covers ingress after the first action/publication ACK.
            self.emit({'kind':'capture_fenced','token':self.capture_token})
            return
        if value.get('kind') in {'capture_pause','capture_resume'}:
            if set(value) != {'kind','token'} or not isinstance(value['token'],str) or not re.fullmatch('[0-9a-f]{32}',value['token']):
                raise WireError('capture_binding')
            if value['kind']=='capture_pause':
                if self.capture_token is not None: raise WireError('capture_binding')
                if self.actions_inflight:
                    # Native must stay online until this action completes.
                    # Defer capture rather than extending any existing deadline.
                    self.emit({'kind':'capture_deferred','token':value['token']})
                    return
                self.capture_token=value['token']; self.capture_ack=False
                self._gates(); self.publication_finished()
            else:
                if self.capture_token != value['token']: raise WireError('capture_binding')
                self.capture_token=None; self.capture_ack=False
                self._gates()
            return
        if value.get('kind')=='send_hold':
            if set(value)!={'kind','hold'} or type(value['hold']) is not bool: raise WireError('send_hold')
            self.send_hold=value['hold']; self._gates()
            return
        if value.get('kind') == 'recovery_protection':
            protection = recovery_protection({k:v for k,v in value.items() if k != 'kind'})
            self.state.protect_recovery(protection['floorSeq'], protection['epoch'])
            if self.recovery_pending is not None:
                # Already externally validated and staged. Never feed a private
                # file descriptor through the external inline-only validator.
                self._admit(self.recovery_pending,self.recovery_pending['data'])
            return
        if value.get('kind') in {'input_image_begin','input_image_chunk','input_image_commit'}:
            if not self.image_input:
                raise WireError('image_policy')
            completed = self.input_receiver.feed(value)
            if completed is not None:
                self.feed(completed)
                self.publication_finished()
            return
        if value == {'kind': 'activate'}:
            self.activation_requested=True
            self._gates()
        elif value.get('kind') == 'event' and set(value) in ({'kind', 'cursor', 'data'}, {'kind', 'cursor', 'data', 'anchor'}):
            if self.recovery_pending is not None:
                raise WireError('recovery_backpressure')
            data = value['data']
            self._check_source(data)
            if 'image' in data:
                input_images.validate(data['image'])
            if not valid_content(native_message(data), self.image_input):
                raise WireError('source_content')
            if 'image' in data and self.image_files is not None:
                try:
                    data = {**data, 'image':self.image_files.stage(data['image'])}
                except input_images.ImageError:
                    self.report('image_input', outcome='unavailable')
                    if data.get('body'):
                        data = {k:v for k,v in data.items() if k!='image'}
                        data['type'] = 1
                    else:
                        self.checkpoint(value)
                        self.emit({'kind':'ack','cursor':value['cursor']})
                        return
            self._admit(value,data)
        elif value.get('kind') == 'checkpoint' and set(value) == {'kind', 'cursor', 'anchor'}:
            self.checkpoint(value)
            self.emit({'kind': 'ack', 'cursor': value['cursor']})
        elif value.get('kind') == 'receipt' and set(value) == {'kind', 'requestId', 'receipt'}:
            future = self.futures.get(value['requestId'])
            receipt = Receipt(**value['receipt'])
            self._prune_retired()
            if (future is not None and future.cancelled() and self.waiting_kind=='text'
                    and self.active_text is not None and self.active_text['requestId']==value['requestId']):
                self._retire_text(value['requestId'],self.active_text['key'],self.waiting_destination)
                future=None
            if future is None and value['requestId'] in self.retired_text:
                retired = self.retired_text[value['requestId']]
                if (receipt.account != self.account or receipt.destination != retired['destination']
                        or retired.get('receipt') is not None and retired['receipt'] != receipt):
                    raise WireError('receipt_binding')
                retired['receipt'] = receipt
                reconciled = bool(self.late_text_handler and
                    self.late_text_handler(retired['key'], retired['destination'], receipt))
                self.report('late_text_receipt', reconciled=reconciled, requestId=value['requestId'],
                    part_key=retired['key'], destination=retired['destination'], send_id=receipt.send_id)
                return
            if (future is None or future.done() or self.waiting_kind != 'text'
                    or receipt.destination != self.waiting_destination or receipt.account != self.account):
                raise WireError('receipt_binding')
            future.set_result(receipt)
        elif value.get('kind') in {'image_chunk_ack', 'voice_chunk_ack'} and set(value) == {'kind', 'requestId', 'index'}:
            if (value['requestId'] not in self.futures or self.waiting_kind not in ('image', 'voice')
                    or value['kind'] != self.waiting_kind + '_chunk_ack'
                    or self.chunk_ack is None or self.chunk_ack.done()
                    or type(value['index']) is not int or value['index'] != self.chunk_index):
                raise WireError('image_chunk_ack_binding')
            self.chunk_ack.set_result(True)
        elif value.get('kind') in {'image_receipt', 'voice_receipt'} and set(value) == {'kind', 'requestId', 'receipt'}:
            future = self.futures.get(value['requestId'])
            receipt = value['receipt']
            if (future is None or future.done() or self.waiting_kind not in ('image', 'voice')
                    or value['kind'] != self.waiting_kind + '_receipt'
                    or not output_images.receipt_bound(receipt, self.account, self.waiting_destination,
                                                       self.waiting_image_sha256)):
                raise WireError('image_receipt_binding')
            future.set_result(receipt)
        elif value == {'kind': 'stop'}:
            self.online = False
            self.action_admission.set()
            self.wake.set()
            self.activation.set()
        else:
            raise WireError('frame')

    def _check_source(self, data):
        return self.policy.check_source(data)

    @staticmethod
    def _checkpoint_value(value):
        return WeChatPolicy.checkpoint(value)

    async def send_text(self, destination, text, key):
        if not self._destination(destination) or not self.online or self.futures:
            raise WireError('destination')
        identity = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.futures[identity] = future
        self.waiting_destination = destination
        self.waiting_kind = 'text'
        self.active_text=dict(requestId=identity,key=key)
        try:
            self.emit({'kind': 'send', 'requestId': identity, 'destination': destination, 'text': text, 'key': key})
            return await asyncio.wait_for(future, self.TEXT_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            self._retire_text(identity,key,destination)
            raise
        finally:
            del self.futures[identity]
            self.waiting_destination = None
            self.waiting_kind = None
            self.active_text=None

    def _retire_text(self,identity,key,destination):
        self._prune_retired()
        if identity in self.retired_text: return
        while len(self.retired_text)>=self.LATE_LIMIT:
            del self.retired_text[next(iter(self.retired_text))]
        self.retired_text[identity]=dict(key=key,destination=destination,
            expires=time.monotonic()+self.LATE_SECONDS)

    def _prune_retired(self):
        now=time.monotonic()
        self.retired_text={key:value for key,value in self.retired_text.items() if value['expires']>now}

    def set_late_text_handler(self, handler):
        self.late_text_handler = handler

    async def send_image(self, destination, image, key):
        return await self._send_media(destination, image, key, 'image')

    async def send_voice(self, destination, audio, key):
        return await self._send_media(destination, audio, key, 'voice')

    async def _send_media(self, destination, image, key, media):
        contract = output_images if media == 'image' else output_voice
        enabled = self.image_output if media == 'image' else self.voice_output
        if not enabled or not self._destination(destination) or not self.online or self.futures:
            raise WireError('image_destination')
        contract.validate(image)
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{32}', key):
            raise WireError('image_key')
        identity = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.futures[identity] = future
        self.waiting_destination = destination
        self.waiting_kind = media
        self.waiting_image_sha256 = image['sha256']
        try:
            async with asyncio.timeout(contract.transfer_window(image) if media == 'image' else 175):
                if image['bytes'] <= 128 * 1024:
                    self.emit({'kind': 'send_' + media, 'requestId': identity, 'destination': destination, media: image, 'key': key})
                else:
                    async def transfer(frame, index):
                        self.chunk_index = index
                        self.chunk_ack = asyncio.get_running_loop().create_future()
                        self.emit(frame)
                        done, _ = await asyncio.wait((self.chunk_ack, future), return_when=asyncio.FIRST_COMPLETED)
                        return future not in done
                    if not await transfer({'kind': media + '_begin', 'requestId': identity, 'destination': destination,
                                           media: contract.metadata(image), 'key': key}, -1):
                        return future.result()
                    raw = base64.b64decode(image['base64'], validate=True)
                    for index, offset in enumerate(range(0, len(raw), contract.CHUNK_BYTES)):
                        if not await transfer({'kind': media + '_chunk', 'requestId': identity, 'index': index,
                            'data': base64.b64encode(raw[offset:offset + contract.CHUNK_BYTES]).decode('ascii')}, index):
                            return future.result()
                    self.chunk_ack = self.chunk_index = None
                    self.emit({'kind': media + '_commit', 'requestId': identity})
                return await future
        finally:
            if self.chunk_ack is not None and not self.chunk_ack.done():
                self.chunk_ack.cancel()
            self.chunk_ack = self.chunk_index = None
            del self.futures[identity]
            self.waiting_destination = None
            self.waiting_kind = None
            self.waiting_image_sha256 = None

    def _destination(self, destination):
        return self.policy.destination(destination)
