"""Offline benchmark manifest validation and paired cohort accounting.

No model calls, downloads, or evaluator commands are executed by this module.
Assets and independent grading are supplied through explicit local receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics

from .core import HarnessError, digest
from . import benchmark_metrics
from .execution_ledger import HASH, SHORT, object_schema, safe_private
from .file_io import read_regular_file
from .strict_yaml import load, loads, dump, validate

SUITES = ("swe-bench-verified", "terminal-bench-2.1", "aider-polyglot",
          "harness-recovery", "harness-retrieval-long-task")
ARMS = {"package": ("plain_stock", "harness_full"),
        "matched": ("plain_matched", "harness_matched")}
FILE_REF = object_schema({"path": SHORT, "sha256": HASH}, ("path", "sha256"))
TASK = object_schema({"id": SHORT, "source": FILE_REF}, ("id", "source"))
SUITE = object_schema({"id": {"enum": list(SUITES)}, "revision": SHORT,
    "assets": FILE_REF, "evaluator": FILE_REF, "adapter": FILE_REF,
    "toolchain": FILE_REF, "tasks": {"type": "array", "minItems": 1, "maxItems": 10000, "items": TASK}},
    ("id", "revision", "assets", "evaluator", "adapter", "toolchain", "tasks"))
MANIFEST_SCHEMA = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_manifest"},
    "comparison": {"enum": list(ARMS)}, "seed": {"type": "integer", "minimum": 0},
    "repeats": {"type": "integer", "minimum": 1, "maximum": 20},
    "bootstrap_resamples": {"type": "integer", "minimum": 100, "maximum": 10000},
    "confidence": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
    "minimum_token_gain": {"type": "number", "minimum": 0, "maximum": 1},
    "minimum_quality_delta": {"type": "number", "minimum": -1, "maximum": 1},
    "model": SHORT, "provider": SHORT, "client_version": SHORT,
    "root_policy": FILE_REF, "child_policy": FILE_REF,
    "isolation": FILE_REF, "approved_budget": FILE_REF,
    "suites": {"type": "array", "minItems": 5, "maxItems": 5, "items": SUITE}},
    ("schema_version", "record_type", "comparison", "seed", "repeats", "bootstrap_resamples", "confidence",
     "minimum_token_gain", "minimum_quality_delta",
     "model", "provider", "client_version", "root_policy", "child_policy", "isolation", "approved_budget", "suites"))
USAGE = object_schema({"invocation_id": SHORT,
    "scope": {"const": "individual_attempt"},
    "phase": {"enum": ["initial", "planner", "worker", "integration", "reviewer", "repair", "retry"]},
    "status": {"enum": ["complete", "partial", "unavailable", "conflicting"]},
    "input_tokens": {"type": ["integer", "null"], "minimum": 0},
    "output_tokens": {"type": ["integer", "null"], "minimum": 0}},
    ("invocation_id", "scope", "phase", "status", "input_tokens", "output_tokens"))
TRIAL = object_schema({"schema_version": {"const": 1}, "manifest_digest": HASH,
    "measurement_kind": {"enum": ["synthetic", "observed"]},
    "trial_id": HASH, "outcome": {"enum": ["accepted", "failed", "cancelled", "timeout", "environment_error"]},
    "elapsed_seconds": {"type": "number", "minimum": 0},
    "usage_coverage": {"enum": ["complete", "partial", "unknown"]},
    "usage": {"type": "array", "maxItems": 10000, "items": USAGE},
    "claimed_accepted": {"type": "boolean"},
    "mandatory_nodes_skipped": {"type": "integer", "minimum": 0},
    "duplicate_writes": {"type": "integer", "minimum": 0},
    "recovery": {"enum": ["not_applicable", "succeeded", "failed", "unknown"]},
    "time_to_accepted_seconds": {"type": ["number", "null"], "minimum": 0},
    "useful_retrieval": {"type": ["boolean", "null"]},
    "stale_reuse": {"type": "integer", "minimum": 0},
    "hook_overhead_ms": {"type": ["number", "null"], "minimum": 0},
    "evaluator_receipt": FILE_REF},
    ("schema_version", "manifest_digest", "measurement_kind", "trial_id", "outcome", "elapsed_seconds", "usage_coverage", "usage",
     "claimed_accepted", "mandatory_nodes_skipped", "duplicate_writes", "recovery", "time_to_accepted_seconds",
     "useful_retrieval", "stale_reuse", "hook_overhead_ms", "evaluator_receipt"))
TRIAL_V1 = TRIAL
TRIAL_V2 = benchmark_metrics.trial_schema(TRIAL_V1)
TRIAL = {"oneOf": [TRIAL_V1, TRIAL_V2]}
GRADE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "independent_grade"},
    "manifest_digest": HASH, "trial_id": HASH, "evaluator_digest": HASH,
    "outcome": {"enum": ["accepted", "failed", "cancelled", "timeout", "environment_error"]}},
    ("schema_version", "record_type", "manifest_digest", "trial_id", "evaluator_digest", "outcome"))


def checked_ref(ref, root):
    """Resolve only ordinary local assets inside the approved bundle root."""
    try:
        path = benchmark_metrics.portable_relative_path(ref["path"])
    except ValueError as error:
        raise HarnessError("BENCH_UNSAFE_ASSET: portable relative asset refs required") from error
    target = safe_private(Path(root).absolute() / path)
    raw = read_regular_file(target, max_bytes=2 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise HarnessError("BENCH_STALE_ASSET: asset digest differs")
    return raw


def validate_manifest(manifest, root):
    validate(manifest, MANIFEST_SCHEMA)
    if {suite["id"] for suite in manifest["suites"]} != set(SUITES):
        raise HarnessError("BENCH_INCOMPLETE_PORTFOLIO: exactly five distinct suites required")
    for field in ("model", "provider", "client_version"):
        if manifest[field].casefold() in {"installed", "unknown", "unavailable", "tbd", ""}:
            raise HarnessError("BENCH_UNKNOWN_IDENTITY: " + field)
    for field in ("root_policy", "child_policy", "isolation", "approved_budget"):
        checked_ref(manifest[field], root)
    for suite in manifest["suites"]:
        if suite["revision"].casefold() in {"latest", "main", "head", "tbd", "unknown", ""}:
            raise HarnessError("BENCH_UNPINNED_REVISION: immutable revision required")
        for field in ("assets", "evaluator", "adapter", "toolchain"):
            checked_ref(suite[field], root)
        identifiers = [task["id"] for task in suite["tasks"]]
        if len(set(identifiers)) != len(identifiers):
            raise HarnessError("BENCH_DUPLICATE_TASK")
        for task in suite["tasks"]:
            checked_ref(task["source"], root)
    return {"schema_version": 1, "status": "assets_verified", "manifest_digest": digest(manifest),
            "planned_trials": sum(len(suite["tasks"]) for suite in manifest["suites"]) * manifest["repeats"] * 2,
            "model_runs": "not_run", "qualification": "local_bytes_only",
            "limitations": ["asset hashes do not prove full official cohort membership",
                            "isolation and budget receipt content require separate operational qualification"]}


def schedule(manifest):
    """Counterbalance each paired task; stable IDs retain failures in denominator."""
    validate(manifest, MANIFEST_SCHEMA)
    seed = random.Random(manifest["seed"])
    fingerprint = digest(manifest)
    pairs = [(suite["id"], task["id"], repeat)
             for suite in manifest["suites"] for task in suite["tasks"]
             for repeat in range(manifest["repeats"])]
    seed.shuffle(pairs)
    trials = []
    for suite, task, repeat in pairs:
        arms = list(ARMS[manifest["comparison"]])
        if seed.getrandbits(1):
            arms.reverse()
        for arm in arms:
            row = {"suite": suite, "task_id": task, "repeat": repeat, "arm": arm}
            trials.append({**row, "trial_id": digest({"manifest_digest": fingerprint, **row})})
    return trials


def summarize(manifest, trials, root):
    """Report every planned trial, including missing/cancelled/failed observations."""
    planned = schedule(manifest)
    by_id = {row["trial_id"]: row for row in planned}
    records, invocation_ids = {}, set()
    for trial in trials:
        validate(trial, TRIAL)
        if trial["schema_version"] == 2:
            benchmark_metrics.validate_measurements({name: trial[name] for name in ("schema_version", "metrics", "phase_seconds")}, root, trial_id=trial["trial_id"], manifest_digest=trial["manifest_digest"])
        key = trial["trial_id"]
        if key not in by_id or trial["manifest_digest"] != digest(manifest):
            raise HarnessError("BENCH_TRIAL_BINDING_MISMATCH")
        if key in records:
            if records[key] != trial:
                raise HarnessError("BENCH_CONFLICTING_TRIAL")
            continue
        if (trial["outcome"] != "accepted" and trial["time_to_accepted_seconds"] is not None
                or trial["time_to_accepted_seconds"] is not None
                and trial["time_to_accepted_seconds"] > trial["elapsed_seconds"]):
            raise HarnessError("BENCH_INVALID_TIME_TO_ACCEPTANCE")
        grade = loads(checked_ref(trial["evaluator_receipt"], root), GRADE)
        suite = next(suite for suite in manifest["suites"] if suite["id"] == by_id[key]["suite"])
        if (grade["manifest_digest"] != trial["manifest_digest"] or grade["trial_id"] != key
                or grade["evaluator_digest"] != suite["evaluator"]["sha256"] or grade["outcome"] != trial["outcome"]):
            raise HarnessError("BENCH_GRADE_BINDING_MISMATCH")
        for usage in trial["usage"]:
            if usage["invocation_id"] in invocation_ids:
                raise HarnessError("BENCH_DUPLICATE_INVOCATION: overlapping coverage")
            invocation_ids.add(usage["invocation_id"])
        records[key] = trial
    arms = ARMS[manifest["comparison"]]
    output = {"schema_version": 1, "manifest_digest": digest(manifest),
              "status": "not_run" if not records else "complete" if len(records) == len(planned) else "insufficient_data",
              "measurement_kind": "synthetic" if any(row["measurement_kind"] == "synthetic" for row in records.values()) else "observed" if records else "unavailable",
              "comparison": manifest["comparison"], "suites": []}
    for suite in manifest["suites"]:
        suite_plan = [row for row in planned if row["suite"] == suite["id"]]
        summaries = {}
        for arm in arms:
            selected = [records[row["trial_id"]] for row in suite_plan
                        if row["arm"] == arm and row["trial_id"] in records]
            expected = sum(row["arm"] == arm for row in suite_plan)
            complete = len(selected) == expected and all(_complete(row) for row in selected)
            summaries[arm] = {"planned": expected, "observed": len(selected), "not_run": expected - len(selected),
                "accepted": sum(row["outcome"] == "accepted" for row in selected),
                "failed_or_cancelled": sum(row["outcome"] != "accepted" for row in selected),
                "false_acceptance": sum(row["claimed_accepted"] and row["outcome"] != "accepted" for row in selected),
                "mandatory_nodes_skipped": _metric_sum(selected, "mandatory_nodes_skipped"),
                "duplicate_writes": _metric_sum(selected, "duplicate_writes"),
                "recovery_succeeded": sum(benchmark_metrics.metric_value(row, "recovery") == "succeeded" and not benchmark_metrics.metric_unknown(row, "recovery") for row in selected),
                "recovery_failed": sum(benchmark_metrics.metric_value(row, "recovery") == "failed" and not benchmark_metrics.metric_unknown(row, "recovery") for row in selected),
                "recovery_unknown": sum(benchmark_metrics.metric_unknown(row, "recovery") or benchmark_metrics.metric_value(row, "recovery") == "unknown" for row in selected),
                "recovery_partial": sum(row["schema_version"] == 2 and row["metrics"]["recovery"]["coverage"] == "partial" for row in selected),
                "observed_model_calls": sum(len(row["usage"]) for row in selected),
                "observed_retries_repairs": sum(usage["phase"] in {"repair", "retry"} for row in selected for usage in row["usage"]),
                "useful_retrieval_true": sum(benchmark_metrics.metric_value(row, "useful_retrieval") is True for row in selected),
                "useful_retrieval_unknown": sum(benchmark_metrics.metric_unknown(row, "useful_retrieval") for row in selected),
                "stale_reuse": _metric_sum(selected, "stale_reuse"),
                "hook_overhead_ms": _metric_sum(selected, "hook_overhead_ms"),
                "hook_overhead_unknown": sum(benchmark_metrics.metric_unknown(row, "hook_overhead_ms") for row in selected),
                "time_to_accepted_seconds": [row["time_to_accepted_seconds"] for row in selected if row["outcome"] == "accepted"],
                "censored_or_unaccepted": sum(row["outcome"] != "accepted" for row in selected),
                "elapsed_seconds": sum(row["elapsed_seconds"] for row in selected),
                "usage_coverage": "complete" if complete else "partial",
                "observed_tokens": sum(value for row in selected for usage in row["usage"]
                                       for value in (usage["input_tokens"], usage["output_tokens"]) if value is not None)}
            summaries[arm].update({name + "_unknown": sum(benchmark_metrics.metric_unknown(row, name) for row in selected)
                                   for name in ("mandatory_nodes_skipped", "duplicate_writes", "stale_reuse", "manual_interventions")})
            summaries[arm]["manual_interventions"] = _metric_sum(selected, "manual_interventions")
            summaries[arm]["phase_seconds"] = {name: _phase_summary(selected, name) for name in benchmark_metrics.PHASES}
        paired, paired_report = _paired_cost_report(suite, suite_plan, records, arms, manifest["repeats"])
        interval = _interval(paired, manifest) if len(paired) >= 2 else None
        quality_delta = ((summaries[arms[1]]["accepted"] - summaries[arms[0]]["accepted"])
                         / summaries[arms[0]]["planned"])
        complete = all(row["usage_coverage"] == "complete" for row in summaries.values())
        verdict = "insufficient_data"
        if complete and interval is not None:
            verdict = ("measured_negative" if quality_delta < manifest["minimum_quality_delta"] or interval["upper"] < 0
                       else "measured_positive" if interval["lower"] >= manifest["minimum_token_gain"]
                       else "no_clear_effect")
        matched_unverified = manifest["comparison"] == "matched" and output["measurement_kind"] == "observed"
        if matched_unverified:
            # These receipt versions have no observed role-level setting map.
            # Equal policy-file hashes cannot qualify a causal effort match.
            verdict = "insufficient_data"
        output["suites"].append({"suite": suite["id"], "full_cohort": summaries,
            "quality_delta": quality_delta,
            "policy_result": verdict,
            "matched_effort_qualification": "not_verified" if matched_unverified else "not_applicable_to_observed_claim",
            "claim_scope": "matched_role_mapping_unavailable" if matched_unverified else "synthetic_accounting_only" if output["measurement_kind"] == "synthetic" else "supplied_qualified_observations_required",
            "accepted_pairs": {**paired_report, "task_clusters": len(paired),
                "median_relative_token_saving": statistics.median(paired) if paired else None,
                "selection": "accepted_in_both_arms_with_complete_usage",
                "uncertainty": "not_estimable" if interval is None else interval}})
    output["limitations"] = ["independent grading and isolation require qualified producers",
                             "no aggregate score across different suites",
                             "sample byte savings are not task token savings"]
    return output


def _metric_sum(rows, name):
    values = [benchmark_metrics.metric_value(row, name) for row in rows]
    known = [value for value in values if value is not None]
    return sum(known) if known or not rows else None


def _phase_summary(rows, name):
    values = [row["phase_seconds"][name] for row in rows if row["schema_version"] == 2]
    known = [item["value"] for item in values if item["value"] is not None]
    return {"observed_seconds": sum(known) if known else None,
            "observed_trials": sum(item["coverage"] == "observed" for item in values),
            "partial_trials": sum(item["coverage"] == "partial" for item in values),
            "unavailable_trials": len(rows) - len(values) + sum(item["coverage"] == "unavailable" for item in values)}


def _paired_cost_report(suite, planned, records, arms, repeats):
    indexed = {(row["task_id"], row["repeat"], row["arm"]): records.get(row["trial_id"])
               for row in planned}
    accepted_count = complete_count = 0
    complete_totals = [0, 0]
    selected = {arm: [] for arm in arms}
    per_task, clusters = [], []
    for task in suite["tasks"]:
        distribution, differences = [], []
        for repeat in range(repeats):
            pair = [indexed[(task["id"], repeat, arm)] for arm in arms]
            totals = [_tokens(row) if row and _complete(row) else None for row in pair]
            accepted = all(row and row["outcome"] == "accepted" for row in pair)
            complete = accepted and all(value is not None for value in totals)
            if accepted:
                accepted_count += 1
                for arm, row in zip(arms, pair):
                    selected[arm].append(row)
            if complete:
                complete_count += 1
                for index, value in enumerate(totals):
                    complete_totals[index] += value
            status = ("not_run" if any(row is None for row in pair) else
                      "not_accepted_in_both" if not accepted else
                      "incomplete_usage" if not complete else
                      "zero_baseline" if totals[0] == 0 else "estimable")
            relative = (totals[0] - totals[1]) / totals[0] if status == "estimable" else None
            if relative is not None:
                differences.append(relative)
            distribution.append({"repeat": repeat,
                "baseline_outcome": pair[0]["outcome"] if pair[0] else "not_run",
                "harness_outcome": pair[1]["outcome"] if pair[1] else "not_run",
                "baseline_tokens": totals[0], "harness_tokens": totals[1],
                "baseline_observed_tokens": _observed_tokens(pair[0]) if pair[0] else None,
                "harness_observed_tokens": _observed_tokens(pair[1]) if pair[1] else None,
                "baseline_usage_coverage": _coverage(pair[0]),
                "harness_usage_coverage": _coverage(pair[1]),
                "absolute_token_saving": totals[0] - totals[1] if complete else None,
                "relative_token_saving": relative, "relative_token_saving_status": status})
        mean = statistics.mean(differences) if differences else None
        if mean is not None:
            clusters.append(mean)
        per_task.append({"task_id": task["id"], "repeats": distribution,
                         "mean_relative_token_saving": mean})
    costs = {arm: {"observed_tokens": sum(_observed_tokens(row) for row in rows),
                   "elapsed_seconds": sum(row["elapsed_seconds"] for row in rows),
                   "usage_coverage": "unknown" if not rows or any(_coverage(row) == "unknown" for row in rows) else "complete" if all(_complete(row) for row in rows) else "partial",
                   "unknown_usage_trials": sum(_coverage(row) == "unknown" for row in rows),
                   "partial_usage_trials": sum(_coverage(row) == "partial" for row in rows)}
             for arm, rows in selected.items()}
    baseline, treatment = complete_totals
    return clusters, {"accepted_repeat_pairs": accepted_count, "complete_usage_repeat_pairs": complete_count,
        "cost_scope": "accepted_in_both_arms_including_partial_usage", "costs": costs,
        "complete_usage_costs": {"baseline_tokens": baseline, "harness_tokens": treatment,
            "absolute_token_saving": baseline - treatment,
            "relative_token_saving_of_totals": (baseline - treatment) / baseline if baseline > 0 else None,
            "relative_token_saving_status": "estimable" if baseline > 0 else "zero_baseline" if complete_count else "not_estimable"},
        "per_task": per_task}


def _observed_tokens(row):
    return sum(value for usage in row["usage"]
               for value in (usage["input_tokens"], usage["output_tokens"]) if value is not None)


def _coverage(row):
    if row is None or row["usage_coverage"] == "unknown":
        return "unknown"
    return "complete" if _complete(row) else "partial"


def _complete(row):
    return (row["usage_coverage"] == "complete" and bool(row["usage"])
            and all(usage["status"] == "complete" and type(usage["input_tokens"]) is int
                    and type(usage["output_tokens"]) is int for usage in row["usage"]))


def _tokens(row):
    return sum(usage["input_tokens"] + usage["output_tokens"] for usage in row["usage"])


def _interval(values, manifest):
    rng = random.Random(manifest["seed"])
    estimates = sorted(statistics.mean(rng.choices(values, k=len(values)))
                       for _ in range(manifest["bootstrap_resamples"]))
    alpha = (1 - manifest["confidence"]) / 2
    return {"method": "task_cluster_bootstrap_mean", "confidence": manifest["confidence"],
            "lower": estimates[int(alpha * len(estimates))],
            "upper": estimates[min(len(estimates)-1, int((1-alpha) * len(estimates)))]}


def dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "schedule", "report"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--assets-root", required=True)
    parser.add_argument("--trials", help="Strict YAML trial receipt array for report")
    parser.add_argument("--output", help="Exclusive Strict YAML output")
    args = parser.parse_args(argv)
    manifest = load(safe_private(args.manifest), MANIFEST_SCHEMA)
    readiness = validate_manifest(manifest, args.assets_root)
    if args.action == "report" and not args.trials:
        parser.error("report requires --trials")
    value = (readiness if args.action == "validate" else
             {**readiness, "trials": schedule(manifest)} if args.action == "schedule" else
             summarize(manifest, load(safe_private(args.trials)), args.assets_root))
    if args.output:
        dump(safe_private(args.output), value, exclusive=True)
    from .output import write_json
    write_json(value)
    return 0
