"""Typed root stops and actual journal/lease/core recovery boundaries."""
from copy import deepcopy
from contextlib import ExitStack, contextmanager, nullcontext
import hashlib
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import shutil
import signal
import time
import unittest
from unittest.mock import patch

from agent_harness import root_preparation as protocol
from agent_harness.agent_lifecycle import GlobalSlots
from agent_harness.core import HarnessError, Run, canonical, digest, file_hash
from agent_harness.failure_policy import PreparationFailure, PREPARATION_TARGETS
from agent_harness.strict_yaml import dump, load
from agent_harness.supervisor import Supervisor
from agent_harness.supervisor_store import SupervisorStore
from managed_process_fixture import BoundFixtureCodexAdapter, bound_fake_script
import test_work_supervisor as native_fixtures
from test_execution_durability import Crash
from test_plan_quality_migration import PUBLIC_BASELINE_TREE
import test_verified_flow as managed_fixtures

EXPECTED_PREPARATION_TARGETS = {
    'NO_PROGRESS':'NO_PROGRESS', 'ATTEMPT_BUDGET':'FAILED_TERMINAL', 'TOKEN_BUDGET':'FAILED_TERMINAL',
    'DEADLINE':'TIMED_OUT', 'ENVIRONMENT':'BLOCKED_ENVIRONMENT', 'APPROVAL':'BLOCKED_APPROVAL',
    'INTEGRITY':'UNKNOWN_OUTCOME', 'AMBIGUOUS':'BLOCKED_AMBIGUOUS', 'INTERNAL':'UNKNOWN_OUTCOME',
    'USAGE_UNVERIFIED':'BLOCKED_ENVIRONMENT', 'CANCELLED':'CANCELLED',
}


