"""Subject-chain fixtures exercise actual local evaluator production."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
import test_task_flow_experiment as fixtures
from agent_harness import task_flow_experiment as flow, evaluation as ev, strict_yaml
from agent_harness import benchmark_bundles as bundles
from agent_harness.core import HarnessError, canonical
import test_benchmark_bundles as bundle_fixtures

class TaskFlowGradeChainTests(unittest.TestCase):
    setUp = fixtures.TaskFlowPreparationTests.setUp
    git = fixtures.TaskFlowPreparationTests.git
    put = fixtures.TaskFlowPreparationTests.put
    document = fixtures.TaskFlowPreparationTests.document
    prepare = fixtures.TaskFlowPreparationTests.prepare
    report = fixtures.TaskFlowPreparationTests.report

    def test_legacy_echo_only_grade_never_counts_strict_full_mission(self):
        prepared = self.prepare()
        report = self.report(prepared)
        summary = flow.aggregate(self.output, [report], reports_root=self.assets)
        self.assertEqual(summary['arms'][report['arm']]['reported_full_mission_verified'], 0)

    def actual_chain(self):
        # Adapt a tiny self-authored artifact bundle to the task-flow task ID.
        corpus = bundle_fixtures.export_own_corpus(self.root/'corpus')
        suite = corpus['suites'][0]; source = suite['tasks'][0]['source']
        bundle = strict_yaml.load(self.root/'corpus'/source['path'])
        task = self.spec['tasks'][0]; bundle['task_id'] = task['id']
        bundle_ref = self.put('tiny-bundle.json', bundle)
        # Bundle's relative assets still belong to its original corpus root.
        (self.root/'corpus'/bundle_ref['path']).parent.mkdir(parents=True, exist_ok=True)
        (self.root/'corpus'/bundle_ref['path']).write_bytes((self.assets/bundle_ref['path']).read_bytes())
        provisional = bundles.materialize_task_bundle(bundle_ref, assets_root=self.root/'corpus', trial_root=self.root/'provisional',
            row={'suite':suite['id'],'task_id':task['id'],'trial_id':'a'*64}, manifest_digest='b'*64)
        hidden = [self.put('tiny-hidden/'+path.relative_to(provisional['evaluator_root']).as_posix(), path.read_bytes())
                  for path in Path(provisional['evaluator_root']).rglob('*') if path.is_file()]
        evaluator = self.document(task['hidden_evaluator']); evaluator['files'] = hidden
        task['hidden_evaluator'] = self.put('tiny-evaluator.json', evaluator)
        prepared = self.prepare(); row = next(row for row in prepared['schedule'] if row['task_id'] == task['id'])
        value = bundles.materialize_task_bundle(bundle_ref, assets_root=self.root/'corpus', trial_root=self.root/'actual',
            row={'suite':suite['id'],'task_id':task['id'],'trial_id':row['trial_id']}, manifest_digest=prepared['preparation_digest'])
        original_id = suite['tasks'][0]['id']
        item = next(item for item in bundle_fixtures.CORPUS if item['id'] == original_id)
        source_root = Path(value['source_root'])
        (source_root/'checkpoint.json').write_text(json.dumps({'stage':'inputs_verified','task':original_id}))
        (source_root/'answer.json').write_text(json.dumps(item['answer']))
        grade_path = self.assets/'actual-grade-input.json'
        start = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat().replace('+00:00','Z')
        ev.write_grade_input(grade_path, evidence_root=self.assets, manifest_digest=prepared['preparation_digest'],
            task_id=task['id'], trial_id=row['trial_id'], source_root=source_root,
            evaluator_bytes=b'tiny artifact-json-v1 evaluator runtime pin', toolchain_bytes=b'tiny fixture toolchain pin',
            evaluator_digest=prepared['tasks'][0]['evaluator_bundle_digest'], bundle_digest=value['bundle_digest'],
            solve_digest='a'*64, solve_outcome='completed', requirement_ids=prepared['tasks'][0]['requirements'])
        pipeline = bundles.evaluate_materialized(self.root/'actual/materialized.yaml', grade_path)
        self.assertEqual(pipeline['outcome'],'accepted')
        execution_ref = ev.put_grade_evidence(self.assets, 'pipeline-grade.json', pipeline, ev.EXECUTION_GRADE)
        wrapper_ref = flow.write_mission_grade(self.assets/'mission-grade.json', preparation_dir=self.output,
            trial_id=row['trial_id'], execution_evidence=execution_ref, reports_root=self.assets)
        template = self.report(prepared)
        template.update(started_at=start, finished_at=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'))
        report_ref = flow.write_trial_report(self.assets/'bound-report.json', preparation_dir=self.output,
            evaluator_receipt=wrapper_ref, reports_root=self.assets, report=template)
        report = self.document(report_ref)
        return prepared, report, pipeline

    def test_actual_producer_wrapper_report_and_aggregate_positive(self):
        prepared, report, pipeline = self.actual_chain()
        summary = flow.aggregate(self.output,[report],reports_root=self.assets)
        self.assertEqual(summary['arms'][report['arm']]['reported_full_mission_verified'],1)
        self.assertEqual(report['candidate_digest'],pipeline['subject']['candidate_digest'])
        self.assertNotEqual(report['candidate_digest'],'b'*64)
        self.assertEqual(summary['status'],'supplied_reports_unqualified')
        self.assertFalse(summary['production_qualified']); self.assertIsNone(summary['winner'])

    def test_X_Y_subject_mismatch_each_report_and_wrapper_edge(self):
        prepared, report, pipeline = self.actual_chain()
        for key in ('preparation_digest','trial_id','candidate_digest','candidate_scheme','evaluator_input_digest'):
            changed = deepcopy(report); changed[key] = 'Y' if key=='candidate_scheme' else 'f'*64
            with self.subTest(report=key),self.assertRaises((HarnessError,ValueError)):
                flow.aggregate(self.output,[changed],reports_root=self.assets)
        wrapper = self.document(report['evaluator_receipt'])
        for key in ('preparation_digest','trial_id','candidate_digest','candidate_scheme','evaluator_bundle_digest','evaluator_input_digest','finished_at'):
            changed = deepcopy(wrapper); changed[key] = 'Y' if key=='candidate_scheme' else '2020-01-01T00:00:00Z' if key=='finished_at' else 'f'*64
            ref = self.put('changed-'+key+'.json',changed)
            changed_report = {**report,'evaluator_receipt':ref}
            with self.subTest(wrapper=key),self.assertRaises((HarnessError,ValueError)):
                flow.aggregate(self.output,[changed_report],reports_root=self.assets)
        for key in report['subject']:
            changed = deepcopy(report); changed['subject'][key] = 'Y' if key in ('task_id','candidate_scheme') else 'f'*64
            with self.subTest(subject=key),self.assertRaises((HarnessError,ValueError)):
                flow.aggregate(self.output,[changed],reports_root=self.assets)


    def test_resigned_actual_inputs_cannot_omit_frozen_hidden_evaluator_bytes(self):
        prepared, report, pipeline = self.actual_chain()
        inputs = ev._evidence_record(pipeline['evaluator_input'],self.assets,ev.EVALUATOR_INPUT)
        inputs['artifacts'] = [row for row in inputs['artifacts'] if row['role'] != 'protected']
        input_ref = ev.put_grade_evidence(self.assets,'changed-input.json',inputs,ev.EVALUATOR_INPUT)
        input_digest = ev.digest(inputs)
        execution = ev._evidence_record(pipeline['execution'],self.assets,ev.EVALUATOR_EXECUTION)
        execution.update(evaluator_input=input_ref,evaluator_input_digest=input_digest)
        execution_ref = ev.put_grade_evidence(self.assets,'changed-execution.json',execution,ev.EVALUATOR_EXECUTION)
        pipeline.update(evaluator_input=input_ref,evaluator_input_digest=input_digest,execution=execution_ref)
        pipeline_ref = ev.put_grade_evidence(self.assets,'changed-pipeline.json',pipeline,ev.EXECUTION_GRADE)
        wrapper = self.document(report['evaluator_receipt'])
        wrapper.update(execution_evidence=pipeline_ref,evaluator_input_digest=input_digest)
        wrapper_ref = ev.put_grade_evidence(self.assets,'changed-wrapper.json',wrapper,flow.GRADE_V2)
        report.update(evaluator_receipt=wrapper_ref,evaluator_input_digest=input_digest)
        with self.assertRaisesRegex(HarnessError,'INPUT_BINDING'):
            flow.aggregate(self.output,[report],reports_root=self.assets)


if __name__ == '__main__': unittest.main()
