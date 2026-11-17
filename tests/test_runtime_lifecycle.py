"""Synthetic production controller turns: no units, Native sessions or network."""
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


class RuntimeIntegration(unittest.TestCase):
    def runtime(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'wechat_bridge/runtime/controller.py').is_file(),
                        'production runtime controller has not been extracted')
        return importlib.import_module('wechat_bridge.runtime')

    def fixture(self):
        runtime = self.runtime()
        from scripts import run_private_forwarder as core
        from wechat_bridge.storage.durable import Spool
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        state = runtime.State()
        state.pinned = SimpleNamespace(unchanged=lambda: True)
        state.settings = None
        state.recovery_path = path/'bridge.sqlite'
        spool = Spool(state.recovery_path); self.addCleanup(spool.close)
        state.audit_path = path/'send.json'; state.image_audit_path = path/'image.json'
        state.counters = {}
        state.media_lease = SimpleNamespace(close=Mock())
        state.voice_relay = SimpleNamespace(pending=False, check_deadline=Mock())
        state.input_relay = SimpleNamespace(check_deadline=Mock(), transferring=False)
        deps = SimpleNamespace(**{name:getattr(core,name) for name in (
            'require','wire','supervisor','recovery_diagnostics','recovery_coordinator',
            'recovery_checkpoints','input_wire','voice_forwarding','media_egress')})
        deps.audit_cleared = lambda audit: audit.get('cleared') is True
        deps.audit_preserved = lambda pinned, audit: deps.audit_cleared(audit)
        deps.settle_outcome = Mock(side_effect=lambda *args: args[2].update(cleared=True))
        deps.tap = SimpleNamespace(stop_unit=Mock())
        deps.owner = SimpleNamespace(STATE=path)
        deps.voice_forwarding = SimpleNamespace(Relay=Mock(side_effect=lambda *a: SimpleNamespace(pending=False, check_deadline=Mock())),preflight=Mock())
        deps.media_egress = SimpleNamespace(Lease=Mock(return_value=SimpleNamespace(close=Mock())))
        controller = runtime.Controller(deps, state)
        controller.workers.spawn_native = Mock()
        return controller, state, deps, spool

    def peer(self, records=(), *, exited=False):
        return SimpleNamespace(tick=Mock(side_effect=[list(records),[],[],[],[],[],[],[],[]]),
            process=SimpleNamespace(poll=lambda: 1 if exited else None), pending=bytearray(),
            selector=SimpleNamespace(get_map=lambda: {}), close=Mock(), send=Mock())

    def attach(self, controller, name, capture):
        controller.workers.peers[name] = capture
        controller.workers.units[name] = name+'-synthetic.service'
        controller.workers.started.append(name+'-synthetic.service')

    def test_transient_error_frame_enters_real_restart_path_once(self):
        c,s,d,db = self.fixture()
        self.attach(c,'host',self.peer([{'kind':'host_error','code':'worker_transport_failed'}],exited=True))
        self.attach(c,'native',self.peer())
        self.assertTrue(c.poll())
        c.workers.spawn_native.assert_called_once()
        self.assertEqual(c.workers.supervisors['host'].state()['crashes'],1)
        self.assertEqual(c.workers.supervisors['host'].state()['restarts'],1)
        self.assertEqual(c.workers.supervisors['native'].state()['crashes'],0)
        self.assertFalse(s.armed)

    def test_stop_failure_never_spawns(self):
        c,s,d,db = self.fixture()
        self.attach(c,'native',self.peer(exited=True))
        d.tap.stop_unit.side_effect=RuntimeError('synthetic stop failed')
        with self.assertRaises(Exception): c.poll()
        c.workers.spawn_native.assert_not_called()
        self.assertIn('native',c.workers.units)

    def test_audit_failure_never_spawns_or_drops_pending(self):
        c,s,d,db = self.fixture()
        self.attach(c,'native',self.peer(exited=True))
        s.request={'kind':'send'}; s.audit={'state':'submitted'}
        d.settle_outcome.side_effect=OSError('synthetic disk failure')
        with self.assertRaises(Exception): c.poll()
        c.workers.spawn_native.assert_not_called()
        self.assertIsNotNone(s.request)

    def test_unknown_audit_remains_blocked_even_after_persist(self):
        c,s,d,db = self.fixture()
        self.attach(c,'native',self.peer(exited=True))
        s.request={'kind':'image_begin'}; s.image_audit={'state':'submitted'}
        d.settle_outcome.side_effect=None
        with self.assertRaises(Exception): c.poll()
        self.assertEqual(s.image_audit['state'],'unknown')
        c.workers.spawn_native.assert_not_called()

    def test_unknown_raw_error_is_not_sanitized_into_transient(self):
        c,s,d,db = self.fixture()
        self.attach(c,'host',self.peer([{'kind':'host_error','code':'private identity mismatch'}],exited=True))
        with self.assertRaises(Exception): c.poll()
        c.workers.spawn_native.assert_not_called()

    def test_protective_final_frame_wins_over_transport(self):
        c,s,d,db = self.fixture()
        p=self.peer([{'kind':'native_error','code':'worker_transport_failed'}],exited=True)
        p.tick.side_effect=[[{'kind':'native_error','code':'worker_transport_failed'}],
                            [{'kind':'native_error','code':'recovery_anchor_mismatch'}],[],[]]
        self.attach(c,'native',p)
        with self.assertRaises(Exception): c.poll()
        c.workers.spawn_native.assert_not_called()
        self.assertEqual(c.workers.supervisors['native'].state()['crashes'],1)

    def test_dead_host_is_processed_before_readiness_timeout(self):
        c,s,d,db=self.fixture()
        s.host_wait={'ready':False,'last':0}
        self.attach(c,'host',self.peer(exited=True))
        self.assertTrue(c.poll())
        c.workers.spawn_native.assert_called_once()

    def test_fresh_relays_and_request_permissions_after_restart(self):
        c,s,d,db=self.fixture()
        old_voice=s.voice_relay; old_input=s.input_relay
        s.last_input={'old-room':1}; s.image_transfer=object(); s.image_ack=3
        s.armed=s.sse_connected=s.astrbot_connected=True
        self.attach(c,'native',self.peer(exited=True))
        self.assertTrue(c.poll())
        self.assertIsNot(s.voice_relay,old_voice); self.assertIsNot(s.input_relay,old_input)
        self.assertEqual(s.last_input,{})
        self.assertIsNone(s.image_transfer); self.assertIsNone(s.image_ack)
        self.assertFalse(s.sse_connected or s.astrbot_connected or s.armed)
        d.voice_forwarding.Relay.assert_called_once()

    def test_third_crash_in_ten_seconds_stops_before_spawn(self):
        c,s,d,db=self.fixture()
        for _ in range(2):
            self.attach(c,'native',self.peer(exited=True)); self.assertTrue(c.poll())
        self.attach(c,'native',self.peer(exited=True))
        with self.assertRaises(Exception): c.poll()
        self.assertEqual(c.workers.spawn_native.call_count,2)

    def test_voice_preflight_is_not_bypassed(self):
        c,s,d,db=self.fixture()
        s.voice_relay=SimpleNamespace(pending=True,quarantine=Mock())
        d.voice_forwarding.Relay.side_effect=RuntimeError('previous_voice_send_requires_review')
        self.attach(c,'host',self.peer(exited=True))
        with self.assertRaises(Exception): c.poll()
        c.workers.spawn_native.assert_not_called()
        s.voice_relay.quarantine.assert_called_once()

    def test_publishing_retirement_commits_before_replacement(self):
        c,s,d,db=self.fixture()
        db.admit('synthetic-input',{'body':'synthetic'})
        db.publishing('synthetic-input')
        def spawning():
            self.assertEqual(db.counts(),{'uncertain':1})
            self.assertIsNone(db.next_pending())
        c.workers.spawn_native.side_effect=spawning
        self.attach(c,'native',self.peer(exited=True))
        c.poll()
        self.assertEqual(db.counts(),{'uncertain':1})

    def test_queue_retirement_failure_never_replaces_workers(self):
        from unittest.mock import patch
        c,s,d,db=self.fixture()
        self.attach(c,'native',self.peer(exited=True))
        with patch.object(d.recovery_checkpoints,'bridge',side_effect=OSError('synthetic write failure')):
            with self.assertRaises(Exception): c.poll()
        c.workers.spawn_native.assert_not_called()

    def handshake_fixture(self):
        c,s,d,db=self.fixture()
        from scripts import run_private_forwarder as core
        for key in ('loops','output_budget','trigger','images','output_images','source_cache_module'):
            setattr(d,key,getattr(core,key))
        d.groups=SimpleNamespace(verify_reader=Mock())
        d.trial=SimpleNamespace(verify_host=Mock())
        d.media_headroom=Mock(return_value=dict(storage_free=128*1024*1024,memory_free=128*1024*1024,
                                              database_bytes=0,file_limit=128*1024*1024))
        s.voice_relay=SimpleNamespace(pending=False,handle=lambda *a,**k:False,check_deadline=Mock())
        s.host_digest='a'*64
        s.settings={'imageInputEnabled':False,'allGroupsEnabled':True}
        s.recovery_seed={}; s.generation='profile:synthetic'
        d.recovery_coordinator=SimpleNamespace(Failure=core.recovery_coordinator.Failure,mark_ready=Mock())
        d.recovery_checkpoints=SimpleNamespace(ForwarderCapture=Mock())
        return c,s,d,db

    def test_handshake_uses_durable_checkpoint_and_waits_for_native_sse(self):
        c,s,d,db=self.handshake_fixture()
        native=self.peer(); native.acknowledge=Mock()
        native.tick.side_effect=[[{'kind':'isolated','ok':True,'checks':{'synthetic':True}},
                                 {'kind':'native_ready','peer':'peer','boundary':'99','selfSenderId':'self-id'}],[],
                                [{'kind':'native_armed','sse':True},
                                 {'kind':'report','stage':'sse_connection','connected':True}]]
        host=self.peer([{'kind':'isolated','ok':True,'checks':{'synthetic':True},'compatibility':d.wire.compatibility(s.host_digest)},
                        {'kind':'host_ready','connected':True,'after':'41','anchor':'b'*64,'counts':{}}])
        c.workers.spawn_host=Mock(side_effect=lambda *a:self.attach(c,'host',host))
        self.attach(c,'native',native)
        c.poll(); self.assertFalse(s.armed); d.recovery_coordinator.mark_ready.assert_not_called()
        c.poll(); self.assertFalse(s.armed); d.recovery_coordinator.mark_ready.assert_not_called()
        self.assertEqual(native.send.call_args.args[0],{'kind':'start','after':'41','anchor':'b'*64})
        self.assertEqual(bytes(host.pending),d.wire.ready_frame(s.host_digest))
        c.poll(); self.assertTrue(s.armed and s.sse_connected and s.astrbot_connected)
        d.recovery_coordinator.mark_ready.assert_called_once()
        host.send.assert_called_with({'kind':'activate'})

    def test_duplicate_host_readiness_is_protective(self):
        c,s,d,db=self.handshake_fixture()
        s.isolated={'host','native'}; s.host_wait['ready']=True
        self.attach(c,'native',self.peer())
        self.attach(c,'host',self.peer([{'kind':'host_ready','connected':True,'after':'41','anchor':'b'*64}]))
        with self.assertRaises(Exception): c.poll()
        c.workers.peers['native'].send.assert_not_called()

    def test_formal_handshake_never_falls_back_to_latest_boundary(self):
        c,s,d,db=self.handshake_fixture()
        s.isolated={'host','native'}; s.boundary='99'
        self.attach(c,'native',self.peer())
        self.attach(c,'host',self.peer([{'kind':'host_ready','connected':True,'after':None,'anchor':None}]))
        with self.assertRaises(Exception): c.poll()
        c.workers.peers['native'].send.assert_not_called()

    def test_exception_classification_is_fixed_and_does_not_hide_permission(self):
        runtime=self.runtime()
        from wechat_bridge.runtime.failures import exception_code, transient
        self.assertEqual(exception_code(ConnectionResetError('private'), 'host_forward_failed'),'worker_transport_failed')
        self.assertFalse(transient(exception_code(PermissionError('private'), 'host_forward_failed')))
        self.assertFalse(transient('native_worker_failed'))

    def test_formal_serve_calls_runtime_and_runner_includes_suite(self):
        root=Path(__file__).resolve().parents[1]
        text=(root/'scripts/run_private_forwarder.py').read_text()
        self.assertIn('controller.poll()',text)
        self.assertNotIn('def restart_session',text)
        self.assertIn('test_runtime_lifecycle.py',(root/'scripts/run_onebot_tests.py').read_text())

    def test_actual_serve_restarts_in_place_without_repeating_admission(self):
        import json
        from contextlib import ExitStack
        from unittest.mock import patch
        from scripts import run_forwarder as formal
        from wechat_bridge.storage.durable import Spool
        core=formal.core
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            spool=Spool(root/'bridge.sqlite')
            self.addCleanup(spool.close)
            real_bridge=core.recovery_checkpoints.bridge
            settings=dict(runtimeSeconds=600,allGroupsEnabled=True,imageInputEnabled=False,
                privateContact='synthetic',groupName='synthetic',groupPrefix='/ai',sendIntervalSeconds=1,
                groupWakeMode='prefix',stickerInputEnabled=False,linkInputEnabled=False,groupContextEnabled=False,
                imageOutputEnabled=False,voiceOutputEnabled=False,segmentBytes=2048)
            pinned=SimpleNamespace(open=Mock(),close=Mock(),unchanged=lambda:True,remaining_seconds=lambda:1000,
                token=lambda:'synthetic-only',netfd=9,host=SimpleNamespace(parent='synthetic',client_pid=2,client_ticks=3))
            captures=[self.peer([{'kind':'native_error','code':'worker_transport_failed'}]),
                      self.peer([{'kind':'native_error','code':'recovery_integrity'}])]
            values=[]
            owner=SimpleNamespace(STATE=root,CONTROL=root,private_directory=lambda p:None,
                read_json=lambda p:json.loads(p.read_text()),write_json=lambda p,v:p.write_text(json.dumps(v)))
            checks=SimpleNamespace(startup=Mock(return_value={'status':'ready'}),
                selected_pair=Mock(return_value=({'selfProof':{}},None)),
                Failure=core.recovery_coordinator.Failure,block=Mock())
            lease=SimpleNamespace(close=Mock())
            probe=SimpleNamespace(start=Mock(),close=Mock()); probe.start.return_value=probe
            bundle=root/'bundle'; bundle.mkdir()
            with ExitStack() as stack:
                def patcher(target,name,value): return stack.enter_context(patch.object(target,name,value))
                patcher(core,'owner',owner); patcher(core,'SETTINGS',settings)
                patcher(core.live_startup,'enabled',Mock(return_value=False))
                patcher(core,'recovery_coordinator',checks)
                image_preflight=patcher(core,'image_send_preflight',Mock(return_value=(root/'image.json',None,None)))
                patcher(core,'status',values.append); patcher(core,'remember_failure',Mock())
                patcher(core,'FILES',()); patcher(core,'Duplex',Mock(side_effect=captures))
                patcher(core.groups,'PinnedNative',Mock(return_value=pinned))
                patcher(core.groups,'reader_properties',Mock(return_value=[]))
                patcher(core.owned,'identity',Mock(return_value='synthetic'))
                patcher(core.tap,'read_network',Mock(return_value={}))
                patcher(core.tap,'business_snapshot',Mock(return_value={}))
                patcher(core.tap,'require_unit_absent',Mock())
                patcher(core.tap,'stop_unit',Mock())
                patcher(core.tap,'compare_baselines',Mock(return_value=({},[])))
                patcher(core.trial,'read_onebot_token',Mock(return_value='synthetic-only'))
                patcher(core.health,'Probe',Mock(return_value=probe))
                patcher(core.media_egress,'Lease',Mock(return_value=lease))
                patcher(core.delivery,'role_digest',Mock(return_value='a'*64))
                patcher(core.tempfile,'mkdtemp',Mock(return_value=str(bundle)))
                original_require=core.require
                patcher(core,'require',lambda ok,stage: None if stage=='linux_root_required' else original_require(ok,stage))
                patcher(core.recovery_checkpoints,'bridge',lambda path:real_bridge(root/'bridge.sqlite'))
                self.assertEqual(core.serve(),0)
                pinned.open.assert_called_once(); image_preflight.assert_called_once()
                checks.startup.assert_called_once(); checks.selected_pair.assert_called_once()
                self.assertEqual(core.Duplex.call_count,2)
            self.assertEqual(values[-1]['code'],'recovery_integrity')
            self.assertFalse(values[-1]['ready'])
            self.assertEqual(core.UNIT,'astrbot-wechat-forwarder.service')
            self.assertEqual(core.ENTRY.name,'run_forwarder.py')

    def test_malformed_error_code_remains_protective_when_exit_follows(self):
        for code in (None,[],{}):
            with self.subTest(code=code):
                c,s,d,db=self.fixture()
                self.attach(c,'native',self.peer([{'kind':'native_error','code':code}],exited=True))
                with self.assertRaises(d.recovery_coordinator.Failure): c.poll()
                c.workers.spawn_native.assert_not_called()

    def test_snapshot_conflict_cannot_be_overwritten_by_empty_replay_barrier(self):
        from unittest.mock import patch
        c,s,d,db=self.fixture()
        d.owner.CONTROL=d.owner.STATE; d.owner.CHECKPOINT_SECONDS=60
        s.armed=True; s.recovery_adapter=object(); s.recovery_refresh=True
        host=self.peer(); self.attach(c,'host',host)
        d.recovery_coordinator=SimpleNamespace(Production=lambda *a,**k:SimpleNamespace(
            reconcile=Mock(side_effect=ValueError('synthetic evidence conflict'))))
        with patch.object(d.recovery_checkpoints,'capture',return_value=object()), \
             patch.object(d.recovery_checkpoints,'replay_barrier',return_value=None):
            c.recovery.capture_if_safe()
        self.assertEqual(s.recovery_state,'requires_review')
        host.send.assert_not_called()

    def test_real_image_assembly_is_disposed_and_never_replayed_after_crash(self):
        import base64,json
        from test_onebot_image_output import png
        from scripts import run_private_forwarder as core
        c,s,d,db=self.handshake_fixture()
        d.owner.write_json=lambda p,v:p.write_text(json.dumps(v))
        d.image_audit_value=core.image_audit_value
        d.output_available=lambda:True; d.reserve_image_upload=lambda *a,**k:True
        s.settings['imageOutputEnabled']=True; s.armed=True
        s.counters={'image_output_requests':0}
        image=d.output_images.from_segment({'type':'image','data':{'file':'base64://'+base64.b64encode(png()).decode()}})
        command={'kind':'image_begin','requestId':'a'*32,'key':'b'*32,'destination':'synthetic@chatroom',
                 'image':d.output_images.metadata(image)}
        native=self.peer(); host=self.peer([command])
        self.attach(c,'native',native); self.attach(c,'host',host)
        c.poll()
        self.assertIsNotNone(s.image_transfer); self.assertEqual(s.image_ack,-1)
        self.assertEqual(json.loads(s.image_audit_path.read_text())['state'],'submitted')
        native.tick.side_effect=[[{'kind':'native_error','code':'worker_transport_failed'}],[]]
        d.recovery_checkpoints=core.recovery_checkpoints
        d.settle_outcome=Mock(side_effect=lambda pinned,p,a,*rest:(a.update(cleared=True),d.owner.write_json(p,a)))
        c.poll()
        self.assertEqual(json.loads(s.image_audit_path.read_text())['state'],'unknown')
        self.assertIsNone(s.request); self.assertIsNone(s.image_transfer); self.assertIsNone(s.image_ack)
        self.assertEqual(native.send.call_count,1)
        c.workers.spawn_native.assert_called_once()

    def test_real_voice_relay_quarantine_retains_preflight_gate(self):
        import json
        from scripts import run_private_forwarder as core
        from test_onebot_voice_output import wav,segment
        c,s,d,db=self.handshake_fixture()
        d.owner.write_json=lambda p,v:p.write_text(json.dumps(v))
        d.owner.read_json=lambda p:json.loads(p.read_text())
        d.voice_forwarding=core.voice_forwarding
        d.output_available=lambda:True; d.reserve_voice_upload=lambda *a:True
        d.recovery_checkpoints=core.recovery_checkpoints
        d.settle_outcome=Mock(side_effect=lambda pinned,p,a,*rest:d.owner.write_json(p,a))
        s.settings['voiceOutputEnabled']=True; s.armed=True
        s.counters=dict(voice_output_requests=0,voice_output_sent=0,uncertain=0,accepted_parts=0)
        s.voice_relay=d.voice_forwarding.Relay(d,s.pinned,s.settings,s.counters)
        audio=core.output_voice.from_segment(segment(wav()))
        command={'kind':'voice_begin','requestId':'a'*32,'key':'b'*32,'destination':'synthetic@chatroom',
                 'voice':core.output_voice.metadata(audio)}
        native=self.peer(); self.attach(c,'native',native); self.attach(c,'host',self.peer([command]))
        c.poll(); old=s.voice_relay
        self.assertTrue(old.pending); self.assertIsNotNone(old.assembly); self.assertEqual(old.ack,-1)
        native.tick.side_effect=[[{'kind':'native_error','code':'worker_transport_failed'}],[]]
        with self.assertRaisesRegex(Exception,'previous_voice_send_requires_review'): c.poll()
        self.assertEqual(json.loads(old.path.read_text())['state'],'unknown')
        self.assertIs(s.voice_relay,old)
        c.workers.spawn_native.assert_not_called()
        self.assertEqual(native.send.call_count,1)

    def test_media_lease_cleanup_failure_blocks_replacement(self):
        c,s,d,db=self.fixture()
        self.attach(c,'native',self.peer(exited=True))
        s.media_lease.close.side_effect=ValueError('media_egress_conflict')
        with self.assertRaisesRegex(ValueError,'media_egress_conflict'): c.poll()
        c.workers.spawn_native.assert_not_called()

    def test_snapshot_safe_point_excludes_every_inflight_media_and_request(self):
        c,s,d,db=self.fixture()
        s.armed=True; s.recovery_adapter=object()
        d.recovery_checkpoints=SimpleNamespace(capture=Mock())
        for field,value in [('request',{}),('image_transfer',object()),
                            ('voice_relay',SimpleNamespace(pending=True)),
                            ('input_relay',SimpleNamespace(transferring=True))]:
            with self.subTest(field=field):
                old=getattr(s,field); setattr(s,field,value)
                c.recovery.capture_if_safe()
                setattr(s,field,old)
        d.recovery_checkpoints.capture.assert_not_called()

    def test_real_child_final_error_and_exit_count_one_generation(self):
        import sys
        from scripts import run_private_forwarder as core
        c,s,d,db=self.fixture()
        code='import sys; sys.stdout.write(\'{"kind":"native_error","code":"worker_transport_failed"}\\n\'); sys.stdout.flush()'
        child=core.Duplex([sys.executable,'-I','-B','-c',code],{})
        self.addCleanup(lambda: child.close() if child.selector.get_map() is not None else None)
        child.process.wait(timeout=5)
        self.attach(c,'native',child)
        c.poll()
        self.assertEqual(c.workers.supervisors['native'].state()['crashes'],1)
        c.workers.spawn_native.assert_called_once()

    def test_rehandshake_does_not_repeat_durable_ready_transition(self):
        from wechat_bridge.runtime.session import handshake
        c,s,d,db=self.handshake_fixture()
        native=self.peer(); host=self.peer()
        self.attach(c,'native',native); self.attach(c,'host',host)
        s.host_wait['ready']=True; s.astrbot_connected=True
        for _ in range(2):
            s.armed=False; s.activated=False; s.sse_connected=True
            handshake(d,s,'native',native,{'kind':'native_armed','sse':True})
        d.recovery_coordinator.mark_ready.assert_called_once()
        self.assertEqual(host.send.call_count,2)

    def test_native_armed_does_not_prove_sse_connected(self):
        from wechat_bridge.runtime.session import handshake
        c,s,d,db=self.handshake_fixture()
        native=self.peer(); self.attach(c,'native',native); self.attach(c,'host',self.peer())
        s.host_wait['ready']=True; s.astrbot_connected=True
        handshake(d,s,'native',native,{'kind':'native_armed','sse':True})
        self.assertFalse(s.sse_connected)
        d.recovery_coordinator.mark_ready.assert_not_called()
        c.workers.peers['host'].send.assert_not_called()

    def test_actual_sse_report_activates_once_and_commits_ready(self):
        c,s,d,db=self.handshake_fixture()
        s.host_wait['ready']=True; s.astrbot_connected=True; s.armed=True
        host=self.peer(); self.attach(c,'host',host)
        native=self.peer([{'kind':'report','stage':'sse_connection','connected':True}])
        self.attach(c,'native',native)
        c.poll()
        host.send.assert_called_once_with({'kind':'activate'})
        d.recovery_coordinator.mark_ready.assert_called_once()
        native.tick.side_effect=[[{'kind':'report','stage':'sse_connection','connected':False}],
                                 [{'kind':'report','stage':'sse_connection','connected':True}]]
        c.poll(); self.assertFalse(s.sse_connected)
        c.poll(); self.assertTrue(s.sse_connected)
        host.send.assert_called_once_with({'kind':'activate'})

    def test_sse_connect_while_websocket_down_waits_for_link(self):
        from wechat_bridge.runtime.reports import report
        c,s,d,db=self.handshake_fixture()
        s.host_wait['ready']=True; s.armed=True; s.astrbot_connected=False
        host=self.peer(); self.attach(c,'host',host)
        report(d,s,'native',{'kind':'report','stage':'sse_connection','connected':True})
        host.send.assert_not_called()
        report(d,s,'host',{'kind':'report','stage':'link','connected':True,'counts':{},'failures':0,'decode_rejections':0})
        host.send.assert_called_once_with({'kind':'activate'})

    def test_sse_connection_before_armed_and_ws_reconnect_preserves_handshake(self):
        from wechat_bridge.runtime.session import handshake
        from wechat_bridge.runtime.reports import report
        c,s,d,db=self.handshake_fixture()
        native=self.peer(); host=self.peer()
        self.attach(c,'native',native); self.attach(c,'host',host)
        s.host_wait['ready']=True; s.astrbot_connected=False
        report(d,s,'native',{'kind':'report','stage':'sse_connection','connected':True})
        self.assertFalse(s.armed)
        host.send.assert_not_called()
        handshake(d,s,'native',native,{'kind':'native_armed','sse':True})
        self.assertTrue(s.armed)
        host.send.assert_not_called()
        d.recovery_coordinator.mark_ready.assert_not_called()
        report(d,s,'host',{'kind':'report','stage':'link','connected':True,'counts':{},'failures':0,'decode_rejections':0})
        host.send.assert_called_once_with({'kind':'activate'})
        d.recovery_coordinator.mark_ready.assert_called_once()
        native.send.assert_not_called()

    def test_activation_gate_refuses_retained_review(self):
        # A retained review is an admission stop for delivery, not only health.
        from wechat_bridge.runtime.session import activate_if_ready
        c,s,d,db=self.handshake_fixture()
        host=self.peer(); self.attach(c,'host',host)
        s.armed=s.sse_connected=s.astrbot_connected=True; s.host_wait['ready']=True
        s.recovery_state='pending'
        activate_if_ready(d,s)
        host.send.assert_called_once_with({'kind':'activate'})
        self.assertTrue(s.activated and s.good)
        # The next generation of the same controller must not reopen delivery.
        s.activated=False; s.good=False; s.code='starting'
        s.recovery_state='requires_review'
        activate_if_ready(d,s)
        self.assertEqual(host.send.call_count,1)
        self.assertFalse(s.activated or s.good)
        self.assertEqual(s.code,'starting')
        d.recovery_coordinator.mark_ready.assert_called_once()

    def test_retained_review_stops_generation_before_queued_delivery(self):
        import time
        from scripts import run_private_forwarder as core
        c,s,d,db=self.handshake_fixture()
        # Retirement opens the real durable bridge; a fake namespace would hide
        # whether pending publishing evidence is actually quarantined.
        d.recovery_checkpoints=core.recovery_checkpoints
        db.admit('queued-input',{'body':'synthetic','timestamp':int(time.time())})
        s.recovery_state='requires_review'
        native=self.peer(); host=self.peer()
        self.attach(c,'native',native); self.attach(c,'host',host)
        with self.assertRaises(core.recovery_coordinator.Failure) as caught:
            c.poll()
        self.assertEqual(caught.exception.stage,'requires_review')
        host.send.assert_not_called(); native.send.assert_not_called()
        # The queue stays admitted but undelivered: no drop, no replay.
        self.assertEqual(db.next_pending()[0],'queued-input')
        self.assertEqual(db.counts(),{'queued':1})
        d.tap.stop_unit.assert_any_call('host-synthetic.service',True)
        c.workers.spawn_native.assert_not_called()
        self.assertFalse(s.armed or s.good)

    def test_current_generation_review_stops_delivery_and_never_respawns(self):
        from unittest.mock import patch
        from scripts import run_private_forwarder as core
        c,s,d,db=self.handshake_fixture()
        native=self.peer(); host=self.peer()
        self.attach(c,'native',native); self.attach(c,'host',host)
        d.owner.CONTROL=d.owner.STATE; d.owner.CHECKPOINT_SECONDS=60
        s.armed=s.sse_connected=s.astrbot_connected=True; s.host_wait['ready']=True
        s.recovery_adapter=object(); s.recovery_refresh=True; s.activated=True
        d.recovery_checkpoints=core.recovery_checkpoints
        d.recovery_coordinator=SimpleNamespace(Failure=core.recovery_coordinator.Failure,
            Production=lambda *a,**k:SimpleNamespace(reconcile=Mock(side_effect=ValueError('synthetic evidence conflict'))))
        with patch.object(d.recovery_checkpoints,'capture',return_value=object()):
            c.recovery.capture_if_safe()
        self.assertEqual(s.recovery_state,'requires_review')
        host.send.assert_not_called()
        with self.assertRaises(d.recovery_coordinator.Failure) as caught:
            c.poll()
        self.assertEqual(caught.exception.stage,'requires_review')
        host.send.assert_not_called()
        c.workers.spawn_native.assert_not_called()
        d.tap.stop_unit.assert_any_call('host-synthetic.service',True)

    def test_formal_serve_blocks_coordinator_and_exits_on_review(self):
        import json
        from contextlib import ExitStack
        from unittest.mock import patch
        from scripts import run_forwarder as formal
        from wechat_bridge.storage.durable import Spool
        core=formal.core
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            spool=Spool(root/'bridge.sqlite'); self.addCleanup(spool.close)
            real_bridge=core.recovery_checkpoints.bridge
            settings=dict(runtimeSeconds=600,allGroupsEnabled=True,imageInputEnabled=False,
                privateContact='synthetic',groupName='synthetic',groupPrefix='/ai',sendIntervalSeconds=1,
                groupWakeMode='prefix',stickerInputEnabled=False,linkInputEnabled=False,groupContextEnabled=False,
                imageOutputEnabled=False,voiceOutputEnabled=False,segmentBytes=2048)
            pinned=SimpleNamespace(open=Mock(),close=Mock(),unchanged=lambda:True,remaining_seconds=lambda:1000,
                token=lambda:'synthetic-only',netfd=9,host=SimpleNamespace(parent='synthetic',client_pid=2,client_ticks=3))
            values=[]
            owner=SimpleNamespace(STATE=root,CONTROL=root,private_directory=lambda p:None,
                read_json=lambda p:json.loads(p.read_text()),write_json=lambda p,v:p.write_text(json.dumps(v)))
            checks=SimpleNamespace(startup=Mock(return_value={'status':'ready'}),
                selected_pair=Mock(return_value=({'selfProof':{}},None)),
                Failure=core.recovery_coordinator.Failure,block=Mock())
            lease=SimpleNamespace(close=Mock())
            probe=SimpleNamespace(start=Mock(),close=Mock()); probe.start.return_value=probe
            bundle=root/'bundle'; bundle.mkdir()
            # serve() resets the transient state to 'pending', so the verdict
            # has to arrive mid-generation. It lands exactly where a failed
            # safe-point proof is discovered, and the real retirement path still
            # runs against the real durable bridge.
            class ReviewedController(core.runtime.Controller):
                def __init__(self,module,state):
                    super().__init__(module,state)
                    real=self.recovery
                    def capture_if_safe():
                        state.recovery_state='requires_review'
                    self.recovery=SimpleNamespace(capture_if_safe=capture_if_safe,retire=real.retire)
            with ExitStack() as stack:
                def patcher(target,name,value): return stack.enter_context(patch.object(target,name,value))
                patcher(core,'owner',owner); patcher(core,'SETTINGS',settings)
                patcher(core.live_startup,'enabled',Mock(return_value=False))
                patcher(core,'recovery_coordinator',checks)
                image_preflight=patcher(core,'image_send_preflight',Mock(return_value=(root/'image.json',None,None)))
                patcher(core,'status',values.append); patcher(core,'remember_failure',Mock())
                patcher(core,'FILES',()); patcher(core,'Duplex',Mock(side_effect=[self.peer(),self.peer()]))
                patcher(core.runtime,'Controller',ReviewedController)
                patcher(core.groups,'PinnedNative',Mock(return_value=pinned))
                patcher(core.groups,'reader_properties',Mock(return_value=[]))
                patcher(core.owned,'identity',Mock(return_value='synthetic'))
                patcher(core.tap,'read_network',Mock(return_value={}))
                patcher(core.tap,'business_snapshot',Mock(return_value={}))
                patcher(core.tap,'require_unit_absent',Mock())
                patcher(core.tap,'stop_unit',Mock())
                patcher(core.tap,'compare_baselines',Mock(return_value=({},[])))
                patcher(core.trial,'read_onebot_token',Mock(return_value='synthetic-only'))
                patcher(core.health,'Probe',Mock(return_value=probe))
                patcher(core.media_egress,'Lease',Mock(return_value=lease))
                patcher(core.delivery,'role_digest',Mock(return_value='a'*64))
                patcher(core.tempfile,'mkdtemp',Mock(return_value=str(bundle)))
                original_require=core.require
                patcher(core,'require',lambda ok,stage: None if stage=='linux_root_required' else original_require(ok,stage))
                patcher(core.recovery_checkpoints,'bridge',lambda path:real_bridge(root/'bridge.sqlite'))
                self.assertEqual(core.serve(),0)
                image_preflight.assert_called_once(); checks.selected_pair.assert_called_once()
            self.assertEqual(values[-1]['code'],'requires_review')
            self.assertTrue(values[-1]['requires_review'])
            self.assertFalse(values[-1]['ready'])
            checks.block.assert_called_once()
            self.assertEqual(checks.block.call_args.args[1],'requires_review')


