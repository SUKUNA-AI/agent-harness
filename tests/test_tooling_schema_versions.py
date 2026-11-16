"""Tooling has its own schema axis; historical schema bytes and hashes stay frozen."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import digest
from agent_harness.scheduling import validate_plan
from agent_harness.strict_yaml import StrictYAMLError, _schema, dump, validate
from agent_harness.supervisor import Supervisor
from agent_harness.supervisor_store import SupervisorStore
from agent_harness.task_packets import schema_file
from agent_harness.work_contract import (AGENT_PLAN_TOOLING_SCHEMA, WORK_CONTRACT_TOOLING_SCHEMA,
    normalize, validate_execution_selection)

ROOT = Path(__file__).resolve().parents[1]
FROZEN = {
    "work-contract.schema.json": "ce757bbe21a3280d1aac158ad2d2e47028f83011af5e6917b589a78e4b8c6fec",
    "agent-plan.schema.json": "6b5c175cb043f4718518e8d78cc6dd928d2fb755b399692433e124c4d7abf11f",
    "agent-state.schema.json": "a7e87be8f3d55f340b5a076e33e649d118b583086aa15ef8a2c59d1e13569444",
    "work-state.schema.json": "76646cffdace5b880b9c1c9dded43cb282938756aaca61df9523f9cbd178392d",
    "work-state.v3.schema.json": "5d14b3e6b163f6d1edf46be3ff8651f8bea3395ee4bcf9bc56a7dfb0849ab584",
}


class ToolingSchemaVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "fixture.py").write_text("VALUE = 1\n")

    def raw_contract(self, version=1):
        result = {"schema_version": version, "task": {"id": "compat", "objective": "read fixture", "read_only": True},
            "acceptance": [{"id": "inspect", "kind": "command", "required": True, "argv": ["python", "-c", "pass"]}]}
        if version == 2:
            result.update(mode="managed", mission={"id": "mission", "generation": 1, "original_request": "read fixture",
                "global_constraints": [], "non_goals": [], "requirements": [{"id": "read", "text": "read fixture",
                "provenance": "user", "kind": "behavior", "required": True, "acceptance_ids": ["inspect"]}]},
                node_checks=[{"id": "local", "kind": "command", "required": True, "argv": ["python", "-c", "pass"]}])
        return result

    def plan(self, contract):
        result = {"schema_version": contract["schema_version"], "task_id": "compat", "tasks": [{"id": "read",
            "goal": "read fixture", "role": "owner", "read_only": True, "read_scope": ["**"], "write_scope": [],
            "depends_on": [], "acceptance_ids": ["inspect"], "outputs": [], "estimated_seconds": 1,
            "max_attempts": 1, "max_wall_seconds": 30, "risk": "low", "impact": 1, "verification": ["inspect"]}]}
        if contract["schema_version"] == 2:
            result.update(generation=1, mission_digest=digest(contract["mission"]))
            result["tasks"][0].update(requirement_ids=["read"], checks={"local": ["local"], "integration": ["local"]})
        return result

    def work_state(self, version=1):
        raw = self.raw_contract(version)
        contract_path = self.root / ("task-" + str(version) + ".yaml")
        dump(contract_path, raw)
        supervisor = Supervisor.create(contract_path, run_dir=self.root / ("run-" + str(version)), cwd=self.source,
            state_dir=self.root / "state")
        return deepcopy(supervisor.state)

    def agent_state(self, version):
        stamp = "2026-11-16T00:00:00+00:00"
        return {"schema_version": version, "id": "fixture", "role": "owner", "task_contract_digest": "a" * 64,
            "worktree": {"schema_version": 1, "id": "fixture", "path": str(self.source), "base_revision": "non-git",
                "branch": None, "role": "owner", "ownership_token": "fixture-history", "created_at": stamp},
            "thread_id": None, "process_identity": None, "status": "stopped", "started_at": stamp,
            "last_progress_at": stamp, "allowed_write_scope": [], "read_only": True, "deadline": stamp,
            "max_attempts": 1, "attempts_used": 0, "stdout_offset": 0, "stderr_offset": 0,
            "checkpoint": 0, "spawn_reason": "fixture historical agent"}

    def test_historical_schema_bytes_exactly_match_pretooling_revision(self):
        for name, expected in FROZEN.items():
            with self.subTest(schema=name):
                self.assertEqual(hashlib.sha256((ROOT / "schemas" / name).read_bytes()).hexdigest(), expected)

    def test_old_contract_normalization_digest_does_not_gain_tooling_defaults(self):
        contract = normalize(self.raw_contract())
        self.assertNotIn("tooling_schema_version", contract)
        self.assertNotIn("execution", contract)
        self.assertEqual(digest(contract), "d74068d7eec181dc0631ab352c6462a8e2498eb7fa05f01c714dea098fe8ae40")
        validate(contract, WORK_CONTRACT_TOOLING_SCHEMA)
        for version in (1, 2):
            contract = normalize(self.raw_contract(version))
            plan = self.plan(contract)
            old_digest = digest(plan)
            validate_plan(plan, contract)
            self.assertEqual(old_digest, digest(plan))
            self.assertNotIn("tooling_schema_version", plan)

    def test_explicit_tool_selection_has_separate_marker_and_strict_legacy_barrier(self):
        for version in (1, 2):
            raw = self.raw_contract(version)
            raw["execution"] = {"backend": "codex", "tool_profile": {"id": "baseline", "version": 1}}
            contract = normalize(raw)
            self.assertEqual(contract["schema_version"], version)
            self.assertEqual(contract["tooling_schema_version"], 1)
            validate(contract, WORK_CONTRACT_TOOLING_SCHEMA)
            with self.assertRaises(StrictYAMLError):
                validate(contract, "work-contract.schema.json")
            for marker in (True, 2):
                invalid = deepcopy(raw)
                invalid["tooling_schema_version"] = marker
                with self.assertRaises(StrictYAMLError):
                    normalize(invalid)
            invalid = deepcopy(raw)
            invalid["execution"]["escape"] = True
            with self.assertRaises(StrictYAMLError):
                normalize(invalid)
        raw = self.raw_contract()
        raw["review_execution"] = {"tool_profile": {"id": "reviewer-readonly", "version": 1}}
        self.assertEqual(normalize(raw)["tooling_schema_version"], 1)

    def test_selection_reference_paths_stay_absolute_and_closed(self):
        from agent_harness.core import HarnessError
        for value in ({"tool_profile": {"id": "baseline", "version": 1, "path": "relative.yaml"}},
                {"graph_index": {"path": "relative.yaml"}}):
            with self.assertRaises(HarnessError):
                validate_execution_selection(value)
        with self.assertRaises(StrictYAMLError):
            validate_execution_selection({"backend": "other"})

    def test_node_and_inherited_tooling_normalize_only_explicit_selection(self):
        for version in (1, 2):
            contract = normalize(self.raw_contract(version))
            plan = self.plan(contract)
            plan["tasks"][0]["execution"] = {"backend": "opencode"}
            validate_plan(plan, contract)
            self.assertEqual(plan["tooling_schema_version"], 1)
            validate(plan, AGENT_PLAN_TOOLING_SCHEMA)
            with self.assertRaises(StrictYAMLError):
                validate(plan, "agent-plan.schema.json")
            raw = self.raw_contract(version)
            raw["execution"] = {"backend": "codex"}
            inherited = normalize(raw)
            plan = self.plan(inherited)
            validate_plan(plan, inherited)
            self.assertEqual(plan["tooling_schema_version"], 1)

    def test_plan_selection_does_not_bypass_strict_task_and_marker_types(self):
        contract = normalize(self.raw_contract())
        for tasks in (None, 1, {}, [None]):
            value = self.plan(contract)
            value["tasks"] = tasks
            with self.assertRaises(StrictYAMLError):
                validate_plan(value, contract)
        value = self.plan(contract)
        value["tasks"][0]["execution"] = {"backend": "codex"}
        value["tooling_schema_version"] = True
        with self.assertRaises(StrictYAMLError):
            validate_plan(value, contract)

    def test_agent_v3_reader_preserves_old_closed_values_and_requires_nullable_fields(self):
        for version in (1, 2):
            agent = self.agent_state(version)
            validate(agent, "agent-state.schema.json")
            validate(agent, "agent-state.v3.schema.json")
            agent["tool_provenance"] = None
            with self.assertRaises(StrictYAMLError):
                validate(agent, "agent-state.v3.schema.json")
        agent = self.agent_state(3)
        with self.assertRaises(StrictYAMLError):
            validate(agent, "agent-state.v3.schema.json")
        agent.update(tool_provenance=None, tool_source_checkpoint=None)
        validate(agent, "agent-state.v3.schema.json")
        with self.assertRaises(StrictYAMLError):
            validate(agent, "agent-state.schema.json")
        agent["tool_provenance"] = {"path": "tooling/receipt.yaml", "digest": "a" * 64}
        agent["tool_source_checkpoint"] = "b" * 64
        validate(agent, "agent-state.v3.schema.json")

    def test_work_state_v4_reader_preserves_versions_and_requires_explicit_base(self):
        legacy = self.work_state()
        self.assertEqual(legacy["schema_version"], 2)
        for version in (1, 2):
            state = deepcopy(legacy)
            state["schema_version"] = version
            validate(state, "work-state.v4.schema.json")
            state["tool_source_checkpoint"] = None
            with self.assertRaises(StrictYAMLError):
                validate(state, "work-state.v4.schema.json")
        state = deepcopy(legacy)
        state.update(schema_version=4, tooling_schema_version=1, base_schema_version=2,
            tool_provenance=None, tool_source_checkpoint=None)
        validate(state, "work-state.v4.schema.json")
        for field in ("tooling_schema_version", "base_schema_version", "tool_provenance", "tool_source_checkpoint"):
            invalid = deepcopy(state)
            invalid.pop(field)
            with self.assertRaises(StrictYAMLError):
                validate(invalid, "work-state.v4.schema.json")
        with self.assertRaises(StrictYAMLError):
            validate(state, "work-state.v3.schema.json")
        quality = self.work_state(2)
        self.assertEqual(quality["schema_version"], 3)
        validate(quality, "work-state.v4.schema.json")
        state["plan_quality"] = quality["plan_quality"]
        with self.assertRaises(StrictYAMLError):
            validate(state, "work-state.v4.schema.json")
        state["base_schema_version"] = 3
        validate(state, "work-state.v4.schema.json")
        state.pop("plan_quality")
        with self.assertRaises(StrictYAMLError):
            validate(state, "work-state.v4.schema.json")

    def test_generated_plan_schema_has_tooling_only_for_an_explicit_context(self):
        supervisor = type("FixtureSupervisor", (), {})()
        supervisor.store = SupervisorStore(self.root / "packets")
        supervisor.contract = normalize(self.raw_contract())
        supervisor.state = {"run_id": "fixture", "contract_digest": digest(supervisor.contract)}
        baseline = json.loads(Path(schema_file(supervisor, "agent-plan.schema.json", 1)).read_text())
        self.assertNotIn("tooling_schema_version", baseline["properties"])
        self.assertNotIn("execution", baseline["properties"]["tasks"]["items"]["properties"])
        explicit = json.loads(Path(schema_file(supervisor, "agent-plan.schema.json", 1,
            plan={"tooling_schema_version": 1})).read_text())
        self.assertEqual(explicit["properties"]["tooling_schema_version"], {"const": 1})
        self.assertIn("execution", explicit["properties"]["tasks"]["items"]["properties"])
        supervisor.contract["tooling_schema_version"] = 1
        inherited = json.loads(Path(schema_file(supervisor, "agent-plan.schema.json", 1)).read_text())
        self.assertEqual(explicit, inherited)


if __name__ == "__main__":
    unittest.main()
