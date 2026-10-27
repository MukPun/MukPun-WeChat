#!/usr/bin/env bash
# Runs exactly one short, offline, empty-account probe. No Docker/FRP commands.
set -euo pipefail
umask 077

if [ "${EUID}" -ne 0 ]; then
  printf '%s\n' 'This isolated probe requires sudo. Do not put passwords in scripts.' >&2
  exit 1
fi
task_probe_args=()
if [ "$#" -gt 0 ]; then
  if [ "$#" -ne 1 ] || [ "$1" != '--isolation-only' ]; then
    printf '%s\n' 'Only the optional --isolation-only mode is supported.' >&2
    exit 2
  fi
  task_probe_args=(--isolation-only)
fi
task_script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
task_binary="${task_script_dir}/assets/wx-v1.0.0-beta.8-linux-x64-gnu"
task_controller="${task_script_dir}/empty_client_probe.py"
task_sha=fde2920e71e0e20b926498b8c306a5064527afa5abd2b558eff9cefa0cce478e

for task_input in "${task_binary}" "${task_controller}"; do
  if [ ! -f "${task_input}" ] || [ -L "${task_input}" ]; then
    printf 'Missing, symlink, or non-regular probe input: %s\n' "${task_input}" >&2
    exit 1
  fi
done
printf '%s  %s\n' "${task_sha}" "${task_binary}" | sha256sum --check --status
[ "$(stat -c %s "${task_binary}")" = '28757872' ]
/usr/bin/python3 -c 'import os; s=os.statvfs("/var/tmp"); assert s.f_bavail*s.f_frsize >= 128*1024*1024, "Insufficient staging headroom"; assert not s.f_flag & os.ST_NOEXEC, "Staging filesystem forbids execution"'
for task_existing in /opt /run/docker.sock /run/containerd /tmp /var/tmp /var/lib /dev/shm; do
  if [ ! -e "${task_existing}" ]; then
    printf 'Expected host path missing; inspect before continuing: %s\n' "${task_existing}" >&2
    exit 1
  fi
done

task_unit="astrbot-wechat-empty-probe-$(date +%s)-$$"
task_bundle=$(mktemp -d /var/tmp/astrbot-wechat-poc.XXXXXXXX)
task_started=0
task_cleanup() {
  task_status=$?
  trap - EXIT INT TERM
  if [ "${task_started}" = 1 ]; then
    systemctl stop "${task_unit}.service" >/dev/null 2>&1 || true
  fi
  case "${task_bundle}" in
    /var/tmp/astrbot-wechat-poc.*)
      rm -f -- "${task_bundle}/wx" "${task_bundle}/empty_client_probe.py" "${task_bundle}/server.toml"
      rmdir -- "${task_bundle}" || true
      ;;
    *) printf '%s\n' 'Unexpected bundle path; refusing cleanup.' >&2 ;;
  esac
  exit "${task_status}"
}
trap task_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

/usr/bin/prlimit --fsize=30000000:30000000 -- /usr/bin/install -m 555 -- "${task_binary}" "${task_bundle}/wx"
install -m 444 -- "${task_controller}" "${task_bundle}/empty_client_probe.py"
printf 'schemaVersion = 1\n' > "${task_bundle}/server.toml"
chmod 444 "${task_bundle}/server.toml"
chmod 755 "${task_bundle}"
# Verify the root-owned copy as well, before making it visible to the sandbox.
printf '%s  %s\n' "${task_sha}" "${task_bundle}/wx" | sha256sum --check --status

printf 'PROBE_UNIT=%s.service\n' "${task_unit}"
printf '%s\n' 'Offline probe: no account creation, QR, WeChat network, or AstrBot connection.'
task_started=1
systemd-run --system --unit="${task_unit}" --collect --wait --pipe \
  --property=Type=exec \
  --property=UMask=0077 \
  --property=DynamicUser=yes \
  --property=CPUQuota=20% \
  --property=CPUQuotaPeriodSec=100ms \
  --property=MemoryHigh=96M \
  --property=MemoryMax=128M \
  --property=MemorySwapMax=0 \
  --property=TasksMax=32 \
  --property=LimitNOFILE=128 \
  --property=LimitCORE=0 \
  --property=LimitFSIZE=8M \
  --property=RuntimeMaxSec=45 \
  --property=TimeoutStartSec=10 \
  --property=TimeoutStopSec=3 \
  --property=SendSIGKILL=yes \
  --property=Restart=no \
  --property=KillMode=control-group \
  --property=OOMPolicy=stop \
  --property=Nice=19 \
  --property=IOSchedulingClass=idle \
  --property=NoNewPrivileges=yes \
  --property=PrivateNetwork=yes \
  --property=PrivateDevices=yes \
  --property=PrivateTmp=no \
  --property=ProtectHome=tmpfs \
  --property=ProtectSystem=strict \
  --property=ProtectProc=invisible \
  --property=ProtectKernelTunables=yes \
  --property=ProtectKernelModules=yes \
  --property=ProtectControlGroups=yes \
  --property=RestrictNamespaces=yes \
  --property=RestrictSUIDSGID=yes \
  --property=RestrictRealtime=yes \
  --property=LockPersonality=yes \
  --property=SystemCallArchitectures=native \
  --property=CapabilityBoundingSet= \
  --property='RestrictAddressFamilies=AF_INET AF_INET6' \
  --property='InaccessiblePaths=-/run/docker.sock -/run/containerd -/run/user -/tmp -/var/tmp' \
  --property="BindReadOnlyPaths=${task_bundle}:/opt" \
  --property='TemporaryFileSystem=/var/lib:rw,size=32M,nr_inodes=1024,mode=1777,nosuid,nodev,noexec /dev/shm:ro,size=4K,nr_inodes=16,mode=000,nosuid,nodev,noexec' \
  /usr/bin/python3 -I -B /opt/empty_client_probe.py "${task_probe_args[@]}"
