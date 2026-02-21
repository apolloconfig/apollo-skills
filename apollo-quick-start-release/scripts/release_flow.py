#!/usr/bin/env python3
"""Apollo quick-start release orchestrator with checkpoint gating and resume support."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

UPSTREAM_REPO = "apolloconfig/apollo-quick-start"
DEFAULT_STATE_FILE = ".apollo-quick-start-release-state.json"
SYNC_WORKFLOW = "sync-apollo-release.yml"
DOCKER_WORKFLOW = "docker-publish.yml"
SYNC_BRANCH_PREFIX = "codex/quick-start-sync-"

CHECKPOINTS = {
    "TRIGGER_SYNC_WORKFLOW",
    "TRIGGER_DOCKER_WORKFLOW",
}

STATE_RESERVED_KEYS = {
    "release_version",
    "docker_tag",
    "steps",
    "timestamps",
}


class ReleaseFlowError(RuntimeError):
    """Raised when quick-start release flow hits a blocking issue."""


class CheckpointPending(ReleaseFlowError):
    """Raised when execution reaches a checkpoint without explicit confirmation."""


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


def parse_semver(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version or "")
    if not match:
        raise ValueError(f"Invalid semantic version: {version}")
    major_text, minor_text, patch_text = match.groups()
    return (int(major_text), int(minor_text), int(patch_text))


def select_workflow_run(
    runs: list[dict[str, Any]],
    started_at: datetime,
) -> Optional[dict[str, Any]]:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    threshold = started_at - timedelta(seconds=5)
    for run in runs:
        raw_created = run.get("createdAt")
        if not isinstance(raw_created, str):
            continue
        created_at = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
        if created_at >= threshold:
            candidates.append((created_at, run))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def is_pr_merged(pr_payload: dict[str, Any]) -> bool:
    merged_at = pr_payload.get("mergedAt")
    return isinstance(merged_at, str) and bool(merged_at.strip())


class ReleaseFlow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo_root = Path.cwd().resolve()
        self.state_path = (self.repo_root / args.state_file).resolve()
        self.state = self._load_state()
        self._validate_inputs()

    def _validate_inputs(self) -> None:
        try:
            parse_semver(self.args.release_version)
        except ValueError as exc:
            raise ReleaseFlowError("--release-version must be in x.y.z format") from exc

        existing_release_version = self.state.get("release_version")
        if existing_release_version and existing_release_version != self.args.release_version:
            raise ReleaseFlowError(
                "State file release_version mismatch. "
                f"state={existing_release_version}, arg={self.args.release_version}"
            )

        provided_docker_tag = (self.args.docker_tag or "").strip() or None
        existing_docker_tag = self.state.get("docker_tag")

        if provided_docker_tag and existing_docker_tag and provided_docker_tag != existing_docker_tag:
            raise ReleaseFlowError(
                "State file docker_tag mismatch. "
                f"state={existing_docker_tag}, arg={provided_docker_tag}"
            )

        resolved_docker_tag = provided_docker_tag or existing_docker_tag or self.args.release_version
        if not resolved_docker_tag:
            raise ReleaseFlowError("docker tag cannot be empty")

        self.state["release_version"] = self.args.release_version
        self.state["docker_tag"] = resolved_docker_tag
        self._save_state()

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ReleaseFlowError(
                    f"State file {self.state_path} is corrupted. "
                    "Delete it to restart, or fix the JSON manually."
                ) from exc
        return {
            "release_version": None,
            "docker_tag": None,
            "timestamps": {},
            "steps": {},
        }

    def _save_state(self) -> None:
        content = json.dumps(self.state, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.state_path.parent),
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)

        temp_path.replace(self.state_path)

    def _mark_timestamp(self, key: str) -> None:
        self.state.setdefault("timestamps", {})[key] = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def _step_done(self, key: str) -> bool:
        return bool(self.state.setdefault("steps", {}).get(key))

    def _mark_step_done(self, key: str, metadata: Optional[dict[str, Any]] = None) -> None:
        self.state.setdefault("steps", {})[key] = True
        if metadata:
            conflicts = STATE_RESERVED_KEYS.intersection(metadata.keys())
            if conflicts:
                raise ReleaseFlowError(
                    "Step metadata contains reserved keys: " + ", ".join(sorted(conflicts))
                )
            self.state.update(metadata)
        self._save_state()

    def _run_command(
        self,
        cmd: list[str],
        *,
        mutate: bool = False,
        check: bool = True,
        timeout_seconds: Optional[int] = None,
    ) -> CommandResult:
        if self.args.dry_run and mutate:
            print(f"[dry-run] {' '.join(cmd)}")
            return CommandResult(stdout="", stderr="", returncode=0)
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_text = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr_text = exc.stderr if isinstance(exc.stderr, str) else ""
            raise ReleaseFlowError(
                f"Command timed out after {timeout_seconds} seconds: {' '.join(cmd)}\n"
                f"stdout:\n{stdout_text}\n"
                f"stderr:\n{stderr_text}"
            ) from exc
        if check and completed.returncode != 0:
            raise ReleaseFlowError(
                f"Command failed ({completed.returncode}): {' '.join(cmd)}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        return CommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )

    def _checkpoint(self, name: str, message: str) -> None:
        if name not in CHECKPOINTS:
            raise ReleaseFlowError(f"Unknown checkpoint: {name}")
        if self.args.dry_run:
            print(f"[dry-run] checkpoint {name}: {message}")
            return

        confirm = self.args.confirm_checkpoint
        pending = self.state.get("pending_checkpoint")
        if confirm == name:
            self.state.pop("pending_checkpoint", None)
            self.state.pop("pending_message", None)
            self._save_state()
            return

        if pending != name:
            self.state["pending_checkpoint"] = name
            self.state["pending_message"] = message
            self._save_state()

        raise CheckpointPending(
            f"Reached checkpoint {name}: {message}\n"
            f"Re-run with --confirm-checkpoint {name} to continue."
        )

    def run(self) -> None:
        self._preflight()
        self._trigger_sync_workflow()
        self._ensure_sync_pr_ready()
        self._trigger_docker_workflow()
        self._print_final_report()

    def _preflight(self) -> None:
        if self._step_done("preflight"):
            return

        required_tools = ["gh", "git", "python3"]
        missing = [tool for tool in required_tools if shutil.which(tool) is None]
        if missing:
            raise ReleaseFlowError(f"Missing required tools: {', '.join(missing)}")

        if self.repo_root.name != "apollo-quick-start":
            raise ReleaseFlowError(
                "Current directory must be the apollo-quick-start repository root "
                "(directory name 'apollo-quick-start')."
            )
        if not (self.repo_root / ".github/workflows/docker-publish.yml").exists():
            raise ReleaseFlowError("Current directory does not contain .github/workflows/docker-publish.yml")
        if not (self.repo_root / "sql/apolloconfigdb.sql").exists():
            raise ReleaseFlowError("Current directory does not contain sql/apolloconfigdb.sql")

        if not self.args.skip_auth_check:
            auth_status = self._run_command(["gh", "auth", "status", "-h", "github.com"], check=True)
            scopes_match = re.search(r"Token scopes:\s*(.+)", auth_status.stdout)
            if scopes_match:
                scopes_text = scopes_match.group(1).strip()
                scopes = {
                    token.strip().strip("'").strip('"')
                    for token in scopes_text.split(",")
                    if token.strip()
                }
            else:
                scopes_text = auth_status.stdout
                scopes = {
                    token.strip()
                    for token in re.split(r"[\s,]+", scopes_text)
                    if token.strip()
                }
            for required_scope in ["repo", "workflow"]:
                if required_scope not in scopes:
                    raise ReleaseFlowError(
                        f"gh auth token is missing '{required_scope}' scope; scopes={sorted(scopes)}"
                    )

        if not self.args.allow_dirty:
            dirty = self._run_command(["git", "status", "--short"], check=True)
            dirty_lines = [
                line
                for line in dirty.stdout.splitlines()
                if not self._is_ignored_state_path(line)
            ]
            if dirty_lines:
                raise ReleaseFlowError(
                    "Working tree is not clean. Commit/stash changes first, "
                    "or pass --allow-dirty if you know what you are doing.\n"
                    + "\n".join(dirty_lines)
                )

        remotes = self._list_remotes()
        upstream_candidates = [name for name, slug in remotes.items() if slug == UPSTREAM_REPO]
        if not upstream_candidates:
            raise ReleaseFlowError("No git remote points to github.com/apolloconfig/apollo-quick-start")

        upstream_remote = sorted(upstream_candidates)[0]
        self._mark_step_done("preflight", {"upstream_remote": upstream_remote})
        self._mark_timestamp("preflight_completed_at")

    def _is_ignored_state_path(self, status_line: str) -> bool:
        if len(status_line) < 4:
            return False
        path_text = status_line[3:].strip()
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1].strip()
        try:
            state_rel = self.state_path.relative_to(self.repo_root).as_posix()
        except ValueError:
            state_rel = os.path.relpath(self.state_path, self.repo_root).replace("\\", "/")
        return path_text == state_rel

    def _list_remotes(self) -> dict[str, str]:
        output = self._run_command(["git", "remote", "-v"], check=True)
        remotes: dict[str, str] = {}
        for line in output.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            name, url = parts[0], parts[1]
            slug = self._normalize_github_slug(url)
            if slug:
                remotes[name] = slug
        return remotes

    @staticmethod
    def _normalize_github_slug(url: str) -> Optional[str]:
        patterns = [
            r"^https?://github\.com/([^/]+/[^/]+?)(?:\.git)?$",
            r"^git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
            r"^ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?$",
        ]
        for pattern in patterns:
            match = re.match(pattern, url)
            if match:
                return match.group(1)
        return None

    def _trigger_sync_workflow(self) -> None:
        if self._step_done("sync_workflow_completed"):
            return

        self._checkpoint(
            "TRIGGER_SYNC_WORKFLOW",
            "Trigger sync-apollo-release.yml to update quick-start assets and create/update release PR.",
        )

        if self.args.dry_run:
            self._mark_step_done(
                "sync_workflow_completed",
                {
                    "sync_workflow_run_id": 0,
                    "sync_workflow_url": "https://example.invalid/sync-workflow",
                    "sync_no_change": False,
                    "sync_pr_number": 0,
                    "sync_pr_url": "https://example.invalid/pull/0",
                    "sync_pr_merged": False,
                    "sync_pr_state": "OPEN",
                },
            )
            return

        started_at = datetime.now(timezone.utc)
        self._run_command(
            [
                "gh",
                "workflow",
                "run",
                SYNC_WORKFLOW,
                "--repo",
                UPSTREAM_REPO,
                "--ref",
                "master",
                "-f",
                f"release_version={self.args.release_version}",
            ],
            mutate=True,
            check=True,
        )

        run = self._wait_for_new_run(SYNC_WORKFLOW, started_at)
        run_id = int(run["databaseId"])
        self._run_command(
            [
                "gh",
                "run",
                "watch",
                str(run_id),
                "--repo",
                UPSTREAM_REPO,
                "--exit-status",
            ],
            mutate=True,
            check=True,
            timeout_seconds=self.args.watch_timeout_seconds,
        )

        self._mark_step_done(
            "sync_workflow_completed",
            {
                "sync_workflow_run_id": run_id,
                "sync_workflow_url": run.get("url"),
            },
        )
        self._mark_timestamp("sync_workflow_completed_at")
        self._refresh_sync_pr_state()

    def _ensure_sync_pr_ready(self) -> None:
        if self._step_done("sync_pr_ready_for_docker"):
            return

        if not self._step_done("sync_workflow_completed"):
            return

        if self.args.dry_run:
            self._mark_step_done("sync_pr_ready_for_docker")
            self._mark_timestamp("sync_pr_ready_for_docker_at")
            return

        self._refresh_sync_pr_state()
        if self.state.get("sync_no_change"):
            self._mark_step_done("sync_pr_ready_for_docker")
            self._mark_timestamp("sync_pr_ready_for_docker_at")
            return

        pr_number = self.state.get("sync_pr_number")
        pr_url = self.state.get("sync_pr_url")
        if not pr_number:
            raise ReleaseFlowError(
                "Sync workflow completed but no matching PR metadata found. "
                "Please inspect workflow logs and rerun."
            )

        if not self.state.get("sync_pr_merged"):
            raise ReleaseFlowError(
                f"Sync PR #{pr_number} is not merged yet ({pr_url}). "
                "Please merge it first, then rerun this command."
            )

        self._mark_step_done("sync_pr_ready_for_docker")
        self._mark_timestamp("sync_pr_ready_for_docker_at")

    def _refresh_sync_pr_state(self) -> None:
        branch = f"{SYNC_BRANCH_PREFIX}{self.args.release_version}"
        prs = self._find_sync_prs(branch=branch)
        if not prs:
            self.state["sync_no_change"] = True
            self.state["sync_pr_number"] = None
            self.state["sync_pr_url"] = None
            self.state["sync_pr_state"] = "NONE"
            self.state["sync_pr_merged"] = False
            self._save_state()
            return

        picked = sorted(prs, key=lambda pr: int(pr.get("number", 0)), reverse=True)[0]
        self.state["sync_no_change"] = False
        self.state["sync_pr_number"] = picked.get("number")
        self.state["sync_pr_url"] = picked.get("url")
        self.state["sync_pr_state"] = picked.get("state")
        self.state["sync_pr_merged"] = is_pr_merged(picked)
        self._save_state()

    def _find_sync_prs(self, branch: str) -> list[dict[str, Any]]:
        result = self._run_command(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                UPSTREAM_REPO,
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "20",
                "--json",
                "number,url,state,mergedAt,headRefName,baseRefName",
            ],
            check=True,
        )
        payload = json.loads(result.stdout or "[]")
        if not isinstance(payload, list):
            raise ReleaseFlowError(f"Unexpected PR list payload: {payload}")
        return payload

    def _trigger_docker_workflow(self) -> None:
        if self._step_done("docker_workflow_completed"):
            return

        self._checkpoint(
            "TRIGGER_DOCKER_WORKFLOW",
            "Trigger docker-publish.yml to publish quick-start Docker image.",
        )

        if self.args.dry_run:
            self._mark_step_done(
                "docker_workflow_completed",
                {
                    "docker_workflow_run_id": 0,
                    "docker_workflow_url": "https://example.invalid/docker-workflow",
                },
            )
            return

        started_at = datetime.now(timezone.utc)
        docker_tag = self.state["docker_tag"]
        self._run_command(
            [
                "gh",
                "workflow",
                "run",
                DOCKER_WORKFLOW,
                "--repo",
                UPSTREAM_REPO,
                "--ref",
                "master",
                "-f",
                f"tag={docker_tag}",
            ],
            mutate=True,
            check=True,
        )

        run = self._wait_for_new_run(DOCKER_WORKFLOW, started_at)
        run_id = int(run["databaseId"])
        self._run_command(
            [
                "gh",
                "run",
                "watch",
                str(run_id),
                "--repo",
                UPSTREAM_REPO,
                "--exit-status",
            ],
            mutate=True,
            check=True,
            timeout_seconds=self.args.watch_timeout_seconds,
        )

        self._mark_step_done(
            "docker_workflow_completed",
            {
                "docker_workflow_run_id": run_id,
                "docker_workflow_url": run.get("url"),
            },
        )
        self._mark_timestamp("docker_workflow_completed_at")

    def _wait_for_new_run(self, workflow: str, started_at: datetime) -> dict[str, Any]:
        timeout_at = datetime.now(timezone.utc) + timedelta(minutes=self.args.workflow_start_timeout_minutes)
        while True:
            result = self._run_command(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    UPSTREAM_REPO,
                    "--workflow",
                    workflow,
                    "--event",
                    "workflow_dispatch",
                    "--limit",
                    "20",
                    "--json",
                    "databaseId,createdAt,status,url,conclusion,headBranch,event",
                ],
                check=True,
            )
            runs = json.loads(result.stdout or "[]")
            if not isinstance(runs, list):
                raise ReleaseFlowError(f"Unexpected workflow runs payload for {workflow}: {runs}")
            matched = select_workflow_run(runs, started_at)
            if matched:
                return matched

            if datetime.now(timezone.utc) >= timeout_at:
                raise ReleaseFlowError(f"Timed out waiting for workflow run for {workflow}")
            time.sleep(self.args.poll_interval_seconds)

    def _print_final_report(self) -> None:
        report = {
            "release_version": self.state.get("release_version"),
            "docker_tag": self.state.get("docker_tag"),
            "sync_workflow_url": self.state.get("sync_workflow_url"),
            "sync_no_change": self.state.get("sync_no_change"),
            "sync_pr_number": self.state.get("sync_pr_number"),
            "sync_pr_url": self.state.get("sync_pr_url"),
            "sync_pr_state": self.state.get("sync_pr_state"),
            "sync_pr_merged": self.state.get("sync_pr_merged"),
            "docker_workflow_url": self.state.get("docker_workflow_url"),
            "pending_checkpoint": self.state.get("pending_checkpoint"),
        }
        print(json.dumps(report, indent=2, ensure_ascii=True))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apollo quick-start release flow orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run release flow")
    run.add_argument("--release-version", required=True, help="Release version in x.y.z format")
    run.add_argument(
        "--docker-tag",
        help="Docker tag passed to docker-publish workflow (default: release version)",
    )
    run.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    run.add_argument("--confirm-checkpoint")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--allow-dirty", action="store_true")
    run.add_argument("--skip-auth-check", action="store_true")
    run.add_argument("--poll-interval-seconds", type=int, default=20)
    run.add_argument("--workflow-start-timeout-minutes", type=int, default=10)
    run.add_argument("--watch-timeout-seconds", type=int, default=7200)

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command != "run":
        raise ReleaseFlowError(f"Unsupported command: {args.command}")

    try:
        flow = ReleaseFlow(args)
        flow.run()
    except CheckpointPending as exc:
        print(str(exc))
        return 2
    except ReleaseFlowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
