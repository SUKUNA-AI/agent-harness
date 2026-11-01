"""End-to-end work scenarios with real contained fake-Codex processes."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.codex_execution import CodexAdapter
from agent_harness.core import HarnessError
from agent_harness.strict_yaml import dump, load
from agent_harness.supervisor import Supervisor

FAKE = r'''
import sys,json,pathlib,time,uuid
args=sys.argv[1:]
if '--version' in args: print('fake-codex-work 1.0');raise SystemExit
if '--help' in args: print('exec resume SESSION_ID --json --output-schema');raise SystemExit
prompt=args[-1]
sid=str(uuid.uuid5(uuid.NAMESPACE_URL,str(pathlib.Path.cwd())))
if 'resume' in args: assert args[args.index('resume')+1]==sid
def event(t,**v):print(json.dumps(dict(type=t,**v)),flush=True)
event('thread.started',thread_id=sid);event('turn.started')
calls=pathlib.Path(__file__).with_suffix('.calls')
with calls.open('a') as f:f.write(('resume' if 'resume' in args else 'exec')+'\n')
if 'Task contract and failure packet:\n' in prompt:
 task=json.loads(prompt.rsplit('Task contract and failure packet:\n',1)[1]); mode=task['objective']
else:
 task=json.loads(prompt.rsplit('\n',1)[1]);mode=task['goal']
if mode=='permission':event('turn.failed',error={'message':'permission denied by sandbox'});raise SystemExit(1)
if mode=='slow':time.sleep(3)
value=42 if mode not in ('repair','no-progress','failure') or 'resume' in args and mode=='repair' else 0
target='outside.py' if mode=='scope-violation' else 'calc.py'
if mode in ('left','right'):target=mode+'.py'
if mode!='managed':pathlib.Path(target).write_text('VALUE = '+str(value)+'\n')
if mode=='partial':raise SystemExit
if mode=='no-usage':event('turn.completed')
else:event('turn.completed',usage={'input_tokens':10,'output_tokens':3})
'''


class WorkSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "calc.py").write_text("VALUE = 0\n")
        self.fake = self.root / "fake.py"
        self.fake.write_text(FAKE)
        self.adapter = CodexAdapter((sys.executable, str(self.fake)))
        self.run = self.root / "private" / "run"

    def tearDown(self):
        self.temp.cleanup()

    def create(self, mode="simple", attempts=3):
        contract = {"schema_version": 1, "task": {"id": "calc", "objective": mode,
                     "allowed_write_scopes": ["calc.py"]},
                    "acceptance": [{"id": "value-test", "kind": "command", "required": True,
                        "argv": [sys.executable, "-c", "from pathlib import Path; assert 'VALUE = 42' in Path('calc.py').read_text()"],
                        "timeout": 15}], "budgets": {"max_attempts": attempts, "max_wall_seconds": 90}}
        path = self.root / "task.yaml"
        dump(path, contract)
        return Supervisor.create(path, run_dir=self.run, cwd=self.source, adapter=self.adapter,
                                 state_dir=self.root / "global-state")

    def calls(self):
        return self.fake.with_suffix(".calls").read_text().splitlines()

    def test_edit_precedes_freeze_and_independent_acceptance(self):
        supervisor = self.create()
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertTrue(supervisor.status(verify=True)["evidence_current"])
        self.assertEqual(self.calls(), ["exec"])
        self.assertTrue((self.run / "reports" / "report.yaml").is_file())
        (self.source / "calc.py").write_text("VALUE = 8\n")
        self.assertFalse(supervisor.status(verify=True)["evidence_current"])

    def test_failed_test_gets_exact_resume_then_fresh_pass(self):
        supervisor = self.create("repair")
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(self.calls(), ["exec", "resume"])
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(len(supervisor.state["failure_history"]), 1)
        self.assertEqual(len(list((self.run / "verification" / "attempts").iterdir())), 2)

    def test_model_completion_does_not_override_failed_test(self):
        result = self.create("failure", attempts=1).start()
        self.assertEqual(result["status"], "FAILED_TERMINAL", result)
        self.assertEqual(result["required_passed"], 0)
        self.assertEqual(self.calls(), ["exec"])

    def test_unchanged_failure_stops_without_third_model_call(self):
        result = self.create("no-progress", attempts=3).start()
        self.assertEqual(result["status"], "NO_PROGRESS", result)
        self.assertEqual(self.calls(), ["exec", "resume"])

    def test_permission_denial_never_retries_with_wider_flags(self):
        result = self.create("permission").start()
        self.assertEqual(result["status"], "BLOCKED_APPROVAL", result)
        self.assertEqual(self.calls(), ["exec"])

    def test_missing_stream_needs_recovery_then_verifies_without_repeating_write(self):
        supervisor = self.create("partial")
        result = supervisor.start()
        self.assertEqual(result["status"], "UNKNOWN_OUTCOME", result)
        recovered = Supervisor(self.run, adapter=self.adapter, state_dir=self.root / "global-state").resume()
        self.assertEqual(recovered["status"], "ACCEPTED", recovered)
        self.assertEqual(self.calls(), ["exec"])

    def test_second_resume_cannot_create_a_writer_during_active_execution(self):
        supervisor = self.create("slow")
        failures = []
        def execute():
            try: supervisor.start()
            except BaseException as error: failures.append(error)
        thread = threading.Thread(target=execute)
        thread.start()
        deadline = time.monotonic() + 10
        while not self.fake.with_suffix(".calls").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            with self.assertRaisesRegex(HarnessError, "busy"):
                Supervisor(self.run, adapter=self.adapter).resume()
        finally:
            thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.calls(), ["exec"])

    def test_missing_usage_remains_unavailable(self):
        result = self.create("no-usage").start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(result["metrics"]["usage_status"], "unavailable")
        self.assertIsNone(result["metrics"]["input_tokens"])

    def test_scope_escape_cannot_be_accepted(self):
        supervisor = self.create("scope-violation")
        result = supervisor.start()
        self.assertEqual(result["status"], "FAILED_TERMINAL", result)

    def test_terminal_resume_does_not_reset_original_budget(self):
        supervisor = self.create("failure", attempts=1)
        supervisor.start()
        with self.assertRaisesRegex(HarnessError, "Terminal"):
            Supervisor(self.run, adapter=self.adapter).resume()
        self.assertEqual(self.calls(), ["exec"])

    def test_optional_failed_check_remains_failed_after_required_acceptance(self):
        supervisor = self.create()
        # Recreate before any execution with an explicitly optional failing check.
        contract = supervisor.contract
        contract["acceptance"].append({"id": "optional-negative", "kind": "command", "required": False,
            "argv": [sys.executable, "-c", "raise SystemExit(1)"], "timeout": 15})
        task = self.root / "optional.yaml"
        dump(task, contract)
        supervisor = Supervisor.create(task, run_dir=self.root / "optional-run", cwd=self.source,
            adapter=self.adapter, state_dir=self.root / "global-state")
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(result["acceptance"][1]["status"], "failed")

    def test_memory_records_verified_receipts_and_revalidates_current_source(self):
        supervisor = self.create()
        contract = supervisor.contract
        contract["memory"] = True
        task = self.root / "memory.yaml"
        dump(task, contract)
        supervisor = Supervisor.create(task, run_dir=self.root / "memory-run", cwd=self.source,
            adapter=self.adapter, state_dir=self.root / "global-state")
        self.assertEqual(supervisor.start()["status"], "ACCEPTED")
        from agent_harness.memory import Memory
        memory = Memory(self.root / "global-state" / "memory", supervisor.repository_identity(), enabled=True)
        self.assertEqual(len(memory.recall("calc", supervisor.provenance())["verified_facts"]), 1)
        (self.source / "calc.py").write_text("VALUE = 7\n")
        self.assertEqual(memory.recall("calc", supervisor.provenance())["verified_facts"], [])

    def test_resume_uses_persisted_registry_and_refuses_a_lost_checkpoint_writer(self):
        from agent_harness.agent_lifecycle import GlobalSlots
        from agent_harness.core import process_identity
        supervisor = self.create()
        self.assertIsNone(supervisor.state["process_identity"])
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with GlobalSlots(supervisor.state_dir).acquire(supervisor.state["run_id"], "lost", "owner") as lease:
                lease.bind({"identity": process_identity(child.pid)})
                with self.assertRaisesRegex(HarnessError, "Global model registry"):
                    Supervisor(self.run, adapter=self.adapter).resume()
                with self.assertRaisesRegex(HarnessError, "conflicts"):
                    Supervisor(self.run, adapter=self.adapter, state_dir=self.root / "wrong-registry").resume()
                child.terminate(); child.wait(timeout=10)
                lease.complete()
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=10)

    def test_expired_context_construction_does_not_spawn_a_model(self):
        supervisor = self.create()
        contract = supervisor.contract
        contract["budgets"]["max_wall_seconds"] = 1
        task = self.root / "deadline.yaml"
        dump(task, contract)
        supervisor = Supervisor.create(task, run_dir=self.root / "deadline-run", cwd=self.source,
            adapter=self.adapter, state_dir=self.root / "global-state")
        original = supervisor._prompt
        def slow_context(**kwargs):
            value = original(**kwargs)
            time.sleep(1.1)
            return value
        supervisor._prompt = slow_context
        result = supervisor.start()
        self.assertEqual(result["status"], "TIMED_OUT", result)
        self.assertFalse(self.fake.with_suffix(".calls").exists())

    def test_token_budget_is_checked_after_observed_turn_before_acceptance(self):
        supervisor = self.create()
        contract = supervisor.contract
        contract["budgets"]["max_tokens"] = 1
        task = self.root / "tokens.yaml"
        dump(task, contract)
        supervisor = Supervisor.create(task, run_dir=self.root / "token-run", cwd=self.source,
            adapter=self.adapter, state_dir=self.root / "global-state")
        result = supervisor.start()
        self.assertEqual(result["status"], "FAILED_TERMINAL", result)
        self.assertEqual(result["required_passed"], 0)
