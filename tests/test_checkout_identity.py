"""Ledger and workspace locks agree on physical Git checkout ownership."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, Run, checkout_identity, checkout_overlap


class CheckoutIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-checkout-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo"
        self.git("init", "-q", str(self.repo))
        self.first, self.second = self.repo / "src", self.repo / "tests"
        self.first.mkdir()
        self.second.mkdir()
        (self.repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "init.templateDir=", "-c",
                               "core.hooksPath=" + str(self.base / "no-hooks"),
                               "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                               *args], cwd=cwd, check=True, capture_output=True, text=True)

    def run_fixture(self, cwd=None, name="run"):
        return Run.create(self.base / name, {
            "schema_version": 1, "run_id": name, "task_id": "fixture", "attempt_id": "a1",
            "workflow": "change", "process_level": "standard", "goal": "Physical checkout ownership",
            "cwd": str(cwd or self.repo), "permissions": ["read", "write", "test"],
            "scope": {"write_paths": ["**"]},
            "checks": [{"id": "fixture", "kind": "command", "required": True, "argv": [sys.executable, "-I", "-B", "-c", "print('fixture')"], "timeout": 3}],
            "resource_lock_root": str(self.base / "locks"),
            "budget": {"native_cap": 4, "native_cap_source": "isolated fixture", "root_counts": True},
        })

    @staticmethod
    def owner(name, cwd):
        return {"owner_id": name, "lane_id": name, "native_ref": "fixture-" + name,
                "status": "running", "observation": "fixture receipt", "write_cwd": str(cwd)}

    def linked_worktree(self):
        self.git("add", "tracked.txt", cwd=self.repo)
        self.git("commit", "-qm", "fixture", cwd=self.repo)
        worktree = self.base / "linked-worktree"
        self.git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=self.repo)
        (worktree / "src").mkdir()
        return worktree

    def test_subdirectories_have_one_identity_without_git_subprocesses(self):
        with patch("subprocess.run", side_effect=AssertionError("No Git subprocess per identity")):
            self.assertEqual(self.repo, checkout_identity(self.first))
            self.assertEqual(self.repo, checkout_identity(self.second))
            self.assertTrue(checkout_overlap(self.first, self.second))

    def test_ledger_rejects_two_subdirectories_of_one_checkout(self):
        run = self.run_fixture()
        run.record_owner(self.owner("first", self.first))
        with self.assertRaisesRegex(HarnessError, "share a mutable checkout"):
            run.record_owner(self.owner("second", self.second))
        self.assertEqual({"first"}, set(run.state()["owners"]))

    def test_workspace_locks_conflict_between_subdirectories(self):
        left = self.run_fixture(self.first, "left")
        right = self.run_fixture(self.second, "right")
        with left.workspace_lock():
            with self.assertRaisesRegex(HarnessError, "Resource busy"):
                with right.workspace_lock():
                    self.fail("Subdirectory lock bypassed checkout ownership")
        with right.workspace_lock():
            pass

    def test_real_linked_worktrees_remain_independent(self):
        worktree = self.linked_worktree()
        self.assertTrue((worktree / ".git").is_file())
        self.assertEqual(worktree, checkout_identity(worktree / "src"))
        self.assertFalse(checkout_overlap(self.first, worktree / "src"))
        run = self.run_fixture()
        run.record_owner(self.owner("main", self.first))
        run.record_owner(self.owner("linked", worktree / "src"))
        other = self.run_fixture(worktree / "src", "other")
        with run.workspace_lock():
            with other.workspace_lock():
                self.assertEqual({"main", "linked"}, set(run.state()["owners"]))

    def test_parent_and_nested_git_checkout_overlap_physically(self):
        nested = self.first / "nested"
        self.git("init", "-q", str(nested))
        self.assertEqual(nested, checkout_identity(nested))
        self.assertTrue(checkout_overlap(self.first, nested))
        self.assertTrue(checkout_overlap(self.base, nested))
        run = self.run_fixture()
        run.record_owner(self.owner("parent", self.base))
        with self.assertRaisesRegex(HarnessError, "share a mutable checkout"):
            run.record_owner(self.owner("nested", nested))

    def test_non_git_parent_child_overlap_and_siblings_do_not(self):
        non_git = self.base / "non-git"
        left, right = non_git / "left", non_git / "right"
        left.mkdir(parents=True)
        right.mkdir()
        self.assertEqual(non_git, checkout_identity(non_git))
        self.assertTrue(checkout_overlap(non_git, left))
        self.assertFalse(checkout_overlap(left, right))
        run = self.run_fixture()
        run.record_owner(self.owner("parent", non_git))
        with self.assertRaisesRegex(HarnessError, "share a mutable checkout"):
            run.record_owner(self.owner("child", left))

    def test_empty_git_placeholder_is_not_a_checkout(self):
        outside = self.base / "placeholder"
        (outside / ".git").mkdir(parents=True)
        child = outside / "child"
        child.mkdir()
        self.assertEqual(child, checkout_identity(child))
        self.assertNotEqual(outside, checkout_identity(child))

    def test_symlink_alias_uses_the_same_identity_and_lock(self):
        alias = self.base / "repo-alias"
        try:
            alias.symlink_to(self.repo, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks unavailable: {exc}")
        self.assertEqual(self.repo, checkout_identity(alias / "src"))
        run = self.run_fixture(self.first)
        other = self.run_fixture(alias / "tests", "alias")
        with run.workspace_lock():
            with self.assertRaisesRegex(HarnessError, "Resource busy"):
                with other.workspace_lock():
                    self.fail("Symlink alias bypassed checkout ownership")

    @unittest.skipUnless(os.name == "nt", "Windows filesystem case aliases")
    def test_windows_case_aliases_use_the_same_lock(self):
        run = self.run_fixture(self.first)
        other = self.run_fixture(Path(str(self.second).upper()), "case-alias")
        with run.workspace_lock():
            with self.assertRaisesRegex(HarnessError, "Resource busy"):
                with other.workspace_lock():
                    self.fail("Path case bypassed checkout ownership")


if __name__ == "__main__":
    unittest.main()
