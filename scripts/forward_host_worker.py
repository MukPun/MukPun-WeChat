#!/usr/bin/env python3
"""Private-only OneBot worker: no Native token, lifecycle or non-loopback URL."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import select
import sys

ROOT = Path('/opt/bridge')
sys.path[:0] = [str(ROOT), str(ROOT / 'onebot-env/lib/python3.12/site-packages')]
from wechat_bridge import forward_wire as wire


def emit(value):
    sys.stdout.buffer.write(wire.encode(value))
    sys.stdout.buffer.flush()


def action_result(answer):
    if answer.get('retcode') != 0:
        # This error is raised by validation, before reserve/send_text. It is
        # a known unsupported output, never an unknown WeChat submission.
        unsupported = (answer.get('retcode') == 1400
                       and answer.get('message') == 'unsupported_message_segment')
        emit({'kind': 'report', 'stage': 'action_unsupported' if unsupported else 'action_rejected',
              'retcode': answer.get('retcode')})


async def run(config):
    from wechat_bridge.continuous import PrivateBridge, TextBridge
    from wechat_bridge.durable import Spool
    from wechat_bridge.forward_gateway import Gateway
    from wechat_bridge.protocol import Config
    from wechat_bridge import transport
    # OneBot action decoding has its own bounded budget. Native pipe frames
    # remain 256KiB; a base64 action must not close WS before validation.
    path = Path('/var/lib/astrbot-wechat-bridge').resolve(strict=True) / 'private-forward.sqlite'
    state = Spool(path)
    gateway = Gateway(config['account'], config['peer'], state, config['generation'], emit,
                      image_output=config.get('imageOutput', False))
    if config.get('roomId'):
        gateway = Gateway(config['account'], config['peer'], state, config['generation'], emit,
                          group=config['roomId'], prefix=config['groupPrefix'], require_activation=True,
                          wake_mode=config['groupWakeMode'], self_sender_id=config['selfSenderId'], image_input=config.get('imageInput', False),
                          group_context=config.get('groupContext', False), image_output=config.get('imageOutput', False))
        core = TextBridge(Config(config['account'], 'account:' + config['account'], config['roomId'],
                                config['groupName'], send_interval=config['sendInterval']),
                          config['peer'], state, gateway, prefix=config['groupPrefix'],
                          segment_bytes=config['segmentBytes'], private_name=config['privateName'], wake_mode=config['groupWakeMode'],
                          self_sender_id=config['selfSenderId'], image_input=config.get('imageInput', False),
                          group_context=config.get('groupContext', False), image_output=config.get('imageOutput', False))
    else:
        core = PrivateBridge(Config(config['account'], 'account:' + config['account'], None, '测试联系人', send_interval=5),
                             config['peer'], state, gateway, image_output=config.get('imageOutput', False))
    runner = transport.Runner(core, transport.Endpoint('ws://127.0.0.1:6199/ws', config['onebotToken']), on_reply=action_result)
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
            gateway.feed(value)
            if value == {'kind': 'stop'}:
                runner.stop.set()
                return
    async def report_status():
        while True:
            emit({'kind': 'report', 'stage': 'link', 'connected': runner.ready.is_set(),
                  'failures': runner.failures, 'decode_rejections': runner.decode_rejections,
                  'counts': state.counts()})
            await asyncio.sleep(5)
    try:
        await asyncio.wait_for(runner.ready.wait(), 12)
        saved = state.cursor(config['generation'])
        if saved is None and config.get('legacyGeneration'):
            saved = state.cursor(config['legacyGeneration'])
        checkpoint = json.loads(saved) if saved and saved.startswith('{') else {'cursor': saved, 'anchor': None}
        ready = {'kind': 'host_ready', 'connected': True, 'after': checkpoint['cursor'], 'counts': state.counts()}
        if config.get('roomId'):
            ready['anchor'] = checkpoint['anchor']
        emit(ready)
        reading = asyncio.create_task(consume())
        reporting = asyncio.create_task(report_status())
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
        image_input = config.pop('imageInput', False)
        image_output = config.pop('imageOutput', False)
        group_context = config.pop('groupContext', False)
        if type(group_context) is not bool:
            raise wire.WireError('group_context_policy')
        if type(image_input) is not bool or type(image_output) is not bool:
            raise wire.WireError('image_policy')
        base = {'account', 'peer', 'generation', 'runtimeSeconds', 'onebotToken'}
        routes = {'roomId', 'groupName', 'privateName', 'groupPrefix', 'sendInterval', 'segmentBytes', 'legacyGeneration', 'selfSenderId'}
        if set(config) == base | routes:
            config['groupWakeMode'] = 'prefix'
        routes.add('groupWakeMode')
        if (set(config) not in (base, base | routes)
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
                      'groupWakeMode': config['groupWakeMode'], 'groupContextEnabled': group_context})
            if not isinstance(config['roomId'], str) or config['roomId'] == '@chatroom' or not config['roomId'].endswith('@chatroom'):
                raise wire.WireError('group_binding')
        spec = importlib.util.spec_from_file_location('_forward_host_guard', Path(__file__).with_name('bridge_host_worker.py'))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        checks = guard.isolation()
        good = len(checks) == 7 and all(checks.values())
        emit({'kind': 'isolated', 'ok': good, 'checks': checks})
        if not good:
            return 1
        ready, _, _ = select.select([sys.stdin.buffer], [], [], 5)
        if not ready or sys.stdin.buffer.readline(32) != b'AWG-READY\n':
            return 1
        return asyncio.run(run({**config, 'imageInput': image_input, 'groupContext': group_context,
                               'imageOutput': image_output}))
    except Exception:
        emit({'kind': 'host_error', 'code': 'host_forward_failed'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
