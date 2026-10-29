import importlib.util
from pathlib import Path
import unittest
import tempfile
from unittest import mock
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('automatic_maintenance', Path(__file__).resolve().parents[1] / 'scripts/maintain_media_cache.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Fake:
    def __init__(self, failure=None, gain=True):
        self.events=[]; self.failure=failure; self.gain=gain; self.saved=[]
    def journal(self, **value): self.saved.append(value)
    def eligibility(self): return 'maintain'
    def preflight(self): self.events.append('preflight'); return self.gain
    def check_idle(self): self.events.append('check_idle')
    def stop_bridge(self): self.events.append('stop_bridge')
    def stop_native(self):
        self.events.append('stop_native')
        if self.failure == 'stop': raise ValueError('injected')
    def clean(self):
        self.events.append('clean')
        if self.failure == 'clean': raise ValueError('injected')
        return {'sources_removed':2, 'bytes_reclaimed':2000000}
    def restore(self):
        self.events.append('restore')
        if self.failure == 'restore': raise ValueError('injected')
    def start_bridge(self): self.events.append('start_bridge')
    def recover(self): self.events.append('recover'); return self.failure != 'restore'
    def retain_backups(self): self.events.append('retain_backups')


class Cycle(unittest.TestCase):
    def test_success_sequence_has_no_resend_and_journals_before_stop(self):
        ops=Fake()
        self.assertEqual(mod.cycle(ops), 'success')
        self.assertEqual(ops.events, ['preflight','check_idle','stop_bridge','stop_native','clean','restore','start_bridge','retain_backups'])
        self.assertEqual(ops.saved[0]['outcome'], 'in_progress')
        self.assertEqual(ops.saved[-1]['outcome'], 'success')

    def test_fresh_no_gain_after_stop_restores_and_uses_long_cooldown(self):
        ops=Fake(); ops.clean=lambda: {'sources_removed':0, 'bytes_reclaimed':0}
        self.assertEqual(mod.cycle(ops), 'no_gain')
        self.assertEqual(ops.saved[-1]['outcome'], 'no_gain')
        self.assertIn('restore', ops.events)
        self.assertIn('start_bridge', ops.events)

    def test_no_gain_never_stops_services(self):
        ops=Fake(gain=False)
        self.assertEqual(mod.cycle(ops), 'no_gain')
        self.assertEqual(ops.events, ['preflight'])
        self.assertEqual(ops.saved[-1]['outcome'], 'no_gain')

    def test_clean_failure_restores_original_without_second_clean_or_resend(self):
        ops=Fake(failure='clean')
        self.assertEqual(mod.cycle(ops), 'failed')
        self.assertEqual(ops.events.count('clean'),1)
        self.assertEqual(ops.events[-1], 'recover')
        self.assertEqual(ops.saved[-1]['outcome'], 'failed')

    def test_failed_restore_blocks_next_automatic_attempt(self):
        ops=Fake(failure='restore')
        self.assertEqual(mod.cycle(ops), 'requires_review')
        self.assertEqual(ops.events.count('restore'),1)
        self.assertEqual(ops.saved[-1]['outcome'], 'in_progress')
        self.assertEqual(ops.saved[-1]['phase'], 'requires_review')

    def test_native_stop_requires_explicit_final_snapshot_and_stop_proof(self):
        base=dict(explicit_stop=['owned.service'], host_namespace_unchanged=True,
                  native_stop_ok=True, final_snapshot_saved=True)
        for changed in ({'final_snapshot_saved':False}, {'native_stop_ok':False},
                        {'final_snapshot_saved':1}, {'final_snapshot_saved':None}, {}):
            with self.subTest(changed=changed):
                receipt={**base, **changed}
                backend=object.__new__(mod.Backend)
                backend.no_workers=mock.Mock(); backend.spool=mock.Mock(); backend.unchanged_audits=mock.Mock()
                backend.owner=SimpleNamespace(CONTROL=Path('/synthetic'), read_json=lambda _: {'cleanup':receipt}, owner_active=lambda:False)
                backend.units={'owned.service'}; backend.stopped_unit=lambda _:True
                backend.native_stopped=False; backend.store=SimpleNamespace(latest=lambda:Path('/synthetic/current.zip'))
                backend.backup=Path('/synthetic/backup'); backend.proof={'keys':'synthetic'}
                backend.analyze=lambda _: backend.proof
                with mock.patch.object(mod.subprocess,'run',return_value=SimpleNamespace(returncode=0)), mock.patch.object(mod,'copy_private') as copied:
                    if changed:
                        with self.assertRaisesRegex(ValueError,'native_cleanup'): backend.stop_native()
                        self.assertFalse(backend.native_stopped); copied.assert_not_called()
                    else:
                        backend.stop_native(); self.assertTrue(backend.native_stopped); copied.assert_called_once()

    def test_real_recovery_never_retries_an_attempted_native_restore(self):
        backend=object.__new__(mod.Backend)
        backend.restore_attempted=True; backend.native_restored=False
        backend.restore=mock.Mock()
        self.assertFalse(backend.recover())
        backend.restore.assert_not_called()

    def test_retention_including_failed_attempts_keeps_three_newest(self):
        with tempfile.TemporaryDirectory() as directory:
            backend=object.__new__(mod.Backend); backend.backups=Path(directory)
            names=[str(1900000000000000000+i) for i in range(5)]
            for name in names:
                p=backend.backups/name; p.mkdir(mode=0o700); (p/'original.zip').write_bytes(b'synthetic')
            unrelated=backend.backups/'manual-backup'; unrelated.mkdir()
            backend.retain_backups()
            self.assertEqual(sorted(p.name for p in backend.backups.iterdir()), names[-3:]+['manual-backup'])

    def test_unit_is_project_only_and_has_no_restart_loop(self):
        service, timer = mod.unit_texts(Path('/example/project/scripts/maintain_media_cache.py'))
        self.assertIn('Type=oneshot', service)
        self.assertIn('--once', service)
        self.assertNotIn('Restart=', service)
        self.assertIn('OnUnitInactiveSec=60s', timer)
        self.assertIn('astrbot-wechat-media-maintenance.service', timer)
        self.assertNotIn('docker', service+timer)


if __name__ == '__main__': unittest.main()
