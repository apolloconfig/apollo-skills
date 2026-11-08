"""Crash-safe public discovery state. All writers must use state_transaction.

The sidecar flock serializes scans and acknowledgements, including discovery API
calls. A failed scan never commits a cursor or partial queue. Requires POSIX.
Legacy global last_scan_at is deliberately not trusted as a discovery cursor.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator


def _time(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("Discovery timestamps must have a timezone")
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _time(value).isoformat().replace("+00:00", "Z")


def candidate_key(candidate: dict[str, Any]) -> str:
    return f"{candidate['repo']}#{candidate['thread_type']}#{candidate['number']}"


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        # Empty or malformed files are errors, never silently reset cursors.
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise ValueError("State must be a JSON object")
    else:
        state = {}
    if state.get("discovery_version", 1) != 1:
        raise ValueError("Unsupported discovery state version")
    for field in ("threads", "discovery_cursors", "pending_candidates"):
        state.setdefault(field, {})
        if not isinstance(state[field], dict):
            raise ValueError(f"Invalid state field: {field}")
    state["discovery_version"] = 1
    return state


def atomic_save(path: Path, state: dict[str, Any]) -> None:
    """Atomically replace state. Caller must hold state_transaction's lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def state_transaction(path: Path) -> Iterator[dict[str, Any]]:
    """Read current state under a stable sidecar lock; save only on success."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = load_state(path)
            yield state
            atomic_save(path, state)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def scan_since(state: dict[str, Any], repo: str, initial_lookback_hours: int = 24,
               *, now: datetime | None = None, overlap_hours: int = 1) -> str:
    now = _time(now or datetime.now(timezone.utc))
    cursor = state.get("discovery_cursors", {}).get(repo)
    # Clamp unsafe caller configuration; twice-daily scans need a full-day bootstrap.
    if cursor:
        start = min(_time(cursor), now) - timedelta(hours=max(1, overlap_hours))
    else:
        start = now - timedelta(hours=max(24, initial_lookback_hours))
    return _iso(start)


def pending_candidates(state: dict[str, Any], repo: str) -> list[dict[str, Any]]:
    return [candidate for key, candidate in sorted(state.get("pending_candidates", {}).items())
            if candidate["repo"] == repo]


def commit_discovery(state: dict[str, Any], repo: str, candidates: list[dict[str, Any]],
                     *, started_at: datetime) -> None:
    """Call only after ALL discovery succeeds, within state_transaction.

    Queue snapshots are replaced by newer discovery of the same thread. An older
    worker cannot acknowledge that newer snapshot because acknowledgement checks
    its activity signature. Candidates already processed at this signature skip.
    """
    queue = state.setdefault("pending_candidates", {})
    for candidate in candidates:
        if candidate["repo"] != repo:
            raise ValueError("Candidate repository does not match scan")
        if not candidate.get("activity_signature"):
            raise ValueError("Candidate requires activity_signature")
        key = candidate_key(candidate)
        if state.get("threads", {}).get(key, {}).get("last_processed_signature") != candidate["activity_signature"]:
            queue[key] = candidate
    cursors = state.setdefault("discovery_cursors", {})
    start = _time(started_at)
    previous = cursors.get(repo)
    cursors[repo] = _iso(max(start, _time(previous))) if previous else _iso(start)


def acknowledge_candidate(state: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Remove only the completed snapshot; never change discovery cursors."""
    key = candidate_key(candidate)
    queue = state.setdefault("pending_candidates", {})
    queued = queue.get(key)
    if (queued and queued.get("activity_signature") == candidate.get("activity_signature")
            and queued.get("context_fingerprint") == candidate.get("context_fingerprint")):
        del queue[key]
