"""Privacy preserving native observer. Never controls execution or acceptance.

Wire projection is intentionally narrower than upstream events. Unknown optional
fields are discarded before hashing, validation or persistence. Capability is
not inferred from a synthetic fixture or absence of events.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import time
from pathlib import Path
import re
import uuid

from .core import HarnessError, Run, digest, lock, now, snapshot
from .execution_ledger import HASH, IDENTIFIER, SHORT, object_schema, repository_identity, safe_private, sanitized
from .file_io import read_regular_file
from .strict_yaml import dump, load, validate
from .receipt_identity import IDENTITY_PROPERTIES, IDENTITY_REQUIRED, envelope, input_ref, seal, verify_record

EVENTS = {"SessionStart", "SessionEnd", "Stop", "SubagentStop", "PreModelRequest", "PostModelRequest", "ToolStart", "ToolEnd"}
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
OUTCOMES = {"started", "completed", "failed", "cancelled", "unknown"}
NULL_ID = {"type": ["string", "null"], "maxLength": 128}
OBSERVER_V1_SCHEMA = object_schema({
    "schema_version": {"const": 1}, "record_type": {"const": "native_observation"},
    "record_id": IDENTIFIER, "project_id": HASH, "producer_kind": {"const": "native_hook_observer"},
    "generation": {"type": "integer", "minimum": 1}, "created_at": SHORT,
    "payload_digest": HASH, "event": {"enum": sorted(EVENTS)}, "event_id": NULL_ID,
    "session_id": NULL_ID, "parent_session_id": NULL_ID, "agent_id": NULL_ID,
    "agent_type": {"enum": ["root", "child", "unknown"]}, "turn_id": NULL_ID,
    "request_id": NULL_ID, "attempt_id": NULL_ID, "context_epoch": {"type": "integer", "minimum": 0},
    "session_cwd": SHORT, "hook_process_cwd": SHORT, "tool_requested_cwd": {"type": ["string", "null"], "maxLength": 512},
    "observed_checkout_ref": HASH, "namespace": SHORT,
    "resolution_status": {"enum": ["observed", "ambiguous", "unavailable"]},
    "outcome": {"enum": sorted(OUTCOMES)},
    "usage": object_schema({key: {"type": "integer", "minimum": 0, "maximum": 10**15} for key in COUNTERS}),
    "usage_binding": {"enum": ["stable_attempt", "observed_unreconciled", "unavailable"]},
    "acceptance": {"const": "not_performed"},
}, ("schema_version", "record_type", "record_id", "project_id", "producer_kind", "generation", "created_at",
    "payload_digest", "event", "event_id", "session_id", "parent_session_id", "agent_id", "agent_type", "turn_id",
    "request_id", "attempt_id", "context_epoch", "session_cwd", "hook_process_cwd", "tool_requested_cwd",
    "observed_checkout_ref", "namespace", "resolution_status", "outcome", "usage", "usage_binding", "acceptance"))

BRIDGE_V1_SCHEMA = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "native_verification_bridge"},
    "session_id": SHORT, "execution_owner": {"const": "codex_native"}, "verification": {"const": "core_gate"},
    "acceptance": {"const": "accepted"}, "candidate_digest": HASH, "native_source_digest": HASH,
    "transfer_projection_digest": HASH, "transfer_policy": {"const": "native-isolated-content-v1"},
    "run_id": IDENTIFIER, "task_id": IDENTIFIER, "attempt_id": IDENTIFIER, "contract_digest": HASH,
    "verdict_digest": HASH, "run_dir": SHORT, "created_at": SHORT, "scheduler_history": {"const": "not_claimed"}},
    ("schema_version", "record_type", "session_id", "execution_owner", "verification", "acceptance", "candidate_digest",
     "native_source_digest", "transfer_projection_digest", "transfer_policy", "run_id", "task_id", "attempt_id",
     "contract_digest", "verdict_digest", "run_dir", "created_at", "scheduler_history"))

OBSERVER_SCHEMA = object_schema({**IDENTITY_PROPERTIES, **OBSERVER_V1_SCHEMA["properties"], "schema_version": {"const": 2}},
                                (*OBSERVER_V1_SCHEMA["required"], *IDENTITY_REQUIRED))
OBSERVER_READ_SCHEMA = {"oneOf": [OBSERVER_V1_SCHEMA, OBSERVER_SCHEMA]}
BRIDGE_SCHEMA = object_schema({**IDENTITY_PROPERTIES, **BRIDGE_V1_SCHEMA["properties"], "schema_version": {"const": 2}},
                              (*BRIDGE_V1_SCHEMA["required"], *IDENTITY_REQUIRED))
BRIDGE_READ_SCHEMA = {"oneOf": [BRIDGE_V1_SCHEMA, BRIDGE_SCHEMA]}


@contextmanager
def observer_lock(path, *, purpose, timeout=5):
    """Bounded contention retry using the existing interprocess lease protocol."""
    deadline = time.monotonic() + timeout
    while True:
        manager = lock(path, purpose=purpose)
        try:
            manager.__enter__()
            break
        except HarnessError as exc:
            if not str(exc).startswith("Resource busy") or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


def _identifier(value, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise HarnessError("INVALID_NATIVE_IDENTITY")
    return value


def observe(state, event, *, hook_process_cwd=None):
    """Intake one allowlisted delivery. No prompt/body/argv/transcript retention."""
    if not isinstance(event, dict) or event.get("schema_version", 1) != 1:
        raise HarnessError("UNSUPPORTED_NATIVE_WIRE_VERSION")
    name = event.get("hook_event_name")
    if name not in EVENTS:
        raise HarnessError("UNSUPPORTED_NATIVE_EVENT")
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd or len(cwd) > 512:
        raise HarnessError("INVALID_NATIVE_CWD")
    root = safe_private(Path(cwd).absolute()).resolve(strict=True)
    if not root.is_dir():
        raise HarnessError("INVALID_NATIVE_CWD")
    ids = {key: _identifier(event.get(key), required=key == "session_id")
           for key in ("event_id", "session_id", "parent_session_id", "agent_id", "turn_id", "request_id", "attempt_id")}
    agent_type = event.get("agent_type", "unknown")
    if agent_type not in {"root", "child", "unknown"}:
        agent_type = "unknown"
    bound = ids["agent_id"] is not None or agent_type == "root" and not ids["parent_session_id"]
    resolution = "observed" if bound else "ambiguous"
    project = repository_identity(root)
    checkout = digest({"root": str(root)})
    namespace = digest({"session": ids["session_id"], "agent": ids["agent_id"],
                        "root_tag": agent_type if bound else None, "checkout": checkout, "context_epoch": event.get("context_epoch", 0)}) if bound else "unbound"
    usage = event.get("usage", {})
    if not isinstance(usage, dict):
        raise HarnessError("INVALID_NATIVE_USAGE")
    usage = {key: usage[key] for key in COUNTERS if key in usage}
    if any(type(value) is not int or not 0 <= value <= 10**15 for value in usage.values()):
        raise HarnessError("INVALID_NATIVE_USAGE")
    epoch = event.get("context_epoch", 0)
    if type(epoch) is not int or epoch < 0:
        raise HarnessError("INVALID_CONTEXT_EPOCH")
    outcome = event.get("outcome", "started" if name == "PreModelRequest" else "unknown")
    if outcome not in OUTCOMES:
        raise HarnessError("INVALID_NATIVE_OUTCOME")
    requested = event.get("tool_requested_cwd")
    if requested is not None and (not isinstance(requested, str) or len(requested) > 512):
        raise HarnessError("INVALID_TOOL_CWD")
    process_cwd = str(Path(hook_process_cwd or Path.cwd()).absolute())
    payload = {"event": name, **ids, "agent_type": agent_type, "context_epoch": epoch,
        "session_cwd": str(root), "hook_process_cwd": process_cwd, "tool_requested_cwd": requested,
        "observed_checkout_ref": checkout, "namespace": namespace, "resolution_status": resolution,
        "outcome": outcome, "usage": usage,
        "usage_binding": ("stable_attempt" if usage and ids["request_id"] and ids["attempt_id"] and bound
                          else "observed_unreconciled" if usage else "unavailable"), "acceptance": "not_performed"}
    sanitized(payload)
    payload_sha = digest(payload)
    # A delivery ID is not an invocation ID. Same bytes without a stable upstream
    # event ID remain separate legitimate/ambiguous deliveries.
    record_id = ("observed-" + digest({"project": project, "session": ids["session_id"], "agent": ids["agent_id"],
                                     "event_id": ids["event_id"]})[:32]) if ids["event_id"] else "delivery-" + uuid.uuid4().hex
    directory = safe_private(Path(state) / "native-observations" / project, create=True)
    with observer_lock(directory / ".lock", purpose="native observer intake"):
        target = safe_private(directory / (record_id + ".yaml"))
        if target.exists():
            previous = load(target, OBSERVER_READ_SCHEMA)
            _verify(previous)
            comparison_sha = payload_sha
            if previous["schema_version"] == 1:
                # V1 persisted context_epoch in the payload but omitted it from
                # namespace derivation. Compare in that exact legacy view;
                # preserve the original bytes and unknown identity coverage.
                legacy_namespace = digest({"session": ids["session_id"], "agent": ids["agent_id"],
                    "root_tag": agent_type if bound else None, "checkout": checkout}) if bound else "unbound"
                comparison_sha = digest({**payload, "namespace": legacy_namespace})
            if previous["payload_digest"] != comparison_sha:
                raise HarnessError("NATIVE_EVENT_ID_CONFLICT")
            return {"record_id": record_id, "path": str(target), "idempotent": True, "resolution_status": resolution}
        # Per-delivery generation is local and monotonic under the existing lock.
        records = list(directory.glob("*.yaml"))
        if len(records) >= 10000:
            raise HarnessError("NATIVE_OBSERVATION_STORE_BOUND")
        generations = []
        for path in records:
            previous = load(safe_private(path), OBSERVER_READ_SCHEMA)
            _verify(previous)
            generations.append(previous["generation"])
        record = {"schema_version": 2, "record_type": "native_observation", "payload_digest": payload_sha,
            **envelope(record_id=record_id, project_id=project, producer_id="native-hook-observer",
                producer_kind="native_hook_observer", producer_module=__name__, generation=max(generations, default=0) + 1,
                attempt_id=ids["attempt_id"], input_refs=[input_ref("native_event_projection", record_id, payload, "session"),
                    input_ref("checkout", checkout, {"root": str(root), "binding": checkout}, "project")]), **payload}
        seal(record)
        validate(record, OBSERVER_SCHEMA)
        dump(target, record, OBSERVER_SCHEMA, exclusive=True)
    return {"record_id": record_id, "path": str(target), "idempotent": False, "resolution_status": resolution}


def _verify(record):
    if record.get("schema_version") == 2:
        verify_record(record)
    metadata = {"schema_version", "record_type", "record_id", "project_id", "producer_kind", "generation", "created_at", "payload_digest"}
    payload = {key: record[key] for key in OBSERVER_V1_SCHEMA["properties"] if key not in metadata}
    if digest(payload) != record["payload_digest"]:
        raise HarnessError("NATIVE_OBSERVATION_INTEGRITY")


def status(state, root, *, session=None, verify_evidence=False):
    """Observed coverage, never a fabricated authoritative usage total."""
    root = Path(root).resolve(strict=True)
    project = repository_identity(root)
    directory = safe_private(Path(state) / "native-observations" / project)
    rows, failures = [], 0
    for index, path in enumerate(directory.glob("*.yaml") if directory.exists() else ()):
        if index >= 10000:
            failures += 1
            break
        try:
            row = load(safe_private(path), OBSERVER_READ_SCHEMA)
            _verify(row)
            if row["project_id"] != project or path.stem != row["record_id"]:
                raise HarnessError("NATIVE_OBSERVATION_BINDING")
            rows.append(row)
        except (OSError, ValueError, HarnessError):
            failures += 1
    known, ambiguous = {}, []
    for row in rows:
        if not row["usage"]:
            continue
        if row["usage_binding"] != "stable_attempt" or not row["event_id"]:
            ambiguous.append(row["record_id"])
            continue
        key = (row["session_id"], row["agent_id"], row["request_id"], row["attempt_id"])
        known.setdefault(key, []).append(row)
    # Multiple terminal usage deliveries for one attempt require reconciliation;
    # event identity alone does not establish which cumulative/per-request source wins.
    stable = [values[0] for values in known.values() if len(values) == 1]
    ambiguous.extend(row["record_id"] for values in known.values() if len(values) > 1 for row in values)
    report = {"schema_version": 1, "execution_owner": "codex_native", "acceptance": "not_performed",
        "verification": "not_performed", "project_id": project, "generated_at": now(),
        "as_of_generation": max((r["generation"] for r in rows), default=0),
        "registered_events": len(rows), "invalid_records": failures,
        "receipt_identity_coverage": {"bound": sum(row.get("schema_version") == 2 for row in rows),
                                      "legacy_unknown": sum(row.get("schema_version") == 1 for row in rows)},
        "usage_coverage": "unknown", "authoritative_usage": None,
        "stable_attempt_observations": len(stable), "observed_unreconciled": len(ambiguous),
        "usage_observed": {counter: sum(row["usage"].get(counter, 0) for row in stable) for counter in COUNTERS},
        "mechanisms": {"observer": {"enabled": True, "invoked": bool(rows), "usable_result": bool(rows),
                                     "consumed_if_observed": None, "independently_verified": False}},
        "capability": "not_tested", "reason_codes": ["HOOK_CAPABILITY_UNVERIFIED", "USAGE_COVERAGE_UNKNOWN"],
        "model": {"requested": None, "configured": None, "observed": None},
        "effort": {"requested": None, "configured": None, "observed": None}}
    if session is not None:
        _verification_status(report, state, root, session, verify_evidence=verify_evidence)
    return report


def capability_receipt(*, client_version, adapter_path, hook_definitions, fixtures=None):
    """Configured evidence only; synthetic fixtures never qualify live support."""
    import platform
    data = read_regular_file(safe_private(adapter_path), max_bytes=2 * 1024 * 1024)
    sanitized({"client_version": client_version})
    return {"schema_version": 1, "record_type": "native_capability", "created_at": now(),
        "client_version": client_version, "os": platform.system(), "adapter_digest": hashlib.sha256(data).hexdigest(),
        "hook_definitions_digest": digest(hook_definitions), "configured": bool(hook_definitions),
        "trusted": "unknown", "invoked": "not_tested", "supported": "not_tested",
        "fixture_results": fixtures or {}, "qualification": "not_tested"}


def verification_bridge(state, session, run_dir, *, expected_candidate):
    """Link an explicitly accepted isolated core run, revalidating its evidence.

    This does not initiate execution, copy data, stop writers or self-accept.
    Existing core init/freeze/check/review/gate remains the verification workflow.
    """
    import json
    from .native_session import SESSION_ID
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        raise HarnessError("Invalid session ID")
    session_dir = safe_private(Path(state) / "native-sessions" / session)
    metadata = json.loads(read_regular_file(safe_private(session_dir / "session.json"), max_bytes=65536))
    run = Run(run_dir)
    checked = run.status(verify_evidence=True)
    contract = run.contract()
    if Path(contract["cwd"]).resolve() == Path(metadata["root"]).resolve():
        raise HarnessError("UNKNOWN_WRITERS: use an explicitly approved isolated core verification candidate")
    if not isinstance(expected_candidate, str) or checked.get("candidate_digest") != expected_candidate:
        raise HarnessError("CANDIDATE_BINDING_MISMATCH")
    native_source = snapshot({**contract, "cwd": metadata["root"]})
    isolated_source = run.candidate()
    if _transfer_projection(native_source) != _transfer_projection(isolated_source):
        raise HarnessError("NATIVE_TRANSFER_SOURCE_MISMATCH")
    if checked["status"] != "accepted" or not checked.get("evidence_current") or not checked.get("source_current"):
        raise HarnessError("CORE_VERIFICATION_NOT_CURRENT")
    row = {"schema_version": 2, "record_type": "native_verification_bridge", "session_id": session,
           "execution_owner": "codex_native", "verification": "core_gate", "acceptance": "accepted",
           "candidate_digest": checked["candidate_digest"], "native_source_digest": native_source["digest"],
           "transfer_projection_digest": digest(_transfer_projection(native_source)),
           "transfer_policy": "native-isolated-content-v1", "run_id": checked["run_id"],
           "task_id": checked["task_id"], "attempt_id": checked["current_attempt"],
           "contract_digest": checked["contract_digest"], "verdict_digest": checked["accepted"]["verdict_digest"],
           "run_dir": str(run.path), "created_at": now(), "scheduler_history": "not_claimed"}
    with observer_lock(session_dir / "bridge.lock", purpose="native verification bridge"):
        generations = []
        for index, path in enumerate(session_dir.glob("verification-*.yaml")):
            if index >= 128:
                raise HarnessError("VERIFICATION_HISTORY_BOUND")
            generations.append(load(safe_private(path), BRIDGE_READ_SCHEMA).get("generation", 0))
        row.update(envelope(record_id="native-verification-" + uuid.uuid4().hex,
            project_id=repository_identity(Path(metadata["root"])), producer_id="native-verification-bridge",
            producer_kind="native_core_verification_bridge", producer_module=__name__, generation=max(generations, default=0) + 1,
            run_id=checked["run_id"], task_id=checked["task_id"], attempt_id=checked["current_attempt"],
            input_refs=[input_ref("native_source", native_source["digest"], _transfer_projection(native_source), "project"),
                        input_ref("core_candidate", checked["candidate_digest"], isolated_source, "candidate"),
                        input_ref("core_verdict", checked["accepted"]["verdict_digest"], checked["accepted"], "candidate")]))
        row["payload_digest"] = digest({key: row[key] for key in BRIDGE_V1_SCHEMA["properties"] if key not in {"schema_version", "created_at"}})
        seal(row)
        sanitized(row)
        dump(session_dir / ("verification-" + digest(row) + ".yaml"), row, BRIDGE_SCHEMA, exclusive=True)
    return row


def _transfer_projection(value):
    """Explicit transfer policy; excludes checkout path, root inode and branch."""
    return {"policy": "native-isolated-content-v1", **{key: value.get(key) for key in (
        "snapshot_scheme_version", "topology_policy", "kind", "files", "external_inputs", "exclude")},
        "git": {key: value.get("git", {}).get(key) for key in ("head", "settings")}}


def _verification_status(report, state, root, session, *, verify_evidence):
    import json
    from .native_session import SESSION_ID
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        raise HarnessError("Invalid session ID")
    directory = safe_private(Path(state) / "native-sessions" / session)
    metadata = json.loads(read_regular_file(safe_private(directory / "session.json"), max_bytes=65536))
    if Path(metadata["root"]).resolve() != root:
        raise HarnessError("Session root mismatch")
    receipts = []
    for index, path in enumerate(directory.glob("verification-*.yaml")):
        if index >= 128:
            report["reason_codes"].append("VERIFICATION_HISTORY_BOUND")
            break
        try:
            row = load(safe_private(path), BRIDGE_READ_SCHEMA)
            if row["schema_version"] == 2:
                verify_record(row)
            if row["session_id"] != session or path.stem != "verification-" + digest(row):
                raise HarnessError("Verification bridge integrity mismatch")
            receipts.append(row)
        except (OSError, ValueError, HarnessError):
            report["reason_codes"].append("VERIFICATION_RECEIPT_INVALID")
    if not receipts:
        return
    receipt = sorted(receipts, key=lambda row: (row["created_at"], row["candidate_digest"]))[-1]
    report.update(verification="core_gate", recorded_acceptance="accepted", acceptance="unknown",
                  candidate_digest=receipt["candidate_digest"], evidence_validation="not_checked")
    report["verification_receipt"] = {"session_id": session, "run_id": receipt["run_id"],
        "task_id": receipt["task_id"], "attempt_id": receipt["attempt_id"], "verdict_digest": receipt["verdict_digest"],
        "candidate_digest": receipt["candidate_digest"], "run_dir": receipt["run_dir"]}
    if not verify_evidence:
        return
    try:
        run = Run(receipt["run_dir"])
        checked = run.status(verify_evidence=True)
        native_source = snapshot({**run.contract(), "cwd": str(root)})
        valid = (checked["status"] == "accepted" and checked.get("evidence_current")
                 and checked.get("candidate_digest") == receipt["candidate_digest"]
                 and (checked.get("accepted") or {}).get("verdict_digest") == receipt["verdict_digest"]
                 and digest(_transfer_projection(native_source)) == receipt["transfer_projection_digest"])
    except (OSError, ValueError, KeyError, TypeError, HarnessError):
        try:
            Path(receipt["run_dir"]).lstat()
            missing = False
        except FileNotFoundError:
            missing = True
        except OSError:
            missing = False
        report.update(acceptance="unknown", evidence_validation="unavailable")
        report["reason_codes"].append("CORE_VERIFICATION_RUN_MISSING" if missing else "CORE_VERIFICATION_RUN_UNAVAILABLE")
        return
    report.update(acceptance="accepted" if valid else "stale", evidence_validation="passed" if valid else "failed")
