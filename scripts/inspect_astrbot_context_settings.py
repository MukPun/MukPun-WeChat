#!/usr/bin/env python3
"""Read only non-secret wake/context switches before changing bridge admission."""
import ast
import json
import os
from pathlib import Path
import resource
import stat

ROOT = Path('/home/bridgeuser/AstrBot/data')


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4 * 1024 * 1024:
            raise ValueError('config_shape')
        value = json.loads(os.read(fd, 4 * 1024 * 1024 + 1).decode('utf-8-sig'))
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise ValueError('config_shape')
    return value


def shape(value):
    if value is None or type(value) in (bool, int, float):
        return value
    return {'type': type(value).__name__, 'count': len(value) if isinstance(value, (dict, list, str)) else None}


def main():
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    config = read(ROOT / 'cmd_config.json')
    profiles = [('default', config)]
    profiles += [(p.stem, read(p)) for p in sorted((ROOT / 'config').glob('abconf_*.json'))[:12]]
    for name, c in profiles:
        provider = c.get('provider_ltm_settings', {})
        platform = c.get('platform_settings', {})
        print(json.dumps({'profile': name, 'wake_prefix': c.get('wake_prefix'),
            'context': {k: shape(provider.get(k)) for k in ('group_icl_enable', 'group_message_max_cnt', 'image_caption')},
            'active_reply': {k: shape(provider.get('active_reply', {}).get(k)) for k in ('enable', 'possibility_reply', 'whitelist')},
            'platform': {k: shape(platform.get(k)) for k in ('enable_id_white_list', 'id_whitelist', 'ignore_bot_self_message', 'ignore_at_all', 'unique_session')},
            'plugin_set': c.get('plugin_set')}, ensure_ascii=True))
    for item in config.get('platform', []):
        if isinstance(item, dict) and item.get('type') == 'aiocqhttp':
            print(json.dumps({'onebot_enabled': item.get('enable'), 'config_binding': {k: v for k, v in item.items()
                if 'config' in k.lower() and isinstance(v, (str, bool, int))}}, ensure_ascii=True))
    plugin = read(ROOT / 'config/astrbot_plugin_group_chat_plus_config.json')
    allowed = ('enable_group_chat', 'initial_probability', 'after_reply_probability', 'probability_duration',
        'max_context_messages', 'proactive_enabled', 'frequency_adjuster_enabled', 'enable_attention',
        'keyword_smart_mode', 'trigger_keywords', 'enable_image_processing', 'image_to_text_scope',
        'enabled_groups', 'enable_private_chat')
    print(json.dumps({'group_plugin': {k: shape(plugin.get(k)) for k in allowed}}, ensure_ascii=True))
    source = ROOT / 'plugins/astrbot_plugin_group_chat_plus/main.py'
    tree = ast.parse(source.read_text())
    switches = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get'
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'config' and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            key = node.args[0].value
            if any(word in key for word in ('probability', 'proactive', 'attention', 'decision', 'cooldown', 'frequency')):
                switches[key] = shape(plugin.get(key))
    print(json.dumps({'group_plugin_trigger_settings': switches}, ensure_ascii=True))


if __name__ == '__main__':
    main()
