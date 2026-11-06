"""Persisted frozen-cohort driver; no CLI enables unqualified production runs.

An application supplies BudgetAuthorization explicitly, plus a prepared adapter.
This module does not turn the manifest's untrusted budget/isolation files into
authority. The shipped transport is an adapter API with contained synthetic
qualification; no production isolation adapter is currently registered. Real
dispatch therefore fails before model calls. Adding one requires reviewed code
for actual filesystem/network/evaluator controls through the solve boundary.

Solving reuses CodexAdapter for plain arms and Supervisor for harness arms.
Receipts use the existing SupervisorStore journal. Unknown execution/grade
outcomes block replay; successful persisted work is never dispatched twice.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import time
import uuid

from . import __version__, evaluation, benchmark_metrics
from .codex_execution import CodexAdapter
from .core import HarnessError, digest, identity_alive, now, snapshot
from .execution_ledger import HASH, SHORT, object_schema
from .execution_receipts import build_digest
from .file_io import read_regular_file
from .runner import _contained_capture
from .strict_yaml import loads, validate
from .supervisor import Supervisor
from .supervisor_store import SupervisorStore, _parents, _ordinary
from .work_contract import load_contract, legacy_contract

NATURAL = {"type": "integer", "minimum": 0}
NULL_COUNT = {"type": ["integer", "null"], "minimum": 0}
ROW = object_schema({"suite": SHORT, "task_id": SHORT, "repeat": NATURAL, "arm": SHORT, "trial_id": HASH},
                    ("suite", "task_id", "repeat", "arm", "trial_id"))
AUTHORIZATION = object_schema({"manifest_digest": HASH, "authorization_id": SHORT,
    "max_total_tokens": {"type": "integer", "minimum": 1}, "deadline": {"type": "string", "format": "utc"},
    "measurement_kind": {"enum": ["synthetic", "observed"]}},
    ("manifest_digest", "authorization_id", "max_total_tokens", "deadline", "measurement_kind"))
IDENTITY = object_schema({"adapter_id": SHORT, "build_digest": HASH, "boundary_digest": HASH,
    "measurement_kind": {"enum": ["synthetic", "observed"]},
    "model": SHORT, "provider": SHORT, "client_version": SHORT},
    ("adapter_id", "build_digest", "boundary_digest", "measurement_kind", "model", "provider", "client_version"))
PREPARED = object_schema({"schema_version": {"const": 1}, "trial_id": HASH, "cwd": SHORT,
    "contract_path": SHORT, "root_policy_digest": HASH, "child_policy_digest": HASH,
    "token_upper_bound": NULL_COUNT},
    ("schema_version", "trial_id", "cwd", "contract_path", "root_policy_digest", "child_policy_digest", "token_upper_bound"))
ADAPTER_PREPARED = PREPARED
PREPARED = object_schema({**ADAPTER_PREPARED["properties"], "contract_digest": HASH, "input_snapshot_digest": HASH},
    (*ADAPTER_PREPARED["required"], "contract_digest", "input_snapshot_digest"))
CONTROL = object_schema({"schema_version": {"const": 1}, "nonce": SHORT, "identity_digest": HASH,
    "manifest_digest": HASH, "measurement_kind": {"enum": ["synthetic", "observed"]},
    "controls": object_schema({name: {"enum": ["synthetic_denied", "denied"]}
        for name in ("filesystem", "network", "evaluator", "peer_state")},
        ("filesystem", "network", "evaluator", "peer_state"))},
    ("schema_version", "nonce", "identity_digest", "manifest_digest", "measurement_kind", "controls"))
MEASUREMENTS = object_schema({"mandatory_nodes_skipped": NATURAL, "duplicate_writes": NATURAL,
    "recovery": {"enum": ["not_applicable", "succeeded", "failed", "unknown"]},
    "useful_retrieval": {"type": ["boolean", "null"]}, "stale_reuse": NATURAL,
    "hook_overhead_ms": {"type": ["number", "null"], "minimum": 0}},
    ("mandatory_nodes_skipped", "duplicate_writes", "recovery", "useful_retrieval", "stale_reuse", "hook_overhead_ms"))
MEASUREMENTS_V1 = MEASUREMENTS
MEASUREMENTS = {"oneOf": [MEASUREMENTS_V1, benchmark_metrics.MEASUREMENTS_V2]}
SOLVE = object_schema({"outcome": {"enum": ["completed", "failed", "cancelled", "timeout", "environment_error", "unknown"]},
    "claimed_accepted": {"type": "boolean"}, "elapsed_seconds": {"type": "number", "minimum": 0},
    "usage_coverage": {"enum": ["complete", "partial", "unknown"]},
    "result_snapshot_digest": {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"},
    "usage": {"type": "array", "items": evaluation.USAGE, "maxItems": 10000}},
    ("outcome", "claimed_accepted", "elapsed_seconds", "usage_coverage", "usage", "result_snapshot_digest"))
SOLVE_ARTIFACT = object_schema({"schema_version": {"const": 1}, "manifest_digest": HASH,
    "trial_id": HASH, "execution_id": SHORT, "prepared_digest": HASH, "result": SOLVE},
    ("schema_version", "manifest_digest", "trial_id", "execution_id", "prepared_digest", "result"))
STATE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_run"},
    "manifest_digest": HASH, "schedule_digest": HASH, "authorization": AUTHORIZATION,
    "identity": IDENTITY, "assets_root": SHORT, "created_at": SHORT,
    "harness_version": SHORT, "harness_build_digest": HASH,
    "trial_token_bounds": {"type": "array", "items": object_schema({"trial_id": HASH, "tokens": NULL_COUNT}, ("trial_id", "tokens"))}},
    ("schema_version", "record_type", "manifest_digest", "schedule_digest", "authorization", "identity", "assets_root", "created_at", "trial_token_bounds", "harness_version", "harness_build_digest"))
PAYLOADS = {
    "qualification": object_schema({"challenge_digest": HASH, "artifact": evaluation.FILE_REF,
        "identity_digest": HASH, "scope": {"enum": ["synthetic_interface_only", "qualified_boundary_controls"]}},
        ("challenge_digest", "artifact", "identity_digest", "scope")),
    "prepared": PREPARED,
    "dispatch": object_schema({"execution_id": SHORT, "prepared_digest": HASH, "identity_digest": HASH,
        "qualification_ref": SHORT, "reservation": NATURAL},
        ("execution_id", "prepared_digest", "identity_digest", "qualification_ref", "reservation")),
    "solve": SOLVE,
    "measurements": MEASUREMENTS,
    "grade_intent": object_schema({"execution_id": SHORT, "solve_digest": HASH, "input_ref": evaluation.FILE_REF},
        ("execution_id", "solve_digest", "input_ref")),
    "grade": evaluation.GRADE,
    "trial": evaluation.TRIAL,
}
# The empty registry is intentional. A Python flag, transcript or manifest file
# cannot install a qualified production boundary. There is no public run CLI.
_SUPPORTED_OBSERVED_ADAPTERS = ()


def _trial_descendant(root, path):
    # Windows resolve may return an extended-length canonical spelling. Both
    # operands must use that spelling; ancestors are separately link-checked.
    return path.is_absolute() and root.resolve(strict=True) in path.resolve(strict=True).parents


class _DeadlineCodexAdapter(CodexAdapter):
    """Preserve the prepared transport while bounding every external action."""
    def __init__(self, backend, runner, deadline):
        self.backend, self.runner, self.deadline = backend, runner, deadline
        self.executable = backend.executable

    def capabilities(self, *, deadline=None):
        end = self.runner._earliest_deadline(self.deadline, deadline)
        self.runner._require_remaining(end)
        if "deadline" not in inspect.signature(self.backend.capabilities).parameters:
            raise HarnessError("BENCH_CAPABILITY_DEADLINE_UNSUPPORTED")
        value = self.backend.capabilities(deadline=end)
        self.runner._require_remaining(end)
        return value

    def preflight(self, prompt, **options):
        self.runner._require_remaining(self.deadline)
        validator = getattr(self.backend, "preflight", None)
        if validator is None:
            raise HarnessError("BENCH_TRANSPORT_PREFLIGHT_UNSUPPORTED")
        result = validator(prompt, **options)
        self.runner._require_remaining(self.deadline)
        return result

    def run(self, prompt, **options):
        end = self.runner._earliest_deadline(self.deadline, options.get("deadline"))
        remaining = self.runner._require_remaining(end)
        options.update(deadline=end, timeout=min(options.get("timeout", remaining), remaining))
        # Returning usage after expiry is still mandatory reconciliation. Do
        # not discard an already-dispatched completion as a free failure.
        return self.backend.run(prompt, **options)


@dataclass(frozen=True)
class BudgetAuthorization:
    """Explicit caller authority; never inferred from an asset's approved flag."""
    manifest_digest: str
    authorization_id: str
    max_total_tokens: int
    deadline: str
    measurement_kind: str


