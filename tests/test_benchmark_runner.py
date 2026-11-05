"""Contained synthetic cohort execution; never invokes a real model/service."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness import evaluation
from agent_harness.benchmark_runner import BenchmarkRunner, BudgetAuthorization, CodexBenchmarkAdapter, production_status
from agent_harness.codex_execution import CodexAdapter
from agent_harness.core import HarnessError, digest
from agent_harness.strict_yaml import dump, dumps
from agent_harness import benchmark_runner
from agent_harness.supervisor import Supervisor

FAKE = r'''
import json,pathlib,sys,time,uuid
args=sys.argv[1:]
if '--version' in args: print('fixture-client');raise SystemExit
if '--help' in args: print('exec resume SESSION_ID --json --output-schema');raise SystemExit
if '--control' in args:
 value=json.loads(args[-1]); value['controls']={k:'synthetic_denied' for k in ('filesystem','network','evaluator','peer_state')}
 print(json.dumps(value));raise SystemExit
def event(t,**v): print(json.dumps(dict(type=t,**v)),flush=True)
with pathlib.Path(__file__).with_suffix('.calls').open('a') as f:f.write('model\n')
event('thread.started',thread_id='synthetic-'+uuid.uuid4().hex);event('turn.started')
mode=pathlib.Path('mode.txt').read_text()
if mode=='timeout':time.sleep(2)
pathlib.Path('answer.txt').write_text('0' if mode=='failed' else '42')
event('turn.completed',usage={'input_tokens':3,'output_tokens':1})
'''
GRADER = r'''
import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
from agent_harness.strict_yaml import load
value=load(sys.argv[2]);outcome=value['solve_outcome']
if outcome in ('completed','failed'):
 outcome='accepted' if pathlib.Path(value['cwd'],'answer.txt').read_text()=='42' else 'failed'
print(json.dumps({'schema_version':1,'record_type':'independent_grade','manifest_digest':value['manifest_digest'],
 'trial_id':value['trial_id'],'evaluator_digest':value['evaluator_digest'],'outcome':outcome}))
'''


class FixtureAdapter(CodexBenchmarkAdapter):
    def __init__(self, root, *, kind="synthetic"):
        self.root, self.kind, self.modes, self.bound = root, kind, {}, 4
        self.wall = 10
        self.phase_deadlines = []
        self.fake = root / "fake.py"
        self.fake.write_text(FAKE)
        self.grader = root / "grader.py"
        self.grader.write_text(GRADER)
        self.prepared_calls = []

    def identity(self):
        return {"adapter_id": "contained-fixture", "build_digest": hashlib.sha256(self.fake.read_bytes()).hexdigest(),
            "boundary_digest": digest([str(self.fake), self.kind, self.modes, self.bound, self.wall]), "measurement_kind": self.kind,
            "model": "fixture-model", "provider": "fixture-route", "client_version": "fixture-client"}

    def control_argv(self, challenge, *, deadline=None):
        self.phase_deadlines.append(("control", deadline))
        return [sys.executable, str(self.fake), "--control", json.dumps(challenge)]

    def estimate_trial_tokens(self, row, manifest, assets_root):
        return self.bound

    def prepare(self, row, manifest, assets_root, trial_root, *, deadline=None):
        self.phase_deadlines.append(("prepare", deadline))
        self.prepared_calls.append(row["trial_id"])
        source = trial_root / "source"
        source.mkdir()
        (source / "answer.txt").write_text("0")
        (source / "mode.txt").write_text(self.modes.get(row["trial_id"], "accepted"))
        task = trial_root / "task.yaml"
        dump(task, {"schema_version": 1, "task": {"id": "fixture", "objective": "Solve this synthetic fixture", "allowed_write_scopes": ["answer.txt"]},
            "mode": "native", "memory": True, "ledger": "inform", "budgets": {"max_attempts": 1, "max_wall_seconds": self.wall,
                "turn_timeout_seconds": 1 if self.modes.get(row["trial_id"]) == "timeout" else 2},
            "acceptance": [{"id": "answer", "kind": "command", "required": True,
                "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('answer.txt').read_text()=='42'"], "timeout": 2}]})
        return {"schema_version": 1, "trial_id": row["trial_id"], "cwd": str(source), "contract_path": str(task),
            "root_policy_digest": manifest["root_policy"]["sha256"], "child_policy_digest": manifest["child_policy"]["sha256"],
            "token_upper_bound": self.bound}

    def codex(self, prepared, *, deadline=None):
        self.phase_deadlines.append(("backend", deadline))
        return CodexAdapter((sys.executable, str(self.fake)))

    def evaluator_argv(self, row, prepared, grade_input, *, deadline=None):
        self.phase_deadlines.append(("evaluator", deadline))
        return [sys.executable, str(self.grader), str(Path(__file__).parents[1] / "src"), str(grade_input)]

    def measurements(self, row, prepared, result, *, deadline=None):
        self.phase_deadlines.append(("measurements", deadline))
        return {"mandatory_nodes_skipped": 0, "duplicate_writes": 0, "recovery": "not_applicable",
            "useful_retrieval": None, "stale_reuse": 0, "hook_overhead_ms": None}


class Crash(BaseException):
    pass


class BenchmarkRunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "isolated-codex")})
        environment.start()
        self.addCleanup(environment.stop)
        self.adapter = FixtureAdapter(self.assets)
        def ref(name):
            raw = (self.assets / name).read_bytes()
            return {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}
        (self.assets / "asset.txt").write_text("synthetic fixture only; no official suite qualification")
        asset = ref("asset.txt")
        self.manifest = {"schema_version": 1, "record_type": "benchmark_manifest", "comparison": "package", "seed": 9,
            "repeats": 1, "bootstrap_resamples": 100, "confidence": .95, "minimum_token_gain": .05, "minimum_quality_delta": 0,
            "model": "fixture-model", "provider": "fixture-route", "client_version": "fixture-client",
            "root_policy": asset, "child_policy": asset, "isolation": asset, "approved_budget": asset,
            "suites": [{"id": name, "revision": "fixture-v1", "assets": asset, "evaluator": ref("grader.py"),
                "adapter": ref("fake.py"), "toolchain": asset, "tasks": [{"id": "task", "source": asset}]} for name in evaluation.SUITES]}

    def runner(self, *, tokens=40, kind="synthetic", seconds=300):
        grant = BudgetAuthorization(digest(self.manifest), "explicit-test-caller", tokens,
            (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(), kind)
        return BenchmarkRunner.create(self.manifest, assets_root=self.assets, run_dir=self.root / "run", adapter=self.adapter, authorization=grant)

    def calls(self):
        path = self.adapter.fake.with_suffix(".calls")
        return path.read_text().splitlines() if path.exists() else []

    def test_full_cohort_executes_existing_engines_and_never_replays_completed_trials(self):
        runner = self.runner()
        result = runner.run()
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual((result["planned_trials"], result["completed_trials"]), (10, 10))
        self.assertEqual(result["usage"]["observed_tokens"], 40)
        self.assertEqual(len(self.calls()), 10)
        roots = list((self.root / "run" / "trials").iterdir())
        self.assertEqual(sum((path / "supervisor").exists() for path in roots), 5)
        for _ in range(2):
            self.assertEqual(BenchmarkRunner(self.root / "run", self.adapter).run()["status"], "complete")
        self.assertEqual(len(self.calls()), 10)
        report = runner.report()
        self.assertEqual(report["measurement_kind"], "synthetic")
        self.assertEqual(report["status"], "complete")
        self.adapter.fake.write_text(FAKE + "\n# changed after completion\n")
        self.assertEqual(runner.status()["status"], "blocked")
        self.assertEqual(len(self.calls()), 10)

    def test_no_manifest_file_or_boolean_grants_budget_authority(self):
        (self.assets / "asset.txt").write_text("approved: true")
        with self.assertRaisesRegex(HarnessError, "EXPLICIT_CALLER_AUTHORIZATION"):
            BenchmarkRunner.create(self.manifest, assets_root=self.assets, run_dir=self.root / "run", adapter=self.adapter, authorization=True)
        self.assertEqual(self.calls(), [])

    def test_unregistered_observed_transport_refuses_before_model_or_control_calls(self):
        self.adapter.kind = "observed"
        result = self.runner(kind="observed").run()
        self.assertEqual(result["reason_code"], "ENVIRONMENT_ISOLATION_UNAVAILABLE")
        self.assertEqual(self.calls(), [])
        self.assertEqual(production_status()["model_calls"], 0)

    def test_full_frozen_estimate_must_fit_before_first_dispatch(self):
        result = self.runner(tokens=39).run()
        self.assertEqual(result["reason_code"], "BENCH_COHORT_EXCEEDS_APPROVED_TOTAL")
        self.assertEqual(result["planned_trials"], 10)
        self.assertEqual(result["completed_trials"], 0)
        self.assertEqual(self.calls(), [])

    def test_unknown_upper_bounds_do_not_claim_a_hard_budget_cap(self):
        self.adapter.bound = None
        result = self.runner().run()
        self.assertEqual(result["reason_code"], "BENCH_COHORT_ESTIMATE_UNAVAILABLE")
        self.assertFalse(result["usage"]["hard_cap_supported"])
        self.assertEqual(self.calls(), [])

    def test_solve_artifact_reconciles_crash_before_projection_without_new_call(self):
        runner = self.runner()
        original = runner._record
        def crash(kind, *args, **kwargs):
            if kind == "solve":
                raise Crash()
            return original(kind, *args, **kwargs)
        with patch.object(runner, "_record", crash), self.assertRaises(Crash):
            runner.advance()
        self.assertEqual(len(self.calls()), 1)
        result = BenchmarkRunner(self.root / "run", self.adapter).advance()
        self.assertEqual(result["completed_trials"], 1, result)
        self.assertEqual(len(self.calls()), 1)

    def test_unknown_dispatch_never_blindly_restarts(self):
        runner = self.runner()
        with patch.object(runner, "_solve", side_effect=Crash), self.assertRaises(Crash):
            runner.advance()
        for _ in range(2):
            result = BenchmarkRunner(self.root / "run", self.adapter).advance()
            self.assertEqual(result["reason_code"], "BENCH_UNKNOWN_SOLVE_OUTCOME")
            self.assertEqual(len(result["unknown_outcome_trial_ids"]), 1)
        self.assertEqual(self.calls(), [])

    def test_failed_trial_retained_and_matched_arms_are_distinct(self):
        self.manifest["comparison"] = "matched"
        first = evaluation.schedule(self.manifest)[0]
        self.adapter.modes[first["trial_id"]] = "failed"
        runner = self.runner()
        result = runner.run()
        self.assertEqual(result["completed_trials"], 10, result)
        self.assertEqual(result["failed_trials"], [first["trial_id"]])
        self.assertEqual({row["arm"] for row in runner.rows}, {"plain_matched", "harness_matched"})
        self.assertEqual(result["planned_trials"], 10)
        self.assertEqual(len(result["never_started_trial_ids"]), 0)
        self.assertEqual(len(self.calls()), 10)

    def test_stale_qualification_nonce_and_identity_drift_refuse_dispatch(self):
        runner = self.runner()
        original = self.adapter.control_argv
        def stale(challenge):
            return original({**challenge, "nonce": "old-delivery"})
        with patch.object(self.adapter, "control_argv", stale):
            self.assertEqual(runner.advance()["reason_code"], "BENCH_STALE_QUALIFICATION")
        self.adapter.fake.write_text(FAKE + "\n# changed backend\n")
        self.assertEqual(runner.advance()["reason_code"], "BENCH_STALE_ASSET")
        self.assertEqual(self.calls(), [])

    def test_timeout_and_unknown_cost_are_retained_and_stop_new_model_work(self):
        first = evaluation.schedule(self.manifest)[0]
        self.adapter.modes[first["trial_id"]] = "timeout"
        runner = self.runner()
        result = runner.run()
        self.assertEqual(result["planned_trials"], 10)
        self.assertEqual(result["completed_trials"], 1, result)
        self.assertEqual(result["failed_trials"], [first["trial_id"]])
        self.assertEqual(result["reason_code"], "BENCH_USAGE_COVERAGE_UNKNOWN")
        self.assertEqual(result["usage"]["covered_invocation_ids"], [])
        self.assertEqual(len(result["usage"]["unknown_invocation_ids"]), 1)
        self.assertFalse(result["budget_verified"])
        self.assertEqual(len(self.calls()), 1)
        trial = next(r["payload"] for r in runner.records() if r["kind"] == "trial")
        self.assertEqual(trial["outcome"], "timeout")
        self.assertIsNone(trial["time_to_accepted_seconds"])

    def test_telemetry_failure_never_erases_completed_usage_or_restarts_solve(self):
        runner = self.runner()
        with patch.object(self.adapter, "measurements", side_effect=HarnessError("BENCH_MEASUREMENTS_UNAVAILABLE")):
            result = runner.advance()
        self.assertEqual(result["reason_code"], "BENCH_MEASUREMENTS_UNAVAILABLE")
        self.assertEqual(result["usage"]["observed_tokens"], 4)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(runner.advance()["completed_trials"], 1)
        self.assertEqual(len(self.calls()), 1)

    def test_unknown_grade_intent_does_not_replay_solver_or_evaluator(self):
        runner = self.runner()
        with patch.object(self.adapter, "evaluator_argv", side_effect=Crash), self.assertRaises(Crash):
            runner.advance()
        result = BenchmarkRunner(self.root / "run", self.adapter).advance()
        self.assertEqual(result["reason_code"], "BENCH_UNKNOWN_GRADE_OUTCOME")
        self.assertEqual(len(result["pending_grade_trial_ids"]), 1)
        self.assertEqual(result["usage"]["observed_tokens"], 4)
        self.assertEqual(len(self.calls()), 1)

    def test_solve_artifact_is_bound_to_exact_execution_and_trial(self):
        runner = self.runner()
        original = runner._record
        def crash(kind, *args, **kwargs):
            if kind == "solve":
                raise Crash()
            return original(kind, *args, **kwargs)
        with patch.object(runner, "_record", crash), self.assertRaises(Crash):
            runner.advance()
        path = next((self.root / "run" / "trials").glob("*/solve.yaml"))
        from agent_harness.strict_yaml import load
        artifact = load(path)
        artifact["execution_id"] = "different-execution-with-identical-result"
        dump(path, artifact)
        result = runner.advance()
        self.assertEqual(result["reason_code"], "BENCH_SOLVE_ARTIFACT_BINDING_MISMATCH")
        self.assertEqual(len(self.calls()), 1)

    def test_receipt_replay_noop_and_same_id_conflict(self):
        runner = self.runner()
        runner.advance()
        receipt = next(r for r in runner.records() if r["kind"] == "solve")
        events = len(runner.store.events())
        with runner.store.lease("synthetic duplicate delivery"):
            self.assertEqual(runner._record("solve", receipt["trial_id"], receipt["payload"], record_id=receipt["record_id"]), receipt)
            with self.assertRaisesRegex(HarnessError, "RECEIPT_ID_CONFLICT"):
                runner._record("solve", receipt["trial_id"], {**receipt["payload"], "elapsed_seconds": 99.}, record_id=receipt["record_id"])
        self.assertEqual(len(runner.store.events()), events)
        self.assertEqual(runner.status()["usage"]["observed_tokens"], 4)

    def test_torn_journal_repair_marker_blocks_new_model_work(self):
        runner = self.runner()
        path = runner.store.path("journal/events.jsonl")
        with path.open("ab") as output:
            output.write(b'{"schema_version":')
        runner.store.repair_torn_tail()
        result = runner.advance()
        self.assertEqual(result["reason_code"], "BENCH_JOURNAL_RECOVERY_REQUIRED")
        self.assertEqual(self.calls(), [])

    def first_harness(self):
        for seed in range(100):
            self.manifest["seed"] = seed
            if evaluation.schedule(self.manifest)[0]["arm"].startswith("harness_"):
                return
        self.fail("No deterministic harness-first ordering")

    def test_original_deadline_expiring_during_prepare_never_dispatches(self):
        self.first_harness()
        runner = self.runner(seconds=.3)
        original = self.adapter.prepare
        def delayed(*args):
            time.sleep(.5)
            return original(*args)
        with patch.object(self.adapter, "prepare", delayed):
            result = runner.advance()
        self.assertEqual(self.calls(), [], result)
        self.assertEqual(result["reason_code"], "BENCH_DEADLINE_EXHAUSTED")

    def test_prepared_receipt_pins_contract_and_source_before_dispatch(self):
        for mutation, code in (("contract", "BENCH_PREPARED_CONTRACT_CHANGED"), ("source", "BENCH_PREPARED_SOURCE_CHANGED")):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                # Separate frozen runs and scopes; the same adapter implementation.
                run_path = Path(temporary) / "run"
                grant = BudgetAuthorization(digest(self.manifest), "explicit-test-caller", 40,
                    (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), "synthetic")
                runner = BenchmarkRunner.create(self.manifest, assets_root=self.assets, run_dir=run_path, adapter=self.adapter, authorization=grant)
                original = runner._record
                def crash(kind, *args, **kwargs):
                    value = original(kind, *args, **kwargs)
                    if kind == "prepared":
                        raise Crash()
                    return value
                with patch.object(runner, "_record", crash), self.assertRaises(Crash):
                    runner.advance()
                prepared = next(r["payload"] for r in runner.records() if r["kind"] == "prepared")
                if mutation == "contract":
                    from agent_harness.strict_yaml import load
                    contract = load(prepared["contract_path"])
                    contract["task"]["objective"] = "DIFFERENT TASK"
                    dump(prepared["contract_path"], contract)
                else:
                    Path(prepared["cwd"], "answer.txt").write_text("unapproved changed input")
                result = BenchmarkRunner(run_path, self.adapter).advance()
                self.assertEqual(result["reason_code"], code, result)
                self.assertEqual(self.calls(), [])

    def test_expiry_during_control_argv_never_launches_control_or_model(self):
        runner = self.runner(seconds=.3)
        original = self.adapter.control_argv
        def delayed(challenge):
            time.sleep(.5)
            return original(challenge)
        with patch.object(self.adapter, "control_argv", delayed), patch.object(benchmark_runner, "_contained_capture", wraps=benchmark_runner._contained_capture) as capture:
            result = runner.advance()
        self.assertEqual(result["reason_code"], "BENCH_DEADLINE_EXHAUSTED")
        self.assertEqual(capture.call_count, 0)
        self.assertEqual(self.calls(), [])

    def test_control_process_timeout_is_remaining_original_grant(self):
        runner = self.runner(seconds=.3)
        with patch.object(self.adapter, "control_argv", return_value=[sys.executable, "-c", "import time; time.sleep(2)"]):
            started = time.monotonic()
            result = runner.advance()
        self.assertEqual(result["reason_code"], "BENCH_DEADLINE_EXHAUSTED")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.calls(), [])

    def test_harness_uses_original_grant_and_all_external_actions_are_bounded(self):
        self.first_harness()
        self.adapter.wall = 60
        runner = self.runner(seconds=30)
        original_start, original_capture = Supervisor.start, benchmark_runner._contained_capture
        deadlines, timeouts = [], []
        def start(supervisor):
            deadlines.append(supervisor.state["deadline"])
            return original_start(supervisor)
        def capture(argv, **options):
            timeouts.append((options["timeout"], runner._remaining()))
            self.assertLessEqual(options["timeout"], runner._remaining() + .02)
            return original_capture(argv, **options)
        with patch.object(Supervisor, "start", start), patch.object(benchmark_runner, "_contained_capture", capture), patch("agent_harness.codex_execution._contained_capture", capture):
            result = runner.advance()
        self.assertEqual(result["completed_trials"], 1, result)
        self.assertEqual(deadlines, [runner.state["authorization"]["deadline"]])
        self.assertGreaterEqual(len(timeouts), 5)  # qualification, three probes, independent evaluator
        self.assertTrue(all(deadline == runner.state["authorization"]["deadline"] for phase, deadline in self.adapter.phase_deadlines if phase != "backend"))
        self.assertEqual(len(self.calls()), 1)

    def test_capability_probe_deadline_checked_before_every_probe(self):
        from agent_harness import codex_execution
        backend = self.adapter.codex({})
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=.15)).isoformat()
        def delayed(*args, **kwargs):
            time.sleep(.2)
            return type("Result", (), {"returncode": 0, "stdout": b"fixture-client"})()
        with patch.object(codex_execution, "_contained_capture", side_effect=delayed) as capture:
            result = backend.capabilities(deadline=deadline)
        self.assertEqual(result["reason"], "deadline_exhausted")
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.calls(), [])

    def test_expiry_during_grade_builder_preserves_spend_without_late_grader(self):
        runner = self.runner(seconds=5)
        original = self.adapter.evaluator_argv
        captures = []
        capture = benchmark_runner._contained_capture
        def build(*args):
            time.sleep(max(0, runner._remaining()) + .03)
            return original(*args)
        def record(argv, **kwargs):
            captures.append(argv)
            return capture(argv, **kwargs)
        with patch.object(self.adapter, "evaluator_argv", build), patch.object(benchmark_runner, "_contained_capture", record):
            result = runner.advance()
        self.assertEqual(result["reason_code"], "BENCH_DEADLINE_EXHAUSTED")
        self.assertEqual(result["usage"]["observed_tokens"], 4)
        self.assertEqual(len(result["pending_grade_trial_ids"]), 1)
        self.assertFalse(any(str(self.adapter.grader) in argv for argv in captures))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(runner.advance()["reason_code"], "BENCH_DEADLINE_EXHAUSTED")
        self.assertEqual(len(self.calls()), 1)

    def test_changed_output_after_solve_blocks_grade_but_keeps_completion_usage(self):
        runner = self.runner()
        original = runner._record
        def crash(kind, *args, **kwargs):
            if kind == "solve":
                raise Crash()
            return original(kind, *args, **kwargs)
        with patch.object(runner, "_record", crash), self.assertRaises(Crash):
            runner.advance()
        prepared = next(r["payload"] for r in runner.records() if r["kind"] == "prepared")
        Path(prepared["cwd"], "answer.txt").write_text("changed after model completion")
        result = BenchmarkRunner(self.root / "run", self.adapter).advance()
        self.assertEqual(result["reason_code"], "BENCH_PREPARED_SOURCE_CHANGED", result)
        self.assertEqual(result["usage"]["observed_tokens"], 4)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(result["completed_trials"], 0)
