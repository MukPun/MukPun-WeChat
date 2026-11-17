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
import resource
import stat
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent


def module(name):
    path = HERE / (name + '.py')
    if name in {'forward_wire', 'loop_guard', 'image_input', 'image_input_wire', 'image_output', 'voice_output', 'group_trigger'} and not path.exists():
        path = HERE.parent / 'wechat_bridge' / (name + '.py')
    spec = importlib.util.spec_from_file_location('_private_forward_' + name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


trial = module('forward_runtime_support')
paths = module('project_paths')
wire = module('forward_wire')
loops = module('loop_guard')
images = module('image_input')
input_wire = module('image_input_wire')
image_export = module('image_input_export')
output_images = module('image_output')
output_voice = module('voice_output')
voice_forwarding = module('voice_forwarding')
output_budget = module('image_output_budget')
source_cache_module = module('image_source_cache')
trigger = module('group_trigger')
media_egress = module('media_egress')
text_recovery = module('text_send_recovery')
image_recovery = module('image_send_recovery')
health = module('native_health_probe')
recovery_dispositions = module('recovery_dispositions')
recovery_checkpoints = module('recovery_checkpoint')
recovery_coordinator = module('recovery_coordinator')
live_startup = module('live_forwarding_startup')
recovery_diagnostics = module('recovery_diagnostics')
supervisor = module('worker_supervisor')
from wechat_bridge import runtime
trial.ipc = wire
groups, owner, tap, owned, require = trial.groups, trial.owner, trial.tap, trial.owned, trial.require
UNIT = 'astrbot-wechat-private-forwarder.service'
STATUS = owner.CONTROL / 'private-forwarder.json'
delivery = module('runtime_manifest')
FILES = delivery.role_files('native')
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
            except BrokenPipeError:
                self.pending.clear()  # Drain its final fixed error before EOF.
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
            # Drain a bounded media block per turn. Repeated 4KiB reads spend
            # the assembly deadline on controller scheduling under CPU limits.
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                self.selector.unregister(key.fileobj)
                key.fileobj.close()
            else:
                require(sum(map(len, self.output.values())) + len(chunk) <= wire.MAX_FRAME * 2, 'ipc_backpressure')
                self.output[key.data].extend(chunk)


def status(value):
    owner.write_json(STATUS, {'schemaVersion': 1, 'updated': int(time.time()), **value})


def remember_failure(code):
    # Startup admission failures must not erase the preceding runtime cause.
    owner.write_json(owner.STATE / 'last-forwarder-failure.json',
                     {'schemaVersion': 1, 'updated': int(time.time()), 'code': code})


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


def media_headroom(pinned):
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
    file_limit = min(resource.prlimit(pinned.host.client_pid, resource.RLIMIT_FSIZE))
    storage_free = output_budget.usable_storage(storage.f_blocks*storage.f_frsize,
        storage.f_bavail*storage.f_frsize, database_bytes)
    if getattr(pinned.host, 'storage_mode', 'snapshot') == 'persistent':
        persistent = owner.companion('session_persistent_store')
        try:
            storage_free = min(storage_free, persistent.storage_headroom(pinned.host.data_fd, pinned.host.client_uid))
        except persistent.PersistentError as error:
            require(error.code != 'persistent_unsafe', 'image_budget_database')
            storage_free = 0  # Defer new media while inventory/headroom is uncertain.
    return {'storage_free':storage_free,'memory_free':free_memory,
            'database_bytes':database_bytes,'file_limit':file_limit,
            'database_copies':2 if storage.f_blocks*storage.f_frsize in (192*1024*1024, 2048*1024*1024) else 0}


def reserve_image_upload(pinned, image, *, reused_source=False):
    path = owner.STATE / 'image-output-budget.json'
    previous = owner.read_json(path) if path.exists() else None
    capacity = media_headroom(pinned)
    file_limit = capacity['file_limit']
    if file_limit not in {16*1024*1024,32*1024*1024,128*1024*1024,384*1024*1024,768*1024*1024}:
        return False
    reserved = output_budget.reserve(previous, image['bytes'],
        storage_free=capacity['storage_free'], memory_free=capacity['memory_free'], database_bytes=capacity['database_bytes'],
        file_limit=file_limit, reused_source=reused_source, database_copies=capacity.get('database_copies',0))
    if reserved is None:
        return False
    owner.write_json(path, reserved)
    return True


def reserve_voice_upload(pinned, audio):
    path = owner.STATE / 'voice-output-budget.json'
    previous = owner.read_json(path) if path.exists() else None
    reserved = output_budget.reserve(previous, audio['bytes'], source_limit=output_voice.MAX_AUDIO,
                                     **media_headroom(pinned))
    if reserved is None:
        return False
    owner.write_json(path, reserved)
    return True


def settle_outcome(pinned, path, audit, kind, classification):
    """Persist exact evidence before releasing the durable gate or any receipt."""
    try:
        marker = recovery_dispositions.record_native_outcome(sys.modules[__name__], pinned, audit, kind, classification, path=path)
    except recovery_dispositions.pending.PendingSend as error:
        # Persist this exact live request without inventing a restore permit.
        recovery_dispositions.pending.preserve(sys.modules[__name__], pinned, path, audit, kind, error.profile)
        return None
    if marker is not None:
        audit['recoveryDisposition'] = marker
        recovery_dispositions.pending.archive(sys.modules[__name__], path, audit, kind)
        recovery_dispositions.write_evidence(sys.modules[__name__], path, audit)
    else:
        owner.write_json(path, audit)
    return marker


def output_available():
    return recovery_dispositions.output_available(sys.modules[__name__])


def audit_cleared(audit):
    return recovery_dispositions.audit_cleared(sys.modules[__name__], audit)


def audit_preserved(pinned, audit):
    return audit_cleared(audit) or recovery_dispositions.pending.covers(sys.modules[__name__], pinned, audit)


def self_proof():
    audit = owner.read_json(owner.STATE / 'forward-send.json')
    require(audit.get('state') == 'accepted', 'self_proof_not_accepted')
    return loops.proof({key: audit.get(key) for key in ('sendId', 'destinationSha256', 'textSha256', 'bytes')})


def held_image_allowed(settings, audit, hold, *, native_identity=None):
    """One root-private, user-approved image hold; never a send-success receipt."""
    keys = {'schemaVersion', 'mode', 'auditSha256', 'jobId', 'nativeClientPid',
            'nativeClientTicks', 'approvedAt'}
    if (not isinstance(settings, dict) or type(settings.get('imageOutputEnabled')) is not bool
            or not isinstance(audit, dict) or audit.get('state') != 'unknown'
            or audit.get('code') != 'image_needs_review' or not output_images.job_id(audit.get('jobId'))
            or not isinstance(hold, dict) or set(hold) != keys
            or type(hold['schemaVersion']) is not int or hold['schemaVersion'] != 1
            or hold['mode'] not in ('text_only', 'new_images') or hold['jobId'] != audit['jobId']
            or hold['mode'] == 'text_only' and settings['imageOutputEnabled'] is not False
            or any(type(hold[key]) is not int or hold[key] <= minimum for key, minimum in
                   (('nativeClientPid', 1), ('nativeClientTicks', 0), ('approvedAt', 0)))):
        return False
    try:
        digest = hashlib.sha256(json.dumps(audit, sort_keys=True, separators=(',', ':'),
                                          allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        return False
    return (hold['auditSha256'] == digest and (native_identity is None or native_identity ==
            (hold['nativeClientPid'], hold['nativeClientTicks'])))


REVIEWS = 'forward-image-review.json'
REVIEW_LIMIT = 32


def record_image_review(reviews, request, audit, code):
    """Settle a gate audit whose outcome never came back confirmed.

    A single lost poll must not end the bridge: the next start would then
    wait for a human review of this very file. The gate records the
    reviewed outcome so startup proceeds, while the trail keeps what is
    actually known - the upload left this host unconfirmed.
    """
    require(isinstance(audit, dict) and isinstance(request, dict)
            and isinstance(request.get('image'), dict), 'image_review_binding')
    entry = {'updated': int(time.time()), 'requestId': request['requestId'],
             'destination': request['destination'], 'sourceSha256': request['image']['sha256'],
             'bytes': request['image']['bytes'], 'jobId': audit.get('jobId'),
             'code': code, 'outcome': 'unconfirmed'}
    trail = (list(reviews) + [entry])[-REVIEW_LIMIT:]
    owner.write_json(owner.STATE / REVIEWS, {'schemaVersion': 1, 'reviews': trail})
    audit.update(state='unknown', code=code, outcome='unconfirmed')
    return trail


def image_send_preflight(settings):
    """Keep the exact held audit immutable; new sends use a separate slot."""
    path = owner.STATE / 'forward-image-send.json'
    live = owner.STATE / 'forward-image-live-send.json'
    hold_path = owner.STATE / 'forward-image-hold.json'
    hold = frozen = None
    if hold_path.exists():
        require(path.exists(), 'held_image_audit_missing')
        frozen, hold = owner.read_json(path), owner.read_json(hold_path)
        if audit_cleared(frozen):
            # Imported evidence binds the immutable original, across owner PIDs.
            # Task 5 must establish cold recovery before reaching these gates.
            require(owner.owner_active(), 'held_native_instance_unavailable')
            path, hold, frozen = live, None, None
        else:
            require(held_image_allowed(settings, frozen, hold), 'previous_image_send_requires_review')
            require(owner.owner_active(), 'held_native_instance_unavailable')
            if hold['mode'] == 'new_images':
                path = live
    elif path.exists():
        audit = owner.read_json(path)
        require(audit.get('state') == 'accepted' or audit_cleared(audit)
                or image_recovery.reconcile(sys.modules[__name__], path, settings),
                'previous_image_send_requires_review')
    # A later image is never covered by the old hold, including when that hold
    # is subsequently reviewed, removed or restored to its text-only mode.
    if live.exists():
        audit = owner.read_json(live)
        # Without this the gate outlives every start: a bridge that wrote
        # 'submitted' and then died cannot settle the job, and a job that
        # provably never left this host would need a human forever.
        require(audit.get('state') == 'accepted' or audit_cleared(audit)
                or image_recovery.reconcile(sys.modules[__name__], live, settings),
                'previous_image_send_requires_review')
        if hold is None:
            path = live
    return path, hold, frozen


def ensure_saved_session(settings, *, image_hold=False):
    if image_hold:
        # A held job belongs to this running Native instance. Neither of the
        # ordinary ensure paths may restore a different instance after a race.
        require(owner.owner_active(), 'held_native_instance_unavailable')
        require(trial.fresh_status().get('connected') is True, 'held_connection_unavailable')
        require(owner.owner_active(), 'held_native_instance_unavailable')
        return
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
    s = runtime.State()
    controller = runtime.Controller(sys.modules[__name__], s)
    s.settings = SETTINGS
    s.live_only = bool(s.settings and live_startup.enabled(sys.modules[__name__]))
    s.live_run_id = secrets.token_hex(16) if s.live_only else None
    startup = None
    if s.settings:
        # Bridge exclusion is held, but recovery's coordination lock is released
        # before this seven-day worker loop. Cold recovery precedes every legacy
        # held-PID/audit gate; /run and its cached status are not restore authority.
        end = time.monotonic() + s.settings['runtimeSeconds'] - 60
        while True:
            startup = (live_startup if s.live_only else recovery_coordinator).startup(sys.modules[__name__])
            if startup['status'] == 'ready' or startup.get('phase') == 'receive_ready' and startup['status']=='recovering':
                break
            if startup['status'] == 'requires_review' or time.monotonic() >= end:
                os.close(lock)
                return 0  # Explicit durable review, never burn systemd starts.
            time.sleep(min(30, max(1, startup.get('retryAt', time.time()+1)-time.time())))
    s.audit_path = owner.STATE / ('forward-send.json' if s.settings else 'private-forward-send.json')
    if s.audit_path.exists():
        s.audit = owner.read_json(s.audit_path)
        pending_accepted = text_recovery.needs_reconciliation(sys.modules[__name__],s.audit_path,s.settings)
        require(s.audit.get('state') == 'accepted' and not pending_accepted or audit_cleared(s.audit)
                or text_recovery.reconcile(sys.modules[__name__], s.audit_path, s.settings),
                'previous_send_requires_review')
    s.image_audit_path, image_hold, held_image_audit = image_send_preflight(s.settings)
    s.image_audit = None
    voice_forwarding.preflight(sys.modules[__name__])
    token = trial.read_onebot_token()
    if not s.settings:
        ensure_saved_session(s.settings, image_hold=image_hold is not None)
    baseline = {kind: owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = tap.read_network(), tap.business_snapshot()
    s.pinned = groups.PinnedNative()
    probe = None
    s.bundle = None
    s.media_lease = None
    interrupted = False
    def stop(_number, _frame):
        nonlocal interrupted
        interrupted = True
    handlers = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT)}
    s.counters = {'received': 0, 'forwarded': 0, 'accepted_parts': 0, 'completed_replies': 0, 'uncertain': 0,
                'unsupported_actions': 0, 'image_output_requests': 0, 'image_output_sent': 0,
                'image_output_rejected': 0, 'voice_output_requests': 0, 'voice_output_sent': 0, 'voice_output_rejected': 0}
    s.voice_relay = voice_forwarding.Relay(sys.modules[__name__], s.pinned, s.settings, s.counters)
    s.route_counts = {key: {'received': 0, 'forwarded': 0, 'replies': 0} for key in ('private', 'group')}
    s.queue_counts = {}
    s.loop_counts = {key: 0 for key in loops.REASONS}
    s.image_counts = {key: 0 for key in ('prepared', 'waiting', 'unavailable', 'expired', 'forwarded')}
    s.group_traffic = {key: 0 for key in ('context_received', 'wakeup_received', 'context_forwarded', 'wakeup_forwarded')}
    s.last_image_error = None
    s.image_reviews = []
    s.last_output_error = None
    s.self_sender_id = None
    s.largest_reply = {'bytes': 0, 'parts': 0}
    s.resources = {}
    s.onebot_link = {'failures': 0, 'decode_rejections': 0}
    started_at = int(time.time())
    s.live_since = started_at if s.live_only else None
    s.last_activity_at = started_at
    s.good, s.code, clean = False, 'starting', False
    try:
        s.pinned.open()
        # The bridge's own signals stay green through a dead WeChat link; only
        # the account resource itself reports the truth. Start the read-only
        # probe here, where the pinned handle and its namespace are valid.
        probe = health.Probe(s.pinned).start()
        if image_hold is not None:
            require(held_image_allowed(s.settings, held_image_audit, image_hold,
                    native_identity=(s.pinned.host.client_pid, s.pinned.host.client_ticks)),
                    'held_native_instance_changed')
        s.cache_path = owner.STATE / 'image-source-cache.json'
        cache_epoch = str(s.pinned.host.client_pid)+':'+str(s.pinned.host.client_ticks)
        try:
            cached = owner.read_json(s.cache_path) if s.cache_path.exists() else None
            s.source_cache = source_cache_module.Cache(cached,cache_epoch,time.time)
        except (ValueError,RuntimeError):
            s.source_cache = None  # An optimization must not block core text.
        s.export_state = (image_export.prepare(paths.ASTRBOT_DATA/'temp/wechat-hd-input', owner)
                        if s.settings and s.settings['imageInputEnabled'] else None)
        s.media_lease = media_egress.Lease(sys.modules[__name__], s.pinned, hd_input=s.export_state is not None)
        s.seconds = min(s.settings['runtimeSeconds'] if s.settings else 3300, s.pinned.remaining_seconds() - 40)
        require(s.seconds >= 600, 'native_session_expiring')
        s.room_id = None
        if s.settings and not s.settings.get('privateOnly', False) and not s.settings['allGroupsEnabled']:
            _, binding = groups.load_send_binding(s.pinned, s.settings['groupName'])
            s.room_id = binding['roomId']
        s.bundle = Path(tempfile.mkdtemp(prefix='astrbot-wechat-forward.', dir='/var/tmp'))
        for name in FILES:
            source = delivery.source_for(HERE.parent, 'native', name)
            groups.legacy.write_public(s.bundle, name, groups.legacy.source_text(source))
        os.chmod(s.bundle, 0o755)
        s.legacy_generation = 'native:' + str(s.pinned.host.client_pid) + ':' + str(s.pinned.host.client_ticks)
        s.generation = 'profile:' + hashlib.sha256(s.pinned.token().encode()).hexdigest() if s.settings else s.legacy_generation
        s.recovery_seed = ((live_startup.read_binding(sys.modules[__name__])['selfProof'] if s.live_only
            else recovery_coordinator.selected_pair(owner.STATE)[0]['selfProof']) if s.settings else None)
        s.host_digest = delivery.role_digest(HERE.parent, 'host', source=True)
        controller.workers.spawn_native()
        s.peer_id = None
        s.boundary = None
        s.isolated = set()
        s.armed = False
        s.sse_connected = False
        s.astrbot_connected = False
        s.queue_counts = {}
        s.request = None
        s.image_transfer = None
        s.input_relay = input_wire.Relay()
        s.image_ack = None
        s.audit = None
        s.last_input = {}
        # Clean up before either worker's own safety deadline, so an ordinary
        # lifetime limit is not misclassified as a crash and auto-restarted.
        deadline = time.monotonic() + s.seconds - 15
        next_status = 0.
        s.recovery_adapter = None
        s.recovery_path = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite')
        s.recovery_refresh = False
        s.recovery_capacity = False
        s.recovery_next = time.monotonic()
        s.recovery_state = 'ready' if s.live_only else 'pending'
        while not interrupted and time.monotonic() < deadline:
            if controller.poll():
                continue
            controller.recovery.capture_if_safe()
            # A safe-point proof failure withdraws delivery admission in this
            # same turn: the queue is no longer replayable, so neither this
            # generation nor a replacement may keep forwarding it.
            if controller.review():
                controller.stop_for_review()
            if time.monotonic() >= next_status:
                capacity = media_headroom(s.pinned)
                media_available = output_budget.allow_input(**capacity)
                if s.armed:
                    s.workers.peers['native'].send({'kind':'media_capacity','allow':media_available})
                if s.settings:
                    for label, group in (('native_parent', '/' + s.pinned.host.parent), ('onebot', '/system.slice/' + s.workers.units['host'] if 'host' in s.workers.units else ''),
                                         ('controller', '/system.slice/' + UNIT)):
                        base = Path('/sys/fs/cgroup') / group.lstrip('/')
                        if group and base.is_dir():
                            events = dict(line.split() for line in (base / 'memory.events').read_text().splitlines())
                            s.resources[label] = {'memory_bytes': int((base / 'memory.current').read_text()),
                                                'oom_kills': int(events['oom_kill'])}
                    filesystem = os.fstatvfs(s.pinned.host.data_fd)
                    s.resources['native_storage'] = {'used_bytes': (filesystem.f_blocks - filesystem.f_bavail) * filesystem.f_frsize,
                                                   'total_bytes': filesystem.f_blocks * filesystem.f_frsize,
                                                   'free_bytes':filesystem.f_bavail * filesystem.f_frsize,
                                                   'media_capacity_available':media_available}
                native_health = probe.snapshot() if probe is not None else {
                    'native_connected': False, 'native_connection': 'unchecked',
                    'native_probe_code': 'starting', 'native_checked_at': 0}
                health_record = wire.health_frame({'kind': 'health', 'observed_at': int(time.time()),
                    **native_health, 'sse': s.sse_connected, 'astrbot_connected': s.astrbot_connected,
                    'admitted': s.armed and not s.capture_paused and not s.recovery_state.endswith('requires_review'), 'recovery': s.recovery_state})
                health_status = wire.health_state.evaluate_health(health_record, time.time())
                if 'host' in s.isolated:
                    s.workers.peers['host'].send(health_record)
                status({'active': True,
                        'ready': health_status['good'], 'online': health_status['online'],
                        'health_observed_at': health_record['observed_at'],
                        'native_fresh_seconds': wire.health_state.NATIVE_FRESH_SECONDS, 'health': health_record,
                        'code': s.code, 'capture_phase':s.capture_phase, 'pending_send_reviews':len(s.send_reviews), **native_health,
                        'receive_policy': 'live_only' if s.live_only else 'recovery',
                        'backup_policy': 'disabled' if s.live_only else 'legacy',
                        'skipped_on_start': s.skipped_on_start,
                        'quarantined_images': int(image_hold is not None),
                        'requires_review': image_hold is not None or s.recovery_state.endswith('requires_review'),
                        'recovery': ('awaiting_network' if not s.astrbot_connected or not s.sse_connected
                                     or native_health.get('native_connected') is not True else s.recovery_state),
                        'quarantined_native': startup.get('quarantined',0) if startup else 0,
                        'private_tester': s.settings['privateContact'] if s.settings else '测试联系人', 'sse': s.sse_connected, 'astrbot_connected': s.astrbot_connected,
                        'queue': s.queue_counts, 'new_qr': False,
                        'outbound_pending':s.request is not None or s.voice_relay.pending or s.input_relay.active,'last_activity_at':s.last_activity_at,
                        'media_capacity_available':media_available,
                        'self_identity_verified': s.self_sender_id is not None, 'loop_guard': s.loop_counts,
                        'image_input_enabled': s.settings['imageInputEnabled'] if s.settings else False,
                        'sticker_input_enabled': s.settings['stickerInputEnabled'] if s.settings else False,
                        'link_input_enabled': s.settings['linkInputEnabled'] if s.settings else False, 'images': s.image_counts,
                        'image_output_enabled': s.settings['imageOutputEnabled'] if s.settings else False,
                        'voice_output_enabled': s.settings['voiceOutputEnabled'] if s.settings else False,
                        'onebot_link': s.onebot_link,
                        'worker_restarts': {name: item.state() for name, item in s.workers.supervisors.items()},
                        'last_image_error': s.last_image_error,
                        'image_reviews': len(s.image_reviews),
                        'last_image_review_at': s.image_reviews[-1]['updated'] if s.image_reviews else None,
                        'last_output_error': s.last_output_error,
                        'image_egress_active': bool(s.media_lease and s.media_lease.changed),
                        'group': s.settings['groupName'] if s.settings else None, 'routes': s.route_counts, 'largest_reply': s.largest_reply,
                        'group_wake_mode': s.settings['groupWakeMode'] if s.settings else 'prefix',
                        'group_context_enabled': s.settings['groupContextEnabled'] if s.settings else False, 'group_traffic': s.group_traffic,
                        'private_only': s.settings.get('privateOnly', False) if s.settings else False,
                        'all_groups_enabled': bool(s.settings and s.settings['allGroupsEnabled'] and not s.settings.get('privateOnly', False)),
                        'started_at': started_at, 'uptime_seconds': int(time.time()) - started_at, 'resources': s.resources,
                        'remaining_seconds': max(0, int(deadline - time.monotonic())), **s.counters})
                next_status = time.monotonic() + 2
        s.code = 'stopped' if interrupted else 'pilot_expired'
    except Exception as error:
        s.good = False
        s.code = getattr(error, 'stage', s.code if s.code not in {'starting', 'running'} else 'forwarder_failed')
        remember_failure(s.code)
        if s.settings and s.code in {'recovery_integrity','recovery_owner','recovery_anchor_mismatch',
                'recovery_rebase','self_identity_unverified','invalid_recovery_anchor','sse_error',
                'requires_review'}:
            (live_startup if s.live_only else recovery_coordinator).block(sys.modules[__name__],s.code)
    finally:
        if hasattr(controller.recovery,'close'):
            try: controller.recovery.close()
            except Exception: s.good=False
        if s.request is not None and s.pinned.host is not None:
            try:
                pending_audit = s.image_audit if s.request.get('kind') in {'send_image','image_begin'} else s.audit
                pending_path = s.image_audit_path if s.request.get('kind') in {'send_image','image_begin'} else s.audit_path
                pending_kind = 'media' if s.request.get('kind') in {'send_image','image_begin'} else 'text'
                if not audit_cleared(pending_audit):
                    pending_audit.update(state='unknown')
                    settle_outcome(s.pinned, pending_path, pending_audit, pending_kind, 'unconfirmed_quarantined')
            except Exception:
                s.good = False
        if s.voice_relay.pending:
            try:
                s.voice_relay.quarantine('voice_response')
            except Exception:
                s.good = False
        if s.media_lease is not None:
            try:
                s.media_lease.close()
            except Exception:
                s.good = False
        for unit in reversed(s.workers.started):
            try:
                tap.stop_unit(unit, True)
            except Exception:
                s.good = False
        for capture in s.workers.peers.values():
            try:
                capture.close()
            except Exception:
                s.good = False
        unchanged = s.pinned.unchanged() if s.pinned.host is not None else False
        if probe is not None:
            probe.close()
        s.pinned.close()
        if s.bundle is not None:
            for name in FILES:
                (s.bundle / name).unlink(missing_ok=True)
            s.bundle.rmdir()
        flags, failures = tap.compare_baselines(baseline, network, business)
        clean = not failures and unchanged
        status({'active': False, 'ready': False, 'code': s.code, 'ok': s.good and clean,
                'quarantined_native': startup.get('quarantined',0) if startup else 0,
                'quarantined_images': int(image_hold is not None),
                'requires_review': image_hold is not None or s.recovery_state.endswith('requires_review'),
                'cleanup_ok': clean, 'native_session_unchanged': unchanged, 'routes': s.route_counts,
                'self_identity_verified': s.self_sender_id is not None, 'loop_guard': s.loop_counts,
                'images': s.image_counts, 'group_traffic': s.group_traffic,
                'last_output_error': s.last_output_error,
                'image_output_enabled': s.settings['imageOutputEnabled'] if s.settings else False,
                'voice_output_enabled': s.settings['voiceOutputEnabled'] if s.settings else False, 'onebot_link': s.onebot_link,
                'largest_reply': s.largest_reply, 'resources': s.resources, 'uptime_seconds': int(time.time()) - started_at,
                'queue': s.queue_counts, **flags, **s.counters})
        for number, handler in handlers.items():
            signal.signal(number, handler)
        os.close(lock)
    permanent = s.settings and s.code in {'recovery_integrity','recovery_owner','recovery_anchor_mismatch',
        'recovery_rebase','self_identity_unverified','invalid_recovery_anchor','sse_error',
        'requires_review'}
    return 0 if clean and (s.good or permanent) else 1


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
    values = ['MemoryMax=1024M', 'MemoryHigh=768M', 'MemorySwapMax=0', 'CPUQuota=100%', 'TasksMax=16',
              'Nice=19', 'RuntimeMaxSec=' + str(SETTINGS['runtimeSeconds'] + 120 if SETTINGS else 3420),
              'TimeoutStopSec=20', 'KillMode=control-group', 'UMask=0077',
              'LimitCORE=0', 'LimitFSIZE=1101004800', 'ProtectKernelModules=yes', 'StandardOutput=null', 'StandardError=null']
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
