"""Conversation routing, persistent publication and reply permissions.

Raw identity syntax and source/media admission belong to the injected policy.
The event projector supplies protocol representation without owning delivery.
"""
import hashlib
import time
import uuid
from .contracts import NativeMessage, ConversationKind


class MessageService:
    def __init__(self, config, peer, state, backend, policy, events, *, clock=time.time,
                 groups=False, all_groups=False, image_input=False):
        self.config, self.peer, self.state, self.backend = config, peer, state, backend
        self.policy, self.events, self.clock = policy, events, clock
        self.groups, self.all_groups, self.image_input = groups, all_groups, image_input
        self.self_id = state.identity('bot', config.self_native_id)[0]
        self.group_id = state.identity('group', config.room_id)[0] if config.room_id else None
        self.peer_id = state.identity('user', peer)[0]
        self.connection = None
        self.reply_until = self.group_reply_until = 0
        self.current = self.pending = None

    @property
    def online(self):
        return self.connection is not None and self.backend.online is True

    def connected(self):
        self.connection = uuid.uuid4().hex

    def disconnected(self):
        self.connection = None

    def is_group(self, raw):
        return self.policy.conversation(self.config.account, raw).kind == ConversationKind.GROUP

    def group_route(self, group):
        raw = self.state.raw_identity('group', group)
        if (raw is None or not self.is_group(raw)
                or not self.all_groups and raw != self.config.room_id):
            raise ValueError('unknown_group')
        return raw

    def private_route(self, user):
        if user != self.peer_id:
            raise ValueError('destination_not_allowed')
        return self.peer

    def group_routes(self):
        if not self.all_groups:
            return [] if self.group_id is None else [(self.group_id, self.config.room_id)]
        return [(group, raw) for group, raw in self.state.known_identities('group') if self.is_group(raw)]

    def reply_active(self, destination):
        if self.all_groups and self.is_group(destination):
            return True
        if self.groups and destination == self.config.room_id:
            return self.clock() <= self.group_reply_until
        return destination == self.peer and self.clock() <= self.reply_until

    def ingest(self, message):
        if (not self.online or not isinstance(message, NativeMessage)
                or not self.policy.validate_message(message, self.image_input)):
            raise ValueError('invalid_source')
        private = message.room_id == self.peer
        if not private and (not self.groups or not self.is_group(message.room_id)
                            or not self.all_groups and message.room_id != self.config.room_id):
            raise ValueError('invalid_group_source')
        # Keep legacy allocation order: new group, incoming message, then user.
        group = None if private else self.state.identity('group', message.room_id)[0]
        mid = self.state.identity('incoming', message.id)[0]
        user = self.peer_id if private else self.state.identity('user', message.sender_id)[0]
        self.state.bind_route(mid, message.room_id)
        wakeup = False if private else self.policy.wakeup(message)
        if private or wakeup or self.state.cursor('reply_trigger:' + message.room_id) is None:
            self.state.cursor('reply_trigger:' + message.room_id, str(mid))
        if private:
            self.reply_until = self.clock() + 600
            self.current = mid
        self.state.publishing(message.id)
        if not private:
            self.group_reply_until = self.clock() + 600
        return self.events.message(self, message, mid, user, group, wakeup)

    async def event_delivered(self, message, success):
        self.state.handoff(message.id, success)
        image = getattr(message, 'image', None)
        metadata = ({'image_bytes': image['bytes'], 'image_sha256': image['sha256'],
                     'image_quality': image['quality']} if image is not None else {})
        if self.is_group(message.room_id):
            metadata['group_wakeup'] = self.policy.wakeup(message)
        self.backend.report('handoff', success=success, counts=self.state.counts(), destination=message.room_id,
                            bytes=len(message.body.encode()), sha256=hashlib.sha256(message.body.encode()).hexdigest(), **metadata)
