#!/usr/bin/env python3
"""Print only adapter shape/booleans; never print configuration credentials."""
import json
import os
from pathlib import Path
import stat

PATH = Path('/home/bridgeuser/AstrBot/data/cmd_config.json')
LIMIT = 4 * 1024 * 1024
STEP = 'opening'


def main():
    global STEP
    descriptor = os.open(PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= LIMIT:
            raise ValueError('invalid configuration file')
        raw = os.read(descriptor, LIMIT + 1)
    finally:
        os.close(descriptor)
    STEP = 'parsing'
    value = json.loads(raw.decode('utf-8-sig'))
    STEP = 'configuration_shape'
    if not isinstance(value, dict):
        raise ValueError('invalid configuration')
    platforms = value.get('platform', [])
    if not isinstance(platforms, list) or len(platforms) > 100:
        raise ValueError('invalid platform list')
    summary = []
    known_types = {'aiocqhttp', 'telegram', 'discord', 'weixin_official_account', 'weixin_oc',
                   'qq_official', 'qq_official_webhook', 'webchat'}
    for item in platforms:
        if not isinstance(item, dict):
            raise ValueError('invalid platform')
        port = item.get('ws_reverse_port')
        kind = item.get('type')
        summary.append({
            'type': kind if isinstance(kind, str) and kind in known_types else 'other',
            'enabled': item.get('enable') is True,
            'reverse_port': port if type(port) is int and 1 <= port <= 65535 else None,
            'has_reverse_token': bool(item.get('ws_reverse_token')),
            'has_access_token': bool(item.get('access_token')),
        })
    print(json.dumps({'ok': True, 'platform_count': len(summary), 'platforms': summary}, sort_keys=True))


try:
    main()
except Exception as error:
    print(json.dumps({'ok': False, 'code': 'readonly_inspection_failed', 'step': STEP,
                      'error_class': type(error).__name__}))
    raise SystemExit(1)
