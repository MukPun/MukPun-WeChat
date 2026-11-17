"""Synthetic health, service/transport and compatible-code admission boundaries."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wechat_bridge import forward_wire as wire
from wechat_bridge.adapters.wechat.assembly import create_service
from wechat_bridge.protocol import Config
from wechat_bridge.storage.durable import Spool

ROOT = Path(__file__).resolve().parents[1]


def record(now=1000, **changes):
    return dict(kind='health', observed_at=now, native_checked_at=now,
        native_connected=True, native_connection='online', native_probe_code='ok',
        sse=True, astrbot_connected=True, admitted=True, recovery='ready', **changes)


class HealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Spool(Path(self.tmp.name) / 'state.sqlite')
        self.addCleanup(self.state.close)
        self.now = 1000
        from wechat_bridge.core.health import HealthState
        self.health = HealthState(clock=lambda: self.now)
        self.service = create_service(Config('account', 'self', None, 'peer'), 'peer', self.state,
            SimpleNamespace(online=True), health=self.health, clock=lambda: self.now)
        self.service.connected()

    async def status(self):
        answer = await self.service.action({'action': 'get_status', 'echo': ['opaque', 1]})
        self.assertEqual(answer['echo'], ['opaque', 1])
        self.assertEqual(answer['data'], self.service.meta('heartbeat')['status'])
        if self.health.record is not None:
            controller = wire.health_state.evaluate_health(self.health.record, self.now,
                websocket=self.service.messages.connection is not None)
            self.assertEqual(answer['data'], controller)
        return answer['data']

    async def test_unknown_native_is_offline_despite_online_socket_and_backend(self):
        self.assertIs(self.service.online, False)
        self.assertEqual(await self.status(), {'online': False, 'good': False})

    async def test_native_online_and_operational_gates_are_distinct(self):
        value = record()
        self.health.update(value)
        self.assertEqual(await self.status(), {'online': True, 'good': True})
        for key in ('sse', 'astrbot_connected', 'admitted'):
            self.health.update(dict(value, **{key: False}))
            self.assertEqual(await self.status(), {'online': True, 'good': False})
        self.health.update(value)
        self.service.disconnected()
        self.assertIs(self.service.online, True)
        self.assertEqual(await self.status(), {'online': True, 'good': False})
        self.service.connected()
        self.assertEqual(await self.status(), {'online': True, 'good': True})

    async def test_failed_native_and_stale_native_or_controller_are_offline(self):
        self.health.update(dict(record(), native_connected=False, native_connection='unreachable', native_probe_code='probe_failed'))
        self.assertEqual(await self.status(), {'online': False, 'good': False})
        self.health.update(record())
        self.now += 16
        self.assertEqual(await self.status(), {'online': True, 'good': True})
        self.health.update(dict(record(self.now), native_checked_at=1000))
        self.assertEqual(await self.status(), {'online': True, 'good': True})
        self.now = 1075
        self.assertEqual(await self.status(), {'online': True, 'good': True})
        self.now = 1076
        self.health.update(dict(record(self.now), native_checked_at=1000))
        self.assertEqual(await self.status(), {'online': False, 'good': False})

    def test_health_frames_validate_exact_types_shapes_and_safe_nonsecret_fields(self):
        self.assertEqual(wire.health_frame(record()), record())
        for bad in (dict(record(), token='secret'), dict(record(), native_connected=1),
                    dict(record(), native_checked_at=True), dict(record(), native_connection='secret'),
                    dict(record(), native_probe_code='Bearer secret'), dict(record(), recovery='surprise'),
                    dict(record(), observed_at=0)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                wire.health_frame(bad)
        with self.assertRaises(ValueError):
            self.health.update(record(1006))
        self.health.update(record())
        with self.assertRaises(ValueError):
            self.health.update(record(999))

    async def test_service_owns_bounded_request_decode_and_unique_echo_rejection(self):
        request, answer, rejected = await self.service.handle_frame('{"params":{"x":1,"x":2},"echo":[7]}')
        self.assertIsNone(request)
        self.assertTrue(rejected)
        self.assertEqual(answer['retcode'], 1400)
        self.assertEqual(answer['echo'], [7])
        request, answer, rejected = await self.service.handle_frame('{"action":"get_status","echo":3}')
        self.assertFalse(rejected)
        self.assertEqual(request['action'], 'get_status')
        self.assertEqual(answer['echo'], 3)


class HostHealthPipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_host_consume_routes_health_to_protocol_not_gateway(self):
        spec = importlib.util.spec_from_file_location('health_pipe_host', ROOT / 'scripts/forward_host_worker.py')
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        captured, runners = [], []
        class Runner:
            def __init__(self, service, endpoint, **options):
                self.service = service
                self.stop, self.ready = asyncio.Event(), asyncio.Event()
                self.failures = self.decode_rejections = 0
                runners.append(self)
            async def run(self, **options):
                self.service.connected()
                self.ready.set()
                await self.stop.wait()
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, wire.encode(record()) + wire.encode({'kind': 'stop'}))
        finally:
            os.close(write_fd)
        with os.fdopen(read_fd, 'rb') as incoming, tempfile.TemporaryDirectory() as temporary:
            with patch.object(host.sys, 'stdin', SimpleNamespace(buffer=incoming)), patch.object(host, 'emit', captured.append):
                result = await host.run(dict(account='account', peer='peer', generation='synthetic',
                    runtimeSeconds=60, onebotToken='synthetic-token-12345'), clock=lambda: 1000,
                    state_path=Path(temporary) / 'state.sqlite', runner_factory=Runner)
            self.assertEqual(result, 0)
            service = runners[0].service
            self.assertEqual(service.health.record, record())
            self.assertEqual(service.status(), {'online': True, 'good': True})
            self.assertEqual(service.meta('heartbeat')['status'], service.status())
            self.assertTrue(any(frame['kind'] == 'host_ready' for frame in captured))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_only_uses_service_and_injected_event_source(self):
        from wechat_bridge.transports.reverse_ws import Runner
        sent, handled, lifecycle = [], [], []
        stopping = asyncio.Event()
        class Socket:
            async def send(self, value):
                sent.append(json.loads(value))
            def __aiter__(self):
                return self
            async def __anext__(self):
                if handled:
                    await stopping.wait()
                    raise StopAsyncIteration
                handled.append(True)
                return 'opaque frame'
        class Source:
            async def next_message(self):
                await asyncio.Future()
        class Service:
            self_id = 1
            def connected(self): lifecycle.append('connected')
            def disconnected(self): lifecycle.append('disconnected')
            def meta(self, kind): return {'kind': kind}
            async def handle_frame(self, raw):
                self.asserted = raw
                stopping.set()
                return None, {'answer': 9}, True
        service = Service()
        runner = Runner(service, SimpleNamespace(), event_source=Source())
        await runner._session(Socket())
        self.assertEqual(service.asserted, 'opaque frame')
        self.assertIn({'answer': 9}, sent)
        self.assertEqual(lifecycle, ['connected', 'disconnected'])
        self.assertEqual(runner.decode_rejections, 1)
        self.assertFalse(runner.ready.is_set())

    def test_concrete_transport_has_no_protocol_storage_or_sdk_dependencies(self):
        path = ROOT / 'wechat_bridge/transports/reverse_ws.py'
        self.assertTrue(path.is_file())
        text = path.read_text()
        self.assertNotIn('send_group_msg', text, 'reply classification belongs to the protocol service')
        for dependency in ('onebot', 'sqlite', 'durable', 'wechat', 'decode_action', 'DecodeError'):
            self.assertNotIn(dependency, '\n'.join(line for line in text.splitlines() if line.startswith('from ')))


class AssemblyTests(unittest.TestCase):
    def test_host_assembly_injects_store_backend_health_endpoint_and_clocks(self):
        from wechat_bridge.adapters.wechat.assembly import create_host
        from wechat_bridge.core.health import HealthState
        from wechat_bridge.transports.config import Endpoint
        with tempfile.TemporaryDirectory() as temporary:
            state = Spool(Path(temporary) / 'state.sqlite')
            self.addCleanup(state.close)
            backend = SimpleNamespace(online=True)
            health = HealthState(clock=lambda: 1000)
            endpoint = Endpoint('ws://127.0.0.1:6200/ws', 'synthetic-test-token')
            config = dict(account='account', peer='peer', runtimeSeconds=60,
                imageInput=False, imageOutput=False, voiceOutput=False)
            service, runner = create_host(config, state, backend, endpoint,
                health=health, clock=lambda: 1000, monotonic_clock=lambda: 20)
            self.assertIs(service.state, state)
            self.assertIs(service.backend, backend)
            self.assertIs(service.health, health)
            self.assertIs(runner.endpoint, endpoint)
            self.assertIs(runner.event_source, backend)
            self.assertEqual(service.clock(), 1000)
            self.assertEqual(runner.clock(), 20)

    def test_restricted_runner_includes_health_suite(self):
        self.assertIn("'test_transport_health.py'", (ROOT / 'scripts/run_onebot_tests.py').read_text())


class CompatibilityTests(unittest.TestCase):
    def test_changed_host_code_and_protocol_reject_before_ready(self):
        from scripts import runtime_manifest as manifest
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'view'
            for name in manifest.role_files('host'):
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(manifest.source_for(ROOT, 'host', name), destination)
            expected = manifest.role_digest(ROOT, 'host', source=True)
            self.assertEqual(manifest.role_digest(target, 'host'), expected)
            value = wire.compatibility(expected)
            wire.verify_compatibility(value, expected)
            for bad in (None, dict(value, version=0), dict(value, digest='0' * 64), dict(value, extra=True)):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    wire.verify_compatibility(bad, expected)
            (target / 'wechat_bridge/onebot/service.py').write_text('# mixed worker\n')
            with self.assertRaises(ValueError):
                wire.verify_compatibility(value, manifest.role_digest(target, 'host'))

    def test_actual_isolated_host_negotiates_version_digest_and_ready_ack(self):
        from scripts import runtime_manifest as manifest
        with tempfile.TemporaryDirectory() as temporary:
            view = Path(temporary)
            for name in manifest.role_files('host'):
                path = view / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(manifest.source_for(ROOT, 'host', name), path)
            # Only OS isolation inspection and the post-handshake runtime are
            # synthetic. The real copied -I entrypoint validates config/digest.
            (view / 'scripts/bridge_host_worker.py').write_text(
                "def isolation(): return {str(i):True for i in range(7)}\n")
            digest = manifest.role_digest(view, 'host')
            config = dict(account='wechat-primary', peer='peer', generation='synthetic',
                          runtimeSeconds=60, onebotToken='synthetic-token-12345',
                          compatibility=wire.compatibility(digest))
            script = ("import importlib.util,sys; from pathlib import Path; "
                "p=Path(sys.argv[1]); sys.path.insert(0,str(p)); "
                "s=importlib.util.spec_from_file_location('host',p/'scripts/forward_host_worker.py'); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.ROOT=p; "
                "exec('async def synthetic_run(config): return 0',m.__dict__); "
                "m.run=m.synthetic_run; raise SystemExit(m.main())")
            def check(value, ack):
                return subprocess.run([sys.executable, '-I', '-B', '-c', script, str(view)],
                    input=wire.encode(value) + ack, capture_output=True, timeout=15)
            result = check(config, wire.ready_frame(digest))
            self.assertEqual(result.returncode, 0, result.stderr.decode()[-1000:])
            isolated = wire.decode(result.stdout.splitlines()[0])
            self.assertEqual(isolated['compatibility'], config['compatibility'])
            self.assertEqual(check(config, b'AWG-READY\n').returncode, 1)
            for compatibility in (None, dict(config['compatibility'], version=1),
                                  dict(config['compatibility'], digest='0' * 64)):
                result = check(dict(config, compatibility=compatibility), wire.ready_frame(digest))
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(b'"isolated"', result.stdout)
            (view / 'wechat_bridge/onebot/service.py').write_text('# incompatible code\n')
            result = check(config, wire.ready_frame(digest))
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(b'"isolated"', result.stdout)

    def test_new_modules_delivered_and_real_isolated_controller_import_works(self):
        from scripts import runtime_manifest as manifest
        for relative in ('wechat_bridge/core/health.py', 'wechat_bridge/transports/__init__.py',
                         'wechat_bridge/transports/reverse_ws.py'):
            self.assertIn(relative, manifest.PUBLIC_CODE)
            self.assertIn(relative, manifest.role_files('host'))
        script = "import importlib.util,sys; s=importlib.util.spec_from_file_location('controller',sys.argv[1]); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); assert m.wire.health_frame"
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script,
            str(ROOT / 'scripts/run_private_forwarder.py')], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode()[-1000:])


class PrivateOnlyHost(unittest.IsolatedAsyncioTestCase):
    def config(self):
        return dict(account='account', peer='peer', generation='profile', runtimeSeconds=60,
            onebotToken='synthetic-token-12345', roomId=None, groupName='unused',
            privateName='指定联系人', groupPrefix='/ai ', sendInterval=10, segmentBytes=1024,
            groupWakeMode='mention_or_prefix', selfSenderId='real-self', legacyGeneration='old',
            privateOnly=True, allGroups=False)

    async def test_formal_private_assembly_keeps_identity_limits_and_blocks_mapped_groups(self):
        from wechat_bridge.adapters.wechat.assembly import create_host
        from wechat_bridge.adapters.wechat.proxy import WeChatIPCBackend
        from wechat_bridge.core.health import HealthState
        from wechat_bridge.core.contracts import NativeMessage
        from wechat_bridge.transports.config import Endpoint
        with tempfile.TemporaryDirectory() as temporary:
            state = Spool(Path(temporary)/'state.sqlite'); self.addCleanup(state.close)
            group = state.identity('group', 'old@chatroom')[0]
            backend = WeChatIPCBackend('account', 'peer', state, 'profile', lambda frame: None,
                self_sender_id='real-self')
            service, _ = create_host(self.config(), state, backend,
                Endpoint('ws://127.0.0.1:6200/ws', 'synthetic-token-12345'), health=HealthState())
            service.connected()
            self.assertEqual(service.private_name, '指定联系人')
            self.assertEqual(service.config.send_interval, 10)
            self.assertEqual(service.content.segment_bytes, 1024)
            self.assertEqual(service.messages.policy.self_sender_id, 'real-self')
            self.assertFalse(service.groups or service.all_groups)
            self.assertEqual((await service.action({'action':'get_group_list'}))['data'], [])
            for action in ('send_group_msg', 'send_msg'):
                answer = await service.action({'action': action, 'params': {'group_id': group, 'message':'拒绝'}})
                self.assertNotEqual(answer['retcode'], 0)
            for room, sender in (('old@chatroom','member'), ('peer','real-self')):
                with self.assertRaises(ValueError):
                    service.ingest(NativeMessage('account','bad',room,sender,'拒绝',int(time.time())))
            message = NativeMessage('account','private','peer','peer','普通私信',int(time.time()))
            from dataclasses import asdict
            state.admit(message.id, asdict(message))
            event = service.ingest(message)
            self.assertEqual(event['message_type'], 'private')
            self.assertEqual(event['sender']['nickname'], '指定联系人')

    async def test_formal_private_host_keeps_activation_and_anchored_ready(self):
        await self.exercise_host_ready()

    async def test_live_host_skips_old_queue_before_activation(self):
        await self.exercise_host_ready(live=True)

    async def exercise_host_ready(self, live=False):
        from wechat_bridge.adapters.wechat.proxy import WeChatIPCBackend
        spec = importlib.util.spec_from_file_location('private_pipe_host', ROOT/'scripts/forward_host_worker.py')
        host = importlib.util.module_from_spec(spec); spec.loader.exec_module(host)
        captured, backends = [], []
        class Backend(WeChatIPCBackend):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs); self.initial_activation = self.activation.is_set(); backends.append(self)
        class Runner:
            def __init__(self, service, endpoint, **kwargs):
                self.stop, self.ready = asyncio.Event(), asyncio.Event()
                self.failures = self.decode_rejections = 0
            async def run(self, **kwargs):
                self.ready.set(); await self.stop.wait()
        read_fd, write_fd = os.pipe()
        os.write(write_fd, wire.encode({'kind':'stop'})); os.close(write_fd)
        with os.fdopen(read_fd,'rb') as incoming, tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'state.sqlite'
            state = Spool(path)
            state.cursor('profile', '{"cursor":"12","anchor":"'+'a'*64+'"}')
            if live:
                state.admit('old-input', {'body': 'preserved fixture'})
            state.close()
            config = self.config()
            if live:
                config['liveStart'] = {'runId': 'c'*32, 'checkpoint': {'cursor': '20', 'anchor': 'b'*64}}
            with patch.object(host.sys,'stdin',SimpleNamespace(buffer=incoming)), patch.object(host,'emit',captured.append):
                self.assertEqual(await host.run(config,state_path=path,backend_factory=Backend,runner_factory=Runner),0)
            if live:
                state = Spool(path)
                try:
                    self.assertIsNone(state.next_pending())
                    row = state.db.execute("SELECT state,payload FROM inbox WHERE source='old-input'").fetchone()
                    self.assertEqual(row[0], 'expired')
                    self.assertIn('preserved fixture', row[1])
                finally:
                    state.close()
        self.assertFalse(backends[-1].initial_activation)
        self.assertEqual(backends[-1].self_sender_id,'real-self')
        ready = next(frame for frame in captured if frame['kind']=='host_ready')
        self.assertEqual((ready['after'],ready.get('anchor')), ('20','b'*64) if live else ('12','a'*64))
        if live:
            self.assertEqual(ready['skipped'], 1)
