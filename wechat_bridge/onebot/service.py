"""Formal OneBot composition: registry + message routing + persistent delivery."""
import time
from ..core.messages import MessageService
from ..core.sending import DeliveryService
from .content import ContentCodec
from .codec import number, result
from .events import EventCodec, meta
from .registry import ActionRegistry
from .frames import handle_frame, reply_sent


class OneBotService:
    result = staticmethod(result)
    handle_frame = handle_frame
    reply_sent = staticmethod(reply_sent)

    def __init__(self, config, peer, state, backend, *, policy, clock=time.time,
                 health=None, groups=False, all_groups=False, private_name=None, segment_bytes=8192,
                 image_input=False, image_output=False, voice_output=False):
        self.groups, self.all_groups = groups, all_groups
        self.health = health
        self.peer, self.private_name = peer, private_name if private_name is not None else config.room_name
        if type(segment_bytes) is not int or not 1024 <= segment_bytes <= 8192:
            raise ValueError('invalid_segment_limit')
        for value, code in ((image_input, 'image_policy'), (image_output, 'image_output_policy'),
                            (voice_output, 'voice_output_policy'), (all_groups, 'all_groups_policy')):
            if type(value) is not bool:
                raise ValueError(code)
        self.events = EventCodec(self.private_name, config.room_name)
        self.messages = MessageService(config, peer, state, backend, policy, self.events,
            clock=clock, groups=groups, all_groups=all_groups, image_input=image_input)
        self.content = ContentCodec(self.messages, segment_bytes=segment_bytes,
            image_output=image_output, voice_output=voice_output)
        self.delivery = DeliveryService(self.messages, result)
        self.registry = ActionRegistry(self)

    @property
    def config(self):
        return self.messages.config

    @config.setter
    def config(self, value):
        self.messages.config = value

    @property
    def state(self):
        return self.messages.state

    @state.setter
    def state(self, value):
        self.messages.state = value

    @property
    def backend(self):
        return self.messages.backend

    @backend.setter
    def backend(self, value):
        self.messages.backend = value

    @property
    def clock(self):
        return self.messages.clock

    @clock.setter
    def clock(self, value):
        self.messages.clock = value

    @property
    def image_input(self):
        return self.messages.image_input

    @image_input.setter
    def image_input(self, value):
        self.messages.image_input = value

    @property
    def image_output(self):
        return self.content.image_output

    @image_output.setter
    def image_output(self, value):
        self.content.image_output = value

    @property
    def voice_output(self):
        return self.content.voice_output

    @voice_output.setter
    def voice_output(self, value):
        self.content.voice_output = value

    @property
    def self_id(self):
        return self.messages.self_id

    @property
    def group_id(self):
        return self.messages.group_id

    @property
    def peer_id(self):
        return self.messages.peer_id

    @property
    def pending(self):
        return self.messages.pending

    @property
    def online(self):
        return self.health.status()['online'] if self.health is not None else self.messages.online

    def connected(self):
        self.messages.connected()

    def disconnected(self):
        self.messages.disconnected()

    def ingest(self, message):
        return self.messages.ingest(message)

    async def event_delivered(self, message, success):
        await self.messages.event_delivered(message, success)

    def status(self):
        if self.health is None:
            return {'online': self.online, 'good': self.online}
        return self.health.status(websocket=self.messages.connection is not None)

    def meta(self, subtype):
        return meta(self, subtype)

    async def action(self, request):
        return await self.registry.dispatch(request)

    def _group(self, params):
        group = number(params.get('group_id'))
        self.messages.group_route(group)
        return group

    def _group_name(self, group):
        return self.events.group_name(self.messages, group)

    def _group_list(self):
        return [{'group_id': group, 'group_name': self._group_name(group)}
                for group, raw in self.messages.group_routes()]

    def _sender(self, group, user, message=None):
        return self.events.sender(self.messages, group, user, message)

    def destination(self, action, params):
        group = (action == 'send_group_msg' or action == 'send_msg' and
                 params.get('message_type', 'group' if self.groups and 'group_id' in params else 'private') == 'group')
        allowed = {'group_id' if group else 'user_id', 'message', 'auto_escape', 'self_id'}
        if action == 'send_msg':
            allowed.add('message_type')
        if (not set(params) <= allowed or 'auto_escape' in params and type(params['auto_escape']) is not bool
                or action == 'send_msg' and params.get('message_type', 'group' if group else 'private') != ('group' if group else 'private')):
            raise ValueError('destination_not_allowed')
        if group:
            return self.messages.group_route(self._group(params))
        return self.messages.private_route(number(params.get('user_id')))
