"""Pinned, self-authored upstream-format fixtures. No official data/model runs."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from agent_harness import strict_yaml,evaluation
from agent_harness.core import HarnessError,digest
from agent_harness.benchmark_bundles import _hash
from agent_harness.benchmark_protocol import (build_prepared_profile,validate_prepared_profile,checked_document,
    PROFILE,COHORT,NETWORK,IMAGE_PREPARATION)
from agent_harness.benchmark_importers import (capture_local_git_checkout,import_upstream_source,
    materialize_upstream_plan,verify_upstream_materialization,RAW_SOURCE)
from agent_harness.benchmark_upstream_evaluator import prepare_evaluation,evaluate_upstream,parse_upstream_result
from agent_harness.benchmark_upstream_adapter import UpstreamBenchmarkAdapter,PortfolioBenchmarkAdapter
from agent_harness.codex_execution import CodexAdapter
from agent_harness.benchmark_corpora import export_own_corpus
from agent_harness.benchmark_bundles import TaskBundleBoundary
from agent_harness.benchmark_runner import BenchmarkRunner,BudgetAuthorization


class FixtureEvaluator:
    """Explicit contained protocol fixture, never an observed boundary."""
    def __init__(self,*,reward=1,resolved=True,returncode=0):
        self.calls=[];self.reward=reward;self.resolved=resolved;self.returncode=returncode
    def run(self,command,**options):
        self.calls.append((command,options))
        output=Path(options['mounts']['output'])
        if 'swebench.harness.run_evaluation'in command:
            trial=command[command.index('--run_id')+1];task=command[command.index('--instance_ids')+1]
            destination=output/'logs/evaluation'/trial/('codex-'+trial)/task/'report.json';destination.parent.mkdir(parents=True)
            destination.write_text(json.dumps({task:{'resolved':self.resolved}}))
            return {'returncode':self.returncode}
        if command==['/bin/bash','/tests/test.sh']:
            self.assert_tests=Path(options['mounts']['terminal_tests_source'],'test.sh').is_file()
            (output/'reward.txt').write_text(json.dumps(self.reward))
            return {'returncode':self.returncode}
        # Exercise actual restored test bytes with a trusted self-authored
        # Python fixture. This is not a real pytest/toolchain qualification.
        result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests'],cwd=options['cwd'],capture_output=True,timeout=options['timeout'])
        return {'returncode':result.returncode}


class ProfileFixture(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.assets=self.root/'assets';self.assets.mkdir()
        self.own=export_own_corpus(self.assets)
        self.revision='a'*40;self.eval_revision='b'*40
        self.sources={};self.raw={};self.preps={}
        def put(name,value):
            raw=value if isinstance(value,bytes)else value.encode() if isinstance(value,str)else json.dumps(value,sort_keys=True).encode()
            path=self.assets/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            return {'path':name,'sha256':_hash(raw)}
        self.put=put
        repository=self.root/'repository';repository.mkdir();(repository/'calc.py').write_text('VALUE = 0\n');(repository/'empty.py').write_bytes(b'')
        for command in [['init'],['add','.'],['-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture']]:
            subprocess.run(['git',*command],cwd=repository,check=True,capture_output=True)
        self.repo=repository
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repository,text=True).strip()
        files,checkout=capture_local_git_checkout(repository,revision,self.assets,prefix='swe/base')
        swe={'instance_id':'fixture__repo-1','repo':'fixture/repo','base_commit':revision,'problem_statement':'Set VALUE to 42.','patch':'GOLD_NOT_FOR_SOLVE','test_patch':'PRIVATE_TEST_PATCH','version':'fixture-v1','FAIL_TO_PASS':'["test_value"]','PASS_TO_PASS':'[]'}
        self.raw['swe-bench-verified']=swe
        self.sources['swe-bench-verified']=self.put('swe/source.json',{'schema_version':1,'record_type':'benchmark_upstream_source','suite':'swe-bench-verified','task_id':swe['instance_id'],'dataset_revision':self.revision,'evaluator_revision':self.eval_revision,'metadata':put('swe/record.json',swe),'files':files,'checkout':checkout,'language':None})
        terminal='schema_version="1.1"\n[task]\nname="terminal-bench/fixture-terminal"\n[agent]\ntimeout_sec=60\n[verifier]\ntimeout_sec=60\n[environment]\ndocker_image="fixture/terminal:prepared"\ncpus=1\nmemory_mb=256\nstorage_mb=256\n'
        terminal_files={'instruction.md':'Set seed.txt to 42.','environment/Dockerfile':'FROM fixture-only\n','tests/test.sh':'#!/bin/bash\nprintf 1 > /logs/verifier/reward.txt\n','solution/solve.sh':'PRIVATE_ORACLE_ONLY'}
        self.sources['terminal-bench-2.1']=put('terminal/source.json',{'schema_version':1,'record_type':'benchmark_upstream_source','suite':'terminal-bench-2.1','task_id':'fixture-terminal','dataset_revision':self.revision,'evaluator_revision':self.eval_revision,'metadata':put('terminal/task.toml',terminal),'files':[{'path':name,'asset':put('terminal/'+name,raw)} for name,raw in terminal_files.items()],'checkout':None,'language':None})
        config={'files':{'solution':['calc.py'],'test':['tests/test_calc.py'],'example':['example.py']},'authors':['Fixture']}
        aider_files={'calc.py':'VALUE = 0\n','tests/test_calc.py':'import unittest\nfrom calc import VALUE\nclass ValueTests(unittest.TestCase):\n def test_value(self): self.assertEqual(VALUE,42)\n','example.py':'PRIVATE_EXAMPLE_SOLUTION','README.md':'Public build context','.docs/instructions.md':'Set VALUE to 42.','.meta/config.json':json.dumps(config)}
        self.sources['aider-polyglot']=put('aider/source.json',{'schema_version':1,'record_type':'benchmark_upstream_source','suite':'aider-polyglot','task_id':'python-fixture','dataset_revision':self.revision,'evaluator_revision':self.eval_revision,'metadata':put('aider/config.json',config),'files':[{'path':name,'asset':put('aider/'+name,raw)} for name,raw in aider_files.items()],'checkout':None,'language':'python'})
        tool=put('tool.bin','synthetic tool identity; never a production executable')
        toolchain=put('upstream-toolchain.json',{'schema_version':1,'record_type':'benchmark_toolchain','revision':self.eval_revision,'python':sys.executable,'tools':[{'name':'fixture','version':'fixture-v1','artifact':tool}]})
        suites=[];images=[]
        protocols=('swebench-jsonl-v1','harbor-task-v1','aider-exercism-v1')
        for suite,protocol in zip(evaluation.SUITES[:3],protocols):
            source=checked_document(self.sources[suite],self.assets,RAW_SOURCE)
            cohort=put(suite+'/cohort.json',{'schema_version':1,'record_type':'benchmark_cohort','suite':suite,'dataset_revision':self.revision,'selection':'synthetic_fixture','split':'fixture','declared_total':1,'tasks':[{'id':source['task_id'],'source':self.sources[suite]}]})
            evaluator=put(suite+'/evaluator.json',{'schema_version':1,'record_type':'benchmark_upstream_evaluator','suite':suite,'revision':self.eval_revision,'protocol':protocol,'entrypoint':put(suite+'/entry.py','fixture upstream evaluator code pin'),'helpers':[]})
            suites.append({'suite':suite,'cohort_ref':cohort,'evaluator_ref':evaluator})
            workspace=put(suite+'/workspace.json',{'schema_version':1,'record_type':'benchmark_workspace_inventory','files':[{'path':'seed.txt','asset':put('terminal/seed.txt','0')}] if suite=='terminal-bench-2.1' else []})
            prep=put(suite+'/image-preparation.json',{'schema_version':1,'record_type':'benchmark_image_preparation','image_digest':'sha256:'+'1'*64,'platform':'linux/amd64','toolchain_ref':toolchain,'workspace_inventory_ref':workspace})
            self.preps[suite]=prep
            images.append({'suite':suite,'image':'fixture/terminal:prepared' if suite=='terminal-bench-2.1' else 'fixture/prepared:local','digest':'sha256:'+'1'*64,'platform':'linux/amd64','preparation_ref':prep})
        boundary=put('boundary.json',{'schema_version':1,'record_type':'benchmark_boundary_profile','backend_id':'synthetic-fixture-only','backend_build_digest':'2'*64,'filesystem':'deny_peer_and_controller','network':'deny_agent_egress','evaluator':'separate_from_solve'})
        reset=put('reset.json',{'schema_version':1,'record_type':'benchmark_reset_profile','home':'exclusive_empty','history':'none','cross_arm_cache':'deny','harness_installation':'arm_specific_pinned_inventory'})
        route=put('route.json',{'schema_version':1,'record_type':'benchmark_model_route','provider':'fixture-route','channel':'separate_controlled_transport','backend_identity_ref':tool})
        self.profile_ref=build_prepared_profile(self.assets,profile_id='pinned-fixture-v1',
            resources={'schema_version':1,'record_type':'benchmark_resources','cpus':1,'memory_mb':512,'storage_mb':512,'pids':64,'max_trial_wall_seconds':120,'max_concurrent_trials':1},
            images={'schema_version':1,'record_type':'benchmark_images','images':images},
            network={'schema_version':1,'record_type':'benchmark_network_policy','agent_egress':'deny','web_search':'deny','package_fetch':'deny','model_transport':{'provider':'fixture-route','route_ref':route},'boundary_profile_ref':boundary},
            state={'schema_version':1,'record_type':'benchmark_state_policy','trial_checkout':'fresh','client_home':'per_trial_per_arm','memory_cohort':'cold','peer_state':'deny','provider_prompt_cache':'uncontrolled_observable_limitation','reset_profile_ref':reset},
            root_policy={'schema_version':1,'record_type':'benchmark_root_policy','model':'fixture-model','provider':'fixture-route','client_version':'fixture-client','reasoning_effort':'high','max_tokens':10000,'max_compaction_tokens':500000},
            child_policy={'schema_version':1,'record_type':'benchmark_child_policy','roles':[{'role':'worker','model':'fixture-model','provider':'fixture-route','reasoning_effort':'high','max_tokens':10000,'max_compaction_tokens':500000}],'observed_role_mapping_ref':None},suites=suites)

    def materialize(self,suite,name=None):
        plan=import_upstream_source(self.sources[suite],self.assets,self.profile_ref)
        root=self.root/(name or suite)
        value=materialize_upstream_plan(plan,assets_root=self.assets,trial_root=root,trial_id=digest(suite),manifest_digest='3'*64)
        receipt=root/'upstream-materialized.yaml';grade=root/'grade-input.yaml'
        strict_yaml.dump(grade,{'schema_version':1,'manifest_digest':'3'*64,'trial_id':digest(suite),'evaluator_digest':plan['evaluator']['entrypoint']['sha256'],'solve_digest':'4'*64,'cwd':value['source_root'],'solve_outcome':'completed'})
        return plan,value,receipt,grade

    def test_typed_profile_is_not_budget_or_isolation_authority(self):
        profile=validate_prepared_profile(self.profile_ref,self.assets)
        self.assertEqual(set(profile['suites']),set(evaluation.SUITES[:3]))
        self.assertFalse(profile['isolation_qualified']);self.assertFalse(profile['budget_authority'])
        raw=deepcopy(profile['profile']);raw['approved']=True
        bad=self.put('bad-profile.json',raw)
        with self.assertRaises(ValueError):validate_prepared_profile(bad,self.assets)

    def test_profile_cohort_membership_image_and_network_fail_closed(self):
        profile=validate_prepared_profile(self.profile_ref,self.assets)['profile']
        cohort=checked_document(profile['suites'][0]['cohort_ref'],self.assets,COHORT);cohort['declared_total']=2
        descriptor=deepcopy(profile);descriptor['suites'][0]['cohort_ref']=self.put('bad-cohort.json',cohort)
        with self.assertRaisesRegex(HarnessError,'COHORT_MEMBERSHIP'):validate_prepared_profile(self.put('bad-profile.json',descriptor),self.assets)
        network=checked_document(profile['network_ref'],self.assets,NETWORK);network['agent_egress']='allow'
        descriptor=deepcopy(profile);descriptor['network_ref']=self.put('bad-network.json',network)
        with self.assertRaises(ValueError):validate_prepared_profile(self.put('bad-network-profile.json',descriptor),self.assets)
        prep=checked_document(self.preps['aider-polyglot'],self.assets,IMAGE_PREPARATION);prep['image_digest']='sha256:'+'9'*64
        (self.assets/self.preps['aider-polyglot']['path']).write_text(json.dumps(prep))
        with self.assertRaisesRegex(HarnessError,'STALE_ASSET'):validate_prepared_profile(self.profile_ref,self.assets)

    def test_raw_imports_split_gold_and_materialize_all_three(self):
        for suite in evaluation.SUITES[:3]:
            plan,value,receipt,_=self.materialize(suite)
            self.assertNotEqual(value['source_root'],value['evaluator_root'])
            self.assertEqual(plan['cohort_selection'],'synthetic_fixture')
            visible=''.join(path.read_text() for path in Path(value['source_root']).rglob('*') if path.is_file() and path.relative_to(value['source_root']).parts[0]!='.git')
            self.assertNotIn('GOLD_NOT_FOR_SOLVE',visible);self.assertNotIn('PRIVATE_',visible)
            self.assertFalse((Path(value['source_root'])/'tests/test.sh').exists())
            verify_upstream_materialization(receipt,receipt_digest=_hash(receipt.read_bytes()),initial=True)

    def test_swe_local_git_capture_dirty_base_refused(self):
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.repo,text=True).strip()
        (self.repo/'calc.py').write_text('different\n')
        with self.assertRaisesRegex(HarnessError,'DIRTY_BASE_CHECKOUT'):
            capture_local_git_checkout(self.repo,revision,self.assets,prefix='dirty')

    def test_swe_prediction_protocol_gold_hidden_and_report_controls(self):
        plan,value,receipt,grade=self.materialize('swe-bench-verified')
        (Path(value['source_root'])/'calc.py').write_text('VALUE = 42\n')
        request=prepare_evaluation(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()))
        self.assertIn('swebench.harness.run_evaluation',request['command'])
        prediction=Path(request['command'][request['command'].index('--predictions_path')+1]).read_text()
        self.assertIn('+VALUE = 42',prediction);self.assertNotIn('GOLD_NOT_FOR_SOLVE',prediction)
        fake=FixtureEvaluator();execution=fake.run(request['command'],cwd=request['cwd'],mounts=request['mounts'])
        self.assertEqual(parse_upstream_result(request,execution),'accepted')
        report=Path(request['result_path']);report.write_text(json.dumps({plan['task_id']:{'resolved':1}}))
        with self.assertRaisesRegex(HarnessError,'INVALID_SWE'):parse_upstream_result(request,execution)
        with self.assertRaisesRegex(HarnessError,'EXISTS_OR_UNKNOWN'):
            prepare_evaluation(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()))

    def test_aider_actual_restored_original_tests_accept_and_reject(self):
        for solved in(True,False):
            plan,value,receipt,grade=self.materialize('aider-polyglot','aider-'+str(solved))
            (Path(value['source_root'])/'calc.py').write_text('VALUE = '+('42' if solved else '0')+'\n')
            fake=FixtureEvaluator()
            result=evaluate_upstream(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()),boundary=fake)
            self.assertEqual(result['outcome'],'accepted' if solved else 'failed')
            self.assertEqual(fake.calls[0][0],[sys.executable,'-m','pytest'])
            self.assertTrue((receipt.parent/'upstream-grade/workspace/tests/test_calc.py').is_file())

    def test_terminal_verifier_fixed_mounts_numeric_reward_and_deadline(self):
        for index,(reward,expected) in enumerate([(1,'accepted'),(0,'failed'),(True,None)]):
            _,value,receipt,grade=self.materialize('terminal-bench-2.1','terminal-'+str(index))
            fake=FixtureEvaluator(reward=reward)
            if expected is None:
                with self.assertRaisesRegex(HarnessError,'INVALID_HARBOR_REWARD'):
                    evaluate_upstream(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()),boundary=fake)
            else:
                result=evaluate_upstream(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()),boundary=fake)
                self.assertEqual(result['outcome'],expected);self.assertTrue(fake.assert_tests)
        _,_,receipt,grade=self.materialize('terminal-bench-2.1','expired')
        past=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();fake=FixtureEvaluator()
        with self.assertRaisesRegex(HarnessError,'DEADLINE_EXHAUSTED'):
            evaluate_upstream(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()),boundary=fake,deadline=past)
        self.assertEqual(fake.calls,[])

    def test_protected_inputs_source_changes_default_boundary_and_no_replay(self):
        plan,value,receipt,grade=self.materialize('aider-polyglot')
        with self.assertRaisesRegex(HarnessError,'ISOLATION_UNAVAILABLE'):
            evaluate_upstream(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()))
        self.assertFalse((receipt.parent/'upstream-grade').exists())
        (Path(value['source_root'])/'conftest.py').write_text('raise RuntimeError("inject")')
        with self.assertRaisesRegex(HarnessError,'UNDECLARED_OUTPUT'):
            verify_upstream_materialization(receipt)
        with self.assertRaisesRegex(HarnessError,'MATERIALIZATION_EXISTS_OR_UNKNOWN'):
            materialize_upstream_plan(plan,assets_root=self.assets,trial_root=receipt.parent,trial_id=digest('aider-polyglot'),manifest_digest='3'*64)

    def manifest(self):
        validated=validate_prepared_profile(self.profile_ref,self.assets)
        suites=[]
        for item in validated['profile']['suites']:
            parsed=validated['suites'][item['suite']]
            image=next(image for image in validated['images'] if image['image']['suite']==item['suite'])
            suites.append({'id':item['suite'],'revision':parsed['cohort']['dataset_revision'],'assets':item['cohort_ref'],
                'evaluator':parsed['evaluator']['entrypoint'],'adapter':item['evaluator_ref'],'toolchain':image['preparation']['toolchain_ref'],'tasks':parsed['cohort']['tasks']})
        return {'schema_version':1,'record_type':'benchmark_manifest','comparison':'package','seed':0,'repeats':1,'bootstrap_resamples':100,'confidence':.95,'minimum_token_gain':.05,'minimum_quality_delta':0,
            'model':'fixture-model','provider':'fixture-route','client_version':'fixture-client','root_policy':validated['profile']['root_policy_ref'],
            'child_policy':validated['profile']['child_policy_ref'],'isolation':self.profile_ref,'approved_budget':self.profile_ref,'suites':suites+self.own['suites']}

    def test_profile_manifest_exact_cohort_and_policy_binding(self):
        manifest=self.manifest();evaluation.validate_manifest(manifest,self.assets)
        validate_prepared_profile(self.profile_ref,self.assets,manifest)
        manifest['suites'][0]['tasks']=[]
        with self.assertRaisesRegex(HarnessError,'COHORT_MEMBERSHIP_CONFLICT'):
            validate_prepared_profile(self.profile_ref,self.assets,manifest)

    def test_cohort_alias_cannot_relabel_another_task_descriptor(self):
        profile=validate_prepared_profile(self.profile_ref,self.assets)['profile']
        cohort=checked_document(profile['suites'][0]['cohort_ref'],self.assets,COHORT)
        cohort['tasks'].append({'id':'alias-task','source':cohort['tasks'][0]['source']});cohort['declared_total']=2
        profile['suites'][0]['cohort_ref']=self.put('alias-cohort.json',cohort)
        alias_profile=self.put('alias-profile.json',profile)
        with self.assertRaisesRegex(HarnessError,'COHORT_SOURCE_SUBJECT_CONFLICT'):
            validate_prepared_profile(alias_profile,self.assets)

    def test_swe_patch_preserves_file_existence_and_executable_modes(self):
        import os
        if os.name=='nt':self.skipTest('Actual POSIX file-mode change transport')
        _,value,receipt,grade=self.materialize('swe-bench-verified')
        source=Path(value['source_root'])
        os.chmod(source/'calc.py',0o700)
        (source/'empty.py').unlink();(source/'new-empty.py').write_bytes(b'')
        (source/'run.sh').write_text('#!/bin/sh\nexit 0\n');os.chmod(source/'run.sh',0o700)
        request=prepare_evaluation(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()))
        prediction=json.loads((Path(request['output_root'])/'predictions.jsonl').read_text())
        patch_text=prediction['model_patch']
        self.assertIn('old mode 100644\nnew mode 100755',patch_text)
        self.assertIn('diff --git a/empty.py b/empty.py\ndeleted file mode 100644',patch_text)
        self.assertIn('diff --git a/new-empty.py b/new-empty.py\nnew file mode 100644',patch_text)
        self.assertIn('diff --git a/run.sh b/run.sh\nnew file mode 100755',patch_text)
        patch_path=self.root/'candidate.diff';patch_path.write_text(patch_text)
        result=subprocess.run(['git','apply','--check',str(patch_path)],cwd=self.repo,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        subprocess.run(['git','apply',str(patch_path)],cwd=self.repo,check=True,capture_output=True)
        self.assertFalse((self.repo/'empty.py').exists());self.assertEqual((self.repo/'new-empty.py').read_bytes(),b'')
        self.assertTrue(os.access(self.repo/'run.sh',os.X_OK));self.assertTrue(os.access(self.repo/'calc.py',os.X_OK))

    def test_swe_patch_rejects_unrepresentable_new_filename(self):
        import os
        if os.name=='nt':self.skipTest('Newline filenames unavailable through Windows filesystem API')
        _,value,receipt,grade=self.materialize('swe-bench-verified')
        (Path(value['source_root'])/'bad\nname.py').write_text('VALUE=42\n')
        with self.assertRaisesRegex(HarnessError,'UNSAFE_BUNDLE_PATH'):
            prepare_evaluation(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()))

    def test_windows_mode_projection_uses_git_metadata_not_synthetic_stat(self):
        # This portable control exercises the Windows projection algorithm;
        # it does not claim Windows chmod provides POSIX execution semantics.
        from types import SimpleNamespace
        from agent_harness import benchmark_upstream_evaluator as evaluator
        _,value,receipt,grade=self.materialize('swe-bench-verified')
        source=Path(value['source_root']);(source/'run.sh').write_text('#!/bin/sh\nexit 0\n')
        for command in [['add','--','run.sh'],['update-index','--chmod=+x','--','run.sh','calc.py']]:
            subprocess.run(['git',*command],cwd=source,check=True,capture_output=True)
        with patch.object(evaluator,'os',SimpleNamespace(name='nt')):
            request=prepare_evaluation(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()))
        candidate=json.loads((Path(request['output_root'])/'predictions.jsonl').read_text())['model_patch']
        self.assertIn('old mode 100644\nnew mode 100755',candidate)
        self.assertIn('diff --git a/run.sh b/run.sh\nnew file mode 100755',candidate)

    def test_terminal_protected_helper_retains_declared_mode(self):
        import os
        source=checked_document(self.sources['terminal-bench-2.1'],self.assets,RAW_SOURCE)
        source['files'].append({'path':'tests/helper.sh','mode':'100755','asset':self.put('terminal/helper.sh','#!/bin/sh\nprintf helper-ran\n')})
        source_ref=self.put('terminal/source-mode.json',source)
        profile=validate_prepared_profile(self.profile_ref,self.assets)['profile']
        row=next(row for row in profile['suites'] if row['suite']=='terminal-bench-2.1')
        cohort=checked_document(row['cohort_ref'],self.assets,COHORT);cohort['tasks'][0]['source']=source_ref
        row['cohort_ref']=self.put('terminal/mode-cohort.json',cohort)
        profile_ref=self.put('terminal/mode-profile.json',profile)
        plan=import_upstream_source(source_ref,self.assets,profile_ref)
        helper=next(item for item in plan['protected_files'] if item['path']=='task/tests/helper.sh')
        self.assertEqual(helper.get('mode'),'100755')
        value=materialize_upstream_plan(plan,assets_root=self.assets,trial_root=self.root/'protected-mode',trial_id=digest('mode'),manifest_digest='3'*64)
        if os.name=='posix':
            result=subprocess.run([str(Path(value['evaluator_root'])/'task/tests/helper.sh')],capture_output=True,timeout=3)
            self.assertEqual((result.returncode,result.stdout),(0,b'helper-ran'))

    def test_concrete_adapter_consumes_plan_arm_receipt_env_and_native_grade(self):
        class Backend(CodexAdapter):
            def with_environment(self,environment):
                self.fixture_environment=dict(environment);return self
            def run(self,prompt,**options):
                self.fixture_calls=getattr(self,'fixture_calls',0)+1
                source=Path(options['cwd'])
                if (source/'calc.py').exists():(source/'calc.py').write_text('VALUE = 42\n')
                else:(source/'seed.txt').write_text('42')
                return {'status':'completed','thread_id':'synthetic-upstream-fixture','outcome_known':True,
                    'process_tree_stopped':True,'exit_code':0,'usage':{'input_tokens':3,'output_tokens':1}}
        class Boundary(TaskBundleBoundary):
            def identity(self):
                return {'adapter_id':'explicit-contained-fixture','build_digest':'5'*64,'boundary_digest':'6'*64,'measurement_kind':'synthetic',
                    'model':'fixture-model','provider':'fixture-route','client_version':'fixture-client'}
            def estimate_trial_tokens(self,row,plan):return 4
            def codex(self,prepared,*,deadline=None):return Backend((sys.executable,))
            def measurements(self,*args,**kwargs):raise HarnessError('BENCH_MEASUREMENTS_UNAVAILABLE')
            def control_argv(self,challenge,*,deadline=None):
                return [sys.executable,'-c',"import json,sys;v=json.loads(sys.argv[1]);v['controls']={k:'synthetic_denied' for k in ('filesystem','network','evaluator','peer_state')};print(json.dumps(v))",json.dumps(challenge)]
        class ArmBuilder:
            def __init__(self):self.verified=[]
            def prepare_arm(self,row,profile,trial_root,*,manifest_digest,deadline=None,workspace=None):
                return {'schema_version':1,'trial_id':row['trial_id'],'manifest_digest':manifest_digest,'profile_digest':profile['profile_digest'],'arm':row['arm'],'status':'prepared'}
            def verify_arm(self,receipt):
                assert receipt['status']=='prepared';self.verified.append(receipt['trial_id']);return receipt
            def launch_environment(self,receipt):
                self.verify_arm(receipt);return {'CODEX_HOME':'fixture-explicit-home','PATH':'fixture-explicit-path'}
        builder=ArmBuilder();boundary=Boundary();evaluator=FixtureEvaluator()
        adapter=UpstreamBenchmarkAdapter(self.profile_ref,self.assets,boundary=boundary,arm_builder=builder,evaluator_boundary=evaluator)
        manifest=self.manifest();self.assertEqual(adapter.identity()['model'],'fixture-model')
        for suite in manifest['suites'][:3]:
            row={'suite':suite['id'],'task_id':suite['tasks'][0]['id'],'trial_id':digest(suite['id']),'repeat':0,'arm':'plain_stock'}
            self.assertEqual(adapter.estimate_trial_tokens(row,manifest,self.assets),4)
            prepared=adapter.prepare(row,manifest,self.assets,self.root/('adapter-'+suite['id']))
            backend=adapter.codex(prepared);self.assertEqual(backend.fixture_environment['CODEX_HOME'],'fixture-explicit-home')
            if suite['id']!='terminal-bench-2.1':(Path(prepared['cwd'])/'calc.py').write_text('VALUE = 42\n')
            grade=Path(prepared['contract_path']).parent/'grade.yaml'
            strict_yaml.dump(grade,{'schema_version':1,'manifest_digest':digest(manifest),'trial_id':row['trial_id'],'evaluator_digest':suite['evaluator']['sha256'],
                'solve_digest':'7'*64,'cwd':prepared['cwd'],'solve_outcome':'completed'})
            self.assertEqual(adapter.independent_grade(row,prepared,grade)['outcome'],'accepted')
            receipt=Path(prepared['contract_path']).parent/'upstream-arm-receipt.yaml';receipt.write_text('different')
            with self.assertRaisesRegex(HarnessError,'ARM_RECEIPT_CHANGED'):adapter.codex(prepared)
        self.assertGreaterEqual(len(builder.verified),9)
        portfolio=PortfolioBenchmarkAdapter(adapter)
        self.assertEqual(portfolio.identity()['adapter_id'],'prepared-portfolio-v1')
        own=manifest['suites'][-1];row={'suite':own['id'],'task_id':own['tasks'][0]['id'],'trial_id':digest('own'),'repeat':0,'arm':'harness_full'}
        prepared=portfolio.prepare(row,manifest,self.assets,self.root/'portfolio-own')
        self.assertEqual(portfolio.codex(prepared).fixture_environment['CODEX_HOME'],'fixture-explicit-home')
        self.assertEqual(portfolio.estimate_trial_tokens(row,manifest,self.assets),4)
        # Exercise the actual current runner grade callback and v2 unknown
        # metric path, retaining the entire frozen five-suite denominator.
        for seed in range(100):
            manifest['seed']=seed
            first=evaluation.schedule(manifest)[0]
            if first['suite']=='swe-bench-verified' and first['arm']=='plain_stock':break
        else:self.fail('No deterministic contained readiness seed')
        grant=BudgetAuthorization(digest(manifest),'explicit-offline-fixture',1000,
            (datetime.now(timezone.utc)+timedelta(seconds=120)).isoformat(),'synthetic')
        runner=BenchmarkRunner.create(manifest,assets_root=self.assets,run_dir=self.root/'driver',adapter=portfolio,authorization=grant)
        report=runner.advance()
        self.assertEqual(report['completed_trials'],1,report)
        self.assertEqual(report['usage']['observed_tokens'],4)
        measured=next(record['payload'] for record in runner.records() if record['kind']=='measurements')
        self.assertEqual(measured['schema_version'],2)
        self.assertTrue(all(metric['coverage']=='unavailable' and metric['value']is None for metric in measured['metrics'].values()))
        reopened=BenchmarkRunner(self.root/'driver',portfolio)
        self.assertEqual(reopened.status()['completed_trials'],1)

    def test_adapter_defaults_refuse_without_claiming_observed_qualification(self):
        adapter=UpstreamBenchmarkAdapter(self.profile_ref,self.assets)
        with self.assertRaisesRegex(HarnessError,'ISOLATION_UNAVAILABLE'):adapter.identity()
        from agent_harness.benchmark_runner import production_status
        self.assertEqual(production_status()['registered_adapters'],[])

    def test_real_arm_builder_and_transport_bind_all_five_suite_environments(self):
        from agent_harness.benchmark_environments import ArmHomeBuilder
        import os
        client=self.assets/'actual-environment-client.py'
        client.write_text(r'''
import json,os,pathlib,sys,time,tomllib
home=pathlib.Path(os.environ['CODEX_HOME'])
row={'home':str(home),'userprofile':os.environ['USERPROFILE'],'settings':tomllib.loads((home/'config.toml').read_text()),'keys':sorted(os.environ)}
with pathlib.Path(__file__).with_suffix('.trace').open('a') as stream:stream.write(json.dumps(row)+'\n')
if '--version'in sys.argv:print('fixture-client');raise SystemExit
if '--help'in sys.argv:print('exec resume SESSION_ID --json --output-schema');raise SystemExit
gate=pathlib.Path(__file__).parent/('actual-bound-'+str(os.getpid()));end=time.monotonic()+15
while not gate.exists():
 if time.monotonic()>=end:raise RuntimeError('fixture process ownership handshake timed out')
 time.sleep(.01)
gate.unlink()
for event in [{'type':'thread.started','thread_id':'fixture-real-environment'},{'type':'turn.started'},{'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}]:print(json.dumps(event),flush=True)
''')
        class Boundary(TaskBundleBoundary):
            def identity(self):
                return {'adapter_id':'actual-local-synthetic-transport','build_digest':_hash(client.read_bytes()),'boundary_digest':digest('synthetic only; not isolation'),
                    'measurement_kind':'synthetic','model':'fixture-model','provider':'fixture-route','client_version':'fixture-client'}
            def estimate_trial_tokens(self,row,plan):return 4
            def codex(self,prepared,*,deadline=None):return CodexAdapter((sys.executable,str(client)))
            def evaluator_argv(self,argv,prepared,*,deadline=None):return argv
        builder=ArmHomeBuilder(Path(__file__).parents[1],self.assets)
        upstream=UpstreamBenchmarkAdapter(self.profile_ref,self.assets,boundary=Boundary(),arm_builder=builder,evaluator_boundary=FixtureEvaluator(reward=0,resolved=False))
        portfolio=PortfolioBenchmarkAdapter(upstream);manifest=self.manifest()
        before={key:os.environ.get(key) for key in('HOME','USERPROFILE','CODEX_HOME','XDG_CONFIG_HOME')};homes=set()
        arms=('plain_stock','harness_full','plain_matched','harness_matched','harness_full')
        for suite,arm in zip(manifest['suites'],arms):
            selected=deepcopy(manifest);selected['comparison']='matched' if arm.endswith('matched')else 'package'
            row=next(row for row in evaluation.schedule(selected) if row['suite']==suite['id'] and row['task_id']==suite['tasks'][0]['id'] and row['arm']==arm)
            prepared=portfolio.prepare(row,selected,self.assets,self.root/('real-arm-'+row['trial_id']))
            backend=portfolio.codex(prepared);capabilities=backend.capabilities()
            self.assertEqual(capabilities['version'],'fixture-client')
            self.assertEqual(capabilities['configured_settings']['model'],'fixture-model')
            result=backend.run('contained environment fixture, no real model',cwd=prepared['cwd'],timeout=15,write_authorized=True,
                log_dir=Path(prepared['contract_path']).parent/'actual-client-logs',
                on_process=lambda process:(client.parent/('actual-bound-'+str(process['identity']['pid']))).write_text('bound'))
            self.assertEqual(result['status'],'completed',result)
            parent=Path(prepared['contract_path']).parent
            receipt=strict_yaml.load(parent/('upstream-arm-receipt.yaml' if suite['id']in evaluation.SUITES[:3]else 'portfolio-arm-receipt.yaml'))
            raw=parent/'actual-transport.json';strict_yaml.dump(raw,result)
            observation=builder.observe_launch(receipt,result,observation_ref={'path':str(raw),'sha256':_hash(raw.read_bytes())})
            self.assertEqual(observation['observation']['environment_binding'],'observed_explicit_transport_argument_mapping')
            self.assertFalse(observation['observation']['effective_client_configuration_observed'])
            self.assertIn('runtime_boundary_isolation_unqualified',receipt['blockers'])
            self.assertTrue(receipt['ancestry']['git_root_observed'])
            codex_home=Path(receipt['homes']['codex_home']);homes.add(str(codex_home))
            self.assertEqual((codex_home/'hooks.json').exists(),arm.startswith('harness_'))
            self.assertEqual(bool(list(Path(receipt['homes']['skills_home']).rglob('SKILL.md'))),arm.startswith('harness_'))
            # Runtime session files may be new; immutable settings remain bound.
            builder.verify_arm(receipt)
            grade=parent/'actual-grade-input.yaml'
            strict_yaml.dump(grade,{'schema_version':1,'manifest_digest':digest(selected),'trial_id':row['trial_id'],
                'evaluator_digest':suite['evaluator']['sha256'],'solve_digest':'8'*64,'cwd':prepared['cwd'],'solve_outcome':'completed'})
            self.assertEqual(portfolio.independent_grade(row,prepared,grade)['outcome'],'failed')
        self.assertEqual(len(homes),5)
        self.assertEqual(before,{key:os.environ.get(key) for key in before})
        observed=[json.loads(row) for row in client.with_suffix('.trace').read_text().splitlines()]
        self.assertEqual(len({row['home'] for row in observed}),5)
        (codex_home/'config.toml').write_text('model="tampered"\n')
        with self.assertRaises(HarnessError):portfolio.codex(prepared)

    def test_upstream_closed_grade_input_never_accepts_a_caller_verdict(self):
        _,value,receipt,grade=self.materialize('terminal-bench-2.1')
        request=strict_yaml.load(grade);request['solve_outcome']='accepted';strict_yaml.dump(grade,request)
        fake=FixtureEvaluator()
        with self.assertRaises(ValueError):
            evaluate_upstream(receipt,grade,self.assets,receipt_digest=_hash(receipt.read_bytes()),boundary=fake)
        self.assertEqual(fake.calls,[])

    def test_exported_protocol_schemas_match_runtime(self):
        from agent_harness import benchmark_protocol as p,benchmark_importers as i,benchmark_git as g
        for name,value in [('benchmark-prepared-profile',p.PROFILE),('benchmark-root-policy',p.ROOT_POLICY),('benchmark-child-policy',p.CHILD_POLICY),('benchmark-resources',p.RESOURCES),('benchmark-images',p.IMAGES),('benchmark-image-preparation',p.IMAGE_PREPARATION),('benchmark-network-policy',p.NETWORK),('benchmark-model-route',p.ROUTE),('benchmark-state-policy',p.STATE),('benchmark-boundary-profile',p.BOUNDARY_PROFILE),('benchmark-reset-profile',p.RESET_PROFILE),('benchmark-toolchain',p.TOOLCHAIN),('benchmark-workspace-inventory',p.WORKSPACE),('benchmark-cohort',p.COHORT),('benchmark-upstream-evaluator',p.EVALUATOR),('benchmark-role-policy-observations',p.ROLE_MAPPING),('benchmark-upstream-source',i.RAW_SOURCE),('benchmark-upstream-preparation',i.PLAN),('benchmark-upstream-materialized',i.MATERIALIZED),('benchmark-fresh-git',g.FRESH_GIT)]:
            self.assertEqual(json.loads((Path(__file__).parents[1]/'schemas'/(name+'.schema.json')).read_text()),value,name)

    def test_swe_executable_mode_tree_is_preserved_in_fresh_git(self):
        (self.repo/'entry.sh').write_text('#!/bin/sh\nexit 0\n')
        import os
        os.chmod(self.repo/'entry.sh',0o700)
        for command in [['add','.'],['update-index','--chmod=+x','--','entry.sh'],['-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','executable fixture']]:
            subprocess.run(['git',*command],cwd=self.repo,check=True,capture_output=True)
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.repo,text=True).strip()
        files,checkout=capture_local_git_checkout(self.repo,revision,self.assets,prefix='executable-base')
        self.assertEqual(next(item['mode'] for item in files if item['path']=='entry.sh'),'100755')
        from agent_harness.benchmark_git import initialize_fresh_git
        destination=self.root/'mode-workspace';destination.mkdir()
        for item in files:
            (destination/item['path']).write_bytes((self.assets/item['asset']['path']).read_bytes())
            os.chmod(destination/item['path'],0o700 if item['mode']=='100755' else 0o600)
        result=initialize_fresh_git(destination,expected_tree=checkout['tree'],executable_paths=['entry.sh'])
        self.assertEqual(result['initial_tree'],checkout['tree'])
        self.assertNotEqual(result['initial_head'],checkout['revision'])
