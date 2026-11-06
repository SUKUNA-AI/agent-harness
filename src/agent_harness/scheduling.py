"""Validated task DAG and bounded critical-path scheduling; no model runtime."""
from __future__ import annotations

from .core import HarnessError, IDENTIFIER, digest
from .orchestration import _path, _overlap, _within


def validate_plan(plan, contract):
    from .strict_yaml import validate
    validate(plan, "agent-plan.schema.json")
    nodes = plan["tasks"]
    by_id = {node["id"]: node for node in nodes}
    if len(by_id) != len(nodes):
        raise HarnessError("Duplicate plan task IDs")
    criteria = {item["id"] for item in contract["acceptance"]}
    scopes = contract["task"]["allowed_write_scopes"]
    budget = contract["budgets"]
    verified = contract["schema_version"] == 2
    if plan["schema_version"] != contract["schema_version"]:
        raise HarnessError("Plan/mission version barrier: legacy integration is not verified task flow")
    if verified:
        if plan["mission_digest"] != digest(contract["mission"]):
            raise HarnessError("Plan belongs to a different mission generation")
        requirements = {r["id"] for r in contract["mission"]["requirements"]}
        mapped = {r for node in nodes for r in node["requirement_ids"]}
        mandatory = {r["id"] for r in contract["mission"]["requirements"] if r["required"]}
        if not mapped <= requirements or not mandatory <= mapped:
            raise HarnessError("MISSION_COVERAGE_INCOMPLETE: mandatory requirement missing from task mapping")
        node_checks = {c["id"] for c in contract["node_checks"]}
    for node in nodes:
        if not IDENTIFIER.fullmatch(node["id"]):
            raise HarnessError("Invalid task ID")
        if verified and any(not set(node["checks"][phase]) <= node_checks for phase in ("local", "integration")):
            raise HarnessError("Unknown frozen node check definition")
        if node["id"] in node["depends_on"] or not set(node["depends_on"]) <= by_id.keys():
            raise HarnessError("Unknown or self dependency")
        if not set(node["acceptance_ids"]) <= criteria or not node["acceptance_ids"]:
            raise HarnessError("Unknown or empty acceptance IDs")
        for scope in node["read_scope"] + node["write_scope"]:
            _path(scope)
        if any(not any(_within(scope, allowed) for allowed in scopes) for scope in node["write_scope"]):
            raise HarnessError("Task write scope exceeds authorized contract")
        if node["role"] == "reviewer" and not node["read_only"]:
            raise HarnessError("Reviewer must be read-only")
        if node["read_only"] and node["write_scope"]:
            raise HarnessError("Read-only task cannot claim write scopes")
        if not node["read_only"] and (contract["task"]["read_only"] or not node["write_scope"]):
            raise HarnessError("Writable worker needs a nonempty scope authorized by a writable parent")
        for output in node["outputs"]:
            _path(output)
            permitted = node["read_scope"] if node["read_only"] else node["write_scope"]
            if not any(_within(output, scope) for scope in permitted):
                raise HarnessError("Planned output is outside the node's authorized scope")
        if node["max_attempts"] > budget["max_attempts"] or node["max_wall_seconds"] > budget["max_wall_seconds"]:
            raise HarnessError("Plan exceeds original attempt/deadline budget")
    order, visiting, done = [], set(), set()
    def visit(name):
        if name in visiting:
            raise HarnessError("Cyclic task DAG")
        if name in done:
            return
        visiting.add(name)
        for dependency in by_id[name]["depends_on"]:
            visit(dependency)
        visiting.remove(name)
        done.add(name)
        order.append(name)
    for name in by_id:
        visit(name)
    ancestors = {}
    for name in order:
        ancestors[name] = set(by_id[name]["depends_on"])
        for dependency in by_id[name]["depends_on"]:
            ancestors[name].update(ancestors[dependency])
    for i, left in enumerate(nodes):
        for right in nodes[i + 1:]:
            if left["id"] in ancestors[right["id"]] or right["id"] in ancestors[left["id"]]:
                continue
            if any(_overlap(a, b) for a in left["write_scope"] for b in right["write_scope"]):
                raise HarnessError("Concurrent tasks have overlapping write scopes")
    return plan


class Scheduler:
    def __init__(self, plan, contract, *, spawn_cost_seconds=2.0):
        self.plan = validate_plan(plan, contract)
        self.nodes = {node["id"]: node for node in plan["tasks"]}
        self.cap = contract["budgets"]["max_concurrent_managed_agents"]
        self.spawn_cost = spawn_cost_seconds
        self.observed = {}

    def paths(self, completed=()):
        completed = set(completed)
        children = {name: [] for name in self.nodes}
        for name, node in self.nodes.items():
            for parent in node["depends_on"]:
                children[parent].append(name)
        memo = {}
        def remaining(name):
            if name in memo:
                return memo[name]
            duration = 0 if name in completed else self.observed.get(name, self.nodes[name]["estimated_seconds"])
            successors = [remaining(child) for child in children[name]]
            tail = max(successors, key=lambda value: value[0], default=(0, []))
            memo[name] = (duration + tail[0], ([name] if duration else []) + tail[1])
            return memo[name]
        for name in self.nodes:
            remaining(name)
        return memo

    def next(self, completed=(), active=(), blocked=()):
        completed, active, blocked = set(completed), set(active), set(blocked)
        remaining = self.paths(completed)
        ready = [name for name, node in self.nodes.items()
                 if name not in completed | active | blocked and set(node["depends_on"]) <= completed]
        ready.sort(key=lambda name: (-remaining[name][0], -self.nodes[name]["impact"], name))
        free = max(0, self.cap - len(active))
        selected = []
        for name in ready:
            if len(selected) >= free:
                break
            if selected and self.nodes[name]["estimated_seconds"] <= self.spawn_cost:
                continue
            selected.append(name)
        critical = max(remaining.values(), key=lambda value: value[0], default=(0, []))
        return {"ready": ready, "selected": selected, "active": sorted(active), "cap": self.cap,
                "critical_path": critical[1], "estimated_remaining_seconds": critical[0],
                "estimate_kind": "heuristic", "spawn_reason": "independent ready scopes prioritized by remaining critical path",
                "expected_benefit_seconds": max(0.0, sum(self.nodes[n]["estimated_seconds"] for n in selected)
                                                - max((self.nodes[n]["estimated_seconds"] for n in selected), default=0)
                                                - self.spawn_cost * max(0, len(selected) - 1))}

    def observe_duration(self, task_id, seconds):
        if task_id not in self.nodes or not isinstance(seconds, (int, float)) or seconds < 0:
            raise HarnessError("Invalid observed task duration")
        self.observed[task_id] = seconds
