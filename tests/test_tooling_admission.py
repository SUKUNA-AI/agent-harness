"""Tooling discovery preserves legacy durable planning-round accounting."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness import tooling_state
from agent_harness.strict_yaml import dump, load
import test_verified_flow as verified_fixtures


class ToolingAdmissionTests(unittest.TestCase):
    setUp = verified_fixtures.VerifiedFlowTests.setUp
    fixture = verified_fixtures.VerifiedFlowTests.fixture

    def assert_no_dispatch(self, supervisor, fake):
        self.assertFalse(fake.with_suffix(".calls").exists())
        self.assertFalse(any(row["record_type"] == "invocation_intent"
                             for row in supervisor.receipts().records()))

    def test_legacy_schema_rejection_reserves_original_round(self):
        supervisor, fake = self.fixture(plan_edit=lambda plan: plan["tasks"][0]["checks"].update(local=[]))
        result = supervisor.start()
        self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
        self.assertEqual(supervisor.state["schema_version"], 3)
        self.assertEqual(supervisor.state["plan_quality"]["rounds_used"], 1)
        self.assertEqual(supervisor.state["plan_quality"]["phase"], "blocked")
        self.assert_no_dispatch(supervisor, fake)

    def test_legacy_malformed_yaml_reserves_original_round(self):
        supervisor, fake = self.fixture()
        Path(supervisor.plan_input).write_text("tasks: [\n", encoding="utf-8")
        result = supervisor.start()
        self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
        self.assertEqual(supervisor.state["plan_quality"]["rounds_used"], 1)
        self.assert_no_dispatch(supervisor, fake)

    def test_legacy_shape_errors_reserve_round_without_tooling_upgrade(self):
        for value in ([], {"schema_version": 2}, {"tasks": 7}, {"tasks": [None]}):
            with self.subTest(value=value):
                supervisor, fake = self.fixture()
                dump(supervisor.plan_input, value)
                result = supervisor.start()
                self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
                self.assertEqual(supervisor.state["schema_version"], 3)
                self.assertEqual(supervisor.state["plan_quality"]["rounds_used"], 1)
                self.assert_no_dispatch(supervisor, fake)
                # A fixture owns one fixed run directory; the next subcase
                # receives an independent repository and journal.
                self.setUp()

    def test_explicit_tooling_keeps_strict_preplanning_reviewer_guard(self):
        supervisor, fake = self.fixture()
        plan = load(supervisor.plan_input)
        plan["tasks"][0]["execution"] = {"backend": "codex"}
        dump(supervisor.plan_input, plan)
        result = supervisor.start()
        self.assertEqual(result["status"], "BLOCKED_ENVIRONMENT", result)
        self.assertIn("before planning", result["reason"])
        self.assertEqual(supervisor.state["plan_quality"]["rounds_used"], 0)
        self.assert_no_dispatch(supervisor, fake)

    def test_raw_plan_discovery_is_shape_safe_and_detects_opt_in(self):
        for value in (None, True, [], {"tasks": 7}, {"tasks": [None, 3, "execution"]}):
            self.assertFalse(tooling_state.enabled({}, value))
        self.assertTrue(tooling_state.enabled({}, {"tooling_schema_version": 1}))
        self.assertTrue(tooling_state.enabled({}, {"tasks": [{"execution": {}}]}))


if __name__ == "__main__":
    unittest.main()
