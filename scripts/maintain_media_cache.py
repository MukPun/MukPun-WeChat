#!/usr/bin/env python3
"""Idle-only, backed-up Native upload-source GC. Never sends or initializes."""
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

HERE = Path(__file__).resolve().parent
SERVICE = 'astrbot-wechat-media-maintenance.service'
TIMER = 'astrbot-wechat-media-maintenance.timer'


def load(name):
    spec = importlib.util.spec_from_file_location('_media_maintenance_'+name, HERE/(name+'.py'))
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


policy = load('media_maintenance_policy')


def cycle(ops):
    reason = ops.eligibility()
    if reason != 'maintain':
        return reason
    stopping = False
    try:
        if not ops.preflight():
            ops.journal(outcome='no_gain', completed_at=int(time.time()))
            return 'no_gain'
        ops.check_idle()
        ops.journal(outcome='in_progress', phase='stop_bridge')
        stopping = True
        ops.stop_bridge()
        ops.journal(outcome='in_progress', phase='stop_native')
        ops.stop_native()
        ops.journal(outcome='in_progress', phase='clean')
        result = ops.clean()
        ops.journal(outcome='in_progress', phase='restore')
        ops.restore()
        ops.start_bridge()
        ops.retain_backups()
        outcome = 'success' if result['bytes_reclaimed'] >= policy.MIN_GAIN else 'no_gain'
        ops.journal(outcome=outcome, completed_at=int(time.time()), **result)
        return outcome
    except Exception:
        recovered = not stopping
        if stopping:
            try:
                recovered = ops.recover()
            except Exception:
                recovered = False
        if recovered:
            ops.journal(outcome='failed', completed_at=int(time.time()))
            return 'failed'
        ops.journal(outcome='in_progress', phase='requires_review')
        return 'requires_review'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_private(source, destination):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('maintenance_private_file')
        with os.fdopen(fd, 'rb', closefd=False) as src, Path(destination).open('xb') as dst:
            os.fchmod(dst.fileno(), 0o600)
            shutil.copyfileobj(src, dst, 65536)
            dst.flush()
            os.fsync(dst.fileno())
    finally:
        os.close(fd)


