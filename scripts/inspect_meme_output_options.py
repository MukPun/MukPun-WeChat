#!/usr/bin/env python3
"""Read only non-secret meme format options and bounded asset statistics."""
import importlib.util
import json
import os
from pathlib import Path
import resource


def main():
    assert os.geteuid() == 0
    resource.setrlimit(resource.RLIMIT_AS, (64*1024*1024, 64*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    root = Path('/home/bridgeuser/AstrBot/data')
    allowed = {'convert_static_to_gif', 'enable_mixed_message', 'mixed_message_probability',
               'send_image_as_base64', 'emotions_probability', 'max_memes_per_reply', 'mode'}
    options = []
    for p in (root / 'config').glob('*meme*json'):
        assert p.stat().st_size < 256*1024 and not p.is_symlink()
        data = json.loads(p.read_text(encoding='utf-8-sig'))
        def select(data, prefix=''):
            result = {}
            for k, v in data.items():
                if k in allowed and (v is None or type(v) in (bool, int, float) or isinstance(v, str) and len(v) <= 32):
                    result[prefix + k] = v
                elif isinstance(v, dict):
                    result.update(select(v, prefix + k + '.'))
            return result
        options.append(select(data))
    spec = importlib.util.spec_from_file_location('_inspected_image', Path(__file__).with_name('image_output.py'))
    image = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(image)
    counts, eligible, rejected = {}, {}, {}
    for p in (root / 'plugin_data/meme_manager/packs').rglob('*'):
        if not p.is_file() or p.is_symlink() or p.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
            continue
        ext = p.suffix.lower()
        counts[ext] = counts.get(ext, 0) + 1
        if not 0 < p.stat().st_size <= image.MAX_IMAGE:
            rejected['size'] = rejected.get('size', 0) + 1
            continue
        try:
            image.payload(p.read_bytes())
            eligible[ext] = eligible.get(ext, 0) + 1
        except image.OutputError as error:
            code = str(error)
            rejected[code] = rejected.get(code, 0) + 1
    print(json.dumps({'stage': 'meme_output_options', 'options': options, 'assets': counts,
                      'eligible': eligible, 'rejected': rejected}, sort_keys=True))


if __name__ == '__main__':
    main()
