import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid
import unittest
from unittest import mock
from test_recovery_snapshot import fixture, event, SEED
from scripts.forward_sse import event_digest
from wechat_bridge.durable import Spool

EPOCH = 'a'*32

def metadata(root, profile, last=8, high=8):
    spool=Spool(root/('capture-'+uuid.uuid4().hex+'.sqlite'))
    spool.db.execute('PRAGMA synchronous=OFF')
    spool.protect_recovery(1,EPOCH)
    for i in range(high):
        spool.admit(str(i),{'body':'same'})
        spool.publishing(str(i)); spool.handoff(str(i),True)
    spool.cursor('capture',json.dumps({'cursor':str(last),'anchor':event_digest(event(last))}))
    return dict(epoch=EPOCH, profileSha256=profile, ledgerRevision=0,
                bridgeBoundary={'cursor':str(last),'anchor':event_digest(event(last))},
                dedup={'floorSeq':1,'highSeq':high}, selfProof=SEED, createdAt='2026-01-01T00:00:00Z',
                capture=dict(spool=spool,checkpointName='capture'))

class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def setup_store(self):
        from scripts.recovery_manifest import Store
        from scripts.recovery_snapshot import inspect
        first = fixture(self.root)
        profile = inspect(first)['profileSha256']
        return Store(self.root/'store'), first, metadata(self.root,profile)

    def test_required_module(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.recovery_manifest'))

    def test_two_versions_recover_at_eight_without_bridge_rollback(self):
        store, first, meta = self.setup_store()
        manifest = store.publish(first, meta)
        spool = Spool(self.root/'bridge.sqlite')
        self.addCleanup(spool.close)
        spool.protect_recovery(1,EPOCH)
        identity = spool.identity('user','synthetic')[0]
        spool.reserve('action','digest','native')
        for i in range(1,11):
            spool.admit(str(i),{'body':'same'})
            spool.publishing(str(i))
            spool.handoff(str(i),True)
        spool.cursor('generation',json.dumps({'cursor':'10','anchor':event_digest(event(10))}))
        original_bridge=spool.path.read_bytes()
        plan = store.recovery_plan(dict(spool=spool,profileSha256=meta['profileSha256'],epoch=EPOCH,checkpointName='generation'),manifest)
        self.assertEqual(plan['boundary'],meta['bridgeBoundary'])
        self.assertEqual(plan['epoch'],EPOCH)
        self.assertEqual(spool.counts(),{'forwarded':10})
        self.assertEqual(spool.raw_identity('user',identity),'synthetic')
        self.assertIsNotNone(spool.submission('action','digest'))
        self.assertEqual(spool.path.read_bytes(),original_bridge)
        second = fixture(self.root,last=10)
        store.publish(second,metadata(self.root,meta['profileSha256'],10,10))
        self.assertEqual(store.previous()[0],manifest[0])
        self.assertEqual(store.current()[0]['nativeBoundary']['cursor'],'10')
        self.assertEqual(store.recovery_plan(dict(spool=spool,profileSha256=meta['profileSha256'],epoch=EPOCH,checkpointName='generation'),store.previous())['boundary']['cursor'],'8')
        third = fixture(self.root,last=11)
        store.publish(third,metadata(self.root,meta['profileSha256'],11,11))
        self.assertEqual(len(list((self.root/'store').glob('*.zip'))),2)

    def test_native_maximum_is_not_receive_start_and_null_bootstrap_is_unusable(self):
        store, first, meta=self.setup_store()
        newer=fixture(self.root,last=10)
        pair=store.publish(newer,meta)
        bridge=dict(spool=meta['capture']['spool'],profileSha256=meta['profileSha256'],epoch=EPOCH,checkpointName='capture')
        self.assertEqual(pair[0]['nativeBoundary']['cursor'],'10')
        self.assertEqual(store.recovery_plan(bridge,pair)['boundary']['cursor'],'8')
        bootstrap={**meta,'bridgeBoundary':None}
        with bootstrap['capture']['spool'].db:
            bootstrap['capture']['spool'].db.execute('DELETE FROM checkpoints')
        nullpair=store.publish(first,bootstrap)
        with self.assertRaises(ValueError): store.recovery_plan(bridge,nullpair)

    def test_every_fsync_and_post_write_fault_preserves_complete_pair(self):
        from scripts.recovery_manifest import Store
        from scripts.recovery_snapshot import inspect
        for stage in list(range(1,8))+['copy','manifest','pointer']:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                paths=[fixture(root,last=n) for n in (8,10,11)]
                profile=inspect(paths[0])['profileSha256']
                store=Store(root/'store')
                store.publish(paths[0],metadata(root,profile))
                store.publish(paths[1],metadata(root,profile,10,10))
                calls=0
                method='_sync' if isinstance(stage,int) else {'copy':'_copy_archive','manifest':'_write_manifest','pointer':'_commit_pointer'}[stage]
                original=getattr(store,method)
                def fail(*args):
                    nonlocal calls
                    calls+=1
                    result=original(*args)
                    if not isinstance(stage,int) or calls==stage:
                        raise OSError('synthetic interruption')
                    return result
                with mock.patch.object(store,method,side_effect=fail):
                    with self.assertRaisesRegex(ValueError,'^recovery_manifest$'):
                        store.publish(paths[2],metadata(root,profile,11,11))
                now=store.current()[0]['nativeBoundary']['cursor']
                before=store.previous()[0]['nativeBoundary']['cursor']
                self.assertIn((now,before),[('10','8'),('11','10')])
                self.assertLessEqual(len(list((root/'store').glob('*.zip'))),3)
                store.publish(paths[2],metadata(root,profile,11,11))
                self.assertEqual(len(list((root/'store').glob('*.zip'))),2)

    def test_failure_boundaries_select_only_old_or_complete_new(self):
        from scripts import recovery_manifest as module
        for point in ('copy','manifest','replace','fsync'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                from scripts.recovery_snapshot import inspect
                old=fixture(root)
                new=fixture(root,last=10)
                store=module.Store(root/'store')
                meta=metadata(root,inspect(old)['profileSha256'])
                before=store.publish(old,meta)
                target={'copy':'_copy_archive','manifest':'_write_manifest','replace':'_commit_pointer','fsync':'_sync'}[point]
                with mock.patch.object(store,target,side_effect=OSError('private failure')):
                    with self.assertRaisesRegex(ValueError,'^recovery_manifest$'):
                        store.publish(new,metadata(root,meta['profileSha256'],10,10))
                selected=store.current()
                self.assertIn(selected[0]['nativeBoundary']['cursor'],('8','10'))
                if selected[0]['nativeBoundary']['cursor']=='8':
                    self.assertEqual(selected[0],before[0])
                store.publish(new,metadata(root,meta['profileSha256'],10,10))
                self.assertLessEqual(len(list((root/'store').glob('*.zip'))),2)

    def test_concurrent_publishers_and_corruption_rejection(self):
        store, first, meta=self.setup_store()
        second=fixture(self.root,last=10)
        errors=[]
        def publish(path,value):
            try:
                fresh=metadata(self.root,meta['profileSha256'],value,value)
                try: store.publish(path,fresh)
                finally: fresh['capture']['spool'].close()
            except Exception as exc:
                errors.append(exc)
        threads=[threading.Thread(target=publish,args=(first,8)),threading.Thread(target=publish,args=(second,10))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors,[])
        self.assertIsNotNone(store.current())
        self.assertIsNotNone(store.previous())
        archive=store.current()[1]
        with archive.open('r+b') as target:
            target.write(b'broken')
        with self.assertRaisesRegex(ValueError,'^recovery_manifest$'):
            store.current()

    def test_reject_unavailable_boundary_and_wrong_profile(self):
        store, first, meta=self.setup_store()
        for change in ({'profileSha256':'0'*64},{'bridgeBoundary':{'cursor':'9','anchor':'0'*64}},{'dedup':{'floorSeq':10,'highSeq':8}}):
            with self.assertRaises(ValueError):
                store.publish(first,{**meta,**change})
        empty=fixture(self.root,last=0)
        with self.assertRaises(ValueError):
            store.publish(empty,{**meta,'bridgeBoundary':None})

    def test_capture_is_required_and_publication_never_advances_protection(self):
        store, first, meta=self.setup_store()
        bad={**meta,'capture':dict(meta['capture'])}
        del bad['capture']
        with self.assertRaises(ValueError):
            store.publish(first,bad)
        for change in [{'epoch':'b'*32},{'dedup':{'floorSeq':1,'highSeq':7}},{'dedup':{'floorSeq':0,'highSeq':8}}]:
            bad={**meta,**change}
            with self.assertRaises(ValueError):
                store.publish(first,bad)
        spool=meta['capture']['spool']
        before=spool.recovery_coverage()
        store.publish(first,meta)
        self.assertEqual(spool.recovery_coverage(),before)

    def test_recovery_rechecks_pair_lineage_coverage_and_uncertainty(self):
        store, first, meta=self.setup_store()
        pair=store.publish(first,meta)
        spool=Spool(self.root/'bridge.sqlite')
        self.addCleanup(spool.close)
        spool.protect_recovery(1,EPOCH)
        for i in range(8):
            spool.admit(str(i),{'body':'same'})
            spool.publishing(str(i)); spool.handoff(str(i),True)
        spool.cursor('generation',json.dumps(meta['bridgeBoundary']))
        bridge=dict(spool=spool,profileSha256=meta['profileSha256'],epoch=EPOCH,checkpointName='generation')
        self.assertEqual(store.recovery_plan(bridge,pair)['boundary'],meta['bridgeBoundary'])
        for change in [{'epoch':'b'*32},{'profileSha256':'0'*64},{'checkpointName':'missing'}]:
            with self.assertRaises(ValueError):
                store.recovery_plan({**bridge,**change},pair)
        forged=copy.deepcopy(pair[0]); forged['dedup']['highSeq']=0
        with self.assertRaises(ValueError):
            store.recovery_plan(bridge,(forged,pair[1]))
        spool.admit('unknown',{'body':'same'}); spool.publishing('unknown')
        with self.assertRaises(ValueError):
            store.recovery_plan(bridge,pair)

    def test_uncertain_inside_and_below_floor_preserves_bytes_and_never_replays(self):
        from scripts.recovery_manifest import Store
        from scripts.recovery_snapshot import inspect
        for floor in (1,2):
            with self.subTest(floor=floor), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                archive=fixture(root)
                meta=metadata(root,inspect(archive)['profileSha256'],high=0)
                spool=meta['capture']['spool']
                self.addCleanup(spool.close)
                spool.admit('held',{'body':'same'})
                spool.publishing('held'); spool.handoff('held',False)
                for i in range(8):
                    spool.admit(str(i),{'body':'same'})
                    spool.publishing(str(i)); spool.handoff(str(i),True)
                spool.protect_recovery(floor,EPOCH)
                meta['dedup']={'floorSeq':floor,'highSeq':9}
                bridge=dict(spool=spool,profileSha256=meta['profileSha256'],epoch=EPOCH,checkpointName='capture')
                original=spool.path.read_bytes()
                store=Store(root/'store')
                pair=store.publish(archive,meta)
                self.assertEqual(spool.path.read_bytes(),original)
                self.assertEqual(store.recovery_plan(bridge,pair)['boundary'],meta['bridgeBoundary'])
                self.assertEqual(spool.path.read_bytes(),original)
                self.assertIsNone(spool.next_pending())
                self.assertFalse(spool.admit('held',{'body':'same'}))
                state,payload=spool.db.execute("SELECT state,payload FROM inbox WHERE source='held'").fetchone()
                self.assertEqual(state,'uncertain')
                self.assertEqual(json.loads(payload),{'body':'same'})
                self.assertEqual(spool.recovery_coverage()['floorSeq'],floor)

    def test_restart_quarantines_publishing_then_publish_and_plan_succeed(self):
        store, archive, meta=self.setup_store()
        spool=meta['capture']['spool']
        spool.admit('interrupted',{'body':'same'}); spool.publishing('interrupted')
        meta['dedup']['highSeq']=9
        with self.assertRaisesRegex(ValueError,'^recovery_manifest$'):
            store.publish(archive,meta)
        path=spool.path
        payload=spool.db.execute("SELECT payload FROM inbox WHERE source='interrupted'").fetchone()[0]
        spool.close()
        spool=Spool(path)
        self.addCleanup(spool.close)
        meta['capture']['spool']=spool
        self.assertEqual(spool.db.execute("SELECT state,payload FROM inbox WHERE source='interrupted'").fetchone(),('uncertain',payload))
        original=spool.path.read_bytes()
        pair=store.publish(archive,meta)
        bridge=dict(spool=spool,profileSha256=meta['profileSha256'],epoch=EPOCH,checkpointName='capture')
        self.assertEqual(store.recovery_plan(bridge,pair)['boundary'],meta['bridgeBoundary'])
        self.assertEqual(spool.path.read_bytes(),original)
        self.assertIsNone(spool.next_pending())
        self.assertFalse(spool.admit('interrupted',{'body':'same'}))

    def test_raw_publishing_unknown_and_malformed_states_still_reject(self):
        store, archive, meta=self.setup_store()
        pair=store.publish(archive,meta)
        spool=meta['capture']['spool']
        self.addCleanup(spool.close)
        bridge=dict(spool=spool,profileSha256=meta['profileSha256'],epoch=EPOCH,checkpointName='capture')
        for state in ('publishing','unknown','',7,b'uncertain'):
            with self.subTest(state=state):
                with spool.db:
                    spool.db.execute('UPDATE inbox SET state=? WHERE source=?',(state,'0'))
                original=spool.path.read_bytes()
                with self.assertRaisesRegex(ValueError,'^recovery_manifest$'):
                    store.publish(archive,meta)
                with self.assertRaisesRegex(ValueError,'^recovery_manifest$'):
                    store.recovery_plan(bridge,pair)
                self.assertEqual(spool.path.read_bytes(),original)

    def test_numeric_cursor_without_active_protection_cannot_claim_recoverability(self):
        store, first, meta=self.setup_store()
        pair=store.publish(first,meta)
        spool=Spool(self.root/'unprotected.sqlite')
        self.addCleanup(spool.close)
        for i in range(10):
            spool.admit(str(i),{'body':'same'})
            spool.publishing(str(i)); spool.handoff(str(i),True)
        spool.admit('held',{'body':'same'}); spool.publishing('held'); spool.handoff('held',False)
        spool.cursor('generation',json.dumps({'cursor':'10','anchor':event_digest(event(10))}))
        bridge=dict(spool=spool,profileSha256=meta['profileSha256'],epoch=EPOCH,checkpointName='generation')
        with self.assertRaises(ValueError): store.recovery_plan(bridge,pair)

    def test_pointer_manifest_and_object_safety(self):
        store, first, meta=self.setup_store()
        store.publish(first,meta)
        pointer=self.root/'store'/'current.json'
        saved=pointer.read_bytes()
        for content in [b'{',b'{"current":null,"previous":null}',saved+b' '*(65537)]:
            pointer.write_bytes(content)
            with self.assertRaises(ValueError): store.current()
        pointer.write_bytes(saved)
        archive=store.current()[1]
        archive.chmod(0o644)
        with self.assertRaises(ValueError): store.current()
        archive.chmod(0o600)
        os.link(archive,self.root/'linked')
        with self.assertRaises(ValueError): store.current()

    def test_noncanonical_manifest_cannot_become_an_unstable_committed_name(self):
        store, first, meta=self.setup_store()
        store.publish(first,meta)
        directory=self.root/'store'
        pointer=json.loads((directory/'current.json').read_bytes())
        old=directory/(pointer['current']+'.json')
        encoded=json.dumps(json.loads(old.read_bytes()),indent=2).encode()
        new=directory/(hashlib.sha256(encoded).hexdigest()+'.json')
        new.write_bytes(encoded); new.chmod(0o600); old.unlink()
        pointer['current']=new.stem
        (directory/'current.json').write_text(json.dumps(pointer))
        with self.assertRaisesRegex(ValueError,'^recovery_manifest$'): store.current()
