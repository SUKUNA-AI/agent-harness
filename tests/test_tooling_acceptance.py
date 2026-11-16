"""Supervisor integration of optional tools with actual CLI/MCP subprocesses.

Fixtures exercise the complete existing gate; they never qualify a live model
client. Live client readiness/permissions remain separate explicit evidence.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.codex_execution import CodexAdapter
from agent_harness.core import HarnessError, digest, identity_alive
from agent_harness.execution import prepare, selection
from agent_harness.opencode_execution import OpenCodeAdapter
from agent_harness.strict_yaml import dump, load
from agent_harness.supervisor import Supervisor
from agent_harness.work_contract import normalize
from managed_process_fixture import BoundFixtureCodexAdapter
from test_managed_scheduling import node
from test_opencode_execution import FixtureOpenCodeAdapter

FIXTURES = Path(__file__).parent / "tooling"


class QualifiedFixtureCodex(BoundFixtureCodexAdapter):
    def capabilities(self, **options):
        return {**super().capabilities(**options), "native_children_disabled": True,
                "effective_tool_catalog": True, "reviewer_isolation_qualified": True}

    def run(self, prompt, **options):
        if json.loads(prompt.rsplit("\n", 1)[-1]).get("goal") == "pause" and not options.get("session_id"):
            options["timeout"] = .3
        return super().run(prompt, **options)


class QualifiedFixtureOpenCode(BoundFixtureCodexAdapter, FixtureOpenCodeAdapter):
    def capabilities(self, **options):
        return {**super().capabilities(**options), "effective_tool_catalog": True}


class ToolingAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex-home")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("left", "right"):
            (self.source / (name + ".py")).write_text("VALUE = 0\ndef shared_symbol(): return '" + name + "'\n")
        for args in (("init",), ("add", "."), ("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "fixture")):
            subprocess.run(["git", *args], cwd=self.source, check=True, capture_output=True)
        self.fake = self.root / "fake_client.py"
        shutil.copyfile(FIXTURES / "fake_managed_client.py", self.fake)
        self.codex = QualifiedFixtureCodex((sys.executable, str(self.fake)))
        self.opencode = QualifiedFixtureOpenCode((sys.executable, str(self.fake)))

    def profile(self, name="left", mode="ready"):
        path = self.root / (name + "-profile.yaml")
        dump(path, {"schema_version": 1, "id": "fixture-tools", "version": 1,
            "servers": [{"id": "fixture", "transport": "stdio", "required": True,
                "command": [sys.executable, str(FIXTURES / "fake_mcp.py"), mode, "{worktree}", name + ".py"],
                "allowed_tools": ["search_symbol", "read_file"], "read_only_tools": ["search_symbol", "read_file"],
                "forbidden_tools": ["write_file", "execute_command"], "required_tools": ["search_symbol"],
                "startup_timeout": .3, "tool_timeout": .3}]})
        return {"id": "fixture-tools", "version": 1, "path": str(path)}

    def create(self, *, backends=("codex", "codex"), profile=True, mode="ready", fail_check=False, pause=False):
        contract = {"schema_version": 1, "task": {"id": "managed", "objective": "managed",
            "allowed_write_scopes": ["left.py", "right.py"]}, "mode": "managed",
            "budgets": {"max_attempts": 1 if fail_check else 3, "max_concurrent_managed_agents": 2,
                        "max_wall_seconds": 180},
            "acceptance": [{"id": "combined", "kind": "command", "required": True,
                "argv": [sys.executable, "-c", "from pathlib import Path; assert " + ("False" if fail_check else
                    "all('VALUE = 42' in Path(n+'.py').read_text() for n in ('left','right'))")], "timeout": 10}]}
        tasks = [node("left"), node("right")]
        for task, backend in zip(tasks, backends):
            if profile or backend != "codex":
                task["execution"] = {"backend": backend,
                    "tool_profile": self.profile(task["id"], mode) if profile else {"id": "baseline", "version": 1}}
            if fail_check:
                task["max_attempts"] = 1
        if pause:
            tasks[0]["goal"] = "pause"
            tasks[1]["depends_on"] = ["left"]
        task_path, plan_path = self.root / "task.yaml", self.root / "plan.yaml"
        dump(task_path, contract)
        dump(plan_path, {"schema_version": 1, "task_id": "managed", "tasks": tasks})
        supervisor = Supervisor.create(task_path, run_dir=self.root / "private" / "run", cwd=self.source,
            state_dir=self.root / "state", adapter={"codex": self.codex, "opencode": self.opencode})
        supervisor.plan_input = str(plan_path)
        return supervisor

    def calls(self):
        return [json.loads(line) for line in self.fake.with_suffix(".calls").read_text().splitlines()]

    def test_A1_two_managed_worktrees_real_mcp_processes_receipts_scope_and_gate(self):
        supervisor = self.create()
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        agents = [supervisor.store.read("agents/" + name + ".yaml") for name in ("left", "right")]
        self.assertNotEqual(agents[0]["worktree"]["path"], agents[1]["worktree"]["path"])
        self.assertEqual(agents[0]["worktree"]["base_revision"], agents[1]["worktree"]["base_revision"])
        self.assertNotEqual(agents[0]["thread_id"], agents[1]["thread_id"])
        mcp = []
        for path in (supervisor.path / "codex").glob("*/*.jsonl"):
            for line in path.read_text().splitlines():
                event = json.loads(line)
                if event.get("item", {}).get("type") == "mcp_tool_call":
                    mcp.append(json.loads(event["item"]["result"]["content"][0]["text"]))
        self.assertEqual(len(mcp), 2)
        self.assertNotEqual(mcp[0]["pid"], mcp[1]["pid"])
        self.assertEqual({r["cwd"] for r in mcp}, {a["worktree"]["path"] for a in agents})
        self.assertEqual({r["project"] for r in mcp}, {r["cwd"] for r in mcp})
        self.assertTrue(any("return 'left'" in r["content"] for r in mcp))
        self.assertTrue(any("return 'right'" in r["content"] for r in mcp))
        self.assertTrue(supervisor.status(verify=True)["evidence_current"])
        self.assertEqual(len(result["tooling"]["agents"]), 2)
        self.assertEqual({r["profile_id"] for r in result["tooling"]["agents"]}, {"fixture-tools"})
        # Loss of tooling evidence invalidates an otherwise successful code gate.
        (supervisor.path / agents[0]["tool_provenance"]["path"]).unlink()
        self.assertEqual(supervisor.status(verify=True)["status"], "STALE")

    def test_A5_mandatory_faults_block_before_model_slot_and_no_fallback(self):
        for mode in ("startup-failure", "missing-tool", "timeout", "disconnect"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as trial:
                # Each fault has its own store; preserve the clean source Git.
                supervisor = self.create(mode=mode)
                result = supervisor.start()
                self.assertIn(result["status"], {"BLOCKED_ENVIRONMENT", "BLOCKED_AMBIGUOUS"}, result)
                self.assertFalse(self.fake.with_suffix(".calls").exists())
                self.assertFalse(any(r["record_type"] == "invocation_intent" for r in supervisor.receipts().records()))
                shutil.rmtree(self.root / "private")

    def test_A6_old_contract_without_mcp_or_opencode_keeps_codex_flow(self):
        supervisor = self.create(profile=False)
        before = deepcopy(supervisor.contract)
        self.assertNotIn("execution", before)
        self.assertEqual(digest(before), digest(normalize(before)))
        with patch.object(OpenCodeAdapter, "__init__", side_effect=AssertionError("must not construct optional backend")):
            result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertFalse((supervisor.path / "tooling").exists())
        self.assertTrue(all(row["backend"] == "codex" for row in self.calls()))

    def test_A7_mcp_success_does_not_replace_independent_failed_acceptance(self):
        supervisor = self.create(fail_check=True)
        result = supervisor.start()
        self.assertEqual(result["status"], "FAILED_TERMINAL", result)
        self.assertEqual(result["required_passed"], 0)
        records = supervisor.receipts().records()
        self.assertTrue(any(r["record_type"] == "integration" for r in records))
        self.assertFalse(any(r["record_type"] == "review" and r["payload"]["verdict"] == "approved" for r in records))

    def test_mixed_and_two_opencode_workers_use_shared_scheduler_and_gate(self):
        for backends in (("codex", "opencode"), ("opencode", "opencode")):
            with self.subTest(backends=backends):
                supervisor = self.create(backends=backends)
                result = supervisor.start()
                self.assertEqual(result["status"], "ACCEPTED", result)
                calls = self.calls()
                self.assertEqual({r["backend"] for r in calls if Path(r["cwd"]).name.startswith(("left-", "right-"))}, set(backends))
                self.assertTrue(all(not identity_alive(row) for row in self.codex.bound_children + self.opencode.bound_children))
                shutil.rmtree(self.root / "private")
                for path in self.root.glob("fake_client.session-*.json"):
                    path.unlink()
                self.fake.with_suffix(".calls").unlink()

    def test_A4_interrupt_supervisor_restart_and_explicit_exact_session_resume(self):
        supervisor = self.create(pause=True)
        result = supervisor.start()
        self.assertEqual(result["status"], "TIMED_OUT", result)
        original = supervisor.store.read("agents/left.yaml")
        self.assertTrue(original["thread_id"])
        old_receipts = {r["record_id"] for r in supervisor.receipts().records()}
        recovered = Supervisor(supervisor.path, state_dir=self.root / "state", adapter={"codex": self.codex})
        result = recovered.resume(new_attempt="Resume exact stopped leaf after controlled interruption")
        self.assertEqual(result["status"], "ACCEPTED", result)
        current = recovered.store.read("agents/left.yaml")
        self.assertEqual(current["thread_id"], original["thread_id"])
        self.assertEqual(current["worktree"], original["worktree"])
        self.assertTrue(old_receipts <= {r["record_id"] for r in recovered.receipts().records()})
        calls = [r for r in self.calls() if r["session"] == original["thread_id"]]
        self.assertEqual([r["resume"] for r in calls], [False, True])
        self.assertFalse(any(identity_alive(row) for row in self.codex.bound_children))

    def test_A4_profile_and_source_drift_refuse_resume_before_new_process(self):
        supervisor = self.create(pause=True)
        self.assertEqual(supervisor.start()["status"], "TIMED_OUT")
        calls = self.calls()
        profile_path = self.root / "left-profile.yaml"
        profile = load(profile_path)
        profile["servers"][0]["tool_timeout"] = 2
        dump(profile_path, profile)
        recovered = Supervisor(supervisor.path, state_dir=self.root / "state", adapter={"codex": self.codex})
        result = recovered.resume(new_attempt="Probe profile drift; no new session authorized")
        self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
        self.assertIn("DRIFT", result["reason"])
        self.assertEqual(self.calls(), calls)

    def test_A3_real_codex_reviewer_profile_fails_closed_before_any_model(self):
        supervisor = self.create(profile=False)
        supervisor.load()
        supervisor.adapter = CodexAdapter((sys.executable, str(self.fake)))
        with self.assertRaisesRegex(HarnessError, "reviewer.*isolation"):
            prepare(supervisor, {"tool_profile": {"id": "reviewer-readonly", "version": 1}},
                root=self.source, role="reviewer", node_id="review", attempt_id="review-1")
        self.assertFalse(self.fake.with_suffix(".calls").exists())

    def test_new_profile_run_cannot_bypass_reviewer_guard_by_omitting_review_execution(self):
        supervisor = self.create()
        supervisor.adapter = CodexAdapter((sys.executable, str(self.fake)))
        # Suppression proof is independent of reviewer/tool isolation.
        with patch.object(supervisor.adapter, "capabilities", return_value={
                "status": "observed", "version": "fixture", "native_children_disabled": True}):
            result = supervisor.start()
        self.assertEqual(result["status"], "BLOCKED_ENVIRONMENT", result)
        self.assertIn("final-reviewer", result["reason"])
        self.assertFalse(self.fake.with_suffix(".calls").exists())

    def test_missing_source_checkpoint_and_source_edit_refuse_exact_resume(self):
        supervisor = self.create(pause=True)
        self.assertEqual(supervisor.start()["status"], "TIMED_OUT")
        calls = self.calls()
        agent = supervisor.store.read("agents/left.yaml")
        agent["tool_source_checkpoint"] = None
        supervisor.store.write("agents/left.yaml", agent, "agent-state.v3.schema.json")
        (Path(agent["worktree"]["path"]) / "left.py").write_text("VALUE = -1\n")
        recovered = Supervisor(supervisor.path, state_dir=self.root / "state", adapter={"codex": self.codex})
        result = recovered.resume(new_attempt="Do not infer a missing source checkpoint")
        self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
        self.assertIn("checkpoint missing", result["reason"])
        self.assertEqual(self.calls(), calls)

    def test_required_mcp_version_or_script_drift_refuses_exact_resume(self):
        supervisor = self.create(pause=True)
        local_server = self.root / "server.py"
        shutil.copyfile(FIXTURES / "fake_mcp.py", local_server)
        profile_path = self.root / "left-profile.yaml"
        profile = load(profile_path)
        profile["servers"][0]["command"][1] = str(local_server)
        dump(profile_path, profile)
        self.assertEqual(supervisor.start()["status"], "TIMED_OUT")
        calls = self.calls()
        local_server.write_text(local_server.read_text().replace('"fixture-1"', '"fixture-2"'))
        recovered = Supervisor(supervisor.path, state_dir=self.root / "state", adapter={"codex": self.codex})
        result = recovered.resume(new_attempt="Required MCP identity must remain exact")
        self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
        self.assertIn("DRIFT", result["reason"])
        self.assertEqual(self.calls(), calls)

    def test_mandatory_runtime_tool_error_rejects_completed_model_output(self):
        supervisor = self.create(mode="tool-error", fail_check=True)
        result = supervisor.start()
        self.assertNotEqual(result["status"], "ACCEPTED", result)
        self.assertTrue(any(r["record_type"] == "completion" and r["payload"]["outcome"] == "failed"
                            for r in supervisor.receipts().records()))
        self.assertFalse(any(r["record_type"] == "integration" for r in supervisor.receipts().records()))

    def test_mandatory_result_must_be_observed_in_the_actual_execution_events(self):
        supervisor = self.create(fail_check=True)
        with patch.dict(os.environ, {"HARNESS_FIXTURE_OMIT_MCP": "1"}):
            result = supervisor.start()
        self.assertNotEqual(result["status"], "ACCEPTED", result)
        self.assertTrue(any(r["record_type"] == "completion" and r["payload"]["outcome"] == "failed"
                            for r in supervisor.receipts().records()))
        self.assertFalse(any(r["record_type"] == "integration" for r in supervisor.receipts().records()))

    def test_tool_receipt_cannot_be_reused_for_another_invocation_subject(self):
        supervisor = self.create()
        _, ref = prepare(supervisor, {"tool_profile": self.profile()}, root=self.source,
            role="owner", node_id="left", attempt_id="left-1")
        for node_id, attempt_id, checkout in (("other", "left-1", self.source),
                ("left", "other-attempt", self.source), ("left", "left-1", self.root)):
            with self.subTest(node_id=node_id, attempt_id=attempt_id, checkout=checkout):
                with self.assertRaisesRegex(HarnessError, "provenance subject"):
                    supervisor.receipts().intent(node_id, attempt_id, checkout=checkout,
                        owner="fixture", tool_provenance=ref)
        self.assertFalse(any(r["record_type"] == "invocation_intent" for r in supervisor.receipts().records()))


if __name__ == "__main__":
    unittest.main()
