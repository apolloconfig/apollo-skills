"""Offline integration edges for discovery, policy, acknowledgement and reports."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import community_review as cr
import discovery_state as ds
import publish_gate
import test_publish_gate as fixtures

REPO = fixtures.REPO
NOW = fixtures.NOW


class PolicyIntegrationTest(unittest.TestCase):
    def setUp(self):
        # Reuse the fully mocked API world, without inheriting or collecting its tests.
        self.world = fixtures.PublishGateTest()
        self.world.setUp()
        self.addCleanup(self.world.doCleanups)

    def test_minimal_information_request_accepts_one_to_three_fields(self):
        for count in (1, 2, 3):
            decision = self.world.decision(request_info_only=True,
                                           missing_info_fields=[f"reproduction detail {i}" for i in range(count)],
                                           draft_body="Could you share these reproduction details?")
            self.assertTrue(decision["auto_send_eligible"], decision["blocking_reasons"])
            self.assertEqual("ready-to-send", decision["result_group"])

    def test_missing_information_still_holds_large_blocking_or_unvalidated_request(self):
        base = {"request_info_only": True, "missing_info_fields": ["version"],
                "draft_body": "Which version was used?"}
        for change in ({"missing_info_fields": ["a", "b", "c", "d"]}, {"blocking": True},
                       {"validation_completed": False}, {"request_info_only": False},
                       {"evidence": []}, {"draft_body": "x" * 1201}):
            with self.subTest(change=change):
                decision = self.world.decision(**{**base, **change})
                self.assertFalse(decision["auto_send_eligible"])

    def test_decoded_unicode_security_output_is_blocked(self):
        decision = self.world.decision(draft_body="这里存在漏洞，需要私下处理。")
        self.assertIn("\\u", decision["review_text"])
        self.assertFalse(decision["auto_send_eligible"])
        self.assertTrue(any("security-sensitive" in reason for reason in decision["blocking_reasons"]))

    def test_raw_synthetic_credential_shapes_are_blocked(self):
        # Intentionally synthetic pattern samples, never genuine credentials.
        samples = ["ghp_" + "x" * 36, "github_pat_" + "x" * 30,
                   "AKIA" + "A" * 16, "sk-proj-" + "x" * 30,
                   "-----BEGIN RSA PRIVATE KEY-----"]
        for sample in samples:
            with self.subTest(sample=sample[:10]):
                decision = self.world.decision(draft_body="Observed text: " + sample)
                self.assertFalse(decision["auto_send_eligible"])
                self.world.issue["body"] = sample
                self.assertFalse(self.world.decision()["auto_send_eligible"])
                self.world.issue["body"] = "Empty response currently fails."

    def test_summary_does_not_claim_unsent_decisions_as_success(self):
        ready = self.world.decision()
        dry = {**ready, "result_group": "dry-run", "thread_number": 2}
        summary = cr.render_summary([ready, dry])
        auto_section = summary.split("## Auto-sent\n")[1].split("## Needs review")[0]
        self.assertEqual("- None", auto_section.strip())
        self.assertIn("issue #1", summary.split("## Needs review")[1])
        self.assertIn("issue #2", summary.split("## Needs review")[1])
        confirmed = {**ready, "result_group": "auto-sent", "comment_url": fixtures.URL}
        summary = cr.render_summary([confirmed])
        self.assertIn(fixtures.URL, summary.split("## Auto-sent")[1].split("## Needs review")[0])

    def discover(self, state):
        original_api = self.world.api
        def api(endpoint, **kwargs):
            if endpoint.split("?")[0] == f"repos/{REPO}/issues":
                return [deepcopy(self.world.issue)]
            return original_api(endpoint, **kwargs)
        with patch.object(cr, "gh_api_json", side_effect=api):
            return cr.discover_candidates(REPO, actor_login="operator", maintainers={"operator", "maintainer"},
                                          state=state, initial_lookback_hours=24, since="2026-11-08T11:00:00Z")

    def test_observed_old_timestamp_body_edit_is_scheduled_once(self):
        candidate = self.world.decision()["candidate"]
        state = {"thread_observations": {cr.state_key(candidate): {
            "root_fingerprint": candidate["root_fingerprint"], "head_sha": None}},
                 "threads": {cr.state_key(candidate): {"last_processed_signature": candidate["activity_signature"]}}}
        self.world.issue["body"] = "Updated reproduction while original creation time remains old."
        found = self.discover(state)
        self.assertEqual(1, len(found))
        self.assertNotEqual(candidate["activity_signature"], found[0]["activity_signature"])
        self.assertEqual([], self.discover(state))

    def test_observed_new_head_with_old_commit_date_is_scheduled(self):
        candidate = self.world.decision(pr=True)["candidate"]
        state = {"thread_observations": {cr.state_key(candidate): {
            "root_fingerprint": candidate["root_fingerprint"], "head_sha": candidate["head_sha"]}},
                 "threads": {cr.state_key(candidate): {"last_processed_signature": candidate["activity_signature"]}}}
        self.world.pr["head"]["sha"] = "pushed-old-dated-commit"
        found = self.discover(state)
        self.assertEqual(1, len(found))
        self.assertEqual("pushed-old-dated-commit", found[0]["head_sha"])
        self.assertEqual("2026-11-08T10:00:00Z", found[0]["latest_external_activity_at"])

    def test_first_seen_recently_edited_old_thread_is_not_lost(self):
        # GitHub includes this thread because updated_at is inside the query window;
        # old created_at must not suppress its newly edited content on migration.
        self.world.issue["updated_at"] = "2026-11-08T11:30:00Z"
        self.world.issue["body"] = "Recently edited reproduction on a previously unseen old issue."
        found = self.discover({})
        self.assertEqual(1, len(found))
        self.assertIn("discovery_uncertainty", found[0])
        decision = cr.build_decision(found[0], fixtures.review_text())
        self.assertFalse(decision["auto_send_eligible"])
        self.assertIn(found[0]["discovery_uncertainty"], decision["blocking_reasons"])

    def check_summary(self, check_pages, status_pages):
        # Bypass only the fixture's high-level summary patch, not network mocks.
        # Call the saved implementation; the network boundary remains mocked.
        with patch.object(cr, "gh_api_json", side_effect=[check_pages, status_pages]) as api:
            result = ORIGINAL_FETCH_CHECK_SUMMARY(REPO, "abc123")
        self.assertTrue(all(call.kwargs.get("paginate") is True for call in api.call_args_list))
        return result

    def test_more_than_one_hundred_checks_include_last_page_failure(self):
        success = {"name": "test", "status": "completed", "conclusion": "success"}
        pages = [{"total_count": 101, "check_runs": [success] * 100},
                 {"total_count": 101, "check_runs": [{**success, "name": "last-page", "conclusion": "failure"}]}]
        result = self.check_summary(pages, [{"total_count": 0, "statuses": [], "state": "success"}])
        self.assertFalse(result["has_uncertainty"])
        self.assertEqual("failure", result["overall"])
        self.assertEqual(101, len(result["details"]))

    def test_incomplete_and_absent_checks_are_held(self):
        success = {"name": "test", "status": "completed", "conclusion": "success"}
        for pages in ([{"total_count": 101, "check_runs": [success] * 100}],
                      [{"total_count": 0, "check_runs": []}], []):
            with self.subTest(count=len(pages)):
                result = self.check_summary(pages, [{"total_count": 0, "statuses": [], "state": "success"}])
                self.assertTrue(result["has_uncertainty"])
                self.assertEqual("unknown", result["overall"])

    def test_incomplete_legacy_status_pages_are_held(self):
        result = self.check_summary([{"total_count": 0, "check_runs": []}],
                                    [{"total_count": 101, "statuses": [{"state": "success"}] * 100, "state": "success"}])
        self.assertTrue(result["has_uncertainty"])

    def test_malformed_status_payload_cannot_default_to_success(self):
        result = self.check_summary([{"total_count": 0, "check_runs": []}],
                                    [{"total_count": 1, "statuses": [{"state": "failure"}]}])
        self.assertTrue(result["has_uncertainty"])
        self.assertEqual("unknown", result["overall"])

    def test_missing_total_count_cannot_claim_complete_check_coverage(self):
        result = self.check_summary([{"check_runs": [{"name": "one", "status": "completed", "conclusion": "success"}]}],
                                    [{"total_count": 0, "statuses": [], "state": "success"}])
        self.assertTrue(result["has_uncertainty"])

    def test_first_seen_uncertainty_survives_post_time_revalidation(self):
        self.world.issue["updated_at"] = "2026-11-08T11:30:00Z"
        candidate = self.discover({})[0]
        decision = cr.build_decision(candidate, fixtures.review_text())
        self.assertFalse(decision["auto_send_eligible"])
        # Attempt to override the gate flag and add the normal disclaimer. The
        # fresh candidate still must retain the original discovery safety hold.
        decision["auto_send_eligible"] = True
        decision["prepared_body"] = cr.append_disclaimer(decision["draft_body"], decision["language"])
        with self.assertRaises(ValueError):
            self.world.publish(decision)
        self.world.send.assert_not_called()

    def test_command_mark_processed_rejects_unconfirmed_outcomes_and_keeps_queue(self):
        candidate = self.world.decision()["candidate"]
        with ds.state_transaction(self.world.state) as state:
            ds.commit_discovery(state, REPO, [candidate], started_at=NOW)
        before = self.world.state.read_bytes()
        candidate_path = self.world.state.parent / "candidate.json"
        decision_path = self.world.state.parent / "decision.json"
        candidate_path.write_text(json.dumps(candidate))
        args = argparse.Namespace(candidate_file=str(candidate_path), decision_file=str(decision_path), state_file=str(self.world.state))
        for result in ("uncertain", "pending", "dry-run", "ready-to-send", "skipped/error", "auto-sent"):
            decision_path.write_text(json.dumps({"result_group": result}))
            with self.subTest(result=result), self.assertRaises(ValueError), redirect_stdout(io.StringIO()):
                cr.command_mark_processed(args)
            self.assertEqual(before, self.world.state.read_bytes())

    def test_command_scan_failure_retains_cursor_queue_and_observations(self):
        candidate = self.world.decision()["candidate"]
        with ds.state_transaction(self.world.state) as state:
            ds.commit_discovery(state, REPO, [candidate], started_at=NOW - timedelta(hours=12))
        before = self.world.state.read_bytes()
        def fail(*args, **kwargs):
            kwargs["state"]["thread_observations"] = {"partial": {"head_sha": "new"}}
            raise RuntimeError("page two failed")
        args = argparse.Namespace(repo=REPO, actor="operator", maintainers="maintainer",
                                  state_file=str(self.world.state), initial_lookback_hours=24)
        with patch.object(cr, "discover_candidates", side_effect=fail), self.assertRaises(RuntimeError), redirect_stdout(io.StringIO()):
            cr.command_scan(args)
        self.assertEqual(before, self.world.state.read_bytes())
        self.world.send.assert_not_called()


# Capture before each fixture installs its mock. No module reload or remote I/O.
ORIGINAL_FETCH_CHECK_SUMMARY = cr.fetch_check_summary

if __name__ == "__main__":
    unittest.main()
