"""Observable task milestones and durable fault handoff, without model retries.

The controller observes artifact predicates, not model call numbers. Claiming a
fault publishes intent before an external controller acts. An interrupted claim
is unknown until explicitly reconciled; this module never stops/resumes a model.
"""
from pathlib import Path
import uuid

from .benchmark_bundles import _deadline, _hash, _json, verify_materialized
from .core import HarnessError, digest
from . import evaluation, strict_yaml
from .execution_ledger import HASH, SHORT, object_schema
from .file_io import read_regular_file
from .supervisor_store import SupervisorStore, _ordinary
from .validation import json_equal

EVENT = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_milestone"},
    "kind": {"enum": ["observed", "fault_intent", "fault_completion"]}, "trial_id": HASH,
    "manifest_digest": HASH, "materialized_digest": HASH, "milestone_id": SHORT, "candidate_digest": HASH,
    "operation_id": {"type": ["string", "null"]}, "fault": SHORT,
    "outcome": {"enum": ["not_applicable", "applied", "not_applied", "unknown"]}},
    ("schema_version", "record_type", "kind", "trial_id", "manifest_digest", "materialized_digest", "milestone_id", "candidate_digest", "operation_id", "fault", "outcome"))


class MilestoneObserver:
    def __init__(self, receipt_path, state_dir):
        self.receipt_path = Path(receipt_path).absolute()
        self.receipt_digest = _hash(read_regular_file(self.receipt_path))
        self.value = verify_materialized(self.receipt_path)
        self.store = SupervisorStore(state_dir)
        # Controller state cannot be read/written by the solve task itself.
        if Path(self.value["source_root"]) in (self.store.root, *self.store.root.parents):
            raise HarnessError("BENCH_MILESTONE_STATE_INSIDE_SOLVE")

    def _candidate(self, *, deadline=None):
        if _hash(read_regular_file(self.receipt_path)) != self.receipt_digest:
            raise HarnessError("BENCH_MATERIALIZED_RECEIPT_CHANGED")
        verify_materialized(self.receipt_path, deadline=deadline)
        root = Path(self.value["source_root"])
        rows = []
        for path in sorted(root.rglob("*")):
            _deadline(deadline)
            _ordinary(path, directory=path.is_dir())
            if path.is_file():
                rows.append([path.relative_to(root).as_posix(), _hash(read_regular_file(path))])
        return digest(rows)

    def records(self):
        rows = []
        for event in self.store.events():
            value = event["payload"]
            strict_yaml.validate(value, EVENT)
            if any(value[key] != expected for key, expected in (("trial_id", self.value["trial_id"]), ("manifest_digest", self.value["manifest_digest"]), ("materialized_digest", self.receipt_digest))):
                raise HarnessError("BENCH_MILESTONE_SUBJECT_MISMATCH")
            rows.append(value)
        return rows

    def _record(self, kind, milestone, candidate, operation=None, outcome="not_applicable"):
        value = {"schema_version": 1, "record_type": "benchmark_milestone", "kind": kind,
            "trial_id": self.value["trial_id"], "manifest_digest": self.value["manifest_digest"],
            "materialized_digest": self.receipt_digest, "milestone_id": milestone["id"], "candidate_digest": candidate,
            "operation_id": operation, "fault": milestone["fault"], "outcome": outcome}
        strict_yaml.validate(value, EVENT)
        self.store.append_event(value)
        return value

    def observe(self, *, deadline=None):
        with self.store.lease("observe common task milestone"):
            candidate = self._candidate(deadline=deadline)
            done = {row["milestone_id"] for row in self.records() if row["kind"] == "observed"}
            milestone = next((item for item in self.value["milestones"] if item["id"] not in done), None)
            if milestone is None:
                return None
            _deadline(deadline)
            try:
                actual = _json(read_regular_file(Path(self.value["source_root"]) / milestone["artifact"]))
            except FileNotFoundError:
                return None
            expected = _json(evaluation.checked_ref(milestone["expected"], self.value["evaluator_root"]))
            if not json_equal(actual, expected):
                return None
            _deadline(deadline)
            return self._record("observed", milestone, candidate)

    def claim_fault(self, milestone_id, *, deadline=None):
        with self.store.lease("claim fault before controller action"):
            candidate = self._candidate(deadline=deadline)
            rows = self.records()
            if any(row["kind"] == "fault_intent" and row["milestone_id"] == milestone_id for row in rows):
                raise HarnessError("BENCH_FAULT_ALREADY_CLAIMED_RECONCILE_OUTCOME")
            observed = next((row for row in rows if row["kind"] == "observed" and row["milestone_id"] == milestone_id), None)
            if observed is None or observed["candidate_digest"] != candidate:
                raise HarnessError("BENCH_FAULT_MILESTONE_NOT_CURRENT")
            milestone = next(item for item in self.value["milestones"] if item["id"] == milestone_id)
            if milestone["fault"] == "observe":
                raise HarnessError("BENCH_NO_FAULT_AT_MILESTONE")
            _deadline(deadline)
            return self._record("fault_intent", milestone, candidate, uuid.uuid4().hex, "unknown")

    def reconcile_fault(self, operation_id, outcome, *, deadline=None):
        if outcome not in {"applied", "not_applied", "unknown"}:
            raise HarnessError("BENCH_INVALID_FAULT_OUTCOME")
        with self.store.lease("reconcile exact fault operation"):
            _deadline(deadline)
            rows = self.records()
            intent = next((row for row in rows if row["kind"] == "fault_intent" and row["operation_id"] == operation_id), None)
            if intent is None:
                raise HarnessError("BENCH_UNKNOWN_FAULT_OPERATION")
            existing = next((row for row in rows if row["kind"] == "fault_completion" and row["operation_id"] == operation_id), None)
            if existing:
                if existing["outcome"] != outcome:
                    raise HarnessError("BENCH_CONFLICTING_FAULT_OUTCOME")
                return existing
            milestone = next(item for item in self.value["milestones"] if item["id"] == intent["milestone_id"])
            return self._record("fault_completion", milestone, intent["candidate_digest"], operation_id, outcome)
