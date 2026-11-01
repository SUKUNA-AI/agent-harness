from __future__ import annotations

from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_harness import presentation
from agent_harness.core import HarnessError, Run, atomic_json, digest
from agent_harness.presentation import handoff, render_json, status_summary


class PresentationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="harness-presentation-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "main.py").write_bytes(b"value = 1\n")
        self.contract = {"schema_version": 1, "run_id": "run", "task_id": "task", "attempt_id": "a1",
                         "workflow": "change", "process_level": "standard", "goal": "Inspect parser result",
                         "cwd": str(self.source), "permissions": ["read", "test"],
                         "checks": [{"id": "unit", "kind": "command", "required": True,
                                     "argv": [sys.executable, "-I", "-B", "-c", "print('fixture')"], "timeout": 3}]}
        self.run = Run.create(self.base / "run", self.contract)

    def files(self):
        return {p.relative_to(self.base).as_posix(): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}

    def save_checkpoint(self, note="Inspect the failing parser edge case", next_step="Read the captured failure before editing"):
        return self.run.checkpoint(note, next_step)

    def edit_checkpoint(self, change):
        path = self.run.path / "checkpoints/000001.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        change(value)
        value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
        atomic_json(path, value)

    def result(self, status="passed", **changes):
        state = self.run.state()
        value = {"run_id": state["run_id"], "task_id": state["task_id"], "attempt_id": state["current_attempt"],
                 "check_id": "unit", "contract_digest": state["contract_digest"],
                 "candidate_digest": state.get("candidate_digest"), "status": status, **changes}
        value["digest"] = digest(value)
        path = self.run.path / f"attempts/{state['current_attempt']}/checks/unit/result.json"
        atomic_json(path, value)
        return path

    def test_current_handoff_returns_bound_notes_without_writes_or_resume(self):
        saved = self.save_checkpoint()
        before = self.files()
        with patch.object(self.run, "resume", side_effect=AssertionError("No resume")), \
                patch.object(self.run, "status", side_effect=AssertionError("No implicit verification")), \
                patch("subprocess.run", side_effect=AssertionError("No commands")):
            value = handoff(self.run)
        self.assertEqual("current", value["checkpoint"]["status"])
        self.assertEqual(saved["note"], value["checkpoint"]["note"]["text"])
        self.assertEqual(saved["next_safe_step"], value["checkpoint"]["next_safe_step"]["text"])
        self.assertEqual("not_checked", value["evidence_validation"]["status"])
        self.assertIsNone(value["evidence_current"])
        self.assertIsNone(value["source_current"])
        self.assertEqual("not_claimed", value["native_resume"])
        self.assertEqual(before, self.files())

    def test_no_checkpoint_is_explicit_not_recorded(self):
        value = handoff(self.run)
        self.assertEqual("not_recorded", value["checkpoint"]["status"])
        self.assertNotIn("note", value["checkpoint"])
        self.assertNotIn("next_safe_step", value["checkpoint"])

    def test_previous_attempt_never_supplies_current_instructions(self):
        self.save_checkpoint("OLD_NOTE", "OLD_NEXT")
        fresh = deepcopy(self.contract)
        fresh["attempt_id"] = "a2"
        self.run.new_attempt(fresh, reason="New scoped attempt")
        before = self.files()
        value = handoff(self.run)
        self.assertEqual("a2", value["current_attempt"])
        self.assertEqual("stale", value["checkpoint"]["status"])
        self.assertNotIn("OLD_", render_json(value))
        self.assertEqual(before, self.files())

    def test_foreign_run_and_task_never_supply_notes(self):
        self.save_checkpoint("PRIVATE_FOREIGN_NOTE", "PRIVATE_FOREIGN_NEXT")
        original = (self.run.path / "checkpoints/000001.json").read_bytes()
        for key in ("run_id", "task_id"):
            with self.subTest(key=key):
                (self.run.path / "checkpoints/000001.json").write_bytes(original)
                self.edit_checkpoint(lambda value: value["state"].update({key: "foreign"}))
                before = self.files()
                value = handoff(self.run)
                self.assertEqual("foreign", value["checkpoint"]["status"])
                self.assertNotIn("PRIVATE_FOREIGN", render_json(value))
                self.assertEqual(before, self.files())

    def test_same_attempt_different_contract_and_candidate_are_stale(self):
        self.save_checkpoint("OLD_NOTE", "OLD_NEXT")
        original = (self.run.path / "checkpoints/000001.json").read_bytes()

        def different_contract(value):
            value["contract"]["goal"] = "Other requirements"
            value["state"]["contract_digest"] = digest(value["contract"])

        for change in (different_contract, lambda value: value["state"].update(candidate_digest="f" * 64)):
            (self.run.path / "checkpoints/000001.json").write_bytes(original)
            self.edit_checkpoint(change)
            value = handoff(self.run)
            self.assertEqual("stale", value["checkpoint"]["status"])
            self.assertNotIn("OLD_", render_json(value))

    def test_missing_corrupt_and_oversize_checkpoint_are_not_current(self):
        self.save_checkpoint()
        path = self.run.path / "checkpoints/000001.json"
        for data in (None, b"{broken", b" " * (presentation.MAX_CHECKPOINT_BYTES + 1)):
            with self.subTest(kind="missing" if data is None else "invalid"):
                path.unlink(missing_ok=True)
                if data is not None:
                    path.write_bytes(data)
                before = self.files()
                value = handoff(self.run)
                self.assertEqual("missing" if data is None else "invalid", value["checkpoint"]["status"])
                self.assertNotIn("note", value["checkpoint"])
                self.assertIsNone(value["evidence_current"])
                self.assertEqual(before, self.files())

    def test_tampered_checkpoint_hash_is_not_accepted(self):
        self.save_checkpoint()
        path = self.run.path / "checkpoints/000001.json"
        value = json.loads(path.read_text())
        value["next_safe_step"] = "UNVERIFIED_NEXT"
        path.write_text(json.dumps(value), encoding="utf-8")
        result = handoff(self.run)
        self.assertEqual("invalid", result["checkpoint"]["status"])
        self.assertNotIn("UNVERIFIED_NEXT", render_json(result))

    def test_invalid_saved_state_schema_and_boolean_counter_are_not_current(self):
        self.save_checkpoint("INVALID_NOTE", "INVALID_NEXT")
        path = self.run.path / "checkpoints/000001.json"
        original = path.read_bytes()
        for change in ({"schema_version": True}, {"checkpoints": True}):
            with self.subTest(change=change):
                path.write_bytes(original)
                self.edit_checkpoint(lambda value: value["state"].update(change))
                before = self.files()
                result = handoff(self.run)
                self.assertEqual("invalid", result["checkpoint"]["status"])
                self.assertNotIn("INVALID_", render_json(result))
                self.assertEqual(before, self.files())

    def test_explicit_verify_only_uses_readonly_status_and_preserves_truth(self):
        self.save_checkpoint()
        state = self.run.state()
        state.update(source_current=True, evidence_current=True,
                     evidence_validation={"status": "passed", "errors": []})
        before = self.files()
        with patch.object(self.run, "status", return_value=state) as observed, \
                patch.object(self.run, "resume", side_effect=AssertionError("No resume")), \
                patch("agent_harness.runner.gate", side_effect=AssertionError("No gate")):
            value = handoff(self.run, verify=True)
        observed.assert_called_once_with(verify_evidence=True)
        self.assertIs(value["evidence_current"], True)
        self.assertEqual("passed", value["evidence_validation"]["status"])
        self.assertEqual(before, self.files())

    def test_summary_counts_and_filters_history_without_changing_input(self):
        state = self.run.state()
        state["owners"] = {"old": {"status": "closed"}, "active": {"status": "running", "native_ref": "native-current"}}
        state["operations"] = {"resolved": {"status": "confirmed", "action": "OLD_ACTION"},
                               "pending": {"status": "unknown", "action": "Inspect remote result"}}
        state["processes"] = {"past": {"status": "passed", "attempt_id": "a1"},
                              "unit": {"status": "running", "attempt_id": "a1"}}
        original = deepcopy(state)
        before = self.files()
        value = status_summary(self.run, state)
        self.assertEqual({"closed": 1, "running": 1}, value["owners"]["counts"])
        self.assertEqual(1, value["owners"]["filtered"])
        self.assertEqual({"confirmed": 1, "unknown": 1}, value["operations"]["counts"])
        self.assertNotIn("OLD_ACTION", render_json(value))
        self.assertEqual("running", value["checks"]["items"][0]["recorded_status"])
        self.assertFalse(value["checks"]["items"][0]["result_available"])
        self.assertEqual(original, state)
        self.assertEqual(before, self.files())

    def test_missing_invalid_and_stale_checks_never_count_as_passed(self):
        self.assertEqual({"not_run": 1}, handoff(self.run)["checks"]["counts"])
        path = self.result(attempt_id="previous")
        self.assertEqual({"stale": 1}, handoff(self.run)["checks"]["counts"])
        path.write_bytes(b"{bad")
        self.assertEqual({"invalid": 1}, handoff(self.run)["checks"]["counts"])
        self.result()
        value = handoff(self.run)
        self.assertEqual({"passed": 1}, value["checks"]["counts"])
        self.assertIsNone(value["evidence_current"], "A recorded result is not fresh artifact verification")

    def test_compact_utf8_wire_bound_includes_escaping_newline_and_omissions(self):
        self.save_checkpoint("\x01🙂\\\"\n" * 5000, "NEXT_" * 1000)
        state = self.run.state()
        state["owners"] = {f"owner-{i}": {"status": "running", "native_ref": "🙂\x01" * 200} for i in range(35)}
        atomic_json(self.run.state_path, state)
        before = self.files()
        for limit in (2048, 4096, 8192):
            with self.subTest(limit=limit):
                value = handoff(self.run, max_bytes=limit)
                wire = render_json(value).encode("utf-8")
                self.assertLessEqual(len(wire), limit)
                self.assertTrue(wire.endswith(b"\n"))
                windows = io.BytesIO()
                stream = io.TextIOWrapper(windows, encoding="utf-8", newline="\r\n")
                print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), file=stream)
                stream.flush()
                self.assertTrue(windows.getvalue().endswith(b"\r\n"))
                self.assertLessEqual(len(windows.getvalue()), limit)
                self.assertTrue(value["output"]["truncated"])
                self.assertEqual(35, value["owners"]["total"])
                self.assertEqual(35, len(value["owners"]["items"]) + value["owners"]["omitted"])
                self.assertEqual("current", value["checkpoint"]["status"])
                note = value["checkpoint"]["note"]
                self.assertEqual(25000, len(note["text"]) + note["omitted_chars"])
        self.assertEqual(before, self.files())

    def test_input_bounds_and_invalid_budget_fail_before_unbounded_read(self):
        for limit in (True, 0, 1024, 65537):
            with patch.object(presentation, "_json", side_effect=AssertionError("No read")), self.assertRaises(HarnessError):
                handoff(self.run, max_bytes=limit)
        self.run.state_path.write_bytes(b" " * (presentation.MAX_METADATA_BYTES + 1))
        with self.assertRaisesRegex(HarnessError, "byte limit"):
            handoff(self.run)

    def test_run_attempt_change_during_handoff_never_returns_old_next(self):
        self.save_checkpoint("OLD_NOTE", "OLD_NEXT")
        original = presentation._checkpoint

        def changed(run, state):
            value = original(run, state)
            latest = run.state()
            latest["current_attempt"] = "a2"
            atomic_json(run.state_path, latest)
            return value

        with patch.object(presentation, "_checkpoint", side_effect=changed), self.assertRaisesRegex(HarnessError, "Run changed"):
            handoff(self.run)


if __name__ == "__main__":
    unittest.main()
