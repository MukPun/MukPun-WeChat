"""One-shot compatibility engine; continuous production uses composed services."""
import asyncio
import hashlib
import json
import math
import time
import uuid
from ...core.contracts import NativeMessage, Receipt
from ...core.text import text
from ...onebot.codec import number, result
from ...onebot.events import meta, legacy_message, EventCodec
from ...onebot.content import legacy_text
from ...onebot.registry import ActionRegistry
from ...onebot.frames import handle_frame, reply_sent
from .policy import WeChatPolicy

class Bridge:
    handle_frame = handle_frame
    reply_sent = staticmethod(reply_sent)

    def __init__(self, config, state, backend, *, clock=time.time):
        self.config, self.state, self.backend, self.clock = config, state, backend, clock
        self.self_id = state.identity('bot', config.self_native_id)[0]
        self.group_id = state.identity('group', config.room_id)[0] if config.room_id else None
        self.connection = None
        self.started = clock()
        self.pending = None
        self.last_sent = -math.inf
        self.send_lock = asyncio.Lock()

    def connected(self):
        self.connection = uuid.uuid4().hex
        self.pending = None
        self.started = self.clock()

    def disconnected(self):
        self.connection = None
        self.pending = None

    @property
    def online(self):
        return self.connection is not None and self.backend.online is True

    def meta(self, subtype):
        return meta(self, subtype)

    def ingest(self, message):
        c = self.config
        if (not self.online or not isinstance(message, NativeMessage) or message.account != c.account
                or c.room_id is None or message.room_id != c.room_id
                or message.origin != 'live' or message.direction != 'incoming'
                or type(message.type) is not int or message.type != 1
                or not text(message.id, 512) or not text(message.sender_id, 256)
                or message.sender_id.endswith('@chatroom') or message.sender_id == c.self_native_id
                or not text(message.body) or not message.body.startswith('/ai ')
                or not message.body[4:].strip() or type(message.timestamp) is not int
                or not self.started - 1 <= message.timestamp <= self.clock() + 5):
            return None
        if self.pending is not None and self.clock() > self.pending['deadline']:
            self.pending = None
        # One pending question per group: never replace its active destination.
        if self.pending is not None:
            return None
        mid, created = self.state.identity('incoming', message.id)
        if not created:
            return None
        uid = self.state.identity('user', message.sender_id)[0]
        body = message.body[4:].strip()
        event = legacy_message(self, message, mid, uid, body)
        self.pending = {'event': event, 'native_id': message.id, 'deadline': self.clock() + c.reply_seconds, 'claimed': False}
        return event

    result = staticmethod(result)

    def _group(self, params):
        if self.group_id is None or number(params.get('group_id')) != self.group_id:
            raise ValueError('group_not_allowed')
        return self.group_id

    def _group_name(self, group):
        return self.config.room_name

    def _sender(self, group, user, message=None):
        return EventCodec.sender(self, group, user, message)

    def _group_list(self):
        return [] if self.group_id is None else [{'group_id': self.group_id, 'group_name': self.config.room_name}]

    def _message_text(self, params):
        return legacy_text(self, params)

    def _accepted(self, receipt):
        return WeChatPolicy.accepted(receipt, self.config.account, self.config.room_id)

    async def _send(self, action, params, request):
        self._group(params)
        allowed = {'group_id', 'message', 'auto_escape', 'self_id'}
        if action == 'send_msg':
            allowed.add('message_type')
            if params.get('message_type', 'group') != 'group':
                raise ValueError('private_not_supported')
        if not set(params) <= allowed or 'auto_escape' in params and type(params['auto_escape']) is not bool:
            raise ValueError('invalid_params')
        # Echo is opaque to OneBot, but duplicates on this connection must not
        # create another native submission. Persist reservation before I/O.
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        echo = json.dumps(request.get('echo', uuid.uuid4().hex), sort_keys=True, ensure_ascii=True)
        key = hashlib.sha256((self.connection + ':' + echo).encode()).hexdigest()
        async with self.send_lock:
            cached = self.state.submission(key, digest)
            if cached is not None:
                return cached
            output = self._message_text(params)
            if not self.online:
                return self.result(1500, message='offline')
            if self.pending is None or self.pending['claimed'] or self.clock() > self.pending['deadline']:
                return self.result(1403, message='no_active_trigger')
            if self.clock() - self.last_sent < self.config.send_interval:
                return self.result(1429, message='rate_limited')
            native_key = uuid.uuid4().hex
            self.state.reserve(key, digest, native_key)
            self.pending['claimed'] = True
            self.last_sent = self.clock()
            try:
                receipt = await asyncio.wait_for(self.backend.send_text(self.config.room_id, output, native_key), timeout=45)
                if self._accepted(receipt):
                    mid = self.state.identity('outgoing', receipt.send_id)[0]
                    answer = self.result(data={'message_id': mid})
                elif isinstance(receipt, Receipt) and receipt.state in {'failed', 'cancelled'}:
                    answer = self.result(1500, message='native_send_failed')
                else:
                    answer = self.result(1408, message='outcome_unknown')
            except asyncio.CancelledError:
                self.state.finish(key, self.result(1408, message='outcome_unknown'))
                raise
            except Exception:
                answer = self.result(1408, message='outcome_unknown')
            self.state.finish(key, answer)
            # Failure also consumes this question's only automatic reply.
            self.pending = None
            return answer

    async def action(self, request):
        return await ActionRegistry(self, legacy=True).dispatch(request)
