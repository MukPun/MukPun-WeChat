#!/usr/bin/env bash
# Trusted Python probe only. No third-party executable, Docker or FRP commands.
# Creates one transient systemd unit which exits automatically.
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
  printf '%s\n' 'Run with sudo in your own server terminal. Do not share your password.' >&2
  exit 1
fi

task_unit="astrbot-wechat-system-probe-$(date +%s)-$$"
for task_mask_target in /run/docker.sock /run/containerd; do
  if [ ! -e "${task_mask_target}" ]; then
    printf 'Expected host path is absent: %s; inspect before testing.\n' "${task_mask_target}" >&2
    exit 1
  fi
done
task_probe_code=$(cat <<'PY'
import json
import os
import pathlib
import stat

def socket_usable(path):
    try:
        item = pathlib.Path(path)
        return stat.S_ISSOCK(item.stat().st_mode) and os.access(path, os.W_OK)
    except (PermissionError, FileNotFoundError):
        return False

net = pathlib.Path('/proc/net/dev').read_text()
interfaces = [line.split(':', 1)[0].strip() for line in net.splitlines() if ':' in line]
control = pathlib.Path('/proc/self/cgroup').read_text().strip().split(':')[-1]
base = pathlib.Path('/sys/fs/cgroup') / control.lstrip('/')
limits = {key: (base / key).read_text().strip() for key in
          ('memory.max', 'memory.high', 'memory.swap.max', 'pids.max', 'cpu.max')}
mountpoints = {line.split()[4] for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()}
checks = {
    'non_root': os.geteuid() != 0,
    'ssh_directory_hidden': not pathlib.Path('/home/bridgeuser/.ssh').exists(),
    'docker_socket_unusable': not socket_usable('/run/docker.sock'),
    'containerd_socket_unusable': not socket_usable('/run/containerd/containerd.sock'),
    'docker_path_mask_mount': '/run/docker.sock' in mountpoints,
    'containerd_path_mask_mount': '/run/containerd' in mountpoints,
    'private_network_loopback_only': interfaces == ['lo'],
    'root_readonly_mount': bool(os.statvfs('/').f_flag & os.ST_RDONLY),
    'usr_readonly_mount': bool(os.statvfs('/usr').f_flag & os.ST_RDONLY),
    'etc_readonly_mount': bool(os.statvfs('/etc').f_flag & os.ST_RDONLY),
    'memory_128mb': limits['memory.max'] == '134217728',
    'memory_high_96mb': limits['memory.high'] == '100663296',
    'no_swap': limits['memory.swap.max'] == '0',
    'tasks_16': limits['pids.max'] == '16',
    'cpu_20_percent': limits['cpu.max'] == '20000 100000',
    'own_test_cgroup': 'astrbot-wechat-system-probe-' in control,
}
print(json.dumps({'checks': checks, 'limits': limits, 'interfaces': interfaces,
                  'uid': os.geteuid()}, indent=2), flush=True)
if not all(checks.values()):
    raise SystemExit('Sandbox verification FAILED: do not run the WeChat client.')
print('SANDBOX_VERIFIED: third-party execution is still a separate step.', flush=True)
PY
)

systemd-run --system --unit="${task_unit}" --collect --wait --pipe \
  --property=Type=exec \
  --property=DynamicUser=yes \
  --property=CPUQuota=20% \
  --property=CPUQuotaPeriodSec=100ms \
  --property=MemoryHigh=96M \
  --property=MemoryMax=128M \
  --property=MemorySwapMax=0 \
  --property=TasksMax=16 \
  --property=RuntimeMaxSec=20 \
  --property=TimeoutStartSec=10 \
  --property=TimeoutStopSec=3 \
  --property=Restart=no \
  --property=KillMode=control-group \
  --property=Nice=19 \
  --property=IOSchedulingClass=idle \
  --property=NoNewPrivileges=yes \
  --property=PrivateNetwork=yes \
  --property=PrivateDevices=yes \
  --property=PrivateTmp=yes \
  --property=ProtectHome=tmpfs \
  --property=ProtectSystem=strict \
  --property=ProtectProc=invisible \
  --property=RestrictNamespaces=yes \
  --property=RestrictSUIDSGID=yes \
  --property=CapabilityBoundingSet= \
  --property='InaccessiblePaths=-/run/docker.sock -/run/containerd -/run/user' \
  /usr/bin/python3 -c "${task_probe_code}"
