"""Git checkout EOL conversion and physical project-root boundaries."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.benchmark_environments import ArmHomeBuilder
from agent_harness.benchmark_git import initialize_fresh_git
from agent_harness.benchmark_importers import capture_local_git_checkout
from agent_harness.core import HarnessError
from agent_harness.file_io import FileReadError


class BenchmarkCheckoutIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='benchmark checkout ')
        self.addCleanup(temporary.cleanup)
        # Keep TEMP's native spelling: Windows may supply an 8.3 alias.
        self.root=Path(temporary.name)
        self.repo=self.root/'repository';self.repo.mkdir()
        self.assets=self.root/'assets';self.assets.mkdir()
        self.environment={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
        self.environment.update(GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_NOSYSTEM='1')
        self.git('init','--template=')
        self.git('config','core.autocrlf','false')
        self.git('config','core.attributesFile',os.devnull)
        (self.repo/'.git/info').mkdir(exist_ok=True)
        self.original=b'first\nsecond\n'
        self.commit(self.original)
        self.builder=ArmHomeBuilder(Path(__file__).resolve().parents[1],self.assets)

    def git(self,*args):
        return subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],
            cwd=self.repo,env=self.environment,check=True,capture_output=True).stdout

    def commit(self,raw):
        (self.repo/'file').write_bytes(raw)
        self.git('add','.');self.git('commit','-m','baseline')
        self.revision=self.git('rev-parse','HEAD').decode().strip()

    def capture(self):
        return capture_local_git_checkout(self.repo,self.revision,self.assets)

    def directory_alias(self,name):
        alias=self.root/name
        if os.name=='nt':
            result=subprocess.run(['cmd','/c','mklink','/J',str(alias),str(self.repo)],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.addCleanup(alias.rmdir)
        else:alias.symlink_to(self.repo,target_is_directory=True)
        return alias

    def test_crlf_exports_blob_bytes_and_recreates_exact_git_tree(self):
        self.git('config','core.autocrlf','true')
        (self.repo/'file').unlink();self.git('checkout','--','file')
        raw=(self.repo/'file').read_bytes()
        self.assertEqual(raw,self.original.replace(b'\n',b'\r\n'))
        files,checkout=self.capture()
        blob=(self.assets/files[0]['asset']['path']).read_bytes()
        self.assertEqual(blob,self.original)
        self.assertEqual(files[0]['asset']['sha256'],hashlib.sha256(blob).hexdigest())
        self.assertEqual((self.repo/'file').read_bytes(),raw)
        destination=self.root/'fresh';destination.mkdir();(destination/'file').write_bytes(blob)
        fresh=initialize_fresh_git(destination,expected_tree=checkout['tree'])
        self.assertEqual(fresh['initial_tree'],checkout['tree'])

    def test_existing_crlf_blob_is_preserved_exactly(self):
        raw=b'first\r\nsecond\r\n'
        self.commit(raw)
        files,_=self.capture()
        self.assertEqual((self.assets/files[0]['asset']['path']).read_bytes(),raw)

    def test_dirty_mixed_and_binary_eol_changes_are_refused(self):
        for raw in (b'changed\r\nsecond\r\n',b'first\r\nsecond\n'):
            with self.subTest(raw=raw):
                (self.repo/'file').write_bytes(raw)
                with self.assertRaisesRegex(HarnessError,'DIRTY_BASE_CHECKOUT'):self.capture()
        self.commit(b'binary\0\n')
        (self.repo/'file').write_bytes(b'binary\0\r\n')
        with self.assertRaisesRegex(HarnessError,'DIRTY_BASE_CHECKOUT'):self.capture()

    def test_crlf_does_not_hide_filters_encoding_ident_or_binary_attributes(self):
        (self.repo/'file').write_bytes(self.original.replace(b'\n',b'\r\n'))
        marker=self.root/'filter-executed'
        # No Git operation in capture may run this repository-defined command.
        self.git('config','filter.custom.clean','echo executed > "'+marker.as_posix()+'"')
        attributes=self.repo/'.git/info/attributes'
        for declaration in ('filter=custom','working-tree-encoding=UTF-8','ident','-text','binary'):
            with self.subTest(attribute=declaration):
                attributes.write_bytes(('file '+declaration+'\n').encode())
                with self.assertRaisesRegex(HarnessError,'FILTERED_BYTES_UNSUPPORTED'):self.capture()
                self.assertFalse(marker.exists())

    def test_crlf_accepts_explicit_text_attributes(self):
        (self.repo/'file').write_bytes(self.original.replace(b'\n',b'\r\n'))
        (self.repo/'.git/info/attributes').write_bytes(b'file text eol=crlf\n')
        files,_=self.capture()
        self.assertEqual((self.assets/files[0]['asset']['path']).read_bytes(),self.original)

    def test_root_and_different_directory_have_distinct_physical_boundaries(self):
        self.assertTrue(self.builder._ancestry(self.repo)['git_root_observed'])
        peer=self.root/'peer';peer.mkdir()
        result=subprocess.CompletedProcess([],0,os.fsencode(peer)+b'\n')
        with patch('agent_harness.benchmark_environments._contained_capture',return_value=result):
            self.assertFalse(self.builder._ancestry(self.repo)['git_root_observed'])
        nested=self.repo/'nested';nested.mkdir()
        self.assertFalse(self.builder._ancestry(nested)['git_root_observed'])

    @unittest.skipUnless(os.name=='nt','Native Windows case and 8.3 aliases')
    def test_windows_case_and_short_aliases_observe_the_same_git_root(self):
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetShortPathNameW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32]
        kernel.GetShortPathNameW.restype=ctypes.c_uint32
        buffer=ctypes.create_unicode_buffer(32768)
        length=kernel.GetShortPathNameW(str(self.repo),buffer,len(buffer))
        self.assertGreater(length,0)
        for spelling in (str(self.repo).swapcase(),buffer.value):
            with self.subTest(spelling=spelling):
                self.assertTrue(self.builder._ancestry(spelling)['git_root_observed'])

    @unittest.skipIf(os.name=='nt','Actual POSIX case-sensitive sibling directories')
    def test_posix_case_distinct_root_is_not_accepted(self):
        peer=self.root/'Repository';peer.mkdir()
        result=subprocess.CompletedProcess([],0,os.fsencode(peer)+b'\n')
        with patch('agent_harness.benchmark_environments._contained_capture',return_value=result):
            self.assertFalse(self.builder._ancestry(self.repo)['git_root_observed'])

    def test_git_root_link_spelling_is_rejected_before_resolution(self):
        alias=self.directory_alias('alias')
        result=subprocess.CompletedProcess([],0,os.fsencode(alias)+b'\n')
        with patch('agent_harness.benchmark_environments._contained_capture',return_value=result):
            with self.assertRaises(FileReadError):self.builder._ancestry(self.repo)

    def test_workspace_link_is_rejected_before_git_probe(self):
        alias=self.directory_alias('workspace-alias')
        with patch('agent_harness.benchmark_environments._present',side_effect=AssertionError('probe before guard')):
            with self.assertRaises(FileReadError):self.builder._ancestry(alias)

    def test_checkout_file_symlink_is_rejected_before_eol_conversion(self):
        target=self.root/'substitute';target.write_bytes(self.original.replace(b'\n',b'\r\n'))
        (self.repo/'file').unlink()
        try:(self.repo/'file').symlink_to(target)
        except OSError as error:self.skipTest('File symlink unavailable: '+str(error))
        with self.assertRaises(FileReadError):self.capture()


if __name__=='__main__':unittest.main()
