"""F18: applicability and local proof, independent of paid model execution."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import digest
from agent_harness.format_bench import (
    benchmark, select_format, render, FORMAT_BENCH_SCHEMA, FORMAT_SAMPLE_SCHEMA,
    EVALUATOR_VERSION, TEMPLATE_VERSION, _lookup, resolve_identity, codex_measure,
)
from agent_harness.strict_yaml import dump, load, validate


class FormatIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.rows = [{"identifier_long": i, "description_long": "value " + str(i)} for i in range(20)]
        self.identity = {"model": "fixture-model", "provider": "fixture-route",
            "reasoning_requested": "high", "reasoning_observed": None,
            "config_digest": digest("config"), "client_version": "fixture-client-v1",
            "provider_route_digest": digest("route"), "client_binary_digest": digest("client"),
            "tool_config_digest": digest("tool-config"),
            "harness_build_digest": digest("build"), "tokenizer_revision": None,
            "revision_scope": "observed_identity_only"}

    def measure(self, name, payload):
        return {"input_tokens": 2000 if name == "json" else 1200,
                "correct": True, "model": self.identity["model"],
                "source": "controlled_fixture", "execution_status": "completed"}

    def sample_receipts(self):
        """Simulate cooperating producer artifacts; this is no live qualification."""
        result = benchmark(self.rows, self.measure, model=self.identity["model"],
                           environment_fingerprint="config-A", execution_identity=self.identity)
        expected = _lookup(self.rows)[2]
        for sample in result["samples"]:
            for name, observation in sample["formats"].items():
                stem = str(sample["repeat"]) + "-" + name
                thread = "fixture-thread-" + stem
                raw = b"\n".join(json.dumps(event).encode() for event in [
                    {"type": "thread.started", "thread_id": thread},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"answer": expected})}},
                    {"type": "turn.completed", "usage": {"input_tokens": observation["input_tokens"], "output_tokens": 1}},
                ]) + b"\n"
                (self.root / (stem + ".jsonl")).write_bytes(raw)
                receipt = {"schema_version": 2, "format": name,
                    "stdout_digest": hashlib.sha256(raw).hexdigest(), "raw_path": stem + ".jsonl",
                    "thread_id": thread, "input_tokens": observation["input_tokens"], "correct": True,
                    "expected_answer": expected, "actual_answer": expected,
                    "execution_status": "completed", "error": None,
                    "execution_identity": self.identity, "data_digest": digest(self.rows),
                    "payload_digest": hashlib.sha256(render(self.rows, name).encode()).hexdigest(),
                    "template_digest": digest(TEMPLATE_VERSION), "evaluator_digest": digest(EVALUATOR_VERSION)}
                dump(self.root / (stem + ".yaml"), receipt, FORMAT_SAMPLE_SCHEMA, exclusive=True)
                observation.update(source="codex_jsonl_turn_completed", evidence=stem + ".yaml")
        result.update(measurement_kind="codex_observed", enabled=True, selected_format="toon")
        validate(result, FORMAT_BENCH_SCHEMA)
        return result

    def select(self, evaluation, **overrides):
        options = dict(evaluation=evaluation, model=self.identity["model"],
                       execution_identity=self.identity, environment_fingerprint="config-A", evidence_root=self.root)
        options.update(overrides)
        return select_format(self.rows, **options)["format"]

    def test_F18_legacy_installed_and_null_fingerprint_never_authorizes_selection(self):
        measured = benchmark(self.rows, lambda n,p: {"input_tokens": 2000 if n == "json" else 1200,
            "correct": True, "model": "installed"})
        measured.update(schema_version=1, enabled=True, selected_format="toon")
        self.assertEqual(select_format(self.rows, evaluation=measured)["format"], "json")

    def test_synthetic_relabel_without_artifacts_is_ineligible(self):
        result = benchmark(self.rows, self.measure, model=self.identity["model"],
                           environment_fingerprint="config-A", execution_identity=self.identity)
        self.assertEqual(result["measurement_kind"], "synthetic")
        self.assertFalse(result["enabled"])
        result.update(measurement_kind="codex_observed", enabled=True, selected_format="toon")
        self.assertEqual(self.select(result), "json")

    def test_resolved_identity_changes_with_route_and_client_binary(self):
        import os
        def configure(url):
            (self.root / "config.toml").write_text(
                'model = "fixture-model"\nmodel_provider = "fixture-route"\n'
                '[model_providers.fixture-route]\nbase_url = "' + url + '"\nwire_api = "responses"\n')
        settings = {"model": "fixture-model", "model_provider": "fixture-route"}
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}), \
                patch("agent_harness.codex_execution.configured_settings", return_value=settings), \
                patch("agent_harness.codex_execution.CodexAdapter") as adapter:
            capability = {"status": "observed", "version": "same-version", "binary_sha256": digest("binary-A")}
            adapter.return_value.capabilities.return_value = capability
            configure("https://provider-a.invalid")
            first = resolve_identity()
            configure("https://provider-b.invalid")
            second = resolve_identity()
            self.assertNotEqual(first, second)
            adapter.return_value.capabilities.return_value = {**capability, "binary_sha256": digest("binary-B")}
            self.assertNotEqual(second, resolve_identity())
            before_tools = resolve_identity()
            (self.root / "config.toml").write_text((self.root / "config.toml").read_text() + '\n[features]\napps = false\n')
            third = resolve_identity()
            self.assertNotEqual(before_tools, third)
            (self.root / "hooks.json").write_text('{"hooks":{}}')
            self.assertIsNone(resolve_identity())

    def test_route_drift_before_dispatch_does_not_call_adapter(self):
        with patch("agent_harness.codex_execution.CodexAdapter") as adapter, \
                patch("agent_harness.codex_execution.configured_settings", return_value={"model": "fixture-model"}), \
                patch("agent_harness.format_bench.resolve_identity", side_effect=[self.identity, {**self.identity, "provider": "changed"}]):
            measure = codex_measure(self.rows, execution_identity=self.identity)
            observed = measure("json", render(self.rows, "json"))
            self.assertEqual(observed["execution_status"], "environment_changed")
            adapter.return_value.run.assert_not_called()

    def test_route_drift_during_sample_preserves_usage_but_blocks_positive_evidence(self):
        expected = _lookup(self.rows)[2]
        def complete(*args, **kwargs):
            logs = Path(kwargs["log_dir"])
            logs.mkdir()
            raw = [
                {"type": "thread.started", "thread_id": "fixture-thread"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"answer": expected})}},
                {"type": "turn.completed", "usage": {"input_tokens": 123, "output_tokens": 1}}]
            stdout = logs / "stdout.jsonl"
            stdout.write_text("\n".join(json.dumps(event) for event in raw) + "\n")
            return {"logs": {"stdout": str(stdout)}, "exit_code": 0, "status": "completed", "error": None}
        with patch("agent_harness.codex_execution.CodexAdapter") as adapter, \
                patch("agent_harness.agent_lifecycle.GlobalSlots"), \
                patch("agent_harness.codex_execution.configured_settings", return_value={"model": "fixture-model"}), \
                patch("agent_harness.format_bench.resolve_identity", side_effect=[self.identity, self.identity, {**self.identity, "provider": "changed"}]):
            adapter.return_value.run.side_effect = complete
            measure = codex_measure(self.rows, execution_identity=self.identity)
            observed = measure("json", render(self.rows, "json"))
            self.assertEqual(observed["execution_status"], "environment_changed")
            self.assertFalse(observed["correct"])
            self.assertEqual(observed["input_tokens"], 123)

    def test_exact_identity_and_complete_evidence_positive_control(self):
        self.assertEqual(self.select(self.sample_receipts()), "toon")

    def test_duplicate_native_event_key_is_rejected_even_with_matching_digest(self):
        evidence = self.sample_receipts()
        path = self.root / "0-json.jsonl"
        raw = path.read_bytes().replace(b'{"type": "thread.started"', b'{"type":"error","type": "thread.started"', 1)
        path.write_bytes(raw)
        receipt_path = self.root / "0-json.yaml"
        receipt = load(receipt_path, FORMAT_SAMPLE_SCHEMA)
        receipt["stdout_digest"] = hashlib.sha256(raw).hexdigest()
        dump(receipt_path, receipt, FORMAT_SAMPLE_SCHEMA)
        self.assertEqual(self.select(evidence), "json")

    def test_every_identity_dimension_and_missing_environment_invalidates(self):
        evidence = self.sample_receipts()
        for key, old in self.identity.items():
            identity = {**self.identity, key: digest("different") if key.endswith("digest") else "changed"}
            with self.subTest(key=key):
                self.assertEqual(self.select(evidence, execution_identity=identity), "json")
        self.assertEqual(self.select(evidence, environment_fingerprint=None), "json")

    def test_expiry_template_evaluator_and_corrupt_raw_invalidate(self):
        evidence = self.sample_receipts()
        for field in ("template_digest", "evaluator_digest", "data_digest"):
            changed = deepcopy(evidence)
            changed[field] = digest("changed")
            with self.subTest(field=field):
                self.assertEqual(self.select(changed), "json")
        expired = deepcopy(evidence)
        expired["evaluated_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.assertEqual(self.select(expired), "json")
        (self.root / "0-json.jsonl").write_text("changed", encoding="utf-8")
        self.assertEqual(self.select(evidence), "json")

    def test_failed_and_partial_observations_invalidate_even_with_positive_label(self):
        for field, value in (("execution_status", "failed"), ("input_tokens", None), ("correct", False)):
            evidence = self.sample_receipts() if field == "execution_status" else deepcopy(evidence)
            evidence["samples"][0]["formats"]["toon"][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.select(evidence), "json")


if __name__ == "__main__":
    unittest.main()
