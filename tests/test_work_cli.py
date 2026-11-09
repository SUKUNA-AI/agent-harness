"""Exercise all six work commands through separate real CLI processes."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.strict_yaml import dump
from test_work_supervisor import FAKE
from unittest.mock import patch


class WorkCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "calc.py").write_text("VALUE = 0\n")
        self.fake = self.root / "fake.py"
        slow = "if mode=='slow':time.sleep(3)"
        self.assertEqual(FAKE.count(slow), 1)
        # This scenario cancels a live owned child. Natural completion after
        # three seconds races slower Windows CLI startup and cancellation IO.
        self.fake.write_text(FAKE.replace(slow, "if mode=='slow':\n while True:time.sleep(0.05)"), encoding="utf-8")
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
            self.assertEqual(json.loads(stdout)["status"], "CANCELLED", stdout + stderr)
            self.assertEqual(self.command("watch", "--timeout", "2", expected=1)["status"], "CANCELLED")
            self.assertEqual((self.source / "calc.py").read_text(), "VALUE = 0\n")
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)

    def test_cli_cancellation_fixture_remains_live_during_a_slow_stop_request(self):
        original = self.command
        def delayed_stop(action, *args, **options):
            if action == "stop":
                time.sleep(4)
            return original(action, *args, **options)
        with patch.object(self, "command", delayed_stop):
            self.test_cli_stop_cancels_live_owned_process_and_watch_reports_failure()

    def test_stop_snapshot_serializes_separate_process_checkpoint_after_validation(self):
        self.check_stop_snapshot_publication("after_validation")

    def test_stop_snapshot_serializes_separate_process_checkpoint_before_read(self):
        self.check_stop_snapshot_publication("before_read")

    def check_stop_snapshot_publication(self, boundary):
        """A real writer probes the kernel guard during stop's validated read."""
        from agent_harness.core import process_identity
        from agent_harness.file_io import ParentDirectory
        from agent_harness.supervisor import Supervisor
        self.start_args("slow")
        owner = Supervisor.create(self.root / "task.yaml", run_dir=self.run_dir,
            cwd=self.source, state_dir=self.root / "state")
        state = {**owner.state, "generation": owner.state["generation"] + 1}
        payload = self.root / "writer-input.json"
        payload.write_text(json.dumps(state), encoding="utf-8")
        reader = Supervisor(self.run_dir)
        paused, release, probed = (threading.Event() for _ in range(3))
        errors, results, lines = [], [], []
        decode = reader.store._decode_events
        def pause():
            paused.set()
            if not release.wait(15):
                raise AssertionError("snapshot reader barrier not released")
        def validated(raw):
            records = decode(raw)
            pause()
            return records
        opening = ParentDirectory.open
        def before_read(parent, *args, **kwargs):
            if parent.path.name == "events.jsonl" and threading.current_thread() is stopping:
                pause()
            return opening(parent, *args, **kwargs)
        def stop():
            try:
                results.append(reader.stop("deterministic concurrent publication"))
            except BaseException as error:
                errors.append(error)
        code = r'''
from contextlib import contextmanager
import json,sys
from pathlib import Path
from agent_harness import core
from agent_harness.supervisor_store import SupervisorStore
original=core._os_lock
reported=False
@contextmanager
def observed(path):
 global reported
 try:
  with original(path):yield
 except core.HarnessError as error:
  if Path(path).name=='journal-snapshot.guard' and str(error).startswith('Resource busy') and not reported:
   reported=True;print('blocked',flush=True)
  raise
core._os_lock=observed
store=SupervisorStore(sys.argv[1])
state=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
with store.lease('deterministic live writer'):
 store.commit(state,'concurrent committed state')
print('committed',flush=True)
'''
        process = None
        identity = None
        stopping = threading.Thread(target=stop)
        probe = None
        hook = (patch.object(reader.store, "_decode_events", side_effect=validated) if boundary == "after_validation"
                else patch.object(ParentDirectory, "open", new=before_read))
        with hook:
            try:
                stopping.start()
                self.assertTrue(paused.wait(15), "stop did not reach validated journal read")
                process = subprocess.Popen([sys.executable, "-c", code, str(self.run_dir), str(payload)],
                    env={**self.environment, "PYTHONPATH": str(self.tool.parents[1] / "src")},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
                identity = process_identity(process.pid)
                def observe():
                    lines.append(process.stdout.readline())
                    probed.set()
                probe = threading.Thread(target=observe)
                probe.start()
                self.assertTrue(probed.wait(15), "writer did not report its kernel lock probe")
                self.assertEqual(lines, ["committed\n"] if boundary == "after_validation" else ["blocked\n"])
                release.set()
                stopping.join(15)
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertIn("committed", "".join(lines) + stdout)
                self.assertEqual(errors, [])
                self.assertEqual(results[0]["status"], "CANCEL_REQUESTED")
                self.assertEqual(results[0]["run_id"], owner.state["run_id"])
                self.assertEqual(reader.state["generation"], owner.state["generation"])
                self.assertTrue((self.run_dir / "cancel.yaml").is_file())
            finally:
                release.set()
                stopping.join(15)
                if process is not None and process.poll() is None:
                    self.assertIsNotNone(identity, "owned writer identity unavailable")
                    self.assertEqual(process_identity(process.pid), identity)
                    process.terminate()
                    process.communicate(timeout=10)
                if probe is not None:
                    probe.join(15)
        self.assertFalse(stopping.is_alive())
        self.assertEqual(reader.store.recover(read_only=True)["state"]["generation"], state["generation"])
