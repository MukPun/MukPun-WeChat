"""Private text forwarding: FIFO input, multi-action replies and full text.

Only one explicitly bound contact is exposed. Unsupported message segments fail
the whole action, rather than making a partly delivered message look complete.
"""
import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field

from .protocol import Bridge, NativeMessage, Receipt, UnsupportedMessageSegment, number, text as base_text
from .group_trigger import triggered, admitted, group_id
from . import image_input as images
from . import image_output as output_images

MAX_TEXT = 32768


@dataclass(frozen=True)
class RoutedMessage(NativeMessage):
    mentioned: bool = False
    sender_nickname: str = field(default='', repr=False, kw_only=True)
    sender_card: str | None = field(default=None, repr=False, kw_only=True)


@dataclass(frozen=True)
class ImageMessage(RoutedMessage):
    image: dict = field(default_factory=dict, repr=False)


def native_message(data):
    if 'image' in data:
        return ImageMessage(**data)
    return RoutedMessage(**data) if {'mentioned', 'sender_nickname', 'sender_card'} & data.keys() else NativeMessage(**data)


def valid_content(message, enabled):
    image = getattr(message, 'image', None)
    if image is not None:
        if not enabled:
            return False
        images.validate(image)
    return (type(message.type) is int and
            (message.type == 1 and text(message.body, MAX_TEXT)
             or message.type == 3 and image is not None and message.body == ''))


def text(value, maximum=MAX_TEXT):
    # Preserve Windows newlines exactly; the original one-shot policy did not.
    return isinstance(value, str) and base_text(value.replace('\r', '\n'), maximum)


def split_text(value, maximum=8192):
    if not text(value, MAX_TEXT) or not value.strip():
        raise ValueError('invalid_message')
    parts, current, size = [], [], 0
    for char in value:
        length = len(char.encode('utf-8'))
        if size + length > maximum:
            parts.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += length
    if current:
        parts.append(''.join(current))
    return parts


