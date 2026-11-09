"""Pinned custom corpus readiness and concrete contained-driver integration.

Synthetic transport injection is explicit; no real model or isolation claim.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness import evaluation, strict_yaml
from agent_harness.benchmark_bundles import (BUNDLE, MATERIALIZED, LocalTaskBundleAdapter,
    TaskBundleBoundary, materialize_task_bundle, verify_materialized, evaluate_materialized, load_bundle, _hash)
from agent_harness.benchmark_corpora import CORPUS, REVISION, export_own_corpus
from agent_harness.benchmark_milestones import MilestoneObserver, EVENT
from agent_harness.benchmark_bundles import ORACLE, GRADE_INPUT
from agent_harness.benchmark_runner import BenchmarkRunner, BudgetAuthorization, production_status
from agent_harness.codex_execution import CodexAdapter
from agent_harness.core import HarnessError, digest

FAKE = r'''
import json,pathlib,re,sys,uuid
if '--version' in sys.argv: print('bundle-fixture');raise SystemExit
if '--help' in sys.argv: print('exec resume SESSION_ID --json --output-schema');raise SystemExit
if '--control' in sys.argv:
 v=json.loads(sys.argv[-1]);v['controls']={k:'synthetic_denied' for k in ('filesystem','network','evaluator','peer_state')};print(json.dumps(v));raise SystemExit
with pathlib.Path(__file__).with_suffix('.calls').open('a') as f:f.write('synthetic\n')
answers=json.loads(sys.argv[1]);task=re.search(r'"task": "([^"]+)"',pathlib.Path('README.md').read_text())[1]
pathlib.Path('checkpoint.json').write_text(json.dumps({'stage':'inputs_verified','task':task}))
pathlib.Path('answer.json').write_text(json.dumps(answers[task]))
print(json.dumps({'type':'thread.started','thread_id':'fixture-'+uuid.uuid4().hex}),flush=True)
print(json.dumps({'type':'turn.started'}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':1}}),flush=True)
'''


class ContainedBoundary(TaskBundleBoundary):
    def __init__(self, root):
        self.fake = root / 'synthetic.py'
        self.fake.write_text(FAKE)
        self.answers = {item['id']: item['answer'] for item in CORPUS}
        self.kind = 'synthetic'
        self.routes = []

    def identity(self):
        return {'adapter_id':'test-only-boundary','build_digest':_hash(self.fake.read_bytes()),
            'boundary_digest':digest(['explicit synthetic injection', self.kind]),'measurement_kind':self.kind,
            'model':'fixture-model','provider':'fixture-route','client_version':'bundle-fixture'}

    def control_argv(self, challenge, *, deadline=None):
        return [sys.executable,str(self.fake),'--control',json.dumps(challenge)]

    def estimate_trial_tokens(self, row, bundle):
        return 4

    def codex(self, prepared, *, deadline=None):
        return CodexAdapter((sys.executable,str(self.fake),json.dumps(self.answers)))

    def evaluator_argv(self, argv, prepared, *, deadline=None):
        self.routes.append(argv)
        return argv

    def measurements(self, row, prepared, result, *, deadline=None):
        # These counters describe this contained fixture only. They are not an
        # observed production metrics producer or a claimed recovery success.
        return {'mandatory_nodes_skipped':0,'duplicate_writes':0,'recovery':'not_applicable',
            'useful_retrieval':None,'stale_reuse':0,'hook_overhead_ms':None}


class BundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root/'assets'
        self.corpus = export_own_corpus(self.assets)
        self.suite = self.corpus['suites'][0]
        self.task = self.suite['tasks'][0]
        self.row = {'suite':self.suite['id'],'task_id':self.task['id'],'trial_id':'1'*64,'arm':'plain_stock','repeat':0}

    def materialize(self, *, row=None, ref=None, name='trial'):
        return materialize_task_bundle(ref or self.task['source'],assets_root=self.assets,trial_root=self.root/name,
            row=row or self.row,manifest_digest='2'*64)

    def grade_input(self, value):
        path=self.root/'grade.yaml'
        strict_yaml.dump(path,{'schema_version':1,'manifest_digest':value['manifest_digest'],'trial_id':value['trial_id'],
            'evaluator_digest':self.corpus['evaluator']['sha256'],'solve_digest':'3'*64,'cwd':value['source_root'],'solve_outcome':'completed'})
        return path

    def fill(self, value, task_id=None):
        item=next(item for item in CORPUS if item['id']==(task_id or value['task_id']))
        source=Path(value['source_root'])
        (source/'checkpoint.json').write_text(json.dumps({'stage':'inputs_verified','task':item['id']}))
        (source/'answer.json').write_text(json.dumps(item['answer']))

    def test_six_versioned_tasks_materialize_and_grade_with_separate_gold(self):
        self.assertEqual(len(CORPUS),6)
        self.assertEqual({item['suite'] for item in CORPUS},set(evaluation.SUITES[-2:]))
        for suite in self.corpus['suites']:
            for task in suite['tasks']:
                row={**self.row,'suite':suite['id'],'task_id':task['id'],'trial_id':digest(task)}
                value=self.materialize(row=row,ref=task['source'],name=task['id'])
                self.assertEqual(value['source_root'],str(self.root/task['id']/'source'))
                self.assertFalse((Path(value['source_root'])/'oracle.yaml').exists())
                self.fill(value)
                verify_materialized(self.root/task['id']/'materialized.yaml')
                self.assertEqual(evaluate_materialized(self.root/task['id']/'materialized.yaml',self.grade_input(value))['outcome'],'accepted')

    def test_closed_schema_hash_paths_and_subject_fail_before_materialization(self):
        original=strict_yaml.load(self.assets/self.task['source']['path'])
        mutations=[lambda b:b.update(unknown=True),lambda b:b.update(schema_version=2),
            lambda b:b['files'][0].update(path='../escape'),lambda b:b['files'][0].update(path='.codex/config.toml'),
            lambda b:b['files'].append(deepcopy(b['files'][0])),lambda b:b.update(task_id='foreign'),
            lambda b:b['files'][0]['source'].update(sha256='0'*64)]
        for index,mutate in enumerate(mutations):
            bundle=deepcopy(original);mutate(bundle)
            path=self.assets/('bad'+str(index)+'.yaml');strict_yaml.dump(path,bundle)
            with self.assertRaises((ValueError,HarnessError)):
                self.materialize(ref={'path':path.name,'sha256':_hash(path.read_bytes())},name='badtrial'+str(index))
            self.assertFalse((self.root/('badtrial'+str(index))).exists())

    def test_allowed_result_changes_pass_immutable_and_protected_changes_refuse(self):
        value=self.materialize();receipt=self.root/'trial/materialized.yaml'
        self.fill(value)
        verify_materialized(receipt)
        with self.assertRaisesRegex(HarnessError,'MATERIALIZED_INPUT_CHANGED'):
            verify_materialized(receipt,initial=True)
        immutable=Path(value['source_root'])/value['immutable_files'][0]['path']
        immutable.write_text('changed')
        with self.assertRaisesRegex(HarnessError,'MATERIALIZED_INPUT_CHANGED'):
            verify_materialized(receipt)

    def test_gold_changes_and_parent_replacement_are_not_accepted(self):
        value=self.materialize();receipt=self.root/'trial/materialized.yaml';self.fill(value)
        gold=Path(value['evaluator_root'])/'expected-0.json';gold.write_text('{}')
        with self.assertRaisesRegex(HarnessError,'MATERIALIZED_INPUT_CHANGED'):
            evaluate_materialized(receipt,self.grade_input(value))
        other=self.materialize(name='other');source=Path(other['source_root']);source.rename(source.with_name('old-source'));source.mkdir()
        with self.assertRaises(ValueError):
            verify_materialized(self.root/'other/materialized.yaml')

    def test_false_acceptance_json_boolean_duplicate_and_extra_output_refuse(self):
        value=self.materialize();receipt=self.root/'trial/materialized.yaml';self.fill(value);inputs=self.grade_input(value)
        for raw in ('true','{"known_tokens":7,"known_tokens":7}',json.dumps({'remaining':['right','join'],'completed':['left'],'input_tokens':True,'output_tokens':2})):
            (Path(value['source_root'])/'answer.json').write_text(raw)
            self.assertEqual(evaluate_materialized(receipt,inputs)['outcome'],'failed')
        (Path(value['source_root'])/'undeclared.txt').write_text('leak')
        with self.assertRaisesRegex(HarnessError,'UNDECLARED_OUTPUT'):
            verify_materialized(receipt)

    def test_grade_input_cannot_supply_acceptance_or_skip_artifact_checks(self):
        value=self.materialize();receipt=self.root/'trial/materialized.yaml';inputs=self.grade_input(value)
        self.assertEqual(evaluate_materialized(receipt,inputs)['outcome'],'failed')
        original=strict_yaml.load(inputs)
        for mutation in ({'solve_outcome':'accepted'},{'solve_outcome':True},{'unknown':True},{'schema_version':2}):
            strict_yaml.dump(inputs,{**original,**mutation})
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                evaluate_materialized(receipt,inputs)
        strict_yaml.dump(inputs,{**original,'solve_outcome':'unknown'})
        with self.assertRaisesRegex(HarnessError,'UNKNOWN_SOLVE_OUTCOME'):
            evaluate_materialized(receipt,inputs)
        strict_yaml.dump(inputs,original)
        self.fill(value)
        self.assertEqual(evaluate_materialized(receipt,inputs)['outcome'],'accepted')
        answer=next(item['answer'] for item in CORPUS if item['id']==value['task_id'])
        (Path(value['source_root'])/'answer.json').write_text(json.dumps({**answer,'input_tokens':True}))
        self.assertEqual(evaluate_materialized(receipt,inputs)['outcome'],'failed')

    def test_no_blind_rematerialization_and_original_deadline(self):
        self.materialize()
        with self.assertRaisesRegex(HarnessError,'MATERIALIZATION_EXISTS_OR_UNKNOWN'):
            self.materialize()
        past=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(HarnessError,'DEADLINE_EXHAUSTED'):
            materialize_task_bundle(self.task['source'],assets_root=self.assets,trial_root=self.root/'late',row=self.row,manifest_digest='2'*64,deadline=past)
        self.assertFalse((self.root/'late').exists())

    def test_milestones_order_claim_reopen_and_explicit_unknown_reconciliation(self):
        value=self.materialize();receipt=self.root/'trial/materialized.yaml';observer=MilestoneObserver(receipt,self.root/'controller')
        self.assertIsNone(observer.observe())
        self.fill(value)
        observed=observer.observe();self.assertEqual(observed['milestone_id'],'inputs-verified')
        intent=observer.claim_fault('inputs-verified')
        reopened=MilestoneObserver(receipt,self.root/'controller')
        with self.assertRaisesRegex(HarnessError,'ALREADY_CLAIMED'):
            reopened.claim_fault('inputs-verified')
        completion=reopened.reconcile_fault(intent['operation_id'],'unknown')
        self.assertEqual(reopened.reconcile_fault(intent['operation_id'],'unknown'),completion)
        with self.assertRaisesRegex(HarnessError,'CONFLICTING_FAULT_OUTCOME'):
            reopened.reconcile_fault(intent['operation_id'],'applied')
        self.assertEqual(reopened.observe()['milestone_id'],'answer-published')
        self.assertIsNone(reopened.observe())

    def test_default_adapter_unavailable_before_writes_and_observed_stays_closed(self):
        adapter=LocalTaskBundleAdapter()
        with self.assertRaisesRegex(HarnessError,'ISOLATION_UNAVAILABLE'):
            adapter.identity()
        with self.assertRaisesRegex(HarnessError,'ISOLATION_UNAVAILABLE'):
            adapter.prepare(self.row,{},self.assets,self.root/'unqualified')
        self.assertFalse((self.root/'unqualified').exists())
        self.assertEqual(production_status()['registered_adapters'],[])

    def test_exported_schemas_match_runtime_closed_contracts(self):
        for name,value in [('benchmark-task-bundle',BUNDLE),('benchmark-artifact-oracle',ORACLE),
                           ('benchmark-materialized-task',MATERIALIZED),('benchmark-milestone',EVENT),
                           ('benchmark-grade-input',GRADE_INPUT)]:
            exported=json.loads((Path(__file__).parents[1]/'schemas'/(name+'.schema.json')).read_text())
            self.assertEqual(exported,value)
            self.assertTrue(all(not branch.get('additionalProperties',False) for branch in value.get('oneOf',[value])))

    def test_typed_git_materialization_preserves_project_docs_and_legacy_refusal(self):
        from agent_harness.benchmark_bundles import MATERIALIZED_V1
        bundle=strict_yaml.load(self.assets/self.task['source']['path'])
        project=self.assets/'project-agents.md';project.write_text('Project-local instruction stays visible.\n')
        bundle['files'].append({'path':'AGENTS.md','source':{'path':project.name,'sha256':_hash(project.read_bytes())}})
        raw=self.assets/'git-bundle.yaml';strict_yaml.dump(raw,bundle,BUNDLE)
        ref={'path':raw.name,'sha256':_hash(raw.read_bytes())}
        value=materialize_task_bundle(ref,assets_root=self.assets,trial_root=self.root/'git-trial',row=self.row,manifest_digest='2'*64,fresh_git=True)
        receipt=self.root/'git-trial/materialized.yaml'
        self.assertEqual(value['schema_version'],2)
        self.assertEqual((Path(value['source_root'])/'AGENTS.md').read_bytes(),project.read_bytes())
        with self.assertRaises(ValueError):strict_yaml.validate(value,MATERIALIZED_V1)
        verify_materialized(receipt,initial=True)
        self.fill(value)
        observer=MilestoneObserver(receipt,self.root/'git-controller')
        self.assertEqual(observer.observe()['milestone_id'],'inputs-verified')
        git=Path(value['source_root'])/'.git';git.rename(git.with_name('replaced-git'));git.mkdir()
        with self.assertRaises(ValueError):verify_materialized(receipt)

    def test_legacy_materialization_does_not_trust_added_git_metadata(self):
        value=self.materialize();source=Path(value['source_root']);(source/'.git').mkdir();(source/'.git/config').write_text('untrusted')
        with self.assertRaisesRegex(HarnessError,'UNDECLARED_OUTPUT'):
            verify_materialized(self.root/'trial/materialized.yaml')

    def test_interrupted_materialization_preserves_partial_without_replay(self):
        from agent_harness import benchmark_bundles
        original=benchmark_bundles.atomic_write
        count=0
        def interrupted(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:
                raise OSError('controlled materialization interruption')
            return original(*args,**kwargs)
        with patch.object(benchmark_bundles,'atomic_write',interrupted),self.assertRaises(OSError):
            self.materialize()
        before=list((self.root/'trial/source').rglob('*'))
        self.assertTrue(before)
        with self.assertRaisesRegex(HarnessError,'MATERIALIZATION_EXISTS_OR_UNKNOWN'):
            self.materialize()
        self.assertEqual(list((self.root/'trial/source').rglob('*')),before)

    def manifest(self):
        # External suite slots are normalized custom controls, explicitly not
        # official assets/cohorts. This tests driver protocol completeness only.
        suites=deepcopy(self.corpus['suites'])
        original=strict_yaml.load(self.assets/self.task['source']['path'])
        for suite_id in evaluation.SUITES[:3]:
            bundle={**original,'suite':suite_id,'provenance':'normalized_external'}
            path=self.assets/(suite_id+'.yaml');strict_yaml.dump(path,bundle,BUNDLE)
            suites.append({**self.suite,'id':suite_id,'revision':'synthetic-normalized-custom-v1','tasks':[{'id':self.task['id'],'source':{'path':path.name,'sha256':_hash(path.read_bytes())}}]})
        ref=self.corpus['index']
        value={'schema_version':1,'record_type':'benchmark_manifest','comparison':'package','seed':0,'repeats':1,'bootstrap_resamples':100,'confidence':.95,'minimum_token_gain':.05,'minimum_quality_delta':0,
            'model':'fixture-model','provider':'fixture-route','client_version':'bundle-fixture','root_policy':ref,'child_policy':ref,'isolation':ref,'approved_budget':ref,'suites':suites}
        for seed in range(100):
            value['seed']=seed
            first=evaluation.schedule(value)[0]
            if first['arm']=='plain_stock' and first['suite']=='harness-recovery':
                return value
        self.fail('deterministic pre-result seed search did not find plain own task')

    def test_concrete_adapter_drives_frozen_trial_and_reopen_without_replay(self):
        boundary=ContainedBoundary(self.assets);adapter=LocalTaskBundleAdapter(boundary);manifest=self.manifest()
        grant=BudgetAuthorization(digest(manifest),'explicit-contained-test',500,(datetime.now(timezone.utc)+timedelta(seconds=120)).isoformat(),'synthetic')
        runner=BenchmarkRunner.create(manifest,assets_root=self.assets,run_dir=self.root/'run',adapter=adapter,authorization=grant)
        result=runner.advance()
        self.assertEqual(result['completed_trials'],1,result)
        self.assertEqual(result['usage']['observed_tokens'],4)
        self.assertEqual(len(boundary.routes),1)
        self.assertEqual(boundary.fake.with_suffix('.calls').read_text().splitlines(),['synthetic'])
        reopened=BenchmarkRunner(self.root/'run',adapter)
        self.assertEqual(reopened.status()['completed_trials'],1)
        self.assertEqual(boundary.fake.with_suffix('.calls').read_text().splitlines(),['synthetic'])
        prepared=next(r['payload'] for r in reopened.records() if r['kind']=='prepared')
        receipt=Path(prepared['contract_path']).parent/'materialized.yaml'
        data=strict_yaml.load(receipt);data['writable_paths'].append('foreign.json');strict_yaml.dump(receipt,data,MATERIALIZED)
        with self.assertRaisesRegex(HarnessError,'MATERIALIZED_RECEIPT_CHANGED'):
            adapter.codex(prepared)

    def test_prepared_crash_then_source_mutation_blocks_without_model_call(self):
        boundary=ContainedBoundary(self.assets);adapter=LocalTaskBundleAdapter(boundary);manifest=self.manifest()
        grant=BudgetAuthorization(digest(manifest),'explicit-contained-test',500,(datetime.now(timezone.utc)+timedelta(seconds=120)).isoformat(),'synthetic')
        runner=BenchmarkRunner.create(manifest,assets_root=self.assets,run_dir=self.root/'run',adapter=adapter,authorization=grant)
        class ControllerCrash(BaseException):
            pass
        original=runner._record
        def after_prepared(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[0]=='prepared':
                raise ControllerCrash()
            return result
        with patch.object(runner,'_record',after_prepared),self.assertRaises(ControllerCrash):
            runner.advance()
        prepared=next(row['payload'] for row in runner.records() if row['kind']=='prepared')
        (Path(prepared['cwd'])/'README.md').write_text('different task')
        report=BenchmarkRunner(self.root/'run',adapter).advance()
        self.assertEqual(report['reason_code'],'BENCH_PREPARED_SOURCE_CHANGED',report)
        self.assertFalse(boundary.fake.with_suffix('.calls').exists())

    def test_concrete_adapter_cannot_assert_itself_into_observed_registry(self):
        boundary=ContainedBoundary(self.assets);boundary.kind='observed';adapter=LocalTaskBundleAdapter(boundary);manifest=self.manifest()
        grant=BudgetAuthorization(digest(manifest),'explicit-contained-test',500,(datetime.now(timezone.utc)+timedelta(seconds=120)).isoformat(),'observed')
        runner=BenchmarkRunner.create(manifest,assets_root=self.assets,run_dir=self.root/'run',adapter=adapter,authorization=grant)
        self.assertEqual(runner.advance()['reason_code'],'ENVIRONMENT_ISOLATION_UNAVAILABLE')
        self.assertFalse(boundary.fake.with_suffix('.calls').exists())
