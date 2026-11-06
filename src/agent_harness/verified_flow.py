"""Evidence-gated managed work items over the existing supervisor and core.

The conservative frontier rechecks the whole integrated candidate after a
change. Narrow dependency reuse requires a separately proven closure model.
Legacy plans deliberately retain their older semantics and cannot claim this
protocol. No model/tool loop is implemented here.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .core import HarnessError, Run, digest, snapshot, matches
from .file_io import read_regular_file
from .runner import run_check, gate
from .work_contract import legacy_contract


def enabled(supervisor):
    return supervisor.contract["schema_version"] == 2 and supervisor.contract["mode"] == "managed"


def mission_digest(supervisor):
    return digest(supervisor.contract["mission"])


def check_errors(supervisor, record):
    """Check immutable raw core evidence; a YAML verdict alone proves nothing."""
    value = record["payload"]
    try:
        path = Path(value["verification_run"])
        if not path.is_relative_to(supervisor.path / "nodes"):
            return ["node verification escaped owned run"]
        run = Run(path)
        if digest(run.contract()) != value["verification_definition_digest"]:
            return ["node verification definition changed"]
        state = run.state()
        if digest(state["accepted"]) != value["verification_digest"]:
            return ["node acceptance receipt changed"]
        if value["verdict"] != "approved" or run.candidate()["digest"] != value["candidate_digest"]:
            return ["node candidate/verdict binding differs"]
        if run.status(verify_evidence=True).get("evidence_current") is not True:
            return ["node raw evidence stale or unavailable"]
        return []
    except (OSError, ValueError, TypeError, KeyError) as error:
        return ["node evidence unavailable: " + type(error).__name__]


def verify_node(supervisor, node, agent, completion, *, phase, cwd, dependencies):
    checks = {row["id"]: row for row in supervisor.contract["node_checks"]}
    selected = [deepcopy(checks[name]) for name in node["checks"][phase]]
    contract = deepcopy(supervisor.contract)
    contract["acceptance"] = selected
    value = legacy_contract(contract, cwd=cwd, source_cwd=supervisor.state["source_cwd"],
        run_id="node-" + digest([supervisor.state["run_id"], node["id"], phase])[:24],
        attempt=1, deadline=min(supervisor.state["deadline"], agent["deadline"]))
    value["task_id"] = node["id"]
    # Check commands may not mutate the frozen source, even for writable nodes.
    value["permissions"] = ["read", "test"]
    value["scope"]["write_paths"] = []
    before = snapshot(value)
    missing = [output for output in node["outputs"] if not any(matches(name, [output]) for name in before["files"])]
    if missing:
        raise HarnessError("NODE_OUTPUT_MISSING: " + ", ".join(missing))
    identity = digest([completion["record_id"], phase, before["digest"], digest(value), dependencies])
    for previous in supervisor.receipts().records():
        if previous["record_id"] == "node-check-" + identity[:48]:
            if check_errors(supervisor, previous):
                raise HarnessError("Recorded node check is stale or negative; bounded repair needs a new attempt")
            return previous
    path = supervisor.path / "nodes" / node["id"] / (phase + "-" + identity)
    run = Run(path) if (path / "state.json").exists() else Run.create(path, value)
    if run.contract() != value:
        raise HarnessError("Node verification immutable definition conflict")
    run.freeze()
    for check in selected:
        receipt = run.attempt_dir() / "checks" / check["id"] / "result.json"
        if not receipt.exists():
            run_check(run, check["id"], cancel_file=supervisor.path / "cancel.yaml")
    verdict = gate(run)
    receipt = supervisor.receipts().envelope("node_check", node["id"], agent["attempt_id"], completion["invocation_id"])
    receipt["record_id"] = "node-check-" + identity[:48]
    receipt["payload"] = {"completion_ref": completion["record_id"], "candidate_digest": before["digest"],
        "phase": phase, "verification_run": str(path), "verification_digest": digest(run.state()["accepted"]),
        "verification_definition_digest": digest(value), "dependency_vector": list(dependencies),
        "requirement_ids": node["requirement_ids"], "verdict": "approved" if verdict["verdict"] == "accepted" else "changes_requested"}
    published = supervisor.receipts().publish(receipt)
    if verdict["verdict"] != "accepted":
        raise HarnessError("NODE_CHECK_FAILED: " + node["id"] + "/" + phase + ": " + str(verdict["errors"])[:2048])
    return published


def reviewer_verdict(supervisor, node, completion):
    if node["role"] != "reviewer":
        return
    from .strict_yaml import loads
    logs = completion["payload"]["logs"]
    raw = read_regular_file(logs["stdout"])
    if hashlib.sha256(raw).hexdigest() != logs["stdout_digest"]:
        raise HarnessError("Node reviewer artifact changed")
    messages = [e["item"]["text"] for e in (json.loads(line) for line in raw.splitlines() if line)
        if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"]
    try:
        verdict = loads(messages[-1], "managed-review.schema.json") if messages else None
    except ValueError as error:
        raise HarnessError("NODE_REVIEW_INVALID: structured verdict unavailable") from error
    if verdict is None or verdict["verdict"] != "approved" or verdict["findings"]:
        raise HarnessError("NODE_REVIEW_NEGATIVE: terminal completion is not a positive review")


def refresh_frontier(supervisor, plan, agents, *, names=None, verify_only=False):
    """Topological proof on the actual integrated source, with exact raw refs."""
    records = supervisor.receipts().records()
    by_id = {r["record_id"]: r for r in records}
    bound = [r for r in records if r["plan_digest"] == digest(plan)]
    integrations = {r["node_id"]: r for r in bound if r["record_type"] == "integration"}
    accepted, visiting = {}, set()
    nodes = {n["id"]: n for n in plan["tasks"]}

    def visit(name):
        if name in accepted:
            return accepted[name]
        if name in visiting or name not in integrations:
            raise HarnessError("VERIFIED_FRONTIER_INCOMPLETE: " + name)
        visiting.add(name)
        node, agent, integrated = nodes[name], agents[name], integrations[name]
        deps = [visit(dep)["record_id"] for dep in node["depends_on"]]
        completion = by_id[integrated["payload"]["completion_ref"]]
        local = [r for r in bound if r["record_type"] == "node_check" and r["payload"]["phase"] == "local"
            and r["payload"]["completion_ref"] == completion["record_id"] and r["payload"]["verdict"] == "approved"]
        if not local or check_errors(supervisor, local[-1]):
            raise HarnessError("Node local verification missing/stale: " + name)
        check = None
        for previous in reversed(bound):
            if previous["record_type"] != "node_check" or previous["node_id"] != name:
                continue
            payload = previous["payload"]
            if payload["phase"] == "integration" and payload["completion_ref"] == completion["record_id"] and payload["dependency_vector"] == deps and not check_errors(supervisor, previous):
                check = previous
                break
        if check is None:
            if verify_only:
                raise HarnessError("Integrated verification stale/unavailable: " + name)
            check = verify_node(supervisor, node, agent, completion, phase="integration", cwd=supervisor.state["cwd"], dependencies=deps)
        identity = digest([mission_digest(supervisor), integrated["record_id"], local[-1]["record_id"], check["record_id"], deps])
        receipt = supervisor.receipts().envelope("node_acceptance", name, agent["attempt_id"], completion["invocation_id"])
        receipt["record_id"] = "node-accepted-" + identity[:48]
        receipt["payload"] = {"completion_ref": completion["record_id"], "local_ref": local[-1]["record_id"],
            "integrated_ref": check["record_id"], "integration_ref": integrated["record_id"], "mission_digest": mission_digest(supervisor),
            "candidate_digest": check["payload"]["candidate_digest"], "dependency_vector": deps, "requirement_ids": node["requirement_ids"]}
        if verify_only:
            previous = by_id.get(receipt["record_id"])
            if previous is None or previous["payload"] != receipt["payload"]:
                raise HarnessError("Current integrated node acceptance missing: " + name)
            receipt = previous
        else:
            # Idempotent retry does not republish mutable envelope timestamps.
            receipt = by_id.get(receipt["record_id"]) or supervisor.receipts().publish(receipt)
        accepted[name] = receipt
        visiting.remove(name)
        return receipt

    for name in (names if names is not None else integrations):
        visit(name)
    return accepted


def coverage(supervisor, plan, frontier):
    rows = []
    for requirement in supervisor.contract["mission"]["requirements"]:
        names = [n["id"] for n in plan["tasks"] if requirement["id"] in n["requirement_ids"]]
        refs = [frontier[name]["record_id"] for name in names if name in frontier]
        current = bool(names) and len(refs) == len(names)
        rows.append({"id": requirement["id"], "status": "accepted" if current else "pending", "evidence_ids": refs})
    return rows
