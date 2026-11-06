"""Self-authored tiny fixtures test preparation mechanics, not long-task quality."""
from collections import Counter
from copy import deepcopy
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from agent_harness import task_flow_experiment as flow
from agent_harness.core import HarnessError, canonical, digest


class TaskFlowPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.assets = self.root/'assets'; self.assets.mkdir()
        self.repo = self.root/'source'; self.repo.mkdir(); (self.repo/'module.py').write_bytes(b'VALUE = 0\n')
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM=os.devnull)
        self.git('init', '--template='); self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'core.attributesFile', os.devnull); self.git('add', '.')
        self.git('commit', '-m', 'self-authored mechanics fixture')
        self.source = {'commit': self.git('rev-parse', 'HEAD').decode().strip(),
                       'tree': self.git('rev-parse', 'HEAD^{tree}').decode().strip()}
        artifact = self.put('policy/tool.json', {'fixture': 'not a client executable'})
        policy = {'model': 'fixture-model', 'provider': 'fixture-provider',
            'client': {'id': 'fixture-client-v1', 'artifact': artifact},
            'tools': [{'id': 'fixture-tool', 'artifact': artifact}], 'permissions': ['read', 'write', 'test'],
            'budgets': {'max_tokens': 10000, 'max_cost_usd': 1, 'max_wall_seconds': 600},
            'effort': {'root': 'high', 'roles': [{'role': 'worker', 'effort': 'high'}, {'role': 'reviewer', 'effort': 'high'}]},
            'environment': self.put('policy/environment.json', {'fixture': 'unqualified local controls'}), 'cache_regime': 'cold'}
        configured = self.put('policy/configured.json', {'schema_version': 1,
            'record_type': 'task_flow_configured_effort', 'policy_digest': digest(policy), 'effort': policy['effort']})
        self.spec = {'schema_version': 1, 'record_type': 'task_flow_experiment', 'experiment_id': 'mechanics',
            'bank_kind': 'synthetic_fixture', 'bank_provenance': self.put('bank-origin.json', {'fixture': True, 'production_corpus': False}),
            'comparison': 'mechanism', 'tasks': [], 'policy': policy,
            'arms': [{'id': arm, 'configured_effort': configured if arm == 'A' else None} for arm in flow.ARMS], 'seeds': [7]}
        for index, (family, tags) in enumerate(flow.FAMILIES.items()):
            name = 'task-'+str(index)
            requirements = [{'id': 'req-'+str(i), 'text': 'Fixture obligation '+tag,
                'mandatory': True, 'tags': [tag]} for i, tag in enumerate(tags)]
            mission = self.put(name+'/mission.json', {'schema_version': 1, 'record_type': 'task_flow_original_mission',
                'task_id': name, 'original_request': 'Mechanics-only fixture; not a production long task: '+family,
                'requirements': requirements, 'invariants': ['Preserve source identity and complete original requirement set']})
            hidden = self.put(name+'/hidden-test.py', '# private independent oracle '+name+'\nSECRET_GOLD_'+name+' = True\n')
            evaluator = self.put(name+'/evaluator.json', {'schema_version': 1, 'record_type': 'task_flow_hidden_evaluator',
                'task_id': name, 'revision': 'a'*40, 'producer_id': 'independent-oracle',
                'requirement_ids': [r['id'] for r in requirements], 'files': [hidden]})
            graph = self.put(name+'/graph.json', {'schema_version': 1, 'record_type': 'task_flow_shared_decomposition',
                'task_id': name, 'nodes': [{'id': 'whole', 'requirements': [r['id'] for r in requirements], 'depends_on': []}]})
            self.spec['tasks'].append({'id': name, 'family': family, 'source': self.source,
                'mission': mission, 'public_assets': [], 'hidden_evaluator': evaluator, 'shared_decomposition': graph})
        self.spec_path = self.root/'experiment.yaml'; self.output = self.root/'prepared'

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            *args], cwd=self.repo, env=self.env, check=True, capture_output=True).stdout

    def put(self, name, value):
        raw = value if isinstance(value, bytes) else value.encode() if isinstance(value, str) else canonical(value)
        path = self.assets/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        return {'path': name, 'sha256': hashlib.sha256(raw).hexdigest()}

    def document(self, ref):
        return json.loads((self.assets/ref['path']).read_bytes())

    def prepare(self, spec=None, output=None):
        self.spec_path.write_bytes(canonical(self.spec if spec is None else spec))
        return flow.prepare(self.spec_path, assets_root=self.assets,
            source_roots={task['id']: self.repo for task in self.spec['tasks']}, output_dir=output or self.output)

    def report(self, prepared, *, grade=True):
        trial = prepared['schedule'][0]; task = next(t for t in prepared['tasks'] if t['id'] == trial['task_id'])
        pipeline = self.put('reports/native-grade.json', {'schema_version': 1, 'record_type': 'independent_grade',
            'manifest_digest': prepared['preparation_digest'], 'trial_id': trial['trial_id'],
            'evaluator_digest': task['evaluator_bundle_digest'], 'outcome': 'accepted'})
        evaluator = self.put('reports/mission-grade.json', {'schema_version': 1, 'record_type': 'task_flow_mission_evaluation',
            'preparation_digest': prepared['preparation_digest'], 'trial_id': trial['trial_id'],
            'candidate_digest': 'b'*64, 'evaluator_bundle_digest': task['evaluator_bundle_digest'],
            'producer_id': 'independent-oracle', 'requirements': [{'id': r, 'status': 'passed'} for r in task['requirements']],
            'verdict': 'accepted', 'finished_at': '2026-01-01T00:00:04Z', 'execution_evidence': pipeline})
        return {'schema_version': 1, 'record_type': 'task_flow_trial_report',
            'preparation_digest': prepared['preparation_digest'], 'trial_id': trial['trial_id'],
            'task_id': trial['task_id'], 'arm': trial['arm'], 'solver_id': 'solver-fixture',
            'measurement_kind': 'synthetic', 'outcome': 'completed', 'claimed_complete': True,
            'candidate_digest': 'b'*64, 'evaluator_receipt': evaluator if grade else None,
            'observed_policy': None,
            'started_at': '2026-01-01T00:00:00Z', 'finished_at': '2026-01-01T00:00:05Z',
            'manual_interventions': None, 'human_minutes': None, 'human_feedback': [],
            'expected_invocation_ids': ['planner-1', 'reviewer-1', 'repair-1'],
            'usage': [{'invocation_id': name, 'scope': 'individual_attempt', 'phase': phase, 'status': status,
                'input_tokens': before, 'output_tokens': after} for name, phase, status, before, after in
                [('planner-1', 'planner', 'complete', 2, 3), ('reviewer-1', 'reviewer', 'complete', 4, 5),
                 ('repair-1', 'repair', 'unavailable', None, None)]], 'usage_coverage': 'partial',
            'phase_seconds': [{'phase': 'planner', 'seconds': 4}, {'phase': 'reviewer', 'seconds': 4}, {'phase': 'repair', 'seconds': None}]}

    def test_prepares_separate_hidden_assets_full_mission_matched_arms_and_counterbalance(self):
        prepared = self.prepare(); self.assertEqual(len(prepared['schedule']), 200)
        self.assertEqual(prepared['qualification'], flow.QUALIFICATION)
        self.assertEqual(prepared['bank_kind'], 'synthetic_fixture')
        self.assertEqual(prepared, flow.verify(self.output, expected_digest=prepared['preparation_digest']))
        for task in prepared['tasks']:
            manifests = [json.loads((self.output/ref['path']).read_bytes()) for ref in task['arm_manifests'].values()]
            self.assertTrue(all(row['requested_policy'] == self.spec['policy'] for row in manifests))
            self.assertTrue(all(row['mission'] == task['mission'] for row in manifests))
            self.assertTrue(all(row['effort']['observed'] == {'status': 'not_run', 'value': None} for row in manifests))
            self.assertEqual(manifests[0]['effort']['configured']['status'], 'declared_unqualified')
            self.assertEqual(manifests[1]['effort']['configured']['value'], None)
            for row in manifests:
                self.assertNotIn('evaluator', json.dumps(row)); self.assertNotIn('SECRET_GOLD', json.dumps(row))
            self.assertEqual(Counter((row['arm'], row['position']) for row in prepared['schedule'] if row['task_id'] == task['id']), Counter({(a, p): 1 for a in flow.ARMS for p in range(5)}))
        self.assertEqual(prepared, self.prepare())
        other = self.prepare(output=self.root/'second-preparation')
        self.assertEqual(other['preparation_digest'], prepared['preparation_digest'])

    def test_hidden_digest_overlap_and_policy_ref_leak_refuse_before_publish(self):
        task = self.spec['tasks'][0]; evaluator = self.document(task['hidden_evaluator'])
        task['public_assets'] = [evaluator['files'][0]]
        with self.assertRaisesRegex(HarnessError, 'HIDDEN_EVALUATOR_LEAK'): self.prepare()
        self.assertFalse(self.output.exists())
        task['public_assets'] = []; self.spec['policy']['environment'] = evaluator['files'][0]
        self.spec['arms'][0]['configured_effort'] = None
        with self.assertRaisesRegex(HarnessError, 'HIDDEN_EVALUATOR_LEAK'): self.prepare()

    def test_hidden_bytes_in_captured_source_refuse(self):
        evaluator = self.document(self.spec['tasks'][0]['hidden_evaluator'])
        (self.repo/'private-test.py').write_bytes((self.assets/evaluator['files'][0]['path']).read_bytes())
        self.git('add', '.'); self.git('commit', '-m', 'would leak hidden test')
        source = {'commit': self.git('rev-parse', 'HEAD').decode().strip(), 'tree': self.git('rev-parse', 'HEAD^{tree}').decode().strip()}
        for task in self.spec['tasks']: task['source'] = source
        with self.assertRaisesRegex(HarnessError, 'HIDDEN_EVALUATOR_LEAK'): self.prepare()

    def test_original_requirements_family_and_graph_omissions_rejected(self):
        task = self.spec['tasks'][0]; evaluator = self.document(task['hidden_evaluator'])
        evaluator['requirement_ids'].pop(); task['hidden_evaluator'] = self.put('bad-evaluator.json', evaluator)
        with self.assertRaisesRegex(HarnessError, 'ORIGINAL_REQUIREMENT_BINDING'): self.prepare()
        task['hidden_evaluator'] = self.put('correct-evaluator.json', {**evaluator, 'requirement_ids': [r['id'] for r in self.document(task['mission'])['requirements']]})
        graph = self.document(task['shared_decomposition']); graph['nodes'][0]['depends_on'] = ['whole']
        task['shared_decomposition'] = self.put('cycle.json', graph)
        with self.assertRaisesRegex(HarnessError, 'CYCLE_OR_OMISSION'): self.prepare()

    def test_configured_policies_cannot_override_requested_and_unknown_never_defaults(self):
        configured = self.document(self.spec['arms'][0]['configured_effort']); configured['effort']['root'] = 'low'
        self.spec['arms'][0]['configured_effort'] = self.put('unmatched.json', configured)
        with self.assertRaisesRegex(HarnessError, 'CONFIGURED_POLICY_MISMATCH'): self.prepare()
        self.spec['arms'][0]['configured_effort'] = None; self.spec['policy']['effort']['root'] = 'unknown'
        with self.assertRaisesRegex(HarnessError, 'REQUESTED_IDENTITY_UNAVAILABLE'): self.prepare()

    def test_product_and_mechanism_information_are_separate(self):
        self.spec['comparison'] = 'product'
        with self.assertRaisesRegex(HarnessError, 'COMPARISON_INFORMATION_MISMATCH'): self.prepare()
        for task in self.spec['tasks']: task['shared_decomposition'] = None
        self.assertEqual(self.prepare()['comparison'], 'product')

    def test_raw_source_drift_filters_and_traversal_refuse(self):
        (self.repo/'module.py').write_bytes(b'VALUE = 1\n')
        with self.assertRaisesRegex(HarnessError, 'DIRTY_BASE_CHECKOUT'): self.prepare()
        (self.repo/'module.py').write_bytes(b'VALUE = 0\n')
        self.spec['tasks'][0]['mission']['path'] = '../escape'
        with self.assertRaisesRegex(HarnessError, 'UNSAFE_PATH'): self.prepare()

    def test_declared_source_filter_refuses_without_running_it(self):
        (self.repo/'.gitattributes').write_text('module.py filter=custom\n')
        self.git('add', '.'); self.git('commit', '-m', 'filter declaration')
        source = {'commit': self.git('rev-parse', 'HEAD').decode().strip(), 'tree': self.git('rev-parse', 'HEAD^{tree}').decode().strip()}
        for task in self.spec['tasks']: task['source'] = source
        with self.assertRaisesRegex(HarnessError, 'SOURCE_FILTER_UNSUPPORTED'): self.prepare()

    def test_mid_publication_source_drift_retains_partial_directory_without_receipt(self):
        original = flow.atomic_write; changed = False
        def race(path, raw, **kwargs):
            nonlocal changed
            result = original(path, raw, **kwargs)
            if path.name == 'mission.json' and not changed:
                changed = True; (self.repo/'module.py').write_bytes(b'VALUE = 2\n')
            return result
        with patch.object(flow, 'atomic_write', side_effect=race), self.assertRaisesRegex(HarnessError, 'DIRTY_BASE_CHECKOUT'):
            self.prepare()
        self.assertTrue(self.output.exists()); self.assertFalse((self.output/'preparation.json').exists())
        before = {p.relative_to(self.output): p.read_bytes() for p in self.output.rglob('*') if p.is_file()}
        (self.repo/'module.py').write_bytes(b'VALUE = 0\n')
        with self.assertRaisesRegex(HarnessError, 'PARTIAL_PREPARATION'): self.prepare()
        self.assertEqual(before, {p.relative_to(self.output): p.read_bytes() for p in self.output.rglob('*') if p.is_file()})

    def test_repeat_preserves_edits_and_conflicting_receipts(self):
        prepared = self.prepare(); target = self.output/prepared['tasks'][0]['mission']['path']
        target.write_bytes(b'user edit')
        with self.assertRaisesRegex(HarnessError, 'BYTES_CHANGED'): self.prepare()
        self.assertEqual(target.read_bytes(), b'user edit')

    def test_added_directory_duplicate_json_and_unexpected_qualification_refuse(self):
        prepared = self.prepare(); (self.output/'untracked-directory').mkdir()
        with self.assertRaisesRegex(HarnessError, 'DIRECTORY_INVENTORY'): flow.verify(self.output)
        (self.output/'untracked-directory').rmdir()
        receipt = self.output/'preparation.json'; raw = receipt.read_bytes()
        receipt.write_bytes(b'{"schema_version":1,'+raw[1:])
        with self.assertRaises(ValueError): flow.verify(self.output)
        receipt.write_bytes(raw)
        modified = deepcopy(prepared); modified['qualification']['client'] = 'passed'
        content = dict(modified); content.pop('preparation_digest'); modified['preparation_digest'] = digest(content)
        receipt.write_bytes(canonical(modified))
        with self.assertRaises(ValueError): flow.verify(self.output)

    def test_missing_reports_and_usage_are_unknown_and_no_winner_is_claimed(self):
        prepared = self.prepare(); summary = flow.aggregate(self.output, [], reports_root=self.assets)
        self.assertEqual(summary['status'], 'not_run'); self.assertIsNone(summary['winner'])
        self.assertFalse(summary['production_qualified'])
        for arm in summary['arms'].values():
            self.assertEqual(arm['planned_trials'], 40); self.assertEqual(arm['not_run'], 40)
            self.assertIsNone(arm['observed_tokens']); self.assertEqual(arm['usage_coverage'], 'unknown')

    def test_unknown_usage_includes_planner_review_repair_and_parallel_time_is_not_summed(self):
        prepared = self.prepare(); report = self.report(prepared)
        summary = flow.aggregate(self.output, [report, deepcopy(report)], reports_root=self.assets)
        arm = summary['arms'][report['arm']]
        self.assertEqual(arm['reported_trials'], 1); self.assertEqual(arm['observed_tokens'], 14)
        self.assertEqual(arm['usage_coverage'], 'partial'); self.assertEqual(arm['reported_wall_seconds'], [5])
        self.assertEqual(arm['time_to_verified_seconds'], [4]); self.assertEqual(arm['reported_full_mission_verified'], 1)
        self.assertEqual(sum(p['seconds'] or 0 for p in arm['phase_seconds']), 8)
        self.assertIsNone(arm['manual_interventions_observed']); self.assertEqual(summary['measurement_kind'], 'synthetic')
        report['usage_coverage'] = 'complete'
        with self.assertRaisesRegex(HarnessError, 'FALSE_USAGE_COVERAGE'): flow.aggregate(self.output, [report], reports_root=self.assets)

    def test_grade_missing_omitted_requirements_or_asset_used_as_execution_proof(self):
        prepared = self.prepare(); report = self.report(prepared, grade=False)
        summary = flow.aggregate(self.output, [report], reports_root=self.assets)
        arm = summary['arms'][report['arm']]
        self.assertEqual(arm['reported_full_mission_verified'], 0); self.assertEqual(arm['false_completion_unknown'], 40)
        report = self.report(prepared); grade = self.document(report['evaluator_receipt']); grade['requirements'].pop()
        report['evaluator_receipt'] = self.put('reports/omitted.json', grade)
        with self.assertRaisesRegex(HarnessError, 'GRADE_REQUIREMENT_OMISSION'): flow.aggregate(self.output, [report], reports_root=self.assets)
        grade = self.document(self.report(prepared)['evaluator_receipt']); grade['execution_evidence'] = self.spec['tasks'][0]['hidden_evaluator']
        report['evaluator_receipt'] = self.put('reports/asset-is-not-run.json', grade)
        with self.assertRaises(ValueError): flow.aggregate(self.output, [report], reports_root=self.assets)

    def test_future_report_candidate_binding_and_usage_overlap_fail_closed(self):
        prepared = self.prepare(); report = self.report(prepared); wrong = deepcopy(report); wrong['candidate_digest'] = 'c'*64
        with self.assertRaisesRegex(HarnessError, 'INDEPENDENT_GRADE_BINDING'): flow.aggregate(self.output, [wrong], reports_root=self.assets)
        second = deepcopy(report); trial = prepared['schedule'][1]
        second.update(trial_id=trial['trial_id'], arm=trial['arm'], evaluator_receipt=None, claimed_complete=None)
        with self.assertRaisesRegex(HarnessError, 'OVERLAPPING_USAGE'): flow.aggregate(self.output, [report, second], reports_root=self.assets)

    def test_false_completion_and_actual_policy_mismatch_are_reported_not_hidden(self):
        prepared = self.prepare(); report = self.report(prepared)
        grade = self.document(report['evaluator_receipt']); grade['verdict'] = 'rejected'; grade['requirements'][0]['status'] = 'failed'
        native = self.document(grade['execution_evidence']); native['outcome'] = 'failed'
        grade['execution_evidence'] = self.put('reports/rejected-native.json', native)
        report['evaluator_receipt'] = self.put('reports/rejected-mission.json', grade)
        actual = deepcopy(self.spec['policy']['effort']); actual['roles'][0]['effort'] = 'low'
        report['observed_policy'] = self.put('reports/policy-observation.json', {'schema_version': 1,
            'record_type': 'task_flow_policy_observation', 'policy_digest': prepared['policy_digest'],
            'coverage': 'complete', 'effort': actual, 'method': 'runtime_query',
            'runtime_evidence': [self.put('reports/runtime-observation.txt', 'self-authored observation fixture')]})
        summary = flow.aggregate(self.output, [report], reports_root=self.assets)
        arm = summary['arms'][report['arm']]
        self.assertEqual(arm['false_completions'], 1); self.assertEqual(arm['mandatory_requirements_failed'], 1)
        self.assertEqual(arm['policy_mismatch_trials'], 1); self.assertEqual(arm['observed_effort'][0]['effort'], actual)
        self.assertFalse(summary['production_qualified']); self.assertIsNone(summary['winner'])

    def test_strict_schema_export_and_cli_preparation_verification_jsonl_aggregation(self):
        schema = json.loads((Path(__file__).resolve().parents[1]/'schemas/task-flow-experiment.schema.json').read_text())
        self.assertEqual(schema, flow.SCHEMA)
        self.spec_path.write_bytes(canonical(self.spec)); output = io.StringIO()
        args = ['prepare', '--spec', str(self.spec_path), '--assets-root', str(self.assets), '--output', str(self.output)]
        for task in self.spec['tasks']: args += ['--source-root', task['id']+'='+str(self.repo)]
        with contextlib.redirect_stdout(output): self.assertEqual(flow.dispatch(args), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'prepared_not_run')
        reports = self.root/'reports.jsonl'; reports.write_bytes(b'')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(flow.dispatch(['verify', '--preparation', str(self.output)]), 0)
            self.assertEqual(flow.dispatch(['aggregate', '--preparation', str(self.output), '--reports', str(reports), '--reports-root', str(self.assets)]), 0)


if __name__ == '__main__': unittest.main()
