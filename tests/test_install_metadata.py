"""Native ACL/xattr preservation and nonblocking installer-input regressions."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tomllib
import unittest
from unittest.mock import patch

from agent_harness import installation as install
import test_install as baseline


class InstallerMetadataTests(unittest.TestCase):
    setUp = baseline.InstallerTests.setUp
    make_plan = baseline.InstallerTests.make_plan
    apply = baseline.InstallerTests.apply

    def dispatch(self, command):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = install.dispatch(command, ["--package-root", str(self.package), "--codex-home", str(self.home),
                "--skills-home", str(self.skills), "--state-dir", str(self.state), "--apply"])
        return code, json.loads(stream.getvalue())

    def acl(self, path, *, default=False):
        if not sys.platform.startswith("linux") or not hasattr(os, "setxattr"):
            self.skipTest("Linux native xattrs/ACLs")
        value = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", tag, permission, identity)
            for tag, permission, identity in [(1, 7 if default else 6, 0xffffffff),
                (2, 5 if default else 4, os.getuid()), (4, 0, 0xffffffff),
                (16, 5 if default else 4, 0xffffffff), (32, 0, 0xffffffff)])
        try:
            os.setxattr(path, "system.posix_acl_default" if default else "system.posix_acl_access", value)
        except OSError as error:
            self.skipTest(f"Fixture filesystem lacks native POSIX ACL: {error}")

    def security(self, path):
        return install.metadata(path)["security"]

    def test_native_acl_xattr_mode_owner_survive_install_reinstall_and_rollback(self):
        config = self.home / "config.toml"
        self.acl(config)
        os.setxattr(config, "user.harness_test", b"preserve")
        before = self.security(config)
        self.apply()
        self.assertEqual(self.security(config), before)
        self.assertEqual(self.apply()["status"], "unchanged")
        self.assertEqual(self.security(config), before)
        self.assertEqual(self.dispatch("rollback")[0], 0)
        self.assertEqual(config.read_text(), self.original_config)
        self.assertEqual(self.security(config), before)

    def test_rollback_preserves_acl_added_after_install_and_user_byte_edits(self):
        self.apply()
        config = self.home / "config.toml"
        config.write_bytes(config.read_bytes() + b"\n[user]\nkeep=42\n")
        self.acl(config)
        os.setxattr(config, "user.harness_test", b"new-user-metadata")
        security = self.security(config)
        code, report = self.dispatch("rollback")
        self.assertEqual(code, 0, report)
        self.assertEqual(tomllib.loads(config.read_text()),
                         {**tomllib.loads(self.original_config), "user": {"keep": 42}})
        self.assertEqual(self.security(config), security)

    def test_unstaggable_metadata_blocks_whole_transaction_before_target_writes(self):
        config = self.home / "config.toml"
        self.acl(config)
        planned = self.make_plan()
        original = {p["path"]: install.read(p["path"]) for p in planned}
        native_set = os.setxattr

        def unsupported(path, name, value, *args, **kwargs):
            target = Path(os.readlink(f"/proc/self/fd/{path}")) if isinstance(path, int) else Path(path)
            if target.name.startswith(".harness-metadata-"):
                raise PermissionError("native ACL cannot be staged")
            return native_set(path, name, value, *args, **kwargs)

        with patch.object(os, "setxattr", side_effect=unsupported), self.assertRaises(PermissionError):
            install.apply_plan(planned, self.state, self.package)
        self.assertEqual(original, {path: install.read(path) for path in original})
        self.assertFalse((self.state / "installs").exists())

    def test_metadata_changed_after_plan_blocks_before_any_target_write(self):
        config = self.home / "config.toml"
        self.acl(config)
        planned = self.make_plan()
        original = {p["path"]: install.read(p["path"]) for p in planned}
        os.setxattr(config, "user.harness_test", b"concurrent")
        with self.assertRaisesRegex(install.InstallError, "Concurrent metadata"):
            install.apply_plan(planned, self.state, self.package)
        self.assertEqual(original, {path: install.read(path) for path in original})
        self.assertEqual(os.getxattr(config, "user.harness_test"), b"concurrent")

    def test_metadata_edit_during_install_is_preserved_while_prior_writes_compensate(self):
        config = self.home / "config.toml"
        self.acl(config)
        original_instructions = (self.home / "AGENTS.md").read_bytes()
        atomic = install.atomic

        def race(path, data):
            result = atomic(path, data)
            if path == self.home / "AGENTS.md":
                os.setxattr(config, "user.harness_test", b"concurrent")
            return result

        with patch.object(install, "atomic", side_effect=race), self.assertRaisesRegex(install.InstallError, "Concurrent metadata"):
            self.apply()
        self.assertEqual(config.read_text(), self.original_config)
        self.assertEqual(os.getxattr(config, "user.harness_test"), b"concurrent")
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), original_instructions)

    def test_metadata_edit_during_rollback_is_preserved_and_other_files_compensate(self):
        self.apply()
        config = self.home / "config.toml"
        self.acl(config)
        before = {Path(p["path"]): install.read(Path(p["path"])) for p in install.active_manifest(self.state)["files"]}
        unlink = install.ParentDirectory.unlink

        def race(parent, *args, **kwargs):
            path = parent.path
            result = unlink(parent, *args, **kwargs)
            if path == self.overlay:
                os.setxattr(config, "user.harness_test", b"concurrent")
            return result

        with patch.object(install.ParentDirectory, "unlink", new=race):
            code, report = self.dispatch("rollback")
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_compensated")
        self.assertEqual(before, {path: install.read(path) for path in before})
        self.assertEqual(os.getxattr(config, "user.harness_test"), b"concurrent")

    def test_late_foreign_metadata_is_never_adopted_as_install_ownership(self):
        instructions = self.home / "AGENTS.md"
        self.acl(instructions)
        planned = self.make_plan()
        original_config = (self.home / "config.toml").read_bytes()
        atomic = install.atomic

        def late_edit(path, data):
            result = atomic(path, data)
            if path == instructions:
                os.setxattr(path, "user.foreign-edit", b"preserve")
            return result

        with patch.object(install, "atomic", side_effect=late_edit), self.assertRaisesRegex(install.InstallError, "before recovery"):
            install.apply_plan(planned, self.state, self.package)
        self.assertEqual(os.getxattr(instructions, "user.foreign-edit"), b"preserve")
        owned = next(p for p in planned if p["path"] == instructions)["_applied_metadata"]
        self.assertNotIn("user.foreign-edit", owned["security"]["xattrs"])
        self.assertEqual((self.home / "config.toml").read_bytes(), original_config)
        manifest = json.loads(next((self.state / "installs").glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["status"], "failed_recovery_required")

    def test_atomic_checks_published_security_after_replace_and_keeps_foreign_edit(self):
        config = self.home / "config.toml"
        self.acl(config)
        before = config.read_bytes()
        replace = os.replace

        def late_edit(source, target, **kwargs):
            result = replace(source, target, **kwargs)
            if Path(target).name == config.name:
                os.setxattr(config, "user.foreign-edit", b"preserve")
            return result

        with patch.object(os, "replace", side_effect=late_edit), self.assertRaisesRegex(install.InstallError, "Published installation metadata changed"):
            install.atomic(config, before + b"# changed\n")
        self.assertEqual(os.getxattr(config, "user.foreign-edit"), b"preserve")

    def test_late_foreign_metadata_after_rollback_write_requires_recovery(self):
        config = self.home / "config.toml"
        self.acl(config)
        self.apply()
        atomic = install.atomic

        def late_edit(path, data):
            result = atomic(path, data)
            if path == config:
                os.setxattr(config, "user.foreign-edit", b"preserve")
            return result

        with patch.object(install, "atomic", side_effect=late_edit):
            code, report = self.dispatch("rollback")
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_recovery_required")
        self.assertIn({"path": str(config), "reason": "concurrent_metadata_edit_preserved"}, report["recovery_errors"])
        self.assertEqual(os.getxattr(config, "user.foreign-edit"), b"preserve")

    def test_late_foreign_metadata_on_published_pointer_is_preserved(self):
        config = self.home / "config.toml"
        self.acl(config)
        pointer = self.state / "active.json"
        atomic = install.atomic

        def late_edit(path, data):
            result = atomic(path, data)
            if path == pointer:
                os.setxattr(pointer, "user.foreign-edit", b"preserve")
                raise OSError("publication committed then wrapper failed")
            return result

        with patch.object(install, "atomic", side_effect=late_edit), self.assertRaisesRegex(install.InstallError, "active manifest pointer"):
            self.apply()
        self.assertTrue(pointer.exists())
        self.assertEqual(os.getxattr(pointer, "user.foreign-edit"), b"preserve")
        manifest = json.loads(next((self.state / "installs").glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["status"], "failed_recovery_required")

    def test_legacy_created_file_removal_requires_ordinary_security_baseline(self):
        target = self.home / "legacy-target"
        installed = self.base / "legacy-installed-copy"
        target.write_bytes(b"installed")
        installed.write_bytes(b"installed")
        target.chmod(0o600)
        self.acl(self.home / "config.toml")  # Require the Linux metadata contract.
        manifest = {"schema_version": 1, "files": [{"path": str(target), "installed_copy": str(installed),
            "backup": None, "before_sha256": None, "after_sha256": install.digest(b"installed"),
            "kind": "owned", "meta": {}}]}
        self.assertIsNone(install.rollback_plan(manifest)[0]["after"])
        target.chmod(0o640)
        with self.assertRaisesRegex(install.InstallError, "user metadata/ACLs"):
            install.rollback_plan(manifest)
        self.assertTrue(target.exists())
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        security = self.security(target)
        self.assertFalse(install.ordinary_legacy_metadata({**security, "mode": 0o600, "uid": security["uid"] + 1}))
        self.assertFalse(install.ordinary_legacy_metadata({**security, "mode": 0o600, "gid": security["gid"] + 1}))

    def test_rollback_refuses_to_remove_created_file_with_later_user_acl(self):
        self.apply()
        self.acl(self.overlay)
        before = {Path(p["path"]): install.read(Path(p["path"])) for p in install.active_manifest(self.state)["files"]}
        security = self.security(self.overlay)
        code, report = self.dispatch("rollback")
        self.assertEqual(code, 2, report)
        self.assertIn("user metadata/ACLs", report["error"])
        self.assertEqual(before, {path: install.read(path) for path in before})
        self.assertEqual(self.security(self.overlay), security)

    def test_compensation_recreates_deleted_file_with_exact_inherited_acl(self):
        self.state.mkdir()
        self.acl(self.state, default=True)
        self.apply()
        before = install.read(self.overlay)
        security = self.security(self.overlay)
        self.assertIn("system.posix_acl_access", security["xattrs"])
        atomic = install.atomic
        config = self.home / "config.toml"

        def fail_config(path, data):
            if path == config:
                raise OSError("injected failure after overlay deletion")
            return atomic(path, data)

        with patch.object(install, "atomic", side_effect=fail_config):
            code, report = self.dispatch("rollback")
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_compensated")
        self.assertEqual(install.read(self.overlay), before)
        self.assertEqual(self.security(self.overlay), security)

    @unittest.skipUnless(os.name == "nt", "Windows native DACL preservation")
    def test_windows_legacy_modern_protected_dacls_survive_install_and_compensation(self):
        from agent_harness import rename
        ctypes, wintypes, api = rename._windows_security()
        api.SetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, wintypes.WORD, wintypes.WORD]
        api.SetSecurityDescriptorControl.restype = wintypes.BOOL
        config = self.home / "config.toml"
        original = config.read_bytes()
        atomic = install.atomic

        def set_model(legacy, protected):
            current = ctypes.create_string_buffer(rename._windows_get_dacl(config))
            if legacy:
                self.assertTrue(api.SetSecurityDescriptorControl(current, 0x1000, 0x1000 if protected else 0))
                self.assertTrue(api.SetFileSecurityW(str(config), 4, current))
            else:
                present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
                self.assertTrue(api.GetSecurityDescriptorDacl(current, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
                self.assertEqual(0, api.SetNamedSecurityInfoW(str(config), 1,
                    4 | (0x80000000 if protected else 0x20000000), None, None, dacl, None))

        for legacy, protected in ((True, False), (True, True), (False, False), (False, True)):
            with self.subTest(legacy=legacy, protected=protected):
                set_model(legacy, protected)
                before = rename._windows_get_dacl(config)
                self.apply()
                self.assertEqual(before, rename._windows_get_dacl(config))
                self.assertEqual(self.apply()["status"], "unchanged")
                code, report = self.dispatch("rollback")
                self.assertEqual(code, 0, report)
                self.assertEqual(config.read_bytes(), original)
                self.assertEqual(before, rename._windows_get_dacl(config))

                def fail_pointer(path, data):
                    if path == self.home / "harness-machine.json":
                        raise OSError("injected failure after Windows config replacement")
                    return atomic(path, data)

                with patch.object(install, "atomic", side_effect=fail_pointer), self.assertRaises(OSError):
                    self.apply()
                self.assertEqual(config.read_bytes(), original)
                self.assertEqual(before, rename._windows_get_dacl(config))

    def test_installer_rejects_oversized_override_without_reading_it(self):
        override = self.home / "AGENTS.override.md"
        with override.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(install.InstallError, "byte limit"):
            self.make_plan()
        self.assertFalse(self.skills.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_fifo_override_blocks_install_and_doctor_promptly_without_mutation(self):
        os.mkfifo(self.home / "AGENTS.override.md")
        executable = Path(__file__).resolve().parents[1] / "tools/harness.py"
        original = (self.home / "AGENTS.md").read_bytes()
        for command in ("doctor", "install", "verify"):
            result = subprocess.run([sys.executable, "-B", str(executable), command,
                "--package-root", str(self.package), "--codex-home", str(self.home),
                "--skills-home", str(self.skills), "--state-dir", str(self.state), "--apply"],
                capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("regular file", json.loads(result.stdout)["error"])
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), original)
        self.assertFalse(self.skills.exists())


if __name__ == "__main__":
    unittest.main()