class PrivateBridge(Bridge):
    def __init__(self, config, peer, state, backend, *, clock=time.time, segment_bytes=8192, self_sender_id=None,
                 image_input=False, image_output=False):
        if not text(peer, 256) or peer.endswith('@chatroom'):
            raise ValueError('invalid_peer')
        super().__init__(config, state, backend, clock=clock)
        self.peer = peer
        if self_sender_id is not None and (not text(self_sender_id, 256) or self_sender_id == peer or self_sender_id.endswith('@chatroom')):
            raise ValueError('invalid_self_sender')
        # Keep the established OneBot bot ID stable; this is the real WeChat ID.
        self.self_sender_id = self_sender_id or config.self_native_id
        if type(image_input) is not bool:
            raise ValueError('image_policy')
        self.image_input = image_input
        if type(image_output) is not bool:
            raise ValueError('image_output_policy')
        self.image_output = image_output
        self.peer_id = state.identity('user', peer)[0]
        self.reply_until = 0
        self.current = None
        if type(segment_bytes) is not int or not 1024 <= segment_bytes <= 8192:
            raise ValueError('invalid_segment_limit')
        self.segment_bytes = segment_bytes
        self.namespace = state.cursor('onebot_namespace')
        if self.namespace is None:
            self.namespace = uuid.uuid4().hex
            state.cursor('onebot_namespace', self.namespace)

    def connected(self):
        self.connection = uuid.uuid4().hex

    def disconnected(self):
        self.connection = None

    def ingest(self, message):
        if (not self.online or not isinstance(message, NativeMessage)
                or message.account != self.config.account or message.room_id != self.peer
                or message.sender_id != self.peer or message.sender_id == self.self_sender_id or message.direction != 'incoming'
                or message.origin != 'live' or not valid_content(message, self.image_input)
                or not text(message.id, 512)):
            raise ValueError('invalid_source')
        mid = self.state.identity('incoming', message.id)[0]
        self.state.bind_route(mid, self.peer)
        self.state.cursor('reply_trigger:' + self.peer, str(mid))
        self.reply_until = self.clock() + 600
        self.current = mid
        self.state.publishing(message.id)
        return {'time': message.timestamp, 'self_id': self.self_id, 'post_type': 'message',
                'message_type': 'private', 'sub_type': 'friend', 'message_id': mid,
                'user_id': self.peer_id, 'font': 0, 'raw_message': message.body,
                'message': ([{'type': 'text', 'data': {'text': message.body}}] if message.body else [])
                           + ([images.segment(message.image)] if isinstance(message, ImageMessage) else []),
                'sender': {'user_id': self.peer_id, 'nickname': self.config.room_name, 'sex': 'unknown', 'age': 0}}

    async def event_delivered(self, message, success):
        self.state.handoff(message.id, success)
        metadata = ({'image_bytes': message.image['bytes'], 'image_sha256': message.image['sha256'],
                     'image_quality': message.image['quality']} if isinstance(message, ImageMessage) else {})
        if group_id(message.room_id):
            metadata['group_wakeup'] = (getattr(message, 'mentioned', False) if self.all_groups else
                triggered(message.body, getattr(message, 'mentioned', False), self.wake_mode, self.prefix))
        self.backend.report('handoff', success=success, counts=self.state.counts(), destination=message.room_id,
                            bytes=len(message.body.encode()), sha256=hashlib.sha256(message.body.encode()).hexdigest(), **metadata)

    def _private_text(self, params, destination=None):
        value = params.get('message')
        if isinstance(value, str):
            if '[CQ:' in value and params.get('auto_escape') is not True:
                raise UnsupportedMessageSegment('unsupported_segment')
            result = value
        elif isinstance(value, list) and 0 < len(value) <= 128:
            parts = []
            for segment in value:
                if not isinstance(segment, dict) or set(segment) != {'type', 'data'} or not isinstance(segment['data'], dict):
                    raise ValueError('invalid_segment')
                data = segment['data']
                if segment['type'] == 'text' and set(data) == {'text'} and isinstance(data['text'], str):
                    parts.append(data['text'])
                elif (segment['type'] == 'reply' and set(data) == {'id'}
                      and self.state.message_route(number(data['id'])) == (destination or self.peer)):
                    pass
                else:
                    raise UnsupportedMessageSegment('unsupported_segment')
            result = ''.join(parts)
        else:
            raise ValueError('invalid_message')
        return result, split_text(result, self.segment_bytes)

    def _destination(self, action, params):
        allowed = {'user_id', 'message', 'auto_escape', 'self_id'}
        if action == 'send_msg':
            allowed.add('message_type')
            if params.get('message_type', 'private') != 'private':
                raise ValueError('destination_not_allowed')
        if (number(params.get('user_id')) != self.peer_id or not set(params) <= allowed
                or 'auto_escape' in params and type(params['auto_escape']) is not bool):
            raise ValueError('destination_not_allowed')
        return self.peer

    def _reply_active(self, destination):
        return destination == self.peer and self.clock() <= self.reply_until

    async def _send(self, action, params, request):
        destination = self._destination(action, params)
        segments = params.get('message')
        if isinstance(segments, list) and any(isinstance(s, dict) and s.get('type') == 'image' for s in segments):
            return await self._send_image(destination, params, request)
        output, parts = self._private_text(params, destination)
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        echo = json.dumps(request.get('echo', uuid.uuid4().hex), sort_keys=True, ensure_ascii=True)
        # Echo counters may restart with AstrBot. Scope deduplication to the
        # actual source conversation so a later identical answer is not lost.
        trigger = self.state.cursor('reply_trigger:' + destination) or 'none'
        segments = params.get('message')
        if isinstance(segments, list):
            quoted = [str(number(item['data']['id'])) for item in segments if item.get('type') == 'reply']
            if quoted:
                trigger = quoted[0]
        key = hashlib.sha256((self.namespace + ':' + destination + ':' + trigger + ':' + echo).encode()).hexdigest()
        async with self.send_lock:
            cached = self.state.submission(key, digest)
            if cached is not None:
                self.backend.report('loop_guard', reason='duplicate_action')
                return cached
            if not self.online or not self._reply_active(destination):
                return self.result(1403, message='no_active_private_conversation')
            # Backpressure waits instead of dropping the second reply.
            await asyncio.sleep(max(0, self.config.send_interval - (self.clock() - self.last_sent)))
            native_key = uuid.uuid4().hex
            self.state.reserve(key, digest, native_key)
            confirmed = []
            answer = self.result(1408, message='outcome_unknown')
            try:
                for index, part in enumerate(parts):
                    if index:
                        await asyncio.sleep(self.config.send_interval)
                    part_key = hashlib.sha256((native_key + ':' + str(index)).encode()).hexdigest()[:32]
                    receipt = await asyncio.wait_for(self.backend.send_text(destination, part, part_key), timeout=45)
                    self.last_sent = self.clock()
                    # Reuse the strict receipt validator with the private bound destination.
                    if not self._destination_accepted(receipt, destination):
                        break
                    confirmed.append(receipt.send_id)
                if len(confirmed) == len(parts):
                    mid = self.state.identity('outgoing', confirmed[-1])[0]
                    answer = self.result(data={'message_id': mid})
            except asyncio.CancelledError:
                self.state.finish(key, answer)
                self.backend.report('reply', success=False, parts=len(parts), accepted=len(confirmed), destination=destination)
                raise
            except Exception:
                pass
            self.state.finish(key, answer)
            self.backend.report('reply', success=answer['retcode'] == 0, parts=len(parts), accepted=len(confirmed),
                                bytes=len(output.encode()), sha256=hashlib.sha256(output.encode()).hexdigest(), destination=destination)
            return answer

    async def _send_image(self, destination, params, request):
        segments = params.get('message')
        if not self.image_output or not isinstance(segments, list) or not 1 <= len(segments) <= 2:
            raise UnsupportedMessageSegment('image_output_disabled')
        image, quote = None, None
        try:
            for segment in segments:
                if (not isinstance(segment, dict) or set(segment) != {'type', 'data'}
                        or not isinstance(segment['data'], dict)):
                    raise output_images.OutputError('output_image_segment')
                if segment['type'] == 'image' and image is None:
                    image = output_images.from_segment(segment)
                elif (segment['type'] == 'reply' and quote is None and set(segment['data']) == {'id'}
                      and self.state.message_route(number(segment['data']['id'])) == destination):
                    quote = str(number(segment['data']['id']))
                else:
                    raise output_images.OutputError('output_image_segment')
            if image is None:
                raise output_images.OutputError('output_image_segment')
        except (output_images.OutputError, ValueError, TypeError) as error:
            reason = {'output_image_source': 'source', 'output_image_size': 'size',
                'output_image_base64': 'base64', 'output_image_format': 'format',
                'output_image_limits': 'dimensions', 'output_image_interlace': 'interlace',
                'output_image_segment': 'segment'}.get(str(error), 'payload')
            self.backend.report('image_output_validation',
                                detail=output_images.rejection(reason, segments))
            raise UnsupportedMessageSegment('output_image_invalid') from None
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        echo = json.dumps(request.get('echo', uuid.uuid4().hex), sort_keys=True, ensure_ascii=True)
        trigger = quote or self.state.cursor('reply_trigger:' + destination) or 'none'
        key = hashlib.sha256((self.namespace + ':' + destination + ':' + trigger + ':' + echo).encode()).hexdigest()
        async with self.send_lock:
            cached = self.state.submission(key, digest)
            if cached is not None:
                self.backend.report('loop_guard', reason='duplicate_action')
                return cached
            if not self.online or not self._reply_active(destination):
                return self.result(1403, message='no_active_private_conversation')
            await asyncio.sleep(max(0, self.config.send_interval - (self.clock() - self.last_sent)))
            native_key = uuid.uuid4().hex
            self.state.reserve(key, digest, native_key)
            answer = self.result(1408, message='outcome_unknown')
            confirmed = False
            rejected = False
            try:
                receipt = await asyncio.wait_for(self.backend.send_image(destination, image, native_key), 50)
                self.last_sent = self.clock()
                if output_images.accepted(receipt, self.config.account, destination, image['sha256']):
                    mid = self.state.identity('outgoing', 'image:' + receipt['job_id'])[0]
                    answer = self.result(data={'message_id': mid})
                    confirmed = True
                elif (output_images.receipt_bound(receipt, self.config.account, destination, image['sha256'])
                      and receipt['state'] == 'rejected'):
                    answer = self.result(1400, message='unsupported_message_segment')
                    rejected = True
            except asyncio.CancelledError:
                self.state.finish(key, answer)
                self.backend.report('reply', success=False, parts=1, accepted=0, destination=destination,
                                    media='image', bytes=image['bytes'], sha256=image['sha256'])
                raise
            except Exception:
                pass
            self.state.finish(key, answer)
            if rejected:
                self.backend.report('image_output', outcome='rejected')
            else:
                self.backend.report('reply', success=confirmed, parts=1, accepted=int(confirmed), destination=destination,
                                    media='image', bytes=image['bytes'], sha256=image['sha256'])
            return answer

    def _destination_accepted(self, receipt, destination):
        # The base validator checks canonical IDs, account and terminal status.
        proxy = Receipt(receipt.account, self.config.room_id, receipt.send_id, receipt.state,
                        receipt.client_id, receipt.server_id) if isinstance(receipt, Receipt) else None
        return isinstance(receipt, Receipt) and receipt.destination == destination and self._accepted(proxy)

    async def action(self, request):
        action = request.get('action') if isinstance(request, dict) else None
        if action == 'can_send_image':
            # Let the base validator check request shape, echo and self_id.
            result = await super().action(request)
            if result.get('retcode') == 0:
                result['data'] = {'yes': self.image_output}
            return result
        if action == 'send_group_msg':
            result = self.result(1403, message='destination_not_allowed')
        elif action == 'get_friend_list':
            result = self.result(data=[{'user_id': self.peer_id, 'nickname': getattr(self, 'private_name', self.config.room_name), 'remark': ''}])
        elif action == 'send_private_msg':
            rewritten = dict(request, action='send_msg')
            return await super().action(rewritten)
        else:
            return await super().action(request)
        if isinstance(request, dict) and 'echo' in request:
            result['echo'] = request['echo']
        return result


