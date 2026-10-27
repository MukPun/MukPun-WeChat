#!/usr/bin/env bash
# Downloads a pinned public executable as data. Never executes that executable.
set -euo pipefail
umask 077

task_script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
task_assets="${task_script_dir}/assets"
task_name=wx-v1.0.0-beta.8-linux-x64-gnu
task_sha=fde2920e71e0e20b926498b8c306a5064527afa5abd2b558eff9cefa0cce478e
task_url="https://github.com/thisnick/agent-wechat/releases/download/v1.0.0-beta.8/${task_name}"

if [ -L "${task_assets}" ]; then
  printf '%s\n' 'Refusing a symlink asset directory.' >&2
  exit 1
fi
install -d -m 700 "${task_assets}"
task_target="${task_assets}/${task_name}"
if [ -e "${task_target}" ] || [ -L "${task_target}" ]; then
  if [ -L "${task_target}" ] || [ ! -f "${task_target}" ]; then
    printf '%s\n' 'Refusing a non-regular or symlink executable.' >&2
    exit 1
  fi
  printf '%s  %s\n' "${task_sha}" "${task_target}" | sha256sum --check --status
  chmod 600 "${task_target}"
  printf '%s\n' 'PINNED_ASSET_VERIFIED: existing download matches SHA256; not executed.'
  exit 0
fi
if [ -e "${task_target}.part" ] || [ -L "${task_target}.part" ]; then
  printf '%s\n' 'Partial download exists; inspect it before retrying. Nothing overwritten.' >&2
  exit 1
fi

task_download_unit="astrbot-wechat-asset-download-$(date +%s)-$$"
systemd-run --user --unit="${task_download_unit}" --collect --wait --pipe \
  --property=Type=exec \
  --property=UMask=0077 \
  --property=CPUQuota=20% \
  --property=CPUQuotaPeriodSec=100ms \
  --property=MemoryMax=128M \
  --property=MemorySwapMax=0 \
  --property=TasksMax=16 \
  --property=LimitFSIZE=32M \
  --property=RuntimeMaxSec=330 \
  --property=TimeoutStopSec=3 \
  --property=Restart=no \
  --property=KillMode=control-group \
  --property=Nice=19 \
  --property=IOSchedulingClass=idle \
  /usr/bin/curl --disable --fail --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 10 --max-time 300 --max-filesize 30000000 \
  --limit-rate 128K --retry 0 --silent --show-error \
  --output "${task_target}.part" "${task_url}"

printf '%s  %s\n' "${task_sha}" "${task_target}.part" | sha256sum --check --status
[ "$(stat -c %s "${task_target}.part")" = '28757872' ]
# Keep it non-executable; only the later root sandbox launcher prepares a copy.
chmod 600 "${task_target}.part"
mv -T --no-clobber -- "${task_target}.part" "${task_target}"
if [ -e "${task_target}.part" ] || [ -L "${task_target}" ]; then
  printf '%s\n' 'Download publish was not exclusive; refusing success.' >&2
  exit 1
fi
printf '%s  %s\n' "${task_sha}" "${task_target}" | sha256sum --check --status
[ "$(stat -c %s "${task_target}")" = '28757872' ]
chmod 600 "${task_target}"
printf '%s\n' 'PINNED_ASSET_VERIFIED: fixed release downloaded and verified; not executed.'
