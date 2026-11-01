from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_harness import file_io
from agent_harness.file_io import FileReadError, read_regular_file


class RegularFileReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "input"

    def test_boundaries_and_only_missing_is_optional(self):
        self.assertIsNone(read_regular_file(self.path, missing_ok=True))
        with self.assertRaises(FileNotFoundError):
            read_regular_file(self.path)
        self.path.write_bytes(b"1234")
        self.assertEqual(read_regular_file(self.path, 4), b"1234")
        with self.assertRaises(FileReadError):
            read_regular_file(self.path, 3)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(FileReadError):
            read_regular_file(self.path, missing_ok=True)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_static_and_raced_fifo_are_rejected_without_blocking(self):
        os.mkfifo(self.path)
        with self.assertRaises(FileReadError):
            read_regular_file(self.path)
        self.path.unlink()
        self.path.write_bytes(b"initial")
        native_open = os.open

        def race(path, flags, *args, **kwargs):
            self.path.unlink()
            os.mkfifo(self.path)
            self.assertTrue(flags & os.O_NONBLOCK)
            return native_open(path, flags, *args, **kwargs)

        with patch("agent_harness.file_io.os.open", side_effect=race):
            with self.assertRaises(FileReadError):
                read_regular_file(self.path)

    def test_growth_or_metadata_change_during_read_is_rejected(self):
        self.path.write_bytes(b"1234")
        native_read = os.read
        changed = False

        def race(fd, size):
            nonlocal changed
            value = native_read(fd, size)
            if not changed:
                changed = True
                self.path.write_bytes(b"12345")
            return value

        with patch("agent_harness.file_io.os.read", side_effect=race), self.assertRaises(FileReadError):
            read_regular_file(self.path, 4)

    def test_final_reparse_check_refuses_same_identity(self):
        self.path.write_bytes(b"data")
        native_lstat = Path.lstat
        calls = 0

        def reparse(path, *args, **kwargs):
            nonlocal calls
            calls += 1
            value = native_lstat(path, *args, **kwargs)
            if calls == 2:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value

        with patch.object(Path, "lstat", new=reparse), self.assertRaisesRegex(FileReadError, "reparse"):
            read_regular_file(self.path)

    def test_windows_stat_fstat_ctime_difference_is_not_a_false_race(self):
        self.path.write_bytes(b"data")
        native_fstat = os.fstat

        def different_ctime(fd):
            value = native_fstat(fd)
            fields = {name: getattr(value, name) for name in (
                "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink", "st_uid", "st_gid")}
            return SimpleNamespace(**fields, st_ctime_ns=value.st_ctime_ns + 123456789)

        with patch("agent_harness.file_io.os.fstat", side_effect=different_ctime):
            self.assertEqual(read_regular_file(self.path), b"data")

    def test_content_verification_rejects_same_size_edit_with_unchanged_observations(self):
        self.path.write_bytes(b'{"a":1}')
        observed = self.path.lstat()
        native_read = os.read
        changed = False

        def editing(fd, size):
            nonlocal changed
            value = native_read(fd, size)
            if not changed:
                changed = True
                self.path.write_bytes(b'{"a":2}')
            return value

        # Model Windows delayed timestamps explicitly: every identity/timestamp
        # observation stays unchanged; only a real content comparison can fail.
        with patch.object(file_io, "_VERIFY_CONTENT", True), \
                patch.object(Path, "lstat", return_value=observed), \
                patch.object(os, "fstat", return_value=observed), \
                patch.object(os, "read", side_effect=editing), \
                self.assertRaisesRegex(FileReadError, "content verification"):
            read_regular_file(self.path)
        self.assertTrue(changed)

    def test_verification_pass_accepts_empty_and_exact_bounds_and_stays_bounded(self):
        native_read = os.read
        for data in (b"", b"x", b"x" * 65536):
            with self.subTest(size=len(data)):
                self.path.write_bytes(data)
                reads = []

                def reading(fd, size):
                    value = native_read(fd, size)
                    reads.append(len(value))
                    return value

                with patch.object(file_io, "_VERIFY_CONTENT", True), patch.object(os, "read", side_effect=reading):
                    self.assertEqual(read_regular_file(self.path, len(data)), data)
                self.assertEqual(sum(reads), 2 * len(data))

    def test_growth_during_verification_is_rejected_with_unchanged_observations(self):
        self.path.write_bytes(b"old")
        observed = self.path.lstat()
        native_seek = os.lseek

        def growing(fd, position, whence):
            self.path.write_bytes(b"longer")
            return native_seek(fd, position, whence)

        with patch.object(file_io, "_VERIFY_CONTENT", True), \
                patch.object(Path, "lstat", return_value=observed), \
                patch.object(os, "fstat", return_value=observed), \
                patch.object(os, "lseek", side_effect=growing), \
                self.assertRaisesRegex(FileReadError, "content verification"):
            read_regular_file(self.path, 3)

    def test_path_identity_is_checked_after_content_verification(self):
        self.path.write_bytes(b"old")
        native_lstat = Path.lstat
        native_seek = os.lseek
        verification_started = False

        def verifying(fd, position, whence):
            nonlocal verification_started
            verification_started = True
            return native_seek(fd, position, whence)

        def changed_entry(path, *args, **kwargs):
            value = native_lstat(path, *args, **kwargs)
            if verification_started:
                # Windows can deny replacement of the open file. Model the
                # changed directory entry without relying on delete sharing.
                fields = {name: getattr(value, name) for name in (
                    "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink",
                    "st_uid", "st_gid", "st_ctime_ns")}
                fields["st_ino"] += 1
                return SimpleNamespace(**fields)
            return value

        with patch.object(file_io, "_VERIFY_CONTENT", True), \
                patch.object(os, "lseek", side_effect=verifying), \
                patch.object(Path, "lstat", new=changed_entry), \
                self.assertRaisesRegex(FileReadError, "changed"):
            read_regular_file(self.path)


if __name__ == "__main__":
    unittest.main()
