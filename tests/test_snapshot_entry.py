import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core import HarnessError, _entry, file_hash


class SnapshotEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_stable_entry_format_and_tracked_deletion_are_preserved(self):
        path = self.root / "file"
        path.write_bytes(b"source bytes")
        path.chmod(0o755)
        self.assertEqual({"kind": "file", "sha256": hashlib.sha256(b"source bytes").hexdigest(),
                          "size": 12, "executable": True if os.name != "nt" else None}, _entry(path))
        self.assertEqual({"kind": "directory"}, _entry(self.root))
        self.assertEqual({"kind": "missing"}, _entry(path / "not-a-directory"))
        path.unlink()
        self.assertEqual({"kind": "missing"}, _entry(path))

    def test_source_replaced_after_hash_is_rejected_even_at_same_size_and_mtime(self):
        path = self.root / "file"
        path.write_bytes(b"before")
        original = path.stat()

        def replace_after_hash(source):
            sha = file_hash(source)
            replacement = self.root / "replacement"
            replacement.write_bytes(b"after!")
            os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
            os.replace(replacement, path)
            return sha

        with patch("agent_harness.core.file_hash", side_effect=replace_after_hash):
            with self.assertRaisesRegex(HarnessError, "changed during snapshot"):
                _entry(path)

    def test_changed_size_during_hash_cannot_create_a_mixed_manifest(self):
        path = self.root / "file"
        path.write_bytes(b"before")

        def mutate_after_hash(source):
            sha = file_hash(source)
            path.write_bytes(b"after, longer")
            return sha

        with patch("agent_harness.core.file_hash", side_effect=mutate_after_hash):
            with self.assertRaisesRegex(HarnessError, "changed during snapshot"):
                _entry(path)

    def test_dangling_symlink_records_target_without_following_it(self):
        path = self.root / "link"
        try:
            path.symlink_to("missing-target")
        except OSError:
            self.skipTest("Symlink creation unavailable")
        self.assertEqual({"kind": "symlink", "target": "missing-target",
                          "sha256": hashlib.sha256(b"missing-target").hexdigest()}, _entry(path))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO only")
    def test_file_replaced_with_fifo_never_blocks_hashing(self):
        # Use a separately bounded process: a regression must fail the test,
        # not hang the suite waiting for a FIFO writer that will never arrive.
        import subprocess
        import sys
        script = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from agent_harness.core import HarnessError, _entry, file_hash
p = Path(sys.argv[2])
p.write_bytes(b"regular")
def swapped(source):
    p.unlink()
    os.mkfifo(p)
    return file_hash(source)
with patch("agent_harness.core.file_hash", side_effect=swapped):
    try:
        _entry(p)
    except HarnessError:
        pass
    else:
        raise SystemExit("FIFO accepted")
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                                 str(Path(__file__).resolve().parents[1] / "src"), str(self.root / "fifo")],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
