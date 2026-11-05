"""Regression coverage for rollback transactions and source-only diagnostics."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import tomllib

from agent_harness import installation as install
import test_install as baseline


class InstallAuditTests(unittest.TestCase):
    setUp = baseline.InstallerTests.setUp
    make_plan = baseline.InstallerTests.make_plan
    apply = baseline.InstallerTests.apply

    def dispatch(self, command="rollback", extra=()):
        args = ["--package-root", str(self.package), "--codex-home", str(self.home),
                "--skills-home", str(self.skills), "--state-dir", str(self.state), "--apply", *extra]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = install.dispatch(command, args)
        return code, json.loads(output.getvalue())

    def targets(self):
        paths = {Path(row["path"]) for manifest in install.manifest_chain(install.active_manifest(self.state)) for row in manifest["files"]}
        paths.add(self.state / "active.json")
        return {path: install.read(path) for path in paths}

    def assert_targets(self, before):
        self.assertEqual(before, {path: install.read(path) for path in before})

    def update_package(self):
        role = self.package / "adapters/codex/agents/delivery_owner.toml"
        role.write_bytes(role.read_bytes() + b"\n# updated package\n")
        self.apply()

    def race_before_config(self, command):
        self.apply()
        if command == "uninstall":
            self.update_package()
        before = self.targets()
        config = self.home / "config.toml"
        unlink = install.ParentDirectory.unlink
        raced = False

        def after_unlink(parent, *args, **kwargs):
            path = parent.path
            nonlocal raced
            value = unlink(parent, *args, **kwargs)
            if path == self.overlay and not raced:
                raced = True
                config.write_bytes(config.read_bytes() + b"\n[user_concurrent]\nkeep=42\n")
            return value

        with patch.object(install.ParentDirectory, "unlink", new=after_unlink):
            code, report = self.dispatch(command)
        self.assertTrue(raced)
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_compensated")
        before[config] += b"\n[user_concurrent]\nkeep=42\n"
        self.assert_targets(before)
        journal = json.loads(Path(report["journal"]).read_text(encoding="utf-8"))
        self.assertEqual(next(row["progress"] for row in journal["files"] if row["path"] == str(config)), "concurrent_edit_preserved")
        code, report = self.dispatch(command)
        self.assertEqual(code, 0, report)
        self.assertEqual(tomllib.loads(config.read_text(encoding="utf-8"))["user_concurrent"], {"keep": 42})
        self.assertEqual(self.apply()["status"], "written_pending_native_verification")
        self.assertEqual(self.apply()["status"], "unchanged")

    def test_rollback_rechecks_each_target_and_preserves_concurrent_user_edit(self):
        self.race_before_config("rollback")

    def test_uninstall_rechecks_targets_across_update_history(self):
        self.race_before_config("uninstall")

    def test_uninstall_does_not_adopt_edit_after_computing_restoration(self):
        self.apply()
        before = self.targets()
        config = self.home / "config.toml"
        rollback_plan = install.rollback_plan
        raced = False

        def edit_after_plan(manifest, current_files=None):
            nonlocal raced
            planned = rollback_plan(manifest, current_files)
            if current_files is not None and not raced:
                raced = True
                config.write_bytes(config.read_bytes() + b"\n[user_during_planning]\nkeep=42\n")
            return planned

        with patch.object(install, "rollback_plan", side_effect=edit_after_plan):
            code, report = self.dispatch("uninstall")
        self.assertTrue(raced)
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "blocked")
        before[config] += b"\n[user_during_planning]\nkeep=42\n"
        self.assert_targets(before)

    def test_config_io_failure_before_and_after_commit_compensates_and_can_retry(self):
        self.apply()
        before = self.targets()
        config = self.home / "config.toml"
        atomic = install.atomic
        for after_commit in (False, True):
            failed = False

            def fail_config(path, data):
                nonlocal failed
                if path == config and not failed:
                    failed = True
                    if after_commit:
                        atomic(path, data)
                    raise OSError("injected config write failure")
                return atomic(path, data)

            with self.subTest(after_commit=after_commit), patch.object(install, "atomic", side_effect=fail_config):
                code, report = self.dispatch()
            self.assertEqual(code, 2, report)
            self.assertEqual(report["status"], "rollback_failed_compensated")
            self.assertFalse(report["recovery_required"])
            self.assert_targets(before)
        self.assertEqual(self.dispatch()[0], 0)

    def test_compensation_failure_reports_targets_and_keeps_recovery_copies(self):
        self.apply()
        before = self.targets()
        config = self.home / "config.toml"
        atomic = install.atomic

        def fail_config_and_overlay_restore(path, data):
            if path in (config, self.overlay):
                raise OSError("injected unavailable target")
            return atomic(path, data)

        with patch.object(install, "atomic", side_effect=fail_config_and_overlay_restore):
            code, report = self.dispatch()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_recovery_required")
        self.assertTrue(report["recovery_required"])
        self.assertIn(str(self.overlay), [row["path"] for row in report["recovery_errors"]])
        journal = json.loads(Path(report["journal"]).read_text(encoding="utf-8"))
        row = next(row for row in journal["files"] if row["path"] == str(self.overlay))
        self.assertEqual(Path(row["before_copy"]).read_bytes(), before[self.overlay])
        self.assertEqual(install.digest(Path(row["before_copy"]).read_bytes()), row["before_sha256"])
        self.assertFalse(self.overlay.exists())
        self.assertEqual((self.state / "active.json").read_bytes(), before[self.state / "active.json"])
        code, report = self.dispatch(extra=("--manifest", report["source_manifest"]["manifest"]))
        self.assertEqual(code, 0, report)
        self.assertFalse((self.state / "active.json").exists())

    def test_compensation_preserves_user_edit_to_already_changed_target(self):
        self.apply()
        before = self.targets()
        config = self.home / "config.toml"
        user_overlay = install.canonical({**json.loads(before[self.overlay]), "user_note": "keep"})
        atomic = install.atomic

        def fail_after_external_edit(path, data):
            if path == config:
                self.overlay.write_bytes(user_overlay)
                raise OSError("injected failure after external overlay edit")
            return atomic(path, data)

        with patch.object(install, "atomic", side_effect=fail_after_external_edit):
            code, report = self.dispatch()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_recovery_required")
        self.assertEqual(self.overlay.read_bytes(), user_overlay)
        self.assertIn({"path": str(self.overlay), "reason": "concurrent_edit_preserved"}, report["recovery_errors"])
        before[self.overlay] = user_overlay
        self.assert_targets(before)
        self.assertEqual(self.dispatch()[0], 2)  # A retry must not erase the unresolved edit.
        self.assert_targets(before)

    def test_active_pointer_unlink_commits_then_raises_and_is_compensated(self):
        self.apply()
        before = self.targets()
        pointer = self.state / "active.json"
        unlink = install.ParentDirectory.unlink
        failed = False

        def fail_pointer(parent, *args, **kwargs):
            path = parent.path
            nonlocal failed
            value = unlink(parent, *args, **kwargs)
            if path == pointer and not failed:
                failed = True
                raise OSError("injected pointer removal failure")
            return value

        with patch.object(install.ParentDirectory, "unlink", new=fail_pointer):
            code, report = self.dispatch()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_compensated")
        self.assert_targets(before)

    def test_update_rollback_pointer_write_failure_restores_current_install(self):
        self.apply()
        self.update_package()
        before = self.targets()
        pointer = self.state / "active.json"
        atomic = install.atomic
        failed = False

        def fail_pointer(path, data):
            nonlocal failed
            value = atomic(path, data)
            if path == pointer and not failed:
                failed = True
                raise OSError("injected pointer publication failure")
            return value

        with patch.object(install, "atomic", side_effect=fail_pointer):
            code, report = self.dispatch()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_compensated")
        self.assert_targets(before)

    def test_progress_journal_failure_compensates_and_final_failure_is_explicit(self):
        self.apply()
        before = self.targets()
        atomic = install.atomic
        failed = False

        def fail_journal(path, data):
            nonlocal failed
            if path.name == "manifest.json" and "rollbacks" in path.parts:
                entry = json.loads(data)
                if failed or any(row["progress"] == "applied" for row in entry["files"]):
                    failed = True
                    raise OSError("injected journal unavailable")
            return atomic(path, data)

        with patch.object(install, "atomic", side_effect=fail_journal):
            code, report = self.dispatch()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_recovery_required")
        self.assertIn("recovery_progress_unrecorded", [row["reason"] for row in report["recovery_errors"]])
        self.assertTrue(Path(report["journal"]).is_file())
        self.assert_targets(before)

    def test_final_journal_failure_restores_pointer_and_all_targets(self):
        self.apply()
        before = self.targets()
        atomic = install.atomic

        def fail_commit(path, data):
            if path.name == "manifest.json" and "rollbacks" in path.parts and json.loads(data)["status"] == "rolled_back":
                atomic(path, data)
                raise OSError("injected final journal publication failure")
            return atomic(path, data)

        with patch.object(install, "atomic", side_effect=fail_commit):
            code, report = self.dispatch()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "rollback_failed_compensated")
        self.assert_targets(before)
        self.assertEqual(json.loads(Path(report["journal"]).read_text(encoding="utf-8"))["status"], report["status"])

    def test_active_reference_changed_while_planning_blocks_before_targets_change(self):
        self.apply()
        before = self.targets()
        pointer = self.state / "active.json"
        changed = b'{"external_change":"preserve"}\n'
        rollback_plan = install.rollback_plan

        def change_after_plan(*args, **kwargs):
            planned = rollback_plan(*args, **kwargs)
            pointer.write_bytes(changed)
            return planned

        with patch.object(install, "rollback_plan", side_effect=change_after_plan):
            code, report = self.dispatch()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "blocked")
        before[pointer] = changed
        self.assert_targets(before)

    def test_active_reference_changed_during_rollback_is_not_overwritten(self):
        self.apply()
        before = self.targets()
        pointer = self.state / "active.json"
        changed = b'{"external_change":"preserve"}\n'
        unlink = install.ParentDirectory.unlink

        def change_reference(parent, *args, **kwargs):
            path = parent.path
            value = unlink(parent, *args, **kwargs)
            if path == self.overlay:
                pointer.write_bytes(changed)
            return value

        with patch.object(install.ParentDirectory, "unlink", new=change_reference):
            code, report = self.dispatch()
        self.assertEqual(code, 2, report)
        before[pointer] = changed
        self.assert_targets(before)

    def link_config(self, contents=None):
        config = self.home / "config.toml"
        target = self.base / "linked-config.toml"
        if contents is not None:
            target.write_bytes(contents)
        config.unlink()
        try:
            config.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Native symlink unavailable: {type(error).__name__}")
        return config, target

    def test_existing_config_link_is_rejected_before_install_targets_change(self):
        original = (self.home / "config.toml").read_bytes()
        instructions = (self.home / "AGENTS.md").read_bytes()
        config, target = self.link_config(original)
        code, report = self.dispatch("install")
        self.assertEqual(code, 2, report)
        self.assertIn("symlink/reparse", report["error"])
        self.assertTrue(config.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), instructions)
        self.assertFalse(self.skills.exists())
        self.assertFalse((self.state / "active.json").exists())

    def test_dangling_config_link_is_not_adopted_as_missing_file(self):
        config, target = self.link_config()
        with self.assertRaisesRegex(install.InstallError, "symlink/reparse"):
            self.make_plan()
        self.assertTrue(config.is_symlink())
        self.assertFalse(target.exists())

    def test_link_inserted_after_install_plan_is_rejected_before_target_writes(self):
        planned = self.make_plan()
        original = (self.home / "config.toml").read_bytes()
        instructions = (self.home / "AGENTS.md").read_bytes()
        config, target = self.link_config(original)
        with self.assertRaisesRegex(install.InstallError, "symlink/reparse"):
            install.apply_plan(planned, self.state, self.package)
        self.assertTrue(config.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), instructions)

    def test_rollback_rejects_same_bytes_link_without_removing_other_targets(self):
        self.apply()
        before = self.targets()
        config, target = self.link_config(before[self.home / "config.toml"])
        code, report = self.dispatch()
        self.assertEqual(code, 2, report)
        self.assertIn("symlink/reparse", report["error"])
        self.assertTrue(config.is_symlink())
        self.assertEqual(target.read_bytes(), before[config])
        # Product reads now refuse links; inspect the intentionally retained
        # fixture link separately instead of asking that reader to follow it.
        del before[config]
        self.assert_targets(before)

    def test_windows_reparse_attribute_is_rejected_without_junction_api(self):
        config = self.home / "config.toml"
        original = config.read_bytes()
        lstat = Path.lstat

        def reparse(path, *args, **kwargs):
            value = lstat(path, *args, **kwargs)
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)) if path == config else value

        with patch.object(Path, "lstat", new=reparse), self.assertRaisesRegex(install.InstallError, "symlink/reparse"):
            self.make_plan()
        self.assertEqual(config.read_bytes(), original)

    def test_doctor_uses_declared_names_and_reports_collisions_without_mutating(self):
        roles = self.home / "agents"
        roles.mkdir()
        (roles / "critic.toml").write_text('name="independent_reviewer"\nmodel_reasoning_effort="high"\n', encoding="utf-8")
        (roles / "alias.toml").write_text('name="independent_reviewer"\ndeveloper_instructions="private-do-not-print"\n', encoding="utf-8")
        (roles / "missing.toml").write_text('model="role-model"\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in roles.iterdir()}
        report = install.doctor(self.package, self.home, self.skills, self.state)["model_configuration"]
        rows = {row["file_stem"]: row for row in report["agent_role_files"]}
        self.assertEqual(rows["critic"]["declared_name"], "independent_reviewer")
        self.assertEqual(rows["critic"]["name"], "independent_reviewer")
        self.assertIsNone(rows["missing"]["name"])
        self.assertIn("missing_or_invalid_declared_role_name", rows["missing"]["warnings"])
        self.assertEqual(report["declared_role_name_collisions"], [{"name": "independent_reviewer", "sources": [str(roles / "alias.toml"), str(roles / "critic.toml")]}])
        self.assertNotIn("private-do-not-print", json.dumps(report))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_doctor_inventories_child_defaults_and_referenced_configs_once(self):
        role = self.base / "outside-role.toml"
        role.write_text('model="referenced-model"\nmodel_reasoning_effort="medium"\nmodel_provider="declared-only"\napi_key="private-do-not-print"\n', encoding="utf-8")
        config = self.home / "config.toml"
        config.write_text('model="coordinator"\nmodel_auto_compact_token_limit=500000\n[agents]\ndefault_subagent_model="child-base"\ndefault_subagent_reasoning_effort="high"\n[agents.reader]\nconfig_file="../outside-role.toml"\n[profiles.legacy.agents]\ndefault_subagent_model="child-inline"\n[profiles.legacy.agents.reader]\nconfig_file="../outside-role.toml"\n', encoding="utf-8")
        (self.home / "deep.config.toml").write_text('[agents]\ndefault_subagent_model="child-profile"\n[agents.reader]\nconfig_file="../outside-role.toml"\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in (config, role, self.home / "deep.config.toml")}
        read_text = Path.read_text
        reads = 0

        def counted(path, *args, **kwargs):
            nonlocal reads
            if path.resolve() == role.resolve():
                reads += 1
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", new=counted):
            report = install.doctor(self.package, self.home, self.skills, self.state)["model_configuration"]
        self.assertEqual(reads, 1)
        self.assertEqual(report["base"]["agent_defaults"], {"default_subagent_model": "child-base", "default_subagent_reasoning_effort": "high"})
        self.assertEqual(report["profile_files"][0]["agent_defaults"]["default_subagent_model"], "child-profile")
        self.assertEqual(report["inline_profile_tables"][0]["agent_defaults"]["default_subagent_model"], "child-inline")
        reference = report["base"]["agent_role_references"][0]
        self.assertEqual(reference["declaring_source"], str(config))
        self.assertEqual(reference["declaring_table"], "agents.reader")
        self.assertEqual(reference["configuration"]["settings"]["model"], "referenced-model")
        self.assertEqual(report["base"]["settings"]["model_auto_compact_token_limit"], 500000)
        self.assertEqual(report["observation"], "files_only")
        self.assertEqual(report["effective_runtime"], "unknown_until_client_probe")
        self.assertNotIn("private-do-not-print", json.dumps(report))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_doctor_invalid_defaults_and_references_do_not_dump_values(self):
        config = self.home / "config.toml"
        config.write_text('[agents]\ndefault_subagent_model={secret="private-do-not-print"}\n[agents.bad]\nconfig_file={secret="private-do-not-print"}\n[agents.missing]\nconfig_file="missing-role.toml"\n', encoding="utf-8")
        report = install.doctor(self.package, self.home, self.skills, self.state)["model_configuration"]
        self.assertIn("agents.default_subagent_model", report["base"]["invalid_setting_types"])
        refs = report["base"]["agent_role_references"]
        self.assertEqual(refs[0]["status"], "invalid_config_file")
        self.assertEqual(refs[1]["configuration"]["status"], "unreadable")
        self.assertNotIn("private-do-not-print", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
