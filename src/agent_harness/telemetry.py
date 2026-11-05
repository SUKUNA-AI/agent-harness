"""Local counters with explicit unavailable usage and wall-clock definitions."""
from __future__ import annotations

from datetime import datetime


COUNTERS = ("model_turns", "model_invocations", "repair_attempts", "managed_spawns", "observable_codex_tool_calls",
            "controlled_tool_calls", "duplicate_tool_calls", "fresh_revalidations",
            "stale_memory_prevented", "manual_interventions", "recoveries", "recovery_successes",
            "cache_hits_informational", "cache_hits_checks", "integration_conflicts")


def initial():
    return {**{name: 0 for name in COUNTERS}, "input_tokens": None, "output_tokens": None,
            "usage_accounting_version": 2,
            "reported_usage_turns": 0, "missing_usage_turns": 0,
            "spawn_seconds": 0.0, "spawn_measurements": 0, "worktree_seconds": 0.0, "integration_seconds": 0.0}


def complete_usage(usage):
    """Zero is observed usage; missing, partial or malformed counts are not."""
    return (isinstance(usage, dict)
            and all(type(usage.get(key)) is int and usage[key] >= 0
                    for key in ("input_tokens", "output_tokens"))
            and all(type(value) is int and value >= 0 for value in usage.values()))


def has_usage_history(metrics):
    return (any(metrics.get(key, 0) for key in ("model_invocations", "model_turns", "reported_usage_turns", "missing_usage_turns"))
            or any(metrics.get(key) is not None for key in ("input_tokens", "output_tokens")))


def budget_usage_known(metrics):
    """Legacy aggregates cannot prove that every historical turn was complete."""
    if metrics.get("coverage_status") == "unverified_legacy" or metrics.get("legacy_history_unverified") is True:
        return False
    if metrics.get("usage_accounting_version") == 3:
        return metrics.get("coverage_status") == "complete" and not metrics.get("unknown_invocation_ids")
    return (not has_usage_history(metrics) or
            metrics.get("usage_accounting_version") == 2
            and not metrics["missing_usage_turns"]
            and complete_usage({key: metrics.get(key) for key in ("input_tokens", "output_tokens")}))


def reconcile(metrics, intents, completions):
    """Rebuild accounting from unique receipts; never add cumulative + hook totals.

    Managed process output is the authoritative invocation_total. Observer hooks
    are intentionally not inputs to this function. Cached/reasoning counters are
    subsets, not additional billable tokens.
    """
    covered, unknown = [], []
    totals = {"input_tokens": None, "output_tokens": None}
    turns = tools = measurements = 0
    model_seconds = 0.0
    slot_wait_seconds = 0.0
    spawn = 0.0
    for invocation in sorted(intents):
        completion = completions.get(invocation)
        if completion is None:
            unknown.append(invocation)
            continue
        value = completion["payload"]
        (covered if value["usage_status"] == "complete" else unknown).append(invocation)
        turns += int(value["turn_started"])
        tools += value["observable_tool_calls"]
        model_seconds += (value["duration_ms"] or 0) / 1000
        slot_wait_seconds += (value["slot_wait_ms"] or 0) / 1000
        if value["spawn_seconds"] is not None:
            spawn += value["spawn_seconds"]
            measurements += 1
        for key in totals:
            if value[key] is not None:
                totals[key] = (totals[key] or 0) + value[key]
    from .core import digest
    metrics.update(totals, usage_accounting_version=3, model_invocations=len(intents), model_turns=turns,
        covered_invocation_ids=covered, unknown_invocation_ids=unknown,
        coverage_status="partial" if unknown else "complete", reported_usage_turns=len(covered),
        missing_usage_turns=len(unknown), usage_sources_digest=digest({key: value["payload_digest"] for key, value in sorted(completions.items())}),
        known_lower_bound_tokens=sum(v or 0 for v in totals.values()),
        input_available=totals["input_tokens"] is not None, output_available=totals["output_tokens"] is not None,
        spawn_seconds=spawn, spawn_measurements=measurements, observable_codex_tool_calls=tools,
        model_seconds=model_seconds, slot_wait_seconds=slot_wait_seconds)


def observe(metrics, result):
    if not has_usage_history(metrics):
        metrics["usage_accounting_version"] = 2
    metrics["model_invocations"] = metrics.get("model_invocations", 0) + 1
    metrics["model_turns"] += int(result.get("turn_started", result.get("usage") is not None))
    usage = result.get("usage")
    spawn_seconds = result.get("spawn_seconds")
    if type(spawn_seconds) in (int, float) and spawn_seconds >= 0:
        metrics["spawn_seconds"] += spawn_seconds
        metrics["spawn_measurements"] = metrics.get("spawn_measurements", 0) + 1
    if not complete_usage(usage):
        metrics["missing_usage_turns"] += 1
    else:
        metrics["reported_usage_turns"] += 1
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                metrics[key] = (metrics[key] or 0) + value
    if type(result.get("observable_tool_calls")) is int:
        metrics["observable_codex_tool_calls"] += result["observable_tool_calls"]
    for event in result.get("events", []) if isinstance(result.get("events"), list) else []:
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") in {"command_execution", "mcp_tool_call", "web_search"}:
            metrics["observable_codex_tool_calls"] += 1


def report(state):
    metrics = dict(state["metrics"])
    accepted = state["status"] == "ACCEPTED"
    start = datetime.fromisoformat(state["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00"))
    metrics.update({"accepted_tasks": int(accepted), "total_tasks": 1,
                    "time_to_acceptance_seconds": max(0, (end - start).total_seconds()) if accepted else None,
                    "usage_status": "unavailable" if metrics["input_tokens"] is None and metrics["output_tokens"] is None else
                        "unverified_legacy" if metrics.get("coverage_status") == "unverified_legacy" or metrics.get("usage_accounting_version") not in {2, 3} else
                        "partial" if metrics["missing_usage_turns"] else "reported",
                    "hidden_codex_tools": "unobservable", "cost": "unavailable"})
    metrics["spawn_overhead_status"] = "measured" if metrics.get("spawn_measurements", 0) else "unavailable"
    metrics.update(hard_cap_supported=False, admission="boundary_check_without_upper_bound_reservations",
                   budget_enforcement="managed_admission_and_acceptance", accounting_source="process_invocation_total")
    return metrics
