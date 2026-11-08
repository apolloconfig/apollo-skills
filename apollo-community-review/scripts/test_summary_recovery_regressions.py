"""Keep durable and immediate publication reports useful without duplicate sends."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import unittest

import community_review as cr
import test_feedback_discovery_regressions as discovery_fixtures
import test_publish_gate as fixtures


class SummaryRecoveryRegressions(unittest.TestCase):
    def result(self, **changes):
        result = {"repo": fixtures.REPO, "thread_type": "issue", "thread_number": 1,
                  "thread_url": f"https://github.com/{fixtures.REPO}/issues/1",
                  "result_group": "auto-sent", "comment_url": fixtures.URL,
                  "decision_confidence": "high", "blocking_reasons": []}
        result.update(changes)
        return result

    def test_same_confirmed_url_is_rendered_once_without_mutating_results(self):
        results = [self.result(), self.result(deduplicated=True)]
        original = deepcopy(results)
        summary = cr.render_summary(results)
        self.assertEqual(1, summary.count(fixtures.URL))
        self.assertEqual(original, results)

    def test_distinct_sends_on_same_thread_are_kept(self):
        second = fixtures.URL + "0"
        summary = cr.render_summary([self.result(), self.result(comment_url=second)])
        self.assertEqual(2, summary.count("  reason: confidence="))
        self.assertIn(f"comment={fixtures.URL}\n", summary)
        self.assertIn(f"comment={second}\n", summary)

    def test_absent_empty_and_invalid_urls_do_not_collapse_results(self):
        for value in (None, "", " ", [], {}):
            with self.subTest(url=value):
                summary = cr.render_summary([self.result(comment_url=value), self.result(comment_url=value)])
                self.assertEqual(2, summary.count("  reason: confidence="))

    def test_other_outcomes_on_same_thread_are_kept(self):
        results = [self.result(), self.result(result_group="needs-review", draft_body="Review this newer snapshot."),
                   self.result(result_group="uncertain", blocking_reasons=["Reconcile the newer attempt."])]
        summary = cr.render_summary(results)
        self.assertEqual(1, summary.count("  reason: confidence="))
        self.assertIn("Review this newer snapshot.", summary)
        self.assertIn("Reconcile the newer attempt.", summary)

    def test_withheld_copy_wins_in_either_input_order(self):
        ordinary = self.result()
        withheld = self.result(blocking_reasons=["security-sensitive details"],
                               thread_url="https://example.invalid/private-details",
                               draft_body="private draft")
        for results in ([ordinary, withheld], [withheld, ordinary]):
            with self.subTest(order=results):
                summary = cr.render_summary(results)
                self.assertEqual(1, summary.count("Security details withheld"))
                self.assertNotIn(fixtures.URL, summary)
                self.assertNotIn("private-details", summary)
                self.assertNotIn("private draft", summary)

    def assert_cli_results_merge_once(self, *, uncertain):
        helper = discovery_fixtures.FeedbackDiscoveryRegressions(methodName="runTest")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        original = helper.uncertain_send() if uncertain else helper.world.decision()
        if uncertain:
            helper.accepted_post()
        result = helper.world.publish(original)
        output = io.StringIO()
        args = argparse.Namespace(repo=fixtures.REPO, actor="operator",
                                  state_file=str(helper.world.state), since=None)
        with redirect_stdout(output):
            cr.command_scan_results(args)
        recovered = json.loads(output.getvalue())
        self.assertEqual(1, len(recovered))
        self.assertEqual(result["comment_url"], recovered[0]["comment_url"])
        self.assertEqual(1, cr.render_summary([result, *recovered]).count(fixtures.URL))
        self.assertEqual(1, helper.world.send.call_count)

    def test_immediate_and_recovered_cli_results_merge_once(self):
        self.assert_cli_results_merge_once(uncertain=True)

    def test_immediate_success_and_outbox_cli_results_merge_once(self):
        self.assert_cli_results_merge_once(uncertain=False)


if __name__ == "__main__":
    unittest.main()
