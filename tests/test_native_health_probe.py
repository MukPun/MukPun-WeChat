"""The Native health probe only reads; these cases use synthetic transports."""
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location('health_probe_tested', ROOT / 'scripts/native_health_probe.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


health = load()


class FakePolicy:
    def __init__(self, prefix):
        self.prefix = prefix
        self.path = None


class FakeLookup:
    """Stand-in for ContactReceiveClient: records the authorized path."""

    def __init__(self, value):
        self.policy = FakePolicy('/v1/accounts/wechat-primary')
        self.value = value
        self.calls = []

    def _get(self, path, deadline, *, timeout):
        self.calls.append((path, timeout))
        return self.value


class ReadAccountTests(unittest.TestCase):
    def test_online_account_is_the_only_healthy_answer(self):
        lookup = FakeLookup({'id': 'wechat-primary', 'authenticated': True, 'connection': 'online'})
        self.assertEqual(health.read_account(lookup, 0),
                         {'ok': True, 'authenticated': True, 'connection': 'online', 'connected': True})
        self.assertEqual(lookup.calls, [('/v1/accounts/wechat-primary', health.REQUEST_TIMEOUT_SECONDS)])
        self.assertEqual(lookup.policy.path, '/v1/accounts/wechat-primary')

    def test_authenticated_but_not_online_is_not_connected(self):
        for connection in ('reconnecting', 'disconnected'):
            with self.subTest(connection=connection):
                lookup = FakeLookup({'id': 'wechat-primary', 'authenticated': True, 'connection': connection})
                self.assertIs(health.read_account(lookup, 0)['connected'], False)

    def test_unauthenticated_online_is_not_connected(self):
        lookup = FakeLookup({'id': 'wechat-primary', 'authenticated': False, 'connection': 'online'})
        self.assertIs(health.read_account(lookup, 0)['connected'], False)

    def test_unexpected_shapes_are_rejected_instead_of_guessed(self):
        shapes = ({'id': 'someone-else', 'authenticated': True, 'connection': 'online'},
                  {'id': 'wechat-primary', 'authenticated': 1, 'connection': 'online'},
                  {'id': 'wechat-primary', 'authenticated': True, 'connection': 'whatever'},
                  {})
        for value in shapes:
            with self.subTest(value=value):
                self.assertEqual(health.read_account(FakeLookup(value), 0),
                                 {'ok': False, 'code': 'invalid_response'})


class ProbeStateTests(unittest.TestCase):
    def test_a_failed_read_never_looks_like_a_healthy_account(self):
        probe = health.Probe(None)
        probe._store({'ok': True, 'connected': True, 'connection': 'online'})
        self.assertIs(probe.snapshot()['native_connected'], True)
        probe._store({'ok': False, 'code': 'transport_timeout'})
        value = probe.snapshot()
        self.assertIs(value['native_connected'], False)
        self.assertEqual(value['native_connection'], 'unreachable')
        self.assertEqual(value['native_probe_code'], 'transport_timeout')

    def test_unreadable_state_is_not_assumed_healthy(self):
        probe = health.Probe(None)
        self.assertIs(probe.snapshot()['native_connected'], False)
        probe._store({'ok': True, 'connected': False, 'connection': 'reconnecting'})
        self.assertEqual(probe.snapshot()['native_connection'], 'reconnecting')
        self.assertGreater(probe.snapshot()['native_checked_at'], 0)

    def test_snapshot_is_a_copy(self):
        probe = health.Probe(None)
        probe.snapshot()['native_connected'] = True
        self.assertIs(probe.snapshot()['native_connected'], False)


class ProbeExecutionTests(unittest.TestCase):
    """No namespace or Native session is touched here: only failure paths."""

    def test_unusable_namespace_handle_reports_instead_of_raising(self):
        class Pinned:
            netfd = -1

            def token(self):
                raise AssertionError('token must not be read without a namespace')

        self.assertEqual(health.probe_once(Pinned()), {'ok': False, 'code': 'probe_namespace_failed'})

    def test_standalone_mode_refuses_anything_but_an_exact_token_request(self):
        class Stdin:
            def __init__(self, payload):
                self.buffer = io.BytesIO(payload)

        for payload in (b'{"token":"x","extra":1}', b'{"secret":"x"}', b'not json', b'[]'):
            with self.subTest(payload=payload):
                with patch.object(sys, 'argv', ['probe', '--read']), \
                        patch.object(sys, 'stdin', Stdin(payload)):
                    self.assertEqual(health.main(), 2)

    def test_standalone_mode_requires_the_read_flag(self):
        class Stdin:
            buffer = io.BytesIO()

        with patch.object(sys, 'argv', ['probe']), patch.object(sys, 'stdin', Stdin()):
            self.assertEqual(health.main(), 2)


if __name__ == '__main__':
    unittest.main()
