"""Versioned supervisor contract normalization and compatibility bridge."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from .core import HarnessError, IDENTIFIER, digest, validate_contract
from .orchestration import _path


def normalize(value):
    from .strict_yaml import validate
    value = deepcopy(value)
    if not isinstance(value, dict):
        raise HarnessError("Task contract must be a mapping")
    value.setdefault("budgets", {})
    for key, default in {"max_attempts": 3, "max_concurrent_managed_agents": 1,
                         "max_wall_seconds": 7200, "turn_timeout_seconds": 1800}.items():
        value["budgets"].setdefault(key, default)
    value.setdefault("model", {"inherit_installed_codex_config": True})
    value.setdefault("mode", "native")
    value.setdefault("memory", False)
    value.setdefault("ledger", "inform")
    value.setdefault("inputs", [])
    if isinstance(value.get("task"), dict):
        value["task"].setdefault("read_only", False)
        value["task"].setdefault("allowed_write_scopes", [])
    if isinstance(value.get("acceptance"), list):
        for check in value["acceptance"]:
            if isinstance(check, dict) and check.get("kind") == "command":
                check.setdefault("timeout", 300)
    validate(value, "work-contract.schema.json")
    if not IDENTIFIER.fullmatch(value["task"]["id"]):
        raise HarnessError("Invalid task ID")
    paths = value["task"]["allowed_write_scopes"]
    if not paths and not value["task"]["read_only"]:
        raise HarnessError("Writable task requires explicit allowed_write_scopes")
    if paths and value["task"]["read_only"]:
        raise HarnessError("Read-only task cannot authorize write scopes")
    for path in paths:
        _path(path)
        if any(part.casefold() in {".git", ".codex", ".agents"} for part in path.split("/")):
            raise HarnessError("Harness/Codex settings and Git internals are outside task write scope")
    checks = value["acceptance"]
    for item in value["inputs"]:
        from .supervisor_store import _parents, _ordinary
        input_path = Path(item["path"])
        if not input_path.is_absolute():
            raise HarnessError("Declared input path must be absolute")
        _parents(input_path)
        _ordinary(input_path)
    if len({check["id"] for check in checks}) != len(checks):
        raise HarnessError("Duplicate acceptance IDs")
    if not any(check["required"] for check in checks):
        raise HarnessError("At least one applicable required acceptance criterion is necessary")
    if value["mode"] == "native" and value["budgets"]["max_concurrent_managed_agents"] != 1:
        raise HarnessError("Native child cap and external managed concurrency are different; native mode uses one coordinator")
    for check in checks:
        if not IDENTIFIER.fullmatch(check["id"]):
            raise HarnessError("Invalid acceptance ID")
        if check["kind"] == "command":
            if not check.get("argv") or any("\x00" in item for item in check["argv"]):
                raise HarnessError("Command check requires safe argv")
        elif not check.get("criteria"):
            raise HarnessError("Non-command check requires explicit criteria")
    return value


def load_contract(path):
    from .strict_yaml import load
    return normalize(load(path))


def deadline_for(contract, started_at):
    start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    return (start + timedelta(seconds=contract["budgets"]["max_wall_seconds"])).astimezone(timezone.utc).isoformat()


def legacy_contract(contract, *, cwd, run_id, attempt, deadline, source_cwd=None):
    checks = deepcopy(contract["acceptance"])
    effective = Path(cwd).resolve()
    original = Path(source_cwd or cwd).resolve()
    # New cache policy is implemented outside the old runner. Final checks always run fresh.
    for check in checks:
        check.pop("cache_policy", None)
        check.pop("input_closure", None)
        if check.get("cwd"):
            explicit = Path(check["cwd"])
            if not explicit.is_absolute() or not explicit.resolve().is_relative_to(original):
                raise HarnessError("Acceptance cwd must be inside the authorized source checkout")
            check["cwd"] = str(effective / explicit.resolve().relative_to(original))
        if effective != original:
            for index, argument in enumerate(check.get("argv", [])):
                argument_path = Path(argument)
                if argument_path.is_absolute() and argument_path.resolve().is_relative_to(original):
                    check["argv"][index] = str(effective / argument_path.resolve().relative_to(original))
                elif str(original) in argument or original.as_posix() in argument:
                    raise HarnessError("Embedded original checkout path in acceptance argv requires explicit staging-safe configuration")
            for report in check.get("reports", []):
                report_path = Path(report["path"])
                if report_path.is_absolute() and report_path.resolve().is_relative_to(original):
                    report["path"] = str(effective / report_path.resolve().relative_to(original))
    value = {"schema_version": 1, "run_id": run_id, "task_id": contract["task"]["id"],
             "attempt_id": "attempt-" + str(attempt), "workflow": "change", "process_level": "standard",
             "goal": contract["task"]["objective"], "persistence": True, "cwd": str(Path(cwd).resolve()),
             "permissions": ["read", "test"] + ([] if contract["task"]["read_only"] else ["write"]),
             "scope": {"write_paths": contract["task"]["allowed_write_scopes"],
                       "protected_paths": [".codex/**", ".agents/**"],
                       "exclude": [".git/**", "**/__pycache__/**", "__pycache__/**"]},
             "budget": {"max_attempts": contract["budgets"]["max_attempts"], "deadline": deadline},
             "checks": checks, "inputs": deepcopy(contract.get("inputs", []))}
    validate_contract(value)
    return value


def source_revision(cwd):
    import subprocess
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
                            text=True, timeout=10, shell=False)
    return result.stdout.strip() if result.returncode == 0 else "non-git"
