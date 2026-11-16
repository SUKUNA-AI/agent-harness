"""Versioned tooling state with the real existing verified mission pipeline."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness import plan_quality
from agent_harness import state_migration
from agent_harness.core import digest
from agent_harness.strict_yaml import load
from agent_harness.supervisor import Supervisor
from test_tooling_acceptance import QualifiedFixtureCodex
import test_verified_flow as verified_fixtures


class ToolingVerifiedFlowTests(unittest.TestCase):
    setUp = verified_fixtures.VerifiedFlowTests.setUp
    fixture = verified_fixtures.VerifiedFlowTests.fixture

    def test_v2_tooling_reviewer_is_blocked_before_any_planning_model_slot(self):
        supervisor, fake = self.fixture(contract_edit=lambda c: c.update(execution={"backend": "codex"}))
        self.assertEqual(supervisor.state["schema_version"], 4)
        self.assertEqual(supervisor.state["base_schema_version"], 3)
        self.assertTrue(plan_quality.enabled(supervisor))
        result = supervisor.start()
        self.assertEqual(result["status"], "BLOCKED_ENVIRONMENT", result)
        self.assertIn("before planning", result["reason"])
        self.assertFalse(fake.with_suffix(".calls").exists())
        self.assertFalse(any(r["record_type"] == "invocation_intent" for r in supervisor.receipts().records()))

    def test_historical_migration_reader_rejects_state4_without_writes(self):
        supervisor, _ = self.fixture(contract_edit=lambda c: c.update(execution={"backend": "codex"}))
        paths = [supervisor.path / "state.yaml", supervisor.path / "journal/events.jsonl"]
        before = [path.read_bytes() for path in paths]
        self.assertEqual(plan_quality.STATE_SCHEMA, "work-state.v3.schema.json")
        with self.assertRaises((ValueError, RuntimeError)):
            state_migration.migrate(supervisor.path, expected_generation=supervisor.state["generation"], target_version=3)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_verified_tooling_keeps_quality_binding_and_frozen_plan_on_resume(self):
        supervisor, fake = self.fixture(contract_edit=lambda c: c.update(execution={"backend": "codex"}))
        supervisor.adapter = QualifiedFixtureCodex(supervisor.adapter.executable)
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(supervisor.state["schema_version"], 4)
        self.assertEqual(supervisor.state["base_schema_version"], 3)
        records = supervisor.receipts().records()
        assessment = next(r for r in records if r["record_type"] == "plan_quality_assessment")
        self.assertTrue(assessment["capability_receipt_ref"])
        self.assertEqual(plan_quality.evidence_errors(supervisor), [])
        plan_path = supervisor.path / supervisor.state["plan_path"]
        before = plan_path.read_bytes()
        calls = fake.with_suffix(".calls").read_bytes()
        resumed = Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
        self.assertEqual(resumed["status"], "ACCEPTED", resumed)
        self.assertEqual(plan_path.read_bytes(), before)
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)

    def test_plan_only_opt_in_is_journaled_without_detaching_state(self):
        def select(plan):
            plan["tasks"][0]["execution"] = {"backend": "codex"}
        supervisor, _ = self.fixture(plan_edit=select)
        self.assertEqual(supervisor.state["schema_version"], 3)
        supervisor.adapter = QualifiedFixtureCodex(supervisor.adapter.executable)
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(supervisor.state["schema_version"], 4)
        self.assertTrue(supervisor.state["integration_worktree"])
        self.assertEqual(set(supervisor.state["agents"]), {"left", "right"})
        history = [event["payload"]["state"] for event in supervisor.store.events() if event["payload"].get("kind") == "checkpoint"]
        self.assertTrue(any(state["schema_version"] == 3 for state in history))
        self.assertTrue(any(state["schema_version"] == 4 for state in history))
        plan = load(supervisor.path / supervisor.state["plan_path"])
        checksum = digest(plan)
        self.assertEqual(plan["tooling_schema_version"], 1)
        self.assertTrue(supervisor.status(verify=True)["evidence_current"])
        self.assertEqual(digest(load(supervisor.path / supervisor.state["plan_path"])), checksum)


if __name__ == "__main__":
    unittest.main()
