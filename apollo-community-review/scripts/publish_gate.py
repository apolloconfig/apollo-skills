"""Fail-closed public posting gate. No network writes occur on import or dry run.

An ambiguous write is never repeated automatically, even when a subsequent read
does not find it. All operators of one actor must share the same durable state.
"""
from __future__ import annotations

from copy import deepcopy
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


def require_fresh_review(decision: dict[str, Any]) -> None:
    created = cr.iso_to_datetime(decision.get("created_at"))
    now = cr.utc_now()
    if created is None or created.tzinfo is None or not timedelta(0) <= now - created <= timedelta(hours=1):
        raise ValueError("review is older than one hour or timestamp is invalid; review again")


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
    require_fresh_review(decision)
    fresh = fresh_candidate(candidate)
    for field in ("activity_signature", "context_fingerprint", "head_sha"):
        if fresh.get(field) != candidate.get(field) or (field == "context_fingerprint" and not fresh.get(field)):
            raise ValueError(f"{field} changed; fetch current context and review again")
    if candidate.get("discovery_uncertainty"):
        fresh["discovery_uncertainty"] = candidate["discovery_uncertainty"]
    if candidate.get("publication_hold"):
        fresh["publication_hold"] = True
    current = cr.build_decision(fresh, decision["review_text"])
    if not current["auto_send_eligible"]:
        raise ValueError("post-time policy rejected: " + "; ".join(current["blocking_reasons"]))
    for field in ("normalized_review", "draft_body", "prepared_body", "recommended_action"):
        if current.get(field) != decision.get(field):
            raise ValueError(f"decision {field} was modified after policy evaluation")
    current["created_at"] = decision["created_at"]
    require_fresh_review(current)
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
        if marker not in (post.get("body") or "").splitlines() or (post.get("user", {}).get("login") or "").lower() != actor:
            continue
        if action == "request changes" and (post.get("state") != "CHANGES_REQUESTED"
                                            or post.get("commit_id") != decision["candidate"].get("head_sha")):
            continue
        if isinstance(post.get("html_url"), str) and post["html_url"].startswith(f"https://github.com/{repo}/"):
            return post
    return None


