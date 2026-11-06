"""Real process death, byte-exact Git transport and validated journal heads."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import installation as install, strict_yaml
from agent_harness.benchmark_importers import capture_local_git_checkout
from agent_harness.benchmark_environments import _safe_join
from agent_harness.benchmark_upstream_evaluator import _patch
from agent_harness.core import HarnessError
from agent_harness.file_io import ParentDirectory
from agent_harness.supervisor_store import SupervisorStore, EVENT_SCHEMA
import test_install as baseline


class InstallationDeathTests(unittest.TestCase):
    setUp = baseline.InstallerTests.setUp
    make_plan = baseline.InstallerTests.make_plan
    apply = baseline.InstallerTests.apply

    def kill_install(self, boundary):
        code = r'''
import os,sys
from pathlib import Path
from agent_harness import installation as i
package,home,skills,state,overlay=map(Path,sys.argv[1:6]);boundary=sys.argv[6]
plans=i.plan(package,home,skills,state,overlay)
target=plans[0]['path']
original=i.ParentDirectory.replace
def replace(parent,name):
    if parent.path==target and boundary=='before':os._exit(73)
    if parent.path==state/'active.json' and boundary=='active_before':os._exit(73)
    result=original(parent,name)
    if parent.path==target and boundary=='after':os._exit(73)
    if parent.path==state/'active.json' and boundary=='active_after':os._exit(73)
    return result
i.ParentDirectory.replace=replace
original_atomic=i.atomic
def atomic(path,data):
    if path.name=='manifest.json' and boundary=='final' and b'"status": "written_pending_native_verification"' in data:os._exit(73)
    return original_atomic(path,data)
i.atomic=atomic
original_publish=i.publish_target
def publish(p):
    original_publish(p)
    if p['path']==target and boundary=='published':os._exit(73)
i.publish_target=publish
with i.lock(state):i.apply_plan(plans,state,package)
'''
        result=subprocess.run([sys.executable,'-c',code,*map(str,(self.package,self.home,self.skills,self.state,self.overlay)),boundary],capture_output=True,env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')})
        self.assertEqual(result.returncode,73,result.stderr.decode())
        path=next((self.state/'installs').glob('*/manifest.json'))
        return path,json.loads(path.read_bytes())

    def reconcile(self, path, manifest):
        plans=install.rollback_plan(manifest)
        ref={'manifest':str(path),'sha256':install.digest(path.read_bytes())}
        install.apply_rollback(plans,self.state,manifest,ref,'rollback',install.read(self.state/'active.json'))
        # The dead owner leaves an explicit lock; tests act as the operator
        # after observing the subprocess exit, without adding auto-unlock.
        (self.state/'install.lock').unlink()

    def test_hard_exit_before_replace_can_rollback_then_install_repeat(self):
        path,manifest=self.kill_install('before')
        self.assertEqual(manifest['files'][0]['progress'],'publishing')
        self.reconcile(path,manifest)
        self.assertEqual(self.apply()['status'],'written_pending_native_verification')
        self.assertEqual(self.apply()['status'],'unchanged')

    def test_hard_exit_after_replace_has_durable_identity_and_restores(self):
        path,manifest=self.kill_install('after')
        row=manifest['files'][0]
        self.assertEqual(row['progress'],'publishing')
        backups={Path(r['backup']):Path(r['backup']).read_bytes() for r in manifest['files'] if r['backup']}
        self.reconcile(path,manifest)
        self.assertEqual(install.read(Path(row['path'])),install.read(Path(row['backup'])) if row['backup'] else None)
        self.assertEqual(backups,{p:p.read_bytes() for p in backups})
        self.assertEqual(self.apply()['status'],'written_pending_native_verification')

    def test_hard_exit_after_publish_preserves_same_content_user_replacement(self):
        path,manifest=self.kill_install('published')
        target=Path(manifest['files'][0]['path']);raw=target.read_bytes()
        replacement=target.with_name('user-replacement');replacement.write_bytes(raw)
        replacement.replace(target)
        with self.assertRaisesRegex(install.InstallError,'user edit preserved'):
            install.rollback_plan(manifest)
        self.assertEqual(target.read_bytes(),raw)

    def test_hard_exit_changed_backup_refuses_without_target_changes(self):
        _,manifest=self.kill_install('after')
        backup=next(Path(r['backup']) for r in manifest['files'] if r['backup'])
        backup.write_bytes(b'user backup edit')
        target=Path(manifest['files'][0]['path']);raw=target.read_bytes()
        with self.assertRaisesRegex(install.InstallError,'Backup integrity'):
            install.rollback_plan(manifest)
        self.assertEqual(target.read_bytes(),raw)

    def test_hard_exit_at_final_manifest_and_active_pointer_boundaries(self):
        for index,boundary in enumerate(('final','active_before','active_after')):
            if index:baseline.InstallerTests.setUp(self)
            with self.subTest(boundary=boundary):
                path,manifest=self.kill_install(boundary)
                self.assertTrue(all(row['progress']=='applied' for row in manifest['files']))
                self.reconcile(path,manifest)
                self.assertEqual(self.apply()['status'],'written_pending_native_verification')
                self.assertEqual(self.apply()['status'],'unchanged')

    @unittest.skipIf(os.name=='nt','Actual POSIX chmod conflict')
    def test_hard_exit_preserves_user_metadata_edit(self):
        _,manifest=self.kill_install('after')
        target=Path(manifest['files'][0]['path']);os.chmod(target,0o640)
        with self.assertRaisesRegex(install.InstallError,'user edit preserved'):
            install.rollback_plan(manifest)
        self.assertEqual(target.stat().st_mode & 0o777,0o640)

    def test_user_edit_during_durable_receipt_is_checked_before_replace(self):
        plans=self.make_plan();target=plans[0]['path'];before=plans[0]['before']
        original=install.atomic;raced=False
        def concurrent_edit(path,data):
            nonlocal raced
            result=original(path,data)
            if path.name=='manifest.json' and b'"progress": "publishing"' in data and not raced:
                raced=True;target.write_bytes((before or b'')+b'\nuser concurrent edit\n')
            return result
        with patch.object(install,'atomic',side_effect=concurrent_edit),self.assertRaises(install.InstallError):
            install.apply_plan(plans,self.state,self.package)
        self.assertTrue(raced)
        self.assertEqual(target.read_bytes(),(before or b'')+b'\nuser concurrent edit\n')
        for plan in plans[1:]:self.assertEqual(install.read(plan['path']),plan['before'])


class GitByteBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.env={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull)
        self.git('init','--template=')
        self.git('config','core.autocrlf','false')
        self.git('config','core.attributesFile',os.devnull)

    def git(self,*args):
        return subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],cwd=self.repo,env=self.env,check=True,capture_output=True).stdout

    def test_portable_serialized_paths_use_only_canonical_posix_components(self):
        self.assertEqual(_safe_join(self.root,'user/.codex/agents'),self.root/'user'/'.codex'/'agents')
        for name in ('','.', './a','a//b','a/../b','a/','/a','a\\b','C:/a','//server/share'):
            with self.subTest(name=name),self.assertRaises(HarnessError):_safe_join(self.root,name)

    def test_git_clean_crlf_is_explicitly_unsupported_without_raw_normalization(self):
        (self.repo/'file').write_bytes(b'a\nb\n')
        self.git('add','.');self.git('commit','-m','baseline')
        revision=self.git('rev-parse','HEAD').decode().strip()
        self.git('config','core.autocrlf','true')
        # Refresh checkout/index metadata as a real checkout would do.
        (self.repo/'file').unlink();self.git('checkout','--','file')
        (self.repo/'file').write_bytes(b'a\r\nb\r\n')
        self.assertEqual(self.git('status','--porcelain'),b'')
        with self.assertRaisesRegex(HarnessError,'FILTERED_BYTES_UNSUPPORTED.*raw-byte checkout'):
            capture_local_git_checkout(self.repo,revision,self.root/'assets')
        self.assertEqual((self.repo/'file').read_bytes(),b'a\r\nb\r\n')

    def test_patch_lf_only_records_apply_to_exact_bytes_and_modes(self):
        variants=[b'LF\n',b'CRLF\r\n','unicode\u2028tail\n'.encode(),'unicode\u0085tail\n'.encode(),b'VT\vrest\n',b'FF\frest\n',b'no-final-LF',b'']
        initial=[];solve=[];gold=self.root/'baseline';gold.mkdir()
        for index,raw in enumerate(variants):
            name='file'+str(index)
            (self.repo/name).write_bytes(raw);(gold/name).write_bytes(raw)
            initial.append({'path':name});solve.append({'path':name,'mode':'100644'})
        self.git('add','.');self.git('commit','-m','baseline')
        head=self.git('rev-parse','HEAD').decode().strip()
        expected={}
        for index,raw in enumerate(variants):
            name='file'+str(index);expected[name]=b'prefix\n'+raw+b'new-tail'
            (self.repo/name).write_bytes(expected[name])
        if os.name!='nt':os.chmod(self.repo/'file0',0o755)
        value={'source_root':str(self.repo),'evaluator_root':str(self.root),'initial_files':initial,'plan':{'solve_files':solve},'fresh_git':{'initial_head':head}}
        candidate=_patch(value).encode('utf-8')
        native=self.git('diff','--full-index','--no-ext-diff','--no-textconv','--no-color','--src-prefix=a/','--dst-prefix=b/')
        self.assertEqual(candidate,native)
        self.git('reset','--hard','HEAD')
        patch_path=self.root/'candidate.patch';patch_path.write_bytes(candidate)
        self.git('apply','--check',str(patch_path));self.git('apply',str(patch_path))
        self.assertEqual(expected,{name:(self.repo/name).read_bytes() for name in expected})
        if os.name!='nt':self.assertTrue(os.access(self.repo/'file0',os.X_OK))


class JournalHeadTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'store';self.store=SupervisorStore(self.root)

    def test_head_cache_validates_only_new_records_with_real_validator(self):
        with patch.object(SupervisorStore,'_decode_events',wraps=SupervisorStore._decode_events) as decode, patch.object(strict_yaml,'validate',wraps=strict_yaml.validate) as validate:
            with self.store.lease():
                for index in range(100):self.store.append_event({'n':index})
                for index in range(5):self.store.commit({'n':index},'snapshot')
            self.assertEqual(decode.call_count,0)
            self.assertGreaterEqual(sum(call.args[1] == EVENT_SCHEMA for call in validate.call_args_list if len(call.args)>1),105)
            self.assertEqual(len(self.store.events()),105)
            self.assertEqual(decode.call_count,1)
        resumed=SupervisorStore(self.root)
        with patch.object(strict_yaml,'loads',wraps=strict_yaml.loads) as loads:
            resumed.append_event({'resumed':True})
            self.assertGreaterEqual(loads.call_count,105)

    def test_same_size_edit_restored_mtime_invalidates_and_fails_closed(self):
        self.store.append_event({'label':'original'})
        path=self.root/'journal/events.jsonl';before=path.stat()
        path.write_bytes(path.read_bytes().replace(b'original',b'modified'))
        os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.assertRaisesRegex(HarnessError,'hash'):
            self.store.append_event({'must':'fail'})

    def test_external_append_replacement_and_truncate_invalidate(self):
        self.store.append_event({'n':1})
        other=SupervisorStore(self.root);other.append_event({'n':2})
        self.assertEqual(self.store.append_event({'n':3})['sequence'],3)
        path=self.root/'journal/events.jsonl';raw=path.read_bytes()
        new=path.with_name('replace');new.write_bytes(raw);new.replace(path)
        self.assertEqual(self.store.append_event({'n':4})['sequence'],4)
        path.write_bytes(raw[:20])
        with self.assertRaisesRegex(HarnessError,'Torn'):
            self.store.append_event({'must':'fail'})

    def test_threads_share_one_validated_head_and_generation(self):
        with self.store.lease(),ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n:self.store.commit({'n':n},'parallel'),range(24)))
        records=SupervisorStore(self.root).events()
        self.assertEqual([r['sequence'] for r in records],list(range(1,25)))
        self.assertEqual([r['payload']['generation'] for r in records],list(range(1,25)))

    def test_replacement_between_head_check_and_open_cannot_receive_append(self):
        self.store.append_event({'n':1})
        path=self.root/'journal/events.jsonl';raw=path.read_bytes()
        original=ParentDirectory.open
        def race(parent,*args,**kwargs):
            if parent.path==path:
                replacement=path.with_name('replacement');replacement.write_bytes(raw)
                replacement.replace(path)
            return original(parent,*args,**kwargs)
        with patch.object(ParentDirectory,'open',new=race),self.assertRaisesRegex(HarnessError,'head changed while opening'):
            self.store.append_event({'must':'fail'})
        self.assertEqual(path.read_bytes(),raw)

    def test_valid_prefix_truncation_or_valid_divergent_chain_is_not_adopted(self):
        self.store.append_event({'n':1});self.store.append_event({'n':2})
        path=self.root/'journal/events.jsonl';raw=path.read_bytes()
        path.write_bytes(raw.split(b'\n',1)[0]+b'\n')
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError,'prefix changed'):self.store.append_event({'must':'fail'})
        path.write_bytes(raw)
        replacement=SupervisorStore(self.root.parent/'other');replacement.append_event({'n':'different'})
        path.write_bytes(replacement.path('journal/events.jsonl').read_bytes())
        with self.assertRaisesRegex(HarnessError,'prefix changed'):self.store.append_event({'must':'fail'})

    def test_failed_preappend_race_retains_prior_prefix_proof(self):
        self.store.append_event({'n':1});path=self.root/'journal/events.jsonl'
        other=SupervisorStore(self.root.parent/'other');other.append_event({'n':2})
        divergent=other.path('journal/events.jsonl').read_bytes()
        original=self.store._journal_stamp;calls=0
        def race(path):
            nonlocal calls
            calls+=1
            if calls==2:path.write_bytes(divergent)
            return original(path)
        with patch.object(self.store,'_journal_stamp',side_effect=race),self.assertRaisesRegex(HarnessError,'head changed before append'):
            self.store.append_event({'must':'fail'})
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError,'prefix changed'):self.store.append_event({'must':'fail'})

    def test_process_hard_exit_after_append_replays_exactly_once(self):
        code="import os,sys;from agent_harness.supervisor_store import SupervisorStore;s=SupervisorStore(sys.argv[1]);s.append_event({'kind':'durable'});os._exit(73)"
        result=subprocess.run([sys.executable,'-c',code,str(self.root)],env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')},capture_output=True)
        self.assertEqual(result.returncode,73,result.stderr)
        fresh=SupervisorStore(self.root)
        self.assertEqual(fresh.append_event({'kind':'resumed'})['sequence'],2)
        self.assertEqual(len(fresh.events()),2)


if __name__=='__main__':unittest.main()
