"""Cooperative, post-solve controller faults; never interrupt/retry a model.

The trial controller calls the two SolveFaultHook methods synchronously. Its
completed solve and accounting are durable before it enters a held barrier;
the hook cannot return until explicitly released. An external owner retains
the real controller Popen/job handles. Only that owner can apply the fault.
Restart reconciles the existing solve artifact, under the original grant. This
does not implement mid-task recovery or a native client compact protocol.
"""
from datetime import datetime, timezone
from contextlib import contextmanager, ExitStack
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import time
import uuid

from . import evaluation, strict_yaml
from .benchmark_milestones import MilestoneObserver
from .core import HarnessError, digest, identity_alive, process_identity
from .execution_ledger import HASH, SHORT, object_schema
from .file_io import read_regular_file
from .receipt_identity import IDENTITY_PROPERTIES, build_identity
from .runner import _start_contained, _terminate
from .supervisor_store import SupervisorStore

IDENTITY = object_schema({"pid": {"type": "integer", "minimum": 1}, "created": SHORT}, ("pid", "created"))
NULL_REF = {"anyOf": [evaluation.FILE_REF, {"type": "null"}]}
SCOPE = "cooperative_postsolve_controller_restart"
CRASH_EXIT_CODE = 86
CONFIG = object_schema({"schema_version": {"const": 1}, "scope": {"const": SCOPE},
    "materialized": evaluation.FILE_REF, "observer_dir": SHORT, "schedule_digest": HASH,
    "environment": NULL_REF}, ("schema_version", "scope", "materialized", "observer_dir", "schedule_digest", "environment"))
SUBJECT = object_schema({"schema_version": {"const": 1}, "config_digest": HASH,
    "manifest_digest": HASH, "trial_id": HASH, "execution_id": SHORT, "prepared_digest": HASH,
    "dispatch_digest": HASH, "root_policy_digest": HASH, "child_policy_digest": HASH,
    "deadline": {"type": "string", "format": "utc"},
    "producer": object_schema({"producer_id": {"const": "benchmark-fault-controller"},
        **{name: IDENTITY_PROPERTIES[name] for name in ("harness_version", "harness_build_digest", "build_provenance")},
        "client_version": {"type": "null"}, "capability_receipt_ref": {"type": "null"}},
        ("producer_id", "harness_version", "harness_build_digest", "build_provenance", "client_version", "capability_receipt_ref"))},
    ("schema_version", "config_digest", "manifest_digest", "trial_id", "execution_id", "prepared_digest",
     "dispatch_digest", "root_policy_digest", "child_policy_digest", "deadline", "producer"))
_COMMON = {"schema_version": {"const": 1}, "subject_digest": HASH}
_FIELDS = {
    "owner": {"identity": IDENTITY},
    "held": {"identity": IDENTITY, "solve": evaluation.FILE_REF, "accounting": evaluation.FILE_REF, "accounting_digest": HASH},
    "intent": {"operation_id": SHORT, "milestone_id": SHORT, "observer_intent_digest": HASH,
               "identity": IDENTITY, "solve": evaluation.FILE_REF},
    "stopped": {"operation_id": SHORT, "identity": IDENTITY},
    "crash_requested": {"operation_id": SHORT, "identity": IDENTITY},
    "exit_observed": {"operation_id": SHORT, "identity": IDENTITY, "exit_code": {"const": CRASH_EXIT_CODE}},
    "released": {"solve": evaluation.FILE_REF},
    "restart_authorized": {"operation_id": SHORT, "solve": evaluation.FILE_REF},
    "restart_launch": {"operation_id": SHORT, "launch_id": SHORT, "argv_digest": HASH, "cwd": SHORT},
    "restart_bound": {"launch_id": SHORT, "identity": IDENTITY},
}
EVENTS = {kind: object_schema({**_COMMON, "kind": {"const": kind}, **fields},
    (*_COMMON, "kind", *fields)) for kind, fields in _FIELDS.items()}
ACCOUNTING = object_schema({"schema_version": {"const": 1}, "subject": SUBJECT,
    "solve": evaluation.FILE_REF, "accounting_digest": HASH, "scope": {"const": SCOPE},
    "usage": {"type": "array", "items": evaluation.USAGE, "minItems": 1, "maxItems": 10000},
    "midtask_resume": {"const": "unsupported"}, "client_compact": {"const": "unsupported"}},
    ("schema_version", "subject", "solve", "accounting_digest", "scope", "usage", "midtask_resume", "client_compact"))


