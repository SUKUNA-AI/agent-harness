"""Frozen evidence delivery never grants access to the private state root."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError
from agent_harness.managed_review import _readonly_context


class ReviewReadContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "value.py").write_text("VALUE = 0\n")
        for args in (("init",), ("add", "."), ("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "source")):
            subprocess.run(["git", *args], cwd=self.source, check=True, capture_output=True)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.source, text=True).strip()
        (self.source / "value.py").write_text("VALUE = 42\n")
        self.attempt = self.base / "private" / "attempt"
        self.attempt.mkdir(parents=True)
        for name in ("candidate.json", "baseline.json", "toolchains.json"):
            (self.attempt / name).write_text("{}\n")
        self.logs = self.attempt / "checks" / "value"
        self.logs.mkdir(parents=True)
        (self.logs / "stdout.log").write_text("independent check passed\n")
        self.schema = self.base / "private" / "schema.json"
        self.schema.write_text('{"type":"object"}\n')
        self.supervisor = SimpleNamespace(state={"cwd": str(self.source), "revision": revision})
        self.run = SimpleNamespace(attempt_dir=lambda: self.attempt)

    def context(self):
        return _readonly_context(self.supervisor, self.run, self.schema, {"digest": "candidate-frozen"})

    def test_exact_check_files_and_diff_without_private_ownership_or_source_writes(self):
        (self.attempt / ".supervisor-root-owner.json").write_text("private ownership fixture")
        hidden = self.logs / ".root-file-stages"
        hidden.mkdir()
        (hidden / "private.stage").write_text("private stage fixture")
        before = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.source)
        files, payload = self.context()
        self.assertIn(self.logs / "stdout.log", files)
        self.assertIn(self.schema, files)
        self.assertTrue(all(not part.startswith(".") for path in files for part in path.relative_to(self.base).parts))
        manifest = json.loads(Path(payload["manifest"]).read_text())
        self.assertEqual(manifest["candidate_digest"], "candidate-frozen")
        difference = Path(payload["combined_diff"]).read_text()
        self.assertIn("-VALUE = 0", difference)
        self.assertIn("+VALUE = 42", difference)
        for entry in manifest["files"]:
            self.assertEqual(entry["sha256"], hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest())
        self.assertEqual(before, subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.source))
        self.assertEqual(self.context(), (files, payload))

    def test_changed_check_output_cannot_rebind_an_existing_frozen_manifest(self):
        self.context()
        (self.logs / "stdout.log").write_text("changed result")
        with self.assertRaisesRegex(HarnessError, "Frozen read-only review context changed"):
            self.context()

    def test_changed_source_cannot_reuse_the_combined_diff(self):
        self.context()
        (self.source / "value.py").write_text("VALUE = -1\n")
        with self.assertRaisesRegex(HarnessError, "Frozen combined review diff changed"):
            self.context()

    def test_external_symlink_is_not_a_read_context_grant(self):
        foreign = self.base / "foreign.txt"
        foreign.write_text("not an authorized review input")
        link = self.logs / "unexpected.log"
        try:
            link.symlink_to(foreign)
        except (OSError, NotImplementedError):
            self.skipTest("Host cannot create symlink fixture")
        with self.assertRaises((HarnessError, OSError, ValueError)):
            self.context()


if __name__ == "__main__":
    unittest.main()
