"""An outage notice must never become a second outage.

The alert unit runs after systemd has already given up, so every path here has
to stay quiet and non-fatal: it reports what it can, and a missing token or an
unresolvable contact is an outcome to journal, not an exception to raise.
"""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('forwarder_alert_test', ROOT / 'scripts/forwarder_alert.py')
alert = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = alert
if Path(spec.origin).exists():
    spec.loader.exec_module(alert)

PEER = 'synthetic-peer'
THREAD = 'synthetic-thread'


def page(items):
    return json.dumps({'items': items}).encode()


def contact(display, identity, **extra):
    value = {'id': identity, 'username': identity, 'displayName': display, 'deleted': None,
             'remark': ''}
    value.update(extra)
    return value


class Resolution(unittest.TestCase):
    """A contains-match search must not decide which contact receives the page."""

    def resolve(self, payload):
        done = type('Done', (), {'returncode': 0, 'stdout': payload})()
        with patch.object(alert.subprocess, 'run', return_value=done):
            return alert.resolve(1, 'token', PEER)

    def test_exact_display_name_wins(self):
        self.assertEqual(self.resolve(page([contact(PEER, THREAD)])), THREAD)

    def test_substring_only_match_is_rejected(self):
        self.assertIsNone(self.resolve(page([contact(PEER + ' 小号', THREAD)])))

    def test_group_thread_is_never_a_private_alert_target(self):
        self.assertIsNone(self.resolve(page([contact(PEER, THREAD + '@chatroom')])))

    def test_official_account_prefix_is_rejected(self):
        self.assertIsNone(self.resolve(page([contact(PEER, 'gh_' + THREAD)])))

    def test_alias_without_matching_username_is_rejected(self):
        broken = contact(PEER, THREAD)
        broken['username'] = 'someone-else'
        self.assertIsNone(self.resolve(page([broken])))

    def test_deleted_contact_is_skipped(self):
        self.assertIsNone(self.resolve(page([contact(PEER, THREAD, deleted=True)])))

    def test_unreadable_page_is_not_an_exception(self):
        self.assertIsNone(self.resolve(b'not json'))


class Health(unittest.TestCase):
    """systemd owns the truth; the bridge's own last status file does not."""

    def state(self, value):
        done = type('Done', (), {'returncode': 0, 'stdout': (value + '\n').encode()})()
        with patch.object(alert.subprocess, 'run', return_value=done):
            return alert.active()

    def test_active_unit_is_recognised(self):
        self.assertEqual(self.state('active'), 'active')

    def test_failed_unit_is_not_mistaken_for_healthy(self):
        self.assertEqual(self.state('failed'), 'failed')

    def test_healthy_run_reports_without_sending(self):
        with patch.object(alert, 'active', return_value='active'), \
                patch.object(alert, 'send') as sending, \
                patch.object(alert, 'record') as recording:
            self.assertEqual(alert.main(), 0)
            sending.assert_not_called()
            self.assertEqual(recording.call_args[0][0]['code'], 'already_healthy')


class Delivery(unittest.TestCase):
    """A failed notice exits 0: one dead unit must not cascade into two."""

    def run_with(self, *, healthy='failed', pid=42, secret='token', thread=THREAD, reply=None):
        done = type('Done', (), {'returncode': 0,
                                 'stdout': json.dumps(reply if reply else {}).encode()})()
        with patch.object(alert, 'active', return_value=healthy), \
                patch.object(alert, 'setting', side_effect=lambda name, default=None:
                             {'privateContact': PEER, 'alertThread': thread}.get(name, default)), \
                patch.object(alert, 'native_pid', return_value=pid), \
                patch.object(alert, 'token', return_value=secret), \
                patch.object(alert, 'reason', return_value={'unit': 'x'}), \
                patch.object(alert, 'text', return_value='body'), \
                patch.object(alert, 'send', return_value=done) as sending, \
                patch.object(alert, 'record') as recording:
            code = alert.main()
        return code, sending, recording

    def test_accepted_send_is_reported_ok(self):
        code, _, recording = self.run_with(reply={'state': 'accepted', 'id': 'abc'})
        self.assertEqual(code, 0)
        self.assertTrue(recording.call_args[0][0]['ok'])

    def test_missing_native_daemon_still_exits_clean(self):
        code, sending, recording = self.run_with(pid=None)
        self.assertEqual(code, 0)
        sending.assert_not_called()
        self.assertEqual(recording.call_args[0][0]['code'], 'native_absent')

    def test_missing_token_still_exits_clean(self):
        code, sending, recording = self.run_with(secret=None)
        self.assertEqual(code, 0)
        sending.assert_not_called()
        self.assertEqual(recording.call_args[0][0]['code'], 'token_unavailable')

    def test_unresolvable_contact_asks_for_a_thread(self):
        code, sending, recording = self.run_with(thread=None)
        self.assertEqual(code, 0)
        sending.assert_not_called()
        self.assertEqual(recording.call_args[0][0]['code'], 'thread_required')

    def test_failure_to_send_does_not_raise(self):
        with patch.object(alert, 'active', return_value='failed'), \
                patch.object(alert, 'setting', side_effect=lambda name, default=None:
                             {'privateContact': PEER, 'alertThread': THREAD}.get(name, default)), \
                patch.object(alert, 'native_pid', return_value=42), \
                patch.object(alert, 'token', return_value='token'), \
                patch.object(alert, 'reason', return_value={}), \
                patch.object(alert, 'text', return_value='body'), \
                patch.object(alert, 'send', side_effect=OSError('gone')), \
                patch.object(alert, 'record'):
            self.assertEqual(alert.main(), 0)


class Units(unittest.TestCase):
    """The trigger is a drop-in: the forwarder's own unit must stay untouched."""

    def test_dropin_only_adds_onfailure(self):
        service, dropin = alert.unit_texts(Path('/opt/example/forwarder_alert.py'))
        self.assertIn('Type=oneshot', service)
        self.assertIn('ExecStart=/usr/bin/python3 -I -B "/opt/example/forwarder_alert.py"', service)
        self.assertEqual(dropin, '# Managed only by astrbot-wechat\n'
                                 '[Unit]\nOnFailure=astrbot-wechat-forwarder-alert.service\n')

    def test_render_reports_reason_without_sending(self):
        with patch.object(alert.sys, 'argv', ['forwarder_alert.py', '--render']), \
                patch.object(alert, 'reason', return_value={'unit': 'x'}), \
                patch.object(alert, 'active') as health, \
                patch.object(alert, 'send') as sending, \
                patch.object(alert, 'record') as recording:
            self.assertEqual(alert.main(), 0)
        health.assert_not_called()
        sending.assert_not_called()
        self.assertEqual(recording.call_args[0][0], {'unit': 'x'})


if __name__ == '__main__':
    unittest.main()
