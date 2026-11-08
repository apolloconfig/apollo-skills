"""Offline crash regressions for confirmed writes whose caller loses the result."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import community_review as cr
import discovery_state as ds
import publish_gate as gate
import test_feedback_discovery_regressions as discovery_fixtures
import test_publish_gate as fixtures


class SuccessfulSendRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.discovery = discovery_fixtures.FeedbackDiscoveryRegressions(methodName="runTest")
        self.discovery.setUp()
        self.addCleanup(self.discovery.doCleanups)
        self.world = self.discovery.world
        self.original = self.world.decision()
        self.key = cr.state_key(self.original["candidate"])
        self.operation = gate.operation_id(self.original)
        self.discovery.save_pending(self.original["candidate"])

    def results(self):
        output = io.StringIO()
        args = argparse.Namespace(repo=fixtures.REPO, actor="operator",
                                  state_file=str(self.world.state), since=None)
        with redirect_stdout(output):
            cr.command_scan_results(args)
        return json.loads(output.getvalue())

    def assert_confirmed(self, state, *, recovery_pending):
        record = state["publication_journal"][self.key]
        self.assertEqual("sent", record["status"])
        self.assertEqual(fixtures.URL, record["url"])
        self.assertIs(recovery_pending, record["recovery_pending"])
        self.assertEqual(self.original, record["decision"])
        self.assertEqual({self.operation}, set(state["publication_results"]))
        result = state["publication_results"][self.operation]["result"]
        self.assertEqual({**self.original, "result_group": "auto-sent", "comment_url": fixtures.URL},
                         {key: value for key, value in result.items() if key != "deduplicated"})
        self.assertEqual("auto-sent", result["result_group"])
        self.assertEqual(fixtures.URL, result["comment_url"])
        self.assertTrue(gate.publication_matches(record, result))
        self.assertEqual([result], self.results())
        self.assertEqual(1, self.world.send.call_count)
        return result

    def assert_original_acknowledged(self, state):
        self.assertNotIn(self.key, state["pending_candidates"])
        processed = state["threads"][self.key]
        self.assertEqual(self.original["activity_signature"], processed["last_processed_signature"])
        self.assertEqual(self.original["candidate"]["context_fingerprint"],
                         processed["last_processed_context_fingerprint"])

    def test_lost_success_stdout_recovers_before_scan_refresh_and_accepts_original_ack(self):
        decision_file = Path(self.world.temp.name) / "decision.json"
        decision_file.write_text(json.dumps(self.original))
        args = argparse.Namespace(decision_file=str(decision_file), state_file=str(self.world.state), dry_run=False)
        with patch("builtins.print", side_effect=BrokenPipeError("stdout closed")), self.assertRaises(BrokenPipeError):
            cr.command_post_comment(args)
        confirmed = ds.load_state(self.world.state)
        original_result = self.assert_confirmed(confirmed, recovery_pending=True)
        self.assertEqual(self.original["candidate"], confirmed["pending_candidates"][self.key])
        self.assertNotIn(self.key, confirmed["threads"])
        self.discovery.accepted_post()
        self.assertEqual([], self.discovery.scan())
        recovered = ds.load_state(self.world.state)
        self.assert_confirmed(recovered, recovery_pending=False)
        self.assert_original_acknowledged(recovered)
        self.discovery.mark(self.original["candidate"], original_result)
        self.discovery.mark(self.original["candidate"], original_result)
        self.assertEqual(recovered, ds.load_state(self.world.state))

    def test_interruption_before_confirmation_replace_leaves_reconcilable_intent(self):
        replace = ds.os.replace
        replacements = 0

        def interrupt_confirmation(source, target):
            nonlocal replacements
            replacements += 1
            if replacements == 2:
                # Confirmation is one full state write, never a sent-only state.
                pending_write = json.loads(Path(source).read_text())
                self.assertTrue(pending_write["publication_journal"][self.key]["recovery_pending"])
                self.assertEqual(self.original["candidate"],
                                 pending_write["publication_results"][self.operation]["result"]["candidate"])
                raise OSError("interrupted before confirmation replace")
            return replace(source, target)

        with patch.object(ds.os, "replace", side_effect=interrupt_confirmation), self.assertRaisesRegex(OSError, "confirmation replace"):
            self.world.publish(self.original)
        interrupted = ds.load_state(self.world.state)
        self.assertEqual("pending", interrupted["publication_journal"][self.key]["status"])
        self.assertNotIn("publication_results", interrupted)
        self.discovery.accepted_post()
        self.assertEqual([], self.discovery.scan())
        recovered = ds.load_state(self.world.state)
        self.assert_confirmed(recovered, recovery_pending=False)
        self.assert_original_acknowledged(recovered)

    def test_interruption_after_confirmation_save_keeps_result_and_resumes_without_remote_read(self):
        save = ds.atomic_save

        def interrupt_after_save(path, state):
            save(path, state)
            if state["publication_journal"][self.key]["status"] == "sent":
                raise RuntimeError("interrupted after confirmation save")

        with patch.object(ds, "atomic_save", side_effect=interrupt_after_save), self.assertRaisesRegex(RuntimeError, "confirmation save"):
            self.world.publish(self.original)
        self.assert_confirmed(ds.load_state(self.world.state), recovery_pending=True)
        with patch.object(gate, "find_post", side_effect=AssertionError("confirmed write needs no remote lookup")):
            cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        recovered = ds.load_state(self.world.state)
        self.assert_confirmed(recovered, recovery_pending=False)
        self.assert_original_acknowledged(recovered)

    def test_interrupted_queue_recovery_leaves_confirmation_and_ack_flag_atomic(self):
        self.world.publish(self.original)
        confirmed = ds.load_state(self.world.state)
        with patch.object(gate, "require_acknowledgeable", side_effect=RuntimeError("ACK interrupted")), self.assertRaises(RuntimeError):
            cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        self.assertEqual(confirmed, ds.load_state(self.world.state))
        self.assert_confirmed(confirmed, recovery_pending=True)
        cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        recovered = ds.load_state(self.world.state)
        self.assert_confirmed(recovered, recovery_pending=False)
        self.assert_original_acknowledged(recovered)

    def test_interrupted_ack_rolls_back_processed_state_and_queue_removal_together(self):
        self.world.publish(self.original)
        confirmed = ds.load_state(self.world.state)
        acknowledge = ds.acknowledge_candidate

        def interrupt_ack(state, candidate):
            acknowledge(state, candidate)
            self.assert_original_acknowledged(state)
            raise RuntimeError("interrupted before recovery commit")

        with patch.object(ds, "acknowledge_candidate", side_effect=interrupt_ack), self.assertRaises(RuntimeError):
            cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        self.assertEqual(confirmed, ds.load_state(self.world.state))
        self.assert_confirmed(confirmed, recovery_pending=True)
        cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        recovered = ds.load_state(self.world.state)
        self.assert_confirmed(recovered, recovery_pending=False)
        self.assert_original_acknowledged(recovered)

    def test_later_failed_scan_keeps_confirmation_and_original_ack_without_advancing_cursor(self):
        self.world.publish(self.original)
        before = ds.load_state(self.world.state)
        self.discovery.accepted_post()
        with patch.object(cr, "discover_candidates", side_effect=RuntimeError("discovery failed")), self.assertRaises(RuntimeError):
            self.discovery.scan()
        recovered = ds.load_state(self.world.state)
        self.assert_confirmed(recovered, recovery_pending=False)
        self.assert_original_acknowledged(recovered)
        self.assertEqual(before["discovery_cursors"], recovered["discovery_cursors"])

    def test_recovery_cannot_acknowledge_newer_context_with_same_activity(self):
        self.world.publish(self.original)
        self.world.comments.append({"id": 90, "body": "bot update", "created_at": "2026-11-08T11:00:00Z",
                                    "user": {"login": "ci[bot]", "type": "Bot"}})
        newer = self.world.decision()["candidate"]
        self.assertEqual(self.original["activity_signature"], newer["activity_signature"])
        self.assertNotEqual(self.original["candidate"]["context_fingerprint"], newer["context_fingerprint"])
        self.discovery.save_pending(newer)
        cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        recovered = ds.load_state(self.world.state)
        result = self.assert_confirmed(recovered, recovery_pending=False)
        self.assertEqual(newer, recovered["pending_candidates"][self.key])
        self.assertNotIn(self.key, recovered["threads"])
        with self.assertRaisesRegex(ValueError, "stale"):
            self.discovery.mark(self.original["candidate"], result)
        with self.assertRaisesRegex(ValueError, "context does not match"):
            self.discovery.mark(newer, result)
        self.assertEqual(recovered, ds.load_state(self.world.state))

    def test_recovery_cannot_acknowledge_newer_activity(self):
        self.world.publish(self.original)
        self.world.issue["body"] = "New reproduction details"
        newer = self.world.decision()["candidate"]
        self.assertNotEqual(self.original["activity_signature"], newer["activity_signature"])
        self.discovery.save_pending(newer)
        cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        recovered = ds.load_state(self.world.state)
        result = self.assert_confirmed(recovered, recovery_pending=False)
        self.assertEqual(newer, recovered["pending_candidates"][self.key])
        self.assertNotIn(self.key, recovered["threads"])
        with self.assertRaisesRegex(ValueError, "stale"):
            self.discovery.mark(self.original["candidate"], result)

    def test_repeated_publish_and_recovery_keep_one_original_result_and_timestamp(self):
        sent = self.world.publish(self.original)
        first_outbox = deepcopy(ds.load_state(self.world.state)["publication_results"])
        self.world.utc_now_mock.return_value = fixtures.NOW + timedelta(hours=2)
        for _ in range(2):
            cached = self.world.publish(self.original)
            self.assertEqual({**sent, "deduplicated": True}, cached)
            cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        recovered = ds.load_state(self.world.state)
        self.assert_confirmed(recovered, recovery_pending=False)
        self.assert_original_acknowledged(recovered)
        self.assertEqual(first_outbox, recovered["publication_results"])

    def test_later_publication_retains_previous_operation_result(self):
        first_result = self.world.publish(self.original)
        first_outbox = deepcopy(ds.load_state(self.world.state)["publication_results"][self.operation])
        self.world.issue["body"] = "New reproduction details"
        newer = self.world.decision()
        newer_operation = gate.operation_id(newer)
        self.assertNotEqual(self.operation, newer_operation)
        self.discovery.save_pending(newer["candidate"])
        new_url = f"https://github.com/{fixtures.REPO}/issues/1#issuecomment-10"
        self.world.send.return_value = {"html_url": new_url}
        new_result = self.world.publish(newer)
        cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        recovered = ds.load_state(self.world.state)
        self.assertEqual({self.operation, newer_operation}, set(recovered["publication_results"]))
        self.assertEqual(first_outbox, recovered["publication_results"][self.operation])
        self.assertCountEqual([first_result, new_result], self.results())
        self.assertEqual(newer_operation, recovered["publication_journal"][self.key]["operation_id"])
        self.assertEqual(newer["activity_signature"], recovered["threads"][self.key]["last_processed_signature"])
        self.assertNotIn(self.key, recovered["pending_candidates"])
        self.assertEqual(2, self.world.send.call_count)

    def test_supported_request_changes_uses_same_durable_confirmation(self):
        self.original = self.world.decision(
            pr=True, recommended_action="request changes", blocking=True,
            blocking_reasons=["The empty response still raises"], reviewed_head_sha="abc123",
            findings=[{"severity": "P1", "path": "src/client.py", "line": 12,
                       "evidence": "Empty response raises ValueError", "impact": "Client cannot retry"}],
            validation_details="pytest tests/test_client.py::test_empty failed at line 12")
        self.key = cr.state_key(self.original["candidate"])
        self.operation = gate.operation_id(self.original)
        self.discovery.save_pending(self.original["candidate"])
        self.world.publish(self.original)
        self.assert_confirmed(ds.load_state(self.world.state), recovery_pending=True)
        cr.recover_publication_state(self.world.state, key=self.key)
        recovered = ds.load_state(self.world.state)
        self.assert_confirmed(recovered, recovery_pending=False)
        self.assert_original_acknowledged(recovered)

    def test_dry_run_creates_no_confirmation_or_result(self):
        result = self.world.publish(self.original, dry_run=True)
        self.assertEqual("dry-run", result["result_group"])
        state = ds.load_state(self.world.state)
        self.assertFalse(state.get("publication_journal"))
        self.assertFalse(state.get("publication_results"))
        self.assertEqual(self.original["candidate"], state["pending_candidates"][self.key])
        self.world.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
