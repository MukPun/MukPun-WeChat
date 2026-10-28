#!/usr/bin/env python3
"""Read image-pipeline counters, plugin switches and history shapes only."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys


def read(path):
    path = Path(path)
    assert not path.is_symlink() and 0 < path.stat().st_size < 4 * 1024 * 1024
    return json.loads(path.read_text(encoding='utf-8-sig'))


def main():
    assert os.geteuid() == 0 and not sys.argv[1:]
    status = read('/run/astrbot-wechat-session/forwarder.json')
    result = {'bridge': {key: status.get(key) for key in ('updated', 'started_at', 'active', 'ready', 'code',
        'sse', 'astrbot_connected', 'all_groups_enabled', 'image_input_enabled', 'images', 'last_image_error',
        'loop_guard', 'received', 'forwarded', 'queue', 'group_traffic', 'uncertain')}}
    config = read('/home/bridgeuser/AstrBot/data/cmd_config.json')
    platforms = [str(item['id']) for item in config.get('platform', []) if item.get('type') == 'aiocqhttp']
    plugin = read('/home/bridgeuser/AstrBot/data/config/astrbot_plugin_group_chat_plus_config.json')
    result['plugin'] = {key: plugin.get(key) for key in ('enable_group_chat', 'enable_private_chat',
        'enable_image_processing', 'image_to_text_scope', 'max_images_per_message', 'private_enable_image_processing')}
    result['plugin']['image_provider_selected'] = bool(plugin.get('image_to_text_provider_id'))
    database = Path('/home/bridgeuser/AstrBot/data/data_v4.db').resolve(strict=True)
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.5)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        columns = {row[1] for row in connection.execute('PRAGMA table_info(platform_message_history)')}
        result['history_columns'] = sorted(columns)
        assert {'id', 'created_at', 'content', 'platform_id', 'user_id'} <= columns
        records, images = [], []
        for identity, created, content, platform, origin in connection.execute(
                'SELECT id,created_at,content,platform_id,user_id FROM platform_message_history ORDER BY id DESC LIMIT 200'):
            if str(platform) not in platforms or 'GroupMessage' not in str(origin):
                continue
            data = json.loads(content) if isinstance(content, str) else content
            if not isinstance(data, dict):
                continue
            parts = data.get('message', [])
            types = [str(part.get('type', 'unknown')).lower() for part in parts if isinstance(part, dict)]
            item = {'id': identity, 'time': str(created), 'role': data.get('type'), 'types': types,
                    'has_image': any(value == 'image' or value.endswith('.image') for value in types)}
            if len(records) < 16:
                records.append(item)
            if item['has_image']:
                images.append(item)
        result['recent_group_records'] = records
        result['recent_group_image_records'] = images[:8]
        result['image_record_count_in_recent_200'] = len(images)
    finally:
        connection.close()
    print(json.dumps({'stage': 'group_image_delivery_inspection', **result}), flush=True)


if __name__ == '__main__':
    main()
