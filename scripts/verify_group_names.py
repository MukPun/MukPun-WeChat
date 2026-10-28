#!/usr/bin/env python3
"""Inspect only name/connection aggregates, never chat bodies or real names."""
import json
import os
from pathlib import Path
import sqlite3
import sys


def connect(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True, timeout=.5)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA trusted_schema=OFF')
    return connection


def main():
    assert os.geteuid() == 0 and not sys.argv[1:]
    status = json.loads(Path('/run/astrbot-wechat-session/forwarder.json').read_text())
    result = {key: status.get(key) for key in ('active', 'ready', 'sse', 'astrbot_connected', 'received', 'forwarded', 'uncertain')}
    with connect('/var/lib/astrbot-wechat-bridge/private-forward.sqlite') as connection:
        result['member_profiles'] = connection.execute('SELECT COUNT(*) FROM member_names').fetchone()[0]
        result['named_profiles'] = connection.execute("SELECT COUNT(*) FROM member_names WHERE nickname<>''").fetchone()[0]
        result['group_cards'] = connection.execute("SELECT COUNT(*) FROM member_names WHERE card<>''").fetchone()[0]
        result['queue'] = dict(connection.execute('SELECT state,COUNT(*) FROM inbox GROUP BY state'))
        expected = set()
        for nickname, card in connection.execute('SELECT nickname,card FROM member_names'):
            if nickname:
                expected.add(card or nickname)
    config = json.loads(Path('/home/bridgeuser/AstrBot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    platforms = [str(item['id']) for item in config.get('platform', []) if item.get('type') == 'aiocqhttp']
    with connect('/home/bridgeuser/AstrBot/data/data_v4.db') as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%history%'")}
        result['history_tables'] = sorted(tables)
        if 'platform_message_history' in tables:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(platform_message_history)')}
            if {'sender_name', 'platform_id', 'id'} <= columns:
                rows = connection.execute('SELECT sender_name,platform_id FROM platform_message_history ORDER BY id DESC LIMIT 100').fetchall()
                selected = [name for name, platform in rows if str(platform) in platforms]
                result['recent_astrbot_records'] = len(selected)
                result['recent_matching_names'] = sum(bool(name) and name in expected for name in selected)
    print(json.dumps({'stage': 'group_names_verification', **result}), flush=True)


if __name__ == '__main__':
    main()
