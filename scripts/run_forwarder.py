#!/usr/bin/env python3
"""Formal two-route forwarder. Own services only; never initializes a login."""
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def module(name, package=False):
    path = HERE / (name + '.py')
    if package and not path.exists():
        path = HERE.parent / 'wechat_bridge' / (name + '.py')
    spec = importlib.util.spec_from_file_location('_formal_forward_' + name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


core = module('run_private_forwarder')
configuration = module('forward_config', True)
core.UNIT = 'astrbot-wechat-forwarder.service'
core.STATUS = core.owner.CONTROL / 'forwarder.json'
core.ENTRY = Path(__file__).resolve()
paths = module('project_paths')
CONFIG = paths.CONFIG
UNIT_FILE = Path('/etc/systemd/system') / core.UNIT


def load_config():
    descriptor = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        core.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o022
                     and 0 < info.st_size <= 4096, 'config_permissions')
        core.SETTINGS = configuration.read(os.read(descriptor, 4097))
    finally:
        os.close(descriptor)


def invoke(action):
    old = sys.argv
    try:
        sys.argv = [str(core.ENTRY), action]
        return core.main()
    finally:
        sys.argv = old


def command(arguments, timeout=30):
    result = subprocess.run(arguments, capture_output=True, timeout=timeout)
    core.require(result.returncode == 0, 'owned_service_command_failed')


def start():
    if UNIT_FILE.exists():
        core.require(UNIT_FILE.read_text() == unit_text(), 'unit_definition_changed')
        command(['/usr/bin/systemctl', 'start', core.UNIT], 10)
        core.owner.wire({'stage': 'forwarder_started', 'ok': True, 'persistent': True, 'new_qr': False})
        return 0
    return invoke('start')


def stopped_receipt():
    value = core.owner.read_json(core.STATUS)
    core.require(value.get('cleanup_ok') is True and value.get('native_session_unchanged') is True
                 and value.get('uncertain') == 0, 'forwarder_requires_review')
    core.require(not value.get('queue', {}).get('queued') and not value.get('queue', {}).get('publishing')
                 and not value.get('queue', {}).get('uncertain'), 'pending_queue_requires_review')
    core.owner.write_json(core.owner.STATE / 'last-forwarder-completion.json', value)
    core.owner.wire({'stage': 'forwarder_previous_run', 'ok': True,
                     **{key: value.get(key) for key in ('received', 'forwarded', 'completed_replies', 'routes',
                        'largest_reply', 'uptime_seconds', 'business_processes_unchanged', 'host_network_unchanged',
                        'native_session_unchanged')}})


def unit_text():
    return '''# Managed only by astrbot-wechat
[Unit]
Description=AstrBot WeChat isolated text forwarder
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=3

[Service]
Type=exec
ExecStart=/usr/bin/python3 -I -B "@ENTRY@" --serve
MemoryMax=64M
MemoryHigh=48M
MemorySwapMax=0
CPUQuota=5%
TasksMax=16
Nice=19
UMask=0077
LimitCORE=0
LimitFSIZE=8388608
ProtectKernelModules=yes
KillMode=control-group
TimeoutStopSec=25
Restart=on-failure
RestartSec=30
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
'''.replace('@ENTRY@', str(core.ENTRY))


