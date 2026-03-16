from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import community_review as cr


FIXTURES = ROOT / "scripts" / "testdata"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def issue_candidate(**overrides):
    candidate = {
        "activity_signature": "issue:1001:issue-comment:11:2026-03-14T00:00:00Z",
        "body": "请问 Apollo 是否已经提供官方 OpenAPI 文档？",
        "draft": False,
        "has_newer_maintainer_activity": False,
        "html_url": "https://github.com/apolloconfig/apollo/issues/1001",
        "number": 1001,
        "repo": "apolloconfig/apollo",
        "thread_id": "1001",
        "thread_type": "issue",
        "title": "是否提供 OpenAPI 文档",
    }
    candidate.update(overrides)
    return candidate


def pr_candidate(**overrides):
    candidate = {
        "activity_signature": "pr:2002:sha:abc123:2026-03-14T00:00:00Z:social:none",
        "body": "Fixes #1999",
        "check_summary": {
            "details": ["build: completed/success", "spotless: completed/success"],
            "has_uncertainty": False,
            "overall": "success",
        },
        "draft": False,
        "has_newer_maintainer_activity": False,
        "head_sha": "abc123",
        "html_url": "https://github.com/apolloconfig/apollo/pull/2002",
        "number": 2002,
        "repo": "apolloconfig/apollo",
        "thread_id": "2002",
        "thread_type": "pr",
        "title": "Refine namespace validation",
    }
    candidate.update(overrides)
    return candidate


