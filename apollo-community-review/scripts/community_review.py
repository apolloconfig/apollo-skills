#!/usr/bin/env python3
"""Helpers for the Apollo community review automation."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import yaml


CHINESE_DISCLAIMER = (
    "注：本回复由 AI 自动生成并自动发送，用于初步分诊；如需进一步确认，维护者会继续跟进。"
)
ENGLISH_DISCLAIMER = (
    "Note: this reply was generated and posted automatically by AI for initial triage; "
    "a maintainer will follow up if needed."
)

DEFAULT_REPO = "apolloconfig/apollo"
DEFAULT_STATE_FILE = Path.home() / ".codex" / "tmp" / "apollo-community-review" / "state.json"
DEFAULT_MIRROR_DIR = Path.home() / ".codex" / "tmp" / "apollo-review-mirror"
READY_CHECK_CONCLUSIONS = {"success", "neutral", "skipped"}
KNOWN_AUTOMATION_LOGINS = {
    "stale",
    "stale[bot]",
    "mergify",
    "mergify[bot]",
}
SECURITY_PATTERN = re.compile(
    r"(vulnerability|sql injection|auth bypass|credential leak|secret leak|token leak|"
    r"remote code execution|\brce\b|\bxss\b|\bcsrf\b|\bssrf\b|漏洞|注入|越权|绕过|提权|"
    r"凭证泄露|密钥泄露|安全问题|安全漏洞)",
    re.IGNORECASE,
)
ADMIN_PATTERN = re.compile(
    r"(\brelease\b|\brollback\b|\brevert\b|\bmerge\b|发布|回滚|合并|发版|打 tag|打tag)",
    re.IGNORECASE,
)
FIXES_ISSUE_PATTERN = re.compile(
    r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)\b"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_command(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def gh_api_json(endpoint: str, *, paginate: bool = False) -> Any:
    command = ["gh", "api", endpoint]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    output = run_command(command)
    payload = json.loads(output)
    if paginate:
        flattened: list[Any] = []
        for page in payload:
            if isinstance(page, list):
                flattened.extend(page)
            else:
                flattened.append(page)
        return flattened
    return payload


def gh_repo_endpoint(repo: str, path: str, params: dict[str, Any] | None = None) -> str:
    clean_path = path.lstrip("/")
    if not params:
        return f"repos/{repo}/{clean_path}"
    query = urlencode({key: value for key, value in params.items() if value is not None})
    return f"repos/{repo}/{clean_path}?{query}"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"last_scan_at": None, "threads": {}}
    raw = path.read_text().strip()
    if not raw:
        return {"last_scan_at": None, "threads": {}}
    return json.loads(raw)


def save_state(path: Path, state: dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def maintainer_set(raw: Iterable[str]) -> set[str]:
    return {value.strip().lower() for value in raw if value.strip()}


def is_automation_account(login: str | None, user_type: str | None) -> bool:
    normalized = (login or "").strip().lower()
    if user_type == "Bot":
        return True
    if normalized.endswith("[bot]"):
        return True
    return normalized in KNOWN_AUTOMATION_LOGINS


def normalize_language(value: str | None) -> str:
    if not value:
        return "en"
    lowered = value.lower()
    if lowered.startswith("zh"):
        return "zh"
    return "en"


def detect_language(text: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "en"


def append_disclaimer(body: str, language: str) -> str:
    disclaimer = CHINESE_DISCLAIMER if normalize_language(language) == "zh" else ENGLISH_DISCLAIMER
    stripped = body.rstrip()
    if disclaimer in stripped:
        return stripped
    if not stripped:
        return disclaimer
    return f"{stripped}\n\n{disclaimer}"


def state_key(candidate: dict[str, Any]) -> str:
    return f"{candidate['repo']}#{candidate['thread_type']}#{candidate['number']}"


def should_process_candidate(candidate: dict[str, Any], state: dict[str, Any]) -> bool:
    thread_state = state.get("threads", {}).get(state_key(candidate), {})
    return thread_state.get("last_processed_signature") != candidate.get("activity_signature")


def mark_processed(
    state: dict[str, Any],
    candidate: dict[str, Any],
    decision: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = to_iso(now or utc_now())
    state.setdefault("threads", {})[state_key(candidate)] = {
        "last_comment_url": decision.get("comment_url"),
        "last_decision_confidence": decision.get("decision_confidence"),
        "last_processed_at": timestamp,
        "last_processed_signature": candidate.get("activity_signature"),
        "last_result_group": decision.get("result_group"),
    }
    state["last_scan_at"] = timestamp
    return state


def scan_since(state: dict[str, Any], initial_lookback_hours: int) -> str:
    last_scan = state.get("last_scan_at")
    if last_scan:
        return last_scan
    return to_iso(utc_now() - timedelta(hours=initial_lookback_hours)) or to_iso(utc_now())


def latest_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    return max(events, key=lambda item: item["timestamp"])


def summarize_check_state(check_runs: list[dict[str, Any]], combined_status: dict[str, Any]) -> dict[str, Any]:
    overall = "success"
    details: list[str] = []
    for run in check_runs:
        name = run.get("name", "unnamed")
        status = run.get("status")
        conclusion = run.get("conclusion")
        details.append(f"{name}: {status}/{conclusion}")
        if status != "completed":
            overall = "pending"
        elif conclusion not in READY_CHECK_CONCLUSIONS and overall != "pending":
            overall = "failure"

    combined_state = combined_status.get("state")
    if combined_state == "pending" and overall == "success":
        overall = "pending"
    if combined_state in {"error", "failure"}:
        overall = "failure"

    return {
        "details": details,
        "has_uncertainty": False,
        "overall": overall,
    }


def fetch_commit_timestamp(repo: str, pr_number: int) -> tuple[str | None, str | None]:
    commits = gh_api_json(
        gh_repo_endpoint(repo, f"pulls/{pr_number}/commits", {"per_page": 100}),
        paginate=True,
    )
    if not commits:
        return None, None
    latest = commits[-1]
    timestamp = (
        latest.get("commit", {}).get("committer", {}).get("date")
        or latest.get("commit", {}).get("author", {}).get("date")
    )
    actor = latest.get("author", {}).get("login") or latest.get("committer", {}).get("login")
    return timestamp, actor


def fetch_check_summary(repo: str, sha: str) -> dict[str, Any]:
    try:
        check_runs_payload = gh_api_json(
            gh_repo_endpoint(repo, f"commits/{sha}/check-runs", {"per_page": 100})
        )
        combined_status = gh_api_json(gh_repo_endpoint(repo, f"commits/{sha}/status"))
    except subprocess.CalledProcessError as error:
        return {
            "details": [error.stderr.strip() or "gh api failed while fetching checks"],
            "has_uncertainty": True,
            "overall": "unknown",
        }

    check_runs = check_runs_payload.get("check_runs", [])
    return summarize_check_state(check_runs, combined_status)


def has_new_external_activity(candidate: dict[str, Any], since: str) -> bool:
    activity_at = iso_to_datetime(candidate.get("latest_external_activity_at"))
    since_dt = iso_to_datetime(since)
    if activity_at is None:
        return False
    if since_dt is None:
        return True
    return activity_at > since_dt


def build_issue_candidate(
    repo: str,
    issue: dict[str, Any],
    *,
    maintainers: set[str],
    actor_login: str,
) -> dict[str, Any]:
    number = issue["number"]
    comments = gh_api_json(
        gh_repo_endpoint(repo, f"issues/{number}/comments", {"per_page": 100}),
        paginate=True,
    )
    external_events: list[dict[str, Any]] = []
    maintainer_events: list[dict[str, Any]] = []

    issue_author = (issue.get("user", {}) or {}).get("login", "")
    issue_author_type = (issue.get("user", {}) or {}).get("type", "")
    issue_ts = issue.get("created_at")
    if issue_ts and not is_automation_account(issue_author, issue_author_type):
        event = {
            "id": issue["id"],
            "kind": "issue-body",
            "timestamp": iso_to_datetime(issue_ts) or utc_now(),
        }
        if issue_author.lower() in maintainers:
            maintainer_events.append(event)
        else:
            external_events.append(event)

    for comment in comments:
        user = comment.get("user", {}) or {}
        login = user.get("login", "")
        if is_automation_account(login, user.get("type")):
            continue
        event = {
            "id": comment["id"],
            "kind": "issue-comment",
            "timestamp": iso_to_datetime(comment.get("updated_at") or comment.get("created_at")) or utc_now(),
        }
        if login.lower() in maintainers:
            maintainer_events.append(event)
        else:
            external_events.append(event)

    latest_external = latest_event(external_events)
    latest_maintainer = latest_event(maintainer_events)
    latest_external_at = latest_external["timestamp"] if latest_external else None
    newer_maintainer_reply = bool(
        latest_external_at
        and latest_maintainer
        and latest_maintainer["timestamp"] > latest_external_at
    )

    latest_external_sig = (
        f"{latest_external['kind']}:{latest_external['id']}:{to_iso(latest_external['timestamp'])}"
        if latest_external
        else "external:none"
    )

    return {
        "activity_signature": f"issue:{number}:{latest_external_sig}",
        "author": issue_author,
        "body": issue.get("body") or "",
        "draft": False,
        "has_newer_maintainer_activity": newer_maintainer_reply,
        "html_url": issue.get("html_url"),
        "latest_external_activity_at": to_iso(latest_external_at),
        "latest_maintainer_activity_at": to_iso(latest_maintainer["timestamp"]) if latest_maintainer else None,
        "number": number,
        "repo": repo,
        "thread_id": str(number),
        "thread_type": "issue",
        "title": issue.get("title") or "",
        "updated_at": issue.get("updated_at"),
        "viewer_actor": actor_login,
    }


def build_pr_candidate(
    repo: str,
    issue_stub: dict[str, Any],
    *,
    maintainers: set[str],
    actor_login: str,
) -> dict[str, Any]:
    number = issue_stub["number"]
    issue = gh_api_json(gh_repo_endpoint(repo, f"issues/{number}"))
    pr = gh_api_json(gh_repo_endpoint(repo, f"pulls/{number}"))
    issue_comments = gh_api_json(
        gh_repo_endpoint(repo, f"issues/{number}/comments", {"per_page": 100}),
        paginate=True,
    )
    reviews = gh_api_json(
        gh_repo_endpoint(repo, f"pulls/{number}/reviews", {"per_page": 100}),
        paginate=True,
    )
    review_comments = gh_api_json(
        gh_repo_endpoint(repo, f"pulls/{number}/comments", {"per_page": 100}),
        paginate=True,
    )
    latest_commit_at, latest_commit_actor = fetch_commit_timestamp(repo, number)
    head_sha = pr.get("head", {}).get("sha")
    check_summary = fetch_check_summary(repo, head_sha) if head_sha else {
        "details": ["missing head SHA"],
        "has_uncertainty": True,
        "overall": "unknown",
    }

    external_social_events: list[dict[str, Any]] = []
    maintainer_social_events: list[dict[str, Any]] = []
    pr_author_user = pr.get("user", {}) or {}
    pr_author = pr_author_user.get("login", "")

    def add_event(
        login: str,
        user_type: str | None,
        event_id: Any,
        kind: str,
        timestamp_value: str | None,
    ) -> None:
        if not timestamp_value:
            return
        if is_automation_account(login, user_type):
            return
        event = {
            "id": event_id,
            "kind": kind,
            "timestamp": iso_to_datetime(timestamp_value) or utc_now(),
        }
        if login.lower() in maintainers:
            maintainer_social_events.append(event)
        else:
            external_social_events.append(event)

    add_event(
        pr_author,
        pr_author_user.get("type"),
        pr.get("id"),
        "pr-body",
        pr.get("created_at"),
    )
    for comment in issue_comments:
        comment_user = comment.get("user", {}) or {}
        add_event(
            comment_user.get("login", ""),
            comment_user.get("type"),
            comment.get("id"),
            "issue-comment",
            comment.get("updated_at") or comment.get("created_at"),
        )
    for review in reviews:
        review_user = review.get("user", {}) or {}
        add_event(
            review_user.get("login", ""),
            review_user.get("type"),
            review.get("id"),
            "review",
            review.get("submitted_at") or review.get("updated_at") or review.get("created_at"),
        )
    for review_comment in review_comments:
        review_comment_user = review_comment.get("user", {}) or {}
        add_event(
            review_comment_user.get("login", ""),
            review_comment_user.get("type"),
            review_comment.get("id"),
            "review-comment",
            review_comment.get("updated_at") or review_comment.get("created_at"),
        )

    latest_external_social = latest_event(external_social_events)
    latest_maintainer_social = latest_event(maintainer_social_events)
    latest_commit_dt = iso_to_datetime(latest_commit_at)
    social_dt = latest_external_social["timestamp"] if latest_external_social else None
    latest_external_activity = max(
        [candidate for candidate in [latest_commit_dt, social_dt] if candidate is not None],
        default=None,
    )
    newer_maintainer_reply = bool(
        latest_external_activity
        and latest_maintainer_social
        and latest_maintainer_social["timestamp"] > latest_external_activity
    )

    social_signature = (
        f"{latest_external_social['kind']}:{latest_external_social['id']}:{to_iso(latest_external_social['timestamp'])}"
        if latest_external_social
        else "social:none"
    )
    commit_signature = f"sha:{head_sha}:{latest_commit_at}:{latest_commit_actor}"

    return {
        "activity_signature": f"pr:{number}:{commit_signature}:{social_signature}",
        "author": pr_author,
        "body": issue.get("body") or pr.get("body") or "",
        "check_summary": check_summary,
        "draft": bool(pr.get("draft")),
        "has_newer_maintainer_activity": newer_maintainer_reply,
        "head_sha": head_sha,
        "html_url": pr.get("html_url"),
        "latest_external_activity_at": to_iso(latest_external_activity),
        "latest_maintainer_activity_at": (
            to_iso(latest_maintainer_social["timestamp"]) if latest_maintainer_social else None
        ),
        "number": number,
        "repo": repo,
        "thread_id": str(number),
        "thread_type": "pr",
        "title": pr.get("title") or "",
        "updated_at": pr.get("updated_at"),
        "viewer_actor": actor_login,
    }


def discover_candidates(
    repo: str,
    *,
    actor_login: str,
    maintainers: set[str],
    state: dict[str, Any],
    initial_lookback_hours: int,
) -> list[dict[str, Any]]:
    since = scan_since(state, initial_lookback_hours)
    issues = gh_api_json(
        gh_repo_endpoint(
            repo,
            "issues",
            {
                "direction": "desc",
                "per_page": 100,
                "since": since,
                "sort": "updated",
                "state": "open",
            },
        ),
        paginate=True,
    )

    candidates: list[dict[str, Any]] = []
    for issue in issues:
        if issue.get("state") != "open":
            continue
        if issue.get("pull_request"):
            candidate = build_pr_candidate(
                repo,
                issue,
                maintainers=maintainers,
                actor_login=actor_login,
            )
        else:
            candidate = build_issue_candidate(
                repo,
                issue,
                maintainers=maintainers,
                actor_login=actor_login,
            )
        if not has_new_external_activity(candidate, since):
            continue
        if should_process_candidate(candidate, state):
            candidates.append(candidate)
    return candidates


def extract_json_block(text: str) -> dict[str, Any] | None:
    pattern = re.compile(
        r"COMMUNITY_REVIEW_DECISION\s*```(?:json|jsonc)?\s*(\{.*?\})\s*```",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None
    return json.loads(match.group(1))


def extract_draft_body_fallback(text: str) -> str:
    sections = [
        "Publish-ready Maintainer Review Draft",
        "Draft Maintainer Reply",
    ]
    end_markers = [
        "Publish Gate",
        "COMMUNITY_REVIEW_DECISION",
        "handoff:",
    ]
    for section in sections:
        stop_pattern = "|".join(re.escape(item) for item in end_markers)
        pattern = re.compile(
            rf"(?:^|\n)(?:#+\s*)?{re.escape(section)}\s*\n(.*?)(?=\n(?:#+\s*)?(?:{stop_pattern})\s*\n|\nCOMMUNITY_REVIEW_DECISION|\nhandoff:|\Z)",
            re.DOTALL,
        )
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return ""


def normalize_review_from_handoff(text: str, thread_type: str) -> dict[str, Any]:
    marker_match = re.search(r"(?m)^handoff:\s*$", text)
    if not marker_match:
        raise ValueError("missing COMMUNITY_REVIEW_DECISION block and handoff YAML")
    payload = yaml.safe_load(text[marker_match.start() :])
    handoff = payload.get("handoff", {})
    draft_body = extract_draft_body_fallback(text)
    if thread_type == "issue":
        issue_classification = handoff.get("issue_classification", {})
        triage = handoff.get("triage_decision", {})
        return {
            "blocking": bool(triage.get("missing_info_fields")),
            "blocking_reasons": [],
            "decision_confidence": issue_classification.get("confidence", "low"),
            "draft_body": draft_body,
            "language": detect_language(text),
            "missing_info_fields": triage.get("missing_info_fields", []),
            "recommended_action": "comment",
            "validation_completed": bool(issue_classification.get("validation_path")),
        }

    review_decision = handoff.get("review_decision", {})
    return {
        "blocking": bool(review_decision.get("blocking")),
        "blocking_reasons": handoff.get("gate_status", {}).get("residual_risks", []),
        "decision_confidence": review_decision.get("decision_confidence", "low"),
        "draft_body": draft_body,
        "language": detect_language(text),
        "missing_info_fields": [],
        "recommended_action": review_decision.get("action", "comment"),
        "validation_completed": True,
    }


def extract_normalized_review(text: str, thread_type: str) -> dict[str, Any]:
    normalized = extract_json_block(text)
    if normalized:
        return normalized
    return normalize_review_from_handoff(text, thread_type)


def is_security_sensitive(candidate: dict[str, Any]) -> bool:
    haystack = "\n".join([candidate.get("title", ""), candidate.get("body", "")])
    return bool(SECURITY_PATTERN.search(haystack))


def is_admin_operation(candidate: dict[str, Any]) -> bool:
    haystack = "\n".join([candidate.get("title", ""), candidate.get("body", "")])
    return bool(ADMIN_PATTERN.search(haystack))


def build_decision(candidate: dict[str, Any], review_text: str) -> dict[str, Any]:
    normalized = extract_normalized_review(review_text, candidate["thread_type"])
    language = normalize_language(normalized.get("language") or detect_language(candidate.get("body", "")))
    confidence = normalized.get("decision_confidence", "low")
    recommended_action = normalized.get("recommended_action", "comment")
    blocking_reasons = list(normalized.get("blocking_reasons", []))

    if is_security_sensitive(candidate):
        blocking_reasons.append("security-sensitive thread")
    if is_admin_operation(candidate):
        blocking_reasons.append("admin operation thread")
    if candidate.get("has_newer_maintainer_activity"):
        blocking_reasons.append("newer maintainer reply exists after latest external activity")

    if candidate["thread_type"] == "issue":
        missing_info_fields = normalized.get("missing_info_fields", [])
        if confidence != "high":
            blocking_reasons.append(f"decision confidence is {confidence}")
        if missing_info_fields:
            blocking_reasons.append(
                "missing information: " + ", ".join(str(item) for item in missing_info_fields)
            )
        if not normalized.get("validation_completed"):
            blocking_reasons.append("validation path not completed")
    else:
        check_summary = candidate.get("check_summary", {})
        if candidate.get("draft"):
            blocking_reasons.append("draft PR")
        if confidence != "high":
            blocking_reasons.append(f"decision confidence is {confidence}")
        if recommended_action != "comment":
            blocking_reasons.append(f"recommended action is {recommended_action}")
        if normalized.get("blocking"):
            blocking_reasons.append("review remains blocking")
        if check_summary.get("has_uncertainty"):
            blocking_reasons.append("required checks are uncertain")
        elif check_summary.get("overall") != "success":
            blocking_reasons.append(f"checks are {check_summary.get('overall')}")

    blocking_reasons = sorted(set(reason for reason in blocking_reasons if reason))
    draft_body = (normalized.get("draft_body") or "").strip()
    auto_send_eligible = bool(draft_body) and not blocking_reasons
    prepared_body = append_disclaimer(draft_body, language) if auto_send_eligible else draft_body

    result_group = "needs-review"
    if auto_send_eligible:
        result_group = "auto-sent"
    elif not draft_body:
        result_group = "skipped/error"

    return {
        "activity_signature": candidate.get("activity_signature"),
        "auto_send_eligible": auto_send_eligible,
        "blocking_reasons": blocking_reasons,
        "decision_confidence": confidence,
        "draft_body": draft_body,
        "language": language,
        "prepared_body": prepared_body,
        "recommended_action": recommended_action,
        "repo": candidate["repo"],
        "result_group": result_group,
        "thread_id": candidate["thread_id"],
        "thread_number": candidate["number"],
        "thread_type": candidate["thread_type"],
        "thread_url": candidate.get("html_url"),
    }


def linked_issues(body: str) -> list[str]:
    return sorted(set(FIXES_ISSUE_PATTERN.findall(body or "")))


def fetch_issue_context(repo: str, number: int) -> dict[str, Any]:
    issue = gh_api_json(gh_repo_endpoint(repo, f"issues/{number}"))
    comments = gh_api_json(
        gh_repo_endpoint(repo, f"issues/{number}/comments", {"per_page": 100}),
        paginate=True,
    )
    return {"issue": issue, "comments": comments}


def fetch_pr_context(repo: str, number: int) -> dict[str, Any]:
    issue_context = fetch_issue_context(repo, number)
    pr = gh_api_json(gh_repo_endpoint(repo, f"pulls/{number}"))
    reviews = gh_api_json(
        gh_repo_endpoint(repo, f"pulls/{number}/reviews", {"per_page": 100}),
        paginate=True,
    )
    review_comments = gh_api_json(
        gh_repo_endpoint(repo, f"pulls/{number}/comments", {"per_page": 100}),
        paginate=True,
    )
    files = gh_api_json(
        gh_repo_endpoint(repo, f"pulls/{number}/files", {"per_page": 100}),
        paginate=True,
    )
    return {
        **issue_context,
        "files": files,
        "pr": pr,
        "review_comments": review_comments,
        "reviews": reviews,
    }


def format_issue_markdown(context: dict[str, Any]) -> str:
    issue = context["issue"]
    labels = ", ".join(label["name"] for label in issue.get("labels", [])) or "(none)"
    lines = [
        f"# Issue Context: {issue.get('title')}",
        "",
        f"- Repo: {issue.get('repository_url', '').split('/repos/')[-1] or DEFAULT_REPO}",
        f"- Number: {issue.get('number')}",
        f"- URL: {issue.get('html_url')}",
        f"- Author: {(issue.get('user', {}) or {}).get('login')}",
        f"- Labels: {labels}",
        "",
        "## Body",
        issue.get("body") or "(empty)",
        "",
        "## Comments",
    ]
    for comment in context["comments"]:
        lines.extend(
            [
                f"### Comment {(comment.get('user', {}) or {}).get('login')} @ {comment.get('created_at')}",
                comment.get("body") or "(empty)",
                "",
            ]
        )
    return "\n".join(lines).strip()


def format_pr_markdown(context: dict[str, Any]) -> str:
    issue = context["issue"]
    pr = context["pr"]
    labels = ", ".join(label["name"] for label in issue.get("labels", [])) or "(none)"
    body = issue.get("body") or pr.get("body") or ""
    linked = ", ".join(f"#{item}" for item in linked_issues(body)) or "(none)"
    lines = [
        f"# PR Context: {pr.get('title')}",
        "",
        f"- Repo: {issue.get('repository_url', '').split('/repos/')[-1] or DEFAULT_REPO}",
        f"- Number: {pr.get('number')}",
        f"- URL: {pr.get('html_url')}",
        f"- Author: {(pr.get('user', {}) or {}).get('login')}",
        f"- Draft: {pr.get('draft')}",
        f"- Head SHA: {pr.get('head', {}).get('sha')}",
        f"- Base Ref: {pr.get('base', {}).get('ref')}",
        f"- Linked Issues: {linked}",
        f"- Labels: {labels}",
        "",
        "## Body",
        body or "(empty)",
        "",
        "## Issue Comments",
    ]
    for comment in context["comments"]:
        lines.extend(
            [
                f"### Comment {(comment.get('user', {}) or {}).get('login')} @ {comment.get('created_at')}",
                comment.get("body") or "(empty)",
                "",
            ]
        )
    lines.append("## Reviews")
    for review in context["reviews"]:
        lines.extend(
            [
                f"### Review {(review.get('user', {}) or {}).get('login')} @ {review.get('submitted_at')}",
                f"- State: {review.get('state')}",
                review.get("body") or "(empty)",
                "",
            ]
        )
    lines.append("## Review Comments")
    for review_comment in context["review_comments"]:
        lines.extend(
            [
                (
                    f"### Review Comment {(review_comment.get('user', {}) or {}).get('login')} "
                    f"@ {review_comment.get('created_at')}"
                ),
                f"- Path: {review_comment.get('path')}:{review_comment.get('line')}",
                review_comment.get("body") or "(empty)",
                "",
            ]
        )
    lines.append("## Changed Files")
    for file_info in context["files"]:
        lines.append(
            (
                f"- {file_info.get('filename')} "
                f"({file_info.get('status')}, +{file_info.get('additions')}/-{file_info.get('deletions')})"
            )
        )
    return "\n".join(lines).strip()


def render_summary(results: list[dict[str, Any]]) -> str:
    groups = {
        "auto-sent": [],
        "needs-review": [],
        "skipped/error": [],
    }
    for result in results:
        groups.setdefault(result.get("result_group", "skipped/error"), []).append(result)

    lines = ["# Apollo Community Review"]
    for heading in ["auto-sent", "needs-review", "skipped/error"]:
        title = {
            "auto-sent": "Auto-sent",
            "needs-review": "Needs review",
            "skipped/error": "Skipped/Error",
        }[heading]
        lines.extend(["", f"## {title}"])
        items = groups.get(heading, [])
        if not items:
            lines.append("- None")
            continue
        for item in items:
            lines.append(
                f"- [{item.get('thread_type')} #{item.get('thread_number')}]({item.get('thread_url')})"
            )
            if heading == "auto-sent":
                lines.append(
                    f"  reason: confidence={item.get('decision_confidence')}, comment={item.get('comment_url')}"
                )
            elif heading == "needs-review":
                lines.append(
                    "  blockers: "
                    + (", ".join(item.get("blocking_reasons", [])) or "manual confirmation required")
                )
                if item.get("draft_body"):
                    lines.append("  draft:")
                    for paragraph in item["draft_body"].splitlines():
                        lines.append(f"    {paragraph}")
            else:
                lines.append(
                    "  detail: "
                    + (", ".join(item.get("blocking_reasons", [])) or "unable to classify")
                )
    return "\n".join(lines).strip() + "\n"


def parse_json_file(path: Path) -> Any:
    return json.loads(path.read_text())


def sync_mirror(repo: str, mirror_dir: Path, default_branch: str) -> dict[str, Any]:
    mirror_dir = mirror_dir.expanduser()
    repo_url = f"https://github.com/{repo}.git"
    if not mirror_dir.exists():
        mirror_dir.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                "git",
                "clone",
                "--origin",
                "origin",
                "--branch",
                default_branch,
                "--single-branch",
                repo_url,
                str(mirror_dir),
            ]
        )
    else:
        run_command(["git", "remote", "set-url", "origin", repo_url], cwd=mirror_dir)
        run_command(["git", "fetch", "--prune", "origin", default_branch], cwd=mirror_dir)

    run_command(["git", "checkout", "-B", default_branch, f"origin/{default_branch}"], cwd=mirror_dir)
    return {"default_branch": default_branch, "mirror_dir": str(mirror_dir), "repo": repo}


def checkout_pr_head(repo: str, mirror_dir: Path, pr_number: int) -> dict[str, Any]:
    mirror_dir = mirror_dir.expanduser()
    branch_name = f"automation/pr-{pr_number}"
    run_command(
        ["git", "fetch", "origin", f"pull/{pr_number}/head:{branch_name}"],
        cwd=mirror_dir,
    )
    run_command(["git", "checkout", branch_name], cwd=mirror_dir)
    return {"branch": branch_name, "mirror_dir": str(mirror_dir), "repo": repo}


def post_comment_via_cli(repo: str, number: int, body: str, *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {
            "body": body,
            "html_url": f"https://github.com/{repo}/issues/{number}#dry-run",
        }
    command = [
        "gh",
        "api",
        gh_repo_endpoint(repo, f"issues/{number}/comments"),
        "-f",
        f"body={body}",
    ]
    output = run_command(command)
    return json.loads(output)


def command_scan(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state_file).expanduser())
    maintainers = maintainer_set(args.maintainers.split(","))
    candidates = discover_candidates(
        args.repo,
        actor_login=args.actor,
        maintainers=maintainers,
        state=state,
        initial_lookback_hours=args.initial_lookback_hours,
    )
    print(json.dumps(candidates, indent=2))


def command_fetch_thread(args: argparse.Namespace) -> None:
    if args.thread_type == "issue":
        context = fetch_issue_context(args.repo, args.number)
        output = format_issue_markdown(context) if args.format == "markdown" else json.dumps(context, indent=2)
    else:
        context = fetch_pr_context(args.repo, args.number)
        output = format_pr_markdown(context) if args.format == "markdown" else json.dumps(context, indent=2)
    print(output)


def command_decide(args: argparse.Namespace) -> None:
    candidate = parse_json_file(Path(args.candidate_file))
    review_text = Path(args.review_file).read_text()
    decision = build_decision(candidate, review_text)
    print(json.dumps(decision, indent=2))


def command_post_comment(args: argparse.Namespace) -> None:
    decision = parse_json_file(Path(args.decision_file))
    response = post_comment_via_cli(
        decision["repo"],
        int(decision["thread_number"]),
        decision["prepared_body"],
        dry_run=args.dry_run,
    )
    decision["comment_url"] = response["html_url"]
    print(json.dumps(decision, indent=2))


def command_mark_processed(args: argparse.Namespace) -> None:
    state_path = Path(args.state_file).expanduser()
    state = load_state(state_path)
    candidate = parse_json_file(Path(args.candidate_file))
    decision = parse_json_file(Path(args.decision_file))
    updated = mark_processed(state, candidate, decision)
    save_state(state_path, updated)
    print(json.dumps(updated, indent=2))


def command_summarize(args: argparse.Namespace) -> None:
    results = parse_json_file(Path(args.results_file))
    print(render_summary(results), end="")


def command_sync_mirror(args: argparse.Namespace) -> None:
    print(json.dumps(sync_mirror(args.repo, Path(args.mirror_dir), args.default_branch), indent=2))


def command_checkout_pr_head(args: argparse.Namespace) -> None:
    print(json.dumps(checkout_pr_head(args.repo, Path(args.mirror_dir), args.number), indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync-mirror")
    sync_parser.add_argument("--repo", default=DEFAULT_REPO)
    sync_parser.add_argument("--mirror-dir", default=str(DEFAULT_MIRROR_DIR))
    sync_parser.add_argument("--default-branch", default="master")
    sync_parser.set_defaults(func=command_sync_mirror)

    checkout_parser = subparsers.add_parser("checkout-pr-head")
    checkout_parser.add_argument("--repo", default=DEFAULT_REPO)
    checkout_parser.add_argument("--mirror-dir", default=str(DEFAULT_MIRROR_DIR))
    checkout_parser.add_argument("--number", required=True, type=int)
    checkout_parser.set_defaults(func=command_checkout_pr_head)

    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--repo", default=DEFAULT_REPO)
    scan_parser.add_argument("--actor", required=True)
    scan_parser.add_argument("--maintainers", required=True)
    scan_parser.add_argument("--initial-lookback-hours", type=int, default=4)
    scan_parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    scan_parser.set_defaults(func=command_scan)

    fetch_parser = subparsers.add_parser("fetch-thread")
    fetch_parser.add_argument("--repo", default=DEFAULT_REPO)
    fetch_parser.add_argument("--thread-type", choices=["issue", "pr"], required=True)
    fetch_parser.add_argument("--number", required=True, type=int)
    fetch_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    fetch_parser.set_defaults(func=command_fetch_thread)

    decide_parser = subparsers.add_parser("decide")
    decide_parser.add_argument("--candidate-file", required=True)
    decide_parser.add_argument("--review-file", required=True)
    decide_parser.set_defaults(func=command_decide)

    post_parser = subparsers.add_parser("post-comment")
    post_parser.add_argument("--decision-file", required=True)
    post_parser.add_argument("--dry-run", action="store_true")
    post_parser.set_defaults(func=command_post_comment)

    mark_parser = subparsers.add_parser("mark-processed")
    mark_parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    mark_parser.add_argument("--candidate-file", required=True)
    mark_parser.add_argument("--decision-file", required=True)
    mark_parser.set_defaults(func=command_mark_processed)

    summarize_parser = subparsers.add_parser("summarize")
    summarize_parser.add_argument("--results-file", required=True)
    summarize_parser.set_defaults(func=command_summarize)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if isinstance(result, int):
        return result
    return 0


if __name__ == "__main__":
    sys.exit(main())
