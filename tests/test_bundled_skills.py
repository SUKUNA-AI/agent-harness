from pathlib import Path
import shutil
import tempfile
import unittest
from agent_harness import installation as install


PACKAGE = Path(__file__).resolve().parents[1]


class BundledSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="harness-skills-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home, self.skills, self.state = [self.base / name for name in ("home", "skills", "state")]

    def plan(self):
        return install.plan(PACKAGE, self.home, self.skills, self.state, self.state / "machine.json")

    def test_bundled_skills_render_reinstall_and_rollback(self):
        planned = self.plan()
        self.assertFalse(self.home.exists(), "dry-run created target home")
        install.apply_plan(planned, self.state, PACKAGE)
        for folder in (PACKAGE / "skills").iterdir():
            installed = self.skills / folder.name / "SKILL.md"
            self.assertTrue(installed.is_file())
            text = installed.read_text(encoding="utf-8")
            self.assertNotIn("{{PACKAGE_ROOT}}", text)
            self.assertNotIn("{{MACHINE_POINTER}}", text)
        self.assertEqual([], self.plan())
        self.assertEqual("structural_verified", install.verify(PACKAGE, self.home, self.skills, self.state)["status"])
        rollback = install.rollback_plan(install.active_manifest(self.state))
        deleted = {p["path"] for p in rollback if p["after"] is None}
        for folder in (PACKAGE / "skills").iterdir():
            self.assertIn(self.skills / folder.name / "SKILL.md", deleted)

    def test_existing_foreign_skill_is_not_adopted(self):
        target = self.skills / "harness-skill-creator/SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("User-owned skill", encoding="utf-8")
        with self.assertRaises(install.InstallError):
            self.plan()
        self.assertEqual("User-owned skill", target.read_text(encoding="utf-8"))
        self.assertFalse(self.home.exists())

    def test_removed_skill_blocks_upgrade_without_deleting_installed_copy(self):
        package = self.base / "package"
        package.mkdir()
        for name in ("policies", "adapters", "skills"):
            shutil.copytree(PACKAGE / name, package / name)
        (package / "tools").mkdir()
        (package / "tools/harness.py").write_text("# fixture", encoding="utf-8")
        (package / "tools/session.py").write_text("# SessionStart fixture", encoding="utf-8")
        def plan():
            return install.plan(package, self.home, self.skills, self.state, self.state / "machine.json")
        install.apply_plan(plan(), self.state, package)
        target = self.skills / "harness-integrations/SKILL.md"
        original = target.read_bytes()
        (package / "skills/harness-integrations").rename(self.base / "removed-skill")
        with self.assertRaisesRegex(install.InstallError, "absent from this package"):
            plan()
        checked = install.verify(package, self.home, self.skills, self.state)
        self.assertEqual("failed", checked["status"])
        self.assertEqual(original, target.read_bytes())

    def test_edited_installed_skill_blocks_reinstall_and_rollback(self):
        install.apply_plan(self.plan(), self.state, PACKAGE)
        target = self.skills / "harness-integrations/SKILL.md"
        target.write_text("Manual edit", encoding="utf-8")
        with self.assertRaises(install.InstallError):
            self.plan()
        with self.assertRaises(install.InstallError):
            install.rollback_plan(install.active_manifest(self.state))
        self.assertEqual("Manual edit", target.read_text(encoding="utf-8"))