def original_publication(record: dict[str, Any]) -> dict[str, Any]:
    """Return the durable original snapshot, never a caller's newer review."""
    original = record.get("decision")
    try:
        if not isinstance(original, dict) or not original["candidate"].get("context_fingerprint"):
            raise ValueError("missing original context")
        candidate = original["candidate"]
        if not isinstance(candidate.get("viewer_actor"), str) or not candidate["viewer_actor"]:
            raise ValueError("missing original actor")
        for top, inner in (("repo", "repo"), ("thread_number", "number"),
                           ("thread_type", "thread_type"), ("activity_signature", "activity_signature")):
            if original.get(top) != candidate.get(inner):
                raise ValueError("original target mismatch")
        if original.get("recommended_action") not in {"comment", "request changes"}:
            raise ValueError("invalid original action")
        op_id = operation_id(original)
        if record.get("operation_id") != op_id or record.get("marker") != f"<!-- apollo-community-review:{op_id} -->":
            raise ValueError("original marker mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("publication journal lacks a trustworthy original snapshot; require operator reconciliation") from exc
    return deepcopy(original)


def publication_matches(record: dict[str, Any], decision: dict[str, Any]) -> bool:
    """Strict acknowledgement binding; result decoration is not review evidence."""
    try:
        original = original_publication(record)
    except ValueError:
        return False
    result_fields = {"result_group", "comment_url", "deduplicated", "dry_run"}
    return (record.get("status") == "sent" and bool(record.get("url"))
            and record.get("url") == decision.get("comment_url")
            and {k: v for k, v in original.items() if k not in result_fields}
            == {k: v for k, v in decision.items() if k not in result_fields})


def require_acknowledgeable(state: dict[str, Any], decision: dict[str, Any]) -> None:
    record = state.get("publication_journal", {}).get(cr.state_key(decision["candidate"]), {})
    if record.get("status") in {"pending", "uncertain"}:
        raise ValueError("publication outcome remains uncertain; cannot acknowledge this thread")
    if decision.get("result_group") == "auto-sent" and not publication_matches(record, decision):
        raise ValueError("auto-sent acknowledgement requires the matching original confirmed publication snapshot in the publication journal")


def confirm_publication(state: dict[str, Any], record: dict[str, Any], url: str, *,
                        state_path: Path, deduplicated: bool = False) -> dict[str, Any]:
    """Persist confirmation and its original result together while holding the state lock.

    Both a successful response and read-only reconciliation need the same durable
    outbox and recovery flag: callers can lose stdout after either confirmation.
    Recovery may acknowledge only the exact original queued snapshot, and a later
    operation replacing the journal slot must not erase this report.
    state_path must be the canonical target selected before acquiring the lock.
    """
    original = original_publication(record)
    result = {**original, "result_group": "auto-sent", "comment_url": url}
    if deduplicated:
        result["deduplicated"] = True
    record.update(status="sent", url=url, recovery_pending=True)
    state.setdefault("publication_results", {}).setdefault(record["operation_id"], {
        "recorded_at": cr.to_iso(cr.utc_now()), "result": result,
    })
    ds.atomic_save(state_path, state)
    return result


def reconcile_publications(state: dict[str, Any], *, state_path: Path,
                           key: str | None = None, repo: str | None = None) -> dict[str, str]:
    """Read remote posts only; caller MUST hold state_transaction's lock.

    Run before changing discovery/acknowledgement state. Each confirmed outcome
    is saved immediately, so unrelated subsequent discovery failure cannot erase
    it. Unresolved records stay pending/uncertain and are returned for actionable
    reporting; no retry or queue acknowledgement is performed here.
    state_path must be the canonical target selected before acquiring the lock.
    """
    unresolved = {}
    for record_key, record in state.setdefault("publication_journal", {}).items():
        if (key is not None and record_key != key) or record.get("status") not in {"pending", "uncertain"}:
            continue
        if repo is not None and record_key.split("#", 1)[0] != repo:
            continue
        try:
            original = original_publication(record)
            if cr.state_key(original["candidate"]) != record_key:
                raise ValueError("publication journal target mismatch")
            require_public_repo(original["repo"])
            if cr.detect_github_login().lower() != original["candidate"]["viewer_actor"].lower():
                raise ValueError("actor changed during publication reconciliation")
            found = find_post(original, record["marker"])
            if not found:
                raise ValueError("previous write outcome is uncertain; do not retry, require operator reconciliation")
        except Exception as exc:
            unresolved[record_key] = str(exc)
            continue
        confirm_publication(state, record, found["html_url"], state_path=state_path, deduplicated=True)
    return unresolved


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
        if prior and prior.get("status") in {"pending", "uncertain"}:
            unresolved = reconcile_publications(state, state_path=state_path, key=key)
            if key in unresolved:
                raise ValueError("previous write outcome is uncertain: " + unresolved[key])
        if prior and prior.get("operation_id") == op_id and prior.get("status") == "sent":
            original = original_publication(prior)
            return {**original, "result_group": "auto-sent", "comment_url": prior["url"], "deduplicated": True}
        current = revalidate(decision)
        if dry_run:
            return {**current, "result_group": "dry-run", "dry_run": True}
        duplicate = find_post(current, marker)
        if duplicate:
            # The marker alone does not encode context or actor. Without its
            # saved snapshot it cannot certify this caller's reviewed context.
            raise ValueError("existing publication marker lacks its original snapshot; require operator reconciliation")
        require_fresh_review(current)
        record = {"operation_id": op_id, "status": "pending", "marker": marker, "decision": deepcopy(current)}
        journal[key] = record
        # Persist intention BEFORE remote write. Crash/timeout/invalid JSON all leave a non-retryable record.
        ds.atomic_save(state_path, state)
        try:
            require_fresh_review(current)
        except ValueError:
            # No write was attempted. Restore the prior journal rather than
            # misclassifying local expiration as an ambiguous remote write.
            if prior is None:
                journal.pop(key, None)
            else:
                journal[key] = prior
            ds.atomic_save(state_path, state)
            raise
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
        return confirm_publication(state, record, url, state_path=state_path)
