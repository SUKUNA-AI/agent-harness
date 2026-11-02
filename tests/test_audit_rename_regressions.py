"""Native Linux metadata and portable pre-publication rename regressions."""
from contextlib import nullcontext
import array
import errno
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import rename
from agent_harness.core import HarnessError


class RenameFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "workspace"
        self.root.mkdir()
        for name in ("a.py", "b.py"):
            (self.root / name).write_bytes(b"target = 1\n")

    def plan(self):
        sources = {name: rename.capture(self.root, name) for name in ("a.py", "b.py")}
        edit = {"range": {"start": {"line": 0, "character": 0},
                          "end": {"line": 0, "character": 6}}, "newText": "renamed"}
        return rename.make_plan(self.root, sources, {name: [edit] for name in sources}, request={}, server={})

    def sources(self):
        return {name: ((self.root / name).read_bytes(), rename.identity((self.root / name).lstat()))
                for name in ("a.py", "b.py")}

    def cleanup_receipt(self, result):
        if result.get("recovery_receipt"):
            receipt = Path(result["recovery_receipt"])
            self.addCleanup(shutil.rmtree, receipt.parent, True)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux inode flags and POSIX ACLs")
class LinuxRenameMetadataTests(RenameFixture):
    def inode_flags(self, path, value=None, *, allow_unsupported=False):
        import fcntl
        flags = array.array("l", [0 if value is None else value])
        request = (0x80006601 if value is None else 0x40006602) | (flags.itemsize << 16)
        try:
            with path.open("rb") as source:
                fcntl.ioctl(source.fileno(), request, flags, True)
        except OSError as error:
            if error.errno in (errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL):
                if allow_unsupported and value is None:
                    return None
                self.skipTest("Fixture filesystem does not support this inode flag ioctl: " + str(error))
            raise
        return flags[0]

    def default_acl(self):
        # Use a mapped UID, including in single-UID test namespaces. The named
        # entry keeps this an actual extended ACL even when it names the owner.
        value = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", tag, permissions, uid)
            for tag, permissions, uid in ((1, 7, 0xffffffff), (2, 4, os.getuid()),
                (4, 0, 0xffffffff), (16, 5, 0xffffffff), (32, 0, 0xffffffff)))
        try:
            os.setxattr(self.root, "system.posix_acl_default", value)
        except OSError as error:
            if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
                self.skipTest("Fixture filesystem does not support POSIX ACLs: " + str(error))
            raise
        self.assertEqual(value, os.getxattr(self.root, "system.posix_acl_default"))
        return value

    def test_nodump_rejected_by_preview_and_apply_before_staging(self):
        target = self.root / "b.py"  # Even a later input must block all staging.
        original_flags = self.inode_flags(target)
        self.inode_flags(target, original_flags | 0x40)  # FS_NODUMP_FL
        self.addCleanup(self.inode_flags, target, original_flags)
        expected_flags = self.inode_flags(target)
        if not expected_flags & 0x40:
            self.skipTest("Fixture filesystem does not retain FS_NODUMP_FL")
        before = self.sources()
        with self.assertRaisesRegex(HarnessError, "Linux inode flags"):
            self.plan()
        # An older reviewed plan must not bypass the current apply preflight.
        with patch.object(rename, "_check_metadata"):
            old_plan = self.plan()
        with patch.object(rename.Parent, "stage") as stage, patch.object(rename.Parent, "replace") as replace:
            with self.assertRaisesRegex(HarnessError, "Linux inode flags"):
                rename.apply_plan(self.root, old_plan)
        stage.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(before, self.sources())
        self.assertEqual(expected_flags, self.inode_flags(target))
        self.assertFalse(list(self.root.glob(".agent-harness-rename-*")))

    def test_ordinary_linux_inode_and_extent_layout_flag_remain_supported(self):
        before = {name: self.inode_flags(self.root / name) for name in ("a.py", "b.py")}
        self.assertTrue(all(value & ~0x80000 == 0 for value in before.values()))
        result = rename.apply_plan(self.root, self.plan())
        self.cleanup_receipt(result)
        self.assertEqual("applied", result["status"])
        for name, flags in before.items():
            self.assertEqual(b"renamed = 1\n", (self.root / name).read_bytes())
            self.assertEqual(flags, self.inode_flags(self.root / name))

    def test_ioctl_reads_the_opened_inode_and_rejects_unexpected_io_errors(self):
        import fcntl
        target = self.root / "a.py"
        seen = []
        def extent_only(fd, request, flags, mutate):
            self.assertIsInstance(fd, int)
            self.assertEqual(target.stat().st_ino, os.fstat(fd).st_ino)
            self.assertEqual(0x80006601 | (flags.itemsize << 16), request)
            self.assertTrue(mutate)
            flags[0] = 0x80000
            seen.append(fd)
        with patch.object(fcntl, "ioctl", side_effect=extent_only):
            rename._check_metadata(target)
        self.assertEqual(1, len(seen))
        with patch.object(fcntl, "ioctl", side_effect=OSError(errno.EIO, "fixture ioctl failure")):
            with self.assertRaises(OSError):
                rename._check_metadata(target)

    def inherited_acl_case(self, *, after_backup):
        before = self.sources()
        expected_flags = {name: self.inode_flags(self.root / name, allow_unsupported=True) for name in before}
        self.assertTrue(all(not os.listxattr(self.root / name) for name in before))
        plan = self.plan()
        native_stage = rename.Parent.stage
        native_metadata = rename._check_linux_metadata
        rejected_sizes, acl, stages = [], [], []
        if not after_backup:
            acl.append(self.default_acl())

        def stage(parent, data, metadata):
            result = native_stage(parent, data, metadata)
            stages.append(result)
            if after_backup and len(stages) == 1:
                acl.append(self.default_acl())
            return result

        def inspect(fd):
            try:
                return native_metadata(fd)
            except HarnessError:
                rejected_sizes.append(os.fstat(fd).st_size)
                raise

        with patch.object(rename.Parent, "stage", stage), patch.object(rename, "_check_linux_metadata", inspect), \
                patch.object(rename.Parent, "replace") as replace:
            result = rename.apply_plan(self.root, plan)
        self.cleanup_receipt(result)
        self.assertEqual("failed_compensated", result["status"], result)
        self.assertIn("extended attributes/ACLs", result["error"])
        self.assertEqual([0], rejected_sizes, "Inherited ACL must be rejected before staging source bytes")
        self.assertEqual(1 if after_backup else 0, len(stages))
        replace.assert_not_called()
        self.assertEqual(before, self.sources())
        self.assertEqual(acl[0], os.getxattr(self.root, "system.posix_acl_default"))
        for name, flags in expected_flags.items():
            self.assertEqual(flags, self.inode_flags(self.root / name, allow_unsupported=True))
            self.assertEqual([], os.listxattr(self.root / name))
        self.assertFalse(list(self.root.glob(".agent-harness-rename-*")))
        receipt = json.loads(Path(result["recovery_receipt"]).read_text(encoding="utf-8"))
        self.assertEqual("failed_compensated", receipt["status"])

    def test_default_acl_on_backup_is_rejected_while_empty_without_changing_source(self):
        self.inherited_acl_case(after_backup=False)

    def test_default_acl_on_replacement_is_rejected_and_clean_backup_removed(self):
        self.inherited_acl_case(after_backup=True)