def install():
    # Installation touches exactly one new project unit and its enable link.
    # No override or dependency on Docker/FRP is created.
    if UNIT_FILE.exists():
        info = UNIT_FILE.lstat()
        core.require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                     and UNIT_FILE.read_text() == unit_text(), 'unit_definition_changed')
    current = core.tap.details(core.UNIT)
    if current.get('ActiveState') == 'active':
        pid = int(current.get('MainPID', '0'))
        core.require(pid > 1 and core.groups.argv(pid) == [b'/usr/bin/python3', b'-I', b'-B',
                     os.fsencode(core.ENTRY), b'--serve'], 'forwarder_identity')
        command(['/usr/bin/systemctl', 'stop', core.UNIT])
        stopped_receipt()
    if not UNIT_FILE.exists():
        descriptor = os.open(UNIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            os.write(descriptor, unit_text().encode())
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    command(['/usr/bin/systemctl', 'daemon-reload'])
    command(['/usr/bin/systemctl', 'enable', core.UNIT])
    core.owner.wire({'stage': 'forwarder_unit_installed', 'ok': True, 'unit': core.UNIT})


def main():
    core.require(sys.platform == 'linux' and os.geteuid() == 0, 'linux_root_required')
    load_config()
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == 'image-output':
        core.require(args[1] in {'enable', 'disable'}, 'image_output_policy')
        if args[1] == 'enable' and (core.owner.STATE / 'forward-image-hold.json').exists():
            try:
                core.image_send_preflight({**(core.SETTINGS or {}), 'imageOutputEnabled': True})
            except Exception:
                core.require(False, 'quarantined_image_requires_review')
        updated = configuration.validate({**core.SETTINGS, 'imageOutputEnabled': args[1] == 'enable'})
        info = CONFIG.lstat()
        core.owner.write_json(CONFIG, updated)
        os.chown(CONFIG, info.st_uid, info.st_gid)
        core.owner.wire({'stage': 'forwarder_image_output_configured', 'ok': True,
                         'enabled': updated['imageOutputEnabled']})
        return 0
    if len(args) == 2 and args[0] == 'context':
        core.require(args[1] in {'enable', 'disable'}, 'group_context_policy')
        updated = configuration.validate({**core.SETTINGS, 'groupContextEnabled': args[1] == 'enable'})
        info = CONFIG.lstat()
        core.owner.write_json(CONFIG, updated)
        os.chown(CONFIG, info.st_uid, info.st_gid)
        core.owner.wire({'stage': 'forwarder_context_configured', 'ok': True, 'enabled': updated['groupContextEnabled']})
        return 0
    if len(args) == 2 and args[0] == 'images':
        core.require(args[1] in {'enable', 'disable'}, 'image_policy')
        updated = configuration.validate({**core.SETTINGS, 'imageInputEnabled': args[1] == 'enable'})
        info = CONFIG.lstat()
        core.owner.write_json(CONFIG, updated)
        os.chown(CONFIG, info.st_uid, info.st_gid)
        core.owner.wire({'stage': 'forwarder_images_configured', 'ok': True, 'enabled': updated['imageInputEnabled']})
        return 0
    if len(args) == 2 and args[0] == 'wakeup':
        core.require(args[1] in {'prefix', 'mention', 'mention_or_prefix'}, 'wakeup_mode')
        updated = configuration.validate({**core.SETTINGS, 'groupWakeMode': args[1]})
        info = CONFIG.lstat()
        core.owner.write_json(CONFIG, updated)
        os.chown(CONFIG, info.st_uid, info.st_gid)
        core.owner.wire({'stage': 'forwarder_config_updated', 'ok': True, 'groupWakeMode': args[1]})
        return 0
    if args == ['--serve']:
        return core.serve()
    if args == ['start']:
        return start()
    if args == ['status']:
        return invoke('status')
    if args == ['stop']:
        return invoke('stop')
    if args == ['install']:
        install()
        return start()
    if args in (['restart'], ['promote']):
        core.require(args != ['promote'] or not (core.owner.STATE / 'forward-image-hold.json').exists(),
                     'quarantined_image_requires_review')
        command(['/usr/bin/systemctl', 'stop', core.UNIT])
        stopped_receipt()
        if args == ['promote']:
            # One deliberate upgrade from the original 1h runtime. Stop uses
            # the owner's final snapshot; restore never permits a new QR.
            core.require(core.owner.owner_active(), 'owner_unavailable')
            pinned = core.groups.PinnedNative()
            try:
                pinned.open()
                remaining = pinned.remaining_seconds()
            finally:
                pinned.close()
            if remaining <= core.owner.RUNTIME_SECONDS:
                core.require(core.owner.client('stop') == 0, 'session_stop_failed')
                core.owner.start_owner(False, seconds=core.owner.LONG_RUNTIME_SECONDS)
                core.trial.ensure_session()
        return start()
    return 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        code = getattr(error, 'stage', 'forwarder_preflight_failed')
        if sys.argv[1:] == ['--serve']:
            try:
                core.status({'active': False, 'ready': False, 'code': code})
            except Exception:
                pass
        core.owner.wire({'stage': 'forwarder_result', 'ok': False, 'code': code})
        raise SystemExit(1)
