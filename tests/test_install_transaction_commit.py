"""Installer commit proof is independent of native-verification/status labels."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from agent_harness import installation as install
import test_boundary_regressions as boundaries


class InstallationCommitTests(unittest.TestCase):
    setUp = boundaries.InstallationDeathTests.setUp
    make_plan = boundaries.InstallationDeathTests.make_plan
    apply = boundaries.InstallationDeathTests.apply
    kill_install = boundaries.InstallationDeathTests.kill_install
    reconcile = boundaries.InstallationDeathTests.reconcile

    def kill_phase(self, phase, created):
        code = r'''
import os,sys,json
from pathlib import Path
from agent_harness import installation as i
package,home,skills,state,overlay=map(Path,sys.argv[1:6]);phase=sys.argv[6];created=sys.argv[7]=='1'
plans=i.plan(package,home,skills,state,overlay)
target=next(p['path'] for p in plans if (p['before'] is None)==created)
def before(path):
 if path==target and phase=='target_before':os._exit(73)
 if path==state/'active.json' and phase=='active_before':os._exit(73)
def after(path):
 if path==target and phase=='target_after':os._exit(73)
 if path==state/'active.json' and phase=='active_after':os._exit(73)
replace=i.ParentDirectory.replace
def replace_at(parent,name):
 before(parent.path);out=replace(parent,name);after(parent.path);return out
i.ParentDirectory.replace=replace_at
windows=i.windows_helper
def windows_at(name,*args,**kwargs):
 if name=='_windows_replace':before(Path(args[0]))
 out=windows(name,*args,**kwargs)
 if name=='_windows_replace':after(Path(args[0]))
 return out
i.windows_helper=windows_at
atomic=i.atomic
def write_at(path,data):
 out=atomic(path,data)
 if path.name=='manifest.json' and phase=='manifest_after' and json.loads(data).get('status')=='written_pending_native_verification':os._exit(73)
 return out
i.atomic=write_at
with i.lock(state):i.apply_plan(plans,state,package)
'''
        result = subprocess.run([sys.executable, '-c', code, *map(str, (self.package,self.home,self.skills,self.state,self.overlay)), phase, '1' if created else '0'],
            capture_output=True, env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src')})
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors='replace'))
        path = next((self.state/'installs').glob('*/manifest.json'))
        manifest = json.loads(path.read_bytes())
        target = next(Path(row['path']) for row in manifest['files'] if (row['before_sha256'] is None) == created)
        return path, manifest, target

    def replace(self, path, raw):
        mode = path.stat().st_mode & 0o777
        replacement = path.with_name('external-replacement')
        replacement.write_bytes(raw)
        replacement.chmod(mode)
        replacement.replace(path)

    def test_pending_final_manifest_same_bytes_foreign_identity_is_preserved(self):
        for created in (False, True):
            if created:
                self.setUp()
            with self.subTest(created=created):
                _, manifest = self.kill_install('active_before')
                self.assertEqual(manifest['status'], 'written_pending_native_verification')
                self.assertIsNone(install.read(self.state/'active.json'))
                row = next(row for row in manifest['files'] if (row['before_sha256'] is None) == created)
                target = Path(row['path']); raw = target.read_bytes()
                self.replace(target, raw)
                foreign = install.metadata(target)
                with self.assertRaisesRegex(install.InstallError, 'conflict|recovery|required|preserved'):
                    install.rollback_plan(manifest)
                self.assertEqual(target.read_bytes(), raw)
                self.assertEqual(install.metadata(target), foreign)

    @unittest.skipIf(os.name == 'nt', 'POSIX mode observation; Windows ACL controls are separate')
    def test_pending_existing_target_security_edit_is_preserved(self):
        _, manifest = self.kill_install('active_before')
        row = next(row for row in manifest['files'] if row['before_sha256'] is not None)
        target = Path(row['path']); raw = target.read_bytes()
        target.chmod(0o640 if (target.stat().st_mode & 0o777) != 0o640 else 0o600)
        foreign = install.metadata(target)
        with self.assertRaisesRegex(install.InstallError, 'conflict|recovery|required|preserved'):
            install.rollback_plan(manifest)
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(install.metadata(target), foreign)

    def test_active_binding_before_optional_commit_receipt_allows_targeted_rollback(self):
        _, manifest = self.kill_install('active_after')
        self.assertIsNotNone(install.active_manifest(self.state))
        config = self.home/'config.toml'
        self.replace(config, config.read_bytes()+b'\n[user_after_commit]\nvalue=7\n')
        planned = install.rollback_plan(manifest)
        restored = next(row['after'] for row in planned if row['path'] == config)
        self.assertIn(b'[user_after_commit]', restored)
        self.assertIn(b'value=7', restored)
        self.assertNotIn(b'model_auto_compact_token_limit', restored)

    def test_completed_install_retains_separate_bound_commit_receipt(self):
        result = self.apply()
        path = Path(result['manifest']); original = path.read_bytes()
        receipt = json.loads((path.parent/'commit.json').read_bytes())
        self.assertEqual(receipt['schema_version'], 1)
        self.assertEqual(receipt['record_type'], 'installation_commit')
        self.assertEqual(receipt['manifest'], {'manifest': str(path), 'sha256': install.digest(original)})
        self.assertEqual(receipt['install_id'], json.loads(original)['install_id'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(json.loads(original)['schema_version'], 3)
        self.assertEqual(json.loads(original)['commit_protocol'], 'active-binding-v1')

    def test_new_manifest_epoch_refuses_unknown_protocol_and_future_version(self):
        result = self.apply(); path = Path(result['manifest']); value = json.loads(path.read_bytes())
        for field, wrong in (('schema_version', 4), ('schema_version', True), ('commit_protocol', 'unknown')):
            with self.subTest(field=field, wrong=wrong):
                changed = dict(value); changed[field] = wrong
                with self.assertRaisesRegex(install.InstallError, 'version|protocol'):
                    install.rollback_plan(changed)

    def test_legacy_unbound_prepared_does_not_gain_new_ownership_trust(self):
        with self.assertRaisesRegex(install.InstallError, 'ownership|recovery'):
            install.rollback_plan({'schema_version': 1, 'status': 'prepared', 'files': []})

    def test_retained_proof_survives_uninstall_and_unrelated_new_commit(self):
        result = self.apply(); old_path = Path(result['manifest']); old = json.loads(old_path.read_bytes())
        active = install.read(self.state/'active.json'); ref = json.loads(active)
        install.apply_rollback(install.rollback_plan(old), self.state, old, ref, 'uninstall', active)
        new = self.apply()
        self.assertNotEqual(Path(new['manifest']), old_path)
        self.assertIsNone(json.loads(Path(new['manifest']).read_bytes())['previous_manifest'])
        self.assertTrue(install._installation_committed(old))

    def test_active_chain_proves_prior_commit_without_extra_receipt(self):
        result = self.apply(); old_path = Path(result['manifest']); old = json.loads(old_path.read_bytes())
        plans = install.plan(self.package, self.home, self.skills, self.state, self.overlay, max_child_threads=2)
        self.assertTrue(plans)
        install.apply_plan(plans, self.state, self.package)
        (old_path.parent/'commit.json').unlink()
        self.assertTrue(install._installation_committed(old))

    def test_commit_receipt_content_checksum_detects_valid_shape_edit(self):
        result = self.apply(); path = Path(result['manifest']); raw = path.read_bytes()
        receipt = json.loads((path.parent/'commit.json').read_bytes())
        receipt['publication']['identity'][0] += 1
        (path.parent/'commit.json').write_bytes(install.canonical(receipt))
        with self.assertRaisesRegex(install.InstallError, 'integrity'):
            install.rollback_plan(json.loads(raw))

    def test_hard_exit_matrix_preserves_foreign_objects_and_restores_owned(self):
        phases = ('target_before', 'target_after', 'manifest_after', 'active_before', 'active_after')
        mutations = ('unchanged', 'replacement', 'bytes', 'mode', 'acl', 'parent', 'backup')
        for phase in phases:
            for created in (False, True):
                for mutation in mutations:
                    self.setUp()
                    with self.subTest(phase=phase, created=created, mutation=mutation):
                        path, manifest, target = self.kill_phase(phase, created)
                        row = next(row for row in manifest['files'] if row['path'] == str(target))
                        current = install.read(target)
                        # Before replace, the unchanged original is never removed.
                        if mutation == 'replacement' and current is not None:
                            self.replace(target, current)
                        elif mutation == 'bytes' and current is not None:
                            target.write_bytes(current+b'\nexternal unmanaged change\n')
                        elif mutation == 'mode' and current is not None:
                            if os.name == 'nt': self.skipTest('POSIX chmod case; actual Windows DACL fixture is required separately')
                            target.chmod(0o640 if target.stat().st_mode & 0o777 != 0o640 else 0o600)
                        elif mutation == 'acl' and current is not None:
                            if os.name == 'nt': self.skipTest('Actual Windows DACL fault cases require the Windows qualification host')
                            try: os.setxattr(target, 'user.foreign-install-edit', b'preserve')
                            except (OSError, AttributeError): self.skipTest('Host filesystem does not support observable user xattrs')
                        elif mutation == 'parent':
                            moved = target.parent.with_name(target.parent.name+'-saved')
                            target.parent.rename(moved); target.parent.mkdir()
                            if current is not None: target.write_bytes(current)
                        elif mutation == 'backup':
                            backup = next((Path(item['backup']) for item in manifest['files'] if item['backup']), None)
                            self.assertIsNotNone(backup); backup.write_bytes(b'foreign backup content')
                        after = install.read(target)
                        identity = install.metadata(target)
                        conflict = mutation in ('parent', 'backup') or (phase != 'active_after' and current != (install.read(Path(row['backup'])) if row['backup'] else None) and mutation in ('replacement','bytes','mode','acl'))
                        if conflict:
                            with self.assertRaises(install.InstallError): install.rollback_plan(manifest)
                            self.assertEqual(install.read(target), after)
                            self.assertEqual(install.metadata(target), identity)
                        elif mutation == 'unchanged':
                            self.reconcile(path, manifest)
                            self.assertEqual(install.read(target), install.read(Path(row['backup'])) if row['backup'] else None)
                        else:
                            # Completed-install targeted rollback may succeed or
                            # report a policy conflict, but planning never mutates.
                            try: install.rollback_plan(manifest)
                            except install.InstallError: pass
                            self.assertEqual(install.read(target), after)
                            self.assertEqual(install.metadata(target), identity)

    def test_corrupt_retained_commit_receipt_is_not_ignored(self):
        result = self.apply(); path = Path(result['manifest'])
        (path.parent/'commit.json').write_text('{"schema_version":1,"record_type":"forged"}')
        manifest = json.loads(path.read_bytes())
        before = {row['path']: install.read(Path(row['path'])) for row in manifest['files']}
        with self.assertRaisesRegex(install.InstallError, 'commit|Commit|integrity'):
            install.rollback_plan(manifest)
        self.assertEqual(before, {name: install.read(Path(name)) for name in before})

    def test_ambiguous_active_reference_stops_pending_recovery(self):
        _, manifest = self.kill_install('active_before')
        target = Path(manifest['files'][0]['path']); raw = target.read_bytes()
        install.atomic(self.state/'active.json', install.canonical({'manifest': str(self.state/'foreign.json'), 'sha256': '0'*64}))
        with self.assertRaisesRegex(install.InstallError, 'active|Active|commit|integrity|recovery'):
            install.rollback_plan(manifest)
        self.assertEqual(target.read_bytes(), raw)

    def test_legacy_interrupted_manifest_cannot_invent_publication_ownership(self):
        _, manifest = self.kill_install('active_before')
        for row in manifest['files']:
            row.pop('publication_identity', None)
        with self.assertRaisesRegex(install.InstallError, 'binding|conflict|ownership|manifest|preserved'):
            install.rollback_plan(manifest)

