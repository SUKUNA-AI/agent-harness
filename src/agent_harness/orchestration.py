"""Read-only planning helpers. The native client remains the sole agent runtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fnmatch
import json
from pathlib import Path
import re
import sys

from .core import HarnessError, Run, digest, file_hash, read_json, snapshot, validate_contract
from .runner import accepted_evidence_errors

SHA = re.compile(r"^[0-9a-f]{64}$")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
STATES = {"planned", "running", "verifying", "ready_for_review", "accepted", "integrated", "blocked", "failed", "cancelled"}
NATIVE_STATES = {"open", "running", "idle", "done", "stopped", "unavailable", "closed"}


def _require(condition, message):
    if not condition:
        raise HarnessError(message)


def _sha(value):
    return isinstance(value, str) and bool(SHA.fullmatch(value))


def _path(value):
    _require(isinstance(value, str) and bool(value), "Write claim must be a relative path")
    value = value.replace("\\", "/").rstrip("/")
    _require(not value.startswith("/") and ":" not in value and all(p.casefold() not in {"", ".", "..", ".git"} for p in value.split("/")), "Unsafe relative write claim")
    _require("?" not in value and "[" not in value and ("*" not in value or value == "**" or (value.endswith("/**") and "*" not in value[:-3])), "Claims support exact paths or directory/** only")
    return value


def _prefix(value):
    return "" if value == "**" else value[:-3] if value.endswith("/**") else value


def _overlap(left, right):
    left, right = _prefix(left).casefold(), _prefix(right).casefold()
    return not left or not right or left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _within(claim, allowed):
    claim = claim.casefold()
    allowed = allowed.replace("\\", "/").casefold().rstrip("/")
    if allowed == "**":
        return True
    if allowed.endswith("/**"):
        parent = allowed[:-3]
        return _prefix(claim) == parent or _prefix(claim).startswith(parent + "/")
    return "*" not in claim and fnmatch.fnmatchcase(claim, allowed)


def _cwd(value):
    _require(isinstance(value, str) and Path(value).is_absolute() and Path(value).is_dir(), "Node cwd must be an existing absolute directory")
    return Path(value).resolve().as_posix().rstrip("/").casefold()


def _checkout(value):
    """Find physical Git checkout without executing Git; nested cwd shares its writer."""
    path = Path(value).resolve()
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return path


def _checkout_overlap(left, right):
    return _overlap(_cwd(left), _cwd(right)) or _overlap(_cwd(str(_checkout(left))), _cwd(str(_checkout(right))))


def _absolute_claim(node, claim):
    return (Path(node["cwd"]) / _prefix(_path(claim))).resolve().as_posix().rstrip("/")


def _logical_claim(node, claim, contract=None):
    cwd = Path(node["cwd"]).resolve()
    root = _checkout(node["cwd"])
    if contract and contract.get("cwd"):
        parent = Path(contract["cwd"]).resolve()
        if cwd == parent or parent in cwd.parents:
            root = parent
    prefix = cwd.relative_to(root).as_posix()
    return claim if prefix == "." else prefix + "/" + claim


def _local_ref(ref, roots, contract):
    _require(isinstance(ref, dict) and isinstance(ref.get("path"), str) and Path(ref["path"]).is_absolute(), "Context/reading ref needs an absolute local path")
    path = Path(ref["path"])
    resolved = path.resolve()
    allowed = any(resolved == root or root in resolved.parents for root in roots) or any(Path(item["path"]).resolve() == resolved for item in contract.get("inputs", []))
    _require(allowed and path.is_file(), "Context/reading path is outside authorized workspace/input files")
    _require(_sha(ref.get("sha256")) and file_hash(path) == ref["sha256"], "Context/reading content hash is stale")
    return resolved


def _parent_binding(node, plan, contract):
    return {"node_id": node["id"], "parent_run_id": contract["run_id"], "parent_task_id": contract["task_id"], "parent_attempt_id": contract["attempt_id"], "parent_contract_digest": plan["contract_digest"], "plan_revision": plan["plan_revision"]}


def validate_plan(plan, contract):
    """Validate current inputs and permissions, without writing run or source state."""
    validate_contract(contract)
    _require(isinstance(plan, dict) and plan.get("schema_version") == 1, "Unsupported orchestration schema")
    _require(plan.get("contract_digest") == digest(contract), "Plan contract digest is stale")
    _require(plan.get("input_digest") == snapshot(contract)["digest"], "Plan input digest is stale")
    _require(type(plan.get("plan_revision")) is int and plan["plan_revision"] >= 1, "Positive plan_revision required")
    _require(plan.get("fallback_reason") in {None, "indivisible", "capacity", "native_unavailable", "user_constraint"}, "Unknown fallback reason")
    _require(type(plan.get("target_owners", 2)) is int and 2 <= plan.get("target_owners", 2) <= 3, "target_owners must be two or three")
    nodes = plan.get("nodes")
    _require(isinstance(nodes, list) and bool(nodes), "Plan requires nodes")
    roots = [Path(contract["cwd"]).resolve()] if contract.get("cwd") else []
    roots.extend(Path(n["cwd"]).resolve() for n in nodes if isinstance(n, dict) and isinstance(n.get("cwd"), str) and Path(n["cwd"]).is_absolute())
    context = plan.get("common_context")
    _require(isinstance(context, list) and bool(context), "Plan requires common_context references")
    for ref in context:
        _local_ref(ref, roots, contract)
    by_id = {}
    assigned = set()
    for node in nodes:
        _require(isinstance(node, dict) and isinstance(node.get("id"), str) and ID.fullmatch(node["id"]), "Invalid node id")
        _require(node["id"] not in by_id, "Duplicate node id")
        by_id[node["id"]] = node
        _require(node.get("role") in {"owner", "helper", "reviewer"}, "Invalid node role")
        _require(isinstance(node.get("goal"), str) and node["goal"].strip(), "Node goal required")
        _require(node.get("state", "planned") in STATES, "Invalid node state")
        _cwd(node.get("cwd"))
        reading = node.get("read_sections")
        _require(isinstance(reading, list) and bool(reading), "Node requires read_sections")
        for ref in reading:
            _local_ref(ref, roots, contract)
            sections = ref.get("sections")
            _require(isinstance(sections, list) and bool(sections) and all(isinstance(s, str) and s.strip() for s in sections), "Reading sections must be nonempty strings")
        outputs = node.get("outputs")
        _require(isinstance(outputs, list) and bool(outputs), "Node requires concrete outputs")
        for output in outputs:
            _require(isinstance(output, dict) and isinstance(output.get("path"), str) and Path(output["path"]).is_absolute() and isinstance(output.get("description"), str) and output["description"].strip(), "Output requires absolute path and description")
            target, cwd = Path(output["path"]).resolve(), Path(node["cwd"]).resolve()
            _require(cwd in target.parents, "Output must stay within assigned cwd")
        permissions = node.get("permissions")
        _require(isinstance(permissions, list) and all(p in contract["permissions"] for p in permissions), "Node permissions expand contract")
        claims = node.get("write_paths", [])
        _require(isinstance(claims, list), "write_paths must be a list")
        _require(not claims or "write" in permissions, "Write claims need write permission")
        _require(not claims or (node["role"] != "reviewer" and contract["workflow"] not in {"review", "diagnostics", "plan", "research", "answer"}), "Read-only intent/role cannot write product")
        for claim in claims:
            claim = _path(claim)
            logical = _logical_claim(node, claim, contract)
            _require(any(_within(logical, allowed) for allowed in contract.get("scope", {}).get("write_paths", [])), "Write claim exceeds contract scope")
            _require(not any(_overlap(logical, _path(p)) for p in contract.get("scope", {}).get("protected_paths", [])), "Write claim intersects protected scope")
            # Symlink aliases cannot establish independent ownership reliably.
            target = Path(node["cwd"]) / _prefix(claim)
            _require(not any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (target, *target.parents)), "Symlink/junction write claims must be serialized outside auto plan")
        deps = node.get("depends_on", [])
        _require(isinstance(deps, list) and all(isinstance(d, str) for d in deps) and len(deps) == len(set(deps)), "Invalid dependencies")
        ref = node.get("native_ref")
        if ref is not None:
            _require(isinstance(ref, str) and bool(ref), "Invalid native_ref")
            if node.get("state", "planned") in {"planned", "running", "verifying"}:
                _require(ref not in assigned, "Duplicate active native_ref ownership")
                assigned.add(ref)
        _require(isinstance(node.get("checks", []), list) and all(c in {x["id"] for x in contract["checks"]} for c in node.get("checks", [])), "Unknown check id")
        if "accepted_result" in node:
            expected = node.get("expected_child")
            _require(isinstance(expected, dict) and _sha(expected.get("contract_digest")) and all(isinstance(expected.get(k), str) and expected[k].strip() for k in ("run_id", "task_id", "attempt_id")), "accepted_result requires expected_child identity and contract digest")
    visited, visiting = set(), set()

    def visit(key):
        _require(key in by_id, "Unknown dependency: " + key)
        _require(key not in visiting, "Dependency cycle: " + key)
        if key in visited:
            return
        visiting.add(key)
        for dep in by_id[key].get("depends_on", []):
            visit(dep)
        visiting.remove(key)
        visited.add(key)

    for key in by_id:
        visit(key)
    return by_id


def observe_capacity(observation, contract):
    _require(isinstance(observation, dict) and observation.get("complete") is True and observation.get("source"), "Complete native inventory and source required")
    try:
        observed = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - observed).total_seconds()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HarnessError("Native observation needs timezone-aware observed_at")
    _require(-5 <= age <= 120, "Native inventory is stale; observe again before spawn")
    cap = observation.get("cap")
    budget = contract.get("budget", {})
    _require(type(cap) is int and cap >= 1 and budget.get("root_counts") is True and type(budget.get("native_cap")) is int and cap <= budget["native_cap"] and budget.get("native_cap_source"), "Unknown/inconsistent observed native cap including root")
    threads = observation.get("threads")
    _require(isinstance(threads, list), "Native threads list required")
    refs, writers = {}, []
    for item in threads:
        _require(isinstance(item, dict) and isinstance(item.get("native_ref"), str) and item["native_ref"] and item.get("status") in NATIVE_STATES, "Invalid native thread observation")
        _require(item["native_ref"] not in refs, "Duplicate native_ref in inventory")
        refs[item["native_ref"]] = item
        if item["status"] == "closed":
            _require(item.get("closed_confirmed") is True and item.get("closure_evidence"), "Closed native ref needs backend proof")
        elif item.get("write_cwd"):
            _cwd(item["write_cwd"])
            _require(not any(_checkout_overlap(item["write_cwd"], other) for other in writers), "Native writer checkout conflict")
            writers.append(item["write_cwd"])
    _require(observation.get("root_ref") in refs and refs[observation["root_ref"]]["status"] != "closed", "Inventory must include open root exactly once")
    counted = sum(item["status"] != "closed" for item in refs.values())
    _require(counted <= cap, "Observed native capacity exceeded")
    return {"cap": cap, "counted": counted, "remaining": cap - counted, "refs": refs}


def _accepted(node, plan, contract):
    if node.get("state") not in {"accepted", "integrated"}:
        return False
    record = node.get("accepted_result", {})
    if not isinstance(record, dict) or not record.get("run_dir") or not _sha(record.get("verdict_digest")):
        return False
    run = Run(record["run_dir"])
    state = run.status()
    accepted = state.get("accepted")
    if not state.get("evidence_current") or not accepted or accepted.get("verdict_digest") != record["verdict_digest"]:
        return False
    child = run.contract(state)
    expected = node.get("expected_child", {})
    if not isinstance(expected, dict) or not _sha(expected.get("contract_digest")):
        return False
    if any(child.get(key) != expected.get(key) for key in ("run_id", "task_id", "attempt_id")) or state["contract_digest"] != expected["contract_digest"]:
        return False
    if child.get("lane_id") != node["id"] or child.get("orchestration_binding") != _parent_binding(node, plan, contract):
        return False
    return not accepted_evidence_errors(run, record["verdict_digest"])


def _conflict(left, right):
    if not left.get("write_paths") or not right.get("write_paths"):
        return False
    return _checkout_overlap(left["cwd"], right["cwd"]) or any(_overlap(_absolute_claim(left, a), _absolute_claim(right, b)) or _overlap(_logical_claim(left, _path(a)), _logical_claim(right, _path(b))) for a in left["write_paths"] for b in right["write_paths"])


def next_wave(plan, contract, observation):
    nodes = validate_plan(plan, contract)
    capacity = observe_capacity(observation, contract)
    active = [n for n in nodes.values() if n.get("state") in {"running", "verifying"}]
    for i, left in enumerate(active):
        for right in active[i + 1:]:
            _require(not _conflict(left, right), "Conflicting active ownership")
    for node in active:
        _require(node.get("native_ref") in capacity["refs"] and capacity["refs"][node["native_ref"]]["status"] not in {"closed", "unavailable"}, "Active owner requires native reconciliation")
        _require(node["native_ref"] != observation["root_ref"], "Root cannot be a lane owner ref")
        item = capacity["refs"][node["native_ref"]]
        if node.get("write_paths") and item.get("write_cwd"):
            _require(_cwd(node["cwd"]) == _cwd(item["write_cwd"]), "Native writer checkout differs from assigned owner")
    executing_refs = {ref for ref, item in capacity["refs"].items() if ref != observation["root_ref"] and item["status"] in {"open", "running"}} | {n["native_ref"] for n in active}
    chosen, blocked = [], {}
    remaining = capacity["remaining"]
    for key, node in sorted(nodes.items()):
        if node.get("state", "planned") != "planned":
            continue
        if any(not _accepted(nodes[d], plan, contract) for d in node.get("depends_on", [])):
            blocked[key] = "dependency_not_accepted_current"
            continue
        if any(_conflict(node, other) for other in active + [nodes[x["node_id"]] for x in chosen]):
            blocked[key] = "ownership_conflict"
            continue
        if node.get("write_paths") and any(item.get("write_cwd") and item["status"] != "closed" and ref != node.get("native_ref") and _checkout_overlap(item["write_cwd"], node["cwd"]) for ref, item in capacity["refs"].items()):
            blocked[key] = "ownership_conflict"
            continue
        if len(executing_refs) + len(chosen) >= plan.get("target_owners", 2):
            blocked[key] = "target_owners"
            continue
        ref = node.get("native_ref")
        if ref:
            item = capacity["refs"].get(ref)
            _require(item and ref != observation["root_ref"] and item["status"] in {"idle", "done", "stopped"}, "Assigned ref is unavailable for follow-up")
            mode = "followup"
        else:
            if remaining == 0:
                blocked[key] = "capacity"
                continue
            mode = "spawn"
            remaining -= 1
        chosen.append({"node_id": key, "mode": mode, "native_ref": ref})
    _require(snapshot(contract)["digest"] == plan["input_digest"], "Inputs changed during planning")
    return {"plan_revision": plan["plan_revision"], "input_digest": plan["input_digest"], "selected": chosen, "blocked": blocked, "capacity": {k: capacity[k] for k in ("cap", "counted", "remaining")}, "fallback_reason": plan.get("fallback_reason") or ("capacity" if "capacity" in blocked.values() else "indivisible" if len(nodes) == 1 else None), "limitation": "Read-only recommendation; root must reobserve and serialize every native spawn/follow-up. This is not native resume or an acceptance gate."}


def brief(plan, contract, node_id):
    nodes = validate_plan(plan, contract)
    _require(node_id in nodes, "Unknown node")
    node = nodes[node_id]
    checks = [c for c in contract["checks"] if c["id"] in node.get("checks", [])]
    return "\n".join([f"Role: {node['role']}; node: {node_id}; plan revision: {plan['plan_revision']}", f"Goal: {node['goal']}", f"Contract: {plan['contract_digest']}; current inputs: {plan['input_digest']}", f"cwd: {node['cwd']}", "Base revision: " + str(node.get("base_revision", contract.get("base_revision", "unknown; root must observe before Git writes"))), "Child contract orchestration_binding: " + json.dumps(_parent_binding(node, plan, contract), sort_keys=True), "Expected child: " + json.dumps(node.get("expected_child"), sort_keys=True), "Common context: " + json.dumps(plan["common_context"], ensure_ascii=False), "Read sections: " + json.dumps(node["read_sections"], ensure_ascii=False), "Concrete outputs: " + json.dumps(node["outputs"], ensure_ascii=False), "Permissions: " + json.dumps(node["permissions"]), "Write scope: " + json.dumps(node.get("write_paths", [])), "Protected scope: " + json.dumps(contract.get("scope", {}).get("protected_paths", [])), "Dependencies: " + json.dumps(node.get("depends_on", [])), "Acceptance: " + json.dumps(contract.get("acceptance_criteria", []), ensure_ascii=False), "Required checks: " + json.dumps(checks, ensure_ascii=False), "You are not alone. Preserve unrelated work; one writer per checkout. Do not spawn helpers; request one from root. Review is read-only. Return ready_for_review with actual evidence and limitations; only root accepts. No external action without current task authorization."])


def repair_brief(plan, contract, node_id, packet):
    nodes = validate_plan(plan, contract)
    _require(node_id in nodes, "Unknown repair node")
    node = nodes[node_id]
    for key, expected in (("node_id", node_id), ("plan_revision", plan["plan_revision"]), ("native_ref", node.get("native_ref")), ("contract_digest", plan["contract_digest"]), ("candidate_digest", plan["input_digest"]), ("attempt_id", contract["attempt_id"])):
        _require(expected is not None and packet.get(key) == expected, "Repair wrong owner/current input: " + key)
    _require(packet.get("check_id") in {c["id"] for c in contract["checks"]}, "Unknown failing check")
    _require(packet.get("status") in {"failed", "timeout", "unknown"} and isinstance(packet.get("log"), str) and packet["log"].strip() and isinstance(packet.get("expected"), str) and packet["expected"].strip(), "Repair requires failing check, log and expected behavior")
    return brief(plan, contract, node_id) + "\nRepair: " + json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\nRoot must safely update this isolated checkout to the current integration candidate before repair; a message does not update files. Preserve dirty work. Rerun affected and mandatory integration checks. Repeated failure does not reset attempts/deadline."


def dispatch(argv):
    parser = argparse.ArgumentParser(description="Read-only native orchestration planning; never spawns agents")
    parser.add_argument("action", choices=("validate", "brief", "next", "repair-brief"))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--native-snapshot")
    parser.add_argument("--node")
    parser.add_argument("--repair")
    args = parser.parse_args(argv)
    try:
        plan, contract = read_json(args.plan), read_json(args.contract)
        if args.action == "validate":
            result = {"valid": True, "nodes": sorted(validate_plan(plan, contract))}
        elif args.action == "next":
            _require(args.native_snapshot, "next needs --native-snapshot")
            result = next_wave(plan, contract, read_json(args.native_snapshot))
        elif args.action == "brief":
            result = {"brief": brief(plan, contract, args.node)}
        else:
            _require(args.repair, "repair-brief needs --repair")
            result = {"brief": repair_brief(plan, contract, args.node, read_json(args.repair))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (HarnessError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}), file=sys.stderr)
        return 2
