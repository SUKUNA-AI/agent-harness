"""One frozen mission completion policy; legacy runs keep their old policy."""
from __future__ import annotations

import hashlib
import json

from .core import HarnessError, digest
from .file_io import read_regular_file
from .supervisor_store import atomic_write

POLICY = "required_or_planned_v2"
POLICY_PATH = "policies/completion-v2.json"
DEFER_REASON = "Optional requirement has no executable node in the frozen plan"
REVIEW_SCHEMA = "managed-review.v3.schema.json"


def pin(supervisor):
    if supervisor.contract["schema_version"] != 2 or supervisor.contract["mode"] != "managed":
        return
    from .task_packets import schema_file
    path = schema_file(supervisor, "managed-review.schema.json", 3)
    schema_digest = hashlib.sha256(read_regular_file(path)).hexdigest()
    value = {"schema_version": 1, "policy": POLICY, "review_version": 3, "review_schema_digest": schema_digest}
    raw = (json.dumps(value, sort_keys=True) + "\n").encode()
    # Intent in the authoritative journal precedes the immutable projection.
    supervisor.store.append_event({"kind": "completion_policy", "value": value,
        "artifact_digest": hashlib.sha256(raw).hexdigest(), "path": POLICY_PATH})
    atomic_write(supervisor.store.path(POLICY_PATH), raw, exclusive=True)


def policy(supervisor):
    pins = [event["payload"] for event in supervisor.store.events() if event["payload"].get("kind") == "completion_policy"]
    path = supervisor.store.path(POLICY_PATH)
    if not pins:
        if path.exists():
            raise HarnessError("Completion policy lacks authoritative pin")
        return None  # Frozen pre-policy runs never acquire new semantics implicitly.
    if len(pins) != 1 or pins[0]["path"] != POLICY_PATH:
        raise HarnessError("Completion policy pin is ambiguous")
    pin_value = pins[0]
    raw = read_regular_file(path)
    value = json.loads(raw)
    if (not isinstance(value, dict) or hashlib.sha256(raw).hexdigest() != pin_value["artifact_digest"] or value != pin_value["value"]
            or value.get("schema_version") != 1 or value.get("policy") != POLICY or value.get("review_version") != 3):
        raise HarnessError("Completion policy integrity conflict")
    return value


def rows(contract, plan, frontier):
    result = []
    for requirement in contract["mission"]["requirements"]:
        names = [n["id"] for n in plan["tasks"] if requirement["id"] in n["requirement_ids"]]
        refs = [frontier[name]["record_id"] for name in names if name in frontier]
        promised = requirement["required"] or bool(names)
        result.append({"id": requirement["id"], "required": requirement["required"], "planned": bool(names),
            "status": "accepted" if names and len(refs) == len(names) else "pending" if promised else "deferred",
            "evidence_ids": refs if promised else [], "reason": "" if promised else DEFER_REASON})
    return result


def complete(coverage):
    return bool(coverage) and all(row["status"] == ("accepted" if row["required"] or row["planned"] else "deferred")
        and (bool(row["evidence_ids"]) if row["required"] or row["planned"] else not row["evidence_ids"] and row["reason"] == DEFER_REASON)
        for row in coverage)


def validate_review(coverage, verdict):
    expected = {row["id"]: row for row in coverage}
    actual = verdict.get("requirements", [])
    ids = [row["id"] for row in actual]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise HarnessError("Final review requirement coverage is missing or ambiguous")
    for row in actual:
        current = expected[row["id"]]
        if row["status"] == "deferred":
            if current["required"] or current["planned"] or row["evidence_ids"]:
                raise HarnessError("Final review deferred a required or promised requirement")
            # The controller authorizes deferral. A reviewer may only append
            # nonempty detail after the literal "; " separator, preserving its
            # explanation; reason text never grants deferral authority.
            base = current["reason"]
            reason = row["reason"]
            if reason != base and not (reason.startswith(base + "; ") and reason[len(base) + 2:].strip()):
                raise HarnessError("Final review deferral reason must retain the controller explanation")
        elif row["status"] == "accepted":
            if (current["status"] != "accepted" or not row["evidence_ids"]
                    or not set(row["evidence_ids"]) <= set(current["evidence_ids"])):
                raise HarnessError("Final review cites unknown or empty coverage evidence")
        if verdict["verdict"] == "approved" and row["status"] != ("accepted" if current["required"] or current["planned"] else "deferred"):
            raise HarnessError("Original mission coverage does not permit final approval")
    if verdict["verdict"] == "approved" and (verdict["findings"] or verdict["unrepresented_requirements"] or not complete(coverage)):
        raise HarnessError("Original mission coverage does not permit final approval")
