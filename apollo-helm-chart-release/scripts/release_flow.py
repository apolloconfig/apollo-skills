#!/usr/bin/env python3
"""
Release workflow for apollo-helm-chart.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


REPO_REMOTE = "github.com/apolloconfig/apollo-helm-chart"
CHART_PATHS = {
    "apollo-portal": Path("apollo-portal/Chart.yaml"),
    "apollo-service": Path("apollo-service/Chart.yaml"),
}
REQUIRED_FILES = [
    CHART_PATHS["apollo-portal"],
    CHART_PATHS["apollo-service"],
    Path("docs/index.yaml"),
]
ALLOWED_EXACT_PATHS = {
    "apollo-portal/Chart.yaml",
    "apollo-service/Chart.yaml",
    "docs/index.yaml",
}
ALLOWED_GLOB_PATHS = [
    "docs/apollo-portal-*.tgz",
    "docs/apollo-service-*.tgz",
]


class FlowError(RuntimeError):
    pass


@dataclass
class ChartMeta:
    name: str
    path: Path
    version: str
    app_version: str


@dataclass
class RunContext:
    repo_root: Path
    executed_commands: List[str]
    planned_commands: List[str]


def command_display(command: List[str] | str) -> str:
    if isinstance(command, str):
        return command
    return shlex.join(command)


def run_command(
    command: List[str] | str,
    cwd: Path,
    executed: Optional[List[str]] = None,
    *,
    check: bool = True,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    if executed is not None:
        executed.append(command_display(command))

    completed = subprocess.run(
        command,
        cwd=cwd,
        shell=shell,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        message = [
            f"Command failed: {command_display(command)}",
            f"Exit code: {completed.returncode}",
        ]
        if completed.stdout.strip():
            message.append(f"STDOUT:\n{completed.stdout.rstrip()}")
        if completed.stderr.strip():
            message.append(f"STDERR:\n{completed.stderr.rstrip()}")
        raise FlowError("\n".join(message))
    return completed


def normalize_remote_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""

    if url.startswith("git@"):
        _, rhs = url.split("@", 1)
        host, path = rhs.split(":", 1)
    else:
        if "://" in url:
            rhs = url.split("://", 1)[1]
        else:
            rhs = url
        if "@" in rhs and rhs.index("@") < rhs.index("/"):
            rhs = rhs.split("@", 1)[1]
        if ":" in rhs and "/" not in rhs.split(":", 1)[0]:
            host, path = rhs.split(":", 1)
        else:
            host, path = rhs.split("/", 1)

    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path.lower()}"


def collect_remote_urls(repo_root: Path, executed: List[str]) -> List[str]:
    output = run_command(["git", "remote", "-v"], repo_root, executed).stdout
    urls: List[str] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls.append(parts[1].strip())
    return sorted(set(urls))


def require_expected_remote(repo_root: Path, executed: List[str]) -> List[str]:
    remote_urls = collect_remote_urls(repo_root, executed)
    normalized = [normalize_remote_url(url) for url in remote_urls]
    if REPO_REMOTE not in normalized:
        raise FlowError(
            "Repository remote check failed.\n"
            f"Expected at least one remote normalized to: {REPO_REMOTE}\n"
            f"Actual remotes: {normalized or ['<none>']}"
        )
    return normalized


def require_repo_layout(repo_root: Path) -> None:
    missing = [str(path) for path in REQUIRED_FILES if not (repo_root / path).exists()]
    if missing:
        raise FlowError(
            "Repository layout check failed. Required paths are missing:\n"
            + "\n".join(f"- {item}" for item in missing)
        )


def require_required_tools() -> Dict[str, bool]:
    tools = {"git": True, "helm": True, "gh": True}
    missing_required: List[str] = []
    for tool in tools:
        if shutil.which(tool) is None:
            tools[tool] = False
            if tool in {"git", "helm"}:
                missing_required.append(tool)
    if missing_required:
        raise FlowError(
            "Missing required tools: " + ", ".join(sorted(missing_required))
        )
    return tools


def ensure_no_root_tgz(repo_root: Path) -> None:
    root_tgz = sorted(path.name for path in repo_root.glob("*.tgz"))
    if root_tgz:
        raise FlowError(
            "Found existing *.tgz files in repository root. "
            "Please clean them first to avoid moving stale packages:\n"
            + "\n".join(f"- {name}" for name in root_tgz)
        )


def read_chart_meta(chart_name: str, chart_path: Path) -> ChartMeta:
    content = yaml.safe_load(chart_path.read_text(encoding="utf-8")) or {}
    version = str(content.get("version", "")).strip()
    app_version = str(content.get("appVersion", "")).strip()
    if not version or not app_version:
        raise FlowError(
            f"Invalid chart metadata in {chart_path}: "
            "'version' and 'appVersion' are required."
        )
    return ChartMeta(
        name=chart_name,
        path=chart_path,
        version=version,
        app_version=app_version,
    )


def read_all_chart_meta(repo_root: Path) -> Dict[str, ChartMeta]:
    charts: Dict[str, ChartMeta] = {}
    for chart_name, relative_path in CHART_PATHS.items():
        chart_path = repo_root / relative_path
        charts[chart_name] = read_chart_meta(chart_name, chart_path)
    return charts


def compare_chart_versions(left: str, right: str) -> int:
    left_main, _, left_pre = left.partition("-")
    right_main, _, right_pre = right.partition("-")

    left_parts = [int(part) if part.isdigit() else part for part in left_main.split(".")]
    right_parts = [int(part) if part.isdigit() else part for part in right_main.split(".")]
    max_len = max(len(left_parts), len(right_parts))

    for idx in range(max_len):
        lv = left_parts[idx] if idx < len(left_parts) else 0
        rv = right_parts[idx] if idx < len(right_parts) else 0
        if lv == rv:
            continue
        if isinstance(lv, int) and isinstance(rv, int):
            return 1 if lv > rv else -1
        return 1 if str(lv) > str(rv) else -1

    if left_pre == right_pre:
        return 0
    if not left_pre and right_pre:
        return 1
    if left_pre and not right_pre:
        return -1
    return 1 if left_pre > right_pre else -1


def read_latest_versions_from_index(index_path: Path) -> Dict[str, Optional[str]]:
    index_data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    entries = index_data.get("entries", {}) or {}

    latest: Dict[str, Optional[str]] = {}
    for chart_name in CHART_PATHS:
        chart_entries = entries.get(chart_name, []) or []
        versions = [
            str(item.get("version")).strip()
            for item in chart_entries
            if str(item.get("version", "")).strip()
        ]
        if not versions:
            latest[chart_name] = None
            continue

        selected = versions[0]
        for candidate in versions[1:]:
            if compare_chart_versions(candidate, selected) > 0:
                selected = candidate
        latest[chart_name] = selected
    return latest


def extract_version_changes_from_diff(
    diff_text: str,
) -> Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]]:
    temp: Dict[str, Dict[str, Dict[str, Optional[str]]]] = {
        chart_name: {} for chart_name in CHART_PATHS
    }
    current_chart: Optional[str] = None

    diff_start_patterns = {
        f"diff --git a/{path.as_posix()} b/{path.as_posix()}": chart_name
        for chart_name, path in CHART_PATHS.items()
    }
    change_pattern = re.compile(r"^([+-])(version|appVersion):\s*(\S+)\s*$")

    for raw_line in diff_text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("diff --git "):
            current_chart = diff_start_patterns.get(line)
            continue
        if current_chart is None:
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        matched = change_pattern.match(line)
        if not matched:
            continue

        sign = matched.group(1)
        field = matched.group(2)
        value = matched.group(3).strip().strip("'\"")

        field_state = temp[current_chart].setdefault(
            field, {"old": None, "new": None}
        )
        if sign == "-":
            field_state["old"] = value
        else:
            field_state["new"] = value

    result: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]] = {
        chart_name: {} for chart_name in CHART_PATHS
    }
    for chart_name, fields in temp.items():
        for field, values in fields.items():
            old = values.get("old")
            new = values.get("new")
            if old != new and (old is not None or new is not None):
                result[chart_name][field] = (old, new)
    return result


def detect_version_changes(repo_root: Path, executed: List[str]) -> Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]]:
    diff_output = run_command(
        [
            "git",
            "diff",
            "HEAD",
            "--",
            CHART_PATHS["apollo-portal"].as_posix(),
            CHART_PATHS["apollo-service"].as_posix(),
        ],
        repo_root,
        executed,
    ).stdout
    return extract_version_changes_from_diff(diff_output)


def has_any_version_change(
    changes: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]]
) -> bool:
    return any(fields for fields in changes.values())


def compute_docs_lagging(
    charts: Dict[str, ChartMeta],
    latest_versions: Dict[str, Optional[str]],
) -> Dict[str, bool]:
    result: Dict[str, bool] = {}
    for chart_name, chart in charts.items():
        published = latest_versions.get(chart_name)
        if published is None:
            result[chart_name] = True
            continue
        result[chart_name] = compare_chart_versions(chart.version, published) > 0
    return result


def validate_chart_consistency(
    charts: Dict[str, ChartMeta],
    allow_version_mismatch: bool,
) -> List[str]:
    warnings: List[str] = []
    portal = charts["apollo-portal"]
    service = charts["apollo-service"]
    if portal.version != service.version:
        message = (
            "Chart version mismatch detected: "
            f"apollo-portal={portal.version}, apollo-service={service.version}"
        )
        if not allow_version_mismatch:
            raise FlowError(
                f"{message}\n"
                "Use --allow-version-mismatch to continue with explicit override."
            )
        warnings.append(message)

    if portal.app_version != service.app_version:
        message = (
            "Chart appVersion mismatch detected: "
            f"apollo-portal={portal.app_version}, "
            f"apollo-service={service.app_version}"
        )
        if not allow_version_mismatch:
            raise FlowError(
                f"{message}\n"
                "Use --allow-version-mismatch to continue with explicit override."
            )
        warnings.append(message)

    return warnings


def parse_changed_paths(status_output: str) -> List[str]:
    paths: List[str] = []
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            old_path, new_path = path.split(" -> ", 1)
            paths.append(old_path.strip())
            paths.append(new_path.strip())
        else:
            paths.append(path)
    return sorted(set(paths))


def collect_changed_paths(repo_root: Path, executed: List[str]) -> List[str]:
    status_output = run_command(
        ["git", "status", "--porcelain"], repo_root, executed
    ).stdout
    return parse_changed_paths(status_output)


def is_allowed_changed_path(path: str) -> bool:
    if path in ALLOWED_EXACT_PATHS:
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in ALLOWED_GLOB_PATHS)


def find_disallowed_paths(paths: List[str]) -> List[str]:
    return sorted(path for path in paths if not is_allowed_changed_path(path))


def sanitize_branch_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    normalized = re.sub(r"-{2,}", "-", normalized)
    normalized = normalized.strip("-")
    return normalized or "unknown"


def build_release_branch(portal_version: str, service_version: str) -> str:
    if portal_version == service_version:
        return f"codex/helm-release-{sanitize_branch_component(portal_version)}"
    return (
        "codex/helm-release-"
        f"{sanitize_branch_component(portal_version)}-"
        f"{sanitize_branch_component(service_version)}"
    )


def build_commit_message(charts: Dict[str, ChartMeta]) -> Tuple[str, str]:
    portal = charts["apollo-portal"]
    service = charts["apollo-service"]
    title = (
        "chore(charts): release helm charts "
        f"(portal {portal.version}, service {service.version})"
    )
    body = (
        f"portal appVersion: {portal.app_version}\n"
        f"service appVersion: {service.app_version}"
    )
    return title, body


def render_pr_body(
    template_path: Path,
    charts: Dict[str, ChartMeta],
    artifacts: List[str],
    command_lines: List[str],
    lint_executed: bool,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    portal = charts["apollo-portal"]
    service = charts["apollo-service"]
    artifact_lines = "\n".join(f"- `{item}`" for item in artifacts)
    command_bullets = "\n".join(f"- `{line}`" for line in command_lines)
    lint_note = "passed for both charts" if lint_executed else "skipped by --skip-lint"

    return template.format(
        portal_version=portal.version,
        portal_app_version=portal.app_version,
        service_version=service.version,
        service_app_version=service.app_version,
        artifacts=artifact_lines,
        commands=command_bullets,
        lint_note=lint_note,
    ).strip() + "\n"


def ensure_branch(repo_root: Path, branch_name: str, executed: List[str]) -> str:
    current_branch = run_command(
        ["git", "branch", "--show-current"], repo_root, executed
    ).stdout.strip()
    if current_branch == branch_name:
        return "reused-current"

    exists = run_command(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        repo_root,
        executed,
        check=False,
    )
    if exists.returncode == 0:
        run_command(["git", "checkout", branch_name], repo_root, executed)
        return "checked-out-existing"

    run_command(["git", "checkout", "-b", branch_name], repo_root, executed)
    return "created-new"


def stage_and_commit(
    repo_root: Path,
    changed_paths: List[str],
    commit_title: str,
    commit_body: str,
    executed: List[str],
) -> None:
    to_stage = sorted(set(path for path in changed_paths if is_allowed_changed_path(path)))
    if not to_stage:
        raise FlowError("No allowed changed files found to stage for commit.")

    run_command(["git", "add", "--", *to_stage], repo_root, executed)
    staged = run_command(
        ["git", "diff", "--cached", "--name-only"], repo_root, executed
    ).stdout.strip()
    if not staged:
        raise FlowError("No staged changes found after git add.")

    run_command(
        ["git", "commit", "-m", commit_title, "-m", commit_body],
        repo_root,
        executed,
    )


def package_and_index(repo_root: Path, context: RunContext) -> List[str]:
    run_command(["helm", "package", "apollo-portal"], repo_root, context.executed_commands)
    run_command(["helm", "package", "apollo-service"], repo_root, context.executed_commands)
    run_command("mv *.tgz docs", repo_root, context.executed_commands, shell=True)
    run_command(["helm", "repo", "index", "."], repo_root / "docs", context.executed_commands)

    artifacts = [
        f"docs/apollo-portal-{read_chart_meta('apollo-portal', repo_root / CHART_PATHS['apollo-portal']).version}.tgz",
        f"docs/apollo-service-{read_chart_meta('apollo-service', repo_root / CHART_PATHS['apollo-service']).version}.tgz",
        "docs/index.yaml",
    ]
    return artifacts


def print_header(title: str) -> None:
    print(f"\n== {title} ==")


def run_flow(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    context = RunContext(repo_root=repo_root, executed_commands=[], planned_commands=[])

    print_header("Preflight")
    require_repo_layout(repo_root)
    normalized_remotes = require_expected_remote(repo_root, context.executed_commands)
    tools = require_required_tools()
    ensure_no_root_tgz(repo_root)
    print(f"- Repository root: {repo_root}")
    print(f"- Remote check: pass ({REPO_REMOTE})")
    print(f"- Normalized remotes: {', '.join(normalized_remotes)}")
    print("- Tool check: git=ok, helm=ok, gh=" + ("ok" if tools["gh"] else "missing"))
    if not tools["gh"]:
        print("- Note: gh is required later for PR creation gate commands.")

    charts = read_all_chart_meta(repo_root)
    latest_docs_versions = read_latest_versions_from_index(repo_root / "docs/index.yaml")
    docs_lagging = compute_docs_lagging(charts, latest_docs_versions)
    version_changes = detect_version_changes(repo_root, context.executed_commands)

    print_header("Version Detection")
    for chart_name in CHART_PATHS:
        chart = charts[chart_name]
        published = latest_docs_versions.get(chart_name) or "<none>"
        print(
            f"- {chart_name}: version={chart.version}, appVersion={chart.app_version}, "
            f"latest-docs={published}, docs-lagging={'yes' if docs_lagging[chart_name] else 'no'}"
        )

    if has_any_version_change(version_changes):
        print("- Trigger: detected version/appVersion changes in git diff.")
    else:
        if any(docs_lagging.values()):
            lagging_charts = [name for name, lagging in docs_lagging.items() if lagging]
            print(
                "- Trigger fallback: no version/appVersion changes in git diff, "
                f"but docs is behind for {', '.join(lagging_charts)}. Continue with warning."
            )
        else:
            raise FlowError(
                "No version/appVersion changes detected, and docs/index.yaml is not behind. "
                "Stop release flow."
            )

    warnings = validate_chart_consistency(charts, args.allow_version_mismatch)
    print_header("Consistency Check")
    if warnings:
        for warning in warnings:
            print(f"- Warning: {warning}")
    else:
        print("- Chart version/appVersion consistency: pass")

    lint_executed = False
    print_header("Lint")
    if args.skip_lint:
        print("- Skipped by --skip-lint")
    else:
        run_command(["helm", "lint", "apollo-portal"], repo_root, context.executed_commands)
        run_command(["helm", "lint", "apollo-service"], repo_root, context.executed_commands)
        lint_executed = True
        print("- helm lint apollo-portal: pass")
        print("- helm lint apollo-service: pass")

    artifacts: List[str]
    if args.dry_run:
        context.planned_commands.extend(
            [
                "helm package apollo-portal",
                "helm package apollo-service",
                "mv *.tgz docs",
                "(cd docs && helm repo index .)",
            ]
        )
        artifacts = [
            f"docs/apollo-portal-{charts['apollo-portal'].version}.tgz",
            f"docs/apollo-service-{charts['apollo-service'].version}.tgz",
            "docs/index.yaml",
        ]
    else:
        print_header("Package and Index")
        artifacts = package_and_index(repo_root, context)
        print("- Packaging and index update completed.")

    changed_paths = collect_changed_paths(repo_root, context.executed_commands)
    disallowed = find_disallowed_paths(changed_paths)
    print_header("Git Change Whitelist")
    if disallowed:
        print("- Changed paths:")
        for path in changed_paths:
            marker = "disallowed" if path in disallowed else "allowed"
            print(f"  - {path} ({marker})")
        raise FlowError("Found non-whitelisted changed files. Stop before commit.")

    if changed_paths:
        for path in changed_paths:
            print(f"- {path}")
    else:
        print("- No changed files detected.")

    branch_name = build_release_branch(
        charts["apollo-portal"].version, charts["apollo-service"].version
    )
    commit_title, commit_body = build_commit_message(charts)
    pr_title = (
        "Release Helm charts: "
        f"portal {charts['apollo-portal'].version}, "
        f"service {charts['apollo-service'].version}"
    )

    template_path = Path(__file__).resolve().parents[1] / "references" / "pr-template.md"
    pr_body = render_pr_body(
        template_path,
        charts,
        artifacts=artifacts,
        command_lines=context.executed_commands
        if not args.dry_run
        else context.executed_commands + context.planned_commands,
        lint_executed=lint_executed,
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="apollo-helm-pr-",
        delete=False,
        encoding="utf-8",
    ) as pr_body_file:
        pr_body_file.write(pr_body)
        pr_body_path = Path(pr_body_file.name)

    branch_action = "dry-run"
    if not args.dry_run:
        print_header("Branch and Commit")
        branch_action = ensure_branch(repo_root, branch_name, context.executed_commands)
        stage_and_commit(
            repo_root,
            changed_paths,
            commit_title,
            commit_body,
            context.executed_commands,
        )
        print(f"- Branch action: {branch_action}")
        print("- Commit created.")

    print_header("Release Draft")
    print(f"- Branch: {branch_name}")
    print(f"- Commit title: {commit_title}")
    print("- Commit body:")
    for line in commit_body.splitlines():
        print(f"  {line}")
    print(f"- PR title: {pr_title}")
    print(f"- PR body file: {pr_body_path}")

    print_header("Command Replay")
    if context.executed_commands:
        print("- Executed commands:")
        for cmd in context.executed_commands:
            print(f"  - {cmd}")
    if context.planned_commands:
        print("- Planned commands (dry-run only):")
        for cmd in context.planned_commands:
            print(f"  - {cmd}")

    print_header("Publish Gates (Explicit Confirmation Required)")
    print("1) Push gate (run only after explicit confirmation):")
    print(f"   git push -u origin {branch_name}")
    print("2) PR gate (run only after explicit confirmation):")
    print(
        "   gh pr create "
        f"--title {shlex.quote(pr_title)} "
        f"--body-file {shlex.quote(str(pr_body_path))}"
    )
    print("   # Ready for review by default (no --draft).")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Release flow for apollo-helm-chart with local automation and publish gates."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Execute the release flow.")
    run_parser.add_argument(
        "--allow-version-mismatch",
        action="store_true",
        help="Allow portal/service version or appVersion mismatch with explicit override.",
    )
    run_parser.add_argument(
        "--skip-lint",
        action="store_true",
        help="Skip helm lint for both charts.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run checks and planning output without mutating repository files.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command != "run":
        parser.error("Unsupported command.")

    try:
        return run_flow(args)
    except FlowError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