class RootPreparationTests(unittest.TestCase):
    setUp = native_fixtures.WorkSupervisorTests.setUp
    tearDown = native_fixtures.WorkSupervisorTests.tearDown
    create = native_fixtures.WorkSupervisorTests.create

    def fixture(self, *, attempts=3, memory=False, contract_edit=None, mode='simple'):
        self.fake.write_text(bound_fake_script(native_fixtures.FAKE))
        self.adapter = BoundFixtureCodexAdapter((sys.executable, str(self.fake)))
        owner = self.create(mode=mode,attempts=attempts)
        if memory or contract_edit:
            contract = deepcopy(owner.contract)
            if memory:contract.update(memory=True, ledger='inform')
            if contract_edit:contract_edit(contract)
            path = self.root / 'memory-task.yaml'; dump(path, contract)
            owner = Supervisor.create(path, run_dir=self.root / 'private' / 'memory-run', cwd=self.source,
                state_dir=self.root / 'global-state', adapter=self.adapter)
        return owner

    def committed(self, owner):
        owner.load(recover=True)
        for state in ('PREFLIGHT', 'EXECUTING', 'FAILED_REPAIRABLE', 'REPAIRING'):
            owner.transition(state, 'fixture actual committed repair phase')

    def calls(self):
        path = self.fake.with_suffix('.calls')
        return path.read_bytes() if path.exists() else b''

    def test_typed_categories_preserve_committed_origin_and_ignore_misleading_text(self):
        message = 'approval NO_PROGRESS deadline exhausted missing tool; diagnostic only'
        self.assertEqual(set(PREPARATION_TARGETS),set(EXPECTED_PREPARATION_TARGETS))
        for index, code in enumerate((*EXPECTED_PREPARATION_TARGETS, 'UNTYPED')):
            if index:self.tearDown();self.setUp()
            owner = self.fixture()
            with owner.store.lease('fixture typed preparation failure'):
                self.committed(owner); before = owner.state['attempts_used']; deadline = owner.state['deadline']
                proof = None
                if code == 'NO_PROGRESS':
                    source, _ = protocol.binding(owner, 'repair')
                    failure = dict(category='test_failure', signature=digest('same failure'), repairable=True, detail=message)
                    owner.state['current_failure'] = failure
                    owner.state['attempts_used'] = 2
                    before = 2
                    owner.state['failure_history'] = [dict(failure, snapshot_id=source['source_snapshot_digest'], attempt=n) for n in (1,2)]
                    owner._save('fixture consecutive independently observed failures')
                    proof = dict(failure_signature=failure['signature'], source_snapshot_digest=source['source_snapshot_digest'],
                        history_digest=digest(owner.state['failure_history']))
                error = HarnessError(message) if code == 'UNTYPED' else PreparationFailure(code, 'core_prepare', message, proof=proof)
                with patch.object(owner, '_new_core_attempt', side_effect=error):result = owner._loop()
                expected = 'UNKNOWN_OUTCOME' if code == 'UNTYPED' else EXPECTED_PREPARATION_TARGETS[code]
                self.assertEqual(result['status'], expected, result)
                self.assertEqual(owner.state['attempts_used'], before)
                self.assertEqual(owner.state['deadline'], deadline)
                self.assertEqual(self.calls(), b'')
                row = protocol.active_failure(owner)
                self.assertEqual(row['origin_state'], 'REPAIRING')
                packet = protocol._check_diagnostic(owner, row)
                self.assertIn(message, [r['detail'] for r in packet['causes']])
                self.assertNotIn('Illegal supervisor transition', json.dumps(packet))

    def test_no_progress_needs_actual_consecutive_failure_proof(self):
        owner = self.fixture()
        with owner.store.lease('fixture false no progress'):
            self.committed(owner)
            with self.assertRaisesRegex(HarnessError, 'proof'):
                owner.transition('NO_PROGRESS', 'arbitrary explanation is not authority')
            with patch.object(owner, '_new_core_attempt', side_effect=PreparationFailure('NO_PROGRESS', 'core_prepare', 'same reason')):
                result = owner._loop()
            self.assertEqual(result['status'], 'UNKNOWN_OUTCOME')
            self.assertEqual(protocol.active_failure(owner)['reason_code'], 'INTEGRITY')
            self.assertEqual(self.calls(), b'')

    def test_causal_packet_survives_transition_failure_and_sanitizer_refusal(self):
        owner = self.fixture()
        with owner.store.lease('fixture causal durability'):
            self.committed(owner)
            original = RuntimeError('specific original preparation failure')
            with patch.object(owner, '_new_core_attempt', side_effect=original),patch.object(owner, 'transition', side_effect=HarnessError('secondary transition failed')):
                result = owner._loop()
            self.assertEqual(result['status'], 'UNKNOWN_OUTCOME')
            self.assertEqual(owner.store.read('state.yaml')['status'], 'REPAIRING')
            row = protocol.active_failure(owner)
            self.assertEqual(protocol._check_diagnostic(owner, row)['causes'][0]['detail'], str(original))
            with patch.dict(os.environ, {'API_SECRET': 'private-fixture-value-12345'}):
                with patch.object(protocol, 'sanitized', side_effect=HarnessError('sanitizer failed')):
                    value = protocol.handle_failure(owner, PreparationFailure('ENVIRONMENT', 'core_prepare', 'API_SECRET=private-fixture-value-12345'))
                raw = owner.store.path(value['diagnostic_ref']['path']).read_bytes()
                self.assertNotIn(b'private-fixture-value-12345', raw)
                self.assertEqual(value['reason_code'], 'ENVIRONMENT')
                self.assertFalse(json.loads(raw)['causes'][0]['sanitized'])
            for command_error in (subprocess.CalledProcessError(2,['fixture-adapter','one line private argument']),
                    subprocess.TimeoutExpired(['fixture-adapter','one line private argument'],2)):
                copied=RuntimeError(str(command_error));copied.__cause__=command_error
                deep=copied
                for _ in range(9):
                    outer=RuntimeError(str(command_error));outer.__cause__=deep;deep=outer
                for rendered in (copied,deep):
                    ref,packet=protocol._diagnostic(owner,rendered,'INTERNAL','invocation_boundary','REPAIRING')
                    raw=owner.store.path(ref['path']).read_bytes()
                    self.assertNotIn(b'one line private argument',raw)
                    self.assertTrue(all(row['truncated'] and not row['sanitized'] for row in packet['causes']))
                    self.assertEqual(packet['omitted_causes'],int(rendered is deep))

    def test_default_core_attempt_cli_semantics_and_frozen_schema_remain(self):
        owner = self.fixture()
        with owner.store.lease('fixture ordinary core attempts'):
            owner.load();run = owner._core(); original = file_hash(run.path/'origin-baseline.json')
            from agent_harness.work_contract import legacy_contract
            for number in (2,3):
                contract = legacy_contract(owner.contract, cwd=owner.state['cwd'], run_id=owner.state['run_id'],
                    attempt=number, deadline=owner.state['deadline'])
                run.new_attempt(contract, reason='same ordinary CLI reason')
            self.assertEqual(len(run.state()['attempts']), 3)
            self.assertEqual({p.name for p in (run.path/'attempts').iterdir()},{'attempt-1','attempt-2','attempt-3'})
            self.assertEqual(file_hash(run.path/'origin-baseline.json'), original)
            with self.assertRaisesRegex(HarnessError, 'budget'):
                run.new_attempt(dict(contract, attempt_id='attempt-4'), reason='another hypothesis')
        schema = Path(__file__).resolve().parents[1]/'schemas/supervisor-journal-event.schema.json'
        archived = subprocess.check_output(['git','show',PUBLIC_BASELINE_TREE+':schemas/supervisor-journal-event.schema.json'])
        self.assertEqual(schema.read_bytes(),archived)

    def test_mixed_journal_versions_default_append_and_future_rejection(self):
        owner = self.fixture()
        self.assertEqual(owner.start()['status'], 'ACCEPTED')
        records = owner.store.events();self.assertEqual({r['schema_version'] for r in records},{1,2})
        with owner.store.lease('fixture version controls'):
            value = owner.store.append_event({'kind':'ordinary_fixture'})
            self.assertEqual(value['schema_version'],1)
            raw = owner.store.path('journal/events.jsonl').read_bytes()
            with self.assertRaises(HarnessError):owner.store.append_event({'kind':'ordinary_fixture'},format_version=2)
            root = next(r for r in records if r['schema_version']==2)
            for version,payload in ((1,root['payload']),(3,root['payload']),(2,{'kind':'ordinary_fixture'})):
                row=dict(schema_version=version,sequence=1,event_id='a'*32,timestamp=root['timestamp'],previous_hash='0'*64,payload=payload)
                row['hash']=digest(row)
                with self.subTest(version=version),self.assertRaises((HarnessError,ValueError)):
                    SupervisorStore._decode_events(canonical(row)+b'\n')
            self.assertEqual(owner.store.path('journal/events.jsonl').read_bytes(),raw)

    def test_root_fact_replay_conflicts_and_causal_parents_are_validated(self):
        owner=self.fixture()
        with owner.store.lease('fixture exact root operation publication'):
            owner.load();owner._core()
            rows=protocol.events(owner)
            intent=next(row for row in rows if row.get('kind')=='root_core_preparation_intent')
            committed=next(row for row in rows if row.get('kind')=='root_core_preparation_commit')
            raw=owner.store.path('journal/events.jsonl').read_bytes()
            fields={k:v for k,v in intent.items() if k not in {'schema_version','kind'}}
            self.assertEqual(protocol.publish(owner,intent['kind'],**fields),intent)
            self.assertEqual(owner.store.path('journal/events.jsonl').read_bytes(),raw)
            changed=deepcopy(fields);changed['material_ref']['sha256']='f'*64
            with self.assertRaisesRegex(PreparationFailure,'conflict'):
                protocol.publish(owner,intent['kind'],**changed)
            self.assertEqual(owner.store.path('journal/events.jsonl').read_bytes(),raw)
            # A valid hash chain and wire schema alone cannot establish the
            # relationship between a core commit and its preparation intent.
            corrupt=dict(committed,operation_id='e'*64)
            owner.store.append_event(corrupt,format_version=2)
            with self.assertRaisesRegex(PreparationFailure,'causal parent'):
                protocol.events(owner)
            self.assertEqual(self.calls(),b'')

    def interrupted_reservation(self, owner, *, before_counter=False):
        saved = owner._save;publish = protocol.publish
        if before_counter:
            def published(supervisor,kind,**fields):
                result=publish(supervisor,kind,**fields)
                if kind=='root_model_reservation_bound':raise Crash()
                return result
            with patch.object(protocol,'publish',published),self.assertRaises(Crash):owner.start()
        else:
            def checkpoint(reason):
                result=saved(reason)
                if reason=='model attempt reserved at validated dispatch boundary':raise Crash()
                return result
            with patch.object(owner,'_save',checkpoint),self.assertRaises(Crash):owner.start()
        owner.load(recover=True)
        return protocol.pending_reservation(owner)

    def test_pending_at_cap_one_replays_exact_uuid_prompt_memory_ledger_and_usage(self):
        for index,before in enumerate((False,True)):
            if index:self.tearDown();self.setUp()
            owner = self.fixture(attempts=1,memory=True);deadline=owner.state['deadline']
            reservation = self.interrupted_reservation(owner,before_counter=before)
            self.assertEqual(self.calls(),b'')
            self.assertEqual(owner.state['attempts_used'],0 if before else 1)
            resumed=Supervisor(owner.path,state_dir=owner.state_dir,adapter=self.adapter)
            result=resumed.resume()
            self.assertEqual(result['status'],'ACCEPTED',self.adapter.failure_summary(result))
            self.assertEqual(self.calls(),b'exec\n')
            self.assertEqual(resumed.state['attempts_used'],1)
            self.assertEqual(resumed.state['deadline'],deadline)
            self.assertEqual(len(protocol.reservations(resumed)),1)
            intent=next(r for r in resumed.receipts().records() if r['record_type']=='invocation_intent')
            self.assertEqual(intent['invocation_id'],reservation['invocation_id'])
            self.assertEqual(result['metrics']['model_invocations'],1)
            self.assertEqual((result['metrics']['input_tokens'],result['metrics']['output_tokens']),(10,3))
            self.assertEqual(len(resumed._core().state()['attempts']),1)
            self.assertEqual(resumed.resume()['status'],'ACCEPTED')
            self.assertEqual(self.calls(),b'exec\n')

    def test_changed_optional_prompt_context_blocks_without_retarget_or_second_reservation(self):
        owner=self.fixture(memory=True)
        reservation=self.interrupted_reservation(owner);deadline=owner.state['deadline']
        resumed=Supervisor(owner.path,adapter=self.adapter);original=resumed._prompt
        with patch.object(resumed,'_prompt',lambda **kw:original(**kw)+'\nChanged optional advisory context'):
            result=resumed.resume()
        self.assertEqual(result['status'],'UNKNOWN_OUTCOME',result)
        self.assertEqual(self.calls(),b'')
        self.assertEqual(resumed.state['attempts_used'],1)
        self.assertEqual(resumed.state['deadline'],deadline)
        self.assertEqual(protocol.reservations(resumed),[reservation])
        self.assertFalse(any(r['record_type']=='invocation_intent' for r in resumed.receipts().records()))

    def hard_exit(self, owner, phase, *, resume=False):
        script=self.root/'hard-exit.py'
        script.write_text('''import os,sys
from pathlib import Path
from agent_harness import root_preparation as p
from agent_harness.core import Run
from agent_harness.execution_receipts import ExecutionReceipts
from agent_harness.supervisor import Supervisor
from agent_harness.supervisor_store import SupervisorStore
from agent_harness.file_io import ParentDirectory
from managed_process_fixture import BoundFixtureCodexAdapter
run,state,fake,phase,mode=sys.argv[1:]
repair_only=phase.startswith('repair_');phase=phase.removeprefix('repair_')
shared_only=phase.startswith('shared_');phase=phase.removeprefix('shared_')
if shared_only:
 from agent_harness import work_contract
 original_contract=work_contract.legacy_contract
 def shared_contract(*args,**kwargs):
  value=original_contract(*args,**kwargs);value['scope']['shared_files']=[{'path':'nested/.supervisor-root-owner.json','format':'json-records','allowed_records':['allowed']}];return value
 work_contract.legacy_contract=shared_contract
owner=Supervisor(run,state_dir=state,adapter=BoundFixtureCodexAdapter((sys.executable,fake)))
publish=p.publish;prepare=Run.prepare_supervisor_attempt;owned=p._owned_directory;save=owner._save;record=ExecutionReceipts.publish;write=SupervisorStore.write;newcore=owner._new_core_attempt;publish_file=p._publish_prepared_file;replace=ParentDirectory.replace
def applies(supervisor):return not repair_only or supervisor.state['status']=='REPAIRING' or getattr(supervisor,'_root_execution_origin',None)=='REPAIRING'
def published(supervisor,kind,**fields):
 value=publish(supervisor,kind,**fields)
 if applies(supervisor) and (phase=='core_intent' and kind=='root_core_preparation_intent' or phase=='files_manifest' and kind=='root_core_preparation_files' or phase=='core_commit' and kind=='root_core_preparation_commit' or phase=='reserved_before_counter' and kind=='root_model_reservation_bound' or phase=='release_authorized' and kind=='root_model_reservation_release_authorized'):os._exit(73)
 return value
def prepared(core,authority):
 value=prepare(core,authority)
 if phase=='core_state' and (not repair_only or authority.material['binding']['purpose']=='repair'):os._exit(73)
 return value
def directory(path,material):
 value=owned(path,material)
 if phase=='owned_attempt' and Path(path).name.startswith('attempt-') and (not repair_only or material['binding']['purpose']=='repair'):os._exit(73)
 return value
def checkpoint(reason):
 value=save(reason)
 if applies(owner) and phase=='reserved_counter' and reason=='model attempt reserved at validated dispatch boundary':os._exit(73)
 return value
def receipt(receipts,value):
 result=record(receipts,value)
 if phase=='invocation_intent' and value['record_type']=='invocation_intent' and value['node_id'] in {'@root','@integration'}:os._exit(73)
 return result
def written(store,name,value,*args,**kwargs):
 result=write(store,name,value,*args,**kwargs)
 if phase=='released_row' and Path(store.root).name=='global-model-slots' and name.startswith('slot-') and value.get('status')=='released':os._exit(73)
 return result
def file_published(authority,row):
 value=publish_file(authority,row)
 if phase=='published_file':os._exit(73)
 return value
def replaced(parent,staged):
 if phase=='state_alias' and parent.path.name=='state.json' and staged.startswith('.root-publish-'):os._exit(73)
 return replace(parent,staged)
p.publish=published;Run.prepare_supervisor_attempt=prepared;p._owned_directory=directory;owner._save=checkpoint;ExecutionReceipts.publish=receipt;SupervisorStore.write=written
p._publish_prepared_file=file_published
ParentDirectory.replace=replaced
if repair_only and phase=='before':owner._new_core_attempt=lambda reason:os._exit(73)
if phase=='unbound_workspace':Run.workspace_lock=lambda core:os._exit(73)
if phase=='bound_child':owner._on_process=lambda process:os._exit(73)
owner.resume() if mode=='resume' else owner.start()
raise AssertionError('Named hard exit boundary was not reached')
''')
        result=subprocess.run([sys.executable,str(script),str(owner.path),str(owner.state_dir),str(self.fake),phase,'resume' if resume else 'start'],
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')+os.pathsep+str(Path(__file__).parent)),
            capture_output=True,text=True,timeout=40)
        self.assertEqual(result.returncode,73,result.stdout+result.stderr)
        owner.load(recover=True)

    @unittest.skipUnless(os.name=='posix','Linux hard-exit/identity qualification; Windows separate')
    def test_real_hard_exits_reuse_owned_core_and_one_model_reservation(self):
        phases=('core_intent','owned_attempt','core_state','core_commit','reserved_before_counter','reserved_counter')
        for index,phase in enumerate(phases):
            if index:self.tearDown();self.setUp()
            owner=self.fixture(attempts=1,memory=True);deadline=owner.state['deadline']
            self.hard_exit(owner,phase)
            self.assertEqual(self.calls(),b'')
            result=Supervisor(owner.path,adapter=self.adapter).resume()
            with self.subTest(phase=phase):
                self.assertEqual(result['status'],'ACCEPTED',self.adapter.failure_summary(result))
                self.assertEqual(self.calls(),b'exec\n')
                fresh=Supervisor(owner.path,adapter=self.adapter);fresh.load()
                self.assertEqual(fresh.state['attempts_used'],1)
                self.assertEqual(fresh.state['deadline'],deadline)
                self.assertEqual(len(fresh._core().state()['attempts']),1)
                self.assertEqual(len(protocol.reservations(fresh)),1)
                self.assertEqual(fresh.resume()['status'],'ACCEPTED')
                self.assertEqual(self.calls(),b'exec\n')

    @unittest.skipUnless(os.name=='posix','Linux hard-exit/identity qualification; Windows separate')
    def test_real_release_hard_exit_and_independent_reuse_do_not_mutate_foreign_slot(self):
        for index,phase in enumerate(('release_authorized','released_row')):
            if index:self.tearDown();self.setUp()
            owner=self.fixture(attempts=1,memory=True);deadline=owner.state['deadline']
            self.hard_exit(owner,'reserved_counter')
            reservation=protocol.pending_reservation(owner);bound=protocol.reservation_bindings(owner,reservation)[-1]
            self.hard_exit(owner,phase,resume=True)
            if phase=='release_authorized':
                result=Supervisor(owner.path,adapter=self.adapter).resume()
            else:
                with GlobalSlots(owner.state_dir).acquire('independent-run','independent-task','owner') as foreign:
                    self.assertEqual(foreign.name,bound['slot_name'])
                    raw=foreign.store.path(foreign.name).read_bytes()
                    result=Supervisor(owner.path,adapter=self.adapter).resume()
                    self.assertEqual(foreign.store.path(foreign.name).read_bytes(),raw)
                    foreign.complete()
            with self.subTest(phase=phase):
                self.assertEqual(result['status'],'ACCEPTED',self.adapter.failure_summary(result))
                self.assertEqual(self.calls(),b'exec\n')
                fresh=Supervisor(owner.path,adapter=self.adapter);fresh.load()
                self.assertEqual(fresh.state['attempts_used'],1);self.assertEqual(fresh.state['deadline'],deadline)
                self.assertEqual(protocol.reservations(fresh),[reservation])
                releases=[r for r in protocol.events(fresh) if r.get('kind')=='root_model_reservation_released']
                self.assertEqual(releases[0]['observation'],'reused' if phase=='released_row' else 'exact')

    @unittest.skipUnless(os.name=='posix','Linux actual child/process-tree identity; Windows separate')
    def test_after_invocation_intent_or_actual_child_unknown_never_replays(self):
        from agent_harness.core import identity_alive,process_identity
        for index,phase in enumerate(('invocation_intent','bound_child')):
            if index:self.tearDown();self.setUp()
            owner=self.fixture(attempts=1);deadline=owner.state['deadline']
            self.hard_exit(owner,phase)
            rows=GlobalSlots(owner.state_dir).inventory(owner.state['run_id'])
            self.assertTrue(any(row['unresolved'] for row in rows))
            records=owner.receipts().records()
            self.assertEqual(len([r for r in records if r['record_type']=='invocation_intent']),1)
            self.assertFalse(any(r['record_type']=='completion' for r in records))
            child=next((row['child_identity'] for row in rows if row['child_identity']),None)
            try:
                if phase=='bound_child':
                    self.assertIsNotNone(child);self.assertNotEqual(child['created'],'unknown')
                    self.assertEqual(process_identity(child['pid']),child)
                with self.assertRaises(HarnessError):Supervisor(owner.path,adapter=self.adapter).resume()
                self.assertEqual(self.calls(),b'')
            finally:
                if child and identity_alive(child):
                    self.assertEqual(os.getpgid(child['pid']),child['pid'])
                    os.killpg(child['pid'],signal.SIGKILL)
                    end=time.monotonic()+5
                    while identity_alive(child) and time.monotonic()<end:time.sleep(.01)
            if child:
                self.assertFalse(identity_alive(child))
                with self.assertRaises(HarnessError):Supervisor(owner.path,adapter=self.adapter).resume()
            owner.load();self.assertEqual(owner.state['attempts_used'],1);self.assertEqual(owner.state['deadline'],deadline)
            self.assertTrue(any(row['unresolved'] for row in GlobalSlots(owner.state_dir).inventory(owner.state['run_id'])))

    @unittest.skipUnless(os.name=='posix','Linux directory/marker identity; Windows separate')
    def test_foreign_unmarked_and_same_bytes_replaced_core_directories_are_preserved(self):
        for index,kind in enumerate(('unmarked','same_bytes','wrong_operation')):
            if index:self.tearDown();self.setUp()
            owner=self.fixture()
            self.hard_exit(owner,'core_intent' if kind=='unmarked' else 'core_state')
            intent,material,_=protocol._materials(owner)[-1]
            target=Path(material['binding']['core_path'])
            if kind=='unmarked':target.mkdir();(target/'foreign.txt').write_text('foreign data')
            else:
                target=target/'attempts'/'attempt-1'
                if kind=='same_bytes':
                    saved=target.with_name('retained-original');target.rename(saved);shutil.copytree(saved,target)
                else:
                    marker=target/'.supervisor-root-owner.json';wire=json.loads(marker.read_text());wire['operation_id']='f'*64
                    marker.write_bytes(canonical(wire)+b'\n')
            before={str(p.relative_to(target)):p.read_bytes() for p in target.rglob('*') if p.is_file()}
            result=Supervisor(owner.path,adapter=self.adapter).resume()
            self.assertEqual(result['status'],'UNKNOWN_OUTCOME',result)
            self.assertEqual({str(p.relative_to(target)):p.read_bytes() for p in target.rglob('*') if p.is_file()},before)
            self.assertEqual(self.calls(),b'')

    @unittest.skipUnless(os.name=='posix','Linux physical file/ancestor replacement; Windows separate')
    def test_each_uncommitted_prepared_file_and_nested_parent_replacement_is_preserved(self):
        from agent_harness import work_contract
        legacy=work_contract.legacy_contract
        def shared(*args,**kwargs):
            value=legacy(*args,**kwargs)
            value['scope']['shared_files']=[dict(path='nested/.supervisor-root-owner.json',format='json-records',allowed_records=['allowed'])]
            return value
        cases=('state','baseline','contract','origin','shared','origin_shared','nested_parent','stage','missing_stage','missing_manifest')
        for index,kind in enumerate(cases):
            if index:self.tearDown();self.setUp()
            (self.source/'nested').mkdir();(self.source/'nested/.supervisor-root-owner.json').write_text('[{"id":"allowed","value":0}]\n')
            owner=self.fixture(attempts=1,memory=True);deadline=owner.state['deadline']
            self.hard_exit(owner,'shared_core_state')
            intent,material,committed=protocol._materials(owner)[-1];self.assertFalse(committed)
            run=Run(material['binding']['core_path'])
            fact=next(r for r in protocol.events(owner) if r.get('kind')=='root_core_preparation_files')
            manifest=protocol._blob_value(owner,fact['manifest_ref'])
            paths=dict(state='state.json',baseline='attempts/attempt-1/baseline.json',
                contract='contracts/'+digest(material['contract'])+'.json',origin='origin-baseline.json',
                shared='attempts/attempt-1/baseline-files/nested/.supervisor-root-owner.json',origin_shared='origin-baseline-files/nested/.supervisor-root-owner.json')
            if kind=='missing_manifest':target=owner.store.path(fact['manifest_ref']['path'])
            elif kind in {'stage','missing_stage'}:target=run.path/next(r['staged_path'] for r in manifest['files'] if r['path']=='state.json')
            elif kind=='nested_parent':target=run.path/'attempts/attempt-1/baseline-files/nested'
            else:target=run.path/paths[kind]
            retained=target.with_name(target.name+'.retained-original');old_identity=protocol._directory(target) if target.is_dir() else protocol._file_identity(target)
            target.rename(retained)
            if kind not in {'missing_stage','missing_manifest'}:
                if retained.is_dir():shutil.copytree(retained,target)
                else:target.write_bytes(retained.read_bytes())
                new_identity=protocol._directory(target) if target.is_dir() else protocol._file_identity(target)
                self.assertNotEqual(new_identity,old_identity)
            before={str(p.relative_to(run.path)):p.read_bytes() for p in run.path.rglob('*') if p.is_file() and not p.name.startswith('.state.lock')}
            with patch.object(work_contract,'legacy_contract',shared):result=Supervisor(owner.path,adapter=self.adapter).resume()
            with self.subTest(kind=kind):
                self.assertEqual(result['status'],'UNKNOWN_OUTCOME',result)
                self.assertEqual(self.calls(),b'')
                self.assertFalse(any(r['record_type']=='invocation_intent' for r in owner.receipts().records()))
                self.assertEqual({str(p.relative_to(run.path)):p.read_bytes() for p in run.path.rglob('*') if p.is_file() and not p.name.startswith('.state.lock')},before)
                persisted=owner.store.recover(read_only=True)['state']
                self.assertEqual(persisted['attempts_used'],0);self.assertEqual(persisted['deadline'],deadline)

    @unittest.skipUnless(os.name=='posix','Linux real durable staged publication hard exits; Windows separate')
    def test_exact_physical_stages_recover_manifest_partial_and_state_publication(self):
        from agent_harness import work_contract
        legacy=work_contract.legacy_contract
        def shared(*args,**kwargs):
            value=legacy(*args,**kwargs)
            value['scope']['shared_files']=[dict(path='nested/.supervisor-root-owner.json',format='json-records',allowed_records=['allowed'])]
            return value
        for index,phase in enumerate(('files_manifest','published_file','core_state','shared_core_state')):
            if index:self.tearDown();self.setUp()
            if phase.startswith('shared_'):
                (self.source/'nested').mkdir();(self.source/'nested/.supervisor-root-owner.json').write_text('[{"id":"allowed","value":0}]\n')
            owner=self.fixture(attempts=1,memory=True);deadline=owner.state['deadline']
            self.hard_exit(owner,phase)
            intent,material,committed=protocol._materials(owner)[-1];self.assertFalse(committed)
            fact=next(r for r in protocol.events(owner) if r.get('kind')=='root_core_preparation_files')
            manifest=protocol._blob_value(owner,fact['manifest_ref']);run=Run(material['binding']['core_path'])
            identities={r['path']:r['identity'] for r in manifest['files']}
            with patch.object(work_contract,'legacy_contract',shared) if phase.startswith('shared_') else nullcontext():
                result=Supervisor(owner.path,adapter=self.adapter).resume()
            with self.subTest(phase=phase):
                self.assertEqual(result['status'],'ACCEPTED',result)
                # state is legitimately replaced by later freeze/check commits;
                # immutable preparation outputs retain their exact published inode.
                for relative,identity in identities.items():
                    if relative!='state.json':self.assertEqual(protocol._file_identity(run.path/relative),identity)
                self.assertEqual(self.calls(),b'exec\n')
                fresh=Supervisor(owner.path,adapter=self.adapter);fresh.load()
                self.assertEqual(fresh.state['attempts_used'],1);self.assertEqual(fresh.state['deadline'],deadline)
                self.assertEqual(len(run.state()['attempts']),1);self.assertEqual(len(protocol.reservations(fresh)),1)

    @unittest.skipUnless(os.name=='posix','Linux exact mutable-state publication identities; Windows separate')
    def test_repair_state_alias_recovers_only_owned_alias_and_exact_prior_state(self):
        for index,kind in enumerate(('healthy','prior_state_replaced','alias_replaced')):
            if index:self.tearDown();self.setUp()
            owner=self.fixture(attempts=2,memory=True,mode='repair');deadline=owner.state['deadline']
            self.hard_exit(owner,'repair_state_alias')
            self.assertEqual(self.calls(),b'exec\n');counter=owner.state['attempts_used']
            intent,material,committed=protocol._materials(owner)[-1];self.assertFalse(committed)
            self.assertIsNotNone(material['prior_state'])
            run=Run(material['binding']['core_path']);alias=next(run.path.glob('.root-publish-*'))
            row=next(row for row in protocol._blob_value(owner,next(r['manifest_ref'] for r in protocol.events(owner)
                if r.get('kind')=='root_core_preparation_files' and r['operation_id']==material['operation_id']))['files'] if row['path']=='state.json')
            self.assertEqual(protocol._file_identity(alias),row['identity'])
            self.assertEqual(protocol._file_identity(run.state_path),row['prior_identity'])
            if kind!='healthy':
                target=run.state_path if kind=='prior_state_replaced' else alias
                raw=target.read_bytes();retained=target.with_name(target.name+'.retained-original');target.rename(retained);target.write_bytes(raw)
                identity=protocol._file_identity(target)
            result=Supervisor(owner.path,adapter=self.adapter).resume()
            with self.subTest(kind=kind):
                if kind=='healthy':
                    self.assertEqual(result['status'],'ACCEPTED',result)
                    self.assertEqual(self.calls(),b'exec\nresume\n')
                else:
                    self.assertEqual(result['status'],'UNKNOWN_OUTCOME',result)
                    self.assertEqual(target.read_bytes(),raw);self.assertEqual(protocol._file_identity(target),identity)
                    self.assertEqual(retained.read_bytes(),raw);self.assertEqual(self.calls(),b'exec\n')
                    self.assertEqual(len([r for r in owner.receipts().records() if r['record_type']=='invocation_intent']),1)
                persisted=owner.store.recover(read_only=True)['state']
                self.assertEqual(persisted['attempts_used'],counter+1 if kind=='healthy' else counter)
                self.assertEqual(persisted['deadline'],deadline)

    def test_physical_publication_capability_and_authority_failures_are_typed_before_model(self):
        for index,(error,target,code) in enumerate(((OSError(errno.ENOTSUP,'controlled capability refusal'),
                'BLOCKED_ENVIRONMENT','ENVIRONMENT'),(PermissionError('controlled authority refusal'),'BLOCKED_APPROVAL','APPROVAL'))):
            if index:self.tearDown();self.setUp()
            owner=self.fixture(attempts=1);deadline=owner.state['deadline']
            original_link=protocol.os.link
            def refused(source,target,**options):
                if str(source).endswith('.stage'):raise error
                return original_link(source,target,**options)
            with patch.object(protocol.os,'link',side_effect=refused):result=owner.start()
            self.assertEqual(result['status'],target,result)
            self.assertEqual(protocol.active_failure(owner)['reason_code'],code)
            self.assertEqual(self.calls(),b'');self.assertEqual(owner.state['attempts_used'],0)
            self.assertEqual(owner.state['deadline'],deadline)
            self.assertFalse(any(r['record_type']=='invocation_intent' for r in owner.receipts().records()))

    def test_generic_slot_preparation_error_preserves_fresh_origin_and_original_cause(self):
        owner=self.fixture();message='controlled root slot OSError; NO_PROGRESS is only text'
        with owner.store.lease('fixture generic slot preparation failure'):
            self.committed(owner);owner._root_execution_origin='PREFLIGHT'
            counter=owner.state['attempts_used'];deadline=owner.state['deadline']
            with patch.object(GlobalSlots,'acquire',side_effect=OSError(message)):result=owner._loop()
            self.assertEqual(result['status'],'UNKNOWN_OUTCOME',result)
            row=protocol.active_failure(owner)
            self.assertEqual((row['origin_state'],row['reason_code']),('REPAIRING','INTERNAL'))
            self.assertIn(message,[cause['detail'] for cause in protocol._check_diagnostic(owner,row)['causes']])
            self.assertEqual(self.calls(),b'');self.assertEqual(owner.state['attempts_used'],counter)
            self.assertEqual(owner.state['deadline'],deadline)

    def test_real_busy_slots_and_caught_workspace_refusal_resume_exact_prepared_repair(self):
        for index,kind in enumerate(('busy_slots','workspace_error')):
            if index:self.tearDown();self.setUp()
            owner=self.fixture(attempts=2,memory=True,mode='repair',
                contract_edit=lambda c:c['budgets'].update(turn_timeout_seconds=2))
            deadline=owner.state['deadline'];prepare=owner._new_core_attempt;workspace=Run.workspace_lock
            foreign=[];before={}
            with ExitStack() as stack:
                def prepared(reason):
                    value=prepare(reason)
                    if kind=='busy_slots':
                        for n in range(5):foreign.append(stack.enter_context(GlobalSlots(owner.state_dir).acquire('independent-'+str(n),'foreign-task','owner',timeout=1)))
                        before.update({lease.name:lease.store.path(lease.name).read_bytes() for lease in foreign})
                    return value
                def entered(run,*args,**options):
                    if kind=='workspace_error' and owner._root_execution_origin=='REPAIRING':
                        raise OSError('controlled workspace acquisition failure')
                    return workspace(run,*args,**options)
                with patch.object(owner,'_new_core_attempt',side_effect=prepared),patch.object(Run,'workspace_lock',entered):result=owner.start()
                self.assertEqual(result['status'],'BLOCKED_ENVIRONMENT' if kind=='busy_slots' else 'UNKNOWN_OUTCOME',result)
                row=protocol.active_failure(owner)
                self.assertEqual((row['origin_state'],row['reason_code']),('REPAIRING','ENVIRONMENT' if kind=='busy_slots' else 'INTERNAL'))
                self.assertEqual(self.calls(),b'exec\n');self.assertEqual(owner.state['attempts_used'],1)
                self.assertEqual((owner.state['metrics']['input_tokens'],owner.state['metrics']['output_tokens']),(10,3))
                self.assertEqual(owner.state['deadline'],deadline)
                run=Run(owner.state['core_run']);self.assertEqual(len(run.state()['attempts']),2)
                self.assertEqual({p.name for p in (run.path/'attempts').iterdir()},{'attempt-1','attempt-2'})
                preparation=protocol._materials(owner)[-1][1]['operation_id'];origin=file_hash(run.path/'origin-baseline.json')
                self.assertFalse(any(r['unresolved'] for r in GlobalSlots(owner.state_dir).inventory(owner.state['run_id'])))
                for lease in foreign:
                    self.assertEqual(lease.store.path(lease.name).read_bytes(),before[lease.name]);lease.complete()
            resumed=Supervisor(owner.path,state_dir=owner.state_dir,adapter=self.adapter);result=resumed.resume()
            self.assertEqual(result['status'],'ACCEPTED',result)
            self.assertEqual(self.calls(),b'exec\nresume\n');self.assertEqual(resumed.state['attempts_used'],2)
            self.assertEqual((resumed.state['metrics']['input_tokens'],resumed.state['metrics']['output_tokens']),(20,6))
            self.assertEqual(resumed.state['deadline'],deadline);self.assertEqual(len(run.state()['attempts']),2)
            self.assertEqual(file_hash(run.path/'origin-baseline.json'),origin)
            self.assertEqual(protocol._materials(resumed)[-1][1]['operation_id'],preparation)
            self.assertEqual(len(protocol.reservations(resumed)),2)

    @unittest.skipUnless(os.name=='posix','Linux actual unbound-slot hard exit; Windows separate')
    def test_unbound_workspace_hard_exit_and_contradictory_intents_never_release_slot(self):
        for index,kind in enumerate(('hard_exit','current_uuid','foreign_uuid_same_lease','foreign_uuid_same_attempt')):
            if index:self.tearDown();self.setUp()
            owner=self.fixture(attempts=1);deadline=owner.state['deadline']
            if kind=='hard_exit':self.hard_exit(owner,'unbound_workspace')
            else:
                def contradicted(run):
                    row=next(r for r in GlobalSlots(owner.state_dir).inventory(owner.state['run_id']) if r['status']=='reserved')
                    owner.receipts().intent('@root' if kind=='foreign_uuid_same_attempt' else 'foreign-node',
                        'attempt-1' if kind=='foreign_uuid_same_attempt' else 'foreign-attempt',checkout=owner.state['cwd'],
                        owner=row['lease_id'] if kind=='foreign_uuid_same_lease' else 'foreign-lease',
                        invocation_id=owner._pending_invocation_id if kind=='current_uuid' else 'f'*32)
                    raise OSError('controlled ambiguous workspace acquisition failure')
                with patch.object(Run,'workspace_lock',contradicted):result=owner.start()
                self.assertEqual(result['status'],'UNKNOWN_OUTCOME',result)
            registry=GlobalSlots(owner.state_dir);rows=registry.inventory(owner.state['run_id'])
            self.assertTrue(any(row['unresolved'] for row in rows))
            before={p.name:p.read_bytes() for p in registry.directory.glob('slot-*.yaml')}
            self.assertEqual(self.calls(),b'');self.assertEqual(owner.state['attempts_used'],0)
            with self.assertRaises(HarnessError):Supervisor(owner.path,state_dir=owner.state_dir,adapter=self.adapter).resume()
            self.assertEqual({p.name:p.read_bytes() for p in registry.directory.glob('slot-*.yaml')},before)
            self.assertEqual(self.calls(),b'')
            self.assertEqual(owner.store.recover(read_only=True)['state']['deadline'],deadline)

    def test_cancel_at_actual_slot_wait_retains_cancellation_without_reservation(self):
        owner=self.fixture(attempts=1);deadline=owner.state['deadline'];acquire=GlobalSlots.acquire
        def cancelled(registry,*args,**options):
            owner.stop('controlled cancellation at slot admission')
            return acquire(registry,*args,**options)
        with patch.object(GlobalSlots,'acquire',cancelled):result=owner.start()
        self.assertEqual(result['status'],'CANCELLED',result)
        self.assertEqual(protocol.active_failure(owner)['reason_code'],'CANCELLED')
        self.assertEqual(self.calls(),b'');self.assertEqual(owner.state['attempts_used'],0)
        self.assertEqual(owner.state['deadline'],deadline)
        self.assertFalse(any(r['record_type']=='invocation_intent' for r in owner.receipts().records()))

    def test_root_preflight_unknown_invocation_wins_over_quality_refusal_without_release(self):
        import agent_harness.supervisor as module
        import test_plan_quality_boundaries as boundaries
        fixture=boundaries.PlanQualityBoundaryTests();fixture.setUp()
        try:
            owner,fake=fixture.fixture();preflight=module.preflight;observed={}
            def changed(adapter,prompt,**options):
                result=preflight(adapter,prompt,**options)
                with GlobalSlots(owner.state_dir).acquire(owner.state['run_id'],owner.state['task_id'],'worker') as unknown:
                    owner.receipts().intent('left','late-attempt',checkout=owner.state['cwd'],owner=unknown.value['lease_id'])
                    observed.update(path=unknown.store.path(unknown.name),raw=unknown.store.path(unknown.name).read_bytes(),calls=fake.with_suffix('.calls').read_bytes())
                fixture.drift(owner,'assessment')
                return result
            with patch.object(module,'preflight',changed):result=owner.start()
            self.assertEqual(result['status'],'UNKNOWN_OUTCOME',result)
            self.assertEqual(observed['path'].read_bytes(),observed['raw'])
            self.assertEqual(fake.with_suffix('.calls').read_bytes(),observed['calls'])
            self.assertEqual(owner.state['attempts_used'],0)
            self.assertFalse(any(r['record_type']=='invocation_intent' and r['node_id']=='@integration' for r in owner.receipts().records()))
            self.assertTrue(any(r['unresolved'] for r in GlobalSlots(owner.state_dir).inventory(owner.state['run_id'])))
            with self.assertRaises(HarnessError):Supervisor(owner.path,adapter=owner.adapter).resume()
            self.assertEqual(observed['path'].read_bytes(),observed['raw'])
        finally:fixture.doCleanups()

    def test_root_postwait_quality_refusal_keeps_original_zero_reservation_and_no_intent(self):
        import test_plan_quality_boundaries as boundaries
        fixture=boundaries.PlanQualityBoundaryTests();fixture.setUp()
        try:
            owner,fake=fixture.fixture();workspace=Run.workspace_lock;observed={}
            @contextmanager
            def waited(run,*args,**options):
                with workspace(run,*args,**options):
                    if not observed and getattr(owner,'_pending_invocation_id',None) and run.path==Path(owner.state['core_run']):
                        observed['calls']=fake.with_suffix('.calls').read_bytes()
                        fixture.drift(owner,'policy')
                    yield
            with patch.object(Run,'workspace_lock',waited):result=owner.start()
            self.assertTrue(observed)
            self.assertEqual(result['status'],'BLOCKED_AMBIGUOUS',result)
            self.assertEqual(fake.with_suffix('.calls').read_bytes(),observed['calls'])
            self.assertEqual(owner.state['attempts_used'],0)
            self.assertFalse(any(r['record_type']=='invocation_intent' and r['node_id']=='@integration' for r in owner.receipts().records()))
            self.assertTrue(all(r['status']=='released' for r in GlobalSlots(owner.state_dir).inventory(owner.state['run_id'])))
        finally:fixture.doCleanups()

    def test_large_core_snapshot_uses_verified_external_blobs_not_journal_rows(self):
        owner=self.fixture()
        baseline=protocol.binding(owner,'initial')[1]
        baseline['files']={('controlled-inventory-'+str(n).zfill(6)+'x'*70+'.py'):
            dict(kind='file',sha256=hashlib.sha256(b'').hexdigest(),size=0,executable=False) for n in range(16000)}
        baseline['digest']=digest({k:v for k,v in baseline.items() if k!='digest'})
        self.assertGreater(len(canonical(baseline)),2*1024*1024)
        with owner.store.lease('controlled valid large inventory fixture'),patch.object(protocol,'snapshot',return_value=baseline):
            owner.load();owner.state['baseline_digest']=baseline['digest'];owner._save('controlled original large inventory')
            run=owner._core();material=protocol._materials(owner)[-1][1]
            self.assertEqual(material['baseline'],baseline)
            self.assertEqual(run.state()['attempts'],['attempt-1'])
            self.assertEqual(owner.state['attempts_used'],0)
            self.assertLess(max(map(len,owner.store.path('journal/events.jsonl').read_bytes().splitlines())),2*1024*1024)
            ref=next(r for r in protocol.events(owner) if r.get('kind')=='root_core_preparation_intent')['material_ref']
            wire=json.loads(owner.store.path(ref['path']).read_text())
            blob=owner.store.path(wire['baseline_ref']['path'])
            self.assertGreater(blob.stat().st_size,2*1024*1024)
            self.assertNotIn('baseline',wire)
            blob.write_bytes(blob.read_bytes()+b' ')
            with self.assertRaises(PreparationFailure):protocol._read(owner,ref)
        self.assertEqual(self.calls(),b'')

    def test_exact_slot_absence_proof_rejects_forged_changed_active_and_corrupt_sources(self):
        cases=('wrong_run','wrong_invocation','existing_intent','changed_lease','active_child','same_bytes_replacement','torn_journal')
        for index,kind in enumerate(cases):
            if index:self.tearDown();self.setUp()
            owner=self.fixture();reservation=self.interrupted_reservation(owner)
            bound=protocol.reservation_bindings(owner,reservation)[-1]
            registry=GlobalSlots(owner.state_dir);path=registry.store.path(bound['slot_name'])
            with owner.store.lease('fixture strict unused-slot proof'):
                supplied=deepcopy(reservation)
                if kind=='wrong_run':supplied['binding']['run_id']='other-run'
                elif kind=='wrong_invocation':supplied['invocation_id']='f'*32
                elif kind=='existing_intent':owner.receipts().intent('@root','attempt-1',checkout=owner.state['cwd'],
                    owner=bound['lease']['lease_id'],invocation_id=reservation['invocation_id'])
                elif kind in {'changed_lease','active_child'}:
                    row=registry.store.read(bound['slot_name'])
                    if kind=='changed_lease':row['lease_id']='f'*32
                    else:
                        from agent_harness.core import process_identity
                        row.update(status='active',child_identity=process_identity(os.getpid()),tree_cleanup_required=True)
                    registry.store.write(bound['slot_name'],row)
                elif kind=='same_bytes_replacement':
                    raw=path.read_bytes();path.rename(path.with_suffix('.retained'));path.write_bytes(raw)
                elif kind=='torn_journal':
                    journal=owner.store.path('journal/events.jsonl');journal.write_bytes(journal.read_bytes()[:-1])
                before=path.read_bytes();counter=owner.state['attempts_used'];deadline=owner.state['deadline']
                with self.subTest(kind=kind),self.assertRaises((HarnessError,ValueError)):
                    registry.reconcile_root_reservation(protocol.RootReservationProof(owner,supplied,bound))
                self.assertEqual(path.read_bytes(),before)
                self.assertEqual(owner.state['attempts_used'],counter);self.assertEqual(owner.state['deadline'],deadline)
                self.assertEqual(self.calls(),b'')

    def test_exact_unused_slot_does_not_block_an_independent_free_slot(self):
        owner=self.fixture();reservation=self.interrupted_reservation(owner)
        bound=protocol.reservation_bindings(owner,reservation)[-1]
        path=GlobalSlots(owner.state_dir).store.path(bound['slot_name']);before=path.read_bytes()
        with GlobalSlots(owner.state_dir).acquire('other-run','other-task','owner',timeout=1) as independent:
            self.assertNotEqual(independent.name,bound['slot_name'])
            self.assertEqual(path.read_bytes(),before)
            independent.complete()
        result=Supervisor(owner.path,adapter=self.adapter).resume()
        self.assertEqual(result['status'],'ACCEPTED',result)
        self.assertEqual(self.calls(),b'exec\n')

    def test_executable_missing_and_required_context_missing_have_distinct_typed_causes(self):
        for index,kind in enumerate(('executable','context')):
            if index:self.tearDown();self.setUp()
            owner=self.fixture()
            if kind=='executable':
                with patch.object(owner.adapter,'preflight',side_effect=FileNotFoundError('ambiguous approval deadline missing input text')):
                    result=owner.start()
                expected='ENVIRONMENT'
            else:
                from agent_harness.task_packets import compile_packet
                packet=compile_packet(owner,'Required immutable context',{'data':'value '*6000})
                manifest=json.loads(packet.rsplit('\n',1)[1]);Path(manifest['parts'][0]['path']).unlink()
                with patch.object(owner,'_prompt',return_value=packet):result=owner.start()
                expected='INTEGRITY'
            self.assertEqual(result['status'],EXPECTED_PREPARATION_TARGETS[expected],result)
            self.assertEqual(protocol.active_failure(owner)['reason_code'],expected)
            self.assertEqual(owner.state['attempts_used'],0);self.assertEqual(self.calls(),b'')

    def test_observed_budget_deadline_usage_and_cancel_facts_remain_distinct(self):
        cases=('attempts','tokens','usage','deadline','cancel')
        for index,kind in enumerate(cases):
            if index:self.tearDown();self.setUp()
            owner=self.fixture(contract_edit=lambda c:c['budgets'].update(max_tokens=5))
            with owner.store.lease('fixture observed preparation limits'):
                owner.load();deadline=owner.state['deadline']
                if kind=='attempts':owner.state['attempts_used']=owner.state['max_attempts']
                if kind in {'tokens','usage'}:
                    intent=owner.receipts().intent('@root','legacy-observed',checkout=owner.state['cwd'],owner='fixture')
                    owner.receipts().completion(intent,dict(status='completed',process_tree_stopped=True,turn_started=True,
                        usage={'input_tokens':5,'output_tokens':0} if kind=='tokens' else None,thread_id='fixture-session',logs={}))
                if kind=='cancel':owner.stop('fixture cancellation')
                expected={'attempts':'ATTEMPT_BUDGET','tokens':'TOKEN_BUDGET','usage':'USAGE_UNVERIFIED','deadline':'DEADLINE','cancel':'CANCELLED'}[kind]
                with patch.object(owner,'remaining',return_value=0) if kind=='deadline' else patch.object(owner,'remaining',wraps=owner.remaining):
                    with self.assertRaises(PreparationFailure) as raised:protocol.admissible(owner)
                self.assertEqual(raised.exception.reason_code,expected)
                self.assertEqual(owner.state['deadline'],deadline);self.assertEqual(self.calls(),b'')
                if kind=='usage':self.assertIsNone(owner.state['metrics']['input_tokens'])

    def test_public_old_writer_refuses_native_root_protocol_before_mutation_or_model(self):
        owner=self.fixture()
        with owner.store.lease('fixture native version barrier'):
            self.committed(owner)
            with patch.object(owner,'_new_core_attempt',side_effect=HarnessError('specific causal preparation error')):
                self.assertEqual(owner._loop()['status'],'UNKNOWN_OUTCOME')
        old=self.root/'public-old';old.mkdir()
        raw=subprocess.check_output(['git','archive',PUBLIC_BASELINE_TREE,'src','schemas'])
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for item in archive.getmembers():
                target=old/item.name
                self.assertTrue(target.resolve().is_relative_to(old))
                if item.isdir():target.mkdir(parents=True,exist_ok=True)
                else:target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.extractfile(item).read())
        script=self.root/'old-writer.py';script.write_text('''import json,sys
sys.path.insert(0,sys.argv[1])
from agent_harness.supervisor import Supervisor
class NoModel:
 calls=0
 def run(self,*a,**kw):self.calls+=1;raise AssertionError('old writer dispatched')
adapter=NoModel()
try:Supervisor(sys.argv[2],state_dir=sys.argv[3],adapter=adapter).resume();error=None
except Exception as caught:error=type(caught).__name__+': '+str(caught)
print(json.dumps({'error':error,'model_calls':adapter.calls}))
assert error is not None and adapter.calls==0
''')
        inventory=lambda:{str(p.relative_to(owner.path)):p.read_bytes() for p in owner.path.rglob('*') if p.is_file() and 'locks' not in p.parts}
        before=inventory()
        first_version2=next(r['sequence'] for r in owner.store.events() if r['schema_version']==2)
        result=subprocess.run([sys.executable,str(script),str(old/'src'),str(owner.path),str(owner.state_dir)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('Corrupt journal record '+str(first_version2),json.loads(result.stdout)['error'])
        self.assertEqual(inventory(),before);self.assertEqual(self.calls(),b'')

    @unittest.skipUnless(os.name=='posix','Linux real committed repair hard exits; Windows separate')
    def test_committed_repair_hard_exits_keep_cause_generation_budget_and_exact_session(self):
        for index,phase in enumerate(('repair_before','repair_core_state','repair_core_commit','repair_reserved_counter')):
            if index:self.tearDown();self.setUp()
            owner=self.fixture(attempts=2,memory=True,mode='repair');deadline=owner.state['deadline']
            self.hard_exit(owner,phase)
            self.assertEqual(self.calls(),b'exec\n')
            history=deepcopy(owner.state['failure_history'])
            self.assertEqual(len(history),1)
            result=Supervisor(owner.path,adapter=self.adapter).resume()
            with self.subTest(phase=phase):
                self.assertEqual(result['status'],'ACCEPTED',self.adapter.failure_summary(result))
                self.assertEqual(self.calls(),b'exec\nresume\n')
                fresh=Supervisor(owner.path,adapter=self.adapter);fresh.load()
                self.assertEqual(fresh.state['attempts_used'],2);self.assertEqual(fresh.state['deadline'],deadline)
                self.assertEqual(fresh.state['failure_history'],history)
                self.assertEqual(len(fresh._core().state()['attempts']),2)
                self.assertEqual(len(protocol.reservations(fresh)),2)
                self.assertEqual(fresh.resume()['status'],'ACCEPTED')
                self.assertEqual(self.calls(),b'exec\nresume\n')

    def test_controller_verification_generation_is_not_an_extra_model_reservation(self):
        owner=self.fixture(attempts=1)
        with owner.store.lease('fixture distinct verification generation authority'):
            owner.load();run=owner._core();origin=file_hash(run.path/'origin-baseline.json')
            owner._new_core_attempt('diagnostic explanation only')
            self.assertEqual(len(run.state()['attempts']),2)
            self.assertEqual(owner.state['attempts_used'],0)
            self.assertEqual(file_hash(run.path/'origin-baseline.json'),origin)
            self.assertEqual(protocol.reservations(owner),[])
            # An exact unused preparation is reused; after model reservation
            # exhaustion another verification generation cannot be allocated.
            owner.state['attempts_used']=1
            with self.assertRaises(PreparationFailure):owner._new_core_attempt('same text is not input proof')
            self.assertEqual(len(run.state()['attempts']),2)
            self.assertEqual(self.calls(),b'')

    def test_material_and_mission_plan_source_drift_never_retarget_unused_operations(self):
        for index,kind in enumerate(('blob','contract','source')):
            if index:self.tearDown();self.setUp()
            owner=self.fixture();reservation=self.interrupted_reservation(owner)
            original=deepcopy(reservation);counter=owner.state['attempts_used'];deadline=owner.state['deadline']
            if kind=='blob':
                intent=next(r for r in protocol.events(owner) if r.get('kind')=='root_core_preparation_intent')
                wire=json.loads(owner.store.path(intent['material_ref']['path']).read_text())
                owner.store.path(wire['baseline_ref']['path']).unlink()
            elif kind=='contract':
                path=owner.store.path(owner.state.get('contract_path','contract.yaml'));wire=load(path)
                wire['task']['objective']='unauthorized changed mission';dump(path,wire)
            else:(self.source/'calc.py').write_text('VALUE = 7\n')
            slot=protocol.reservation_bindings(owner,reservation)[-1]
            registry=GlobalSlots(owner.state_dir);before=registry.store.path(slot['slot_name']).read_bytes()
            try:result=Supervisor(owner.path,adapter=self.adapter).resume()
            except (HarnessError,ValueError):result=None
            if result is not None:self.assertNotEqual(result['status'],'ACCEPTED')
            self.assertEqual(registry.store.path(slot['slot_name']).read_bytes(),before)
            self.assertEqual(self.calls(),b'')
            persisted=owner.store.recover(read_only=True)['state']
            self.assertEqual(persisted['attempts_used'],counter);self.assertEqual(persisted['deadline'],deadline)
            self.assertEqual(protocol.reservations(owner),[original])

    @unittest.skipUnless(os.name=='posix','Linux actual competing writer lock; Windows separate')
    def test_live_competing_supervisor_writer_cannot_supply_release_authority(self):
        owner=self.fixture();reservation=self.interrupted_reservation(owner)
        slot=protocol.reservation_bindings(owner,reservation)[-1]
        registry=GlobalSlots(owner.state_dir);before=registry.store.path(slot['slot_name']).read_bytes()
        ready=self.root/'writer-ready.json';script=self.root/'writer.py'
        script.write_text('''import json,os,sys,time
from pathlib import Path
from agent_harness.supervisor_store import SupervisorStore
from agent_harness.core import process_identity
with SupervisorStore(sys.argv[1]).lease('actual competing writer'):
 Path(sys.argv[2]).write_text(json.dumps(process_identity(os.getpid())))
 time.sleep(20)
''')
        process=subprocess.Popen([sys.executable,str(script),str(owner.path),str(ready)],env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            end=time.monotonic()+5
            while not ready.exists() and process.poll() is None and time.monotonic()<end:time.sleep(.01)
            self.assertTrue(ready.exists())
            identity=json.loads(ready.read_text());self.assertNotEqual(identity['created'],'unknown')
            with self.assertRaises(HarnessError):Supervisor(owner.path,adapter=self.adapter).resume()
            self.assertEqual(registry.store.path(slot['slot_name']).read_bytes(),before)
            self.assertEqual(self.calls(),b'')
        finally:
            process.terminate();process.communicate(timeout=5)

    def test_explicit_no_progress_reopening_consumes_original_remaining_reservation(self):
        owner=self.fixture(mode='no-progress',attempts=3,memory=True);deadline=owner.state['deadline']
        result=owner.start();self.assertEqual(result['status'],'NO_PROGRESS',result)
        self.assertEqual(owner.state['attempts_used'],2)
        with self.assertRaises(HarnessError):Supervisor(owner.path,adapter=self.adapter).resume()
        self.fake.write_text(self.fake.read_text().replace("mode=='repair'", "mode in ('repair','no-progress')"))
        fresh=Supervisor(owner.path,adapter=self.adapter)
        result=fresh.resume(new_attempt='New controlled hypothesis: fix the observed value assertion')
        self.assertEqual(result['status'],'ACCEPTED',self.adapter.failure_summary(result))
        self.assertEqual(fresh.state['attempts_used'],3);self.assertEqual(fresh.state['deadline'],deadline)
        self.assertEqual(self.calls(),b'exec\nresume\nresume\n')
        self.assertEqual(len(protocol.reservations(fresh)),3)
        self.assertTrue(any(r.get('kind')=='root_preparation_failure_reconciled' and r['explicit_reopen'] for r in protocol.events(fresh)))

    def test_artifact_bound_overflow_fails_before_core_or_model_side_effect(self):
        from agent_harness.native import MAX_RESPONSE_BYTES
        self.assertEqual(protocol.ARTIFACT_BYTES,MAX_RESPONSE_BYTES)
        owner=self.fixture()
        with owner.store.lease('controlled artifact capacity branch'),patch.object(protocol,'ARTIFACT_BYTES',128):
            owner.load()
            with self.assertRaises(PreparationFailure) as raised:owner._core()
            self.assertEqual(raised.exception.reason_code,'ENVIRONMENT')
            self.assertFalse(any(r.get('kind')=='root_core_preparation_intent' for r in protocol.events(owner)))
            self.assertIsNone(owner.state['core_run']);self.assertEqual(owner.state['attempts_used'],0)
            self.assertEqual(self.calls(),b'')

    @unittest.skipUnless(os.name=='posix','Linux durable authorization contradiction; Windows separate')
    def test_same_old_lease_active_after_durable_cancel_authorization_blocks_without_mutation(self):
        from agent_harness.core import process_identity
        owner=self.fixture(attempts=1)
        self.hard_exit(owner,'reserved_counter');self.hard_exit(owner,'release_authorized',resume=True)
        reservation=protocol.pending_reservation(owner);bound=protocol.reservation_bindings(owner,reservation)[-1]
        registry=GlobalSlots(owner.state_dir);row=registry.store.read(bound['slot_name'])
        self.assertEqual(row['lease_id'],bound['lease']['lease_id'])
        row.update(status='active',child_identity=process_identity(os.getpid()),tree_cleanup_required=True)
        registry.store.write(bound['slot_name'],row);before=registry.store.path(bound['slot_name']).read_bytes()
        result=Supervisor(owner.path,adapter=self.adapter).resume()
        self.assertEqual(result['status'],'UNKNOWN_OUTCOME',result)
        self.assertEqual(registry.store.path(bound['slot_name']).read_bytes(),before)
        self.assertEqual(protocol.reservation_bindings(owner,reservation),[bound])
        self.assertFalse(any(r['record_type']=='invocation_intent' for r in owner.receipts().records()))
        self.assertEqual(self.calls(),b'')
        persisted=owner.store.recover(read_only=True)['state'];self.assertEqual(persisted['attempts_used'],1)

    def test_managed_plan_and_quality_drift_block_then_exact_restoration_resumes_one_reservation(self):
        fixture=managed_fixtures.VerifiedFlowTests();fixture.setUp()
        try:
            owner,fake=fixture.fixture();reservation=self.interrupted_reservation(owner)
            calls=fake.with_suffix('.calls').read_bytes();counter=owner.state['attempts_used'];deadline=owner.state['deadline']
            materials=protocol._materials(owner)
            self.assertEqual(len(materials),2)  # origin bootstrap, then current integrated input
            origin=materials[0][1];current=materials[1][1]
            self.assertIsNone(origin['prior_state'])
            self.assertNotEqual(origin['binding']['source_snapshot_digest'],current['binding']['source_snapshot_digest'])
            self.assertEqual(current['operation_id'],reservation['preparation_id'])
            self.assertEqual(current['binding']['source_snapshot_digest'],reservation['binding']['source_snapshot_digest'])
            run=Run(owner.state['core_run']);origin_path=run.path/'origin-baseline.json';origin_bytes=origin_path.read_bytes()
            self.assertEqual(json.loads(origin_bytes),origin['baseline'])
            bound=protocol.reservation_bindings(owner,reservation)[-1];registry=GlobalSlots(owner.state_dir)
            slot=registry.store.path(bound['slot_name']);slot_bytes=slot.read_bytes()
            plan_path=owner.store.path(owner.state['plan_path']);plan_bytes=plan_path.read_bytes()
            plan=load(plan_path);plan['tasks'][0]['goal']+=' unapproved plan drift';dump(plan_path,plan)
            self.assertEqual(Supervisor(owner.path,adapter=owner.adapter).resume()['status'],'UNKNOWN_OUTCOME')
            self.assertEqual(slot.read_bytes(),slot_bytes);self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
            plan_path.write_bytes(plan_bytes)
            original_ref=owner.state['plan_quality']['assessment_ref']
            with owner.store.lease('fixture authoritative wrong quality binding'):
                owner.load();owner.state['plan_quality']['assessment_ref']='foreign-quality-assessment';owner._save('fixture quality subject drift')
            self.assertEqual(Supervisor(owner.path,adapter=owner.adapter).resume()['status'],'UNKNOWN_OUTCOME')
            self.assertEqual(slot.read_bytes(),slot_bytes);self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
            persisted=owner.store.recover(read_only=True)['state']
            self.assertEqual(persisted['attempts_used'],counter);self.assertEqual(persisted['deadline'],deadline)
            with owner.store.lease('restore exact original quality subject'):
                owner.load();owner.state['plan_quality']['assessment_ref']=original_ref;owner._save('restore exact original immutable binding')
            resumed=Supervisor(owner.path,adapter=owner.adapter);result=resumed.resume()
            self.assertEqual(result['status'],'ACCEPTED',result)
            self.assertEqual(protocol.reservations(resumed),[reservation]);self.assertEqual(resumed.state['attempts_used'],counter)
            self.assertEqual(origin_path.read_bytes(),origin_bytes)
            self.assertEqual(len(run.state()['attempts']),2)
        finally:fixture.doCleanups()

    def test_authorized_managed_mission_successor_preserves_old_reservation_without_reuse(self):
        fixture=managed_fixtures.VerifiedFlowTests();fixture.setUp()
        try:
            owner,fake=fixture.fixture();reservation=self.interrupted_reservation(owner)
            calls=fake.with_suffix('.calls').read_bytes();counter=owner.state['attempts_used'];deadline=owner.state['deadline']
            old_plan=owner.store.path(owner.state['plan_path']);contract=deepcopy(owner.contract)
            contract['mission'].update(generation=2,previous_digest=digest(owner.contract['mission']))
            contract['mission']['original_request']+=' Authorized successor retains original scope and budgets.'
            path=fixture.root/'successor.yaml';dump(path,contract)
            owner.update_mission(path,reason='explicit controlled successor')
            self.assertEqual(owner.state['attempts_used'],counter);self.assertEqual(owner.state['deadline'],deadline)
            with owner.store.lease('verify old operation cannot target successor'):
                with self.assertRaises(PreparationFailure):protocol._subject_current(owner,reservation['binding'])
            resumed=Supervisor(owner.path,adapter=owner.adapter);resumed.plan_input=str(old_plan)
            result=resumed.resume()
            self.assertNotEqual(result['status'],'ACCEPTED');self.assertEqual(fake.with_suffix('.calls').read_bytes(),calls)
            persisted=owner.store.recover(read_only=True)['state']
            self.assertEqual(persisted['attempts_used'],counter);self.assertEqual(persisted['deadline'],deadline)
            self.assertEqual(protocol.reservations(resumed),[reservation]);self.assertIsNone(protocol.pending_reservation(resumed))
            self.assertFalse(any(r['record_type']=='invocation_intent' and r['node_id']=='@integration' for r in resumed.receipts().records()))
        finally:fixture.doCleanups()
