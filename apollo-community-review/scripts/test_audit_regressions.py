"""Regression cases found by an independent review of the local proposal."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import community_review as cr
import discovery_state as ds
import test_publish_gate as publication_tests


class AuditRegressions(unittest.TestCase):
    def setUp(self):
        self.fixture = publication_tests.PublishGateTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def mark(self, candidate, decision):
        root = Path(self.fixture.temp.name)
        (root / "candidate.json").write_text(json.dumps(candidate))
        (root / "result.json").write_text(json.dumps(decision))
        args = argparse.Namespace(candidate_file=str(root / "candidate.json"),
                                  decision_file=str(root / "result.json"),
                                  state_file=str(self.fixture.state))
        with redirect_stdout(io.StringIO()):
            cr.command_mark_processed(args)

    def test_security_summary_never_interpolates_private_reasons(self):
        private = "synthetic confidential reproduction detail"
        item = {"thread_type": "issue", "thread_number": 1, "thread_url": private,
                "blocking_reasons": [private, "security-sensitive content; keep out of public workflow"],
                "draft_body": private}
        for group in ("needs-review", "skipped/error", "auto-sent"):
            with self.subTest(group=group):
                summary = cr.render_summary([{**item, "result_group": group}])
                self.assertNotIn(private, summary)
                self.assertIn("Security details withheld", summary)

    def test_skipped_pending_uncertain_and_error_results_do_not_disappear(self):
        for group in ("skipped", "pending", "uncertain", "error", "unknown-state"):
            summary = cr.render_summary([{"result_group": group, "thread_type": "issue", "thread_number": 1234}])
            self.assertIn("issue #1234", summary.split("## Skipped/Error", 1)[1])

    def test_empty_patch_and_inaccurate_totals_fail_closed(self):
        for changes in ({"patch": ""}, {"additions": 1000}, {"deletions": 1000}, {"additions": None}):
            with self.subTest(changes=changes):
                original = dict(self.fixture.files[0])
                self.fixture.files[0].update(changes)
                decision = self.fixture.decision(pr=True)
                self.assertFalse(decision["candidate"]["diff_complete"])
                self.assertFalse(decision["auto_send_eligible"])
                self.fixture.files[0] = original

    def test_truncated_hunk_context_fails_even_when_change_totals_match(self):
        patch = {"patch": "@@ -1,3 +1,3 @@\n-old\n+new", "additions": 1, "deletions": 1}
        self.assertFalse(cr.complete_file_patch(patch))
        patch["patch"] = "@@ -1,3 +1,3 @@\n before\n-old\n+new\n after"
        self.assertTrue(cr.complete_file_patch(patch))

    def test_new_file_zero_count_and_multiple_hunks(self):
        self.assertTrue(cr.complete_file_patch({"patch": "@@ -0,0 +1,2 @@\n+one\n+two", "additions": 2, "deletions": 0}))
        self.assertTrue(cr.complete_file_patch({"patch": "@@ -1 +1 @@\n-old\n+new\n@@ -10 +10 @@\n-a\n+b", "additions": 2, "deletions": 2}))

    def test_unrelated_manual_result_cannot_acknowledge_candidate(self):
        decision = self.fixture.decision()
        candidate = decision["candidate"]
        decision.update(result_group="needs-review", thread_number=999)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.mark(candidate, decision)

    def test_unconfirmed_or_wrong_url_cannot_acknowledge_auto_sent(self):
        decision = self.fixture.decision()
        candidate = decision["candidate"]
        decision.update(result_group="auto-sent", comment_url="https://example.com/unrelated")
        with self.assertRaisesRegex(ValueError, "publication journal"):
            self.mark(candidate, decision)

    def test_old_context_acknowledgement_cannot_remove_refreshed_queue(self):
        decision = self.fixture.decision()
        candidate = decision["candidate"]
        refreshed = {**candidate, "context_fingerprint": "newer-context"}
        with ds.state_transaction(self.fixture.state) as state:
            state["pending_candidates"][cr.state_key(candidate)] = refreshed
        decision["result_group"] = "needs-review"
        with self.assertRaisesRegex(ValueError, "snapshot is stale"):
            self.mark(candidate, decision)
        state = ds.load_state(self.fixture.state)
        ds.acknowledge_candidate(state, candidate)
        self.assertIn(cr.state_key(candidate), state["pending_candidates"])

    def test_old_completion_cannot_overwrite_newer_processed_signature(self):
        decision = self.fixture.decision()
        candidate = decision["candidate"]
        with ds.state_transaction(self.fixture.state) as state:
            state["threads"][cr.state_key(candidate)] = {"last_processed_signature": "newer", "last_processed_context_fingerprint": "newer-context"}
        decision["result_group"] = "needs-review"
        with self.assertRaisesRegex(ValueError, "newer completion"):
            self.mark(candidate, decision)
        self.assertEqual("newer", ds.load_state(self.fixture.state)["threads"][cr.state_key(candidate)]["last_processed_signature"])

    def test_matching_confirmed_journal_acknowledges_only_its_candidate(self):
        decision = self.fixture.decision()
        candidate = decision["candidate"]
        with ds.state_transaction(self.fixture.state) as state:
            state["pending_candidates"][cr.state_key(candidate)] = candidate
        result = self.fixture.publish(decision)
        self.mark(candidate, result)
        state = ds.load_state(self.fixture.state)
        self.assertNotIn(cr.state_key(candidate), state["pending_candidates"])
        self.assertEqual(candidate["activity_signature"], state["threads"][cr.state_key(candidate)]["last_processed_signature"])

    def test_future_commit_timestamp_cannot_suppress_manual_review(self):
        self.fixture.fetch_commit_timestamp_mock.return_value = ("2099-01-01T00:00:00Z", "contributor")
        decision = self.fixture.decision(pr=True)
        self.assertTrue(decision["candidate"]["activity_time_uncertainty"])
        self.assertFalse(decision["auto_send_eligible"])

    def test_in_range_commit_date_cannot_override_a_maintainer_reply(self):
        self.fixture.fetch_commit_timestamp_mock.return_value = ("2026-11-08T11:59:00Z", "contributor")
        self.fixture.comments.append({"id": 81, "body": "I am handling this", "created_at": "2026-11-08T11:50:00Z",
                                      "user": {"login": "maintainer", "type": "User"}})
        decision = self.fixture.decision(pr=True)
        self.assertTrue(decision["candidate"]["activity_time_uncertainty"])
        self.assertFalse(decision["auto_send_eligible"])

    def test_pending_snapshot_is_refreshed_despite_old_external_timestamp(self):
        candidate = self.fixture.decision()["candidate"]
        state = {"pending_candidates": {cr.state_key(candidate): candidate}, "threads": {},
                 "thread_observations": {cr.state_key(candidate): {"root_fingerprint": candidate["root_fingerprint"], "head_sha": None}}}
        self.fixture.comments.append({"id": 80, "body": "automation update", "created_at": "2026-11-08T11:00:00Z",
                                      "user": {"login": "stale[bot]", "type": "Bot"}})
        original_api = self.fixture.api
        for listed in ([], [self.fixture.issue]):
            def api(endpoint, **kwargs):
                if endpoint.split("?")[0] == f"repos/{publication_tests.REPO}/issues":
                    return list(listed)
                return original_api(endpoint, **kwargs)
            with self.subTest(listed=bool(listed)), patch.object(cr, "gh_api_json", side_effect=api):
                refreshed = cr.discover_candidates(publication_tests.REPO, actor_login="operator", maintainers={"operator"},
                                                   state=state, initial_lookback_hours=24, since="2026-11-08T11:30:00Z")
            self.assertEqual(1, len(refreshed))
            self.assertNotEqual(candidate["context_fingerprint"], refreshed[0]["context_fingerprint"])


if __name__ == "__main__":
    unittest.main()
