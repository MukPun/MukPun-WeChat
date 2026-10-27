#!/usr/bin/env python3
"""Root pipe broker for a bounded continuous private forwarding pilot.

No Docker, FRP, firewall or host network mutation. No QR initialization. Only the
already approved tester is resolved by the restricted Native-side worker.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent


def module(name):
    path = HERE / (name + '.py')
    if name in {'forward_wire', 'loop_guard', 'image_input', 'image_output', 'group_trigger'} and not path.exists():
        path = HERE.parent / 'wechat_bridge' / (name + '.py')
    spec = importlib.util.spec_from_file_location('_private_forward_' + name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


trial = module('run_bridge_trial')
wire = module('forward_wire')
loops = module('loop_guard')
images = module('image_input')
output_images = module('image_output')
output_budget = module('image_output_budget')
trigger = module('group_trigger')
media_egress = module('media_egress')
trial.ipc = wire
groups, owner, tap, owned, require = trial.groups, trial.owner, trial.tap, trial.owned, trial.require
UNIT = 'astrbot-wechat-private-forwarder.service'
STATUS = owner.CONTROL / 'private-forwarder.json'
FILES = groups.FILES + ('bridge_native_protocol.py', 'forward_native_worker.py', 'forward_sse.py', 'forward_wire.py', 'group_trigger.py', 'loop_guard.py', 'image_native.py', 'image_input.py', 'image_output.py', 'image_send_native.py')
SETTINGS = None  # Only the formal wrapper supplies validated, non-secret settings.
ENTRY = Path(__file__).resolve()


class Duplex(trial.Duplex):
    def tick(self):
        # At most one acknowledged 64KiB image chunk is outstanding. Larger
        # nonblocking writes reduce pipe latency without widening frame caps.
        if self.pending:
            try:
                count = os.write(self.process.stdin.fileno(), self.pending[:65536])
                del self.pending[:count]
            except BlockingIOError:
                pass
        self.drain(.005)
        records, output = [], self.output['stdout']
        while b'\n' in output:
            line, _, rest = output.partition(b'\n')
            output[:] = rest
            records.append(wire.decode(bytes(line)))
        require(len(output) < wire.MAX_FRAME, 'ipc_frame_bound')
        return records

    def drain(self, delay=.05):
        # The historical one-shot capture was capped at 64KiB. Preserve the
        # same bounded design but allow a complete escaped 32KiB text frame.
        for key, _ in self.selector.select(delay):
            chunk = os.read(key.fileobj.fileno(), 4096)
            if not chunk:
                self.selector.unregister(key.fileobj)
                key.fileobj.close()
            else:
                require(sum(map(len, self.output.values())) + len(chunk) <= wire.MAX_FRAME * 2, 'ipc_backpressure')
                self.output[key.data].extend(chunk)


def status(value):
    owner.write_json(STATUS, {'schemaVersion': 1, 'updated': int(time.time()), **value})


def audit_value(record, peer):
    require(set(record) == {'kind', 'requestId', 'destination', 'text', 'key'}
            and record['destination'] == peer and isinstance(record['requestId'], str)
            and wire.HEX.fullmatch(record['requestId']) and isinstance(record['key'], str)
            and wire.HEX.fullmatch(record['key']) and isinstance(record['text'], str)
            and 0 < len(record['text'].encode()) <= 8192, 'send_binding')
    return {'state': 'submitted', 'key': record['key'], 'requestId': record['requestId'],
            'destinationSha256': hashlib.sha256(peer.encode()).hexdigest(),
            'textSha256': hashlib.sha256(record['text'].encode()).hexdigest(), 'bytes': len(record['text'].encode())}


def image_audit_value(record):
    require(set(record) == {'kind', 'requestId', 'destination', 'image', 'key'}
            and isinstance(record.get('requestId'), str) and wire.HEX.fullmatch(record['requestId'])
            and isinstance(record.get('key'), str) and wire.HEX.fullmatch(record['key']), 'image_send_binding')
    if record['kind'] == 'image_begin':
        output_images.Assembly(record['image'])
    else:
        require(record['kind'] == 'send_image', 'image_send_binding')
        output_images.validate(record['image'])
    return {'state': 'submitted', 'key': record['key'], 'requestId': record['requestId'],
            'destinationSha256': hashlib.sha256(record['destination'].encode()).hexdigest(),
            'sourceSha256': record['image']['sha256'], 'bytes': record['image']['bytes']}


def reserve_image_upload(pinned, image):
    path = owner.STATE / 'image-output-budget.json'
    previous = owner.read_json(path) if path.exists() else None
    storage = os.fstatvfs(pinned.host.data_fd)
    memory = Path('/sys/fs/cgroup') / pinned.host.parent
    free_memory = int((memory / 'memory.max').read_text()) - int((memory / 'memory.current').read_text())
    handles = []
    try:
        accounts = os.open('accounts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pinned.host.data_fd)
        handles.append(accounts)
        names = os.listdir(accounts)
        require(len(names) == 1, 'image_budget_accounts')
        account = os.open(names[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=accounts)
        handles.append(account)
        agent = os.open('agent', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=account)
        handles.append(agent)
        database = os.stat('control.sqlite', dir_fd=agent, follow_symlinks=False)
        require(stat.S_ISREG(database.st_mode) and database.st_nlink == 1, 'image_budget_database')
        database_bytes = database.st_size
        for suffix in ('-wal', '-journal'):
            try:
                extra = os.stat('control.sqlite' + suffix, dir_fd=agent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            require(stat.S_ISREG(extra.st_mode), 'image_budget_database')
            database_bytes += extra.st_size
    finally:
        for fd in reversed(handles):
            os.close(fd)
    reserved = output_budget.reserve(previous, image['bytes'],
        storage_free=storage.f_bavail * storage.f_frsize, memory_free=free_memory, database_bytes=database_bytes)
    if reserved is None:
        return False
    owner.write_json(path, reserved)
    return True


def self_proof():
    audit = owner.read_json(owner.STATE / 'forward-send.json')
    require(audit.get('state') == 'accepted', 'self_proof_not_accepted')
    return loops.proof({key: audit.get(key) for key in ('sendId', 'destinationSha256', 'textSha256', 'bytes')})


def ensure_saved_session(settings):
    if settings and not owner.owner_active():
        try:
            previous = owner.read_json(owner.CONTROL / 'status.json')
        except FileNotFoundError:
            # /run is volatile. A machine reboot must restore the durable
            # profile rather than requiring a cached status file or new QR.
            store = owner.companion('session_state_store').SnapshotStore(owner.STATE / 'checkpoints')
            store.prepare()
            require(store.latest() is not None, 'saved_session_missing')
        else:
            require(previous.get('ok') is True and previous.get('accounts') == 1
                    and owner.cleanup_ok(previous.get('cleanup')), 'saved_session_not_ready')
        owner.start_owner(False, seconds=owner.LONG_RUNTIME_SECONDS)
    trial.ensure_session()


def serve():
    require(sys.platform == 'linux' and os.geteuid() == 0, 'linux_root_required')
    owner.private_directory(owner.STATE)
    owner.private_directory(owner.CONTROL)
    lock = os.open(owner.STATE / 'bridge.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    settings = SETTINGS
    audit_path = owner.STATE / ('forward-send.json' if settings else 'private-forward-send.json')
    if audit_path.exists():
        require(owner.read_json(audit_path).get('state') == 'accepted', 'previous_send_requires_review')
    image_audit_path = owner.STATE / 'forward-image-send.json'
    if image_audit_path.exists():
        require(owner.read_json(image_audit_path).get('state') in {'accepted', 'rejected'},
                'previous_image_send_requires_review')
    token = trial.read_onebot_token()
    ensure_saved_session(settings)
    baseline = {kind: owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = tap.read_network(), tap.business_snapshot()
    pinned = groups.PinnedNative()
    peers, started = {}, []
    bundle = None
    media_lease = None
    interrupted = False
    def stop(_number, _frame):
        nonlocal interrupted
        interrupted = True
    handlers = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT)}
    counters = {'received': 0, 'forwarded': 0, 'accepted_parts': 0, 'completed_replies': 0, 'uncertain': 0,
                'unsupported_actions': 0, 'image_output_requests': 0, 'image_output_sent': 0,
                'image_output_rejected': 0}
    route_counts = {key: {'received': 0, 'forwarded': 0, 'replies': 0} for key in ('private', 'group')}
    loop_counts = {key: 0 for key in loops.REASONS}
    image_counts = {key: 0 for key in ('prepared', 'waiting', 'unavailable', 'expired', 'forwarded')}
    group_traffic = {key: 0 for key in ('context_received', 'wakeup_received', 'context_forwarded', 'wakeup_forwarded')}
    last_image_error = None
    last_output_error = None
    self_sender_id = None
    largest_reply = {'bytes': 0, 'parts': 0}
    resources = {}
    onebot_link = {'failures': 0, 'decode_rejections': 0}
    started_at = int(time.time())
    good, code, clean = False, 'starting', False
    try:
        pinned.open()
        media_lease = media_egress.Lease(sys.modules[__name__], pinned)
        seconds = min(settings['runtimeSeconds'] if settings else 3300, pinned.remaining_seconds() - 40)
        require(seconds >= 600, 'native_session_expiring')
        room_id = None
        if settings:
            _, binding = groups.load_send_binding(pinned, settings['groupName'])
            room_id = binding['roomId']
        bundle = Path(tempfile.mkdtemp(prefix='astrbot-wechat-forward.', dir='/var/tmp'))
        for name in FILES:
            source = HERE / name
            if name in {'forward_wire.py', 'group_trigger.py', 'loop_guard.py', 'image_input.py', 'image_output.py'} and not source.exists():
                source = HERE.parent / 'wechat_bridge' / name
            groups.legacy.write_public(bundle, name, groups.legacy.source_text(source))
        os.chmod(bundle, 0o755)
        native_unit = groups.READER_PREFIX + secrets.token_hex(6) + '.service'
        host_unit = 'astrbot-wechat-private-host-' + secrets.token_hex(6) + '.service'
        legacy_generation = 'native:' + str(pinned.host.client_pid) + ':' + str(pinned.host.client_ticks)
        generation = 'profile:' + hashlib.sha256(pinned.token().encode()).hexdigest() if settings else legacy_generation
        def launch(name, unit, properties, entry, config):
            tap.require_unit_absent(unit)
            arguments = ['/usr/bin/systemd-run', '--system', '--quiet', '--collect', '--wait', '--pipe', '--unit=' + unit,
                         *properties, '/usr/bin/python3', '-I', '-B', entry]
            started.append(unit)
            peers[name] = Duplex(arguments, config)
        native_config = {'account': 'wechat-primary', 'token': pinned.token(), 'runtimeSeconds': seconds}
        if settings:
            native_config.update(privateName=settings['privateContact'], groupName=settings['groupName'],
                                 roomId=room_id, groupPrefix=settings['groupPrefix'], sendInterval=settings['sendIntervalSeconds'],
                                 groupWakeMode=settings['groupWakeMode'], selfProof=self_proof(), imageInput=settings['imageInputEnabled'],
                                 groupContext=settings['groupContextEnabled'], imageOutput=settings['imageOutputEnabled'])
        launch('native', native_unit, groups.reader_properties(bundle, 'awpf-' + secrets.token_hex(4),
               pinned.host.parent, pinned.netfd, seconds), '/opt/forward_native_worker.py', native_config)
        del native_config
        peer_id = None
        boundary = None
        isolated = set()
        armed = False
        sse_connected = False
        astrbot_connected = False
        queue_counts = {}
        request = None
        image_transfer = None
        image_ack = None
        audit = None
        last_input = {}
        # Clean up before either worker's own safety deadline, so an ordinary
        # lifetime limit is not misclassified as a crash and auto-restarted.
        deadline = time.monotonic() + seconds - 15
        next_status = 0.
        while not interrupted and time.monotonic() < deadline:
            for name, capture in tuple(peers.items()):
                for record in capture.tick():
                    kind = record.get('kind')
                    if kind == 'isolated':
                        require(name not in isolated and record.get('ok') is True and isinstance(record.get('checks'), dict)
                                and all(flag is True for flag in record['checks'].values()), 'worker_isolation')
                        if name == 'native':
                            groups.verify_reader(native_unit, pinned, 'forward_native_worker.py')
                        else:
                            trial.verify_host(host_unit, pinned, 'forward_host_worker.py')
                        isolated.add(name)
                        capture.acknowledge()
                    elif kind == 'native_ready':
                        require(name == 'native' and peer_id is None and 'native' in isolated, 'worker_readiness')
                        peer_id, boundary = record.get('peer'), record.get('boundary')
                        require(isinstance(peer_id, str) and 0 < len(peer_id.encode()) <= 256
                                and not peer_id.endswith('@chatroom') and peer_id != 'filehelper'
                                and not peer_id.startswith('weixin'), 'contact_binding')
                        host_config = {'account': 'wechat-primary', 'peer': peer_id, 'generation': generation,
                                       'runtimeSeconds': seconds, 'onebotToken': token}
                        if settings:
                            self_sender_id = record.get('selfSenderId')
                            require(loops.sender_id(self_sender_id) and self_sender_id != peer_id, 'self_identity_unverified')
                            host_config.update(roomId=room_id, groupName=settings['groupName'], privateName=settings['privateContact'],
                                               groupPrefix=settings['groupPrefix'], sendInterval=settings['sendIntervalSeconds'],
                                               segmentBytes=settings['segmentBytes'], legacyGeneration=legacy_generation,
                                               groupWakeMode=settings['groupWakeMode'], selfSenderId=self_sender_id,
                                               imageInput=settings['imageInputEnabled'], groupContext=settings['groupContextEnabled'],
                                               imageOutput=settings['imageOutputEnabled'])
                        launch('host', host_unit, trial.host_properties(HERE / 'verification', seconds),
                               '/opt/bridge/scripts/forward_host_worker.py', host_config)
                        del host_config
                        token = None
                    elif kind == 'host_ready':
                        require(name == 'host' and 'host' in isolated and record.get('connected') is True, 'onebot_not_connected')
                        counts = record.get('counts', {})
                        queue_counts = counts
                        astrbot_connected = True
                        require(not counts.get('uncertain'), 'inbox_requires_review')
                        start = {'kind': 'start', 'after': record.get('after') or boundary}
                        if settings:
                            start['anchor'] = record.get('anchor')
                        peers['native'].send(start)
                    elif kind == 'native_armed':
                        require(name == 'native' and not armed and record.get('sse') is True, 'worker_readiness')
                        armed, good, code = True, True, 'running'
                        if settings:
                            peers['host'].send({'kind': 'activate'})
                    elif kind == 'event':
                        expected = {'kind', 'cursor', 'data', 'anchor'} if settings else {'kind', 'cursor', 'data'}
                        require(name == 'native' and armed and set(record) == expected and isinstance(record['data'], dict), 'message_binding')
                        data = record['data']
                        if 'image' in data:
                            require(settings and settings['imageInputEnabled'], 'image_disabled')
                            images.validate(data['image'])
                        destination = data.get('room_id')
                        require(destination in ({peer_id, room_id} if room_id else {peer_id})
                                and (destination != peer_id or data.get('sender_id') == peer_id)
                                and data.get('direction') == 'incoming' and data.get('origin') == 'live'
                                and data.get('sender_id') != self_sender_id
                                and (destination != room_id or trigger.admitted(data.get('body'), data.get('mentioned', False),
                                     settings['groupWakeMode'], settings['groupPrefix'],
                                     context=settings['groupContextEnabled'], image='image' in data)), 'message_binding')
                        counters['received'] += 1
                        if destination == room_id:
                            wakeup = trigger.triggered(data['body'], data.get('mentioned', False), settings['groupWakeMode'], settings['groupPrefix'])
                            group_traffic['wakeup_received' if wakeup else 'context_received'] += 1
                        route_counts['private' if destination == peer_id else 'group']['received'] += 1
                        last_input[destination] = time.monotonic()
                        peers['host'].send(record)
                    elif kind == 'checkpoint':
                        require(settings and name == 'native' and armed and set(record) == {'kind', 'cursor', 'anchor'}, 'cursor_checkpoint')
                        peers['host'].send(record)
                    elif kind in {'media_wait', 'media_done'}:
                        require(name == 'native' and armed and settings
                                and (settings['imageInputEnabled'] or settings['imageOutputEnabled'])
                                and set(record) == {'kind', 'jobId'}, 'media_egress_frame')
                        if kind == 'media_wait':
                            media_lease.wait(record['jobId'])
                        else:
                            media_lease.done(record['jobId'])
                    elif kind == 'ack':
                        require(name == 'host' and armed and set(record) == {'kind', 'cursor'}, 'cursor_ack')
                        peers['native'].send(record)
                    elif kind == 'send':
                        destination = record.get('destination')
                        require(name == 'host' and armed and request is None
                                and destination in last_input and time.monotonic() - last_input[destination] <= 600,
                                'send_not_allowed')
                        audit = audit_value(record, destination)
                        owner.write_json(audit_path, audit)
                        request = record
                        peers['native'].send(record)
                    elif kind in {'send_image', 'image_begin'}:
                        destination = record.get('destination')
                        require(name == 'host' and armed and settings and settings['imageOutputEnabled'] and request is None
                                and destination in last_input and time.monotonic() - last_input[destination] <= 600,
                                'image_send_not_allowed')
                        image_audit = image_audit_value(record)
                        if not reserve_image_upload(pinned, record['image']):
                            peers['host'].send({'kind': 'image_receipt', 'requestId': record['requestId'], 'receipt': {
                                'account': 'wechat-primary', 'destination': destination, 'job_id': '', 'state': 'rejected',
                                'server_id': '', 'source_sha256': record['image']['sha256'], 'projection_verified': False}})
                            continue
                        owner.write_json(image_audit_path, image_audit)
                        counters['image_output_requests'] += 1
                        request = record
                        if kind == 'image_begin':
                            image_transfer = output_images.Assembly(record['image'])
                            image_ack = -1
                        peers['native'].send(record)
                    elif kind == 'image_chunk_ack':
                        require(name == 'native' and request is not None and request['kind'] == 'image_begin'
                                and set(record) == {'kind', 'requestId', 'index'}
                                and record['requestId'] == request['requestId'] and type(record['index']) is int
                                and image_ack is not None and record['index'] == image_ack, 'image_chunk_ack')
                        image_ack = None
                        peers['host'].send(record)
                    elif kind == 'image_chunk':
                        require(name == 'host' and request is not None and request['kind'] == 'image_begin'
                                and set(record) == {'kind', 'requestId', 'index', 'data'}
                                and record['requestId'] == request['requestId'] and image_ack is None
                                and image_transfer is not None, 'image_chunk_binding')
                        image_transfer.add(record['index'], record['data'])
                        image_ack = record['index']
                        peers['native'].send(record)
                    elif kind == 'image_commit':
                        require(name == 'host' and request is not None and request['kind'] == 'image_begin'
                                and record == {'kind': 'image_commit', 'requestId': request['requestId']}
                                and image_transfer is not None and image_ack is None, 'image_commit_binding')
                        image_transfer.finish()
                        image_transfer = None
                        peers['native'].send(record)
                    elif kind == 'image_send_intent':
                        require(name == 'native' and request is not None and request.get('kind') in {'send_image', 'image_begin'}
                                and record.get('requestId') == request['requestId'] and output_images.job_id(record.get('jobId')),
                                'image_send_intent')
                        image_audit['jobId'] = record['jobId']
                        owner.write_json(image_audit_path, image_audit)
                    elif kind == 'native_image_result':
                        require(name == 'native' and request is not None and request.get('kind') in {'send_image', 'image_begin'}
                                and record.get('requestId') == request['requestId'], 'image_send_receipt')
                        receipt = record.get('receipt', {})
                        accepted = (record.get('ok') is True and record.get('server_confirmed') is True
                            and receipt.get('job_id') == image_audit.get('jobId')
                            and output_images.accepted(receipt, 'wechat-primary', request['destination'], request['image']['sha256']))
                        unsubmitted = record.get('submitted') is False and record.get('server_confirmed') is False
                        if unsubmitted:
                            receipt = {'account': 'wechat-primary', 'destination': request['destination'], 'job_id': '',
                                'state': 'rejected', 'server_id': '', 'source_sha256': request['image']['sha256'],
                                'projection_verified': False}
                        image_audit.update(state='accepted' if accepted else 'rejected' if unsubmitted else 'unknown',
                                           code=record.get('code', 'image_response'))
                        owner.write_json(image_audit_path, image_audit)
                        peers['host'].send({'kind': 'image_receipt', 'requestId': request['requestId'], 'receipt': receipt})
                        counters['accepted_parts'] += int(accepted)
                        counters['image_output_sent'] += int(accepted)
                        counters['uncertain'] += int(not accepted and not unsubmitted)
                        request = None
                        image_transfer = image_ack = None
                        require(accepted or unsubmitted, 'image_send_requires_review')
                    elif kind == 'send_intent':
                        require(name == 'native' and request is not None and record.get('requestId') == request['requestId']
                                and isinstance(record.get('sendId'), str) and groups.OPERATION_RE.fullmatch(record['sendId']), 'send_intent')
                        audit['sendId'] = record['sendId']
                        owner.write_json(audit_path, audit)
                    elif kind == 'native_result':
                        require(name == 'native' and request is not None and record.get('requestId') == request['requestId'], 'send_receipt')
                        receipt = record.get('receipt', {})
                        accepted = (record.get('ok') is True and record.get('server_confirmed') is True
                                    and receipt.get('account') == 'wechat-primary' and receipt.get('destination') == request['destination']
                                    and receipt.get('send_id') == audit.get('sendId') and receipt.get('state') == 'accepted')
                        if settings:
                            accepted = accepted and record.get('projection_verified') is True
                        audit.update(state='accepted' if accepted else 'unknown')
                        owner.write_json(audit_path, audit)
                        peers['host'].send({'kind': 'receipt', 'requestId': request['requestId'], 'receipt': receipt})
                        counters['accepted_parts'] += int(accepted)
                        counters['uncertain'] += int(not accepted)
                        request = None
                        require(accepted, 'send_requires_review')
                    elif kind == 'report':
                        if name in {'native', 'host'} and record.get('stage') == 'image_input':
                            require(set(record) in ({'kind', 'stage', 'outcome'}, {'kind', 'stage', 'outcome', 'code'})
                                    and record.get('outcome') in {'prepared', 'waiting', 'unavailable', 'expired'}
                                    and ('code' not in record or record['code'] in images.ERROR_CODES), 'image_report')
                            image_counts[record['outcome']] += 1
                            if 'code' in record:
                                last_image_error = record['code']
                        elif record.get('stage') == 'loop_guard':
                            allowed = {'own_sender', 'outgoing_event', 'outgoing_message'} if name == 'native' else {'duplicate_input', 'duplicate_action'}
                            require(set(record) == {'kind', 'stage', 'reason'} and record.get('reason') in allowed, 'loop_report')
                            loop_counts[record['reason']] += 1
                        elif name == 'native' and record.get('stage') == 'sse_connection':
                            sse_connected = record.get('connected') is True
                        elif name == 'host' and record.get('stage') == 'link':
                            astrbot_connected = record.get('connected') is True
                            queue_counts = record.get('counts', {})
                            onebot_link = {k: record.get(k, 0) for k in ('failures', 'decode_rejections')}
                        elif name == 'host' and record.get('stage') == 'handoff':
                            image_counts['forwarded'] += int(record.get('success') is True and 'image_bytes' in record)
                            counters['forwarded'] += int(record.get('success') is True)
                            counters['uncertain'] += int(record.get('success') is not True)
                            require(record.get('success') is True, 'inbox_requires_review')
                            require(record.get('destination') in {peer_id, room_id}, 'handoff_binding')
                            if record.get('destination') == room_id:
                                require(type(record.get('group_wakeup')) is bool, 'handoff_wakeup')
                                group_traffic['wakeup_forwarded' if record['group_wakeup'] else 'context_forwarded'] += 1
                            last_input[record['destination']] = time.monotonic()
                            route_counts['private' if record.get('destination') == peer_id else 'group']['forwarded'] += 1
                        elif name == 'host' and record.get('stage') == 'reply':
                            counters['completed_replies'] += int(record.get('success') is True)
                            counters['uncertain'] += int(record.get('success') is not True)
                            route_counts['private' if record.get('destination') == peer_id else 'group']['replies'] += int(record.get('success') is True)
                            if record.get('success') is True and record.get('bytes', 0) > largest_reply['bytes']:
                                largest_reply = {key: record[key] for key in ('bytes', 'parts', 'sha256')}
                        elif name == 'host' and record.get('stage') == 'image_output_validation':
                            require(set(record) == {'kind', 'stage', 'detail'}, 'image_output_validation')
                            last_output_error = output_images.validate_rejection(record['detail'])
                            counters['image_output_rejected'] += 1
                        elif name == 'host' and record.get('stage') == 'image_output':
                            require(set(record) == {'kind', 'stage', 'outcome'} and record['outcome'] == 'rejected',
                                    'image_output_report')
                            counters['image_output_rejected'] += 1
                        elif name == 'host' and record.get('stage') == 'action_unsupported':
                            require(set(record) == {'kind', 'stage', 'retcode'} and record['retcode'] == 1400,
                                    'action_report')
                            counters['unsupported_actions'] += 1
                        elif name == 'host' and record.get('stage') == 'action_rejected':
                            counters['uncertain'] += 1
                            require(False, 'onebot_action_requires_review')
                        elif name != 'native' or record.get('stage') != 'unsupported_incoming':
                            raise RuntimeError('report_rejected')
                    elif kind in {'native_error', 'host_error'}:
                        code = record.get('code', 'worker_failed')
                        raise RuntimeError('worker_failed')
                    else:
                        raise RuntimeError('frame_rejected')
            require(pinned.unchanged(), 'native_changed')
            require(all(item.process.poll() is None for item in peers.values()), 'worker_exited')
            if time.monotonic() >= next_status:
                if settings:
                    for label, group in (('native_parent', '/' + pinned.host.parent), ('onebot', '/system.slice/' + host_unit),
                                         ('controller', '/system.slice/' + UNIT)):
                        base = Path('/sys/fs/cgroup') / group.lstrip('/')
                        if base.is_dir():
                            events = dict(line.split() for line in (base / 'memory.events').read_text().splitlines())
                            resources[label] = {'memory_bytes': int((base / 'memory.current').read_text()),
                                                'oom_kills': int(events['oom_kill'])}
                    filesystem = os.fstatvfs(pinned.host.data_fd)
                    resources['native_storage'] = {'used_bytes': (filesystem.f_blocks - filesystem.f_bavail) * filesystem.f_frsize,
                                                   'total_bytes': filesystem.f_blocks * filesystem.f_frsize}
                status({'active': True, 'ready': armed and sse_connected and astrbot_connected, 'code': code,
                        'private_tester': settings['privateContact'] if settings else '测试联系人', 'sse': sse_connected, 'astrbot_connected': astrbot_connected,
                        'queue': queue_counts, 'new_qr': False,
                        'self_identity_verified': self_sender_id is not None, 'loop_guard': loop_counts,
                        'image_input_enabled': settings['imageInputEnabled'] if settings else False, 'images': image_counts,
                        'image_output_enabled': settings['imageOutputEnabled'] if settings else False,
                        'onebot_link': onebot_link,
                        'last_image_error': last_image_error,
                        'last_output_error': last_output_error,
                        'image_egress_active': bool(media_lease and media_lease.changed),
                        'group': settings['groupName'] if settings else None, 'routes': route_counts, 'largest_reply': largest_reply,
                        'group_wake_mode': settings['groupWakeMode'] if settings else 'prefix',
                        'group_context_enabled': settings['groupContextEnabled'] if settings else False, 'group_traffic': group_traffic,
                        'started_at': started_at, 'uptime_seconds': int(time.time()) - started_at, 'resources': resources,
                        'remaining_seconds': max(0, int(deadline - time.monotonic())), **counters})
                next_status = time.monotonic() + 2
        code = 'stopped' if interrupted else 'pilot_expired'
    except Exception as error:
        good = False
        code = getattr(error, 'stage', code if code not in {'starting', 'running'} else 'forwarder_failed')
    finally:
        if media_lease is not None:
            try:
                media_lease.close()
            except Exception:
                good = False
        for unit in reversed(started):
            try:
                tap.stop_unit(unit, True)
            except Exception:
                good = False
        for capture in peers.values():
            try:
                capture.close()
            except Exception:
                good = False
        unchanged = pinned.unchanged() if pinned.host is not None else False
        pinned.close()
        if bundle is not None:
            for name in FILES:
                (bundle / name).unlink(missing_ok=True)
            bundle.rmdir()
        flags, failures = tap.compare_baselines(baseline, network, business)
        clean = not failures and unchanged
        status({'active': False, 'ready': False, 'code': code, 'ok': good and clean,
                'cleanup_ok': clean, 'native_session_unchanged': unchanged, 'routes': route_counts,
                'self_identity_verified': self_sender_id is not None, 'loop_guard': loop_counts,
                'images': image_counts, 'group_traffic': group_traffic,
                'last_output_error': last_output_error,
                'image_output_enabled': settings['imageOutputEnabled'] if settings else False, 'onebot_link': onebot_link,
                'largest_reply': largest_reply, 'resources': resources, 'uptime_seconds': int(time.time()) - started_at,
                'queue': queue_counts, **flags, **counters})
        for number, handler in handlers.items():
            signal.signal(number, handler)
        os.close(lock)
    return 0 if good and clean else 1


def main():
    require(os.geteuid() == 0, 'linux_root_required')
    args = sys.argv[1:]
    if args == ['--serve']:
        return serve()
    if args == ['status']:
        owner.wire(owner.read_json(STATUS))
        return 0
    if args == ['stop']:
        result = subprocess.run(['/usr/bin/systemctl', 'stop', UNIT], capture_output=True, timeout=25)
        return result.returncode
    if args == ['restart']:
        result = subprocess.run(['/usr/bin/systemctl', 'stop', UNIT], capture_output=True, timeout=25)
        require(result.returncode == 0, 'forwarder_stop_failed')
        previous = owner.read_json(STATUS)
        require(previous.get('cleanup_ok') is True and previous.get('native_session_unchanged') is True
                and previous.get('uncertain') == 0, 'forwarder_requires_review')
        owner.write_json(owner.STATE / 'last-private-forwarder-completion.json', previous)
        owner.wire({'stage': 'private_forwarder_previous_run', 'ok': True,
                    'native_session_unchanged': True, 'cleanup_ok': True,
                    'received': previous.get('received'), 'forwarded': previous.get('forwarded'),
                    'completed_replies': previous.get('completed_replies'),
                    'business_processes_unchanged': previous.get('business_processes_unchanged'),
                    'host_network_unchanged': previous.get('host_network_unchanged')})
        args = ['start']
    if args != ['start']:
        return 2
    tap.require_unit_absent(UNIT)
    owner.private_directory(owner.CONTROL)
    status({'active': False, 'ready': False, 'code': 'starting'})
    values = ['MemoryMax=64M', 'MemoryHigh=48M', 'MemorySwapMax=0', 'CPUQuota=5%', 'TasksMax=16',
              'Nice=19', 'RuntimeMaxSec=' + str(SETTINGS['runtimeSeconds'] + 120 if SETTINGS else 3420),
              'TimeoutStopSec=20', 'KillMode=control-group', 'UMask=0077',
              'LimitCORE=0', 'LimitFSIZE=8388608', 'ProtectKernelModules=yes', 'StandardOutput=null', 'StandardError=null']
    result = subprocess.run(['/usr/bin/systemd-run', '--quiet', '--collect', '--unit=' + UNIT,
                             *['--property=' + value for value in values], '/usr/bin/python3', '-I', '-B',
                             str(ENTRY), '--serve'], capture_output=True, timeout=5)
    require(result.returncode == 0, 'forwarder_start_failed')
    owner.wire({'stage': 'private_forwarder_start', 'ok': True, 'new_qr': False})
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        if sys.argv[1:] == ['--serve']:
            try:
                status({'active': False, 'ready': False, 'code': getattr(error, 'stage', 'preflight_failed')})
            except Exception:
                pass
        owner.wire({'stage': 'private_forwarder', 'ok': False, 'code': 'preflight_failed'})
        raise SystemExit(1)
