from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
from agent_harness.core import AcceptanceDeadlineExhausted, HarnessError, Run, atomic_json, digest, file_hash, identity_alive, lock, process_identity, read_json, snapshot, validate_contract
from agent_harness.runner import gate, record_evidence, run_check


class CoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(os.environ.get("HARNESS_TEST_TMP", Path(__file__).resolve().parents[1] / ".test-tmp"))
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="core-", dir=temp_root)
        self.base = Path(self.temp.name)
        self.cwd = self.base / "Проба with spaces"
        self.cwd.mkdir()
        (self.cwd / "main.py").write_text("answer = 42\n", encoding="utf-8")
        self.contract = {"schema_version": 1, "run_id": "fixture-run", "task_id": "fixture-task", "attempt_id": "a1", "workflow": "change", "process_level": "standard", "goal": "Verify an isolated fixture", "cwd": str(self.cwd.resolve()), "permissions": ["read", "write", "test"], "scope": {"write_paths": ["main.py", "records.json"], "protected_paths": ["protected/**"], "exclude": ["__pycache__/**"]}, "checks": [{"id": "unit", "kind": "command", "required": True, "argv": [sys.executable, "-I", "-B", "-c", "print('one real case')"], "timeout": 3, "expected_exit": 0, "assertions": [{"stream": "stdout", "regex": "one real case"}]}], "budget": {"max_attempts": 5, "native_cap": 3, "root_counts": True, "native_cap_source": "isolated fixture semantics, not an observed native runtime"}}

    def tearDown(self):
        self.temp.cleanup()

    def run_fixture(self, contract=None, name="run"):
        return Run.create(self.base / name, deepcopy(contract or self.contract))

    def passing(self, contract=None):
        run = self.run_fixture(contract)
        run.freeze()
        result = run_check(run, "unit")
        self.assertEqual("passed", result["status"], result)
        return run, result

    def owner(self, owner_id="one", **overrides):
        return {"owner_id": owner_id, "lane_id": "lane-" + owner_id, "native_ref": "fixture-" + owner_id, "status": "running", "observation": "fixture backend observation", **overrides}

    def write_report_command(self, report, contents):
        self.contract["checks"][0]["argv"] = [sys.executable, "-I", "-B", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8'); print('one real case')", str(report), contents]

    def test_real_command_and_evidence_gate(self):
        self.contract["checks"][0]["tool_versions"] = {"python": [sys.executable, "--version"]}
        run, evidence = self.passing()
        self.assertIn("Python", evidence["tools"]["python"]["version"])
        self.assertEqual(evidence["input_before"], evidence["input_after"])
        self.assertEqual("accepted", gate(run)["verdict"])
        self.assertTrue(run.status(verify_evidence=True)["evidence_current"])

    def test_nonzero_exit_is_failed(self):
        self.contract["checks"][0]["argv"][-1] = "import sys; print('one real case'); sys.exit(7)"
        run = self.run_fixture()
        run.freeze()
        result = run_check(run, "unit")
        self.assertEqual(("failed", 7), (result["status"], result["exit_code"]))
        self.assertEqual("rejected", gate(run)["verdict"])

    def test_missing_dependency_has_separate_status(self):
        self.contract["checks"][0]["argv"] = ["harness-deliberately-nonexistent-4f38"]
        run = self.run_fixture()
        run.freeze()
        self.assertEqual("missing_dependency", run_check(run, "unit")["status"])

    def test_timeout_kills_owned_descendant_process(self):
        pid_file = self.base / "child.json"
        code = "import subprocess,sys,time,json; p=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)']); open(sys.argv[1],'w').write(str(p.pid)); time.sleep(30)"
        self.contract["checks"][0].update({"argv": [sys.executable, "-I", "-B", "-c", code, str(pid_file)], "timeout": 0.6, "assertions": []})
        run = self.run_fixture()
        run.freeze()
        result = run_check(run, "unit")
        self.assertEqual("timeout", result["status"], result)
        child_pid = int(pid_file.read_text())
        for _ in range(30):
            if process_identity(child_pid) is None:
                break
            time.sleep(0.02)
        self.assertIsNone(process_identity(child_pid), "Descendant survived the check timeout")
        self.assertEqual("rejected", gate(run)["verdict"])

    def test_cancel_signal_has_distinct_status(self):
        cancel = self.base / "cancel"
        self.contract["checks"][0].update({"argv": [sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"], "timeout": 5, "assertions": []})
        # The old fixed 150 ms delay cancelled before spawn on a slow CI runner.
        self.contract["checks"][0]["tool_versions"] = {"slow-probe": [sys.executable, "-I", "-B", "-c", "import time; time.sleep(0.2); print('fixture version')"]}
        run = self.run_fixture()
        run.freeze()
        finished = threading.Event()

        def cancel_after_start():
            deadline = time.monotonic() + 10
            while not finished.is_set() and time.monotonic() < deadline:
                if run.state().get("processes", {}).get("unit", {}).get("status") == "running":
                    cancel.touch()
                    return True
                finished.wait(0.01)
            return False

        with ThreadPoolExecutor(max_workers=1) as executor:
            cancellation = executor.submit(cancel_after_start)
            try:
                result = run_check(run, "unit", cancel_file=cancel)
            finally:
                finished.set()
            self.assertTrue(cancellation.result(), "Check never entered the running state")
        self.assertEqual("cancelled", result["status"], result)
        self.assertIn("process", result)
        self.assertFalse(identity_alive(result["process"]["identity"]))

    def test_cancel_before_start_does_not_spawn(self):
        cancel = self.base / "cancel-before-start"
        cancel.touch()
        run = self.run_fixture()
        run.freeze()
        with patch("agent_harness.runner._start_contained") as start:
            result = run_check(run, "unit", cancel_file=cancel)
        start.assert_not_called()
        self.assertEqual("cancelled", result["status"])
        self.assertNotIn("process", result)

    def test_mandatory_check_missing_cannot_accept(self):
        run = self.run_fixture()
        run.freeze()
        result = gate(run)
        self.assertEqual("rejected", result["verdict"])
        self.assertIn("required check missing: unit", result["errors"])

    def test_zero_test_report_rejected_despite_exit_zero(self):
        report = self.base / "report.xml"
        self.write_report_command(report, '<testsuites><testsuite tests="0"/></testsuites>')
        self.contract["checks"][0]["reports"] = [{"path": str(report), "format": "junit", "min_tests": 1}]
        run = self.run_fixture()
        run.freeze()
        result = run_check(run, "unit")
        self.assertEqual("failed", result["status"])
        self.assertIn("test report collected too few cases", result["assertion_errors"])

    def test_skipped_required_case_rejected(self):
        report = self.base / "report.xml"
        self.write_report_command(report, '<testsuite><testcase name="must_run"><skipped/></testcase></testsuite>')
        self.contract["checks"][0]["reports"] = [{"path": str(report), "format": "junit", "required_cases": ["must_run"]}]
        run = self.run_fixture()
        run.freeze()
        self.assertEqual("failed", run_check(run, "unit")["status"])

    def test_missing_report_rejected(self):
        self.contract["checks"][0]["reports"] = [{"path": str(self.base / "missing.xml"), "format": "junit"}]
        run = self.run_fixture()
        run.freeze()
        self.assertEqual("failed", run_check(run, "unit")["status"])

    def test_stale_success_report_cannot_accept_a_noop(self):
        report = self.base / "stale.xml"
        report.write_text('<testsuite><testcase name="old-pass"/></testsuite>', encoding="utf-8")
        self.contract["checks"][0]["reports"] = [{"path": str(report), "format": "junit", "required_cases": ["old-pass"]}]
        run = self.run_fixture()
        run.freeze()
        result = run_check(run, "unit")
        self.assertEqual("failed", result["status"])
        self.assertFalse(result["report_observations"][0]["fresh"])
        self.assertTrue(any("not produced by this check" in error for error in result["assertion_errors"]))
        self.assertEqual("rejected", gate(run)["verdict"])
        self.assertTrue(report.is_file(), "The runner must not remove a user's old report")

    def test_identical_report_rewritten_by_current_check_is_fresh(self):
        report = self.base / "rewritten.xml"
        contents = '<testsuite><testcase name="still-passes"/></testsuite>'
        report.write_text(contents, encoding="utf-8")
        original_hash = file_hash(report)
        os.utime(report, (946684800, 946684800))
        self.write_report_command(report, contents)
        self.contract["checks"][0]["reports"] = [{"path": str(report), "format": "junit", "required_cases": ["still-passes"]}]
        run, result = self.passing()
        self.assertEqual(original_hash, file_hash(report))
        self.assertTrue(result["report_observations"][0]["fresh"])
        self.assertEqual("accepted", gate(run)["verdict"])

    def test_json_report_shape_errors_persist_failed_evidence(self):
        for index, contents in enumerate(('{"summary":null}', '{"summary":[]}', '{"summary":7}', '{}')):
            with self.subTest(contents=contents):
                report = self.base / f"bad-{index}.json"
                self.write_report_command(report, contents)
                self.contract["checks"][0]["reports"] = [{"path": str(report), "format": "json", "equals": {"summary.tests": 1}}]
                run = self.run_fixture(name=f"bad-run-{index}")
                run.freeze()
                result = run_check(run, "unit")
                persisted = read_json(run.attempt_dir() / "checks/unit/result.json")
                self.assertEqual("failed", result["status"])
                self.assertEqual(result["digest"], persisted["digest"])
                self.assertTrue(any("invalid test report" in error for error in result["assertion_errors"]))
                self.assertEqual("failed", run.state()["processes"]["unit"]["status"])
                self.assertFalse(identity_alive(result["process"]["identity"]))
                self.assertEqual("rejected", gate(run)["verdict"])

    def test_fresh_json_report_accepts_matching_fields(self):
        report = self.base / "current.json"
        self.write_report_command(report, '{"summary":{"tests":1}}')
        self.contract["checks"][0]["reports"] = [{"path": str(report), "format": "json", "equals": {"summary.tests": 1}}]
        run, result = self.passing()
        self.assertTrue(result["report_observations"][0]["fresh"])
        self.assertEqual("accepted", gate(run)["verdict"])

    def test_report_capture_failure_closes_check_and_keeps_logs(self):
        report = self.base / "capture-error.json"
        self.write_report_command(report, '{"tests":1}')
        self.contract["checks"][0]["reports"] = [{"path": str(report), "format": "json", "equals": {"tests": 1}}]
        run = self.run_fixture()
        run.freeze()
        with patch("agent_harness.runner.shutil.copyfile", side_effect=OSError("fixture report copy failed")):
            result = run_check(run, "unit")
        self.assertEqual("failed", result["status"])
        self.assertTrue((run.attempt_dir() / "checks/unit/result.json").is_file())
        self.assertEqual("failed", run.state()["processes"]["unit"]["status"])
        self.assertEqual({"stdout.log", "stderr.log"}, {Path(item["path"]).name for item in result["artifacts"]})

    def test_final_snapshot_failure_still_persists_failed_result(self):
        run = self.run_fixture()
        run.freeze()
        with patch("agent_harness.runner.snapshot", side_effect=HarnessError("fixture input disappeared")):
            result = run_check(run, "unit")
        self.assertEqual("failed", result["status"])
        self.assertIn("Evidence finalization failed", result["error"])
        self.assertEqual("failed", read_json(run.attempt_dir() / "checks/unit/result.json")["status"])
        self.assertEqual("failed", run.state()["processes"]["unit"]["status"])

    def test_source_change_makes_result_stale(self):
        run, _ = self.passing()
        gate(run)
        (self.cwd / "main.py").write_text("answer = 43\n")
        self.assertEqual("stale", run.status()["status"])
        with self.assertRaisesRegex(HarnessError, "Stale candidate"):
            gate(run)

    def test_command_mutating_source_cannot_pass(self):
        self.contract["checks"][0]["argv"][-1] = "open('main.py','w').write('changed'); print('one real case')"
        run = self.run_fixture()
        run.freeze()
        self.assertEqual("inputs_changed", run_check(run, "unit")["status"])

    def test_missing_log_rejected(self):
        run, evidence = self.passing()
        (run.path / evidence["artifacts"][0]["path"]).unlink()
        self.assertTrue(any("artifact missing" in x for x in gate(run)["errors"]))

    def test_tampered_log_rejected(self):
        run, evidence = self.passing()
        (run.path / evidence["artifacts"][0]["path"]).write_text("fabricated pass")
        self.assertTrue(any("artifact integrity failed" in x for x in gate(run)["errors"]))

    def test_wrong_attempt_result_rejected_even_with_recomputed_hash(self):
        run, evidence = self.passing()
        path = run.attempt_dir() / "checks/unit/result.json"
        evidence.pop("digest")
        evidence["attempt_id"] = "wrong"
        evidence["digest"] = digest(evidence)
        atomic_json(path, evidence)
        self.assertTrue(any("wrong attempt_id" in x for x in gate(run)["errors"]))

    def test_changed_contract_same_schema_version_rejected(self):
        run, _ = self.passing()
        state = run.state()
        path = run.path / "contracts" / (state["contract_digest"] + ".json")
        contract = read_json(path)
        contract["checks"][0]["required"] = False
        atomic_json(path, contract)
        with self.assertRaisesRegex(HarnessError, "integrity"):
            gate(run)

    def test_new_attempt_cannot_inherit_old_checks(self):
        run, _ = self.passing()
        fresh = deepcopy(self.contract)
        fresh["attempt_id"] = "a2"
        run.new_attempt(fresh, reason="Recheck revised requirement")
        run.freeze()
        self.assertEqual("rejected", gate(run)["verdict"])

    def test_protected_and_foreign_path_edits_rejected_before_checks(self):
        run = self.run_fixture()
        (self.cwd / "unrelated.txt").write_text("wrong")
        with self.assertRaisesRegex(HarnessError, "out-of-scope"):
            run.freeze()
        (self.cwd / "unrelated.txt").unlink()
        (self.cwd / "protected").mkdir()
        (self.cwd / "protected/secret.txt").write_text("wrong")
        with self.assertRaisesRegex(HarnessError, "protected path"):
            run.freeze()

    def test_shared_record_scope_detects_other_owners_edit(self):
        path = self.cwd / "records.json"
        path.write_text(json.dumps({"records": [{"id": "U03", "goal": "old"}, {"id": "U04", "goal": "old"}], "budget": 20}))
        self.contract["scope"]["shared_files"] = [{"path": "records.json", "format": "json-records", "records_key": "records", "allowed_records": ["U03"]}]
        run = self.run_fixture()
        value = json.loads(path.read_text())
        value["records"][1]["goal"] = "foreign edit"
        path.write_text(json.dumps(value))
        run.freeze()
        run_check(run, "unit")
        self.assertIn("foreign shared record changed: U04", gate(run)["errors"])

    def test_shared_record_scope_accepts_only_assigned_record(self):
        path = self.cwd / "records.json"
        path.write_text(json.dumps([{"id": "U03", "goal": "old"}, {"id": "U04", "goal": "old"}]))
        self.contract["scope"]["shared_files"] = [{"path": "records.json", "format": "json-records", "allowed_records": ["U03"]}]
        run = self.run_fixture()
        value = json.loads(path.read_text())
        value[0]["goal"] = "assigned edit"
        path.write_text(json.dumps(value))
        run.freeze()
        run_check(run, "unit")
        self.assertEqual("accepted", gate(run)["verdict"])

    def test_shared_record_scope_preserves_nonrecord_metadata(self):
        path = self.cwd / "records.json"
        path.write_text(json.dumps({"records": [{"id": "U03"}], "budget": 20}))
        self.contract["scope"]["shared_files"] = [{"path": "records.json", "format": "json-records", "records_key": "records", "allowed_records": ["U03"]}]
        run = self.run_fixture()
        value = json.loads(path.read_text())
        value["budget"] = 40
        path.write_text(json.dumps(value))
        run.freeze()
        run_check(run, "unit")
        self.assertIn("shared metadata changed: records.json", gate(run)["errors"])

    def test_read_only_review_contract_rejects_edit_permissions(self):
        self.contract["workflow"] = "review"
        with self.assertRaisesRegex(HarnessError, "must not authorize"):
            self.run_fixture()

    def test_non_git_snapshot_never_fabricates_commit(self):
        observed = snapshot(self.contract)
        self.assertEqual("directory", observed["kind"])
        self.assertNotIn("git", observed)

    def test_forbidden_persistence_creates_no_run(self):
        self.contract["persistence"] = False
        target = self.base / "should-not-exist"
        with self.assertRaisesRegex(HarnessError, "Persistence prohibited"):
            Run.create(target, self.contract)
        self.assertFalse(target.exists())

    def test_immutable_check_is_not_repeated_accidentally(self):
        run, _ = self.passing()
        with self.assertRaisesRegex(HarnessError, "immutable"):
            run_check(run, "unit")

    def test_checkpoint_resume_reconciles_actual_inputs(self):
        run, _ = self.passing()
        self.assertEqual(1, run.checkpoint("Decision and owner context", "Review exact candidate")["version"])
        self.assertEqual(2, run.checkpoint("More progress", "Continue review")["version"])
        recovered = Run(run.path)
        self.assertTrue(recovered.resume()["safe_to_start_new_owner"])
        (self.cwd / "main.py").write_text("changed after interruption")
        self.assertFalse(recovered.resume()["safe_to_start_new_owner"])

    def test_unknown_operation_must_be_observed_before_repeat(self):
        run = self.run_fixture()
        run.record_operation("commit-1", "local commit", "planned", "")
        run.record_operation("commit-1", "local commit", "unknown", "interrupted")
        with self.assertRaisesRegex(HarnessError, "unknown"):
            run.record_operation("commit-1", "local commit", "planned", "")
        run.record_operation("commit-1", "local commit", "confirmed", "git log contains the exact intended commit")
        with self.assertRaisesRegex(HarnessError, "already completed"):
            run.record_operation("commit-1", "local commit", "planned", "")

    def test_unresolved_operation_blocks_acceptance(self):
        run, _ = self.passing()
        run.record_operation("action", "fixture operation", "unknown", "No receipt")
        self.assertTrue(any("unknown operation" in x for x in gate(run)["errors"]))

    def test_one_ledger_cap_counts_idle_done_and_helpers(self):
        run = self.run_fixture()
        run.record_owner(self.owner())
        run.record_owner(self.owner("two", status="idle", logical_owner="one", role="helper"))
        with self.assertRaisesRegex(HarnessError, "cap exceeded"):
            run.record_owner(self.owner("three"))
        run.record_owner(self.owner("two", status="done"))
        with self.assertRaisesRegex(HarnessError, "cap exceeded"):
            run.record_owner(self.owner("three"))
        run.record_owner(self.owner("two", status="closed", observation="fixture close confirmation", closed_confirmed=True, closure_evidence="fixture native closed receipt"))
        run.record_owner(self.owner("three"))
        self.assertEqual(3, run.state()["max_observed_counted_threads"])

    def test_native_apis_require_explicit_root_counting(self):
        self.contract["budget"].pop("root_counts")
        run = self.run_fixture()
        before = run.state_path.read_bytes()
        with self.assertRaisesRegex(HarnessError, "root_counts"):
            run.capacity()
        with self.assertRaisesRegex(HarnessError, "root_counts"):
            run.record_owner(self.owner())
        self.assertEqual(before, run.state_path.read_bytes())

    def test_non_native_work_does_not_require_a_native_budget(self):
        self.contract.pop("budget")
        run, result = self.passing()
        self.assertEqual("passed", result["status"])
        self.assertEqual("accepted", gate(run)["verdict"])
        with self.assertRaisesRegex(HarnessError, "capacity is unknown"):
            run.capacity()
        with self.assertRaisesRegex(HarnessError, "native cap"):
            run.record_owner(self.owner())

    def test_native_ledger_counts_root_only_for_total_limit(self):
        for root_counts in (False, True):
            with self.subTest(root_counts=root_counts):
                contract = deepcopy(self.contract)
                contract["budget"].update(native_cap=2, root_counts=root_counts)
                run = self.run_fixture(contract, name=f"capacity-{root_counts}")
                self.assertEqual(int(root_counts), run.capacity()["counted"])
                self.assertEqual(root_counts, run.capacity()["root_counts"])
                for index in range(2 - int(root_counts)):
                    run.record_owner(self.owner(f"owner-{index}", status="idle"))
                self.assertEqual(2, run.capacity()["counted"])
                self.assertEqual(0, run.capacity()["remaining"])
                self.assertFalse(run.capacity()["spawn_allowed"])
                with self.assertRaisesRegex(HarnessError, "cap exceeded"):
                    run.record_owner(self.owner("over-cap"))
                run.record_owner(self.owner("owner-0", status="closed", closed_confirmed=True, closure_evidence="fixture backend close receipt"))
                self.assertEqual(1, run.capacity()["remaining"])
                self.assertEqual(2, run.state()["max_observed_counted_threads"])

    def test_two_writers_cannot_share_checkout(self):
        run = self.run_fixture()
        run.record_owner(self.owner(write_cwd=str(self.cwd)))
        with self.assertRaisesRegex(HarnessError, "must not share"):
            run.record_owner(self.owner("two", write_cwd=str(self.cwd)))

    def test_resume_does_not_assume_native_owner_disappeared(self):
        run = self.run_fixture()
        run.record_owner(self.owner())
        self.assertFalse(run.resume()["safe_to_start_new_owner"])
        fresh = deepcopy(self.contract)
        fresh["attempt_id"] = "a2"
        with self.assertRaisesRegex(HarnessError, "Reconcile"):
            run.new_attempt(fresh, reason="Repair")

    def test_late_result_cannot_overwrite_new_attempt(self):
        run, evidence = self.passing()
        old = run.state()
        fresh = deepcopy(self.contract)
        fresh["attempt_id"] = "a2"
        run.new_attempt(fresh, reason="Repair actual new requirement")
        run.freeze()
        result = {"run_id": old["run_id"], "task_id": old["task_id"], "attempt_id": "a1", "contract_digest": old["contract_digest"], "candidate_digest": old["candidate_digest"], "status": "ready_for_review", "summary": "Old result", "limitations": "Fixture", "next_safe_step": "Review", "evidence_ids": ["unit"]}
        with self.assertRaisesRegex(HarnessError, "Late or mismatched"):
            run.submit_result(result)
        self.assertEqual("a2", run.state()["current_attempt"])

    def test_attempt_budget_does_not_reset_with_owner(self):
        self.contract["budget"]["max_attempts"] = 2
        run = self.run_fixture()
        fresh = deepcopy(self.contract)
        fresh["attempt_id"] = "a2"
        run.new_attempt(fresh, reason="New evidence")
        fresh["attempt_id"] = "a3"
        with self.assertRaisesRegex(HarnessError, "budget exhausted"):
            run.new_attempt(fresh, reason="Try another agent")

    def test_new_attempt_cannot_increase_or_remove_attempt_limit(self):
        self.contract["budget"]["max_attempts"] = 2
        run = self.run_fixture()
        for budget in ({"max_attempts": 100}, {}):
            fresh = deepcopy(self.contract)
            fresh.update(attempt_id="a2", budget=budget)
            with self.subTest(budget=budget), self.assertRaisesRegex(HarnessError, "cannot increase"):
                run.new_attempt(fresh, reason="Retry with changed hypothesis")
        self.assertEqual(["a1"], run.state()["attempts"])

    def test_new_attempt_cannot_extend_or_remove_deadline(self):
        self.contract["budget"]["deadline"] = "2000-01-01T00:00:00Z"
        run = self.run_fixture()
        for value in (None, "2100-01-01T00:00:00Z"):
            fresh = deepcopy(self.contract)
            fresh["attempt_id"] = "a2"
            if value is None:
                fresh["budget"].pop("deadline")
            else:
                fresh["budget"]["deadline"] = value
            with self.subTest(deadline=value), self.assertRaisesRegex(HarnessError, "cannot extend or remove"):
                run.new_attempt(fresh, reason="Retry with changed hypothesis")
        self.assertEqual(["a1"], run.state()["attempts"])

    def test_new_attempt_can_tighten_budget_and_preserve_equivalent_deadline(self):
        self.contract["budget"]["deadline"] = "2100-01-01T00:00:00Z"
        run = self.run_fixture()
        fresh = deepcopy(self.contract)
        fresh["attempt_id"] = "a2"
        fresh["budget"].update(max_attempts=2, deadline="2100-01-01T03:00:00+03:00")
        run.new_attempt(fresh, reason="Smaller retry budget")
        self.assertEqual(["a1", "a2"], run.state()["attempts"])
        fresh["attempt_id"] = "a3"
        with self.assertRaisesRegex(HarnessError, "budget exhausted"):
            run.new_attempt(fresh, reason="Budget remains exhausted")

    def test_budget_rejects_ambiguous_types_without_requiring_native_settings(self):
        for key, value in (("max_attempts", True), ("native_cap", True), ("native_cap", 0), ("native_cap", 1.5), ("root_counts", 1), ("root_counts", "false")):
            contract = deepcopy(self.contract)
            contract["budget"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(HarnessError):
                validate_contract(contract)
        contract = deepcopy(self.contract)
        contract.pop("budget")
        validate_contract(contract)
        contract["budget"] = {"max_attempts": 3}
        validate_contract(contract)
        contract["budget"] = {"native_cap": 3, "root_counts": False}
        validate_contract(contract)

    def test_live_state_lock_prevents_second_writer(self):
        run = self.run_fixture()
        with lock(run.path / ".state.lock", purpose="first writer"):
            with self.assertRaisesRegex(HarnessError, "Resource busy"):
                run.checkpoint("Another writer", "Must not overwrite")

    def test_actual_git_snapshot_tracks_dirty_untracked_and_deletion(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.cwd), *args], check=True, capture_output=True)
        git("init")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "add", "main.py")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture baseline")
        baseline = snapshot(self.contract)
        (self.cwd / "main.py").write_text("dirty bytes")
        dirty = snapshot(self.contract)
        self.assertNotEqual(baseline["digest"], dirty["digest"])
        self.assertEqual(baseline["git"]["head"], dirty["git"]["head"])
        (self.cwd / "new.txt").write_text("untracked bytes")
        untracked = snapshot(self.contract)
        self.assertIn("new.txt", untracked["files"])
        (self.cwd / "main.py").unlink()
        self.assertEqual("missing", snapshot(self.contract)["files"]["main.py"]["kind"])

    def test_source_evidence_requires_captured_support_not_boolean(self):
        self.contract["checks"] = [{"id": "sources", "kind": "source", "required": True, "criteria": ["claim-supported"]}]
        run = self.run_fixture()
        run.freeze()
        with self.assertRaisesRegex(HarnessError, "needs source"):
            record_evidence(run, "sources", {"kind": "source", "status": "satisfied", "method": "source_review", "assessor": "fixture reviewer", "assessment": "A reviewed claim", "criteria": {"claim-supported": "Reason"}, "artifact_paths": []})
        capture = self.base / "capture.txt"
        capture.write_text("Fixture source says the observed value is 42.")
        proof = {"kind": "source", "status": "satisfied", "method": "source_review", "assessor": "fixture reviewer", "assessment": "Fixture assessment, not a live web test", "criteria": {"claim-supported": "The captured passage directly states 42"}, "artifact_paths": [str(capture)], "sources": [{"url": "https://example.invalid/fixture", "retrieved_at": "2026-10-31T00:00:00Z", "claim": "Fixture observed value is 42", "support": "observed value is 42", "artifact": str(capture)}]}
        record_evidence(run, "sources", proof)
        self.assertEqual("accepted", gate(run)["verdict"])

    def test_visual_evidence_requires_viewed_artifact(self):
        self.contract["checks"] = [{"id": "layout", "kind": "visual", "required": True, "criteria": ["layout-readable"]}]
        run = self.run_fixture()
        run.freeze()
        with self.assertRaisesRegex(HarnessError, "captured artifacts"):
            record_evidence(run, "layout", {"kind": "visual", "status": "satisfied", "method": "agent_visual_review", "assessor": "fixture reviewer", "assessment": "Not actually observed", "criteria": {"layout-readable": "Claim"}, "artifact_paths": []})

    def test_scope_cannot_be_laundered_by_new_attempt(self):
        run = self.run_fixture()
        (self.cwd / "foreign.txt").write_text("not allowed")
        fresh = deepcopy(self.contract)
        fresh["attempt_id"] = "a2"
        run.new_attempt(fresh, reason="Recheck")
        with self.assertRaisesRegex(HarnessError, "out-of-scope"):
            run.freeze()

    def test_secret_output_redacted_in_both_logs(self):
        key = "HARNESS_FIXTURE_SECRET"
        old = os.environ.get(key)
        os.environ[key] = "private-fixture-value-never-persist"
        try:
            self.contract["checks"][0].update({"argv": [sys.executable, "-I", "-B", "-c", "import os,sys; print(os.environ['HARNESS_FIXTURE_SECRET']); print('one real case'); print(os.environ['HARNESS_FIXTURE_SECRET'],file=sys.stderr)"]})
            run, result = self.passing()
            for artifact in result["artifacts"]:
                self.assertNotIn(os.environ[key], (run.path / artifact["path"]).read_text(encoding="utf-8"))
        finally:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def test_ids_and_scope_reject_traversal(self):
        self.contract["attempt_id"] = "../escape"
        with self.assertRaises(HarnessError):
            validate_contract(self.contract)

    def test_unavailable_native_thread_is_not_a_free_slot(self):
        run = self.run_fixture()
        run.record_owner(self.owner())
        run.record_owner(self.owner("two", status="unavailable"))
        with self.assertRaisesRegex(HarnessError, "cap exceeded"):
            run.record_owner(self.owner("three"))
        with self.assertRaisesRegex(HarnessError, "closure evidence"):
            run.record_owner(self.owner("two", status="closed"))

    def test_unknown_evidence_file_is_rejected(self):
        run, evidence = self.passing()
        (run.attempt_dir() / "checks/unit/unregistered-proof.txt").write_text("unregistered")
        self.assertTrue(any("unknown artifact" in error for error in gate(run)["errors"]))

    def test_resource_groups_exclude_overlapping_runs(self):
        self.contract["checks"][0]["resources"] = ["exclusive-fixture-resource"]
        run = self.run_fixture()
        run.freeze()
        with lock(run.resource_path("resource:exclusive-fixture-resource"), purpose="other cooperating run"):
            with self.assertRaisesRegex(HarnessError, "Resource busy"):
                run_check(run, "unit")

    def test_shared_yaml_adapter_checks_only_assigned_records(self):
        # This fixture intentionally uses a tiny custom format. A project YAML adapter
        # supplies its own real parser; core does not pretend to parse arbitrary YAML.
        source = self.cwd / "records.json"
        source.write_text(json.dumps({"U03": "old", "U04": "old"}))
        adapter = self.base / "scope_adapter.py"
        adapter.write_text("import json,sys\na,b=[json.load(open(p)) for p in sys.argv[1:3]]\nallowed=json.loads(sys.argv[3])\nassert all(a.get(k)==b.get(k) for k in set(a)|set(b) if k not in allowed)\n", encoding="utf-8")
        self.contract["scope"]["shared_files"] = [{"path": "records.json", "format": "validator", "allowed_records": ["U03"], "validator_argv": [sys.executable, "-I", "-B", str(adapter), "{baseline_file}", "{candidate_file}", "{allowed_records_json}"], "validator_files": {str(adapter): file_hash(adapter)}}]
        run = self.run_fixture()
        source.write_text(json.dumps({"U03": "old", "U04": "foreign"}))
        run.freeze()
        run_check(run, "unit")
        self.assertTrue(any("scope adapter rejected" in error for error in gate(run)["errors"]))

    def test_baseline_manifest_tampering_is_not_silently_trusted(self):
        run = self.run_fixture()
        baseline = read_json(run.path / "origin-baseline.json")
        baseline["files"]["main.py"]["sha256"] = "f" * 64
        atomic_json(run.path / "origin-baseline.json", baseline)
        with self.assertRaisesRegex(HarnessError, "Baseline manifest integrity"):
            run.freeze()

    def test_two_identical_failures_need_new_hypothesis(self):
        self.contract["checks"][0].update({"argv": [sys.executable, "-I", "-B", "-c", "raise SystemExit(7)"], "assertions": []})
        run = self.run_fixture()
        run.freeze()
        run_check(run, "unit")
        second = deepcopy(self.contract)
        second["attempt_id"] = "a2"
        run.new_attempt(second, reason="Same hypothesis")
        run.freeze()
        run_check(run, "unit")
        third = deepcopy(self.contract)
        third["attempt_id"] = "a3"
        with self.assertRaisesRegex(HarnessError, "identical failed"):
            run.new_attempt(third, reason="Same hypothesis")

    def test_integration_only_defect_cannot_hide_behind_unit_checks(self):
        (self.cwd / "main.py").write_text("left=20\nright=21\n")
        self.contract["checks"] = [
            {"id": "left", "kind": "command", "required": True, "argv": [sys.executable, "-I", "-B", "-c", "assert 20 == 20"], "timeout": 3},
            {"id": "right", "kind": "command", "required": True, "argv": [sys.executable, "-I", "-B", "-c", "assert 21 == 21"], "timeout": 3},
            {"id": "integrated", "kind": "command", "required": True, "argv": [sys.executable, "-I", "-B", "-c", "x={};exec(open('main.py').read(),x);assert x['left']+x['right']==42"], "timeout": 3}
        ]
        run = self.run_fixture()
        run.freeze()
        self.assertEqual("passed", run_check(run, "left")["status"])
        self.assertEqual("passed", run_check(run, "right")["status"])
        self.assertEqual("failed", run_check(run, "integrated")["status"])
        self.assertEqual("rejected", gate(run)["verdict"])

    def test_toolchain_change_after_freeze_is_rejected_before_execution(self):
        tool_version = self.base / "tool-version.txt"
        tool_version.write_text("version-one")
        self.contract["checks"][0]["tool_versions"] = {"fixture-tool": [sys.executable, "-I", "-B", "-c", "import sys;print(open(sys.argv[1]).read())", str(tool_version)]}
        run = self.run_fixture()
        run.freeze()
        tool_version.write_text("version-two")
        with self.assertRaisesRegex(HarnessError, "Toolchain changed"):
            run_check(run, "unit")

    def test_toolchain_change_during_check_cannot_pass(self):
        tool_version = self.base / "tool-version.txt"
        tool_version.write_text("version-one")
        self.contract["checks"][0].update({"tool_versions": {"fixture-tool": [sys.executable, "-I", "-B", "-c", "import sys;print(open(sys.argv[1]).read())", str(tool_version)]}, "argv": [sys.executable, "-I", "-B", "-c", "import sys;open(sys.argv[1],'w').write('version-two');print('one real case')", str(tool_version)]})
        run = self.run_fixture()
        run.freeze()
        self.assertEqual("toolchain_changed", run_check(run, "unit")["status"])
        self.assertEqual("rejected", gate(run)["verdict"])

    @unittest.skipUnless(os.name == "nt", "Windows suspended-creation safety is platform-specific")
    def test_containment_failure_never_runs_suspended_user_code(self):
        marker = self.base / "must-not-exist.txt"
        self.contract["checks"][0].update({"argv": [sys.executable, "-I", "-B", "-c", "import sys;open(sys.argv[1],'w').write('executed')", str(marker)], "assertions": []})
        run = self.run_fixture()
        run.freeze()
        with patch("agent_harness.runner.WindowsJob.__init__", side_effect=HarnessError("fixture denied containment")):
            result = run_check(run, "unit")
        self.assertEqual("failed", result["status"])
        self.assertFalse(marker.exists(), "User code executed before containment was established")

    def test_resume_verifies_evidence_instead_of_trusting_passed_state(self):
        run, evidence = self.passing()
        gate(run)
        (run.path / evidence["artifacts"][0]["path"]).write_text("tampered after interruption")
        resumed = Run(run.path).resume()
        self.assertFalse(resumed["safe_to_start_new_owner"])
        self.assertTrue(any("integrity" in message for message in resumed["resume"]["problems"]))

    def test_resume_revalidates_accepted_dependency_source_and_evidence(self):
        for mutation in ("source", "artifact"):
            with self.subTest(mutation=mutation):
                upstream = self.run_fixture(name=f"upstream-{mutation}")
                upstream.freeze()
                upstream_result = run_check(upstream, "unit")
                upstream_verdict = gate(upstream)
                downstream_cwd = self.base / f"downstream-source-{mutation}"
                downstream_cwd.mkdir()
                (downstream_cwd / "main.py").write_text("independent downstream candidate\n")
                child = deepcopy(self.contract)
                child.update(run_id=f"downstream-{mutation}", cwd=str(downstream_cwd), dependencies=[{"run_dir": str(upstream.path), "verdict_digest": upstream_verdict["digest"]}])
                downstream = self.run_fixture(child, name=f"downstream-run-{mutation}")
                downstream.freeze()
                run_check(downstream, "unit")
                self.assertEqual("accepted", gate(downstream)["verdict"])
                if mutation == "source":
                    (self.cwd / "main.py").write_text("changed upstream\n")
                else:
                    (upstream.path / upstream_result["artifacts"][0]["path"]).unlink()
                resumed = downstream.resume()
                self.assertFalse(resumed["safe_to_start_new_owner"])
                self.assertTrue(any("accepted result" in problem for problem in resumed["resume"]["problems"]))
                self.assertEqual("blocked", downstream.state()["status"])
                self.assertIsNone(downstream.state()["accepted"])

    def test_capacity_preflight_requires_native_reconciliation(self):
        run = self.run_fixture()
        self.assertTrue(run.capacity()["spawn_allowed"])
        run.record_owner(self.owner(status="unavailable"))
        capacity = run.capacity()
        self.assertEqual(1, capacity["remaining"])
        self.assertFalse(capacity["spawn_allowed"])

    def test_resume_observes_real_live_process_identity(self):
        run = self.run_fixture()
        process = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(30)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with run.edit() as state:
                state["processes"] = {"fixture-live": {"identity": process_identity(process.pid), "status": "running"}}
            resumed = run.resume()
            self.assertFalse(resumed["safe_to_start_new_owner"])
            self.assertTrue(any("still alive" in message for message in resumed["resume"]["problems"]))
        finally:
            process.terminate()
            process.wait(timeout=5)

    @unittest.skipIf(os.name == "nt", "POSIX process namespace observation")
    def test_process_identity_rejects_stale_proc_entry_after_esrch(self):
        # A /proc mount can expose a different namespace than kill(2). A stale
        # entry must never keep a completed check alive in the run ledger.
        with patch("agent_harness.core.os.kill", side_effect=ProcessLookupError()), patch("agent_harness.core.Path.read_text") as read_stat:
            self.assertIsNone(process_identity(12345))
        read_stat.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX process namespace observation")
    def test_process_identity_does_not_treat_permission_denied_as_dead(self):
        with patch("agent_harness.core.os.kill", side_effect=PermissionError()), patch("agent_harness.core.Path.exists", return_value=False):
            self.assertEqual({"pid": 12345, "created": "unknown"}, process_identity(12345))
        with patch("agent_harness.core.os.kill", side_effect=PermissionError()), patch("agent_harness.core.Path.exists", return_value=True), patch("agent_harness.core.Path.read_text", side_effect=PermissionError()):
            self.assertTrue(identity_alive({"pid": 12345, "created": "previously-observed"}))

    def test_expired_deadline_does_not_start_checks_or_new_owner(self):
        self.contract["budget"]["deadline"] = "2000-01-01T00:00:00Z"
        run = self.run_fixture()
        with self.assertRaisesRegex(AcceptanceDeadlineExhausted, "deadline exhausted"):
            run.freeze()
        with self.assertRaisesRegex(AcceptanceDeadlineExhausted, "check not_run"):
            run_check(run, "unit")
        with self.assertRaisesRegex(HarnessError, "deadline exhausted"):
            run.record_owner(self.owner())

    def test_deadline_bounds_the_real_owned_process(self):
        self.contract["checks"][0].update({"argv": [sys.executable, "-I", "-B", "-c", "import time;time.sleep(30)"], "assertions": [], "timeout": 5})
        self.contract["budget"]["deadline"] = (datetime.now(timezone.utc) + timedelta(seconds=1.5)).isoformat()
        run = self.run_fixture()
        run.freeze()
        result = run_check(run, "unit")
        self.assertEqual("budget_exhausted", result["status"])
        self.assertFalse(identity_alive(result["process"]["identity"]))

    def test_wrong_or_fabricated_git_base_is_rejected_without_creating_run(self):
        self.contract["base_revision"] = "f" * 40
        with self.assertRaisesRegex(HarnessError, "actual initial Git checkout"):
            self.run_fixture()
        self.assertFalse((self.base / "run").exists())
        self.contract["attempt_id"] = "a1"
        self.contract["scope"]["write_paths"] = ["../other"]
        with self.assertRaises(HarnessError):
            validate_contract(self.contract)


if __name__ == "__main__":
    unittest.main()
