from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import tomllib

from agent_harness import installation as install


class ConfigPatchTests(unittest.TestCase):
    def test_unrelated_bytes_comments_and_security_preserved(self):
        original = '# user comment\r\nmodel = "user-model"\r\nsandbox_mode = "read-only"\r\n[opaque]\r\nfuture = [1, 2]\r\n'
        changed, meta = install.patch_config(original)
        self.assertTrue(changed.startswith(original))
        self.assertEqual(tomllib.loads(changed)["opaque"], {"future": [1, 2]})
        self.assertEqual(tomllib.loads(changed)["sandbox_mode"], "read-only")
        self.assertEqual(install.revert_config(original, changed, changed, meta), original)

    def test_existing_agents_stricter_cap_and_role_preserved(self):
        text = '[agents]\nmax_concurrent_threads_per_session = 2 # user cap\ninterrupt_message = false\n[agents.my_role]\ndescription = "mine"\n'
        changed, _ = install.patch_config(text)
        self.assertIn("max_concurrent_threads_per_session = 2 # user cap", changed)
        self.assertEqual(tomllib.loads(changed)["agents"]["my_role"]["description"], "mine")
        self.assertFalse(tomllib.loads(changed)["agents"]["interrupt_message"])
        self.assertEqual(changed.count("[agents]"), 1)

    def test_cap_reduced_without_changing_unknown_fields(self):
        text = '[agents]\nmax_concurrent_threads_per_session = 9 # count\nfuture = "preserve"\n'
        changed, meta = install.patch_config(text)
        self.assertIn("= 4 # count", changed)
        self.assertEqual(tomllib.loads(changed)["agents"]["future"], "preserve")
        edited = changed + '\n[unrelated]\nvalue = 99\n'
        restored = install.revert_config(text, changed, edited, meta)
        self.assertEqual(tomllib.loads(restored)["agents"]["max_concurrent_threads_per_session"], 9)
        self.assertIn("value = 99", restored)

    def test_invalid_or_explicit_disable_fail_closed(self):
        for text in ("invalid=[", "[agents]\nenabled=false\n", "[agents]\nmax_threads=0\n", "[agents]\nenabled=42\n"):
            with self.subTest(text=text), self.assertRaises(install.InstallError):
                install.patch_config(text)

    def test_inline_table_not_silently_reformatted(self):
        with self.assertRaises(install.InstallError):
            install.patch_config('agents = { enabled = true }\n')

    def test_dotted_keys_and_legacy_cap(self):
        for text in ('agents.enabled = true\n[other]\nx=1\n', 'agents.max_threads = 2\n[other]\nx=1\n'):
            changed, _ = install.patch_config(text)
            data = tomllib.loads(changed)
            self.assertTrue(data["agents"]["enabled"])
            self.assertEqual(data["agents"]["max_concurrent_threads_per_session"], 2 if "max_threads" in text else 4)
            self.assertEqual(data["other"], {"x": 1})

    def test_manual_edit_in_managed_key_is_conflict(self):
        before = "model='mine'\n"
        installed, meta = install.patch_config(before)
        current = installed.replace("enabled = true", "enabled = false")
        with self.assertRaises(install.InstallError):
            install.revert_config(before, installed, current, meta)

    def test_preserve_user_addition_to_created_agents_section(self):
        before = 'model="mine"\n'
        installed, meta = install.patch_config(before)
        current = installed + 'new_user_key = "keep"\n'
        restored = install.revert_config(before, installed, current, meta)
        parsed = tomllib.loads(restored)
        self.assertEqual(parsed["agents"], {"new_user_key": "keep"})

    def test_explicit_five_children_and_default_stricter_cap(self):
        text = '[agents]\nmax_concurrent_threads_per_session=4\n'
        changed, meta = install.patch_config(text, preferred_cap=5, explicit_cap=True)
        self.assertEqual(tomllib.loads(changed)["agents"]["max_concurrent_threads_per_session"], 5)
        self.assertEqual(install.revert_config(text, changed, changed, meta), text)
        stricter, _ = install.patch_config(text, preferred_cap=5)
        self.assertEqual(tomllib.loads(stricter)["agents"]["max_concurrent_threads_per_session"], 4)
        for bad in (0, -1, True, "5"):
            with self.subTest(bad=bad), self.assertRaises(install.InstallError):
                install.patch_config(text, preferred_cap=bad, explicit_cap=True)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="harness пробелы ")
        self.addCleanup(self.tmp.cleanup)
        # tempfile on Windows may return an 8.3 alias; CLI resolves paths.
        self.base = Path(self.tmp.name).resolve()
        self.package = self.base / "переносимый пакет"
        self.package.mkdir()
        source = Path(__file__).resolve().parents[1]
        for name in ("policies", "adapters", "workflows", "roles", "skills"):
            shutil.copytree(source / name, self.package / name)
        (self.package / "tools").mkdir()
        (self.package / "tools/harness.py").write_text("# Installation-only fixture, no native execution claimed\n", encoding="utf-8")
        self.home = self.base / "профиль .codex"
        self.skills = self.base / "skills"
        self.state = self.base / "private state"
        self.overlay = self.state / "machine.json"
        self.home.mkdir()
        self.original_config = '# user\nmodel = "mine"\n[future]\nvalue = "preserve"\n'
        (self.home / "config.toml").write_text(self.original_config, encoding="utf-8")
        (self.home / "AGENTS.md").write_text("# User\nСохранить пользовательские инструкции.\n", encoding="utf-8")

    def make_plan(self):
        return install.plan(self.package, self.home, self.skills, self.state, self.overlay)

    def apply(self):
        with install.lock(self.state):
            return install.apply_plan(self.make_plan(), self.state, self.package)

    def test_dry_run_writes_no_targets(self):
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        planned = self.make_plan()
        self.assertGreater(len(planned), 5)
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
        self.assertFalse(self.skills.exists())

    def test_install_reinstall_structural_verify_and_exact_rollback(self):
        original = {str(p): p.read_bytes() for p in self.home.iterdir() if p.is_file()}
        result = self.apply()
        self.assertEqual(result["status"], "written_pending_native_verification")
        first = {str(p): p.read_bytes() for root in (self.home, self.skills) for p in root.rglob("*") if p.is_file()}
        self.assertEqual(self.make_plan(), [])
        repeated = self.apply()
        self.assertEqual(repeated["status"], "unchanged")
        self.assertEqual(first, {str(p): p.read_bytes() for root in (self.home, self.skills) for p in root.rglob("*") if p.is_file()})
        checked = install.verify(self.package.resolve(), self.home, self.skills, self.state)
        self.assertEqual(checked["status"], "structural_verified", checked)
        self.assertEqual(checked["native"]["status"], "not_run")
        self.assertEqual(checked["hosted_work"], "not_configured_by_local_files")
        manifest = install.active_manifest(self.state)
        planned = install.rollback_plan(manifest)
        for p in planned:
            if p["after"] is None:
                p["path"].unlink()
            else:
                install.atomic(p["path"], p["after"])
        self.assertEqual(original, {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()})

    def test_nonempty_override_is_active_and_base_preserved(self):
        base = (self.home / "AGENTS.md").read_bytes()
        (self.home / "AGENTS.override.md").write_text("# Override\n", encoding="utf-8")
        self.apply()
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), base)
        self.assertIn(install.START, (self.home / "AGENTS.override.md").read_text(encoding="utf-8"))

    def test_empty_override_does_not_shadow_base(self):
        (self.home / "AGENTS.override.md").write_text(" \n", encoding="utf-8")
        self.apply()
        self.assertIn(install.START, (self.home / "AGENTS.md").read_text(encoding="utf-8"))

    def test_user_config_and_instruction_edits_preserved_on_rollback(self):
        self.apply()
        config = self.home / "config.toml"
        config.write_text(config.read_text(encoding="utf-8") + '\n[added_by_user]\nkeep=42\n', encoding="utf-8")
        instructions = self.home / "AGENTS.md"
        instructions.write_text(instructions.read_text(encoding="utf-8") + "\nНовая пользовательская инструкция.\n", encoding="utf-8")
        result = install.rollback_plan(install.active_manifest(self.state))
        by_path = {p["path"]: p["after"] for p in result}
        self.assertIn(b"keep=42", by_path[config])
        self.assertIn("Новая пользовательская инструкция.", by_path[instructions].decode("utf-8"))
        self.assertNotIn(install.START, by_path[instructions].decode("utf-8"))

    def test_manual_managed_instruction_edit_blocks_all_rollback_writes(self):
        self.apply()
        p = self.home / "AGENTS.md"
        p.write_text(p.read_text(encoding="utf-8").replace(f"HARNESS_POLICY_VERSION={install.VERSION}", "HARNESS_POLICY_VERSION=user"), encoding="utf-8")
        before = {str(f): f.read_bytes() for f in self.home.rglob("*") if f.is_file()}
        with self.assertRaises(install.InstallError):
            install.rollback_plan(install.active_manifest(self.state))
        self.assertEqual(before, {str(f): f.read_bytes() for f in self.home.rglob("*") if f.is_file()})

    def test_existing_role_not_overwritten(self):
        p = self.home / "agents/delivery_owner.toml"
        p.parent.mkdir()
        p.write_text('name="mine"', encoding="utf-8")
        with self.assertRaises(install.InstallError):
            self.make_plan()
        self.assertEqual(p.read_text(encoding="utf-8"), 'name="mine"')

    def test_invalid_config_original_unchanged(self):
        p = self.home / "config.toml"
        p.write_text("[broken", encoding="utf-8")
        with self.assertRaises(install.InstallError):
            self.make_plan()
        self.assertEqual(p.read_bytes(), b"[broken")

    def test_instruction_limit_and_malformed_markers_fail_closed(self):
        p = self.home / "config.toml"
        p.write_text("project_doc_max_bytes=30\n", encoding="utf-8")
        with self.assertRaises(install.InstallError):
            self.make_plan()
        p.write_text(self.original_config, encoding="utf-8")
        (self.home / "AGENTS.md").write_text(install.START, encoding="utf-8")
        with self.assertRaises(install.InstallError):
            self.make_plan()

    def test_concurrent_edit_and_writer_lock(self):
        planned = self.make_plan()
        (self.home / "AGENTS.md").write_text("concurrent edit", encoding="utf-8")
        with self.assertRaises(install.InstallError):
            install.apply_plan(planned, self.state, self.package)
        with install.lock(self.state), self.assertRaises(install.InstallError):
            with install.lock(self.state):
                self.fail("second writer acquired lock")

    def test_backup_tamper_is_not_accepted(self):
        self.apply()
        manifest = install.active_manifest(self.state)
        backup = next(p["backup"] for p in manifest["files"] if p["backup"])
        Path(backup).write_bytes(b"tampered")
        with self.assertRaises(install.InstallError):
            install.rollback_plan(manifest)

    def test_utf8_bom_and_crlf_preserved(self):
        p = self.home / "config.toml"
        original = b'\xef\xbb\xbf# user\r\nmodel="mine"\r\n'
        p.write_bytes(original)
        self.apply()
        self.assertTrue(p.read_bytes().startswith(original))
        restored = {q["path"]: q["after"] for q in install.rollback_plan(install.active_manifest(self.state))}
        self.assertEqual(restored[p], original)

    def test_modified_role_is_not_structural_verified(self):
        self.apply()
        p = self.home / "agents/delivery_owner.toml"
        p.write_text(p.read_text(encoding="utf-8").replace("Own implementation", "Ignore implementation"), encoding="utf-8")
        result = install.verify(self.package.resolve(), self.home, self.skills, self.state)
        self.assertEqual(result["status"], "failed")

    def test_uninstall_unwinds_updates_preserving_user_fields(self):
        self.apply()
        source = self.package / "adapters/codex/agents/delivery_owner.toml"
        source.write_text(source.read_text(encoding="utf-8") + "\n# package update\n", encoding="utf-8")
        self.apply()
        config = self.home / "config.toml"
        config.write_text(config.read_text(encoding="utf-8") + '\n[user_added]\nkeep=12\n', encoding="utf-8")
        args = ["--package-root", str(self.package), "--codex-home", str(self.home), "--skills-home", str(self.skills), "--state-dir", str(self.state), "--apply"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = install.dispatch("uninstall", args)
        self.assertEqual(result, 0, output.getvalue())
        self.assertFalse((self.home / "agents/delivery_owner.toml").exists())
        self.assertNotIn(install.START, (self.home / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertEqual(tomllib.loads(config.read_text(encoding="utf-8"))["user_added"], {"keep": 12})
        self.assertFalse((self.state / "active.json").exists())

    def test_diff_does_not_expose_secret(self):
        diff = install.redacted_diff(Path("config.toml"), b"api_key='secret-value'\n", b"api_key='new-secret'\n")
        self.assertNotIn("secret-value", diff)
        self.assertNotIn("new-secret", diff)

    def test_cli_dry_run_and_invalid_config_exit(self):
        args = ["--package-root", str(self.package), "--codex-home", str(self.home), "--skills-home", str(self.skills), "--state-dir", str(self.state)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = install.dispatch("install", args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "dry_run")
        (self.home / "config.toml").write_text("[broken", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = install.dispatch("install", args + ["--apply"])
        self.assertEqual(code, 2)
        self.assertFalse(self.skills.exists())

    def test_explicit_five_persists_plain_reinstall_and_rolls_back(self):
        self.apply()
        original_cap = tomllib.loads((self.home / "config.toml").read_text(encoding="utf-8"))["agents"]["max_concurrent_threads_per_session"]
        self.assertEqual(original_cap, 4)
        planned = install.plan(self.package, self.home, self.skills, self.state, self.overlay, max_child_threads=5)
        install.apply_plan(planned, self.state, self.package)
        self.assertEqual(tomllib.loads((self.home / "config.toml").read_text(encoding="utf-8"))["agents"]["max_concurrent_threads_per_session"], 5)
        self.assertEqual(json.loads(self.overlay.read_text(encoding="utf-8"))["preferred_native_child_cap"], 5)
        self.assertEqual(self.make_plan(), [])
        checked = install.verify(self.package.resolve(), self.home, self.skills, self.state)
        self.assertEqual(checked["status"], "structural_verified", checked)
        restored = {p["path"]: p["after"] for p in install.rollback_plan(install.active_manifest(self.state))}
        self.assertEqual(tomllib.loads(restored[self.home / "config.toml"].decode("utf-8"))["agents"]["max_concurrent_threads_per_session"], 4)
        self.assertEqual(json.loads(restored[self.overlay])["preferred_native_child_cap"], 4)

    def test_preferred_five_does_not_raise_later_user_stricter_cap(self):
        self.apply()
        obj = json.loads(self.overlay.read_text(encoding="utf-8"))
        obj["preferred_native_child_cap"] = 5
        self.overlay.write_bytes(install.canonical(obj))
        config = self.home / "config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace("max_concurrent_threads_per_session = 4", "max_concurrent_threads_per_session = 2"), encoding="utf-8")
        planned = self.make_plan()
        self.assertFalse(any(p["path"] == config for p in planned))
        self.assertEqual(tomllib.loads(config.read_text(encoding="utf-8"))["agents"]["max_concurrent_threads_per_session"], 2)

    def test_machine_json_field_rollback_preserves_user_capabilities(self):
        self.apply()
        machine = json.loads(self.overlay.read_text(encoding="utf-8"))
        machine["user_capability"] = {"keep": "yes"}
        self.overlay.write_bytes(install.canonical(machine))
        install.apply_plan(install.plan(self.package, self.home, self.skills, self.state, self.overlay, max_child_threads=5), self.state, self.package)
        machine = json.loads(self.overlay.read_text(encoding="utf-8"))
        machine["user_added_after"] = 12
        self.overlay.write_bytes(install.canonical(machine))
        planned = install.rollback_plan(install.active_manifest(self.state))
        restored = json.loads(next(p["after"] for p in planned if p["path"] == self.overlay))
        self.assertEqual(restored["preferred_native_child_cap"], 4)
        self.assertEqual(restored["user_capability"], {"keep": "yes"})
        self.assertEqual(restored["user_added_after"], 12)

    def test_cli_default_state_discovered_from_installed_pointer(self):
        self.apply()
        args = ["--package-root", str(self.package), "--codex-home", str(self.home), "--skills-home", str(self.skills)]
        for command in ("doctor", "verify", "rollback"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = install.dispatch(command, args)
            self.assertEqual(code, 0, out.getvalue())

            obj = json.loads(out.getvalue())
            if command == "doctor":
                self.assertEqual(Path(obj["state_dir"]), self.state.resolve())
            elif command == "verify":
                self.assertEqual(obj["status"], "structural_verified")
            else:
                self.assertEqual(obj["status"], "rollback_dry_run")
        # Also discover the initial 1.0.0 pointer using its verified manifest parent.
        pointer = self.home / "harness-machine.json"
        data = json.loads(pointer.read_text(encoding="utf-8")); data.pop("installation_state")
        pointer.write_bytes(install.canonical(data))
        machine = json.loads(self.overlay.read_text(encoding="utf-8")); machine.pop("installation_state")
        self.overlay.write_bytes(install.canonical(machine))
        state, overlay = install.installed_locations(self.package, self.home)
        self.assertEqual(state, self.state.resolve())
        self.assertEqual(overlay, self.overlay.resolve())

    def test_relocated_package_updates_overlay_and_rolls_back(self):
        self.apply()
        before = json.loads(self.overlay.read_text(encoding="utf-8"))
        before["user_capability"] = "keep"
        self.overlay.write_bytes(install.canonical(before))
        relocated = self.base / "новый пакет"
        shutil.copytree(self.package, relocated)
        planned = install.plan(relocated, self.home, self.skills, self.state, self.overlay)
        install.apply_plan(planned, self.state, relocated)
        after = json.loads(self.overlay.read_text(encoding="utf-8"))
        self.assertEqual(after["package_root"], str(relocated))
        self.assertEqual(after["codex_home"], str(self.home))
        self.assertEqual(after["user_capability"], "keep")
        self.assertEqual(install.verify(relocated, self.home, self.skills, self.state)["status"], "structural_verified")
        rollback = install.rollback_plan(install.active_manifest(self.state))
        restored = json.loads(next(row["after"] for row in rollback if row["path"] == self.overlay))
        self.assertEqual(restored, before)

    def test_cli_rejects_invalid_child_cap_before_writes(self):
        args = ["--package-root", str(self.package), "--codex-home", str(self.home), "--skills-home", str(self.skills), "--state-dir", str(self.state), "--max-child-threads", "0", "--apply"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install.dispatch("install", args)
        self.assertEqual(code, 2)
        self.assertFalse((self.state / "active.json").exists())


if __name__ == "__main__":
    unittest.main()
