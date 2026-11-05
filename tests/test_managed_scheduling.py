"""DAG rejection, critical path and real isolated managed patch integration."""
from pathlib import Path
from copy import deepcopy
import subprocess
import sys
import tempfile
import unittest
import os
from unittest.mock import patch as environment_patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError
from managed_process_fixture import BoundFixtureCodexAdapter as CodexAdapter, bound_fake_script
from agent_harness.strict_yaml import dump
from agent_harness.work_contract import normalize
from agent_harness.scheduling import Scheduler, validate_plan
from agent_harness.supervisor import Supervisor
from test_work_supervisor import FAKE


def node(name, dependencies=(), scopes=None, seconds=60):
    return {"id": name, "goal": name, "role": "owner", "read_only": False,
            "read_scope": ["**"], "write_scope": scopes or [name + ".py"],
            "depends_on": list(dependencies), "acceptance_ids": ["combined"],
            "outputs": [name + ".py"], "estimated_seconds": seconds, "max_attempts": 3,
            "max_wall_seconds": 120, "risk": "low", "impact": 5, "verification": ["combined"]}


class ManagedSchedulingTests(unittest.TestCase):
    def setUp(self):
        isolated_profile = tempfile.TemporaryDirectory()
        self.addCleanup(isolated_profile.cleanup)
        environment = environment_patch.dict(os.environ, {"CODEX_HOME": str(Path(isolated_profile.name) / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.contract = normalize({"schema_version": 1, "task": {"id": "managed", "objective": "managed",
            "allowed_write_scopes": ["left.py", "right.py"]}, "mode": "managed",
            "budgets": {"max_concurrent_managed_agents": 2, "max_wall_seconds": 120},
            "acceptance": [{"id": "combined", "kind": "command", "required": True,
                "argv": [sys.executable, "-c", "from pathlib import Path; assert all('VALUE = 42' in Path(n+'.py').read_text() for n in ('left','right'))"],
                "timeout": 15}]})
        self.plan = {"schema_version": 1, "task_id": "managed", "tasks": [node("left"), node("right")]}

    def test_invalid_dag_or_scope_is_rejected_before_any_spawn(self):
        cyclic = deepcopy(self.plan)
        cyclic["tasks"][0]["depends_on"] = ["right"]
        cyclic["tasks"][1]["depends_on"] = ["left"]
        overlap = deepcopy(self.plan)
        overlap["tasks"][1]["write_scope"] = ["left.py"]
        unknown = deepcopy(self.plan)
        unknown["tasks"][0]["depends_on"] = ["missing"]
        unsafe = deepcopy(self.plan)
        unsafe["tasks"][0]["write_scope"] = ["../config.toml"]
        output_escape = deepcopy(self.plan)
        output_escape["tasks"][0]["outputs"] = ["../outside.py"]
        no_scope = deepcopy(self.plan)
        no_scope["tasks"][0]["write_scope"] = []
        for plan in (cyclic, overlap, unknown, unsafe, output_escape, no_scope):
            with self.subTest(plan=plan):
                with self.assertRaises((HarnessError, ValueError)):
                    validate_plan(plan, self.contract)

    def test_critical_path_and_dependency_readiness_bound_spawn(self):
        self.plan["tasks"][1]["depends_on"] = ["left"]
        self.plan["tasks"][1]["estimated_seconds"] = 120
        scheduler = Scheduler(self.plan, self.contract)
        self.assertEqual(scheduler.next()["selected"], ["left"])
        self.assertEqual(scheduler.next()["critical_path"], ["left", "right"])
        self.assertEqual(scheduler.next(completed=["left"])["selected"], ["right"])
        self.assertEqual(scheduler.next(active=["left"])["selected"], [])

    def test_two_worktrees_integrate_into_new_frozen_candidate(self):
        class FakeManaged(CodexAdapter):
            def capabilities(self):
                return {**super().capabilities(), "native_children_disabled": True}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            for name in ("left", "right"):
                (source / (name + ".py")).write_text("VALUE = 0\n")
            for args in (["init"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "fixture"]):
                subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
            fake = root / "fake.py"
            fake.write_text(bound_fake_script(FAKE))
            task, plan = root / "task.yaml", root / "plan.yaml"
            dump(task, self.contract)
            dump(plan, self.plan)
            supervisor = Supervisor.create(task, run_dir=root / "private" / "run", cwd=source,
                state_dir=root / "state", adapter=FakeManaged((sys.executable, str(fake))))
            supervisor.plan_input = str(plan)
            result = supervisor.start()
            self.assertEqual(result["status"], "ACCEPTED", result)
            self.assertTrue(supervisor.status(verify=True)["evidence_current"])
            self.assertNotEqual(result["cwd"], str(source))
            self.assertEqual((source / "left.py").read_text(), "VALUE = 0\n")
            self.assertEqual(result["metrics"]["managed_spawns"], 2)
            self.assertGreater(result["metrics"]["spawn_seconds"], 0)
            self.assertEqual(result["metrics"]["spawn_measurements"], 4)
            self.assertGreater(result["metrics"]["worktree_seconds"], 0)
            agents = [supervisor.store.read("agents/" + name + ".yaml", "agent-state.schema.json") for name in ("left", "right")]
            self.assertNotEqual(agents[0]["worktree"]["path"], agents[1]["worktree"]["path"])
            self.assertTrue(all(agent["status"] == "integrated" for agent in agents))

    def test_managed_concurrency_without_runtime_proof_fails_closed(self):
        from agent_harness.managed_execution import validate_suppression
        with self.assertRaisesRegex(HarnessError, "suppression proof"):
            validate_suppression(None, {"version": "unproven", "binary_sha256": "x"})

    def test_planner_materializes_validated_schema_in_private_run(self):
        import json
        from agent_harness.managed_execution import _plan
        from agent_harness.strict_yaml import load
        plan = deepcopy(self.plan)
        class Planner:
            def run(adapter, prompt, **options):
                schema_path = Path(options["output_schema"])
                self.assertTrue(schema_path.is_relative_to(supervisor.path))
                self.assertTrue(options["read_only"])
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                self.assertEqual(schema["required"], ["schema_version", "task_id", "tasks"])
                log = supervisor.path / "planner-stream.jsonl"
                log.write_text(json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": json.dumps(plan)}}) + "\n", encoding="utf-8")
                return {"status": "completed", "turn_started": True,
                    "usage": {"input_tokens": 5, "output_tokens": 2}, "logs": {"stdout": str(log)}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            task = root / "task.yaml"
            dump(task, self.contract)
            supervisor = Supervisor.create(task, run_dir=root / "private" / "run", cwd=source,
                state_dir=root / "state", adapter=Planner())
            self.assertEqual(_plan(supervisor, {"output_schema": True}), plan)
            self.assertEqual(load(supervisor.path / "plans/current.yaml", "agent-plan.schema.json"), plan)
            self.assertEqual(supervisor.state["metrics"]["input_tokens"], 5)
            self.assertEqual(supervisor.state["process_status"], "completed")

    def test_interrupted_wave_with_completion_receipts_preserves_configured_token_budget(self):
        from unittest.mock import patch
        from agent_harness.agent_lifecycle import Worktrees
        class FakeManaged(CodexAdapter):
            def capabilities(self):
                return {**super().capabilities(), "native_children_disabled": True}
        class ControllerCrash(BaseException):
            pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            for name in ("left", "right"):
                (source / (name + ".py")).write_text("VALUE = 0\n")
            for args in (["init"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "fixture"]):
                subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
            fake = root / "fake.py"
            fake.write_text(bound_fake_script(FAKE))
            contract = deepcopy(self.contract)
            contract["budgets"]["max_tokens"] = 10000
            task, plan = root / "task.yaml", root / "plan.yaml"
            dump(task, contract); dump(plan, self.plan)
            adapter = FakeManaged((sys.executable, str(fake)))
            supervisor = Supervisor.create(task, run_dir=root / "private" / "run", cwd=source,
                state_dir=root / "state", adapter=adapter)
            supervisor.plan_input = str(plan)
            with patch.object(Worktrees, "integrate", side_effect=ControllerCrash):
                try:
                    unexpected = supervisor.start()
                except ControllerCrash:
                    pass
                else:
                    self.fail('Integration fault was not reached: ' + repr(unexpected))
            calls = fake.with_suffix(".calls").read_bytes()
            result = Supervisor(supervisor.path, adapter=adapter).resume()
            self.assertEqual(result["status"], "ACCEPTED", result)
            self.assertEqual((result["metrics"]["input_tokens"], result["metrics"]["output_tokens"]), (40, 12))
            self.assertEqual(fake.with_suffix(".calls").read_text().splitlines(), ["exec"] * 4)
            self.assertTrue(fake.with_suffix(".calls").read_bytes().startswith(calls))

    def test_acceptance_cwd_and_internal_paths_are_rebased_to_staging(self):
        from agent_harness.work_contract import legacy_contract
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, staging = root / "original", root / "staging"
            original.mkdir(); staging.mkdir()
            contract = deepcopy(self.contract)
            contract["acceptance"][0]["cwd"] = str(original)
            contract["acceptance"][0]["argv"] = [sys.executable, str(original / "test.py")]
            value = legacy_contract(contract, cwd=staging, source_cwd=original, run_id="test", attempt=1,
                                    deadline="2099-01-01T00:00:00+00:00")
            self.assertEqual(Path(value["checks"][0]["cwd"]).resolve(), staging.resolve())
            self.assertEqual(Path(value["checks"][0]["argv"][1]).resolve(), (staging / "test.py").resolve())

    def test_orphan_child_prevents_slot_reuse_after_controller_crash(self):
        import json, os, signal
        from agent_harness.agent_lifecycle import GlobalSlots
        from agent_harness.core import identity_alive
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "crash.py"
            script.write_text('import sys,os,subprocess,json,pathlib\n'
                + 'sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1] / "src")) + ')\n'
                + 'from agent_harness.agent_lifecycle import GlobalSlots\n'
                + 'from agent_harness.core import process_identity\n'
                + 'with GlobalSlots(sys.argv[1]).acquire("orphan-run","task","owner") as slot:\n'
                + ' child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
                + ' identity=process_identity(child.pid);slot.bind({"identity":identity})\n'
                + ' pathlib.Path(sys.argv[2]).write_text(json.dumps(identity))\n'
                + ' os._exit(0)\n')
            receipt = root / "child.json"
            subprocess.run([sys.executable, str(script), str(root / "state"), str(receipt)], check=True)
            identity = json.loads(receipt.read_text())
            try:
                self.assertTrue(identity_alive(identity))
                with GlobalSlots(root / "state").acquire("new-run", "task", "owner", timeout=2) as lease:
                    self.assertNotEqual(lease.name, "slot-0.yaml")
                    lease.complete()
            finally:
                if identity_alive(identity):
                    os.kill(identity["pid"], signal.SIGTERM)

    def test_managed_recovery_resumes_exact_workers_and_reconciles_already_applied_patch(self):
        from unittest.mock import patch
        from agent_harness.agent_lifecycle import Worktrees
        class FakeManaged(CodexAdapter):
            def capabilities(self):
                return {**super().capabilities(), "native_children_disabled": True}
        class ControllerCrash(BaseException):
            pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            for name in ("left", "right"):
                (source / (name + ".py")).write_text("VALUE = 0\n")
            for args in (["init"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "fixture"]):
                subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
            fake = root / "fake.py"
            fake.write_text(bound_fake_script(FAKE))
            task, plan = root / "task.yaml", root / "plan.yaml"
            dump(task, self.contract); dump(plan, self.plan)
            adapter = FakeManaged((sys.executable, str(fake)))
            supervisor = Supervisor.create(task, run_dir=root / "private" / "run", cwd=source,
                                           state_dir=root / "state", adapter=adapter)
            supervisor.plan_input = str(plan)
            original = Worktrees.integrate
            def crash_after_write(trees, staging, patch_bytes):
                original(trees, staging, patch_bytes)
                raise ControllerCrash()
            with patch.object(Worktrees, "integrate", crash_after_write):
                try:
                    unexpected = supervisor.start()
                except ControllerCrash:
                    pass
                else:
                    self.fail('Integration fault was not reached: ' + repr(unexpected))
            recovered = Supervisor(supervisor.path, adapter=adapter)
            result = recovered.resume()
            self.assertGreaterEqual(len(adapter.bound_children), 4)
            self.assertEqual(result["status"], "ACCEPTED", result)
            self.assertTrue(result["evidence_current"])
            agents = [recovered.store.read("agents/" + name + ".yaml", "agent-state.schema.json") for name in ("left", "right")]
            self.assertTrue(all(row["attempts_used"] == 1 for row in agents))
            self.assertNotEqual(agents[0]["thread_id"], agents[1]["thread_id"])
            self.assertEqual(fake.with_suffix(".calls").read_text().splitlines().count("resume"), 0)
            self.assertEqual((source / "left.py").read_text(), "VALUE = 0\n")

    def test_recovery_rejects_foreign_worker_or_staging_before_any_model_spawn(self):
        from unittest.mock import patch
        from agent_harness.agent_lifecycle import Worktrees
        class FakeManaged(CodexAdapter):
            def capabilities(self):
                return {**super().capabilities(), "native_children_disabled": True}
        class ControllerCrash(BaseException):
            pass
        for corrupt in ("worker", "staging", "scope"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, foreign = root / "source", root / "foreign"
                for folder in (source, foreign):
                    folder.mkdir()
                    for name in ("left", "right"):
                        (folder / (name + ".py")).write_text("VALUE = 0\n")
                    for args in (["init"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "fixture"]):
                        subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)
                fake = root / "fake.py"
                # Exact session selection does not itself authorize a cwd. This
                # fake honors the supplied ID, like a restored session adapter;
                # the supervisor must prevent foreign writes before invocation.
                fake.write_text(bound_fake_script(FAKE.replace("if 'resume' in args: assert args[args.index('resume')+1]==sid",
                                             "if 'resume' in args: sid=args[args.index('resume')+1]")))
                task, plan = root / "task.yaml", root / "plan.yaml"
                dump(task, self.contract); dump(plan, self.plan)
                adapter = FakeManaged((sys.executable, str(fake)))
                supervisor = Supervisor.create(task, run_dir=root / "private" / "run", cwd=source,
                                               state_dir=root / "state", adapter=adapter)
                supervisor.plan_input = str(plan)
                with patch.object(Worktrees, "integrate", side_effect=ControllerCrash):
                    try:
                        unexpected = supervisor.start()
                    except ControllerCrash:
                        pass
                    else:
                        self.fail("Expected integration fault; observed " + repr(unexpected))
                calls_before = fake.with_suffix(".calls").read_bytes()
                target = "integration.yaml" if corrupt == "staging" else "agents/left.yaml"
                record = supervisor.store.read(target)
                if corrupt == "staging":
                    record["path"] = str(foreign)
                elif corrupt == "worker":
                    record["worktree"]["path"] = str(foreign)
                else:
                    record["allowed_write_scope"] = ["**"]
                supervisor.store.write(target, record)
                with self.assertRaises(HarnessError):
                    Supervisor(supervisor.path, adapter=adapter).resume()
                self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls_before)
                self.assertEqual((foreign / "left.py").read_text(), "VALUE = 0\n")

    def test_all_recovery_variants_keep_child_live_during_delayed_bind(self):
        import time
        from agent_harness.agent_lifecycle import ModelLease
        from agent_harness.core import identity_alive
        original = ModelLease.bind
        delayed = []
        def delayed_left_bind(lease, process):
            if lease.value['task_id'] == 'left':
                delayed.append(process['identity'])
                # On the old raw fake this waits for natural exit, then the
                # real bind correctly refuses it. The handshake child remains
                # live through the entire delay and real ownership callback.
                end = time.monotonic() + 2
                while identity_alive(process['identity']) and time.monotonic() < end:
                    time.sleep(0.001)
            return original(lease, process)
        methods = (
            'test_interrupted_wave_with_completion_receipts_preserves_configured_token_budget',
            'test_managed_recovery_resumes_exact_workers_and_reconciles_already_applied_patch',
            'test_recovery_rejects_foreign_worker_or_staging_before_any_model_spawn',
        )
        with environment_patch.object(ModelLease, 'bind', delayed_left_bind):
            for method in methods:
                with self.subTest(recovery=method):
                    case = ManagedSchedulingTests(method)
                    try:
                        case.setUp()
                        getattr(case, method)()
                    finally:
                        case.doCleanups()
        self.assertEqual(len(delayed), 5)