class CodexBenchmarkAdapter:
    """Prepared environment integration API, not a second agent runtime.

    Implementations provide pinned identity, preparation, a fresh control
    command through their solve boundary, Codex transport and independent
    evaluator argv. Only synthetic adapters are currently executable. A future
    production adapter must be registered in reviewed package code after actual
    boundary qualification; a caller cannot assert itself into that registry.
    """
    def identity(self):
        raise HarnessError("ENVIRONMENT_ISOLATION_UNAVAILABLE: no prepared transport")

    def control_argv(self, challenge, *, deadline=None):
        raise HarnessError("ENVIRONMENT_ISOLATION_UNAVAILABLE: fresh boundary controls unavailable")

    def prepare(self, row, manifest, assets_root, trial_root, *, deadline=None):
        raise HarnessError("BENCH_PREPARATION_UNAVAILABLE")

    def codex(self, prepared, *, deadline=None):
        raise HarnessError("BENCH_TRANSPORT_UNAVAILABLE")

    def evaluator_argv(self, row, prepared, grade_input, *, deadline=None):
        raise HarnessError("BENCH_EVALUATOR_UNAVAILABLE")

    def measurements(self, row, prepared, result, *, deadline=None):
        # Unknown counters must not be silently replaced by zero.
        raise HarnessError("BENCH_MEASUREMENTS_UNAVAILABLE")

    def estimate_trial_tokens(self, row, manifest, assets_root):
        """Return a supported upper bound, not a predicted average; None blocks."""
        return None


def production_status():
    return {"status": "unavailable", "reason_code": "ENVIRONMENT_ISOLATION_UNAVAILABLE",
        "registered_adapters": [], "model_calls": 0,
        "next_step": "Provide and qualify a supported filesystem/network/evaluator boundary; do not bypass permissions"}


