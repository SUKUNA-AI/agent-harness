from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_harness.mcp_server import MAX_FILE_BYTES, MAX_OUTPUT_BYTES, MetadataError, RunMetadataStore, _bounded


class MetadataTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(os.environ.get("HARNESS_TEST_TMP", ROOT / ".test-tmp"))
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-", dir=temp_root)
        self.base = Path(self.temp.name)
        self.store = self.base / "private store"
        self.run = self.store / "demo"
        self.run.mkdir(parents=True)
        self.state = {"schema_version": 1, "run_id": "demo", "task_id": "task", "workflow": "change",
                      "current_attempt": "a1", "status": "accepted", "checkpoints": 1,
                      "created_at": "2026-10-31T12:00:00Z", "updated_at": "2026-10-31T12:01:00Z",
                      "candidate_digest": "a" * 64, "contract_digest": "b" * 64,
                      "goal": "PRIVATE_PROMPT_SENTINEL", "cwd": "PRIVATE_PATH_SENTINEL",
                      "credentials": "PRIVATE_CREDENTIAL_SENTINEL",
                      "owners": {"builder": {"lane_id": "lane-1", "status": "running",
                                              "observation": "PRIVATE_LOG_SENTINEL", "native_ref": "PRIVATE_REF_SENTINEL",
                                              "needs_native_reconciliation": True}}}
        self.write_state()
        check_dir = self.run / "attempts" / "a1" / "checks" / "unit"
        check_dir.mkdir(parents=True)
        self.check = check_dir / "result.json"
        self.check.write_text(json.dumps({"check_id": "unit", "attempt_id": "a1", "candidate_digest": "a" * 64,
                                          "status": "passed", "kind": "command", "exit_code": 0,
                                          "stdout": "PRIVATE_OUTPUT_SENTINEL", "argv": ["PRIVATE_COMMAND_SENTINEL"],
                                          "error": "PRIVATE_ERROR_SENTINEL", "artifacts": ["PRIVATE_ARTIFACT_SENTINEL"]}), encoding="utf-8")
        (self.run / "raw.log").write_text("PRIVATE_FILE_SENTINEL", encoding="utf-8")
        self.reader = RunMetadataStore(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self):
        (self.run / "state.json").write_text(json.dumps(self.state), encoding="utf-8")

    def snapshot(self):
        return {p.relative_to(self.store).as_posix(): p.read_bytes() for p in self.store.rglob("*") if p.is_file()}

    def test_allowlist_never_exposes_raw_fields_or_writes(self):
        before = self.snapshot()
        with patch("subprocess.run", side_effect=AssertionError("No commands")), patch("socket.socket", side_effect=AssertionError("No network")):
            summary = self.reader.list_runs()
            status = self.reader.run_status("demo")
        self.assertEqual("accepted", status["recorded_status"])
        self.assertEqual("not_checked", status["evidence_freshness"])
        self.assertEqual("passed", status["checks"][0]["recorded_status"])
        self.assertTrue(status["checks"][0]["matches_recorded_candidate"])
        self.assertEqual("builder", status["owners"][0]["owner_id"])
        self.assertNotIn("PRIVATE_", json.dumps([summary, status]))
        self.assertNotIn("goal", status)
        self.assertEqual(before, self.snapshot())

    def test_reject_paths_and_traversal_without_echo(self):
        for value in ("../outside", "..", "/tmp", "a/b", "a\\b", "C:\\secret", ".", "x\0", "x" * 81):
            with self.subTest(value=value), self.assertRaises(MetadataError) as error:
                self.reader.run_status(value)
            self.assertNotIn(value, str(error.exception))

    def test_explicit_existing_absolute_store_required(self):
        for value in ("relative-store", self.base / "missing", self.store / ".." / "private store"):
            with self.subTest(value=value), self.assertRaises((MetadataError, OSError)):
                RunMetadataStore(value)

    def test_cli_requires_store_before_sdk_startup(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "tools/harness_mcp.py")], capture_output=True, text=True, timeout=10)
        self.assertEqual(2, result.returncode)
        self.assertIn("--store", result.stderr)
        self.assertEqual("", result.stdout)

    def test_canonical_store_aliases_are_normalized_without_following_links(self):
        value = str(self.store)
        if os.name == "nt":
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            size = ctypes.windll.kernel32.GetShortPathNameW(value, buffer, len(buffer))
            self.assertGreater(size, 0)
            value = buffer.value
        reader = RunMetadataStore(value)
        self.assertEqual(self.store.resolve(), reader.root)
        self.assertEqual("demo", reader.run_status("demo")["run_id"])

    def test_invalid_oversize_or_deep_json_fails_closed(self):
        for data in (b"{broken", b"[]", b'{"schema_version":NaN}', b'{"run_id":"a","run_id":"b"}',
                     b" " * (MAX_FILE_BYTES + 1), b'{"x":' + b"[" * 40 + b"0" + b"]" * 40 + b"}"):
            with self.subTest(size=len(data)):
                (self.run / "state.json").write_bytes(data)
                with self.assertRaises(MetadataError):
                    self.reader.run_status("demo")
                self.assertEqual([], self.reader.list_runs()["runs"])

    def test_wrong_identity_and_attempt_escape_rejected(self):
        for change in ({"run_id": "outside"}, {"current_attempt": "../outside"}, {"schema_version": True}):
            state = deepcopy(self.state)
            self.state.update(change)
            self.write_state()
            with self.assertRaises(MetadataError):
                self.reader.run_status("demo")
            self.state = state

    def test_pagination_input_and_output_bounds(self):
        second = self.store / "other"
        second.mkdir()
        (second / "state.json").write_text(json.dumps({**self.state, "run_id": "other"}), encoding="utf-8")
        first = self.reader.list_runs(limit=1)
        self.assertEqual(1, first["next_offset"])
        self.assertEqual("other", self.reader.list_runs(limit=1, offset=1)["runs"][0]["run_id"])
        for values in ((0, 0), (51, 0), (True, 0), (2, -1), (2, 999999), (2.5, 0)):
            with self.assertRaises(MetadataError):
                self.reader.list_runs(*values)
        with self.assertRaises(MetadataError):
            _bounded({"x": "x" * MAX_OUTPUT_BYTES})
        with patch("agent_harness.mcp_server.MAX_DIRECTORY_ENTRIES", 1), self.assertRaises(MetadataError):
            self.reader.list_runs()

    def test_check_and_owner_limits_fail_instead_of_partial_success(self):
        self.state["owners"] = {f"owner-{i}": {} for i in range(101)}
        self.write_state()
        with self.assertRaises(MetadataError):
            self.reader.run_status("demo")
        self.state["owners"] = {}
        self.write_state()
        with patch("agent_harness.mcp_server.MAX_CHECKS", 0), self.assertRaises(MetadataError):
            self.reader.run_status("demo")

    def test_missing_and_mismatched_checks_do_not_appear_passed(self):
        self.check.write_text('{"check_id":"wrong","status":"passed"}', encoding="utf-8")
        with self.assertRaises(MetadataError):
            self.reader.run_status("demo")
        self.check.unlink()
        with self.assertRaises(MetadataError):
            self.reader.run_status("demo")

    def test_running_check_without_final_result_keeps_status_available(self):
        self.check.unlink()
        self.state.update(status="verifying", processes={"unit": {"status": "running", "attempt_id": "a1"}})
        self.write_state()
        before = self.snapshot()
        result = self.reader.run_status("demo")
        check = result["checks"][0]
        self.assertEqual("running", check["recorded_status"])
        self.assertIs(check["result_available"], False)
        self.assertIsNone(check["matches_recorded_candidate"])
        self.assertEqual("not_checked", result["evidence_freshness"])
        self.assertEqual("builder", result["owners"][0]["owner_id"])
        self.assertEqual(before, self.snapshot())

    def test_check_folder_before_process_registration_is_unknown(self):
        self.check.unlink()
        self.state["status"] = "verifying"
        self.write_state()
        check = self.reader.run_status("demo")["checks"][0]
        self.assertEqual("unknown", check["recorded_status"])
        self.assertIs(check["result_available"], False)

    def test_missing_result_rechecks_after_process_finalizes(self):
        final = self.check.read_bytes()
        self.check.unlink()
        self.state.update(status="verifying", processes={"unit": {"status": "running", "attempt_id": "a1"}})
        self.write_state()
        original = self.reader._read
        published = False

        def publish_after_missing(*parts, **kwargs):
            nonlocal published
            value = original(*parts, **kwargs)
            if parts[-1] == "result.json" and value is None and not published:
                published = True
                self.check.write_bytes(final)
                self.state["processes"]["unit"]["status"] = "passed"
                self.write_state()
            return value

        with patch.object(self.reader, "_read", side_effect=publish_after_missing):
            check = self.reader.run_status("demo")["checks"][0]
        self.assertTrue(published)
        self.assertEqual("passed", check["recorded_status"])
        self.assertTrue(check["result_available"])

    def test_in_progress_does_not_hide_corrupt_or_wrong_attempt_records(self):
        self.state.update(status="verifying", processes={"unit": {"status": "running", "attempt_id": "a1"}})
        self.write_state()
        self.check.write_bytes(b"{invalid")
        with self.assertRaises(MetadataError):
            self.reader.run_status("demo")
        self.check.unlink()
        for status, attempt in (("passed", "a1"), ("running", "previous")):
            self.state["processes"]["unit"] = {"status": status, "attempt_id": attempt}
            self.write_state()
            with self.assertRaises(MetadataError):
                self.reader.run_status("demo")

    def test_file_and_directory_symlink_escapes_rejected(self):
        external = self.base / "outside"
        external.mkdir()
        secret = external / "state.json"
        secret.write_text(json.dumps(self.state), encoding="utf-8")
        link = self.store / "linked"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(external), str(link))
        else:
            link.symlink_to(external, target_is_directory=True)
        self.assertEqual([], [row for row in self.reader.list_runs()["runs"] if row["run_id"] == "linked"])
        with self.assertRaises(MetadataError):
            self.reader.run_status("linked")
        with self.assertRaises(MetadataError):
            RunMetadataStore(link)
        if os.name == "nt":
            # A real directory junction above exercises Windows reparse escape.
            # File-symlink creation requires extra OS privilege; also cover the
            # same file reparse guard without changing those machine privileges.
            original_lstat = Path.lstat

            def reparse_file(path, *args, **kwargs):
                if path == self.run / "state.json":
                    return SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", reparse_file), self.assertRaises(MetadataError):
                self.reader.run_status("demo")
        else:
            (self.run / "state.json").unlink()
            (self.run / "state.json").symlink_to(secret)
            with self.assertRaises(MetadataError):
                self.reader.run_status("demo")

    def test_hardlinks_and_handle_escape_rejected(self):
        external = self.base / "outside.json"
        external.write_text(json.dumps(self.state), encoding="utf-8")
        (self.run / "state.json").unlink()
        os.link(external, self.run / "state.json")
        with self.assertRaises(MetadataError):
            self.reader.run_status("demo")
        (self.run / "state.json").unlink()
        self.write_state()
        with patch("agent_harness.mcp_server._opened_path", return_value=external), self.assertRaises(MetadataError):
            self.reader.run_status("demo")

    def test_untrusted_types_not_reflected_or_crash(self):
        self.state.update({"status": {"secret": "PRIVATE_SENTINEL"}, "workflow": [], "updated_at": "PRIVATE_DATE", "candidate_digest": "PRIVATE_DIGEST"})
        self.write_state()
        result = self.reader.run_status("demo")
        self.assertEqual("unknown", result["recorded_status"])
        self.assertEqual("unknown", result["workflow"])
        self.assertIsNone(result["updated_at"])
        self.assertNotIn("PRIVATE", json.dumps(result))

if __name__ == "__main__":
    unittest.main()
