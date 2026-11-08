"""Directory aliases must not change authority or hide links during recovery."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_harness.file_io import canonical_directory, same_directory, FileReadError


def short_path(path):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetShortPathNameW.restype = wintypes.DWORD
    size = kernel.GetShortPathNameW(str(path), None, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(size)
    if not kernel.GetShortPathNameW(str(path), buffer, size):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value)


class DirectoryAddressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='harness-long-directory-address-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_missing_suffix_is_bound_to_its_physical_parent(self):
        first = self.root / 'first'; second = self.root / 'second'
        first.mkdir(); second.mkdir()
        self.assertTrue(same_directory(first / 'future' / 'run', first / 'future' / 'run'))
        self.assertFalse(same_directory(first / 'future', second / 'future'))
        self.assertFalse(same_directory(first / 'future', first / 'other'))
        self.assertEqual(canonical_directory(first / 'future'), first / 'future')
        self.assertFalse((first / 'future').exists())

    def test_links_are_rejected_before_resolution_including_missing_children(self):
        target = self.root / 'target'; target.mkdir()
        alias = self.root / 'alias'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(alias), str(target)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(target, target_is_directory=True)
        for path in (alias, alias / 'missing' / 'run'):
            with self.subTest(path=path):
                with self.assertRaises(FileReadError): canonical_directory(path)
                with self.assertRaises(FileReadError): same_directory(path, target)
        with self.assertRaises(FileReadError): canonical_directory(alias / '..')
        with self.assertRaises(FileReadError): same_directory(alias / '..', self.root)

    def test_ordinary_parent_traversal_keeps_native_directory_semantics(self):
        directory = self.root / 'child'; directory.mkdir()
        self.assertEqual(canonical_directory(directory / '..'), self.root)
        self.assertTrue(same_directory(directory / '..', self.root))
        with self.assertRaises(FileNotFoundError):
            canonical_directory(self.root / 'nonexistent' / '..')

    @unittest.skipUnless(os.name == 'nt', 'Windows path aliases')
    def test_short_names_and_case_share_existing_and_future_directory_addresses(self):
        alias = short_path(self.root)
        if str(alias).lower() == str(self.root).lower():
            self.skipTest('8.3 short names are disabled on this volume')
        for path in (alias, Path(str(self.root).swapcase())):
            self.assertTrue(same_directory(path, self.root))
            self.assertTrue(same_directory(path / 'future', self.root / 'future'))
            self.assertEqual(canonical_directory(path / 'future'), self.root / 'future')


@unittest.skipUnless(os.name == 'nt', 'Windows historical 8.3 paths')
class SupervisorAliasRecoveryTests(unittest.TestCase):
    def test_legacy_short_preparation_and_receipts_recover_from_long_spelling(self):
        from test_work_supervisor import WorkSupervisorTests
        from agent_harness import root_preparation as protocol
        from agent_harness.supervisor import Supervisor
        fixture = WorkSupervisorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        alias = short_path(fixture.root)
        if str(alias).lower() == str(fixture.root.resolve()).lower():
            self.skipTest('8.3 short names are disabled on this volume')
        owner = fixture.create()
        # Reproduce an old checkpoint whose authenticated strings predate the
        # canonical Supervisor representation. Do not migrate its frozen bytes.
        owner.path = alias / 'private' / 'run'
        with owner.store.lease('historical short-path preparation'):
            owner.load()
            for key in ('cwd', 'source_cwd'):
                owner.state[key] = str(alias / 'source')
            owner._save('historical source spellings')
            run, identity = protocol.prepare(owner, 'initial')
            material = protocol.current_preparation(owner, identity)
            self.assertIn('~', material['binding']['core_path'])
            frozen = (owner.path / material['publication_prior_ref']['path']).read_bytes()
            owner._assert_candidate_binding(run)
        recovered = Supervisor(fixture.run.resolve(), adapter=fixture.adapter)
        with recovered.store.lease('long-path recovery'):
            recovered.load(recover=True)
            again = protocol.current_preparation(recovered, identity)
            self.assertEqual(again, material)
            self.assertEqual((recovered.path / again['publication_prior_ref']['path']).read_bytes(), frozen)
            self.assertEqual(protocol.prepare_dispatch(recovered, repair=False).state(), run.state())

    def test_supervisor_rejects_source_alias_inside_private_boundary(self):
        from agent_harness.core import HarnessError
        from agent_harness.strict_yaml import dump
        from agent_harness.supervisor import Supervisor
        import sys
        with tempfile.TemporaryDirectory(prefix='harness-boundary-address-') as directory:
            source = Path(directory) / 'source'; source.mkdir()
            alias = short_path(source)
            contract = Path(directory) / 'task.yaml'
            dump(contract, {'schema_version': 1, 'task': {'id': 'alias', 'objective': 'boundary',
                'allowed_write_scopes': ['**']}, 'acceptance': [{'id': 'check', 'kind': 'command',
                'required': True, 'argv': [sys.executable, '-c', 'pass'], 'timeout': 5}],
                'budgets': {'max_attempts': 1, 'max_wall_seconds': 90}})
            with self.assertRaisesRegex(HarnessError, 'outside'):
                Supervisor.create(contract, cwd=alias, run_dir=source / 'private')
            self.assertFalse((source / 'private').exists())
