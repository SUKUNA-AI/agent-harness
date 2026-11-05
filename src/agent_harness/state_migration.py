"""Explicit, bounded work-state v1 to v2 migration using the existing journal.

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


def migrate(run_dir, *, expected_generation, apply=False, expected_plan_digest=None, recover=None):
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


def dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--expected-generation", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--recover", metavar="OPERATION_ID", help="Explicitly finish a pending migration from its immutable intent")
    args = parser.parse_args(argv)
    value = migrate(args.run_dir, expected_generation=args.expected_generation, apply=args.apply,
                    expected_plan_digest=args.expected_plan_digest, recover=args.recover)
    from .output import write_json
    write_json(value)
    return 0
