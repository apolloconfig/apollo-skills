"""Offline policy and publication safety regressions. All remote calls are mocked."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import community_review as cr
import discovery_state as ds
import publish_gate as gate

REPO = "apolloconfig/apollo"
NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
URL = f"https://github.com/{REPO}/issues/1#issuecomment-9"


def review_text(**changes):
    review = {"language": "en", "decision_confidence": "high", "recommended_action": "comment",
              "validation_completed": True, "missing_info_fields": [], "blocking": False,
              "blocking_reasons": [], "draft_body": "The retry branch now handles the empty response correctly.",
              "evidence": ["src/client.py:12: empty response regression passed with pytest tests/test_client.py"]}
    review.update(changes)
    return "COMMUNITY_REVIEW_DECISION\n```json\n" + json.dumps(review) + "\n```"


class PublishGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state.json"
        self.repo = {"private": False, "visibility": "public", "archived": False, "disabled": False}
        self.issue = {"id": 101, "number": 1, "state": "open", "locked": False,
                      "title": "Handle empty response", "body": "Empty response currently fails.",
                      "created_at": "2026-11-08T10:00:00Z", "updated_at": "2026-11-08T10:00:00Z",
                      "user": {"login": "contributor", "type": "User"}, "author_association": "NONE",
                      "html_url": f"https://github.com/{REPO}/issues/1"}
        self.pr = {**self.issue, "head": {"sha": "abc123"}, "draft": False, "changed_files": 1}
        self.comments = []
        self.reviews = []
        self.review_comments = []
        self.files = [{"filename": "src/client.py", "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@\n-old\n+new"}]
        self.checks = {"overall": "success", "has_uncertainty": False, "details": []}
        for target, kwargs in (
            ("utc_now", {"return_value": NOW}),
            ("detect_github_login", {"return_value": "operator"}),
            ("gh_api_json", {"side_effect": self.api}),
            ("fetch_commit_timestamp", {"return_value": ("2026-11-08T10:00:00Z", "contributor")}),
            ("fetch_check_summary", {"side_effect": lambda *args: deepcopy(self.checks)}),
        ):
            mock = patch.object(cr, target, **kwargs)
            setattr(self, target + "_mock", mock.start())
            self.addCleanup(mock.stop)
        self.send_patch = patch.object(gate, "send_once", return_value={"html_url": URL})
        self.send = self.send_patch.start()
        self.addCleanup(self.send_patch.stop)
        # Any unexpected subprocess call fails instead of reaching GitHub.
        self.run_patch = patch.object(cr, "run_command", side_effect=AssertionError("unexpected remote command"))
        self.run = self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def api(self, endpoint, **kwargs):
        path = endpoint.split("?")[0]
        responses = {f"repos/{REPO}": self.repo,
                     f"repos/{REPO}/issues/1": self.issue,
                     f"repos/{REPO}/issues/1/comments": self.comments,
                     f"repos/{REPO}/pulls/1": self.pr,
                     f"repos/{REPO}/pulls/1/reviews": self.reviews,
                     f"repos/{REPO}/pulls/1/comments": self.review_comments,
                     f"repos/{REPO}/pulls/1/files": self.files}
        if path not in responses:
            raise AssertionError(f"unexpected endpoint {endpoint}")
        return deepcopy(responses[path])

    def decision(self, *, pr=False, **review_changes):
        if pr:
            self.issue["pull_request"] = {"url": "example"}
        builder = cr.build_pr_candidate if pr else cr.build_issue_candidate
        candidate = builder(REPO, deepcopy(self.issue), maintainers={"maintainer", "operator"}, actor_login="operator")
        return cr.build_decision(candidate, review_text(**review_changes))

    def publish(self, decision, *, dry_run=False):
        return gate.publish_decision(decision, state_path=self.state, dry_run=dry_run)

    def assert_blocked(self, decision):
        with self.assertRaises(ValueError):
            self.publish(decision)
        self.send.assert_not_called()

    def test_issue_publishes_once_with_marker_and_disclaimer(self):
        decision = self.decision()
        self.assertTrue(decision["auto_send_eligible"])
        result = self.publish(decision)
        self.assertEqual("auto-sent", result["result_group"])
        body = self.send.call_args.args[1]
        self.assertIn("<!-- apollo-community-review:", body)
        self.assertIn(cr.ENGLISH_DISCLAIMER, body)
        state = ds.load_state(self.state)
        self.assertEqual("sent", state["publication_journal"][cr.state_key(decision["candidate"])]["status"])

    def test_supported_request_changes_pins_reviewed_head(self):
        decision = self.decision(pr=True, recommended_action="request changes", blocking=True,
                                 blocking_reasons=["The empty response still raises"], reviewed_head_sha="abc123",
                                 findings=[{"severity": "P1", "path": "src/client.py", "line": 12,
                                            "evidence": "Empty response raises ValueError", "impact": "Client cannot retry"}],
                                 validation_details="pytest tests/test_client.py::test_empty failed at line 12")
        self.assertTrue(decision["auto_send_eligible"], decision["blocking_reasons"])
        self.publish(decision)
        self.send.assert_called_once()
        # Exercise the real writer without issuing a command.
        self.send_patch.stop()
        self.run.side_effect = None
        self.run.return_value = json.dumps({"html_url": URL})
        gate.send_once(decision, "body")
        args = self.run.call_args.args[0]
        self.assertIn("event=REQUEST_CHANGES", args)
        self.assertIn("commit_id=abc123", args)
        self.assertNotIn("event=APPROVE", args)

    def test_approve_and_unsupported_actions_always_block(self):
        for action in ("approve", "merge", "close", "REQUEST_CHANGES"):
            with self.subTest(action=action):
                self.assert_blocked(self.decision(pr=True, recommended_action=action))

    def test_tampered_eligibility_does_not_override_policy(self):
        decision = self.decision(decision_confidence="low")
        decision["auto_send_eligible"] = True
        self.assert_blocked(decision)

    def test_tampered_body_and_normalized_review_are_rejected(self):
        for field in ("prepared_body", "draft_body", "normalized_review"):
            decision = self.decision()
            decision[field] = {} if field == "normalized_review" else "Changed outgoing response"
            with self.subTest(field=field):
                self.assert_blocked(decision)

    def test_closed_and_locked_threads_block(self):
        decision = self.decision()
        for change in ({"state": "closed"}, {"locked": True}):
            original = deepcopy(self.issue)
            self.issue.update(change)
            self.assert_blocked(decision)
            self.issue = original

    def test_draft_new_head_and_new_checks_require_review(self):
        decision = self.decision(pr=True)
        self.pr["draft"] = True
        self.assert_blocked(decision)
        self.pr["draft"] = False
        self.pr["head"]["sha"] = "newhead"
        self.assert_blocked(decision)
        self.pr["head"]["sha"] = "abc123"
        self.checks["overall"] = "failure"
        self.assert_blocked(decision)

    def test_new_comment_invalidates_issue(self):
        decision = self.decision()
        self.comments.append({"id": 2, "body": "Additional reproduction detail", "created_at": "2026-11-08T11:00:00Z",
                              "user": {"login": "contributor", "type": "User"}})
        self.assert_blocked(decision)

    def test_new_maintainer_reply_invalidates_issue(self):
        decision = self.decision()
        self.comments.append({"id": 2, "body": "I am handling this", "created_at": "2026-11-08T11:00:00Z",
                              "user": {"login": "maintainer", "type": "User"}})
        self.assert_blocked(decision)
        current = self.decision()
        self.assertIn("newer maintainer reply exists after latest external activity", current["blocking_reasons"])

    def test_actor_mismatch_blocks(self):
        decision = self.decision()
        self.detect_github_login_mock.return_value = "another-actor"
        self.assert_blocked(decision)

    def test_private_unknown_and_advisory_repos_block(self):
        decision = self.decision()
        self.repo["private"] = True
        self.assert_blocked(decision)
        del self.repo["private"]
        self.assert_blocked(decision)
        with self.assertRaises(ValueError):
            gate.require_public_repo("apolloconfig/apollo-ghsa-abcd")
        self.send.assert_not_called()

    def test_explicit_internal_visibility_is_not_public(self):
        decision = self.decision()
        self.repo["visibility"] = "internal"
        self.assert_blocked(decision)

    def test_request_changes_requires_finding_validation_and_current_head(self):
        common = {"recommended_action": "request changes", "blocking": True,
                  "blocking_reasons": ["Empty response raises"], "reviewed_head_sha": "abc123",
                  "findings": [{"severity": "P2", "path": "src/client.py", "line": 12,
                                "evidence": "Empty response raises", "impact": "Retry fails"}],
                  "validation_details": "pytest tests/test_client.py::test_empty failed"}
        for change in ({"findings": []}, {"reviewed_head_sha": "old"}, {"validation_details": ""},
                       {"blocking": False}, {"blocking_reasons": []}):
            with self.subTest(change=change):
                self.assert_blocked(self.decision(pr=True, **{**common, **change}))

    def test_unknown_write_response_is_uncertain_not_success(self):
        decision = self.decision()
        self.send.return_value = {"html_url": "https://elsewhere.example/result"}
        with self.assertRaises(ValueError):
            self.publish(decision)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.publish(decision)
        self.assertEqual(1, self.send.call_count)

    def test_crash_after_intent_requires_reconciliation_without_write(self):
        decision = self.decision()
        op_id = gate.operation_id(decision)
        with ds.state_transaction(self.state) as state:
            state["publication_journal"] = {
                cr.state_key(decision["candidate"]): {
                    "status": "pending", "operation_id": op_id,
                    "marker": f"<!-- apollo-community-review:{op_id} -->", "decision": decision}}
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.publish(decision)
        self.send.assert_not_called()

    def test_timeout_is_durable_uncertain_and_never_retried(self):
        decision = self.decision()
        self.send.side_effect = subprocess.TimeoutExpired("gh api", 10)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.publish(decision)
        state = ds.load_state(self.state)
        self.assertEqual("uncertain", state["publication_journal"][cr.state_key(decision["candidate"])]["status"])
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.publish(decision)
        self.assertEqual(1, self.send.call_count)

    def test_successful_retry_deduplicates_without_write(self):
        decision = self.decision()
        self.publish(decision)
        result = self.publish(decision)
        self.assertTrue(result["deduplicated"])
        self.assertEqual(1, self.send.call_count)

    def test_timeout_reconciles_existing_matching_post_without_retry(self):
        decision = self.decision()
        self.send.side_effect = TimeoutError("connection lost")
        with self.assertRaises(TimeoutError):
            self.publish(decision)
        marker = self.send.call_args.args[1]
        self.comments.append({"body": marker, "user": {"login": "operator"}, "html_url": URL})
        result = self.publish(decision)
        self.assertTrue(result["deduplicated"])
        self.assertEqual(1, self.send.call_count)
        state = ds.load_state(self.state)
        self.assertEqual("sent", state["publication_journal"][cr.state_key(decision["candidate"])]["status"])

    def test_reconciliation_ignores_other_actor_marker(self):
        decision = self.decision()
        self.send.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.publish(decision)
        self.comments.append({"body": self.send.call_args.args[1], "user": {"login": "contributor"}, "html_url": URL})
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.publish(decision)
        self.assertEqual(1, self.send.call_count)

    def test_dry_run_never_writes_remote_or_pending_intent(self):
        result = self.publish(self.decision(), dry_run=True)
        self.assertEqual("dry-run", result["result_group"])
        self.send.assert_not_called()
        self.run.assert_not_called()
        self.assertFalse(ds.load_state(self.state)["publication_journal"])

    def test_security_in_comment_diff_check_output_and_review_output_blocks(self):
        locations = ("comment", "diff", "check", "review-comment", "review-output")
        for location in locations:
            with self.subTest(location=location):
                self.comments = []
                self.review_comments = []
                self.files[0]["patch"] = "@@ -1 +1 @@\n-old\n+new"
                self.checks["details"] = []
                if location == "comment":
                    self.comments.append({"id": 2, "body": "Possible credential leak", "created_at": "2026-11-08T11:00:00Z", "user": {"login": "contributor"}})
                elif location == "diff":
                    self.files[0]["patch"] = "+private key"
                elif location == "check":
                    self.checks["details"] = ["Potential vulnerability found"]
                elif location == "review-comment":
                    self.review_comments.append({"id": 3, "body": "auth bypass", "created_at": "2026-11-08T11:00:00Z", "user": {"login": "contributor"}})
                changes = {"draft_body": "This is a security advisory"} if location == "review-output" else {}
                decision = self.decision(pr=True, **changes)
                self.assertFalse(decision["auto_send_eligible"])
                self.assertTrue(any("security-sensitive" in reason for reason in decision["blocking_reasons"]))
                self.assert_blocked(decision)

    def test_schema_rejects_string_bools_and_nonlist_evidence(self):
        for changes in ({"blocking": "false"}, {"validation_completed": "true"},
                        {"request_info_only": "true"}, {"evidence": "tested"}, {"blocking": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.decision(**changes)
        self.assertFalse(self.decision(evidence=[])["auto_send_eligible"])

    def test_obsolete_schema_and_string_eligibility_fail_closed(self):
        for changes in ({"schema_version": 1}, {"auto_send_eligible": "true"}, {"auto_send_eligible": 1}):
            decision = self.decision()
            decision.update(changes)
            self.assert_blocked(decision)

    def test_expired_and_future_reviews_block(self):
        for offset in (-61, 1):
            decision = self.decision()
            decision["created_at"] = cr.to_iso(NOW + timedelta(minutes=offset))
            self.assert_blocked(decision)

    def test_wrong_target_or_boolean_number_blocks(self):
        decision = self.decision()
        decision["thread_number"] = 2
        self.assert_blocked(decision)
        decision = self.decision()
        decision["thread_number"] = True
        decision["candidate"]["number"] = True
        self.assert_blocked(decision)


if __name__ == "__main__":
    unittest.main()
