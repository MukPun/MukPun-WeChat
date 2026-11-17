#!/usr/bin/env python3
"""Root-only synthetic mount/UID/SQLite gate; no account, Native or network.

Use the formal SessionHost, replacing only the Native executable/controller
with a synthetic guest. All units and directories are fresh and disposable.
"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
GUEST = '''import importlib.util
import os
from pathlib import Path
import sqlite3
import time
spec = importlib.util.spec_from_file_location('guest_library', '/opt/guest_library.py')
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)
guest.prepare_persistent_environment()
try:
    list(guest.PERSISTENT_DATA.iterdir())
except PermissionError:
    pass
else:
    raise RuntimeError('premature_data_access')
guest.emit(0, dict(stage='runtime_isolated', ok=True))
guest.wait_persistent_handoff()
with sqlite3.connect(guest.PERSISTENT_DATA / 'registry.sqlite') as db:
    db.execute('insert into identity values (?)', ('synthetic-run',))
guest.emit(0, dict(stage='runtime_ready', ok=True, restored=False, accounts=1, connected=False))
deadline = time.monotonic() + 35
last = 0
while time.monotonic() < deadline:
    command = guest.read_command()
    if command['sequence'] > last:
        last = command['sequence']
        if command['action'] == 'stop':
            guest.emit(last, dict(stage='runtime_stopped', ok=True))
        elif command['action'] == 'release':
            break
    time.sleep(.03)
else:
    raise RuntimeError('synthetic_deadline')
'''


def require(value):
    if not value:
        raise RuntimeError('persistent_lifecycle_gate')


def fixture(source):
    source.mkdir(mode=0o700)
    for name in ('api-token', 'storage-key', 'cursor-key'):
        path = source / name
        path.write_bytes(('synthetic-' + name).encode())
        path.chmod(0o600)
    with sqlite3.connect(source / 'registry.sqlite') as db:
        db.execute('create table identity(value text)')
        db.execute("insert into identity values ('synthetic-account')")
    (source / 'registry.sqlite').chmod(0o600)
    for part in ('accounts', 'accounts/synthetic-account', 'accounts/synthetic-account/agent'):
        (source / part).mkdir(mode=0o700)
    path = source / 'accounts/synthetic-account/agent/control.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE outbox(id,idempotency_key,payload_hash,destination,payload,state,client_id,server_id,error_code,created_at);
            CREATE TABLE media_jobs(id,idempotency_key,request_hash,kind,state,phase,attempts,cancel_requested,retryable,error_code,data_json,payload,created_at,updated_at);''')
    path.chmod(0o600)


def lifecycle(module, state, expected_count):
    host = module.SessionHost(state / 'checkpoints', offline=True, seconds=40)
    original_source = module.legacy.source_text

    def copy_synthetic(bundle):
        module.legacy.write_public(bundle, 'guest_library.py', original_source(ROOT / 'scripts/session_runtime_client.py'))

    def source_text(path):
        return GUEST if path.name == 'session_runtime_client.py' else original_source(path)

    # No production method, cgroup limit or mount property is replaced.
    with patch.object(module.legacy, 'copy_asset', copy_synthetic), \
            patch.object(module.legacy, 'source_text', source_text), \
            patch.object(module, 'FILES', module.FILES + ('guest_library.py',)):
        try:
            host.start()
            deadline = time.monotonic() + 18
            while not host.ready and time.monotonic() < deadline:
                host.pump(.05)
            require(host.ready and host.storage_mode == 'persistent' and not host.can_snapshot)
            require(host.client_uid > 0 and os.fstat(host.data_fd).st_ino == (state / 'persistent/data').stat().st_ino)
            group = Path('/sys/fs/cgroup') / host.parent / host.unit
            limits = {'memory.max': str(2560 * 1024**2), 'memory.high': str(2304 * 1024**2),
                      'memory.swap.max': '0', 'pids.max': '32'}
            require(all((group / name).read_text().strip() == value for name, value in limits.items()))
            mounts = module.guest.empty.parse_mounts(
                Path('/proc', str(host.client_pid), 'mountinfo').read_text())
            mount = module.guest.empty.containing_mount(mounts, module.persistent.GUEST_DATA)
            require(mount['point'] == module.persistent.GUEST_DATA and {'rw', 'noexec'} <= mount['options'])
            host._freeze = lambda: (_ for _ in ()).throw(AssertionError('unexpected_freeze'))
            host.store.commit = lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic_backup_failure'))
            try:
                host.checkpoint()
            except module.legacy.Failure as error:
                require(str(error) == 'persistent_backup_disabled')
            else:
                raise RuntimeError('unexpected_checkpoint')
            # Repeated polls continue after the deliberately unavailable backup.
            host.pump(.05)
        finally:
            receipt = host.close()
        require(receipt['ok'] and receipt['native_stop_ok'] and not receipt['final_snapshot_saved'])
    metadata = module.persistent.storage_metadata(state)
    require(metadata['state'] == 'sealed' and metadata['uid'] == 0)
    with sqlite3.connect(state / 'persistent/data/registry.sqlite') as db:
        require(db.execute('select count(*) from identity').fetchone() == (expected_count,))
    require(not list(state.rglob('*.zip')))


def main():
    require(sys.platform == 'linux' and os.geteuid() == 0)
    spec = importlib.util.spec_from_file_location('_persistent_lifecycle_host', ROOT / 'scripts/session_runtime_host.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix='wechat-persistent-gate-', dir='/var/tmp') as temporary:
        root = Path(temporary)
        state = root / 'state'
        state.mkdir(mode=0o700)
        fixture(root / 'source')
        module.persistent.PersistentStore(state).initialize_from_stopped(
            root / 'source', expected_uid=0, stopped=lambda: True)
        identity = (state / 'persistent/data').stat().st_ino
        for count in (2, 3):
            lifecycle(module, state, count)
            require((state / 'persistent/data').stat().st_ino == identity)
    print(json.dumps(dict(stage='persistent_storage_lifecycle', ok=True, lifecycles=2,
                          synthetic=True, native_started=False, messages_sent=0)), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
