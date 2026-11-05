"""Immutable managed operation records, committed through the existing journal.

The journal is authoritative; per-record YAML files and usage are projections.
An intent never proves dispatch, completion or zero cost. Unknown invocations
are deliberately not replayed by recovery.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import uuid

from . import __version__, telemetry
from .core import HarnessError, digest, now
from .strict_yaml import validate

SCHEMA = "execution-receipt.schema.json"
REQUIRED_PAYLOAD = {
    "invocation_intent": {"checkout", "owner_id", "dependency_vector", "contract_digest", "deadline", "dispatch_state", "candidate_digest"},
    "completion": {"intent_ref", "outcome", "dispatch_state", "usage_scope", "usage_status", "input_tokens", "output_tokens",
        "cached_input_tokens", "reasoning_tokens", "usage_source_ref", "covered_event_ids", "correlation_status", "started_at",
        "finished_at", "duration_ms", "slot_wait_ms", "clock", "thread_id", "turn_started", "observable_tool_calls", "spawn_seconds", "process_tree_stopped", "logs"},
    "integration_intent": {"completion_ref", "patch_ref", "patch_digest", "worker_checkout_ref", "ownership_token", "pre_head", "pre_tree", "expected_tree", "result_kind", "dependency_vector"},
    "integration": {"integration_intent_ref", "completion_ref", "result_head", "result_tree", "result_digest", "result_kind"},
    "review": {"completion_ref", "candidate_digest", "verdict", "findings"},
}


def validate_record(record):
    validate(record, SCHEMA)
    if set(record["payload"]) != REQUIRED_PAYLOAD[record["record_type"]]:
        raise HarnessError("Execution receipt payload fields do not match record type")
    if record["record_type"] in {"invocation_intent", "completion"} and record["invocation_id"] is None:
        raise HarnessError("Invocation identity is required for execution receipts")


def build_digest():
    """Bind package code, schemas and vendored runtime; no user source reads."""
    from .receipt_identity import package_build_digest
    return package_build_digest()


class ExecutionReceipts:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.store = supervisor.store

    def records(self, *, repair=False):
        # The journal and its immutable projections form one publication. Keep
        # readers outside the append/link/unlink window of worker callbacks.
        with self.supervisor.mutex:
            return self._records_locked(repair=repair)

    def _records_locked(self, *, repair=False):
        records = {}
        for event in self.store.events():
            payload = event["payload"]
            if payload.get("kind") != "execution_receipt":
                continue
            record = payload["receipt"]
            validate_record(record)
            content = dict(record)
            supplied = content.pop("payload_digest")
            if supplied != digest(content):
                raise HarnessError("Execution receipt content integrity conflict")
            record_id = record["record_id"]
            if record_id in records and records[record_id] != record:
                raise HarnessError("Execution receipt ID integrity conflict")
            state = self.supervisor.state
            if record["run_id"] != state["run_id"] or record["task_id"] != state["task_id"]:
                raise HarnessError("Execution receipt subject binding mismatch")
            name = "receipts/" + record_id + ".yaml"
            if self.store.path(name).exists():
                if self.store.read(name, SCHEMA) != record:
                    raise HarnessError("Execution receipt projection integrity conflict")
            elif repair:
                self.store.write(name, record, SCHEMA, exclusive=True)
            records[record_id] = record
        return list(records.values())

    def publish(self, record):
        with self.supervisor.mutex:
            record = dict(record)
            record.pop("payload_digest", None)
            record["payload_digest"] = digest(record)
            validate_record(record)
            existing = {r["record_id"]: r for r in self.records()}
            if record["record_id"] in existing:
                if existing[record["record_id"]] != record:
                    raise HarnessError("Execution receipt ID integrity conflict")
                return existing[record["record_id"]]
            # fsync the authoritative chain BEFORE exposing derived state.
            self.store.append_event({"kind": "execution_receipt", "receipt": record})
            self.store.write("receipts/" + record["record_id"] + ".yaml", record, SCHEMA, exclusive=True)
            return record

    def envelope(self, kind, node_id, attempt_id, invocation_id=None):
        state = self.supervisor.state
        plan = self.store.read(state["plan_path"], "agent-plan.schema.json") if state["plan_path"] else None
        return {"schema_version": 1, "record_type": kind, "record_id": uuid.uuid4().hex,
            "project_id": self.supervisor.repository_identity(), "run_id": state["run_id"],
            "task_id": state["task_id"], "plan_digest": digest(plan) if plan else None,
            "node_id": node_id, "attempt_id": attempt_id, "invocation_id": invocation_id,
            "producer_id": "supervisor", "producer_kind": "managed_supervisor",
            "generation": state["generation"], "created_at": now(), "harness_version": __version__,
            "harness_build_digest": build_digest(), "client_version": state["environment"]["codex_version"],
            "capability_receipt_ref": None, "execution_owner": "managed_supervisor",
            "input_refs": [state["contract_digest"]], "payload": {}}

    def intent(self, node_id, attempt_id, *, checkout, owner, dependencies=(), candidate_digest=None):
        value = self.envelope("invocation_intent", node_id, attempt_id, uuid.uuid4().hex)
        value["payload"] = {"checkout": str(checkout), "owner_id": owner,
            "dependency_vector": list(dependencies), "contract_digest": self.supervisor.state["contract_digest"],
            "deadline": self.supervisor.state["deadline"], "dispatch_state": "possibly_dispatched", "candidate_digest": candidate_digest}
        return self.publish(value)

    def completion(self, intent, result, *, duration_ms=None):
        value = self.envelope("completion", intent["node_id"], intent["attempt_id"], intent["invocation_id"])
        value["record_id"] = intent["invocation_id"] + "-completion"
        value["plan_digest"] = intent["plan_digest"]
        usage = result.get("usage")
        counters = {}
        invalid = False
        for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
            count = usage.get(key) if isinstance(usage, dict) else None
            if count is not None and (type(count) is not int or count < 0):
                invalid = True
                count = None
            counters[key] = count
        usage_status = ("invalid" if invalid else "complete" if telemetry.complete_usage(usage)
            else "partial" if any(v is not None for v in counters.values()) else "unavailable")
        status = result.get("status", "unknown")
        dispatch = ("started_observed" if result.get("process_tree_stopped") is False else
            "finished_observed" if status in {"completed", "failed", "cancelled"} else
            "started_observed" if result.get("turn_started") else "possibly_dispatched")
        from .file_io import read_regular_file
        logs = {key: result.get("logs", {}).get(key) for key in ("stdout", "stderr")}
        for stream in ("stdout", "stderr"):
            logs[stream + "_digest"] = hashlib.sha256(read_regular_file(logs[stream])).hexdigest() if logs[stream] else None
        value["payload"] = {"intent_ref": intent["record_id"], "outcome": status if status in {"completed", "failed", "cancelled"} else "unknown",
            "dispatch_state": dispatch, "usage_scope": "invocation_total", "usage_status": usage_status,
            **counters, "usage_source_ref": result.get("logs", {}).get("stdout"), "covered_event_ids": [],
            "correlation_status": "explicit", "started_at": intent["created_at"], "finished_at": now(),
            "duration_ms": duration_ms, "slot_wait_ms": result.get("slot_wait_ms"), "clock": "monotonic", "thread_id": result.get("thread_id"),
            "turn_started": bool(result.get("turn_started", usage is not None)),
            "observable_tool_calls": result.get("observable_tool_calls", 0),
            "spawn_seconds": result.get("spawn_seconds"), "process_tree_stopped": result.get("process_tree_stopped"),
            "logs": logs}
        for existing in self.records():
            if existing["record_id"] == value["record_id"]:
                stable = lambda payload: {k: v for k, v in payload.items() if k not in {"finished_at", "duration_ms"}}
                if stable(existing["payload"]) != stable(value["payload"]):
                    raise HarnessError("Conflicting invocation completion")
                return existing
        return self.publish(value)

    def reconcile_usage(self, *, repair=False):
        records = self.records(repair=repair)
        intents = {r["invocation_id"]: r for r in records if r["record_type"] == "invocation_intent"}
        completions = {}
        for record in records:
            if record["record_type"] != "completion":
                continue
            invocation = record["invocation_id"]
            if invocation not in intents:
                raise HarnessError("Completion lacks authoritative invocation intent")
            intent = intents[invocation]
            if any(record[key] != intent[key] for key in ("node_id", "attempt_id", "plan_digest")) or record["payload"]["intent_ref"] != intent["record_id"]:
                raise HarnessError("Completion subject differs from invocation")
            if invocation in completions and completions[invocation] != record:
                raise HarnessError("Conflicting invocation completion")
            completions[invocation] = record
        if self.supervisor.state.get("schema_version") == 1 or self.supervisor.state["metrics"].get("legacy_history_unverified") is True:
            # A legacy aggregate cannot be upgraded by observing a later call.
            metrics = self.supervisor.state["metrics"]
            observed = telemetry.initial()
            telemetry.reconcile(observed, intents, completions)
            metrics.update(coverage_status="unverified_legacy", legacy_history_unverified=True,
                receipt_input_tokens=observed["input_tokens"], receipt_output_tokens=observed["output_tokens"],
                receipt_model_invocations=observed["model_invocations"],
                covered_invocation_ids=observed["covered_invocation_ids"],
                unknown_invocation_ids=["legacy-history", *observed["unknown_invocation_ids"]],
                usage_sources_digest=observed["usage_sources_digest"],
                missing_usage_turns=max(metrics["missing_usage_turns"], len(observed["unknown_invocation_ids"]) + 1))
            return {"records": records, "intents": intents, "completions": completions}
        telemetry.reconcile(self.supervisor.state["metrics"], intents, completions)
        return {"records": records, "intents": intents, "completions": completions}

    @staticmethod
    def result(completion):
        value = completion["payload"]
        usage = {key: value[key] for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens") if value[key] is not None}
        return {"status": value["outcome"], "usage": usage or None, "thread_id": value["thread_id"],
            "logs": {k: v for k, v in value["logs"].items() if k in {"stdout", "stderr"} and v is not None},
            "turn_started": value["turn_started"], "process_tree_stopped": value["process_tree_stopped"]}
