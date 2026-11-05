"""Deterministic benchmark readiness/accounting; no external assets or model calls."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, digest
from agent_harness.evaluation import SUITES, validate_manifest, schedule, summarize
from agent_harness.strict_yaml import dumps


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        ref = self.ref("asset.yaml", "synthetic fixture, no official cohort qualification")
        self.manifest = {"schema_version": 1, "record_type": "benchmark_manifest", "comparison": "package",
            "seed": 23, "repeats": 2, "bootstrap_resamples": 100, "confidence": .95,
            "minimum_token_gain": .05, "minimum_quality_delta": -.02,
            "model": "fixture-model", "provider": "fixture-route", "client_version": "fixture-client",
            "root_policy": ref, "child_policy": ref, "isolation": ref, "approved_budget": ref,
            "suites": [{"id": name, "revision": "fixture-v1", "assets": ref, "evaluator": ref,
                        "adapter": ref, "toolchain": ref,
                        "tasks": [{"id": task, "source": ref} for task in ("first", "second")]} for name in SUITES]}

    def ref(self, name, text):
        raw = text.encode()
        (self.root / name).write_bytes(raw)
        return {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}

    def trial(self, row, tokens=100, outcome="accepted", coverage="complete"):
        fingerprint = digest(self.manifest)
        grade = {"schema_version": 1, "record_type": "independent_grade", "manifest_digest": fingerprint,
                 "trial_id": row["trial_id"], "evaluator_digest": self.manifest["suites"][0]["evaluator"]["sha256"],
                 "outcome": outcome}
        ref = self.ref(row["trial_id"] + ".yaml", dumps(grade))
        return {"schema_version": 1, "manifest_digest": fingerprint, "trial_id": row["trial_id"],
                "outcome": outcome, "measurement_kind": "synthetic", "elapsed_seconds": 3., "usage_coverage": coverage,
                "claimed_accepted": outcome == "accepted", "mandatory_nodes_skipped": 0, "duplicate_writes": 0,
                "recovery": "not_applicable", "time_to_accepted_seconds": 3. if outcome == "accepted" else None,
                "useful_retrieval": None, "stale_reuse": 0, "hook_overhead_ms": None,
                "usage": [{"invocation_id": "call-" + row["trial_id"], "scope": "individual_attempt", "phase": "initial",
                           "status": "complete", "input_tokens": tokens, "output_tokens": 0}],
                "evaluator_receipt": ref}

    def test_readiness_is_local_asset_verification_and_never_live_pass(self):
        value = validate_manifest(self.manifest, self.root)
        self.assertEqual(value["model_runs"], "not_run")
        self.assertEqual(value["planned_trials"], 40)
        self.assertEqual(value["qualification"], "local_bytes_only")

    def test_changed_asset_unknown_identity_and_missing_portfolio_fail_closed(self):
        for transform in (lambda m: m.update(model="installed"),
                          lambda m: m["suites"][0].update(revision="main"),
                          lambda m: m["suites"].__setitem__(0, m["suites"][1])):
            manifest = deepcopy(self.manifest)
            transform(manifest)
            with self.assertRaises((HarnessError, ValueError)):
                validate_manifest(manifest, self.root)
        (self.root / "asset.yaml").write_text("changed")
        with self.assertRaises(HarnessError):
            validate_manifest(self.manifest, self.root)

    def test_schedule_stable_and_comparison_arms_distinct(self):
        self.assertEqual(schedule(self.manifest), schedule(self.manifest))
        self.assertEqual(len({row["trial_id"] for row in schedule(self.manifest)}), 40)
        matched = {**self.manifest, "comparison": "matched"}
        self.assertEqual({row["arm"] for row in schedule(matched)}, {"plain_matched", "harness_matched"})

    def test_missing_timeouts_and_partial_usage_remain_in_denominator(self):
        planned = schedule(self.manifest)
        first = planned[0]
        record = self.trial(first, tokens=0, outcome="timeout", coverage="partial")
        report = summarize(self.manifest, [record], self.root)
        suite = next(row for row in report["suites"] if row["suite"] == first["suite"])
        arm = suite["full_cohort"][first["arm"]]
        self.assertEqual(arm["planned"], 4)
        self.assertEqual(arm["not_run"], 3)
        self.assertEqual(arm["failed_or_cancelled"], 1)
        self.assertEqual(arm["usage_coverage"], "partial")
        self.assertIsNone(suite["accepted_pairs"]["median_relative_token_saving"])

    def test_paired_costs_and_task_cluster_uncertainty(self):
        trials = [self.trial(row, 100 if row["arm"] == "plain_stock" else 70) for row in schedule(self.manifest)]
        report = summarize(self.manifest, trials, self.root)
        self.assertEqual(len(report["suites"]), 5)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["measurement_kind"], "synthetic")
        for suite in report["suites"]:
            self.assertAlmostEqual(suite["accepted_pairs"]["median_relative_token_saving"], .3)
            self.assertEqual(suite["accepted_pairs"]["task_clusters"], 2)
            self.assertEqual(suite["accepted_pairs"]["uncertainty"]["method"], "task_cluster_bootstrap_mean")
            self.assertEqual(suite["policy_result"], "measured_positive")
            self.assertEqual(suite["claim_scope"], "synthetic_accounting_only")

    def test_duplicate_delivery_idempotent_but_conflicting_or_overlap_rejected(self):
        planned = schedule(self.manifest)
        first, second = [self.trial(row) for row in planned[:2]]
        self.assertEqual(summarize(self.manifest, [first, first], self.root), summarize(self.manifest, [first], self.root))
        changed = {**first, "elapsed_seconds": 4.}
        with self.assertRaises(HarnessError):
            summarize(self.manifest, [first, changed], self.root)
        second["usage"][0]["invocation_id"] = first["usage"][0]["invocation_id"]
        with self.assertRaises(HarnessError):
            summarize(self.manifest, [first, second], self.root)


if __name__ == "__main__":
    unittest.main()
