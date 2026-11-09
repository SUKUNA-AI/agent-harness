"""Expected-safe F03/F04/F05/F12 controls, with real isolated IO."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import core, installation, validation, work_contract
from agent_harness.file_io import ParentDirectory, FileReadError, read_regular_file
import test_install


class ReportSpecTests(unittest.TestCase):
    def check_xml(self, xml, **definition):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'report.xml'
            path.write_bytes(xml.encode())
            return validation.validate({'schema_version': 1, 'reports': [{'path': str(path),
                'definition': {'format': 'junit', **definition}}]})

    def test_suite_errors_failures_and_summary_counts_never_pass(self):
        for issue in ('<error/>', '<failure/>', ''):
            for count in ('errors="1"', 'failures="1"'):
                result = self.check_xml(f'<testsuite tests="1" {count}><testcase name="required"/>{issue}</testsuite>', required_cases=['required'])
                self.assertEqual('failed', result['status'], result)

    def test_native_wrapper_refuses_old_same_product_version_capability(self):
        from agent_harness import native
        old = {'schema_version': 1, 'kind': 'non_file', 'files': {}, 'external_inputs': {}}
        with patch.object(native, '_mode', return_value='native'), patch.object(native, 'find_binary', return_value=Path('/fixture/native')), patch.object(native, 'invoke', return_value=old):
            with self.assertRaisesRegex(core.HarnessError, 'capability is stale'):
                native.snapshot({})


    def test_healthy_nested_namespace_report_counts_once_and_case_ids_distinguish(self):
        result = self.check_xml('<testsuites xmlns="urn:junit" tests="2" errors="0"><testsuite name="one" tests="1"><testcase name="required"/></testsuite><testsuite name="two" tests="1"><testcase name="required"/></testsuite></testsuites>', required_cases=['required'])
        self.assertEqual('passed', result['status'], result)
        report = result['reports'][0]
        self.assertEqual(2, report['tests'])
        self.assertEqual(2, len(set(report['case_ids'])))
        self.assertEqual('junit-harness-v2', report['parser_profile'])

    def test_every_required_match_must_execute_and_pass(self):
        for attrs, body in [('status="notrun"', ''), ('status="disabled"', ''),
                            ('result="suppressed"', ''), ('status="unknown"', ''), ('', '<skipped/>')]:
            result = self.check_xml(f'<testsuite><testcase name="required"/><testcase name="required" {attrs}>{body}</testcase></testsuite>', required_cases=['required'], max_skipped=1)
            self.assertEqual('failed', result['status'], result)

    def test_unexpected_structure_counts_and_entity_payload_fail_closed(self):
        corpus = ['<other><testcase/></other>', '<testsuite tests="2"><testcase/></testsuite>',
                  '<testsuite xmlns="urn:unknown"><testcase/></testsuite>', '<testsuite/>',
                  '<testsuites><testcase/></testsuites>', '<testsuite><wrapper><testcase/></wrapper></testsuite>',
                  '<!DOCTYPE a [<!ENTITY x "value">]><testsuite><testcase name="&x;"/></testsuite>',
                  '<testsuite skipped="1"><testcase/></testsuite>', '<testsuite errors="-1"><testcase/></testsuite>']
        for xml in corpus:
            self.assertEqual('failed', self.check_xml(xml)['status'], xml)

    def test_parser_bounds_are_enforced(self):
        with patch.object(validation, 'REPORT_MAX_BYTES', 64):
            self.assertEqual('failed', self.check_xml('<testsuite>' + '<testcase/>' * 10 + '</testsuite>')['status'])
        with patch.object(validation, 'REPORT_MAX_DEPTH', 3):
            self.assertEqual('failed', self.check_xml('<testsuite>' * 4 + '<testcase/>' + '</testsuite>' * 4)['status'])
        with patch.object(validation, 'REPORT_MAX_NODES', 3):
            self.assertEqual('failed', self.check_xml('<testsuite>' + '<testcase/>' * 4 + '</testsuite>')['status'])

    def test_json_report_rejects_duplicates_nonfinite_and_missing_null(self):
        for raw, expected, status in [('{"a":null}', None, 'passed'), ('{}', None, 'failed'),
                                      ('{"a":false}', 0, 'failed'), ('{"a":1.0}', 1, 'passed'),
                                      ('{"a":1,"a":2}', 2, 'failed'), ('{"a":NaN}', None, 'failed')]:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'r.json'
                path.write_text(raw)
                result = validation.validate({'schema_version': 1, 'reports': [{'path': str(path),
                    'definition': {'format': 'json', 'equals': {'a': expected}}}]})
                self.assertEqual(status, result['status'], result)

    def test_managed_and_core_share_bounded_equals_corpus(self):
        from agent_harness.strict_yaml import StrictYAMLError
        with tempfile.TemporaryDirectory() as temporary:
            values = [{}, {'status': 'ok'}, {'deep.path': {'a': [None, False, 0, 1.0, 'text']}}]
            for equals in values:
                report = {'path': str(Path(temporary) / 'r.json'), 'format': 'json', 'equals': equals}
                check = {'id': 'report', 'kind': 'command', 'required': True, 'argv': [sys.executable, '-c', 'pass'], 'timeout': 3, 'reports': [report]}
                managed = {'schema_version': 1, 'task': {'id': 'equals', 'objective': 'Validate JSON', 'read_only': True}, 'acceptance': [check]}
                self.assertEqual(equals, work_contract.normalize(managed)['acceptance'][0]['reports'][0]['equals'])
                contract = {'schema_version': 1, 'run_id': 'r', 'task_id': 't', 'attempt_id': 'a', 'goal': 'Validate JSON',
                    'workflow': 'review', 'process_level': 'standard', 'cwd': temporary, 'permissions': ['read', 'test'], 'checks': [check]}
                core.validate_contract(contract)
                for bad in [{1: 'value'}, {'a': float('inf')}, {'a': float('nan')}, {'a': object()}]:
                    report['equals'] = bad
                    with self.assertRaises(ValueError):
                        work_contract.normalize(managed)
                    with self.assertRaises(core.HarnessError):
                        core.validate_contract(contract)
                report['equals'] = equals
                report['unknown'] = True
                with self.assertRaises(StrictYAMLError):
                    work_contract.normalize(managed)
                with self.assertRaises(core.HarnessError):
                    core.validate_contract(contract)


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'source'
        self.root.mkdir()
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        (self.outside / 'file').write_bytes(b'identical')

    def link(self, target, path):
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f'Runtime cannot create symlink: {error}')

    def test_ignored_tracked_parent_retarget_refuses_read_and_acceptance(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        (self.root / '.gitignore').write_text('parent/\n')
        parent = self.root / 'parent'
        parent.mkdir()
        (parent / 'file').write_bytes(b'identical')
        subprocess.run(['git', '-C', str(self.root), 'add', '-f', 'parent/file', '.gitignore'], check=True, capture_output=True)
        contract = {'cwd': str(self.root)}
        before = core.snapshot_python(contract)
        self.assertEqual(hashlib.sha256(b'identical').hexdigest(), before['files']['parent/file']['sha256'])
        parent.rename(self.root / 'original')
        self.link(self.outside, parent)
        with patch.object(core, 'file_hash', wraps=core.file_hash) as observed:
            with self.assertRaises(core.HarnessError):
                core.snapshot_python(contract)
            self.assertFalse(any(str(call.args[0]).endswith('parent/file') for call in observed.call_args_list))
        self.assertEqual(b'identical', (self.outside / 'file').read_bytes())

    def test_gate_and_readonly_status_cannot_keep_retargeted_acceptance_current(self):
        from agent_harness import runner
        parent = self.root / 'parent'
        parent.mkdir()
        (parent / 'file').write_bytes(b'identical')
        contract = {'schema_version': 1, 'run_id': 'physical', 'task_id': 'physical', 'attempt_id': 'a1',
            'workflow': 'review', 'process_level': 'standard', 'goal': 'Verify physical source',
            'cwd': str(self.root), 'permissions': ['read', 'test'], 'checks': [
                {'id': 'unit', 'kind': 'command', 'required': True, 'argv': [sys.executable, '-c', 'print(42)'], 'timeout': 3}]}
        run = core.Run.create(self.base / 'run', contract)
        run.freeze()
        self.assertEqual('passed', runner.run_check(run, 'unit')['status'])
        self.assertEqual('accepted', runner.gate(run)['verdict'])
        # Non-Git directory snapshots include the link object, changing digest.
        parent.rename(self.base / 'original-parent')
        self.link(self.outside, parent)
        with self.assertRaises(core.HarnessError):
            runner.gate(run)
        observed = run.status(verify_evidence=True)
        self.assertFalse(observed['evidence_current'])
        self.assertEqual('stale', observed['status'])


    def test_leaf_link_literal_and_root_alias_refused(self):
        link = self.root / 'leaf'
        self.link(self.outside / 'file', link)
        result = core.snapshot_python({'cwd': str(self.root)})
        self.assertEqual('symlink', result['files']['leaf']['kind'])
        # Windows may expose the stored target with its verbatim \\?\ prefix.
        # The snapshot contract preserves os.readlink's literal target.
        self.assertEqual(os.readlink(link), result['files']['leaf']['target'])
        self.assertEqual(2, result['snapshot_scheme_version'])
        alias = self.base / 'alias'
        self.link(self.root, alias)
        with self.assertRaises(core.HarnessError):
            core.snapshot_python({'cwd': str(alias)})

    def test_anchored_read_refuses_nested_dangling_and_raced_parent(self):
        self.link(self.outside, self.root / 'nested')
        self.link(self.base / 'missing', self.root / 'dangling')
        for name in ['nested', 'dangling']:
            with self.assertRaises((FileReadError, OSError)):
                read_regular_file(self.root / name / 'file')
        parent = self.root / 'parent'
        parent.mkdir()
        (parent / 'file').write_bytes(b'owned')
        original = ParentDirectory.open
        def race(anchor, *args, **kwargs):
            if anchor.path == parent / 'file':
                parent.rename(self.root / 'original')
                self.link(self.outside, parent)
            return original(anchor, *args, **kwargs)
        with patch.object(ParentDirectory, 'open', new=race), self.assertRaises(FileReadError):
            read_regular_file(parent / 'file')
        self.assertEqual(b'identical', (self.outside / 'file').read_bytes())


class InstallationBoundaryTests(unittest.TestCase):
    setUp = test_install.InstallerTests.setUp
    make_plan = test_install.InstallerTests.make_plan
    apply = test_install.InstallerTests.apply

    def test_unsafe_descendant_refuses_whole_plan_before_mutation(self):
        outside = self.base / 'outside'
        outside.mkdir()
        try:
            (self.home / 'agents').symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(str(error))
        before = (self.home / 'config.toml').read_bytes()
        with self.assertRaises(installation.InstallError):
            self.make_plan()
        self.assertEqual([], list(outside.iterdir()))
        self.assertEqual(before, (self.home / 'config.toml').read_bytes())
        self.assertFalse(self.state.exists())

    def test_same_bytes_new_parent_does_not_establish_rollback_ownership(self):
        self.apply()
        manifest = installation.active_manifest(self.state)
        original = self.home / 'agents'
        moved = self.home / 'original-agents'
        original.rename(moved)
        shutil.copytree(moved, original)
        copies = {path.name: path.read_bytes() for path in original.iterdir()}
        with self.assertRaisesRegex(installation.InstallError, 'physical identity'):
            installation.rollback_plan(manifest)
        self.assertEqual(copies, {path.name: path.read_bytes() for path in original.iterdir()})
        self.assertTrue(Path(manifest['files'][0]['installed_copy']).exists())
        self.assertEqual('failed', installation.verify(self.package, self.home, self.skills, self.state)['status'])

    def test_legacy_manifest_reader_and_old_writer_version_barrier(self):
        self.apply()
        manifest = installation.active_manifest(self.state)
        self.assertEqual(3, manifest['schema_version'])
        self.assertEqual('active-binding-v1', manifest['commit_protocol'])
        modern_ref = json.loads((self.state / 'active.json').read_bytes())
        modern_path = Path(modern_ref['manifest'])
        modern_bytes = modern_path.read_bytes()
        self.assertTrue(installation.rollback_plan(manifest))

        # Relabelling a bound modern record is not a legacy installation and
        # must not acquire rollback authority from identical target bytes.
        forged = deepcopy(manifest)
        forged['schema_version'] = 1
        forged.pop('boundary_protocol')
        forged.pop('commit_protocol')
        with self.assertRaisesRegex(installation.InstallError, 'immutable manifest binding'):
            installation.rollback_plan(forged)

        target = self.home / 'AGENTS.md'
        marker = b'\n# unrelated edit after installation\n'
        target.write_bytes(target.read_bytes() + marker)
        current = {row['path']: Path(row['path']).read_bytes() for row in manifest['files']}
        for version in (1, 2):
            with self.subTest(legacy_version=version):
                # Independent owned historical-format transaction: immutable
                # bytes, matching active reference, real installed/backup data.
                # Never rewrite the modern manifest to manufacture old trust.
                state = self.base / f'legacy-state-v{version}'
                legacy = deepcopy(manifest)
                legacy['schema_version'] = version
                legacy['install_id'] = f'legacy-v{version}'
                legacy['previous_manifest'] = None
                legacy.pop('commit_protocol')
                if version == 1:
                    legacy.pop('boundary_protocol')
                transaction = state / 'installs' / legacy['install_id']
                transaction.mkdir(parents=True)
                for row in legacy['files']:
                    for key in ('installed_copy', 'backup'):
                        if row[key] is not None:
                            source = Path(row[key])
                            copied = transaction / source.name
                            copied.write_bytes(source.read_bytes())
                            row[key] = str(copied)
                    if version == 1:
                        for key in ('parent_binding', 'publication_identity', 'progress'):
                            row.pop(key, None)
                path = transaction / 'manifest.json'
                raw = installation.canonical(legacy)
                path.write_bytes(raw)
                (state / 'active.json').write_bytes(installation.canonical({
                    'manifest': str(path), 'sha256': installation.digest(raw)}))
                read = installation.active_manifest(state)
                self.assertEqual(legacy, read)
                plans = installation.rollback_plan(read)
                restored = next(plan['after'] for plan in plans if plan['path'] == target)
                self.assertIn(marker.strip(), restored)
                self.assertEqual(current, {name: Path(name).read_bytes() for name in current})
                # Reader compatibility still requires exact backup bytes.
                Path(legacy['files'][0]['installed_copy']).write_bytes(b'corrupt fixture copy')
                with self.assertRaisesRegex(installation.InstallError, 'Backup integrity'):
                    installation.rollback_plan(read)

        self.assertEqual(modern_bytes, modern_path.read_bytes())
        self.assertEqual(modern_ref, json.loads((self.state / 'active.json').read_bytes()))
        for version in (999, True, 1.0):
            future = deepcopy(manifest)
            future['schema_version'] = version
            with self.subTest(unsupported_version=version), self.assertRaises(installation.InstallError):
                installation.rollback_plan(future)


    def test_hook_rollback_edit_under_non_utf8_default_locale(self):
        # Model the Windows3.11/3.13 default without changing process locale or
        # relying on this host's UTF-8 mode. Product JSON is explicitly UTF-8.
        original_read = Path.read_text
        def legacy_read(path, encoding=None, errors=None, **kwargs):
            return original_read(path, encoding=encoding or "cp1252", errors=errors, **kwargs)
        with patch.object(Path, "read_text", new=legacy_read):
            self.test_hooks_foreign_handler_idempotence_and_rollback()

    def test_hooks_foreign_handler_idempotence_and_rollback(self):
        foreign = {'matcher': 'startup', 'hooks': [{'type': 'command', 'command': 'printf user-handler'}]}
        path = self.home / 'hooks.json'
        path.write_text(json.dumps({'hooks': {'SessionStart': [foreign]}}), encoding="utf-8")
        self.apply()
        groups = json.loads(path.read_text(encoding="utf-8"))['hooks']['SessionStart']
        self.assertEqual(foreign, groups[0])
        self.assertEqual(2, len(groups))
        self.assertEqual('unchanged', self.apply()['status'])
        # New user handlers survive an addressed rollback.
        added = {'matcher': 'clear', 'hooks': [{'type': 'command', 'command': 'printf another-user'}]}
        groups.append(added)
        path.write_text(json.dumps({'hooks': {'SessionStart': groups}}), encoding="utf-8")
        plans = installation.rollback_plan(installation.active_manifest(self.state))
        hooks_plan = next(plan for plan in plans if plan['path'] == path)
        self.assertEqual([foreign, added], json.loads(hooks_plan['after'])['hooks']['SessionStart'])

    def test_launcher_uses_stdin_without_interpreting_event_shell_text(self):
        script = self.package / 'tools/session.py'
        script.write_text("import sys\nprint(sys.stdin.read(), end='')\n", encoding='utf-8')
        _, _, handler = installation.hook_payload(self.package, self.home)
        self.apply()
        marker = self.base / 'event-must-not-execute'
        raw = json.dumps({'hook_event_name': 'SessionStart', 'source': '; touch ' + str(marker)})
        command = handler['hooks'][0]['command']
        result = subprocess.run(command, input=raw, shell=True, text=True, capture_output=True, timeout=3)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(raw, result.stdout)
        self.assertFalse(marker.exists())

    def test_reinstall_refuses_identical_copies_in_replaced_parent(self):
        self.apply()
        original = self.home / 'agents'
        moved = self.home / 'original-agents'
        original.rename(moved)
        shutil.copytree(moved, original)
        with self.assertRaisesRegex(installation.InstallError, 'physical identity'):
            self.make_plan()


    def test_duplicate_or_unmanaged_harness_handler_blocks_before_writes(self):
        _, _, handler = installation.hook_payload(self.package, self.home)
        (self.home / 'hooks.json').write_text(json.dumps({'hooks': {'SessionStart': [handler]}}))
        with self.assertRaisesRegex(installation.InstallError, 'unmanaged'):
            self.make_plan()
        self.assertFalse((self.home / 'agent-harness-hook.py').exists())
        self.assertFalse((self.home / 'harness-machine.json').exists())


class NativeBoundaryParityTests(unittest.TestCase):
    link = SourceBoundaryTests.link
    def setUp(self):
        SourceBoundaryTests.setUp(self)
        if not os.environ.get("AGENT_HARNESS_NATIVE"):
            self.skipTest("Compiled backend qualification requires AGENT_HARNESS_NATIVE")

    def test_real_native_parity_and_linked_tracked_parent_refusal(self):
        from agent_harness.native import invoke
        (self.root / "file").write_bytes(b"source")
        self.link("missing", self.root / "dangling-leaf")
        self.link(self.outside / "file", self.root / "leaf")
        contract = {"cwd": str(self.root)}
        self.assertEqual(core.snapshot_python(contract), invoke({"operation": "snapshot", "contract": contract}))
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        (self.root / '.gitignore').write_text('parent/\n')
        parent = self.root / 'parent'
        parent.mkdir()
        (parent / 'file').write_bytes(b'identical')
        subprocess.run(['git', '-C', str(self.root), 'add', '-f', 'parent/file', '.gitignore'], check=True, capture_output=True)
        self.assertEqual(core.snapshot_python(contract), invoke({"operation": "snapshot", "contract": contract}))
        parent.rename(self.root / 'original')
        self.link(self.outside, parent)
        with self.assertRaises(core.HarnessError):
            invoke({"operation": "snapshot", "contract": contract})
        with self.assertRaises(core.HarnessError):
            core.snapshot_python(contract)
        self.assertEqual(b'identical', (self.outside / 'file').read_bytes())
