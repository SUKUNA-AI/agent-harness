"""Fresh read-only review of an exact managed candidate, with durable recovery."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from .agent_lifecycle import GlobalSlots
from .core import HarnessError, digest
from .file_io import read_regular_file
from .strict_yaml import loads, _schema
from .supervisor_store import atomic_write
from .task_packets import compile_packet, preflight, invoke, schema_file


def evidence_errors(supervisor, candidate_digest):
    """Validate the final reviewer subject and raw artifact without model work."""
    records = supervisor.receipts().records()
    plan_digest = digest(supervisor.store.read(supervisor.state["plan_path"], "agent-plan.schema.json"))
    reviews = [r for r in records if r["plan_digest"] == plan_digest and r["record_type"] == "review" and r["payload"]["candidate_digest"] == candidate_digest and r["payload"]["verdict"] == "approved"]
    if not reviews:
        return ["final-review receipt missing"]
    errors = _record_errors(records, reviews[-1])
    if supervisor.contract["schema_version"] == 2:
        from .verified_flow import mission_digest
        proofs = [r for r in records if r["record_type"] == "coverage" and r["invocation_id"] == reviews[-1]["invocation_id"]]
        expected = {r["id"] for r in supervisor.contract["mission"]["requirements"]}
        if len(proofs) != 1:
            errors.append("final original-mission coverage receipt missing or ambiguous")
        else:
            value = proofs[0]["payload"]
            rows = value["coverage"]
            ids = [r["id"] for r in rows]
            current = {r["id"]: set(r["evidence_ids"]) for r in supervisor.plan_progress().get("requirements") or []}
            if (value["candidate_digest"] != candidate_digest or value["mission_digest"] != mission_digest(supervisor)
                    or len(ids) != len(set(ids)) or set(ids) != expected
                    or any(r["status"] != "accepted" or not r["evidence_ids"] or not set(r["evidence_ids"]) <= current.get(r["id"], set()) for r in rows)):
                errors.append("final coverage does not match the current mission/frontier")
    return errors


def _record_errors(records, record):
    """Integrity of a known verdict is independent of whether it approves."""
    by_id = {r["record_id"]: r for r in records}
    completion = by_id.get(record["payload"]["completion_ref"])
    if completion is None or completion["record_type"] != "completion" or completion["invocation_id"] != record["invocation_id"]:
        return ["final-review completion subject mismatch"]
    try:
        artifact = completion["payload"]["logs"]
        raw = read_regular_file(artifact["stdout"])
        if hashlib.sha256(raw).hexdigest() != artifact["stdout_digest"]:
            return ["final-review artifact integrity conflict"]
        return []
    except (OSError, ValueError, TypeError):
        return ["final-review artifact unavailable"]


def review_candidate(supervisor, run, candidate):
    receipts = supervisor.receipts()
    accounting = receipts.reconcile_usage()
    history = accounting["records"]
    plan_digest = digest(supervisor.store.read(supervisor.state["plan_path"], "agent-plan.schema.json"))
    cached = [r for r in history if r["plan_digest"] == plan_digest and r["record_type"] == "review" and r["payload"]["candidate_digest"] == candidate["digest"]]
    if cached:
        errors = _record_errors(history, cached[-1])
        if not errors and supervisor.contract["schema_version"] == 2:
            errors = evidence_errors(supervisor, candidate["digest"]) if cached[-1]["payload"]["verdict"] == "approved" else []
        if errors:
            raise HarnessError("; ".join(errors))
        return cached[-1]["payload"]
    all_intents = [r for r in accounting["intents"].values() if r["node_id"] == "@review"]
    intents = [r for r in all_intents if r["plan_digest"] == plan_digest]
    matching = [r for r in intents if r["payload"]["candidate_digest"] == candidate["digest"]]
    unresolved = [r for r in intents if r["invocation_id"] not in accounting["completions"]]
    if unresolved:
        supervisor.transition("UNKNOWN_OUTCOME", "UNKNOWN_OUTCOME: final reviewer may have dispatched; no blind retry")
        return None
    prior_failure = None
    if matching:
        last = accounting["completions"][matching[-1]["invocation_id"]]
        payload = last["payload"]
        if payload["outcome"] != "completed":
            if payload["dispatch_state"] == "not_dispatched_proven":
                supervisor.transition("BLOCKED_AMBIGUOUS", "NOT_DISPATCHED: reviewer refused locally")
                return None
            if payload["outcome"] == "unknown" or payload["process_tree_stopped"] is not True:
                supervisor.transition("UNKNOWN_OUTCOME", "Final reviewer outcome/cleanup uncertain")
                return None
            from .failure_policy import classify, repair_decision
            prior_failure = classify(receipts.result(last))
            history = [dict(classify(receipts.result(c)), snapshot_id=candidate["digest"])
                for i in matching[:-1] for c in [accounting["completions"][i["invocation_id"]]]]
            decision = repair_decision(history, prior_failure, candidate["digest"],
                attempts=len(all_intents), max_attempts=supervisor.state["max_attempts"])
            if decision["action"] != "repair":
                if decision["status"] == "NO_PROGRESS":
                    supervisor.transition("FAILED_REPAIRABLE", "Repeated known reviewer failure without new progress")
                supervisor.transition(decision["status"], "Known reviewer failure: " + decision["reason"])
                return None
            supervisor.state["metrics"]["repair_attempts"] += 1
            matching = []  # A fresh read-only attempt, never replay an unknown writer.
    if matching:
        intent = matching[-1]
        completion = accounting["completions"][intent["invocation_id"]]
    else:
        if len(all_intents) >= supervisor.state["max_attempts"]:
            supervisor.transition("FAILED_TERMINAL", "Original final-review attempt budget exhausted")
            return None
        if not supervisor._model_budget_ok(for_spawn=True):
            return None
        if not supervisor._adapter().capabilities().get("output_schema"):
            supervisor.transition("BLOCKED_ENVIRONMENT", "Fresh final review needs structured output capability")
            return None
        schema = supervisor.store.path("plans/final-review-schema.json")
        data = read_regular_file(schema_file(supervisor, "managed-review.schema.json", supervisor.contract["schema_version"]))
        if schema.exists():
            if read_regular_file(schema) != data:
                raise HarnessError("Final-review output schema integrity conflict")
        else:
            atomic_write(schema, data, exclusive=True)
        # This is a fresh session: the integrator's exact thread remains intact
        # for bounded repairs by the same combined-candidate owner.
        prompt = compile_packet(supervisor, "HARNESS_FINAL_REVIEW\nReview the final frozen candidate read-only. "
            "Compare the ORIGINAL mission against all requirements, outputs, combined diff and raw evidence. "
            "Do not edit, spawn, change settings, commit or publish. Return only structured schema output. "
            "Use changes_requested for defects and blocked for missing evidence. Read every required part.", {
                "goal": supervisor.contract["task"]["objective"], "task": supervisor.contract["task"],
                "mission": supervisor.contract.get("mission"),
                "plan": supervisor.store.read(supervisor.state["plan_path"], "agent-plan.schema.json"),
                "acceptance": supervisor.contract["acceptance"], "candidate_digest": candidate["digest"],
                "raw_evidence": str(run.attempt_dir()),
                "coverage": supervisor.plan_progress().get("requirements"),
                "prior_reviewer_failure": prior_failure,
                "required_verdict_version": 2 if supervisor.contract["schema_version"] == 2 else 1})
        preflight(supervisor._adapter(), prompt, read_only=True, write_authorized=False,
            native_children=False, output_schema=str(schema))
        with GlobalSlots(supervisor.state_dir).acquire(supervisor.state["run_id"], "@review", "reviewer",
                cancel_file=supervisor.path / "cancel.yaml", timeout=min(60, supervisor.remaining())) as slot:
            intent = receipts.intent("@review", "review-" + str(len(all_intents) + 1), checkout=supervisor.state["cwd"],
                owner=slot.value["lease_id"], candidate_digest=candidate["digest"])
            start = time.monotonic()
            result = invoke(supervisor._adapter(), prompt, slot=slot, cwd=supervisor.state["cwd"], read_only=True,
                write_authorized=False, native_children=False, output_schema=str(schema),
                timeout=min(supervisor.remaining(), supervisor.contract["budgets"]["turn_timeout_seconds"]),
                deadline=supervisor.state["deadline"], cancel_file=supervisor.path / "cancel.yaml",
                on_process=lambda value: (slot.bind(value), supervisor._on_process(value)),
                log_dir=supervisor.path / "codex" / ("review-" + str(len(all_intents) + 1)))
            result["slot_wait_ms"] = slot.wait_seconds * 1000
            completion = receipts.completion(intent, result, duration_ms=(time.monotonic() - start) * 1000)
            if result.get("process_tree_stopped") is True or supervisor.contract["schema_version"] == 1 and result.get("process_tree_stopped") is not False:
                slot.complete(result)
        supervisor.state.update(process_identity=None, process_status=result["status"])
        receipts.reconcile_usage()
        supervisor._save("Fresh final-review completion persisted before gate")
        if (completion["payload"]["outcome"] != "completed"
                and completion["payload"]["dispatch_state"] != "not_dispatched_proven"
                and completion["payload"]["process_tree_stopped"] is True
                and completion["payload"]["outcome"] != "unknown"):
            return review_candidate(supervisor, run, candidate)
    if not supervisor._model_budget_ok():
        return None
    if completion["payload"]["dispatch_state"] == "not_dispatched_proven":
        supervisor.transition("BLOCKED_AMBIGUOUS", "NOT_DISPATCHED: reviewer refused before process creation")
        return None
    if (completion["payload"]["outcome"] != "completed" or completion["payload"]["process_tree_stopped"] is False
            or supervisor.contract["schema_version"] == 2 and completion["payload"]["process_tree_stopped"] is not True):
        supervisor.transition("UNKNOWN_OUTCOME", "Final reviewer has no confirmed successful response")
        return None
    artifact = completion["payload"]["logs"]
    if artifact["stdout"] is None:
        supervisor.transition("BLOCKED_AMBIGUOUS", "Fresh reviewer has no structured verdict artifact")
        return None
    path = Path(artifact["stdout"])
    if supervisor.path not in path.absolute().parents:
        raise HarnessError("Review artifact escaped owned run")
    raw = read_regular_file(path)
    if hashlib.sha256(raw).hexdigest() != artifact["stdout_digest"]:
        raise HarnessError("Review completion artifact integrity conflict")
    events = [json.loads(line) for line in raw.splitlines() if line]
    messages = [event["item"]["text"] for event in events if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message"]
    if not messages:
        supervisor.transition("BLOCKED_AMBIGUOUS", "Fresh reviewer completed without structured verdict")
        return None
    verdict = loads(messages[-1], "managed-review.schema.json")
    if supervisor.contract["schema_version"] == 2:
        expected = {r["id"] for r in supervisor.contract["mission"]["requirements"]}
        rows = verdict.get("requirements", [])
        actual = [r["id"] for r in rows]
        if verdict["schema_version"] != 2 or len(actual) != len(set(actual)) or set(actual) != expected:
            raise HarnessError("Final review requirement coverage is missing or ambiguous")
        proof = supervisor.plan_progress().get("requirements") or []
        allowed = {row["id"]: set(row["evidence_ids"]) for row in proof}
        if any(not row["evidence_ids"] or not set(row["evidence_ids"]) <= allowed.get(row["id"], set()) for row in rows if row["status"] == "accepted"):
            raise HarnessError("Final review cites unknown or empty coverage evidence")
        if verdict["verdict"] == "approved" and (verdict["findings"] or verdict["unrepresented_requirements"]
                or any(r["status"] != "accepted" for r in rows)):
            raise HarnessError("Original mission coverage does not permit final approval")
    run.assert_current(run.state(), run.contract())  # A read-only promise is insufficient.
    record = receipts.envelope("review", "@review", intent["attempt_id"], intent["invocation_id"])
    record["record_id"] = intent["invocation_id"] + "-review"
    record["payload"] = {"completion_ref": completion["record_id"], "candidate_digest": candidate["digest"],
        "verdict": verdict["verdict"], "findings": verdict["findings"]}
    if supervisor.contract["schema_version"] == 2:
        from .verified_flow import mission_digest
        proof = receipts.envelope("coverage", "@coverage", intent["attempt_id"], intent["invocation_id"])
        proof["record_id"] = intent["invocation_id"] + "-coverage"
        proof["payload"] = {"candidate_digest": candidate["digest"], "mission_digest": mission_digest(supervisor),
            "coverage": [{"id": row["id"], "status": "accepted" if row["status"] == "accepted" else "blocked", "evidence_ids": row["evidence_ids"]} for row in verdict["requirements"]]}
        existing = next((r for r in history if r["record_id"] == proof["record_id"]), None)
        if existing is not None and existing["payload"] != proof["payload"]:
            raise HarnessError("Final coverage replay conflict")
        if existing is None:
            receipts.publish(proof)
    receipts.publish(record)
    return record["payload"]
