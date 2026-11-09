"""Explicit upgrades of public c9 source bytes; no client/model calls.

These required source qualification cases need the immutable public baseline
tree in Git. An extracted non-Git unit context is not this qualification and
must not turn the entire required class into a successful skip.
"""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import completion_eligibility, plan_quality, state_migration as migration
from agent_harness.agent_lifecycle import GlobalSlots
from agent_harness.core import HarnessError, digest
from agent_harness.strict_yaml import dump, load, validate
from agent_harness.supervisor import Supervisor
from agent_harness.supervisor_store import SupervisorStore

ROOT = Path(__file__).resolve().parents[1]
# c9 baseline tree, independent of rewritten/local commit identities. A tree-ish
# archive preserves the actual public old writer even when its commit is absent.
PUBLIC_BASELINE_TREE = 'fac67b4f88e0e457c1c7e470d75c05bacf3d1c1b'
BASELINE_SCHEMAS = ('work-state.schema.json', 'work-contract.schema.json', 'agent-plan.schema.json',
                    'execution-receipt.schema.json', 'managed-review.schema.json', 'agent-state.schema.json')
# Later R02 additions have their own frozen bytes; they are not c9 schemas and
# do not require a private implementation commit to exist in CI.
R02_SCHEMA_GOLDENS = {
    'execution-receipt.v2.schema.json': 'fff078e29aa17fbf982e0ca1768f3ec39afa21b6f94a60a4843f2671fe8ddd52',
    'managed-review.v3.schema.json': '85438dca2c64372cc3cf95349dafa6df9b7201470a625b0c025deb1820a80cce',
}


class PlanQualityMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = tempfile.TemporaryDirectory(prefix='quality-old-writer-')
        cls.addClassCleanup(cls.old.cleanup)
        cls.old_root = Path(cls.old.name).resolve()
        archived = subprocess.run(['git', '-C', str(ROOT), 'archive', PUBLIC_BASELINE_TREE, 'src', 'schemas'],
            capture_output=True, timeout=20)
        if archived.returncode:
            raise AssertionError('REQUIRED public c9 baseline tree unavailable: migration source qualification '
                                 'cannot run; fetch the public baseline history (non-Git units are separately unqualified)')
        with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
            for item in archive.getmembers():
                target = cls.old_root/item.name
                if not target.resolve().is_relative_to(cls.old_root) or not (item.isdir() or item.isfile()):
                    raise AssertionError('Unsafe old-writer archive')
                if item.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(item).read())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='quality-migration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root/'source'
        self.source.mkdir()
        (self.source/'calc.py').write_text('VALUE = 42\n')
        self.run = self.root/'run'
        self.state_dir = self.root/'state'
        check = {'id': 'check', 'kind': 'command', 'required': True,
                 'argv': [sys.executable, '-c', 'print("synthetic check")'], 'timeout': 5}
        self.contract = {'schema_version': 2, 'mode': 'managed', 'memory': True, 'ledger': 'inform',
            'task': {'id': 'migration-fixture', 'objective': 'Verify one bounded migration outcome',
                     'allowed_write_scopes': ['calc.py']},
            'mission': {'id': 'migration-mission', 'generation': 1, 'original_request': 'Verify the result',
                'global_constraints': ['No publication'], 'non_goals': [], 'requirements': [
                    {'id': 'R-check', 'text': 'Verify the result', 'provenance': 'original request',
                     'kind': 'behavior', 'required': True, 'acceptance_ids': ['check']}]},
            'node_checks': [deepcopy(check)], 'acceptance': [check],
            'budgets': {'max_attempts': 3, 'max_wall_seconds': 600, 'max_tokens': 1000}}
        contract_path = self.root/'input.yaml'
        dump(contract_path, self.contract)
        code = '''import sys
sys.path.insert(0,sys.argv[1])
from agent_harness.supervisor import Supervisor
Supervisor.create(sys.argv[2],run_dir=sys.argv[3],cwd=sys.argv[4],state_dir=sys.argv[5])
'''
        created = subprocess.run([sys.executable, '-I', '-c', code, str(self.old_root/'src'),
            str(contract_path), str(self.run), str(self.source), str(self.state_dir)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.store = SupervisorStore(self.run, plan_quality.STATE_SCHEMA, create=False)
        self.contract = self.store.read('contract.yaml', 'work-contract.schema.json')
        self.assertEqual(self.state()['schema_version'], 2)
        # c9 does not publish R02 policy. Seed that later protocol explicitly for
        # pinned-v2 cases; this is fixture publication, never automatic upgrade.
        original_state = (self.run/'state.yaml').read_bytes()
        source = Supervisor(self.run)
        source.load()
        with source.store.lease('explicit R02 completion-policy fixture publication'):
            completion_eligibility.pin(source)
        self.assertEqual((self.run/'state.yaml').read_bytes(), original_state)

    def state(self):
        return self.store.recover(read_only=True)['state']

    def files(self):
        return {path.relative_to(self.run).as_posix(): path.read_bytes() for path in self.run.rglob('*')
                if path.is_file() and 'locks' not in path.relative_to(self.run).parts}

    def preview(self, **kwargs):
        return migration.migrate(self.run, target_version=3, expected_generation=self.state()['generation'], **kwargs)

    def apply(self, **kwargs):
        preview = self.preview(**kwargs)
        return migration.migrate(self.run, target_version=3, expected_generation=self.state()['generation'],
            apply=True, expected_plan_digest=preview['plan_digest'], **kwargs)

    def recover(self):
        state = self.state()
        return migration.migrate(self.run, target_version=3, expected_generation=state['generation'],
            recover=state['migration']['operation_id'])

    def commit(self, **updates):
        state = self.state()
        state.update(updates)
        state['generation'] += 1
        self.store.commit(state, 'synthetic historical source fixture')
        return state

    def no_old_writer(self):
        before = self.files()
        code = '''import json,sys
sys.path.insert(0,sys.argv[1])
from agent_harness.supervisor import Supervisor
class NoClient:
    def capabilities(self): raise AssertionError("Client boundary must never be reached")
    def run(self,*a,**k): raise AssertionError("Model boundary must never be reached")
try:
    Supervisor(sys.argv[2],adapter=NoClient()).resume()
except (ValueError,RuntimeError) as error:
    print(json.dumps({"blocked":type(error).__name__=="StrictYAMLError" and
        any(marker in str(error) for marker in ("schema_version","plan_quality")),"type":type(error).__name__}))
else:
    raise SystemExit("old writer accepted state3")
'''
        result = subprocess.run([sys.executable, '-I', '-c', code, str(self.old_root/'src'), str(self.run)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['blocked'], result.stdout)
        self.assertEqual(self.files(), before)

    def unpinned_source(self):
        state = self.state()
        self.run = self.root/'historical-unpinned'
        self.store = SupervisorStore(self.run, plan_quality.STATE_SCHEMA)
        self.store.write('contract.yaml', self.contract, 'work-contract.schema.json')
        state['generation'] = 1
        self.store.commit(state, 'synthetic historical unpinned v2 head')

    def test_read_only_legacy_status_and_preview_do_not_install_quality(self):
        self.unpinned_source()
        before = self.files()
        Supervisor(self.run).load()
        Supervisor(self.run).status()
        first = self.preview()
        self.assertEqual(first, self.preview())
        self.assertEqual(self.files(), before)
        self.assertFalse(list(self.run.glob('policies/plan-quality-*')))
        self.assertFalse((self.run/completion_eligibility.POLICY_PATH).exists())
        self.assertFalse(first['mutated'])
        self.assertEqual(first['plan']['source_version'], 2)
        self.assertEqual(first['plan']['destination_version'], 3)
        with self.assertRaisesRegex(HarnessError, 'UNSUPPORTED_VERSION'):
            migration.migrate(self.run, expected_generation=self.state()['generation'])

    def test_upgrade_preserves_original_budget_usage_history_and_requires_fresh_assessment(self):
        source = Supervisor(self.run)
        source.load()
        registry = GlobalSlots(self.state_dir)
        intent = source.receipts().intent('@integration', 'old-attempt', checkout=self.source, owner='synthetic-owned-slot')
        source.receipts().completion(intent, {'status': 'failed', 'process_tree_stopped': True,
            'usage': {'input_tokens': 5, 'output_tokens': 2}, 'turn_started': True})
        source.receipts().reconcile_usage()
        source.state['attempts_used'] = 1
        source.state['failure_history'] = [{'category': 'environment', 'signature': 'f'*64,
            'detail': 'synthetic retained history', 'repairable': True, 'snapshot_id': source.state['baseline_digest'], 'attempt': 1}]
        source._save('synthetic stopped invocation; counters already reconciled')
        before, old_files = self.state(), self.files()
        receipt = self.apply()
        after = self.state()
        for key, value in before.items():
            if key not in {'schema_version', 'status', 'reason', 'generation', 'updated_at', 'migration'}:
                self.assertEqual(after[key], value, key)
        self.assertEqual(after['schema_version'], 3)
        self.assertEqual(after['status'], 'RECOVERY_REQUIRED')
        self.assertEqual(after['plan_quality']['phase'], 'unassessed')
        self.assertEqual(after['plan_quality']['rounds_used'], 0)
        self.assertIsNone(after['plan_quality']['assessment_ref'])
        self.assertEqual(receipt['assessment'], 'unassessed')
        self.assertEqual(after['metrics'], before['metrics'])
        self.assertEqual(after['deadline'], before['deadline'])
        for name, raw in old_files.items():
            if name not in {'state.yaml', 'journal/events.jsonl'}:
                self.assertEqual((self.run/name).read_bytes(), raw, name)
        self.assertEqual(len([e for e in self.store.events() if e['payload'].get('kind') == 'plan_quality_policy']), 1)
        self.assertEqual(registry.inventory(), [])
        self.no_old_writer()

    def test_generation_source_and_target_artifact_CAS_fail_without_conversion(self):
        before = self.files()
        preview = self.preview()
        with self.assertRaisesRegex(HarnessError, 'GENERATION_CONFLICT'):
            migration.migrate(self.run, target_version=3, expected_generation=0)
        self.assertEqual(before, self.files())
        (self.source/'calc.py').write_text('VALUE = 13\n')
        with self.assertRaisesRegex(HarnessError, 'PLAN_CHANGED'):
            migration.migrate(self.run, target_version=3, expected_generation=self.state()['generation'],
                apply=True, expected_plan_digest=preview['plan_digest'])
        self.assertEqual(before, self.files())
        preview = self.preview()
        target = preview['plan']['target_policy_ref']['path']
        path = self.store.path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
        with self.assertRaisesRegex(HarnessError, 'ARTIFACT_CONFLICT'):
            migration.migrate(self.run, target_version=3, expected_generation=self.state()['generation'],
                apply=True, expected_plan_digest=preview['plan_digest'])
        self.assertEqual(self.state()['schema_version'], 2)
        self.assertFalse((self.run/'migrations').exists())

    def test_successor_committed_contract_path_is_used(self):
        successor = deepcopy(self.contract)
        successor['mission']['generation'] = 2
        successor['mission']['previous_digest'] = digest(self.contract['mission'])
        path = 'missions/2/contract.yaml'
        self.store.write(path, successor, 'work-contract.schema.json', exclusive=True)
        state = self.state()
        agent = {'schema_version': 1, 'id': 'retained', 'role': 'owner', 'task_contract_digest': 'f'*64,
            'worktree': {'schema_version': 1, 'id': 'retained', 'path': str(self.source), 'base_revision': 'non-git',
                'branch': None, 'role': 'owner', 'ownership_token': 'synthetic-history', 'created_at': state['started_at']},
            'thread_id': None, 'process_identity': None, 'status': 'stopped', 'started_at': state['started_at'],
            'last_progress_at': state['last_progress_at'], 'allowed_write_scope': ['calc.py'], 'read_only': False,
            'deadline': state['deadline'], 'max_attempts': 1, 'attempts_used': 0, 'stdout_offset': 0, 'stderr_offset': 0,
            'checkpoint': 0, 'spawn_reason': 'retained synthetic historical agent'}
        agent_path = 'agents/mission-2/retained.yaml'
        self.store.write(agent_path, agent, 'agent-state.schema.json')
        self.commit(contract_path=path, contract_digest=digest(successor), agents=['retained'])
        preview = self.preview()
        self.assertEqual(preview['plan']['contract_path'], path)
        self.assertIn(path, {r['path'] for r in preview['plan']['targets']})
        self.assertIn(agent_path, {r['path'] for r in preview['plan']['targets']})
        self.apply()
        self.assertEqual(self.state()['contract_path'], path)
        self.assertEqual(self.state()['contract_digest'], digest(successor))

    def test_old_terminal_acceptance_does_not_retroapprove_new_quality(self):
        self.commit(status='ACCEPTED', reason='synthetic historical acceptance marker')
        self.apply()
        self.assertEqual(self.state()['status'], 'RECOVERY_REQUIRED')
        self.assertEqual(self.state()['plan_quality']['phase'], 'unassessed')
        self.assertIsNone(self.state()['plan_quality']['assessment_ref'])

    def test_round_limit_only_tightens_and_unknown_versions_fail_closed(self):
        for limit in (0, -1, True, 1.0, self.contract['budgets']['max_attempts']+1):
            with self.subTest(limit=limit), self.assertRaises((HarnessError, ValueError)):
                self.preview(max_planning_rounds=limit)
        for version in (True, 0, 4):
            with self.subTest(version=version), self.assertRaises(HarnessError):
                migration.migrate(self.run, target_version=version, expected_generation=self.state()['generation'])
        self.apply(max_planning_rounds=1)
        self.assertEqual(self.state()['plan_quality']['max_rounds'], 1)
        with self.assertRaisesRegex(HarnessError, 'UNSUPPORTED_VERSION'):
            self.preview()

    def test_unresolved_invocation_never_becomes_quiescent(self):
        supervisor = Supervisor(self.run)
        supervisor.load()
        supervisor.receipts().intent('@planner', 'old-attempt', checkout=self.source, owner='unresolved')
        before = self.files()
        with self.assertRaises(HarnessError):
            self.preview()
        self.assertEqual(before, self.files())

    def test_missing_or_uncertain_registry_blocks_without_changing_counters(self):
        self.commit(attempts_used=1)
        before = self.files()
        with self.assertRaisesRegex(HarnessError, 'missing registry'):
            self.preview()
        self.assertEqual(before, self.files())
        registry = GlobalSlots(self.state_dir)
        with registry.acquire(self.state()['run_id'], self.state()['task_id'], 'synthetic'):
            with self.assertRaisesRegex(HarnessError, 'registry'):
                self.preview()
        self.assertEqual(before, self.files())

    def test_expired_initial_admission_refuses_without_writes(self):
        expired = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat().replace('+00:00', 'Z')
        self.commit(deadline=expired)
        before = self.files()
        with self.assertRaisesRegex(HarnessError, 'DEADLINE_EXPIRED'):
            self.preview()
        self.assertEqual(before, self.files())

    def test_before_barrier_failure_leaves_legacy_head_and_no_quality_pins(self):
        before = self.state()
        commit = SupervisorStore.commit
        def fail(store, state, *args, **kwargs):
            if state.get('migration', {}).get('status') == 'pending':
                raise OSError('injected before barrier')
            return commit(store, state, *args, **kwargs)
        with patch.object(SupervisorStore, 'commit', fail), self.assertRaises(OSError):
            self.apply()
        self.assertEqual(self.state(), before)
        self.assertFalse(any(e['payload'].get('kind') == 'plan_quality_policy' for e in self.store.events()))

    def test_pending_barrier_blocks_runtime_and_old_writer_before_policy_publication(self):
        with patch.object(plan_quality, 'publish_bootstrap', side_effect=OSError('injected bootstrap failure')):
            with self.assertRaises(OSError):
                self.apply()
        state = self.state()
        self.assertEqual(state['migration']['status'], 'pending')
        self.assertEqual(state['schema_version'], 3)
        self.assertFalse(self.store.path(state['plan_quality']['policy_ref']['path']).exists())
        with self.assertRaisesRegex(HarnessError, 'MIGRATION_RECOVERY_REQUIRED'):
            Supervisor(self.run).load()
        self.no_old_writer()
        first = self.recover()
        events = self.store.events()
        self.assertEqual(first, self.recover())
        self.assertEqual(events, self.store.events())

    def test_durable_barrier_or_final_with_missing_projection_recovers_exactly(self):
        self.check_missing_projection('pending')

    def test_durable_final_with_missing_projection_recovers_exactly(self):
        self.check_missing_projection('committed')

    def check_missing_projection(self, target):
        write = SupervisorStore.write
        def fail(store, name, value, *args, **kwargs):
            if str(name).startswith('checkpoints/') and value['state'].get('migration', {}).get('status') == target:
                raise OSError('injected checkpoint projection failure')
            return write(store, name, value, *args, **kwargs)
        with patch.object(SupervisorStore, 'write', fail), self.assertRaises(OSError):
            self.apply()
        self.no_old_writer()
        receipt = self.recover()
        self.assertEqual(load(self.run/'state.yaml', plan_quality.STATE_SCHEMA)['migration']['status'], 'committed')
        self.assertEqual(receipt['destination_state_digest'], digest(self.state()))

    def test_partial_policy_events_and_receipt_delivery_recover_without_duplicates(self):
        append = SupervisorStore.append_event
        def fail(store, event):
            if event.get('kind') == 'plan_quality_policy':
                raise OSError('injected after artifacts')
            return append(store, event)
        with patch.object(SupervisorStore, 'append_event', fail), self.assertRaises(OSError):
            self.apply()
        self.recover()
        self.assertEqual(sum(e['payload'].get('kind') == 'plan_quality_policy' for e in self.store.events()), 1)
        self.assertEqual(sum(e['payload'].get('kind') == 'completion_policy' for e in self.store.events()), 1)
        # Delivery failure after the final durable head uses the same intent.
        receipt_path = next(self.run.glob('migrations/*/receipt.yaml'))
        expected = load(receipt_path)
        receipt_path.unlink()  # Only this fixture's derived delivery receipt.
        self.assertEqual(self.recover()['destination_state_digest'], expected['destination_state_digest'])

    def test_unpinned_legacy_bootstrap_recovery_adds_exact_policy_pins_once(self):
        self.unpinned_source()
        append = SupervisorStore.append_event
        def fail(store, event):
            if event.get('kind') == 'completion_policy':
                raise OSError('injected after quality pin before completion pin')
            return append(store, event)
        with patch.object(SupervisorStore, 'append_event', fail), self.assertRaises(OSError):
            self.apply()
        self.assertEqual(self.state()['migration']['status'], 'pending')
        self.no_old_writer()
        self.recover()
        for kind in ('completion_policy', 'plan_quality_policy'):
            self.assertEqual(sum(e['payload'].get('kind') == kind for e in self.store.events()), 1, kind)

    def test_receipt_write_failure_reconciles_committed_head_without_republication(self):
        write = SupervisorStore.write
        def fail(store, name, *args, **kwargs):
            if str(name).endswith('/receipt.yaml'):
                raise OSError('injected receipt delivery failure')
            return write(store, name, *args, **kwargs)
        with patch.object(SupervisorStore, 'write', fail), self.assertRaises(OSError):
            self.apply()
        state, events = self.state(), self.store.events()
        self.assertEqual(state['migration']['status'], 'committed')
        self.assertEqual(self.recover()['destination_state_digest'], digest(state))
        self.assertEqual(self.store.events(), events)

    def test_frozen_pending_recovery_can_finish_after_expiry_without_new_budget(self):
        deadline = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.commit(deadline=deadline.isoformat().replace('+00:00', 'Z'))
        class BeforeExpiry(datetime):
            @classmethod
            def now(cls, tz=None):
                return deadline-timedelta(seconds=1)
        with patch('datetime.datetime', BeforeExpiry), patch.object(plan_quality, 'publish_bootstrap', side_effect=OSError('pending')):
            with self.assertRaises(OSError):
                self.apply()
        old = self.state()
        with self.assertRaisesRegex(HarnessError, 'FROZEN_POLICY'):
            migration.migrate(self.run, target_version=3, expected_generation=old['generation'],
                recover=old['migration']['operation_id'], max_planning_rounds=1)
        self.recover()
        self.assertEqual(self.state()['deadline'], old['deadline'])
        self.assertEqual(self.state()['metrics'], old['metrics'])
        self.assertEqual(self.state()['attempts_used'], old['attempts_used'])

    def test_changed_backup_or_frozen_final_cannot_reset_counters(self):
        with patch.object(plan_quality, 'publish_bootstrap', side_effect=OSError('injected pending')):
            with self.assertRaises(OSError):
                self.apply()
        intent_path = next(self.run.glob('migrations/*/intent.yaml'))
        intent = load(intent_path, migration.INTENT_V2_SCHEMA)
        original = intent_path.read_bytes()
        intent['final_state']['attempts_used'] += 1
        intent.pop('payload_digest')
        intent['payload_digest'] = digest(intent)
        dump(intent_path, intent, migration.INTENT_V2_SCHEMA)
        with self.assertRaisesRegex(HarnessError, 'INVALID_DESTINATION'):
            self.recover()
        intent_path.write_bytes(original)
        backup = intent_path.parent/'backup/contract.yaml'
        backup.write_bytes(backup.read_bytes()+b'\n')
        with self.assertRaisesRegex(HarnessError, 'BACKUP_CHANGED'):
            self.recover()

    def test_policy_ref_must_bind_exact_prepared_artifacts_before_barrier(self):
        before = self.files()
        prepare = plan_quality.prepare_upgrade
        def corrupt(*args, **kwargs):
            material = prepare(*args, **kwargs)
            material['state']['plan_quality']['policy_ref']['sha256'] = '0'*64
            return material
        with patch.object(plan_quality, 'prepare_upgrade', corrupt), self.assertRaisesRegex(HarnessError, 'POLICY_BINDING'):
            self.preview()
        self.assertEqual(before, self.files())

    def test_legacy_migration_schemas_and_state_reader_bytes_stay_frozen(self):
        for name in BASELINE_SCHEMAS:
            old_bytes = (self.old_root/'schemas'/name).read_bytes()
            current_bytes = (ROOT/'schemas'/name).read_bytes()
            if name in {'work-contract.schema.json', 'agent-plan.schema.json'}:
                expected = json.loads(old_bytes)
                # These are the only authorized backward-compatible extensions
                # to c9. The archived old schemas/writer remain unchanged.
                if name == 'work-contract.schema.json':
                    budget = expected['properties']['budgets']['properties']['max_concurrent_managed_agents']
                    self.assertEqual(budget['maximum'], 5)
                    budget['maximum'] = 12
                else:
                    task = expected['properties']['tasks']['items']
                    self.assertNotIn('reasoning_effort', task['properties'])
                    self.assertNotIn('reasoning_effort', task['required'])
                    task['properties']['reasoning_effort'] = {'enum': ['medium', 'high', 'ultra']}
                self.assertEqual(json.loads(current_bytes), expected, name)
            else:
                self.assertEqual(old_bytes, current_bytes, name)
        for name, expected in R02_SCHEMA_GOLDENS.items():
            self.assertEqual(hashlib.sha256((ROOT/'schemas'/name).read_bytes()).hexdigest(), expected, name)
        code = '''import json,sys
sys.path.insert(0,sys.argv[1])
from agent_harness import state_migration as m
from agent_harness.core import digest
print(json.dumps([digest(m.PLAN_SCHEMA),digest(m.INTENT_SCHEMA),digest(m.RECEIPT_SCHEMA)]))
'''
        result = subprocess.run([sys.executable, '-I', '-c', code, str(self.old_root/'src')],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [digest(migration.PLAN_SCHEMA), digest(migration.INTENT_SCHEMA), digest(migration.RECEIPT_SCHEMA)])

    def test_start_CLI_forwards_tightened_round_limit_without_dispatch(self):
        from agent_harness import work_cli
        class NoClient:
            def capabilities(self):
                raise AssertionError('No actual client qualification in a CLI creation fixture')
        run = self.root/'cli-new-run'
        with patch.object(work_cli, '_adapter', return_value=NoClient()), patch.object(work_cli, '_foreground',
                return_value={'status': 'NEW'}), redirect_stdout(io.StringIO()):
            result = work_cli.dispatch(['start', '--contract', str(self.root/'input.yaml'), '--run-dir', str(run),
                '--state-dir', str(self.state_dir), '--cwd', str(self.source), '--max-planning-rounds', '1', '--format', 'json'])
        self.assertEqual(result, 1)  # Creation is not acceptance.
        self.assertEqual(load(run/'state.yaml', plan_quality.STATE_SCHEMA)['plan_quality']['max_rounds'], 1)

    def test_exported_versioned_schemas_and_actual_cli_dry_run(self):
        for name, value in [('plan', migration.PLAN_V2_SCHEMA), ('intent', migration.INTENT_V2_SCHEMA),
                            ('receipt', migration.RECEIPT_V2_SCHEMA)]:
            self.assertEqual(json.loads((ROOT/'schemas'/('work-state-migration-'+name+'.v2.schema.json')).read_bytes()), value)
        preview = self.preview()
        validate(preview['plan'], 'work-state-migration-plan.v2.schema.json')
        with self.assertRaises(ValueError):
            validate({**preview['plan'], 'unknown': True}, migration.PLAN_V2_SCHEMA)
        before = self.files()
        result = subprocess.run([sys.executable, str(ROOT/'tools/harness.py'), 'work', 'migrate',
            '--run-dir', str(self.run), '--expected-generation', str(self.state()['generation']),
            '--target-version', '3', '--max-planning-rounds', '1'], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['plan']['destination_version'], 3)
        self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
