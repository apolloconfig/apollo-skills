"""Raw GitHub response regressions, with all remote boundaries mocked."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import community_review as cr
from test_publish_gate import review_text


class CheckEvidenceRegressionTest(unittest.TestCase):
    def setUp(self):
        self.checks = [{"total_count": 1, "check_runs": [{
            "name": "build", "status": "completed", "conclusion": "success",
            "output": {"title": "Build passed", "summary": "All tests passed", "text": "Log complete"},
        }]}]
        # This is GitHub's actual empty legacy-status shape for Check-Runs-only CI.
        self.statuses = [{"total_count": 0, "statuses": [], "state": "pending"}]
        self.issue = {"number": 1, "id": 1, "state": "open", "title": "Handle empty response",
                      "body": "Handle empty response", "user": {"login": "contributor", "type": "User"},
                      "created_at": "2026-11-08T10:00:00Z"}
        self.pr = {**self.issue, "head": {"sha": "abc123"}, "changed_files": 1, "draft": False}
        self.calls = []
        for target, kwargs in (
            ("gh_api_json", {"side_effect": self.api}),
            ("utc_now", {"return_value": datetime(2026, 11, 8, 12, tzinfo=timezone.utc)}),
            ("run_command", {"side_effect": AssertionError("unexpected remote command")}),
        ):
            mock = patch.object(cr, target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def api(self, endpoint, **kwargs):
        path = endpoint.split("?")[0].removeprefix("repos/apolloconfig/apollo/")
        self.calls.append((path, kwargs))
        responses = {
            "issues/1": self.issue, "pulls/1": self.pr,
            "issues/1/comments": [], "pulls/1/reviews": [], "pulls/1/comments": [],
            "pulls/1/files": [{"filename": "src/client.py", "additions": 1, "deletions": 1,
                               "patch": "@@ -1 +1 @@\n-old\n+new"}],
            "pulls/1/commits": [{"commit": {"committer": {"date": "2026-11-08T10:00:00Z"}},
                                 "author": {"login": "contributor"}}],
            "commits/abc123/check-runs": self.checks, "commits/abc123/status": self.statuses,
        }
        if path not in responses:
            raise AssertionError(f"unexpected endpoint: {endpoint}")
        return deepcopy(responses[path])

    def candidate(self):
        return cr.build_pr_candidate("apolloconfig/apollo", {"number": 1},
                                     maintainers={"operator"}, actor_login="operator")

    def assert_held(self):
        candidate = self.candidate()
        self.assertTrue(candidate["check_summary"]["has_uncertainty"])
        self.assertFalse(cr.build_decision(candidate, review_text())["auto_send_eligible"])

    def test_check_runs_only_empty_pending_legacy_is_eligible(self):
        candidate = self.candidate()
        self.assertEqual("success", candidate["check_summary"]["overall"])
        self.assertTrue(cr.build_decision(candidate, review_text())["auto_send_eligible"])
        self.assertTrue(all(kwargs.get("paginate") for path, kwargs in self.calls
                            if path.startswith("commits/")))

    def test_each_raw_check_output_field_changes_fingerprint_and_blocks_secrets(self):
        before = self.candidate()
        for field in ("title", "summary", "text"):
            with self.subTest(field=field):
                output = self.checks[0]["check_runs"][0]["output"]
                original = output[field]
                output[field] = "credential leak in build logs"
                after = self.candidate()
                self.assertNotEqual(before["context_fingerprint"], after["context_fingerprint"])
                self.assertEqual(before["activity_signature"], after["activity_signature"])
                decision = cr.build_decision(after, review_text())
                self.assertFalse(decision["auto_send_eligible"])
                self.assertIn("security-sensitive content; keep out of public workflow", decision["blocking_reasons"])
                output[field] = original

    def test_raw_legacy_description_changes_fingerprint_and_security_gate(self):
        self.statuses = [{"total_count": 1, "state": "success", "statuses": [
            {"context": "external-ci", "state": "success", "description": "Build passed"}]}]
        before = self.candidate()
        self.assertTrue(cr.build_decision(before, review_text())["auto_send_eligible"])
        self.statuses[0]["statuses"][0]["description"] = "credential leak in log"
        after = self.candidate()
        self.assertNotEqual(before["context_fingerprint"], after["context_fingerprint"])
        decision = cr.build_decision(after, review_text())
        self.assertFalse(decision["auto_send_eligible"])
        self.assertIn("security-sensitive content; keep out of public workflow", decision["blocking_reasons"])

    def test_transport_metadata_does_not_change_fingerprint(self):
        before = self.candidate()
        self.checks[0]["check_runs"][0].update({"url": "https://example.test/changed", "id": 1234})
        self.checks[0]["check_runs"][0]["output"]["annotations_url"] = "https://example.test/annotations"
        self.assertEqual(before["context_fingerprint"], self.candidate()["context_fingerprint"])

    def test_real_pending_and_failed_legacy_statuses_hold(self):
        for state in ("pending", "failure", "error"):
            with self.subTest(state=state):
                self.statuses = [{"total_count": 1, "state": state,
                                  "statuses": [{"context": "external-ci", "state": state}]}]
                candidate = self.candidate()
                self.assertFalse(cr.build_decision(candidate, review_text())["auto_send_eligible"])

    def test_both_sources_empty_still_hold(self):
        self.checks = [{"total_count": 0, "check_runs": []}]
        self.assert_held()

    def test_incomplete_or_malformed_counts_still_hold(self):
        for field in ("checks", "statuses"):
            for count in (None, True, -1, "0", 2):
                with self.subTest(field=field, count=count):
                    pages = getattr(self, field)
                    old_count = pages[0]["total_count"]
                    pages[0]["total_count"] = count
                    self.assert_held()
                    pages[0]["total_count"] = old_count

    def test_invalid_empty_legacy_state_still_holds(self):
        for state in (None, "unknown"):
            with self.subTest(state=state):
                self.statuses[0]["state"] = state
                self.assert_held()

    def test_uncertain_counts_retain_available_raw_evidence(self):
        before = self.candidate()
        self.checks[0]["total_count"] = 2
        self.checks[0]["check_runs"][0]["output"]["text"] = "credential leak in build logs"
        after = self.candidate()
        self.assertTrue(after["check_summary"]["has_uncertainty"])
        self.assertNotEqual(before["context_fingerprint"], after["context_fingerprint"])
        decision = cr.build_decision(after, review_text())
        self.assertIn("security-sensitive content; keep out of public workflow", decision["blocking_reasons"])


if __name__ == "__main__":
    unittest.main()
