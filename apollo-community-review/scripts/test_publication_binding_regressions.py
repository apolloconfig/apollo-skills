"""Offline regressions for original-publication binding and crash recovery."""
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

import community_review as cr
import discovery_state as ds
import publish_gate as gate
import test_publish_gate as fixtures
from test_publish_gate import NOW, URL


class PublicationBindingTests(unittest.TestCase):
    setUp = fixtures.PublishGateTest.setUp
    api = fixtures.PublishGateTest.api
    decision = fixtures.PublishGateTest.decision
    publish = fixtures.PublishGateTest.publish

    def record(self):
        return next(iter(ds.load_state(self.state)["publication_journal"].values()))

    def uncertain(self, decision):
        self.send.side_effect = TimeoutError("lost response")
        with self.assertRaises(TimeoutError):
            self.publish(decision)
        return self.send.call_args.args[1]

    def test_cached_success_returns_original_context_and_actor(self):
        decision = self.decision()
        original = self.publish(decision)
        changed = deepcopy(decision)
        changed["candidate"]["context_fingerprint"] = "new-context"
        changed["candidate"]["viewer_actor"] = "different-actor"
        self.assertEqual(gate.operation_id(decision), gate.operation_id(changed))
        cached = self.publish(changed)
        self.assertEqual(original["candidate"], cached["candidate"])
        self.assertTrue(gate.publication_matches(self.record(), cached))
        forged = {**changed, "comment_url": URL, "result_group": "auto-sent"}
        self.assertFalse(gate.publication_matches(self.record(), forged))
        self.assertEqual(1, self.send.call_count)

    def test_normal_and_reconciled_records_keep_full_original_snapshot(self):
        decision = self.decision()
        body = self.uncertain(decision)
        self.comments.append({"body": body, "user": {"login": "operator"}, "html_url": URL})
        changed = deepcopy(decision)
        changed["candidate"]["context_fingerprint"] = "newer"
        result = self.publish(changed)
        self.assertEqual(decision["candidate"], result["candidate"])
        self.assertEqual(decision["created_at"], self.record()["decision"]["created_at"])
        self.assertTrue(gate.publication_matches(self.record(), result))
        self.assertEqual(decision["candidate"], self.publish(changed)["candidate"])
        self.assertEqual(1, self.send.call_count)

    def test_legacy_sent_without_original_cannot_certify_new_snapshot(self):
        decision = self.decision()
        self.publish(decision)
        with ds.state_transaction(self.state) as state:
            del state["publication_journal"][cr.state_key(decision["candidate"])]["decision"]
        with self.assertRaisesRegex(ValueError, "original snapshot"):
            self.publish(decision)
        self.assertFalse(gate.publication_matches(self.record(), {**decision, "comment_url": URL}))
        self.assertEqual(1, self.send.call_count)

    def test_orphan_existing_marker_cannot_certify_current_snapshot(self):
        decision = self.decision()
        marker = f"<!-- apollo-community-review:{gate.operation_id(decision)} -->"
        with patch.object(gate, "find_post", return_value={"html_url": URL, "body": marker}):
            with self.assertRaisesRegex(ValueError, "original snapshot"):
                self.publish(decision)
        self.send.assert_not_called()

    def test_unresolved_write_blocks_manual_handoff_and_skip_acknowledgements(self):
        decision = self.decision()
        self.uncertain(decision)
        with ds.state_transaction(self.state) as state:
            unresolved = gate.reconcile_publications(state, state_path=self.state)
            self.assertIn(cr.state_key(decision["candidate"]), unresolved)
            for group in ("needs-review", "skipped", "auto-sent"):
                with self.assertRaisesRegex(ValueError, "uncertain"):
                    gate.require_acknowledgeable(state, {**decision, "result_group": group})
        self.assertEqual("uncertain", self.record()["status"])
        self.assertEqual(1, self.send.call_count)

    def test_reconciled_success_survives_later_scan_failure(self):
        decision = self.decision()
        body = self.uncertain(decision)
        self.comments.append({"body": body, "user": {"login": "operator"}, "html_url": URL})
        with self.assertRaisesRegex(RuntimeError, "scan failed"):
            with ds.state_transaction(self.state) as state:
                self.assertEqual({}, gate.reconcile_publications(state, state_path=self.state))
                state["discovery_cursors"][decision["repo"]] = "uncommitted"
                raise RuntimeError("scan failed")
        self.assertEqual("sent", self.record()["status"])
        self.assertNotIn(decision["repo"], ds.load_state(self.state)["discovery_cursors"])
        self.assertEqual(1, self.send.call_count)

    def test_reconciliation_requires_exact_marker_actor_action_and_head(self):
        decision = self.decision(pr=True, recommended_action="request changes", blocking=True,
                                 blocking_reasons=["The empty response still raises"], reviewed_head_sha="abc123",
                                 findings=[{"severity": "P1", "path": "src/client.py", "line": 12,
                                            "evidence": "Empty response raises ValueError", "impact": "Client cannot retry"}],
                                 validation_details="pytest tests/test_client.py::test_empty failed at line 12")
        body = self.uncertain(decision)
        valid = {"body": body, "user": {"login": "operator"}, "html_url": URL,
                 "state": "CHANGES_REQUESTED", "commit_id": "abc123"}
        for update in ({"body": body + " extra"}, {"user": {"login": "other"}},
                       {"state": "APPROVED"}, {"commit_id": "different"}):
            self.reviews = [{**valid, **update}]
            with ds.state_transaction(self.state) as state:
                self.assertTrue(gate.reconcile_publications(state, state_path=self.state))
            self.assertEqual("uncertain", self.record()["status"])
        self.reviews = [valid]
        with ds.state_transaction(self.state) as state:
            self.assertFalse(gate.reconcile_publications(state, state_path=self.state))
        self.assertEqual("sent", self.record()["status"])
        self.assertEqual(1, self.send.call_count)

    def test_revalidation_does_not_renew_review_timestamp(self):
        decision = self.decision()
        decision["created_at"] = cr.to_iso(NOW - timedelta(minutes=40))
        result = self.publish(decision)
        self.assertEqual(decision["created_at"], result["created_at"])
        self.assertEqual(decision["created_at"], self.record()["decision"]["created_at"])

    def test_review_expiring_during_fresh_reads_is_not_sent(self):
        decision = self.decision()
        original = gate.fresh_candidate
        def slow_read(candidate):
            result = original(candidate)
            self.utc_now_mock.return_value = NOW + timedelta(hours=2)
            return result
        with patch.object(gate, "fresh_candidate", side_effect=slow_read):
            with self.assertRaisesRegex(ValueError, "older than one hour"):
                self.publish(decision)
        self.send.assert_not_called()

    def test_review_expiring_while_persisting_intent_does_not_leave_uncertain_write(self):
        decision = self.decision()
        save = ds.atomic_save
        def slow_save(path, state):
            save(path, state)
            self.utc_now_mock.return_value = NOW + timedelta(hours=2)
        with patch.object(ds, "atomic_save", side_effect=slow_save):
            with self.assertRaisesRegex(ValueError, "older than one hour"):
                self.publish(decision)
        self.send.assert_not_called()
        self.assertFalse(ds.load_state(self.state)["publication_journal"])

    def test_review_expiring_during_duplicate_read_is_not_sent(self):
        decision = self.decision()
        def slow_lookup(*args):
            self.utc_now_mock.return_value = NOW + timedelta(hours=2)
            return None
        with patch.object(gate, "find_post", side_effect=slow_lookup):
            with self.assertRaisesRegex(ValueError, "older than one hour"):
                self.publish(decision)
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
