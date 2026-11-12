"""Validated worker reports and aggregate counters."""
import time
from .session import activate_if_ready


def report(core, s, name, record):
    if name == 'host' and record.get('stage') == 'recovery':
        core.require(record == {'kind':'report','stage':'recovery','code':'recovery_checkpoint_required'}, 'recovery_report')
        s.recovery_refresh = s.recovery_capacity = True
    elif name in {'native', 'host'} and record.get('stage') == 'image_input':
        core.require(set(record) in ({'kind', 'stage', 'outcome'}, {'kind', 'stage', 'outcome', 'code'})
                and record.get('outcome') in {'prepared', 'waiting', 'unavailable', 'expired'}
                and ('code' not in record or record['code'] in core.images.ERROR_CODES), 'image_report')
        s.image_counts[record['outcome']] += 1
        if 'code' in record:
            s.last_image_error = record['code']
    elif record.get('stage') == 'loop_guard':
        allowed = {'own_sender', 'outgoing_event', 'outgoing_message'} if name == 'native' else {'duplicate_input', 'duplicate_action'}
        core.require(set(record) == {'kind', 'stage', 'reason'} and record.get('reason') in allowed, 'loop_report')
        s.loop_counts[record['reason']] += 1
    elif name == 'native' and record.get('stage') == 'sse_connection':
        core.require(set(record) == {'kind', 'stage', 'connected'} and type(record['connected']) is bool, 'worker_readiness')
        s.sse_connected = record['connected']
        activate_if_ready(core, s)
    elif name == 'host' and record.get('stage') == 'link':
        core.require(set(record)=={'kind','stage','connected','failures','decode_rejections','counts'}
            and type(record['connected']) is bool and isinstance(record['counts'],dict)
            and all(type(record[k]) is int and record[k]>=0 for k in ('failures','decode_rejections')),
            'link_report')
        s.host_wait['last'] = time.monotonic()
        s.astrbot_connected = record.get('connected') is True
        s.queue_counts = record.get('counts', {})
        s.onebot_link = {k: record.get(k, 0) for k in ('failures', 'decode_rejections')}
        activate_if_ready(core, s)
    elif name == 'host' and record.get('stage') == 'handoff':
        s.image_counts['forwarded'] += int(record.get('success') is True and 'image_bytes' in record)
        s.counters['forwarded'] += int(record.get('success') is True)
        s.counters['uncertain'] += int(record.get('success') is not True)
        core.require(record.get('success') is True, 'inbox_requires_review')
        core.require(record.get('destination') in {s.peer_id, s.room_id}
                or s.settings and s.settings['allGroupsEnabled'] and core.trigger.group_id(record.get('destination')), 'handoff_binding')
        if core.trigger.group_id(record.get('destination')):
            core.require(type(record.get('group_wakeup')) is bool, 'handoff_wakeup')
            s.group_traffic['wakeup_forwarded' if record['group_wakeup'] else 'context_forwarded'] += 1
        s.last_input[record['destination']] = time.monotonic()
        s.route_counts['private' if record.get('destination') == s.peer_id else 'group']['forwarded'] += 1
    elif name == 'host' and record.get('stage') == 'reply':
        s.recovery_refresh = True
        s.counters['completed_replies'] += int(record.get('success') is True)
        s.counters['uncertain'] += int(record.get('success') is not True)
        s.route_counts['private' if record.get('destination') == s.peer_id else 'group']['replies'] += int(record.get('success') is True)
        if record.get('success') is True and record.get('bytes', 0) > s.largest_reply['bytes']:
            s.largest_reply = {key: record[key] for key in ('bytes', 'parts', 'sha256')}
    elif name == 'host' and record.get('stage') == 'voice_output_validation':
        core.require(set(record) == {'kind', 'stage', 'code'} and record['code'] in core.output_voice.ERRORS,
                'voice_output_validation')
        s.counters['voice_output_rejected'] += 1
    elif name == 'host' and record.get('stage') == 'voice_output':
        core.require(set(record) == {'kind', 'stage', 'outcome'} and record['outcome'] == 'rejected',
                'voice_output_report')
        s.counters['voice_output_rejected'] += 1
    elif name == 'host' and record.get('stage') == 'image_output_validation':
        core.require(set(record) == {'kind', 'stage', 'detail'}, 'image_output_validation')
        s.last_output_error = core.output_images.validate_rejection(record['detail'])
        s.counters['image_output_rejected'] += 1
    elif name == 'host' and record.get('stage') == 'image_output':
        core.require(set(record) == {'kind', 'stage', 'outcome'} and record['outcome'] == 'rejected',
                'image_output_report')
        s.counters['image_output_rejected'] += 1
    elif name == 'host' and record.get('stage') == 'action_unsupported':
        core.require(set(record) == {'kind', 'stage', 'retcode'} and record['retcode'] == 1400,
                'action_report')
        s.counters['unsupported_actions'] += 1
    elif name == 'host' and record.get('stage') == 'action_rejected':
        s.counters['uncertain'] += 1
        core.require(False, 'onebot_action_requires_review')
    elif name != 'native' or record.get('stage') != 'unsupported_incoming':
        raise RuntimeError('report_rejected')
