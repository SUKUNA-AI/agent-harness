"""Bounded parsing, literal compatibility, and durable validator recovery."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness import runner, validation
from agent_harness.core import Run, identity_alive, read_json


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="validation-", dir=os.environ.get("HARNESS_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.cwd = self.base / "workspace with spaces"
        self.cwd.mkdir()
        (self.cwd / "main.py").write_text("answer = 42\n", encoding="utf-8")
        self.stdout, self.stderr = self.base / "stdout.log", self.base / "stderr.log"
        self.stdout.write_text("", encoding="utf-8")
        self.stderr.write_text("", encoding="utf-8")
        self.contract = {
            "schema_version": 1, "run_id": "validation", "task_id": "fixture", "attempt_id": "a1",
            "workflow": "change", "process_level": "standard", "goal": "Validate evidence in isolation",
            "cwd": str(self.cwd), "permissions": ["read", "write", "test"], "scope": {"write_paths": ["main.py"]},
            "checks": [{"id": "unit", "kind": "command", "required": True,
                        "argv": [sys.executable, "-I", "-B", "-c", "print('one real case')"], "timeout": 3}],
        }
        self.count = 0

    def frozen(self):
        self.count += 1
        run = Run.create(self.base / f"run-{self.count}", deepcopy(self.contract))
        run.freeze()
        return run

    def parse(self, assertions, **kwargs):
        return runner._validate_evidence({"assertions": assertions}, self.stdout, self.stderr, [], timeout=2, **kwargs)

    def report_check(self, xml, **definition):
        path = self.base / "tests.xml"
        self.contract["checks"][0].update(
            argv=[sys.executable, "-I", "-B", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')", str(path), xml],
            reports=[{"path": str(path), "format": "junit", **definition}],
        )
        run = self.frozen()
        return run, runner.run_check(run, "unit")

    def test_plain_command_does_not_invoke_validation(self):
        run = self.frozen()
        with patch.object(runner, "_validate_evidence", side_effect=AssertionError("unexpected parsing")):
            result = runner.run_check(run, "unit")
        self.assertEqual("passed", result["status"], result)
        self.assertEqual({"status": "not_required", "mode": "none"}, result["validation"])
        self.assertEqual("accepted", runner.gate(run)["verdict"])

    def test_literal_fast_path_preserves_counts_streams_and_newlines(self):
        self.stdout.write_bytes(b"one one\r\n")
        self.stderr.write_bytes(b"two\rthree")
        assertions = [{"regex": "one", "stream": "stdout", "min_matches": 2},
                      {"regex": "two\nthree", "stream": "stderr"},
                      {"regex": "one one\n\ntwo\nthree", "stream": "combined"},
                      {"regex": "", "stream": "stdout", "min_matches": 9},
                      {"regex": "absent", "forbidden": True}]
        with patch.object(runner.subprocess, "Popen", side_effect=AssertionError("literal spawned a worker")):
            info, errors, reports = self.parse(assertions)
            failed, failure_errors, _ = self.parse([{"regex": "one", "forbidden": True}])
        self.assertEqual(("literal", "passed", [], []), (info["mode"], info["status"], errors, reports))
        self.assertEqual("failed", failed["status"])
        self.assertIn("forbidden output pattern found: one", failure_errors)

    def test_escaped_dot_wildcard_and_anchor_use_full_regex_worker(self):
        self.stdout.write_text("alphabet\n", encoding="utf-8")
        for pattern, status in [(r"\.", "failed"), (".", "passed"), ("^alpha$", "failed"), ("^alphabet$", "passed")]:
            with self.subTest(pattern=pattern):
                info, _, _ = self.parse([{"regex": pattern, "stream": "stdout"}])
                self.assertEqual(("worker", status), (info["mode"], info["status"]))
                self.assertFalse(identity_alive(info["process"]["identity"]))

    def test_catastrophic_regex_times_out_and_closes_durable_ledger(self):
        self.contract["checks"][0].update(argv=[sys.executable, "-I", "-B", "-c", "print('a' * 32 + '!')"], timeout=1,
                                            assertions=[{"regex": "(a+)+$", "stream": "stdout"}])
        run = self.frozen()
        started = time.monotonic()
        result = runner.run_check(run, "unit")
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(("validation_timeout", 0), (result["status"], result["exit_code"]), result)
        self.assertEqual("validation_timeout", result["validation"]["status"])
        self.assertFalse(identity_alive(result["validation"]["process"]["identity"]))
        ledger = run.state()["processes"]["unit"]
        self.assertEqual(("validation", "validation_timeout"), (ledger["phase"], ledger["status"]))
        self.assertEqual(result, read_json(run.attempt_dir() / "checks" / "unit" / "result.json"))
        stdout = next(item for item in result["artifacts"] if item["path"].endswith("stdout.log"))
        self.assertEqual("a" * 32 + "!\n", (run.path / stdout["path"]).read_text(encoding="utf-8"))
        self.assertEqual("rejected", runner.gate(run)["verdict"])

    def test_validation_respects_remaining_run_deadline(self):
        self.contract["checks"][0]["assertions"] = [{"regex": "^one real case$", "stream": "stdout"}]
        run = self.frozen()
        # The command had time to finish; the run budget expires before parsing.
        with patch.object(runner, "remaining_budget_seconds", side_effect=[3, 3, 0]):
            result = runner.run_check(run, "unit")
        self.assertEqual(("validation_timeout", 0), (result["status"], result["validation"]["timeout"]))
        self.assertNotIn("process", result["validation"])
        self.assertEqual("validation_timeout", run.state()["processes"]["unit"]["status"])
        self.assertTrue((run.attempt_dir() / "checks" / "unit" / "result.json").exists())

    def test_junit_summary_is_bounded_but_required_cases_are_not_truncated(self):
        xml = "<testsuite>" + "".join(f'<testcase name="case-{i}"/>' for i in range(105)) + "</testsuite>"
        run, result = self.report_check(xml, required_cases=["case-104$"])
        self.assertEqual("passed", result["status"], result)
        report = result["reports"][0]
        self.assertEqual((105, 100, 5, True), (report["tests"], len(report["cases"]), report["cases_omitted"], report["cases_truncated"]))
        raw = next(item for item in result["artifacts"] if "report-0" in item["path"])
        self.assertIn("case-104", (run.path / raw["path"]).read_text(encoding="utf-8"))
        self.assertEqual("accepted", runner.gate(run)["verdict"])
        xml = xml.replace('<testcase name="case-104"/>', '<testcase name="case-104"><skipped/></testcase>')
        _, result = self.report_check(xml, required_cases=["case-104$"], max_skipped=1)
        self.assertEqual("failed", result["status"])
        self.assertIn("mandatory case did not pass: case-104$", result["assertion_errors"])

    def test_junit_long_names_use_full_value_for_matching(self):
        name = "a" * 1100 + "required-tail"
        _, result = self.report_check(f'<testsuite><testcase name="{name}"/></testsuite>', required_cases=["required-tail$"])
        self.assertEqual("passed", result["status"], result)
        report = result["reports"][0]
        self.assertEqual((1000, 1, 0, True), (len(report["cases"][0]), report["case_names_shortened"], report["cases_omitted"], report["cases_truncated"]))

    def test_junit_required_case_regex_is_also_bounded(self):
        self.contract["checks"][0]["timeout"] = 1
        _, result = self.report_check('<testsuite><testcase name="' + "a" * 32 + '!"/></testsuite>', required_cases=["(a+)+$"])
        self.assertEqual("validation_timeout", result["status"], result)
        self.assertFalse(identity_alive(result["validation"]["process"]["identity"]))

    def test_worker_cancellation_is_visible_and_reaps_process(self):
        cancel = self.base / "cancel"
        self.contract["checks"][0].update(argv=[sys.executable, "-I", "-B", "-c", "print('a' * 32 + '!')"], timeout=3,
                                            assertions=[{"regex": "(a+)+$"}])
        run = self.frozen()
        done = threading.Event()

        def cancel_validation():
            deadline = time.monotonic() + 8
            while not done.is_set() and time.monotonic() < deadline:
                entry = run.state().get("processes", {}).get("unit", {})
                if entry.get("phase") == "validation" and entry["status"] == "running":
                    cancel.touch()
                    return True
                done.wait(0.01)
            return False

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cancel_validation)
            try:
                result = runner.run_check(run, "unit", cancel_file=cancel)
            finally:
                done.set()
            self.assertTrue(future.result(), "validator never appeared in process ledger")
        self.assertEqual("cancelled", result["status"], result)
        self.assertEqual("cancelled", run.state()["processes"]["unit"]["status"])
        self.assertFalse(identity_alive(result["validation"]["process"]["identity"]))
        self.assertTrue((run.attempt_dir() / "checks" / "unit" / "result.json").exists())

    def test_keyboard_interrupt_during_worker_registration_reaps_it(self):
        identities = []

        def interrupted(identity):
            identities.append(identity)
            raise KeyboardInterrupt()

        info, errors, _ = self.parse([{"regex": "."}], on_start=interrupted)
        self.assertEqual("cancelled", info["status"])
        self.assertTrue(errors)
        self.assertEqual(1, len(identities))
        self.assertFalse(identity_alive(identities[0]))

    def test_oversized_request_and_inputs_fail_before_worker_spawn(self):
        with self.stdout.open("wb") as output:
            output.truncate(validation.MAX_INPUT_BYTES + 1)
        with patch.object(runner.subprocess, "Popen", side_effect=AssertionError("oversize spawned a worker")):
            request_info, request_errors, _ = self.parse([{"regex": "a" * validation.MAX_REQUEST_BYTES}])
            input_info, input_errors, _ = self.parse([{"regex": "a"}])
        self.assertEqual(("failed", "failed"), (request_info["status"], input_info["status"]))
        self.assertIn("validation request exceeds", request_errors[0])
        self.assertIn("validation inputs exceed", input_errors[0])
        self.assertEqual(validation.MAX_INPUT_BYTES + 1, self.stdout.stat().st_size)

    def test_worker_big_output_and_stderr_cannot_deadlock_or_pass(self):
        real_popen = subprocess.Popen
        script = "import sys; sys.stderr.write('e' * 262144); sys.stdout.write('x' * 1048577)"

        def oversized(_argv, **kwargs):
            return real_popen([sys.executable, "-I", "-B", "-c", script], **kwargs)

        with patch.object(runner.subprocess, "Popen", side_effect=oversized):
            info, errors, _ = self.parse([{"regex": "."}])
        self.assertEqual("failed", info["status"])
        self.assertIn("response exceeds byte limit", errors[0])
        self.assertFalse(identity_alive(info["process"]["identity"]))

    def test_worker_oversized_report_summary_fails_with_small_response(self):
        report = self.base / "many-long-names.xml"
        # Escaped supplementary Unicode produces >1 MiB even after name/count caps.
        case = '<testcase name="' + "\U0001f600" * 1000 + '"/>'
        report.write_text("<testsuite>" + case * 100 + "</testsuite>", encoding="utf-8")
        definition = {"format": "junit", "path": str(report)}
        info, errors, reports = runner._validate_evidence({"reports": [definition]}, self.stdout, self.stderr, [(definition, report)], timeout=2)
        self.assertEqual("failed", info["status"])
        self.assertIn("validation response exceeds", errors[0])
        self.assertLess(len(json.dumps(errors)), 200)
        self.assertEqual([], reports)
        self.assertTrue(report.exists())

    def test_worker_ignores_inherited_python_injection_paths(self):
        poison = self.base / "poison"
        poison.mkdir()
        marker = self.base / "injected"
        (poison / "json.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('injection')\n", encoding="utf-8")
        self.stdout.write_text("normal", encoding="utf-8")
        with patch.dict(os.environ, {"PYTHONPATH": str(poison), "PYTHONHOME": str(poison)}):
            info, errors, _ = self.parse([{"regex": "^normal$"}])
        self.assertEqual(("passed", []), (info["status"], errors))
        self.assertFalse(marker.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO fixture")
    def test_literal_inputs_reject_fifo_without_waiting_for_writer(self):
        self.stdout.unlink()
        os.mkfifo(self.stdout)
        started = time.monotonic()
        info, errors, _ = self.parse([{"regex": "literal"}])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual("failed", info["status"])
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
