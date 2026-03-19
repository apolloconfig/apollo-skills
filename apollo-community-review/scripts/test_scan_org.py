from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scan_org


class ScanOrgTest(unittest.TestCase):
    def test_run_command_retries_retryable_gh_api_errors(self):
        error = subprocess.CalledProcessError(
            1,
            ["gh", "api", "repos/apolloconfig/apollo/issues"],
            stderr='Get "https://api.github.com/repos/apolloconfig/apollo/issues": EOF',
        )
        success = subprocess.CompletedProcess(
            ["gh", "api", "repos/apolloconfig/apollo/issues"],
            0,
            stdout='[]',
            stderr='',
        )

        with patch.object(scan_org.subprocess, "run", side_effect=[error, success]) as run_mock, patch.object(
            scan_org.time, "sleep"
        ) as sleep_mock:
            output = scan_org.run_command(["gh", "api", "repos/apolloconfig/apollo/issues"])

        self.assertEqual("[]", output)
        self.assertEqual(2, run_mock.call_count)
        sleep_mock.assert_called_once_with(1)

    def test_run_command_does_not_retry_non_gh_commands(self):
        error = subprocess.CalledProcessError(
            1,
            ["python3", "script.py"],
            stderr="EOF",
        )

        with patch.object(scan_org.subprocess, "run", side_effect=error) as run_mock, self.assertRaises(
            subprocess.CalledProcessError
        ):
            scan_org.run_command(["python3", "script.py"])

        self.assertEqual(1, run_mock.call_count)

    def test_build_repo_plan_orders_priority_and_skips_archived_disabled(self):
        policy = {
            "org": "apolloconfig",
            "defaultMaintainers": [],
            "excludeRepoPrefixes": ["apolloconfig/apollo-ghsa-"],
            "priorityRepos": [
                "apolloconfig/apollo",
                "apolloconfig/apollo-java",
            ],
            "repoMaintainers": {
                "apolloconfig/apollo-java": ["alice", "bob"],
            },
        }
        discovered = [
            {"full_name": "apolloconfig/apollo-openapi", "archived": False, "disabled": False, "private": False, "fork": False},
            {"full_name": "apolloconfig/apollo-java", "archived": False, "disabled": False, "private": True, "fork": False},
            {"full_name": "apolloconfig/apollo", "archived": False, "disabled": False, "private": False, "fork": False},
            {"full_name": "apolloconfig/archived-repo", "archived": True, "disabled": False, "private": False, "fork": False},
            {"full_name": "apolloconfig/disabled-repo", "archived": False, "disabled": True, "private": False, "fork": False},
            {"full_name": "apolloconfig/apollo-ghsa-1234", "archived": False, "disabled": False, "private": True, "fork": False},
        ]

        plan, skipped = scan_org.build_repo_plan(policy, discovered, actor="carol")

        self.assertEqual(
            ["apolloconfig/apollo", "apolloconfig/apollo-java", "apolloconfig/apollo-openapi"],
            [item["repo"] for item in plan],
        )
        self.assertTrue(plan[0]["priority"])
        self.assertTrue(plan[1]["priority"])
        self.assertFalse(plan[2]["priority"])
        self.assertEqual(["alice", "bob"], plan[1]["maintainers"])
        self.assertEqual(
            [
                {"repo": "apolloconfig/archived-repo", "reason": "archived"},
                {"repo": "apolloconfig/disabled-repo", "reason": "disabled"},
                {"repo": "apolloconfig/apollo-ghsa-1234", "reason": "excluded"},
            ],
            skipped,
        )

    def test_scan_organization_collects_candidates_and_errors(self):
        policy = {
            "org": "apolloconfig",
            "defaultMaintainers": [],
            "priorityRepos": ["apolloconfig/apollo"],
        }
        discovered = [
            {"full_name": "apolloconfig/apollo", "archived": False, "disabled": False, "private": False, "fork": False},
            {"full_name": "apolloconfig/apollo-java", "archived": False, "disabled": False, "private": False, "fork": False},
        ]

        def fake_run_scan(repo: str, actor: str, maintainers: list[str], lookback: int, state_file: Path):
            if repo == "apolloconfig/apollo":
                return [{"repo": repo, "number": 1}]
            raise __import__("subprocess").CalledProcessError(1, ["python3"], stderr="boom")

        with patch.object(scan_org, "list_org_repositories", return_value=discovered), patch.object(
            scan_org, "run_scan", side_effect=fake_run_scan
        ):
            payload = scan_org.scan_organization(
                policy,
                actor="nobodyiam",
                state_file=Path("/tmp/state.json"),
                lookback=4,
            )

        self.assertEqual(2, payload["repoCount"])
        self.assertEqual(1, payload["candidateCount"])
        self.assertEqual(1, payload["errorCount"])
        self.assertEqual([{"repo": "apolloconfig/apollo", "number": 1}], payload["candidates"])
        self.assertEqual("apolloconfig/apollo", payload["byRepo"][0]["repo"])
        self.assertEqual("apolloconfig/apollo-java", payload["errors"][0]["repo"])
        self.assertEqual("boom", payload["errors"][0]["error"])

    def test_merge_policy_overlays_operator_repo_maintainers(self):
        base = {
            "defaultMaintainers": ["base-user"],
            "repoMaintainers": {"apolloconfig/apollo": ["base-user"]},
        }
        override = {
            "actor": "carol",
            "repoMaintainers": {"apolloconfig/apollo-java": ["alice", "bob"]},
        }

        merged = scan_org.merge_policy(base, override)

        self.assertEqual("carol", merged["actor"])
        self.assertEqual(["base-user"], merged["repoMaintainers"]["apolloconfig/apollo"])
        self.assertEqual(["alice", "bob"], merged["repoMaintainers"]["apolloconfig/apollo-java"])

    def test_merge_policy_can_layer_shared_maintainers_then_operator(self):
        base = {
            "repoMaintainers": {"apolloconfig/apollo": ["shared-a"]},
        }
        maintainers = {
            "repoMaintainers": {"apolloconfig/apollo-java": ["shared-b"]},
        }
        operator = {
            "actor": "carol",
        }

        merged = scan_org.merge_policy(scan_org.merge_policy(base, maintainers), operator)

        self.assertEqual("carol", merged["actor"])
        self.assertEqual(["shared-a"], merged["repoMaintainers"]["apolloconfig/apollo"])
        self.assertEqual(["shared-b"], merged["repoMaintainers"]["apolloconfig/apollo-java"])


if __name__ == "__main__":
    unittest.main()
