"""Fail-closed public posting gate. No network writes occur on import or dry run.

An ambiguous write is never repeated automatically, even when a subsequent read
does not find it. All operators of one actor must share the same durable state.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import community_review as cr
import discovery_state as ds


def require_public_repo(repo: str) -> dict[str, Any]:
    if not re.fullmatch(r"apolloconfig/[A-Za-z0-9_.-]+", repo):
        raise ValueError("public community workflow is restricted to apolloconfig repositories")
    if "ghsa-" in repo.lower():
        raise ValueError("security advisory repositories require a separate private workflow")
    metadata = cr.gh_api_json(f"repos/{repo}")
    if (metadata.get("private") is not False or metadata.get("visibility", "public") != "public"
            or metadata.get("archived") or metadata.get("disabled")):
        raise ValueError("repository must be confirmed public, active and enabled")
    return metadata


def fresh_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    repo, number = candidate["repo"], candidate["number"]
    actor = candidate.get("viewer_actor")
    if not isinstance(actor, str) or not actor:
        raise ValueError("candidate is missing authenticated actor")
    if cr.detect_github_login().lower() != actor.lower():
        raise ValueError("authenticated GitHub actor changed; review again")
    maintainers = candidate.get("maintainers")
    if not isinstance(maintainers, list) or not maintainers:
        raise ValueError("candidate is missing maintainer scope")
    require_public_repo(repo)
    issue = cr.gh_api_json(cr.gh_repo_endpoint(repo, f"issues/{number}"))
    if issue.get("state") != "open" or issue.get("locked"):
        raise ValueError("thread is closed or locked")
    actual_type = "pr" if issue.get("pull_request") else "issue"
    if actual_type != candidate["thread_type"]:
        raise ValueError("thread type changed or decision target is inconsistent")
    builder = cr.build_pr_candidate if actual_type == "pr" else cr.build_issue_candidate
    return builder(repo, issue, actor_login=actor,
                   maintainers=cr.maintainer_set(maintainers) | {actor.lower()})


def revalidate(decision: dict[str, Any]) -> dict[str, Any]:
    if decision.get("schema_version") != 2 or decision.get("auto_send_eligible") is not True:
        raise ValueError("decision is not eligible or uses the obsolete decision schema")
    candidate = decision["candidate"]
    for top, inner in (("repo", "repo"), ("thread_number", "number"), ("thread_type", "thread_type"),
                       ("activity_signature", "activity_signature")):
        if decision.get(top) != candidate.get(inner):
            raise ValueError("decision target does not match reviewed candidate")
    if type(candidate.get("number")) is not int or candidate["number"] <= 0:
        raise ValueError("invalid thread number")
    created = cr.iso_to_datetime(decision.get("created_at"))
    now = cr.utc_now()
    if created is None or created.tzinfo is None or not timedelta(0) <= now - created <= timedelta(hours=1):
        raise ValueError("review is older than one hour or timestamp is invalid; review again")
    fresh = fresh_candidate(candidate)
    for field in ("activity_signature", "context_fingerprint", "head_sha"):
        if fresh.get(field) != candidate.get(field) or (field == "context_fingerprint" and not fresh.get(field)):
            raise ValueError(f"{field} changed; fetch current context and review again")
    if candidate.get("discovery_uncertainty"):
        fresh["discovery_uncertainty"] = candidate["discovery_uncertainty"]
    current = cr.build_decision(fresh, decision["review_text"])
    if not current["auto_send_eligible"]:
        raise ValueError("post-time policy rejected: " + "; ".join(current["blocking_reasons"]))
    for field in ("normalized_review", "draft_body", "prepared_body", "recommended_action"):
        if current.get(field) != decision.get(field):
            raise ValueError(f"decision {field} was modified after policy evaluation")
    return current


def operation_id(decision: dict[str, Any]) -> str:
    payload = [decision["repo"], decision["thread_number"], decision["thread_type"],
               decision["activity_signature"], decision["candidate"].get("head_sha"),
               decision["recommended_action"], decision["prepared_body"]]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def find_post(decision: dict[str, Any], marker: str) -> dict[str, Any] | None:
    repo, number = decision["repo"], decision["thread_number"]
    action = decision["recommended_action"]
    endpoint = f"pulls/{number}/reviews" if action == "request changes" else f"issues/{number}/comments"
    posts = cr.gh_api_json(cr.gh_repo_endpoint(repo, endpoint, {"per_page": 100}), paginate=True)
    actor = decision["candidate"]["viewer_actor"].lower()
    for post in posts:
        if marker not in (post.get("body") or "") or (post.get("user", {}).get("login") or "").lower() != actor:
            continue
        if action == "request changes" and (post.get("state") != "CHANGES_REQUESTED"
                                            or post.get("commit_id") != decision["candidate"].get("head_sha")):
            continue
        if post.get("html_url"):
            return post
    return None


def send_once(decision: dict[str, Any], body: str) -> dict[str, Any]:
    if decision["recommended_action"] == "comment":
        return cr.post_comment_via_cli(decision["repo"], decision["thread_number"], body, dry_run=False)
    if decision["recommended_action"] != "request changes" or decision["thread_type"] != "pr":
        raise ValueError("unsupported automatic action")
    # Pin formal review to the exact reviewed commit. APPROVE is never supported.
    output = cr.run_command([
        "gh", "api", cr.gh_repo_endpoint(decision["repo"], f"pulls/{decision['thread_number']}/reviews"),
        "--method", "POST", "-f", "event=REQUEST_CHANGES", "-f", f"body={body}",
        "-f", f"commit_id={decision['candidate']['head_sha']}",
    ])
    return json.loads(output)


def publish_decision(decision: dict[str, Any], *, state_path: Path, dry_run: bool) -> dict[str, Any]:
    state_path = state_path.expanduser().resolve()
    # Validate identity before journal lookup. Untrusted decision files cannot select arbitrary endpoints.
    candidate = decision.get("candidate", {})
    if not re.fullmatch(r"apolloconfig/[A-Za-z0-9_.-]+", str(decision.get("repo", ""))):
        raise ValueError("invalid public repository")
    if decision.get("repo") != candidate.get("repo") or decision.get("thread_number") != candidate.get("number"):
        raise ValueError("decision target mismatch")
    if decision.get("recommended_action") not in {"comment", "request changes"}:
        raise ValueError("approvals and other formal actions require per-PR confirmation outside automation")
    key = cr.state_key(candidate)
    op_id = operation_id(decision)
    marker = f"<!-- apollo-community-review:{op_id} -->"
    with ds.state_transaction(state_path) as state:
        journal = state.setdefault("publication_journal", {})
        prior = journal.get(key)
        if prior and prior.get("operation_id") == op_id and prior.get("status") == "sent":
            return {**decision, "result_group": "auto-sent", "comment_url": prior["url"], "deduplicated": True}
        if prior and prior.get("status") in {"pending", "uncertain"}:
            # Reconcile the ORIGINAL operation even if a newer decision arrived.
            original = prior["decision"]
            require_public_repo(original["repo"])
            if cr.detect_github_login().lower() != original["candidate"]["viewer_actor"].lower():
                raise ValueError("actor changed during publication reconciliation")
            found = find_post(original, prior["marker"])
            if not found:
                raise ValueError("previous write outcome is uncertain; do not retry, require operator reconciliation")
            prior.update(status="sent", url=found["html_url"])
            ds.atomic_save(state_path, state)
            if prior["operation_id"] == op_id:
                return {**original, "result_group": "auto-sent", "comment_url": found["html_url"], "deduplicated": True}
        current = revalidate(decision)
        if dry_run:
            return {**current, "result_group": "dry-run", "dry_run": True}
        duplicate = find_post(current, marker)
        if duplicate:
            journal[key] = {"operation_id": op_id, "status": "sent", "url": duplicate["html_url"]}
            return {**current, "result_group": "auto-sent", "comment_url": duplicate["html_url"], "deduplicated": True}
        record = {"operation_id": op_id, "status": "pending", "marker": marker, "decision": current}
        journal[key] = record
        # Persist intention BEFORE remote write. Crash/timeout/invalid JSON all leave a non-retryable record.
        ds.atomic_save(state_path, state)
        try:
            response = send_once(current, current["prepared_body"] + "\n\n" + marker)
            url = response.get("html_url")
            expected = f"https://github.com/{current['repo']}/"
            if not isinstance(url, str) or not url.startswith(expected):
                raise ValueError("GitHub write returned no trustworthy result URL")
        except Exception:
            record["status"] = "uncertain"
            ds.atomic_save(state_path, state)
            raise
        record.update(status="sent", url=url)
        record.pop("decision", None)
        return {**current, "result_group": "auto-sent", "comment_url": url}
