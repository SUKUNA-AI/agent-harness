"""Fresh read-only review of an exact managed candidate, with durable recovery."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, TypedDict, cast

from .agent_lifecycle import GlobalSlots, guard_invocations, invocation_timeout, post_invocation_guard, InvocationUnresolved
from .codex_execution import MAX_EVENT_BYTES, MAX_LOG_BYTES
from .core import HarnessError, digest
from .file_io import read_regular_file
from .strict_yaml import loads, validate
from .supervisor_store import atomic_write
from .task_packets import compile_packet, preflight, invoke, schema_file


class ReceiptRecord(TypedDict):
    """Validated journal record; retry diagnostics never enter this collection."""
    record_id: str
    record_type: str
    plan_digest: str | None
    invocation_id: str | None
    attempt_id: str
    payload: dict[str, Any]


class FailureHistoryEntry(TypedDict):
    category: str
    signature: str
    repairable: bool
    detail: str
    snapshot_id: str


class InvalidRawVerdict(ValueError):
    """A persisted response violates the bounded reviewer output protocol."""
    def __init__(self, reason):
        super().__init__("Invalid final-review raw verdict: " + reason)


def _raw_verdict(raw, schema="managed-review.schema.json"):
    if not isinstance(raw, bytes) or len(raw) > MAX_LOG_BYTES:
        raise InvalidRawVerdict("event stream exceeds byte limit or is not bytes")
    message = None
    for line in raw.splitlines():
        if not line:
            continue
        if len(line) > MAX_EVENT_BYTES:
            raise InvalidRawVerdict("event exceeds byte limit")
        try:
            event = json.loads(line)
        except (ValueError, RecursionError) as error:
            raise InvalidRawVerdict("event is not valid bounded JSON") from error
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise InvalidRawVerdict("event must be an object with a text type")
        if event["type"] != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise InvalidRawVerdict("completed item must be an object with a text type")
        if item["type"] == "agent_message":
            if not isinstance(item.get("text"), str):
                raise InvalidRawVerdict("agent message text must be a string")
            message = item["text"]
    if message is None:
        raise InvalidRawVerdict("completed stream has no agent message verdict")
    try:
        verdict = loads(message)
        if not isinstance(verdict, dict):
            raise InvalidRawVerdict("structured verdict must be an object")
        validate(verdict, schema)
        return verdict
    except InvalidRawVerdict:
        raise
    except ValueError as error:
        raise InvalidRawVerdict("structured verdict fails schema validation") from error


def _review_binding(supervisor):
    from .completion_eligibility import policy
    pinned = policy(supervisor)
    if pinned is None:
        return None
    path = supervisor.store.path("context/required-" + pinned["review_schema_digest"] + ".schema.json")
    checksum = hashlib.sha256(read_regular_file(path)).hexdigest()
    if checksum != pinned["review_schema_digest"]:
        raise HarnessError("Frozen final-review schema integrity conflict")
    return {"policy_digest": digest(pinned), "schema_digest": checksum, "schema_path": str(path), "review_version": 3}


def _bound_schema(binding):
    if hashlib.sha256(read_regular_file(binding["schema_path"])).hexdigest() != binding["schema_digest"]:
        raise HarnessError("Pinned final-review schema integrity conflict")
    return binding["schema_path"]


def _coverage_payload(supervisor, candidate_digest, verdict):
    from .verified_flow import mission_digest
    binding = _review_binding(supervisor)
    value = {"candidate_digest": candidate_digest, "mission_digest": mission_digest(supervisor),
        "coverage": [{"id": row["id"], "status": row["status"] if row["status"] in {"accepted", "deferred"} else "blocked",
            "evidence_ids": row["evidence_ids"], **({"reason": row["reason"]} if binding else {})} for row in verdict["requirements"]]}
    if binding:
        value["review_binding"] = binding
    return value


def _publish_verdict_record(receipts, receipt_index, intent, completion, candidate_digest, kind, payload):
    """Stable publication bytes across the completion/coverage/review boundary."""
    node = "@coverage" if kind == "coverage" else "@review"
    record = receipts.envelope(kind, node, intent["attempt_id"], intent["invocation_id"])
    record["record_id"] = kind + "-" + digest([intent["invocation_id"], candidate_digest, kind])
    # The derivation belongs to the exact persisted completion, including its
    # publication time/generation, rather than the time of a recovery attempt.
    record["created_at"] = completion["created_at"]
    record["generation"] = completion["generation"]
    record["payload"] = payload
    record["input_refs"] = list(intent["input_refs"])
    existing = receipt_index.get(record["record_id"])
    legacy = receipt_index.get(intent["invocation_id"] + "-" + kind)
    if legacy is not None:
        # A prior version could stop after coverage and before review. Keep its
        # immutable bytes/ID, but only after the caller has validated the exact
        # raw verdict and the full candidate/invocation subject matches.
        subject = ("record_type", "project_id", "run_id", "task_id", "plan_digest", "node_id",
            "attempt_id", "invocation_id", "producer_id", "producer_kind", "execution_owner", "input_refs", "payload")
        if existing is not None or any(legacy[key] != record[key] for key in subject):
            raise HarnessError("Final " + kind + " legacy replay integrity conflict")
        return receipts.publish(legacy)
    if existing is not None and {k: v for k, v in existing.items() if k != "payload_digest"} != record:
        raise HarnessError("Final " + kind + " replay integrity conflict")
    return receipts.publish(record)


def evidence_errors(supervisor, candidate_digest):
    """Validate the final reviewer subject and raw artifact without model work."""
    records = supervisor.receipts().records()
    plan_digest = digest(supervisor.store.read(supervisor.state["plan_path"], "agent-plan.schema.json"))
    reviews = [r for r in records if r["plan_digest"] == plan_digest and r["record_type"] == "review" and r["payload"]["candidate_digest"] == candidate_digest and r["payload"]["verdict"] == "approved"]
    if not reviews:
        return ["final-review receipt missing"]
    try:
        binding = _review_binding(supervisor)
    except (HarnessError, OSError, ValueError) as error:
        return [str(error)]
    if binding and (reviews[-1]["schema_version"] != 2 or reviews[-1]["payload"].get("review_binding") != binding):
        return ["final-review policy/schema subject mismatch"]
    try:
        errors = _record_errors(records, reviews[-1], strict=supervisor.contract["schema_version"] == 2)
    except InvalidRawVerdict as error:
        return [str(error)]
    if binding:
        proofs = [r for r in records if r["record_type"] == "coverage" and r["invocation_id"] == reviews[-1]["invocation_id"]]
        if len(proofs) != 1:
            return errors + ["final original-mission coverage receipt missing or ambiguous"]
        if not errors:
            completion = next(r for r in records if r["record_id"] == reviews[-1]["payload"]["completion_ref"])
            try:
                artifact = completion["payload"]["logs"]
                raw = read_regular_file(artifact["stdout"])
                if hashlib.sha256(raw).hexdigest() != artifact["stdout_digest"]:
                    return ["final-review artifact integrity conflict"]
                verdict = _raw_verdict(raw, _bound_schema(binding))
                from .completion_eligibility import validate_review
                validate_review(supervisor.plan_progress()["requirements"], verdict)
                if proofs[0]["schema_version"] != 2 or proofs[0]["payload"] != _coverage_payload(supervisor, candidate_digest, verdict):
                    errors.append("final coverage differs from current policy/raw verdict")
            except (HarnessError, OSError, ValueError) as error:
                errors.append(str(error))
        return errors
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
            if not errors:
                completion = next(r for r in records if r["record_id"] == reviews[-1]["payload"]["completion_ref"])
                try:
                    artifact = completion["payload"]["logs"]
                    raw = read_regular_file(artifact["stdout"])
                    if hashlib.sha256(raw).hexdigest() != artifact["stdout_digest"]:
                        errors.append("final-review artifact integrity conflict")
                    elif value != _coverage_payload(supervisor, candidate_digest, _raw_verdict(raw)):
                        errors.append("final coverage differs from the raw reviewer verdict")
                except InvalidRawVerdict as error:
                    errors.append(str(error))
                except (OSError, ValueError, TypeError, KeyError):
                    errors.append("final-review artifact unavailable")
    return errors


def _record_errors(records, record, *, strict=False):
    """Integrity of a known verdict is independent of whether it approves."""
    by_id = {r["record_id"]: r for r in records}
    completion = by_id.get(record["payload"]["completion_ref"])
    if completion is None or completion["record_type"] != "completion" or completion["invocation_id"] != record["invocation_id"]:
        return ["final-review completion subject mismatch"]
    intent = by_id.get(completion["payload"]["intent_ref"])
    if (intent is None or intent["record_type"] != "invocation_intent" or intent["node_id"] != "@review"
            or intent["invocation_id"] != record["invocation_id"]
            or intent["payload"]["candidate_digest"] != record["payload"]["candidate_digest"]
            or any(record[key] != completion[key] or completion[key] != intent[key] for key in ("plan_digest", "attempt_id"))
            or completion["payload"]["outcome"] != "completed"):
        return ["final-review invocation/candidate subject mismatch"]
    if record["schema_version"] != completion["schema_version"] or completion["schema_version"] != intent["schema_version"]:
        return ["final-review receipt version subject mismatch"]
    binding = intent["payload"].get("review_binding")
    if binding and (record["schema_version"] != 2 or record["payload"].get("review_binding") != binding
            or record["input_refs"] != intent["input_refs"] or completion["input_refs"] != intent["input_refs"]):
        return ["final-review policy/schema subject mismatch"]
    if strict:
        try:
            post_invocation_guard(intent, completion, {"intents": {intent["invocation_id"]: intent},
                "completions": {completion["invocation_id"]: completion}})
        except InvocationUnresolved as error:
            return [str(error)]
    try:
        artifact = completion["payload"]["logs"]
        raw = read_regular_file(artifact["stdout"])
        if hashlib.sha256(raw).hexdigest() != artifact["stdout_digest"]:
            return ["final-review artifact integrity conflict"]
        verdict = _raw_verdict(raw, _bound_schema(binding) if binding else "managed-review.schema.json")
        if verdict is None or any(record["payload"][key] != verdict[key] for key in ("verdict", "findings")):
            return ["final-review receipt differs from the raw reviewer verdict"]
        return []
    except InvalidRawVerdict:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        return ["final-review artifact unavailable"]


def review_candidate(supervisor, run, candidate):
    receipts = supervisor.receipts()
    accounting = receipts.reconcile_usage()
    try:
        guard_invocations(supervisor, accounting=accounting)
    except InvocationUnresolved as error:
        supervisor.transition("UNKNOWN_OUTCOME", str(error))
        return None
    receipt_records = cast(list[ReceiptRecord], accounting["records"])
    receipt_index = {record["record_id"]: record for record in receipt_records}
    binding = _review_binding(supervisor)
    plan_digest = digest(supervisor.store.read(supervisor.state["plan_path"], "agent-plan.schema.json"))
    cached = [r for r in receipt_records if r["plan_digest"] == plan_digest and r["record_type"] == "review" and r["payload"]["candidate_digest"] == candidate["digest"]]
    if binding:
        if any(r["schema_version"] == 2 and r["payload"].get("review_binding") != binding for r in cached):
            raise HarnessError("Final-review policy/schema subject mismatch")
        cached = [r for r in cached if r["schema_version"] == 2 and r["payload"].get("review_binding") == binding]
    if cached:
        errors = _record_errors(receipt_records, cached[-1], strict=supervisor.contract["schema_version"] == 2)
        if not errors and supervisor.contract["schema_version"] == 2:
            errors = evidence_errors(supervisor, candidate["digest"]) if cached[-1]["payload"]["verdict"] == "approved" else []
        if errors:
            raise HarnessError("; ".join(errors))
        return cached[-1]["payload"]
    all_intents = [r for r in accounting["intents"].values() if r["node_id"] == "@review"]
    intents = [r for r in all_intents if r["plan_digest"] == plan_digest]
    matching = [r for r in intents if r["payload"]["candidate_digest"] == candidate["digest"]
        and (r["payload"].get("review_binding") == binding if binding else r["schema_version"] == 1)]
    matching_completions = cast(list[ReceiptRecord], [accounting["completions"][r["invocation_id"]]
        for r in matching if r["invocation_id"] in accounting["completions"]])
    unresolved = [r for r in intents if r["invocation_id"] not in accounting["completions"]]
    if unresolved:
        supervisor.transition("UNKNOWN_OUTCOME", "UNKNOWN_OUTCOME: final reviewer may have dispatched; no blind retry")
        return None
    prior_failure = None
    if matching:
        last = matching_completions[-1]
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
            failure_history = cast(list[FailureHistoryEntry], [dict(classify(receipts.result(c)), snapshot_id=candidate["digest"])
                for c in matching_completions[:-1]])
            decision = repair_decision(failure_history, prior_failure, candidate["digest"],
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
        if binding:
            schema = Path(_bound_schema(binding))
        else:
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
                "completion_policy": binding,
                "required_verdict_version": 3 if binding else 2 if supervisor.contract["schema_version"] == 2 else 1})
        preflight(supervisor._adapter(), prompt, read_only=True, write_authorized=False,
            native_children=False, output_schema=str(schema))
        with GlobalSlots(supervisor.state_dir).acquire(supervisor.state["run_id"], "@review", "reviewer",
                cancel_file=supervisor.path / "cancel.yaml", timeout=min(60, supervisor.remaining())) as slot:
            intent = receipts.intent("@review", "review-" + str(len(all_intents) + 1), checkout=supervisor.state["cwd"],
                owner=slot.value["lease_id"], candidate_digest=candidate["digest"], review_binding=binding)
            start = time.monotonic()
            result = invoke(supervisor._adapter(), prompt, slot=slot, cwd=supervisor.state["cwd"], read_only=True,
                write_authorized=False, native_children=False, output_schema=str(schema),
                timeout=invocation_timeout(supervisor, "reviewer"),
                deadline=supervisor.state["deadline"], cancel_file=supervisor.path / "cancel.yaml",
                on_process=lambda value: (slot.bind(value), supervisor._on_process(value)),
                log_dir=supervisor.path / "codex" / ("review-" + str(len(all_intents) + 1)))
            result["slot_wait_ms"] = slot.wait_seconds * 1000
            try:
                completion = receipts.completion(intent, result, duration_ms=(time.monotonic() - start) * 1000)
                guard_invocations(supervisor, node_ids={"@review"})
            except (HarnessError, ValueError) as error:
                supervisor.transition("UNKNOWN_OUTCOME", str(error))
                return None
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
    verdict = _raw_verdict(raw, _bound_schema(binding) if binding else "managed-review.schema.json")
    if binding:
        from .completion_eligibility import validate_review
        validate_review(supervisor.plan_progress()["requirements"], verdict)
    elif supervisor.contract["schema_version"] == 2:
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
    if supervisor.contract["schema_version"] == 2:
        _publish_verdict_record(receipts, receipt_index, intent, completion, candidate["digest"], "coverage",
            _coverage_payload(supervisor, candidate["digest"], verdict))
    record = _publish_verdict_record(receipts, receipt_index, intent, completion, candidate["digest"], "review",
        {"completion_ref": completion["record_id"], "candidate_digest": candidate["digest"],
            "verdict": verdict["verdict"], "findings": verdict["findings"], **({"review_binding": binding} if binding else {})})
    return record["payload"]
