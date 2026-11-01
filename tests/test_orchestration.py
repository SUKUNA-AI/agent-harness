from copy import deepcopy
from datetime import datetime, timedelta, timezone
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, Run, digest, file_hash, snapshot
from agent_harness.orchestration import _checkout, _conflict, _parent_binding, brief, dispatch, next_wave, observe_capacity, repair_brief, validate_plan
from agent_harness.runner import accepted_evidence_errors, gate, run_check


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
        self.plan = {"schema_version": 1, "plan_revision": 1, "contract_digest": digest(self.contract), "input_digest": snapshot(self.contract)["digest"], "common_context": [self.context_ref()], "nodes": [self.node("a"), self.node("b")]}
        self.observation = {"complete": True, "source": "fixture observation, not actual native receipt", "observed_at": datetime.now(timezone.utc).isoformat(), "cap": 6, "root_ref": "root", "threads": [{"native_ref": "root", "status": "running"}]}

    def node(self, key):
        return {"id": key, "role": "owner", "goal": "Deliver " + key, "cwd": str(self.base / ("owner-" + key)), "permissions": ["read", "write", "test"], "write_paths": [key + "/public/**"], "depends_on": [], "checks": ["unit"], "read_sections": [{**self.context_ref(), "sections": ["value definition"]}], "outputs": [{"path": str(self.base / ("owner-" + key) / key / "public/out.json"), "description": "Concrete verified output for " + key}]}

    def context_ref(self):
        return {"path": str(self.source / "main.py"), "sha256": file_hash(self.source / "main.py")}

    def refresh(self):
        self.plan["contract_digest"] = digest(self.contract)
        self.plan["input_digest"] = snapshot(self.contract)["digest"]
        self.plan["common_context"] = [self.context_ref()]
        for node in self.plan["nodes"]:
            node["read_sections"] = [{**self.context_ref(), "sections": ["value definition"]}]

    def git(self, cwd, *args):
        return subprocess.run(["git", "-c", "init.templateDir=", "-c", "core.hooksPath=" + str(self.base / "no-hooks"), "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", "-c", "user.name=Harness Test", "-c", "user.email=harness-test@example.invalid", "-C", str(cwd), *args], check=True, capture_output=True, text=True)

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

    def test_one_owner_target_serializes_useful_work_without_extra_agents(self):
        self.plan["target_owners"] = 1
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual(["a"], [x["node_id"] for x in result["selected"]])
        self.assertEqual("target_owners", result["blocked"]["b"])
        self.plan["nodes"][0].update(state="running", native_ref="owner-a")
        self.observation["threads"].append({"native_ref": "owner-a", "status": "running"})
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual([], result["selected"])
        self.assertEqual("target_owners", result["blocked"]["b"])
        for invalid in (0, True, "1"):
            self.plan["target_owners"] = invalid
            with self.subTest(target=invalid), self.assertRaises(HarnessError):
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
        self.plan["nodes"][1]["outputs"][0]["path"] = str(Path(self.plan["nodes"][0]["cwd"]) / "b/public/out.json")
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

    def test_child_only_capacity_uses_all_child_slots_and_reuses_idle_refs(self):
        self.contract["budget"].update(root_counts=False, native_cap=2)
        self.observation["cap"] = 2
        self.refresh()
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual(["a", "b"], [x["node_id"] for x in result["selected"]])
        self.assertEqual({"cap": 2, "root_counts": False, "counted": 0, "remaining": 2}, result["capacity"])
        self.observation["threads"] += [{"native_ref": "child-a", "status": "idle"}, {"native_ref": "child-b", "status": "done"}]
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual([], result["selected"])
        self.assertEqual(2, result["capacity"]["counted"])
        self.plan["nodes"][0]["native_ref"] = "child-a"
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual("followup", result["selected"][0]["mode"])
        self.observation["threads"].append({"native_ref": "too-many", "status": "stopped"})
        with self.assertRaisesRegex(HarnessError, "capacity exceeded"):
            observe_capacity(self.observation, self.contract)

    def test_inventory_capacity_matches_core_ledger_for_both_semantics(self):
        for root_counts in (False, True):
            with self.subTest(root_counts=root_counts):
                contract = deepcopy(self.contract)
                contract["budget"].update(root_counts=root_counts, native_cap=3)
                run = Run.create(self.base / ("ledger-total" if root_counts else "ledger-children"), contract)
                observation = deepcopy(self.observation)
                observation["cap"] = 3
                for key, status in (("a", "idle"), ("b", "done")):
                    record = {"owner_id": key, "lane_id": key, "native_ref": key, "status": status, "observation": "fixture inventory"}
                    run.record_owner(record)
                    observation["threads"].append({"native_ref": key, "status": status})
                measured = observe_capacity(observation, contract)
                ledger = run.capacity()
                for key in ("cap", "root_counts", "counted", "remaining"):
                    self.assertEqual(ledger[key], measured[key])

    def test_capacity_semantics_require_an_explicit_boolean(self):
        for invalid in (None, 0, 1, "false", [], {}):
            self.contract["budget"]["root_counts"] = invalid
            with self.subTest(root_counts=invalid), self.assertRaisesRegex(HarnessError, "root_counts"):
                observe_capacity(self.observation, self.contract)
        self.contract["budget"].pop("root_counts")
        with self.assertRaisesRegex(HarnessError, "root_counts"):
            observe_capacity(self.observation, self.contract)

    def test_child_only_inventory_still_requires_and_protects_root(self):
        self.contract["budget"]["root_counts"] = False
        self.refresh()
        root = self.observation["threads"][0]
        root["write_cwd"] = self.plan["nodes"][0]["cwd"]
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual("ownership_conflict", result["blocked"]["a"])
        self.assertEqual(0, result["capacity"]["counted"])
        self.observation["threads"] = []
        with self.assertRaisesRegex(HarnessError, "root"):
            observe_capacity(self.observation, self.contract)

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

    def accepted_upstream(self, binding=None):
        upstream = self.base / "upstream"
        upstream.mkdir()
        (upstream / "file.txt").write_text("upstream\n", encoding="utf-8")
        contract = deepcopy(self.contract)
        contract.update(cwd=str(upstream), run_id="upstream", lane_id="a", orchestration_binding=binding or _parent_binding(self.plan["nodes"][0], self.plan, self.contract))
        contract["scope"] = {"write_paths": ["**"]}
        run = Run.create(self.base / "upstream-run", contract)
        run.freeze()
        self.assertEqual("passed", run_check(run, "unit")["status"])
        accepted = gate(run)
        self.assertEqual("accepted", accepted["verdict"])
        self.plan["nodes"][0].update(state="accepted", accepted_result={"run_dir": str(run.path), "verdict_digest": accepted["digest"]}, expected_child={"run_id": contract["run_id"], "task_id": contract["task_id"], "attempt_id": contract["attempt_id"], "contract_digest": digest(contract)})
        self.plan["nodes"][1]["depends_on"] = ["a"]
        return run, upstream

    def test_actual_accepted_dependency_then_stale(self):
        run, upstream = self.accepted_upstream()
        self.assertEqual("b", next_wave(self.plan, self.contract, self.observation)["selected"][0]["node_id"])
        folder = run.attempt_dir() / "checks/unit"
        result_path = folder / "result.json"
        original = result_path.read_bytes()
        changed = json.loads(original)
        changed.pop("digest")
        changed["elapsed_seconds"] = 999
        changed["digest"] = digest(changed)
        result_path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertEqual("dependency_not_accepted_current", next_wave(self.plan, self.contract, self.observation)["blocked"]["b"])
        result_path.write_bytes(original)
        (upstream / "file.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual("dependency_not_accepted_current", next_wave(self.plan, self.contract, self.observation)["blocked"]["b"])

    def test_gate_dependency_uses_same_readonly_integrity_and_cycle_guard(self):
        child, _ = self.accepted_upstream()
        accepted = child.state()["accepted"]
        contract = deepcopy(self.contract)
        contract["dependencies"] = [{"run_dir": str(child.path), "verdict_digest": accepted["verdict_digest"]}]
        parent = Run.create(self.base / "parent-run", contract)
        parent.freeze()
        self.assertEqual("passed", run_check(parent, "unit")["status"])
        self.assertEqual("accepted", gate(parent)["verdict"])
        next((child.attempt_dir() / "checks/unit").rglob("stdout.log")).write_text("tampered log", encoding="utf-8")
        self.assertEqual("rejected", gate(parent)["verdict"])
        self.assertEqual(["accepted dependency cycle"], accepted_evidence_errors(child, accepted["verdict_digest"], _ancestors=(str(child.path),)))
    def test_dependency_missing_tampered_logs_results_toolchain_read_only(self):
        run, _ = self.accepted_upstream()
        state_before = run.state_path.read_bytes()
        folder = run.attempt_dir() / "checks/unit"
        for path in (next(folder.rglob("stdout.log")), next(folder.rglob("stderr.log")), folder / "result.json", run.attempt_dir() / "toolchains.json"):
            original = path.read_bytes()
            for mutation in ("missing", "tampered"):
                with self.subTest(path=path.name, mutation=mutation):
                    if mutation == "missing":
                        path.unlink()
                    else:
                        path.write_bytes(original + b' altered')
                    result = next_wave(self.plan, self.contract, self.observation)
                    self.assertEqual("dependency_not_accepted_current", result["blocked"]["b"])
                    self.assertEqual(state_before, run.state_path.read_bytes())
                    path.write_bytes(original)
        self.assertEqual("b", next_wave(self.plan, self.contract, self.observation)["selected"][0]["node_id"])

    def test_dependency_expected_identity_and_current_parent_binding(self):
        run, _ = self.accepted_upstream()
        node = self.plan["nodes"][0]
        original = deepcopy(node["expected_child"])
        for key in ("run_id", "task_id", "attempt_id", "contract_digest"):
            node["expected_child"][key] = "f" * 64 if key == "contract_digest" else "foreign"
            self.assertEqual("dependency_not_accepted_current", next_wave(self.plan, self.contract, self.observation)["blocked"]["b"])
            node["expected_child"] = deepcopy(original)
        self.plan["plan_revision"] += 1
        self.assertEqual("dependency_not_accepted_current", next_wave(self.plan, self.contract, self.observation)["blocked"]["b"])
        self.plan["plan_revision"] -= 1
        self.contract["attempt_id"] = "a2"
        self.refresh()
        self.assertEqual("dependency_not_accepted_current", next_wave(self.plan, self.contract, self.observation)["blocked"]["b"])

    def test_accepted_foreign_node_binding_does_not_release(self):
        binding = _parent_binding(self.plan["nodes"][0], self.plan, self.contract)
        binding["node_id"] = "foreign"
        self.accepted_upstream(binding=binding)
        self.assertEqual("dependency_not_accepted_current", next_wave(self.plan, self.contract, self.observation)["blocked"]["b"])

    def test_nested_cwd_absolute_claim_and_all_inventory_guards(self):
        child = self.source / "src"
        child.mkdir()
        a, b = self.plan["nodes"]
        a.update(cwd=str(self.source), write_paths=["src/agent_harness/**"], outputs=[{"path": str(self.source / "out-a.json"), "description": "a output"}])
        b.update(cwd=str(child), write_paths=["agent_harness/**"], outputs=[{"path": str(child / "out-b.json"), "description": "b output"}])
        self.contract["scope"] = {"write_paths": ["**"]}
        self.refresh()
        self.assertTrue(_conflict(a, b))
        self.assertEqual("ownership_conflict", next_wave(self.plan, self.contract, self.observation)["blocked"]["b"])
        self.observation["threads"].append({"native_ref": "external", "status": "idle", "write_cwd": str(self.source)})
        self.assertEqual([], next_wave(self.plan, self.contract, self.observation)["selected"])
        self.observation["threads"].append({"native_ref": "nested", "status": "idle", "write_cwd": str(child)})
        with self.assertRaisesRegex(HarnessError, "checkout conflict"):
            observe_capacity(self.observation, self.contract)

    def test_sibling_cwds_in_one_actual_checkout_conflict(self):
        root = self.base / "owner-a"
        self.git(root, "init", "--quiet")
        (root / "one").mkdir()
        (root / "two").mkdir()
        left = {"cwd": str(root / "one"), "write_paths": ["left.py"]}
        right = {"cwd": str(root / "two"), "write_paths": ["right.py"]}
        self.assertTrue(_conflict(left, right))

    def test_empty_git_placeholder_does_not_merge_independent_directories(self):
        marker = self.base / ".git"
        marker.mkdir()
        self.assertEqual(self.base / "owner-a", _checkout(self.base / "owner-a"))
        result = next_wave(self.plan, self.contract, self.observation)
        self.assertEqual(["a", "b"], [x["node_id"] for x in result["selected"]])
        (marker / "objects").mkdir()
        (marker / "refs").mkdir()
        (marker / "HEAD").write_text("not a Git head\n", encoding="ascii")
        self.assertEqual(self.base / "owner-a", _checkout(self.base / "owner-a"))

    def test_separate_git_directory_pointer_detects_shared_checkout(self):
        root = self.base / "owner-a"
        self.git(root, "init", "--quiet", "--separate-git-dir", str(self.base / "git-metadata"))
        (root / ".git").write_text("gitdir: ../git-metadata\n", encoding="utf-8")
        (root / "one").mkdir()
        (root / "two").mkdir()
        self.assertEqual(root.resolve(), Path(self.git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve())
        self.assertEqual(root, _checkout(root / "one"))
        self.assertTrue(_conflict({"cwd": str(root / "one"), "write_paths": ["left.py"]}, {"cwd": str(root / "two"), "write_paths": ["right.py"]}))

    def test_linked_worktrees_keep_isolated_writers_and_detect_nested_cwds(self):
        root = self.base / "owner-a"
        self.git(root, "init", "--quiet")
        self.git(root, "commit", "--quiet", "--allow-empty", "-m", "Fixture")
        worktree = self.base / "linked-worktree"
        self.git(root, "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
        for name in ("one", "two"):
            (worktree / name).mkdir()
        self.assertEqual(worktree, _checkout(worktree / "one"))
        self.assertTrue(_conflict({"cwd": str(worktree / "one"), "write_paths": ["left.py"]}, {"cwd": str(worktree / "two"), "write_paths": ["right.py"]}))
        self.assertFalse(_conflict({"cwd": str(root), "write_paths": ["left.py"]}, {"cwd": str(worktree), "write_paths": ["right.py"]}))

    def test_windows_case_alias_checkout_and_original_symlink_spelling(self):
        a, b = deepcopy(self.plan["nodes"])
        a["write_paths"], b["write_paths"] = ["one.py"], ["two.py"]
        if os.name == "nt":
            b["cwd"] = a["cwd"].upper()
            self.assertTrue(_conflict(a, b))
        self.contract["scope"] = {"write_paths": ["**"]}
        self.refresh()
        self.plan["nodes"][0]["write_paths"] = ["Src/Link/**"]
        probed = []
        def probe(path):
            spelling = path.as_posix()
            probed.append(spelling)
            return spelling.endswith("/Src/Link")
        with patch.object(Path, "is_symlink", probe), self.assertRaisesRegex(HarnessError, "Symlink"):
            validate_plan(self.plan, self.contract)
        self.assertTrue(any(p.endswith("/Src/Link") for p in probed))

    def test_brief_context_reading_output_and_invalid_context(self):
        text = brief(self.plan, self.contract, "a")
        for value in (self.plan["common_context"][0]["path"], "value definition", self.plan["nodes"][0]["outputs"][0]["path"], "Concrete verified output for a"):
            self.assertIn(value.replace("\\", "\\\\"), text)
        self.plan["common_context"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(HarnessError, "hash is stale"):
            validate_plan(self.plan, self.contract)
        self.plan["common_context"] = [self.context_ref()]
        self.plan["nodes"][0]["read_sections"] = []
        with self.assertRaisesRegex(HarnessError, "read_sections"):
            validate_plan(self.plan, self.contract)

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
        packet = {"node_id": "a", "plan_revision": 1, "native_ref": "owner-a", "contract_digest": self.plan["contract_digest"], "candidate_digest": self.plan["input_digest"], "attempt_id": "a1", "check_id": "unit", "status": "failed", "log": "Observed check failed: assertion mismatch", "expected": "Check succeeds on current candidate"}
        self.assertIn("safely update", repair_brief(self.plan, self.contract, "a", packet))
        repair = repair_brief(self.plan, self.contract, "a", packet)
        self.assertIn("Common context:", repair)
        self.assertIn("value definition", repair)
        self.assertIn("Concrete verified output for a", repair)
        for field, wrong in (("native_ref", "someone-else"), ("candidate_digest", "0" * 64), ("attempt_id", "old"), ("plan_revision", 99), ("check_id", "missing")):
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
