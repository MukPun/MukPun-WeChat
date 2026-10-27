#!/usr/bin/env python3
"""Read only non-secret image switches and scope shapes; never dump configs."""
import json
from pathlib import Path
import resource


def main():
    resource.setrlimit(resource.RLIMIT_AS, (134217728, 134217728))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    path = Path('/home/bridgeuser/AstrBot/data/config/astrbot_plugin_group_chat_plus_config.json')
    if path.is_symlink() or not 0 < path.stat().st_size <= 1024 * 1024:
        raise ValueError('config_shape')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    keys = ('enable_group_chat', 'enable_private_chat', 'enable_image_processing', 'image_to_text_scope',
            'image_to_text_provider_id', 'max_images_per_message', 'private_enable_image_processing',
            'private_image_to_text_provider_id', 'private_image_to_text_scope')
    flags = {k: value.get(k, '<not set>') for k in keys}
    scopes = {k: {'type': type(v).__name__, 'count': len(v) if isinstance(v, (list, dict)) else None}
              for k, v in value.items() if any(word in k.lower() for word in ('whitelist', 'blacklist', 'group_id', 'group_list', 'session', 'group_config'))}
    print(json.dumps({'ok': True, 'group_plugin_image_settings': flags, 'scope_shapes': scopes}))


if __name__ == '__main__':
    main()
