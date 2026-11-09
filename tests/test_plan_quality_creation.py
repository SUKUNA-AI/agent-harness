"""Prepared v3 creation is recovered only by an exact explicit create retry."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from agent_harness import plan_quality
from agent_harness.core import HarnessError
from agent_harness.file_io import read_regular_file
from agent_harness.strict_yaml import dump
from agent_harness.supervisor import Supervisor
import test_verified_flow as fixtures


class CreationCrash(BaseException):
    pass


class PlanQualityCreationTests(unittest.TestCase):
    setUp = fixtures.VerifiedFlowTests.setUp
    fixture = fixtures.VerifiedFlowTests.fixture

    def initialize(self):
        self.original, self.fake = self.fixture()
        self.contract_path = self.root / 'task-v2.yaml'
        self.source = Path(self.original.state['source_cwd'])
        self.number = 0

    def create(self, run, **changes):
        values = dict(run_dir=run, cwd=self.source,
            adapter=self.original.adapter, state_dir=self.original.state_dir,
            max_planning_rounds=2)
        values.update(changes)
        contract_path = values.pop('contract_path', self.contract_path)
        with patch.object(self.original.adapter, 'run', side_effect=AssertionError('Creation must not dispatch a model')):
            return Supervisor.create(contract_path, **values)

    def interrupt_creation(self):
        self.number += 1
        run = self.root / 'private' / ('prepared-create-' + str(self.number))
        publish = plan_quality.publish_bootstrap
        def crash(store, material):
            publish(store, material)
            raise CreationCrash()
        with patch.object(plan_quality, 'publish_bootstrap', crash), self.assertRaises(CreationCrash):
            self.create(run)
        store = Supervisor(run, adapter=self.original.adapter, state_dir=self.original.state_dir).store
        events = [event['payload'] for event in store.events()]
        prepared = [event for event in events if event.get('kind') == 'plan_quality_creation_prepared']
        self.assertEqual(len(prepared), 1)
        self.assertFalse(any(event.get('kind') in {'checkpoint', 'execution_receipt'} for event in events))
        self.assertFalse((run / 'state.yaml').exists())
        document = json.loads(read_regular_file(run / prepared[0]['document_ref']['path']))
        return run, store, prepared[0], document

    def assert_no_calls(self):
        self.assertFalse(self.fake.with_suffix('.calls').exists())

    def test_exact_create_retry_preserves_original_identity_deadline_and_limits(self):
        self.initialize()
        run, store, _, document = self.interrupt_creation()
        frozen = deepcopy(document['material']['state'])
        restored = self.create(run)
        for key in ('run_id', 'task_id', 'started_at', 'deadline', 'max_attempts',
                    'attempts_used', 'metrics', 'baseline_digest', 'contract_digest', 'state_dir'):
            self.assertEqual(restored.state[key], frozen[key], key)
        self.assertEqual(restored.state['plan_quality'], frozen['plan_quality'])
        self.assertEqual(restored.state['status'], 'NEW')
        self.assertEqual(restored.state['schema_version'], 3)
        events = [event['payload'] for event in store.events()]
        for kind in ('plan_quality_creation_prepared', 'completion_policy', 'plan_quality_policy', 'checkpoint'):
            self.assertEqual(sum(event.get('kind') == kind for event in events), 1, kind)
        before = read_regular_file(run / 'journal/events.jsonl')
        with self.assertRaises(HarnessError):
            self.create(run)
        self.assertEqual(read_regular_file(run / 'journal/events.jsonl'), before)
        self.assert_no_calls()

    def test_changed_creation_inputs_cannot_retarget_prepared_work(self):
        self.initialize()
        alternate = self.root / 'alternate-task.yaml'
        changed = deepcopy(self.original.contract)
        changed['task']['objective'] += ' changed request'
        dump(alternate, changed)
        other_source = self.root / 'other-source'; other_source.mkdir()
        variants = ({'contract_path': alternate}, {'cwd': other_source},
            {'state_dir': self.root / 'different-state'},
            {'max_planning_rounds': 1}, {'max_planning_rounds': 3},
            {'max_planning_rounds': 2.0}, {'max_planning_rounds': True})
        for changes in variants:
            with self.subTest(changes=changes):
                run, _, _, _ = self.interrupt_creation()
                before = read_regular_file(run / 'journal/events.jsonl')
                with self.assertRaises((HarnessError, OSError, ValueError)):
                    self.create(run, **changes)
                self.assertEqual(read_regular_file(run / 'journal/events.jsonl'), before)
                self.assertFalse((run / 'state.yaml').exists())
        self.assert_no_calls()

    def test_missing_or_corrupt_prepared_document_is_not_rebuilt(self):
        self.initialize()
        for kind in ('missing', 'changed'):
            with self.subTest(kind=kind):
                run, _, prepared, _ = self.interrupt_creation()
                path = run / prepared['document_ref']['path']
                if kind == 'missing':
                    path.unlink()
                else:
                    path.write_bytes(b'{}\n')
                before = read_regular_file(run / 'journal/events.jsonl')
                with self.assertRaises((HarnessError, OSError, ValueError)):
                    self.create(run)
                self.assertEqual(read_regular_file(run / 'journal/events.jsonl'), before)
                self.assertEqual(path.exists(), kind != 'missing')
                if path.exists():
                    self.assertEqual(path.read_bytes(), b'{}\n')
                self.assertFalse((run / 'state.yaml').exists())
        self.assert_no_calls()

    def test_existing_conflicting_policy_bytes_are_preserved(self):
        self.initialize()
        run, _, _, document = self.interrupt_creation()
        artifact = next(item for item in document['material']['artifacts']
            if item['path'].startswith('policies/plan-quality-'))
        target = run / artifact['path']
        changed = target.read_bytes() + b' '
        target.write_bytes(changed)
        before = read_regular_file(run / 'journal/events.jsonl')
        with self.assertRaises((HarnessError, OSError, ValueError)):
            self.create(run)
        self.assertEqual(target.read_bytes(), changed)
        self.assertEqual(read_regular_file(run / 'journal/events.jsonl'), before)
        self.assertFalse((run / 'state.yaml').exists())
        self.assert_no_calls()

    def test_inspection_and_resume_never_bootstrap_a_headless_creation(self):
        self.initialize()
        run, _, _, document = self.interrupt_creation()
        paths = [run / item['path'] for item in document['material']['artifacts']]
        before = {str(path): hashlib.sha256(read_regular_file(path)).hexdigest() for path in paths}
        journal = read_regular_file(run / 'journal/events.jsonl')
        for operation in ('status', 'resume'):
            with self.subTest(operation=operation), self.assertRaises((HarnessError, OSError, ValueError)):
                getattr(Supervisor(run, adapter=self.original.adapter,
                    state_dir=self.original.state_dir), operation)()
        self.assertFalse((run / 'state.yaml').exists())
        self.assertEqual(read_regular_file(run / 'journal/events.jsonl'), journal)
        self.assertEqual({str(path): hashlib.sha256(read_regular_file(path)).hexdigest() for path in paths}, before)
        self.assert_no_calls()

    def test_hard_exit_after_bootstrap_recovers_exact_prepared_creation(self):
        self.initialize()
        run = self.root / 'private' / 'hard-exit-create'
        code = '''
import os,sys
from unittest.mock import patch
from agent_harness import plan_quality
from agent_harness.supervisor import Supervisor
class NoClient:
 def run(self,*a,**k):raise AssertionError('No model invocation during creation')
 def capabilities(self,*a,**k):return {'version':'creation-fixture','output_schema':True}
publish=plan_quality.publish_bootstrap
def stop(store,material):
 publish(store,material)
 os._exit(73)
with patch.object(plan_quality,'publish_bootstrap',stop):
 Supervisor.create(sys.argv[1],run_dir=sys.argv[2],cwd=sys.argv[3],
  state_dir=sys.argv[4],adapter=NoClient(),max_planning_rounds=2)
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.contract_path),
            str(run), str(self.source), str(self.original.state_dir)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
            env={**os.environ, 'PYTHONPATH': str(Path(plan_quality.__file__).parents[1]),
                 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors='replace'))
        store = Supervisor(run, adapter=self.original.adapter, state_dir=self.original.state_dir).store
        events = [event['payload'] for event in store.events()]
        prepared = next(event for event in events if event.get('kind') == 'plan_quality_creation_prepared')
        self.assertFalse((run / 'state.yaml').exists())
        self.assertFalse(any(event.get('kind') in {'checkpoint', 'execution_receipt'} for event in events))
        document = json.loads(read_regular_file(run / prepared['document_ref']['path']))
        frozen = document['material']['state']
        restored = self.create(run)
        for key in ('run_id', 'started_at', 'deadline', 'max_attempts', 'attempts_used', 'metrics', 'plan_quality'):
            self.assertEqual(restored.state[key], frozen[key], key)
        self.assertEqual(restored.state['status'], 'NEW')
        self.assert_no_calls()
