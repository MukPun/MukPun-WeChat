#!/usr/bin/env python3
"""Bounded read-only extraction of meme selections from existing host logs."""
import ast
import json
import os
from pathlib import Path
import re
import resource
import stat


def bounded(path, maximum, tail=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode)
        if tail:
            os.lseek(fd, max(0, info.st_size - maximum), os.SEEK_SET)
        else:
            assert info.st_size <= maximum
        return os.read(fd, maximum)
    finally:
        os.close(fd)


def main():
    assert os.geteuid() == 0
    resource.setrlimit(resource.RLIMIT_AS, (96*1024*1024, 96*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    result = {'stage': 'failed_meme_selection', 'matched_containers': 0, 'selections': [], 'emotions': [], 'failures': 0,
              'data_mounts': []}
    folders = list(Path('/var/lib/docker/containers').iterdir())
    assert len(folders) <= 64
    for folder in folders:
        if not re.fullmatch('[0-9a-f]{64}', folder.name) or folder.is_symlink():
            continue
        config = json.loads(bounded(folder / 'config.v2.json', 1024*1024))
        if 'astrbot' not in str(config.get('Name', '')).lower():
            continue
        result['matched_containers'] += 1
        for mount in config.get('MountPoints', {}).values():
            if mount.get('Destination') == '/AstrBot/data':
                result['data_mounts'].append({'source': mount.get('Source'), 'destination': '/AstrBot/data'})
        log = folder / (folder.name + '-json.log')
        if not log.exists():
            continue
        for raw in bounded(log, 4*1024*1024, tail=True).splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            at = str(row.get('time', ''))
            if not ('2026-10-27T03:48:' <= at < '2026-10-27T03:51:'):
                continue
            line = row.get('log', '')
            if not isinstance(line, str):
                continue
            result['failures'] += int('发送表情图片失败' in line)
            match = re.search(r'已选择语义表情[：:]\s*(\[[^\r\n]{1,2048}\])', line)
            if match:
                ids = ast.literal_eval(match[1])
                if isinstance(ids, list) and len(ids) <= 8 and all(isinstance(i, str) and re.fullmatch(r'[a-zA-Z0-9_:/.-]{1,256}', i) for i in ids):
                    result['selections'].append({'at': at[:19], 'ids': ids})
            match = re.search(r'去重后的最终表情列表[：:]\s*(\[[^\r\n]{1,512}\])', line)
            if match:
                labels = ast.literal_eval(match[1])
                if isinstance(labels, list) and len(labels) <= 8 and all(isinstance(i, str) and re.fullmatch(r'[\w-]{1,64}', i) for i in labels):
                    result['emotions'].append({'at': at[:19], 'labels': labels})
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
