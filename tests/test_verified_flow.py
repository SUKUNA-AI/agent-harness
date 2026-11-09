"""Verified queues, lifecycle crashes and transport, using contained fixtures."""
from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, digest
from agent_harness.strict_yaml import dump, load
from agent_harness.supervisor import Supervisor
from agent_harness.scheduling import validate_plan
from agent_harness.task_packets import compile_packet, preflight, NotDispatched
from agent_harness import verified_flow
import test_execution_durability as durability
Crash = durability.Crash

PLAN_QUALITY_FIXTURE = """if 'HARNESS_PLAN_QUALITY' in prompt:
 quality_packet=json.loads(prompt.rsplit('\\n',1)[1]);quality_plan=quality_packet['input']['plan']
 quality_verdict='approved';quality_findings=[];quality_nodes=[]
 for quality_node in quality_plan['tasks']:
  quality_nodes.append(dict(node_id=quality_node['id'],outcome=quality_node['goal'][:1500],requirement_ids=quality_node['requirement_ids'],depends_on=quality_node['depends_on'],dependency_rationale='Local acceptance uses only already available prerequisites.',dependency_valid=True,atomicity='coherent',atomicity_rationale='One observable outcome with matching local and integrated assertions.',indivisibility_condition='The scoped behavior and its checks form one coherent outcome.',owner=quality_node['id'],scope_rationale='One frozen owner for the listed write scope.',local_checks=quality_node['checks']['local'],integration_checks=quality_node['checks']['integration'],local_check_rationale='Assert the actual local output.',integration_check_rationale='Assert the merged output independently.',checks_meaningful=True,review_handoff='Local and integrated checks passed on the bound candidate.',budget_rationale='Original attempts, turn timeout and deadline remain bounded.',shared_interfaces=[]))
 quality_wire=dict(schema_version=1,**quality_packet['binding'],verdict=quality_verdict,findings=quality_findings,nodes=quality_nodes)
 event('item.completed',item={'type':'agent_message','text':json.dumps(quality_wire)})
 event('turn.completed',usage={'input_tokens':10,'output_tokens':3});raise SystemExit
"""


def legacy_verified_creation(supervisor, material):
    """Test-only frozen v2 creation: no quality artifacts/pins are published."""
    from agent_harness.completion_eligibility import pin
    supervisor.store.write('contract.yaml', supervisor.contract, 'work-contract.schema.json', exclusive=True)
    pin(supervisor)
    supervisor._save('fixture frozen v2 creation; no implicit quality upgrade')


VERIFIED_FLOW_WALL_SECONDS = 300
# Two-phase fixtures exercise an initial flow and one recovery or repair flow
# under a single persisted deadline. Each phase can use the single-flow budget.
TWO_PHASE_WALL_SECONDS = 2 * VERIFIED_FLOW_WALL_SECONDS


class VerifiedFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex-home")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def fixture(self, *, verified=True, attempts=3, script_edit=None, plan_edit=None, contract_edit=None,
                planning_rounds=None, legacy=False, wall_seconds=VERIFIED_FLOW_WALL_SECONDS):
        supervisor, fake = durability.ExecutionDurabilityTests().fixture(self.root)
        contract = deepcopy(supervisor.contract)
        plan_path = self.root / "plan.yaml"
        plan = load(plan_path, "agent-plan.schema.json")
        contract["budgets"].update(max_wall_seconds=wall_seconds, max_attempts=attempts)
        for n in plan["tasks"]:
            n.update(max_attempts=attempts, max_wall_seconds=wall_seconds)
        if verified:
            contract["schema_version"] = 2
            contract["memory"] = True
            contract["mission"] = {"id": "fixture-mission", "generation": 1,
                "original_request": "Repair left and right, retain API; independently check both before opening dependents.",
                "global_constraints": ["Do not change the public API", "Do not publish"], "non_goals": [],
                "requirements": [{"id": "R-" + name, "text": "Repair " + name,
                    "provenance": "original request: " + name, "kind": "behavior", "required": True,
                    "acceptance_ids": ["combined"]} for name in ("left", "right")]}
            contract["node_checks"] = [{"id": name + "-value", "kind": "command", "required": True,
                "argv": [sys.executable, "-c", "from pathlib import Path; assert 'VALUE = 42' in Path('" + name + ".py').read_text()"],
                "timeout": 15} for name in ("left", "right")]
            plan.update(schema_version=2, generation=1)
            for n in plan["tasks"]:
                n.update(requirement_ids=["R-" + n["id"]], checks={"local": [n["id"] + "-value"], "integration": [n["id"] + "-value"]})
        if plan_edit:
            plan_edit(plan)
        if contract_edit:
            contract_edit(contract)
        if verified:
            plan["mission_digest"] = digest(contract["mission"])
        dump(self.root / "task-v2.yaml", contract)
        dump(plan_path, plan)
        # Reuse only the synthetic source/adapter, with a distinct new run.
        with patch('agent_harness.plan_quality.publish_creation', legacy_verified_creation) if legacy else nullcontext():
            supervisor = Supervisor.create(self.root / "task-v2.yaml", run_dir=self.root / "private" / "verified",
                cwd=self.root / "source", state_dir=self.root / "state", adapter=supervisor.adapter, max_planning_rounds=planning_rounds)
        supervisor.plan_input = str(plan_path)
        script = fake.read_text()
        script = script.replace("prompt=args[-1]", """prompt=args[-1]
manifest=json.loads(prompt.rsplit('\\n',1)[1]) if 'required_local_file_parts_v1' in prompt else None
if manifest:
 import hashlib
 raw=b''.join(pathlib.Path(p['path']).read_bytes() for p in manifest['parts'])
 assert hashlib.sha256(raw).hexdigest()==manifest['packet']['sha256']
 prompt=prompt.rsplit('\\n',1)[0]+'\\n'+raw.decode('utf-8')
""")
        script = script.replace("task['goal']", "task['goal'].split()[0]")
        if verified:
            script = script.replace("json.dumps({'schema_version':1,'verdict':'approved','findings':[]})", "json.dumps({'schema_version':json.loads(prompt.rsplit('\\n',1)[1])['required_verdict_version'],'verdict':'approved','findings':[], 'requirements':[dict({'id':r['id'],'status':'deferred' if r['status']=='deferred' else 'accepted','evidence_ids':r['evidence_ids']}, **({'reason':r['reason']} if 'reason' in r else {})) for r in json.loads(prompt.rsplit('\\n',1)[1])['coverage']], 'unrepresented_requirements':[]})")
            script = script.replace("if 'HARNESS_FINAL_REVIEW' in prompt:\n", PLAN_QUALITY_FIXTURE + "if 'HARNESS_FINAL_REVIEW' in prompt:\n")
        if script_edit:
            script = script_edit(script)
        fake.write_text(script)
        return supervisor, fake

    def test_verified_dependency_gate_and_final_coverage_are_current(self):
        supervisor, fake = self.fixture()
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertTrue(supervisor.status(verify=True)["evidence_current"])
        records = supervisor.receipts().records()
        right_intent = next(i for i, r in enumerate(records) if r["record_type"] == "invocation_intent" and r["node_id"] == "right")
        self.assertTrue(any(r["record_type"] == "node_acceptance" and r["node_id"] == "left" for r in records[:right_intent]))
        self.assertTrue(any(r["record_type"] == "coverage" for r in records))
        self.assertTrue(all(r["status"] == "accepted" for r in result["progress"]["requirements"]))
        calls = fake.with_suffix(".calls").read_bytes()
        self.assertEqual(Supervisor(supervisor.path, adapter=supervisor.adapter).resume()["status"], "ACCEPTED")
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)

    def test_omitted_requirement_or_unknown_check_blocks_before_writes(self):
        supervisor, fake = self.fixture(plan_edit=lambda p: p["tasks"].pop())
        result = supervisor.start()
        self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
        self.assertFalse(fake.with_suffix(".calls").exists())

    def test_version_barrier_prevents_legacy_plan_claiming_verification(self):
        supervisor, _ = self.fixture()
        plan = load(self.root / "plan.yaml")
        plan["schema_version"] = 1
        plan.pop("generation")
        with self.assertRaisesRegex(HarnessError, "version barrier"):
            validate_plan(plan, supervisor.contract)

    def test_failed_node_does_not_open_dependent_even_when_global_smoke_is_weak(self):
        supervisor, fake = self.fixture(attempts=1, script_edit=lambda s: s.replace("if mode in ('left','right'):target=mode+'.py'", "if mode in ('left','right'):target=mode+'.py'\nif mode=='left':value=0"))
        result = supervisor.start()
        self.assertEqual(result["status"], "FAILED_TERMINAL", result)
        self.assertFalse(any(r["node_id"] == "right" and r["record_type"] == "invocation_intent" for r in supervisor.receipts().records()))
        self.assertEqual(fake.with_suffix(".calls").read_text().splitlines(), ["exec", "exec"])

    def test_local_check_failure_gets_bounded_repair_same_task(self):
        def edit(script):
            return script.replace("if mode in ('left','right'):target=mode+'.py'", "if mode in ('left','right'):target=mode+'.py'\nif mode=='left' and 'resume' not in args:value=0")
        supervisor, fake = self.fixture(script_edit=edit)
        deadline = supervisor.state["deadline"]
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(supervisor.state["deadline"], deadline)
        self.assertEqual(supervisor.store.read("agents/left.yaml")["attempts_used"], 2)
        self.assertIn("resume", fake.with_suffix(".calls").read_text())

    def test_known_stopped_worker_failure_is_retryable_without_unknown_dispatch(self):
        def edit(script):
            return script.replace("if mode=='permission':", "if mode=='left' and 'resume' not in args:\n event('turn.failed',error={'message':'transient fixture failure'});raise SystemExit(1)\nif mode=='permission':")
        supervisor, _ = self.fixture(verified=False, script_edit=edit)
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(supervisor.store.read("agents/left.yaml")["attempts_used"], 2)

    def test_required_part_manifest_preserves_all_utf8_bytes(self):
        supervisor, _ = self.fixture(verified=False)
        payload = {"goal": "Ж" * 15000, "global_constraint": "KEEP-THIS-TAIL"}
        prompt = compile_packet(supervisor, "Required task", payload)
        self.assertLessEqual(len(prompt.encode("utf-8")), 24000)
        manifest = json.loads(prompt.rsplit("\n", 1)[1])
        raw = b"".join(Path(p["path"]).read_bytes() for p in manifest["parts"])
        self.assertEqual(json.loads(raw), payload)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest["packet"]["sha256"])

    def test_preflight_oversize_creates_no_slot_intent_or_model_call(self):
        supervisor, _ = self.fixture(verified=False)
        with self.assertRaises(NotDispatched):
            preflight(supervisor.adapter, "Ж" * 12001)
        self.assertFalse((self.root / "state" / "global-model-slots").exists())
        self.assertEqual(supervisor.receipts().records(), [])

    def test_large_aggregate_final_review_uses_complete_reference_packet(self):
        def edit(plan):
            for n in plan["tasks"]:
                n["goal"] += " " + "requirement " * 1200
        supervisor, _ = self.fixture(plan_edit=edit)
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertTrue(list((supervisor.path / "context").glob("required-*.part")))

    def test_crash_wave_has_prepared_agent_and_resume_does_not_lose_node(self):
        supervisor, _ = self.fixture(verified=False)
        original = supervisor._save
        def save(reason):
            result = original(reason)
            if reason.startswith("spawn ready wave"):
                raise Crash()
            return result
        with patch.object(supervisor, "_save", save), self.assertRaises(Crash):
            supervisor.start()
        self.assertEqual(supervisor.store.read("agents/left.yaml")["status"], "prepared")
        result = Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
        self.assertEqual(result["status"], "ACCEPTED", result)

    def test_legacy_missing_never_dispatched_projection_is_reconstructed(self):
        supervisor, _ = self.fixture(verified=False)
        original = supervisor._save
        def save(reason):
            result = original(reason)
            if reason.startswith("spawn ready wave"):
                supervisor.store.path("agents/left.yaml").unlink()
                raise Crash()
            return result
        with patch.object(supervisor, "_save", save), self.assertRaises(Crash):
            supervisor.start()
        result = Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
        self.assertEqual(result["status"], "ACCEPTED", result)

    def test_negative_final_original_requirement_audit_blocks_acceptance(self):
        supervisor, _ = self.fixture(script_edit=lambda s: s.replace("'unrepresented_requirements':[]", "'unrepresented_requirements':['missing original deliverable']"))
        result = supervisor.start()
        self.assertNotEqual(result["status"], "ACCEPTED", result)
        self.assertIsNone(supervisor._core().state()["accepted"])

    def test_changed_integrated_source_invalidates_requirement_frontier(self):
        supervisor, _ = self.fixture(wall_seconds=TWO_PHASE_WALL_SECONDS)
        deadline = supervisor.state["deadline"]
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        (Path(result["cwd"]) / "left.py").write_text("VALUE = 3\n")
        status = supervisor.status(verify=True)
        self.assertEqual(status["status"], "STALE")
        self.assertEqual(supervisor.state["deadline"], deadline)
        self.assertFalse(status["progress"]["execution_complete"])

    def test_integrated_ancestor_regression_allows_only_responsible_repair(self):
        def contract(value):
            value["node_checks"][0]["argv"][-1] += "; assert 'VALUE = 42' not in Path('right.py').read_text()"
            value["node_checks"][1]["argv"][-1] = "from pathlib import Path; assert any('VALUE = '+str(v) in Path('right.py').read_text() for v in (42,43))"
        def script(value):
            return value.replace("if mode in ('left','right'):target=mode+'.py'", "if mode in ('left','right'):target=mode+'.py'\nif mode=='right':\n marker=pathlib.Path(__file__).with_suffix('.right-seen');value=43 if marker.exists() else 42;marker.write_text('seen')")
        supervisor, fake = self.fixture(contract_edit=contract, script_edit=script,
                                        wall_seconds=TWO_PHASE_WALL_SECONDS)
        deadline = supervisor.state["deadline"]
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(supervisor.store.read("agents/right.yaml")["attempts_used"], 2)
        self.assertIn("VALUE = 43", (Path(result["cwd"]) / "right.py").read_text())
        self.assertEqual(supervisor.state["deadline"], deadline)

    def test_independent_integration_regression_repairs_responsible_writer(self):
        # The same cross-file regression must be checked without a dependency
        # edge forcing traversal of the earlier node's accepted evidence.
        fixture = self.fixture
        created = []
        def independent(**options):
            options["plan_edit"] = lambda plan: plan["tasks"][1].update(depends_on=[])
            result = fixture(**options)
            created.append(result[0])
            return result
        with patch.object(self, "fixture", independent):
            self.test_integrated_ancestor_regression_allows_only_responsible_repair()
        self.assertEqual(created[0].store.read("agents/left.yaml")["attempts_used"], 1)

    def test_mid_wave_repair_preserves_later_completed_writer(self):
        def contract(value):
            value["budgets"]["max_concurrent_managed_agents"] = 3
            value["task"]["allowed_write_scopes"].append("zzobserver.py")
            value["mission"]["original_request"] += " Also create the observer output."
            requirement = deepcopy(value["mission"]["requirements"][0])
            requirement.update(id="R-zzobserver", text="Create the observer output")
            value["mission"]["requirements"].append(requirement)
            check = deepcopy(value["node_checks"][1])
            check.update(id="observer-value", argv=[sys.executable, "-c",
                "from pathlib import Path; assert 'VALUE = 42' in Path('zzobserver.py').read_text()"])
            value["node_checks"].append(check)
            value["node_checks"][0]["argv"][-1] += "; assert 'VALUE = 42' not in Path('right.py').read_text()"
            value["node_checks"][1]["argv"][-1] = "from pathlib import Path; assert any('VALUE = '+str(v) in Path('right.py').read_text() for v in (42,43))"
        def plan(value):
            value["tasks"][0]["estimated_seconds"] = 90
            value["tasks"][1].update(depends_on=[], estimated_seconds=60)
            observer = deepcopy(value["tasks"][1])
            observer.update(id="zzobserver", goal="zzobserver", write_scope=["zzobserver.py"],
                outputs=["zzobserver.py"], requirement_ids=["R-zzobserver"], estimated_seconds=30,
                checks={"local": ["observer-value"], "integration": ["observer-value"]})
            value["tasks"].append(observer)
        def script(value):
            return value.replace("if mode in ('left','right'):target=mode+'.py'",
                "if mode in ('left','right','zzobserver'):target=mode+'.py'\nif mode=='right':\n marker=pathlib.Path(__file__).with_suffix('.right-seen');value=43 if marker.exists() else 42;marker.write_text('seen')")
        supervisor, fake = self.fixture(contract_edit=contract, plan_edit=plan, script_edit=script,
                                        wall_seconds=TWO_PHASE_WALL_SECONDS)
        deadline = supervisor.state["deadline"]
        capabilities = supervisor.adapter.capabilities
        with patch.object(supervisor.adapter, "capabilities", lambda: dict(capabilities(), native_children_disabled=True)):
            result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(supervisor.state["deadline"], deadline)
        for name, attempts in (("left", 1), ("right", 2), ("zzobserver", 1)):
            self.assertEqual(supervisor.store.read("agents/" + name + ".yaml")["attempts_used"], attempts)
        records = supervisor.receipts().records()
        observer_completions = [i for i, row in enumerate(records) if row["record_type"] == "completion" and row["node_id"] == "zzobserver"]
        observer_integrations = [i for i, row in enumerate(records) if row["record_type"] == "integration" and row["node_id"] == "zzobserver"]
        right_integrations = [i for i, row in enumerate(records) if row["record_type"] == "integration" and row["node_id"] == "right"]
        self.assertEqual((len(observer_completions), len(observer_integrations), len(right_integrations)), (1, 1, 2))
        self.assertLess(observer_completions[0], right_integrations[0])
        self.assertGreater(observer_integrations[0], right_integrations[-1])
        self.assertEqual(len(fake.with_suffix(".calls").read_text().splitlines()),
            sum(row["record_type"] == "invocation_intent" for row in records))

    def test_review_coverage_publication_crashes_reconcile_without_model_replay(self):
        from agent_harness.execution_receipts import ExecutionReceipts
        supervisor, fake = self.fixture()
        real = ExecutionReceipts.publish
        def publish(receipts, record):
            result = real(receipts, record)
            if record["record_type"] == "coverage":
                raise Crash()
            return result
        with patch.object(ExecutionReceipts, "publish", publish), self.assertRaises(Crash):
            supervisor.start()
        before = fake.with_suffix(".calls").read_bytes()
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        self.assertEqual(recovered.resume()["status"], "ACCEPTED")
        self.assertEqual(before, fake.with_suffix(".calls").read_bytes())
        self.assertTrue(any(r["record_type"] == "coverage" for r in recovered.receipts().records()))

    def test_runtime_adapter_exception_is_unknown_and_slot_cannot_be_released(self):
        supervisor, _ = self.fixture()
        with patch.object(supervisor.adapter, "run", side_effect=RuntimeError("possible effect, callback lost")):
            result = supervisor.start()
        self.assertEqual(result["status"], "UNKNOWN_OUTCOME", result)
        completion = next(r for r in supervisor.receipts().records() if r["record_type"] == "completion")
        self.assertEqual(completion["payload"]["dispatch_state"], "possibly_dispatched")
        self.assertIsNone(completion["payload"]["process_tree_stopped"])
        with self.assertRaisesRegex(HarnessError, "uncertain writers"):
            Supervisor(supervisor.path, adapter=supervisor.adapter).resume()

    def test_cross_requirement_final_evidence_is_rejected(self):
        def edit(script):
            return script.replace("'evidence_ids':r['evidence_ids']", "'evidence_ids':json.loads(prompt.rsplit('\\n',1)[1])['coverage'][0]['evidence_ids']")
        supervisor, _ = self.fixture(script_edit=edit)
        self.assertNotEqual(supervisor.start()["status"], "ACCEPTED")

    def test_corrupt_required_part_refuses_before_slot_or_intent(self):
        supervisor, _ = self.fixture(verified=False)
        prompt = compile_packet(supervisor, "Required task", {"goal": "Ж" * 15000})
        manifest = json.loads(prompt.rsplit("\n", 1)[1])
        Path(manifest["parts"][0]["path"]).write_bytes(b"discarded requirements")
        with self.assertRaisesRegex(NotDispatched, "changed"):
            preflight(supervisor.adapter, prompt)
        self.assertFalse((self.root / "state" / "global-model-slots").exists())
        self.assertEqual(supervisor.receipts().records(), [])

    def test_malformed_reviewer_node_is_known_validation_failure(self):
        def plan(value):
            value["tasks"][1].update(role="reviewer", read_only=True, write_scope=[])
        def script(value):
            value = value.replace("if mode!='managed':", "if mode not in ('managed','right'):")
            return value.replace("if mode=='partial':", "if mode=='right':event('item.completed',item={'type':'agent_message','text':'unstructured review'})\nif mode=='partial':")
        supervisor, _ = self.fixture(attempts=1, plan_edit=plan, script_edit=script)
        self.assertEqual(supervisor.start()["status"], "FAILED_TERMINAL")

    def test_explicit_mission_revision_preserves_budget_and_drops_old_acceptance(self):
        from agent_harness.core import digest
        from datetime import datetime
        # Two complete generations share one frozen deadline. Allow the real
        # verification/receipt work to finish; the assertions below still require
        # that mission revision never extends it or resets attempts/usage.
        original_wall_seconds = 900
        def original_node_budgets(plan):
            for node in plan["tasks"]:
                node["max_wall_seconds"] = original_wall_seconds
        supervisor, _ = self.fixture(
            contract_edit=lambda c: c["budgets"].update(max_wall_seconds=original_wall_seconds),
            plan_edit=original_node_budgets)
        self.assertEqual(supervisor.start()["status"], "ACCEPTED")
        old_deadline, old_attempts = supervisor.state["deadline"], supervisor.state["attempts_used"]
        original_paths = {name: supervisor.agent_path(name) for name in ("left", "right")}
        original_nodes = {name: supervisor.store.read(path) for name, path in original_paths.items()}
        root_deadline = datetime.fromisoformat(old_deadline.replace("Z", "+00:00"))
        for agent in original_nodes.values():
            node_deadline = datetime.fromisoformat(agent["deadline"].replace("Z", "+00:00"))
            self.assertLessEqual(node_deadline, root_deadline)
            self.assertEqual(node_deadline, root_deadline)  # min() selects the original root bound.
        usage = deepcopy(supervisor.state["metrics"])
        revised = deepcopy(supervisor.contract)
        revised["mission"].update(generation=2, previous_digest=digest(supervisor.contract["mission"]),
            original_request="Explicit updated request; retain all existing constraints")
        revised["mission"]["requirements"][0]["text"] += " and update docs"
        path = self.root / "mission-v2.yaml"
        dump(path, revised)
        result = supervisor.update_mission(path, reason="explicit user request for updated output")
        self.assertEqual(result["status"], "NEW")
        self.assertEqual(supervisor.state["deadline"], old_deadline)
        self.assertEqual(supervisor.state["attempts_used"], old_attempts)
        self.assertEqual(supervisor.state["metrics"]["input_tokens"], usage["input_tokens"])
        self.assertTrue(all(c["status"] == "pending" for c in supervisor.state["acceptance"]))
        for name, path in original_paths.items():
            self.assertEqual(supervisor.store.read(path), original_nodes[name])
        reloaded = Supervisor(supervisor.path, adapter=supervisor.adapter)
        reloaded.load(recover=True)
        self.assertEqual(reloaded.contract["mission"]["generation"], 2)
        self.assertIsNone(reloaded.state["plan_path"])
        old_plan = load(self.root / "plan.yaml")
        with self.assertRaisesRegex(HarnessError, "different mission"):
            validate_plan(old_plan, reloaded.contract)
        old_plan["mission_digest"] = digest(reloaded.contract["mission"])
        old_plan["generation"] += 1
        dump(self.root / "mission-plan.yaml", old_plan)
        reloaded.plan_input = str(self.root / "mission-plan.yaml")
        completed = reloaded.resume()
        self.assertEqual(completed["status"], "ACCEPTED", completed)
        for name, original in original_nodes.items():
            agent = reloaded.store.read(reloaded.agent_path(name))
            self.assertEqual(agent["deadline"], original["deadline"])
            self.assertLessEqual(datetime.fromisoformat(agent["deadline"].replace("Z", "+00:00")), root_deadline)
            self.assertEqual(agent["max_attempts"], original["max_attempts"])
            self.assertEqual(agent["attempts_used"], original["attempts_used"] + 1)
            self.assertEqual(reloaded.store.read(original_paths[name]), original)
        self.assertEqual(reloaded.store.read(reloaded.agent_path("left"))["attempts_used"], 2)
        self.assertEqual(reloaded.state["attempts_used"], old_attempts + 1)
        self.assertGreater(reloaded.state["metrics"]["input_tokens"], usage["input_tokens"])

    def revision(self, supervisor, **changes):
        revised = deepcopy(supervisor.contract)
        revised["mission"].update(generation=revised["mission"]["generation"] + 1,
            previous_digest=digest(supervisor.contract["mission"]))
        for key, value in changes.items():
            revised[key].update(value)
        path = self.root / "mission-revision.yaml"
        dump(path, revised)
        return path

    def test_readonly_status_uses_committed_mission_head_after_projection_crash(self):
        supervisor, _ = self.fixture()
        self.assertEqual(supervisor.start()["status"], "ACCEPTED")
        before = supervisor.store.path("state.yaml").read_bytes()
        path = self.revision(supervisor)
        real = supervisor.store.write
        def write(relative, *args, **kwargs):
            if relative == "state.yaml":
                raise Crash()
            return real(relative, *args, **kwargs)
        with patch.object(supervisor.store, "write", write), self.assertRaises(Crash):
            supervisor.update_mission(path, reason="authorized revision before crash")
        readonly = Supervisor(supervisor.path, adapter=supervisor.adapter)
        result = readonly.status(verify=True)
        self.assertEqual(result["status"], "NEW", result)
        self.assertEqual(readonly.contract["mission"]["generation"], 2)
        self.assertEqual(supervisor.store.path("state.yaml").read_bytes(), before)

    def test_lower_mission_wall_budget_tightens_original_deadline(self):
        supervisor, _ = self.fixture()
        old = supervisor.state["deadline"]
        supervisor.update_mission(self.revision(supervisor, budgets={"max_wall_seconds": 1}), reason="explicit stricter deadline")
        self.assertLess(supervisor.state["deadline"], old)
        self.assertLessEqual(supervisor.remaining(), 1)

    def test_unknown_completion_blocks_mission_update_without_slot_registry(self):
        supervisor, _ = self.fixture()
        intent = supervisor.receipts().intent("left", "left-1", checkout=supervisor.state["cwd"], owner="lost-slot")
        supervisor.receipts().completion(intent, {"status": "unknown_outcome", "process_tree_stopped": False})
        self.assertFalse((self.root / "state" / "global-model-slots").exists())
        with self.assertRaisesRegex(HarnessError, "unknown outcome"):
            supervisor.update_mission(self.revision(supervisor), reason="explicit revision cannot reconcile side effects")

    def test_mission_update_rejects_wrong_repository_pin(self):
        supervisor, _ = self.fixture()
        with self.assertRaisesRegex(HarnessError, "repository revision"):
            supervisor.update_mission(self.revision(supervisor, task={"repository_revision": "0" * 40}), reason="explicit revision")

    def test_independent_verified_lanes_integrate_current_proofs(self):
        supervisor, _ = self.fixture(plan_edit=lambda p: p["tasks"][1].update(depends_on=[]),
            contract_edit=lambda c: c["budgets"].update(max_concurrent_managed_agents=2))
        capabilities = supervisor.adapter.capabilities
        with patch.object(supervisor.adapter, "capabilities", lambda: dict(capabilities(), native_children_disabled=True)):
            result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertTrue(supervisor.status(verify=True)["evidence_current"])

    def test_required_missing_check_is_blocked_not_empty_pass(self):
        supervisor, _ = self.fixture(attempts=1,
            contract_edit=lambda c: c["node_checks"][0].update(argv=["/missing/harness-required-check"]))
        result = supervisor.start()
        self.assertNotEqual(result["status"], "ACCEPTED", result)
        self.assertFalse(any(r["record_type"] == "node_acceptance" for r in supervisor.receipts().records()))

    def test_failed_final_regression_keeps_local_success_partial(self):
        supervisor, _ = self.fixture(attempts=1,
            contract_edit=lambda c: c["acceptance"][0].update(argv=[sys.executable, "-c", "raise SystemExit(9)"]))
        result = supervisor.start()
        self.assertEqual(result["status"], "FAILED_TERMINAL", result)
        self.assertTrue(any(r["record_type"] == "node_acceptance" for r in supervisor.receipts().records()))
        self.assertIsNone(supervisor._core().state()["accepted"])

    def test_persisted_cancellation_prevents_all_model_dispatch(self):
        supervisor, fake = self.fixture()
        supervisor.stop("user cancelled before dispatch")
        self.assertEqual(supervisor.start()["status"], "CANCELLED")
        self.assertFalse(fake.with_suffix(".calls").exists())

    def test_cancel_before_process_creation_has_nonbillable_no_dispatch_receipt(self):
        supervisor, fake = self.fixture()
        cancel = self.root / "early-cancel"
        cancel.write_text("stop")
        result = supervisor.adapter.run("fixture", cwd=self.root / "source", cancel_file=cancel,
            log_dir=self.root / "cancel-logs")
        self.assertEqual(result["dispatch_state"], "not_dispatched_proven")
        intent = supervisor.receipts().intent("left", "left-1", checkout=self.root / "source", owner="fixture")
        receipt = supervisor.receipts().completion(intent, result)
        self.assertEqual(receipt["payload"]["usage_status"], "not_applicable")
        supervisor.receipts().reconcile_usage()
        self.assertEqual(supervisor.state["metrics"]["model_invocations"], 0)
        self.assertIsNone(supervisor.state["metrics"]["input_tokens"])
        self.assertFalse(fake.with_suffix(".calls").exists())

    def test_typed_adapter_refusal_releases_slot_without_inventing_usage(self):
        from agent_harness.agent_lifecycle import GlobalSlots
        supervisor, fake = self.fixture()
        with patch.object(supervisor.adapter, "run", side_effect=NotDispatched("known local refusal")):
            result = supervisor.start()
        self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
        self.assertEqual(supervisor.state["metrics"]["model_invocations"], 0)
        self.assertIsNone(supervisor.state["metrics"]["input_tokens"])
        self.assertTrue(all(row["status"] == "released" for row in GlobalSlots(supervisor.state_dir).inventory()))
        self.assertFalse(fake.with_suffix(".calls").exists())

    def test_known_failure_retry_cannot_dispatch_at_exhausted_token_boundary(self):
        def script(value):
            return value.replace("if mode=='permission':", "if mode=='left':event('turn.failed',error={'message':'retryable fixture failure'});raise SystemExit(1)\nif mode=='permission':")
        supervisor, fake = self.fixture(script_edit=script,
            contract_edit=lambda c: c["budgets"].update(max_tokens=26))
        run = supervisor.adapter.run
        def counted(*args, **kwargs):
            result = run(*args, **kwargs)
            if result["status"] == "failed":
                # Controlled accounting fixture, not an observed provider bill.
                result["usage"] = {"input_tokens": 10, "output_tokens": 3}
            return result
        with patch.object(supervisor.adapter, "run", counted):
            result = supervisor.start()
        self.assertEqual(result["status"], "FAILED_TERMINAL", result)
        self.assertIn("token budget exhausted", result["reason"])
        self.assertEqual(fake.with_suffix(".calls").read_text().splitlines(), ["exec", "exec"])
        self.assertEqual(supervisor.store.read("agents/left.yaml")["attempts_used"], 1)

    def test_known_stopped_reviewer_failure_gets_bounded_fresh_retry(self):
        def script(value):
            return value.replace("if 'HARNESS_FINAL_REVIEW' in prompt:\n", "if 'HARNESS_FINAL_REVIEW' in prompt:\n marker=pathlib.Path(__file__).with_suffix('.review-seen')\n if not marker.exists():\n  marker.write_text('seen');event('turn.failed',error={'message':'transient reviewer failure'});raise SystemExit(1)\n")
        supervisor, _ = self.fixture(script_edit=script)
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        completions = [r for r in supervisor.receipts().records() if r["record_type"] == "completion" and r["node_id"] == "@review"]
        self.assertEqual([r["payload"]["outcome"] for r in completions], ["failed", "completed"])
        self.assertEqual(supervisor.state["attempts_used"], 1)

    def test_recovery_repairs_known_stopped_root_failure_without_reset(self):
        from agent_harness.execution_receipts import ExecutionReceipts
        def script(value):
            return value.replace("if mode=='permission':", "if mode=='managed' and 'resume' not in args:event('turn.failed',error={'message':'transient integration failure'});raise SystemExit(1)\nif mode=='permission':")
        supervisor, fake = self.fixture(script_edit=script, wall_seconds=TWO_PHASE_WALL_SECONDS)
        deadline = supervisor.state["deadline"]
        real = ExecutionReceipts.completion
        def complete(receipts, intent, result, **options):
            receipt = real(receipts, intent, result, **options)
            if intent["node_id"] == "@integration" and receipt["payload"]["outcome"] == "failed":
                raise Crash()
            return receipt
        with patch.object(ExecutionReceipts, "completion", complete), self.assertRaises(Crash):
            supervisor.start()
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        result = recovered.resume()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(recovered.state["attempts_used"], 2)
        self.assertEqual(recovered.state["deadline"], deadline)
        self.assertIn("resume", fake.with_suffix(".calls").read_text())

    def test_committed_repair_decision_is_not_a_second_failed_attempt_on_resume(self):
        def script(value):
            return value.replace("if mode=='permission':", "if mode=='managed' and 'resume' not in args:event('turn.failed',error={'message':'transient integration failure'});raise SystemExit(1)\nif mode=='permission':")
        supervisor, _ = self.fixture(script_edit=script, wall_seconds=TWO_PHASE_WALL_SECONDS)
        deadline = supervisor.state["deadline"]
        save = supervisor._save
        def crash_after_decision(reason):
            result = save(reason)
            if supervisor.state["status"] == "REPAIRING":
                raise Crash()
            return result
        with patch.object(supervisor, "_save", crash_after_decision), self.assertRaises(Crash):
            supervisor.start()
        self.assertEqual(len(supervisor.state["failure_history"]), 1)
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        result = recovered.resume()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(recovered.state["attempts_used"], 2)
        self.assertEqual(len(recovered.state["failure_history"]), 1)
        self.assertEqual(recovered.state["deadline"], deadline)

    def test_prepared_core_repair_attempt_survives_a_second_crash(self):
        def script(value):
            return value.replace("if mode=='permission':", "if mode=='managed' and 'resume' not in args:event('turn.failed',error={'message':'transient integration failure'});raise SystemExit(1)\nif mode=='permission':")
        supervisor, _ = self.fixture(script_edit=script, wall_seconds=TWO_PHASE_WALL_SECONDS)
        deadline = supervisor.state["deadline"]
        prepare = supervisor._new_core_attempt
        def crash_after_prepare(reason):
            prepare(reason)
            raise Crash()
        with patch.object(supervisor, "_new_core_attempt", crash_after_prepare), self.assertRaises(Crash):
            supervisor.start()
        # Origin bootstrap, current post-DAG input, then root repair; only two
        # root model reservations are consumed across these three generations.
        self.assertEqual(len(supervisor._core().state()["attempts"]), 3)
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        result = recovered.resume()
        self.assertEqual(result["status"], "ACCEPTED", recovered.adapter.failure_summary(result))
        self.assertEqual(len(recovered._core().state()["attempts"]), 3)
        self.assertEqual(recovered.state["deadline"], deadline)

    def test_null_process_cleanup_keeps_lease_unresolved_on_resume(self):
        from agent_harness.agent_lifecycle import GlobalSlots
        supervisor, _ = self.fixture()
        with patch.object(supervisor.adapter, "run", return_value={"status": "failed", "error": "missing cleanup proof", "process_tree_stopped": None, "usage": None, "logs": {}}):
            result = supervisor.start()
        self.assertEqual(result["status"], "UNKNOWN_OUTCOME")
        with self.assertRaisesRegex(HarnessError, "uncertain writers"):
            Supervisor(supervisor.path, adapter=supervisor.adapter).resume()
        self.assertTrue(all(row["unresolved"] for row in GlobalSlots(supervisor.state_dir).inventory()))

    def test_hard_exit_after_prepared_wave_recovers_never_started_work(self):
        import subprocess
        supervisor, fake = self.fixture(verified=False)
        package = Path(__file__).resolve().parents[1]
        script = """
import os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from agent_harness.supervisor import Supervisor
from managed_process_fixture import BoundFixtureCodexAdapter
root=Path(sys.argv[3])
sup=Supervisor(root/'private'/'verified',adapter=BoundFixtureCodexAdapter((sys.executable,str(root/'fake.py'))))
sup.plan_input=str(root/'plan.yaml')
save=sup._save
def publish(reason):
 result=save(reason)
 if reason.startswith('spawn ready wave'):os._exit(73)
 return result
sup._save=publish
sup.start()
"""
        result = subprocess.run([sys.executable, "-c", script, str(package / "src"), str(package / "tests"), str(self.root)],
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))
        self.assertFalse(fake.with_suffix(".calls").exists())
        self.assertEqual(Supervisor(supervisor.path, adapter=supervisor.adapter).resume()["status"], "ACCEPTED")

    def test_equal_independent_failures_are_counted_per_work_item(self):
        def script(value):
            return value.replace("if mode=='permission':", "if mode in ('left','right') and 'resume' not in args:event('turn.failed',error={'message':'same transient failure'});raise SystemExit(1)\nif mode=='permission':")
        supervisor, _ = self.fixture(script_edit=script, wall_seconds=TWO_PHASE_WALL_SECONDS,
            plan_edit=lambda p: p["tasks"][1].update(depends_on=[]),
            contract_edit=lambda c: c["budgets"].update(max_concurrent_managed_agents=2))
        deadline = supervisor.state["deadline"]
        capabilities = supervisor.adapter.capabilities
        with patch.object(supervisor.adapter, "capabilities", lambda: dict(capabilities(), native_children_disabled=True)):
            result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertTrue(all(supervisor.store.read("agents/" + name + ".yaml")["attempts_used"] == 2 for name in ("left", "right")))
        self.assertEqual(supervisor.state["deadline"], deadline)

    def test_unchanged_node_check_failure_stops_with_a_valid_terminal_transition(self):
        def script(value):
            return value.replace("if mode in ('left','right'):target=mode+'.py'", "if mode in ('left','right'):target=mode+'.py'\nif mode=='left':value=0")
        supervisor, fake = self.fixture(script_edit=script)
        result = supervisor.start()
        self.assertEqual(result["status"], "NO_PROGRESS", result)
        self.assertEqual(len(fake.with_suffix(".calls").read_text().splitlines()), 3)

    def test_resume_honors_committed_repair_of_a_failed_final_check(self):
        def contract(value):
            value["acceptance"][0]["argv"][-1] = "from pathlib import Path; assert 'REPAIRED' in Path('left.py').read_text()"
        def script(value):
            return value.replace("if mode=='permission':", "if mode=='managed' and 'resume' in args:pathlib.Path('left.py').write_text('VALUE = 42\\n# REPAIRED\\n')\nif mode=='permission':")
        supervisor, _ = self.fixture(script_edit=script, contract_edit=contract,
                                     wall_seconds=TWO_PHASE_WALL_SECONDS)
        deadline = supervisor.state["deadline"]
        save = supervisor._save
        def crash_after_decision(reason):
            result = save(reason)
            if supervisor.state["status"] == "REPAIRING":
                raise Crash()
            return result
        with patch.object(supervisor, "_save", crash_after_decision), self.assertRaises(Crash):
            supervisor.start()
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        result = recovered.resume()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(recovered.state["attempts_used"], 2)
        self.assertEqual(len(recovered.state["failure_history"]), 1)
        self.assertEqual(recovered.state["deadline"], deadline)

    def test_cap_one_uses_structured_plan_without_an_explicit_plan_argument(self):
        supervisor, fake = self.fixture(wall_seconds=TWO_PHASE_WALL_SECONDS)
        deadline = supervisor.state["deadline"]
        plan_file = self.root / "plan.yaml"
        script = fake.read_text()
        planner = "if 'Return a complete map of coherent bounded outcomes.' in prompt:\n plan=json.loads(" + repr(json.dumps(load(plan_file))) + ")\n event('item.completed',item={'type':'agent_message','text':json.dumps(plan)});event('turn.completed',usage={'input_tokens':10,'output_tokens':3});raise SystemExit\n"
        fake.write_text(script.replace("if 'HARNESS_FINAL_REVIEW' in prompt:\n", planner + "if 'HARNESS_FINAL_REVIEW' in prompt:\n"))
        supervisor.plan_input = None
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        self.assertEqual(supervisor.state["deadline"], deadline)
        self.assertEqual(len(result["progress"]["nodes"]), 2)
        self.assertTrue(any(r["record_type"] == "invocation_intent" and r["node_id"] == "@planner" for r in supervisor.receipts().records()))

    def test_repeated_known_reviewer_failure_stops_as_no_progress(self):
        def script(value):
            return value.replace("if 'HARNESS_FINAL_REVIEW' in prompt:\n", "if 'HARNESS_FINAL_REVIEW' in prompt:\n event('turn.failed',error={'message':'unchanged reviewer failure'});raise SystemExit(1)\n")
        supervisor, fake = self.fixture(script_edit=script)
        result = supervisor.start()
        self.assertEqual(result["status"], "NO_PROGRESS", result)
        self.assertEqual(len(fake.with_suffix(".calls").read_text().splitlines()), 6)
        with self.assertRaisesRegex(HarnessError, "Terminal work"):
            Supervisor(supervisor.path, adapter=supervisor.adapter).resume()


if __name__ == "__main__":
    unittest.main()
