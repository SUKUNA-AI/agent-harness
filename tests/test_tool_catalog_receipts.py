"""Actual-run catalog evidence is immutable and closed by completion receipts."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError
from agent_harness.execution import prepare
from agent_harness.execution_receipts import ExecutionReceipts, validate_record, verify_tool_artifacts
import test_tooling_acceptance as fixtures


class ToolCatalogReceiptTests(unittest.TestCase):
    setUp = fixtures.ToolingAcceptanceTests.setUp
    create = fixtures.ToolingAcceptanceTests.create
    profile = fixtures.ToolingAcceptanceTests.profile

    def subject(self, *, bound=True):
        supervisor = self.create(profile=False)
        ref = None
        if bound:
            _, ref = prepare(supervisor, {"backend": "codex"}, root=self.source,
                role="owner", node_id="left", attempt_id="left-1")
        receipts = supervisor.receipts()
        intent = receipts.intent("left", "left-1", checkout=str(self.source),
            owner="catalog-fixture-lease", tool_provenance=ref)
        artifact = supervisor.path / "native-catalog.json"
        artifact.write_text(json.dumps({"session_id": "ses_catalog", "tools": ["read"]}))
        result = {"status": "completed", "thread_id": "ses_catalog", "turn_started": True,
            "usage": {"input_tokens": 1, "output_tokens": 1}, "process_tree_stopped": True,
            "catalog_artifacts": [{"path": str(artifact),
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
        return supervisor, receipts, intent, result, artifact

    def test_catalog_hash_is_pinned_and_survives_journal_replay(self):
        supervisor, receipts, intent, result, artifact = self.subject()
        record = receipts.completion(intent, result)
        self.assertIn(result["catalog_artifacts"][0]["sha256"], record["input_refs"])
        self.assertEqual(record["payload"]["tool_artifacts"][0]["path"], "native-catalog.json")
        self.assertEqual(verify_tool_artifacts(supervisor, record), result["catalog_artifacts"])
        self.assertEqual(receipts.records()[-1], record)
        replay = ExecutionReceipts.result(record)
        self.assertEqual(receipts.completion(intent, replay), record)

    def test_changed_or_deleted_catalog_cannot_revalidate(self):
        supervisor, receipts, intent, result, artifact = self.subject()
        record = receipts.completion(intent, result)
        artifact.write_text(json.dumps({"session_id": "ses_catalog", "tools": ["write"]}))
        with self.assertRaisesRegex(HarnessError, "digest mismatch"):
            verify_tool_artifacts(supervisor, record)
        artifact.unlink()
        with self.assertRaises((HarnessError, ValueError, OSError)):
            verify_tool_artifacts(supervisor, record)

    def test_unbound_catalog_and_unsafe_artifact_paths_are_rejected(self):
        supervisor, receipts, intent, result, artifact = self.subject(bound=False)
        with self.assertRaisesRegex(HarnessError, "Unbound"):
            receipts.completion(intent, result)
        # A fresh ordinary legacy result retains its exact old field set.
        del result["catalog_artifacts"]
        legacy = receipts.completion(intent, result)
        self.assertNotIn("tool_artifacts", legacy["payload"])
        self.assertNotIn("catalog_artifacts", ExecutionReceipts.result(legacy))
        self.assertEqual(verify_tool_artifacts(supervisor, legacy), [])

    def test_parent_escape_duplicate_and_symlink_fail_before_publication(self):
        supervisor, receipts, intent, result, artifact = self.subject()
        valid = dict(result["catalog_artifacts"][0])
        outside = self.root / "outside.json"
        outside.write_bytes(artifact.read_bytes())
        for values in ([dict(valid, path=str(outside))], [dict(valid, path="../outside.json")],
                       [valid, valid], [dict(valid, sha256=True)]):
            with self.subTest(artifacts=values), self.assertRaises((HarnessError, ValueError, OSError)):
                receipts.completion(intent, dict(result, catalog_artifacts=values))
        if os.name != "nt":
            link = supervisor.path / "linked.json"
            link.symlink_to(artifact)
            with self.assertRaises((HarnessError, ValueError, OSError)):
                receipts.completion(intent, dict(result, catalog_artifacts=[dict(valid, path=str(link))]))
        self.assertFalse(any(r["record_type"] == "completion" for r in receipts.records()))


if __name__ == "__main__":
    unittest.main()
