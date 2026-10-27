"""Exercise the real boot decision without importing privileged Linux modules."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class BootRestore(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'scripts/run_private_forwarder.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == 'ensure_saved_session')
        self.store = Mock()
        self.store.latest.return_value = Path('/private/current.zip')
        self.owner = Mock(STATE=Path('/private/state'), CONTROL=Path('/volatile/control'), LONG_RUNTIME_SECONDS=604800)
        self.owner.owner_active.return_value = False
        self.owner.read_json.side_effect = FileNotFoundError
        self.owner.companion.return_value = SimpleNamespace(SnapshotStore=Mock(return_value=self.store))
        self.trial = Mock()
        def require(value, code):
            if not value:
                raise ValueError(code)
        namespace = {'owner': self.owner, 'trial': self.trial, 'require': require}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        self.ensure = namespace['ensure_saved_session']

    def test_reboot_without_volatile_status_restores_durable_profile(self):
        self.ensure({'runtimeSeconds': 604200})
        self.store.prepare.assert_called_once_with()
        self.owner.start_owner.assert_called_once_with(False, seconds=604800)
        self.trial.ensure_session.assert_called_once_with()

    def test_missing_snapshot_stops_before_starting_native(self):
        self.store.latest.return_value = None
        with self.assertRaisesRegex(ValueError, 'saved_session_missing'):
            self.ensure({'runtimeSeconds': 604200})
        self.owner.start_owner.assert_not_called()
        self.trial.ensure_session.assert_not_called()

    def test_failed_previous_cleanup_cannot_be_bypassed(self):
        self.owner.read_json.side_effect = None
        self.owner.read_json.return_value = {'ok': False, 'accounts': 1, 'cleanup': {}}
        with self.assertRaisesRegex(ValueError, 'saved_session_not_ready'):
            self.ensure({'runtimeSeconds': 604200})
        self.owner.start_owner.assert_not_called()

    def test_online_native_is_reused(self):
        self.owner.owner_active.return_value = True
        self.ensure({'runtimeSeconds': 604200})
        self.owner.start_owner.assert_not_called()
        self.owner.read_json.assert_not_called()
        self.trial.ensure_session.assert_called_once_with()
