#!/usr/bin/env python3
"""Apollo formal release orchestrator with checkpoint gating and resume support."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from github_discussion import create_discussion  # noqa: E402
from release_notes_builder import (  # noqa: E402
    build_release_content,
    normalize_tag,
    parse_highlight_pr_numbers,
)

UPSTREAM_REPO = "apolloconfig/apollo"
DEFAULT_STATE_FILE = ".apollo-release-state.json"
PACKAGE_WORKFLOW = "release-packages.yml"
DOCKER_WORKFLOW = "docker-publish.yml"
CHECKPOINTS = {
    "PUSH_RELEASE_PR",
    "CREATE_PRERELEASE",
    "TRIGGER_PACKAGE_WORKFLOW",
    "TRIGGER_DOCKER_WORKFLOW",
    "PROMOTE_RELEASE",
    "CREATE_ANNOUNCEMENT_DISCUSSION",
    "MANAGE_MILESTONES",
    "PUSH_POST_RELEASE_PR",
}


class ReleaseFlowError(RuntimeError):
    """Raised when release flow encounters a blocking issue."""


class CheckpointPending(ReleaseFlowError):
    """Raised when execution reaches a checkpoint without explicit confirmation."""


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class ReleaseFlow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo_root = Path.cwd().resolve()
        self.state_path = (self.repo_root / args.state_file).resolve()
        self.state = self._load_state()
        self._validate_inputs()

    def _validate_inputs(self) -> None:
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.args.release_version):
            raise ReleaseFlowError("--release-version must be in x.y.z format")
        if not re.fullmatch(r"\d+\.\d+\.\d+-SNAPSHOT", self.args.next_snapshot):
            raise ReleaseFlowError("--next-snapshot must be in x.y.z-SNAPSHOT format")
        if self.args.previous_tag:
            try:
                self.args.previous_tag = normalize_tag(self.args.previous_tag)
            except ValueError as exc:
                raise ReleaseFlowError(f"--previous-tag is invalid: {exc}") from exc

        existing_release_version = self.state.get("release_version")
        existing_next_snapshot = self.state.get("next_snapshot")
        if existing_release_version and existing_release_version != self.args.release_version:
            raise ReleaseFlowError(
                "State file release_version mismatch. "
                f"state={existing_release_version}, arg={self.args.release_version}"
            )
        if existing_next_snapshot and existing_next_snapshot != self.args.next_snapshot:
            raise ReleaseFlowError(
                "State file next_snapshot mismatch. "
                f"state={existing_next_snapshot}, arg={self.args.next_snapshot}"
            )

        highlight_prs_arg = (self.args.highlight_prs or "").strip()
        parsed_highlight_prs: Optional[list[int]] = None
        if highlight_prs_arg:
            try:
                parsed_highlight_prs = parse_highlight_pr_numbers(highlight_prs_arg)
            except ValueError as exc:
                raise ReleaseFlowError(f"--highlight-prs is invalid: {exc}") from exc

        existing_highlight_prs = self.state.get("highlight_prs")
        if parsed_highlight_prs and existing_highlight_prs:
            if list(existing_highlight_prs) != parsed_highlight_prs:
                raise ReleaseFlowError(
                    "State file highlight_prs mismatch. "
                    f"state={existing_highlight_prs}, arg={parsed_highlight_prs}"
                )

        resolved_highlight_prs = parsed_highlight_prs or existing_highlight_prs
        if not resolved_highlight_prs:
            raise ReleaseFlowError(
                "--highlight-prs is required, e.g. --highlight-prs 5336,5361,5365"
            )

        self.state["release_version"] = self.args.release_version
        self.state["next_snapshot"] = self.args.next_snapshot
        self.state["highlight_prs"] = list(resolved_highlight_prs)
        self._save_state()

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {
            "release_version": None,
            "next_snapshot": None,
            "timestamps": {},
            "steps": {},
        }

    def _save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _mark_timestamp(self, key: str) -> None:
        self.state.setdefault("timestamps", {})[key] = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def _step_done(self, key: str) -> bool:
        return bool(self.state.setdefault("steps", {}).get(key))

    def _mark_step_done(self, key: str, metadata: Optional[dict[str, Any]] = None) -> None:
        self.state.setdefault("steps", {})[key] = True
        if metadata:
            self.state.update(metadata)
        self._save_state()

    def _run_command(
        self,
        cmd: list[str],
        *,
        mutate: bool = False,
        check: bool = True,
    ) -> CommandResult:
        if self.args.dry_run and mutate:
            print(f"[dry-run] {' '.join(cmd)}")
            return CommandResult(stdout="", stderr="", returncode=0)
        completed = subprocess.run(cmd, capture_output=True, text=True)
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
        self._prepare_release_pr()
        self._wait_release_pr_merge()
        self._create_prerelease()
        self._trigger_package_workflow()
        self._trigger_docker_workflow()
        self._promote_release()
        self._create_announcement_discussion()
        self._prepare_post_release_pr()
        self._print_final_report()

    def _preflight(self) -> None:
        if self._step_done("preflight"):
            return

        required_tools = ["gh", "git", "jq", "python3"]
        missing = [tool for tool in required_tools if shutil.which(tool) is None]
        if missing:
            raise ReleaseFlowError(f"Missing required tools: {', '.join(missing)}")

        if self.repo_root.name != "apollo":
            raise ReleaseFlowError(
                "Current directory must be the apollo repository root (directory name 'apollo')."
            )
        pom_path = self.repo_root / "pom.xml"
        if not pom_path.exists():
            raise ReleaseFlowError("Current directory does not contain pom.xml")
        if self._read_root_artifact_id(pom_path) != "apollo":
            raise ReleaseFlowError("Root pom.xml artifactId is not 'apollo'")

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
            if dirty.stdout.strip():
                raise ReleaseFlowError(
                    "Working tree is not clean. Commit/stash changes first, "
                    "or pass --allow-dirty if you know what you are doing."
                )

        remotes = self._list_remotes()
        upstream_candidates = [name for name, slug in remotes.items() if slug == UPSTREAM_REPO]
        if not upstream_candidates:
            raise ReleaseFlowError("No git remote points to github.com/apolloconfig/apollo")

        upstream_remote = sorted(upstream_candidates)[0]
        push_remote = self._detect_push_remote(remotes)
        push_owner = remotes[push_remote].split("/", 1)[0]

        self.state["upstream_remote"] = upstream_remote
        self.state["push_remote"] = push_remote
        self.state["push_owner"] = push_owner

        self._mark_step_done("preflight")
        self._mark_timestamp("preflight_completed_at")

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

    @staticmethod
    def _read_root_artifact_id(pom_path: Path) -> Optional[str]:
        content = pom_path.read_text(encoding="utf-8")
        match = re.search(r"<artifactId>([^<]+)</artifactId>", content)
        if not match:
            return None
        return match.group(1).strip()

    def _detect_push_remote(self, remotes: dict[str, str]) -> str:
        override = os.getenv("APOLLO_RELEASE_PUSH_REMOTE")
        if override:
            if override not in remotes:
                raise ReleaseFlowError(
                    f"APOLLO_RELEASE_PUSH_REMOTE={override} does not exist in git remotes"
                )
            return override

        non_upstream = [name for name, slug in remotes.items() if slug != UPSTREAM_REPO]
        if "origin" in non_upstream:
            return "origin"
        if non_upstream:
            return sorted(non_upstream)[0]
        if "origin" in remotes:
            return "origin"
        return sorted(remotes.keys())[0]

    def _read_revision(self) -> str:
        pom = (self.repo_root / "pom.xml").read_text(encoding="utf-8")
        match = re.search(r"<revision>([^<]+)</revision>", pom)
        if not match:
            raise ReleaseFlowError("Failed to locate <revision> in pom.xml")
        return match.group(1).strip()

    def _write_revision(self, revision: str) -> None:
        pom_path = self.repo_root / "pom.xml"
        content = pom_path.read_text(encoding="utf-8")
        new_content, count = re.subn(
            r"(<revision>)([^<]+)(</revision>)",
            rf"\g<1>{revision}\g<3>",
            content,
            count=1,
        )
        if count != 1:
            raise ReleaseFlowError("Failed to update <revision> in pom.xml")
        if self.args.dry_run:
            print(f"[dry-run] update pom.xml revision -> {revision}")
            return
        pom_path.write_text(new_content, encoding="utf-8")

    def _prepare_release_pr(self) -> None:
        if not self._step_done("release_pr_prepared"):
            release_branch = f"codex/release-{self.args.release_version}"
            upstream_remote = self.state["upstream_remote"]

            self._run_command(["git", "fetch", upstream_remote, "master"], mutate=True, check=True)
            self._run_command(
                ["git", "checkout", "-B", release_branch, f"{upstream_remote}/master"],
                mutate=True,
                check=True,
            )

            current_revision = self._read_revision()
            expected_snapshot = f"{self.args.release_version}-SNAPSHOT"
            if current_revision not in {expected_snapshot, self.args.release_version}:
                raise ReleaseFlowError(
                    "Unexpected current revision before release bump: "
                    f"{current_revision}. expected {expected_snapshot}."
                )

            self._write_revision(self.args.release_version)
            if not self.args.dry_run:
                self._run_command(["git", "add", "pom.xml"], mutate=True, check=True)
                diff_cached = self._run_command(["git", "diff", "--cached", "--name-only"], check=True)
                if "pom.xml" not in diff_cached.stdout:
                    raise ReleaseFlowError("No staged pom.xml change detected for release bump")
                self._run_command(
                    ["git", "commit", "-m", f"chore: bump version to {self.args.release_version}"],
                    mutate=True,
                    check=True,
                )

            body_path = self.repo_root / ".git" / f"release-pr-{self.args.release_version}.md"
            body_path.write_text(self._render_release_pr_body(self.args.release_version), encoding="utf-8")

            self._mark_step_done(
                "release_pr_prepared",
                {
                    "release_branch": release_branch,
                    "release_pr_body_path": str(body_path),
                },
            )
            self._mark_timestamp("release_pr_prepared_at")

        if self._step_done("release_pr_created"):
            return

        self._checkpoint(
            "PUSH_RELEASE_PR",
            "Push release branch and create the release preparation PR.",
        )

        if self.args.dry_run:
            self._mark_step_done(
                "release_pr_created",
                {
                    "release_pr_url": "https://example.invalid/release-pr",
                    "release_pr_number": 0,
                },
            )
            return

        push_remote = self.state["push_remote"]
        self._run_command(
            ["git", "push", "-u", push_remote, self.state["release_branch"]],
            mutate=True,
            check=True,
        )

        head_ref = self._build_head_ref(self.state["push_owner"], self.state["release_branch"])
        create_pr = self._run_command(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                UPSTREAM_REPO,
                "--base",
                "master",
                "--head",
                head_ref,
                "--title",
                f"Release Apollo {self.args.release_version}",
                "--body-file",
                self.state["release_pr_body_path"],
            ],
            mutate=True,
            check=True,
        )
        pr_url = self._extract_url(create_pr.stdout)
        pr_number = self._extract_pr_number(pr_url)

        self._mark_step_done(
            "release_pr_created",
            {
                "release_pr_url": pr_url,
                "release_pr_number": pr_number,
            },
        )
        self._mark_timestamp("release_pr_created_at")

    def _wait_release_pr_merge(self) -> None:
        if self._step_done("release_pr_merged"):
            return
        if not self._step_done("release_pr_created"):
            raise ReleaseFlowError("release_pr_created step must complete before waiting merge")

        if self.args.dry_run:
            self._mark_step_done("release_pr_merged")
            return

        pr_number = self.state["release_pr_number"]
        timeout_at = datetime.now(timezone.utc) + timedelta(minutes=self.args.pr_merge_timeout_minutes)

        while True:
            view = self._run_command(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    UPSTREAM_REPO,
                    "--json",
                    "state,mergedAt,url",
                ],
                check=True,
            )
            payload = json.loads(view.stdout)
            if payload.get("mergedAt"):
                self._mark_step_done("release_pr_merged")
                self._mark_timestamp("release_pr_merged_at")
                return
            if payload.get("state") == "CLOSED":
                raise ReleaseFlowError(f"Release PR #{pr_number} was closed without merge")
            if datetime.now(timezone.utc) >= timeout_at:
                raise ReleaseFlowError(
                    f"Timed out waiting for PR #{pr_number} to merge. Re-run later to continue."
                )
            print(f"Waiting for PR #{pr_number} to merge...")
            time.sleep(self.args.poll_interval_seconds)

    def _create_prerelease(self) -> None:
        if self._step_done("prerelease_created"):
            return

        notes_path = self.repo_root / ".git" / f"release-notes-{self.args.release_version}.md"
        content = build_release_content(
            repo=UPSTREAM_REPO,
            release_version=self.args.release_version,
            changes_file=self.repo_root / "CHANGES.md",
            target_commitish="master",
            highlight_pr_numbers=list(self.state.get("highlight_prs", [])),
            previous_tag_name=self.args.previous_tag,
            delta_src_root=self.repo_root / "scripts/sql/src/delta",
            profiles_delta_root=Path("scripts/sql/profiles/mysql-default/delta"),
        )
        notes_path.write_text(content["release_notes"], encoding="utf-8")

        self.state["release_notes_path"] = str(notes_path)
        announcement_path = self.repo_root / ".git" / f"announcement-{self.args.release_version}.md"
        self.state["announcement_body_path"] = str(announcement_path)
        announcement_path.write_text(content["announcement"], encoding="utf-8")
        self.state["release_previous_tag"] = content.get("previous_tag")
        self.state["highlight_candidates"] = content.get("highlights")
        self._save_state()

        print(f"Generated release notes draft: {self.state['release_notes_path']}")
        print(f"Generated announcement draft: {self.state['announcement_body_path']}")
        self._print_highlights(content.get("highlights", []))

        self._checkpoint(
            "CREATE_PRERELEASE",
            "Review generated release notes/announcement and confirm highlights wording before creating GitHub pre-release.",
        )

        if self.args.dry_run:
            self._mark_step_done(
                "prerelease_created",
                {
                    "release_tag": f"v{self.args.release_version}",
                    "release_url": "https://example.invalid/release",
                },
            )
            return

        tag = f"v{self.args.release_version}"
        cmd = [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            UPSTREAM_REPO,
            "--target",
            "master",
            "--title",
            f"Apollo {self.args.release_version} Release",
            "--notes-file",
            self.state["release_notes_path"],
            "--prerelease",
        ]
        output = self._run_command(cmd, mutate=True, check=True)
        release_url = self._extract_url(output.stdout)

        self._mark_step_done(
            "prerelease_created",
            {
                "release_tag": tag,
                "release_url": release_url,
            },
        )
        self._mark_timestamp("prerelease_created_at")

    def _print_highlights(self, highlights: list[dict[str, str]]) -> None:
        if not highlights:
            print("No highlights generated.")
            return
        print("Selected highlights:")
        for index, item in enumerate(highlights, start=1):
            title = item.get("title", "Release Update")
            body = item.get("body", "")
            print(f"  {index}. {title}")
            print(f"     {body}")

    def _trigger_package_workflow(self) -> None:
        if self._step_done("package_workflow_completed"):
            return

        self._checkpoint(
            "TRIGGER_PACKAGE_WORKFLOW",
            "Trigger release-packages.yml to build packages in GitHub Action and upload release assets.",
        )

        if self.args.dry_run:
            self._mark_step_done(
                "package_workflow_completed",
                {
                    "package_workflow_run_id": 0,
                    "package_workflow_url": "https://example.invalid/package-workflow",
                    "release_assets": self._expected_release_assets(),
                },
            )
            return

        started_at = datetime.now(timezone.utc)
        self._run_command(
            [
                "gh",
                "workflow",
                "run",
                PACKAGE_WORKFLOW,
                "--repo",
                UPSTREAM_REPO,
                "--ref",
                "master",
                "-f",
                f"release_tag=v{self.args.release_version}",
            ],
            mutate=True,
            check=True,
        )

        run = self._wait_for_new_run(PACKAGE_WORKFLOW, started_at)
        run_id = run["databaseId"]
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
        )

        assets = self._verify_release_assets()
        self._mark_step_done(
            "package_workflow_completed",
            {
                "package_workflow_run_id": run_id,
                "package_workflow_url": run.get("url"),
                "release_assets": assets,
            },
        )
        self._mark_timestamp("package_workflow_completed_at")

    def _verify_release_assets(self) -> list[str]:
        tag = f"v{self.args.release_version}"
        expected = self._expected_release_assets()
        timeout_at = datetime.now(timezone.utc) + timedelta(minutes=self.args.asset_verify_timeout_minutes)

        while True:
            view = self._run_command(
                [
                    "gh",
                    "release",
                    "view",
                    tag,
                    "--repo",
                    UPSTREAM_REPO,
                    "--json",
                    "assets,url",
                ],
                check=True,
            )
            payload = json.loads(view.stdout)
            assets = payload.get("assets", [])
            names = sorted({asset.get("name", "") for asset in assets if asset.get("name")})
            missing = sorted(set(expected) - set(names))
            if not missing:
                return names
            if datetime.now(timezone.utc) >= timeout_at:
                raise ReleaseFlowError(
                    "Release assets verification failed. Missing files after package workflow: "
                    f"{', '.join(missing)}"
                )
            print(f"Waiting for release assets to appear: {', '.join(missing)}")
            time.sleep(self.args.poll_interval_seconds)

    def _expected_release_assets(self) -> list[str]:
        version = self.args.release_version
        files = [
            f"apollo-configservice-{version}-github.zip",
            f"apollo-adminservice-{version}-github.zip",
            f"apollo-portal-{version}-github.zip",
        ]
        checksums = [f"{name}.sha1" for name in files]
        return sorted(files + checksums)

    def _trigger_docker_workflow(self) -> None:
        if self._step_done("docker_workflow_completed"):
            return

        self._checkpoint(
            "TRIGGER_DOCKER_WORKFLOW",
            "Trigger docker-publish.yml to publish Docker images.",
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
                f"version={self.args.release_version}",
            ],
            mutate=True,
            check=True,
        )

        run = self._wait_for_new_run(DOCKER_WORKFLOW, started_at)
        run_id = run["databaseId"]
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
            list_cmd = self._run_command(
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
                    "databaseId,createdAt,status,url,headBranch,event",
                ],
                check=True,
            )
            runs = json.loads(list_cmd.stdout)
            for run in runs:
                created = datetime.fromisoformat(run["createdAt"].replace("Z", "+00:00"))
                if created >= started_at - timedelta(seconds=5):
                    return run

            if datetime.now(timezone.utc) >= timeout_at:
                raise ReleaseFlowError(f"Timed out waiting for workflow run for {workflow}")
            time.sleep(self.args.poll_interval_seconds)

    def _promote_release(self) -> None:
        if self._step_done("release_promoted"):
            return
        if not self._step_done("prerelease_created"):
            return
        if not self._step_done("package_workflow_completed"):
            return
        if not self._step_done("docker_workflow_completed"):
            return

        self._checkpoint(
            "PROMOTE_RELEASE",
            "Promote prerelease to official release and mark it as latest.",
        )

        if self.args.dry_run:
            self._mark_step_done("release_promoted")
            return

        self._run_command(
            [
                "gh",
                "release",
                "edit",
                self.state["release_tag"],
                "--repo",
                UPSTREAM_REPO,
                "--prerelease=false",
                "--latest",
            ],
            mutate=True,
            check=True,
        )
        self._mark_step_done("release_promoted")
        self._mark_timestamp("release_promoted_at")

    @staticmethod
    def _extract_section_bullets(markdown: str, section_title: str) -> list[str]:
        section_pattern = re.compile(
            rf"^## {re.escape(section_title)}[ \t]*\r?$\n?([\s\S]*?)(?=^## |\Z)",
            re.MULTILINE,
        )
        match = section_pattern.search(markdown)
        if not match:
            return []
        lines: list[str] = []
        for line in match.group(1).splitlines():
            stripped = line.strip()
            if stripped.startswith("* "):
                lines.append(stripped)
        return lines

    @staticmethod
    def _extract_full_changelog(markdown: str) -> Optional[str]:
        match = re.search(r"\*\*Full Changelog\*\*:\s*(\S+)", markdown)
        if not match:
            return None
        return match.group(1)

    @classmethod
    def _render_announcement_from_release_notes(
        cls,
        release_version: str,
        release_notes_markdown: str,
        release_url: Optional[str],
    ) -> str:
        change_lines = cls._extract_section_bullets(release_notes_markdown, "What's Changed")
        full_changelog_url = cls._extract_full_changelog(release_notes_markdown)

        lines: list[str] = [
            "Hi all,",
            "",
            f"Apollo Team is glad to announce the release of Apollo {release_version}.",
            "",
            "This release includes the following changes.",
            "",
        ]

        if change_lines:
            lines.extend([line.replace("* ", "- ", 1) for line in change_lines])
        else:
            lines.append("- No user-facing changes were listed in release notes.")

        changelog_link = release_url or full_changelog_url
        if changelog_link:
            lines.extend(
                [
                    "",
                    "Please refer to the change log for the complete list of changes:",
                    changelog_link,
                ]
            )

        lines.extend(
            [
                "",
                "Apollo website: https://www.apolloconfig.com/",
                "",
                "Downloads: https://github.com/apolloconfig/apollo/releases",
                "",
                "Apollo Resources:",
                "GitHub: https://github.com/apolloconfig/apollo",
                "Issue: https://github.com/apolloconfig/apollo/issues",
                "Mailing list: [apollo-config@googlegroups.com](mailto:apollo-config@googlegroups.com)",
                "",
                "Apollo Team",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    def _sync_announcement_with_release_notes(self) -> None:
        release_notes_path_value = self.state.get("release_notes_path")
        announcement_path_value = self.state.get("announcement_body_path")
        if not release_notes_path_value or not announcement_path_value:
            return

        release_notes_path = Path(release_notes_path_value)
        announcement_path = Path(announcement_path_value)
        if not release_notes_path.exists():
            return

        announcement_text = self._render_announcement_from_release_notes(
            self.args.release_version,
            release_notes_path.read_text(encoding="utf-8"),
            self.state.get("release_url"),
        )
        announcement_path.write_text(announcement_text, encoding="utf-8")

    def _create_announcement_discussion(self) -> None:
        if self._step_done("announcement_done"):
            return

        self._sync_announcement_with_release_notes()

        self._checkpoint(
            "CREATE_ANNOUNCEMENT_DISCUSSION",
            "Create GitHub discussion in Announcements category.",
        )

        title = f"[Announcement] Apollo {self.args.release_version} released"
        body_path = Path(self.state["announcement_body_path"])

        if self.args.dry_run:
            self._mark_step_done(
                "announcement_done",
                {
                    "announcement_status": "dry_run",
                    "announcement_title": title,
                    "announcement_url": "https://example.invalid/discussion",
                },
            )
            return

        try:
            url = create_discussion(
                repo=UPSTREAM_REPO,
                category="Announcements",
                title=title,
                body=body_path.read_text(encoding="utf-8"),
            )
            metadata = {
                "announcement_status": "posted",
                "announcement_title": title,
                "announcement_url": url,
            }
        except Exception as exc:  # noqa: BLE001
            metadata = {
                "announcement_status": "manual_required",
                "announcement_title": title,
                "announcement_body_path": str(body_path),
                "announcement_error": str(exc),
                "announcement_manual_url": "https://github.com/apolloconfig/apollo/discussions/new?category=announcements",
            }

        self._mark_step_done("announcement_done", metadata)
        self._mark_timestamp("announcement_done_at")

    def _prepare_post_release_pr(self) -> None:
        if self._step_done("post_release_pr_created"):
            return

        branch_name = f"codex/post-release-{self.args.next_snapshot}"
        upstream_remote = self.state["upstream_remote"]
        next_release = self.args.next_snapshot.replace("-SNAPSHOT", "")

        if not self._step_done("post_release_branch_prepared"):
            self._run_command(["git", "fetch", upstream_remote, "master"], mutate=True, check=True)
            self._run_command(
                ["git", "checkout", "-B", branch_name, f"{upstream_remote}/master"],
                mutate=True,
                check=True,
            )

            archived_path = self.repo_root / "changes" / f"changes-{self.args.release_version}.md"
            current_changes = (self.repo_root / "CHANGES.md").read_text(encoding="utf-8")
            if self.args.dry_run:
                print(f"[dry-run] archive CHANGES.md -> {archived_path}")
            else:
                archived_path.write_text(current_changes, encoding="utf-8")

            self._write_revision(self.args.next_snapshot)
            self._write_changes_template(next_release, -1)

            self._mark_step_done(
                "post_release_branch_prepared",
                {
                    "post_release_branch": branch_name,
                    "changes_archive_path": str(archived_path),
                },
            )
            self._mark_timestamp("post_release_branch_prepared_at")

        if not self._step_done("milestones_managed"):
            self._checkpoint(
                "MANAGE_MILESTONES",
                "Close current milestone and create next release milestone.",
            )

            if self.args.dry_run:
                next_milestone_number = -1
            else:
                next_milestone_number = self._ensure_next_milestone(next_release)

            self._write_changes_template(next_release, next_milestone_number)
            self._mark_step_done(
                "milestones_managed",
                {
                    "next_milestone_number": next_milestone_number,
                },
            )
            self._mark_timestamp("milestones_managed_at")

        if not self._step_done("post_release_commit_created"):
            archived_path = self.state["changes_archive_path"]
            if not self.args.dry_run:
                self._run_command(
                    ["git", "add", "pom.xml", "CHANGES.md", str(archived_path)],
                    mutate=True,
                    check=True,
                )
                self._run_command(
                    ["git", "commit", "-m", f"chore: bump version to {self.args.next_snapshot}"],
                    mutate=True,
                    check=True,
                )
            self._mark_step_done("post_release_commit_created")
            self._mark_timestamp("post_release_commit_created_at")

        body_path = self.repo_root / ".git" / f"post-release-pr-{next_release}.md"
        body_path.write_text(self._render_post_release_pr_body(next_release), encoding="utf-8")

        self._checkpoint(
            "PUSH_POST_RELEASE_PR",
            "Push post-release branch and create snapshot bump PR.",
        )

        if self.args.dry_run:
            self._mark_step_done(
                "post_release_pr_created",
                {
                    "post_release_pr_url": "https://example.invalid/post-release-pr",
                    "post_release_branch": branch_name,
                },
            )
            return

        push_remote = self.state["push_remote"]
        self._run_command(
            ["git", "push", "-u", push_remote, branch_name],
            mutate=True,
            check=True,
        )
        head_ref = self._build_head_ref(self.state["push_owner"], branch_name)
        create_pr = self._run_command(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                UPSTREAM_REPO,
                "--base",
                "master",
                "--head",
                head_ref,
                "--title",
                f"Prepare Apollo {self.args.next_snapshot}",
                "--body-file",
                str(body_path),
            ],
            mutate=True,
            check=True,
        )
        pr_url = self._extract_url(create_pr.stdout)

        self._mark_step_done(
            "post_release_pr_created",
            {
                "post_release_pr_url": pr_url,
                "post_release_branch": branch_name,
            },
        )
        self._mark_timestamp("post_release_pr_created_at")

    def _ensure_next_milestone(self, next_release: str) -> int:
        milestones = self._gh_api_json("repos/apolloconfig/apollo/milestones?state=all&per_page=100")

        current_release_number = None
        next_milestone_number = None

        for milestone in milestones:
            title = milestone.get("title", "")
            number = milestone.get("number")
            state = milestone.get("state")
            if title == self.args.release_version:
                current_release_number = (number, state)
            if title == next_release:
                next_milestone_number = number

        if current_release_number and current_release_number[1] != "closed":
            number = current_release_number[0]
            self._gh_api_json(
                f"repos/apolloconfig/apollo/milestones/{number}",
                method="PATCH",
                fields={"state": "closed"},
            )

        if next_milestone_number is None:
            created = self._gh_api_json(
                "repos/apolloconfig/apollo/milestones",
                method="POST",
                fields={"title": next_release},
            )
            next_milestone_number = created["number"]

        return int(next_milestone_number)

    def _write_changes_template(self, next_release: str, milestone_number: int) -> None:
        milestone_ref = "TBD" if milestone_number < 0 else str(milestone_number)
        template = (
            "Changes by Version\n"
            "==================\n"
            "Release Notes.\n"
            "\n"
            f"Apollo {next_release}\n"
            "\n"
            "------------------\n"
            "* \n"
            "\n"
            "------------------\n"
            f"All issues and pull requests are [here](https://github.com/apolloconfig/apollo/milestone/{milestone_ref}?closed=1)\n"
        )
        if self.args.dry_run:
            print("[dry-run] rewrite CHANGES.md for next snapshot")
            return
        (self.repo_root / "CHANGES.md").write_text(template, encoding="utf-8")

    @staticmethod
    def _build_head_ref(owner: str, branch: str) -> str:
        if owner == "apolloconfig":
            return branch
        return f"{owner}:{branch}"

    def _gh_api_json(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        fields: Optional[dict[str, str]] = None,
    ) -> Any:
        cmd = ["gh", "api"]
        if method != "GET":
            cmd.extend(["-X", method])
        cmd.append(endpoint)
        if fields:
            for key, value in fields.items():
                cmd.extend(["-f", f"{key}={value}"])
        output = self._run_command(cmd, check=True)
        return json.loads(output.stdout)

    @staticmethod
    def _extract_url(raw_text: str) -> str:
        for token in raw_text.split():
            if token.startswith("https://"):
                return token.strip()
        raise ReleaseFlowError(f"Unable to find URL in output:\n{raw_text}")

    @staticmethod
    def _extract_pr_number(url: str) -> int:
        match = re.search(r"/pull/(\d+)$", url)
        if not match:
            raise ReleaseFlowError(f"Unable to parse PR number from URL: {url}")
        return int(match.group(1))

    @staticmethod
    def _render_release_pr_body(release_version: str) -> str:
        return (
            "## What's the purpose of this PR\n\n"
            f"Prepare Apollo {release_version} release by removing `-SNAPSHOT` from root revision.\n\n"
            "## Which issue(s) this PR fixes:\n"
            "Fixes #N/A (release task)\n\n"
            "## Brief changelog\n\n"
            f"- bump version to {release_version}\n\n"
            "Follow this checklist to help us incorporate your contribution quickly and easily:\n\n"
            "- [x] Read the [Contributing Guide](https://github.com/apolloconfig/apollo/blob/master/CONTRIBUTING.md) before making this pull request.\n"
            "- [x] Write a pull request description that is detailed enough to understand what the pull request does, how, and why.\n"
            "- [ ] Write necessary unit tests to verify the code.\n"
            "- [ ] Run `mvn clean test` to make sure this pull request doesn't break anything.\n"
            "- [ ] Run `mvn spotless:apply` to format your code.\n"
            "- [ ] Update the [`CHANGES` log](https://github.com/apolloconfig/apollo/blob/master/CHANGES.md).\n"
        )

    @staticmethod
    def _render_post_release_pr_body(next_release: str) -> str:
        return (
            "## What's the purpose of this PR\n\n"
            f"Post-release housekeeping for Apollo {next_release}: bump next SNAPSHOT and archive CHANGES.md.\n\n"
            "## Which issue(s) this PR fixes:\n"
            "Fixes #N/A (release task)\n\n"
            "## Brief changelog\n\n"
            f"- bump version to {next_release}-SNAPSHOT\n"
            "- archive CHANGES.md into changes/\n"
            "- refresh milestone link in CHANGES.md\n\n"
            "Follow this checklist to help us incorporate your contribution quickly and easily:\n\n"
            "- [x] Read the [Contributing Guide](https://github.com/apolloconfig/apollo/blob/master/CONTRIBUTING.md) before making this pull request.\n"
            "- [x] Write a pull request description that is detailed enough to understand what the pull request does, how, and why.\n"
            "- [ ] Write necessary unit tests to verify the code.\n"
            "- [ ] Run `mvn clean test` to make sure this pull request doesn't break anything.\n"
            "- [ ] Run `mvn spotless:apply` to format your code.\n"
            "- [x] Update the [`CHANGES` log](https://github.com/apolloconfig/apollo/blob/master/CHANGES.md).\n"
        )

    def _print_final_report(self) -> None:
        report = {
            "release_version": self.args.release_version,
            "next_snapshot": self.args.next_snapshot,
            "highlight_prs": self.state.get("highlight_prs"),
            "state_file": str(self.state_path),
            "release_pr_url": self.state.get("release_pr_url"),
            "release_url": self.state.get("release_url"),
            "package_workflow_url": self.state.get("package_workflow_url"),
            "docker_workflow_url": self.state.get("docker_workflow_url"),
            "announcement_status": self.state.get("announcement_status"),
            "announcement_url": self.state.get("announcement_url"),
            "post_release_pr_url": self.state.get("post_release_pr_url"),
        }
        print(json.dumps(report, indent=2, ensure_ascii=True))


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apollo release flow orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Execute release flow")
    run.add_argument("--release-version", required=True)
    run.add_argument("--next-snapshot", required=True)
    run.add_argument(
        "--highlight-prs",
        default="",
        help="Comma-separated PR numbers used for Highlights, e.g. 5336,5361,5365",
    )
    run.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    run.add_argument("--previous-tag", default=None)
    run.add_argument("--confirm-checkpoint", choices=sorted(CHECKPOINTS), default=None)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--allow-dirty", action="store_true")
    run.add_argument("--skip-auth-check", action="store_true")
    run.add_argument("--poll-interval-seconds", type=int, default=30)
    run.add_argument("--pr-merge-timeout-minutes", type=int, default=360)
    run.add_argument("--workflow-start-timeout-minutes", type=int, default=10)
    run.add_argument("--asset-verify-timeout-minutes", type=int, default=10)

    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    if args.command != "run":
        raise SystemExit("Unsupported command")

    flow = ReleaseFlow(args)
    try:
        flow.run()
    except CheckpointPending as exc:
        print(str(exc))
        return 0
    except ReleaseFlowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
