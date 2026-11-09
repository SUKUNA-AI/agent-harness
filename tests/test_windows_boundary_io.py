"""Windows IO contracts, with portable failure models and actual host controls."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_harness import file_io, installation


class WindowsBoundaryIOTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'config.toml'
        self.path.write_bytes(b'original')

    def test_windows_cross_api_ignores_only_synthetic_execute_bits(self):
        fields = dict(st_dev=7, st_ino=2**100 + 3, st_mode=stat.S_IFREG | 0o666,
                      st_size=8, st_mtime_ns=100, st_nlink=1, st_uid=0, st_gid=0,
                      st_ctime_ns=200)
        opened = SimpleNamespace(**fields)
        observed = SimpleNamespace(**{**fields, 'st_mode': fields['st_mode'] | 0o111,
                                      'st_ctime_ns': 300})
        with patch.object(file_io.os, 'name', 'nt'):
            self.assertEqual(file_io._stamp(observed, cross_api=True), file_io._stamp(opened, cross_api=True))
            self.assertNotEqual(file_io._stamp(observed), file_io._stamp(opened))
            execute_only = SimpleNamespace(**{**fields, 'st_mode': fields['st_mode'] | 0o111})
            self.assertNotEqual(file_io._stamp(execute_only), file_io._stamp(opened))
            for name, changed in [('st_dev', 8), ('st_ino', 3), ('st_mode', stat.S_IFREG | 0o444),
                                  ('st_size', 9), ('st_mtime_ns', 101), ('st_nlink', 2),
                                  ('st_uid', 1), ('st_gid', 1)]:
                with self.subTest(field=name):
                    other = SimpleNamespace(**{**fields, name: changed})
                    self.assertNotEqual(file_io._stamp(opened, cross_api=True), file_io._stamp(other, cross_api=True))

    def test_posix_cross_api_keeps_execute_mode_changes_visible(self):
        info = self.path.stat()
        fields = {name: getattr(info, name) for name in (
            'st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_nlink', 'st_uid', 'st_gid', 'st_ctime_ns')}
        changed = SimpleNamespace(**{**fields, 'st_mode': info.st_mode ^ 0o111})
        with patch.object(file_io.os, 'name', 'posix'):
            self.assertNotEqual(file_io._stamp(info, cross_api=True), file_io._stamp(changed, cross_api=True))

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows stat/fstat executable semantics')
    def test_actual_windows_interpreter_and_executable_suffixes_read_without_false_race(self):
        interpreter = Path(sys.executable).resolve()
        self.assertEqual(interpreter.read_bytes(), file_io.read_regular_file(interpreter, interpreter.stat().st_size))
        for suffix in ('.exe', '.EXE', '.bat', '.cmd', '.com', '.txt'):
            path = self.path.with_suffix(suffix)
            path.write_bytes(b'fixed executable input')
            with self.subTest(suffix=suffix):
                self.assertEqual(b'fixed executable input', file_io.read_regular_file(path))

    def test_atomic_flushes_same_writable_staging_descriptor_after_metadata(self):
        real_open, real_sync = file_io.ParentDirectory.open, os.fsync
        modes, flushed, metadata_seen = {}, [], []
        real_copy = installation.copy_metadata

        def opened(parent, flags, *args, **kwargs):
            fd = real_open(parent, flags, *args, **kwargs)
            modes[fd] = flags
            return fd

        def sync(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                if not modes.get(fd, 0) & (os.O_WRONLY | os.O_RDWR):
                    raise OSError(errno.EBADF, 'Windows FlushFileBuffers requires writable handle')
                flushed.append(fd)
            return real_sync(fd)

        def copy(path, desired):
            metadata_seen.append(len(flushed))
            return real_copy(path, desired)

        with patch.object(file_io.ParentDirectory, 'open', new=opened), \
                patch.object(installation, 'copy_metadata', side_effect=copy), \
                patch.object(os, 'fsync', side_effect=sync):
            installation.atomic(self.path, b'replacement')
        self.assertEqual(b'replacement', self.path.read_bytes())
        self.assertEqual([1], metadata_seen)
        self.assertEqual(2, len(flushed))
        self.assertEqual(flushed[0], flushed[1])

    def test_receipt_metadata_is_captured_after_writer_close(self):
        real_metadata, real_copy, real_sync = installation._metadata, installation.copy_metadata, os.fsync
        copied, writer, postclose_observed = False, None, False

        def sync(fd):
            nonlocal writer
            if stat.S_ISREG(os.fstat(fd).st_mode):
                writer = fd
            return real_sync(fd)

        def copy(path, desired):
            nonlocal copied
            value = real_copy(path, desired)
            copied = True
            return value

        def metadata(path):
            nonlocal postclose_observed
            if copied and path.name.startswith('.harness-'):
                with self.assertRaises(OSError):
                    os.fstat(writer)
                postclose_observed = True
            return real_metadata(path)

        with patch.object(installation, 'copy_metadata', side_effect=copy), \
                patch.object(installation, '_metadata', side_effect=metadata), \
                patch.object(os, 'fsync', side_effect=sync):
            installation.atomic(self.path, b'replacement')
        self.assertTrue(postclose_observed)
        self.assertEqual(b'replacement', self.path.read_bytes())

    def test_metadata_flush_failure_keeps_original_and_cleans_stage(self):
        real_sync, calls = os.fsync, 0
        before_mode = stat.S_IMODE(self.path.stat().st_mode)

        def sync(fd):
            nonlocal calls
            if stat.S_ISREG(os.fstat(fd).st_mode):
                calls += 1
                if calls == 2:
                    raise OSError(errno.EIO, 'injected metadata flush failure')
            return real_sync(fd)

        with patch.object(os, 'fsync', side_effect=sync), self.assertRaises(OSError):
            installation.atomic(self.path, b'replacement')
        self.assertEqual(2, calls)
        self.assertEqual(b'original', self.path.read_bytes())
        self.assertEqual(before_mode, stat.S_IMODE(self.path.stat().st_mode))
        self.assertEqual([], list(self.path.parent.glob('.harness-*')))

    def test_staging_identity_change_cannot_be_published(self):
        real_metadata, real_copy = installation._metadata, installation.copy_metadata
        copied = False

        def copy(path, desired):
            nonlocal copied
            value = real_copy(path, desired)
            copied = True
            return value

        def metadata(path):
            value = real_metadata(path)
            if copied and path.name.startswith('.harness-') and value:
                value = {**value, 'identity': (value['identity'][0], value['identity'][1] + 1, *value['identity'][2:])}
            return value

        with patch.object(installation, 'copy_metadata', side_effect=copy), \
                patch.object(installation, '_metadata', side_effect=metadata), \
                self.assertRaisesRegex(installation.InstallError, 'Staging identity changed'):
            installation.atomic(self.path, b'replacement')
        self.assertEqual(b'original', self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob('.harness-*')))
