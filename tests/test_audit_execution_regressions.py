"""A1–A5: real contained fake sessions and negative acceptance/recovery checks."""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.agent_lifecycle import GlobalSlots, Worktrees
from agent_harness.codex_execution import CodexAdapter
from agent_harness.core import HarnessError, identity_alive, process_identity
from agent_harness.strict_yaml import dump
from agent_harness.supervisor import Supervisor
from agent_harness import telemetry


FAKE = r'''
import json,pathlib,sys,time
args=sys.argv[1:]
if '--version' in args: print('regression-fake 1.0'); raise SystemExit
if '--help' in args: print('exec resume SESSION_ID --json --output-schema'); raise SystemExit
def event(kind, **values): print(json.dumps(dict(type=kind, **values)), flush=True)
event('thread.started',thread_id='regression-session');event('turn.started')
ACTION
'''


class ExecutionAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "isolated-codex")})
        environment.start()
        self.addCleanup(environment.stop)

    def create(self, action, *, managed=False, cap=1, max_tokens=None,
               expected="after", attempts=3, ledger="off"):
        source = self.root / "source"
        source.mkdir()
        (source / "value.txt").write_text("before")
        if managed:
            for args in (["init"], ["add", "value.txt"], ["-c", "user.name=Fixture",
                         "-c", "user.email=fixture@localhost", "-c", "commit.gpgsign=false",
                         "-c", "core.hooksPath=" + str(self.root / "empty-hooks"), "commit", "-m", "fixture"]):
                subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
        budgets = {"max_attempts": attempts, "max_wall_seconds": 90,
                   "turn_timeout_seconds": 10, "max_concurrent_managed_agents": cap}
        if max_tokens is not None:
            budgets["max_tokens"] = max_tokens
        contract = {"schema_version": 1, "task": {"id": "regression", "objective": "Fixture",
            "allowed_write_scopes": ["value.txt"]}, "acceptance": [{"id": "value",
            "kind": "command", "required": True, "argv": [sys.executable, "-c",
                "from pathlib import Path; assert Path('value.txt').read_text() == " + repr(expected)],
            "timeout": 5}], "budgets": budgets, "ledger": ledger,
            "mode": "managed" if managed else "native"}
        task = self.root / "task.yaml"
        dump(task, contract)
        adapter = CodexAdapter((sys.executable, "-c", FAKE.replace("ACTION", action)))
        return Supervisor.create(task, run_dir=self.root / "private" / "run", cwd=source,
                                 state_dir=self.root / "state", adapter=adapter)

    def plan(self, supervisor):
        path = self.root / "plan.yaml"
        dump(path, {"schema_version": 1, "task_id": "regression", "tasks": [{
            "id": "implementation", "goal": "Fixture", "role": "owner", "read_only": False,
            "read_scope": ["**"], "write_scope": ["value.txt"], "depends_on": [],
            "acceptance_ids": ["value"], "outputs": ["value.txt"], "estimated_seconds": 10,
            "max_attempts": 1, "max_wall_seconds": 60, "risk": "low", "impact": 5,
            "verification": ["value"]}]})
        supervisor.plan_input = str(path)

    def test_a1_early_managed_recovery_checks_the_reported_candidate(self):
        class Interrupted(BaseException):
            pass
        action = ("if 'Task contract and failure packet:' not in args[-1]: pathlib.Path('value.txt').write_text('after')\n"
                  "event('turn.completed',usage={'input_tokens':1,'output_tokens':1})")
        supervisor = self.create(action, managed=True, expected="before", attempts=1)
        with patch.object(Worktrees, "create", side_effect=Interrupted):
            with self.assertRaises(Interrupted):
                supervisor.start()
        self.assertEqual(supervisor.state["status"], "PLANNING")
        self.assertIsNone(supervisor.state["core_run"])
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        result = recovered.resume()
        self.assertEqual(result["status"], "FAILED_TERMINAL", result)
        self.assertEqual(result["required_passed"], 0)
        self.assertEqual(Path(recovered._core().contract()["cwd"]), Path(result["cwd"]))
        self.assertNotEqual(result["cwd"], result["source_cwd"])
        self.assertEqual((Path(result["source_cwd"]) / "value.txt").read_text(), "before")
        self.assertEqual((Path(result["cwd"]) / "value.txt").read_text(), "after")
        check = subprocess.run(recovered.contract["acceptance"][0]["argv"], cwd=result["cwd"], capture_output=True)
        self.assertNotEqual(check.returncode, 0)

    def test_a1_status_rejects_a_core_bound_to_another_checkout(self):
        supervisor = self.create("pathlib.Path('value.txt').write_text('after');event('turn.completed',usage={'input_tokens':1,'output_tokens':1})")
        self.assertEqual(supervisor.start()["status"], "ACCEPTED")
        foreign = self.root / "other"
        foreign.mkdir()
        (foreign / "value.txt").write_text("before")
        supervisor.state["cwd"] = str(foreign)
        with self.assertRaisesRegex(HarnessError, "different candidate"):
            supervisor._core()
        result = supervisor.status(verify=True)
        self.assertEqual(result["status"], "STALE")
        self.assertFalse(result["evidence_current"])
        self.assertEqual(result["metrics"]["accepted_tasks"], 0)

    def test_a1_status_does_not_recreate_missing_worktree_ownership(self):
        supervisor = self.create("pathlib.Path('value.txt').write_text('after');event('turn.completed',usage={'input_tokens':1,'output_tokens':1})", managed=True)
        self.assertEqual(supervisor.start()["status"], "ACCEPTED")
        ownership = supervisor.path / "worktree-ownership"
        ownership.rename(supervisor.path / "retained-ownership")
        result = supervisor.status(verify=True)
        self.assertEqual(result["status"], "STALE")
        self.assertFalse(result["evidence_current"])
        self.assertFalse(ownership.exists())

    def test_a3_partial_usage_blocks_a_configured_budget(self):
        supervisor = self.create("pathlib.Path('value.txt').write_text('after');event('turn.completed',usage={})", max_tokens=1)
        result = supervisor.start()
        self.assertEqual(result["status"], "BLOCKED_ENVIRONMENT", result)
        self.assertEqual(result["required_passed"], 0)
        self.assertEqual(result["metrics"]["missing_usage_turns"], 1)
        self.assertEqual(result["metrics"]["reported_usage_turns"], 0)
        self.assertEqual(result["metrics"]["usage_status"], "unavailable")

    def test_a3_usage_requires_both_nonnegative_integer_counts(self):
        for usage in (None, {}, {"input_tokens": 1}, {"output_tokens": 2},
                      {"input_tokens": True, "output_tokens": 0},
                      {"input_tokens": -1, "output_tokens": 0},
                      {"input_tokens": 1, "output_tokens": "2"}):
            with self.subTest(usage=usage):
                metrics = telemetry.initial()
                telemetry.observe(metrics, {"usage": usage})
                self.assertEqual(metrics["missing_usage_turns"], 1)
                self.assertEqual(metrics["reported_usage_turns"], 0)
        metrics = telemetry.initial()
        telemetry.observe(metrics, {"usage": {"input_tokens": 0, "output_tokens": 0}})
        self.assertEqual(metrics["missing_usage_turns"], 0)
        self.assertEqual(metrics["reported_usage_turns"], 1)
        self.assertEqual((metrics["input_tokens"], metrics["output_tokens"]), (0, 0))

    def test_a3_legacy_aggregates_are_not_promoted_by_one_complete_turn(self):
        for old in ({"reported_usage_turns": 1}, {"model_turns": 1}, {"missing_usage_turns": 1},
                    {"input_tokens": 0, "output_tokens": 0}):
            with self.subTest(old=old):
                metrics = telemetry.initial()
                metrics.pop("usage_accounting_version")
                metrics.pop("model_invocations")
                metrics.update(old)
                telemetry.observe(metrics, {"usage": {"input_tokens": 1, "output_tokens": 1}})
                self.assertFalse(telemetry.budget_usage_known(metrics))
                self.assertNotIn("usage_accounting_version", metrics)
        metrics = telemetry.initial()
        metrics.pop("usage_accounting_version")
        telemetry.observe(metrics, {"usage": {"input_tokens": 0, "output_tokens": 0}})
        self.assertTrue(telemetry.budget_usage_known(metrics))

    def test_a3_legacy_acceptance_is_not_reaffirmed_with_unproven_budget(self):
        supervisor = self.create("pathlib.Path('value.txt').write_text('after');event('turn.completed',usage={'input_tokens':0,'output_tokens':0})", max_tokens=1)
        self.assertEqual(supervisor.start()["status"], "ACCEPTED")
        # A complete old aggregate is indistinguishable from one containing
        # an earlier empty usage object. Do not invent a historical receipt.
        supervisor.state["metrics"].pop("usage_accounting_version")
        supervisor._save("fixture legacy usage aggregate")
        result = supervisor.status(verify=True)
        self.assertEqual(result["status"], "STALE")
        self.assertFalse(result["evidence_current"])
        self.assertEqual(result["recorded_status"], "ACCEPTED")
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        self.assertEqual(recovered.resume()["status"], "STALE")
        self.assertEqual(recovered.state["status"], "ACCEPTED")

    def test_a4_repairable_model_failure_resumes_the_exact_session(self):
        supervisor = self.create("if 'resume' not in args: event('turn.failed',error={'message':'transient failure'}); raise SystemExit(1)\n"
            "assert args[args.index('resume')+1]=='regression-session'\n"
            "pathlib.Path('value.txt').write_text('after');event('turn.completed',usage={'input_tokens':1,'output_tokens':1})")
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["metrics"]["repair_attempts"], 1)
        self.assertEqual(len(supervisor.state["failure_history"]), 1)

    def test_a4_repeated_model_failure_stops_without_illegal_transition(self):
        supervisor = self.create("event('turn.failed',error={'message':'transient failure'});raise SystemExit(1)")
        result = supervisor.start()
        self.assertEqual(result["status"], "NO_PROGRESS", result)
        self.assertEqual(result["attempt"], 2)

    def test_a5_resolved_managed_environment_reenters_preflight(self):
        supervisor = self.create("pathlib.Path('value.txt').write_text('after');event('turn.completed',usage={'input_tokens':1,'output_tokens':1})", managed=True, cap=2)
        self.assertEqual(supervisor.start()["status"], "BLOCKED_ENVIRONMENT")
        supervisor.adapter.capabilities = lambda: {"status": "observed", "native_children_disabled": True}
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        self.plan(recovered)
        result = recovered.resume()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertTrue(result["evidence_current"])
        self.assertEqual((Path(result["source_cwd"]) / "value.txt").read_text(), "before")

    def test_a5_unresolved_environment_does_not_spawn_on_resume(self):
        supervisor = self.create("raise AssertionError('must not spawn')", managed=True, cap=2)
        self.assertEqual(supervisor.start()["status"], "BLOCKED_ENVIRONMENT")
        result = Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
        self.assertEqual(result["status"], "BLOCKED_ENVIRONMENT")
        self.assertEqual(result["attempt"], 0)
        self.assertIsNone(supervisor.state["core_run"])

    def test_a11_real_ledger_failure_preserves_stopped_process_checkpoint(self):
        supervisor = self.create("pathlib.Path('value.txt').write_text('after');event('turn.completed',usage={'input_tokens':1,'output_tokens':1})", ledger="inform")
        with patch.object(supervisor, "_ledger", side_effect=OSError("fixture disk failure")):
            result = supervisor.start()
        self.assertEqual(result["status"], "UNKNOWN_OUTCOME")
        restored = Supervisor(supervisor.path, adapter=supervisor.adapter)
        restored.load()
        self.assertEqual(restored.state["process_status"], "completed")
        self.assertIsNone(restored.state["process_identity"])
        self.assertEqual(restored.state["metrics"]["reported_usage_turns"], 1)
        self.assertEqual(result["required_passed"], 0)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group regression")
    def test_a2_checkpoint_failure_after_leader_exit_stops_descendant_writes(self):
        counter, child_pid = self.root / "writes", self.root / "child.pid"
        child = ("import pathlib,signal,sys,time\n"
                 "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                 "for n in range(200):\n pathlib.Path(sys.argv[1]).write_text(str(n));time.sleep(.02)\n")
        action = ("import subprocess\n"
                  "child=subprocess.Popen([sys.executable,'-c'," + repr(child) + "," + repr(str(counter)) + "],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                  "pathlib.Path(" + repr(str(child_pid)) + ").write_text(str(child.pid));time.sleep(.15)\n")
        supervisor = self.create(action)
        observed = []
        original = supervisor._on_process
        def interrupted(process):
            original(process)
            observed.append(process["identity"])
            deadline = time.monotonic() + 2
            while identity_alive(process["identity"]) and time.monotonic() < deadline:
                time.sleep(.01)
            raise OSError("checkpoint unavailable after leader exit")
        supervisor._on_process = interrupted
        try:
            result = supervisor.start()
            self.assertEqual(result["status"], "UNKNOWN_OUTCOME", result)
            self.assertTrue(counter.exists(), "descendant must actually write before fault injection")
            at_return = counter.read_bytes()
            time.sleep(.15)
            self.assertEqual(counter.read_bytes(), at_return, "descendant wrote after supervisor returned")
            self.assertFalse(identity_alive(observed[0]))
            descendant = process_identity(int(child_pid.read_text()))
            self.assertFalse(identity_alive(descendant))
            slot = GlobalSlots(supervisor.state_dir).inventory(supervisor.state["run_id"])[0]
            with GlobalSlots(supervisor.state_dir).acquire("next", "task", "owner", timeout=1) as replacement:
                if slot["unresolved"]:
                    self.assertNotEqual(replacement.name, "slot-" + str(slot["slot"]) + ".yaml")
                replacement.complete()
        finally:
            if observed:
                try:
                    os.killpg(observed[0]["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_a2_uncertain_dead_leader_slot_is_not_implicitly_reclaimed(self):
        registry = GlobalSlots(self.root / "state")
        with registry.acquire("uncertain", "task", "owner") as lease:
            process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                lease.bind({"identity": process_identity(process.pid), "tree_control": "private_process_group"})
            finally:
                process.terminate()
                process.wait(timeout=5)
            with self.assertRaisesRegex(HarnessError, "process-tree cleanup"):
                lease.complete({"process_tree_stopped": False})
            original_slot = lease.name
        self.assertTrue(registry.inventory("uncertain")[0]["unresolved"])
        with registry.acquire("replacement", "task", "owner") as replacement:
            self.assertNotEqual(replacement.name, original_slot)
            replacement.complete()

    def test_managed_shared_parent_creation_race_rechecks_the_winning_type(self):
        from agent_harness.supervisor_store import _mkdir
        original = Path.mkdir
        for ordinary in (True, False):
            with self.subTest(ordinary=ordinary):
                target = self.root / ("directory" if ordinary else "file")
                def concurrent_create(path, *args, **kwargs):
                    if path == target:
                        if ordinary:
                            original(path, *args, **kwargs)
                        else:
                            path.write_text("not a directory")
                        raise FileExistsError(path)
                    return original(path, *args, **kwargs)
                with patch.object(Path, "mkdir", concurrent_create):
                    if ordinary:
                        _mkdir(target)
                        self.assertTrue(target.is_dir())
                    else:
                        with self.assertRaisesRegex(HarnessError, "Unsupported private path type"):
                            _mkdir(target)


if __name__ == "__main__":
    unittest.main()
