"""Validated worker reports and aggregate counters."""
import time
import hashlib
from .session import activate_if_ready
from . import outcomes


def track_text(core,s,native_key,destination):
    core.require(isinstance(native_key,str) and core.wire.HEX.fullmatch(native_key)
        and isinstance(destination,str) and 0<len(destination.encode())<=256,'action_report')
    part_key=hashlib.sha256((native_key+':0').encode()).hexdigest()[:32]
    if s.resolved_text.get(part_key)==destination: return
    if s.send_reviews.get(part_key)==destination: return
    # Diagnostic correlation only. A failed action does not withdraw delivery.
    while len(s.send_reviews) >= 32 and part_key not in s.send_reviews:
        del s.send_reviews[next(iter(s.send_reviews))]
    s.send_reviews[part_key]=destination
    outcomes.record(core, 'text', 'unknown', 'outcome_unknown',
        {'key': part_key, 'destinationSha256': hashlib.sha256(destination.encode()).hexdigest()}, retcode=1408)


def late_text(core,s,record):
    core.require(set(record)=={'kind','stage','reconciled','requestId','part_key','destination','send_id'}
        and type(record['reconciled']) is bool
        and isinstance(record['requestId'],str) and core.wire.HEX.fullmatch(record['requestId'])
        and isinstance(record['part_key'],str) and core.wire.HEX.fullmatch(record['part_key'])
        and isinstance(record['destination'],str) and isinstance(record['send_id'],str),'late_receipt_report')
    audit=s.text_audits.get(record['part_key'], s.audit or {})
    if not (record['reconciled'] and s.send_reviews.get(record['part_key'])==record['destination']
            and audit.get('state')=='accepted' and audit.get('requestId')==record['requestId']
            and audit.get('key')==record['part_key'] and audit.get('sendId')==record['send_id']
            and audit.get('destinationSha256')==hashlib.sha256(record['destination'].encode()).hexdigest()):
        return
    del s.send_reviews[record['part_key']]
    while len(s.resolved_text)>=32: del s.resolved_text[next(iter(s.resolved_text))]
    s.resolved_text[record['part_key']]=record['destination']
    s.counters['reconciled_replies']=s.counters.get('reconciled_replies',0)+1
    s.recovery_refresh=True
    outcomes.record(core, 'text', 'reconciled', 'late_receipt', audit)


def report(core, s, name, record):
    if name == 'native' and record.get('stage') == 'backlog_skipped':
        core.require(s.live_only and record == {'kind': 'report', 'stage': 'backlog_skipped'}, 'live_receive_boundary')
        s.skipped_on_start += 1
    elif name=='host' and record.get('stage')=='late_text_receipt':
        late_text(core,s,record)
    elif name=='host' and record.get('stage')=='action_pending':
        core.require(set(record)=={'kind','stage','native_key','destination'},'action_report')
        track_text(core,s,record['native_key'],record['destination'])
    elif name == 'host' and record.get('stage') == 'recovery':
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
        s.counters['uncertain'] += int(record.get('success') is not True and record.get('retcode',1408)==1408)
        if record.get('success') is not True and record.get('retcode',1408)==1408 and 'native_key' in record:
            track_text(core,s,record['native_key'],record.get('destination'))
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
        core.require(set(record) in ({'kind', 'stage', 'retcode'}, {'kind', 'stage', 'retcode', 'code'}) and record['retcode'] == 1400,
                'action_report')
        s.counters['unsupported_actions'] += 1
        outcomes.record(core, 'action', 'rejected', 'unsupported_message_segment', retcode=1400)
    elif name=='host' and record.get('stage')=='action_failed':
        core.require(set(record) in ({'kind','stage','retcode'}, {'kind','stage','retcode','code'}) and type(record['retcode']) is int
            and record['retcode'] in {1400,1403,1501},'action_report')
        s.counters['rejected_actions']=s.counters.get('rejected_actions',0)+1
        outcomes.record(core, 'action', 'rejected', outcomes.action_code(record, 'action_failed'), retcode=record['retcode'])
    elif name == 'host' and record.get('stage') == 'action_rejected':
        core.require(set(record) in ({'kind','stage','retcode'}, {'kind','stage','retcode','code'}) and type(record['retcode']) is int,'action_report')
        if record['retcode']!=1408:
            outcomes.record(core, 'action', 'rejected', outcomes.action_code(record, 'action_requires_review'), retcode=record['retcode'])
            core.require(False,'onebot_action_requires_review')
        outcomes.record(core, 'action', 'unknown', 'outcome_unknown', retcode=1408)
    elif name != 'native' or record.get('stage') != 'unsupported_incoming':
        raise RuntimeError('report_rejected')