class PrivateOnlyRuntime(unittest.TestCase):
    def test_controller_rejects_group_event_and_group_send_before_dispatch(self):
        from wechat_bridge.runtime.broker import dispatch
        from wechat_bridge.runtime.state import State
        from scripts.run_private_forwarder import require
        state = State(settings={'privateOnly':True}, peer_id='peer')
        core = SimpleNamespace(input_wire=SimpleNamespace(FRAMES=()),require=require)
        for name, frame in (('native',{'kind':'event','data':{'room_id':'old@chatroom'}}),
                            ('host',{'kind':'send','destination':'old@chatroom'})):
            with self.subTest(kind=frame['kind']), self.assertRaises(Exception) as raised:
                dispatch(core,state,name,None,frame)
            self.assertEqual(getattr(raised.exception,'stage',None),'private_destination_required')

    def test_both_worker_configs_disable_all_groups(self):
        from wechat_bridge.runtime.processes import Workers
        from wechat_bridge.runtime.state import State
        from wechat_bridge.forward_config import DEFAULT
        state = State(settings={**DEFAULT,'privateOnly':True},room_id=None,seconds=600,
            generation='profile',legacy_generation='old',host_digest='a'*64,
            pinned=SimpleNamespace(token=lambda:'synthetic-native-token'))
        from wechat_bridge import forward_wire as wire
        core = SimpleNamespace(wire=wire,trial=SimpleNamespace(read_onebot_token=lambda:'synthetic-onebot-token',
            host_properties=lambda *a,**kw:[]), paths=SimpleNamespace(VERIFICATION=Path('/synthetic')))
        workers = Workers.__new__(Workers); workers.core,workers.state=core,state
        native = workers.native_options()
        workers.launch=Mock(); workers.spawn_host('peer','real-self')
        host = workers.launch.call_args.args[-1]
        for value in (native,host):
            self.assertIs(value.get('privateOnly'),True)
            self.assertIs(value['allGroups'],False)
            self.assertIsNone(value['roomId'])