class PortableRenamePreflightTests(RenameFixture):
    def checked_apply(self, *, reject=None):
        events = []
        class FakeParent:
            def __init__(self, root, name):
                self.name, self.path = name, root / name
                self.windows_dacl_uncertain = False
                self.copies = []

            def stage(self, data, metadata):
                name = "backup" if not self.copies else "staged"
                self.copies.append(name)
                events.append(("stage", self.name, name))
                return name, dict(metadata)

            def verify_backup(self, name, expected, sha256, size):
                events.append(("verify", self.name, name))
                if reject == (self.name, name):
                    raise HarnessError("fixture unsupported staging metadata")

            def replace(self, name, **kwargs):
                events.append(("replace", self.name, name))
                raise HarnessError("fixture stop before first replacement")

            def unlink(self, name):
                pass

            def close(self):
                pass

        before = self.sources()
        with patch.object(rename, "_check_metadata"), patch.object(rename, "Parent", FakeParent), \
                patch.object(rename, "workspace_lock", side_effect=lambda root: nullcontext()):
            result = rename.apply_plan(self.root, self.plan())
        self.cleanup_receipt(result)
        self.assertEqual("failed_compensated", result["status"])
        self.assertEqual(before, self.sources())
        return events

    def test_every_backup_and_replacement_is_checked_before_first_publication(self):
        events = self.checked_apply()
        replacement = next(index for index, event in enumerate(events) if event[0] == "replace")
        self.assertEqual([("verify", name, copy) for name in ("a.py", "b.py") for copy in ("backup", "staged")],
                         [event for event in events[:replacement] if event[0] == "verify"])

    def test_either_copy_of_a_later_file_can_block_all_original_replacements(self):
        for copy in ("backup", "staged"):
            with self.subTest(copy=copy):
                events = self.checked_apply(reject=("b.py", copy))
                self.assertIn(("verify", "b.py", copy), events)
                self.assertFalse(any(event[0] == "replace" for event in events))


if __name__ == "__main__":
    unittest.main()
