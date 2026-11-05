"""Explicit child environments and per-arm homes never mutate the caller profile."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

from agent_harness.codex_execution import CodexAdapter, configured_settings
from agent_harness.core import HarnessError

FAKE = r'''
import json,os,pathlib,sys,time,tomllib
home=pathlib.Path(os.environ['CODEX_HOME'])
settings=tomllib.loads((home/'config.toml').read_text(encoding='utf-8'))
row={'phase':'probe' if '--help' in sys.argv or '--version' in sys.argv else 'run',
 'tag':os.environ['FIXTURE_TAG'],'codex_home':str(home),'home':os.environ['HOME'],
 'userprofile':os.environ['USERPROFILE'],'settings':settings,'keys':sorted(os.environ)}
with pathlib.Path(os.environ['TRACE']).open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
if '--version' in sys.argv:print('fixture-'+os.environ['FIXTURE_TAG']);raise SystemExit
if '--help' in sys.argv:print('exec resume SESSION_ID --json --output-schema');raise SystemExit
end=time.monotonic()+10
while not pathlib.Path(os.environ['READY']).exists():
 if time.monotonic()>=end:raise RuntimeError('fixture transport observation missing')
 time.sleep(.01)
for value in [{'type':'thread.started','thread_id':'fixture-'+os.environ['FIXTURE_TAG']},
 {'type':'turn.started'},{'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}]:
 print(json.dumps(value),flush=True)
'''


class ExplicitEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fake = self.root / 'client.py'
        self.fake.write_text(FAKE, encoding='utf-8')

    def environment(self, tag):
        home = self.root / tag
        codex = home / '.codex'
        codex.mkdir(parents=True)
        (codex / 'config.toml').write_text('model='+json.dumps(tag)+'\nmodel_provider="fixture"\n', encoding='utf-8')
        value = {'CODEX_HOME':str(codex), 'HOME':str(home), 'USERPROFILE':str(home),
            'FIXTURE_TAG':tag, 'TRACE':str(home/'trace.jsonl'), 'READY':str(home/'ready'),
            'PATH':os.path.dirname(sys.executable)+os.pathsep+os.defpath,
            'TEMP':str(home),'TMP':str(home),'LANG':'C.UTF-8'}
        if os.name == 'nt':
            value['SystemRoot'] = os.environ['SystemRoot']
        return value

    def test_concurrent_real_children_and_all_probes_observe_their_own_home(self):
        before = {name:os.environ.get(name) for name in ('CODEX_HOME','HOME','USERPROFILE','FIXTURE_TAG')}
        environments = [self.environment(tag) for tag in ('arm-a','arm-b')]
        base = CodexAdapter((sys.executable,str(self.fake)))
        adapters = [base.with_environment(value) for value in environments]
        def run(pair):
            adapter, value = pair
            capabilities = adapter.capabilities()
            self.assertEqual(capabilities['configured_settings']['model'], value['FIXTURE_TAG'])
            self.assertEqual(capabilities['version'], 'fixture-'+value['FIXTURE_TAG'])
            result = adapter.run('synthetic environment observation', cwd=self.root, timeout=10,
                native_children=True, log_dir=self.root/(value['FIXTURE_TAG']+'-logs'),
                on_process=lambda _:Path(value['READY']).write_text('observed',encoding='ascii'))
            return result
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, zip(adapters,environments)))
        self.assertTrue(all(result['environment_digest']==digest(value) for result,value in zip(results,environments)))
        self.assertTrue(all(result['status']=='completed' and result['process_tree_stopped'] for result in results),results)
        for value in environments:
            rows = [json.loads(line) for line in Path(value['TRACE']).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(4,len(rows))
            for row in rows:
                self.assertEqual(value['CODEX_HOME'],row['codex_home'])
                self.assertEqual(value['HOME'],row['home'])
                self.assertEqual(value['USERPROFILE'],row['userprofile'])
                self.assertEqual(value['FIXTURE_TAG'],row['settings']['model'])
                self.assertNotIn('OPENAI_API_KEY',row['keys'])
        self.assertIsNone(base.environment)
        self.assertEqual(before,{name:os.environ.get(name) for name in before})

    def test_environment_copy_clone_and_config_projection_are_bound(self):
        first, second = self.environment('first'), self.environment('second')
        adapter = CodexAdapter((sys.executable,str(self.fake)),environment=first)
        clone = adapter.with_environment(second)
        first['CODEX_HOME']=second['CODEX_HOME']
        self.assertEqual('first',configured_settings(environment=adapter.environment)['model'])
        self.assertEqual('second',configured_settings(environment=clone.environment)['model'])
        self.assertEqual(adapter.executable,clone.executable)
        with self.assertRaises(TypeError):
            clone.environment['CODEX_HOME']='changed'
        self.assertIsNone(CodexAdapter((sys.executable,str(self.fake))).environment)

    def test_explicit_readonly_home_cannot_be_overridden_for_task_writes(self):
        value = self.environment('readonly')
        Path(value['CODEX_HOME'],'config.toml').write_text('sandbox_mode="read-only"\n',encoding='utf-8')
        adapter = CodexAdapter((sys.executable,str(self.fake)),environment=value)
        with self.assertRaisesRegex(HarnessError,'prohibits task writes'):
            adapter.argv('synthetic',write_authorized=True)
        self.assertFalse(Path(value['TRACE']).exists())

    def test_explicit_mapping_must_be_unambiguous_bounded_and_has_home(self):
        for value in ({}, {'CODEX_HOME':'relative'}, {'CODEX_HOME':str(self.root),'PATH':'x','path':'y'},
                      {'CODEX_HOME':str(self.root),'INVALID=KEY':'x'},
                      {'CODEX_HOME':str(self.root),'value':'bad\x00value'},
                      {'CODEX_HOME':str(self.root),'large':'x'*(1024*1024)}):
            with self.subTest(keys=list(value)),self.assertRaises(HarnessError):
                CodexAdapter((sys.executable,str(self.fake)),environment=value)

    def test_unknown_delegating_clone_refuses_before_dispatch(self):
        env=self.environment('delegate');inner=CodexAdapter((sys.executable,str(self.fake)))
        class Delegate(CodexAdapter):
            def __init__(self,inner):self.inner=inner;self.executable=inner.executable;self.environment=None
            def capabilities(self,**options):return self.inner.capabilities(**options)
            def run(self,*args,**options):return self.inner.run(*args,**options)
        with self.assertRaisesRegex(HarnessError,'ENVIRONMENT_CLONE_CONTRACT'):
            Delegate(inner).with_environment(env)
        self.assertFalse(Path(env['TRACE']).exists())

    def test_dynamic_delegation_refuses_before_any_child_dispatch(self):
        first,second=self.environment('dynamic-a'),self.environment('dynamic-b')
        class DynamicDelegate(CodexAdapter):
            def __init__(self,inner):self.inner=inner;self.executable=inner.executable;self.environment=None
            def __getattribute__(self,name):
                if name in ('run','capabilities','argv'):
                    return getattr(object.__getattribute__(self,'inner'),name)
                return super().__getattribute__(name)
        adapter=DynamicDelegate(CodexAdapter((sys.executable,str(self.fake)),environment=first))
        with self.assertRaisesRegex(HarnessError,'ENVIRONMENT_CLONE_CONTRACT'):
            clone=adapter.with_environment(second)
            # Reproduce the old leak on private fake homes only. A fixed guard
            # raises before any probe or child below can be dispatched.
            capabilities=clone.capabilities()
            raw=clone.run('synthetic dynamic delegate',cwd=self.root,timeout=10,native_children=True,
                log_dir=self.root/'dynamic-logs',on_process=lambda _:Path(first['READY']).write_text('observed',encoding='ascii'))
            self.fail(json.dumps({'advertised_environment_digest':digest(dict(clone.environment)),
                'actual_environment_digest':raw['environment_digest'],'actual_model':capabilities['configured_settings']['model'],
                'transport_status':raw['status']}))
        self.assertFalse(Path(first['TRACE']).exists())
        self.assertFalse(Path(second['TRACE']).exists())

    def test_passive_unknown_subclass_also_requires_its_own_clone_contract(self):
        class Unknown(CodexAdapter):
            pass
        with self.assertRaisesRegex(HarnessError,'ENVIRONMENT_CLONE_CONTRACT'):
            Unknown((sys.executable,str(self.fake))).with_environment(self.environment('passive'))

    def test_instance_shadow_refuses_environment_clone(self):
        for name in ('run','capabilities','argv'):
            adapter=CodexAdapter((sys.executable,str(self.fake)))
            setattr(adapter,name,lambda *args,**kwargs:None)
            with self.subTest(method=name),self.assertRaisesRegex(HarnessError,'ENVIRONMENT_CLONE_CONTRACT'):
                adapter.with_environment(self.environment(name))

    def test_explicit_delegating_clone_contract_is_usable(self):
        first,second=self.environment('contract-a'),self.environment('contract-b')
        class Delegate(CodexAdapter):
            def __init__(self,inner):self.inner=inner;self.executable=inner.executable;self.environment=None
            def capabilities(self,**options):return self.inner.capabilities(**options)
            def run(self,*args,**options):return self.inner.run(*args,**options)
            def with_environment(self,value):return type(self)(self.inner.with_environment(value))
        class InheritedContract(Delegate):
            pass
        original=InheritedContract(CodexAdapter((sys.executable,str(self.fake)),environment=first))
        clone=original.with_environment(second)
        self.assertEqual('contract-b',clone.capabilities()['configured_settings']['model'])
        self.assertEqual('contract-a',original.capabilities()['configured_settings']['model'])
        raw=clone.run('synthetic explicit clone contract',cwd=self.root,timeout=10,native_children=True,
            log_dir=self.root/'contract-logs',on_process=lambda _:Path(second['READY']).write_text('observed',encoding='ascii'))
        self.assertEqual('completed',raw['status'],raw)
        self.assertEqual(digest(second),raw['environment_digest'])


from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib
import subprocess
from agent_harness.benchmark_environments import ArmHomeBuilder, ARM_HOME_RECEIPT
from agent_harness import strict_yaml,installation
import agent_harness.benchmark_environments as module
from unittest.mock import patch
from agent_harness.core import digest
from agent_harness.benchmark_metrics import validate_measurements


class ArmHomeBuilderTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.assets=self.root/'assets';self.assets.mkdir()
        self.package=Path(__file__).resolve().parents[1]
        root={'schema_version':1,'record_type':'benchmark_root_policy','model':'fixture-model','provider':'fixture-route',
            'client_version':'fixture-client','reasoning_effort':'high','max_tokens':1000,'max_compaction_tokens':500000}
        child={'schema_version':1,'record_type':'benchmark_child_policy','roles':[{'role':'delivery_owner','model':'fixture-child',
            'provider':'fixture-route','reasoning_effort':'medium','max_tokens':100,'max_compaction_tokens':20000}],
            'observed_role_mapping_ref':None}
        documents={'root_policy':root,'child_policy':child,**{name:{'fixture':name} for name in ('resources','images','network','state')}}
        profile={'schema_version':1,'record_type':'benchmark_prepared_profile','profile_id':'fixture-policy','suites':[]}
        for name,value in documents.items():profile[name+'_ref']=self.asset(name+'.json',value)
        for suite in ('swe-bench-verified','terminal-bench-2.1','aider-polyglot'):
            profile['suites'].append({'suite':suite,'cohort_ref':profile['resources_ref'],'evaluator_ref':profile['resources_ref']})
        self.wrapped={'profile':profile,'profile_ref':self.asset('profile.json',profile),'assets_root':str(self.assets),
            'profile_digest':digest(profile),'documents':documents}
        self.builder=ArmHomeBuilder(self.package,self.assets)

    def asset(self,name,value):
        raw=json.dumps(value).encode('utf-8');(self.assets/name).write_bytes(raw)
        return {'path':name,'sha256':hashlib.sha256(raw).hexdigest()}

    def prepare(self,arm='plain_stock',name='trial'):
        trial=self.root/'run'/'trials'/name;trial.mkdir(parents=True)
        workspace=trial/'source';workspace.mkdir()
        environment={'PATH':os.path.dirname(sys.executable)+os.pathsep+os.defpath,
            'HOME':str(trial),'USERPROFILE':str(trial),'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1'}
        if os.name=='nt':environment['SystemRoot']=os.environ['SystemRoot']
        subprocess.run(['git','-c','init.templateDir=','-c','core.hooksPath='+os.devnull,'init','-q',str(workspace)],
            env=environment,check=True,capture_output=True,timeout=10)
        (workspace/'AGENTS.md').write_text('Legitimate upstream project instructions',encoding='utf-8')
        receipt=self.builder.prepare_arm({'trial_id':digest(name),'arm':arm},self.wrapped,trial,
            manifest_digest='a'*64,workspace=workspace)
        return receipt

    def test_four_arms_have_isolated_homes_pinned_policy_and_specific_payload(self):
        before={key:os.environ.get(key) for key in ('CODEX_HOME','HOME','USERPROFILE')}
        homes=[]
        for arm in ('plain_stock','plain_matched','harness_full','harness_matched'):
            with self.subTest(arm=arm):
                receipt=self.prepare(arm,arm)
                strict_yaml.validate(receipt,json.loads((self.package/'schemas/benchmark-arm-home-receipt.schema.json').read_text(encoding='utf-8')))
                self.assertEqual('prepared',receipt['status'])
                home=Path(receipt['homes']['codex_home']);homes.append(home)
                settings=tomllib.loads((home/'config.toml').read_text(encoding='utf-8'))
                self.assertEqual('fixture-model',settings['model'])
                self.assertEqual('fixture-route',settings['model_provider'])
                self.assertEqual('high',settings['model_reasoning_effort'])
                self.assertEqual(500000,settings['model_auto_compact_token_limit'])
                child=tomllib.loads((home/'agents/delivery_owner.toml').read_text(encoding='utf-8'))
                self.assertEqual('fixture-child',child['model'])
                self.assertEqual('medium',child['model_reasoning_effort'])
                self.assertEqual(arm.startswith('harness_'),(home/'hooks.json').exists())
                self.assertEqual(arm.startswith('harness_'),(home/'AGENTS.md').exists())
                self.assertEqual(arm.startswith('harness_'),bool(list(Path(receipt['homes']['skills_home']).rglob('SKILL.md'))))
                env=self.builder.launch_environment(receipt)
                self.assertEqual(str(home),env['CODEX_HOME'])
                self.assertEqual(env['HOME'],env['USERPROFILE'])
                self.assertNotIn('OPENAI_API_KEY',env)
                self.assertNotIn('PYTHONPATH',env)
                self.assertTrue(receipt['ancestry']['git_root_observed'])
                self.assertEqual('unavailable',receipt['ancestry']['qualification'])
                self.assertTrue(Path(receipt['trial_root'],'source/AGENTS.md').exists())
                self.assertIn('hard_max_tokens_mapping_unavailable',receipt['blockers'])
        self.assertEqual(4,len(set(homes)))
        self.assertEqual(before,{key:os.environ.get(key) for key in before})

    def test_reused_target_and_changed_policies_or_home_files_refuse(self):
        receipt=self.prepare()
        with self.assertRaises(FileExistsError):
            self.builder.prepare_arm({'trial_id':receipt['trial_id'],'arm':'plain_stock'},self.wrapped,
                receipt['trial_root'],manifest_digest='a'*64)
        home=Path(receipt['homes']['codex_home'])
        original=(home/'config.toml').read_bytes()
        (home/'config.toml').write_bytes(original+b'\n# user change\n')
        with self.assertRaisesRegex(HarnessError,'FILE_CHANGED'):
            self.builder.verify_arm(receipt)
        (home/'config.toml').write_bytes(original)
        (self.assets/'root_policy.json').write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(HarnessError,'STALE_REFERENCE'):
            self.builder.verify_arm(receipt)

    def test_new_hook_or_dangling_plugin_link_cannot_enter_empty_stock_home(self):
        receipt=self.prepare()
        home=Path(receipt['homes']['codex_home'])
        (home/'hooks.json').write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(HarnessError,'DEFINITION_CHANGED'):
            self.builder.launch_environment(receipt)
        (home/'hooks.json').unlink()
        try:(home/'plugins').symlink_to(home/'missing',target_is_directory=True)
        except OSError as error:self.skipTest(str(error))
        with self.assertRaisesRegex(HarnessError,'LINK_REFUSED'):
            self.builder.verify_arm(receipt)

    def test_allowed_runtime_history_does_not_change_immutable_bootstrap_inventory(self):
        receipt=self.prepare()
        home=Path(receipt['homes']['codex_home'])
        (home/'sessions').mkdir();(home/'sessions/session.jsonl').write_text('{}',encoding='utf-8')
        (Path(receipt['homes']['state_home'])/'runtime-record.json').write_text('{}',encoding='utf-8')
        self.assertEqual(receipt,self.builder.verify_arm(receipt))
        metrics=self.builder.measurements(receipt,run_root=self.root/'run')
        validate_measurements(metrics,self.root/'run',trial_id=receipt['trial_id'],manifest_digest='a'*64)
        self.assertEqual('observed',metrics['phase_seconds']['environment_prepare']['coverage'])
        for value in metrics['metrics'].values():
            self.assertIsNone(value['value']);self.assertEqual('unavailable',value['coverage'])
        with self.assertRaisesRegex(HarnessError,'METRIC_ESCAPE'):
            self.builder.measurements(receipt,run_root=self.assets)

    def test_owned_root_replacement_and_mutated_receipt_refuse(self):
        receipt=self.prepare()
        mutated=deepcopy(receipt);mutated['homes']['user']=str(self.assets)
        with self.assertRaisesRegex(HarnessError,'RECEIPT_CHANGED'):
            self.builder.verify_arm(mutated)
        skills=Path(receipt['homes']['skills_home'])
        skills.rename(skills.with_name('old-skills'));skills.mkdir()
        with self.assertRaises((HarnessError,ValueError)):
            self.builder.verify_arm(receipt)

    def test_launch_observation_binds_actual_transport_mapping_and_pid(self):
        receipt=self.prepare()
        trial=Path(receipt['trial_root']);workspace=trial/'source'
        fake=trial/'observer-client.py';ready=fake.with_suffix('.ready')
        script="""import json,pathlib,time
