#!/usr/bin/env python3
"""Scan accessible apolloconfig repositories for candidate community-review threads."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import community_review as cr

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
DEFAULT_POLICY = SKILL_ROOT / "references" / "repo-policy.json"
COMMUNITY_REVIEW = SCRIPT_DIR / "community_review.py"
RETRYABLE_GH_ERROR_MARKERS = (
    "eof",
    "tls handshake timeout",
    "connection reset by peer",
    "i/o timeout",
    "timed out",
    "bad gateway",
    "http 502",
    "http 503",
    "http 504",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--operator-file", type=Path, default=None)
    parser.add_argument("--maintainers-file", type=Path, default=None)
    parser.add_argument("--org", default=None, help="Override organization login; defaults to policy org")
    parser.add_argument("--actor", default=None, help="Override actor login; defaults to operator/env/gh auth")
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--initial-lookback-hours", type=int, default=4)
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser.parse_args()


def load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def merge_policy(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    if not override:
        return dict(base)
    merged = dict(base)
    for key, value in override.items():
        if key in {"repoMaintainers"}:
            combined = dict(base.get(key, {}))
            combined.update(value or {})
            merged[key] = combined
        else:
            merged[key] = value
    return merged


def is_gh_api_command(command: list[str]) -> bool:
    return len(command) >= 2 and command[0] == "gh" and command[1] == "api"


def is_retryable_gh_error(error: subprocess.CalledProcessError) -> bool:
    combined = "\n".join(part for part in [error.stdout or "", error.stderr or ""] if part).lower()
    return any(marker in combined for marker in RETRYABLE_GH_ERROR_MARKERS)


def run_command(command: list[str]) -> str:
    max_attempts = 3 if is_gh_api_command(command) else 1
    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            return result.stdout
        except subprocess.CalledProcessError as error:
            if attempt >= max_attempts or not is_retryable_gh_error(error):
                raise
            time.sleep(attempt)
    raise RuntimeError("unreachable")


def list_org_repositories(org: str) -> list[dict[str, Any]]:
    command = [
        "gh",
        "api",
        "--paginate",
        f"orgs/{org}/repos?per_page=100&type=all",
        "--jq",
        ".[] | @json",
    ]
    output = run_command(command)
    repositories: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        repositories.append(json.loads(line))
    return repositories


def resolve_maintainers(repo: str, policy: dict[str, Any], actor: str) -> list[str]:
    repo_maintainers = policy.get("repoMaintainers", {})
    maintainers = repo_maintainers.get(repo) or policy.get("defaultMaintainers") or [actor]
    return [item for item in maintainers if item]


def build_repo_plan(
    policy: dict[str, Any], discovered_repos: list[dict[str, Any]], actor: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    org = policy["org"]
    exclude_prefixes = tuple(policy.get("excludeRepoPrefixes", []))
    exclude_repos = set(policy.get("excludeRepos", []))
    by_name: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []

    for repo in discovered_repos:
        full_name = repo.get("full_name") or ""
        if not full_name.startswith(f"{org}/"):
            continue
        if full_name in exclude_repos or any(full_name.startswith(prefix) for prefix in exclude_prefixes):
            skipped.append({"repo": full_name, "reason": "excluded"})
            continue
        if repo.get("archived"):
            skipped.append({"repo": full_name, "reason": "archived"})
            continue
        if repo.get("disabled"):
            skipped.append({"repo": full_name, "reason": "disabled"})
            continue
        by_name[full_name] = {
            "repo": full_name,
            "private": bool(repo.get("private")),
            "fork": bool(repo.get("fork")),
            "maintainers": resolve_maintainers(full_name, policy, actor),
            "priority": False,
        }

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for repo_name in policy.get("priorityRepos", []):
        repo_entry = by_name.get(repo_name)
        if not repo_entry:
            continue
        repo_entry = dict(repo_entry)
        repo_entry["priority"] = True
        ordered.append(repo_entry)
        seen.add(repo_name)

    for repo_name in sorted(name for name in by_name if name not in seen):
        ordered.append(dict(by_name[repo_name]))

    return ordered, skipped


def run_scan(repo: str, actor: str, maintainers: list[str], lookback: int, state_file: Path) -> list[dict[str, Any]]:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(COMMUNITY_REVIEW),
        "scan",
        "--repo",
        repo,
        "--actor",
        actor,
        "--maintainers",
        ",".join(maintainers),
        "--initial-lookback-hours",
        str(lookback),
        "--state-file",
        str(state_file),
    ]
    output = run_command(command).strip() or "[]"
    return json.loads(output)


def scan_organization(policy: dict[str, Any], *, actor: str, state_file: Path, lookback: int) -> dict[str, Any]:
    repositories = list_org_repositories(policy["org"])
    repo_plan, skipped_repos = build_repo_plan(policy, repositories, actor)

    flat_candidates: list[dict[str, Any]] = []
    by_repo: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for entry in repo_plan:
        repo = entry["repo"]
        try:
            candidates = run_scan(repo, actor, entry["maintainers"], lookback, state_file)
            by_repo.append(
                {
                    "repo": repo,
                    "priority": entry["priority"],
                    "private": entry["private"],
                    "fork": entry["fork"],
                    "candidateCount": len(candidates),
                    "candidates": candidates,
                }
            )
            flat_candidates.extend(candidates)
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            error_entry = {
                "repo": repo,
                "priority": entry["priority"],
                "private": entry["private"],
                "fork": entry["fork"],
                "error": stderr or f"scan failed with exit code {error.returncode}",
            }
            by_repo.append(error_entry)
            errors.append(error_entry)

    return {
        "org": policy["org"],
        "actor": actor,
        "stateFile": str(state_file),
        "repoCount": len(repo_plan),
        "candidateCount": len(flat_candidates),
        "errorCount": len(errors),
        "skippedRepoCount": len(skipped_repos),
        "skippedRepos": skipped_repos,
        "errors": errors,
        "byRepo": by_repo,
        "candidates": flat_candidates,
    }


def main() -> None:
    args = parse_args()
    policy = load_json_file(args.policy_file)
    maintainers = load_json_file(args.maintainers_file) if args.maintainers_file else None
    operator = load_json_file(args.operator_file) if args.operator_file else None
    policy = merge_policy(policy, maintainers)
    policy = merge_policy(policy, operator)
    if args.org:
        policy = dict(policy)
        policy["org"] = args.org
    actor = cr.resolve_actor_login(args.actor or policy.get("actor"))
    state_file = cr.resolve_state_path(str(args.state_file) if args.state_file else None, actor)
    payload = scan_organization(
        policy,
        actor=actor,
        state_file=state_file,
        lookback=args.initial_lookback_hours,
    )
    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
