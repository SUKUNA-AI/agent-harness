"""Explicit, bounded work-state upgrades using the existing journal.

Legacy evidence and usage are never promoted. No migration runs at bootstrap.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import time
import uuid

from .core import HarnessError, digest, identity_alive, now
from .execution_ledger import safe_private, object_schema, HASH, SHORT, repository_identity
from .file_io import read_regular_file
from .strict_yaml import loads, validate
from .supervisor_store import SupervisorStore, atomic_write, CHECKPOINT_SCHEMA

MAX_FILES = 512
MAX_BYTES = 32 * 1024 * 1024
TARGET_SCHEMA = object_schema({"path": SHORT, "sha256": HASH, "bytes": {"type": "integer", "minimum": 0}},
                              ("path", "sha256", "bytes"))
TARGETS_SCHEMA = {"type": "array", "maxItems": MAX_FILES, "items": TARGET_SCHEMA}
PLAN_SCHEMA = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "work_state_migration_plan"},
    "source_version": {"const": 1}, "destination_version": {"const": 2}, "run_id": SHORT,
    "expected_generation": {"type": "integer", "minimum": 0}, "targets": TARGETS_SCHEMA,
    "usage_policy": {"const": "legacy_history_unverified"}, "downgrade_policy": {"const": "baseline_writer_refuses_v2"}},
    ("schema_version", "record_type", "source_version", "destination_version", "run_id", "expected_generation",
     "targets", "usage_policy", "downgrade_policy"))
ENVELOPE = {"record_id": SHORT, "project_id": HASH, "run_id": SHORT, "task_id": SHORT,
    "node_id": {"type": "null"}, "attempt_id": {"type": "null"}, "invocation_id": {"type": "null"},
    "producer_id": {"const": "explicit-migration"}, "producer_kind": {"const": "migration"},
    "generation": {"type": "integer", "minimum": 0}, "created_at": {"type": "string", "format": "utc"},
    "harness_version": SHORT, "harness_build_digest": HASH, "client_version": {"const": "unknown"},
    "capability_receipt_ref": {"type": "null"}, "input_refs": TARGETS_SCHEMA, "payload_digest": HASH}
INTENT_SCHEMA = object_schema({**ENVELOPE,
    "schema_version": {"const": 1}, "record_type": {"const": "work_state_migration_intent"},
    "operation_id": SHORT, "plan_digest": HASH, "plan": PLAN_SCHEMA, "status": {"const": "prepared"},
    "source_state_digest": HASH, "pending_state_digest": HASH,
    "final_state": {"type": "object", "additionalProperties": True}},
    (*ENVELOPE, "schema_version", "record_type", "operation_id", "plan_digest", "plan", "status",
     "source_state_digest", "pending_state_digest", "final_state"))
RECEIPT_SCHEMA = object_schema({**ENVELOPE,
    "schema_version": {"const": 1}, "record_type": {"const": "work_state_migration_receipt"},
    "operation_id": SHORT, "plan_digest": HASH, "status": {"const": "committed"},
    "source_state_digest": HASH, "destination_state_digest": HASH,
    "journal_hash": HASH, "backup_manifest": TARGETS_SCHEMA, "legacy_history_unverified": {"const": True}},
    (*ENVELOPE, "schema_version", "record_type", "operation_id", "plan_digest", "status",
     "source_state_digest", "destination_state_digest", "journal_hash", "backup_manifest", "legacy_history_unverified"))


def _identify(record, state, targets):
    from . import __version__
    from .execution_receipts import build_digest
    result = {**record, "record_id": record["operation_id"] + "-" + record["record_type"],
        "project_id": repository_identity(state.get("source_cwd", state["cwd"])),
        "run_id": state["run_id"], "task_id": state["task_id"],
        "node_id": None, "attempt_id": None, "invocation_id": None,
        "producer_id": "explicit-migration", "producer_kind": "migration", "generation": state["generation"],
        "created_at": record.get("created_at", now()), "harness_version": __version__,
        "harness_build_digest": build_digest(), "client_version": "unknown", "capability_receipt_ref": None,
        "input_refs": targets}
    result["payload_digest"] = digest(result)
    return result


def _verify_record(record):
    payload = dict(record)
    recorded = payload.pop("payload_digest")
    if digest(payload) != recorded:
        raise HarnessError("MIGRATION_RECEIPT_INTEGRITY_CONFLICT")


def _inputs(store, expected_generation):
    started = time.monotonic()
    state = store.read("state.yaml", "work-state.schema.json")
    if state["generation"] != expected_generation:
        raise HarnessError("MIGRATION_GENERATION_CONFLICT")
    if state["schema_version"] != 1:
        raise HarnessError("MIGRATION_UNSUPPORTED_VERSION: only explicit work-state v1 to v2")
    if store.path("recovery-required.yaml").exists():
        raise HarnessError("MIGRATION_RECOVERY_REQUIRED")
    if state.get("process_identity") or state.get("process_status") == "running":
        raise HarnessError("MIGRATION_WRITER_UNRECONCILED: reconcile owned execution first")
    lease = store.path("locks/supervisor.yaml")
    if store._lease is None and lease.exists():
        owner = store.read("locks/supervisor.yaml")
        if not owner.get("identity") or identity_alive(owner["identity"]):
            raise HarnessError("MIGRATION_ACTIVE_WRITER")
    events = store.events()
    checkpoints = [event for event in events if event["payload"].get("kind") == "checkpoint"]
    if not checkpoints or checkpoints[-1]["payload"].get("state") != state:
        raise HarnessError("MIGRATION_HEAD_NOT_RECONCILED")
    names = {"state.yaml", "contract.yaml", "journal/events.jsonl"}
    for position, event in enumerate(checkpoints, 1):
        payload = event["payload"]
        if payload.get("generation") != position or digest(payload.get("state")) != payload.get("state_digest"):
            raise HarnessError("MIGRATION_CORRUPT_CHECKPOINT")
        validate(payload["state"], "work-state.schema.json")
        name = f"checkpoints/{position:06d}.yaml"
        checkpoint = store.read(name, CHECKPOINT_SCHEMA)
        if checkpoint["state"] != payload["state"] or checkpoint["journal_hash"] != event["hash"]:
            raise HarnessError("MIGRATION_CORRUPT_CHECKPOINT")
        names.add(name)
    contract = store.read("contract.yaml", "work-contract.schema.json")
    if digest(contract) != state["contract_digest"]:
        raise HarnessError("MIGRATION_CONTRACT_BINDING_MISMATCH")
    for agent_id in state.get("agents", []):
        name = "agents/" + agent_id + ".yaml"
        agent = store.read(name, "agent-state.schema.json")
        if agent.get("process_identity") or agent["status"] in {"running", "starting", "unknown"}:
            raise HarnessError("MIGRATION_WRITER_UNRECONCILED: agent")
        names.add(name)
    if state.get("plan_path"):
        name = state["plan_path"]
        store.read(name, "agent-plan.schema.json")
        names.add(name)
    inventory, raw, total = [], {}, 0
    if len(names) > MAX_FILES:
        raise HarnessError("MIGRATION_INPUT_LIMIT")
    for name in sorted(names):
        if time.monotonic() - started > 10:
            raise HarnessError("MIGRATION_INPUT_DEADLINE")
        data = read_regular_file(safe_private(store.path(name)), max_bytes=MAX_BYTES)
        total += len(data)
        if total > MAX_BYTES:
            raise HarnessError("MIGRATION_INPUT_LIMIT")
        raw[name] = data
        inventory.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    return state, inventory, raw


def migrate(run_dir, *, expected_generation, apply=False, expected_plan_digest=None, recover=None,
            target_version=None, max_planning_rounds=None):
    if target_version is not None and (type(target_version) is not int or target_version not in (2, 3)):
        raise HarnessError("MIGRATION_UNSUPPORTED_TARGET")
    if target_version == 3:
        return _upgrade_quality(run_dir, expected_generation=expected_generation, apply=apply,
            expected_plan_digest=expected_plan_digest, recover=recover, max_rounds=max_planning_rounds)
    if max_planning_rounds is not None:
        raise HarnessError("MIGRATION_PLAN_QUALITY_TARGET_REQUIRED")
    store = SupervisorStore(run_dir, "work-state.schema.json", create=False)
    if type(expected_generation) is not int or expected_generation < 0:
        raise HarnessError("MIGRATION_INVALID_GENERATION")
    if recover is not None:
        if not re.fullmatch(r"migration-[a-f0-9]{32}", recover):
            raise HarnessError("MIGRATION_INVALID_OPERATION")
        with store.lease("explicit migration recovery"):
            reconciled = store.recover()
            if reconciled.get("recovery_required"):
                raise HarnessError("MIGRATION_JOURNAL_RECOVERY_REQUIRED")
            state = reconciled["state"]
            if state["generation"] != expected_generation:
                raise HarnessError("MIGRATION_GENERATION_CONFLICT")
            prefix = "migrations/" + recover + "/"
            intent = store.read(prefix + "intent.yaml", INTENT_SCHEMA)
            _verify_record(intent)
            if intent["operation_id"] != recover or state.get("migration", {}).get("operation_id") != recover:
                raise HarnessError("MIGRATION_SUBJECT_MISMATCH")
            if state["migration"]["status"] == "committed":
                return _migration_receipt(store, prefix, intent, state)
            if digest(state) != intent["pending_state_digest"]:
                raise HarnessError("MIGRATION_PENDING_STATE_CHANGED")
            for target in intent["plan"]["targets"]:
                data = read_regular_file(store.path(prefix + "backup/" + target["path"]), max_bytes=MAX_BYTES)
                if hashlib.sha256(data).hexdigest() != target["sha256"]:
                    raise HarnessError("MIGRATION_BACKUP_CHANGED")
            final = intent["final_state"]
            validate(final, "work-state.schema.json")
            if final["generation"] != state["generation"] + 1 or final["migration"]["status"] != "committed":
                raise HarnessError("MIGRATION_INVALID_DESTINATION")
            store.commit(final, "complete explicitly recovered migration", owner="migration")
            return _migration_receipt(store, prefix, intent, final)

    def prepare():
        state, inventory, raw = _inputs(store, expected_generation)
        plan = {"schema_version": 1, "record_type": "work_state_migration_plan",
                "source_version": 1, "destination_version": 2, "run_id": state["run_id"],
                "expected_generation": expected_generation, "targets": inventory,
                "usage_policy": "legacy_history_unverified", "downgrade_policy": "baseline_writer_refuses_v2"}
        validate(plan, PLAN_SCHEMA)
        return state, plan, raw

    if not apply:
        _, plan, _ = prepare()
        return {"status": "planned", "plan_digest": digest(plan), "plan": plan, "mutated": False}
    if expected_plan_digest is None:
        raise HarnessError("MIGRATION_PLAN_REQUIRED: pass reviewed dry-run plan digest")
    with store.lease("explicit state migration"):
        state, plan, raw = prepare()
        if digest(plan) != expected_plan_digest:
            raise HarnessError("MIGRATION_PLAN_CHANGED")
        migrated = deepcopy(state)
        migrated["schema_version"] = 2
        migrated["generation"] += 1
        migrated["updated_at"] = now()
        migrated["metrics"]["legacy_history_unverified"] = True
        # A versioned authoritative head is the old-writer barrier. Counts stay
        # legacy even when zero: this operation does not reconstruct invocations.
        operation = "migration-" + uuid.uuid4().hex
        prefix = "migrations/" + operation + "/"
        migrated["migration"] = {"operation_id": operation, "status": "pending", "plan_digest": digest(plan)}
        final = deepcopy(migrated)
        final["generation"] += 1
        final["migration"]["status"] = "committed"
        validate(migrated, "work-state.schema.json")
        validate(final, "work-state.schema.json")
        intent = {"schema_version": 1, "record_type": "work_state_migration_intent",
                  "operation_id": operation, "created_at": now(), "plan_digest": digest(plan),
                  "plan": plan, "status": "prepared", "source_state_digest": digest(state),
                  "pending_state_digest": digest(migrated), "final_state": final}
        intent = _identify(intent, state, plan["targets"])
        for name, data in raw.items():
            atomic_write(store.path(prefix + "backup/" + name), data, exclusive=True)
        store.write(prefix + "intent.yaml", intent, INTENT_SCHEMA, exclusive=True)
        store.commit(migrated, "migration barrier published; explicit recovery required if interrupted",
                     owner="migration", attempt_id=None)
        store.commit(final, "explicit v1 to v2 migration complete; legacy history remains unverified",
                     owner="migration", attempt_id=None)
        return _migration_receipt(store, prefix, intent, final)


def _migration_receipt(store, prefix, intent, state):
    if digest(state) != digest(intent["final_state"]):
        raise HarnessError("MIGRATION_FINAL_STATE_CHANGED")
    checkpoint = [event for event in store.events() if event["payload"].get("kind") == "checkpoint"][-1]
    if checkpoint["payload"]["state"] != state:
        raise HarnessError("MIGRATION_HEAD_NOT_RECONCILED")
    receipt = {"schema_version": 1, "record_type": "work_state_migration_receipt",
               "operation_id": intent["operation_id"], "plan_digest": intent["plan_digest"], "status": "committed",
               "source_state_digest": intent["source_state_digest"], "destination_state_digest": digest(state),
               "generation": checkpoint["payload"]["generation"], "journal_hash": checkpoint["hash"],
               "backup_manifest": intent["plan"]["targets"], "legacy_history_unverified": True}
    receipt = _identify(receipt, state, intent["plan"]["targets"])
    if store.path(prefix + "receipt.yaml").exists():
        existing = store.read(prefix + "receipt.yaml", RECEIPT_SCHEMA)
        _verify_record(existing)
        # Repeated recovery retains original publication time/build observation.
        receipt = {**receipt, "created_at": existing["created_at"],
                   "harness_build_digest": existing["harness_build_digest"]}
        receipt.pop("payload_digest")
        receipt["payload_digest"] = digest(receipt)
        if existing != receipt:
            raise HarnessError("MIGRATION_RECEIPT_CONFLICT")
    else:
        store.write(prefix + "receipt.yaml", receipt, RECEIPT_SCHEMA, exclusive=True)
    return receipt


ARTIFACT_SCHEMA = object_schema({"path": SHORT, "data": {"type": "string"}, "sha256": HASH},
                                ("path", "data", "sha256"))
MATERIAL_SCHEMA = object_schema({"state": {"type": "object", "additionalProperties": True},
    "artifacts": {"type": "array", "maxItems": 16, "items": ARTIFACT_SCHEMA},
    "events": {"type": "array", "maxItems": 16, "items": {"type": "object", "additionalProperties": True}}},
    ("state", "artifacts", "events"))
SNAPSHOT_SCHEMA = object_schema({"root": SHORT, "digest": HASH, "scheme": {"const": 2}},
                                ("root", "digest", "scheme"))
POLICY_REF_SCHEMA = object_schema({"path": SHORT, "sha256": HASH, "digest": HASH,
    "output_schema_ref": object_schema({"path": SHORT, "sha256": HASH}, ("path", "sha256"))},
    ("path", "sha256", "digest", "output_schema_ref"))
PLAN_V2_SCHEMA = object_schema({"schema_version": {"const": 2}, "record_type": {"const": "work_state_migration_plan"},
    "source_version": {"const": 2}, "destination_version": {"const": 3}, "run_id": SHORT, "task_id": SHORT,
    "expected_generation": {"type": "integer", "minimum": 0}, "targets": TARGETS_SCHEMA,
    "contract_path": SHORT, "contract_digest": HASH, "source_state_digest": HASH, "source_journal_hash": HASH,
    "source_snapshots": {"type": "array", "maxItems": 2, "items": SNAPSHOT_SCHEMA},
    "absent_artifacts": {"type": "array", "maxItems": 16, "items": SHORT}, "material_digest": HASH,
    "target_policy_ref": POLICY_REF_SCHEMA,
    "usage_policy": {"const": "preserve_source_trust"}, "assessment": {"const": "unassessed"},
    "downgrade_policy": {"const": "v2_writer_refuses_v3"}},
    ("schema_version", "record_type", "source_version", "destination_version", "run_id", "task_id", "expected_generation",
     "targets", "contract_path", "contract_digest", "source_state_digest", "source_journal_hash", "source_snapshots",
     "absent_artifacts", "material_digest", "target_policy_ref", "usage_policy", "assessment", "downgrade_policy"))
INTENT_V2_SCHEMA = object_schema({**ENVELOPE, "schema_version": {"const": 2},
    "record_type": {"const": "work_state_migration_intent"}, "operation_id": SHORT, "plan_digest": HASH,
    "plan": PLAN_V2_SCHEMA, "status": {"const": "prepared"}, "source_state_digest": HASH,
    "pending_state_digest": HASH, "destination_state_digest": HASH, "material": MATERIAL_SCHEMA,
    "final_state": {"type": "object", "additionalProperties": True}},
    (*ENVELOPE, "schema_version", "record_type", "operation_id", "plan_digest", "plan", "status",
     "source_state_digest", "pending_state_digest", "destination_state_digest", "material", "final_state"))
RECEIPT_V2_SCHEMA = object_schema({**ENVELOPE, "schema_version": {"const": 2},
    "record_type": {"const": "work_state_migration_receipt"}, "operation_id": SHORT, "plan_digest": HASH,
    "status": {"const": "committed"}, "source_state_digest": HASH, "destination_state_digest": HASH,
    "journal_hash": HASH, "backup_manifest": TARGETS_SCHEMA, "material_digest": HASH,
    "usage_policy": {"const": "preserve_source_trust"}, "assessment": {"const": "unassessed"}},
    (*ENVELOPE, "schema_version", "record_type", "operation_id", "plan_digest", "status", "source_state_digest",
     "destination_state_digest", "journal_hash", "backup_manifest", "material_digest", "usage_policy", "assessment"))


def _source_snapshots(state):
    from .core import snapshot
    result = []
    for root in sorted({state["cwd"], state["source_cwd"]}):
        current = snapshot({"cwd": root})
        result.append({"root": root, "digest": current["digest"], "scheme": current["snapshot_scheme_version"]})
    return result


def _quality_accounting(store, state, contract):
    from threading import RLock
    from types import SimpleNamespace
    from .execution_receipts import ExecutionReceipts
    from .agent_lifecycle import GlobalSlots, guard_invocations
    observer = SimpleNamespace(store=store, state=deepcopy(state), contract=contract, mutex=RLock())
    observer.receipts = lambda: ExecutionReceipts(observer)
    accounting = observer.receipts().reconcile_usage()
    guard_invocations(observer, accounting=accounting)
    registry = GlobalSlots(state["state_dir"], create=False)
    if registry.store is None and state["attempts_used"]:
        raise HarnessError("MIGRATION_WRITER_UNRECONCILED: missing registry")
    if any(row["unresolved"] or row["child_alive"] for row in registry.inventory(state["run_id"])):
        raise HarnessError("MIGRATION_WRITER_UNRECONCILED: registry")
    return observer, accounting


def _quality_sources(store, expected_generation):
    """Read-only reconciliation; never repair projections or normalize counters."""
    from datetime import datetime, timezone
    from . import completion_eligibility, plan_quality
    state = store.read("state.yaml", plan_quality.STATE_SCHEMA)
    if state["generation"] != expected_generation:
        raise HarnessError("MIGRATION_GENERATION_CONFLICT")
    if state["schema_version"] != 2:
        raise HarnessError("MIGRATION_UNSUPPORTED_VERSION: explicit plan-quality upgrade requires state v2")
    if (store.path("recovery-required.yaml").exists() or state.get("migration", {}).get("status") == "pending"
            or state["status"] in {"UNKNOWN_OUTCOME", "RECOVERY_REQUIRED", "CANCEL_REQUESTED"}
            or store.path("cancel.yaml").exists()):
        raise HarnessError("MIGRATION_RECOVERY_REQUIRED")
    if datetime.fromisoformat(state["deadline"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise HarnessError("MIGRATION_DEADLINE_EXPIRED")
    if state.get("process_identity") or state.get("process_status") in {"running", "unknown", "starting"}:
        raise HarnessError("MIGRATION_WRITER_UNRECONCILED")
    if store._lease is None and store.path("locks/supervisor.yaml").exists():
        owner = store.read("locks/supervisor.yaml")
        if not owner.get("identity") or identity_alive(owner["identity"]):
            raise HarnessError("MIGRATION_ACTIVE_WRITER")
    events = store.events()
    checkpoints = [event for event in events if event["payload"].get("kind") == "checkpoint"]
    if not checkpoints or checkpoints[-1]["payload"].get("state") != state:
        raise HarnessError("MIGRATION_HEAD_NOT_RECONCILED")
    contract_path = state.get("contract_path", "contract.yaml")
    contract = store.read(contract_path, "work-contract.schema.json")
    if digest(contract) != state["contract_digest"]:
        raise HarnessError("MIGRATION_CONTRACT_BINDING_MISMATCH")
    if contract["schema_version"] != 2 or contract["mode"] != "managed":
        raise HarnessError("MIGRATION_MANAGED_V2_REQUIRED")
    names = {"state.yaml", contract_path, "journal/events.jsonl"}
    for position, event in enumerate(checkpoints, 1):
        value = event["payload"]
        validate(value["state"], plan_quality.STATE_SCHEMA)
        name = f"checkpoints/{position:06d}.yaml"
        expected = {"schema_version": 1, "generation": position, "event_id": event["event_id"], "journal_hash": event["hash"],
            "state_digest": value["state_digest"], "state": value["state"], "reason": value["reason"], "owner": value["owner"],
            "attempt_id": value["attempt_id"], "timestamp": event["timestamp"]}
        if (value.get("generation") != position or digest(value["state"]) != value["state_digest"]
                or store.read(name, CHECKPOINT_SCHEMA) != expected):
            raise HarnessError("MIGRATION_CORRUPT_CHECKPOINT")
        historical = value["state"].get("contract_path", "contract.yaml")
        if digest(store.read(historical, "work-contract.schema.json")) != value["state"]["contract_digest"]:
            raise HarnessError("MIGRATION_CONTRACT_BINDING_MISMATCH")
        names.update((name, historical))
    generation = contract.get("mission", {}).get("generation", 1)
    for identifier in state["agents"]:
        name = ("agents/" if generation == 1 else f"agents/mission-{generation}/") + identifier + ".yaml"
        agent = store.read(name, "agent-state.schema.json")
        if agent.get("process_identity") or agent["status"] in {"spawned", "running", "starting", "unknown", "unknown_outcome"}:
            raise HarnessError("MIGRATION_WRITER_UNRECONCILED: agent")
        names.add(name)
    if state["plan_path"]:
        store.read(state["plan_path"], "agent-plan.schema.json")
        names.add(state["plan_path"])
    observer, accounting = _quality_accounting(store, state, contract)
    for record in accounting["records"]:
        name = "receipts/" + record["record_id"] + ".yaml"
        if store.path(name).exists():
            names.add(name)
    completion_policy = completion_eligibility.policy(observer)
    if completion_policy is not None:
        names.update((completion_eligibility.POLICY_PATH,
            "context/required-" + completion_policy["review_schema_digest"] + ".schema.json"))
        schema_bytes = read_regular_file(store.path("context/required-" + completion_policy["review_schema_digest"] + ".schema.json"))
        if hashlib.sha256(schema_bytes).hexdigest() != completion_policy["review_schema_digest"]:
            raise HarnessError("MIGRATION_REVIEW_SCHEMA_CHANGED")
    return state, contract_path, contract, names, completion_policy, events[-1]["hash"]


def _material_state(source, material):
    from . import plan_quality
    validate(material, MATERIAL_SCHEMA)
    if source["schema_version"] != 2:
        raise HarnessError("MIGRATION_UNSUPPORTED_VERSION")
    expected = deepcopy(source)
    expected.update(schema_version=3, status="RECOVERY_REQUIRED", reason="Explicit upgrade requires current plan-quality assessment",
                    plan_quality=material["state"]["plan_quality"])
    if material["state"] != expected:
        raise HarnessError("MIGRATION_SOURCE_HISTORY_CHANGED")
    validate(material["state"], plan_quality.STATE_SCHEMA)
    quality = material["state"]["plan_quality"]
    if (quality["phase"] != "unassessed" or quality["rounds_used"] != 0 or quality["plan_revision"] != 0
            or any(quality[key] is not None for key in ("active_round", "active_plan_digest", "assessment_ref"))):
        raise HarnessError("MIGRATION_ASSESSMENT_NOT_INITIAL")
    artifacts = {}
    for item in material["artifacts"]:
        raw = item["data"].encode("utf-8")
        if item["path"] in artifacts or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise HarnessError("MIGRATION_ARTIFACT_CONFLICT")
        artifacts[item["path"]] = raw
    ref = quality["policy_ref"]
    output = ref["output_schema_ref"]
    if ref["path"] not in artifacts or output["path"] not in artifacts:
        raise HarnessError("MIGRATION_POLICY_BINDING")
    policy = loads(artifacts[ref["path"]], "plan-quality-policy.v1.schema.json")
    if (hashlib.sha256(artifacts[ref["path"]]).hexdigest() != ref["sha256"] or digest(policy) != ref["digest"]
            or hashlib.sha256(artifacts[output["path"]]).hexdigest() != output["sha256"]
            or policy["output_schema_digest"] != output["sha256"] or policy["max_rounds"] != quality["max_rounds"]):
        raise HarnessError("MIGRATION_POLICY_BINDING")
    pin = {"kind": "plan_quality_policy", "value": policy, "artifact_digest": ref["sha256"], "path": ref["path"]}
    if [event for event in material["events"] if event.get("kind") == "plan_quality_policy"] != [pin]:
        raise HarnessError("MIGRATION_POLICY_BINDING")


def _quality_plan(store, expected_generation, max_rounds):
    from . import plan_quality
    started = time.monotonic()
    state, contract_path, contract, names, completion_policy, head = _quality_sources(store, expected_generation)
    material = plan_quality.prepare_upgrade(state, contract, contract_path=contract_path,
        max_rounds=max_rounds, completion_policy=completion_policy)
    _material_state(state, material)
    absent, seen = [], set()
    for artifact in material["artifacts"]:
        name, data = artifact["path"], artifact["data"].encode("utf-8")
        if name in seen or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise HarnessError("MIGRATION_ARTIFACT_CONFLICT")
        seen.add(name)
        if store.path(name).exists():
            if read_regular_file(store.path(name)) != data:
                raise HarnessError("MIGRATION_ARTIFACT_CONFLICT")
            names.add(name)
        else:
            absent.append(name)
    if len(names) > MAX_FILES:
        raise HarnessError("MIGRATION_INPUT_LIMIT")
    inventory, raw, total = [], {}, 0
    for name in sorted(names):
        if time.monotonic() - started > 10:
            raise HarnessError("MIGRATION_INPUT_DEADLINE")
        data = read_regular_file(safe_private(store.path(name)), max_bytes=MAX_BYTES)
        total += len(data)
        if total > MAX_BYTES:
            raise HarnessError("MIGRATION_INPUT_LIMIT")
        inventory.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        raw[name] = data
    plan = {"schema_version": 2, "record_type": "work_state_migration_plan", "source_version": 2,
        "destination_version": 3, "run_id": state["run_id"], "task_id": state["task_id"], "expected_generation": expected_generation,
        "targets": inventory, "contract_path": contract_path, "contract_digest": state["contract_digest"],
        "source_state_digest": digest(state), "source_journal_hash": head, "source_snapshots": _source_snapshots(state),
        "absent_artifacts": sorted(absent), "material_digest": digest(material),
        "target_policy_ref": material["state"]["plan_quality"]["policy_ref"], "usage_policy": "preserve_source_trust",
        "assessment": "unassessed", "downgrade_policy": "v2_writer_refuses_v3"}
    validate(plan, PLAN_V2_SCHEMA)
    return state, plan, raw, material


def _quality_receipt(store, prefix, intent, state):
    if state != intent["final_state"]:
        raise HarnessError("MIGRATION_FINAL_STATE_CHANGED")
    checkpoints = [event for event in store.events() if event["payload"].get("kind") == "checkpoint"]
    if not checkpoints or checkpoints[-1]["payload"]["state"] != state:
        raise HarnessError("MIGRATION_HEAD_NOT_RECONCILED")
    receipt = _identify({"schema_version": 2, "record_type": "work_state_migration_receipt",
        "operation_id": intent["operation_id"], "plan_digest": intent["plan_digest"], "status": "committed",
        "source_state_digest": intent["source_state_digest"], "destination_state_digest": digest(state),
        "journal_hash": checkpoints[-1]["hash"], "backup_manifest": intent["plan"]["targets"],
        "material_digest": intent["plan"]["material_digest"], "usage_policy": "preserve_source_trust", "assessment": "unassessed"},
        state, intent["plan"]["targets"])
    name = prefix + "receipt.yaml"
    if store.path(name).exists():
        existing = store.read(name, RECEIPT_V2_SCHEMA)
        _verify_record(existing)
        receipt.update(created_at=existing["created_at"], harness_build_digest=existing["harness_build_digest"])
        receipt.pop("payload_digest")
        receipt["payload_digest"] = digest(receipt)
        if receipt != existing:
            raise HarnessError("MIGRATION_RECEIPT_CONFLICT")
    else:
        store.write(name, receipt, RECEIPT_V2_SCHEMA, exclusive=True)
    return receipt


def _quality_recover(store, expected_generation, operation):
    from . import plan_quality
    if not re.fullmatch(r"migration-[a-f0-9]{32}", operation):
        raise HarnessError("MIGRATION_INVALID_OPERATION")
    prefix = "migrations/" + operation + "/"
    intent = store.read(prefix + "intent.yaml", INTENT_V2_SCHEMA)
    _verify_record(intent)
    plan = intent["plan"]
    if (intent["operation_id"] != operation or digest(plan) != intent["plan_digest"]
            or digest(intent["material"]) != plan["material_digest"]):
        raise HarnessError("MIGRATION_SUBJECT_MISMATCH")
    backups = {}
    for target in plan["targets"]:
        if target["path"] in backups:
            raise HarnessError("MIGRATION_BACKUP_CHANGED")
        data = read_regular_file(store.path(prefix + "backup/" + target["path"]), max_bytes=MAX_BYTES)
        if hashlib.sha256(data).hexdigest() != target["sha256"] or len(data) != target["bytes"]:
            raise HarnessError("MIGRATION_BACKUP_CHANGED")
        backups[target["path"]] = data
        if target["path"] not in {"state.yaml", "journal/events.jsonl"} and read_regular_file(store.path(target["path"]), max_bytes=MAX_BYTES) != data:
            raise HarnessError("MIGRATION_SOURCE_CHANGED")
    source = loads(backups["state.yaml"], plan_quality.STATE_SCHEMA)
    if digest(source) != intent["source_state_digest"] or digest(source) != plan["source_state_digest"]:
        raise HarnessError("MIGRATION_SOURCE_CHANGED")
    if (plan["run_id"] != source["run_id"] or plan["task_id"] != source["task_id"]
            or plan["expected_generation"] != source["generation"]
            or plan["contract_path"] != source.get("contract_path", "contract.yaml")
            or plan["contract_digest"] != source["contract_digest"]
            or plan["target_policy_ref"] != intent["material"]["state"]["plan_quality"]["policy_ref"]):
        raise HarnessError("MIGRATION_SUBJECT_MISMATCH")
    contract = loads(backups[plan["contract_path"]], "work-contract.schema.json")
    if (digest(contract) != plan["contract_digest"] or contract["schema_version"] != 2 or contract["mode"] != "managed"
            or intent["material"]["state"]["plan_quality"]["max_rounds"] > contract["budgets"]["max_attempts"]):
        raise HarnessError("MIGRATION_CONTRACT_BINDING_MISMATCH")
    original_events = store._decode_events(backups["journal/events.jsonl"])
    if not original_events or original_events[-1]["hash"] != plan["source_journal_hash"]:
        raise HarnessError("MIGRATION_SOURCE_CHANGED")
    _material_state(source, intent["material"])
    if _source_snapshots(source) != plan["source_snapshots"]:
        raise HarnessError("MIGRATION_SOURCE_CHANGED")
    journal = read_regular_file(store.path("journal/events.jsonl"), max_bytes=64 * 1024 * 1024)
    if not journal.startswith(backups["journal/events.jsonl"]):
        raise HarnessError("MIGRATION_SOURCE_CHANGED")
    observed = store.recover(read_only=True)
    state = observed["state"]
    if observed.get("recovery_required"):
        raise HarnessError("MIGRATION_JOURNAL_RECOVERY_REQUIRED")
    if state["generation"] != expected_generation:
        raise HarnessError("MIGRATION_GENERATION_CONFLICT")
    marker = state.get("migration", {})
    if (marker.get("operation_id") != operation or marker.get("plan_digest") != intent["plan_digest"]
            or state["run_id"] != plan["run_id"] or state["task_id"] != plan["task_id"]):
        raise HarnessError("MIGRATION_SUBJECT_MISMATCH")
    final = intent["final_state"]
    validate(final, plan_quality.STATE_SCHEMA)
    if digest(final) != intent["destination_state_digest"]:
        raise HarnessError("MIGRATION_INVALID_DESTINATION")
    expected = deepcopy(intent["material"]["state"])
    expected.update(generation=source["generation"] + 2, updated_at=final["updated_at"],
        migration={"operation_id": operation, "status": "committed", "plan_digest": intent["plan_digest"]})
    if final != expected:
        raise HarnessError("MIGRATION_INVALID_DESTINATION")
    _quality_accounting(store, source, contract)
    if marker["status"] == "committed":
        store.recover()
        plan_quality.publish_bootstrap(store, intent["material"])
        return _quality_receipt(store, prefix, intent, state)
    if digest(state) != intent["pending_state_digest"] or final["generation"] != state["generation"] + 1:
        raise HarnessError("MIGRATION_PENDING_STATE_CHANGED")
    # Only an explicit recovery repairs derived checkpoints/state. Its policy
    # inputs stay the exact frozen material, even after the deadline expires.
    store.recover()
    plan_quality.publish_bootstrap(store, intent["material"])
    store.commit(final, "explicitly recovered v2 to v3 plan-quality upgrade", owner="migration")
    return _quality_receipt(store, prefix, intent, final)


def _upgrade_quality(run_dir, *, expected_generation, apply, expected_plan_digest, recover, max_rounds):
    from . import plan_quality
    from .strict_yaml import dumps
    if type(expected_generation) is not int or expected_generation < 0:
        raise HarnessError("MIGRATION_INVALID_GENERATION")
    store = SupervisorStore(run_dir, plan_quality.STATE_SCHEMA, create=False)
    if recover is not None:
        if max_rounds is not None:
            raise HarnessError("MIGRATION_RECOVERY_USES_FROZEN_POLICY")
        with store.lease("explicit plan-quality migration recovery"):
            return _quality_recover(store, expected_generation, recover)
    if not apply:
        _, plan, _, _ = _quality_plan(store, expected_generation, max_rounds)
        return {"status": "planned", "plan_digest": digest(plan), "plan": plan, "mutated": False}
    if expected_plan_digest is None:
        raise HarnessError("MIGRATION_PLAN_REQUIRED")
    with store.lease("explicit plan-quality migration"):
        source, plan, raw, material = _quality_plan(store, expected_generation, max_rounds)
        if digest(plan) != expected_plan_digest:
            raise HarnessError("MIGRATION_PLAN_CHANGED")
        operation = "migration-" + uuid.uuid4().hex
        prefix = "migrations/" + operation + "/"
        pending = deepcopy(material["state"])
        pending.update(generation=source["generation"] + 1, updated_at=now(),
            migration={"operation_id": operation, "status": "pending", "plan_digest": digest(plan)})
        final = deepcopy(pending)
        final["generation"] += 1
        final["migration"]["status"] = "committed"
        validate(pending, plan_quality.STATE_SCHEMA)
        validate(final, plan_quality.STATE_SCHEMA)
        intent = _identify({"schema_version": 2, "record_type": "work_state_migration_intent", "operation_id": operation,
            "plan_digest": digest(plan), "plan": plan, "status": "prepared", "source_state_digest": digest(source),
            "pending_state_digest": digest(pending), "destination_state_digest": digest(final),
            "material": material, "final_state": final}, source, plan["targets"])
        dumps(intent, INTENT_V2_SCHEMA)  # Full structured bound before backups or barrier.
        for name, data in raw.items():
            atomic_write(store.path(prefix + "backup/" + name), data, exclusive=True)
        store.write(prefix + "intent.yaml", intent, INTENT_V2_SCHEMA, exclusive=True)
        store.commit(pending, "v3 pending barrier before publishing plan-quality policy", owner="migration")
        plan_quality.publish_bootstrap(store, material)
        store.commit(final, "explicit v2 to v3 upgrade; current plan assessment still required", owner="migration")
        return _quality_receipt(store, prefix, intent, final)


def dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--expected-generation", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--recover", metavar="OPERATION_ID", help="Explicitly finish a pending migration from its immutable intent")
    parser.add_argument("--target-version", type=int, choices=(2, 3), help="Explicit 3 enables managed v2 plan-quality upgrade; default retains v1 to v2")
    parser.add_argument("--max-planning-rounds", type=int, help="Only tighten plan-quality rounds for --target-version 3")
    args = parser.parse_args(argv)
    value = migrate(args.run_dir, expected_generation=args.expected_generation, apply=args.apply,
                    expected_plan_digest=args.expected_plan_digest, recover=args.recover,
                    target_version=args.target_version, max_planning_rounds=args.max_planning_rounds)
    from .output import write_json
    write_json(value)
    return 0
