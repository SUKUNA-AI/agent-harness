"""Source-bound metric v2 receipts and conservative v1 report compatibility."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src")]
from agent_harness import benchmark_metrics as metrics, evaluation, strict_yaml
from agent_harness.core import HarnessError, digest
import test_evaluation as fixtures


class BenchmarkMetricTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EvaluationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.row = evaluation.schedule(self.fixture.manifest)[0]
        self.recorder = metrics.MetricRecorder(self.root, trial_id=self.row["trial_id"],
            manifest_digest=digest(self.fixture.manifest), producer_id="contained-test-observer")
        raw = b'{"interventions":["review"],"elapsed_seconds":0.125}\n'
        (self.root / "observation.json").write_bytes(raw)
        self.source = {"path": "observation.json", "sha256": hashlib.sha256(raw).hexdigest()}

    def observation(self, name, value, coverage="observed"):
        return self.recorder.record(name, value, coverage=coverage,
            source_refs=[self.source], observation_scope="contained_fixture")

    def trial(self, measured):
        trial = self.fixture.trial(self.row)
        for name in metrics.METRIC_TYPES:
            trial.pop(name, None)
        trial.update(measured)
        return trial

    def test_unknown_metrics_are_null_while_observed_manual_and_phase_survive_report(self):
        measured = metrics.unavailable_measurements()
        measured["metrics"]["manual_interventions"] = self.observation("manual_interventions", 1)
        measured["phase_seconds"]["environment_prepare"] = self.observation("phase_seconds.environment_prepare", .125)
        report = evaluation.summarize(self.fixture.manifest, [self.trial(measured)], self.root)
        suite = next(value for value in report["suites"] if value["suite"] == self.row["suite"])
        summary = suite["full_cohort"][self.row["arm"]]
        self.assertIsNone(summary["mandatory_nodes_skipped"])
        self.assertEqual(summary["mandatory_nodes_skipped_unknown"], 1)
        self.assertIsNone(summary["duplicate_writes"])
        self.assertEqual(summary["manual_interventions"], 1)
        self.assertEqual(summary["manual_interventions_unknown"], 0)
        self.assertEqual(summary["phase_seconds"]["environment_prepare"]["observed_seconds"], .125)
        self.assertIsNone(summary["phase_seconds"]["solve"]["observed_seconds"])
        self.assertEqual(summary["recovery_unknown"], 1)

    def test_observed_zero_has_receipt_and_boolean_is_not_a_count(self):
        measured = metrics.unavailable_measurements()
        measured["metrics"]["duplicate_writes"] = self.observation("duplicate_writes", 0)
        metrics.validate_measurements(measured, self.root, trial_id=self.row["trial_id"], manifest_digest=digest(self.fixture.manifest))
        with self.assertRaises(ValueError):
            self.observation("duplicate_writes", False)
        bad = deepcopy(measured)
        bad["metrics"]["duplicate_writes"]["evidence_refs"] = []
        with self.assertRaisesRegex(HarnessError, "REQUIRES_EVIDENCE"):
            metrics.validate_measurements(bad, self.root)
        with self.assertRaises(HarnessError):
            self.observation("manual_interventions", 0, "unavailable")

    def test_changed_source_foreign_subject_and_relabelled_metric_are_rejected(self):
        measured = metrics.unavailable_measurements()
        measured["metrics"]["manual_interventions"] = self.observation("manual_interventions", 1)
        with self.assertRaisesRegex(HarnessError, "SUBJECT_MISMATCH"):
            metrics.validate_measurements(measured, self.root, trial_id="f" * 64)
        other = deepcopy(measured)
        other["metrics"]["stale_reuse"] = deepcopy(other["metrics"]["manual_interventions"])
        with self.assertRaisesRegex(HarnessError, "SUBJECT_MISMATCH"):
            metrics.validate_measurements(other, self.root)
        (self.root / "observation.json").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "STALE_EVIDENCE"):
            evaluation.summarize(self.fixture.manifest, [self.trial(measured)], self.root)

    def test_metric_refs_cannot_escape_or_claim_unknown_as_observed(self):
        measured = metrics.unavailable_measurements()
        measured["metrics"]["stale_reuse"] = {"value": 1, "coverage": "partial",
            "evidence_refs": [{"path": "../foreign.yaml", "sha256": "f" * 64}]}
        with self.assertRaisesRegex(HarnessError, "UNSAFE_REF"):
            metrics.validate_measurements(measured, self.root)
        with self.assertRaises(HarnessError):
            self.observation("stale_reuse", None)

    def test_v1_remains_readable_and_schema_versions_are_disjoint(self):
        old = self.fixture.trial(self.row)
        evaluation.summarize(self.fixture.manifest, [old], self.root)
        strict_yaml.validate(old, evaluation.TRIAL_V1)
        with self.assertRaises(ValueError):
            strict_yaml.validate(old, evaluation.TRIAL_V2)
        current = self.trial(metrics.unavailable_measurements())
        strict_yaml.validate(current, evaluation.TRIAL_V2)
        with self.assertRaises(ValueError):
            strict_yaml.validate(current, evaluation.TRIAL_V1)
        schema_root = Path(__file__).resolve().parents[1] / "schemas"
        self.assertEqual(json.loads((schema_root / "benchmark-trial.schema.json").read_text()), evaluation.TRIAL_V1)
        self.assertEqual(json.loads((schema_root / "benchmark-trial-v2.schema.json").read_text()), evaluation.TRIAL_V2)

    def test_partial_recovery_is_not_a_fully_observed_success(self):
        measured = metrics.unavailable_measurements()
        measured["metrics"]["recovery"] = self.observation("recovery", "succeeded", "partial")
        def summary():
            report = evaluation.summarize(self.fixture.manifest, [self.trial(measured)], self.root)
            return next(item for item in report["suites"] if item["suite"] == self.row["suite"])["full_cohort"][self.row["arm"]]
        partial = summary()
        self.assertEqual(partial["recovery_succeeded"], 0)
        self.assertEqual(partial["recovery_unknown"], 1)
        self.assertEqual(partial["recovery_partial"], 1)
        measured["metrics"]["recovery"] = self.observation("recovery", "succeeded")
        observed = summary()
        self.assertEqual(observed["recovery_succeeded"], 1)
        self.assertEqual(observed["recovery_unknown"], 0)

    def test_portable_refs_reject_windows_root_drive_unc_and_separator_escapes(self):
        raw = b"local proof"
        # Isolate lexical confinement from filesystem existence on either OS.
        # No unsafe path is opened or created by this deterministic control.
        with patch.object(metrics, "safe_private", side_effect=lambda path: path), patch.object(metrics, "read_regular_file", return_value=raw):
            for name in ("\\outside\\proof.yaml", "C:proof.yaml", "C:\\outside\\proof.yaml", "\\\\server\\share\\proof.yaml", "nested\\proof.yaml", "../proof.yaml", "nested/D:proof.yaml", "nested/D:/outside/proof.yaml", "proof.yaml:stream"):
                with self.subTest(name=name), self.assertRaisesRegex(HarnessError, "UNSAFE_REF"):
                    metrics._read_ref({"path": name, "sha256": hashlib.sha256(raw).hexdigest()}, self.root)
            self.assertEqual(metrics._read_ref({"path": "nested/proof.yaml", "sha256": hashlib.sha256(raw).hexdigest()}, self.root), raw)
        with patch.object(evaluation, "safe_private", side_effect=lambda path: path), patch.object(evaluation, "read_regular_file", return_value=raw):
            for name in ("nested/D:proof.yaml", "nested/D:/outside/proof.yaml", "\\outside\\proof.yaml", "proof.yaml:stream"):
                with self.subTest(asset=name), self.assertRaisesRegex(HarnessError, "UNSAFE_ASSET"):
                    evaluation.checked_ref({"path": name, "sha256": hashlib.sha256(raw).hexdigest()}, self.root)


if __name__ == "__main__":
    unittest.main()
