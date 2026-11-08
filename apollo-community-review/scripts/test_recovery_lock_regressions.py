"""Recovery must not release the writer lock before discovery or acknowledgement."""
from contextlib import contextmanager
from unittest.mock import patch
import unittest

import community_review as cr
import discovery_state as ds
import test_feedback_discovery_regressions as fixtures


class RecoveryLockRegressions(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.FeedbackDiscoveryRegressions(methodName="runTest")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.original = self.helper.world.decision()
        self.helper.save_pending(self.original["candidate"])

    def assert_one_locked_recovery(self, operation):
        real_transaction = ds.state_transaction
        real_recover = cr.recover_publications_in_state
        active = False
        entries = 0
        recoveries = 0

        @contextmanager
        def tracked_transaction(path):
            nonlocal active, entries
            self.assertFalse(active, "nested state lock")
            entries += 1
            with real_transaction(path) as state:
                active = True
                try:
                    yield state
                finally:
                    active = False

        def tracked_recover(*args, **kwargs):
            nonlocal recoveries
            self.assertTrue(active, "recovery must run under the final operation's lock")
            recoveries += 1
            return real_recover(*args, **kwargs)

        with patch.object(ds, "state_transaction", tracked_transaction), \
                patch.object(cr, "recover_publications_in_state", tracked_recover):
            operation()
        self.assertEqual(1, entries)
        self.assertEqual(1, recoveries)

    def test_scan_recovers_and_discovers_under_one_continuous_lock(self):
        self.helper.world.publish(self.original)
        self.helper.accepted_post()
        self.assert_one_locked_recovery(lambda: self.assertEqual([], self.helper.scan()))
        state = ds.load_state(self.helper.world.state)
        key = cr.state_key(self.original["candidate"])
        self.assertEqual(self.original["candidate"]["context_fingerprint"],
                         state["threads"][key]["last_processed_context_fingerprint"])
        self.assertNotIn(key, state["pending_candidates"])
        self.assertEqual(1, self.helper.world.send.call_count)

    def test_mark_processed_recovers_and_acknowledges_under_one_continuous_lock(self):
        result = self.helper.world.publish(self.original)
        self.assert_one_locked_recovery(lambda: self.helper.mark(self.original["candidate"], result))
        state = ds.load_state(self.helper.world.state)
        self.assertEqual("auto-sent", state["threads"][cr.state_key(self.original["candidate"])]["last_result_group"])
        self.assertEqual(1, self.helper.world.send.call_count)

    def test_publisher_winning_before_scan_lock_is_recovered_before_refresh(self):
        real_transaction = ds.state_transaction
        inserted = False

        @contextmanager
        def publish_before_scan_lock(path):
            nonlocal inserted
            if not inserted:
                inserted = True
                self.helper.world.publish(self.original)
                self.helper.accepted_post()
            with real_transaction(path) as state:
                yield state

        with patch.object(ds, "state_transaction", publish_before_scan_lock):
            self.assertEqual([], self.helper.scan())
        state = ds.load_state(self.helper.world.state)
        key = cr.state_key(self.original["candidate"])
        self.assertEqual(self.original["activity_signature"], state["threads"][key]["last_processed_signature"])
        self.assertFalse(state["publication_journal"][key]["recovery_pending"])
        self.assertEqual(1, len(state["publication_results"]))
        self.assertEqual(1, self.helper.world.send.call_count)


if __name__ == "__main__":
    unittest.main()
