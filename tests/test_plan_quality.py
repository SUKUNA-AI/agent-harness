"""Versioned plan-quality gate, with actual schema validators and contained processes."""
from pathlib import Path
from copy import deepcopy
import hashlib
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_harness.execution_receipts import ExecutionReceipts
from agent_harness import plan_quality
from agent_harness.core import HarnessError, digest
from agent_harness.strict_yaml import load, dump, validate, _schema
from agent_harness.supervisor import Supervisor
from agent_harness.managed_review import _raw_verdict
import test_verified_flow as fixtures


class Crash(BaseException):
    pass


class PlanQualityTests(unittest.TestCase):
    setUp = fixtures.VerifiedFlowTests.setUp
    fixture = fixtures.VerifiedFlowTests.fixture

    @staticmethod
    def planner(script, plan):
        reply = ("if 'Return a complete map of coherent bounded outcomes.' in prompt:\n plan=json.loads(" +
            repr(json.dumps(plan)) + ")\n event('item.completed',item={'type':'agent_message','text':json.dumps(plan)});event('turn.completed',usage={'input_tokens':10,'output_tokens':3});raise SystemExit\n")
        return script.replace("if 'HARNESS_FINAL_REVIEW' in prompt:\n", reply + "if 'HARNESS_FINAL_REVIEW' in prompt:\n")

    @staticmethod
    def giant(plan):
        node=deepcopy(plan['tasks'][0])
        node.update(requirement_ids=[r for task in plan['tasks'] for r in task['requirement_ids']],
            write_scope=['left.py','right.py'],outputs=['left.py','right.py'],depends_on=[],
            checks={phase:[check for task in plan['tasks'] for check in task['checks'][phase]] for phase in ('local','integration')})
        plan['tasks']=[node]

    def quality_wire(self, supervisor):
        record=next(r for r in supervisor.receipts().records() if r['record_type']=='plan_quality_assessment')
        wire=json.loads(supervisor.store.path(record['payload']['assessment_ref']).read_bytes())
        return record,wire

    def test_new_managed_verified_run_uses_state3(self):
        supervisor,_=self.fixture()
        self.assertEqual(supervisor.state['schema_version'],3)

    def test_plan_requires_quality_assessment_before_first_worker_intent(self):
        supervisor,_=self.fixture()
        publish=ExecutionReceipts.publish
        def first_worker(receipts,record):
            result=publish(receipts,record)
            if record['record_type']=='invocation_intent' and record['node_id']=='left':
                raise Crash()
            return result
        with patch.object(ExecutionReceipts,'publish',first_worker),self.assertRaises(Crash):
            supervisor.start()
        records=supervisor.receipts().records()
        worker=next(index for index,r in enumerate(records) if r['record_type']=='invocation_intent' and r['node_id']=='left')
        self.assertTrue(any(r['record_type']=='plan_quality_assessment' and r['payload']['verdict']=='approved' for r in records[:worker]))

    def test_unrelated_giant_replans_with_feedback_before_any_worker(self):
        atomic={}
        def giant(plan):
            atomic.update(deepcopy(plan));self.giant(plan)
        def script(value):
            atomic['mission_digest']=digest(load(self.root/'task-v2.yaml')['mission'])
            value=self.planner(value,atomic)
            rejection="\n if quality_packet['binding']['round']==1:\n  quality_verdict='requires_revision';quality_findings=[dict(node_ids=[quality_nodes[0]['node_id']],reason='Unrelated left and right outcomes are bundled only because cap is one.',revision_request='Use independent coherent outcomes with their own checks.')];quality_nodes[0]['atomicity']='too_broad'\n"
            return value.replace(" quality_wire=dict(",rejection+" quality_wire=dict(")
        supervisor,_=self.fixture(plan_edit=giant,script_edit=script,
                                  wall_seconds=fixtures.TWO_PHASE_WALL_SECONDS)
        deadline=supervisor.state['deadline']
        result=supervisor.start()
        self.assertEqual(result['status'],'ACCEPTED',result)
        records=supervisor.receipts().records()
        quality=[r for r in records if r['record_type']=='plan_quality_assessment']
        self.assertEqual([r['payload']['verdict'] for r in quality],['requires_revision','approved'])
        self.assertEqual(supervisor.state['plan_quality']['rounds_used'],2)
        self.assertEqual(supervisor.state['plan_quality']['plan_revision'],2)
        first_worker=next(i for i,r in enumerate(records) if r['record_type']=='invocation_intent' and r['node_id']=='left')
        self.assertTrue(any(r['record_id']==quality[-1]['record_id'] for r in records[:first_worker]))
        self.assertEqual(result['metrics']['model_invocations'],7)
        self.assertEqual(supervisor.state['deadline'],deadline)

    def test_justified_single_coherent_operation_is_not_split(self):
        def high_risk(plan):
            self.giant(plan)
            plan['tasks'][0].update(risk='high', reasoning_effort='medium')
        def script(value):
            marker="if mode!='managed':pathlib.Path(target).write_text('VALUE = '+str(value)+'\\n')"
            return value.replace(marker,marker+"\nif mode=='left':pathlib.Path('right.py').write_text('VALUE = '+str(value)+'\\n')")
        supervisor,_=self.fixture(plan_edit=high_risk,script_edit=script,
            contract_edit=lambda c:c['mission'].update(original_request='Change both values as one coherent operation with joint acceptance.'))
        efforts=[]
        run=supervisor.adapter.run
        def observed(prompt, **options):
            efforts.append(options.get('reasoning_effort'))
            return run(prompt, **options)
        with patch.object(supervisor.adapter,'run',side_effect=observed):
            result=supervisor.start()
        self.assertEqual(result['status'],'ACCEPTED',result)
        self.assertEqual(efforts,['ultra','medium','ultra'])
        _,wire=self.quality_wire(supervisor)
        self.assertEqual(len(wire['nodes']),1)
        self.assertEqual(wire['nodes'][0]['atomicity'],'coherent')
        self.assertEqual(supervisor.state['plan_quality']['rounds_used'],1)
        self.assertFalse(any(r['node_id']=='@planner' for r in supervisor.receipts().records()))

    def test_deterministic_rejections_spend_round_without_worker_or_semantic_call(self):
        def missing(plan):plan['tasks'].pop()
        def cycle(plan):plan['tasks'][0]['depends_on']=['right']
        def overlap(plan):
            plan['tasks'][1].update(depends_on=[],write_scope=['left.py'],outputs=['left.py'])
        def empty_goal(plan):plan['tasks'][0]['goal']='   '
        def empty_check(plan):plan['tasks'][0]['checks']['local']=[]
        for index,edit in enumerate((missing,cycle,overlap,empty_goal,empty_check)):
            if index:self.setUp()
            supervisor,fake=self.fixture(plan_edit=edit)
            with self.subTest(case=edit.__name__):
                self.assertEqual(supervisor.start()['status'],'BLOCKED_AMBIGUOUS')
                self.assertEqual(supervisor.state['plan_quality']['rounds_used'],1)
                self.assertFalse(fake.with_suffix('.calls').exists())
                self.assertFalse(any(r['record_type']=='invocation_intent' for r in supervisor.receipts().records()))

    def test_bound_assessment_schema_and_controller_negatives(self):
        supervisor,_=self.fixture()
        self.assertEqual(supervisor.start()['status'],'ACCEPTED')
        record,wire=self.quality_wire(supervisor)
        plan=supervisor.store.read(supervisor.state['plan_path'],'agent-plan.schema.json')
        binding={key:wire[key] for key in ('mission_digest','mission_generation','contract_digest','plan_revision','plan_digest','policy_version','policy_digest','input_digest','round')}
        plan_quality._validate_assessment(supervisor,plan,wire,binding)
        for field in binding:
            changed=deepcopy(wire);changed[field]=wire[field]+1 if type(wire[field]) is int else '0'*64
            with self.subTest(field=field),self.assertRaises((HarnessError,ValueError)):
                plan_quality._validate_assessment(supervisor,plan,changed,binding)
        for case in ('missing','duplicate','requiredness','local-check','dependency','owner','empty-rationale','whitespace-rationale','broad-approved','weak-check','bad-dependency'):
            changed=deepcopy(wire)
            if case=='missing':changed['nodes'].pop()
            elif case=='duplicate':changed['nodes'][1]=deepcopy(changed['nodes'][0])
            elif case=='requiredness':changed['nodes'][0]['required']=False
            elif case=='local-check':changed['nodes'][0]['local_checks']=['unknown']
            elif case=='dependency':changed['nodes'][0]['depends_on']=['right']
            elif case=='owner':changed['nodes'][0]['owner']='unknown'
            elif case=='empty-rationale':changed['nodes'][0]['atomicity_rationale']=''
            elif case=='whitespace-rationale':changed['nodes'][0]['atomicity_rationale']=' \n\t'
            elif case=='broad-approved':changed['nodes'][0]['atomicity']='too_broad'
            elif case=='weak-check':changed['nodes'][0]['checks_meaningful']=False
            else:changed['nodes'][0]['dependency_valid']=False
            with self.subTest(case=case),self.assertRaises((HarnessError,ValueError)):
                plan_quality._validate_assessment(supervisor,plan,changed,binding)
        self.assertEqual(record['schema_version'],2)
        self.assertEqual(_schema('agent-plan.schema.json')['properties']['schema_version']['enum'],[1,2])
        conflict=deepcopy(record);conflict['payload']['verdict']='requires_revision'
        journal=supervisor.store.path('journal/events.jsonl').read_bytes()
        with supervisor.store.lease('fixture receipt ID conflict'),self.assertRaisesRegex(HarnessError,'integrity conflict'):
            supervisor.receipts().publish(conflict)
        self.assertEqual(supervisor.store.path('journal/events.jsonl').read_bytes(),journal)

    def test_wrong_model_binding_blocks_fresh_and_completion_replay(self):
        def script(value):return value.replace(" event('item.completed',item={'type':'agent_message','text':json.dumps(quality_wire)})", " quality_wire['plan_digest']='0'*64\n event('item.completed',item={'type':'agent_message','text':json.dumps(quality_wire)})")
        supervisor,fake=self.fixture(script_edit=script)
        self.assertEqual(supervisor.start()['status'],'BLOCKED_AMBIGUOUS')
        calls=fake.with_suffix('.calls').read_bytes()
        self.assertFalse(any(r['record_type']=='plan_quality_assessment' for r in supervisor.receipts().records()))
        self.assertEqual(Supervisor(supervisor.path,adapter=supervisor.adapter).resume()['status'],'BLOCKED_AMBIGUOUS')
        self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)

    def test_completion_assessment_publication_boundaries_recover_without_double_call(self):
        for index,boundary in enumerate(('completion','artifact','receipt','state')):
            if index:self.setUp()
            supervisor,_=self.fixture(wall_seconds=fixtures.TWO_PHASE_WALL_SECONDS)
            deadline=supervisor.state['deadline']
            publish=ExecutionReceipts.publish;artifact=plan_quality._publish;save=supervisor._save
            def crash_publish(receipts,value):
                result=publish(receipts,value)
                if (boundary=='completion' and value['record_type']=='completion' and value['node_id']=='@plan-quality'
                        or boundary=='receipt' and value['record_type']=='plan_quality_assessment'):raise Crash()
                return result
            def crash_artifact(owner,value):
                result=artifact(owner,value)
                if boundary=='artifact' and 'nodes' in value and 'verdict' in value:raise Crash()
                return result
            def crash_state(reason):
                if boundary=='state' and reason.startswith('controller published exact plan-quality'):raise Crash()
                return save(reason)
            with self.subTest(boundary=boundary),patch.object(ExecutionReceipts,'publish',crash_publish),patch.object(plan_quality,'_publish',crash_artifact),patch.object(supervisor,'_save',crash_state),self.assertRaises(Crash):
                supervisor.start()
            old=next(r for r in supervisor.receipts().records() if r['record_type']=='completion' and r['node_id']=='@plan-quality')
            recovered=Supervisor(supervisor.path,adapter=supervisor.adapter)
            result=recovered.resume()
            self.assertEqual(result['status'],'ACCEPTED',result)
            records=recovered.receipts().records()
            self.assertEqual(sum(r['record_type']=='invocation_intent' and r['node_id']=='@plan-quality' for r in records),1)
            self.assertEqual(sum(r['record_type']=='plan_quality_assessment' for r in records),1)
            self.assertEqual(next(r for r in records if r['record_id']==old['record_id']),old)
            self.assertEqual(result['metrics']['model_invocations'],5)
            self.assertEqual(recovered.state['plan_quality']['rounds_used'],1)
            self.assertEqual(recovered.state['deadline'],deadline)

    def test_missing_or_corrupt_raw_completion_never_synthesizes_assessment(self):
        for index,corrupt in enumerate((False,True)):
            if index:self.setUp()
            supervisor,fake=self.fixture();publish=ExecutionReceipts.publish
            def crash(receipts,value):
                result=publish(receipts,value)
                if value['record_type']=='completion' and value['node_id']=='@plan-quality':raise Crash()
                return result
            with patch.object(ExecutionReceipts,'publish',crash),self.assertRaises(Crash):supervisor.start()
            completion=next(r for r in supervisor.receipts().records() if r['record_type']=='completion')
            raw=Path(completion['payload']['logs']['stdout'])
            if corrupt:raw.write_bytes(b'corrupt raw')
            else:raw.unlink()
            calls=fake.with_suffix('.calls').read_bytes()
            result=Supervisor(supervisor.path,adapter=supervisor.adapter).resume()
            self.assertEqual(result['status'],'BLOCKED_AMBIGUOUS',result)
            self.assertFalse(any(r['record_type']=='plan_quality_assessment' for r in supervisor.receipts().records()))
            self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)

    def test_unknown_assessor_cleanup_blocks_all_workers_and_reports_unresolved(self):
        supervisor,fake=self.fixture();run=supervisor.adapter.run
        def uncertain(prompt,**options):
            result=run(prompt,**options)
            if 'HARNESS_PLAN_QUALITY' in prompt:result['process_tree_stopped']=None
            return result
        with patch.object(supervisor.adapter,'run',side_effect=uncertain):result=supervisor.start()
        self.assertEqual(result['status'],'UNKNOWN_OUTCOME')
        self.assertEqual([r['node_id'] for r in supervisor.receipts().records() if r['record_type']=='invocation_intent'],['@plan-quality'])
        self.assertIsNone(result['unresolved_invocations'][0]['process_tree_stopped'])
        self.assertFalse(result['progress']['plan_quality']['passed'])
        self.assertEqual(result['progress']['coverage_counts']['mandatory'],{'accepted':0,'total':2})
        calls=fake.with_suffix('.calls').read_bytes()
        with self.assertRaisesRegex(HarnessError,'uncertain writers'):Supervisor(supervisor.path,adapter=supervisor.adapter).resume()
        self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)

    def test_assessment_spends_original_token_budget_before_worker_admission(self):
        supervisor,fake=self.fixture(contract_edit=lambda c:c['budgets'].update(max_tokens=13))
        deadline=supervisor.state['deadline'];result=supervisor.start()
        self.assertEqual(result['status'],'FAILED_TERMINAL',result)
        self.assertEqual([r['node_id'] for r in supervisor.receipts().records() if r['record_type']=='invocation_intent'],['@plan-quality'])
        self.assertEqual((result['metrics']['model_invocations'],result['metrics']['input_tokens'],result['metrics']['output_tokens']),(1,10,3))
        self.assertEqual(supervisor.state['plan_quality']['rounds_used'],1)
        self.assertEqual(supervisor.state['deadline'],deadline)
        self.assertEqual(fake.with_suffix('.calls').read_text().splitlines(),['exec'])

    def test_known_stopped_assessor_retry_consumes_round_and_repeated_failure_stops(self):
        for index,repeated in enumerate((False,True)):
            if index:self.setUp()
            def script(value):
                refusal="if 'HARNESS_PLAN_QUALITY' in prompt:\n quality_seen=pathlib.Path(__file__).with_suffix('.quality-seen')\n"
                refusal+=(" if True:\n" if repeated else " if not quality_seen.exists():\n")
                refusal+="  quality_seen.write_text('seen');event('turn.failed',error={'message':'compiler fixture assessment failure'});raise SystemExit(1)\n"
                return value.replace("if 'HARNESS_PLAN_QUALITY' in prompt:\n",refusal)
            supervisor,_=self.fixture(script_edit=script);deadline=supervisor.state['deadline']
            result=supervisor.start()
            self.assertEqual(result['status'],'NO_PROGRESS' if repeated else 'ACCEPTED',result)
            self.assertEqual(supervisor.state['plan_quality']['rounds_used'],2)
            self.assertEqual(supervisor.state['plan_quality']['plan_revision'],1)
            self.assertEqual(supervisor.state['deadline'],deadline)
            records=supervisor.receipts().records()
            outcomes=[r['payload']['outcome'] for r in records if r['record_type']=='completion' and r['node_id']=='@plan-quality']
            self.assertEqual(outcomes,['failed','failed' if repeated else 'completed'])
            self.assertEqual(result['metrics']['model_invocations'],2 if repeated else 6)
            self.assertFalse(any(r['node_id']=='@planner' for r in records))

    def test_revision_round_cap_and_no_progress_do_not_reset(self):
        for index,change in enumerate((False,True)):
            if index:self.setUp()
            def script(value):
                plan=load(self.root/'plan.yaml')
                reply="if 'Return a complete map of coherent bounded outcomes.' in prompt:\n plan=json.loads("+repr(json.dumps(plan))+")\n"
                if change:reply+=" request=json.loads(prompt.rsplit('\\n',1)[1])['planning_request'];plan['tasks'][0]['goal']='left proposal '+str(request['round'])\n"
                reply+=" event('item.completed',item={'type':'agent_message','text':json.dumps(plan)});event('turn.completed',usage={'input_tokens':10,'output_tokens':3});raise SystemExit\n"
                value=value.replace("if 'HARNESS_FINAL_REVIEW' in prompt:\n",reply+"if 'HARNESS_FINAL_REVIEW' in prompt:\n")
                value=value.replace("quality_verdict='approved';quality_findings=[]", "quality_verdict='requires_revision';quality_findings=[dict(node_ids=[],reason='Outcome boundary still needs refinement.',revision_request='Apply the scoped feedback before admission.')]")
                if not change:
                    value=value.replace(" quality_wire=dict(","\n if quality_packet['binding']['round']>1:quality_verdict='approved';quality_findings=[]\n quality_wire=dict(")
                return value
            supervisor,_=self.fixture(attempts=3,planning_rounds=2 if change else 3,script_edit=script)
            deadline=supervisor.state['deadline'];result=supervisor.start()
            self.assertEqual(result['status'],'FAILED_TERMINAL' if change else 'NO_PROGRESS',result)
            self.assertEqual(supervisor.state['plan_quality']['rounds_used'],2)
            self.assertEqual(result['metrics']['model_invocations'],3 if change else 2)
            self.assertEqual(supervisor.state['deadline'],deadline)
            self.assertFalse(any(r['record_type']=='invocation_intent' and r['node_id'] in {'left','right'} for r in supervisor.receipts().records()))

    def test_generation_bump_cannot_erase_published_rejection_after_crash(self):
        def script(value):
            return value.replace(" quality_wire=dict(","\n if quality_packet['binding']['round']==1:quality_verdict='requires_revision';quality_findings=[dict(node_ids=[],reason='Unchanged outcome boundary rejected.',revision_request='Revise the actual outcome/check content.') ]\n quality_wire=dict(")
        supervisor,fake=self.fixture(script_edit=script);publish=ExecutionReceipts.publish
        def crash(receipts,value):
            result=publish(receipts,value)
            if value['record_type']=='plan_quality_assessment':raise Crash()
            return result
        with patch.object(ExecutionReceipts,'publish',crash),self.assertRaises(Crash):supervisor.start()
        self.assertFalse(any(e['payload'].get('kind')=='plan_quality_round_result' for e in supervisor.store.events()))
        calls=fake.with_suffix('.calls').read_bytes();deadline=supervisor.state['deadline']
        revised=deepcopy(supervisor.contract)
        revised['mission'].update(generation=2,previous_digest=digest(supervisor.contract['mission']))
        path=self.root/'generation2.yaml';dump(path,revised)
        plan=supervisor.store.read(supervisor.state['plan_path'],'agent-plan.schema.json')
        plan.update(generation=2,mission_digest=digest(revised['mission']))
        plan_path=self.root/'generation2-plan.yaml';dump(plan_path,plan)
        supervisor.update_mission(path,reason='Administrative generation bump; outcome/check content unchanged')
        supervisor.plan_input=str(plan_path)
        result=supervisor.start()
        self.assertEqual(result['status'],'NO_PROGRESS',result)
        self.assertEqual(result['metrics']['model_invocations'],1)
        self.assertEqual(supervisor.state['plan_quality']['rounds_used'],2)
        self.assertEqual(supervisor.state['deadline'],deadline)
        self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)

    def test_strict_status_counts_and_final_root_repair_require_current_assessment(self):
        def optional(contract):
            contract['mission']['requirements'][0]['required']=False
            row=deepcopy(contract['mission']['requirements'][1]);row.update(id='R-later',required=False,text='Optional later outcome')
            contract['mission']['requirements'].append(row)
        supervisor,fake=self.fixture(contract_edit=optional)
        self.assertEqual(supervisor.start()['status'],'ACCEPTED')
        result=supervisor.status(verify=True)
        self.assertEqual(result['progress']['coverage_counts'],{'mandatory':{'accepted':1,'total':1},'optional':{'accepted':1,'deferred':1,'planned':1,'total':2}})
        self.assertEqual(result['progress']['plan_quality']['status'],'current')
        record,_=self.quality_wire(supervisor);artifact=supervisor.store.path(record['payload']['assessment_ref']);raw=artifact.read_bytes()
        calls=fake.with_suffix('.calls').read_bytes();journal=supervisor.store.path('journal/events.jsonl').read_bytes()
        for relative in (supervisor.state['plan_quality']['policy_ref']['path'],supervisor.state['plan_path']):
            path=supervisor.store.path(relative);original=path.read_bytes()
            if relative.endswith('.json'):path.write_bytes(original+b' ')
            else:
                changed=load(path);changed['tasks'][0]['goal']+=' changed';dump(path,changed)
            self.assertEqual(supervisor.status(verify=True)['status'],'STALE')
            self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
            self.assertEqual(supervisor.store.path('journal/events.jsonl').read_bytes(),journal)
            path.write_bytes(original)
            self.assertFalse(plan_quality.evidence_errors(supervisor))
        artifact.unlink()
        stale=supervisor.status(verify=True)
        self.assertEqual(stale['status'],'STALE')
        self.assertFalse(stale['progress']['plan_quality']['passed'])
        self.assertEqual(supervisor.store.path('journal/events.jsonl').read_bytes(),journal)
        with self.assertRaisesRegex(HarnessError,'PLAN_QUALITY'):supervisor._verify()
        with supervisor.store.lease('fixture committed root repair'):
            supervisor.state['status']='REPAIRING';supervisor._save('fixture committed root repair before independent quality admission')
            self.assertFalse(supervisor._execute(repair=True))
        self.assertEqual(supervisor.state['status'],'BLOCKED_AMBIGUOUS')
        self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
        artifact.write_bytes(raw)
        self.assertFalse(plan_quality.evidence_errors(supervisor))


if __name__=='__main__':
    unittest.main()
