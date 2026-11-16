"""Synthetic package, compatibility and dependency boundaries for the rewrite."""
import ast
from dataclasses import asdict
import importlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


class Foundation(unittest.TestCase):
    def test_formal_controller_uses_shared_runtime_support(self):
        root=Path(__file__).resolve().parents[1]
        code=("import importlib.util, pathlib, sys; "
              "p=pathlib.Path('scripts/run_private_forwarder.py'); "
              "s=importlib.util.spec_from_file_location('formal_support_probe',p); "
              "m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); "
              "assert pathlib.Path(m.trial.__file__).name=='forward_runtime_support.py'; "
              "assert '_private_forward_run_bridge_trial' not in sys.modules; "
              "assert all(callable(getattr(m.trial,n)) for n in "
              "('fresh_status','ensure_session','read_onebot_token','host_properties','verify_host','Duplex'))")
        result=subprocess.run([sys.executable,'-B','-c',code],cwd=root,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_storage_compatibility_exports_have_one_identity(self):
        old=importlib.import_module('wechat_bridge.state')
        storage=importlib.import_module('wechat_bridge.storage')
        durable=importlib.import_module('wechat_bridge.durable')
        self.assertIs(old.State,storage.State);self.assertIs(old.StateError,storage.StateError)
        self.assertIs(durable.Spool,storage.Spool)
        self.assertIs(durable.State,storage.State);self.assertIs(durable.StateError,storage.StateError)
        self.assertIs(durable.Spool.__bases__[0],old.State)
        self.assertEqual(storage.State.__module__,'wechat_bridge.storage.state')

    def test_legacy_db_ids_echo_and_publishing_survive_storage_move(self):
        from wechat_bridge.storage import Spool,StateError
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture.sqlite'
            db=sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE identities(id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id BETWEEN 1 AND 2147483647),kind TEXT NOT NULL,raw TEXT NOT NULL,UNIQUE(kind,raw));
                INSERT INTO identities VALUES(731,'group','opaque-room');
                CREATE TABLE submissions(key TEXT PRIMARY KEY,digest TEXT NOT NULL,native_key TEXT NOT NULL,result TEXT);
                INSERT INTO submissions VALUES('echo-key','digest','native-key','{"status":"ok","retcode":0,"data":{"message_id":732},"echo":{"request":[null,4]}}');
                CREATE TABLE inbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT UNIQUE NOT NULL,digest TEXT NOT NULL,payload TEXT,state TEXT NOT NULL,created INTEGER NOT NULL);
                INSERT INTO inbox VALUES(42,'opaque-source','digest','{"body":"synthetic evidence"}','publishing',1);
            """)
            db.close();path.chmod(0o600)
            state=Spool(path)
            try:
                self.assertEqual(state.identity('group','opaque-room'),(731,False))
                self.assertEqual(state.identity('outgoing','opaque-send'),(732,True))
                self.assertEqual(state.submission('echo-key','digest')['echo'],{'request':[None,4]})
                with self.assertRaisesRegex(StateError,'echo_conflict'):state.submission('echo-key','other')
                self.assertIsNone(state.next_pending())
                self.assertEqual(state.db.execute('SELECT seq,state,payload FROM inbox').fetchone(),
                                 (42,'uncertain','{"body":"synthetic evidence"}'))
            finally:state.close()

    def test_core_reuses_wire_models_without_changing_fields_or_identity(self):
        from wechat_bridge import protocol
        from wechat_bridge.core import contracts
        self.assertIs(protocol.NativeMessage,contracts.NativeMessage)
        self.assertIs(protocol.Receipt,contracts.Receipt)
        message=contracts.NativeMessage('fixture','source','opaque-room','sender','body',7)
        self.assertEqual(asdict(message),dict(account='fixture',id='source',room_id='opaque-room',
                         sender_id='sender',body='body',timestamp=7,origin='live',direction='incoming',type=1))
        conversation=contracts.Conversation('fixture','opaque-room',contracts.ConversationKind.GROUP)
        self.assertEqual(conversation.raw_id,'opaque-room')
        self.assertEqual(contracts.SendResult(contracts.SendOutcome.UNKNOWN).outcome,'unknown')

    def test_core_and_storage_have_no_concrete_platform_network_or_runtime_imports(self):
        root=Path(__file__).resolve().parents[1]/'wechat_bridge'
        forbidden=('adapters','onebot','transports','runtime','protocol','continuous','forward_gateway',
                   'group_trigger','loop_guard','httpx','websockets','aiocqhttp','subprocess','systemd')
        for package in ('core','storage'):
            files=list((root/package).rglob('*.py'));self.assertTrue(files,package)
            for path in files:
                for node in ast.walk(ast.parse(path.read_text())):
                    names=[alias.name for alias in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
                    for name in names:
                        self.assertFalse(set(name.split('.'))&set(forbidden),(path,name))
                if package=='core':self.assertNotIn('@chatroom',path.read_text())

    def test_role_manifest_matches_native_worker_and_controller(self):
        root=Path(__file__).resolve().parents[1]
        from scripts import runtime_manifest as delivery
        names=delivery.role_files('native')
        self.assertIn('runtime_manifest.py',names)
        self.assertEqual(len(names),len(set(names)))
        for name in names:
            self.assertTrue(delivery.source_for(root,'native',name).is_file(),name)
        for relative in delivery.PUBLIC_CODE:
            self.assertTrue((root/relative).is_file(),relative)
        self.assertIn('wechat_bridge/core/__init__.py',delivery.role_files('host'))
        self.assertIn('wechat_bridge/storage/state.py',delivery.role_files('host'))
        for filename in ('scripts/run_private_forwarder.py','scripts/forward_native_worker.py'):
            self.assertIn("role_files('native')",(root/filename).read_text())

    def test_clean_code_view_imports_host_packages_and_flat_native_role(self):
        import importlib.util
        import shutil
        import subprocess
        import sys
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('foundation_sync',root/'tools/sync_runtime.py')
        tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
        with tempfile.TemporaryDirectory() as directory:
            clean=Path(directory)/'checkout';clean.mkdir()
            for relative,source in tool.public_files().items():
                target=clean/relative;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(source,target)
            tool.ROOT=clean;tool.sync(True)
            view=clean/'.runtime/verification'
            code=("import sys;sys.path.insert(0,sys.argv[1]);"
                  "from scripts import runtime_manifest as m;"
                  "assert m.verify_role(sys.argv[1],'host');"
                  "from wechat_bridge.storage import State,Spool;"
                  "from wechat_bridge.state import State as old;assert old is State;"
                  "from wechat_bridge.continuous import PrivateBridge;"
                  "from wechat_bridge.forward_gateway import Gateway")
            result=subprocess.run([sys.executable,'-I','-B','-c',code,str(view)],
                                  capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            native=Path(directory)/'native';native.mkdir()
            for name in tool.delivery.role_files('native'):
                shutil.copyfile(tool.delivery.source_for(clean,'native',name),native/name)
            code=("import importlib.util,sys;from pathlib import Path;"
                  "p=Path(sys.argv[1])/'forward_native_worker.py';"
                  "s=importlib.util.spec_from_file_location('clean_native',p);"
                  "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;"
                  "s.loader.exec_module(m);assert m.reader.CODE_FILES==m.delivery.role_files('native')")
            result=subprocess.run([sys.executable,'-I','-B','-c',code,str(native)],
                                  capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_isolated_runner_covers_foundation_contracts(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn("'test_architecture_foundation.py'",(root/'scripts/run_onebot_tests.py').read_text())
