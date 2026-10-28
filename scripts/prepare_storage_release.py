#!/usr/bin/env python3
"""Hash only reviewed public-code paths; never read instance configuration."""
import ast
import difflib
import hashlib
import json
from pathlib import Path

NAMES = (
    'run_private_forwarder', 'image_output_budget', 'image_source_cache',
    'image_send_native', 'forward_native_worker', 'session_state_store',
    'empty_client_probe', 'session_runtime_client', 'session_runtime_host',
    'session_runtime', 'run_group_probe', 'group_reader_client',
    'run_bridge_trial', 'bridge_host_worker', 'accept_forwarder_core', 'media_cache_maintenance',
)
LIBRARIES = ('image_input', 'image_output')


def mapping():
    result = {}
    for name in (*NAMES, *LIBRARIES):
        result[name+'.py'] = 'verification/scripts/'+name+'.py'
        result['verification/scripts/'+name+'.py'] = 'verification/scripts/'+name+'.py'
    for name in (*LIBRARIES, 'transport'):
        result['verification/wechat_bridge/'+name+'.py'] = 'verification/wechat_bridge/'+name+'.py'
    return result


def inspect(root):
    stage = root/'context-staging'
    manifest = {}
    summary = []
    for target, source in mapping().items():
        raw = (stage/source).read_bytes()
        ast.parse(raw)
        old = (root/target).read_bytes() if (root/target).exists() else None
        manifest[target] = {'source': source, 'sha256': hashlib.sha256(raw).hexdigest(),
                            'previous': hashlib.sha256(old).hexdigest() if old is not None else None}
        if old != raw:
            changes = list(difflib.unified_diff((old or b'').decode().splitlines(), raw.decode().splitlines(), n=1))
            summary.append({'path': target, 'changed_lines': len(changes), 'new': old is None})
    (stage/'storage-manifest.json').write_text(json.dumps(manifest,sort_keys=True))
    print(json.dumps({'stage':'storage_release_prepared','files':len(manifest),'changes':summary}))


if __name__ == '__main__':
    inspect(Path(__file__).resolve().parent)
