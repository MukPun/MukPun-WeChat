"""Retiring in-flight rows when only the worker died, not the controller.

Spool.__init__ retires 'publishing' rows once per process. An in-place worker
restart never re-runs it, because the controller keeps the same Spool: without
an explicit call the inbound row that was being written when the pipe broke
would stay 'publishing' forever, and the next restart of the whole process
would then be the only thing that retired it.
"""
import json
from pathlib import Path
import tempfile
import unittest

from wechat_bridge.durable import Spool


class Quarantine(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'spool.sqlite'
        self.state = Spool(self.path)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.state.close)

    def admitted(self, source, body='hello'):
        self.state.admit(source, {'body': body, 'type': 1})
        return source

    def test_rows_in_flight_become_uncertain(self):
        source = self.admitted('member:1')
        self.state.publishing(source)
        self.assertEqual(self.state.quarantine_publishing(), 1)
        self.assertEqual(self.state.counts(), {'uncertain': 1})

    def test_quarantine_keeps_the_payload_as_evidence(self):
        source = self.admitted('member:1')
        self.state.publishing(source)
        self.state.quarantine_publishing()
        row = self.state.db.execute('SELECT payload FROM inbox WHERE source=?', (source,)).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(json.loads(row[0])['body'], 'hello')

    def test_queued_rows_are_untouched(self):
        waiting = self.admitted('member:1')
        flying = self.admitted('member:2')
        self.state.publishing(flying)
        self.assertEqual(self.state.quarantine_publishing(), 1)
        self.assertEqual(self.state.counts(), {'queued': 1, 'uncertain': 1})
        self.assertEqual(self.state.next_pending()[0], waiting)

    def test_forwarded_rows_are_untouched(self):
        source = self.admitted('member:1')
        self.state.publishing(source)
        self.state.handoff(source, True)
        self.assertEqual(self.state.quarantine_publishing(), 0)
        self.assertEqual(self.state.counts(), {'forwarded': 1})

    def test_second_call_is_a_no_op(self):
        source = self.admitted('member:1')
        self.state.publishing(source)
        self.assertEqual(self.state.quarantine_publishing(), 1)
        self.assertEqual(self.state.quarantine_publishing(), 0)

    def test_a_fresh_construction_still_does_the_same_work(self):
        source = self.admitted('member:1')
        self.state.publishing(source)
        # The old path: the whole process died, so __init__ runs again.
        reopened = Spool(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.counts(), {'uncertain': 1})
        # And the supervisor's path reaches the same state without reopening.
        self.assertEqual(reopened.quarantine_publishing(), 0)

    def test_retired_rows_never_reach_the_llm_again(self):
        source = self.admitted('member:1')
        self.state.publishing(source)
        self.state.quarantine_publishing()
        self.assertIsNone(self.state.next_pending())


if __name__ == '__main__':
    unittest.main()
