"""Reviewer slot wait must not bypass immutable tooling admission.

Run from package root: python -B /path/to/this/test_reviewer_tool_admission.py
Uses real prepare/revalidate and fixture MCP subprocesses, never a model client.
"""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import test_tooling_acceptance as fixtures
from agent_harness.core import HarnessError
from agent_harness.managed_review import review_candidate
from agent_harness.strict_yaml import dump, load


class ReviewerToolAdmissionTests(unittest.TestCase):
    def test_profile_drift_during_slot_wait_refuses_before_intent_and_invocation(self):
        fixture = fixtures.ToolingAcceptanceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        supervisor = fixture.create(profile=False)
        profile_ref = fixture.profile()
        supervisor.contract["review_execution"] = {"backend": "codex", "tool_profile": profile_ref}
        plan = load(fixture.root / "plan.yaml")
        supervisor.store.write("plans/review-admission.yaml", plan, "agent-plan.tooling.v1.schema.json")
        supervisor.state["plan_path"] = "plans/review-admission.yaml"
        schema = fixture.root / "review-schema.json"
        schema.write_text('{"type":"object"}\n')
        run = SimpleNamespace(attempt_dir=lambda: fixture.root / "attempt")
        observed = {"slot_completed": False}

        class Slot:
            value = {"lease_id": "review-admission-fixture-lease"}
            wait_seconds = 0

            def __enter__(self):
                # An operator tightens private policy while the reviewer waits.
                current = load(profile_ref["path"])
                current["servers"][0]["allowed_tools"] = ["search_symbol"]
                current["servers"][0]["read_only_tools"] = ["search_symbol"]
                dump(Path(profile_ref["path"]), current)
                return self

            def __exit__(self, *_):
                return False

            def complete(self, *_):
                observed["slot_completed"] = True

        class Registry:
            def __init__(self, *_args, **_kwargs):
                pass

            def acquire(self, *_args, **_kwargs):
                return Slot()

        class UnexpectedDispatch(Exception):
            pass

        receipts = supervisor.receipts()
        with ExitStack() as stack:
            replacements = {
                "_review_binding": lambda *_: None,
                "guard_invocations": lambda *_args, **_kwargs: None,
                "_readonly_context": lambda *_: ([], {"files": []}),
                "schema_file": lambda *_: schema,
                "compile_packet": lambda *_: "read-only review",
                "plan_reviewer_effort": lambda *_: "high",
                "preflight": lambda *_args, **_kwargs: None,
                "invocation_timeout": lambda *_: 60,
                "GlobalSlots": Registry,
            }
            for name, replacement in replacements.items():
                stack.enter_context(patch("agent_harness.managed_review." + name, replacement))
            stack.enter_context(patch.object(supervisor, "receipts", return_value=receipts))
            stack.enter_context(patch.object(supervisor, "_model_budget_ok", return_value=True))
            stack.enter_context(patch.object(supervisor, "plan_progress", return_value={}))
            intent_spy = stack.enter_context(patch.object(receipts, "intent", wraps=receipts.intent))
            invoke_spy = stack.enter_context(patch("agent_harness.managed_review.invoke", side_effect=UnexpectedDispatch))
            try:
                review_candidate(supervisor, run, {"digest": "a" * 64})
            except HarnessError as error:
                self.assertIn("EXACT_RESUME_DRIFT", str(error))
            except UnexpectedDispatch:
                pass
            self.assertFalse(invoke_spy.called, "Reviewer reached model invocation with a stale profile")
            self.assertFalse(intent_spy.called, "Admission refusal must precede immutable invocation intent")
            self.assertTrue(observed["slot_completed"], "Proven non-dispatch must release its slot")


if __name__ == "__main__":
    unittest.main(verbosity=2)
