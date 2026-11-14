"""Serialized text/image output, chunk ACKs and bound durable evidence."""
import time


def output(core, s, name, record):
    kind = record.get('kind')
    if kind == 'send':
        s.last_activity_at = int(time.time())
        destination = record.get('destination')
        core.require(name == 'host' and s.armed and s.request is None and not s.voice_relay.pending
                and (s.settings and s.settings['allGroupsEnabled'] and core.trigger.group_id(destination)
                     or destination in s.last_input and time.monotonic() - s.last_input[destination] <= 600),
                'send_not_allowed')
        s.audit = core.audit_value(record, destination)
        if s.send_reviews or not core.output_available():
            s.workers.peers['host'].send({'kind':'receipt','requestId':record['requestId'],
                'receipt':{'account':'wechat-primary','destination':destination,'send_id':'',
                           'state':'recovery_capacity','client_id':'','server_id':''}})
            s.last_output_error = 'recovery_capacity'
            return True
        if s.settings:
            s.audit.update(nativeClientPid=s.pinned.host.client_pid,
                nativeClientTicks=s.pinned.host.client_ticks, segmentBytes=s.settings['segmentBytes'])
        core.owner.write_json(s.audit_path, s.audit)
        s.request = record
        s.workers.peers['native'].send(record)
    elif kind in {'send_image', 'image_begin'}:
        s.last_activity_at = int(time.time())
        destination = record.get('destination')
        core.require(name == 'host' and s.armed and s.settings and s.settings['imageOutputEnabled'] and s.request is None and not s.voice_relay.pending
                and (s.settings['allGroupsEnabled'] and core.trigger.group_id(destination)
                     or destination in s.last_input and time.monotonic() - s.last_input[destination] <= 600),
                'image_send_not_allowed')
        s.image_audit = core.image_audit_value(record)
        cached_source = s.source_cache.lookup(record['image']) if s.source_cache else None
        available = core.output_available()
        if not available or not core.reserve_image_upload(s.pinned, record['image'], reused_source=bool(cached_source)):
            s.workers.peers['host'].send({'kind': 'image_receipt', 'requestId': record['requestId'], 'receipt': {
                'account': 'wechat-primary', 'destination': destination, 'job_id': '', 'state': 'rejected' if available else 'recovery_capacity',
                'server_id': '', 'source_sha256': record['image']['sha256'], 'projection_verified': False}})
            return True
        core.owner.write_json(s.image_audit_path, s.image_audit)
        s.counters['image_output_requests'] += 1
        s.request = record
        if kind == 'image_begin':
            s.image_transfer = core.output_images.Assembly(record['image'])
            s.image_ack = -1
        if cached_source:
            core.owner.write_json(s.cache_path,s.source_cache.value())
        s.workers.peers['native'].send({**record,'sourceId':cached_source} if cached_source else record)
    elif kind == 'image_source_uploaded':
        core.require(name=='native' and s.request is not None and s.request.get('kind') in {'send_image','image_begin'}
                and set(record)=={'kind','requestId','sourceId'} and record['requestId']==s.request['requestId']
                and core.source_cache_module.valid_id(record['sourceId']),'image_source_binding')
        if s.source_cache:
            s.source_cache.put(s.request['image'],record['sourceId'])
            core.owner.write_json(s.cache_path,s.source_cache.value())
    elif kind == 'image_chunk_ack':
        core.require(name == 'native' and s.request is not None and s.request['kind'] == 'image_begin'
                and set(record) == {'kind', 'requestId', 'index'}
                and record['requestId'] == s.request['requestId'] and type(record['index']) is int
                and s.image_ack is not None and record['index'] == s.image_ack, 'image_chunk_ack')
        s.image_ack = None
        s.workers.peers['host'].send(record)
    elif kind == 'image_chunk':
        core.require(name == 'host' and s.request is not None and s.request['kind'] == 'image_begin'
                and set(record) == {'kind', 'requestId', 'index', 'data'}
                and record['requestId'] == s.request['requestId'] and s.image_ack is None
                and s.image_transfer is not None, 'image_chunk_binding')
        s.image_transfer.add(record['index'], record['data'])
        s.image_ack = record['index']
        s.workers.peers['native'].send(record)
    elif kind == 'image_commit':
        core.require(name == 'host' and s.request is not None and s.request['kind'] == 'image_begin'
                and record == {'kind': 'image_commit', 'requestId': s.request['requestId']}
                and s.image_transfer is not None and s.image_ack is None, 'image_commit_binding')
        s.image_transfer.finish()
        s.image_transfer = None
        s.workers.peers['native'].send(record)
    elif kind == 'image_send_intent':
        core.require(name == 'native' and s.request is not None and s.request.get('kind') in {'send_image', 'image_begin'}
                and record.get('requestId') == s.request['requestId'] and core.output_images.job_id(record.get('jobId')),
                'image_send_intent')
        s.image_audit['jobId'] = record['jobId']
        core.owner.write_json(s.image_audit_path, s.image_audit)
    elif kind == 'native_image_result':
        s.last_activity_at = int(time.time())
        core.require(name == 'native' and s.request is not None and s.request.get('kind') in {'send_image', 'image_begin'}
                and record.get('requestId') == s.request['requestId'], 'image_send_receipt')
        receipt = record.get('receipt', {})
        accepted = (record.get('ok') is True and record.get('server_confirmed') is True
            and receipt.get('job_id') == s.image_audit.get('jobId')
            and core.output_images.accepted(receipt, 'wechat-primary', s.request['destination'], s.request['image']['sha256']))
        unsubmitted = record.get('submitted') is False and record.get('server_confirmed') is False
        if unsubmitted and s.source_cache:
            s.source_cache.discard(s.request['image']['sha256'])
            core.owner.write_json(s.cache_path,s.source_cache.value())
        if unsubmitted:
            receipt = {'account': 'wechat-primary', 'destination': s.request['destination'], 'job_id': '',
                'state': 'rejected', 'server_id': '', 'source_sha256': s.request['image']['sha256'],
                'projection_verified': False}
        s.code = record.get('code', 'image_response')
        if accepted or unsubmitted:
            s.image_audit.update(state='accepted' if accepted else 'unknown', code=s.code)
        else:
            s.image_reviews = core.record_image_review(s.image_reviews, s.request, s.image_audit, s.code)
            s.last_image_error = s.code
        marker = core.settle_outcome(s.pinned, s.image_audit_path, s.image_audit, 'media',
            'accepted_verified' if accepted else 'rejected_before_submit' if unsubmitted else 'unconfirmed_quarantined')
        unsubmitted = unsubmitted and marker is not None and marker['classification'] == 'rejected_before_submit'
        if not accepted and not unsubmitted:
            receipt = {**receipt, 'state':'unknown'}
        s.workers.peers['host'].send({'kind': 'image_receipt', 'requestId': s.request['requestId'], 'receipt': receipt})
        s.counters['accepted_parts'] += int(accepted)
        s.counters['image_output_sent'] += int(accepted)
        s.counters['uncertain'] += int(not accepted and not unsubmitted)
        s.request = None
        s.image_transfer = s.image_ack = None
        s.recovery_refresh = True
    elif kind == 'send_intent':
        core.require(name == 'native' and s.request is not None and record.get('requestId') == s.request['requestId']
                and isinstance(record.get('sendId'), str) and core.groups.OPERATION_RE.fullmatch(record['sendId']), 'send_intent')
        s.audit['sendId'] = record['sendId']
        core.owner.write_json(s.audit_path, s.audit)
    elif kind == 'native_result':
        s.last_activity_at = int(time.time())
        core.require(name == 'native' and s.request is not None and record.get('requestId') == s.request['requestId'], 'send_receipt')
        receipt = record.get('receipt', {})
        accepted = (record.get('ok') is True and record.get('server_confirmed') is True
                    and receipt.get('account') == 'wechat-primary' and receipt.get('destination') == s.request['destination']
                    and receipt.get('send_id') == s.audit.get('sendId') and receipt.get('state') == 'accepted')
        if s.settings:
            accepted = accepted and record.get('projection_verified') is True
        s.audit.update(state='accepted' if accepted else 'unknown')
        if not accepted:
            s.audit['confirmationCode'] = record.get('code', 'outcome_unknown')
        core.settle_outcome(s.pinned, s.audit_path, s.audit, 'text',
            'accepted_verified' if accepted else 'unconfirmed_quarantined')
        s.workers.peers['host'].send({'kind': 'receipt', 'requestId': s.request['requestId'],
                           'receipt': receipt if accepted else {**receipt, 'state': 'unknown'}})
        s.counters['accepted_parts'] += int(accepted)
        s.counters['uncertain'] += int(not accepted)
        s.request = None
        s.recovery_refresh = True
        core.require(accepted or core.audit_cleared(s.audit), 'send_requires_review')
    else:
        return False
    return True