class BenchmarkRunner:
    def __init__(self, run_dir, adapter, *, fault_controller=None):
        if not isinstance(adapter, CodexBenchmarkAdapter):
            raise HarnessError("BENCH_ADAPTER_API_REQUIRED")
        self.store = SupervisorStore(run_dir, STATE, create=False)
        self.adapter = adapter
        self.fault_controller = fault_controller
        self.state = self.store.read("state.yaml", STATE)
        self.manifest = self.store.read("manifest.yaml", evaluation.MANIFEST_SCHEMA)
        self.rows = evaluation.schedule(self.manifest)
        if digest(self.manifest) != self.state["manifest_digest"] or digest(self.rows) != self.state["schedule_digest"]:
            raise HarnessError("BENCH_FROZEN_INPUT_CONFLICT")
        self._authorization_end = time.monotonic() + max(0, self._remaining())

    @classmethod
    def create(cls, manifest, *, assets_root, run_dir, adapter, authorization, fault_controller=None):
        if not isinstance(adapter, CodexBenchmarkAdapter):
            raise HarnessError("BENCH_ADAPTER_API_REQUIRED")
        if not isinstance(authorization, BudgetAuthorization):
            raise HarnessError("BENCH_EXPLICIT_CALLER_AUTHORIZATION_REQUIRED")
        grant = asdict(authorization)
        validate(grant, AUTHORIZATION)
        evaluation.validate_manifest(manifest, assets_root)
        if grant["manifest_digest"] != digest(manifest):
            raise HarnessError("BENCH_AUTHORIZATION_BINDING_MISMATCH")
        identity = adapter.identity()
        validate(identity, IDENTITY)
        if identity["measurement_kind"] != grant["measurement_kind"]:
            raise HarnessError("BENCH_MEASUREMENT_KIND_CONFLICT")
        assets_root, run_dir = Path(assets_root).absolute(), Path(run_dir).absolute()
        if run_dir == assets_root or assets_root in run_dir.parents:
            raise HarnessError("BENCH_PRIVATE_RUN_MUST_BE_OUTSIDE_ASSETS")
        rows = evaluation.schedule(manifest)
        bounds = [{"trial_id": row["trial_id"], "tokens": adapter.estimate_trial_tokens(row, manifest, assets_root)} for row in rows]
        store = SupervisorStore(run_dir, STATE)
        with store.lease("freeze benchmark cohort"):
            if store.path("state.yaml").exists() or store.events():
                raise HarnessError("BENCH_RUN_EXISTS")
            store.write("manifest.yaml", manifest, evaluation.MANIFEST_SCHEMA, exclusive=True)
            store.write("schedule.yaml", rows, {"type": "array", "items": ROW}, exclusive=True)
            state = {"schema_version": 1, "record_type": "benchmark_run", "manifest_digest": digest(manifest),
                "schedule_digest": digest(rows), "authorization": grant, "identity": identity,
                "assets_root": str(assets_root), "created_at": now(), "trial_token_bounds": bounds,
                "harness_version": __version__, "harness_build_digest": build_digest()}
            store.commit(state, "frozen complete cohort and explicit caller budget")
        return cls(run_dir, adapter, fault_controller=fault_controller)

    def _record(self, kind, trial_id, payload, *, record_id=None):
        validate(payload, PAYLOADS[kind])
        record = {"schema_version": 1, "record_type": "benchmark_operation", "record_id": record_id or uuid.uuid4().hex,
            "manifest_digest": self.state["manifest_digest"], "kind": kind, "trial_id": trial_id,
            "created_at": now(), "payload": payload}
        record["payload_digest"] = digest(record)
        previous = {r["record_id"]: r for r in self.records()}
        if record["record_id"] in previous:
            old = previous[record["record_id"]]
            if old["kind"] != kind or old["trial_id"] != trial_id or old["payload"] != payload:
                raise HarnessError("BENCH_RECEIPT_ID_CONFLICT")
            return old
        self.store.append_event({"kind": "benchmark_operation", "receipt": record})
        self.store.write("receipts/" + record["record_id"] + ".yaml", record, self._schema(kind), exclusive=True)
        return record

    def _schema(self, kind):
        return object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_operation"},
            "record_id": SHORT, "manifest_digest": HASH, "kind": {"const": kind},
            "trial_id": {"type": ["string", "null"]}, "created_at": SHORT,
            "payload": PAYLOADS[kind], "payload_digest": HASH},
            ("schema_version", "record_type", "record_id", "manifest_digest", "kind", "trial_id", "created_at", "payload", "payload_digest"))

    def records(self, *, repair=False):
        result, subjects = {}, set()
        for event in self.store.events():
            if event["payload"].get("kind") != "benchmark_operation":
                continue
            row = event["payload"]["receipt"]
            if row.get("kind") not in PAYLOADS:
                raise HarnessError("BENCH_UNKNOWN_RECEIPT")
            validate(row, self._schema(row["kind"]))
            content = dict(row)
            supplied = content.pop("payload_digest")
            if digest(content) != supplied or row["manifest_digest"] != self.state["manifest_digest"]:
                raise HarnessError("BENCH_RECEIPT_INTEGRITY_CONFLICT")
            if row["record_id"] in result and result[row["record_id"]] != row:
                raise HarnessError("BENCH_RECEIPT_ID_CONFLICT")
            subject = (row["kind"], row["trial_id"])
            if row["kind"] != "qualification" and subject in subjects and row["record_id"] not in result:
                raise HarnessError("BENCH_CONFLICTING_TRIAL_OPERATION")
            if row["kind"] != "qualification" and row["trial_id"] not in {r["trial_id"] for r in self.rows}:
                raise HarnessError("BENCH_TRIAL_BINDING_MISMATCH")
            subjects.add(subject)
            path = "receipts/" + row["record_id"] + ".yaml"
            if self.store.path(path).exists():
                if self.store.read(path, self._schema(row["kind"])) != row:
                    raise HarnessError("BENCH_RECEIPT_PROJECTION_CONFLICT")
            elif repair:
                self.store.write(path, row, self._schema(row["kind"]), exclusive=True)
            result[row["record_id"]] = row
        records = list(result.values())
        by_subject = {(row["kind"], row["trial_id"]): row for row in records if row["kind"] != "qualification"}
        for row in records:
            trial_id, kind = row["trial_id"], row["kind"]
            if kind == "dispatch":
                prepared = by_subject.get(("prepared", trial_id))
                qualification = result.get(row["payload"]["qualification_ref"])
                if (prepared is None or digest(prepared["payload"]) != row["payload"]["prepared_digest"]
                        or qualification is None or qualification["kind"] != "qualification"
                        or row["payload"]["identity_digest"] != digest(self.state["identity"])):
                    raise HarnessError("BENCH_DISPATCH_BINDING_MISMATCH")
            elif kind in {"solve", "measurements", "grade_intent", "grade", "trial"}:
                if ("dispatch", trial_id) not in by_subject:
                    raise HarnessError("BENCH_COMPLETION_WITHOUT_DISPATCH")
                if kind in {"measurements", "grade_intent", "grade", "trial"} and ("solve", trial_id) not in by_subject:
                    raise HarnessError("BENCH_GRADING_WITHOUT_SOLVE")
            if kind in {"prepared", "grade", "trial"} and row["payload"]["trial_id"] != trial_id:
                raise HarnessError("BENCH_TRIAL_BINDING_MISMATCH")
        return records

    def _fresh_inputs(self):
        if self.state["harness_version"] != __version__ or self.state["harness_build_digest"] != build_digest():
            raise HarnessError("BENCH_HARNESS_BUILD_CHANGED")
        if self.store.read("manifest.yaml", evaluation.MANIFEST_SCHEMA) != self.manifest:
            raise HarnessError("BENCH_FROZEN_MANIFEST_CHANGED")
        evaluation.validate_manifest(self.manifest, self.state["assets_root"])
        identity = self.adapter.identity()
        validate(identity, IDENTITY)
        if identity != self.state["identity"]:
            raise HarnessError("BENCH_BACKEND_IDENTITY_CHANGED")
        for key in ("model", "provider", "client_version"):
            if identity[key] != self.manifest[key]:
                raise HarnessError("BENCH_BACKEND_IDENTITY_MISMATCH: " + key)
        if self.store.read("schedule.yaml", {"type": "array", "items": ROW}) != self.rows:
            raise HarnessError("BENCH_FROZEN_SCHEDULE_CHANGED")
        for record in self.records():
            if record["kind"] != "qualification":
                continue
            control = loads(evaluation.checked_ref(record["payload"]["artifact"], self.store.root), CONTROL)
            challenge = {key: value for key, value in control.items() if key != "controls"}
            if digest(challenge) != record["payload"]["challenge_digest"] or control["identity_digest"] != digest(identity):
                raise HarnessError("BENCH_QUALIFICATION_ARTIFACT_CONFLICT")
        records = self.records()
        for record in records:
            if record["kind"] != "prepared":
                continue
            prepared = record["payload"]
            self._contract(prepared)
            solved = next((r for r in records if r["kind"] == "solve" and r["trial_id"] == record["trial_id"]), None)
            if solved is not None:
                if solved["payload"]["result_snapshot_digest"] is not None:
                    self._binding(prepared, solved["payload"]["result_snapshot_digest"])
            elif not any(r["kind"] == "dispatch" and r["trial_id"] == record["trial_id"] for r in records):
                self._binding(prepared, prepared["input_snapshot_digest"])

    def _remaining(self):
        try:
            deadline = datetime.fromisoformat(self.state["authorization"]["deadline"].replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                raise ValueError("timezone missing")
        except ValueError as error:
            raise HarnessError("BENCH_INVALID_AUTHORIZATION_DEADLINE") from error
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if hasattr(self, "_authorization_end"):
            remaining = min(remaining, self._authorization_end - time.monotonic())
        return remaining

    def _earliest_deadline(self, *deadlines):
        values = [self.state["authorization"]["deadline"], *[value for value in deadlines if value is not None]]
        return min(values, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))

    def _require_remaining(self, deadline=None):
        remaining = self._remaining()
        if deadline is not None:
            remaining = min(remaining, (datetime.fromisoformat(deadline.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            raise HarnessError("BENCH_DEADLINE_EXHAUSTED: original authorization/task deadline reached")
        return remaining

    def _call_phase(self, callback, *args, deadline=None):
        """Portable cooperative callback bound; no detached writer survives it.

        Callbacks may accept an absolute deadline to bound their own IO. Legacy
        callbacks remain supported with before/after checks. This does not claim
        a hard interruption guarantee for an uncooperative blocking OS syscall.
        External actions are separately checked immediately before launch.
        """
        end = self._earliest_deadline(deadline)
        self._require_remaining(end)
        options = {"deadline": end} if "deadline" in inspect.signature(callback).parameters else {}
        value = callback(*args, **options)
        self._require_remaining(end)
        return value

    def _capture(self, argv, limit, *, deadline=None):
        timeout = min(limit, self._require_remaining(deadline))
        try:
            value = _contained_capture(argv, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self._require_remaining(deadline)
            raise HarnessError("BENCH_EXTERNAL_ACTION_TIMEOUT") from error
        self._require_remaining(deadline)
        return value

    def _contract(self, prepared):
        value = load_contract(prepared["contract_path"])
        if "contract_digest" in prepared and digest(value) != prepared["contract_digest"]:
            raise HarnessError("BENCH_PREPARED_CONTRACT_CHANGED")
        return value

    def _source_digest(self, prepared, contract=None):
        contract = contract or self._contract(prepared)
        value = legacy_contract(contract, cwd=prepared["cwd"], run_id="bench-" + prepared["trial_id"],
            attempt=1, deadline=self.state["authorization"]["deadline"])
        return snapshot(value)["digest"]

    def _binding(self, prepared, expected):
        self._contract(prepared)
        if expected is None:
            raise HarnessError("BENCH_RESULT_SOURCE_UNAVAILABLE")
        if self._source_digest(prepared) != expected:
            raise HarnessError("BENCH_PREPARED_SOURCE_CHANGED")

    def _qualify(self):
        self._require_remaining()
        self._fresh_inputs()
        identity = self.state["identity"]
        if identity["measurement_kind"] == "observed" and type(self.adapter) not in _SUPPORTED_OBSERVED_ADAPTERS:
            raise HarnessError("ENVIRONMENT_ISOLATION_UNAVAILABLE: no supported qualified production adapter")
        nonce = uuid.uuid4().hex
        challenge = {"schema_version": 1, "nonce": nonce, "manifest_digest": self.state["manifest_digest"],
            "identity_digest": digest(identity), "measurement_kind": identity["measurement_kind"]}
        argv = self._call_phase(self.adapter.control_argv, challenge)
        if not isinstance(argv, (tuple, list)) or not argv or not all(isinstance(v, str) for v in argv):
            raise HarnessError("BENCH_CONTROL_COMMAND_REQUIRED")
        result = self._capture(list(argv), 15)
        if result.returncode:
            raise HarnessError("ENVIRONMENT_ISOLATION_UNAVAILABLE: control process failed")
        control = loads(result.stdout, CONTROL)
        if any(control[key] != challenge[key] for key in challenge):
            raise HarnessError("BENCH_STALE_QUALIFICATION")
        expected = "synthetic_denied" if identity["measurement_kind"] == "synthetic" else "denied"
        if any(value != expected for value in control["controls"].values()):
            raise HarnessError("ENVIRONMENT_ISOLATION_UNAVAILABLE: controls do not qualify this execution kind")
        relative = "qualification/" + nonce + ".yaml"
        self.store.write(relative, control, CONTROL, exclusive=True)
        ref = {"path": relative, "sha256": hashlib.sha256(read_regular_file(self.store.path(relative))).hexdigest()}
        return self._record("qualification", None, {"challenge_digest": digest(challenge), "artifact": ref,
            "identity_digest": digest(identity), "scope": "synthetic_interface_only" if identity["measurement_kind"] == "synthetic" else "qualified_boundary_controls"})

    def _prepared(self, row, records):
        existing = [r for r in records if r["trial_id"] == row["trial_id"] and r["kind"] == "prepared"]
        if existing:
            prepared = existing[0]["payload"]
        else:
            root = self.store.path("trials/" + row["trial_id"])
            if root.exists():
                raise HarnessError("BENCH_PREPARATION_UNKNOWN: preserve partial preparation; do not replay")
            self._require_remaining()
            root.mkdir(parents=True)
            prepared = self._call_phase(self.adapter.prepare, row, self.manifest, Path(self.state["assets_root"]), root)
            validate(prepared, ADAPTER_PREPARED)
        if prepared["trial_id"] != row["trial_id"]:
            raise HarnessError("BENCH_PREPARED_SUBJECT_MISMATCH")
        root = self.store.path("trials/" + row["trial_id"])
        for key in ("cwd", "contract_path"):
            path = Path(prepared[key])
            _parents(path)
            _ordinary(path, directory=key == "cwd")
            if not _trial_descendant(root, path):
                raise HarnessError("BENCH_CROSS_TRIAL_STATE: prepared paths must be separate ordinary trial descendants")
        contract = self._contract(prepared)
        estimate = next(r["tokens"] for r in self.state["trial_token_bounds"] if r["trial_id"] == row["trial_id"])
        if prepared["token_upper_bound"] != estimate:
            raise HarnessError("BENCH_PREPARED_BUDGET_BOUND_CHANGED")
        if self.manifest["comparison"] == "matched":
            if (prepared["root_policy_digest"] != self.manifest["root_policy"]["sha256"]
                    or prepared["child_policy_digest"] != self.manifest["child_policy"]["sha256"]):
                raise HarnessError("BENCH_MATCHED_POLICY_MISMATCH")
        if row["arm"].startswith("harness_") and (not contract["memory"] or contract["ledger"] == "off"):
            raise HarnessError("BENCH_HARNESS_ARM_POLICY_MISMATCH")
        if not existing:
            prepared = {**prepared, "contract_digest": digest(contract),
                "input_snapshot_digest": self._call_phase(self._source_digest, prepared, contract)}
            self._record("prepared", row["trial_id"], prepared, record_id=row["trial_id"] + "-prepared")
        if not any(r["kind"] == "dispatch" and r["trial_id"] == row["trial_id"] for r in records):
            self._call_phase(self._binding, prepared, prepared["input_snapshot_digest"])
        return prepared

    def _usage(self, records):
        known = {}
        unknown = []
        solved = {r["trial_id"]: r for r in records if r["kind"] == "solve"}
        for row in records:
            if row["kind"] != "dispatch":
                continue
            result = solved.get(row["trial_id"])
            if result is None:
                unknown.append(row["trial_id"])
                continue
            result = result["payload"]
            if result["usage_coverage"] != "complete":
                unknown.append(row["trial_id"])
            for usage in result["usage"]:
                key = usage["invocation_id"]
                if key in known:
                    raise HarnessError("BENCH_DUPLICATE_INVOCATION: repeated accounting source")
                known[key] = usage
                if usage["status"] != "complete" or usage["input_tokens"] is None or usage["output_tokens"] is None:
                    unknown.append(row["trial_id"])
        complete = {key for key, item in known.items() if item["status"] == "complete"
            and item["input_tokens"] is not None and item["output_tokens"] is not None}
        return {"observed_tokens": sum(v for item in known.values() for v in (item["input_tokens"], item["output_tokens"]) if v is not None),
            "unknown_trial_ids": sorted(set(unknown)), "covered_invocation_ids": sorted(complete),
            "unknown_invocation_ids": sorted(set(known) - complete),
            "unknown_execution_ids": [r["payload"]["execution_id"] for r in records if r["kind"] == "dispatch" and r["trial_id"] not in solved],
            "coverage": "partial" if unknown else "complete", "hard_cap_supported": False}

    def _solve(self, row, prepared, execution_id):
        root = self.store.path("trials/" + row["trial_id"])
        result_path = root / "solve.yaml"
        if result_path.exists():
            return self._solve_artifact(row, prepared, execution_id)
        self._require_remaining()
        contract = self._contract(prepared)
        dispatch = next(r for r in self.records() if r["kind"] == "dispatch" and r["trial_id"] == row["trial_id"])
        started_at = datetime.fromisoformat(dispatch["created_at"].replace("Z", "+00:00"))
        deadline = self._earliest_deadline((started_at + timedelta(seconds=contract["budgets"]["max_wall_seconds"])).isoformat())
        if self.fault_controller is not None:
            self._require_remaining(deadline)
            self.fault_controller.before_solve(manifest_digest=self.state["manifest_digest"],
                trial_id=row["trial_id"], execution_id=execution_id, prepared=prepared,
                dispatch_digest=digest(dispatch["payload"]), deadline=deadline)
            self._require_remaining(deadline)
        backend = self._call_phase(self.adapter.codex, prepared, deadline=deadline)
        if not isinstance(backend, CodexAdapter):
            raise HarnessError("BENCH_CODEX_ADAPTER_REQUIRED")
        backend = _DeadlineCodexAdapter(backend, self, deadline)
        self._call_phase(self._binding, prepared, prepared["input_snapshot_digest"], deadline=deadline)
        started = time.monotonic()
        usage = []
        if row["arm"].startswith("harness_"):
            supervisor = Supervisor.create(prepared["contract_path"], run_dir=root / "supervisor", cwd=prepared["cwd"],
                adapter=backend, state_dir=root / "state")
            # The contract remains frozen. Narrow the authoritative invocation
            # deadline before start, rather than giving the task a fresh grant.
            with supervisor.store.lease("bound task to original benchmark authorization"):
                supervisor.state["deadline"] = self._earliest_deadline(supervisor.state["deadline"], deadline)
                supervisor._save("original benchmark authorization limits task deadline")
            self._require_remaining(deadline)
            report = supervisor.start()
            outcome = {"ACCEPTED": "completed", "TIMED_OUT": "timeout", "CANCELLED": "cancelled", "UNKNOWN_OUTCOME": "unknown"}.get(report["status"], "failed")
            accounting = supervisor.receipts().reconcile_usage()
            for key, intent in accounting["intents"].items():
                complete = accounting["completions"].get(key)
                payload = complete["payload"] if complete else {}
                phase = {"@planner": "planner", "@integration": "integration", "@review": "reviewer", "@root": "initial"}.get(intent["node_id"], "worker")
                if intent["attempt_id"].startswith("attempt-") and intent["attempt_id"] != "attempt-1":
                    phase = "repair"
                usage.append({"invocation_id": key, "scope": "individual_attempt", "phase": phase,
                    "status": payload.get("usage_status", "unavailable") if payload.get("usage_status") in {"complete", "partial", "unavailable", "conflicting"} else "unavailable",
                    "input_tokens": payload.get("input_tokens"), "output_tokens": payload.get("output_tokens")})
            claimed = report["recorded_status"] == "ACCEPTED"
            coverage = "complete" if accounting["intents"] and report["metrics"].get("coverage_status") == "complete" else "unknown"
        else:
            raw = backend.run(contract["task"]["objective"], cwd=prepared["cwd"],
                timeout=min(self._require_remaining(deadline), contract["budgets"]["turn_timeout_seconds"]),
                deadline=deadline, native_children=True,
                read_only=contract["task"]["read_only"], write_authorized=not contract["task"]["read_only"],
                allow_non_git=True, log_dir=root / "codex")
            outcome = {"completed": "completed", "timed_out": "timeout", "cancelled": "cancelled"}.get(raw["status"], "failed" if raw.get("outcome_known") else "unknown")
            if raw.get("process_tree_stopped") is False:
                outcome = "unknown"
            counts = raw.get("usage") or {}
            complete = all(type(counts.get(k)) is int and counts[k] >= 0 for k in ("input_tokens", "output_tokens"))
            usage = [{"invocation_id": execution_id, "scope": "individual_attempt", "phase": "initial",
                "status": "complete" if complete else "partial" if counts else "unavailable",
                "input_tokens": counts.get("input_tokens"), "output_tokens": counts.get("output_tokens")}]
            claimed, coverage = raw["status"] == "completed", "complete" if complete else "partial"
        # Durable accounting survives expiry or failed output inspection. A
        # missing result binding blocks grading, never erases observed usage.
        result_pin = None
        if self._remaining() > 0:
            try:
                result_pin = self._source_digest(prepared)
            except (HarnessError, OSError, ValueError):
                pass
        result = {"outcome": outcome, "claimed_accepted": claimed, "elapsed_seconds": time.monotonic() - started,
            "usage_coverage": coverage, "usage": usage, "result_snapshot_digest": result_pin}
        artifact = {"schema_version": 1, "manifest_digest": self.state["manifest_digest"], "trial_id": row["trial_id"],
            "execution_id": execution_id, "prepared_digest": digest(prepared), "result": result}
        self.store.write(result_path.relative_to(self.store.root).as_posix(), artifact, SOLVE_ARTIFACT, exclusive=True)
        if self.fault_controller is not None:
            try:
                self._require_remaining(deadline)
            except HarnessError as error:
                if not str(error).startswith("BENCH_DEADLINE_EXHAUSTED"):
                    raise
                # Publish the completed usage through the normal caller path;
                # an expired grant cannot authorize another fault action.
                return result
            self.fault_controller.after_solve(result_path)
        return result

    def _solve_artifact(self, row, prepared, execution_id):
        artifact = self.store.read("trials/" + row["trial_id"] + "/solve.yaml", SOLVE_ARTIFACT)
        expected = {"manifest_digest": self.state["manifest_digest"], "trial_id": row["trial_id"],
            "execution_id": execution_id, "prepared_digest": digest(prepared)}
        if any(artifact[key] != value for key, value in expected.items()):
            raise HarnessError("BENCH_SOLVE_ARTIFACT_BINDING_MISMATCH")
        return artifact["result"]

    def _reconcile_artifacts(self, records):
        """Recover billing before checking expiry/current editable output.

        A completion tied to the exact old invocation remains an observation
        even when the current source changed. It cannot authorize grading or
        dispatch; those separately revalidate the current bindings/deadline.
        """
        solved = {r["trial_id"] for r in records if r["kind"] == "solve"}
        for dispatch in records:
            trial_id = dispatch["trial_id"]
            if dispatch["kind"] != "dispatch" or trial_id in solved:
                continue
            path = "trials/" + trial_id + "/solve.yaml"
            if not self.store.path(path).exists():
                continue
            row = next(row for row in self.rows if row["trial_id"] == trial_id)
            prepared = next(r["payload"] for r in records if r["kind"] == "prepared" and r["trial_id"] == trial_id)
            result = self._solve_artifact(row, prepared, dispatch["payload"]["execution_id"])
            self._record("solve", trial_id, result, record_id=trial_id + "-solve")
        return self.records()

    def _grade(self, row, prepared, solve, records):
        self._contract(prepared)
        self._binding(prepared, solve["result_snapshot_digest"])
        trial_id = row["trial_id"]
        grade = next((r for r in records if r["kind"] == "grade" and r["trial_id"] == trial_id), None)
        if grade:
            return grade["payload"]
        intent = next((r for r in records if r["kind"] == "grade_intent" and r["trial_id"] == trial_id), None)
        if intent:
            raise HarnessError("BENCH_UNKNOWN_GRADE_OUTCOME: reconcile exact grading operation; no replay")
        suite = next(s for s in self.manifest["suites"] if s["id"] == row["suite"])
        relative = "trials/" + trial_id + "/grade-input.yaml"
        inputs = {"schema_version": 1, "manifest_digest": self.state["manifest_digest"], "trial_id": trial_id,
            "evaluator_digest": suite["evaluator"]["sha256"], "solve_digest": digest(solve),
            "cwd": prepared["cwd"], "solve_outcome": solve["outcome"]}
        self.store.write(relative, inputs, {"type": "object", "properties": {
            "schema_version": {"const": 1}, "manifest_digest": HASH, "trial_id": HASH, "evaluator_digest": HASH,
            "solve_digest": HASH, "cwd": SHORT, "solve_outcome": SHORT}, "required": list(inputs)}, exclusive=True)
        ref = {"path": relative, "sha256": hashlib.sha256(read_regular_file(self.store.path(relative))).hexdigest()}
        self._record("grade_intent", trial_id, {"execution_id": uuid.uuid4().hex, "solve_digest": digest(solve), "input_ref": ref}, record_id=trial_id + "-grade-intent")
        evaluator = evaluation.checked_ref(suite["evaluator"], self.state["assets_root"])
        evaluator_path = Path(self.state["assets_root"]) / suite["evaluator"]["path"]
        self._call_phase(self._binding, prepared, solve["result_snapshot_digest"])
        independent = getattr(self.adapter, "independent_grade", None)
        if independent is not None:
            if not callable(independent):
                raise HarnessError("BENCH_EVALUATOR_API_REQUIRED")
            # A callback is the sole dispatch path once selected. An unknown
            # external outcome must never fall back to a second argv run.
            grade = self._call_phase(independent, row, prepared, self.store.path(relative))
            validate(grade, evaluation.GRADE)
        else:
            argv = self._call_phase(self.adapter.evaluator_argv, row, prepared, self.store.path(relative))
            if not argv or str(evaluator_path) not in argv:
                raise HarnessError("BENCH_EVALUATOR_COMMAND_NOT_PINNED")
            result = self._capture(argv, 30)
            if result.returncode:
                raise HarnessError("BENCH_UNKNOWN_GRADE_OUTCOME: evaluator failed; preserve intent and diagnostics")
            grade = loads(result.stdout, evaluation.GRADE)
        if any(grade[key] != inputs[key] for key in ("manifest_digest", "trial_id", "evaluator_digest")):
            raise HarnessError("BENCH_GRADE_BINDING_MISMATCH")
        forced = {"timeout", "cancelled", "environment_error"}
        if solve["outcome"] in forced and grade["outcome"] != solve["outcome"]:
            raise HarnessError("BENCH_GRADE_ERASED_TERMINAL_FAILURE")
        self._call_phase(self._fresh_inputs)
        self._call_phase(self._binding, prepared, solve["result_snapshot_digest"])
        self._record("grade", trial_id, grade, record_id=trial_id + "-grade")
        return grade

    def advance(self):
        """One next frozen trial; failures remain in place and future IDs never shift."""
        with self.store.lease("benchmark cohort driver"):
            recovered = self.store.recover()
            if recovered["recovery_required"]:
                return self.status(blocker="BENCH_JOURNAL_RECOVERY_REQUIRED")
            records = self.records(repair=True)
            try:
                records = self._reconcile_artifacts(records)
            except (HarnessError, OSError, ValueError) as error:
                return self.status(blocker=str(error).split(":", 1)[0])
            done = {r["trial_id"] for r in records if r["kind"] == "trial"}
            if len(done) == len(self.rows):
                return self.status()
            if self._remaining() <= 0:
                return self.status(blocker="BENCH_DEADLINE_EXHAUSTED")
            try:
                qualification = self._qualify()
                bounds = [item["tokens"] for item in self.state["trial_token_bounds"]]
                if any(bound is None for bound in bounds):
                    return self.status(blocker="BENCH_COHORT_ESTIMATE_UNAVAILABLE")
                if sum(bounds) > self.state["authorization"]["max_total_tokens"]:
                    return self.status(blocker="BENCH_COHORT_EXCEEDS_APPROVED_TOTAL")
                row = next(row for row in self.rows if row["trial_id"] not in done)
                prepared = self._prepared(row, records)
                records = self.records()
                solved = next((r for r in records if r["kind"] == "solve" and r["trial_id"] == row["trial_id"]), None)
                dispatch = next((r for r in records if r["kind"] == "dispatch" and r["trial_id"] == row["trial_id"]), None)
                if solved is None:
                    if dispatch is not None:
                        # Only the exact already-published solve artifact may
                        # reconcile dispatch; no model/supervisor resume here.
                        path = "trials/" + row["trial_id"] + "/solve.yaml"
                        if not self.store.path(path).exists():
                            return self.status(blocker="BENCH_UNKNOWN_SOLVE_OUTCOME")
                        result = self._solve_artifact(row, prepared, dispatch["payload"]["execution_id"])
                    else:
                        usage = self._usage(records)
                        bound = prepared["token_upper_bound"]
                        if usage["unknown_trial_ids"]:
                            return self.status(blocker="BENCH_USAGE_COVERAGE_UNKNOWN")
                        if bound is None or bound < 0:
                            return self.status(blocker="BENCH_ADMISSION_UPPER_BOUND_UNAVAILABLE")
                        if usage["observed_tokens"] + bound > self.state["authorization"]["max_total_tokens"]:
                            return self.status(blocker="BENCH_APPROVED_TOTAL_BUDGET_EXHAUSTED")
                        self._call_phase(self._binding, prepared, prepared["input_snapshot_digest"])
                        self._require_remaining()
                        dispatch = self._record("dispatch", row["trial_id"], {"execution_id": uuid.uuid4().hex,
                            "prepared_digest": digest(prepared), "identity_digest": digest(self.state["identity"]),
                            "qualification_ref": qualification["record_id"], "reservation": bound}, record_id=row["trial_id"] + "-dispatch")
                        result = self._solve(row, prepared, dispatch["payload"]["execution_id"])
                    solved = self._record("solve", row["trial_id"], result, record_id=row["trial_id"] + "-solve")
                solve = solved["payload"]
                self._require_remaining()
                spent = sum(v for item in solve["usage"] for v in (item["input_tokens"], item["output_tokens"]) if v is not None)
                if spent > dispatch["payload"]["reservation"]:
                    return self.status(blocker="BENCH_ADAPTER_UPPER_BOUND_VIOLATED")
                if solve["outcome"] == "unknown":
                    return self.status(blocker="BENCH_UNKNOWN_SOLVE_OUTCOME")
                self._call_phase(self._binding, prepared, solve["result_snapshot_digest"])
                measured = next((r["payload"] for r in self.records() if r["kind"] == "measurements" and r["trial_id"] == row["trial_id"]), None)
                if measured is None:
                    # Nonessential telemetry cannot erase durable model usage.
                    measured = self._call_phase(self.adapter.measurements, row, prepared, solve)
                    if isinstance(measured, dict) and measured.get("schema_version") == 2:
                        benchmark_metrics.validate_measurements(measured, self.store.root,
                            trial_id=row["trial_id"], manifest_digest=self.state["manifest_digest"])
                    self._record("measurements", row["trial_id"], measured, record_id=row["trial_id"] + "-measurements")
                grade = self._grade(row, prepared, solve, self.records())
                grade_ref = "grades/" + row["trial_id"] + ".yaml"
                if not self.store.path(grade_ref).exists():
                    self.store.write(grade_ref, grade, evaluation.GRADE, exclusive=True)
                ref = {"path": grade_ref, "sha256": hashlib.sha256(read_regular_file(self.store.path(grade_ref))).hexdigest()}
                dispatched_at = datetime.fromisoformat(dispatch["created_at"].replace("Z", "+00:00"))
                elapsed = max(solve["elapsed_seconds"], (datetime.now(timezone.utc) - dispatched_at).total_seconds())
                trial = {"schema_version": 1, "manifest_digest": self.state["manifest_digest"], "measurement_kind": self.state["identity"]["measurement_kind"],
                    "trial_id": row["trial_id"], "outcome": grade["outcome"], "elapsed_seconds": elapsed,
                    "usage_coverage": solve["usage_coverage"], "usage": solve["usage"], "claimed_accepted": solve["claimed_accepted"],
                    "time_to_accepted_seconds": elapsed if grade["outcome"] == "accepted" else None,
                    "evaluator_receipt": ref, **measured}
                self._record("trial", row["trial_id"], trial, record_id=row["trial_id"] + "-trial")
                return self.status()
            except (HarnessError, OSError, ValueError) as error:
                return self.status(blocker=str(error).split(":", 1)[0])

    def run(self):
        while True:
            report = self.advance()
            if report["status"] in {"complete", "blocked"}:
                return report

    def status(self, *, blocker=None):
        records = self.records()
        trials = [r["payload"] for r in records if r["kind"] == "trial"]
        trial_ids = {row["trial_id"] for row in self.rows}
        if any(trial["trial_id"] not in trial_ids for trial in trials):
            raise HarnessError("BENCH_TRIAL_BINDING_MISMATCH")
        usage = self._usage(records)
        if len(trials) != len(self.rows) and self._remaining() <= 0:
            blocker = blocker or "BENCH_DEADLINE_EXHAUSTED"
        if len(trials) == len(self.rows):
            if usage["unknown_trial_ids"]:
                blocker = blocker or "BENCH_USAGE_COVERAGE_UNKNOWN"
            elif usage["observed_tokens"] > self.state["authorization"]["max_total_tokens"]:
                blocker = blocker or "BENCH_APPROVED_TOTAL_BUDGET_EXCEEDED"
        bounds = [item["tokens"] for item in self.state["trial_token_bounds"]]
        try:
            self._fresh_inputs()
            if trials:
                evaluation.summarize(self.manifest, trials, self.store.root)
        except (HarnessError, OSError, ValueError) as error:
            blocker = blocker or str(error).split(":", 1)[0]
        start = datetime.fromisoformat(self.state["created_at"].replace("Z", "+00:00"))
        end = max((datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) for r in records if r["kind"] == "trial"), default=datetime.now(timezone.utc)) if len(trials) == len(self.rows) else datetime.now(timezone.utc)
        report = {"schema_version": 1, "manifest_digest": self.state["manifest_digest"],
            "harness_build_digest": self.state["harness_build_digest"],
            "cohort_elapsed_seconds": max(0, (end - start).total_seconds()), "cohort_clock": "utc_elapsed_across_restarts",
            "measurement_kind": self.state["identity"]["measurement_kind"], "comparison": self.manifest["comparison"],
            "status": "blocked" if blocker else "complete" if len(trials) == len(self.rows) else "running",
            "reason_code": blocker, "planned_trials": len(self.rows), "completed_trials": len(trials),
            "pending_trial_ids": [row["trial_id"] for row in self.rows if row["trial_id"] not in {trial["trial_id"] for trial in trials}],
            "never_started_trial_ids": [row["trial_id"] for row in self.rows if row["trial_id"] not in {r["trial_id"] for r in records if r["kind"] == "dispatch"}],
            "unknown_outcome_trial_ids": [r["trial_id"] for r in records if r["kind"] == "dispatch" and not any(s["kind"] == "solve" and s["trial_id"] == r["trial_id"] and s["payload"]["outcome"] != "unknown" for s in records)],
            "pending_grade_trial_ids": [r["trial_id"] for r in records if r["kind"] == "solve" and not any(s["kind"] == "grade" and s["trial_id"] == r["trial_id"] for s in records)],
            "failed_trials": [trial["trial_id"] for trial in trials if trial["outcome"] != "accepted"],
            "usage": usage, "max_total_tokens": self.state["authorization"]["max_total_tokens"],
            "frozen_cohort_tokens_upper_bound": sum(bounds) if all(b is not None for b in bounds) else None,
            "budget_verified": blocker is None and len(trials) == len(self.rows) and usage["coverage"] == "complete" and usage["observed_tokens"] <= self.state["authorization"]["max_total_tokens"],
            "qualification_scope": "synthetic_interface_only" if self.state["identity"]["measurement_kind"] == "synthetic" else "unavailable"}
        return report

    def report(self):
        report = evaluation.summarize(self.manifest, [r["payload"] for r in self.records() if r["kind"] == "trial"], self.store.root)
        report["execution"] = self.status()
        return report
