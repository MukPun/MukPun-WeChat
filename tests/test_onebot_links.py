"""Share-card input stays a text relay behind an explicit opt-in switch.

Native projects a type-49 share card to a body of
"[Link] title / description / url" (wechat-core links.rs), so the bridge only
has to admit it, never to fetch or rebuild it. These tests pin the policy half:
an upgraded configuration must not start relaying cards on its own.
"""
import json
import unittest

from wechat_bridge.forward_config import DEFAULT, read, validate


class LinkPolicy(unittest.TestCase):
    def test_old_configs_keep_links_disabled_until_configured(self):
        value = read(json.dumps({k: v for k, v in DEFAULT.items() if k != 'linkInputEnabled'}).encode())
        self.assertIs(value['linkInputEnabled'], False)
        self.assertEqual(set(value), set(DEFAULT))

    def test_link_switch_accepts_only_booleans(self):
        self.assertIs(validate({**DEFAULT, 'linkInputEnabled': True})['linkInputEnabled'], True)
        self.assertIs(validate({**DEFAULT, 'linkInputEnabled': False})['linkInputEnabled'], False)
        for bad in (1, 0, 'true', None):
            with self.assertRaises(ValueError):
                validate({**DEFAULT, 'linkInputEnabled': bad})

    def test_link_switch_is_not_reachable_under_a_worker_only_name(self):
        with self.assertRaises(ValueError):
            validate({**DEFAULT, 'linkInput': True})

    def test_default_payload_round_trips(self):
        self.assertEqual(read(json.dumps(DEFAULT).encode()), DEFAULT)
