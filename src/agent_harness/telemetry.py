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
    return (not has_usage_history(metrics) or
            metrics.get("usage_accounting_version") == 2
            and not metrics["missing_usage_turns"]
            and complete_usage({key: metrics.get(key) for key in ("input_tokens", "output_tokens")}))


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
                        "unverified_legacy" if metrics.get("usage_accounting_version") != 2 else
                        "partial" if metrics["missing_usage_turns"] else "reported",
                    "hidden_codex_tools": "unobservable", "cost": "unavailable"})
    metrics["spawn_overhead_status"] = "measured" if metrics.get("spawn_measurements", 0) else "unavailable"
    return metrics