class TextBridge(PrivateBridge):
    """Private forwarding and stable group routes, with AstrBot owning group policy."""
    def __init__(self, config, peer, state, backend, *, clock=time.time, prefix='/ai ', segment_bytes=8192,
                 private_name='测试联系人', wake_mode='prefix', self_sender_id=None, image_input=False,
                 group_context=False, image_output=False, all_groups=False):
        if type(all_groups) is not bool:
            raise ValueError('all_groups_policy')
        if (config.room_id is None and not all_groups) or prefix != '/ai ':
            raise ValueError('group_route_required')
        super().__init__(config, peer, state, backend, clock=clock, segment_bytes=segment_bytes,
                         self_sender_id=self_sender_id, image_input=image_input, image_output=image_output)
        self.prefix = prefix
        self.private_name = private_name
        if wake_mode not in {'prefix', 'mention_or_prefix', 'mention'}:
            raise ValueError('invalid_wake_mode')
        self.wake_mode = wake_mode
        if type(group_context) is not bool:
            raise ValueError('group_context_policy')
        self.group_context = group_context
        self.all_groups = all_groups
        self.group_reply_until = 0

    def ingest(self, message):
        if message.room_id == self.peer:
            event = super().ingest(message)
            event['sender']['nickname'] = self.private_name
            return event
        if (not self.online or message.account != self.config.account
                or not (group_id(message.room_id) if self.all_groups else message.room_id == self.config.room_id)
                or message.direction != 'incoming' or message.origin != 'live'
                or not valid_content(message, self.image_input) or not text(message.id, 512) or not text(message.sender_id, 256)
                or message.sender_id.endswith('@chatroom') or message.sender_id == self.self_sender_id
                or not self.all_groups and (not text(message.body, MAX_TEXT)
                or not admitted(message.body, getattr(message, 'mentioned', False), self.wake_mode, self.prefix,
                                context=self.group_context, image=isinstance(message, ImageMessage)))):
            raise ValueError('invalid_group_source')
        wakeup = (getattr(message, 'mentioned', False) if self.all_groups else
                  triggered(message.body, getattr(message, 'mentioned', False), self.wake_mode, self.prefix))
        group = self.state.identity('group', message.room_id)[0]
        mid = self.state.identity('incoming', message.id)[0]
        uid = self.state.identity('user', message.sender_id)[0]
        self.state.bind_route(mid, message.room_id)
        # Ordinary context must not change deduplication scope while a wakeup
        # reply is still arriving. AstrBot may also choose to reply to ordinary
        # text, so keep the existing bounded same-group reply permission.
        if wakeup or self.state.cursor('reply_trigger:' + message.room_id) is None:
            self.state.cursor('reply_trigger:' + message.room_id, str(mid))
        self.state.publishing(message.id)
        self.group_reply_until = self.clock() + 600
        return {'time': message.timestamp, 'self_id': self.self_id, 'post_type': 'message',
                'message_type': 'group', 'sub_type': 'normal', 'message_id': mid,
                'group_id': group, 'group_name': self._group_name(group), 'user_id': uid,
                'font': 0, 'anonymous': None, 'raw_message': message.body,
                'message': ([{'type': 'at', 'data': {'qq': str(self.self_id)}}] if wakeup else [])
                           + [{'type': 'text', 'data': {'text': message.body[len(self.prefix):]
                                if not self.all_groups and wakeup and message.body.startswith(self.prefix) else message.body}}]
                           + ([images.segment(message.image)] if isinstance(message, ImageMessage) else []),
                'sender': self._sender(group, uid, message)}

    def _destination(self, action, params):
        if action == 'send_group_msg' or action == 'send_msg' and params.get('message_type', 'group' if 'group_id' in params else 'private') == 'group':
            allowed = {'group_id', 'message', 'auto_escape', 'self_id'}
            if action == 'send_msg':
                allowed.add('message_type')
            group = self._group(params)
            if (not set(params) <= allowed
                    or 'auto_escape' in params and type(params['auto_escape']) is not bool):
                raise ValueError('group_not_allowed')
            return self.state.raw_identity('group', group)
        return super()._destination(action, params)

    def _reply_active(self, destination):
        if self.all_groups and group_id(destination):
            return True
        if destination == self.config.room_id:
            return self.clock() <= self.group_reply_until
        return super()._reply_active(destination)

    def _group(self, params):
        if not self.all_groups:
            return super()._group(params)
        group = number(params.get('group_id'))
        if not group_id(self.state.raw_identity('group', group)):
            raise ValueError('unknown_group')
        return group

    def _group_name(self, group):
        return self.config.room_name if group == self.group_id else '微信群' + str(group)

    def _group_list(self):
        if not self.all_groups:
            return super()._group_list()
        return [{'group_id': group, 'group_name': self._group_name(group)}
                for group, raw in self.state.known_identities('group') if group_id(raw)]

    async def action(self, request):
        if isinstance(request, dict) and request.get('action') in {'send_group_msg', 'send_msg'}:
            return await Bridge.action(self, request)
        return await super().action(request)

