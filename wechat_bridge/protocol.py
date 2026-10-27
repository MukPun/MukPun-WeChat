"""OneBot v11 policy and conversion, independent of network and model service."""
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import time
import uuid

from .state import StateError

MAX_JSON = 65536
MAX_TEXT = 8192
MAX_JSON_DEPTH = 32
MAX_JSON_TOKENS = 8192
_NO_ECHO = object()


class DecodeError(ValueError):
    """Invalid action; an unambiguous, bounded echo may still be returned."""
    def __init__(self, code, *, echo=_NO_ECHO):
        super().__init__(code)
        self.has_echo = echo is not _NO_ECHO
        self.echo = echo if self.has_echo else None


class _JSONObject(dict):
    __slots__ = ('duplicates',)

    def __init__(self):
        super().__init__()
        self.duplicates = set()


def _structure_budget(raw):
    # Bound containers and members before json.loads allocates their objects.
    # Long opaque base64 strings do not consume this structural budget.
    quoted = escaped = False
    depth = tokens = 0
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        else:
            if char in '[{':
                depth += 1
                tokens += 1
                if depth > MAX_JSON_DEPTH:
                    raise DecodeError('json_structure_limit')
            elif char in ']}':
                depth -= 1
            elif char in ',:':
                tokens += 1
            if tokens > MAX_JSON_TOKENS:
                raise DecodeError('json_structure_limit')


def _unique_echo(value):
    if not isinstance(value, _JSONObject) or 'echo' not in value or 'echo' in value.duplicates:
        return _NO_ECHO
    echo = value['echo']
    pending = [echo]
    while pending:
        part = pending.pop()
        if isinstance(part, _JSONObject):
            if part.duplicates:
                return _NO_ECHO
            pending.extend(part.values())
        elif isinstance(part, list):
            pending.extend(part)
    try:
        return echo if len(json.dumps(echo, ensure_ascii=True, allow_nan=False)) <= 4096 else _NO_ECHO
    except (ValueError, TypeError, RecursionError):
        return _NO_ECHO


class UnsupportedMessageSegment(ValueError):
    """Rejected before any native submission; safe to keep forwarding."""


def text(value, maximum=MAX_TEXT):
    try:
        return (isinstance(value, str) and 0 < len(value.encode('utf-8')) <= maximum
                and not any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in value))
    except UnicodeError:
        return False


def number(value):
    if type(value) is int and 0 < value <= 2147483647:
        return value
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,9}', value) and int(value) <= 2147483647:
        return int(value)
    raise ValueError('invalid_id')


def decode(raw, *, maximum=MAX_JSON):
    if type(maximum) is not int or maximum <= 0 or not isinstance(raw, str) or len(raw.encode('utf-8')) > maximum:
        raise DecodeError('invalid_json')
    _structure_budget(raw)
    duplicates = False
    def pairs(items):
        nonlocal duplicates
        result = _JSONObject()
        for key, value in items:
            if key in result:
                result.duplicates.add(key)
                duplicates = True
            result[key] = value
        return result
    def invalid(_):
        raise DecodeError('invalid_constant')
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise DecodeError('invalid_constant')
        return number
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite)
    if not isinstance(result, dict):
        raise DecodeError('invalid_json')
    if duplicates:
        raise DecodeError('duplicate_key', echo=_unique_echo(result))
    return result


@dataclass(frozen=True)
class NativeMessage:
    account: str = field(repr=False)
    id: str = field(repr=False)
    room_id: str = field(repr=False)
    sender_id: str = field(repr=False)
    body: str = field(repr=False)
    timestamp: int
    origin: str = 'live'
    direction: str = 'incoming'
    type: int = 1


@dataclass(frozen=True)
class Receipt:
    account: str = field(repr=False)
    destination: str = field(repr=False)
    send_id: str = field(repr=False)
    state: str
    client_id: str = field(default='', repr=False)
    server_id: str = field(default='', repr=False)


@dataclass(frozen=True)
class Config:
    account: str = field(repr=False)
    self_native_id: str = field(repr=False)
    room_id: str | None = field(repr=False)
    room_name: str
    reply_seconds: int = 90
    send_interval: float = 5.

    def __post_init__(self):
        if (not text(self.account, 64) or not text(self.self_native_id, 256)
                or self.room_id is not None and (not text(self.room_id, 256)
                    or self.room_id == '@chatroom' or not self.room_id.endswith('@chatroom'))
                or not text(self.room_name, 256) or type(self.reply_seconds) is not int
                or not 1 <= self.reply_seconds <= 180
                or type(self.send_interval) not in (int, float) or not math.isfinite(self.send_interval)
                or not 0 <= self.send_interval <= 60):
            raise ValueError('invalid_config')


