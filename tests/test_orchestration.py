from copy import deepcopy
from datetime import datetime, timedelta, timezone
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, Run, digest, snapshot
from agent_harness.orchestration import brief, dispatch, next_wave, observe_capacity, repair_brief, validate_plan
from agent_harness.runner import gate, run_check


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "main.py").write_text("value = 1\n", encoding="utf-8")
        for name in ("owner-a", "owner-b", "owner-c"):
            (self.base / name).mkdir()
        self.contract = {"schema_version": 1, "run_id": "orchestration-test", "task_id": "task", "attempt_id": "a1", "workflow": "change", "process_level": "coordinated", "goal": "Bounded test", "cwd": str(self.source), "permissions": ["read", "write", "test"], "scope": {"write_paths": ["a/**", "b/**", "c/**"], "protected_paths": ["a/private/**"]}, "checks": [{"id": "unit", "kind": "command", "required": True, "argv": [sys.executable, "-I", "-B", "-c", "print('real execution')"], "timeout": 10}], "budget": {"max_attempts": 3, "native_cap": 6, "root_counts": True, "native_cap_source": "test fixture, not actual native runtime"}}
        self.plan = {"schema_version": 1, "plan_revision": 1, "contract_digest": digest(self.contract), "input_digest": snapshot(self.contract)["digest"], "nodes": [self.node("a"), self.node("b")]}
        self.observation = {"complete": True, "source": "fixture observation, not actual native receipt", "observed_at": datetime.now(timezone.utc).isoformat(), "cap": 6, "root_ref": "root", "threads": [{"native_ref": "root", "status": "running"}]}

    def node(self, key):
        return {"id": key, "role": "owner", "goal": "Deliver " + key, "cwd": str(self.base / ("owner-" + key)), "permissions": ["read", "write", "test"], "write_paths": [key + "/public/**"], "depends_on": [], "checks": ["unit"]}

    def refresh(self):
        self.plan["contract_digest"] = digest(self.contract)
        self.plan["input_digest"] = snapshot(self.contract)["digest"]

    def test_two_ready_owners_and_no_side_effects(self):
        before = snapshot(self.contract)
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual(["a", "b"], [x["node_id"] for x in result["selected"]])
        self.assertTrue(all(x["mode"] == "spawn" for x in result["selected"]))
        self.assertEqual(before, snapshot(self.contract))
        self.assertEqual(["main.py"], sorted(p.name for p in self.source.iterdir()))

    def test_third_owner_opt_in_and_target_bound(self):
        self.plan["nodes"].append(self.node("c"))
        self.assertEqual(2, len(next_wave(self.plan, self.contract, self.observation)["selected"]))
        self.plan["target_owners"] = 3
        self.assertEqual(3, len(next_wave(self.plan, self.contract, self.observation)["selected"]))
        self.plan["target_owners"] = 6
        with self.assertRaises(HarnessError):
            validate_plan(self.plan, self.contract)

    def test_cycle_rejected(self):
        self.plan["nodes"][0]["depends_on"] = ["b"]
        self.plan["nodes"][1]["depends_on"] = ["a"]
        with self.assertRaisesRegex(HarnessError, "cycle"):
            validate_plan(self.plan, self.contract)

    def test_unknown_dependency_and_duplicate_node_rejected(self):
        self.plan["nodes"][0]["depends_on"] = ["missing"]
        with self.assertRaisesRegex(HarnessError, "Unknown dependency"):
            validate_plan(self.plan, self.contract)
        self.plan["nodes"][0]["depends_on"] = []
        self.plan["nodes"].append(deepcopy(self.plan["nodes"][0]))
        with self.assertRaisesRegex(HarnessError, "Duplicate node"):
            validate_plan(self.plan, self.contract)

    def test_permission_and_protected_scope_rejected(self):
        self.plan["nodes"][0]["permissions"].append("external")
        with self.assertRaisesRegex(HarnessError, "permissions"):
            validate_plan(self.plan, self.contract)
        self.plan["nodes"][0]["permissions"].remove("external")
        self.plan["nodes"][0]["write_paths"] = ["a/private/file.py"]
        with self.assertRaisesRegex(HarnessError, "protected"):
            validate_plan(self.plan, self.contract)

    def test_read_only_intent_and_reviewer_cannot_write(self):
        for workflow in ("review", "diagnostics", "plan", "research"):
            self.contract["workflow"] = workflow
            self.refresh()
            with self.assertRaises(HarnessError):
                validate_plan(self.plan, self.contract)
        self.contract["workflow"] = "change"
        self.refresh()
        self.plan["nodes"][0]["role"] = "reviewer"
        with self.assertRaisesRegex(HarnessError, "Read-only"):
            validate_plan(self.plan, self.contract)

    def test_unsafe_and_outside_paths_rejected(self):
        for claim in ("../a", "C:/a", "/a", "a/.git/config", "b/../a", "outside/file", "a/*/file"):
            self.plan["nodes"][0]["write_paths"] = [claim]
            with self.subTest(claim=claim), self.assertRaises(HarnessError):
                validate_plan(self.plan, self.contract)

    def test_case_slash_overlap_serialized(self):
        self.plan["nodes"][1]["write_paths"] = ["A\\public\\child.py"]
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual(["a"], [x["node_id"] for x in result["selected"]])
        self.assertEqual("ownership_conflict", result["blocked"]["b"])

    def test_same_checkout_different_claims_serialized(self):
        self.plan["nodes"][1]["cwd"] = self.plan["nodes"][0]["cwd"]
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual("ownership_conflict", result["blocked"]["b"])

    def test_active_writer_conflict_fails_closed(self):
        for index, node in enumerate(self.plan["nodes"]):
            node.update(state="running", native_ref="owner" + str(index))
            self.observation["threads"].append({"native_ref": node["native_ref"], "status": "running"})
        self.plan["nodes"][1]["write_paths"] = ["a/public/file.py"]
        with self.assertRaisesRegex(HarnessError, "Conflicting active"):
            next_wave(self.plan, self.contract, self.observation)

    def test_idle_done_stopped_unavailable_and_descendant_count(self):
        self.observation["threads"] += [{"native_ref": "child" + str(i), "status": state, "parent_ref": "unregistered-owner"} for i, state in enumerate(("idle", "done", "stopped", "unavailable", "running"))]
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual(6, result["capacity"]["counted"])
        self.assertEqual([], result["selected"])
        self.assertEqual("capacity", result["fallback_reason"])
        self.plan["nodes"][0]["native_ref"] = "child0"
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual("followup", result["selected"][0]["mode"])
        self.assertEqual(6, result["capacity"]["counted"])

    def test_duplicate_ref_and_missing_root_rejected(self):
        self.observation["threads"].append(deepcopy(self.observation["threads"][0]))
        with self.assertRaisesRegex(HarnessError, "Duplicate native"):
            observe_capacity(self.observation, self.contract)
        self.observation["threads"] = []
        with self.assertRaisesRegex(HarnessError, "root"):
            observe_capacity(self.observation, self.contract)

    def test_closed_requires_backend_evidence(self):
        closed = {"native_ref": "closed-child", "status": "closed"}
        self.observation["threads"].append(closed)
        with self.assertRaisesRegex(HarnessError, "backend proof"):
            observe_capacity(self.observation, self.contract)
        closed.update(closed_confirmed=True, closure_evidence="fixture close receipt")
        self.assertEqual(1, observe_capacity(self.observation, self.contract)["counted"])

    def test_unregistered_running_descendant_limits_active_target(self):
        self.observation["threads"].append({"native_ref": "outside-plan", "status": "running"})
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual(1, len(result["selected"]))
        self.assertEqual("target_owners", result["blocked"]["b"])

    def test_unregistered_writer_checkout_blocks_dispatch(self):
        self.observation["threads"].append({"native_ref": "other", "status": "idle", "write_cwd": self.plan["nodes"][0]["cwd"]})
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual("ownership_conflict", result["blocked"]["a"])
        self.assertEqual("b", result["selected"][0]["node_id"])

    def test_root_cannot_be_lane_owner(self):
        self.plan["nodes"][0].update(state="running", native_ref="root")
        with self.assertRaisesRegex(HarnessError, "Root cannot"):
            next_wave(self.plan, self.contract, self.observation)

    def test_unknown_stale_or_larger_capacity_fails_closed(self):
        for field, value in (("cap", None), ("cap", 7), ("complete", False), ("observed_at", "2020-01-01T00:00:00Z"), ("observed_at", "2026-01-01T00:00:00")):
            observation = deepcopy(self.observation)
            observation[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(HarnessError):
                observe_capacity(observation, self.contract)

    def test_input_and_contract_changes_stale_plan(self):
        (self.source / "main.py").write_text("value = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "input digest"):
            validate_plan(self.plan, self.contract)
        self.refresh()
        self.contract["attempt_id"] = "a2"
        with self.assertRaisesRegex(HarnessError, "contract digest"):
            validate_plan(self.plan, self.contract)

    def test_ready_for_review_does_not_release_dependency(self):
        self.plan["nodes"][0]["state"] = "ready_for_review"
        self.plan["nodes"][1]["depends_on"] = ["a"]
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual("dependency_not_accepted_current", result["blocked"]["b"])

    def test_actual_accepted_dependency_then_stale(self):
        upstream = self.base / "upstream"
        upstream.mkdir()
        (upstream / "file.txt").write_text("upstream\n", encoding="utf-8")
        contract = deepcopy(self.contract)
        contract.update(cwd=str(upstream), run_id="upstream")
        contract["scope"] = {"write_paths": ["**"]}
        run = Run.create(self.base / "upstream-run", contract)
        run.freeze()
        self.assertEqual("passed", run_check(run, "unit")["status"])
        accepted = gate(run)
        self.assertEqual("accepted", accepted["verdict"])
        self.plan["nodes"][0].update(state="accepted", accepted_result={"run_dir": str(run.path), "verdict_digest": accepted["digest"]})
        self.plan["nodes"][1]["depends_on"] = ["a"]
        self.assertEqual("b", next_wave(self.plan, self.contract, self.observation)["selected"][0]["node_id"])
        (upstream / "file.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual("dependency_not_accepted_current", next_wave(self.plan, self.contract, self.observation)["blocked"]["b"])

    def test_duplicate_active_owner_and_unavailable_followup(self):
        for node in self.plan["nodes"]:
            node["native_ref"] = "same"
        with self.assertRaisesRegex(HarnessError, "Duplicate active"):
            validate_plan(self.plan, self.contract)
        self.plan["nodes"][1].pop("native_ref")
        self.observation["threads"].append({"native_ref": "same", "status": "unavailable"})
        with self.assertRaisesRegex(HarnessError, "unavailable"):
            next_wave(self.plan, self.contract, self.observation)

    def test_brief_deterministic_and_repair_same_owner_current(self):
        self.plan["nodes"][0]["native_ref"] = "owner-a"
        self.assertEqual(brief(self.plan, self.contract, "a"), brief(self.plan, self.contract, "a"))
        packet = {"node_id": "a", "native_ref": "owner-a", "contract_digest": self.plan["contract_digest"], "candidate_digest": self.plan["input_digest"], "attempt_id": "a1", "check_id": "unit", "status": "failed", "log": "Observed check failed: assertion mismatch", "expected": "Check succeeds on current candidate"}
        self.assertIn("safely update", repair_brief(self.plan, self.contract, "a", packet))
        for field, wrong in (("native_ref", "someone-else"), ("candidate_digest", "0" * 64), ("attempt_id", "old"), ("check_id", "missing")):
            bad = {**packet, field: wrong}
            with self.subTest(field=field), self.assertRaises(HarnessError):
                repair_brief(self.plan, self.contract, "a", bad)

    def test_cli_error_is_structured_and_read_only(self):
        plan_path, contract_path = self.base / "plan.json", self.base / "contract.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        contract_path.write_text(json.dumps(self.contract), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, dispatch(["validate", "--plan", str(plan_path), "--contract", str(contract_path)]))
        self.assertTrue(json.loads(output.getvalue())["valid"])
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(2, dispatch(["next", "--plan", str(plan_path), "--contract", str(contract_path)]))
        self.assertIn("native-snapshot", json.loads(errors.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
