"""Native catalog extensions use closed variants; historical receipt bytes freeze."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, digest
from agent_harness.execution_receipts import (ExecutionReceipts, record_schema, schema_for_record,
    validate_record, verify_tool_artifacts)
from agent_harness.strict_yaml import dumps, validate
import test_tool_catalog_receipts as catalog_fixtures

ROOT = Path(__file__).resolve().parents[1]
FROZEN = {"execution-receipt.schema.json": "c12e5e00f406311dd43509ce5421e2bd41357808c9802414d60c0bd448e450c3",
          "execution-receipt.v2.schema.json": "fff078e29aa17fbf982e0ca1768f3ec39afa21b6f94a60a4843f2671fe8ddd52"}
# Produced with the pre-catalog writer at d53a34a^ and this fixed envelope.
# They compare serialized Strict YAML bytes, not only normalized dictionaries.
LEGACY_YAML = {1: '3b846e1ca2643e23ed7ca7fda513d3b3c9431d901489b00f6ed4b364cab00ea3', 2: '9c12d3641d31a41c21fa0ca6dba161f6eb2b42e11d03dee767b26a56b4c9fd64'}
STAMP = "2026-11-16T00:00:00+00:00"


def legacy_subject(version):
    envelope = {"schema_version": version, "record_type": "completion", "record_id": "placeholder",
        "project_id": "fixture-project", "run_id": "work-fixture", "task_id": "fixture", "plan_digest": None,
        "node_id": "left", "attempt_id": "left-1", "invocation_id": "fixture-invocation", "producer_id": "supervisor",
        "producer_kind": "managed_supervisor", "generation": 1, "created_at": STAMP, "harness_version": "1.6.1",
        "harness_build_digest": "a" * 64, "client_version": "fixture", "capability_receipt_ref": None,
        "execution_owner": "managed_supervisor", "input_refs": ["b" * 64], "payload": {}}
    receipts = object.__new__(ExecutionReceipts)
    receipts.supervisor = SimpleNamespace(contract={"schema_version": version})
    receipts.envelope = lambda *_: deepcopy(envelope)
    receipts.records = lambda: []
    receipts.publish = lambda value: {**value, "payload_digest": digest(value)}
    intent = {"node_id": "left", "attempt_id": "left-1", "invocation_id": "fixture-invocation",
        "record_id": "fixture-intent", "plan_digest": None, "input_refs": ["b" * 64],
        "capability_receipt_ref": None, "client_version": "fixture", "created_at": STAMP}
    result = {"status": "completed", "thread_id": "fixture-thread", "turn_started": True,
        "usage": {"input_tokens": 1, "output_tokens": 1}, "process_tree_stopped": True}
    return receipts, intent, result


class ToolingReceiptSchemaVersionTests(unittest.TestCase):
    setUp = catalog_fixtures.ToolCatalogReceiptTests.setUp
    create = catalog_fixtures.ToolCatalogReceiptTests.create
    profile = catalog_fixtures.ToolCatalogReceiptTests.profile
    subject = catalog_fixtures.ToolCatalogReceiptTests.subject

    def test_historical_v1_v2_schema_bytes_match_preextension_source(self):
        for name, expected in FROZEN.items():
            self.assertEqual(hashlib.sha256((ROOT / "schemas" / name).read_bytes()).hexdigest(), expected)

    def test_default_schema_dispatch_and_legacy_yaml_are_byte_identical(self):
        for version in (1, 2):
            with self.subTest(version=version):
                receipts, intent, result = legacy_subject(version)
                with patch("agent_harness.execution_receipts.now", return_value=STAMP):
                    record = receipts.completion(intent, result, duration_ms=12)
                old = "execution-receipt.schema.json" if version == 1 else "execution-receipt.v2.schema.json"
                self.assertEqual(record_schema(version), old)
                self.assertEqual(schema_for_record(record), old)
                self.assertEqual(validate_record(record), old)
                self.assertNotIn("tool_artifacts", record["payload"])
                self.assertNotIn("catalog_artifacts", ExecutionReceipts.result(record))
                self.assertEqual(hashlib.sha256(dumps(record, old).encode()).hexdigest(), LEGACY_YAML[version])

    def test_explicit_artifacts_choose_closed_completion_only_variant_both_versions(self):
        supervisor, receipts, intent, result, _ = self.subject()
        record = receipts.completion(intent, result)
        for version in (1, 2):
            value = dict(record, schema_version=version)
            schema = "execution-receipt.codex-tooling.v" + str(version) + ".schema.json"
            self.assertEqual(validate_record(value), schema)
            validate(value, schema)
            with self.assertRaises(ValueError):
                validate(value, record_schema(version))
            with self.assertRaises((HarnessError, ValueError)):
                validate_record(dict(value, record_type="review"))
        # Restore a missing derived projection from the same immutable journal;
        # neither the record content nor its existing payload digest is rewritten.
        projection = supervisor.store.path("receipts/" + record["record_id"] + ".yaml")
        original = projection.read_bytes()
        projection.unlink()
        self.assertEqual(receipts.records(repair=True)[-1], record)
        self.assertEqual(projection.read_bytes(), original)
        self.assertEqual(receipts.completion(intent, ExecutionReceipts.result(record)), record)

    def test_codex_extension_keeps_historical_opencode_artifacts_readable(self):
        _, receipts, intent, result, _ = self.subject()
        record = receipts.completion(intent, result)
        self.assertEqual(record["payload"]["tool_artifacts"][0]["kind"], "codex_catalog")
        legacy = deepcopy(record)
        for version in (1, 2):
            legacy["schema_version"] = version
            legacy["payload"]["tool_artifacts"][0]["kind"] = "opencode_catalog"
            self.assertEqual(validate_record(legacy), record_schema(version, tooling=True))
        wrong = deepcopy(record)
        wrong["client_version"] = "opencode:fixture"
        with self.assertRaisesRegex(HarnessError, "backend"):
            validate_record(wrong)

    def test_unbound_bad_hash_and_unpinned_artifact_variants_are_rejected(self):
        supervisor, receipts, intent, result, _ = self.subject()
        original = receipts.completion(intent, result)
        for capability in (None, "", "trusted", True):
            with self.subTest(capability=capability), self.assertRaises((HarnessError, ValueError)):
                validate_record(dict(original, capability_receipt_ref=capability))
        value = deepcopy(original)
        value["input_refs"].remove(original["capability_receipt_ref"])
        with self.assertRaises((HarnessError, ValueError)):
            validate_record(value)
        with self.assertRaises((HarnessError, ValueError)):
            verify_tool_artifacts(supervisor, value)
        value = deepcopy(original)
        value["input_refs"].remove(value["payload"]["tool_artifacts"][0]["sha256"])
        with self.assertRaises((HarnessError, ValueError)):
            validate_record(value)
        self.assertEqual(receipts.records()[-1], original)

    def test_unknown_fields_empty_extension_and_fake_versions_fail_closed(self):
        _, receipts, intent, result, _ = self.subject()
        original = receipts.completion(intent, result)
        for artifacts in (None, [], [{"kind": "opencode_catalog", "path": "x", "sha256": "a" * 64, "trust": True}]):
            value = deepcopy(original)
            value["payload"]["tool_artifacts"] = artifacts
            with self.subTest(artifacts=artifacts), self.assertRaises((HarnessError, ValueError)):
                validate_record(value)
        for version in (True, False, 0, 3, "2"):
            with self.subTest(version=version), self.assertRaises((HarnessError, ValueError)):
                validate_record(dict(original, schema_version=version))
        with self.assertRaises((HarnessError, ValueError)):
            validate_record(dict(original, tooling_schema_version=1))


if __name__ == "__main__":
    unittest.main()