def run(arguments, timeout=40):
    result = subprocess.run(arguments, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError('maintenance_owned_command')
    return result.stdout


class Backend:
    def __init__(self):
        self.forward = load('run_forwarder')
        self.forward.load_config()
        self.c = self.forward.core
        self.owner = self.c.owner
        self.gc, self.proofs, self.guest = (load(n) for n in
            ('media_cache_maintenance','media_maintenance_snapshot','session_runtime_client'))
        self.store = self.guest.state_store.SnapshotStore(self.owner.STATE/'checkpoints')
        self.record = self.owner.STATE/'media-maintenance.json'
        self.backups = self.owner.STATE/'media-maintenance-backups'
        self.backup = None
        self.proof = self.audit = self.hold = None
        self.audit_bytes = {}
        self.native_stopped = self.restore_attempted = self.bridge_stopped = False
        self.native_restored = False

    def journal(self, **value):
        if self.backup is not None:
            value['backup'] = self.backup.name
        self.owner.write_json(self.record, {'schemaVersion':1, **value})

    def eligibility(self):
        previous = self.owner.read_json(self.record) if self.record.exists() else None
        p = self.c.groups.PinnedNative()
        try:
            p.open()
            capacity = self.c.media_headroom(p)
            fs = os.fstatvfs(p.host.data_fd)
            capacity['storage_used'] = (fs.f_blocks-fs.f_bavail)*fs.f_frsize
        finally:
            p.close()
        return policy.decision(self.owner.read_json(self.c.STATUS), capacity, previous, now=int(time.time()))

    def spool(self, destination=None):
        path = Path('/var/lib/astrbot-wechat-bridge/private-forward.sqlite')
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1) as db:
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('maintenance_spool_integrity')
            counts = dict(db.execute('SELECT state,count(*) FROM inbox GROUP BY state'))
            if any(counts.get(k) for k in ('queued','publishing','uncertain')):
                raise ValueError('maintenance_spool_busy')
            if destination:
                with sqlite3.connect(destination) as target:
                    db.backup(target)
                destination.chmod(0o600)

    def check_idle(self):
        if self.eligibility() != 'maintain':
            raise ValueError('maintenance_no_longer_idle')
        if self.forward.UNIT_FILE.read_text() != self.forward.unit_text():
            raise ValueError('maintenance_bridge_unit')
        state = self.c.tap.details(self.c.UNIT)
        pid = int(state.get('MainPID','0'))
        if (state.get('ActiveState') != 'active' or pid <= 1 or
                self.c.groups.argv(pid) != [b'/usr/bin/python3', b'-I', b'-B', os.fsencode(self.c.ENTRY), b'--serve']):
            raise ValueError('maintenance_bridge_identity')
        self.spool()
        for name in ('forward-send.json','forward-image-send.json','forward-image-live-send.json','forward-voice-send.json'):
            path = self.owner.STATE/name
            if not path.exists():
                continue
            value = self.owner.read_json(path)
            self.audit_bytes[name] = path.read_bytes()
            if name == 'forward-image-send.json' and (self.owner.STATE/'forward-image-hold.json').exists():
                self.audit = value
                self.hold = self.owner.read_json(self.owner.STATE/'forward-image-hold.json')
            elif value.get('state') not in ('accepted','rejected'):
                raise ValueError('maintenance_audit_requires_review')
        p = self.c.groups.PinnedNative()
        try:
            p.open()
            self.identity = (p.host.client_pid, p.host.client_ticks)
            self.saved_keys = self.keys(p)
            suffix = self.c.groups.UNIT_RE.fullmatch(p.host.unit).group(1)
            self.units = {p.host.unit, p.host.parent, self.c.groups.owned.PREFIX+suffix+'.service',
                          self.c.groups.tap.HELPER_PREFIX+suffix+'.service'}
            self.approved = None
            if self.hold is not None:
                if not self.c.held_image_allowed(self.c.SETTINGS, self.audit, self.hold, native_identity=self.identity):
                    raise ValueError('maintenance_hold_changed')
                ticket = self.owner.read_json(self.owner.STATE/'native-1.2.0-migration.json')
                if ticket.get('phase') != 'consumed':
                    raise ValueError('maintenance_restore_ticket')
                self.approved = ticket.get('quarantinedSend')
                self.guest.validate_quarantined_send(self.approved)
        finally:
            p.close()

    def keys(self, p):
        result = {}
        for name in ('api-token','storage-key','cursor-key'):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=p.host.data_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 8192:
                    raise ValueError('maintenance_key')
                result[name] = hashlib.sha256(os.read(fd,8193)).hexdigest()
            finally:
                os.close(fd)
        return result

    def analyze(self, path):
        proof = self.proofs.analyze(path, self.guest, audit=self.audit, approved=self.approved)
        if proof['keys'] != self.saved_keys:
            raise ValueError('maintenance_login_changed')
        return proof

    def preflight(self):
        self.check_idle()
        with tempfile.TemporaryDirectory(prefix='wechat-media-estimate.') as work:
            source, candidate = Path(work)/'source.zip', Path(work)/'candidate.zip'
            copy_private(self.store.latest(), source)
            self.proof = self.analyze(source)
            result = self.gc.rewrite_snapshot(source, candidate, self.guest.state_store,
                offline=True, pressure=True, keep_bytes=0)
            if self.analyze(candidate) != self.proof:
                raise ValueError('maintenance_proof_changed')
            return policy.worthwhile(result)

    def stopped_unit(self, unit):
        code, value = self.c.tap.load_state(unit)
        if value.get('LoadState') == 'not-found':
            return True
        if code != 0 or value.get('ActiveState') not in ('inactive','failed'):
            return False
        if unit.endswith('.service') and value.get('MainPID') != '0':
            return False
        group = value.get('ControlGroup')
        if group:
            if not group.startswith('/') or '..' in Path(group).parts:
                return False
            procs = Path('/sys/fs/cgroup')/group.lstrip('/')/'cgroup.procs'
            if procs.exists() and procs.read_text().strip():
                return False
        return True

    def no_workers(self):
        if not self.stopped_unit(self.c.UNIT):
            raise ValueError('maintenance_bridge_still_active')
        rows = run(['/usr/bin/systemctl','list-units','--all','--type=service','--plain','--no-legend','--no-pager']).decode().splitlines()
        for row in rows:
            name = row.split()[0] if row.split() else ''
            if any(re.fullmatch(re.escape(prefix)+r'[0-9a-f]{12}\.service', name) for prefix in
                   ('astrbot-wechat-private-host-',self.c.groups.READER_PREFIX)) and not self.stopped_unit(name):
                raise ValueError('maintenance_worker_still_active')

    def unchanged_audits(self):
        for name, raw in self.audit_bytes.items():
            if (self.owner.STATE/name).read_bytes() != raw:
                raise ValueError('maintenance_audit_changed')

    def stop_bridge(self):
        self.owner.private_directory(self.backups)
        self.backup = self.backups/str(time.time_ns())
        self.owner.private_directory(self.backup)
        self.retain_backups()
        for name in self.audit_bytes:
            copy_private(self.owner.STATE/name, self.backup/name)
        for name in ('forward-image-hold.json','native-1.2.0-migration.json'):
            path = self.owner.STATE/name
            if path.exists():
                copy_private(path, self.backup/name)
        self.journal(outcome='in_progress', phase='stop_bridge')
        run(['/usr/bin/systemctl','stop',self.c.UNIT], 60)
        self.bridge_stopped = True
        receipt = self.owner.read_json(self.c.STATUS)
        if not (receipt.get('cleanup_ok') is True and receipt.get('native_session_unchanged') is True
                and receipt.get('uncertain') == 0):
            raise ValueError('maintenance_bridge_cleanup')
        self.no_workers()
        self.spool(self.backup/'bridge.sqlite')
        self.unchanged_audits()

    def stop_native(self):
        self.no_workers()
        self.spool()
        self.unchanged_audits()
        result = subprocess.run(['/usr/bin/python3','-I','-B',str(HERE/'session_runtime.py'),'stop'], capture_output=True, timeout=180)
        receipt = self.owner.read_json(self.owner.CONTROL/'status.json').get('cleanup',{})
        if (result.returncode not in (0,1) or self.owner.owner_active()
                or set(receipt.get('explicit_stop',[])) != self.units
                or receipt.get('host_namespace_unchanged') is not True
                or receipt.get('native_stop_ok') is not True
                or receipt.get('final_snapshot_saved') is not True
                or not all(self.stopped_unit(u) for u in self.units)):
            raise ValueError('maintenance_native_cleanup')
        self.native_stopped = True
        self.current = self.store.latest()
        copy_private(self.current, self.backup/'original.zip')
        if self.analyze(self.current) != self.proof:
            raise ValueError('maintenance_fresh_proof_changed')

    def clean(self):
        self.no_workers()
        if self.owner.owner_active() or not self.native_stopped:
            raise ValueError('maintenance_native_running')
        candidate = self.backup/'cleaned.zip'
        result = self.gc.rewrite_snapshot(self.current, candidate, self.guest.state_store,
            offline=True, pressure=True, keep_bytes=0)
        if self.analyze(candidate) != self.proof:
            raise ValueError('maintenance_candidate_proof_changed')
        if policy.worthwhile(result):
            with self.store._private() as directory:
                fcntl.flock(directory, fcntl.LOCK_EX)
                if digest(self.current) != digest(self.backup/'original.zip'):
                    raise ValueError('maintenance_snapshot_changed')
                os.replace(candidate, self.current)
                os.fsync(directory)
        else:
            candidate.unlink()
            return {'sources_removed':0, 'bytes_reclaimed':0}
        return {'sources_removed':result['sources_removed'], 'bytes_reclaimed':max(0,
            result['snapshot_bytes_before']-result['snapshot_bytes_after'])}

    def restore(self):
        if self.restore_attempted or not self.native_stopped or self.owner.owner_active():
            raise ValueError('maintenance_restore_state')
        self.no_workers()
        if self.analyze(self.store.latest()) != self.proof:
            raise ValueError('maintenance_restore_proof')
        self.unchanged_audits()
        self.restore_attempted = True
        if self.approved is None:
            self.owner.start_owner(False, seconds=self.owner.LONG_RUNTIME_SECONDS)
        else:
            migration = load('resume_quarantined_native')
            with migration.migration_lock():
                self.owner.write_json(self.owner.STATE/'native-1.2.0-migration.json',
                    {'schemaVersion':1,'phase':'prepared','snapshotSha256':digest(self.store.latest()),
                     'quarantinedSend':self.approved})
            for name in ('request.json','status.json','login-qr.png','.request.json.next','.status.json.next','.login-qr.png.next'):
                (self.owner.CONTROL/name).unlink(missing_ok=True)
            # Same bounded owner definition; only its fixed entry point differs.
            args = ['/usr/bin/systemd-run','--quiet','--collect','--unit='+self.owner.OWNER,
                '--property=MemoryMax=64M','--property=MemoryHigh=48M','--property=MemorySwapMax=0',
                '--property=CPUQuota=5%','--property=TasksMax=16','--property=Nice=19',
                '--property=RuntimeMaxSec='+str(self.owner.LONG_RUNTIME_SECONDS+60),
                '--property=TimeoutStopSec=25','--property=KillMode=control-group','--property=UMask=0077',
                '--property=LimitCORE=0','--property=LimitFSIZE=1101004800','--property=ProtectKernelModules=yes',
                '--property=StandardOutput=null','--property=StandardError=null',
                '/usr/bin/python3','-I','-B',str(HERE/'resume_quarantined_native.py'),'--serve']
            run(args, 10)
            self.wait(lambda: self.owner.read_json(self.owner.CONTROL/'status.json').get('ready') is True, 90)
        self.wait(self.connected, 120)
        p = self.c.groups.PinnedNative()
        try:
            p.open()
            identity = (p.host.client_pid,p.host.client_ticks)
            if identity == self.identity or self.keys(p) != self.saved_keys:
                raise ValueError('maintenance_restored_identity')
            if self.analyze(self.store.latest()) != self.proof:
                raise ValueError('maintenance_restored_proof')
            if self.hold is not None:
                updated = {**self.hold,'nativeClientPid':identity[0],'nativeClientTicks':identity[1]}
                if not self.c.held_image_allowed(self.c.SETTINGS,self.audit,updated,native_identity=identity):
                    raise ValueError('maintenance_restored_hold')
                self.owner.write_json(self.owner.STATE/'forward-image-hold.json',updated)
        finally:
            p.close()
        self.unchanged_audits()
        self.native_restored = True

    @staticmethod
    def wait(check, seconds):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except FileNotFoundError:
                pass
            time.sleep(.5)
        raise ValueError('maintenance_ready_timeout')

    def connected(self):
        value = self.c.trial.fresh_status()
        if value.get('qrVersion') != 0 or value.get('accounts') != 1:
            raise ValueError('maintenance_saved_login_failed')
        return value.get('connected') is True

    def start_bridge(self):
        self.no_workers()
        self.spool()
        self.unchanged_audits()
        started = int(time.time())
        run(['/usr/bin/systemctl','reset-failed',self.c.UNIT], 10)
        run(['/usr/bin/systemctl','start',self.c.UNIT], 10)
        def ready():
            value = self.owner.read_json(self.c.STATUS)
            return (value.get('updated',0) >= started and value.get('ready') is True
                    and value.get('sse') is True and value.get('astrbot_connected') is True
                    and value.get('uncertain') == 0 and value.get('new_qr') is False)
        self.wait(ready, 60)

    def recover(self):
        # Never roll back after a Native start, or issue a second restore.
        if self.restore_attempted and not self.native_restored:
            return False
        if self.native_stopped and not self.native_restored:
            self.restore()
        if self.bridge_stopped:
            self.start_bridge()
        return True

    def retain_backups(self):
        entries = sorted((p for p in self.backups.iterdir() if re.fullmatch(r'[0-9]{19,20}',p.name)), key=lambda p:p.name)
        for path in entries[:-3]:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('maintenance_backup_path')
            shutil.rmtree(path)


