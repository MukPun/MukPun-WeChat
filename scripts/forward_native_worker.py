#!/usr/bin/env python3
"""Continuous SSE/private worker beside an existing Native, without lifecycle APIs."""
import importlib.util
import os
from pathlib import Path
import queue
import select
import sys
import threading
import time


def module(name):
    path = Path(__file__).with_name(name + '.py')
    if name in {'forward_wire', 'group_trigger', 'loop_guard', 'image_input', 'image_output'} and not path.exists():
        path = Path(__file__).resolve().parents[1] / 'wechat_bridge' / (name + '.py')
    spec = importlib.util.spec_from_file_location('_forward_native_' + name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


wire = module('forward_wire')
reader = module('group_reader_client')
native = module('bridge_native_protocol')
sse = module('forward_sse')
trigger = module('group_trigger')
loops = module('loop_guard')
images = module('image_native')
image_sender = module('image_send_native')
contact = native.groups.contact
reader.CODE_FILES += ('forward_native_worker.py', 'forward_sse.py', 'forward_wire.py', 'bridge_native_protocol.py', 'group_trigger.py', 'loop_guard.py', 'image_native.py', 'image_input.py', 'image_output.py', 'image_send_native.py')
EMIT_LOCK = threading.Lock()


def resolve_self(lookup, seed, destinations, deadline):
    try:
        loops.proof(seed)
    except ValueError:
        raise wire.WireError('self_identity_unverified') from None
    lookup.policy.path = lookup.policy.prefix + '/sends/' + seed['sendId']
    intent = lookup._get(lookup.policy.path, deadline, timeout=8)
    identity = intent.get('localMessageId')
    if not contact._text(identity, 512):
        raise wire.WireError('self_identity_unverified')
    message = lookup._get(lookup.policy.message(identity), deadline, timeout=8)
    try:
        return loops.verified_sender(seed, intent, message, lookup.policy.account, destinations)
    except ValueError:
        raise wire.WireError('self_identity_unverified') from None


def emit(value):
    raw = wire.encode(value)
    with EMIT_LOCK:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()


def send_image_command(config, own, command, deadline, media_lock):
    request = command['requestId']
    fallback = {'account': config['account'], 'destination': command['destination'], 'job_id': '',
                'state': 'unknown', 'server_id': '', 'source_sha256': command['image']['sha256'],
                'projection_verified': False}
    try:
        with media_lock:
            sender = image_sender.Client(config['account'], config['token'], command['destination'], command['image'],
                command['key'], self_sender_id=own,
                on_intent=lambda job: emit({'kind': 'image_send_intent', 'requestId': request, 'jobId': job}),
                on_wait=lambda job: emit({'kind': 'media_wait', 'jobId': job}),
                on_done=lambda job: emit({'kind': 'media_done', 'jobId': job}))
            result = sender.run(min(deadline, time.monotonic() + 40))
        emit({'kind': 'native_image_result', 'requestId': request, 'receipt': result.receipt,
              'ok': result.ok, 'server_confirmed': result.server_confirmed,
              'submitted': result.submitted, 'code': result.code})
    except Exception:
        emit({'kind': 'native_image_result', 'requestId': request, 'receipt': fallback,
              'ok': False, 'server_confirmed': False, 'submitted': True, 'code': 'image_response'})


class PrivateSender(native.TextSendClient):
    valid_target = staticmethod(lambda value: contact._text(value, 256) and not value.endswith('@chatroom')
                               and value != 'filehelper' and not value.startswith('weixin'))


class RouteSender(native.TextSendClient):
    valid_target = staticmethod(lambda value: PrivateSender.valid_target(value)
        or contact._text(value, 256) and value != '@chatroom' and value.endswith('@chatroom'))

    def _intent(self, value, target):
        result = super()._intent(value, target)
        self.projection_id = value.get('localMessageId')
        return result


def main():
    pump = None
    try:
        value = wire.decode(sys.stdin.buffer.readline(4097))
        base = {'account', 'token', 'runtimeSeconds'}
        route_fields = {'privateName', 'groupName', 'roomId', 'groupPrefix', 'sendInterval'}
        durable_cursor = route_fields <= set(value)
        options = {'imageInput', 'imageProbe', 'imageProbeDownload', 'groupContext', 'stickerProbe', 'imageOutput'}
        if (set(value) - options not in (base, base | route_fields | {'selfProof'}, base | route_fields | {'groupWakeMode', 'selfProof'}) or value['account'] != 'wechat-primary'
                or any(type(value[k]) is not bool for k in options if k in value)
                or options.intersection(value) and not durable_cursor
                or type(value['runtimeSeconds']) is not int or not 60 <= value['runtimeSeconds'] <= 604800):
            raise wire.WireError('config')
        checks = reader.isolation_checks()
        good = set(checks) == reader.CHECK_NAMES and all(checks.values())
        emit({'kind': 'isolated', 'ok': good, 'checks': checks})
        if not good or not reader.await_ready():
            return 1
        deadline = time.monotonic() + value['runtimeSeconds'] - 3
        if route_fields <= set(value):
            if (not contact._text(value['privateName'], 256) or not contact._text(value['groupName'], 256)
                    or not contact._text(value['roomId'], 256) or value['roomId'] == '@chatroom'
                    or not value['roomId'].endswith('@chatroom') or value['groupPrefix'] != '/ai '
                    or value.get('groupWakeMode', 'prefix') not in trigger.MODES
                    or type(value['sendInterval']) is not int or not 5 <= value['sendInterval'] <= 60):
                raise wire.WireError('config')
            contact.TARGET_NAME = value['privateName']
        lookup = contact.ContactReceiveClient(value['account'], value['token'])
        peer, boundary = lookup._target(min(deadline, time.monotonic() + 40))
        room = value.get('roomId')
        group_lookup = None
        if room:
            group_lookup = native.groups.GroupReceiveClient(value['account'], value['token'], value['groupName'])
            resolved, group_boundary = group_lookup._target(min(deadline, time.monotonic() + 40))
            if resolved != room:
                raise wire.WireError('group_binding_changed')
            boundary = str(min(int(boundary), int(group_boundary)))
        destinations = {peer} | ({room} if room else set())
        own = resolve_self(lookup, value['selfProof'], destinations, min(deadline, time.monotonic() + 20)) if durable_cursor else None
        ready = {'kind': 'native_ready', 'peer': peer, 'boundary': boundary}
        if own:
            ready['selfSenderId'] = own
        emit(ready)
        media = images.Client(value['account'], value['token'],
                              on_wait=lambda job: emit({'kind': 'media_wait', 'jobId': job}),
                              on_done=lambda job: emit({'kind': 'media_done', 'jobId': job}))
        if value.get('imageProbe'):
            emit({'kind': 'image_probe', **images.recent_summary(lookup, peer, media, min(deadline, time.monotonic() + 30),
                                                               download=value.get('imageProbeDownload', False), sticker=value.get('stickerProbe', False))})
            return 0
        os.set_blocking(sys.stdin.fileno(), False)
        lines = wire.Lines()
        pending = None
        used = set()
        last_send = -100.
        armed = False
        last_connected = False
        group_images = {}
        image_thread = None
        image_transfer = None
        media_lock = threading.Lock()
        while time.monotonic() < deadline:
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], .05)
            if readable:
                raw = os.read(sys.stdin.fileno(), 4096)
                if not raw:
                    return 0
                lines.feed(raw)
            while True:
                command = lines.pop()
                if command is None:
                    break
                kind = command.get('kind')
                if command == {'kind': 'stop'}:
                    return 0
                if kind == 'start' and not armed and set(command) in ({'kind', 'after'}, {'kind', 'after', 'anchor'}):
                    after = sse.cursor(command['after'])
                    if int(after) > int(boundary):
                        raise wire.WireError('checkpoint_ahead_of_native')
                    if command.get('anchor'):
                        sse.verify_checkpoint(lookup, after, command['anchor'], min(deadline, time.monotonic() + 15))
                    pump = sse.Pump(value['account'], value['token'], after, deadline)
                    pump.start()
                    armed = True
                    emit({'kind': 'native_armed', 'sse': True})
                elif kind == 'ack' and pending is not None and command == {'kind': 'ack', 'cursor': pending}:
                    pump.ack(pending)
                    pending = None
                elif kind in {'image_chunk', 'image_commit'} and armed:
                    if (image_transfer is None or command.get('requestId') != image_transfer['command']['requestId']
                            or time.monotonic() >= image_transfer['deadline']):
                        raise wire.WireError('image_transfer_binding')
                    if kind == 'image_chunk':
                        if set(command) != {'kind', 'requestId', 'index', 'data'}:
                            raise wire.WireError('image_chunk_binding')
                        image_transfer['assembly'].add(command['index'], command['data'])
                        emit({'kind': 'image_chunk_ack', 'requestId': command['requestId'], 'index': command['index']})
                    else:
                        if set(command) != {'kind', 'requestId'}:
                            raise wire.WireError('image_commit_binding')
                        complete = {**image_transfer['command'], 'kind': 'send_image',
                                    'image': image_transfer['assembly'].finish()}
                        image_thread = threading.Thread(target=send_image_command,
                            args=(value, own, complete, image_transfer['deadline'], media_lock), daemon=True)
                        image_transfer = None
                        image_thread.start()
                elif kind in {'send', 'send_image', 'image_begin'} and armed:
                    required = {'kind', 'requestId', 'destination', 'key', 'text' if kind == 'send' else 'image'}
                    if (set(command) != required or command['destination'] not in destinations
                            or not isinstance(command['key'], str) or not wire.HEX.fullmatch(command['key'])
                            or not isinstance(command['requestId'], str)
                            or not wire.HEX.fullmatch(command['requestId']) or command['key'] in used
                            or image_thread is not None and image_thread.is_alive()
                            or image_transfer is not None
                            or len(used) >= 4096 or time.monotonic() - last_send < value.get('sendInterval', 5) - .2
                            or kind != 'send' and not value.get('imageOutput', False)):
                        raise wire.WireError('send_not_allowed')
                    used.add(command['key'])
                    last_send = time.monotonic()
                    if kind == 'image_begin':
                        image_transfer = {'command': command, 'assembly': image_sender.images.Assembly(command['image']),
                                          'deadline': min(deadline, time.monotonic() + 40)}
                        emit({'kind': 'image_chunk_ack', 'requestId': command['requestId'], 'index': -1})
                        continue
                    if kind == 'send_image':
                        image_sender.images.validate(command['image'])
                        image_thread = threading.Thread(target=send_image_command,
                            args=(value, own, command, deadline, media_lock), daemon=True)
                        image_thread.start()
                        continue
                    sender = RouteSender(value['account'], value['token'], command['destination'], command['text'], command['key'],
                        on_intent=lambda identity: emit({'kind': 'send_intent', 'requestId': command['requestId'], 'sendId': identity}))
                    result = sender.run(min(deadline, time.monotonic() + 40), lambda _: None)
                    receipt = sender.receipt or {'account': value['account'], 'destination': command['destination'], 'send_id': '',
                                                'state': 'unknown', 'client_id': '', 'server_id': ''}
                    projection_verified = False
                    if durable_cursor and result.ok and getattr(sender, 'projection_id', None):
                        projected = lookup._get(lookup.policy.message(sender.projection_id), min(deadline, time.monotonic() + 10), timeout=8)
                        projection_verified = (projected.get('accountId') == value['account']
                            and projected.get('id') == sender.projection_id and projected.get('threadId') == command['destination']
                            and projected.get('direction') == 'outgoing' and projected.get('type') == 1
                            and (own is None or projected.get('senderId') == own)
                            and projected.get('body') == command['text'])
                    report = {'kind': 'native_result', 'requestId': command['requestId'], 'receipt': receipt,
                              'ok': result.ok, 'server_confirmed': result.server_confirmed}
                    if durable_cursor:
                        report['projection_verified'] = projection_verified
                    emit(report)
                else:
                    raise wire.WireError('command')
            if not armed or pending is not None:
                continue
            connected = pump.connected.is_set()
            if connected != last_connected:
                emit({'kind': 'report', 'stage': 'sse_connection', 'connected': connected})
                last_connected = connected
            try:
                event = pump.items.get_nowait()
            except queue.Empty:
                continue
            if 'error' in event:
                raise wire.WireError('sse_error')
            current = sse.cursor(event.get('cursor'))
            anchor = sse.event_digest(event)
            data = event.get('data')
            if (event.get('kind') == 'message.committed' and isinstance(data, dict)
                    and data.get('threadId') in destinations and data.get('direction') == 'outgoing'):
                emit({'kind': 'report', 'stage': 'loop_guard', 'reason': 'outgoing_event'})
            if (event.get('kind') != 'message.committed' or not isinstance(data, dict)
                    or data.get('origin') != 'live' or data.get('direction') != 'incoming' or data.get('threadId') not in destinations):
                if durable_cursor:
                    pending = current
                    emit({'kind': 'checkpoint', 'cursor': current, 'anchor': anchor})
                else:
                    pump.ack(current)
                continue
            identity = data.get('messageId')
            target = data['threadId']
            selected = lookup if target == peer else group_lookup
            message = selected._get(selected.policy.message(identity), min(deadline, time.monotonic() + 30), timeout=30)
            reason = ('own_sender' if own and message.get('senderId') == own else
                      'outgoing_message' if message.get('direction') == 'outgoing' else None)
            if reason:
                emit({'kind': 'report', 'stage': 'loop_guard', 'reason': reason})
                if durable_cursor:
                    pending = current
                    emit({'kind': 'checkpoint', 'cursor': current, 'anchor': anchor})
                else:
                    pump.ack(current)
                continue
            if type(message.get('type')) is not int:
                raise wire.WireError('message_type')
            is_image = message['type'] == 3 and value.get('imageInput', False)
            quoted_image = (message['type'] == 49 and value.get('imageInput', False)
                            and message.get('appMessageType') == 57 and isinstance(message.get('quote'), dict)
                            and type(message['quote'].get('type')) is int and message['quote']['type'] == 3)
            if message['type'] != 1 and not is_image and not quoted_image:
                emit({'kind': 'report', 'stage': 'unsupported_incoming', 'type': message['type']})
                if durable_cursor:
                    pending = current
                    emit({'kind': 'checkpoint', 'cursor': current, 'anchor': anchor})
                else:
                    pump.ack(current)
                continue
            if is_image or quoted_image:
                if (message.get('id') != identity or message.get('accountId') != value['account']
                        or message.get('threadId') != target or message.get('direction') != 'incoming'
                        or not loops.sender_id(message.get('senderId')) or target == peer and message['senderId'] != peer):
                    raise wire.WireError('image_source_binding')
                if quoted_image and (not isinstance(message.get('body'), str) or not 0 < len(message['body'].encode()) <= 32768):
                    raise wire.WireError('message_invalid_or_too_large')
                created = sse.timestamp(message.get('createdAt'))
                if not -5 <= time.time() - created <= 300:
                    emit({'kind': 'report', 'stage': 'image_input', 'outcome': 'expired'})
                    pending = current
                    emit({'kind': 'checkpoint', 'cursor': current, 'anchor': anchor})
                    continue
                if target == room and is_image:
                    now = time.monotonic()
                    group_images = {key: item for key, item in group_images.items() if now - item[1] <= 90}
                    if len(group_images) >= 16 and message['senderId'] not in group_images:
                        del group_images[next(iter(group_images))]
                    group_images[message['senderId']] = (identity, now)
                    emit({'kind': 'report', 'stage': 'image_input', 'outcome': 'waiting'})
                    pending = current
                    emit({'kind': 'checkpoint', 'cursor': current, 'anchor': anchor})
                    continue
            elif (not selected._valid_message(message, identity, target) or not 0 < len(message['body'].encode()) <= 32768):
                raise wire.WireError('message_invalid_or_too_large')
            mentioned = target == room and trigger.self_mentioned(message.get('mentions'))
            wakeup = target == room and trigger.triggered(message['body'], mentioned, value.get('groupWakeMode', 'prefix'), value['groupPrefix'])
            if target == room and not trigger.admitted(message['body'], mentioned, value.get('groupWakeMode', 'prefix'),
                    value['groupPrefix'], context=value.get('groupContext', False), image=is_image or quoted_image):
                pending = current
                emit({'kind': 'checkpoint', 'cursor': current, 'anchor': anchor})
                continue
            image_id = identity if is_image or quoted_image else None
            associated = group_images.get(message['senderId']) if target == room and wakeup else None
            if not quoted_image and associated and time.monotonic() - associated[1] <= 90:
                image_id = associated[0]
            record = {'account': value['account'], 'id': identity, 'room_id': target, 'sender_id': message['senderId'],
                      'body': '' if is_image else message['body'], 'timestamp': sse.timestamp(message.get('createdAt')),
                      'origin': 'live', 'direction': 'incoming', 'type': 3 if is_image else 1}
            if image_id:
                try:
                    if quoted_image:
                        image_id = media.quoted_source(identity, target, min(deadline, time.monotonic() + 8))
                    with media_lock:
                        record['image'] = media.read(image_id, min(deadline, time.monotonic() + 10))
                except images.ImageError as error:
                    emit({'kind': 'report', 'stage': 'image_input', 'outcome': 'unavailable',
                          'code': str(error) if str(error) in images.ERROR_CODES else 'image_failed'})
                    pending = current
                    emit({'kind': 'checkpoint', 'cursor': current, 'anchor': anchor})
                    continue
                finally:
                    if target == room:
                        group_images.pop(message['senderId'], None)
                emit({'kind': 'report', 'stage': 'image_input', 'outcome': 'prepared'})
            if mentioned:
                record['mentioned'] = True
            pending = current
            output = {'kind': 'event', 'cursor': current, 'data': record}
            if durable_cursor:
                output['anchor'] = anchor
            emit(output)
        return 0
    except Exception as error:
        code = (str(error) if isinstance(error, (wire.WireError, sse.StreamError)) else
                str(error) if isinstance(error, images.ImageError) and str(error) in images.ERROR_CODES else 'native_worker_failed')
        emit({'kind': 'native_error', 'code': code})
        return 1
    finally:
        if pump:
            pump.close()


if __name__ == '__main__':
    raise SystemExit(main())
