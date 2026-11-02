from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import recovery_checkpoint as recovery
from scripts import recovery_manifest, recovery_snapshot
from scripts.forward_sse import event_digest
from wechat_bridge.durable import Spool
from test_recovery_snapshot import fixture, event, SEED


class CheckpointCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = self.root/'private'
        self.state_dir.mkdir(mode=0o700)
        self.spool = Spool(self.root/'bridge.sqlite')
        self.addCleanup(self.spool.close)
        self.spool.db.execute('PRAGMA synchronous=OFF')  # synthetic bulk fixture only
        self.generation = 'profile:'+'b'*64
        self.archive = fixture(self.root,last=10)
        keydir = self.root/'keys'; keydir.mkdir(mode=0o700)
        for name in ('api-token','storage-key','cursor-key'):
            (keydir/name).write_bytes(('synthetic-'+name).encode())
            (keydir/name).chmod(0o600)
        keyfd = os.open(keydir,os.O_RDONLY|os.O_DIRECTORY)
        self.addCleanup(os.close,keyfd)
        self.pinned = SimpleNamespace(unchanged=lambda:True,host=SimpleNamespace(client_pid=123,client_ticks=456,
            data_fd=keyfd,client_uid=os.geteuid()))
        self.during = lambda:None
        @contextmanager
        def checkpoint_copy(pinned):
            self.assertFalse(self.spool.db.in_transaction)
            self.assertIs(pinned,self.pinned)
            self.during()
            yield self.archive
        self.owner = SimpleNamespace(checkpoint_copy=checkpoint_copy)
        self.adapter = recovery.ForwarderCapture(self.owner,self.pinned,self.generation,SEED)

    def add(self,n,*,commit_cursor=True,terminal=True):
        source = 'source-'+str(n)
        data = dict(account='wechat-primary',id=source,body='same',sender_id='peer',room_id='peer',
                    direction='incoming',origin='live',type=1,timestamp=1)
        if commit_cursor:
            self.spool.admit_checkpoint(source,data,self.generation,json.dumps(self.boundary(n)))
        else:
            self.spool.admit(source,data)
        if terminal:
            self.spool.publishing(source)
            self.spool.handoff(source,True)
        return data

    @staticmethod
    def boundary(n): return {'cursor':str(n),'anchor':event_digest(event(n))}

    def capture(self):
        return recovery.capture(self.adapter,self.spool.path,self.state_dir)

    def target(self,pair,get_event=event):
        return dict(epoch=pair[0]['epoch'],profileSha256=pair[0]['profileSha256'],
                    checkpointName=self.generation,get_event=lambda cursor:get_event(int(cursor)))