class Bridge:
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
        base = {'time': int(self.clock()), 'self_id': self.self_id, 'post_type': 'meta_event', 'meta_event_type': subtype}
        if subtype == 'lifecycle':
            return {**base, 'sub_type': 'connect'}
        return {**base, 'status': {'online': self.online, 'good': self.online}, 'interval': 15000}

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
        segments = [{'type': 'at', 'data': {'qq': str(self.self_id)}}, {'type': 'text', 'data': {'text': body}}]
        event = {'time': message.timestamp, 'self_id': self.self_id, 'post_type': 'message',
                 'message_type': 'group', 'sub_type': 'normal', 'message_id': mid, 'group_id': self.group_id,
                 'group_name': c.room_name, 'user_id': uid, 'message': segments,
                 'raw_message': '/ai ' + body, 'font': 0, 'anonymous': None,
                 'sender': {'user_id': uid, 'nickname': '微信成员' + str(uid), 'card': '', 'role': 'member'}}
        self.pending = {'event': event, 'native_id': message.id, 'deadline': self.clock() + c.reply_seconds, 'claimed': False}
        return event

    @staticmethod
    def result(code=0, data=None, message=''):
        value = {'status': 'ok' if code == 0 else 'failed', 'retcode': code, 'data': data}
        if message:
            value['message'] = value['wording'] = message
        return value

    def _group(self, params):
        if self.group_id is None or number(params.get('group_id')) != self.group_id:
            raise ValueError('group_not_allowed')

    def _message_text(self, params):
        value = params.get('message')
        if isinstance(value, str):
            if '[CQ:' in value and params.get('auto_escape') is not True:
                raise UnsupportedMessageSegment('unsupported_segment')
            output = value
        elif isinstance(value, list) and 0 < len(value) <= 32:
            parts = []
            for segment in value:
                if not isinstance(segment, dict) or set(segment) != {'type', 'data'} or not isinstance(segment['data'], dict):
                    raise ValueError('invalid_message')
                data = segment['data']
                if segment['type'] == 'text' and set(data) == {'text'} and isinstance(data['text'], str):
                    parts.append(data['text'])
                elif segment['type'] == 'reply' and set(data) == {'id'} and self.pending is not None:
                    if number(data['id']) != self.pending['event']['message_id']:
                        raise ValueError('reply_mismatch')
                else:
                    raise UnsupportedMessageSegment('unsupported_segment')
            output = ''.join(parts)
        else:
            raise ValueError('invalid_message')
        if not text(output) or not output.strip():
            raise ValueError('invalid_message')
        return output

    def _accepted(self, receipt):
        try:
            identity = uuid.UUID(receipt.send_id)
            return (isinstance(receipt, Receipt) and receipt.account == self.config.account
                    and receipt.destination == self.config.room_id and receipt.state == 'accepted'
                    and identity.version == 4 and str(identity) == receipt.send_id
                    and isinstance(receipt.client_id, str) and re.fullmatch(r'[1-9][0-9]{0,9}', receipt.client_id)
                    and int(receipt.client_id) <= 2**32-1
                    and isinstance(receipt.server_id, str) and re.fullmatch(r'[1-9][0-9]{0,19}', receipt.server_id)
                    and int(receipt.server_id) <= 2**64-1)
        except (ValueError, AttributeError, TypeError):
            return False

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
        answer = self.result(1400, message='invalid_request')
        try:
            if not isinstance(request, dict) or not set(request) <= {'action', 'params', 'echo'}:
                raise ValueError('invalid_request')
            if 'echo' in request and len(json.dumps(request['echo'], ensure_ascii=True, allow_nan=False)) > 4096:
                raise ValueError('echo_too_large')
            action, params = request.get('action'), request.get('params', {})
            if not isinstance(action, str) or not isinstance(params, dict):
                raise ValueError('invalid_request')
            if 'self_id' in params and number(params['self_id']) != self.self_id:
                raise ValueError('self_id_mismatch')
            if action in {'send_group_msg', 'send_msg'}:
                answer = await self._send(action, params, request)
            elif action == 'get_login_info':
                answer = self.result(data={'user_id': self.self_id, 'nickname': '微信助手'})
            elif action == 'get_status':
                answer = self.result(data={'online': self.online, 'good': self.online})
            elif action == 'get_version_info':
                answer = self.result(data={'app_name': 'astrbot-wechat-bridge', 'app_version': '0.1.0', 'protocol_version': 'v11'})
            elif action in {'can_send_image', 'can_send_record'}:
                answer = self.result(data={'yes': False})
            elif action == 'get_group_list':
                answer = self.result(data=[] if self.group_id is None else [{'group_id': self.group_id, 'group_name': self.config.room_name}])
            elif action == 'get_group_info':
                self._group(params)
                answer = self.result(data={'group_id': self.group_id, 'group_name': self.config.room_name})
            elif action == 'get_group_member_info':
                self._group(params)
                user = number(params.get('user_id'))
                if user != self.self_id and not self.state.known('user', user):
                    raise ValueError('unknown_member')
                answer = self.result(data={'group_id': self.group_id, 'user_id': user,
                    'nickname': '微信助手' if user == self.self_id else '微信成员' + str(user),
                    'card': '', 'role': 'member'})
            elif action == 'get_stranger_info':
                user = number(params.get('user_id'))
                if user != self.self_id and not self.state.known('user', user):
                    raise ValueError('unknown_member')
                answer = self.result(data={'user_id': user,
                    'nickname': '微信助手' if user == self.self_id else '微信成员' + str(user), 'sex': 'unknown', 'age': 0})
            elif action == 'get_msg' and self.pending is not None and number(params.get('message_id')) == self.pending['event']['message_id']:
                event = self.pending['event']
                answer = self.result(data={key: event[key] for key in ('time', 'message_type', 'message_id', 'sender', 'message')})
            else:
                answer = self.result(1501, message='action_not_supported')
        except UnsupportedMessageSegment:
            answer = self.result(1400, message='unsupported_message_segment')
        except StateError:
            answer = self.result(1500, message='state_unavailable')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            answer = self.result(1400, message='invalid_request')
        if isinstance(request, dict) and 'echo' in request:
            answer = {**answer, 'echo': request['echo']}
        return answer
