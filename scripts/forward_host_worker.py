#!/usr/bin/env python3
"""Restricted OneBot worker: no Native token, lifecycle or non-loopback URL."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import select
import sys
import time

ROOT = Path('/opt/bridge')
sys.path[:0] = [str(ROOT), str(ROOT / 'onebot-env/lib/python3.12/site-packages')]
from wechat_bridge import forward_wire as wire
from wechat_bridge.runtime.failures import exception_code


def emit(value):
    sys.stdout.buffer.write(wire.encode(value))
    sys.stdout.buffer.flush()


def action_result(answer, binding=None):
    if answer.get('retcode') != 0:
        if answer.get('retcode')==1408 and binding is not None:
            emit({'kind':'report','stage':'action_pending',**binding})
            return
        # This error is raised by validation, before reserve/send_text. It is
        # a known unsupported output, never an unknown WeChat submission.
        unsupported = (answer.get('retcode') == 1400
                       and answer.get('message') == 'unsupported_message_segment')
        known = type(answer.get('retcode')) is int and answer['retcode'] in {1400,1403,1501}
        codes = {'invalid_request', 'unsupported_message_segment', 'send_requires_review',
                 'no_active_private_conversation', 'recovery_capacity', 'destination_not_allowed',
                 'outcome_unknown', 'state_unavailable', 'action_not_supported'}
        detail = {'code': answer['message']} if answer.get('message') in codes else {}
        emit({'kind': 'report', 'stage': 'action_unsupported' if unsupported else 'action_failed' if known else 'action_rejected',
              'retcode': answer.get('retcode'), **detail})


async def run(config, *, endpoint=None, clock=time.time, state_factory=None,
              backend_factory=None, runner_factory=None, state_path=None,
              monotonic_clock=time.monotonic):
    from wechat_bridge.adapters.wechat.assembly import create_host
    from wechat_bridge.durable import Spool
    from wechat_bridge.core.health import HealthState
    from wechat_bridge.adapters.wechat.proxy import WeChatIPCBackend as Gateway
    from wechat_bridge.transports.config import Endpoint
    # OneBot action decoding has its own bounded budget. Native pipe frames
    # remain 256KiB; a base64 action must not close WS before validation.
    path = (Path(state_path) if state_path is not None else
            Path('/var/lib/astrbot-wechat-bridge').resolve(strict=True) / 'private-forward.sqlite')
    state = (state_factory or Spool)(path)
    shared_health = HealthState(clock=clock)
    Gateway = backend_factory or Gateway
    if config.get('recoveryProtection') is not None:
        protection = wire.recovery_protection(config['recoveryProtection'])
        state.protect_recovery(protection['floorSeq'], protection['epoch'])
    from wechat_bridge.image_input_files import ImageFiles
    image_files = ImageFiles(path.parent/'hd-input') if config.get('imageInput', False) else None
    gateway = Gateway(config['account'], config['peer'], state, config['generation'], emit,
                      image_output=config.get('imageOutput', False), voice_output=config.get('voiceOutput', False), image_input=config.get('imageInput', False), image_files=image_files)
    if 'selfSenderId' in config:
        gateway = Gateway(config['account'], config['peer'], state, config['generation'], emit,
                          group=config['roomId'], prefix=config['groupPrefix'], require_activation=True,
                          wake_mode=config['groupWakeMode'], self_sender_id=config['selfSenderId'], image_input=config.get('imageInput', False),
                          group_context=config.get('groupContext', False), image_output=config.get('imageOutput', False), voice_output=config.get('voiceOutput', False),
                          all_groups=config.get('allGroups', False), image_files=image_files)
    core, runner = create_host(config, state, gateway,
        endpoint or Endpoint('ws://127.0.0.1:6199/ws', config['onebotToken']),
        health=shared_health, clock=clock, monotonic_clock=monotonic_clock, runner_factory=runner_factory)
    runner.on_reply = lambda answer: action_result(answer,getattr(gateway,'last_action_binding',None))
    loop = asyncio.get_running_loop()
    incoming = asyncio.StreamReader(limit=wire.MAX_FRAME)
    pipe, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(incoming), sys.stdin.buffer)
    task = asyncio.create_task(runner.run(maximum_seconds=config['runtimeSeconds']))
    reading = None
    reporting = None
    async def consume():
        while True:
            raw = await incoming.readline()
            if not raw:
                runner.stop.set()
                return
            value = wire.decode(raw)
            if value.get('kind') == 'health':
                shared_health.update(wire.health_frame(value))
                continue
            gateway.feed(value)
            if value == {'kind': 'stop'}:
                runner.stop.set()
                return
    async def report_status():
        while True:
            gateway.input_receiver.check_deadline()
            if image_files is not None:
                image_files.cleanup()
            emit({'kind': 'report', 'stage': 'link', 'connected': runner.ready.is_set(),
                  'failures': runner.failures, 'decode_rejections': runner.decode_rejections,
                  'counts': state.counts()})
            await asyncio.sleep(5)
    try:
        reporting = asyncio.create_task(report_status())
        ready_wait = asyncio.create_task(runner.ready.wait())
        try:
            done, _ = await asyncio.wait([task, ready_wait], return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                task.result()
                return 1
        finally:
            ready_wait.cancel()
            await asyncio.gather(ready_wait, return_exceptions=True)
        saved = state.cursor(config['generation'])
        if saved is None and config.get('legacyGeneration'):
            saved = state.cursor(config['legacyGeneration'])
        checkpoint = json.loads(saved) if saved and saved.startswith('{') else {'cursor': saved, 'anchor': None}
        ready = {'kind': 'host_ready', 'connected': True, 'after': checkpoint['cursor'], 'counts': state.counts()}
        if 'selfSenderId' in config:
            ready['anchor'] = checkpoint['anchor']
        emit(ready)
        reading = asyncio.create_task(consume())
        done, _ = await asyncio.wait([task, reading], return_when=asyncio.FIRST_COMPLETED)
        for item in done:
            item.result()
        return 0
    finally:
        runner.stop.set()
        for item in (task, reading, reporting):
            if item:
                item.cancel()
        await asyncio.gather(*[item for item in (task, reading, reporting) if item], return_exceptions=True)
        state.close()
        pipe.close()


def main():
    try:
        config = wire.decode(sys.stdin.buffer.readline(4097))
        expected_compatibility = config.pop('compatibility', None)
        spec = importlib.util.spec_from_file_location('_forward_manifest', Path(__file__).with_name('runtime_manifest.py'))
        manifest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(manifest)
        code_digest = manifest.role_digest(ROOT, 'host')
        wire.verify_compatibility(expected_compatibility, code_digest)
        protection = config.pop('recoveryProtection', None)
        if protection is not None:
            wire.recovery_protection(protection)
        image_input = config.pop('imageInput', False)
        sticker_input = config.pop('stickerInput', False)
        image_output = config.pop('imageOutput', False)
        voice_output = config.pop('voiceOutput', False)
        group_context = config.pop('groupContext', False)
        all_groups = config.pop('allGroups', False)
        private_only = config.pop('privateOnly', False)
        if type(private_only) is not bool or private_only and all_groups:
            raise wire.WireError('private_only_policy')
        if type(all_groups) is not bool:
            raise wire.WireError('all_groups_policy')
        if type(group_context) is not bool:
            raise wire.WireError('group_context_policy')
        if type(image_input) is not bool or type(sticker_input) is not bool or type(image_output) is not bool or type(voice_output) is not bool:
            raise wire.WireError('image_policy')
        base = {'account', 'peer', 'generation', 'runtimeSeconds', 'onebotToken'}
        routes = {'roomId', 'groupName', 'privateName', 'groupPrefix', 'sendInterval', 'segmentBytes', 'legacyGeneration', 'selfSenderId'}
        if set(config) == base | routes:
            config['groupWakeMode'] = 'prefix'
        routes.add('groupWakeMode')
        if (set(config) not in (base, base | routes)
                or private_only and not routes <= set(config)
                or config['account'] != 'wechat-primary' or not isinstance(config['peer'], str)
                or config['peer'].endswith('@chatroom') or type(config['runtimeSeconds']) is not int
                or not 60 <= config['runtimeSeconds'] <= 604800):
            raise wire.WireError('config')
        if routes <= set(config):
            from wechat_bridge.loop_guard import sender_id
            if not sender_id(config['selfSenderId']) or config['selfSenderId'] == config['peer']:
                raise wire.WireError('self_identity')
            from wechat_bridge.forward_config import validate
            validate({'schemaVersion': 1, 'privateContact': config['privateName'], 'groupName': config['groupName'],
                      'groupPrefix': config['groupPrefix'], 'runtimeSeconds': max(600, min(config['runtimeSeconds'], 604200)),
                      'sendIntervalSeconds': config['sendInterval'], 'segmentBytes': config['segmentBytes'],
                      'groupWakeMode': config['groupWakeMode'], 'groupContextEnabled': group_context,
                      'allGroupsEnabled': all_groups, 'privateOnly': private_only})
            from wechat_bridge.group_trigger import group_id
            if not (config['roomId'] is None if private_only else
                    all_groups and config['roomId'] is None or group_id(config['roomId'])):
                raise wire.WireError('group_binding')
        spec = importlib.util.spec_from_file_location('_forward_host_guard', Path(__file__).with_name('bridge_host_worker.py'))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        checks = guard.isolation()
        good = len(checks) == 7 and all(checks.values())
        emit({'kind': 'isolated', 'ok': good, 'checks': checks, 'compatibility': wire.compatibility(code_digest)})
        if not good:
            return 1
        ready, _, _ = select.select([sys.stdin.buffer], [], [], 5)
        if not ready or sys.stdin.buffer.readline(129) != wire.ready_frame(code_digest):
            return 1
        return asyncio.run(run({**config, 'imageInput': image_input, 'stickerInput': sticker_input, 'groupContext': group_context,
                               'imageOutput': image_output, 'voiceOutput': voice_output, 'allGroups': all_groups, 'privateOnly': private_only,
                               'recoveryProtection': protection}))
    except Exception as error:
        emit({'kind': 'host_error', 'code': exception_code(error, 'host_forward_failed')})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