ready=pathlib.Path(__file__).with_suffix('.ready')
end=time.monotonic()+10
while not ready.exists():
 if time.monotonic()>=end:raise RuntimeError('transport observation timeout')
 time.sleep(.01)
for event in [{'type':'thread.started','thread_id':'fixture-launch'}, {'type':'turn.started'},
 {'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}]:print(json.dumps(event),flush=True)
"""
        fake.write_text(script,encoding='utf-8')
        adapter=CodexAdapter((sys.executable,str(fake))).with_environment(self.builder.launch_environment(receipt))
        raw=adapter.run('synthetic launch observation',cwd=workspace,timeout=10,native_children=True,
            log_dir=trial/'logs',on_process=lambda _:ready.write_text('observed',encoding='ascii'))
        self.assertEqual('completed',raw['status'],raw)
        path=trial/'raw.json';path.write_text(json.dumps(raw),encoding='utf-8')
        ref={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        observed=self.builder.observe_launch(receipt,raw,observation_ref=ref)['observation']
        strict_yaml.validate(observed,json.loads((self.package/'schemas/benchmark-arm-launch-observation.schema.json').read_text(encoding='utf-8')))
        self.assertEqual(raw['process']['identity'],observed['process']['identity'])
        self.assertEqual(raw['environment_digest'],observed['environment_digest'])
        self.assertFalse(observed['effective_client_configuration_observed'])
        bad=deepcopy(raw);bad['environment_digest']='0'*64
        path.write_text(json.dumps(bad),encoding='utf-8');ref['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HarnessError,'MAPPING_MISMATCH'):
            self.builder.observe_launch(receipt,bad,observation_ref=ref)

    def test_non_git_workspace_stays_explicitly_unqualified_and_cannot_launch(self):
        trial=self.root/'non-git';trial.mkdir();workspace=trial/'source';workspace.mkdir()
        receipt=self.builder.prepare_arm({'trial_id':'b'*64,'arm':'plain_stock'},self.wrapped,trial,
            manifest_digest='a'*64,workspace=workspace)
        self.assertIn('source_git_root_unavailable',receipt['blockers'])
        with self.assertRaisesRegex(HarnessError,'PROJECT_BOUNDARY_UNAVAILABLE'):
            self.builder.launch_environment(receipt)

    def test_ancestor_instructions_are_inventoried_without_hiding_upstream_docs(self):
        ancestor=self.root/'AGENTS.md'
        ancestor.write_text('Ambient ancestor instructions',encoding='utf-8')
        receipt=self.prepare()
        refs=receipt['ancestry']['ancestor_documents']
        self.assertIn({'path':str(ancestor),'sha256':hashlib.sha256(ancestor.read_bytes()).hexdigest()},refs)
        self.assertEqual('unavailable',receipt['ancestry']['qualification'])
        self.assertTrue(Path(receipt['trial_root'],'source/AGENTS.md').exists())
        ancestor.write_text('Changed ambient instructions',encoding='utf-8')
        with self.assertRaisesRegex(HarnessError,'ANCESTRY_CHANGED'):
            self.builder.launch_environment(receipt)

    def test_external_workspace_cannot_be_bound_into_a_private_arm(self):
        trial=self.root/'external';trial.mkdir()
        with self.assertRaisesRegex(HarnessError,'WORKSPACE_ESCAPE'):
            self.builder.prepare_arm({'trial_id':'c'*64,'arm':'plain_stock'},self.wrapped,trial,
                manifest_digest='a'*64,workspace=self.assets)
        self.assertFalse((trial/'arm-environment').exists())

    def test_deadline_failure_has_no_environment_side_effect(self):
        trial=self.root/'expired';trial.mkdir()
        deadline=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(HarnessError,'DEADLINE'):
            self.builder.prepare_arm({'trial_id':'c'*64,'arm':'plain_stock'},self.wrapped,trial,
                manifest_digest='a'*64,deadline=deadline)
        self.assertFalse((trial/'arm-environment').exists())

    def test_each_install_effect_checks_original_deadline(self):
        trial=self.root/'deadline-install';trial.mkdir()
        original_time=module.datetime;expired=False;writes=[]
        class Clock(original_time):
            @classmethod
            def now(cls,tz=None):return original_time.now(tz)+timedelta(seconds=120 if expired else 0)
        original_atomic=installation.atomic
        def atomic(path,data):
            nonlocal expired
            writes.append({'path':str(path),'after_expiry':expired})
            value=original_atomic(path,data)
            expired=True
            return value
        deadline=(original_time.now(timezone.utc)+timedelta(seconds=90)).isoformat()
        with patch.object(module,'datetime',Clock),patch.object(installation,'atomic',side_effect=atomic):
            with self.assertRaisesRegex(HarnessError,'DEADLINE'):
                self.builder.prepare_arm({'trial_id':'d'*64,'arm':'harness_full'},self.wrapped,trial,
                    manifest_digest='a'*64,deadline=deadline)
        self.assertEqual(1,len(writes),writes)

    def test_final_publish_checks_deadline_after_slow_verification(self):
        trial=self.root/'deadline-final';trial.mkdir()
        original_time=module.datetime;expired=False;late=[]
        class Clock(original_time):
            @classmethod
            def now(cls,tz=None):return original_time.now(tz)+timedelta(seconds=120 if expired else 0)
        original_verify=self.builder.verify_arm;original_atomic=installation.atomic
        def verify(receipt):
            nonlocal expired
            value=original_verify(receipt);expired=True;return value
        def atomic(path,data):
            if expired:late.append(str(path))
            return original_atomic(path,data)
        deadline=(original_time.now(timezone.utc)+timedelta(seconds=90)).isoformat()
        with patch.object(module,'datetime',Clock),patch.object(self.builder,'verify_arm',side_effect=verify),patch.object(installation,'atomic',side_effect=atomic):
            with self.assertRaisesRegex(HarnessError,'DEADLINE'):
                self.builder.prepare_arm({'trial_id':'e'*64,'arm':'plain_stock'},self.wrapped,trial,
                    manifest_digest='a'*64,deadline=deadline)
        self.assertEqual([],late)

    def test_portable_ambiguous_observation_components_refuse_before_read(self):
        receipt=self.prepare()
        raw={'environment_digest':digest(self.builder.launch_environment(receipt)),
            'process':None,'status':'unknown_outcome','outcome_known':False}
        original=module._read_ref
        for spelling in ('.. /outside.json','raw.json:stream','directory./raw.json'):
            ref={'path':spelling,'sha256':'0'*64};seen=[]
            def read(value,*args,**kwargs):
                if value['path']==str(Path(receipt['trial_root'])/spelling):seen.append(value['path'])
                return original(value,*args,**kwargs)
            with self.subTest(path=spelling),patch.object(module,'_read_ref',side_effect=read):
                with self.assertRaisesRegex(HarnessError,'OBSERVATION_ESCAPE'):
                    self.builder.observe_launch(receipt,raw,observation_ref=ref)
            self.assertEqual([],seen)

    def test_parent_observation_path_refused_before_read(self):
        receipt=self.prepare()
        trial=Path(receipt['trial_root']);outside=trial.parent/'outside-transport.json'
        raw={'environment_digest':digest(self.builder.launch_environment(receipt)),
            'process':None,'status':'unknown_outcome','outcome_known':False}
        outside.write_text(json.dumps(raw),encoding='utf-8')
        path=trial/'..'/outside.name;seen=[];original=module._read_ref
        def read(ref,*args,**kwargs):
            if ref['path']==str(path):seen.append(ref['path'])
            return original(ref,*args,**kwargs)
        ref={'path':str(path),'sha256':hashlib.sha256(outside.read_bytes()).hexdigest()}
        with patch.object(module,'_read_ref',side_effect=read):
            with self.assertRaisesRegex(HarnessError,'OBSERVATION_ESCAPE'):
                self.builder.observe_launch(receipt,raw,observation_ref=ref)
        self.assertEqual([],seen)
