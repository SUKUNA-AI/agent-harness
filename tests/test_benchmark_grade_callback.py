"""Independent boundary evaluation is a single durable external operation."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src")]
from agent_harness.core import HarnessError
from agent_harness.strict_yaml import load
import test_benchmark_runner as fixtures


class GradeCallbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BenchmarkRunnerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.effects = []

    def grade(self, row, prepared, grade_input, *, deadline=None):
        self.effects.append(row["trial_id"])
        value = load(grade_input)
        return {"schema_version": 1, "record_type": "independent_grade",
            "manifest_digest": value["manifest_digest"], "trial_id": value["trial_id"],
            "evaluator_digest": value["evaluator_digest"],
            "outcome": "accepted" if (Path(prepared["cwd"]) / "answer.txt").read_text() == "42" else "failed"}

    def test_pinned_callback_records_grade_without_fallback_argv(self):
        self.fixture.adapter.independent_grade = self.grade
        runner = self.fixture.runner()
        with patch.object(self.fixture.adapter, "evaluator_argv", side_effect=AssertionError("fallback is a second evaluator")):
            result = runner.advance()
        self.assertEqual(result["completed_trials"], 1, result)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(sum(row["kind"] == "grade" for row in runner.records()), 1)

    def test_callback_effect_then_failure_is_not_replayed_or_given_argv_fallback(self):
        def interrupted(*args, **options):
            self.grade(*args, **options)
            raise HarnessError("BENCH_EVALUATOR_INTERRUPTED_AFTER_EFFECT")
        self.fixture.adapter.independent_grade = interrupted
        runner = self.fixture.runner()
        with patch.object(self.fixture.adapter, "evaluator_argv", side_effect=AssertionError("unsafe fallback")):
            first = runner.advance()
            second = runner.advance()
        self.assertEqual(first["reason_code"], "BENCH_EVALUATOR_INTERRUPTED_AFTER_EFFECT")
        self.assertEqual(second["reason_code"], "BENCH_UNKNOWN_GRADE_OUTCOME")
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(len(self.fixture.calls()), 1)
        self.assertEqual(second["usage"]["observed_tokens"], 4)

    def test_callback_cannot_change_pinned_evaluator_bytes_after_dispatch(self):
        def changed(*args, **options):
            value = self.grade(*args, **options)
            self.fixture.adapter.grader.write_text("changed after effect", encoding="utf-8")
            return value
        self.fixture.adapter.independent_grade = changed
        runner = self.fixture.runner()
        result = runner.advance()
        self.assertEqual(result["reason_code"], "BENCH_STALE_ASSET", result)
        self.assertEqual(len(self.effects), 1)
        self.assertFalse(any(row["kind"] == "grade" for row in runner.records()))

    def test_expiry_after_real_solve_preserves_usage_without_calling_fault_action(self):
        runner = self.fixture.runner()
        calls = []
        class Hook:
            def before_solve(self, **subject):
                calls.append("before")
            def after_solve(self, path):
                calls.append("after")
        runner.fault_controller = Hook()
        original = self.fixture.adapter.codex
        def factory(prepared, *, deadline=None):
            backend = original(prepared, deadline=deadline)
            run = backend.run
            def expire_after_run(*args, **options):
                result = run(*args, **options)
                runner._authorization_end = time.monotonic() - 1
                return result
            backend.run = expire_after_run
            return backend
        with patch.object(self.fixture.adapter, "codex", factory):
            result = runner.advance()
        self.assertEqual(result["reason_code"], "BENCH_DEADLINE_EXHAUSTED", result)
        self.assertEqual(calls, ["before"])
        reconciled = runner.advance()
        self.assertEqual(reconciled["usage"]["observed_tokens"], 4)
        self.assertTrue(any(row["kind"] == "solve" for row in runner.records()))


if __name__ == "__main__":
    unittest.main()
