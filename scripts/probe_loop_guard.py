#!/usr/bin/env python3
"""Verify the new identity guard beside the live bridge; GET only, no SSE/send."""
import importlib.util
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time


def main():
    spec = importlib.util.spec_from_file_location('_loop_probe_formal', Path(__file__).with_name('run_forwarder.py'))
    formal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(formal)
    core = formal.core
    core.require(os.geteuid() == 0, 'linux_root_required')
    formal.load_config()
    settings = core.SETTINGS
    image_probe = sys.argv[1:] in (['--image'], ['--image-download'], ['--sticker'])
    sticker_probe = sys.argv[1:] == ['--sticker']
    baseline = {kind: core.owned.identity('/proc/self/ns/' + kind) for kind in ('net', 'user')}
    network, business = core.tap.read_network(), core.tap.business_snapshot()
    pinned = core.groups.PinnedNative()
    unit = core.groups.READER_PREFIX + secrets.token_hex(6) + '.service'
    bundle, capture = None, None
    verified, clean, unchanged = False, False, False
    identity_verified, image_result = False, None
    failure_code = None
    media_lease = None
    egress_verified = False
    try:
        pinned.open()
        media_lease = core.media_egress.Lease(core, pinned)
        if image_probe:
            media_lease.original = media_lease.policy()
            core.require(media_lease.original == media_lease.base_policy(), 'media_egress_policy')
            media_lease.helper()
            media_lease.sdk_destination()
            egress_verified = True
        _, binding = core.groups.load_send_binding(pinned, settings['groupName'])
        bundle = Path(tempfile.mkdtemp(prefix='astrbot-wechat-loop-check.', dir='/var/tmp'))
        for name in core.FILES:
            source = core.HERE / name
            if sticker_probe and name in {'image_native.py', 'forward_native_worker.py', 'image_input.py'}:
                source = core.HERE / 'sticker-probe-staging' / name
            if not source.exists():
                source = core.HERE.parent / 'wechat_bridge' / name
            core.groups.legacy.write_public(bundle, name, core.groups.legacy.source_text(source))
        os.chmod(bundle, 0o755)
        core.tap.require_unit_absent(unit)
        properties = core.groups.reader_properties(bundle, 'awlc-' + secrets.token_hex(4), pinned.host.parent, pinned.netfd, 120)
        config = {'account': 'wechat-primary', 'token': pinned.token(), 'runtimeSeconds': 120,
                  'privateName': settings['privateContact'], 'groupName': settings['groupName'], 'roomId': binding['roomId'],
                  'groupPrefix': settings['groupPrefix'], 'groupWakeMode': settings['groupWakeMode'],
                  'sendInterval': settings['sendIntervalSeconds'], 'selfProof': core.self_proof()}
        if image_probe:
            config.update(imageProbe=True, imageProbeDownload=sys.argv[1:] == ['--image-download'])
            if sticker_probe:
                config['stickerProbe'] = True
        arguments = ['/usr/bin/systemd-run', '--system', '--quiet', '--collect', '--wait', '--pipe', '--unit=' + unit,
                     *properties, '/usr/bin/python3', '-I', '-B', '/opt/forward_native_worker.py']
        capture = core.Duplex(arguments, config)
        del config
        isolated = False
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline and not verified:
            for record in capture.tick():
                if record.get('kind') == 'isolated' and record.get('ok') is True and not isolated:
                    core.groups.verify_reader(unit, pinned, 'forward_native_worker.py')
                    isolated = True
                    capture.acknowledge()
                elif record.get('kind') == 'native_ready' and isolated:
                    identity_verified = core.loops.sender_id(record.get('selfSenderId')) and record['selfSenderId'] != record.get('peer')
                    core.require(identity_verified, 'self_identity_unverified')
                    if not image_probe:
                        verified = True
                        capture.send({'kind': 'stop'})
                elif record.get('kind') == 'image_probe' and image_probe and identity_verified:
                    image_result = {k: v for k, v in record.items() if k != 'kind'}
                    verified = True
                elif record.get('kind') in {'media_wait', 'media_done'} and image_probe and identity_verified:
                    if record['kind'] == 'media_wait':
                        media_lease.wait(record['jobId'])
                    else:
                        media_lease.done(record['jobId'])
                elif record.get('kind') == 'native_error':
                    failure_code = record.get('code') if record.get('code') in core.images.ERROR_CODES else 'image_failed'
                    raise RuntimeError('probe_native_failed')
                else:
                    raise RuntimeError('loop_probe_frame')
            core.require(capture.process.poll() is None or verified or image_probe and identity_verified, 'loop_probe_exited')
        core.require(verified, 'loop_probe_deadline')
    except Exception as error:
        if str(error) in core.media_egress.ERROR_CODES:
            failure_code = str(error)
        verified = False
    finally:
        if media_lease:
            try:
                media_lease.close()
            except Exception:
                verified = False
        try:
            core.tap.stop_unit(unit, True)
            clean = True
        except Exception:
            pass
        if capture:
            capture.close()
        unchanged = pinned.host is not None and pinned.unchanged()
        pinned.close()
        if bundle:
            for name in core.FILES:
                (bundle / name).unlink(missing_ok=True)
            bundle.rmdir()
        flags, failures = core.tap.compare_baselines(baseline, network, business)
        good = verified and clean and unchanged and not failures
        core.owner.wire({'stage': 'image_input_probe' if image_probe else 'loop_guard_readonly_probe', 'ok': good,
                         'self_identity_verified': identity_verified, **({'image': image_result} if image_probe else {}),
                         **({'code': failure_code} if failure_code else {}),
                         **({'egress_baseline_verified': egress_verified,
                             'temporary_cdn_grants': media_lease.grants if media_lease else 0,
                             'temporary_egress_closed': bool(media_lease and not media_lease.changed)} if image_probe else {}),
                         'native_session_unchanged': unchanged, 'own_worker_removed': clean,
                         'model_called': False, 'new_qr': False, **flags})
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
