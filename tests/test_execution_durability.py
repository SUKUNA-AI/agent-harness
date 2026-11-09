"""Expected-safe F01/F02 controls, with real contained synthetic processes."""
from pathlib import Path
import os
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from managed_process_fixture import BoundFixtureCodexAdapter as CodexAdapter, bound_fake_script
from agent_harness.strict_yaml import dump, load
from agent_harness.supervisor import Supervisor
from agent_harness.core import HarnessError
from agent_harness.execution_receipts import validate_record
from agent_harness.work_contract import normalize
from test_managed_scheduling import node
from test_work_supervisor import FAKE


class Crash(BaseException):
    pass


class ExecutionDurabilityTests(unittest.TestCase):
    def fixture(self, root, *, cap=None):
        source = root / "source"
        source.mkdir()
        for name in ("left", "right"):
            (source / (name + ".py")).write_text("VALUE = 0\n")
        for args in (["init"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "fixture"]):
            subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
        fake = root / "fake.py"
        fake.write_text(bound_fake_script(FAKE))
        budgets = {"max_concurrent_managed_agents": 1, "max_wall_seconds": 120}
        if cap is not None:
            budgets["max_tokens"] = cap
        contract = normalize({"schema_version": 1, "task": {"id": "managed", "objective": "managed", "allowed_write_scopes": ["left.py", "right.py"]},
            "mode": "managed", "budgets": budgets,
            "acceptance": [{"id": "combined", "kind": "command", "required": True,
                "argv": [sys.executable, "-c", "from pathlib import Path; assert 'VALUE = 42' in Path('left.py').read_text()"], "timeout": 15}]})
        task, plan = root / "task.yaml", root / "plan.yaml"
        dump(task, contract)
        dump(plan, {"schema_version": 1, "task_id": "managed", "tasks": [node("left"), node("right", ["left"])]})
        supervisor = Supervisor.create(task, run_dir=root / "private" / "run", cwd=source,
            state_dir=root / "state", adapter=CodexAdapter((sys.executable, str(fake))))
        supervisor.plan_input = str(plan)
        return supervisor, fake

    def interrupted(self, *, before_checkpoint=False, cap=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                supervisor, fake = self.fixture(root, cap=cap)
                original = supervisor._save
                def save(reason):
                    if reason.startswith("integrated scoped task left") and before_checkpoint:
                        raise Crash()
                    value = original(reason)
                    if reason.startswith("integrated scoped task left"):
                        raise Crash()
                    return value
                with patch.object(supervisor, "_save", save):
                    with self.assertRaises(Crash):
                        supervisor.start()
                recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
                result = recovered.resume()
                self.assertEqual(result["status"], "ACCEPTED", result)
                if before_checkpoint:
                    self.assertEqual(result["metrics"]["input_tokens"], 40)
                    self.assertEqual(result["metrics"]["output_tokens"], 12)
                self.assertIn("VALUE = 42", (Path(result["cwd"]) / "right.py").read_text())
                self.assertEqual(result["metrics"]["input_tokens"], 40)
                self.assertEqual(result["metrics"]["output_tokens"], 12)
                self.assertEqual(result["metrics"]["missing_usage_turns"], 0)
                calls = fake.with_suffix(".calls").read_bytes()
                for _ in range(3):
                    self.assertEqual(Supervisor(supervisor.path, adapter=supervisor.adapter).resume()["status"], "ACCEPTED")
                self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)

    def test_f01_wave_checkpoint_preserves_never_started_nodes(self):
        self.interrupted()

    def test_f02_integrated_before_aggregate_save_recovers_exact_usage(self):
        self.interrupted(before_checkpoint=True, cap=1000)

    def test_receipt_replay_conflict_and_distinct_equal_invocations(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor, _ = self.fixture(Path(temp))
            receipts = supervisor.receipts()
            for _ in range(2):
                intent = receipts.intent("left", "attempt-1", checkout=supervisor.state["cwd"], owner="fixture")
                completion = receipts.completion(intent, {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 3}})
                before = len(supervisor.store.events())
                self.assertEqual(receipts.publish(completion), completion)
                self.assertEqual(len(supervisor.store.events()), before)
            receipts.reconcile_usage()
            self.assertEqual((supervisor.state["metrics"]["input_tokens"], supervisor.state["metrics"]["output_tokens"]), (20, 6))
            changed = dict(completion, payload=dict(completion["payload"], input_tokens=999))
            with self.assertRaisesRegex(HarnessError, "integrity conflict"):
                receipts.publish(changed)
            receipts.reconcile_usage()
            self.assertEqual(supervisor.state["metrics"]["input_tokens"], 20)

    def test_unknown_partial_invalid_and_zero_usage_are_distinct(self):
        from agent_harness import telemetry
        for usage, expected in ((None, "unavailable"), ({"input_tokens": 4}, "partial"),
                ({"input_tokens": True, "output_tokens": 0}, "invalid"), ({"input_tokens": 0, "output_tokens": 0}, "complete")):
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as temp:
                supervisor, _ = self.fixture(Path(temp), cap=1000)
                receipts = supervisor.receipts()
                intent = receipts.intent("left", "attempt-1", checkout=supervisor.state["cwd"], owner="fixture")
                receipts.reconcile_usage()
                self.assertFalse(telemetry.budget_usage_known(supervisor.state["metrics"]))
                self.assertEqual(supervisor.state["metrics"]["missing_usage_turns"], 1)
                complete = receipts.completion(intent, {"status": "completed", "usage": usage})
                self.assertEqual(complete["payload"]["usage_status"], expected)
                receipts.reconcile_usage()
                self.assertEqual(telemetry.budget_usage_known(supervisor.state["metrics"]), expected == "complete")

    def test_journal_completion_recovers_missing_projection_without_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor, _ = self.fixture(Path(temp))
            receipts = supervisor.receipts()
            intent = receipts.intent("left", "attempt-1", checkout=supervisor.state["cwd"], owner="fixture")
            original = supervisor.store.write
            def fail_projection(relative, *args, **kwargs):
                if relative.endswith("-completion.yaml"):
                    raise OSError("synthetic disk-full after authoritative fsync")
                return original(relative, *args, **kwargs)
            with patch.object(supervisor.store, "write", fail_projection):
                with self.assertRaises(OSError):
                    receipts.completion(intent, {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 3}})
            accounting = receipts.reconcile_usage(repair=True)
            self.assertEqual(supervisor.state["metrics"]["input_tokens"], 10)
            self.assertEqual(len(accounting["completions"]), 1)
            self.assertTrue(supervisor.store.path("receipts/" + intent["invocation_id"] + "-completion.yaml").exists())

    def test_receipt_schema_is_closed_and_future_versions_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor, _ = self.fixture(Path(temp))
            record = supervisor.receipts().intent("left", "attempt-1", checkout=supervisor.state["cwd"], owner="fixture")
            for change in ({"schema_version": 99}, {"unexpected": True}, {"payload": {}}):
                with self.subTest(change=change), self.assertRaises((HarnessError, ValueError)):
                    validate_record(dict(record, **change))

    def test_cancel_survives_recovery_and_prevents_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            supervisor, fake = self.fixture(root)
            supervisor.stop("fixture cancellation before dispatch")
            result = supervisor.resume()
            self.assertEqual(result["status"], "CANCELLED")
            self.assertTrue(supervisor.store.path("cancel.yaml").exists())
            self.assertFalse(fake.with_suffix(".calls").exists())

    def test_pending_migration_is_a_writer_barrier(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor, fake = self.fixture(Path(temp))
            supervisor.state["migration"] = {"operation_id": "fixture", "status": "pending", "plan_digest": "a" * 64}
            supervisor._save("fixture migration publication barrier")
            before = supervisor.store.path("journal/events.jsonl").read_bytes()
            with self.assertRaisesRegex(HarnessError, "MIGRATION_RECOVERY_REQUIRED"):
                Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
            self.assertEqual(supervisor.store.path("journal/events.jsonl").read_bytes(), before)
            self.assertFalse(fake.with_suffix(".calls").exists())

    def test_migrated_unknown_history_is_not_promoted_by_complete_receipts(self):
        from agent_harness import telemetry
        with tempfile.TemporaryDirectory() as temp:
            supervisor, _ = self.fixture(Path(temp))
            supervisor.state["metrics"].update(legacy_history_unverified=True, input_tokens=17, output_tokens=2)
            receipts = supervisor.receipts()
            intent = receipts.intent("left", "attempt-1", checkout=supervisor.state["cwd"], owner="fixture")
            receipts.completion(intent, {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 3}})
            for _ in range(3):
                receipts.reconcile_usage()
            metrics = supervisor.state["metrics"]
            self.assertFalse(telemetry.budget_usage_known(metrics))
            self.assertEqual((metrics["input_tokens"], metrics["receipt_input_tokens"]), (17, 10))
            self.assertIn("legacy-history", metrics["unknown_invocation_ids"])

    def test_fresh_review_blocks_defects_invalid_output_writes_and_unknown_usage(self):
        for variant in ("blocked", "invalid", "writes", "partial"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                    supervisor, fake = self.fixture(root, cap=1000)
                    script = fake.read_text()
                    if variant == "blocked":
                        script = script.replace("'verdict':'approved'", "'verdict':'blocked'")
                    elif variant == "invalid":
                        script = script.replace("json.dumps({'schema_version':1,'verdict':'approved','findings':[]})", "'not json'")
                    elif variant == "writes":
                        script = script.replace("if 'HARNESS_FINAL_REVIEW' in prompt:\n", "if 'HARNESS_FINAL_REVIEW' in prompt:\n pathlib.Path('left.py').write_text('VALUE = 987\\n')\n")
                    else:
                        script = script.replace("event('turn.completed',usage={'input_tokens':10,'output_tokens':3});raise SystemExit", "event('turn.completed');raise SystemExit")
                    fake.write_text(script)
                    result = supervisor.start()
                    self.assertIn(result["status"], {"BLOCKED_AMBIGUOUS", "BLOCKED_ENVIRONMENT", "UNKNOWN_OUTCOME"}, result)
                    self.assertIsNone(supervisor._core().state()["accepted"])

    def test_review_findings_return_to_same_integration_owner_with_original_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                supervisor, fake = self.fixture(root)
                fake.write_text(fake.read_text().replace("'verdict':'approved','findings':[]", "'verdict':'changes_requested','findings':['fixture defect']"))
                result = supervisor.start()
                self.assertEqual(result["status"], "NO_PROGRESS", result)
                calls = fake.with_suffix(".calls").read_text().splitlines()
                self.assertIn("resume", calls)
                self.assertEqual(calls, ["exec"] * 4 + ["resume"])
                self.assertEqual(result["metrics"]["model_invocations"], 5)
                self.assertEqual(sum(r["record_type"] == "review" for r in supervisor.receipts().records()), 1)
                self.assertLessEqual(result["attempt"], result["max_attempts"])
                self.assertIsNone(supervisor._core().state()["accepted"])

    def test_unchanged_blocked_review_replays_actual_verdict_on_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                supervisor, fake = self.fixture(root)
                fake.write_text(fake.read_text().replace("'verdict':'approved'", "'verdict':'blocked'"))
                self.assertEqual(supervisor.start()["status"], "BLOCKED_AMBIGUOUS")
                calls = fake.with_suffix(".calls").read_bytes()
                for _ in range(2):
                    result = Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
                    self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
                self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
                self.assertEqual(sum(r["record_type"] == "review" for r in supervisor.receipts().records()), 1)

    def test_review_completion_and_verdict_recovery_never_repeat_model_work(self):
        from agent_harness.execution_receipts import ExecutionReceipts
        for boundary in ("completion", "review"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                    supervisor, fake = self.fixture(root, cap=52)  # Exactly all four invocations.
                    original = ExecutionReceipts.publish
                    def crash(instance, record):
                        value = original(instance, record)
                        if record["record_type"] == boundary and record["node_id"] == "@review":
                            raise Crash()
                        return value
                    with patch.object(ExecutionReceipts, "publish", crash):
                        with self.assertRaises(Crash):
                            supervisor.start()
                    calls = fake.with_suffix(".calls").read_bytes()
                    result = Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
                    self.assertEqual(result["status"], "ACCEPTED", result)
                    self.assertEqual((result["metrics"]["input_tokens"], result["metrics"]["output_tokens"]), (40, 12))
                    self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)

    def test_changed_review_artifact_is_stale_without_rewriting_historical_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                supervisor, _ = self.fixture(root)
                self.assertEqual(supervisor.start()["status"], "ACCEPTED")
                completion = next(r for r in supervisor.receipts().records() if r["record_type"] == "completion" and r["node_id"] == "@review")
                Path(completion["payload"]["logs"]["stdout"]).write_text("changed after review\n")
                status = supervisor.status(verify=True)
                self.assertEqual(status["status"], "STALE")
                self.assertFalse(status["evidence_current"])
                self.assertEqual(status["recorded_status"], "ACCEPTED")

    def test_successful_terminal_event_without_process_tree_cleanup_cannot_integrate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                supervisor, fake = self.fixture(root)
                run = supervisor.adapter.run
                completed = []
                def unresolved(*args, **kwargs):
                    result = run(*args, **kwargs)
                    diagnostic = dict(result)
                    diagnostic["log_tails"] = {
                        name: Path(path).read_text(encoding="utf-8", errors="replace")[-1500:]
                        for name, path in result.get("logs", {}).items() if Path(path).is_file()
                    }
                    self.assertEqual(result["status"], "completed",
                        "Cleanup scenario requires a real completed turn before injection: " + repr(diagnostic))
                    self.assertTrue(result["turn_completed"], diagnostic)
                    self.assertEqual(result["usage"], {"input_tokens": 10, "output_tokens": 3}, diagnostic)
                    self.assertTrue(result["process_tree_stopped"], diagnostic)
                    self.assertEqual(fake.with_suffix(".calls").read_text(encoding="utf-8").splitlines(), ["exec"])
                    completed.append(result)
                    return {**result, "process_tree_stopped": False}
                with patch.object(supervisor.adapter, "run", unresolved):
                    result = supervisor.start()
                self.assertEqual(result["status"], "UNKNOWN_OUTCOME")
                self.assertEqual(len(completed), 1, result)
                self.assertEqual(fake.with_suffix(".calls").read_text(encoding="utf-8").splitlines(), ["exec"])
                self.assertFalse(supervisor.plan_progress()["execution_complete"])

    def test_cleanup_fixture_does_not_mask_pre_exec_failure(self):
        with patch("agent_harness.codex_execution._start_contained",
                   side_effect=HarnessError("synthetic pre-exec failure")):
            # Ordinary adapter assertions now produce durable UNKNOWN_OUTCOME.
            # The control must still fail its count of real completed turns;
            # a pre-exec failure cannot masquerade as a cleanup scenario.
            with self.assertRaisesRegex(AssertionError, "0 != 1"):
                self.test_successful_terminal_event_without_process_tree_cleanup_cannot_integrate()

    def test_join_read_only_node_has_real_completion_without_fictitious_commit(self):
        from agent_harness.strict_yaml import load
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                supervisor, _ = self.fixture(root)
                plan = load(supervisor.plan_input, "agent-plan.schema.json")
                join = node("join", ["left", "right"])
                join.update(goal="managed", role="reviewer", read_only=True, write_scope=[], outputs=["left.py"])
                plan["tasks"].append(join)
                dump(supervisor.plan_input, plan, "agent-plan.schema.json")
                result = supervisor.start()
                self.assertEqual(result["status"], "ACCEPTED", result)
                integration = next(r for r in supervisor.receipts().records() if r["record_type"] == "integration" and r["node_id"] == "join")
                self.assertEqual(integration["payload"]["result_kind"], "read_only")
                self.assertEqual(result["metrics"]["model_invocations"], 5)

    def test_integration_faults_reconcile_pre_post_and_completion_without_replay(self):
        from agent_harness.agent_lifecycle import Worktrees
        from agent_harness.execution_receipts import ExecutionReceipts
        for boundary in ("intent", "apply", "commit", "receipt", "root_completion"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                    supervisor, fake = self.fixture(root, cap=1000)
                    publish = ExecutionReceipts.publish
                    integrate = Worktrees.integrate
                    from agent_harness import managed_execution
                    git = managed_execution.git
                    fired = []
                    def publication(instance, record):
                        value = publish(instance, record)
                        fault = ((boundary == "intent" and record["record_type"] == "integration_intent")
                            or (boundary == "receipt" and record["record_type"] == "integration")
                            or (boundary == "root_completion" and record["record_type"] == "completion" and record["node_id"] == "@integration"))
                        if fault and not fired:
                            fired.append(True)
                            raise Crash()
                        return value
                    def apply(trees, staging, data):
                        value = integrate(trees, staging, data)
                        if boundary == "apply" and not fired:
                            fired.append(True)
                            raise Crash()
                        return value
                    def commit(cwd, *args, **kwargs):
                        value = git(cwd, *args, **kwargs)
                        if boundary == "commit" and "commit" in args and not fired:
                            fired.append(True)
                            raise Crash()
                        return value
                    with patch.object(ExecutionReceipts, "publish", publication), patch.object(Worktrees, "integrate", apply), patch.object(managed_execution, "git", commit):
                        with self.assertRaises(Crash):
                            supervisor.start()
                    recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
                    result = recovered.resume()
                    if result["status"] != "ACCEPTED":
                        result = {**result, "fixture_transport": supervisor.adapter.failure_summary(result)}
                    self.assertEqual(result["status"], "ACCEPTED", result)
                    self.assertEqual((result["metrics"]["input_tokens"], result["metrics"]["output_tokens"]), (40, 12))
                    self.assertEqual(fake.with_suffix(".calls").read_text().splitlines(), ["exec"] * 4)

    def test_unknown_dispatch_and_third_integration_state_never_replay(self):
        from agent_harness.execution_receipts import ExecutionReceipts
        for boundary in ("dispatch", "third_state"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}):
                    supervisor, fake = self.fixture(root, cap=1000)
                    publish = ExecutionReceipts.publish
                    def fault(instance, record):
                        value = publish(instance, record)
                        if record["record_type"] == ("invocation_intent" if boundary == "dispatch" else "integration_intent"):
                            raise Crash()
                        return value
                    with patch.object(ExecutionReceipts, "publish", fault):
                        with self.assertRaises(Crash):
                            supervisor.start()
                    calls = fake.with_suffix(".calls").read_bytes() if fake.with_suffix(".calls").exists() else b""
                    if boundary == "third_state":
                        (Path(supervisor.state["cwd"]) / "left.py").write_text("VALUE = 987\n")
                        result = Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
                        self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
                    else:
                        with self.assertRaisesRegex(HarnessError, "uncertain writers"):
                            Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
                    self.assertEqual(fake.with_suffix(".calls").read_bytes() if fake.with_suffix(".calls").exists() else b"", calls)

    @unittest.skipUnless(os.name == "posix", "Real SIGKILL protocol qualification is POSIX-specific")
    def test_process_kill_recovers_with_a_new_controller_process(self):
        tests = Path(__file__).parent
        script = r'''
import json, os, pathlib, signal, sys
sys.path.insert(0, sys.argv[1])
from test_execution_durability import ExecutionDurabilityTests
from agent_harness.execution_receipts import ExecutionReceipts
root = pathlib.Path(sys.argv[2]); boundary = sys.argv[3]
os.environ['CODEX_HOME'] = str(root / 'codex-home')
supervisor, fake = ExecutionDurabilityTests().fixture(root, cap=1000)
publish = ExecutionReceipts.publish
def fault(instance, record):
    result = publish(instance, record)
    if record['record_type'] == boundary and record['node_id'] == 'left':
        os.kill(os.getpid(), signal.SIGKILL)
    return result
ExecutionReceipts.publish = fault
supervisor.start()
'''
        resume = r'''
import json, os, pathlib, sys
sys.path.insert(0, sys.argv[1]); root = pathlib.Path(sys.argv[2])
os.environ['CODEX_HOME'] = str(root / 'codex-home')
from test_execution_durability import Supervisor, CodexAdapter
result = Supervisor(root / 'private' / 'run', adapter=CodexAdapter((sys.executable, str(root / 'fake.py')))).resume()
print(json.dumps(result))
'''
        for boundary in ("completion", "integration_intent", "integration"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                killed = subprocess.run([sys.executable, "-c", script, str(tests), str(root), boundary], capture_output=True, timeout=30)
                self.assertEqual(killed.returncode, -9, killed.stderr.decode())
                original_deadline = load(root / "private" / "run" / "state.yaml")["deadline"]
                watchdog = load(root / "task.yaml")["budgets"]["max_wall_seconds"]
                # Recovery can use the fixture's wall budget. The child enforces the persisted deadline.
                recovered = subprocess.run(
                    [sys.executable, "-c", resume, str(tests), str(root)],
                    capture_output=True, timeout=watchdog,
                )
                self.assertEqual(recovered.returncode, 0, recovered.stderr.decode())
                result = json.loads(recovered.stdout)
                self.assertEqual(result["status"], "ACCEPTED", result)
                self.assertEqual(result["deadline"], original_deadline)
                self.assertEqual((result["metrics"]["input_tokens"], result["metrics"]["output_tokens"]), (40, 12))
                self.assertEqual((root / "fake.calls").read_text().splitlines(), ["exec"] * 4)
