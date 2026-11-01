"""Acceptance regressions with isolated workspaces and real local commands."""
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
import json
import os
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, Run, file_hash, read_json
from agent_harness import runner


class RunnerAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-audit-", dir=os.environ.get("HARNESS_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.cwd = self.base / "workspace with spaces"
        self.cwd.mkdir()
        (self.cwd / "main.py").write_text("value = 1\n", encoding="utf-8")
        self.contract = {
            "schema_version": 1, "run_id": "runner-audit", "task_id": "fixture", "attempt_id": "a1",
            "workflow": "change", "process_level": "standard", "goal": "Verify isolated acceptance fixtures",
            "cwd": str(self.cwd), "permissions": ["read", "write", "test"],
            "scope": {"write_paths": ["records.json"], "exclude": [".tools/**"]},
            "checks": [{"id": "unit", "kind": "command", "required": True,
                        "argv": [sys.executable, "-I", "-B", "-c", "print('fixture passed')"], "timeout": 5}],
        }
        self.counter = 0

    def create(self, contract=None):
        self.counter += 1
        return Run.create(self.base / ("run-" + str(self.counter)), deepcopy(contract or self.contract))

    def finish(self, run):
        run.freeze()
        for check in run.contract()["checks"]:
            result = runner.run_check(run, check["id"])
            self.assertEqual("passed", result["status"], result)
        return runner.gate(run)

    def junit(self, xml, required):
        report = self.base / "report.xml"
        self.contract["checks"][0].update(
            argv=[sys.executable, "-I", "-B", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')", str(report), xml],
            reports=[{"path": str(report), "format": "junit", "max_skipped": 1, "required_cases": required}],
        )
        run = self.create()
        run.freeze()
        return runner.run_check(run, "unit"), runner.gate(run)

    def records(self, before, after, definition=None):
        path = self.cwd / "records.json"
        path.write_text(json.dumps(before), encoding="utf-8")
        self.contract["scope"]["shared_files"] = [{"path": "records.json", "format": "json-records", "allowed_records": ["mine"], **(definition or {})}]
        run = self.create()
        path.write_text(json.dumps(after), encoding="utf-8")
        return self.finish(run)

    def executable(self, name="verify", text="fixture passed", cwd=None):
        directory = (cwd or self.cwd) / ".tools"
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text("#!/bin/sh\nprintf '%s\\n' '" + text + "'\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_required_case_cannot_use_optional_skip_allowance(self):
        result, verdict = self.junit('<testsuite><testcase name="must_run"><skipped/></testcase><testcase name="optional"/></testsuite>', ["must_run"])
        self.assertEqual("failed", result["status"])
        self.assertIn("mandatory case did not pass: must_run", result["assertion_errors"])
        self.assertEqual("rejected", verdict["verdict"])

    def test_required_pass_with_optional_skip_is_accepted(self):
        result, verdict = self.junit('<testsuite><testcase name="must_run"/><testcase name="optional"><skipped/></testcase></testsuite>', ["must_run"])
        self.assertEqual("passed", result["status"])
        self.assertEqual("accepted", verdict["verdict"])

    def test_required_regex_means_at_least_one_passed_match(self):
        result, verdict = self.junit('<testsuite><testcase name="family_one"><skipped/></testcase><testcase name="family_two"/></testsuite>', ["family_"])
        self.assertEqual("passed", result["status"])
        self.assertEqual("accepted", verdict["verdict"])

    def test_failed_case_still_rejects_report_with_another_matching_pass(self):
        result, verdict = self.junit('<testsuite><testcase name="family_one"><failure/></testcase><testcase name="family_two"/></testsuite>', ["family_"])
        self.assertEqual("failed", result["status"])
        self.assertIn("test report contains failures", result["assertion_errors"])
        self.assertEqual("rejected", verdict["verdict"])

    def test_duplicate_candidate_cannot_hide_foreign_insertion(self):
        before = [{"id": "mine", "value": 0}, {"id": "theirs", "value": 1}]
        after = [before[0], {"id": "theirs", "value": 999}, before[1]]
        verdict = self.records(before, after)
        self.assertEqual("rejected", verdict["verdict"])
        self.assertTrue(any("candidate contains duplicate record ID: theirs" in error for error in verdict["errors"]))

    def test_duplicate_baseline_is_rejected(self):
        rows = [{"id": "mine"}, {"id": "theirs", "value": 999}, {"id": "theirs", "value": 1}]
        verdict = self.records(rows, rows)
        self.assertEqual("rejected", verdict["verdict"])
        self.assertTrue(any("baseline contains duplicate record ID: theirs" in error for error in verdict["errors"]))

    def test_invalid_record_shapes_and_id_types_fail_closed(self):
        invalid = [{}, ["not an object"], [{"value": 1}], *[[{"id": value}] for value in (None, True, False, 1.5, [], {}, "")]]
        valid = [{"id": "mine"}]
        for side in ("baseline", "candidate"):
            for value in invalid:
                with self.subTest(side=side, value=value):
                    before, after = (value, valid) if side == "baseline" else (valid, value)
                    verdict = self.records(before, after)
                    self.assertEqual("rejected", verdict["verdict"])
                    self.assertTrue(any("invalid shared records" in error for error in verdict["errors"]))

    def test_invalid_json_reported_as_scope_error(self):
        path = self.cwd / "records.json"
        path.write_text('[{"id":"mine"}]', encoding="utf-8")
        self.contract["scope"]["shared_files"] = [{"path": "records.json", "format": "json-records", "allowed_records": ["mine"]}]
        run = self.create()
        path.write_text('{"unterminated":', encoding="utf-8")
        verdict = self.finish(run)
        self.assertEqual("rejected", verdict["verdict"])
        self.assertTrue(any("invalid shared records" in error for error in verdict["errors"]))

    def test_records_key_requires_objects_with_array_values(self):
        valid = {"records": [{"id": "mine"}], "metadata": "preserved"}
        for invalid in ([], {}, {"records": {}}, {"records": None}):
            for side in ("baseline", "candidate"):
                with self.subTest(side=side, value=invalid):
                    before, after = (invalid, valid) if side == "baseline" else (valid, invalid)
                    verdict = self.records(before, after, {"records_key": "records"})
                    self.assertEqual("rejected", verdict["verdict"])
                    self.assertTrue(any("invalid shared records" in error for error in verdict["errors"]))

    def test_integer_and_string_ids_are_distinct_and_preserve_metadata(self):
        before = {"rows": [{"key": 1, "value": 0}, {"key": "1", "value": 1}], "metadata": "preserved"}
        after = deepcopy(before)
        after["rows"][0]["value"] = 2
        definition = {"records_key": "rows", "id_key": "key", "allowed_records": [1]}
        self.assertEqual("accepted", self.records(before, after, definition)["verdict"])
        after["metadata"] = "changed"
        verdict = self.records(before, after, definition)
        self.assertIn("shared metadata changed: records.json", verdict["errors"])

    def test_boolean_allowed_id_cannot_authorize_integer_id(self):
        verdict = self.records([{"id": 1, "value": 0}], [{"id": 1, "value": 2}], {"allowed_records": [True]})
        self.assertEqual("rejected", verdict["verdict"])
        self.assertTrue(any("invalid shared records" in error for error in verdict["errors"]))

    def test_custom_validator_keeps_its_own_record_semantics(self):
        adapter = self.base / "custom_scope.py"
        adapter.write_text("import json, sys\na, b = [json.load(open(p)) for p in sys.argv[1:3]]\nassert a['foreign'] == b['foreign']\n", encoding="utf-8")
        definition = {"format": "validator", "validator_argv": [sys.executable, "-I", "-B", str(adapter), "{baseline_file}", "{candidate_file}"], "validator_files": {str(adapter): file_hash(adapter)}}
        before = {"mine": {"custom": None}, "foreign": 1}
        after = {"mine": {"custom": []}, "foreign": 1}
        self.assertEqual("accepted", self.records(before, after, definition)["verdict"])
        after["foreign"] = 2
        verdict = self.records(before, after, definition)
        self.assertEqual("rejected", verdict["verdict"])
        self.assertTrue(any("scope adapter rejected" in error for error in verdict["errors"]))

    @unittest.skipUnless(os.name == "posix", "POSIX executable search uses the child cwd")
    def test_changed_relative_executable_is_rejected_after_freeze(self):
        executable = self.executable()
        self.contract["checks"][0]["argv"] = ["./.tools/verify"]
        run = self.create()
        run.freeze()
        tools = read_json(run.attempt_dir() / "toolchains.json")["unit"]["executable"]
        self.assertEqual(str(executable), tools["resolved"])
        self.assertEqual(file_hash(executable), tools["sha256"])
        self.executable(text="different tool")
        with self.assertRaisesRegex(HarnessError, "Toolchain changed"):
            runner.run_check(run, "unit")
        self.assertFalse((run.attempt_dir() / "checks/unit/result.json").exists())

    def test_version_probe_cannot_replace_the_executable_fingerprint(self):
        check = self.contract["checks"][0]
        check["tool_versions"] = {"executable": [sys.executable, "--version"]}
        directory = self.base / "invalid-run"
        with self.assertRaisesRegex(HarnessError, "'executable' is reserved"):
            Run.create(directory, self.contract)
        self.assertFalse(directory.exists(), "Invalid contracts must fail before persistence")
        # Direct callers of the fingerprint helper must also fail closed.
        with patch.object(runner, "_contained_capture") as capture:
            with self.assertRaisesRegex(HarnessError, "'executable' is reserved"):
                runner._versions(check, cwd=self.cwd)
            capture.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX executable search uses the child cwd")
    def test_normal_version_probe_keeps_changed_executable_detection(self):
        self.executable(text="original command")
        self.contract["checks"][0].update(argv=["./.tools/verify"], tool_versions={"python": [sys.executable, "--version"]})
        run = self.create()
        run.freeze()
        tools = read_json(run.attempt_dir() / "toolchains.json")["unit"]
        self.assertIn("sha256", tools["executable"])
        self.assertIn("Python", tools["python"]["version"])
        self.executable(text="changed command")
        with self.assertRaisesRegex(HarnessError, "Toolchain changed"):
            runner.run_check(run, "unit")
        self.assertEqual("rejected", runner.gate(run)["verdict"])

    @unittest.skipUnless(os.name == "posix", "POSIX executable search uses the child cwd")
    def test_unchanged_relative_executable_in_overridden_cwd_is_accepted(self):
        other = self.base / "different cwd"
        other.mkdir()
        executable = self.executable(cwd=other)
        self.contract["checks"][0].update(argv=["./.tools/verify"], cwd=str(other))
        run = self.create()
        self.assertEqual("accepted", self.finish(run)["verdict"])
        tools = read_json(run.attempt_dir() / "toolchains.json")["unit"]["executable"]
        self.assertEqual(str(executable), tools["resolved"])

    @unittest.skipUnless(os.name == "posix", "POSIX PATH entries are relative to the child cwd")
    def test_relative_path_entry_uses_command_cwd(self):
        executable = self.executable()
        self.contract["checks"][0]["argv"] = ["verify"]
        with patch.dict(os.environ, {"PATH": ".tools" + os.pathsep + os.environ.get("PATH", os.defpath)}):
            run = self.create()
            self.assertEqual("accepted", self.finish(run)["verdict"])
        tools = read_json(run.attempt_dir() / "toolchains.json")["unit"]["executable"]
        self.assertEqual(str(executable), tools["resolved"])

    @unittest.skipUnless(os.name == "posix", "Symlink executable fixture")
    def test_executable_fingerprint_preserves_invoked_symlink_path(self):
        target = self.executable()
        link = target.with_name("alias")
        link.symlink_to(target.name)
        self.contract["checks"][0]["argv"] = ["./.tools/alias"]
        run = self.create()
        self.assertEqual("accepted", self.finish(run)["verdict"])
        tools = read_json(run.attempt_dir() / "toolchains.json")["unit"]["executable"]
        self.assertEqual(str(link), tools["resolved"])
        self.assertEqual(file_hash(target), tools["sha256"])

    def test_version_probe_runs_in_effective_check_cwd(self):
        other = self.base / "version project"
        other.mkdir()
        (other / "VERSION").write_text("project-v2", encoding="utf-8")
        self.contract["checks"][0].update(cwd=str(other), tool_versions={"local": [sys.executable, "-I", "-B", "-c", "from pathlib import Path; print(Path('VERSION').read_text())"]})
        run = self.create()
        self.assertEqual("accepted", self.finish(run)["verdict"])
        self.assertEqual("project-v2", read_json(run.attempt_dir() / "toolchains.json")["unit"]["local"]["version"])

    @unittest.skipUnless(os.name == "nt", "Windows executable search uses the parent cwd")
    def test_windows_relative_executable_keeps_native_parent_search(self):
        try:
            relative = os.path.relpath(sys.executable, Path.cwd())
        except ValueError:
            self.skipTest("Python and checkout are on different drives")
        self.contract["checks"][0]["argv"][0] = relative
        run = self.create()
        self.assertEqual("accepted", self.finish(run)["verdict"])
        tools = read_json(run.attempt_dir() / "toolchains.json")["unit"]["executable"]
        self.assertEqual(os.path.normcase(os.path.abspath(sys.executable)), os.path.normcase(tools["resolved"]))

    def test_persistent_lock_guard_does_not_contaminate_check_evidence(self):
        real_lock = runner.lock

        @contextmanager
        def persistent_guard(path, **kwargs):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.with_name(path.name + ".guard").touch()
            with real_lock(path, **kwargs) as lease:
                yield lease

        run = self.create()
        with patch.object(runner, "lock", persistent_guard):
            verdict = self.finish(run)
        self.assertEqual("accepted", verdict["verdict"])
        self.assertTrue(list(run.path.rglob("*.guard")))
        self.assertFalse(list((run.attempt_dir() / "checks").rglob("*.guard")))

    def test_identical_probes_are_deduplicated_only_within_each_phase(self):
        check = self.contract["checks"][0]
        check["tool_versions"] = {"first": [sys.executable, "--version"], "alias": [sys.executable, "--version"]}
        self.contract["checks"] = [{**deepcopy(check), "id": "check" + str(i)} for i in range(3)]
        run = self.create()
        with patch.object(runner, "_contained_capture", wraps=runner._contained_capture) as capture, patch.object(runner, "file_hash", wraps=runner.file_hash) as hashes:
            self.assertEqual("accepted", self.finish(run)["verdict"])
        # One shared freeze probe + independent before/after probes for 3 checks.
        self.assertEqual(7, capture.call_count)
        executable_hashes = [call for call in hashes.call_args_list if Path(call.args[0]).resolve() == Path(sys.executable).resolve()]
        # Main executable and both probe aliases share one physical file. Each
        # phase still reads fresh bytes, even though size/timestamps are unchanged.
        self.assertEqual(7, len(executable_hashes))

    @unittest.skipUnless(os.name == "posix", "POSIX self-replacing command fixture")
    def test_executable_replacement_during_check_is_detected_after_execution(self):
        executable = self.executable()
        code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('#!/bin/sh\\nexit 0\\n', encoding='utf-8')"
        executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -I -B -c " + shlex.quote(code) + ' "$0"\n', encoding="utf-8")
        self.contract["checks"][0]["argv"] = ["./.tools/verify"]
        run = self.create()
        run.freeze()
        result = runner.run_check(run, "unit")
        self.assertEqual(0, result["exit_code"])
        self.assertEqual("toolchain_changed", result["status"])
        self.assertNotEqual(result["tools"]["executable"]["sha256"], result["tools_after"]["executable"]["sha256"])
        self.assertEqual("rejected", runner.gate(run)["verdict"])

    def test_probe_cache_separates_cwd_environment_and_argv(self):
        other = self.base / "other"
        other.mkdir()
        check = deepcopy(self.contract["checks"][0])
        argv = [sys.executable, "-I", "-B", "-c", "import os, sys; print(os.getcwd(), os.environ.get('HARNESS_FIXTURE_VERSION'), sys.argv[1])", "one"]
        check["tool_versions"] = {"probe": argv}
        cache = {}
        with patch.object(runner, "_contained_capture", wraps=runner._contained_capture) as capture:
            with patch.dict(os.environ, {"HARNESS_FIXTURE_VERSION": "first"}):
                first = runner._versions(check, cwd=self.cwd, probe_cache=cache)
                same = runner._versions(check, cwd=self.cwd, probe_cache=cache)
                moved = runner._versions(check, cwd=other, probe_cache=cache)
            with patch.dict(os.environ, {"HARNESS_FIXTURE_VERSION": "second"}):
                changed_env = runner._versions(check, cwd=other, probe_cache=cache)
                check["tool_versions"]["probe"] = [*argv[:-1], "two"]
                changed_argv = runner._versions(check, cwd=other, probe_cache=cache)
        self.assertEqual(first, same)
        self.assertEqual(4, capture.call_count)
        self.assertEqual(4, len({result["probe"]["version"] for result in (first, moved, changed_env, changed_argv)}))
        self.assertEqual({"executable", "probe"}, set(first))
        self.assertEqual({"argv", "exit_code", "version"}, set(first["probe"]))

    @unittest.skipUnless(os.name == "posix", "POSIX executable probe fixture")
    def test_probe_cache_separates_changed_executable_bytes(self):
        self.executable(name="probe", text="first version")
        check = deepcopy(self.contract["checks"][0])
        check["tool_versions"] = {"probe": ["./.tools/probe"]}
        cache, fingerprints = {}, {}
        with patch.object(runner, "_contained_capture", wraps=runner._contained_capture) as capture:
            first = runner._versions(check, cwd=self.cwd, probe_cache=cache, fingerprint_cache=fingerprints)
            self.executable(name="probe", text="second version")
            second = runner._versions(check, cwd=self.cwd, probe_cache=cache, fingerprint_cache=fingerprints)
        self.assertEqual(2, capture.call_count)
        self.assertNotEqual(first["probe"]["version"], second["probe"]["version"])


if __name__ == "__main__":
    unittest.main()
