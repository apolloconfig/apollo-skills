#!/usr/bin/env python3
"""Scan Apollo contributors and produce promotion review recommendations."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
DEFAULT_POLICY_FILE = SKILL_ROOT / "references" / "review-policy.example.json"
TEAM_REPO = "apolloconfig/apollo"
TEAM_PATH = "docs/en/community/team.md"
MEMBERSHIP_REPO = "apolloconfig/apollo-community"

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
INVALID_SEARCH_USER_MARKERS = (
    "the listed users cannot be searched",
    "users do not exist or you do not have permission to view the users",
)
SEARCH_RATE_LIMIT_MARKERS = (
    "api rate limit exceeded",
    "secondary rate limit",
)
KNOWN_AUTOMATION_LOGINS = {
    "copilot",
    "copilot-pull-request-reviewer",
    "copilot-swe-agent",
    "coderabbitai",
    "codecov",
    "dependabot",
    "dependabot[bot]",
    "dosubot",
    "dosubot[bot]",
    "github-actions",
    "github-actions[bot]",
    "hound",
    "mergify",
    "mergify[bot]",
    "renovate",
    "renovate[bot]",
    "stale",
    "stale[bot]",
}
MEMBERSHIP_TITLE_RE = re.compile(r"REQUEST:\s+New membership for\s+<?([A-Za-z0-9-]+)>?", re.IGNORECASE)
MENTION_RE = re.compile(r"@([A-Za-z0-9-]+)")
RAW_LOGIN_RE = re.compile(r"\b([A-Za-z0-9-]{2,39})\b")
MEMBERSHIP_PLACEHOLDERS = {"your-github-username", "some-github-user-name", "github", "username"}
ACCEPTED_MEMBER_REPLY_PATTERNS = (
    "added to the apollo organization",
    "added to the apollo org",
    "check the invitation from github",
    "please check the invitation",
    "已加入",
    "请查收 github 邀请",
    "请查收github邀请",
)
SECTION_MEMBERS = "members"
SECTION_COMMITTERS = "committers"
SECTION_PMCS = "pmcs"
SECTION_CONTINUE = "continueObserving"

RECENT_PRS_QUERY = """
query($searchQuery: String!, $cursor: String) {
  search(query: $searchQuery, type: ISSUE, first: 50, after: $cursor) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on PullRequest {
        number
        title
        url
        isDraft
        state
        createdAt
        updatedAt
        mergedAt
        repository {
          nameWithOwner
        }
        author {
          login
        }
        reviews(first: 50) {
          nodes {
            url
            state
            submittedAt
            author {
              login
            }
          }
        }
        comments(first: 50) {
          nodes {
            url
            createdAt
            author {
              login
            }
          }
        }
      }
    }
  }
}
"""

RECENT_ISSUES_QUERY = """
query($searchQuery: String!, $cursor: String) {
  search(query: $searchQuery, type: ISSUE, first: 50, after: $cursor) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on Issue {
        number
        title
        url
        state
        createdAt
        updatedAt
        repository {
          nameWithOwner
        }
        author {
          login
        }
        comments(first: 50) {
          nodes {
            url
            createdAt
            author {
              login
            }
          }
        }
      }
    }
  }
}
"""

RECENT_DISCUSSIONS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 50, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        url
        createdAt
        updatedAt
        author {
          login
        }
        comments(first: 50) {
          nodes {
            url
            createdAt
            author {
              login
            }
          }
        }
      }
    }
  }
}
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def quarter_key(value: datetime | None) -> str | None:
    if value is None:
        return None
    quarter = (value.month - 1) // 3 + 1
    return f"{value.year}-Q{quarter}"


def normalize_login(login: str | None, aliases: dict[str, str] | None = None) -> str:
    normalized = (login or "").strip().lstrip("@").lower()
    if not normalized:
        return ""
    seen: set[str] = set()
    while aliases and normalized in aliases:
        if normalized in seen:
            break
        seen.add(normalized)
        target = aliases[normalized].strip().lstrip("@").lower()
        if not target or target == normalized:
            break
        normalized = target
    return normalized


def normalize_login_list(values: list[str] | None, aliases: dict[str, str] | None = None) -> set[str]:
    return {normalize_login(value, aliases) for value in values or [] if normalize_login(value, aliases)}


def normalize_alias_map(raw: dict[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in (raw or {}).items():
        alias = normalize_login(key)
        target = normalize_login(value)
        if alias and target:
            normalized[alias] = target
    return normalized


def is_automation_account(login: str) -> bool:
    normalized = normalize_login(login)
    if not normalized:
        return True
    return normalized in KNOWN_AUTOMATION_LOGINS or normalized.endswith("[bot]")


def is_human_login(login: str | None, ignored_logins: set[str], aliases: dict[str, str]) -> bool:
    normalized = normalize_login(login, aliases)
    if not normalized:
        return False
    if normalized in ignored_logins:
        return False
    return not is_automation_account(normalized)


def is_retryable_gh_error(error: subprocess.CalledProcessError) -> bool:
    combined = "\n".join(part for part in [error.stdout or "", error.stderr or ""] if part).lower()
    return any(marker in combined for marker in RETRYABLE_GH_ERROR_MARKERS)


def is_invalid_search_user_error(error: subprocess.CalledProcessError) -> bool:
    combined = "\n".join(part for part in [error.stdout or "", error.stderr or ""] if part).lower()
    return "validation failed" in combined and all(marker in combined for marker in INVALID_SEARCH_USER_MARKERS)


def is_search_rate_limit_error(error: subprocess.CalledProcessError) -> bool:
    combined = "\n".join(part for part in [error.stdout or "", error.stderr or ""] if part).lower()
    return "http 403" in combined and any(marker in combined for marker in SEARCH_RATE_LIMIT_MARKERS)


def wait_for_search_rate_limit_reset() -> None:
    payload = gh_api_json("rate_limit")
    search = payload.get("resources", {}).get("search", {})
    remaining = search.get("remaining")
    if remaining:
        return
    reset = search.get("reset")
    if reset is None:
        time.sleep(1)
        return
    delay_seconds = max(1, int(reset - time.time()) + 1)
    time.sleep(delay_seconds)


def run_command(command: list[str]) -> str:
    is_gh_api = len(command) >= 2 and command[0] == "gh" and command[1] == "api"
    attempts = 3 if is_gh_api else 1
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            return result.stdout
        except subprocess.CalledProcessError as error:
            if attempt >= attempts or not is_retryable_gh_error(error):
                raise
            time.sleep(attempt)
    raise RuntimeError("unreachable")


def gh_api_json(
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    paginate: bool = False,
) -> Any:
    command = ["gh", "api", "-X", method]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    command.append(endpoint)
    for key, value in (params or {}).items():
        if value is None:
            continue
        command.extend(["-F", f"{key}={value}"])
    payload = json.loads(run_command(command))
    if not paginate:
        return payload
    flattened: list[Any] = []
    for page in payload:
        if isinstance(page, list):
            flattened.extend(page)
        else:
            flattened.append(page)
    return flattened


def gh_graphql_json(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in (variables or {}).items():
        if value is None:
            continue
        command.extend(["-F", f"{key}={value}"])
    return json.loads(run_command(command))


def gh_repo_endpoint(repo: str, path: str, params: dict[str, Any] | None = None) -> str:
    clean_path = path.lstrip("/")
    if not params:
        return f"repos/{repo}/{clean_path}"
    return f"repos/{repo}/{clean_path}?{urlencode({key: value for key, value in params.items() if value is not None})}"


def parse_repo_name_from_url(repo_url: str | None) -> str:
    if not repo_url:
        return ""
    parts = repo_url.rstrip("/").split("/")
    if len(parts) < 2:
        return ""
    return f"{parts[-2]}/{parts[-1]}"


def occurred_at_or_after(timestamp: str | None, cutoff: datetime) -> bool:
    occurred_at = iso_to_datetime(timestamp)
    if occurred_at is None:
        return False
    return occurred_at >= cutoff


def load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_scan_payload(path: str) -> dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text())


def render_json(payload: dict[str, Any], pretty: bool) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return json.dumps(payload, ensure_ascii=False)


def load_policy(path: Path, lookback_days: int | None = None, top_n: int | None = None) -> dict[str, Any]:
    policy = load_json_file(path)
    if lookback_days is not None:
        policy["lookbackDays"] = lookback_days
    if top_n is not None:
        policy["topN"] = top_n
    return policy


def load_role_overrides(path: Path | None) -> dict[str, Any]:
    overrides = {"members": [], "committers": [], "pmcs": [], "ignoredLogins": [], "aliases": {}}
    if path is None:
        return overrides
    payload = load_json_file(path)
    overrides.update(payload)
    return overrides


def load_repo_text(repo: str, path: str) -> str:
    payload = gh_api_json(gh_repo_endpoint(repo, f"contents/{path}"))
    content = payload.get("content", "")
    if payload.get("encoding") == "base64":
        return base64.b64decode(content).decode("utf-8")
    return content


def list_org_repositories(org: str) -> list[dict[str, Any]]:
    endpoint = f"orgs/{org}/repos?per_page=100&type=all"
    return gh_api_json(endpoint, paginate=True)


def build_repo_inventory(policy: dict[str, Any], discovered: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, str]]]:
    org = policy["org"]
    exclude_repos = set(policy.get("excludeRepos", []))
    exclude_prefixes = tuple(policy.get("excludeRepoPrefixes", []))
    repos: list[str] = []
    excluded: list[dict[str, str]] = []

    for repo in discovered:
        full_name = repo.get("full_name") or ""
        if not full_name.startswith(f"{org}/"):
            continue
        reason = ""
        if full_name in exclude_repos or any(full_name.startswith(prefix) for prefix in exclude_prefixes):
            reason = "excluded"
        elif repo.get("archived"):
            reason = "archived"
        elif repo.get("disabled"):
            reason = "disabled"
        if reason:
            excluded.append({"repo": full_name, "reason": reason})
            continue
        repos.append(full_name)
    return sorted(repos), excluded


def fetch_search_nodes(search_query: str, query: str) -> list[dict[str, Any]]:
    cursor: str | None = None
    nodes: list[dict[str, Any]] = []
    while True:
        payload = gh_graphql_json(query, {"searchQuery": search_query, "cursor": cursor})
        search = payload.get("data", {}).get("search", {})
        nodes.extend(node for node in search.get("nodes", []) if node)
        page_info = search.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
    return nodes


def fetch_recent_pull_requests(org: str, cutoff_date: str) -> list[dict[str, Any]]:
    return fetch_search_nodes(f"org:{org} is:pr updated:>={cutoff_date} sort:updated-desc", RECENT_PRS_QUERY)


def fetch_recent_issues(org: str, cutoff_date: str) -> list[dict[str, Any]]:
    return fetch_search_nodes(f"org:{org} is:issue updated:>={cutoff_date} sort:updated-desc", RECENT_ISSUES_QUERY)


def fetch_recent_discussions(repo: str, cutoff: datetime) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    cursor: str | None = None
    nodes: list[dict[str, Any]] = []
    while True:
        payload = gh_graphql_json(
            RECENT_DISCUSSIONS_QUERY,
            {"owner": owner, "name": name, "cursor": cursor},
        )
        discussions = payload.get("data", {}).get("repository", {}).get("discussions", {})
        page_nodes = discussions.get("nodes", [])
        if not page_nodes:
            break
        for node in page_nodes:
            updated_at = iso_to_datetime(node.get("updatedAt"))
            if updated_at is None or updated_at < cutoff:
                return nodes
            nodes.append(node)
        page_info = discussions.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
    return nodes


def parse_team_roles(markdown: str, aliases: dict[str, str] | None = None) -> dict[str, set[str]]:
    pmcs: set[str] = set()
    committers: set[str] = set()
    current_section = ""
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("### Project Management Committee"):
            current_section = SECTION_PMCS
            continue
        if line.startswith("### Committer"):
            current_section = SECTION_COMMITTERS
            continue
        if line.startswith("### Contributors") or line.startswith("## "):
            current_section = ""
        if not current_section or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells:
            continue
        if cells[0] in {"GitHub ID", "-----------", "----------- ", "-----------"}:
            continue
        if set(cells[0]) == {"-"}:
            continue
        login = normalize_login(cells[0], aliases)
        if not login:
            continue
        if current_section == SECTION_PMCS:
            pmcs.add(login)
        elif current_section == SECTION_COMMITTERS:
            committers.add(login)
    return {SECTION_PMCS: pmcs, SECTION_COMMITTERS: committers}


def extract_candidate_login_from_request(issue: dict[str, Any], aliases: dict[str, str]) -> str:
    body = issue.get("body") or ""
    section_match = re.search(r"### GitHub Username(.*?)(?:\n### |\Z)", body, re.S | re.I)
    if section_match:
        section = section_match.group(1)
        for mention in MENTION_RE.findall(section):
            login = normalize_login(mention, aliases)
            if login and login not in MEMBERSHIP_PLACEHOLDERS:
                return login
        for line in section.splitlines():
            stripped = line.strip().strip("<>").lstrip("@")
            login_match = RAW_LOGIN_RE.fullmatch(stripped)
            if not login_match:
                continue
            login = normalize_login(login_match.group(1), aliases)
            if login and login not in MEMBERSHIP_PLACEHOLDERS:
                return login
    title_match = MEMBERSHIP_TITLE_RE.search(issue.get("title") or "")
    if title_match:
        login = normalize_login(title_match.group(1), aliases)
        if login and login not in MEMBERSHIP_PLACEHOLDERS:
            return login
    return ""


def membership_request_is_accepted(
    comments: list[dict[str, Any]],
    pmcs: set[str],
    maintainers: set[str],
    aliases: dict[str, str],
) -> bool:
    sponsors: set[str] = set()
    for comment in comments:
        login = normalize_login(comment.get("user", {}).get("login"), aliases)
        body = (comment.get("body") or "").strip().lower()
        if login in pmcs and body == "+1":
            sponsors.add(login)
        if login in maintainers and any(pattern in body for pattern in ACCEPTED_MEMBER_REPLY_PATTERNS):
            return True
    return len(sponsors) >= 2


def resolve_members_from_membership_requests(
    issues: list[dict[str, Any]],
    comments_by_issue: dict[int, list[dict[str, Any]]],
    pmcs: set[str],
    committers: set[str],
    aliases: dict[str, str],
) -> set[str]:
    members: set[str] = set()
    maintainers = pmcs | committers
    for issue in issues:
        if issue.get("pull_request"):
            continue
        candidate = extract_candidate_login_from_request(issue, aliases)
        if not candidate:
            continue
        comments = comments_by_issue.get(issue["number"], [])
        if membership_request_is_accepted(comments, pmcs, maintainers, aliases):
            members.add(candidate)
    return members


def fetch_member_logins(pmcs: set[str], committers: set[str], aliases: dict[str, str]) -> set[str]:
    issues = gh_api_json(
        gh_repo_endpoint(MEMBERSHIP_REPO, "issues", {"state": "all", "per_page": 100}),
        paginate=True,
    )
    relevant_issues = [issue for issue in issues if MEMBERSHIP_TITLE_RE.search(issue.get("title") or "")]
    comments_by_issue: dict[int, list[dict[str, Any]]] = {}
    for issue in relevant_issues:
        comments_by_issue[issue["number"]] = gh_api_json(
            gh_repo_endpoint(MEMBERSHIP_REPO, f"issues/{issue['number']}/comments", {"per_page": 100}),
            paginate=True,
        )
    return resolve_members_from_membership_requests(relevant_issues, comments_by_issue, pmcs, committers, aliases)


def build_role_directory(overrides: dict[str, Any]) -> tuple[dict[str, set[str]], dict[str, str], set[str]]:
    aliases = normalize_alias_map(overrides.get("aliases"))
    ignored_logins = normalize_login_list(overrides.get("ignoredLogins"), aliases)
    team_markdown = load_repo_text(TEAM_REPO, TEAM_PATH)
    team_roles = parse_team_roles(team_markdown, aliases)
    pmcs = set(team_roles[SECTION_PMCS]) | normalize_login_list(overrides.get("pmcs"), aliases)
    committers = set(team_roles[SECTION_COMMITTERS]) | normalize_login_list(overrides.get("committers"), aliases)
    committers |= pmcs
    members = fetch_member_logins(pmcs, committers, aliases) | normalize_login_list(overrides.get("members"), aliases)
    return ({SECTION_PMCS: pmcs, SECTION_COMMITTERS: committers, SECTION_MEMBERS: members}, aliases, ignored_logins)


def current_role_for(login: str, roles: dict[str, set[str]]) -> str:
    if login in roles[SECTION_PMCS]:
        return "pmc"
    if login in roles[SECTION_COMMITTERS]:
        return "committer"
    if login in roles[SECTION_MEMBERS]:
        return "member"
    return "contributor"


def new_contributor(login: str) -> dict[str, Any]:
    return {
        "login": login,
        "currentRole": "contributor",
        "recentScore": 0,
        "recentBreakdown": {
            "mergedPrs": 0,
            "openPrs": 0,
            "reviews": 0,
            "prComments": 0,
            "issueComments": 0,
            "discussionComments": 0,
            "repoBonus": 0,
            "qualifyingActivities": 0,
            "contributionTypes": 0,
        },
        "recentRepos": [],
        "recentEvidence": [],
        "history": {},
        "recommendation": {},
        "blockers": [],
        "_activity_keys": set(),
        "_recent_repo_set": set(),
        "_contribution_types": set(),
        "_activity_score": 0,
        "_quarter_keys": set(),
    }


def evidence_label(kind: str, title: str) -> str:
    labels = {
        "merged_pr": "Merged PR",
        "open_pr": "Open PR",
        "review": "PR review",
        "pr_comment": "PR comment",
        "issue_comment": "Issue comment",
        "discussion_comment": "Discussion comment",
    }
    prefix = labels.get(kind, kind)
    return f"{prefix}: {title}"


def recent_breakdown_field(kind: str) -> str:
    mapping = {
        "merged_pr": "mergedPrs",
        "open_pr": "openPrs",
        "review": "reviews",
        "pr_comment": "prComments",
        "issue_comment": "issueComments",
        "discussion_comment": "discussionComments",
    }
    return mapping[kind]


def ensure_contributor(contributors: dict[str, dict[str, Any]], login: str) -> dict[str, Any]:
    if login not in contributors:
        contributors[login] = new_contributor(login)
    return contributors[login]


def record_recent_activity(
    contributors: dict[str, dict[str, Any]],
    *,
    login: str,
    kind: str,
    repo: str,
    url: str,
    title: str,
    occurred_at: str | None,
    score: int,
) -> None:
    contributor = ensure_contributor(contributors, login)
    activity_key = f"{kind}:{url}:{occurred_at or ''}"
    if activity_key in contributor["_activity_keys"]:
        return
    contributor["_activity_keys"].add(activity_key)
    contributor["_recent_repo_set"].add(repo)
    if kind != "discussion_comment":
        contributor["recentBreakdown"]["qualifyingActivities"] += 1
        contributor["_contribution_types"].add(kind)
    contributor["recentBreakdown"][recent_breakdown_field(kind)] += 1
    contributor["_activity_score"] += score
    occurred_dt = iso_to_datetime(occurred_at)
    quarter = quarter_key(occurred_dt)
    if quarter:
        contributor["_quarter_keys"].add(quarter)
    contributor["recentEvidence"].append(
        {
            "kind": kind,
            "label": evidence_label(kind, title),
            "repo": repo,
            "url": url,
            "occurredAt": occurred_at,
            "score": score,
        }
    )


def aggregate_recent_activity(
    *,
    pr_nodes: list[dict[str, Any]],
    issue_nodes: list[dict[str, Any]],
    discussion_nodes_by_repo: dict[str, list[dict[str, Any]]],
    allowed_repos: set[str],
    policy: dict[str, Any],
    ignored_logins: set[str],
    aliases: dict[str, str],
    cutoff: datetime,
) -> dict[str, dict[str, Any]]:
    weights = policy["scoreWeights"]
    contributors: dict[str, dict[str, Any]] = {}

    for pr in pr_nodes:
        repo = pr.get("repository", {}).get("nameWithOwner") or ""
        if repo not in allowed_repos:
            continue
        title = pr.get("title") or f"PR #{pr.get('number')}"
        author = normalize_login(pr.get("author", {}).get("login"), aliases)
        merged_at = pr.get("mergedAt")
        last_pr_activity = pr.get("updatedAt") or pr.get("createdAt")
        if is_human_login(author, ignored_logins, aliases):
            if occurred_at_or_after(merged_at, cutoff):
                record_recent_activity(
                    contributors,
                    login=author,
                    kind="merged_pr",
                    repo=repo,
                    url=pr.get("url") or "",
                    title=title,
                    occurred_at=merged_at,
                    score=weights["merged_pr"],
                )
            elif (
                pr.get("state") == "OPEN"
                and not pr.get("isDraft")
                and occurred_at_or_after(last_pr_activity, cutoff)
            ):
                record_recent_activity(
                    contributors,
                    login=author,
                    kind="open_pr",
                    repo=repo,
                    url=pr.get("url") or "",
                    title=title,
                    occurred_at=last_pr_activity,
                    score=weights["open_pr"],
                )
        for review in pr.get("reviews", {}).get("nodes", []):
            reviewer = normalize_login(review.get("author", {}).get("login"), aliases)
            if not is_human_login(reviewer, ignored_logins, aliases) or reviewer == author:
                continue
            submitted_at = review.get("submittedAt")
            if not occurred_at_or_after(submitted_at, cutoff):
                continue
            record_recent_activity(
                contributors,
                login=reviewer,
                kind="review",
                repo=repo,
                url=review.get("url") or "",
                title=title,
                occurred_at=submitted_at,
                score=weights["review"],
            )
        for comment in pr.get("comments", {}).get("nodes", []):
            commenter = normalize_login(comment.get("author", {}).get("login"), aliases)
            if not is_human_login(commenter, ignored_logins, aliases) or commenter == author:
                continue
            created_at = comment.get("createdAt")
            if not occurred_at_or_after(created_at, cutoff):
                continue
            record_recent_activity(
                contributors,
                login=commenter,
                kind="pr_comment",
                repo=repo,
                url=comment.get("url") or "",
                title=title,
                occurred_at=created_at,
                score=weights["pr_comment"],
            )

    for issue in issue_nodes:
        repo = issue.get("repository", {}).get("nameWithOwner") or ""
        if repo not in allowed_repos:
            continue
        title = issue.get("title") or f"Issue #{issue.get('number')}"
        author = normalize_login(issue.get("author", {}).get("login"), aliases)
        for comment in issue.get("comments", {}).get("nodes", []):
            commenter = normalize_login(comment.get("author", {}).get("login"), aliases)
            if not is_human_login(commenter, ignored_logins, aliases) or commenter == author:
                continue
            created_at = comment.get("createdAt")
            if not occurred_at_or_after(created_at, cutoff):
                continue
            record_recent_activity(
                contributors,
                login=commenter,
                kind="issue_comment",
                repo=repo,
                url=comment.get("url") or "",
                title=title,
                occurred_at=created_at,
                score=weights["issue_comment"],
            )

    for repo, discussions in discussion_nodes_by_repo.items():
        if repo not in allowed_repos:
            continue
        for discussion in discussions:
            title = discussion.get("title") or f"Discussion #{discussion.get('number')}"
            author = normalize_login(discussion.get("author", {}).get("login"), aliases)
            for comment in discussion.get("comments", {}).get("nodes", []):
                commenter = normalize_login(comment.get("author", {}).get("login"), aliases)
                if not is_human_login(commenter, ignored_logins, aliases) or commenter == author:
                    continue
                created_at = comment.get("createdAt")
                if not occurred_at_or_after(created_at, cutoff):
                    continue
                record_recent_activity(
                    contributors,
                    login=commenter,
                    kind="discussion_comment",
                    repo=repo,
                    url=comment.get("url") or "",
                    title=title,
                    occurred_at=created_at,
                    score=0,
                )

    return contributors


def select_evidence(entries: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for entry in sorted(
        entries,
        key=lambda item: (
            item.get("score", 0),
            item.get("occurredAt") or "",
            item.get("url") or "",
        ),
        reverse=True,
    ):
        url = entry.get("url") or ""
        if url and url not in unique:
            unique[url] = entry
        if len(unique) >= limit:
            break
    return list(unique.values())


def select_diverse_evidence(entries: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    sorted_entries = sorted(
        entries,
        key=lambda item: (
            item.get("score", 0),
            item.get("occurredAt") or "",
            item.get("url") or "",
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_kinds: set[str] = set()

    for entry in sorted_entries:
        url = entry.get("url") or ""
        kind = entry.get("kind") or ""
        if not url or url in seen_urls or kind in seen_kinds:
            continue
        selected.append(entry)
        seen_urls.add(url)
        seen_kinds.add(kind)
        if len(selected) >= limit:
            return selected

    for entry in sorted_entries:
        url = entry.get("url") or ""
        if not url or url in seen_urls:
            continue
        selected.append(entry)
        seen_urls.add(url)
        if len(selected) >= limit:
            break
    return selected


def finalize_recent_contributors(contributors: dict[str, dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    bonus_per_repo = policy["scoreWeights"]["extra_repo_bonus"]
    max_repo_bonus = int(policy.get("maxRepoBonus", 6))
    finalized: list[dict[str, Any]] = []
    for contributor in contributors.values():
        recent_repos = sorted(contributor["_recent_repo_set"])
        repo_bonus = min(max(0, len(recent_repos) - 1) * bonus_per_repo, max_repo_bonus)
        contributor["recentRepos"] = recent_repos
        contributor["recentBreakdown"]["repoBonus"] = repo_bonus
        contributor["recentBreakdown"]["contributionTypes"] = len(contributor["_contribution_types"])
        contributor["recentScore"] = contributor["_activity_score"] + repo_bonus
        contributor["recentEvidence"] = select_evidence(contributor["recentEvidence"], 8)
        finalized.append(contributor)
    return sorted(
        finalized,
        key=lambda item: (
            item["recentScore"],
            item["recentBreakdown"]["qualifyingActivities"],
            item["login"],
        ),
        reverse=True,
    )


def search_issues(query: str, *, limit: int = 20) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    total_count = 0
    per_page = min(100, max(limit, 1))
    page = 1
    while len(items) < limit:
        try:
            payload = gh_api_json(
                "search/issues",
                params={"q": query, "per_page": per_page, "page": page},
                method="GET",
            )
        except subprocess.CalledProcessError as error:
            if is_invalid_search_user_error(error):
                return {"totalCount": 0, "items": [], "searchUnavailable": True}
            if is_search_rate_limit_error(error):
                wait_for_search_rate_limit_reset()
                continue
            raise
        if page == 1:
            total_count = payload.get("total_count", 0)
        page_items = payload.get("items", [])
        items.extend(page_items)
        if len(page_items) < per_page:
            break
        page += 1
    return {"totalCount": total_count, "items": items[:limit]}


def best_item_timestamp(item: dict[str, Any]) -> str | None:
    return item.get("closed_at") or item.get("updated_at") or item.get("created_at")


def search_item_to_evidence(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    repo = parse_repo_name_from_url(item.get("repository_url"))
    title = item.get("title") or item.get("html_url") or "GitHub evidence"
    return {
        "kind": kind,
        "label": evidence_label(kind, title),
        "repo": repo,
        "url": item.get("html_url") or "",
        "occurredAt": best_item_timestamp(item),
        "score": 0,
    }


def enrich_history(
    contributor: dict[str, Any],
    *,
    policy: dict[str, Any],
    include_discussions: bool,
) -> None:
    login = contributor["login"]
    org = policy["org"]
    merged = search_issues(f"org:{org} is:pr author:{login} is:merged", limit=20)
    authored = search_issues(f"org:{org} is:pr author:{login}", limit=20)
    reviewed = search_issues(f"org:{org} is:pr reviewed-by:{login}", limit=20)
    issue_help = search_issues(f"org:{org} is:issue commenter:{login} -author:{login}", limit=20)

    quarter_keys = set(contributor.get("_quarter_keys", set()))
    for bucket in (merged, authored, reviewed, issue_help):
        for item in bucket["items"]:
            quarter = quarter_key(iso_to_datetime(best_item_timestamp(item)))
            if quarter:
                quarter_keys.add(quarter)

    discussion_evidence = []
    if include_discussions:
        discussion_evidence = [entry for entry in contributor["recentEvidence"] if entry["kind"] == "discussion_comment"]

    community_help_interactions = issue_help["totalCount"] + len(discussion_evidence)
    search_unavailable = any(bucket.get("searchUnavailable") for bucket in (merged, authored, reviewed, issue_help))
    key_evidence = []
    for item in merged["items"][:2]:
        key_evidence.append(search_item_to_evidence("merged_pr", item))
    for item in reviewed["items"][:2]:
        key_evidence.append(search_item_to_evidence("review", item))
    for item in issue_help["items"][:1]:
        key_evidence.append(search_item_to_evidence("issue_comment", item))
    key_evidence.extend(discussion_evidence[:1])

    contributor["history"] = {
        "authoredPullRequests": authored["totalCount"],
        "mergedPullRequests": merged["totalCount"],
        "reviewedPullRequests": reviewed["totalCount"],
        "helpedIssues": issue_help["totalCount"],
        "discussionComments": len(discussion_evidence),
        "communityHelpInteractions": community_help_interactions,
        "activityQuarters": sorted(quarter_keys),
        "activityQuarterCount": len(quarter_keys),
        "keyEvidence": select_diverse_evidence(key_evidence, 5),
        "searchUnavailable": search_unavailable,
    }


def recommendation_summary(
    *,
    label: str,
    key: str,
    rationale: list[str],
    blockers: list[str],
    manual_checks: list[str],
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "rationale": rationale,
        "manualChecks": manual_checks,
        "blockers": blockers,
    }


def build_recommendation(contributor: dict[str, Any], thresholds: dict[str, int]) -> tuple[dict[str, Any], list[str]]:
    role = contributor["currentRole"]
    score = contributor["recentScore"]
    breakdown = contributor["recentBreakdown"]
    history = contributor["history"]
    blockers: list[str] = []

    if role == "pmc":
        blockers.append("Already recognized as PMC.")
        recommendation = recommendation_summary(
            label="Already PMC",
            key=SECTION_CONTINUE,
            rationale=["Current role is already PMC."],
            blockers=blockers,
            manual_checks=[],
        )
        return recommendation, blockers

    if history.get("searchUnavailable"):
        blockers.append("Historical GitHub search evidence is incomplete for this login.")
        recommendation = recommendation_summary(
            label="Continue observing",
            key=SECTION_CONTINUE,
            rationale=["Public GitHub search evidence is incomplete, so automation should not make a promotion recommendation."],
            blockers=blockers,
            manual_checks=["Verify the contributor account is valid/searchable and review historical contributions manually."],
        )
        return recommendation, blockers

    if role == "committer":
        if score >= thresholds["pmc_discussion"] and len(contributor["recentRepos"]) >= 2 and history.get("mergedPullRequests", 0) >= 10 and history.get("reviewedPullRequests", 0) >= 15 and history.get("communityHelpInteractions", 0) >= 10:
            recommendation = recommendation_summary(
                label="Candidate for PMC discussion",
                key=SECTION_PMCS,
                rationale=[
                    f"Recent score {score} meets PMC discussion threshold {thresholds['pmc_discussion']}.",
                    f"Active across {len(contributor['recentRepos'])} repos in the recent window.",
                    f"History shows {history.get('mergedPullRequests', 0)} merged PRs and {history.get('reviewedPullRequests', 0)} reviewed PR threads.",
                ],
                blockers=[],
                manual_checks=[
                    "Validate sustained leadership and stewardship beyond public GitHub signals.",
                    "PMC invitation, discussion, and voting remain manual.",
                ],
            )
            return recommendation, []
        if score < thresholds["pmc_discussion"]:
            blockers.append(f"Recent score {score} is below PMC discussion threshold {thresholds['pmc_discussion']}.")
        if len(contributor["recentRepos"]) < 2:
            blockers.append("Recent activity does not span at least 2 repositories.")
        if history.get("mergedPullRequests", 0) < 10:
            blockers.append("Historical merged PR count is below 10.")
        if history.get("reviewedPullRequests", 0) < 15:
            blockers.append("Historical reviewed PR thread count is below 15.")
        if history.get("communityHelpInteractions", 0) < 10:
            blockers.append("Community-help interactions are below 10.")
        recommendation = recommendation_summary(
            label="Continue observing",
            key=SECTION_CONTINUE,
            rationale=["Current committer activity is valuable but not yet strong enough for PMC discussion."],
            blockers=blockers,
            manual_checks=["Revisit after another quarter of sustained multi-repo activity."],
        )
        return recommendation, blockers

    if role == "member":
        if score >= thresholds["committer_discussion"] and breakdown["reviews"] >= 4 and history.get("mergedPullRequests", 0) >= 5 and history.get("activityQuarterCount", 0) >= 2:
            recommendation = recommendation_summary(
                label="Committer discussion",
                key=SECTION_COMMITTERS,
                rationale=[
                    f"Recent score {score} meets committer discussion threshold {thresholds['committer_discussion']}.",
                    f"Recent review count is {breakdown['reviews']}, meeting the minimum of 4.",
                    f"History shows {history.get('mergedPullRequests', 0)} merged PRs across {history.get('activityQuarterCount', 0)} quarters.",
                ],
                blockers=[],
                manual_checks=[
                    "Confirm sustained review quality and responsibility before nomination.",
                    "PMC nomination and voting remain manual.",
                ],
            )
            return recommendation, []
        if score < thresholds["committer_discussion"]:
            blockers.append(f"Recent score {score} is below committer discussion threshold {thresholds['committer_discussion']}.")
        if breakdown["reviews"] < 4:
            blockers.append("Recent review count is below 4.")
        if history.get("mergedPullRequests", 0) < 5:
            blockers.append("Historical merged PR count is below 5.")
        if history.get("activityQuarterCount", 0) < 2:
            blockers.append("Activity evidence does not span at least 2 quarters.")
        recommendation = recommendation_summary(
            label="Continue observing",
            key=SECTION_CONTINUE,
            rationale=["Current member activity is promising but not yet ready for committer discussion."],
            blockers=blockers,
            manual_checks=["Revisit after more review activity and another quarter of evidence."],
        )
        return recommendation, blockers

    if score >= thresholds["committer_discussion"] and breakdown["reviews"] >= 4 and history.get("mergedPullRequests", 0) >= 5:
        blockers.append("Not currently recognized as member; observe for member first.")
        recommendation = recommendation_summary(
            label="Observe for member first",
            key=SECTION_CONTINUE,
            rationale=["Public evidence is strong, but the contributor is not currently recognized as member."],
            blockers=blockers,
            manual_checks=["Confirm member readiness first, then revisit committer discussion."],
        )
        return recommendation, blockers

    has_merged_pr_evidence = breakdown["mergedPrs"] >= 1 or history.get("mergedPullRequests", 0) >= 1
    if (
        score >= thresholds["member"]
        and breakdown["contributionTypes"] >= 2
        and breakdown["qualifyingActivities"] >= 3
        and contributor["recentEvidence"]
        and has_merged_pr_evidence
    ):
        recommendation = recommendation_summary(
            label="Member",
            key=SECTION_MEMBERS,
            rationale=[
                f"Recent score {score} meets member threshold {thresholds['member']}.",
                f"Recent activity spans {breakdown['contributionTypes']} contribution types and {breakdown['qualifyingActivities']} qualifying activities.",
            ],
            blockers=[],
            manual_checks=[
                "Confirm 2FA is enabled before inviting the contributor.",
                "Membership request sponsorship remains manual.",
            ],
        )
        return recommendation, []

    if score < thresholds["member"]:
        blockers.append(f"Recent score {score} is below member threshold {thresholds['member']}.")
    if breakdown["contributionTypes"] < 2:
        blockers.append("Recent activity does not cover at least 2 contribution types.")
    if breakdown["qualifyingActivities"] < 3:
        blockers.append("Recent activity does not cover at least 3 qualifying activities.")
    if not has_merged_pr_evidence:
        blockers.append("No merged PR evidence yet; wait for landed contributions before recommending member.")
    recommendation = recommendation_summary(
        label="Continue observing",
        key=SECTION_CONTINUE,
        rationale=["Contributor has visible activity but not enough evidence for a promotion recommendation."],
        blockers=blockers,
        manual_checks=["Track another quarter of activity before reassessing."],
    )
    return recommendation, blockers


def collect_key_evidence(contributor: dict[str, Any]) -> list[dict[str, Any]]:
    combined = list(contributor.get("recentEvidence", [])) + list(contributor.get("history", {}).get("keyEvidence", []))
    return select_diverse_evidence(combined, 5)


def build_recommendation_groups(contributors: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups = {
        SECTION_MEMBERS: [],
        SECTION_COMMITTERS: [],
        SECTION_PMCS: [],
        SECTION_CONTINUE: [],
    }
    for contributor in contributors:
        summary = {
            "login": contributor["login"],
            "currentRole": contributor["currentRole"],
            "recentScore": contributor["recentScore"],
            "label": contributor["recommendation"]["label"],
            "blockers": contributor["blockers"],
        }
        groups[contributor["recommendation"]["key"]].append(summary)
    return groups


def scan_contributors(policy: dict[str, Any], overrides: dict[str, Any], *, include_discussions: bool) -> dict[str, Any]:
    roles, aliases, ignored_logins = build_role_directory(overrides)
    repositories = list_org_repositories(policy["org"])
    repos_scanned, excluded_repos = build_repo_inventory(policy, repositories)
    cutoff = utc_now() - timedelta(days=int(policy["lookbackDays"]))
    cutoff_date = cutoff.date().isoformat()
    pull_requests = fetch_recent_pull_requests(policy["org"], cutoff_date)
    issues = fetch_recent_issues(policy["org"], cutoff_date)
    discussion_nodes_by_repo: dict[str, list[dict[str, Any]]] = {}
    if include_discussions:
        for repo in repos_scanned:
            discussion_nodes_by_repo[repo] = fetch_recent_discussions(repo, cutoff)

    contributors = aggregate_recent_activity(
        pr_nodes=pull_requests,
        issue_nodes=issues,
        discussion_nodes_by_repo=discussion_nodes_by_repo,
        allowed_repos=set(repos_scanned),
        policy=policy,
        ignored_logins=ignored_logins,
        aliases=aliases,
        cutoff=cutoff,
    )
    ranked = finalize_recent_contributors(contributors, policy)[: int(policy["topN"])]
    thresholds = policy["thresholds"]
    for contributor in ranked:
        contributor["currentRole"] = current_role_for(contributor["login"], roles)
        enrich_history(contributor, policy=policy, include_discussions=include_discussions)
        recommendation, blockers = build_recommendation(contributor, thresholds)
        contributor["recommendation"] = recommendation
        contributor["blockers"] = blockers
        contributor["recentEvidence"] = select_evidence(contributor["recentEvidence"], 5)
        contributor["history"]["keyEvidence"] = collect_key_evidence(contributor)
        for internal_key in ("_activity_keys", "_recent_repo_set", "_contribution_types", "_activity_score", "_quarter_keys"):
            contributor.pop(internal_key, None)

    generated_at = utc_now()
    payload = {
        "generatedAt": to_iso(generated_at),
        "windowStart": cutoff.date().isoformat(),
        "windowEnd": generated_at.date().isoformat(),
        "org": policy["org"],
        "lookbackDays": int(policy["lookbackDays"]),
        "topN": int(policy["topN"]),
        "reposScanned": repos_scanned,
        "excludedRepos": excluded_repos,
        "rankedContributors": ranked,
        "recommendations": build_recommendation_groups(ranked),
        "notes": [
            f"Recent window covers {cutoff.date().isoformat()} through {generated_at.date().isoformat()}.",
            "Recent score includes merged/open PRs, reviews, PR comments, issue comments, and repository bonus.",
            "Discussion comments are supplemental only and do not change the default score.",
            "Member detection is inferred from apollo-community membership requests and may require role-overrides for historical/manual cases.",
            "Historical review/help counts rely on GitHub search thread counts, not exact review submission totals.",
        ],
    }
    return payload


def format_breakdown(breakdown: dict[str, Any]) -> str:
    parts = [
        f"merged PRs {breakdown['mergedPrs']}",
        f"open PRs {breakdown['openPrs']}",
        f"reviews {breakdown['reviews']}",
        f"PR comments {breakdown['prComments']}",
        f"issue comments {breakdown['issueComments']}",
    ]
    if breakdown.get("discussionComments"):
        parts.append(f"discussion comments {breakdown['discussionComments']}")
    parts.append(f"repo bonus {breakdown['repoBonus']}")
    return ", ".join(parts)


def render_candidate(contributor: dict[str, Any]) -> list[str]:
    history = contributor["history"]
    evidence = collect_key_evidence(contributor)
    lines = [
        f"### @{contributor['login']}",
        f"- Current role: `{contributor['currentRole']}`",
        f"- Recent score: `{contributor['recentScore']}`",
        f"- Recent breakdown: {format_breakdown(contributor['recentBreakdown'])}",
        f"- Recent repos: {', '.join(contributor['recentRepos']) if contributor['recentRepos'] else 'None'}",
        (
            "- History: "
            f"{history.get('mergedPullRequests', 0)} merged PRs, "
            f"{history.get('authoredPullRequests', 0)} authored PRs, "
            f"{history.get('reviewedPullRequests', 0)} reviewed PR threads, "
            f"{history.get('helpedIssues', 0)} helped issues, "
            f"{history.get('communityHelpInteractions', 0)} community-help interactions, "
            f"quarters {', '.join(history.get('activityQuarters', [])) or 'None'}"
        ),
        f"- Recommendation: `{contributor['recommendation']['label']}`",
    ]
    if contributor["recommendation"]["rationale"]:
        lines.append(f"- Rationale: {' '.join(contributor['recommendation']['rationale'])}")
    if contributor["recommendation"]["manualChecks"]:
        lines.append(f"- Manual checks: {' '.join(contributor['recommendation']['manualChecks'])}")
    if contributor["blockers"]:
        lines.append(f"- Blockers: {' '.join(contributor['blockers'])}")
    lines.append("- Evidence:")
    if not evidence:
        lines.append("  - No evidence links captured.")
    for item in evidence[:5]:
        lines.append(
            f"  - [{item['label']}]({item['url']})"
            + (f" ({item['repo']}, {item['occurredAt']})" if item.get("repo") or item.get("occurredAt") else "")
        )
    lines.append("")
    return lines


def render_group(title: str, contributors: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not contributors:
        lines.append("- None")
        lines.append("")
        return lines
    for contributor in contributors:
        lines.append(
            f"- `@{contributor['login']}` ({contributor['currentRole']}, score {contributor['recentScore']}): {contributor['label']}"
        )
    lines.append("")
    return lines


def render_summary(payload: dict[str, Any]) -> str:
    top_n = int(payload.get("topN") or len(payload.get("rankedContributors", [])) or 10)
    lines = [
        "# Apollo Contributor Promotion Review",
        "",
        f"- Generated at: {payload['generatedAt']}",
        f"- Review window: {payload.get('windowStart')} to {payload.get('windowEnd')}",
        f"- Organization: {payload['org']}",
        f"- Repositories scanned: {len(payload['reposScanned'])}",
        "",
        f"## Top {top_n} Recent Contributors",
        "",
    ]
    for contributor in payload.get("rankedContributors", []):
        lines.extend(render_candidate(contributor))
    recommendations = payload.get("recommendations", {})
    lines.extend(render_group("Recommend Member", recommendations.get(SECTION_MEMBERS, [])))
    lines.extend(render_group("Recommend Committer Discussion", recommendations.get(SECTION_COMMITTERS, [])))
    lines.extend(render_group("Recommend PMC Discussion", recommendations.get(SECTION_PMCS, [])))
    lines.extend(render_group("Continue Observing / Manual Checks", recommendations.get(SECTION_CONTINUE, [])))
    if payload.get("notes"):
        lines.append("## Notes")
        lines.append("")
        for note in payload["notes"]:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan recent Apollo contributors and emit JSON.")
    scan_parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY_FILE)
    scan_parser.add_argument("--role-overrides", type=Path, default=None)
    scan_parser.add_argument("--lookback-days", type=int, default=None)
    scan_parser.add_argument("--top-n", type=int, default=None)
    scan_parser.add_argument("--include-discussions", action="store_true")
    scan_parser.add_argument("--pretty", action="store_true")

    summary_parser = subparsers.add_parser("summarize", help="Render a markdown report from scan JSON.")
    summary_parser.add_argument("--scan-file", required=True)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "scan":
        policy = load_policy(args.policy_file, lookback_days=args.lookback_days, top_n=args.top_n)
        overrides = load_role_overrides(args.role_overrides)
        payload = scan_contributors(policy, overrides, include_discussions=args.include_discussions)
        print(render_json(payload, args.pretty))
        return
    if args.command == "summarize":
        print(render_summary(load_scan_payload(args.scan_file)), end="")
        return
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
