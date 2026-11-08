"""State aliases must share one lock, recovery target, queue, cursor and outbox."""
import argparse
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
import multiprocessing
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import community_review as cr
import discovery_state as ds
import publish_gate as gate
import test_feedback_discovery_regressions as discovery_fixtures
import test_publish_gate as fixtures


def increment_state(path):
    for _ in range(10):
        with ds.state_transaction(Path(path)) as state:
            state["counter"] = state.get("counter", 0) + 1


class StatePathRegressions(unittest.TestCase):
    def setUp(self):
        self.discovery = discovery_fixtures.FeedbackDiscoveryRegressions(methodName="runTest")
        self.discovery.setUp()
        self.addCleanup(self.discovery.doCleanups)
        self.world = self.discovery.world
        self.target = self.world.state.resolve()
        self.alias = self.target.with_name("alias.json")
        # A relative symlink target also exercises resolution from the link's directory.
        self.alias.symlink_to(self.target.name)
        self.original = self.world.decision()
        self.key = cr.state_key(self.original["candidate"])
        self.operation = gate.operation_id(self.original)
        self.discovery.save_pending(self.original["candidate"])
        self.initial_cursors = deepcopy(ds.load_state(self.target)["discovery_cursors"])
        self.discovery.args.state_file = str(self.alias)

    @contextmanager
    def canonical_io(self):
        """Assert entry-path resolution precedes locking and every save stays there."""
        transaction = ds.state_transaction
        flock = ds.fcntl.flock
        save = ds.atomic_save
        resolve = Path.resolve
        held = False
        entries = []
        saves = []

        @contextmanager
        def checked_transaction(path):
            self.assertEqual(self.target, path, "entry point must resolve before the transaction")
            entries.append(path)
            with transaction(path) as state:
                yield state

        def checked_flock(fd, operation):
            nonlocal held
            result = flock(fd, operation)
            held = operation != ds.fcntl.LOCK_UN
            return result

        def checked_save(path, state):
            self.assertTrue(held, "explicit and final saves must hold the state lock")
            self.assertEqual(self.target, path, "all saves must use the captured canonical path")
            saves.append(deepcopy(state))
            return save(path, state)

        def checked_resolve(path, *args, **kwargs):
            self.assertFalse(held, "do not resolve an alias after acquiring the lock")
            return resolve(path, *args, **kwargs)

        with patch.object(ds, "state_transaction", checked_transaction), \
                patch.object(ds.fcntl, "flock", checked_flock), \
                patch.object(ds, "atomic_save", checked_save), \
                patch.object(Path, "resolve", checked_resolve):
            yield saves
        self.assertEqual([self.target], entries)
        self.assertFalse(held)
        self.assertTrue(self.target.with_name(self.target.name + ".lock").exists())
        self.assertFalse(self.alias.with_name(self.alias.name + ".lock").exists())

    def state(self):
        self.assertTrue(self.alias.is_symlink(), "atomic saves must not replace the alias")
        self.assertEqual(self.target.read_bytes(), self.alias.read_bytes())
        return ds.load_state(self.target)

    def publish(self, *, uncertain=False, path=None):
        if uncertain:
            self.world.send.side_effect = TimeoutError("response lost")
            with self.assertRaises(TimeoutError):
                gate.publish_decision(self.original, state_path=path or self.target, dry_run=False)
            return None
        return gate.publish_decision(self.original, state_path=path or self.target, dry_run=False)

    def mark(self, result, path=None):
        root = self.target.parent
        (root / "candidate.json").write_text(json.dumps(self.original["candidate"]))
        (root / "result.json").write_text(json.dumps(result))
        args = argparse.Namespace(candidate_file=str(root / "candidate.json"),
                                  decision_file=str(root / "result.json"),
                                  state_file=str(path or self.alias))
        with redirect_stdout(io.StringIO()):
            cr.command_mark_processed(args)

    def results(self, path=None):
        args = argparse.Namespace(repo=fixtures.REPO, actor="operator",
                                  state_file=str(path or self.alias), since=None)
        output = io.StringIO()
        with redirect_stdout(output):
            cr.command_scan_results(args)
        return json.loads(output.getvalue())

    def assert_recovered(self, cursors):
        state = self.state()
        self.assertEqual(cursors, state["discovery_cursors"])
        self.assertEqual({}, state["pending_candidates"])
        self.assertEqual(self.original["activity_signature"], state["threads"][self.key]["last_processed_signature"])
        self.assertEqual(self.original["candidate"]["context_fingerprint"],
                         state["threads"][self.key]["last_processed_context_fingerprint"])
        record = state["publication_journal"][self.key]
        self.assertEqual("sent", record["status"])
        self.assertFalse(record["recovery_pending"])
        self.assertEqual(self.original, record["decision"])
        self.assertEqual({self.operation}, set(state["publication_results"]))
        result = state["publication_results"][self.operation]["result"]
        self.assertEqual({**self.original, "result_group": "auto-sent", "comment_url": fixtures.URL},
                         {key: value for key, value in result.items() if key != "deduplicated"})
        self.assertEqual([result], self.results())
        self.assertEqual(1, self.world.send.call_count)
        return state

    def check_scan_recovery(self, *, uncertain=False, fail=False):
        self.publish(uncertain=uncertain)
        original_outbox = deepcopy(self.state().get("publication_results"))
        self.discovery.accepted_post()
        with self.canonical_io() as saves:
            if fail:
                with patch.object(cr, "discover_candidates", side_effect=RuntimeError("later scan failed")), \
                        self.assertRaisesRegex(RuntimeError, "later scan failed"):
                    self.discovery.scan()
            else:
                self.assertEqual([], self.discovery.scan())
        expected_cursors = self.initial_cursors if fail else {fixtures.REPO: cr.to_iso(fixtures.NOW)}
        recovered = self.assert_recovered(expected_cursors)
        if original_outbox is not None:
            self.assertEqual(original_outbox, recovered["publication_results"])
        # Failed discovery still keeps the explicit recovery save, without a final commit.
        self.assertEqual((2 if uncertain else 1) + (0 if fail else 1), len(saves))

    def test_symlink_scan_recovers_success_and_advances_one_cursor(self):
        self.check_scan_recovery()

    def test_symlink_scan_reconciles_timeout_and_advances_one_cursor(self):
        self.check_scan_recovery(uncertain=True)

    def test_symlink_success_recovery_survives_later_scan_failure(self):
        self.check_scan_recovery(fail=True)

    def test_symlink_timeout_recovery_survives_later_scan_failure(self):
        self.check_scan_recovery(uncertain=True, fail=True)

    def test_unresolved_symlink_recovery_persists_hold_after_scan_failure(self):
        self.publish(uncertain=True)
        before = self.state()
        with self.canonical_io(), \
                patch.object(cr, "discover_candidates", side_effect=RuntimeError("later scan failed")), \
                self.assertRaises(RuntimeError):
            self.discovery.scan()
        state = self.state()
        self.assertEqual(self.initial_cursors, state["discovery_cursors"])
        self.assertEqual(before["publication_journal"], state["publication_journal"])
        self.assertEqual({self.key: {**self.original["candidate"], "publication_hold": True}},
                         state["pending_candidates"])
        self.assertEqual({}, state["threads"])
        self.assertFalse(state.get("publication_results"))
        self.assertEqual(1, self.world.send.call_count)

    def test_standalone_symlink_recovery_and_ack_are_idempotent(self):
        result = self.publish()
        with self.canonical_io():
            self.assertEqual(set(), cr.recover_publication_state(self.alias, repo=fixtures.REPO))
        recovered = self.assert_recovered(self.initial_cursors)
        with self.canonical_io():
            cr.recover_publication_state(self.alias, key=self.key)
        with self.canonical_io():
            self.mark(result)
        self.assertEqual(recovered, self.state())

    def test_standalone_timeout_confirmation_survives_interrupted_ack(self):
        self.publish(uncertain=True)
        self.discovery.accepted_post()
        with self.canonical_io(), \
                patch.object(gate, "require_acknowledgeable", side_effect=RuntimeError("ACK interrupted")), \
                self.assertRaisesRegex(RuntimeError, "ACK interrupted"):
            cr.recover_publication_state(self.alias, key=self.key)
        interrupted = self.state()
        self.assertEqual(self.initial_cursors, interrupted["discovery_cursors"])
        self.assertEqual("sent", interrupted["publication_journal"][self.key]["status"])
        self.assertTrue(interrupted["publication_journal"][self.key]["recovery_pending"])
        self.assertEqual({self.key: self.original["candidate"]}, interrupted["pending_candidates"])
        self.assertEqual({}, interrupted["threads"])
        self.assertEqual({self.operation}, set(interrupted["publication_results"]))
        with self.canonical_io():
            cr.recover_publication_state(self.alias, key=self.key)
        self.assert_recovered(self.initial_cursors)

    def test_symlink_mark_processed_recovers_confirmed_original(self):
        result = self.publish()
        with self.canonical_io():
            self.mark(result)
        self.assert_recovered(self.initial_cursors)

    def test_symlink_mark_processed_reconciles_timeout_before_ack(self):
        self.publish(uncertain=True)
        self.discovery.accepted_post()
        result = {**self.original, "result_group": "auto-sent", "comment_url": fixtures.URL}
        with self.canonical_io():
            self.mark(result)
        self.assert_recovered(self.initial_cursors)

    def test_symlink_mark_processed_cannot_clear_uncertain_send(self):
        self.publish(uncertain=True)
        result = {**self.original, "result_group": "skipped"}
        with self.canonical_io(), self.assertRaisesRegex(ValueError, "uncertain"):
            self.mark(result)
        state = self.state()
        self.assertEqual(self.initial_cursors, state["discovery_cursors"])
        self.assertEqual("uncertain", state["publication_journal"][self.key]["status"])
        self.assertTrue(state["pending_candidates"][self.key]["publication_hold"])
        self.assertEqual({}, state["threads"])
        self.assertFalse(state.get("publication_results"))

    def test_symlink_publisher_and_canonical_reader_share_outbox(self):
        with self.canonical_io():
            result = self.publish(path=self.alias)
        state = self.state()
        self.assertEqual([result], self.results(self.target))
        self.assertEqual([result], self.results(self.alias))
        self.assertEqual(self.initial_cursors, state["discovery_cursors"])
        self.assertEqual({self.key: self.original["candidate"]}, state["pending_candidates"])
        with self.canonical_io():
            cached = self.publish(path=self.alias)
        self.assertEqual({**result, "deduplicated": True}, cached)
        self.assertEqual(1, self.world.send.call_count)

    def test_symlink_publisher_reconciles_without_resending(self):
        with self.canonical_io():
            self.publish(uncertain=True, path=self.alias)
        self.discovery.accepted_post()
        with self.canonical_io():
            result = self.publish(path=self.alias)
        self.assertEqual(self.original["candidate"], result["candidate"])
        self.assertEqual([result], self.results(self.target))
        self.assertTrue(self.state()["publication_journal"][self.key]["recovery_pending"])
        self.assertEqual(1, self.world.send.call_count)

    def test_failed_recovery_replace_preserves_target_alias_and_temp_cleanup(self):
        self.publish()
        before = self.target.read_bytes()
        with self.canonical_io(), patch.object(ds.os, "replace", side_effect=OSError("disk error")), \
                self.assertRaisesRegex(OSError, "disk error"):
            cr.recover_publication_state(self.alias)
        self.assertEqual(before, self.target.read_bytes())
        self.state()
        self.assertEqual([], list(self.target.parent.glob(".state.json.*")))
        with self.canonical_io():
            cr.recover_publication_state(self.alias)
        self.assert_recovered(self.initial_cursors)

    def test_relative_home_and_default_paths_choose_the_canonical_target(self):
        cwd = Path.cwd()
        relative = os.path.relpath(self.alias, cwd)
        with patch.dict(os.environ, {"HOME": str(self.target.parent)}):
            for path in (relative, "~/alias.json", str(self.alias), str(self.target)):
                with self.subTest(path=path):
                    self.assertEqual(self.target, cr.resolve_state_path(path, "operator"))
        # Both actor-scoped and legacy default paths can also be symlink aliases.
        with patch.object(cr, "DEFAULT_STATE_ROOT", self.target.parent), \
                patch.object(cr, "DEFAULT_STATE_FILE", self.alias):
            actor_dir = self.target.parent / "operator"
            actor_dir.mkdir()
            (actor_dir / "state.json").symlink_to(self.target)
            self.assertEqual(self.target, cr.resolve_state_path(None, "operator"))
            self.assertEqual(self.target, cr.resolve_state_path(None))
        missing = self.target.parent / "not-created" / "state.json"
        self.assertEqual(missing, cr.resolve_state_path(str(missing)))

    def test_relative_scan_and_home_recovery_ack_and_read_use_one_target(self):
        self.discovery.args.state_file = os.path.relpath(self.alias, Path.cwd())
        self.check_scan_recovery()
        result = self.results()[0]
        with patch.dict(os.environ, {"HOME": str(self.target.parent)}):
            with self.canonical_io():
                cr.recover_publication_state(Path("~/alias.json"))
            with self.canonical_io():
                self.mark(result, "~/alias.json")
            self.assertEqual([result], self.results("~/alias.json"))
        self.assert_recovered({fixtures.REPO: cr.to_iso(fixtures.NOW)})

    def test_alias_and_canonical_transactions_serialize_without_lost_updates(self):
        paths = [str(self.alias), str(self.target), str(self.alias)]
        processes = [multiprocessing.Process(target=increment_state, args=(path,)) for path in paths]
        for process in processes:
            process.start()
        try:
            for process in processes:
                process.join(15)
                self.assertEqual(0, process.exitcode)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        self.assertEqual(30, self.state()["counter"])
        self.assertTrue(self.target.with_name(self.target.name + ".lock").exists())
        self.assertFalse(self.alias.with_name(self.alias.name + ".lock").exists())


if __name__ == "__main__":
    unittest.main()
