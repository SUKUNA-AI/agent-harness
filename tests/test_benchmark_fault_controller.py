"""Real contained synthetic controller restart; no live model qualification."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_benchmark_bundles as bundle_fixture
from agent_harness import strict_yaml
from agent_harness.benchmark_bundles import LocalTaskBundleAdapter
from agent_harness.benchmark_fault_controller import OwnedController, SolveFaultHook, file_ref
from agent_harness.benchmark_runner import BenchmarkRunner, BudgetAuthorization, SOLVE_ARTIFACT
from agent_harness.core import HarnessError, digest, identity_alive


CHILD = '''
import json,pathlib,sys
sys.path[:0]=json.loads(sys.argv[1])
import test_benchmark_bundles as fixture
from agent_harness.benchmark_bundles import LocalTaskBundleAdapter
from agent_harness.benchmark_fault_controller import SolveFaultHook
from agent_harness.benchmark_runner import BenchmarkRunner
root=pathlib.Path(sys.argv[2])
class Hook:
 def before_solve(self, **values):
  receipt=pathlib.Path(values['prepared']['contract_path']).parent/'materialized.yaml'
  self.actual=SolveFaultHook(root/'fault',receipt)
  self.actual.before_solve(**values)
  if (root/'pause-before-model').exists():
   import time
   from datetime import datetime,timezone
   (root/'before-model-ready').write_text('dispatch frozen; no model called')
   end=datetime.fromisoformat(values['deadline'].replace('Z','+00:00'))
   while datetime.now(timezone.utc)<end: time.sleep(.025)
   raise SystemExit(79)
 def after_solve(self, path):
  self.actual.after_solve(path)
boundary=fixture.ContainedBoundary(root/'assets')
runner=BenchmarkRunner(root/'run',LocalTaskBundleAdapter(boundary),fault_controller=Hook())
result=runner.advance()
(root/'report.json').write_text(json.dumps(result))
'''


class FaultControllerTests(unittest.TestCase):
    def setUp(self):
        # Reuse the versioned custom task corpus; this fixture's external suite
        # slots are synthetic controls, never official benchmark qualification.
        self.fixture = bundle_fixture.BundleTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.assets = self.fixture.root, self.fixture.assets
        self.manifest = self.fixture.manifest()
        self.boundary = bundle_fixture.ContainedBoundary(self.assets)
        self.adapter = LocalTaskBundleAdapter(self.boundary)
        self.deadline = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
        self.grant = BudgetAuthorization(digest(self.manifest), 'explicit-fault-fixture', 500, self.deadline, 'synthetic')
        self.runner = BenchmarkRunner.create(self.manifest, assets_root=self.assets, run_dir=self.root/'run',
            adapter=self.adapter, authorization=self.grant)
        self.script = self.root/'controller.py'
        self.script.write_text(CHILD)
        source = str(Path(__file__).resolve().parents[1]/'src')
        tests = str(Path(__file__).resolve().parent)
        self.argv = [sys.executable, str(self.script), json.dumps([source, tests]), str(self.root)]

    def launch(self):
        owner = OwnedController(self.argv, cwd=self.root)
        self.addCleanup(owner.close)
        try:
            hook = owner.wait_held(self.root/'fault', timeout=30)
        except BaseException:
            owner.close()
            if owner.process.poll() is not None:
                # Capture a bounded diagnostic without hiding the original
                # exception or serializing the complete runner report.
                error = self.root/'report.json'
                if error.exists():
                    self.fail(error.read_text()[:1500])
            raise
        self.assertIsNone(owner.process.poll())
        self.assertFalse((self.root/'report.json').exists(), 'held runner returned before release')
        observed = hook.observer().observe()
        self.assertIsNotNone(observed)
        return owner, hook, observed['milestone_id']

    def calls(self):
        return self.boundary.fake.with_suffix('.calls').read_text().splitlines()

    def test_actual_runner_restart_reconciles_exact_dispatch_usage_and_original_deadline(self):
        owner, hook, milestone = self.launch()
        subject = hook.subject()
        records = self.runner.records()
        dispatch = next(row for row in records if row['kind']=='dispatch')
        prepared = next(row['payload'] for row in records if row['kind']=='prepared')
        self.assertEqual(subject['dispatch_digest'], digest(dispatch['payload']))
        self.assertEqual(subject['prepared_digest'], digest(prepared))
        self.assertEqual(subject['execution_id'], dispatch['payload']['execution_id'])
        self.assertIsNone(subject['producer']['client_version'])
        self.assertIsNone(subject['producer']['capability_receipt_ref'])
        self.assertEqual(subject['producer']['build_provenance']['source'], 'on_disk_package_and_loaded_producer')
        self.assertLessEqual(datetime.fromisoformat(subject['deadline']), datetime.fromisoformat(self.deadline))
        accounting = hook.completion_accounting()
        before = hook._solve(accounting['solve'])
        self.assertEqual(self.calls(), ['synthetic'])
        intent = hook.claim(milestone)
        with self.assertRaisesRegex(HarnessError, 'STILL_LIVE'):
            hook.reconcile_stopped(intent['operation_id'])
        with self.assertRaisesRegex(HarnessError, 'NOT_RELEASABLE'):
            hook.release()
        owner.interrupt(hook, intent['operation_id'])
        self.assertEqual(owner.process.returncode, 86, 'actual cooperative controller crash required')
        self.assertFalse(identity_alive(owner.identity))
        restarted = owner.restart(hook, intent['operation_id'])
        self.addCleanup(restarted.close)
        out, err = restarted.wait(timeout=30)
        self.assertEqual(restarted.process.returncode, 0, (out, err))
        report = json.loads((self.root/'report.json').read_text())
        self.assertEqual(report['completed_trials'], 1, report)
        self.assertEqual(report['usage']['observed_tokens'], 4)
        self.assertEqual(self.calls(), ['synthetic'], 'known completed solve must not call model again')
        self.assertEqual(hook.subject(), subject)
        self.assertEqual(hook._solve(accounting['solve']), before)
        final = self.runner.records()
        self.assertEqual(len([r for r in final if r['kind']=='dispatch']), 1)
        trial = next(r for r in final if r['kind']=='trial')
        self.assertEqual(trial['trial_id'], subject['trial_id'])
        self.assertEqual(trial['payload']['usage'], before['result']['usage'])
        kinds = [row['kind'] for row in hook.records()]
        self.assertLess(kinds.index('intent'), kinds.index('crash_requested'))
        self.assertLess(kinds.index('crash_requested'), kinds.index('exit_observed'))
        self.assertLess(kinds.index('exit_observed'), kinds.index('stopped'))
        self.assertLess(kinds.index('stopped'), kinds.index('restart_launch'))
        self.assertEqual(accounting['scope'], 'cooperative_postsolve_controller_restart')
        self.assertEqual(file_ref(hook.accounting_ref()['path']), hook.accounting_ref())
        self.assertEqual(accounting['midtask_resume'], 'unsupported')
        self.assertEqual(accounting['client_compact'], 'unsupported')
        with self.assertRaisesRegex(HarnessError, 'RESTART_LAUNCH_ALREADY_CLAIMED'):
            owner.restart(hook, intent['operation_id'])

    def test_release_without_fault_returns_original_call(self):
        owner, hook, _ = self.launch()
        hook.release()
        hook.release()
        out, err = owner.wait(timeout=30)
        self.assertEqual(owner.process.returncode, 0, (out, err))
        self.assertEqual(json.loads((self.root/'report.json').read_text())['completed_trials'], 1)
        self.assertEqual(self.calls(), ['synthetic'])
        self.assertFalse(any(row['kind']=='intent' for row in hook.records()))

    def test_crash_after_stop_before_completion_reconciles_without_second_interrupt(self):
        owner, hook, milestone = self.launch()
        intent = hook.claim(milestone)
        class Crash(BaseException):
            pass
        with patch.object(hook, 'reconcile_stopped', side_effect=Crash), self.assertRaises(Crash):
            owner.interrupt(hook, intent['operation_id'])
        self.assertFalse(identity_alive(owner.identity))
        reopened = SolveFaultHook(self.root/'fault')
        first = reopened.reconcile_stopped(intent['operation_id'])
        self.assertEqual(reopened.reconcile_stopped(intent['operation_id']), first)
        self.assertEqual(len([row for row in reopened.records() if row['kind']=='stopped']), 1)
        restarted = owner.restart(reopened, intent['operation_id'])
        self.addCleanup(restarted.close)
        restarted.wait(timeout=30)
        self.assertEqual(restarted.process.returncode, 0)
        self.assertEqual(self.calls(), ['synthetic'])

    def test_missing_exit_observation_stays_unknown_until_retained_handle_reconciles(self):
        owner, hook, milestone = self.launch()
        intent = hook.claim(milestone)
        original = hook._append
        class Crash(BaseException):
            pass
        def append(kind, **fields):
            if kind == 'exit_observed':
                raise Crash()
            return original(kind, **fields)
        with patch.object(hook, '_append', side_effect=append), self.assertRaises(Crash):
            owner.interrupt(hook, intent['operation_id'])
        self.assertEqual(owner.process.returncode, 86)
        reopened = SolveFaultHook(self.root/'fault')
        with self.assertRaisesRegex(HarnessError, 'EXIT_OBSERVATION_UNAVAILABLE'):
            reopened.reconcile_stopped(intent['operation_id'])
        with self.assertRaisesRegex(HarnessError, 'STOP_NOT_CONFIRMED'):
            reopened.authorize_restart(intent['operation_id'])
        # Reconcile through the original retained handle; a dead PID by itself
        # could not supply this exit code, including after deadline expiry.
        with patch('agent_harness.benchmark_fault_controller.datetime') as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = datetime.now(timezone.utc)+timedelta(seconds=120)
            owner.interrupt(reopened, intent['operation_id'])
        self.assertEqual(len([r for r in reopened.records() if r['kind']=='exit_observed']), 1)

    def test_unknown_active_solve_cannot_be_claimed_or_restarted(self):
        owner, hook, milestone = self.launch()
        # A valid typed artifact with unknown outcome/partial billing must not
        # become restart authority merely because a file exists.
        held = hook.held()
        value = strict_yaml.load(held['solve']['path'], SOLVE_ARTIFACT)
        value['result']['outcome'] = 'unknown'
        value['result']['usage_coverage'] = 'unknown'
        value['result']['usage'][0]['status'] = 'unavailable'
        value['result']['usage'][0]['input_tokens'] = None
        path = self.root/'unknown-solve.yaml'
        strict_yaml.dump(path, value, SOLVE_ARTIFACT)
        with self.assertRaisesRegex(HarnessError, 'UNKNOWN_SOLVE_OR_ACCOUNTING'):
            hook._solve(file_ref(path))
        with self.assertRaisesRegex(HarnessError, 'UNKNOWN_OPERATION'):
            hook.authorize_restart('no-known-operation')
        self.assertIsNone(owner.process.poll())
        self.assertEqual(self.calls(), ['synthetic'])

    def test_foreign_handle_cannot_stop_owner_and_duplicate_claim_cannot_release(self):
        owner, hook, milestone = self.launch()
        intent = hook.claim(milestone)
        other = OwnedController([sys.executable, '-c', 'import time;time.sleep(30)'], cwd=self.root)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(HarnessError, 'FOREIGN_CONTROLLER'):
            other.interrupt(hook, intent['operation_id'])
        with self.assertRaisesRegex(HarnessError, 'ALREADY_CLAIMED'):
            hook.claim(milestone)
        with patch('agent_harness.benchmark_fault_controller.process_identity',
                   return_value={**owner.identity, 'created': 'reused-pid'}), self.assertRaisesRegex(HarnessError, 'IDENTITY_CHANGED'):
            owner.interrupt(hook, intent['operation_id'])
        self.assertIsNone(owner.process.poll())
        self.assertIsNone(other.process.poll())
        owner.interrupt(hook, intent['operation_id'])
        other.close()
        with self.assertRaisesRegex(HarnessError, 'FOREIGN_CONTROLLER'):
            other.restart(hook, intent['operation_id'])
        self.assertFalse(any(row['kind']=='restart_launch' for row in hook.records()))

    def test_changed_materialization_or_solve_prevents_fault_before_signal(self):
        owner, hook, milestone = self.launch()
        solve = Path(hook.held()['solve']['path'])
        original = solve.read_bytes()
        solve.write_bytes(original+b'\n')
        with self.assertRaisesRegex(HarnessError, 'ARTIFACT_CHANGED'):
            hook.claim(milestone)
        self.assertIsNone(owner.process.poll())
        solve.write_bytes(original)
        accounting = Path(hook.accounting_ref()['path'])
        original_accounting = accounting.read_bytes()
        accounting.write_bytes(original_accounting+b'\n')
        with self.assertRaisesRegex(HarnessError, 'ARTIFACT_CHANGED'):
            hook.claim(milestone)
        self.assertIsNone(owner.process.poll())
        accounting.write_bytes(original_accounting)
        materialized = Path(hook.config['materialized']['path'])
        materialized.write_bytes(materialized.read_bytes()+b'\n')
        with self.assertRaisesRegex(HarnessError, 'ARTIFACT_CHANGED'):
            hook.claim(milestone)
        self.assertIsNone(owner.process.poll())

    def test_original_deadline_blocks_restart_after_stop(self):
        owner, hook, milestone = self.launch()
        intent = hook.claim(milestone)
        owner.interrupt(hook, intent['operation_id'])
        # Simulate passage beyond the frozen deadline without changing subject.
        with patch('agent_harness.benchmark_fault_controller.datetime') as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = datetime.now(timezone.utc)+timedelta(seconds=120)
            with self.assertRaisesRegex(HarnessError, 'ORIGINAL_DEADLINE_EXHAUSTED'):
                owner.restart(hook, intent['operation_id'])
        self.assertFalse(any(row['kind']=='restart_launch' for row in hook.records()))

    def test_deadline_expiring_during_restart_intent_prevents_actual_process_start(self):
        owner, hook, milestone = self.launch()
        intent = hook.claim(milestone)
        owner.interrupt(hook, intent['operation_id'])
        original = hook.claim_restart_launch
        expired = False
        def claim(*args, **kwargs):
            nonlocal expired
            result = original(*args, **kwargs)
            expired = True  # durable publication consumed the remaining grant
            return result
        with patch('agent_harness.benchmark_fault_controller.datetime') as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.side_effect = lambda tz: datetime.now(tz)+timedelta(seconds=120 if expired else 0)
            with patch.object(hook, 'claim_restart_launch', side_effect=claim), patch(
                    'agent_harness.benchmark_fault_controller._start_contained',
                    side_effect=AssertionError('restart reached actual start after original deadline')) as start:
                with self.assertRaisesRegex(HarnessError, 'ORIGINAL_DEADLINE_EXHAUSTED'):
                    owner.restart(hook, intent['operation_id'])
                start.assert_not_called()
        self.assertEqual(len([row for row in hook.records() if row['kind']=='restart_launch']), 1)
        self.assertFalse(any(row['kind']=='restart_bound' for row in hook.records()))
        self.assertEqual(self.calls(), ['synthetic'])
        with self.assertRaisesRegex(HarnessError, 'RESTART_LAUNCH_ALREADY_CLAIMED'):
            owner.restart(hook, intent['operation_id'])

    def test_observer_intent_crash_window_reuses_exact_operation(self):
        owner, hook, milestone = self.launch()
        observed = hook.observer().claim_fault(milestone)
        intent = hook.claim(milestone)
        self.assertEqual(intent['operation_id'], observed['operation_id'])
        self.assertEqual(len([r for r in hook.observer().records() if r['kind']=='fault_intent']), 1)
        owner.interrupt(hook, intent['operation_id'])

    def test_actual_pre_solve_controller_crash_remains_unknown_without_model_replay(self):
        (self.root/'pause-before-model').write_text('synthetic crash before model dispatch')
        owner = OwnedController(self.argv, cwd=self.root)
        self.addCleanup(owner.close)
        end = time.monotonic()+30
        while not (self.root/'before-model-ready').exists() and time.monotonic()<end:
            self.assertIsNone(owner.process.poll())
            time.sleep(.025)
        self.assertTrue((self.root/'before-model-ready').exists())
        hook = SolveFaultHook(self.root/'fault')
        self.assertIsNone(hook.held())
        with self.assertRaisesRegex(HarnessError, 'UNAVAILABLE'):
            hook.claim('inputs-verified')
        owner.close()
        report = self.runner.advance()
        self.assertEqual(report['reason_code'], 'BENCH_UNKNOWN_SOLVE_OUTCOME', report)
        self.assertFalse(self.boundary.fake.with_suffix('.calls').exists())
        self.assertEqual(len([r for r in self.runner.records() if r['kind']=='dispatch']), 1)

    def test_unexpected_exit_before_fault_intent_only_reconciles_known_artifact(self):
        owner, hook, milestone = self.launch()
        owner.close()
        with self.assertRaisesRegex(HarnessError, 'STOPPED_BEFORE_INTENT'):
            hook.claim(milestone)
        self.assertFalse(any(row['kind']=='intent' for row in hook.records()))
        report = self.runner.advance()
        self.assertEqual(report['completed_trials'], 1, report)
        self.assertEqual(self.calls(), ['synthetic'])

    def test_held_journal_snapshot_serializes_cross_process_claim_append(self):
        # Pause the child's strict reader after its old file identity was
        # captured. A legitimate parent append must wait for that entire read,
        # rather than making the child's freshness validation fail.
        instrumentation = '''
from agent_harness import file_io
import os,time
original_read=file_io._read_bounded
def gated_read(fd,limit):
 target=root/'fault/journal/events.jsonl'
 if (root/'arm-read').exists() and not (root/'reader-ready').exists() and target.exists() and (os.fstat(fd).st_dev,os.fstat(fd).st_ino)==(target.stat().st_dev,target.stat().st_ino):
  (root/'reader-ready').write_text('strict reader opened old journal identity')
  end=time.monotonic()+15
  while not (root/'release-read').exists():
   if time.monotonic()>end: raise RuntimeError('test journal read release timeout')
   time.sleep(.01)
 return original_read(fd,limit)
file_io._read_bounded=gated_read
'''
        self.script.write_text(CHILD.replace('class Hook:\n', instrumentation+'\nclass Hook:\n'))
        owner, hook, milestone = self.launch()
        (self.root/'arm-read').write_text('pause next actual child journal read')
        end = time.monotonic()+10
        while not (self.root/'reader-ready').exists() and time.monotonic()<end:
            self.assertIsNone(owner.process.poll())
            time.sleep(.01)
        self.assertTrue((self.root/'reader-ready').exists())
        from agent_harness import supervisor_store
        original_lock = supervisor_store._os_lock
        attempted, done = threading.Event(), threading.Event()
        result, errors = [], []
        @contextmanager
        def observed_lock(path):
            if path == hook.store.path('locks/supervisor.guard'):
                attempted.set()
            with original_lock(path):
                yield
        def claim():
            try:
                result.append(hook.claim(milestone))
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()
        thread = threading.Thread(target=claim)
        try:
            with patch.object(supervisor_store, '_os_lock', observed_lock):
                thread.start()
                self.assertTrue(attempted.wait(5), 'parent must attempt the actual cross-process lock')
                self.assertFalse(done.wait(.2), 'append completed while child held a strict read snapshot')
                self.assertIsNone(owner.process.poll())
        finally:
            (self.root/'release-read').write_text('finish read before parent append')
            if thread.ident is not None:
                thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        owner.interrupt(hook, result[0]['operation_id'])
        self.assertEqual(owner.process.returncode, 86)
        restarted = owner.restart(hook, result[0]['operation_id'])
        self.addCleanup(restarted.close)
        out, err = restarted.wait(timeout=30)
        self.assertEqual(restarted.process.returncode, 0, (out, err))
        report = json.loads((self.root/'report.json').read_text())
        self.assertEqual(report['completed_trials'], 1, report)
        self.assertEqual(report['usage']['observed_tokens'], 4)
        self.assertEqual(self.calls(), ['synthetic'])

    def test_corrupt_snapshot_fails_once_without_replaying_read_or_repairing_bytes(self):
        owner, hook, _ = self.launch()
        owner.close()
        journal = hook.store.path('journal/events.jsonl')
        corrupt = journal.read_bytes()+b'not a journal record\n'
        journal.write_bytes(corrupt)
        with patch.object(hook.store, 'events', wraps=hook.store.events) as read:
            with self.assertRaisesRegex(HarnessError, 'Corrupt journal record'):
                hook.records()
            self.assertEqual(read.call_count, 1)
        self.assertEqual(journal.read_bytes(), corrupt)
        self.assertEqual(self.calls(), ['synthetic'])


if __name__ == '__main__':
    unittest.main()
