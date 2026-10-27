"""Real aiocqhttp WebSocket interoperability with synthetic WeChat messages.

No live config, credentials, SDK state, WeChat endpoint or AstrBot is used.
The mock server binds a newly allocated 127.0.0.1 port, never an existing port.
"""
import asyncio
from collections import deque
from contextlib import ExitStack
import json
import logging
from pathlib import Path
import secrets
import tempfile
import time
import uuid

from .protocol import Bridge, Config, NativeMessage, Receipt
from .state import State
from .transport import Endpoint, Runner


class SimulatedWechat:
    online = True

    def __init__(self):
        self.incoming = asyncio.Queue(maxsize=16)
        self.sent = []
        self.outcomes = deque()

    async def next_message(self):
        return await self.incoming.get()

    async def send_text(self, room, body, key):
        self.sent.append((room, body, key))
        outcome = self.outcomes.popleft() if self.outcomes else 'accepted'
        if isinstance(outcome, Exception):
            raise outcome
        return Receipt('simulation-account', room, str(uuid.uuid4()), outcome, '42', '1001')


async def simulate(note=lambda phase: None):
    from aiocqhttp import CQHttp
    from aiocqhttp.exceptions import ActionFailed
    from hypercorn.config import Config as HyperConfig
    from hypercorn.asyncio.run import worker_serve
    from hypercorn.utils import wrap_app

    token = secrets.token_urlsafe(32)
    bot = CQHttp('wechat_bridge_simulation', access_token=token, api_timeout_sec=3)
    bot.logger.setLevel(logging.CRITICAL)
    server_config = HyperConfig()
    server_config.bind = ['127.0.0.1:0']
    server_config.accesslog = None
    server_config.errorlog = None
    server_config.graceful_timeout = 1
    sockets = server_config.create_sockets()
    port = sockets.insecure_sockets[0].getsockname()[1]
    shutdown = asyncio.Event()
    server = asyncio.create_task(worker_serve(wrap_app(bot.server_app, server_config.wsgi_max_body_size, 'asgi'),
        server_config, sockets=sockets, shutdown_trigger=shutdown.wait))
    note('server_started')
    events, results = [], []
    received = asyncio.Queue(maxsize=8)
    checks = {}

    @bot.on_message('group')
    async def group(event):
        try:
            events.append(dict(event))
            login = await bot.call_action('get_login_info', self_id=event.self_id)
            status = await bot.call_action('get_status', self_id=event.self_id)
            member = await bot.call_action('get_group_member_info', self_id=event.self_id,
                                           group_id=event.group_id, user_id=event.user_id)
            assert login['user_id'] == event.self_id and status['online'] and member['user_id'] == event.user_id
            # AstrBot resolves the synthetic bot mention via these two APIs.
            bot_member = await bot.call_action('get_group_member_info', self_id=event.self_id,
                                               group_id=event.group_id, user_id=event.self_id, no_cache=False)
            bot_info = await bot.call_action('get_stranger_info', self_id=event.self_id, user_id=event.self_id, no_cache=False)
            assert bot_member['user_id'] == bot_info['user_id'] == event.self_id
            try:
                response = await bot.call_action('send_group_msg', self_id=event.self_id, group_id=event.group_id,
                    message=[{'type': 'reply', 'data': {'id': str(event.message_id)}},
                             {'type': 'text', 'data': {'text': '模拟回复：桥接正常。'}}])
                results.append(('ok', response))
            except ActionFailed as error:
                results.append(('failed', error.retcode))
            await received.put(True)
        except Exception:
            await received.put(False)

    task = runner = state = None
    try:
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='aw-onebot-sim-'))
            state_path = Path(directory) / 'state.sqlite'
            state = State(state_path)
            stack.callback(lambda: state.close())
            gateway = SimulatedWechat()
            config = Config('simulation-account', 'synthetic-self', 'synthetic-room@chatroom', '模拟测试群', send_interval=0)
            core = Bridge(config, state, gateway)
            runner = Runner(core, Endpoint('ws://127.0.0.1:' + str(port) + '/ws', token))
            task = asyncio.create_task(runner.run(maximum_seconds=15))
            await asyncio.wait_for(runner.ready.wait(), timeout=4)
            note('websocket_ready')
            await asyncio.sleep(.1)
            now = int(time.time())
            def message(identity, **changes):
                values = dict(account='simulation-account', id=identity, room_id='synthetic-room@chatroom',
                              sender_id='synthetic-user', body='/ai 模拟问题', timestamp=now)
                return NativeMessage(**{**values, **changes})
            for item in (message('other', room_id='other@chatroom'), message('ordinary', body='普通聊天'),
                         message('own', sender_id='synthetic-self'), message('history', origin='history'),
                         message('outgoing', direction='outgoing'), message('first'), message('first')):
                await gateway.incoming.put(item)
            assert await asyncio.wait_for(received.get(), timeout=5)
            note('first_reply_received')
            checks['authenticated_universal_ws'] = runner.connections == 1
            checks['trigger_and_whitelist'] = len(events) == 1
            checks['text_array_with_bot_mention'] = events[0]['message'][0]['type'] == 'at'
            checks['accepted_reply_same_group'] = gateway.sent[0][:2] == ('synthetic-room@chatroom', '模拟回复：桥接正常。')
            checks['numeric_receipt'] = results[0][0] == 'ok' and type(results[0][1]['message_id']) is int
            checks['private_ids_not_on_wire'] = all(raw not in json.dumps(events) for raw in (
                'synthetic-room@chatroom', 'synthetic-user', 'synthetic-self'))
            first_self, first_group, first_user = core.self_id, core.group_id, events[0]['user_id']
            gateway.outcomes.append('needs_review')
            await gateway.incoming.put(message('second'))
            assert await asyncio.wait_for(received.get(), timeout=5)
            note('failed_reply_received')
            checks['unknown_is_failure'] = results[-1] == ('failed', 1408)
            checks['no_native_retry'] = len(gateway.sent) == 2 and gateway.sent[0][2] != gateway.sent[1][2]
            old_connections = runner.connections
            await runner.socket.close(code=1001)
            for _ in range(80):
                if runner.connections > old_connections and runner.ready.is_set():
                    break
                await asyncio.sleep(.025)
            checks['websocket_reconnect'] = runner.connections > old_connections
            note('reconnected')
            await gateway.incoming.put(message('first', timestamp=int(time.time())))
            await asyncio.sleep(.15)
            checks['reconnect_does_not_replay'] = len(events) == 2 and len(gateway.sent) == 2
            runner.stop.set()
            await asyncio.wait_for(task, timeout=3)
            note('runner_stopped')
            state.close()
            state = State(state_path)
            restored = Bridge(config, state, gateway)
            checks['identities_survive_restart'] = (restored.self_id == first_self and restored.group_id == first_group
                and state.identity('user', 'synthetic-user')[0] == first_user)
            bad = Runner(restored, Endpoint('ws://127.0.0.1:' + str(port) + '/ws', secrets.token_urlsafe(32)))
            await bad.run(maximum_seconds=.7)
            note('bad_token_checked')
            checks['bad_token_rejected'] = bad.connections == 0 and bad.failures > 0
            checks['ok'] = all(checks.values())
    finally:
        note('cleanup_started')
        if runner is not None:
            runner.stop.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if state is not None:
            state.close()
        shutdown.set()
        try:
            await asyncio.wait_for(server, timeout=3)
        except TimeoutError:
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)
        for sock in sockets.insecure_sockets:
            sock.close()
        note('cleanup_finished')
    return checks


def main():
    try:
        checks = asyncio.run(simulate())
    except Exception:
        checks = {'ok': False, 'code': 'simulation_failed'}
    print(json.dumps({'stage': 'onebot_simulation', **checks}, sort_keys=True))
    return 0 if checks['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
