"""MAN-10/11: generation closure, canonical locks and historical freshness."""
from copy import deepcopy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import shutil

import test_core as fixtures
from agent_harness import dependency_commit, runner
from agent_harness.core import AcceptanceDeadlineExhausted, HarnessError, Run


class DependencyCommitTests(unittest.TestCase):
    def fixture(self):
        value = fixtures.CoreIntegrationTests()
        value.setUp()
        self.addCleanup(value.tearDown)
        return value

    def downstream(self, fixture, parent, verdict, name):
        source = fixture.base / (name + "-source")
        source.mkdir()
        (source / "main.py").write_text("independent = True\n")
        contract = deepcopy(fixture.contract)
        contract.update(run_id=name, cwd=str(source), dependencies=[{"run_dir": str(parent.path), "verdict_digest": verdict["digest"]}])
        run = fixture.run_fixture(contract, name=name)
        run.freeze()
        runner.run_check(run, "unit")
        return run

    def test_upstream_generation_changes_before_commit_refuse_publication(self):
        fixture = self.fixture()
        parent, _ = fixture.passing()
        verdict = runner.gate(parent)
        child = self.downstream(fixture, parent, verdict, "child")
        original = dependency_commit.commit_guard
        attempts = []
        @contextmanager
        def barrier(run, observed, **options):
            attempts.append(True)
            with parent.edit() as state:
                state["checkpoints"] += 1
            with original(run, observed, **options) as locked:
                yield locked
        with patch.object(dependency_commit, "commit_guard", barrier):
            with self.assertRaisesRegex(HarnessError, "DEPENDENCY_RETRY_EXHAUSTED"):
                runner.gate(child)
        self.assertEqual(len(attempts), 3)
        self.assertIsNone(child.state()["accepted"])

    def test_generation_race_then_stable_reprepares_with_released_locks(self):
        fixture = self.fixture()
        parent, _ = fixture.passing()
        child = self.downstream(fixture, parent, runner.gate(parent), "child")
        original = dependency_commit.commit_guard
        attempts = []
        @contextmanager
        def once(run, observed, **options):
            attempts.append(True)
            if len(attempts) == 1:
                with parent.edit() as state:
                    state["checkpoints"] += 1
            with original(run, observed, **options) as locked:
                yield locked
        with patch.object(dependency_commit, "commit_guard", once):
            verdict = runner.gate(child)
        self.assertEqual(verdict["verdict"], "accepted", verdict)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(runner.accepted_evidence_errors(child, verdict["digest"]), [])

    def test_generation_retry_keeps_original_deadline(self):
        fixture = self.fixture()
        parent, _ = fixture.passing()
        child = self.downstream(fixture, parent, runner.gate(parent), "child")
        original = dependency_commit.commit_guard
        clock, attempts = [0.0], []
        @contextmanager
        def expire(run, observed, **options):
            attempts.append(True)
            with parent.edit() as state:
                state["checkpoints"] += 1
            clock[0] = 2.0
            with original(run, observed, **options) as locked:
                yield locked
        with patch.object(runner, "remaining_budget_seconds", return_value=1), patch.object(runner.time, "monotonic", side_effect=lambda: clock[0]), patch.object(dependency_commit, "commit_guard", expire):
            with self.assertRaisesRegex(AcceptanceDeadlineExhausted, "GATE_DEADLINE_EXHAUSTED"):
                runner.gate(child)
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(child.state()["accepted"])

    def test_source_error_is_not_retried(self):
        fixture = self.fixture()
        parent, _ = fixture.passing()
        child = self.downstream(fixture, parent, runner.gate(parent), "child")
        (fixture.cwd / "main.py").write_text("source changed after accepted dependency\n")
        with patch.object(runner, "_gate_once", wraps=runner._gate_once) as attempts:
            with self.assertRaises(HarnessError):
                runner.gate(child)
        self.assertEqual(attempts.call_count, 1)
        self.assertIsNone(child.state()["accepted"])

    def test_evidence_integrity_failure_is_not_retried(self):
        fixture = self.fixture()
        parent, evidence = fixture.passing()
        child = self.downstream(fixture, parent, runner.gate(parent), "child")
        (parent.path / evidence["artifacts"][0]["path"]).write_text("tampered immutable evidence\n")
        with patch.object(runner, "_gate_once", wraps=runner._gate_once) as attempts:
            verdict = runner.gate(child)
        self.assertEqual(verdict["verdict"], "rejected")
        self.assertEqual(attempts.call_count, 1)
        self.assertIsNone(child.state()["accepted"])

    def test_status_separates_child_configuration_from_runtime_qualification(self):
        from test_execution_durability import ExecutionDurabilityTests
        fixture = self.fixture()
        supervisor, _ = ExecutionDurabilityTests().fixture(fixture.base)
        status = supervisor.status()
        self.assertEqual(status["native_children"], "configured_disabled")
        self.assertEqual(status["native_children_configuration"], "disabled_per_process")
        self.assertEqual(status["native_children_qualification"], "not_tested")
        self.assertIsNone(status["native_children_suppression_proof"])

    def test_byte_identical_store_replacement_is_not_a_retryable_generation(self):
        fixture = self.fixture()
        parent, _ = fixture.passing()
        child = self.downstream(fixture, parent, runner.gate(parent), "child")
        original = dependency_commit.commit_guard
        attempts = []
        @contextmanager
        def replace(run, observed, **options):
            attempts.append(True)
            archive = parent.path.with_name("parent-original")
            parent.path.rename(archive)
            shutil.copytree(archive, parent.path)
            with original(run, observed, **options) as locked:
                yield locked
        with patch.object(dependency_commit, "commit_guard", replace):
            with self.assertRaisesRegex(HarnessError, "physical store identity changed"):
                runner.gate(child)
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(child.state()["accepted"])

    def test_transitive_vector_and_post_commit_changes_are_stale(self):
        fixture = self.fixture()
        first, _ = fixture.passing()
        first_verdict = runner.gate(first)
        second = self.downstream(fixture, first, first_verdict, "second")
        second_verdict = runner.gate(second)
        third = self.downstream(fixture, second, second_verdict, "third")
        accepted = runner.gate(third)
        self.assertEqual(len(accepted["dependency_vector"]), 2)
        recorded = third.state()["accepted"]
        self.assertEqual(runner.accepted_evidence_errors(third, accepted["digest"]), [])
        with first.edit() as state:
            state["checkpoints"] += 1
        errors = runner.accepted_evidence_errors(third, accepted["digest"])
        self.assertIn("accepted dependency generation changed", errors)
        self.assertEqual(third.state()["accepted"], recorded)

    def test_opposite_user_order_uses_same_lock_order_without_deadlock(self):
        fixture = self.fixture()
        parent, _ = fixture.passing()
        verdict = runner.gate(parent)
        left = self.downstream(fixture, parent, verdict, "left")
        right = self.downstream(fixture, parent, verdict, "right")
        start = threading.Barrier(2)
        def commit(run):
            observed = dependency_commit.closure(run)
            start.wait(timeout=5)
            with dependency_commit.commit_guard(run, observed, timeout=5):
                return len(observed)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(commit, [right, left])), [2, 2])

    def test_bounded_closure_and_cycle_reject_before_locks(self):
        fixture = self.fixture()
        parent, _ = fixture.passing()
        verdict = runner.gate(parent)
        child = self.downstream(fixture, parent, verdict, "child")
        with patch.object(dependency_commit, "MAX_NODES", 1):
            with self.assertRaisesRegex(HarnessError, "node limit"):
                dependency_commit.closure(child)
        a = deepcopy(fixture.contract)
        b = deepcopy(fixture.contract)
        a.update(run_id="a", dependencies=[{"run_dir": str(fixture.base / "b"), "verdict_digest": "a" * 64}])
        b.update(run_id="b", dependencies=[{"run_dir": str(fixture.base / "a"), "verdict_digest": "b" * 64}])
        first = fixture.run_fixture(a, name="a")
        fixture.run_fixture(b, name="b")
        with self.assertRaisesRegex(HarnessError, "dependency cycle"):
            dependency_commit.closure(first)
