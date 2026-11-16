"""Bounded IPC ingress, media input leases and forwarding."""
import time


from .session import handshake
from .output import output
from .reports import report


def dispatch(core, s, name, capture, record):
    kind = record.get('kind')
    if kind in {'capture_paused','capture_fenced','capture_deferred'}:
        s.recovery_backend.barrier(name,record)
        return
    large_input = False
    if kind in core.input_wire.FRAMES:
        core.require(s.settings and s.settings['imageInputEnabled'] and s.armed, 'image_disabled')  # sticker frames share the image relay; the worker itself rejects type 47 unless stickerInput is armed
        s.last_activity_at = int(time.time())
        header = s.input_relay.handle(name, record, s.workers.peers)
        if header is None:
            return
        record, kind, large_input = header, 'event', True
    if s.settings and s.settings.get('privateOnly', False):
        # Enforce the rollout scope independently of host action validation and
        # Native filtering, including persisted handoff reports/media headers.
        destination = record.get('data', {}).get('room_id') if kind == 'event' else record.get('destination')
        if kind == 'event' or 'destination' in record:
            core.require(destination == s.peer_id, 'private_destination_required')
    voice_destination = record.get('destination')
    voice_allowed = (name == 'host' and s.armed and s.settings
        and (s.settings['allGroupsEnabled'] and core.trigger.group_id(voice_destination)
             or voice_destination in s.last_input and time.monotonic() - s.last_input[voice_destination] <= 600))
    if s.voice_relay.handle(name, record, s.workers.peers, allowed=voice_allowed, busy=s.request is not None):
        s.last_activity_at = int(time.time())
        return
    if handshake(core, s, name, capture, record) or output(core, s, name, record):
        return
    if kind == 'event':
        s.last_activity_at = int(time.time())
        expected = {'kind', 'cursor', 'data', 'anchor'} if s.settings else {'kind', 'cursor', 'data'}
        core.require(name == 'native' and s.armed and set(record) == expected and isinstance(record['data'], dict), 'message_binding')
        data = record['data']
        if 'image' in data:
            core.require(s.settings and s.settings['imageInputEnabled'], 'image_disabled')  # stickerInput feeds the same image field; the worker gate is stickerInputEnabled
            if large_input:
                core.images.validate_metadata(data['image'])
            else:
                core.images.validate(data['image'])
        destination = data.get('room_id')
        is_group = core.trigger.group_id(destination)
        core.require((destination in ({s.peer_id, s.room_id} if s.room_id else {s.peer_id})
                 or s.settings and s.settings['allGroupsEnabled'] and is_group)
                and (destination != s.peer_id or data.get('sender_id') == s.peer_id)
                and data.get('direction') == 'incoming' and data.get('origin') == 'live'
                and data.get('sender_id') != s.self_sender_id
                and (s.settings and s.settings['allGroupsEnabled'] or destination != s.room_id or core.trigger.admitted(data.get('body'), data.get('mentioned', False),
                     s.settings['groupWakeMode'], s.settings['groupPrefix'],
                     context=s.settings['groupContextEnabled'], image='image' in data)), 'message_binding')
        s.counters['received'] += 1
        if is_group:
            wakeup = (data.get('mentioned', False) if s.settings['allGroupsEnabled'] else
                core.trigger.triggered(data['body'], data.get('mentioned', False), s.settings['groupWakeMode'], s.settings['groupPrefix']))
            s.group_traffic['wakeup_received' if wakeup else 'context_received'] += 1
        s.route_counts['private' if destination == s.peer_id else 'group']['received'] += 1
        s.last_input[destination] = time.monotonic()
        if large_input:
            s.input_relay.forward_begin(s.workers.peers)
        else:
            s.workers.peers['host'].send(record)
    elif kind == 'checkpoint':
        core.require(s.settings and name == 'native' and s.armed and set(record) == {'kind', 'cursor', 'anchor'}, 'cursor_checkpoint')
        s.workers.peers['host'].send(record)
    elif kind in {'media_wait', 'media_done'}:
        core.require(name == 'native' and s.armed and s.settings
                and (s.settings['imageInputEnabled'] or s.settings['imageOutputEnabled'])
                and set(record) in ({'kind', 'jobId'}, {'kind', 'jobId', 'prewarm'})
                and record.get('prewarm') in (None, True), 'media_egress_frame')
        if kind == 'media_wait':
            if record.get('prewarm'):
                try:
                    s.media_lease.prewarm(record['jobId'])
                except ValueError:
                    pass
            else:
                s.media_lease.wait(record['jobId'])
        else:
            s.media_lease.done(record['jobId'])
    elif kind == 'ack':
        core.require(name == 'host' and s.armed and set(record) == {'kind', 'cursor'}, 'cursor_ack')
        s.input_relay.complete(record['cursor'])
        s.workers.peers['native'].send(record)
        if s.recovery_state == 'pending':
            s.recovery_refresh = True
    elif kind == 'report':
        report(core, s, name, record)
    else:
        raise RuntimeError('frame_rejected')