def file_ref(path):
    """Pin an ordinary local artifact; refs contain no contents or prompts."""
    path = Path(path).absolute()
    return {"path": str(path), "sha256": hashlib.sha256(read_regular_file(path)).hexdigest()}


def _check_ref(ref):
    strict_yaml.validate(ref, evaluation.FILE_REF)
    if not Path(ref["path"]).is_absolute() or file_ref(ref["path"]) != ref:
        raise HarnessError("BENCH_FAULT_ARTIFACT_CHANGED")
    return Path(ref["path"])


def _require_deadline(deadline):
    remaining = (datetime.fromisoformat(deadline.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise HarnessError("BENCH_FAULT_ORIGINAL_DEADLINE_EXHAUSTED")
    return remaining


@contextmanager
def _store_lease(store, purpose):
    """Wait boundedly for a guard; never retry an entered transaction/read.

    SupervisorStore's OS lock deliberately fails fast. Controller polling and
    parent appends are cooperating operations, so serialize their snapshots.
    Only a proven nonacquired guard may be attempted again. Stale owner leases,
    malformed bytes and any error after entry remain failures without retry.
    """
    end = time.monotonic() + 5
    with store._mutex, ExitStack() as stack:
        while True:
            try:
                stack.enter_context(store.lease(purpose))
                break
            except HarnessError as error:
                cause = error.__cause__
                if not (str(error).startswith("Resource busy:") and isinstance(cause, OSError)
                        and cause.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}):
                    raise
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(.01, remaining))
        yield