@contextmanager
def maintenance_lock(owner):
    fd = os.open(owner.STATE/'media-maintenance.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('maintenance_lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def unit_texts(entry=Path(__file__).resolve()):
    return ('''# Managed only by astrbot-wechat
[Unit]
Description=Idle WeChat media upload cache maintenance
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I -B "@ENTRY@" --once
MemoryMax=512M
MemoryHigh=384M
MemorySwapMax=0
CPUQuota=50%
TasksMax=16
Nice=19
UMask=0077
LimitCORE=0
LimitFSIZE=1101004800
ProtectKernelModules=yes
TimeoutStartSec=900
TimeoutStopSec=25
StandardOutput=null
StandardError=null
'''.replace('@ENTRY@',str(entry)), '''# Managed only by astrbot-wechat
[Unit]
Description=Check idle WeChat media capacity every minute
[Timer]
OnBootSec=90s
OnUnitInactiveSec=60s
AccuracySec=5s
Unit=astrbot-wechat-media-maintenance.service
[Install]
WantedBy=timers.target
''')


def install():
    for name, text in zip((SERVICE,TIMER), unit_texts()):
        path = Path('/etc/systemd/system')/name
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                    or path.read_text() != text):
                raise ValueError('maintenance_unit_changed')
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            try:
                os.write(fd,text.encode()); os.fsync(fd)
            finally:
                os.close(fd)
    run(['/usr/bin/systemctl','daemon-reload'])
    run(['/usr/bin/systemctl','enable','--now',TIMER])


def main():
    if os.geteuid() != 0:
        return 2
    if sys.argv[1:] == ['install']:
        install()
        return 0
    if sys.argv[1:] != ['--once']:
        return 2
    ops = Backend()
    with maintenance_lock(ops.owner):
        result = cycle(ops)
    print(result)
    return 1 if result in {'failed','requires_review'} else 0


if __name__ == '__main__':
    def interrupted(_number, _frame):
        raise RuntimeError('maintenance_interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        raise SystemExit(main())
    except BlockingIOError:
        raise SystemExit(0)
    except Exception:
        print('maintenance_requires_review')
        raise SystemExit(1)
