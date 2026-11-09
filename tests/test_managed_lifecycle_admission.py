"""Managed v2 invocation admission uses exact observations on fresh and replay paths."""
import json
from copy import deepcopy
from itertools import product
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_harness import managed_execution
from agent_harness.agent_lifecycle import (GlobalSlots, guard_invocations,
    post_invocation_guard, InvocationUnresolved, invocation_timeout)
from agent_harness.core import HarnessError, digest
from agent_harness.execution_receipts import ExecutionReceipts, validate_record
from agent_harness.managed_review import _review_binding
from agent_harness.supervisor import Supervisor
from agent_harness.task_packets import invoke, NotDispatched
from agent_harness.strict_yaml import load
import test_verified_flow as fixtures


class ManagedLifecycleAdmissionTests(unittest.TestCase):
    setUp = fixtures.VerifiedFlowTests.setUp
    fixture = fixtures.VerifiedFlowTests.fixture

    def planner_fixture(self, **options):
        supervisor, fake = self.fixture(**options)
        script = fake.read_text()
        planner = ("if 'Return a complete map of coherent bounded outcomes.' in prompt:\n plan=json.loads(" +
            repr(json.dumps(load(self.root / 'plan.yaml'))) + ")\n event('item.completed',item={'type':'agent_message','text':json.dumps(plan)});event('turn.completed',usage={'input_tokens':10,'output_tokens':3});raise SystemExit\n")
        fake.write_text(script.replace("if 'HARNESS_FINAL_REVIEW' in prompt:\n", planner + "if 'HARNESS_FINAL_REVIEW' in prompt:\n"))
        supervisor.plan_input = None
        return supervisor, fake

    def test_unresolved_planner_never_admits_workers_even_with_free_slots(self):
        supervisor, _ = self.planner_fixture()
        run = supervisor.adapter.run
        def missing_cleanup(prompt, **options):
            result = run(prompt, **options)
            if 'Return a complete map of coherent bounded outcomes.' in prompt:
                result['process_tree_stopped'] = None
            return result
        with patch.object(supervisor.adapter, 'run', side_effect=missing_cleanup):
            result = supervisor.start()
        observed = {'status': result['status'], 'intents': [r['node_id'] for r in supervisor.receipts().records() if r['record_type']=='invocation_intent']}
        self.assertEqual(observed, {'status':'UNKNOWN_OUTCOME', 'intents':['@planner']})
        self.assertIsNone(supervisor.state['plan_path'])
        saved = supervisor.receipts().records()
        completion = next(r for r in saved if r['record_type']=='completion')
        self.assertIsNone(completion['payload']['process_tree_stopped'])
        registry = GlobalSlots(supervisor.state_dir)
        metrics = deepcopy(supervisor.state['metrics'])
        for _ in range(2):
            accounting = supervisor.receipts().reconcile_usage()
            registry.reconcile(supervisor.state['run_id'], accounting)
            self.assertEqual(supervisor.state['metrics'], metrics)
        self.assertTrue(any(r['unresolved'] for r in registry.inventory(supervisor.state['run_id'])))
        with registry.acquire('independent-run', 'independent-task', 'owner', timeout=1) as lease:
            lease.complete()
        self.assertEqual(supervisor.receipts().records(), saved)

    def test_planner_timeout_respects_original_turn_cap(self):
        supervisor, _ = self.planner_fixture(contract_edit=lambda c: c['budgets'].update(turn_timeout_seconds=17))
        observed = {}
        class Capture(BaseException):
            pass
        def capture(prompt, **options):
            observed.update(options)
            raise Capture()
        with patch.object(supervisor, 'remaining', return_value=90), patch.object(supervisor.adapter, 'run', side_effect=capture), self.assertRaises(Capture):
            managed_execution._plan(supervisor, supervisor.adapter.capabilities())
        self.assertEqual(observed['timeout'], 17)

    def test_shared_guard_role_outcome_cleanup_matrix_fresh_and_replay(self):
        supervisor, _ = self.fixture()
        receipts = supervisor.receipts()
        cases = accepted = 0
        for role in ('@planner', 'worker', '@integration', '@review'):
            intent = receipts.intent(role, 'matrix-1', checkout=self.root, owner='matrix-owner',
                review_binding=_review_binding(supervisor) if role=='@review' else None)
            completion = receipts.completion(intent, {'status':'completed', 'process_tree_stopped':True,
                'turn_started':True, 'usage':{'input_tokens':3,'output_tokens':1}})
            persisted = receipts.reconcile_usage()
            self.assertEqual(post_invocation_guard(intent, completion, persisted), 'completed')
            for phase, outcome, cleanup in product(('fresh','replay'), ('completed','failed','unknown'), (True,False,None,'absent',1,'true',{})):
                cases += 1
                value = deepcopy(completion)
                value['payload']['outcome'] = outcome
                if cleanup=='absent':
                    value['payload'].pop('process_tree_stopped')
                else:
                    value['payload']['process_tree_stopped'] = cleanup
                value['payload_digest'] = digest({k:v for k,v in value.items() if k!='payload_digest'})
                if phase=='replay':
                    value=json.loads(json.dumps(value))
                accounting={'intents':{intent['invocation_id']:intent},'completions':{intent['invocation_id']:value}}
                with self.subTest(role=role, phase=phase, outcome=outcome, cleanup=cleanup):
                    if cleanup is True and outcome!='unknown':
                        self.assertEqual(post_invocation_guard(intent,value,accounting), outcome)
                        accepted += 1
                    else:
                        with self.assertRaises(InvocationUnresolved):
                            post_invocation_guard(intent,value,accounting)
        self.assertEqual((cases,accepted), (168,16))

    def test_subject_digest_and_accounting_mismatch_cannot_admit(self):
        supervisor, _ = self.fixture()
        receipts=supervisor.receipts()
        intent=receipts.intent('worker','attempt-1',checkout=self.root,owner='owner')
        original=receipts.completion(intent,{'status':'completed','process_tree_stopped':True})
        for field in ('project_id','run_id','task_id','node_id','attempt_id','invocation_id','plan_digest','input_refs','intent_ref','payload_digest','missing-accounting'):
            value=deepcopy(original)
            if field=='intent_ref': value['payload']['intent_ref']='another-intent'
            elif field not in ('payload_digest','missing-accounting'): value[field]=['other'] if field=='input_refs' else 'a'*64 if field=='plan_digest' else 'other'
            value['payload_digest']=digest({k:v for k,v in value.items() if k!='payload_digest'})
            if field=='payload_digest': value['payload_digest']='0'*64
            accounting={'intents':{intent['invocation_id']:intent},'completions':{intent['invocation_id']:value}}
            if field=='missing-accounting': accounting['completions'].clear()
            with self.subTest(field=field), self.assertRaises(InvocationUnresolved):
                post_invocation_guard(intent,value,accounting)

    def test_raw_cleanup_null_absent_false_and_wrong_types_are_not_coerced(self):
        supervisor, _ = self.fixture()
        receipts=supervisor.receipts()
        for index, cleanup in enumerate((None,'absent',False,1,'true',{})):
            intent=receipts.intent('worker','attempt-'+str(index),checkout=self.root,owner='owner')
            raw={'status':'completed'}
            if cleanup!='absent': raw['process_tree_stopped']=cleanup
            with self.subTest(cleanup=cleanup):
                if cleanup is None or cleanup=='absent' or cleanup is False:
                    completion=receipts.completion(intent,raw)
                    self.assertIs(completion['payload']['process_tree_stopped'],False if cleanup is False else None)
                    with self.assertRaises(InvocationUnresolved): guard_invocations(supervisor)
                else:
                    with self.assertRaises(HarnessError): receipts.completion(intent,raw)
                    self.assertFalse(any(r['record_type']=='completion' and r['invocation_id']==intent['invocation_id'] for r in receipts.records()))

    def test_no_dispatch_requires_consistent_turn_usage_and_cleanup_and_waits_for_accounting(self):
        supervisor, _=self.fixture()
        receipts=supervisor.receipts()
        valid={'status':'failed','dispatch_state':'not_dispatched_proven','process_tree_stopped':True,'turn_started':False,'usage':None}
        for index, change in enumerate(({'turn_started':True},{'turn_started':None},{'turn_started':0},{'usage':{}},
                {'usage':{'input_tokens':0,'output_tokens':0}},{'process_tree_stopped':False},{'process_tree_stopped':None},
                {'process_tree_stopped':1},{'status':'completed'})):
            intent=receipts.intent('worker','contradiction-'+str(index),checkout=self.root,owner='owner')
            with self.subTest(change=change), self.assertRaises(HarnessError): receipts.completion(intent,dict(valid,**change))
        with GlobalSlots(supervisor.state_dir).acquire(supervisor.state['run_id'],'decline','owner',timeout=1) as lease:
            intent=receipts.intent('decline','local-refusal',checkout=self.root,owner=lease.value['lease_id'])
            with patch.object(supervisor.adapter,'run',side_effect=NotDispatched('local validation refused')):
                result=invoke(supervisor.adapter,'valid required prompt',slot=lease)
            self.assertFalse(lease.completed)
            completion=receipts.completion(intent,result)
            self.assertEqual(post_invocation_guard(intent,completion,receipts.reconcile_usage()),'failed')
            lease.complete(result)
        self.assertFalse(any(r['unresolved'] for r in GlobalSlots(supervisor.state_dir).inventory(supervisor.state['run_id'])))

    def test_real_role_paths_block_uncertain_cleanup_fresh_and_exact_replay(self):
        for index,(role,cleanup) in enumerate((('@planner',None),('left',False),('@integration','absent'),('@review',None))):
            if index: self.setUp()
            supervisor,fake=self.planner_fixture()
            run=supervisor.adapter.run
            def uncertain(prompt, **options):
                node=next(r['node_id'] for r in reversed(supervisor.receipts().records()) if r['record_type']=='invocation_intent')
                result=run(prompt,**options)
                if node==role:
                    if cleanup=='absent': result.pop('process_tree_stopped',None)
                    else: result['process_tree_stopped']=cleanup
                return result
            with self.subTest(role=role),patch.object(supervisor.adapter,'run',side_effect=uncertain):
                self.assertEqual(supervisor.start()['status'],'UNKNOWN_OUTCOME')
            before=supervisor.receipts().records(); calls=fake.with_suffix('.calls').read_bytes()
            intents=[r for r in before if r['record_type']=='invocation_intent']
            self.assertEqual(intents[-1]['node_id'],role)
            for _ in range(2):
                with self.assertRaisesRegex(HarnessError,'uncertain writers'):
                    Supervisor(supervisor.path,adapter=supervisor.adapter).resume()
            # Also test the role replay entry independently of slot inventory.
            with supervisor.store.lease('test exact replay'),patch.object(supervisor.adapter,'run',side_effect=AssertionError('no duplicate invocation')):
                self.assertEqual(managed_execution.execute_managed(supervisor,recovering=True)['status'],'UNKNOWN_OUTCOME')
            self.assertEqual(supervisor.receipts().records(),before)
            self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)

    def test_real_wrong_cleanup_type_keeps_slot_unresolved_without_downstream_intent(self):
        for index,cleanup in enumerate((1,'true',{})):
            if index: self.setUp()
            supervisor,_=self.planner_fixture()
            run=supervisor.adapter.run
            def wrong(prompt,**options):
                result=run(prompt,**options); result['process_tree_stopped']=cleanup; return result
            with self.subTest(cleanup=cleanup),patch.object(supervisor.adapter,'run',side_effect=wrong):
                self.assertEqual(supervisor.start()['status'],'UNKNOWN_OUTCOME')
            records=supervisor.receipts().records()
            self.assertEqual([r['node_id'] for r in records if r['record_type']=='invocation_intent'],['@planner'])
            self.assertFalse(any(r['record_type']=='completion' for r in records))
            self.assertTrue(any(r['unresolved'] for r in GlobalSlots(supervisor.state_dir).inventory(supervisor.state['run_id'])))

    def test_real_stopped_roles_respect_timeout_caps_and_original_deadline(self):
        supervisor,fake=self.planner_fixture(wall_seconds=fixtures.TWO_PHASE_WALL_SECONDS,
            contract_edit=lambda c:c['budgets'].update(turn_timeout_seconds=17))
        deadline=supervisor.state['deadline']; observed={}; run=supervisor.adapter.run
        def timed(prompt,**options):
            node=next(r['node_id'] for r in reversed(supervisor.receipts().records()) if r['record_type']=='invocation_intent')
            observed[node]=options['timeout']; return run(prompt,**options)
        with patch.object(supervisor,'remaining',return_value=90),patch.object(supervisor.adapter,'run',side_effect=timed):
            result=supervisor.start()
        self.assertEqual(result['status'],'ACCEPTED',result)
        self.assertEqual(set(observed),{'@planner','@plan-quality','left','right','@integration','@review'})
        self.assertTrue(all(value==17 for value in observed.values()),observed)
        self.assertEqual(supervisor.state['deadline'],deadline)
        calls=fake.with_suffix('.calls').read_bytes()
        self.assertEqual(Supervisor(supervisor.path,adapter=supervisor.adapter).resume()['status'],'ACCEPTED')
        self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
        for remaining,turn in ((1000,900),(90,17),(7,17)):
            with patch.object(supervisor,'remaining',return_value=remaining):
                supervisor.contract['budgets']['turn_timeout_seconds']=turn
                for role in ('planner','worker','root','reviewer'):
                    self.assertEqual(invocation_timeout(supervisor,role),min(remaining,turn,300) if role=='planner' else min(remaining,turn))
                self.assertEqual(invocation_timeout(supervisor,'worker',node_remaining=3),min(remaining,turn,3))

    def test_known_stopped_failed_planner_never_becomes_a_successful_plan(self):
        supervisor,fake=self.planner_fixture(legacy=True)
        marker="event('item.completed',item={'type':'agent_message','text':json.dumps(plan)});event('turn.completed',usage={'input_tokens':10,'output_tokens':3});raise SystemExit\n"
        self.assertEqual(fake.read_text().count(marker),1)
        script=fake.read_text().replace(marker, "event('turn.failed',error={'message':'planner fixture failure'});raise SystemExit(1)\n",1)
        fake.write_text(script)
        self.assertEqual(supervisor.start()['status'],'BLOCKED_AMBIGUOUS')
        self.assertIsNone(supervisor.state['plan_path'])
        calls=fake.with_suffix('.calls').read_bytes(); deadline=supervisor.state['deadline']
        result=Supervisor(supervisor.path,adapter=supervisor.adapter).resume()
        self.assertEqual(result['status'],'FAILED_TERMINAL')
        self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
        self.assertEqual(result['metrics']['model_invocations'],1)
        self.assertEqual(supervisor.state['deadline'],deadline)

    def test_legacy_contract_keeps_original_admission_policy(self):
        supervisor,_=self.fixture(verified=False)
        receipts=supervisor.receipts()
        intent=receipts.intent('@planner','legacy-1',checkout=self.root,owner='owner')
        receipts.completion(intent,{'status':'completed','process_tree_stopped':None})
        self.assertEqual(guard_invocations(supervisor)['intents'][intent['invocation_id']],intent)
        declined=receipts.intent('legacy-decline','legacy-2',checkout=self.root,owner='owner')
        completion=receipts.completion(declined,{'status':'failed','dispatch_state':'not_dispatched_proven','process_tree_stopped':True})
        self.assertEqual(completion['payload']['usage_status'],'not_applicable')


if __name__ == '__main__':
    unittest.main()