class SolveFaultHook:
    """Optional BenchmarkRunner hook, constructed in its owned trial process.

    The materialization/schedule and optional arm environment receipt are frozen
    before dispatch. The current implementation uses the existing custom-bundle
    MilestoneObserver protocol; upstream schedule protocols are not inferred.
    """
    def __init__(self, controller_dir, materialized_receipt=None, observer_dir=None, *, environment_receipt=None):
        self.store = SupervisorStore(controller_dir)
        if materialized_receipt is not None:
            observer = MilestoneObserver(materialized_receipt, observer_dir or self.store.root / "observer")
            config = {"schema_version": 1, "scope": SCOPE,
                "materialized": file_ref(materialized_receipt), "observer_dir": str(observer.store.root),
                "schedule_digest": digest(observer.value["milestones"]),
                "environment": file_ref(environment_receipt) if environment_receipt else None}
            with self._lease("freeze post-solve fault configuration"):
                if self.store.path("config.yaml").exists():
                    if self.store.read("config.yaml", CONFIG) != config:
                        raise HarnessError("BENCH_FAULT_CONFIG_CONFLICT")
                else:
                    self.store.write("config.yaml", config, CONFIG, exclusive=True)
        # Exclusive publication briefly has two hard links until its stage is
        # removed. Readers must share the publisher's guard so that strict
        # file identity checks never observe that unfinished transaction.
        with self._lease("read frozen fault configuration"):
            self.config = self.store.read("config.yaml", CONFIG)
            self._verify_config()

    def _verify_config(self):
        _check_ref(self.config["materialized"])
        if self.config["environment"] is not None:
            _check_ref(self.config["environment"])
        observer = self.observer()
        source = Path(observer.value["source_root"])
        if source in (self.store.root, *self.store.root.parents):
            raise HarnessError("BENCH_FAULT_STATE_INSIDE_SOLVE")
        if digest(observer.value["milestones"]) != self.config["schedule_digest"]:
            raise HarnessError("BENCH_FAULT_SCHEDULE_CHANGED")

    def observer(self):
        return MilestoneObserver(self.config["materialized"]["path"], self.config["observer_dir"])

    def _lease(self, purpose):
        return _store_lease(self.store, purpose)

    def before_solve(self, *, manifest_digest, trial_id, execution_id, prepared, dispatch_digest, deadline):
        # Prepared is supplied from the runner's already-frozen, validated row.
        from .benchmark_runner import PREPARED
        strict_yaml.validate(prepared, PREPARED)
        self._verify_config()
        value = self.observer().value
        if value["manifest_digest"] != manifest_digest or value["trial_id"] != trial_id or prepared["trial_id"] != trial_id:
            raise HarnessError("BENCH_FAULT_SUBJECT_MISMATCH")
        subject = {"schema_version": 1, "config_digest": digest(self.config), "manifest_digest": manifest_digest,
            "trial_id": trial_id, "execution_id": execution_id, "prepared_digest": digest(prepared),
            "dispatch_digest": dispatch_digest, "root_policy_digest": prepared["root_policy_digest"],
            "child_policy_digest": prepared["child_policy_digest"], "deadline": deadline,
            "producer": {"producer_id": "benchmark-fault-controller", **build_identity(__name__),
                         "client_version": None, "capability_receipt_ref": None}}
        strict_yaml.validate(subject, SUBJECT)
        self._deadline(subject)
        identity = process_identity(os.getpid())
        if identity is None or identity["created"] == "unknown":
            raise HarnessError("BENCH_FAULT_CONTROLLER_IDENTITY_UNAVAILABLE")
        with self._lease("bind original dispatch to controller"):
            self._deadline(subject)
            if self.store.path("subject.yaml").exists():
                if self.subject() != subject:
                    raise HarnessError("BENCH_FAULT_SUBJECT_CONFLICT")
                owners = [row for row in self._records_locked() if row["kind"] == "owner"]
                if not owners or owners[-1]["identity"] != identity:
                    raise HarnessError("BENCH_FAULT_DISPATCH_ALREADY_BOUND")
                return
            self.store.write("subject.yaml", subject, SUBJECT, exclusive=True)
            self._append("owner", identity=identity)

    def subject(self):
        subject = self.store.read("subject.yaml", SUBJECT)
        if subject["config_digest"] != digest(self.config):
            raise HarnessError("BENCH_FAULT_CONFIG_CONFLICT")
        return subject

    def _deadline(self, subject=None):
        subject = subject or self.subject()
        return _require_deadline(subject["deadline"])

    def records(self):
        # A strict read must see a whole journal generation. The store's thread
        # mutex alone cannot serialize legitimate appends by another process.
        with self._lease("snapshot controller journal"):
            return self._records_locked()

    def _records_locked(self):
        """Caller holds this store's cross-process lease; never reacquire it."""
        subject_digest = digest(self.subject())
        rows = []
        for event in self.store.events():
            row = event["payload"]
            if row.get("kind") not in EVENTS:
                raise HarnessError("BENCH_FAULT_UNKNOWN_RECORD")
            strict_yaml.validate(row, EVENTS[row["kind"]])
            if row["subject_digest"] != subject_digest:
                raise HarnessError("BENCH_FAULT_SUBJECT_CONFLICT")
            rows.append(row)
        return rows

    def _observer_records(self):
        observer = self.observer()
        with _store_lease(observer.store, "snapshot fault milestone journal"):
            return observer.records()

    def _append(self, kind, **fields):
        row = {"schema_version": 1, "subject_digest": digest(self.subject()), "kind": kind, **fields}
        strict_yaml.validate(row, EVENTS[kind])
        self.store.append_event(row)
        return row

    def _solve(self, ref):
        from .benchmark_runner import SOLVE_ARTIFACT
        path = _check_ref(ref)
        value = strict_yaml.load(path, SOLVE_ARTIFACT)
        subject = self.subject()
        if any(value[key] != subject[key] for key in ("manifest_digest", "trial_id", "execution_id", "prepared_digest")):
            raise HarnessError("BENCH_FAULT_SOLVE_SUBJECT_MISMATCH")
        result = value["result"]
        if result["outcome"] != "completed" or result["usage_coverage"] != "complete" or not result["usage"] or any(
                item["status"] != "complete" or any(type(item[key]) is not int for key in ("input_tokens", "output_tokens"))
                for item in result["usage"]):
            raise HarnessError("BENCH_FAULT_UNKNOWN_SOLVE_OR_ACCOUNTING")
        ids = [item["invocation_id"] for item in result["usage"]]
        if len(ids) != len(set(ids)):
            raise HarnessError("BENCH_FAULT_DUPLICATE_USAGE_IDENTITY")
        return value

    def after_solve(self, result_path):
        """Held synchronous post-solve barrier; caller must not dispatch in parallel."""
        self._deadline()
        self._verify_config()
        ref = file_ref(result_path)
        value = self._solve(ref)
        identity = process_identity(os.getpid())
        with self._lease("hold after durable terminal solve accounting"):
            self._deadline()
            rows = self._records_locked()
            owner = next(row for row in rows if row["kind"] == "owner")
            if owner["identity"] != identity:
                raise HarnessError("BENCH_FAULT_FOREIGN_CONTROLLER")
            held = next((row for row in rows if row["kind"] == "held"), None)
            if held is None:
                accounting = {"schema_version": 1, "subject": self.subject(), "solve": ref,
                    "accounting_digest": digest(value["result"]["usage"]), "scope": SCOPE,
                    "usage": value["result"]["usage"], "midtask_resume": "unsupported", "client_compact": "unsupported"}
                self.store.write("completion-accounting.yaml", accounting, ACCOUNTING, exclusive=True)
                self._append("held", identity=identity, solve=ref, accounting=file_ref(self.store.path("completion-accounting.yaml")),
                             accounting_digest=digest(value["result"]["usage"]))
            elif held["solve"] != ref:
                raise HarnessError("BENCH_FAULT_SOLVE_CHANGED")
        # No callback into the runner occurs while held. Only the external
        # owner can publish a release; fault intent permanently disables it.
        while True:
            self._deadline()
            rows = self.records()
            crash = next((row for row in rows if row["kind"] == "crash_requested"), None)
            if crash is not None:
                intent = self._intent(crash["operation_id"])
                if crash["identity"] != identity or intent["identity"] != identity:
                    raise HarnessError("BENCH_FAULT_FOREIGN_CONTROLLER")
                self._deadline()
                # The externally requested abrupt exit is the supported fault.
                # No model is active; skip Python finally/normal solve return.
                os._exit(CRASH_EXIT_CODE)
            if any(row["kind"] == "released" for row in rows):
                return
            time.sleep(min(0.025, self._deadline()))

    def held(self):
        with self._lease("snapshot held controller checkpoint"):
            return self._held_locked()

    def _held_locked(self):
        self._verify_config()
        rows = self._records_locked()
        value = next((row for row in rows if row["kind"] == "held"), None)
        if value is not None:
            result = self._solve(value["solve"])
            if value["accounting_digest"] != digest(result["result"]["usage"]):
                raise HarnessError("BENCH_FAULT_ACCOUNTING_CHANGED")
            accounting = strict_yaml.load(_check_ref(value["accounting"]), ACCOUNTING)
            if accounting["subject"] != self.subject() or accounting["solve"] != value["solve"] or accounting["usage"] != result["result"]["usage"] or accounting["accounting_digest"] != value["accounting_digest"]:
                raise HarnessError("BENCH_FAULT_ACCOUNTING_CHANGED")
        return value

    def release(self):
        with self._lease("release without injecting controller fault"):
            held = self._held_locked()
            if held is None or any(row["kind"] == "intent" for row in self._records_locked()):
                raise HarnessError("BENCH_FAULT_BARRIER_NOT_RELEASABLE")
            self._deadline()
            if not any(row["kind"] == "released" for row in self._records_locked()):
                self._append("released", solve=held["solve"])

    def claim(self, milestone_id):
        """Durable intent after the observed predicate and held solve agree."""
        with self._lease("claim exact held controller fault"):
            self._deadline()
            held = self._held_locked()
            rows = self._records_locked()
            if held is None or any(row["kind"] in {"released", "intent"} for row in rows):
                raise HarnessError("BENCH_FAULT_BARRIER_ALREADY_CLAIMED_OR_UNAVAILABLE")
            if not identity_alive(held["identity"]):
                raise HarnessError("BENCH_FAULT_CONTROLLER_STOPPED_BEFORE_INTENT")
            observer = self.observer()
            milestone = next((item for item in observer.value["milestones"] if item["id"] == milestone_id), None)
            if milestone is None or milestone["fault"] not in {"controller_restart", "after_durable_checkpoint"}:
                raise HarnessError("BENCH_FAULT_UNSUPPORTED_OPERATION")
            # Persisted observer intent can precede this store's append if the
            # controller crashes. Reconcile that exact claim, never claim twice.
            old = next((row for row in self._observer_records() if row["kind"] == "fault_intent" and row["milestone_id"] == milestone_id), None)
            intent = old or observer.claim_fault(milestone_id, deadline=self.subject()["deadline"])
            if intent["candidate_digest"] != observer._candidate(deadline=self.subject()["deadline"]):
                raise HarnessError("BENCH_FAULT_MILESTONE_CHANGED")
            self._deadline()
            return self._append("intent", operation_id=intent["operation_id"], milestone_id=milestone_id,
                observer_intent_digest=digest(intent), identity=held["identity"], solve=held["solve"])

    def _intent(self, operation_id):
        with self._lease("snapshot exact controller fault intent"):
            return self._intent_locked(operation_id)

    def _intent_locked(self, operation_id):
        intent = next((row for row in self._records_locked() if row["kind"] == "intent" and row["operation_id"] == operation_id), None)
        if intent is None:
            raise HarnessError("BENCH_FAULT_UNKNOWN_OPERATION")
        held = self._held_locked()
        if held is None or held["solve"] != intent["solve"] or held["identity"] != intent["identity"]:
            raise HarnessError("BENCH_FAULT_BARRIER_CHANGED")
        observer_intent = next((row for row in self._observer_records() if row["kind"] == "fault_intent" and row["operation_id"] == operation_id), None)
        if observer_intent is None or digest(observer_intent) != intent["observer_intent_digest"]:
            raise HarnessError("BENCH_FAULT_OBSERVER_INTENT_CHANGED")
        return intent

    def reconcile_stopped(self, operation_id):
        """Reconcile lost publication using a retained-handle exit observation.

        A dead PID alone cannot prove the requested fault was applied. If the
        external owner died before saving its actual exit observation, retain
        unknown rather than manufacturing the missing exit code.
        """
        with self._lease("reconcile controller stop before restart"):
            intent = self._intent_locked(operation_id)
            if identity_alive(intent["identity"]):
                raise HarnessError("BENCH_FAULT_CONTROLLER_STILL_LIVE")
            rows = self._records_locked()
            if not any(row["kind"] == "crash_requested" and row["operation_id"] == operation_id for row in rows) or not any(
                    row["kind"] == "exit_observed" and row["operation_id"] == operation_id and row["identity"] == intent["identity"] for row in rows):
                raise HarnessError("BENCH_FAULT_EXIT_OBSERVATION_UNAVAILABLE")
            existing = next((row for row in self._records_locked() if row["kind"] == "stopped" and row["operation_id"] == operation_id), None)
            result = existing or self._append("stopped", operation_id=operation_id, identity=intent["identity"])
            self.observer().reconcile_fault(operation_id, "applied")
            return result

    def authorize_restart(self, operation_id):
        with self._lease("authorize same-dispatch artifact reconciliation"):
            self._deadline()
            intent = self._intent_locked(operation_id)
            rows = self._records_locked()
            if identity_alive(intent["identity"]) or not any(row["kind"] == "stopped" and row["operation_id"] == operation_id for row in rows):
                raise HarnessError("BENCH_FAULT_STOP_NOT_CONFIRMED")
            if not any(row["kind"] == "fault_completion" and row["operation_id"] == operation_id and row["outcome"] == "applied"
                       for row in self._observer_records()):
                raise HarnessError("BENCH_FAULT_OBSERVER_COMPLETION_MISSING")
            existing = next((row for row in rows if row["kind"] == "restart_authorized"), None)
            return existing or self._append("restart_authorized", operation_id=operation_id, solve=intent["solve"])

    def claim_restart_launch(self, operation_id, argv, cwd):
        self.authorize_restart(operation_id)
        with self._lease("durable same-controller restart launch intent"):
            self._deadline()
            if any(row["kind"] == "restart_launch" for row in self._records_locked()):
                raise HarnessError("BENCH_FAULT_RESTART_LAUNCH_ALREADY_CLAIMED_RECONCILE")
            return self._append("restart_launch", operation_id=operation_id, launch_id=uuid.uuid4().hex,
                                argv_digest=digest(list(argv)), cwd=str(Path(cwd).absolute()))

    def completion_accounting(self):
        """Exact dispatch completion artifact for the runner's existing reconcile."""
        held = self.held()
        if held is None:
            raise HarnessError("BENCH_FAULT_NO_DURABLE_SOLVE")
        value = self.store.read("completion-accounting.yaml", ACCOUNTING)
        result = self._solve(held["solve"])["result"]
        if value["subject"] != self.subject() or value["solve"] != held["solve"] or value["usage"] != result["usage"] or value["accounting_digest"] != held["accounting_digest"]:
            raise HarnessError("BENCH_FAULT_ACCOUNTING_CHANGED")
        return value

    def accounting_ref(self):
        self.completion_accounting()
        return self.held()["accounting"]


