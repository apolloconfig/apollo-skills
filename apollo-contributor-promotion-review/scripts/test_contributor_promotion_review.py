from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import contributor_promotion_review as cpr


class ContributorPromotionReviewTest(unittest.TestCase):
    def test_normalize_login_breaks_alias_cycle(self):
        aliases = {"alice": "bob", "bob": "alice"}

        normalized = cpr.normalize_login("alice", aliases)

        self.assertEqual("alice", normalized)

    def test_parse_team_roles_distinguishes_pmcs_and_committers(self):
        markdown = """
### Project Management Committee(PMC)

| GitHub ID | Name |
| --------- | ---- |
| FooBar    | Foo  |

### Committer

| GitHub ID | Name |
| --------- | ---- |
| BarBaz    | Bar  |
| FooBar    | Foo  |
"""

        roles = cpr.parse_team_roles(markdown)

        self.assertEqual({"foobar"}, roles[cpr.SECTION_PMCS])
        self.assertEqual({"barbaz", "foobar"}, roles[cpr.SECTION_COMMITTERS])

    def test_membership_requests_require_sponsors_or_acceptance_reply(self):
        aliases = {}
        issues = [
            {
                "number": 10,
                "title": "REQUEST: New membership for <alice>",
                "body": "### GitHub Username\n\n@alice\n",
            },
            {
                "number": 11,
                "title": "REQUEST: New membership for bob",
                "body": "### GitHub Username\n\n@bob\n",
            },
        ]
        comments_by_issue = {
            10: [
                {"user": {"login": "pmc1"}, "body": "+1"},
                {"user": {"login": "pmc2"}, "body": "+1"},
            ],
            11: [
                {
                    "user": {"login": "pmc1"},
                    "body": "@bob You have been added to the Apollo organization, please check the invitation from GitHub.",
                }
            ],
        }

        members = cpr.resolve_members_from_membership_requests(
            issues,
            comments_by_issue,
            {"pmc1", "pmc2"},
            {"pmc1", "pmc2", "committer1"},
            aliases,
        )

        self.assertEqual({"alice", "bob"}, members)

    def test_recent_aggregation_filters_bots_and_self_interactions(self):
        policy = {
            "scoreWeights": {
                "merged_pr": 8,
                "open_pr": 4,
                "review": 3,
                "pr_comment": 1,
                "issue_comment": 1,
                "extra_repo_bonus": 2,
            }
        }
        pr_nodes = [
            {
                "title": "Improve namespace validation",
                "url": "https://example/pr/1",
                "mergedAt": "2026-03-10T00:00:00Z",
                "updatedAt": "2026-03-11T00:00:00Z",
                "createdAt": "2026-03-09T00:00:00Z",
                "repository": {"nameWithOwner": "apolloconfig/apollo"},
                "author": {"login": "alice"},
                "reviews": {
                    "nodes": [
                        {
                            "url": "https://example/pr/1/reviews/1",
                            "submittedAt": "2026-03-10T01:00:00Z",
                            "author": {"login": "bob"},
                        },
                        {
                            "url": "https://example/pr/1/reviews/2",
                            "submittedAt": "2026-03-10T02:00:00Z",
                            "author": {"login": "alice"},
                        },
                    ]
                },
                "comments": {
                    "nodes": [
                        {
                            "url": "https://example/pr/1/comments/1",
                            "createdAt": "2026-03-10T03:00:00Z",
                            "author": {"login": "carol"},
                        },
                        {
                            "url": "https://example/pr/1/comments/2",
                            "createdAt": "2026-03-10T04:00:00Z",
                            "author": {"login": "coderabbitai"},
                        },
                    ]
                },
            },
            {
                "title": "Improve docs",
                "url": "https://example/pr/2",
                "mergedAt": "2026-03-12T00:00:00Z",
                "updatedAt": "2026-03-12T00:00:00Z",
                "createdAt": "2026-03-11T00:00:00Z",
                "repository": {"nameWithOwner": "apolloconfig/apollo-java"},
                "author": {"login": "alice"},
                "reviews": {"nodes": []},
                "comments": {"nodes": []},
            },
        ]
        issue_nodes = [
            {
                "title": "Need help",
                "url": "https://example/issues/1",
                "repository": {"nameWithOwner": "apolloconfig/apollo"},
                "author": {"login": "dave"},
                "comments": {
                    "nodes": [
                        {
                            "url": "https://example/issues/1/comments/1",
                            "createdAt": "2026-03-13T00:00:00Z",
                            "author": {"login": "bob"},
                        },
                        {
                            "url": "https://example/issues/1/comments/2",
                            "createdAt": "2026-03-13T01:00:00Z",
                            "author": {"login": "dave"},
                        },
                    ]
                },
            }
        ]

        contributors = cpr.aggregate_recent_activity(
            pr_nodes=pr_nodes,
            issue_nodes=issue_nodes,
            discussion_nodes_by_repo={},
            allowed_repos={"apolloconfig/apollo", "apolloconfig/apollo-java"},
            policy=policy,
            ignored_logins=set(),
            aliases={},
            cutoff=cpr.iso_to_datetime("2026-03-01T00:00:00Z"),
        )
        ranked = cpr.finalize_recent_contributors(contributors, {"scoreWeights": policy["scoreWeights"], "maxRepoBonus": 6})
        alice = next(item for item in ranked if item["login"] == "alice")
        bob = next(item for item in ranked if item["login"] == "bob")
        carol = next(item for item in ranked if item["login"] == "carol")

        self.assertEqual(18, alice["recentScore"])
        self.assertEqual(2, alice["recentBreakdown"]["mergedPrs"])
        self.assertEqual(2, alice["recentBreakdown"]["repoBonus"])
        self.assertEqual(4, bob["recentScore"])
        self.assertEqual(1, bob["recentBreakdown"]["reviews"])
        self.assertEqual(1, bob["recentBreakdown"]["issueComments"])
        self.assertEqual(1, carol["recentScore"])
        self.assertEqual(1, carol["recentBreakdown"]["prComments"])

    def test_recent_aggregation_ignores_events_outside_cutoff(self):
        policy = {
            "scoreWeights": {
                "merged_pr": 8,
                "open_pr": 4,
                "review": 3,
                "pr_comment": 1,
                "issue_comment": 1,
                "extra_repo_bonus": 2,
            }
        }
        cutoff = cpr.iso_to_datetime("2026-03-01T00:00:00Z")
        pr_nodes = [
            {
                "title": "Old merged PR with a new comment",
                "url": "https://example/pr/10",
                "isDraft": False,
                "state": "MERGED",
                "mergedAt": "2026-01-10T00:00:00Z",
                "updatedAt": "2026-03-15T00:00:00Z",
                "createdAt": "2026-01-01T00:00:00Z",
                "repository": {"nameWithOwner": "apolloconfig/apollo"},
                "author": {"login": "alice"},
                "reviews": {
                    "nodes": [
                        {
                            "url": "https://example/pr/10/reviews/1",
                            "submittedAt": "2026-01-11T00:00:00Z",
                            "author": {"login": "bob"},
                        }
                    ]
                },
                "comments": {
                    "nodes": [
                        {
                            "url": "https://example/pr/10/comments/1",
                            "createdAt": "2026-03-15T01:00:00Z",
                            "author": {"login": "carol"},
                        }
                    ]
                },
            },
            {
                "title": "Recently updated open PR",
                "url": "https://example/pr/11",
                "isDraft": False,
                "state": "OPEN",
                "mergedAt": None,
                "updatedAt": "2026-03-16T00:00:00Z",
                "createdAt": "2026-02-20T00:00:00Z",
                "repository": {"nameWithOwner": "apolloconfig/apollo-java"},
                "author": {"login": "dave"},
                "reviews": {"nodes": []},
                "comments": {"nodes": []},
            },
        ]
        issue_nodes = [
            {
                "title": "Old issue with old and new comments",
                "url": "https://example/issues/20",
                "repository": {"nameWithOwner": "apolloconfig/apollo"},
                "author": {"login": "erin"},
                "comments": {
                    "nodes": [
                        {
                            "url": "https://example/issues/20/comments/1",
                            "createdAt": "2026-02-01T00:00:00Z",
                            "author": {"login": "frank"},
                        },
                        {
                            "url": "https://example/issues/20/comments/2",
                            "createdAt": "2026-03-17T00:00:00Z",
                            "author": {"login": "grace"},
                        },
                    ]
                },
            }
        ]

        contributors = cpr.aggregate_recent_activity(
            pr_nodes=pr_nodes,
            issue_nodes=issue_nodes,
            discussion_nodes_by_repo={},
            allowed_repos={"apolloconfig/apollo", "apolloconfig/apollo-java"},
            policy=policy,
            ignored_logins=set(),
            aliases={},
            cutoff=cutoff,
        )
        ranked = cpr.finalize_recent_contributors(contributors, {"scoreWeights": policy["scoreWeights"], "maxRepoBonus": 6})

        ranked_by_login = {item["login"]: item for item in ranked}
        self.assertNotIn("alice", ranked_by_login)
        self.assertNotIn("bob", ranked_by_login)
        self.assertEqual(1, ranked_by_login["carol"]["recentBreakdown"]["prComments"])
        self.assertEqual(4, ranked_by_login["dave"]["recentScore"])
        self.assertEqual(1, ranked_by_login["grace"]["recentBreakdown"]["issueComments"])
        self.assertNotIn("frank", ranked_by_login)

    def test_build_recommendation_prefers_committer_discussion_for_qualified_member(self):
        contributor = {
            "login": "alice",
            "currentRole": "member",
            "recentScore": 28,
            "recentRepos": ["apolloconfig/apollo", "apolloconfig/apollo-java"],
            "recentBreakdown": {
                "mergedPrs": 2,
                "openPrs": 0,
                "reviews": 5,
                "prComments": 1,
                "issueComments": 1,
                "discussionComments": 0,
                "repoBonus": 2,
                "qualifyingActivities": 7,
                "contributionTypes": 4,
            },
            "recentEvidence": [{"url": "https://example/pr/1", "kind": "merged_pr", "label": "Merged PR", "repo": "apolloconfig/apollo", "occurredAt": "2026-03-12T00:00:00Z", "score": 8}],
            "history": {
                "mergedPullRequests": 7,
                "authoredPullRequests": 8,
                "reviewedPullRequests": 9,
                "helpedIssues": 5,
                "discussionComments": 0,
                "communityHelpInteractions": 5,
                "activityQuarters": ["2025-Q4", "2026-Q1"],
                "activityQuarterCount": 2,
                "keyEvidence": [],
            },
        }

        recommendation, blockers = cpr.build_recommendation(
            contributor,
            {"member": 10, "committer_discussion": 24, "pmc_discussion": 30},
        )

        self.assertEqual(cpr.SECTION_COMMITTERS, recommendation["key"])
        self.assertEqual("Committer discussion", recommendation["label"])
        self.assertEqual([], blockers)

    def test_build_recommendation_downgrades_non_member_to_observe_first(self):
        contributor = {
            "login": "alice",
            "currentRole": "contributor",
            "recentScore": 30,
            "recentRepos": ["apolloconfig/apollo", "apolloconfig/apollo-java"],
            "recentBreakdown": {
                "mergedPrs": 2,
                "openPrs": 0,
                "reviews": 4,
                "prComments": 1,
                "issueComments": 1,
                "discussionComments": 0,
                "repoBonus": 2,
                "qualifyingActivities": 6,
                "contributionTypes": 4,
            },
            "recentEvidence": [{"url": "https://example/pr/1", "kind": "merged_pr", "label": "Merged PR", "repo": "apolloconfig/apollo", "occurredAt": "2026-03-12T00:00:00Z", "score": 8}],
            "history": {
                "mergedPullRequests": 8,
                "authoredPullRequests": 8,
                "reviewedPullRequests": 9,
                "helpedIssues": 5,
                "discussionComments": 0,
                "communityHelpInteractions": 5,
                "activityQuarters": ["2025-Q4", "2026-Q1"],
                "activityQuarterCount": 2,
                "keyEvidence": [],
            },
        }

        recommendation, blockers = cpr.build_recommendation(
            contributor,
            {"member": 10, "committer_discussion": 24, "pmc_discussion": 30},
        )

        self.assertEqual(cpr.SECTION_CONTINUE, recommendation["key"])
        self.assertEqual("Observe for member first", recommendation["label"])
        self.assertIn("Not currently recognized as member; observe for member first.", blockers)

    def test_render_summary_contains_all_sections(self):
        payload = {
            "generatedAt": "2026-03-28T00:00:00Z",
            "windowStart": "2025-12-18",
            "windowEnd": "2026-03-28",
            "org": "apolloconfig",
            "topN": 3,
            "reposScanned": ["apolloconfig/apollo"],
            "rankedContributors": [
                {
                    "login": "alice",
                    "currentRole": "member",
                    "recentScore": 28,
                    "recentRepos": ["apolloconfig/apollo"],
                    "recentBreakdown": {
                        "mergedPrs": 1,
                        "openPrs": 0,
                        "reviews": 4,
                        "prComments": 1,
                        "issueComments": 1,
                        "discussionComments": 0,
                        "repoBonus": 0,
                        "qualifyingActivities": 6,
                        "contributionTypes": 4,
                    },
                    "recentEvidence": [
                        {
                            "kind": "merged_pr",
                            "label": "Merged PR: example",
                            "repo": "apolloconfig/apollo",
                            "url": "https://example/pr/1",
                            "occurredAt": "2026-03-12T00:00:00Z",
                            "score": 8,
                        }
                    ],
                    "history": {
                        "mergedPullRequests": 7,
                        "authoredPullRequests": 8,
                        "reviewedPullRequests": 9,
                        "helpedIssues": 5,
                        "discussionComments": 0,
                        "communityHelpInteractions": 5,
                        "activityQuarters": ["2025-Q4", "2026-Q1"],
                        "activityQuarterCount": 2,
                        "keyEvidence": [],
                    },
                    "recommendation": {
                        "key": cpr.SECTION_COMMITTERS,
                        "label": "Committer discussion",
                        "rationale": ["Strong sustained member activity."],
                        "manualChecks": ["PMC nomination remains manual."],
                        "blockers": [],
                    },
                    "blockers": [],
                }
            ],
            "recommendations": {
                cpr.SECTION_MEMBERS: [],
                cpr.SECTION_COMMITTERS: [
                    {
                        "login": "alice",
                        "currentRole": "member",
                        "recentScore": 28,
                        "label": "Committer discussion",
                        "blockers": [],
                    }
                ],
                cpr.SECTION_PMCS: [],
                cpr.SECTION_CONTINUE: [],
            },
            "notes": ["Recent score includes merged/open PRs, reviews, PR comments, issue comments, and repository bonus."],
        }

        summary = cpr.render_summary(payload)

        self.assertIn("## Top 3 Recent Contributors", summary)
        self.assertIn("## Recommend Member", summary)
        self.assertIn("## Recommend Committer Discussion", summary)
        self.assertIn("## Recommend PMC Discussion", summary)
        self.assertIn("## Continue Observing / Manual Checks", summary)
        self.assertIn("https://example/pr/1", summary)


if __name__ == "__main__":
    unittest.main()