class CaptureTests(CheckpointCase):
    def test_capture_bridge_advances_after_raw_snapshot_and_max_is_not_receive_start(self):
        for n in range(1,9): self.add(n)
        self.during = lambda:self.add(9)
        pair = self.capture()
        self.assertEqual(pair[0]['bridgeBoundary'],self.boundary(9))
        self.assertEqual(pair[0]['nativeBoundary'],self.boundary(10))
        self.assertEqual(pair[0]['dedup'],dict(floorSeq=9,highSeq=9))
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],9)

    def test_old_split_admission_retains_last_source_and_live_publishing_is_not_repaired(self):
        for n in range(1,9): self.add(n)
        self.add(9,commit_cursor=False,terminal=False)
        self.spool.publishing('source-9')
        pair = self.capture()
        self.assertEqual(pair[0]['bridgeBoundary'],self.boundary(8))
        self.assertEqual(pair[0]['dedup']['floorSeq'],9)
        self.assertEqual(self.spool.counts(),{'forwarded':8,'publishing':1})

    def test_two_complete_generations_protect_minimum_until_second_distinct_refresh(self):
        self.add(1)
        first = self.capture()
        for n in range(2,9): self.add(n)
        second = self.capture()
        self.assertNotEqual(first,second)
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],1)
        third = self.capture()
        self.assertNotEqual(second[0]['createdAt'],third[0]['createdAt'])
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],8)
        store = recovery_manifest.Store(self.state_dir/'recovery')
        self.assertEqual(store.previous()[0],second[0])
        self.assertEqual(first[0]['epoch'],third[0]['epoch'])

    def test_pointer_committed_then_interrupted_never_advances_floor(self):
        self.add(1); self.capture()
        self.add(2); self.capture()
        original = recovery_manifest.Store._commit_pointer
        def interrupted(store,*args):
            original(store,*args)
            raise OSError('synthetic power interruption')
        with mock.patch.object(recovery_manifest.Store,'_commit_pointer',interrupted):
            with self.assertRaisesRegex(ValueError,'^recovery_checkpoint$'): self.capture()
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],1)
        store = recovery_manifest.Store(self.state_dir/'recovery')
        self.assertEqual(store.current()[0]['dedup']['floorSeq'],2)
        self.assertEqual(store.previous()[0]['dedup']['floorSeq'],2)
        self.capture()
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],2)

    def test_legacy_numeric_requires_current_pinned_native_generation(self):
        self.add(1)
        self.spool.cursor(self.generation,'1')
        with self.assertRaises(ValueError): self.capture()
        self.generation = 'native:123:456'
        self.adapter.checkpoint_name = self.generation
        self.spool.cursor(self.generation,'1')
        self.assertEqual(self.capture()[0]['bridgeBoundary'],self.boundary(1))

    def test_missing_mismatched_and_expired_anchor_never_choose_native_max(self):
        with self.assertRaises(ValueError): self.capture()
        self.add(1)
        for boundary in ({'cursor':'1','anchor':'0'*64},self.boundary(11)):
            self.spool.cursor(self.generation,json.dumps(boundary))
            with self.assertRaises(ValueError): self.capture()

    def test_fresh_causal_capture_can_bootstrap_pruned_history_but_old_manifest_cannot(self):
        for n in range(1,9): self.add(n)
        with self.spool.db:
            self.spool.db.execute('DELETE FROM inbox WHERE seq<7')
            self.spool.db.execute('UPDATE recovery_window SET lost_through=6')
        pair = self.capture()
        self.assertEqual(pair[0]['dedup']['floorSeq'],8)
        self.assertEqual(self.spool.recovery_coverage()['floorSeq'],8)
        with self.spool.db:
            self.spool.db.execute('UPDATE recovery_window SET lost_through=8')
        with self.assertRaisesRegex(ValueError,'^recovery_rebase$'):
            recovery.rebase_bridge(self.spool.path,pair,self.target(pair))

    def test_pending_native_send_and_unaccepted_seed_fail_closed(self):
        self.add(1)
        with tempfile.TemporaryDirectory() as directory:
            self.archive = fixture(Path(directory),pending=True,last=10)
            with self.assertRaises(ValueError): self.capture()
        with tempfile.TemporaryDirectory() as directory:
            self.archive = fixture(Path(directory),seed=False,last=10)
            with self.assertRaises(ValueError): self.capture()

    def test_maintenance_preserves_pending_history_and_committed_immutable_archives(self):
        self.add(1)
        pair = self.capture()
        original = pair[1].read_bytes()
        recovery.validate_maintenance_snapshot(self.archive,self.archive,self.state_dir)
        with tempfile.TemporaryDirectory() as directory:
            changed = fixture(Path(directory),last=10,pending=True)
            with self.assertRaisesRegex(ValueError,'^recovery_maintenance$'):
                recovery.validate_maintenance_snapshot(self.archive,changed,self.state_dir)
        self.assertEqual(pair[1].read_bytes(),original)

    def test_private_import_under_isolated_interpreter(self):
        path = Path(recovery.__file__).resolve()
        code = ('import importlib.util,sys; '
                's=importlib.util.spec_from_file_location("_capture_test",sys.argv[1]); '
                'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                'assert callable(m.capture) and callable(m.rebase_bridge)')
        result = subprocess.run([sys.executable,'-I','-B','-c',code,str(path)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr.decode())

    def test_live_bridge_rejects_symlink_hardlink_and_nonprivate_file(self):
        path = self.spool.path
        link = self.root/'link.sqlite'
        link.symlink_to(path)
        with self.assertRaisesRegex(ValueError,'^recovery_bridge$'):
            with recovery.bridge(link): pass
        link.unlink(); os.link(path,link)
        with self.assertRaises(ValueError):
            with recovery.bridge(path): pass
        link.unlink(); path.chmod(0o644)
        with self.assertRaises(ValueError):
            with recovery.bridge(path): pass

    def test_only_exact_systemd_alias_accepts_service_owned_private_state(self):
        public = self.root/'public/private-forward.sqlite'
        private = self.root/'managed/state/private-forward.sqlite'
        private.parent.mkdir(parents=True,mode=0o700)
        recovery.backup(self.spool,private)
        public.parent.symlink_to(private.parent)
        actual_fstat, actual_lstat = os.fstat,Path.lstat
        final_inode=private.parent.stat().st_ino
        def root_owned(info):
            values=list(info); values[4]=0
            return os.stat_result(values)
        def pinned_stat(fd):
            info=actual_fstat(fd)
            return root_owned(info) if stat.S_ISDIR(info.st_mode) and info.st_ino!=final_inode else info
        def link_stat(path):
            info=actual_lstat(path)
            return root_owned(info) if path==public.parent else info
        with mock.patch.object(recovery,'BRIDGE_PUBLIC',public), mock.patch.object(recovery,'BRIDGE_PRIVATE',private), \
             mock.patch.object(recovery.os,'geteuid',return_value=0), mock.patch.object(recovery.os,'fstat',side_effect=pinned_stat), \
             mock.patch.object(Path,'lstat',link_stat):
            with recovery.bridge(public) as live:
                self.assertEqual(live.recovery_coverage()['highSeq'],0)
            public.parent.unlink(); public.parent.symlink_to(self.root)
            with self.assertRaisesRegex(ValueError,'^recovery_bridge$'):
                with recovery.bridge(public): pass
