#!/usr/bin/env bash
# Read-only host inventory. Does not read account/configuration files.
set -u

printf '%s\n' 'AstrBot WeChat bridge: read-only Linux preflight'
if [ "$(uname -s)" != 'Linux' ]; then
  printf '%s\n' 'Run this script on the target Linux server.' >&2
  exit 1
fi

printf '\n%s\n' '[Operating system]'
if [ -r /etc/os-release ]; then
  awk -F= '$1 == "PRETTY_NAME" { print $0 }' /etc/os-release
fi
printf 'Architecture: %s\n' "$(uname -m)"

printf '\n%s\n' '[C library]'
if command -v getconf >/dev/null 2>&1; then
  getconf GNU_LIBC_VERSION 2>/dev/null || printf '%s\n' 'GNU libc version unavailable.'
else
  printf '%s\n' 'getconf unavailable; verify libc compatibility separately.'
fi
printf '%s\n' 'Candidate native agent-wechat requires glibc 2.36+; confirm its pinned release requirements.'

printf '\n%s\n' '[Docker]'
if command -v docker >/dev/null 2>&1; then
  docker --version
  docker compose version 2>/dev/null || printf '%s\n' 'Docker Compose plugin unavailable.'
else
  printf '%s\n' 'Docker CLI unavailable. Existing AstrBot may use another deployment method.'
fi

printf '\n%s\n' '[Common AstrBot ports; service identity not verified]'
if command -v ss >/dev/null 2>&1; then
  ss -ltn '( sport = :6185 or sport = :6199 )' 2>/dev/null || printf '%s\n' 'Could not inspect listening ports.'
else
  printf '%s\n' 'ss unavailable; inspect the actual AstrBot listeners separately.'
fi

printf '\n%s\n' 'Inventory complete. WeChat login, group messaging and AstrBot health have not been tested.'
