from __future__ import annotations
import json
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import discovery_state as ds

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
REPO = "apolloconfig/apollo"


def candidate(signature="first", repo=REPO, number=1):
    return {"repo": repo, "number": number, "thread_type": "issue", "activity_signature": signature}


def increment(path):
    for _ in range(10):
        with ds.state_transaction(Path(path)) as state:
            state["counter"] = state.get("counter", 0) + 1


class DiscoveryStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_legacy_global_cursor_ignored_and_unknown_keys_preserved(self):
        self.path.write_text(json.dumps({"last_scan_at": NOW.isoformat(), "threads": {"old": {}}, "publication": {"x": 1}}))
        with ds.state_transaction(self.path) as state:
            self.assertEqual("2026-11-07T12:00:00Z", ds.scan_since(state, REPO, 4, now=NOW))
        self.assertEqual({"x": 1}, ds.load_state(self.path)["publication"])
        self.assertEqual({"old": {}}, ds.load_state(self.path)["threads"])

    def test_repository_cursors_and_overlap_are_independent(self):
        state = {"discovery_cursors": {REPO: NOW.isoformat()}}
        self.assertEqual("2026-11-08T11:00:00Z", ds.scan_since(state, REPO, now=NOW, overlap_hours=0))
        self.assertEqual("2026-11-07T12:00:00Z", ds.scan_since(state, "apolloconfig/other", now=NOW))
        self.assertEqual("2026-11-08T10:00:00Z", ds.scan_since(state, REPO, now=NOW, overlap_hours=2))

    def test_success_saves_scan_start_and_queue_survives_restart(self):
        with ds.state_transaction(self.path) as state:
            ds.commit_discovery(state, REPO, [candidate()], started_at=NOW)
        state = ds.load_state(self.path)
        self.assertEqual("2026-11-08T12:00:00Z", state["discovery_cursors"][REPO])
        self.assertEqual([candidate()], ds.pending_candidates(state, REPO))
        with ds.state_transaction(self.path) as state:
            ds.commit_discovery(state, REPO, [], started_at=NOW + timedelta(hours=12))
        self.assertEqual([candidate()], ds.pending_candidates(ds.load_state(self.path), REPO))

    def test_error_leaves_entire_state_unchanged(self):
        with ds.state_transaction(self.path) as state:
            ds.commit_discovery(state, REPO, [], started_at=NOW)
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError):
            with ds.state_transaction(self.path) as state:
                ds.commit_discovery(state, REPO, [candidate()], started_at=NOW + timedelta(hours=12))
                raise RuntimeError("page 2 failed")
        self.assertEqual(before, self.path.read_bytes())

    def test_replace_failure_preserves_prior_file_and_cleans_temp(self):
        with ds.state_transaction(self.path):
            pass
        before = self.path.read_bytes()
        with patch.object(ds.os, "replace", side_effect=OSError("disk error")), self.assertRaises(OSError):
            with ds.state_transaction(self.path) as state:
                ds.commit_discovery(state, REPO, [candidate()], started_at=NOW)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob(".state.json.*")))

    def test_old_ack_cannot_remove_new_work_or_advance_cursor(self):
        state = {}
        ds.commit_discovery(state, REPO, [candidate()], started_at=NOW)
        ds.commit_discovery(state, REPO, [candidate("new")], started_at=NOW + timedelta(hours=12))
        cursor = dict(state["discovery_cursors"])
        ds.acknowledge_candidate(state, candidate())
        self.assertEqual([candidate("new")], ds.pending_candidates(state, REPO))
        ds.acknowledge_candidate(state, candidate("new"))
        self.assertEqual([], ds.pending_candidates(state, REPO))
        self.assertEqual(cursor, state["discovery_cursors"])

    def test_processed_signature_not_requeued(self):
        item = candidate()
        state = {"threads": {ds.candidate_key(item): {"last_processed_signature": "first"}}}
        ds.commit_discovery(state, REPO, [item], started_at=NOW)
        self.assertEqual([], ds.pending_candidates(state, REPO))

    def test_invalid_candidate_rolls_back_whole_scan(self):
        with self.assertRaises(ValueError):
            with ds.state_transaction(self.path) as state:
                ds.commit_discovery(state, REPO, [candidate(), candidate(repo="other/repo")], started_at=NOW)
        self.assertFalse(self.path.exists())

    def test_corrupt_state_fails_closed(self):
        for raw in ("", "{", "[]", '{"pending_candidates": []}', '{"discovery_version": 2}'):
            self.path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                with ds.state_transaction(self.path):
                    pass
            self.assertEqual(raw, self.path.read_text())

    def test_scan_cursor_cannot_move_backwards(self):
        state = {}
        ds.commit_discovery(state, REPO, [], started_at=NOW)
        ds.commit_discovery(state, REPO, [], started_at=NOW - timedelta(hours=12))
        self.assertEqual("2026-11-08T12:00:00Z", state["discovery_cursors"][REPO])

    def test_overlapping_processes_do_not_lose_updates(self):
        processes = [multiprocessing.Process(target=increment, args=(str(self.path),)) for _ in range(3)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(0, process.exitcode)
        self.assertEqual(30, ds.load_state(self.path)["counter"])


if __name__ == "__main__":
    unittest.main()
