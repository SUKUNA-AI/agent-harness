"""Bounded dependency closure and ordered cooperative gate commit locks.

Every core writer already serializes .state.lock.guard. Workspace guards also
cover cooperating source writers. These locks do not control arbitrary external
processes; dependencies must be materialized core runs with immutable contracts.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import time

from .core import HarnessError, Run, _os_lock, digest
from .supervisor_store import _parents, _ordinary

MAX_DEPTH = 32
MAX_NODES = 256


class DependencyChanged(HarnessError):
    """Only a changed mutable closure/generation permits a bounded retry."""


def _identity(path):
    path = Path(path).resolve(strict=True)
    _parents(path)
    info = _ordinary(path, directory=True)
    return (info.st_dev, info.st_ino, os.path.normcase(str(path)))


def closure(run):
    """Pin complete mutable dependency generations before taking any locks."""
    nodes, visiting = {}, set()
    def visit(current, depth):
        if depth > MAX_DEPTH:
            raise HarnessError("unverifiable_dependency: depth limit")
        identity = _identity(current.path)
        physical = identity[:2]
        if physical in visiting:
            raise HarnessError("unverifiable_dependency: dependency cycle")
        if physical in nodes:
            return
        if len(nodes) >= MAX_NODES:
            raise HarnessError("unverifiable_dependency: node limit")
        state = current.state()
        contract = current.contract(state)
        visiting.add(physical)
        nodes[physical] = {"run": current, "identity": identity, "state": state,
            "state_digest": digest(state), "contract": contract}
        for dependency in contract.get("dependencies", []):
            if not isinstance(dependency.get("run_dir"), str) or not dependency.get("verdict_digest"):
                raise HarnessError("unverifiable_dependency: immutable core input pin required")
            visit(Run(dependency["run_dir"]), depth + 1)
        visiting.remove(physical)
    try:
        visit(run, 0)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HarnessError("unverifiable_dependency: " + str(error)) from error
    return nodes


def vector(nodes, *, exclude=None):
    excluded = _identity(exclude.path)[:2] if exclude is not None else None
    return [{"store_ref": str(item["run"].path), "run_id": item["state"]["run_id"],
        "generation": item["state_digest"], "contract_digest": item["state"]["contract_digest"],
        "accepted_verdict_digest": (item["state"].get("accepted") or {}).get("verdict_digest")}
        for key, item in sorted(nodes.items(), key=lambda pair: pair[1]["identity"]) if key != excluded]


@contextmanager
def commit_guard(run, observed, *, timeout=30, allow_root_change=False):
    """Lock all closure state/workspaces in one physical order, then recheck.

    A changed closure is refused; caller may prepare a new bounded attempt within
    its original deadline. No own lock is held while acquiring another closure.
    """
    until = time.monotonic() + max(0, timeout)
    guards = {}
    for item in observed.values():
        current = item["run"]
        state_guard = current.path / ".state.lock.guard"
        workspace, _ = current._workspace_lease(item["contract"])
        workspace.parent.mkdir(parents=True, exist_ok=True)
        for path in (state_guard, workspace.with_name(workspace.name + ".guard")):
            _parents(path)
            _ordinary(path, missing_ok=True)
            parent_identity = _identity(path.parent)
            guards[(parent_identity[:2], path.name)] = path
    ordered = sorted(guards.values(), key=lambda p: (*_identity(p.parent), p.name))
    with ExitStack() as stack:
        for path in ordered:
            while True:
                try:
                    stack.enter_context(_os_lock(path))
                    break
                except HarnessError as error:
                    if "busy" not in str(error).lower() or time.monotonic() >= until:
                        raise
                    time.sleep(min(0.05, max(0, until - time.monotonic())))
        actual = closure(run)
        original_bindings = {str(item["run"].path): item["identity"] for item in observed.values()}
        for item in actual.values():
            reference = str(item["run"].path)
            if reference in original_bindings and item["identity"] != original_bindings[reference]:
                raise HarnessError("unverifiable_dependency: physical store identity changed")
        if (vector(actual, exclude=run) != vector(observed, exclude=run)
                or not allow_root_change and vector(actual) != vector(observed)):
            raise DependencyChanged("DEPENDENCY_GENERATION_CHANGED: prepare acceptance again against current closure")
        for item in actual.values():
            current = item["run"]
            if _identity(current.path) != item["identity"]:
                raise HarnessError("unverifiable_dependency: physical store identity changed")
            current.assert_current(item["state"], item["contract"])
        if time.monotonic() >= until:
            raise HarnessError("GATE_DEADLINE_EXHAUSTED: dependency commit deadline reached before publication")
        yield actual


def current_vector_errors(expected):
    """Read-only historical receipt validation; later upstream changes are stale."""
    for item in expected:
        try:
            state = Run(item["store_ref"]).state()
            if state["run_id"] != item["run_id"] or digest(state) != item["generation"]:
                return ["accepted dependency generation changed"]
        except (HarnessError, OSError, ValueError, KeyError, TypeError):
            return ["accepted dependency generation unavailable"]
    return []
