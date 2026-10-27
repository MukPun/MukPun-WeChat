#!/usr/bin/env python3
"""Restore only this project's slirp allowlist to its verified startup metadata."""
import importlib.util
import os
from pathlib import Path


def main():
    spec = importlib.util.spec_from_file_location('_media_restore_formal', Path(__file__).with_name('run_forwarder.py'))
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core = formal.core
    core.require(os.geteuid() == 0, 'linux_root_required')
    pinned = core.groups.PinnedNative()
    baseline = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    lease = None
    restored = False
    try:
        pinned.open()
        lease = core.media_egress.Lease(core, pinned)
        lease.original = lease.base_policy()
        lease.expected = lease.policy()
        core.require(lease.original <= lease.expected and len(lease.expected - lease.original) <= 1, 'media_egress_policy')
        lease.identity = lease.helper()
        lease.changed = lease.original != lease.expected
        lease.close()
        restored = lease.policy() == lease.original and lease.helper() == lease.identity
    finally:
        unchanged = pinned.host is not None and pinned.unchanged()
        pinned.close()
        flags, failures = core.tap.compare_baselines(baseline, network, business)
        good = restored and unchanged and not failures
        core.owner.wire({'stage': 'media_egress_restore', 'ok': good, 'original_allowlist_restored': restored,
                         'native_session_unchanged': unchanged, 'model_called': False, **flags})
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
