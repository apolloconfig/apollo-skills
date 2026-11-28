#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import release_flow
from release_flow import ReleaseFlow


class ReleaseFlowHelpersTest(unittest.TestCase):
    def test_parse_semver(self) -> None:
        self.assertEqual(release_flow.parse_semver("2.5.0"), (2, 5, 0))
        with self.assertRaises(ValueError):
            release_flow.parse_semver("2.5")
        with self.assertRaises(ValueError):
            release_flow.parse_semver("x.y.z")

    def test_default_docker_tag_follows_release_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = release_flow.parse_args(
                [
                    "run",
                    "--release-version",
                    "2.5.0",
                    "--state-file",
                    "state.json",
                    "--dry-run",
                ]
            )
            with mock.patch("pathlib.Path.cwd", return_value=Path(tmp)):
                flow = ReleaseFlow(args)
        self.assertEqual(flow.state["docker_tag"], "2.5.0")

    def test_publication_resumes_from_disk_without_a_second_dispatch(self) -> None:
        version = "3.1.0"
        archive = f"apollo-quick-start-{version}.zip"
        args = release_flow.parse_args(["run", "--release-version", version, "--docker-tag", "archive",
                                        "--state-file", "state.json", "--confirm-checkpoint", "TRIGGER_DOCKER_WORKFLOW"])
        dispatch = release_flow.CommandResult(json.dumps({"workflow_run_id": 42, "html_url": "run/42"}), "", 0)
        ok = release_flow.CommandResult("", "", 0)
        details = {"isDraft": False, "url": "release/3.1.0",
                   "assets": [{"name": name, "state": "uploaded"} for name in (archive, archive + ".sha256")]}

        with tempfile.TemporaryDirectory() as work, mock.patch("pathlib.Path.cwd", return_value=Path(work)):
            # A pre-ZIP run ID belongs to the old Docker-only flow.
            state = Path(work, "state.json")
            state.write_text(json.dumps({"steps": {"docker_workflow_completed": True}, "docker_workflow_run_id": 17}))
            responses = [dispatch, release_flow.ReleaseFlowError("watch timeout"), ok,
                         release_flow.ReleaseFlowError("release view timeout"),
                         release_flow.CommandResult(json.dumps({**details, "isDraft": True}), "", 0),
                         release_flow.CommandResult(json.dumps({**details, "assets": details["assets"][:1]}), "", 0),
                         release_flow.CommandResult(json.dumps(details), "", 0)]
            with mock.patch.object(ReleaseFlow, "_run_command", side_effect=responses) as command:
                with self.assertRaisesRegex(release_flow.ReleaseFlowError, "watch timeout"):
                    ReleaseFlow(args)._trigger_docker_workflow()
                self.assertEqual(json.loads(state.read_text())["docker_workflow_run_id"], 42)
                args.confirm_checkpoint = None
                with self.assertRaisesRegex(release_flow.ReleaseFlowError, "release view timeout"):
                    ReleaseFlow(args)._trigger_docker_workflow()
                self.assertTrue(json.loads(state.read_text())["steps"]["distribution_run_succeeded"])
                for _ in range(2):
                    with self.assertRaisesRegex(release_flow.ReleaseFlowError, "missing its published ZIP or checksum"):
                        ReleaseFlow(args)._trigger_docker_workflow()
                    self.assertFalse(ReleaseFlow(args)._step_done("distribution_workflow_completed"))
                ReleaseFlow(args)._trigger_docker_workflow()
                ReleaseFlow(args)._trigger_docker_workflow()
                calls = [call.args[0] for call in command.call_args_list]
                self.assertEqual([cmd[1:3] for cmd in calls],
                                 [["api", "--method"], ["run", "watch"], ["run", "watch"]] + [["release", "view"]] * 4)
                self.assertIn("inputs[release_version]=3.1.0", calls[0])
                self.assertIn("inputs[tag]=archive", calls[0])
                self.assertEqual([cmd[3] for cmd in calls[1:3]], ["42", "42"])
                self.assertEqual(json.loads(state.read_text())["release_url"], details["url"])

    def test_dry_run_preserves_saved_publication_state(self) -> None:
        with tempfile.TemporaryDirectory() as work, mock.patch("pathlib.Path.cwd", return_value=Path(work)):
            state = Path(work) / "state.json"
            original = '{"release_version":"3.1.0","steps":{}}'
            state.write_text(original)
            args = release_flow.parse_args(["run", "--release-version", "3.1.0", "--state-file",
                                            state.name, "--dry-run"])
            flow = ReleaseFlow(args)
            flow._trigger_docker_workflow()
            self.assertEqual(state.read_text(), original)

    def test_checkpoint_persistence_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            first_args = release_flow.parse_args(
                [
                    "run",
                    "--release-version",
                    "2.5.0",
                    "--state-file",
                    "state.json",
                ]
            )
            with mock.patch("pathlib.Path.cwd", return_value=repo_root):
                flow = ReleaseFlow(first_args)
                with self.assertRaises(release_flow.CheckpointPending):
                    flow._checkpoint("TRIGGER_SYNC_WORKFLOW", "trigger sync")

            state_path = repo_root / "state.json"
            pending = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["pending_checkpoint"], "TRIGGER_SYNC_WORKFLOW")

            second_args = release_flow.parse_args(
                [
                    "run",
                    "--release-version",
                    "2.5.0",
                    "--state-file",
                    "state.json",
                    "--confirm-checkpoint",
                    "TRIGGER_SYNC_WORKFLOW",
                ]
            )
            with mock.patch("pathlib.Path.cwd", return_value=repo_root):
                resumed = ReleaseFlow(second_args)
                resumed._checkpoint("TRIGGER_SYNC_WORKFLOW", "trigger sync")

            cleared = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("pending_checkpoint", cleared)
            self.assertNotIn("pending_message", cleared)

    def test_select_workflow_run(self) -> None:
        started_at = datetime(2026, 2, 21, 8, 0, 0, tzinfo=timezone.utc)
        runs = [
            {
                "databaseId": 1,
                "createdAt": "2026-02-21T07:59:10Z",
                "url": "https://example.com/runs/1",
            },
            {
                "databaseId": 2,
                "createdAt": "2026-02-21T08:00:05Z",
                "url": "https://example.com/runs/2",
            },
            {
                "databaseId": 3,
                "createdAt": "2026-02-21T08:00:01Z",
                "url": "https://example.com/runs/3",
            },
        ]

        selected = release_flow.select_workflow_run(runs, started_at)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["databaseId"], 2)

    def test_select_workflow_run_empty_list(self) -> None:
        started_at = datetime(2026, 2, 21, 8, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(release_flow.select_workflow_run([], started_at))

    def test_select_workflow_run_before_threshold(self) -> None:
        started_at = datetime(2026, 2, 21, 8, 0, 0, tzinfo=timezone.utc)
        runs = [
            {
                "databaseId": 1,
                "createdAt": "2026-02-21T07:59:54Z",
            },
            {
                "databaseId": 2,
                "createdAt": "2026-02-21T07:59:00Z",
            },
        ]
        self.assertIsNone(release_flow.select_workflow_run(runs, started_at))

    def test_select_workflow_run_threshold_boundary(self) -> None:
        started_at = datetime(2026, 2, 21, 8, 0, 0, tzinfo=timezone.utc)
        runs = [
            {
                "databaseId": 99,
                "createdAt": "2026-02-21T07:59:55Z",
            }
        ]
        selected = release_flow.select_workflow_run(runs, started_at)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["databaseId"], 99)

    def test_is_pr_merged(self) -> None:
        merged_pr = {"number": 12, "mergedAt": "2026-02-21T09:01:02Z"}
        open_pr = {"number": 13, "mergedAt": None}
        self.assertTrue(release_flow.is_pr_merged(merged_pr))
        self.assertFalse(release_flow.is_pr_merged(open_pr))

    def test_normalize_github_slug(self) -> None:
        cases = {
            "https://github.com/apolloconfig/apollo-quick-start.git": "apolloconfig/apollo-quick-start",
            "https://github.com/apolloconfig/apollo-quick-start": "apolloconfig/apollo-quick-start",
            "git@github.com:apolloconfig/apollo-quick-start.git": "apolloconfig/apollo-quick-start",
            "ssh://git@github.com/apolloconfig/apollo-quick-start.git": "apolloconfig/apollo-quick-start",
        }
        for raw, expected in cases.items():
            self.assertEqual(ReleaseFlow._normalize_github_slug(raw), expected)

    def test_corrupted_state_file_raises_release_flow_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_path = repo_root / "state.json"
            state_path.write_text('{"release_version": "2.5.0",', encoding="utf-8")
            args = release_flow.parse_args(
                [
                    "run",
                    "--release-version",
                    "2.5.0",
                    "--state-file",
                    "state.json",
                ]
            )
            with mock.patch("pathlib.Path.cwd", return_value=repo_root):
                with self.assertRaises(release_flow.ReleaseFlowError):
                    ReleaseFlow(args)

    def test_reserved_keys_include_checkpoint_fields(self) -> None:
        self.assertIn("pending_checkpoint", release_flow.STATE_RESERVED_KEYS)
        self.assertIn("pending_message", release_flow.STATE_RESERVED_KEYS)

    def test_find_sync_prs_invalid_json_raises_release_flow_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            args = release_flow.parse_args(
                [
                    "run",
                    "--release-version",
                    "2.5.0",
                    "--state-file",
                    "state.json",
                    "--dry-run",
                ]
            )
            with mock.patch("pathlib.Path.cwd", return_value=repo_root):
                flow = ReleaseFlow(args)
            with mock.patch.object(
                flow,
                "_run_command",
                return_value=release_flow.CommandResult(
                    stdout="{not-json",
                    stderr="simulated stderr",
                    returncode=0,
                ),
            ):
                with self.assertRaises(release_flow.ReleaseFlowError):
                    flow._find_sync_prs(branch="codex/quick-start-sync-2.5.0")

    def test_wait_for_new_run_invalid_json_raises_release_flow_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            args = release_flow.parse_args(
                [
                    "run",
                    "--release-version",
                    "2.5.0",
                    "--state-file",
                    "state.json",
                    "--dry-run",
                ]
            )
            with mock.patch("pathlib.Path.cwd", return_value=repo_root):
                flow = ReleaseFlow(args)
            with mock.patch.object(
                flow,
                "_run_command",
                return_value=release_flow.CommandResult(
                    stdout="{not-json",
                    stderr="simulated stderr",
                    returncode=0,
                ),
            ):
                with self.assertRaises(release_flow.ReleaseFlowError):
                    flow._wait_for_new_run(
                        workflow=release_flow.SYNC_WORKFLOW,
                        started_at=datetime(2026, 2, 21, 8, 0, 0, tzinfo=timezone.utc),
                    )


if __name__ == "__main__":
    unittest.main()
