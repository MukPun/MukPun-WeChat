"""Durable private input and bounded anonymous-pipe Native sender."""
import asyncio
import base64
import secrets
import time

from .protocol import NativeMessage, Receipt
from .forward_wire import WireError
from .continuous import text, MAX_TEXT, native_message, valid_content
from .group_trigger import admitted, group_id
from . import image_output as output_images
import json
import re


class Gateway:
    online = True

    def __init__(self, account, peer, state, generation, emit, *, group=None, prefix='/ai ',
                 require_activation=False, wake_mode='prefix', self_sender_id=None, image_input=False,
                 group_context=False, image_output=False, all_groups=False):
        self.account, self.peer, self.state, self.generation, self.emit = account, peer, state, generation, emit
        self.wake = asyncio.Event()
        self.futures = {}
        self.claimed = None
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
        if type(group_context) is not bool:
            raise WireError('group_context_policy')
        self.group_context = group_context
        if type(all_groups) is not bool:
            raise WireError('all_groups_policy')
        self.all_groups = all_groups
        self.destinations = {peer} | ({group} if group else set())
        self.waiting_destination = None
        self.waiting_kind = None
        self.waiting_image_sha256 = None
        self.chunk_ack = None
        self.chunk_index = None
        self.activation = asyncio.Event()
        if not require_activation:
            self.activation.set()

    def report(self, stage, **fields):
        self.emit({'kind': 'report', 'stage': stage, **fields})

    def feed(self, value):
        if value == {'kind': 'activate'}:
            self.activation.set()
        elif value.get('kind') == 'event' and set(value) in ({'kind', 'cursor', 'data'}, {'kind', 'cursor', 'data', 'anchor'}):
            data = value['data']
            if (not isinstance(data, dict) or data.get('account') != self.account
                    or not self._destination(data.get('room_id'))
                    or data.get('direction') != 'incoming' or data.get('origin') != 'live'
                    or not text(data.get('id'), 512) or type(data.get('timestamp')) is not int
                    or not text(data.get('sender_id'), 256) or data['sender_id'].endswith('@chatroom')
                    or data['sender_id'] == self.self_sender_id
                    or data['room_id'] == self.peer and data['sender_id'] != self.peer
                    or type(data.get('mentioned', False)) is not bool
                    or not self.all_groups and data['room_id'] == self.group and not admitted(data.get('body'), data.get('mentioned', False),
                        self.wake_mode, self.prefix, context=self.group_context, image='image' in data)):
                raise WireError('source_binding')
            if not valid_content(native_message(data), self.image_input):
                raise WireError('source_content')
            inserted = self.state.admit(data['id'], data)
            if not inserted:
                self.report('loop_guard', reason='duplicate_input')
            self.checkpoint(value)
            self.emit({'kind': 'ack', 'cursor': value['cursor']})
            self.wake.set()
        elif value.get('kind') == 'checkpoint' and set(value) == {'kind', 'cursor', 'anchor'}:
            self.checkpoint(value)
            self.emit({'kind': 'ack', 'cursor': value['cursor']})
        elif value.get('kind') == 'receipt' and set(value) == {'kind', 'requestId', 'receipt'}:
            future = self.futures.get(value['requestId'])
            receipt = Receipt(**value['receipt'])
            if (future is None or future.done() or self.waiting_kind != 'text'
                    or receipt.destination != self.waiting_destination or receipt.account != self.account):
                raise WireError('receipt_binding')
            future.set_result(receipt)
        elif value.get('kind') == 'image_chunk_ack' and set(value) == {'kind', 'requestId', 'index'}:
            if (value['requestId'] not in self.futures or self.waiting_kind != 'image'
                    or self.chunk_ack is None or self.chunk_ack.done()
                    or type(value['index']) is not int or value['index'] != self.chunk_index):
                raise WireError('image_chunk_ack_binding')
            self.chunk_ack.set_result(True)
        elif value.get('kind') == 'image_receipt' and set(value) == {'kind', 'requestId', 'receipt'}:
            future = self.futures.get(value['requestId'])
            receipt = value['receipt']
            if (future is None or future.done() or self.waiting_kind != 'image'
                    or not output_images.receipt_bound(receipt, self.account, self.waiting_destination,
                                                       self.waiting_image_sha256)):
                raise WireError('image_receipt_binding')
            future.set_result(receipt)
        elif value == {'kind': 'stop'}:
            self.online = False
            self.wake.set()
            self.activation.set()
        else:
            raise WireError('frame')

    def checkpoint(self, value):
        cursor = value['cursor']
        if not isinstance(cursor, str) or not re.fullmatch(r'0|[1-9][0-9]{0,18}', cursor) or int(cursor) > 2**63 - 1:
            raise WireError('cursor')
        anchor = value.get('anchor')
        if anchor is not None and (not isinstance(anchor, str) or not re.fullmatch('[0-9a-f]{64}', anchor)):
            raise WireError('anchor')
        saved = json.dumps({'cursor': cursor, 'anchor': anchor}, separators=(',', ':')) if anchor else cursor
        self.state.cursor(self.generation, saved)

    async def next_message(self):
        await self.activation.wait()
        while self.online:
            self.wake.clear()
            row = self.state.next_pending()
            if row:
                if 'image' in row[1] and not -5 <= time.time() - row[1]['timestamp'] <= 300:
                    self.state.expire(row[0])
                    self.report('image_input', outcome='expired')
                    continue
                return native_message(row[1])
            await self.wake.wait()
        raise WireError('stopped')

    async def send_text(self, destination, text, key):
        if not self._destination(destination) or not self.online or self.futures:
            raise WireError('destination')
        identity = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.futures[identity] = future
        self.waiting_destination = destination
        self.waiting_kind = 'text'
        try:
            self.emit({'kind': 'send', 'requestId': identity, 'destination': destination, 'text': text, 'key': key})
            return await asyncio.wait_for(future, 43)
        finally:
            del self.futures[identity]
            self.waiting_destination = None
            self.waiting_kind = None

    async def send_image(self, destination, image, key):
        if not self.image_output or not self._destination(destination) or not self.online or self.futures:
            raise WireError('image_destination')
        output_images.validate(image)
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{32}', key):
            raise WireError('image_key')
        identity = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.futures[identity] = future
        self.waiting_destination = destination
        self.waiting_kind = 'image'
        self.waiting_image_sha256 = image['sha256']
        try:
            async with asyncio.timeout(48):
                if image['bytes'] <= 128 * 1024:
                    self.emit({'kind': 'send_image', 'requestId': identity, 'destination': destination, 'image': image, 'key': key})
                else:
                    async def transfer(frame, index):
                        self.chunk_index = index
                        self.chunk_ack = asyncio.get_running_loop().create_future()
                        self.emit(frame)
                        done, _ = await asyncio.wait((self.chunk_ack, future), return_when=asyncio.FIRST_COMPLETED)
                        return future not in done
                    if not await transfer({'kind': 'image_begin', 'requestId': identity, 'destination': destination,
                                           'image': output_images.metadata(image), 'key': key}, -1):
                        return future.result()
                    raw = base64.b64decode(image['base64'], validate=True)
                    for index, offset in enumerate(range(0, len(raw), output_images.CHUNK_BYTES)):
                        if not await transfer({'kind': 'image_chunk', 'requestId': identity, 'index': index,
                            'data': base64.b64encode(raw[offset:offset + output_images.CHUNK_BYTES]).decode('ascii')}, index):
                            return future.result()
                    self.chunk_ack = self.chunk_index = None
                    self.emit({'kind': 'image_commit', 'requestId': identity})
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
        return destination in self.destinations or self.all_groups and group_id(destination)
