"""Offline reproductions for PR #9 discovery, ordering, and recovery feedback."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import community_review as cr
import discovery_state as ds
import publish_gate as gate
import test_publish_gate as fixtures


class FeedbackDiscoveryRegressions(unittest.TestCase):
    def setUp(self):
        self.world = fixtures.PublishGateTest(methodName="runTest")
        self.world.setUp()
        self.addCleanup(self.world.doCleanups)
        self.args = argparse.Namespace(repo=fixtures.REPO, actor="operator", maintainers="maintainer",
                                       state_file=str(self.world.state), initial_lookback_hours=24)

    def api_with_list(self, listed=None, errors=None):
        listed = [self.world.issue] if listed is None else listed
        original = self.world.api
        def api(endpoint, **kwargs):
            path = endpoint.split("?")[0]
            if path == f"repos/{fixtures.REPO}/issues":
                return deepcopy(listed)
            if errors and path in errors:
                raise errors[path]
            if path == f"repos/{fixtures.REPO}/issues/2":
                return deepcopy(next(item for item in listed if item["number"] == 2))
            if path == f"repos/{fixtures.REPO}/issues/2/comments":
                return []
            return original(endpoint, **kwargs)
        return api

    def scan(self, **kwargs):
        output = io.StringIO()
        with patch.object(cr, "gh_api_json", side_effect=self.api_with_list(**kwargs)), redirect_stdout(output):
            cr.command_scan(self.args)
        return json.loads(output.getvalue())

    def save_pending(self, candidate):
        with ds.state_transaction(self.world.state) as state:
            ds.commit_discovery(state, fixtures.REPO, [candidate], started_at=fixtures.NOW - timedelta(hours=12))

    def discover(self, state, since="2026-11-08T11:30:00Z", listed=None):
        with patch.object(cr, "gh_api_json", side_effect=self.api_with_list(listed=listed)):
            return cr.discover_candidates(fixtures.REPO, actor_login="operator", maintainers={"operator", "maintainer"},
                                         state=state, initial_lookback_hours=24, since=since)

    def mark(self, candidate, result):
        root = Path(self.world.temp.name)
        (root / "candidate.json").write_text(json.dumps(candidate))
        (root / "result.json").write_text(json.dumps(result))
        args = argparse.Namespace(candidate_file=str(root / "candidate.json"), decision_file=str(root / "result.json"),
                                  state_file=str(self.world.state))
        with redirect_stdout(io.StringIO()):
            cr.command_mark_processed(args)

    def test_confirmed_deleted_pending_does_not_block_unrelated_discovery(self):
        candidate = self.world.decision()["candidate"]
        self.save_pending(candidate)
        other = {**self.world.issue, "number": 2, "id": 102}
        error = subprocess.CalledProcessError(1, ["gh", "api"], stderr="gh: Gone (HTTP 410)")
        found = self.scan(listed=[other], errors={f"repos/{fixtures.REPO}/issues/1": error})
        state = ds.load_state(self.world.state)
        self.assertEqual([2], [item["number"] for item in found])
        self.assertEqual("skipped", state["discovery_results"][cr.state_key(candidate)]["result_group"])
        self.assertEqual(cr.to_iso(fixtures.NOW), state["discovery_cursors"][fixtures.REPO])

    def test_ambiguous_not_found_auth_and_transient_errors_still_roll_back(self):
        candidate = self.world.decision()["candidate"]
        self.save_pending(candidate)
        before = self.world.state.read_bytes()
        other = {**self.world.issue, "number": 2, "id": 102}
        for status in (404, 403, 500):
            error = subprocess.CalledProcessError(1, ["gh", "api"], stderr=f"gh: failure (HTTP {status})")
            with self.subTest(status=status), self.assertRaises(subprocess.CalledProcessError):
                self.scan(listed=[other], errors={f"repos/{fixtures.REPO}/issues/1": error})
            self.assertEqual(before, self.world.state.read_bytes())

    def test_reverted_signature_discards_superseded_pending_in_both_branches(self):
        original = self.world.decision()["candidate"]
        self.world.issue["body"] = "temporary edited body"
        edited = self.world.decision()["candidate"]
        self.world.issue["body"] = original["body"]
        key = cr.state_key(original)
        for observed in (original, edited):
            state = {"threads": {key: {"last_processed_signature": original["activity_signature"]}},
                     "pending_candidates": {key: edited}, "thread_observations": {key: {
                         "root_fingerprint": observed["root_fingerprint"], "head_sha": None, "draft": False}}}
            found = self.discover(state)
            ds.commit_discovery(state, fixtures.REPO, found, started_at=fixtures.NOW)
            self.assertEqual([], found)
            self.assertNotIn(key, state["pending_candidates"])
            self.assertEqual(original["activity_signature"], state["threads"][key]["last_processed_signature"])

    def test_commit_discovery_also_prunes_verified_processed_signature(self):
        candidate = self.world.decision()["candidate"]
        key = cr.state_key(candidate)
        state = {"threads": {key: {"last_processed_signature": candidate["activity_signature"]}},
                 "pending_candidates": {key: {**candidate, "activity_signature": "superseded"}}}
        ds.commit_discovery(state, fixtures.REPO, [candidate], started_at=fixtures.NOW)
        self.assertNotIn(key, state["pending_candidates"])

    def test_draft_to_ready_is_rediscovered_inside_and_outside_overlap(self):
        self.world.pr["draft"] = True
        draft = self.world.decision(pr=True)
        state = {}
        candidates = self.discover(state, since="2026-11-08T09:00:00Z")
        ds.commit_discovery(state, fixtures.REPO, candidates, started_at=fixtures.NOW)
        cr.mark_processed(state, draft["candidate"], draft)
        ds.acknowledge_candidate(state, draft["candidate"])
        self.world.pr["draft"] = False
        self.world.pr["updated_at"] = "2026-11-08T11:55:00Z"
        for since in ("2026-11-08T09:00:00Z", "2026-11-08T11:30:00Z"):
            with self.subTest(since=since):
                found = self.discover(deepcopy(state), since=since)
                self.assertEqual(1, len(found))
                self.assertFalse(found[0]["draft"])
                self.assertNotEqual(draft["activity_signature"], found[0]["activity_signature"])

    def test_equal_commit_and_maintainer_timestamps_require_hold(self):
        self.world.fetch_commit_timestamp_mock.return_value = ("2026-11-08T11:50:00Z", "contributor")
        self.world.comments.append({"id": 81, "body": "I am handling this", "created_at": "2026-11-08T11:50:00Z",
                                    "user": {"login": "maintainer", "type": "User"}})
        decision = self.world.decision(pr=True)
        self.assertTrue(decision["candidate"]["activity_time_uncertainty"])
        self.assertFalse(decision["auto_send_eligible"])
        self.world.comments.append({"id": 82, "body": "New reproduction", "created_at": "2026-11-08T11:50:01Z",
                                    "user": {"login": "contributor", "type": "User"}})
        decision = self.world.decision(pr=True)
        self.assertFalse(decision["candidate"]["activity_time_uncertainty"])
        self.assertTrue(decision["auto_send_eligible"])

    def test_list_detail_shape_is_hardened_without_assuming_live_difference(self):
        list_item = deepcopy(self.world.issue)
        self.world.issue["closed_by"] = None  # Synthetic compatibility fixture only.
        candidate = self.discover({}, since="2026-11-08T09:00:00Z", listed=[list_item])[0]
        decision = cr.build_decision(candidate, fixtures.review_text())
        self.assertEqual("dry-run", self.world.publish(decision, dry_run=True)["result_group"])
        self.world.send.assert_not_called()

    def uncertain_send(self):
        decision = self.world.decision()
        self.save_pending(decision["candidate"])
        self.world.send.side_effect = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.world.publish(decision)
        return decision

    def accepted_post(self):
        self.world.comments.append({"id": 200, "body": self.world.send.call_args.args[1],
                                    "created_at": "2026-11-08T11:59:00Z", "html_url": fixtures.URL,
                                    "user": {"login": "operator", "type": "User"}})

    def test_accepted_timeout_is_reconciled_before_scan_refresh(self):
        original = self.uncertain_send()
        self.accepted_post()
        found = self.scan()
        state = ds.load_state(self.world.state)
        self.assertEqual([], found)
        self.assertEqual("sent", state["publication_journal"][cr.state_key(original["candidate"])]["status"])
        recovered = list(state["publication_results"].values())[0]["result"]
        self.assertEqual(original["candidate"]["context_fingerprint"], recovered["candidate"]["context_fingerprint"])
        self.assertEqual(fixtures.URL, recovered["comment_url"])
        self.assertEqual(1, self.world.send.call_count)

    def test_unresolved_send_stays_queued_and_cannot_be_acknowledged(self):
        original = self.uncertain_send()
        found = self.scan()
        self.assertTrue(found[0]["publication_hold"])
        decision = cr.build_decision(found[0], fixtures.review_text())
        self.assertFalse(decision["auto_send_eligible"])
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.mark(found[0], decision)
        self.assertIn(cr.state_key(original["candidate"]), ds.load_state(self.world.state)["pending_candidates"])
        self.assertEqual(1, self.world.send.call_count)

    def test_recovery_is_durable_even_when_later_discovery_fails(self):
        original = self.uncertain_send()
        self.accepted_post()
        with patch.object(cr, "discover_candidates", side_effect=RuntimeError("scan interrupted")), self.assertRaises(RuntimeError):
            self.scan()
        state = ds.load_state(self.world.state)
        self.assertEqual("sent", state["publication_journal"][cr.state_key(original["candidate"])]["status"])
        self.assertEqual(1, len(state["publication_results"]))

    def test_recovery_resumes_after_confirmation_saved_before_outbox_ack(self):
        original = self.uncertain_send()
        self.accepted_post()
        with patch.object(gate, "require_acknowledgeable", side_effect=RuntimeError("interrupted")), self.assertRaises(RuntimeError):
            cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        key = cr.state_key(original["candidate"])
        interrupted = ds.load_state(self.world.state)
        self.assertEqual("sent", interrupted["publication_journal"][key]["status"])
        self.assertTrue(interrupted["publication_journal"][key]["recovery_pending"])
        self.assertEqual(1, len(interrupted["publication_results"]))
        cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        recovered = ds.load_state(self.world.state)
        self.assertFalse(recovered["publication_journal"][key]["recovery_pending"])
        self.assertEqual(1, len(recovered["publication_results"]))
        self.assertNotIn(key, recovered["pending_candidates"])
        self.assertEqual(1, self.world.send.call_count)

    def test_direct_publisher_reconciliation_keeps_original_result_outside_journal_slot(self):
        original = self.uncertain_send()
        self.accepted_post()
        recovered = self.world.publish(original)
        self.assertEqual(original["candidate"], recovered["candidate"])
        old_operation = gate.operation_id(original)
        with ds.state_transaction(self.world.state) as state:
            state["publication_journal"][cr.state_key(original["candidate"])] = {
                "status": "pending", "operation_id": "later-operation"}
        state = ds.load_state(self.world.state)
        self.assertEqual(original["candidate"], state["publication_results"][old_operation]["result"]["candidate"])
        self.assertEqual(1, self.world.send.call_count)

    def test_recovery_does_not_acknowledge_a_newer_queued_snapshot(self):
        original = self.uncertain_send()
        self.world.comments.append({"id": 90, "body": "bot update", "created_at": "2026-11-08T11:00:00Z",
                                    "user": {"login": "ci[bot]", "type": "Bot"}})
        newer = self.world.decision()["candidate"]
        self.save_pending(newer)
        self.accepted_post()
        cr.recover_publication_state(self.world.state, repo=fixtures.REPO)
        state = ds.load_state(self.world.state)
        self.assertEqual(newer["context_fingerprint"], state["pending_candidates"][cr.state_key(newer)]["context_fingerprint"])
        self.assertNotEqual(original["candidate"]["context_fingerprint"], newer["context_fingerprint"])

    def test_cached_success_cannot_acknowledge_refreshed_context(self):
        original = self.world.decision()
        self.world.publish(original)
        self.world.comments.append({"id": 90, "body": "bot update", "created_at": "2026-11-08T11:00:00Z",
                                    "user": {"login": "ci[bot]", "type": "Bot"}})
        newer = self.world.decision()
        self.save_pending(newer["candidate"])
        result = self.world.publish(newer)
        self.assertEqual(original["candidate"], result["candidate"])
        with self.assertRaisesRegex(ValueError, "context does not match"):
            self.mark(newer["candidate"], result)
        self.assertIn(cr.state_key(newer["candidate"]), ds.load_state(self.world.state)["pending_candidates"])


if __name__ == "__main__":
    unittest.main()
