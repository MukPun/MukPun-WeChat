"""One generation handshake, identity/digest admission and readiness."""
import re
from pathlib import Path


def admit_activation(s):
    """Retained review evidence is an admission stop for delivery too.

    A cold start stays 'pending' until the first safe-point capture proves
    coverage, so only an explicit review verdict closes the gate here.
    """
    return not s.recovery_state.endswith('requires_review')


def activate_if_ready(core, s):
    """The pump thread starting is not proof of a connected SSE stream."""
    if s.activated or not (s.armed and s.sse_connected and s.astrbot_connected and s.host_wait['ready']):
        return
    # A replacement generation may only reopen the delivery gate under the
    # same admission the retained durable review state still withholds.
    if not admit_activation(s):
        return
    if s.settings:
        if not s.ready_committed:
            core.recovery_coordinator.mark_ready(core)
            s.ready_committed = True
        if s.send_reviews:
            s.workers.peers['host'].send({'kind':'send_hold','hold':True})
        s.workers.peers['host'].send({'kind': 'activate'})
    s.activated = True
    s.good, s.code = True, 'running'


def handshake(core, s, name, capture, record):
    kind = record.get('kind')
    if kind == 'isolated':
        core.require(name not in s.isolated and record.get('ok') is True and isinstance(record.get('checks'), dict)
                and all(flag is True for flag in record['checks'].values()), 'worker_isolation')
        if name == 'native':
            core.groups.verify_reader(s.workers.units['native'], s.pinned, 'forward_native_worker.py', large_images=bool(s.settings))
        else:
            core.wire.verify_compatibility(record.get('compatibility'), s.host_digest)
            core.trial.verify_host(s.workers.units['host'], s.pinned, 'forward_host_worker.py', hd_images=s.export_state is not None)
            if s.export_state is not None:
                uid = int(core.owned.parse_status(core.owned.read(Path('/proc')/str(core.tap.details(s.workers.units['host'])['MainPID'])/'status'))['Uid'].split()[0])
                core.image_export.assign(*s.export_state, uid, core.owner)
        s.isolated.add(name)
        if name == 'host':
            capture.pending.extend(core.wire.ready_frame(s.host_digest))
        else:
            capture.acknowledge()
    elif kind == 'native_ready':
        core.require(name == 'native' and s.peer_id is None and 'native' in s.isolated, 'worker_readiness')
        s.peer_id, s.boundary = record.get('peer'), record.get('boundary')
        core.require(isinstance(s.peer_id, str) and 0 < len(s.peer_id.encode()) <= 256
                and not s.peer_id.endswith('@chatroom') and s.peer_id != 'filehelper'
                and not s.peer_id.startswith('weixin'), 'contact_binding')
        if s.settings:
            s.self_sender_id = record.get('selfSenderId')
            core.require(core.loops.sender_id(s.self_sender_id) and s.self_sender_id != s.peer_id, 'self_identity_unverified')
            # native_ready is emitted only after the accepted
            # seed's actual Native GET/self-sender verification.
            s.recovery_adapter = core.recovery_checkpoints.ForwarderCapture(core.owner,s.pinned,s.generation,s.recovery_seed)
        s.workers.spawn_host(s.peer_id, record.get('selfSenderId'))
    elif kind == 'host_ready':
        core.require(name == 'host' and 'host' in s.isolated and not s.host_wait['ready']
                and record.get('connected') is True, 'onebot_not_connected')
        if s.settings:
            core.require(isinstance(record.get('after'), str)
                    and re.fullmatch('[0-9]{1,20}', record['after']) is not None
                    and int(record['after']) > 0
                    and isinstance(record.get('anchor'), str)
                    and re.fullmatch('[0-9a-f]{64}', record['anchor']) is not None,
                    'invalid_recovery_anchor')
        s.host_wait['ready'] = True
        counts = record.get('counts', {})
        s.queue_counts = counts
        s.astrbot_connected = True
        start = {'kind': 'start', 'after': record.get('after') or s.boundary}
        if s.settings:
            start['anchor'] = record.get('anchor')
        media_available = core.output_budget.allow_input(**core.media_headroom(s.pinned))
        s.workers.peers['native'].send({'kind':'media_capacity','allow':media_available})
        s.workers.peers['native'].send(start)
    elif kind == 'native_armed':
        core.require(name == 'native' and not s.armed and record.get('sse') is True, 'worker_readiness')
        core.require(s.host_wait['ready'], 'worker_readiness')
        s.armed = True
        activate_if_ready(core, s)
    else:
        return False
    return True
