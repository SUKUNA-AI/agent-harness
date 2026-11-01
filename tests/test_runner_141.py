"""Regressions for type-safe evidence and atomic attempt/acceptance boundaries."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import core, runner


class Runner141Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner141-", dir=os.environ.get("HARNESS_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.cwd = self.base / "source"
        self.cwd.mkdir()
        (self.cwd / "input.txt").write_text("fixture", encoding="utf-8")
        self.artifact = self.base / "assessment.txt"
        self.artifact.write_text("fixture observation", encoding="utf-8")
        self.contract = {
            "schema_version": 1, "run_id": "audit141", "task_id": "fixture", "attempt_id": "a1",
            "workflow": "change", "process_level": "standard", "goal": "Verify evidence boundaries",
            "cwd": str(self.cwd), "permissions": ["read", "write", "test"],
            "scope": {"write_paths": ["records.json"]}, "budget": {"max_attempts": 3},
            "checks": [{"id": "review", "kind": "content", "required": True, "criteria": ["done"]}],
        }
        self.count = 0

    def create(self):
        self.count += 1
        return core.Run.create(self.base / f"run-{self.count}", deepcopy(self.contract))

    def evidence(self, run, check="review"):
        runner.record_evidence(run, check, {"kind": "content", "status": "satisfied", "method": "agent_assessment",
            "assessor": "fixture", "assessment": "observed fixture", "criteria": {"done": "observed"},
            "artifact_paths": [str(self.artifact)]})

    def command(self, report=None, definition=None):
        marker = self.base / "executed"
        path = marker if report is None else self.base / "report"
        self.contract["checks"] = [{"id": "unit", "kind": "command", "required": True, "timeout": 3,
            "argv": [sys.executable, "-I", "-B", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')", str(path), report or "executed"]}]
        if definition:
            self.contract["checks"][0]["reports"] = [{"path": str(path), **definition}]
        return marker

    def test_recursive_json_report_distinguishes_boolean_and_number(self):
        for actual, expected, status in [
            ({"deep": [{"flag": 1}]}, {"deep": [{"flag": True}]}, "failed"),
            ({"deep": [False]}, {"deep": [0]}, "failed"),
            ({"deep": [{"flag": True}]}, {"deep": [{"flag": True}]}, "passed"),
            ({"deep": [1.0]}, {"deep": [1]}, "passed"),
        ]:
            with self.subTest(actual=actual, expected=expected):
                self.command(json.dumps({"value": actual}), {"format": "json", "equals": {"value": expected}})
                run = self.create(); run.freeze()
                result = runner.run_check(run, "unit")
                self.assertEqual(status, result["status"], result)
                self.assertEqual("accepted" if status == "passed" else "rejected", runner.gate(run)["verdict"])

    def test_foreign_records_and_metadata_preserve_json_types(self):
        before = {"rows": [{"id": "mine", "value": 0}, {"id": "foreign", "nested": [{"flag": False}]}],
                  "metadata": {"nested": [False]}}
        self.contract["scope"]["shared_files"] = [{"path": "records.json", "format": "json-records", "records_key": "rows", "allowed_records": ["mine"]}]
        for area in ("record", "metadata", "allowed"):
            with self.subTest(area=area):
                path = self.cwd / "records.json"
                path.write_text(json.dumps(before), encoding="utf-8")
                run = self.create()
                after = deepcopy(before)
                if area == "record":
                    after["rows"][1]["nested"][0]["flag"] = 0
                elif area == "metadata":
                    after["metadata"]["nested"][0] = 0
                else:
                    after["rows"][0]["value"] = True
                path.write_text(json.dumps(after), encoding="utf-8")
                run.freeze(); self.evidence(run)
                verdict = runner.gate(run)
                self.assertEqual("accepted" if area == "allowed" else "rejected", verdict["verdict"], verdict)
                if area != "allowed":
                    self.assertTrue(any(("foreign shared record" if area == "record" else "shared metadata") in e for e in verdict["errors"]))

    def test_junit_execution_attributes_cannot_invent_a_required_pass(self):
        for attrs, required, allowance, status in [
            ('status="notrun" result="suppressed"', ["must_run"], 1, "failed"),
            ('status="run" result="suppressed"', ["must_run"], 1, "failed"),
            ('status="unexpected" result="completed"', ["must_run"], 1, "failed"),
            ('status="run" result="mystery"', ["must_run"], 1, "failed"),
            ('status="notrun" result="suppressed"', [], 0, "failed"),
            ('status="notrun" result="suppressed"', ["actual_pass"], 1, "passed"),
            ('status="run" result="completed"', ["must_run"], 0, "passed"),
        ]:
            with self.subTest(attrs=attrs, required=required, allowance=allowance):
                xml = f'<testsuite><testcase name="must_run" {attrs}/><testcase name="actual_pass"/></testsuite>'
                self.command(xml, {"format": "junit", "required_cases": required, "max_skipped": allowance})
                run = self.create(); run.freeze()
                result = runner.run_check(run, "unit")
                self.assertEqual(status, result["status"], result)
                self.assertEqual("accepted" if status == "passed" else "rejected", runner.gate(run)["verdict"])
                if "notrun" in attrs or "suppressed" in attrs:
                    self.assertEqual(1, result["reports"][0]["skipped"])

    def test_attempt_transition_is_excluded_during_preflight_then_allowed_after_check(self):
        marker = self.command()
        run = self.create(); run.freeze()
        fresh = deepcopy(self.contract); fresh["attempt_id"] = "a2"
        fresh["permissions"] = ["read"]; fresh["scope"] = {}
        fresh["checks"] = [{"id": "review", "kind": "content", "required": True, "criteria": ["done"]}]
        versions = runner._versions
        attempts = []

        def preflight(*args, **kwargs):
            with self.assertRaisesRegex(core.HarnessError, "Resource busy"):
                run.new_attempt(fresh, reason="Stop executable tests")
            attempts.append(run.state()["current_attempt"])
            return versions(*args, **kwargs)

        with patch.object(runner, "_versions", preflight):
            result = runner.run_check(run, "unit")
        self.assertEqual("passed", result["status"], result)
        self.assertTrue(marker.exists())
        self.assertEqual(["a1", "a1"], attempts)
        old_path = run.attempt_dir() / "checks/unit/result.json"
        run.new_attempt(fresh, reason="Stop executable tests")
        self.assertEqual("a2", run.state()["current_attempt"])
        self.assertTrue(old_path.is_file())
        self.assertFalse((run.attempt_dir() / "checks/unit/result.json").exists())
        self.assertNotEqual("running", run.state()["processes"]["unit"]["status"])

    def test_state_read_before_workspace_lease_cannot_start_old_command_after_cwd_change(self):
        marker = self.command()
        run = self.create(); run.freeze()
        other = self.base / "other"; other.mkdir()
        (other / "input.txt").write_text("fixture", encoding="utf-8")
        fresh = deepcopy(self.contract); fresh.update(attempt_id="a2", cwd=str(other), permissions=["read"], scope={}, resource_lock_root=str(self.base / "new-locks"))
        fresh["checks"] = [{"id": "review", "kind": "content", "required": True, "criteria": ["done"]}]
        workspace_lock = run.workspace_lock

        @contextmanager
        def transition_before_lease(contract=None):
            run.new_attempt(fresh, reason="Move to read-only workspace")
            with workspace_lock(contract):
                yield

        with patch.object(run, "workspace_lock", transition_before_lease):
            with self.assertRaisesRegex(core.HarnessError, "Attempt changed before workspace lease"):
                runner.run_check(run, "unit")
        self.assertFalse(marker.exists())
        self.assertEqual(("a2", "planned", {}), (run.state()["current_attempt"], run.state()["status"], run.state().get("processes", {})))
        # The new cwd and namespace receive their own functioning lease.
        run.freeze(); self.evidence(run)
        self.assertEqual("accepted", runner.gate(run)["verdict"])

    def test_gate_rejects_operation_added_after_the_operation_check(self):
        run = self.create(); run.freeze(); self.evidence(run)
        snapshot = runner.snapshot

        def mutate(contract):
            run.record_operation("external", "fixture only", "unknown", "")
            return snapshot(contract)

        with patch.object(runner, "snapshot", mutate):
            verdict = runner.gate(run)
        self.assertEqual("rejected", verdict["verdict"], verdict)
        self.assertTrue(any("state changed" in error for error in verdict["errors"]))
        self.assertIsNone(run.state()["accepted"])
        self.assertEqual("unknown", run.state()["operations"]["external"]["status"])

    def test_accepted_receipts_bind_optional_presence_and_exact_results(self):
        self.contract["checks"].append({"id": "optional", "kind": "content", "required": False, "criteria": ["done"]})
        for change in ("never_run", "deleted_optional", "deleted_required", "added_optional", "modified_optional"):
            with self.subTest(change=change):
                run = self.create(); run.freeze(); self.evidence(run)
                if change in {"deleted_optional", "modified_optional"}:
                    self.evidence(run, "optional")
                verdict = runner.gate(run)
                self.assertEqual("accepted", verdict["verdict"], verdict)
                if change.startswith("deleted_"):
                    check = "optional" if change == "deleted_optional" else "review"
                    (run.attempt_dir() / f"checks/{check}/result.json").unlink()
                elif change == "added_optional":
                    self.evidence(run, "optional")
                elif change == "modified_optional":
                    path = run.attempt_dir() / "checks/optional/result.json"
                    result = core.read_json(path); result.pop("digest")
                    result["assessment"] = "changed"; result["digest"] = core.digest(result)
                    core.atomic_json(path, result)
                self.assertEqual(change == "never_run", run.status(verify_evidence=True)["evidence_current"])

    def test_accepted_custom_scope_receipts_survive_read_only_revalidation(self):
        path = self.cwd / "records.json"; path.write_text('{}', encoding="utf-8")
        adapter = self.base / "adapter.py"; adapter.write_text('print("scope checked")', encoding="utf-8")
        self.contract["scope"]["shared_files"] = [{"path": "records.json", "format": "validator", "allowed_records": ["mine"],
            "validator_argv": [sys.executable, "-I", "-B", str(adapter)], "validator_files": {str(adapter): core.file_hash(adapter)}}]
        run = self.create(); run.freeze(); self.evidence(run)
        verdict = runner.gate(run)
        self.assertEqual("accepted", verdict["verdict"], verdict)
        self.assertEqual("records.json", verdict["receipts"][0]["path"])
        with patch.object(runner, "_contained_capture", side_effect=AssertionError("read-only revalidation executed adapter")):
            self.assertTrue(run.status(verify_evidence=True)["evidence_current"])

    @unittest.skipIf(os.name == "nt", "Windows trims trailing filename whitespace")
    def test_git_root_trailing_whitespace_retains_git_snapshot_semantics(self):
        for suffix in (" ", "\t", "\n"):
            with self.subTest(suffix=repr(suffix)):
                root = self.base / ("repo" + suffix); root.mkdir()
                subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", str(root)], check=True, capture_output=True)
                (root / "tracked").write_text("tracked", encoding="utf-8")
                (root / ".gitignore").write_text("ignored\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(root), "add", "tracked", ".gitignore"], check=True, capture_output=True)
                (root / "tracked").unlink()
                (root / "ignored").write_text("ignore", encoding="utf-8")
                result = core.snapshot_python({"cwd": str(root)})
                self.assertEqual("git", result["kind"], result)
                self.assertEqual({"kind": "missing"}, result["files"]["tracked"])
                self.assertNotIn("ignored", result["files"])


if __name__ == "__main__":
    unittest.main()
