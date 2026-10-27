#!/usr/bin/env python3
"""Record the user's explicit AW01 private-delivery confirmation; never sends."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
SHA = '264a52467f2becaec18c7373782130a967d33651ee981f2bcd7376f879fb37e2'


def main():
    if os.geteuid() != 0 or sys.argv[1:]:
        return 2
    spec = importlib.util.spec_from_file_location('_confirmed_image', ROOT / 'run_forwarder.py')
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core, owner = formal.core, formal.core.owner
    path = owner.STATE / 'forward-image-send.json'
    attempt = owner.STATE / 'image-output-poc-private.json'
    audit, previous = owner.read_json(path), owner.read_json(attempt)
    core.require(audit.get('state') == 'unknown' and audit.get('code') == 'image_receipt_binding'
        and audit.get('sourceSha256') == SHA and audit.get('bytes') == 793
        and previous.get('scope') == 'private' and previous.get('key') == audit.get('key')
        and previous.get('jobId') == audit.get('jobId') and core.output_images.job_id(audit.get('jobId')),
        'confirmation_not_bound')
    native = core.groups.PinnedNative()
    native.open()
    host = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net','user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    try:
        audit.update(state='accepted', code='ok_user_confirmed',
                     verification='user_confirmed_complete_aw01_private')
        owner.write_json(path, audit)
        owner.write_json(attempt, {**audit, 'scope': 'private'})
        formal.start()
        until = time.monotonic() + 35
        while time.monotonic() < until:
            now = owner.read_json(core.STATUS)
            if now.get('ready') is True:
                flags, failures = core.tap.compare_baselines(host, network, business)
                core.require(not failures and native.unchanged(), 'restored_bridge_requires_review')
                print(json.dumps({'stage':'confirmed_private_image','ok':True,'bridge_ready':True,
                    'native_session_unchanged':True,'new_qr':False,**flags},sort_keys=True))
                return 0
            time.sleep(.5)
        raise ValueError('bridge_not_ready')
    finally:
        native.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'stage':'confirmed_private_image_failed','ok':False,
                          'code':getattr(error,'stage','confirmation_failed')}))
        raise SystemExit(1)