class OwnedController:
    """Actual contained subprocess, addressed through retained handles only."""
    def __init__(self, argv, *, cwd, deadline=None):
        self.argv, self.cwd = tuple(argv), str(Path(cwd).absolute())
        launch_argv = list(self.argv)
        if deadline is not None:
            _require_deadline(deadline)
        self.process, self.job = _start_contained(launch_argv, self.cwd)
        self.identity = process_identity(self.process.pid)
        if self.identity is None or self.identity["created"] == "unknown":
            self.close()
            raise HarnessError("BENCH_FAULT_CONTROLLER_IDENTITY_UNAVAILABLE")

    def wait_held(self, controller_dir, *, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                controller = SolveFaultHook(controller_dir)
                held = controller.held()
                if held:
                    if held["identity"] != self.identity:
                        raise HarnessError("BENCH_FAULT_FOREIGN_CONTROLLER")
                    return controller
            except FileNotFoundError:
                pass
            if self.process.poll() is not None:
                raise HarnessError("BENCH_FAULT_CONTROLLER_EXITED_BEFORE_BARRIER")
            time.sleep(0.025)
        raise HarnessError("BENCH_FAULT_BARRIER_TIMEOUT")

    def interrupt(self, controller, operation_id):
        with controller._lease("validate exact owned interrupt"):
            intent = controller._intent_locked(operation_id)
            if intent["identity"] != self.identity:
                raise HarnessError("BENCH_FAULT_FOREIGN_CONTROLLER")
            if self.process.poll() is None and process_identity(self.process.pid) != self.identity:
                raise HarnessError("BENCH_FAULT_CONTROLLER_IDENTITY_CHANGED")
            requested = any(row["kind"] == "crash_requested" and row["operation_id"] == operation_id for row in controller._records_locked())
            if not requested:
                controller._deadline()
                if self.process.poll() is not None:
                    raise HarnessError("BENCH_FAULT_EXIT_BEFORE_CRASH_REQUEST")
                controller._append("crash_requested", operation_id=operation_id, identity=self.identity)
        # Child self-exits from the held barrier. There is no numeric PID/group
        # signal and no opportunity to dispatch between the request and exit.
        try:
            code = self.process.poll()
            if code is None:
                code = self.process.wait(timeout=min(5, controller._deadline()))
        except subprocess.TimeoutExpired as error:
            raise HarnessError("BENCH_FAULT_CRASH_OUTCOME_UNKNOWN") from error
        if code != CRASH_EXIT_CODE:
            raise HarnessError("BENCH_FAULT_UNEXPECTED_CONTROLLER_EXIT")
        self.job.close()
        with controller._lease("persist actual owned-controller exit code"):
            if not any(row["kind"] == "exit_observed" and row["operation_id"] == operation_id for row in controller._records_locked()):
                controller._append("exit_observed", operation_id=operation_id, identity=self.identity, exit_code=code)
        return controller.reconcile_stopped(operation_id)

    def restart(self, controller, operation_id):
        if controller._intent(operation_id)["identity"] != self.identity:
            raise HarnessError("BENCH_FAULT_FOREIGN_CONTROLLER")
        if self.process.poll() is None:
            raise HarnessError("BENCH_FAULT_CONTROLLER_STILL_LIVE")
        intent = controller.claim_restart_launch(operation_id, self.argv, self.cwd)
        child = type(self)(self.argv, cwd=self.cwd, deadline=controller.subject()["deadline"])
        try:
            with controller._lease("record actual restarted controller identity"):
                controller._append("restart_bound", launch_id=intent["launch_id"], identity=child.identity)
        except BaseException:
            child.close()
            raise
        return child

    def wait(self, *, timeout):
        return self.process.communicate(timeout=timeout)

    def close(self):
        try:
            if self.process.poll() is None:
                _terminate(self.process, self.job)
        finally:
            self.job.close()
            for stream in (self.process.stdout, self.process.stderr):
                if stream and not stream.closed:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
