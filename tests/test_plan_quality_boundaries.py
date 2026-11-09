"""Current quality proof at actual worker/final publication use boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_harness import plan_quality, managed_execution
from agent_harness import supervisor as supervisor_module
from agent_harness.agent_lifecycle import GlobalSlots
from agent_harness.execution_receipts import ExecutionReceipts
from agent_harness.supervisor import Supervisor
from agent_harness.core import HarnessError
from agent_harness.strict_yaml import dump, load
import test_verified_flow as fixtures
from test_plan_quality import Crash


class PlanQualityBoundaryTests(unittest.TestCase):
    setUp=fixtures.VerifiedFlowTests.setUp
    fixture=fixtures.VerifiedFlowTests.fixture

    def drift(self, owner, kind):
        self.assertFalse(plan_quality.evidence_errors(owner))
        quality=owner.state['plan_quality']
        records=owner.receipts().records()
        receipt=next(r for r in records if r['record_id']==quality['assessment_ref'])
        if kind=='policy':
            target=owner.store.path(quality['policy_ref']['path']);target.unlink()
        elif kind=='assessment':
            target=owner.store.path(receipt['payload']['assessment_ref']);target.write_bytes(target.read_bytes()+b' ')
        elif kind=='raw':
            completion=next(r for r in records if r['record_id']==receipt['payload']['completion_ref'])
            target=Path(completion['payload']['logs']['stdout']);target.unlink()
        else:
            target=None;quality['assessment_ref']='wrong-assessment-reference'
            owner._save('fixture wrong authoritative assessment reference')
        self.assertTrue(plan_quality.evidence_errors(owner))
        return target

    def test_worker_currentness_after_selection_and_preflight_has_no_intent_or_poisoned_slot(self):
        index=0
        for boundary in ('selection','preflight'):
            for kind in ('policy','assessment','raw','reference'):
                if index:self.setUp()
                index+=1
                owner,fake=self.fixture();select=plan_quality.select_plan;preflight=managed_execution.preflight;publish=ExecutionReceipts.publish
                hits=[]
                def selected(supervisor,capabilities):
                    plan=select(supervisor,capabilities)
                    if boundary=='selection':self.drift(supervisor,kind)
                    return plan
                def checked(adapter,prompt,**options):
                    value=preflight(adapter,prompt,**options)
                    packet=json.loads(prompt.rsplit('\n',1)[1])
                    if boundary=='preflight' and packet.get('id')=='left':
                        hits.append(True)
                        if len(hits)==2:self.drift(owner,kind)
                    return value
                def no_worker(receipts,value):
                    record=publish(receipts,value)
                    if value['record_type']=='invocation_intent' and not value['node_id'].startswith('@'):raise Crash()
                    return record
                with self.subTest(boundary=boundary,kind=kind),patch.object(plan_quality,'select_plan',selected),patch.object(managed_execution,'preflight',checked),patch.object(ExecutionReceipts,'publish',no_worker):
                    try:result=owner.start()
                    except Crash:self.fail('Worker intent persisted with stale quality proof')
                self.assertEqual(result['status'],'BLOCKED_AMBIGUOUS',result)
                if boundary=='preflight':self.assertEqual(len(hits),2)
                self.assertEqual([r['node_id'] for r in owner.receipts().records() if r['record_type']=='invocation_intent'],['@plan-quality'])
                calls=fake.with_suffix('.calls').read_bytes()
                self.assertEqual(calls,b'exec\n')
                self.assertTrue(all(row['status']=='released' for row in GlobalSlots(owner.state_dir).inventory(owner.state['run_id'])))
                for _ in range(2):
                    self.assertEqual(Supervisor(owner.path,adapter=owner.adapter).resume()['status'],'BLOCKED_AMBIGUOUS')
                self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
                self.assertTrue(plan_quality.evidence_errors(owner))

    @staticmethod
    def readonly(plan):
        node=deepcopy(plan['tasks'][-1])
        node.update(id='audit',goal='review',role='reviewer',read_only=True,read_scope=['left.py','right.py'],
            write_scope=[],depends_on=['left','right'],outputs=['left.py','right.py'])
        node['checks']={phase:['left-value','right-value'] for phase in ('local','integration')}
        plan['tasks'].append(node)

    @staticmethod
    def readonly_script(script):
        handler="if mode=='review':event('item.completed',item={'type':'agent_message','text':json.dumps({'schema_version':1,'verdict':'approved','findings':[]})});event('turn.completed',usage={'input_tokens':10,'output_tokens':3});raise SystemExit\n"
        return script.replace("if mode=='permission':",handler+"if mode=='permission':")

    def test_readonly_and_repair_worker_preflight_and_completion_replay_reject_drift(self):
        for index,mode in enumerate(('readonly','repair','replay')):
            if index:self.setUp()
            if mode=='readonly':owner,fake=self.fixture(plan_edit=self.readonly,script_edit=self.readonly_script)
            elif mode=='repair':
                owner,fake=self.fixture(script_edit=lambda script:script.replace("if mode in ('left','right'):target=mode+'.py'", "if mode in ('left','right'):target=mode+'.py'\nif mode=='left' and 'resume' not in args:value=0"))
            else:owner,fake=self.fixture()
            if mode=='replay':
                publish=ExecutionReceipts.publish
                def stopped(receipts,value):
                    record=publish(receipts,value)
                    if value['record_type']=='completion' and value['node_id']=='left':raise Crash()
                    return record
                with patch.object(ExecutionReceipts,'publish',stopped),self.assertRaises(Crash):owner.start()
                self.drift(owner,'assessment')
                calls=fake.with_suffix('.calls').read_bytes()
                result=Supervisor(owner.path,adapter=owner.adapter).resume()
                self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
            else:
                preflight=managed_execution.preflight
                hits=[]
                def checked(adapter,prompt,**options):
                    value=preflight(adapter,prompt,**options)
                    packet=json.loads(prompt.rsplit('\n',1)[1])
                    if (mode=='readonly' and packet.get('id')=='audit' or mode=='repair' and packet.get('id')=='left' and options.get('session_id')):
                        hits.append(True)
                        if len(hits)==2:self.drift(owner,'assessment')
                    return value
                with patch.object(managed_execution,'preflight',checked):result=owner.start()
                self.assertEqual(len(hits),2)
            with self.subTest(mode=mode):
                self.assertEqual(result['status'],'BLOCKED_AMBIGUOUS',result)
                intents=[r for r in owner.receipts().records() if r['record_type']=='invocation_intent']
                self.assertFalse(any(r['node_id']=='audit' or r['node_id']=='left' and r['attempt_id']=='left-2' for r in intents))
                self.assertTrue(all(row['status']=='released' for row in GlobalSlots(owner.state_dir).inventory(owner.state['run_id'])))

    def test_healthy_parallel_and_readonly_admission_remain_positive(self):
        def plan(value):self.readonly(value);value['tasks'][1]['depends_on']=[]
        owner,_=self.fixture(plan_edit=plan,script_edit=self.readonly_script,
            contract_edit=lambda c:c['budgets'].update(max_concurrent_managed_agents=2))
        capabilities=owner.adapter.capabilities
        with patch.object(owner.adapter,'capabilities',lambda:dict(capabilities(),native_children_disabled=True)):
            result=owner.start()
        self.assertEqual(result['status'],'ACCEPTED',result)
        self.assertTrue(owner.status(verify=True)['evidence_current'])
        self.assertTrue(any(r['record_type']=='completion' and r['node_id']=='audit' for r in owner.receipts().records()))

    def test_root_preflight_drift_never_reserves_attempt_or_intent(self):
        for index,repair in enumerate((False,True)):
            if index:self.setUp()
            owner,fake=self.fixture();preflight=supervisor_module.preflight
            tripped=[];before=[]
            def checked(adapter,prompt,**options):
                value=preflight(adapter,prompt,**options)
                before.append(owner.state['attempts_used'])
                self.drift(owner,'assessment');tripped.append(True)
                return value
            if repair:
                self.assertEqual(owner.start()['status'],'ACCEPTED')
                owner.state['status']='REPAIRING';owner._save('fixture bounded root repair entry')
                calls=fake.with_suffix('.calls').read_bytes()
                with patch.object(supervisor_module,'preflight',checked):self.assertFalse(owner._execute(repair=True))
                self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
            else:
                with patch.object(supervisor_module,'preflight',checked):owner.start()
            self.assertTrue(tripped)
            self.assertEqual(owner.state['status'],'BLOCKED_AMBIGUOUS')
            self.assertEqual(owner.state['attempts_used'],before[0])
            intents=[r for r in owner.receipts().records() if r['record_type']=='invocation_intent' and r['node_id']=='@integration']
            self.assertEqual(len(intents),1 if repair else 0)
            self.assertTrue(all(row['status']=='released' for row in GlobalSlots(owner.state_dir).inventory(owner.state['run_id'])))

    def test_proof_lost_after_committed_acceptance_preserves_history_and_reports_stale(self):
        owner,fake=self.fixture()
        self.assertEqual(owner.start()['status'],'ACCEPTED')
        journal=owner.store.path('journal/events.jsonl').read_bytes()
        report=owner.store.path('acceptance/report.yaml').read_bytes()
        core=owner._core().state()['accepted']
        calls=fake.with_suffix('.calls').read_bytes()
        self.drift(owner,'assessment')
        fresh=Supervisor(owner.path,adapter=owner.adapter)
        for result in (fresh.status(verify=True),fresh.resume()):
            self.assertEqual(result['status'],'STALE')
        self.assertEqual(owner.store.path('journal/events.jsonl').read_bytes(),journal)
        self.assertEqual(owner.store.path('acceptance/report.yaml').read_bytes(),report)
        self.assertEqual(owner._core().state()['accepted'],core)
        self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
        fresh.load();self.assertEqual(fresh.state['status'],'ACCEPTED')

    def test_selected_plan_drift_cannot_reuse_in_memory_approval(self):
        owner,fake=self.fixture();select=plan_quality.select_plan
        def selected(supervisor,capabilities):
            plan=select(supervisor,capabilities)
            path=supervisor.store.path(supervisor.state['plan_path'])
            changed=deepcopy(plan);changed['tasks'][0]['goal']+=' changed after selection'
            dump(path,changed)
            self.assertTrue(plan_quality.evidence_errors(supervisor,plan))
            return plan
        with patch.object(plan_quality,'select_plan',selected):result=owner.start()
        self.assertEqual(result['status'],'BLOCKED_AMBIGUOUS',result)
        self.assertEqual([r['node_id'] for r in owner.receipts().records() if r['record_type']=='invocation_intent'],['@plan-quality'])
        self.assertEqual(fake.with_suffix('.calls').read_bytes(),b'exec\n')

    def test_final_use_boundaries_never_persist_accepted_mission_with_stale_quality(self):
        from agent_harness import managed_review
        for index,boundary in enumerate(('after_checks','after_review','after_core_gate','after_report','after_PASS')):
            if index:self.setUp()
            owner,_=self.fixture();check=supervisor_module.run_check;review=managed_review.review_candidate;gate=supervisor_module.gate;write=owner.store.write;transition=owner.transition
            tripped=[]
            def trip():
                if not tripped:
                    self.drift(owner,'assessment');tripped.append(True)
            def checked(run,*args,**options):
                value=check(run,*args,**options)
                if boundary=='after_checks':trip()
                return value
            def reviewed(supervisor,run,candidate):
                value=review(supervisor,run,candidate)
                if boundary=='after_review':trip()
                return value
            def gated(run):
                value=gate(run)
                if boundary=='after_core_gate':trip()
                return value
            def written(name,value,*args,**options):
                result=write(name,value,*args,**options)
                if boundary=='after_report' and name=='acceptance/report.yaml' and value['verdict']['verdict']=='accepted':trip()
                return result
            def moved(target,reason):
                value=transition(target,reason)
                if boundary=='after_PASS' and target=='PASS':trip()
                return value
            with self.subTest(boundary=boundary),patch.object(supervisor_module,'run_check',checked),patch.object(managed_review,'review_candidate',reviewed),patch.object(supervisor_module,'gate',gated),patch.object(owner.store,'write',written),patch.object(owner,'transition',moved):
                result=owner.start()
            self.assertTrue(tripped)
            self.assertEqual(result['status'],'BLOCKED_AMBIGUOUS',result)
            fresh=Supervisor(owner.path,adapter=owner.adapter);fresh.load()
            self.assertNotEqual(fresh.state['status'],'ACCEPTED')
            self.assertFalse(any(event['payload'].get('kind')=='checkpoint' and event['payload']['state']['status']=='ACCEPTED' for event in fresh.store.events()))
            self.assertEqual(fresh.store.read('acceptance/report.yaml')['verdict']['verdict'],'blocked')
            if boundary in ('after_checks','after_review'):self.assertIsNone(fresh._core().state()['accepted'])
            self.assertFalse(fresh.status(verify=True)['progress']['complete'])