class CommunityReviewTest(unittest.TestCase):
    def test_issue_zh_consultative_high_confidence_auto_send(self):
        decision = cr.build_decision(issue_candidate(), fixture("issue_consultative_zh_high.txt"))
        self.assertTrue(decision["auto_send_eligible"])
        self.assertEqual("zh", decision["language"])
        self.assertIn(cr.CHINESE_DISCLAIMER, decision["prepared_body"])
        self.assertEqual([], decision["blocking_reasons"])

    def test_issue_bug_reproduced_high_confidence_auto_send(self):
        decision = cr.build_decision(
            issue_candidate(
                title="Config publish returns 500",
                body="Publishing a namespace returns 500 on 2.4.0.",
                thread_id="1002",
                number=1002,
            ),
            fixture("issue_bug_en_reproduced_high.txt"),
        )
        self.assertTrue(decision["auto_send_eligible"])
        self.assertEqual("en", decision["language"])
        self.assertIn(cr.ENGLISH_DISCLAIMER, decision["prepared_body"])

    def test_issue_bug_missing_info_routes_to_manual_review(self):
        decision = cr.build_decision(
            issue_candidate(
                title="Config publish returns 500",
                body="Publishing a namespace returns 500 on 2.4.0.",
                thread_id="1003",
                number=1003,
            ),
            fixture("issue_bug_en_missing_info.txt"),
        )
        self.assertFalse(decision["auto_send_eligible"])
        self.assertIn("missing information", " ".join(decision["blocking_reasons"]))
        self.assertNotIn(cr.ENGLISH_DISCLAIMER, decision["prepared_body"])

    def test_pr_high_confidence_comment_can_auto_send(self):
        decision = cr.build_decision(pr_candidate(), fixture("pr_comment_en_high.txt"))
        self.assertTrue(decision["auto_send_eligible"])
        self.assertEqual("comment", decision["recommended_action"])
        self.assertIn(cr.ENGLISH_DISCLAIMER, decision["prepared_body"])

    def test_pr_request_changes_stays_manual(self):
        decision = cr.build_decision(pr_candidate(), fixture("pr_request_changes_zh.txt"))
        self.assertFalse(decision["auto_send_eligible"])
        self.assertIn("recommended action is request changes", decision["blocking_reasons"])

    def test_pr_failed_checks_stays_manual(self):
        candidate = pr_candidate(
            check_summary={
                "details": ["build: completed/failure"],
                "has_uncertainty": False,
                "overall": "failure",
            }
        )
        decision = cr.build_decision(candidate, fixture("pr_comment_en_high.txt"))
        self.assertFalse(decision["auto_send_eligible"])
        self.assertIn("checks are failure", decision["blocking_reasons"])

    def test_draft_pr_stays_manual(self):
        decision = cr.build_decision(pr_candidate(draft=True), fixture("pr_comment_en_high.txt"))
        self.assertFalse(decision["auto_send_eligible"])
        self.assertIn("draft PR", decision["blocking_reasons"])

    def test_newer_maintainer_reply_stays_manual(self):
        decision = cr.build_decision(
            issue_candidate(has_newer_maintainer_activity=True),
            fixture("issue_consultative_zh_high.txt"),
        )
        self.assertFalse(decision["auto_send_eligible"])
        self.assertIn(
            "newer maintainer reply exists after latest external activity",
            decision["blocking_reasons"],
        )

    def test_security_thread_forces_manual(self):
        decision = cr.build_decision(
            issue_candidate(title="Potential auth bypass vulnerability"),
            fixture("issue_bug_en_reproduced_high.txt"),
        )
        self.assertFalse(decision["auto_send_eligible"])
        self.assertIn("security-sensitive thread", decision["blocking_reasons"])

    def test_yaml_fallback_is_supported(self):
        review_text = """Issue Summary

Draft Maintainer Reply
Apollo currently does not expose this through OpenAPI.

Publish Gate

handoff:
  issue_classification:
    type: "功能咨询"
    validation_path: "consultative-support"
    confidence: "high"
  triage_decision:
    labels_to_add: []
    missing_info_fields: []
    ready_for_issue_to_pr: false
    ready_reason: ""
  implementation_handoff:
    goal: ""
    acceptance_criteria: []
    suggested_modules: []
    risk_hints: []
"""
        decision = cr.build_decision(issue_candidate(), review_text)
        self.assertTrue(decision["auto_send_eligible"])
        self.assertIn(cr.CHINESE_DISCLAIMER, decision["prepared_body"])

    def test_dedupe_uses_activity_signature(self):
        candidate = issue_candidate()
        state = {"last_scan_at": None, "threads": {}}
        decision = cr.build_decision(candidate, fixture("issue_consultative_zh_high.txt"))
        updated = cr.mark_processed(state, candidate, decision, now=datetime(2026, 3, 14, tzinfo=timezone.utc))
        self.assertFalse(cr.should_process_candidate(candidate, updated))

    def test_summary_contains_expected_sections(self):
        results = [
            {
                "comment_url": "https://github.com/apolloconfig/apollo/issues/1001#issuecomment-1",
                "decision_confidence": "high",
                "result_group": "auto-sent",
                "thread_number": 1001,
                "thread_type": "issue",
                "thread_url": "https://github.com/apolloconfig/apollo/issues/1001",
            },
            {
                "blocking_reasons": ["checks are failure"],
                "draft_body": "Please rerun the failing build.",
                "result_group": "needs-review",
                "thread_number": 2002,
                "thread_type": "pr",
                "thread_url": "https://github.com/apolloconfig/apollo/pull/2002",
            },
        ]
        summary = cr.render_summary(results)
        self.assertIn("## Auto-sent", summary)
        self.assertIn("## Needs review", summary)
        self.assertIn("Please rerun the failing build.", summary)

    def test_mark_processed_persists_state_schema(self):
        candidate = issue_candidate()
        decision = cr.build_decision(candidate, fixture("issue_consultative_zh_high.txt"))
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            state = cr.load_state(state_file)
            cr.save_state(state_file, cr.mark_processed(state, candidate, decision))
            persisted = json.loads(state_file.read_text())
            self.assertIn("threads", persisted)
            self.assertIn(cr.state_key(candidate), persisted["threads"])

    def test_issue_candidate_ignores_stale_bot_only_activity(self):
        issue = {
            "body": "Original issue body",
            "created_at": "2026-03-10T00:00:00Z",
            "html_url": "https://github.com/apolloconfig/apollo/issues/3001",
            "id": 3001,
            "number": 3001,
            "state": "open",
            "title": "Issue with stale noise",
            "updated_at": "2026-03-16T00:00:00Z",
            "user": {"login": "community-user", "type": "User"},
        }
        comments = [
            {
                "created_at": "2026-03-16T00:00:00Z",
                "id": 4001,
                "updated_at": "2026-03-16T00:00:00Z",
                "user": {"login": "stale[bot]", "type": "Bot"},
            }
        ]
        with patch.object(cr, "gh_api_json", return_value=comments):
            candidate = cr.build_issue_candidate(
                "apolloconfig/apollo",
                issue,
                maintainers={"nobodyiam"},
                actor_login="nobodyiam",
            )

        self.assertEqual("2026-03-10T00:00:00Z", candidate["latest_external_activity_at"])
        self.assertEqual(
            "issue:3001:issue-body:3001:2026-03-10T00:00:00Z",
            candidate["activity_signature"],
        )
        self.assertFalse(cr.has_new_external_activity(candidate, "2026-03-15T00:00:00Z"))

    def test_pr_candidate_ignores_mergify_only_activity(self):
        issue_stub = {
            "number": 4002,
            "pull_request": {"url": "https://api.github.com/repos/apolloconfig/apollo/pulls/4002"},
            "state": "open",
        }
        issue = {
            "body": "Original PR body",
            "created_at": "2026-03-10T00:00:00Z",
            "html_url": "https://github.com/apolloconfig/apollo/pull/4002",
            "labels": [],
            "number": 4002,
            "repository_url": "https://api.github.com/repos/apolloconfig/apollo",
            "title": "PR with mergify noise",
            "updated_at": "2026-03-16T00:00:00Z",
            "user": {"login": "contributor", "type": "User"},
        }
        pr = {
            "created_at": "2026-03-10T00:00:00Z",
            "draft": False,
            "head": {"sha": "abc123"},
            "html_url": "https://github.com/apolloconfig/apollo/pull/4002",
            "id": 5002,
            "number": 4002,
            "title": "PR with mergify noise",
            "updated_at": "2026-03-16T00:00:00Z",
            "user": {"login": "contributor", "type": "User"},
        }
        issue_comments = [
            {
                "created_at": "2026-03-16T00:00:00Z",
                "id": 6002,
                "updated_at": "2026-03-16T00:00:00Z",
                "user": {"login": "mergify[bot]", "type": "Bot"},
            }
        ]
        with patch.object(
            cr,
            "gh_api_json",
            side_effect=[issue, pr, issue_comments, [], [], {"check_runs": []}, {"state": "success"}],
        ), patch.object(cr, "fetch_commit_timestamp", return_value=("2026-03-10T00:00:00Z", "contributor")):
            candidate = cr.build_pr_candidate(
                "apolloconfig/apollo",
                issue_stub,
                maintainers={"nobodyiam"},
                actor_login="nobodyiam",
            )

        self.assertEqual("2026-03-10T00:00:00Z", candidate["latest_external_activity_at"])
        self.assertTrue(
            candidate["activity_signature"].endswith(":pr-body:5002:2026-03-10T00:00:00Z"),
            candidate["activity_signature"],
        )
        self.assertFalse(cr.has_new_external_activity(candidate, "2026-03-15T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
