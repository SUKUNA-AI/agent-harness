"""Actual data-only evaluator producer controls; never model qualification."""
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_benchmark_bundles as fixtures
from agent_harness import evaluation as ev, strict_yaml, benchmark_metrics
from agent_harness import benchmark_bundles as bundles
from agent_harness.core import HarnessError, digest


class GradeSubjectChainTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BundleTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.value = self.fixture.materialize()
        self.fixture.fill(self.value)
        self.receipt = self.root/'trial/materialized.yaml'
        self.path = self.root/'input.json'

    def request(self, **changes):
        args = dict(evidence_root=self.root, manifest_digest=self.value['manifest_digest'],
            task_id=self.value['task_id'], trial_id=self.value['trial_id'], source_root=self.value['source_root'],
            evaluator_bytes=(self.fixture.assets/self.fixture.corpus['evaluator']['path']).read_bytes(),
            toolchain_bytes=b'fixture declared toolchain', bundle_digest=self.value['bundle_digest'],
            solve_digest='a'*64, solve_outcome='completed')
        args.update(changes)
        return ev.write_grade_input(self.path, **args)

    def produce(self):
        self.request()
        return bundles.evaluate_materialized(self.receipt, self.path)

    def test_actual_producer_captures_and_verifies_whole_chain(self):
        grade = self.produce()
        self.assertEqual(grade['schema_version'], 2)
        self.assertEqual(grade['outcome'], 'accepted')
        self.assertEqual(ev.verify_execution_grade(grade, self.root), 'verified_subject_chain')
        inputs = ev._evidence_record(grade['evaluator_input'], self.root, ev.EVALUATOR_INPUT)
        self.assertEqual({row['role'] for row in inputs['artifacts']}, {'protected', 'candidate'})
        self.assertTrue(inputs['artifacts'])
        execution = ev._evidence_record(grade['execution'], self.root, ev.EVALUATOR_EXECUTION)
        self.assertEqual(execution['outcome'], 'accepted')
        self.assertEqual(execution['returncode'], 0)
        self.assertLessEqual(execution['started_at'], execution['finished_at'])

    def test_every_subject_field_and_each_receipt_edge_is_checked(self):
        grade = self.produce()
        for key in grade['subject']:
            changed = deepcopy(grade)
            changed['subject'][key] = 'different' if key == 'task_id' else 'b'*64
            with self.subTest(subject=key), self.assertRaises((HarnessError, ValueError)):
                ev.verify_execution_grade(changed, self.root)
        for key in ('evaluator_input_digest', 'evaluator_input', 'execution', 'outcome', 'finished_at'):
            changed = deepcopy(grade)
            if key in ('evaluator_input', 'execution'): changed[key]['sha256'] = 'b'*64
            else: changed[key] = 'failed' if key == 'outcome' else '2020-01-01T00:00:00Z' if key == 'finished_at' else 'b'*64
            with self.subTest(edge=key), self.assertRaises((HarnessError, ValueError)):
                ev.verify_execution_grade(changed, self.root)
        inputs = ev._evidence_record(grade['evaluator_input'], self.root, ev.EVALUATOR_INPUT)
        artifact = self.root/inputs['artifacts'][0]['asset']['path']
        artifact.write_bytes(b'mutated frozen actual evaluator input')
        with self.assertRaisesRegex(HarnessError, 'EVIDENCE_CHANGED'): ev.verify_execution_grade(grade, self.root)

    def test_expected_original_request_ref_must_remain_fresh(self):
        grade = self.produce()
        expected = {'path': self.path.name, 'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest()}
        self.assertEqual(ev.verify_execution_grade(grade,self.root,expected_input_ref=expected),'verified_subject_chain')
        self.path.write_bytes(b'changed original grade input after evaluator returned')
        with self.assertRaisesRegex(HarnessError,'EVIDENCE_CHANGED'):
            ev.verify_execution_grade(grade,self.root,expected_input_ref=expected)

    def test_resigned_execution_cannot_accept_nonzero_exit(self):
        grade = self.produce()
        execution = ev._evidence_record(grade['execution'],self.root,ev.EVALUATOR_EXECUTION)
        execution['returncode'] = 7
        grade['execution'] = ev.put_grade_evidence(self.root,'changed-execution.json',execution,ev.EVALUATOR_EXECUTION)
        with self.assertRaisesRegex(HarnessError,'EXECUTION_BINDING'):
            ev.verify_execution_grade(grade,self.root)

    def test_resigned_actual_input_cannot_omit_captured_candidate_bytes(self):
        grade = self.produce()
        inputs = ev._evidence_record(grade['evaluator_input'],self.root,ev.EVALUATOR_INPUT)
        inputs['artifacts'] = [item for item in inputs['artifacts'] if item['role'] != 'candidate']
        input_ref = ev.put_grade_evidence(self.root,'changed-input.json',inputs,ev.EVALUATOR_INPUT)
        execution = ev._evidence_record(grade['execution'],self.root,ev.EVALUATOR_EXECUTION)
        execution.update(evaluator_input=input_ref,evaluator_input_digest=digest(inputs))
        execution_ref = ev.put_grade_evidence(self.root,'changed-execution.json',execution,ev.EVALUATOR_EXECUTION)
        grade.update(evaluator_input=input_ref,evaluator_input_digest=digest(inputs),execution=execution_ref)
        with self.assertRaisesRegex(HarnessError,'ACTUAL_INPUT_BINDING'):
            ev.verify_execution_grade(grade,self.root)

    def test_candidate_Y_cannot_be_evaluated_as_captured_X(self):
        self.request()
        (Path(self.value['source_root'])/'answer.json').write_text('{}')
        with self.assertRaisesRegex(HarnessError, 'CANDIDATE_DRIFT'):
            bundles.evaluate_materialized(self.receipt, self.path)
        self.assertFalse((self.root/'grading'/self.value['trial_id']/'execution.json').exists())

    def test_candidate_drift_during_actual_evaluation_prevents_grade(self):
        self.request()
        original = bundles.json_equal
        def drift(actual, expected):
            result = original(actual, expected)
            (Path(self.value['source_root'])/'answer.json').write_text('{}')
            return result
        with patch.object(bundles, 'json_equal', side_effect=drift):
            with self.assertRaisesRegex(HarnessError, 'CANDIDATE_DRIFT'):
                bundles.evaluate_materialized(self.receipt, self.path)
        self.assertFalse((self.root/'grading'/self.value['trial_id']/'execution.json').exists())

    def test_transfer_uses_portable_projection_and_actual_destination_snapshot(self):
        request = self.request()
        destination = self.root/'destination'
        shutil.copytree(self.value['source_root'], destination)
        transfer_ref = ev.transfer_candidate(request, destination)
        transfer = ev._evidence_record(transfer_ref, self.root, ev.CANDIDATE_TRANSFER)
        declared = ev._evidence_record(request['candidate_capture'], self.root, ev.CANDIDATE_CAPTURE)
        self.assertEqual(declared['projection'], transfer['destination']['projection'])
        self.assertNotEqual(declared['physical']['snapshot_digest'], transfer['destination']['physical']['snapshot_digest'])
        self.assertNotEqual(declared['physical']['root'], transfer['destination']['physical']['root'])
        (destination/'answer.json').write_text('{}')
        with self.assertRaisesRegex(HarnessError, 'TRANSFER_MISMATCH'):
            ev.transfer_candidate(request, destination)

    def test_producer_refuses_wrong_materialization_subjects(self):
        self.request(bundle_digest='b'*64)
        with self.assertRaisesRegex(HarnessError, 'SUBJECT_MISMATCH'):
            bundles.evaluate_materialized(self.receipt, self.path)

    def test_real_producer_rejects_foreign_trial_preparation_and_task(self):
        request = self.request()
        for key in ('trial_id', 'manifest_digest', 'task_id'):
            changed = deepcopy(request)
            changed['subject'][key] = 'foreign-task' if key == 'task_id' else 'e'*64
            if key in changed: changed[key] = changed['subject'][key]
            path = self.root/('foreign-'+key+'.json')
            ev.put_grade_evidence(self.root,path.name,changed,ev.GRADE_INPUT_V2)
            with self.subTest(subject=key),self.assertRaisesRegex(HarnessError,'SUBJECT_MISMATCH'):
                bundles.evaluate_materialized(self.receipt,path)

    def test_legacy_producer_input_is_readable_but_unverified(self):
        legacy = self.fixture.grade_input(self.value)
        grade = bundles.evaluate_materialized(self.receipt, legacy)
        self.assertEqual(grade['schema_version'], 1)
        self.assertEqual(ev.verify_execution_grade(grade, self.root), 'unverified_legacy')

    def test_runner_binds_actual_request_to_frozen_grade_intent(self):
        boundary = fixtures.ContainedBoundary(self.fixture.assets)
        adapter = bundles.LocalTaskBundleAdapter(boundary)
        manifest = self.fixture.manifest()
        def alternate(argv, prepared, *, deadline=None):
            request = strict_yaml.load(argv[4], ev.GRADE_INPUT_V2)
            request['solve_digest'] = 'f'*64
            ref = ev.put_grade_evidence(request['evidence_root'], 'alternate-input.json', request, ev.GRADE_INPUT_V2)
            argv[4] = str(Path(request['evidence_root'])/ref['path'])
            return argv
        boundary.evaluator_argv = alternate
        grant = fixtures.BudgetAuthorization(digest(manifest), 'contained-intent-control', 500,
            (fixtures.datetime.now(fixtures.timezone.utc)+fixtures.timedelta(seconds=120)).isoformat(), 'synthetic')
        runner = fixtures.BenchmarkRunner.create(manifest, assets_root=self.fixture.assets,
            run_dir=self.root/'run', adapter=adapter, authorization=grant)
        result = runner.advance()
        self.assertEqual(result.get('reason_code'), 'BENCH_GRADE_INPUT_BINDING', result)
        self.assertFalse(any(row['kind'] == 'grade' for row in runner.records()))
        self.assertEqual(runner.advance()['reason_code'], 'BENCH_UNKNOWN_GRADE_OUTCOME')

    def test_real_runner_and_aggregate_preserve_both_telemetry_contracts(self):
        for version in (3, 4):
            with self.subTest(trial_version=version):
                boundary = fixtures.ContainedBoundary(self.fixture.assets)
                if version == 4:
                    boundary.measurements = lambda *args, **kwargs: benchmark_metrics.unavailable_measurements()
                adapter = bundles.LocalTaskBundleAdapter(boundary)
                manifest = self.fixture.manifest()
                grant = fixtures.BudgetAuthorization(digest(manifest), 'contained-version-control', 500,
                    (fixtures.datetime.now(fixtures.timezone.utc)+fixtures.timedelta(seconds=120)).isoformat(), 'synthetic')
                runner = fixtures.BenchmarkRunner.create(manifest, assets_root=self.fixture.assets,
                    run_dir=self.root/('run-v'+str(version)), adapter=adapter, authorization=grant)
                result = runner.advance()
                self.assertEqual(result['completed_trials'], 1, result)
                trial = next(row['payload'] for row in runner.records() if row['kind'] == 'trial')
                self.assertEqual(trial['schema_version'], version)
                schema = ev.TRIAL_V3 if version == 3 else ev.TRIAL_V4
                strict_yaml.validate(trial, schema)
                exported = Path(__file__).parents[1]/('schemas/benchmark-trial-v'+str(version)+'.schema.json')
                self.assertEqual(json.loads(exported.read_bytes()), schema)
                summary = ev.summarize(manifest, [trial], runner.store.root)
                self.assertEqual(summary['grade_binding']['verified_subject_chain'], 1)
                self.assertEqual(summary['grade_binding']['unverified_legacy'], 0)
                # New draft shapes cannot masquerade as a frozen legacy form.
                missing_version = deepcopy(trial)
                del missing_version['grade_subject']['subject_version']
                with self.assertRaises(ValueError):
                    ev.summarize(manifest, [missing_version], runner.store.root)
                missing_binding = deepcopy(trial)
                del missing_binding['evaluator_input_digest']
                with self.assertRaises(ValueError):
                    ev.summarize(manifest, [missing_binding], runner.store.root)

    def test_typed_records_exceeding_reader_bound_are_never_published(self):
        with self.assertRaisesRegex(HarnessError,'EVIDENCE_BOUND'):
            ev.put_grade_evidence(self.root,'oversized.json',['x'*200000]*12,{'type':'array','items':{'type':'string'}})
        self.assertFalse((self.root/'oversized.json').exists())

    def test_real_closed_schemas_and_exported_new_versions(self):
        grade = self.produce()
        strict_yaml.validate(grade, ev.EXECUTION_GRADE)
        self.assertEqual(grade['subject']['subject_version'],1)
        for mutation in ({**grade, 'unknown': True}, {**grade, 'subject': None},
                         {**grade, 'evaluator_input_digest': None}):
            with self.assertRaises(ValueError): strict_yaml.validate(mutation, ev.EXECUTION_GRADE)
        self.assertEqual(json.loads((Path(__file__).parents[1]/'schemas/benchmark-grade.schema.json').read_bytes()), ev.GRADE)
        self.assertEqual(json.loads((Path(__file__).parents[1]/'schemas/benchmark-execution-grade-v2.schema.json').read_bytes()), ev.EXECUTION_GRADE)


if __name__ == '__main__': unittest.main()
