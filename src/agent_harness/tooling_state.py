"""Explicit tooling format upgrade using the existing immutable state journal.

Historical schema bytes and old state values stay unchanged. Only an opted-in
contract/plan activates v4; its predecessor remains in the same checkpoint
chain. This does not change a backend, resume a model or reset any budget.
"""
from copy import deepcopy

from .core import HarnessError, digest
from .strict_yaml import validate


def enabled(contract, plan=None):
    return (contract.get("tooling_schema_version") == 1
            or "execution" in contract or "review_execution" in contract
            or plan is not None and (plan.get("tooling_schema_version") == 1
                or any("execution" in node for node in plan["tasks"])))


def upgraded(state):
    result = deepcopy(state)
    if state["schema_version"] == 4:
        validate(result, "work-state.v4.schema.json")
        return result
    if state["schema_version"] not in (2, 3):
        raise HarnessError("Tooling requires explicit legacy v1 migration before format upgrade")
    if state.get("process_identity"):
        raise HarnessError("Cannot upgrade tooling format with a potentially live process")
    result.update(schema_version=4, base_schema_version=state["schema_version"],
        tooling_schema_version=1, tool_provenance=None, tool_source_checkpoint=None)
    validate(result, "work-state.v4.schema.json")
    return result


def ensure(supervisor, *, plan=None):
    if not enabled(supervisor.contract, plan) or supervisor.state["schema_version"] == 4:
        return
    with supervisor.mutex:
        source = supervisor.state
        target = upgraded(source)
        before = digest(source)
        version = source["schema_version"]
        supervisor.state.update(target)
        supervisor._save("Explicit tooling state format upgrade from " + str(version) + " to 4; source=" + before)
