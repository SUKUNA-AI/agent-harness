"""Portable regressions for bounded inventory, revoked facts and receipt coverage."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core import HarnessError, digest, now, snapshot
from agent_harness.execution_ledger import Ledger, provenance_for, repository_identity, sanitized
from agent_harness.memory import EPISODE_SCHEMA, Memory
from agent_harness.repomap_queries import _files, query_repository
from agent_harness.strict_yaml import dump


class ContextAuditRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "auth.py").write_text("def validate_token():\n    return True\n", encoding="utf-8")
        self.identity = repository_identity(self.repo)
        self.current = provenance_for(self.repo, snapshot({"cwd": str(self.repo)}))

    def test_fallback_cutoff_counts_non_source_files_as_incomplete(self):
        root = self.base / "inventory"
        root.mkdir()
        for index in range(4097):
            (root / (str(index) + ".txt")).touch()
        tests = root / "tests"
        tests.mkdir()
        (tests / "test_auth.py").write_text("from auth import validate_token\n", encoding="utf-8")
        with patch("agent_harness.repomap_queries.shutil.which", return_value=None), \
                patch("agent_harness.native.backend_info", return_value={"available": False}):
            names, partial, method = _files(root)
            result = query_repository(root, "related-tests", target="auth.py")
        self.assertEqual(names, [])
        self.assertTrue(partial)
        self.assertEqual(method, "bounded_file_lookup_rg_unavailable")
        self.assertFalse(result["complete_inventory"])
        self.assertEqual(result["results"], [])

    def test_small_complete_fallback_inventory_remains_complete(self):
        (self.repo / "test_auth.py").write_text("from auth import validate_token\n", encoding="utf-8")
        with patch("agent_harness.repomap_queries.shutil.which", return_value=None), \
                patch("agent_harness.native.backend_info", return_value={"available": False}):
            result = query_repository(self.repo, "related-tests", target="auth.py")
        self.assertTrue(result["complete_inventory"])
        self.assertIn("test_auth.py", {row["file"] for row in result["results"]})

    def command_fixture(self):
        ledger = Ledger(self.base / "run", self.identity)
        operation = {"argv": ["rg", "--no-config", "--no-ignore-parent", "--no-ignore-global",
                              "--no-ignore-vcs", "absent", "."],
            "cwd": str(self.repo), "effect": "read_only", "purpose": "find absent symbol",
            "input_closure_complete": True, "toolchain_digest": digest("tool"),
            "env_digest": digest({}), "config_digest": digest({})}
        # Exercise governor validity independently of an optional installed rg.
        # Existing knowledge scenarios also cover the real executable/closure path.
        context = {"input_closure": {"digest": digest("physical fixture")},
                   "binary_digest": digest("binary"), "environment_digest": digest("environment")}
        proof = patch("agent_harness.execution_ledger._rg_reuse_context", return_value=context)
        proof.start()
        self.addCleanup(proof.stop)
        first = ledger.govern(operation, self.current)
        evidence = self.base / "command-result.txt"
        evidence.write_text("", encoding="utf-8")
        event = ledger.append("command", {"argv": operation["argv"], "cwd": str(self.repo),
            "effect": "read_only", "status": "completed", "exit_code": 1,
            "operation_fingerprint": first["operation_fingerprint"], "evidence_path": str(evidence),
            "evidence_digest": hashlib.sha256(b"").hexdigest()}, self.current)
        return ledger, operation, event, evidence

    def test_superseded_command_is_neither_informed_nor_reused(self):
        ledger, operation, event, _ = self.command_fixture()
        self.assertEqual(ledger.govern(operation, self.current)["reused_event_id"], event["event_id"])
        ledger.append("decision", {"reason": "withdraw observed result", "supersedes": event["event_id"]}, self.current)
        informed = ledger.inform(self.current)
        self.assertIn({"event_id": event["event_id"], "status": "superseded"}, informed["required"]["stale_items"])
        decision = ledger.govern(operation, self.current)
        self.assertEqual(decision["decision"], "allow")
        self.assertIsNone(decision["reused_event_id"])

    def test_nonvalid_record_status_is_never_reused(self):
        ledger, operation, event, _ = self.command_fixture()
        for status in ("needs_revalidation", "superseded", "rejected", "expired"):
            with self.subTest(status=status):
                row = {**event, "validity_status": status}
                with patch.object(ledger, "_records", return_value=[row]):
                    informed = ledger.inform(self.current)
                    decision = ledger.govern(operation, self.current)
                self.assertEqual(informed["required"]["stale_items"][0]["status"], status)
                self.assertIsNone(decision["reused_event_id"])

    def test_current_command_still_reuses_until_evidence_changes(self):
        ledger, operation, event, evidence = self.command_fixture()
        self.assertEqual(ledger.govern(operation, self.current)["reused_event_id"], event["event_id"])
        self.assertEqual(ledger.govern(operation, self.current, force=True)["decision"], "allow")
        evidence.write_text("changed result", encoding="utf-8")
        self.assertIsNone(ledger.govern(operation, self.current)["reused_event_id"])

    def episode_fixture(self):
        refs = []
        for name in ("first-check", "second-check"):
            path = self.base / (name + ".json")
            path.write_text('{"status":"passed"}', encoding="utf-8")
            refs.append({"id": name, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        episode = {"schema_version": 1, "id": "episode-two-checks", "repository_identity": self.identity,
            "problem_signature": "two-checks", "category": "accepted_task", "context": "two independent checks",
            "hypothesis": "bounded fix", "attempted_fix": "fixture", "root_cause": "not inferred",
            "outcome": "verified", "evidence_ids": [ref["id"] for ref in refs], "evidence_refs": refs,
            "affected_symbols": [], "tool_version": "fixture", "trust_level": "verified", "tags": [],
            "notes": "local regression", "actor": "fixture evaluator", "created_at": now(),
            "expires_at": None, "validity_status": "valid", "supersedes": None, "provenance": self.current}
        return Memory(self.base / "memory", self.identity, enabled=True), episode

    def invalid_coverage(self, episode):
        missing = deepcopy(episode)
        missing["evidence_refs"].pop()
        extra = deepcopy(episode)
        extra["evidence_refs"][1]["id"] = "unclaimed-check"
        duplicate_ids = deepcopy(episode)
        duplicate_ids["evidence_ids"].append(duplicate_ids["evidence_ids"][0])
        duplicate_refs = deepcopy(episode)
        duplicate_refs["evidence_refs"].append(deepcopy(duplicate_refs["evidence_refs"][0]))
        duplicate_both = deepcopy(episode)
        duplicate_both["evidence_ids"][1] = duplicate_both["evidence_ids"][0]
        duplicate_both["evidence_refs"][1] = deepcopy(duplicate_both["evidence_refs"][0])
        return {"missing": missing, "extra": extra, "duplicate_ids": duplicate_ids,
                "duplicate_refs": duplicate_refs, "duplicate_both": duplicate_both}

    def test_verified_episode_requires_unique_exact_receipt_coverage(self):
        memory, episode = self.episode_fixture()
        for name, record in self.invalid_coverage(episode).items():
            record["id"] = "invalid-" + name
            with self.subTest(coverage=name), self.assertRaises(HarnessError):
                memory.record_episode(record)
        self.assertEqual(list((memory.root / "episodes").glob("*.yaml")), [])

    def test_legacy_incomplete_or_duplicate_receipts_are_stale_on_recall(self):
        memory, episode = self.episode_fixture()
        target = memory.root / "episodes"
        target.mkdir()
        invalid = self.invalid_coverage(episode)
        for name, record in invalid.items():
            record["id"] = "legacy-" + name
            # Old records remain readable; the current semantic check must not
            # promote schema-valid but incomplete evidence back to verified facts.
            dump(target / (record["id"] + ".yaml"), record, EPISODE_SCHEMA, exclusive=True)
        recalled = memory.recall("two-checks", self.current)
        self.assertEqual(recalled["verified_facts"], [])
        self.assertEqual(recalled["stale_memory_prevented"], len(invalid))
        self.assertTrue(all(row["status"] == "needs_revalidation" for row in recalled["stale_items"]))

    def test_complete_receipts_accept_any_order_and_check_every_file(self):
        memory, episode = self.episode_fixture()
        episode["evidence_refs"].reverse()
        memory.record_episode(episode)
        self.assertEqual(len(memory.recall("two-checks", self.current)["verified_facts"]), 1)
        Path(episode["evidence_refs"][1]["path"]).write_text('{"status":"failed"}', encoding="utf-8")
        recalled = memory.recall("two-checks", self.current)
        self.assertEqual(recalled["verified_facts"], [])
        self.assertEqual(recalled["stale_memory_prevented"], 1)

    def test_malformed_receipt_coverage_returns_false(self):
        _, episode = self.episode_fixture()
        for key, value in (("evidence_ids", "first-check"), ("evidence_ids", [None]),
                           ("evidence_refs", [None]), ("evidence_refs", {"id": "first-check"})):
            with self.subTest(key=key, value=value):
                self.assertFalse(Memory._evidence_current({**episode, key: value}))


class PathSanitizationRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="AuditHarnessSupervisorLongPath123-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.parent = self.base / "regression-fixtures" / "independent-execution-checks" / "ordinary-local-workspace"
        self.parent.mkdir(parents=True)
        # Synthetic high-entropy data, not a real provider token or credential.
        self.opaque = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789aB"

    def test_existing_structured_path_does_not_become_one_entropy_token(self):
        report = self.parent / "report.json"
        report.write_text("{}", encoding="utf-8")
        value = {"cwd": self.parent.as_posix(), "evidence_path": report.as_posix(), "argv": [report.as_posix()]}
        self.assertIs(sanitized(value), value)

    def test_default_ledger_supervisor_completes_under_long_temporary_parent(self):
        from test_work_supervisor import WorkSupervisorTests
        with patch("tempfile.tempdir", str(self.parent)):
            case = WorkSupervisorTests("test_edit_precedes_freeze_and_independent_acceptance")
            try:
                case.setUp()
                supervisor = case.create()
                self.assertEqual(supervisor.contract["ledger"], "inform")
                result = supervisor.start()
                self.assertEqual(result["status"], "ACCEPTED", result)
                self.assertTrue(result["evidence_current"])
                self.assertTrue((case.run / "reports" / "report.yaml").is_file())
                self.assertEqual(case.calls(), ["exec"])
            finally:
                case.tearDown()
                case.doCleanups()

    def test_path_field_name_cannot_bypass_opaque_token_detection(self):
        for name in ("cwd", "path", "evidence_path", "result_pointer", "fact"):
            with self.subTest(field=name), self.assertRaisesRegex(HarnessError, "High-entropy"):
                sanitized({name: self.opaque})

    def test_unknown_path_like_opaque_token_is_not_exempt(self):
        value = (self.parent / self.opaque[:32] / self.opaque[32:]).as_posix()
        with self.assertRaisesRegex(HarnessError, "High-entropy"):
            sanitized({"path": value})

    def test_existing_path_still_checks_opaque_components(self):
        path = self.base / self.opaque
        path.touch()
        with self.assertRaisesRegex(HarnessError, "High-entropy"):
            sanitized({"evidence_path": str(path)})

    def test_existing_path_still_checks_known_credentials(self):
        for name in ("ghp_" + "a" * 32, "password=fixture-only-value"):
            path = self.parent / name
            path.touch()
            with self.subTest(name=name), self.assertRaises(HarnessError):
                sanitized({"path": str(path)})

    def test_existing_path_still_checks_environment_credential_values(self):
        synthetic = "fixture-only-private-value"
        path = self.parent / synthetic
        path.touch()
        with patch.dict(os.environ, {"HARNESS_REGRESSION_API_KEY": synthetic}):
            with self.assertRaisesRegex(HarnessError, "Environment credential"):
                sanitized({"path": str(path)})

    def test_json_escaping_does_not_hide_environment_credentials(self):
        synthetic = 'fixture-only-"quoted"-value'
        with patch.dict(os.environ, {"HARNESS_REGRESSION_API_KEY": synthetic}):
            with self.assertRaisesRegex(HarnessError, "Environment credential"):
                sanitized({"path": synthetic})

    def test_link_path_does_not_gain_an_entropy_exemption(self):
        link = self.parent / "linked-output"
        try:
            link.symlink_to(self.parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaisesRegex(HarnessError, "High-entropy"):
            sanitized({"path": link.as_posix()})

    def test_raw_text_tokens_and_credential_shapes_remain_rejected(self):
        values = [self.opaque, "observation " + self.opaque, "password=fixture-only-value",
                  "https://" + "fixture-user:fixture-password" + "@example.invalid/resource"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(HarnessError):
                sanitized(value)


if __name__ == "__main__":
    unittest.main()
