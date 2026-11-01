"""Exercise all six work commands through separate real CLI processes."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.strict_yaml import dump
from test_work_supervisor import FAKE


class WorkCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "calc.py").write_text("VALUE = 0\n")
        self.fake = self.root / "fake.py"
        self.fake.write_text(FAKE)
        self.run_dir = self.root / "private" / "run"
        self.environment = {**os.environ, "CODEX_HOME": str(self.root / "codex-home")}
        self.tool = Path(__file__).resolve().parents[1] / "tools" / "harness.py"

    def argv(self, action, *extra):
        return [sys.executable, "-I", "-B", str(self.tool), "work", action,
                "--run-dir", str(self.run_dir), "--format", "json", *extra]

    def command(self, action, *extra, expected=0):
        result = subprocess.run(self.argv(action, *extra), cwd=self.source,
            env=self.environment, capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def start_args(self, objective):
        task = self.root / "task.yaml"
        dump(task, {"schema_version": 1, "task": {"id": "cli", "objective": objective,
            "allowed_write_scopes": ["calc.py"]}, "budgets": {"max_attempts": 2, "max_wall_seconds": 90},
            "acceptance": [{"id": "value", "kind": "command", "required": True,
                "argv": [sys.executable, "-c", "from pathlib import Path; assert 'VALUE = 42' in Path('calc.py').read_text()"], "timeout": 15}]})
        return ["--contract", str(task), "--cwd", str(self.source), "--codex", str(self.fake),
                "--state-dir", str(self.root / "state")]

    def test_cli_repair_status_watch_report_and_accepted_resume(self):
        result = self.command("start", *self.start_args("repair"))
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(self.fake.with_suffix(".calls").read_text().splitlines(), ["exec", "resume"])
        status = self.command("status", "--summary", "--verify-evidence")
        self.assertTrue(status["evidence_current"])
        self.assertEqual(status["required_passed"], status["required_total"])
        self.assertEqual(self.command("watch", "--interval", "0.1", "--timeout", "2")["status"], "ACCEPTED")
        self.assertTrue(self.command("report")["evidence_current"])
        self.assertEqual(self.command("resume", "--codex", str(self.fake))["status"], "ACCEPTED")
        self.assertEqual(len(self.fake.with_suffix(".calls").read_text().splitlines()), 2)

    def test_cli_stop_cancels_live_owned_process_and_watch_reports_failure(self):
        process = subprocess.Popen(self.argv("start", *self.start_args("slow")), cwd=self.source,
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            deadline = time.monotonic() + 15
            while not self.fake.with_suffix(".calls").exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.fake.with_suffix(".calls").exists())
            self.command("stop", "--reason", "contained CLI cancellation test")
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 1, stdout + stderr)
            self.assertEqual(json.loads(stdout)["status"], "CANCELLED")
            self.assertEqual(self.command("watch", "--timeout", "2", expected=1)["status"], "CANCELLED")
            self.assertEqual((self.source / "calc.py").read_text(), "VALUE = 0\n")
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
