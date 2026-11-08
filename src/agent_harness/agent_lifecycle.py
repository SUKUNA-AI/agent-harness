"""On-demand model leases and isolated Git worktrees, without a daemon."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from pathlib import Path
import os
import subprocess
import time
import uuid
import re

from .core import HarnessError, _os_lock, process_identity, identity_alive, now, digest
from .file_io import read_regular_file
from .orchestration import _within

IDENTITY_SCHEMA = {"type": "object", "additionalProperties": False,
                   "properties": {"pid": {"type": "integer"}, "created": {"type": "string"}},
                   "required": ["pid", "created"]}
LEASE_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"schema_version": {"const": 1}, "run_id": {"type": "string"},
                    "task_id": {"type": "string"}, "role": {"type": "string"},
                    "identity": IDENTITY_SCHEMA, "created_at": {"type": "string"},
                    "lease_id": {"type": "string"}, "status": {"enum": ["reserved", "active", "released"]},
                    "tree_cleanup_required": {"type": "boolean"},
                    "child_identity": {**IDENTITY_SCHEMA, "type": ["object", "null"]}},
                "required": ["schema_version", "run_id", "task_id", "role", "identity", "created_at", "lease_id", "status", "child_identity"]}

MAX_MANAGED_MODEL_SLOTS = 12
MAX_CONCURRENT_REVIEWERS = 3


class InvocationUnresolved(HarnessError):
    """A persisted invocation cannot authorize a dependent action or release."""


class SlotCapacityUnavailable(HarnessError):
    """The bounded global-slot scan found no available model lease."""


class SlotWaitCancelled(HarnessError):
    """A persisted cancellation stopped global-slot admission."""


def post_invocation_guard(intent, completion, accounting):
    """One strict receipt/accounting predicate for all managed v2 roles.

    A known failed invocation is resolved, never a successful task or plan.
    The caller's original bounded failure policy decides whether to retry it.
    """
    from .execution_receipts import validate_record
    try:
        validate_record(intent)
        validate_record(completion)
        for record in (intent, completion):
            if record["payload_digest"] != digest({key: value for key, value in record.items() if key != "payload_digest"}):
                raise InvocationUnresolved("Invocation receipt content digest is not exact")
        invocation = intent["invocation_id"]
        if (intent["record_type"] != "invocation_intent" or completion["record_type"] != "completion"
                or accounting["intents"].get(invocation) != intent
                or accounting["completions"].get(invocation) != completion
                or any(completion[key] != intent[key] for key in
                    ("project_id", "run_id", "task_id", "node_id", "attempt_id", "invocation_id", "plan_digest", "input_refs"))
                or completion["payload"]["intent_ref"] != intent["record_id"]):
            raise InvocationUnresolved("Invocation subject/accounting is not exact")
        value = completion["payload"]
        if value["outcome"] not in {"completed", "failed", "cancelled", "timed_out"} or value["process_tree_stopped"] is not True:
            raise InvocationUnresolved("Invocation terminal outcome or owned process-tree cleanup is unknown")
        if value["dispatch_state"] == "not_dispatched_proven":
            if (value["outcome"] == "completed" or value["turn_started"] is not False
                    or value["usage_status"] != "not_applicable"
                    or any(value[key] is not None for key in
                        ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"))):
                raise InvocationUnresolved("Contradictory no-dispatch invocation evidence")
        elif value["dispatch_state"] != "finished_observed" or value["usage_status"] == "not_applicable":
            raise InvocationUnresolved("Invocation dispatch/accounting outcome is unresolved")
        return value["outcome"]
    except InvocationUnresolved:
        raise
    except (HarnessError, ValueError, KeyError, TypeError) as error:
        raise InvocationUnresolved("Invalid invocation cleanup/subject evidence") from error


def guard_invocations(supervisor, *, accounting=None, node_ids=None):
    """Reconcile before admitting dependent work; other runs keep free slots."""
    accounting = accounting if accounting is not None else supervisor.receipts().reconcile_usage()
    if supervisor.contract["schema_version"] == 1:
        return accounting  # Preserve the frozen legacy admission policy.
    for invocation, intent in accounting["intents"].items():
        if node_ids is None or intent["node_id"] in node_ids:
            completion = accounting["completions"].get(invocation)
            if completion is None:
                raise InvocationUnresolved("Invocation may have dispatched without a completion: " + intent["node_id"])
            post_invocation_guard(intent, completion, accounting)
    return accounting


def invocation_timeout(supervisor, role, *, node_remaining=None):
    limits = [supervisor.remaining(), supervisor.contract["budgets"]["turn_timeout_seconds"]]
    if role == "planner":
        limits.append(300)
    if node_remaining is not None:
        limits.append(node_remaining)
    return min(limits)


class ModelLease:
    def __init__(self, store, name, value):
        self.store, self.name, self.value = store, name, value
        self.completed = False

    def bind(self, process):
        identity = process.get("identity", process)
        if not isinstance(identity, dict) or not identity_alive(identity):
            raise HarnessError("Cannot bind model slot without a live exact child identity")
        if self.value["child_identity"] is not None and self.value["child_identity"] != identity:
            raise HarnessError("A model lease cannot silently switch its child identity")
        self.value.update(child_identity=identity, status="active",
                          tree_cleanup_required=process.get("tree_control") in {
                              "private_process_group", "windows_suspended_job_kill_on_close"})
        self.store.write(self.name, self.value, LEASE_SCHEMA)

    def complete(self, result=None):
        if identity_alive(self.value["child_identity"]):
            raise HarnessError("Cannot release model slot while the exact child is alive")
        if self.value.get("tree_cleanup_required") and (not isinstance(result, dict) or result.get("process_tree_stopped") is not True):
            raise HarnessError("Cannot release model slot without confirmed owned process-tree cleanup")
        self.completed = True

    def close(self):
        if self.completed:
            self.value["status"] = "released"
            self.store.write(self.name, self.value, LEASE_SCHEMA)


class GlobalSlots:
    """Managed model invocations share 12 leases, with at most three reviewers."""
    def __init__(self, state_dir, *, create=True):
        from .supervisor_store import SupervisorStore
        self.directory = Path(state_dir) / "global-model-slots"
        self.store = SupervisorStore(self.directory) if create or self.directory.exists() else None

    def inventory(self, run_id=None):
        if self.store is None:
            return []
        rows = []
        for slot in range(MAX_MANAGED_MODEL_SLOTS):
            name = "slot-" + str(slot) + ".yaml"
            if not self.store.path(name).exists():
                continue
            value = self.store.read(name, LEASE_SCHEMA)
            if run_id is not None and value["run_id"] != run_id:
                continue
            child = value["child_identity"]
            rows.append({"slot": slot, **value, "child_alive": identity_alive(child),
                         "unresolved": value["status"] != "released"})
        return rows

    def reconcile(self, run_id, accounting):
        """Release only a receipt-proven finished invocation under its slot lock."""
        if self.store is None:
            return
        by_lease = {r["payload"]["owner_id"]: invocation for invocation, r in accounting["intents"].items()}
        for row in self.inventory(run_id):
            invocation = by_lease.get(row["lease_id"])
            completion = accounting["completions"].get(invocation)
            if not row["unresolved"] or completion is None or row["child_alive"]:
                continue
            try:
                post_invocation_guard(accounting["intents"][invocation], completion, accounting)
            except InvocationUnresolved:
                continue
            if row.get("tree_cleanup_required") and completion["payload"]["process_tree_stopped"] is not True:
                continue
            name = "slot-" + str(row["slot"]) + ".yaml"
            with _os_lock(self.store.root / ("slot-" + str(row["slot"]) + ".guard")):
                value = self.store.read(name, LEASE_SCHEMA)
                if value["lease_id"] != row["lease_id"] or identity_alive(value["child_identity"]):
                    raise HarnessError("Model slot changed during completion reconciliation")
                value["status"] = "released"
                self.store.write(name, value, LEASE_SCHEMA)

    def reconcile_root_reservation(self, proof):
        """Cancel only an exact journal-proven root binding before its intent."""
        from .root_preparation import RootReservationProof, fail
        if type(proof) is not RootReservationProof or self.store is None:
            fail('INTEGRITY', 'reservation_recovery', 'Exact root reservation authority/registry required')
        supervisor = proof.supervisor
        if self.directory != Path(supervisor.state_dir) / 'global-model-slots':
            fail('INTEGRITY', 'reservation_recovery', 'Root reservation registry differs')
        name = proof.bound['slot_name']
        with supervisor.mutex:
            proof.validate()
            prior = proof.authorization()
            observed = self.store.read(name, LEASE_SCHEMA)
            if prior is not None and observed['lease_id'] != proof.bound['lease']['lease_id']:
                # No foreign guard is needed: cancellation is irrevocable and
                # this path neither mutates nor reclaims the independent row.
                return proof.acknowledge(prior, 'reused')
            with _os_lock(self.directory / (Path(name).stem + '.guard')):
                row = self.store.read(name, LEASE_SCHEMA)
                stamp = self.store._journal_stamp(self.store.path(name))
                prior = proof.authorization()
                if prior is not None and row['lease_id'] != proof.bound['lease']['lease_id']:
                    # Durable cancellation already prohibited old dispatch.
                    # An independent run's reused row is never modified.
                    return proof.acknowledge(prior, 'reused')
                authorization = proof.authorize(row, stamp)
                expected = dict(proof.bound['lease'], status='released')
                if row == expected:
                    return proof.acknowledge(authorization, 'exact')
                if row != proof.bound['lease'] or list(stamp) != proof.bound['slot_identity']:
                    fail('INTEGRITY', 'reservation_recovery', 'Authorized old lease is active or changed')
                proof.validate()
                row['status'] = 'released'
                self.store.write(name, row, LEASE_SCHEMA)
                return proof.acknowledge(authorization, 'exact')

    @contextmanager
    def acquire(self, run_id, task_id, role, *, cancel_file=None, timeout=60):
        if self.store is None:
            from .supervisor_store import SupervisorStore
            self.store = SupervisorStore(self.directory)
        start = time.monotonic()
        reviewer_guard = self.store.root / "reviewer-admission.guard"
        while True:
            if cancel_file and Path(cancel_file).exists():
                raise SlotWaitCancelled("Cancelled while waiting for a global model slot")
            admission = nullcontext()
            if role == "reviewer":
                if reviewer_guard.is_symlink():
                    raise HarnessError("Unsafe reviewer admission guard")
                admission = _os_lock(reviewer_guard)
            selected = None
            try:
                # Serialize reviewer admission across *all* runs sharing this
                # state root. Hold this lock only while reserving a lease, not
                # during the model invocation.
                with admission:
                    if role != "reviewer" or sum(row["unresolved"] and row["role"] == "reviewer"
                                                       for row in self.inventory()) < MAX_CONCURRENT_REVIEWERS:
                        for slot in range(MAX_MANAGED_MODEL_SLOTS):
                            guard = self.store.root / ("slot-" + str(slot) + ".guard")
                            if guard.is_symlink():
                                raise HarnessError("Unsafe model slot guard")
                            context = _os_lock(guard)
                            try:
                                context.__enter__()
                            except HarnessError as error:
                                if "busy" in str(error).lower():
                                    continue
                                raise
                            name = "slot-" + str(slot) + ".yaml"
                            try:
                                if self.store.path(name).exists():
                                    previous = self.store.read(name, LEASE_SCHEMA)
                                    if previous["status"] != "released":
                                        # A dead process alone never proves that its
                                        # descendants stopped; retain the lease.
                                        context.__exit__(None, None, None)
                                        continue
                                lease = {"schema_version": 1, "run_id": run_id, "task_id": task_id,
                                         "role": role, "identity": process_identity(os.getpid()),
                                         "created_at": now(), "lease_id": uuid.uuid4().hex,
                                         "status": "reserved", "child_identity": None}
                                ownership = ModelLease(self.store, name, lease)
                                ownership.wait_seconds = time.monotonic() - start
                                self.store.write(name, lease, LEASE_SCHEMA)
                                selected = (ownership, context)
                                break
                            except BaseException:
                                context.__exit__(None, None, None)
                                raise
            except HarnessError as error:
                if "busy" not in str(error).lower():
                    raise
            if selected is not None:
                ownership, context = selected
                try:
                    yield ownership
                    return
                finally:
                    # Historical ownership receipts remain for recovery.
                    try:
                        ownership.close()
                    finally:
                        context.__exit__(None, None, None)
            if time.monotonic() - start >= timeout:
                raise SlotCapacityUnavailable("Managed model or reviewer capacity is exhausted")
            time.sleep(0.1)


def git(cwd, *args, input_bytes=None, timeout=30, env=None):
    result = subprocess.run(["git", *args], cwd=cwd, input=input_bytes,
                            capture_output=True, timeout=timeout, shell=False, env=env)
    if result.returncode:
        from .runner import redact
        raise HarnessError("Git operation failed: " + redact(result.stderr.decode("utf-8", errors="replace"))[:2048])
    return result.stdout


class Worktrees:
    def __init__(self, repository, directory, *, create=True):
        self.repository = Path(repository).resolve(strict=True)
        from .supervisor_store import _mkdir, _parents, _ordinary
        raw_directory = Path(os.path.abspath(directory))
        if create:
            _mkdir(raw_directory)
        _parents(raw_directory)
        _ordinary(raw_directory, directory=True)
        self.directory = raw_directory.resolve()
        if self.directory == self.repository or self.repository in self.directory.parents:
            raise HarnessError("Managed worktrees must be outside the source checkout")
        self.ownership = SupervisorWorktreeOwnership(self.directory.parent / "worktree-ownership", create=create)

    def create(self, task_id, base_revision, *, role="owner"):
        from .supervisor_store import SupervisorStore
        from .strict_yaml import dump
        if not task_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in task_id):
            raise HarnessError("Invalid worktree task ID")
        base = git(self.repository, "rev-parse", "--verify", base_revision + "^{commit}").decode().strip()
        path = self.directory / (task_id + "-" + uuid.uuid4().hex[:8])
        git(self.repository, "worktree", "add", "--detach", str(path), base)
        record = {"schema_version": 1, "id": task_id, "path": str(path), "base_revision": base,
                "branch": None, "role": role, "ownership_token": uuid.uuid4().hex,
                "created_at": now()}
        self.ownership.write(record)
        self.validate(record, expected_id=task_id, expected_role=role, integrated=role == "integrator")
        return record

    def validate(self, record, *, expected_id, expected_role, integrated=False):
        from .strict_yaml import validate
        from .supervisor_store import _parents, _ordinary
        validate(record, WORKTREE_SCHEMA)
        raw = Path(record["path"])
        if not raw.is_absolute():
            raise HarnessError("Worktree path must be absolute")
        _parents(raw)
        _ordinary(raw, directory=True)
        path = raw.resolve(strict=True)
        if path.parent != self.directory or not re.fullmatch(re.escape(expected_id) + r"-[0-9a-f]{8}", path.name):
            raise HarnessError("Worktree ownership boundary mismatch before model spawn")
        if record["id"] != expected_id or record["role"] != expected_role:
            raise HarnessError("Recovered worktree task/role binding mismatch")
        if self.ownership.read(record["ownership_token"]) != record:
            raise HarnessError("Recovered worktree differs from immutable ownership receipt")
        def common(root):
            value = Path(git(root, "rev-parse", "--git-common-dir").decode().strip())
            return (Path(root) / value).resolve() if not value.is_absolute() else value.resolve()
        if common(path) != common(self.repository):
            raise HarnessError("Recovered worktree belongs to another Git repository")
        if integrated:
            git(path, "merge-base", "--is-ancestor", record["base_revision"], "HEAD")
        elif git(path, "rev-parse", "HEAD").decode().strip() != record["base_revision"]:
            raise HarnessError("Worker changed its exact base revision")
        return path

    def patch(self, record, allowed_scopes):
        path = self.validate(record, expected_id=record["id"], expected_role=record["role"])
        if git(path, "rev-parse", "HEAD").decode().strip() != record["base_revision"]:
            raise HarnessError("Worker changed its base revision; proposed commits need explicit reconciliation")
        untracked = git(path, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        for raw in filter(None, untracked):
            name = raw.decode("utf-8")
            if not any(_within(name, scope) for scope in allowed_scopes):
                raise HarnessError("Untracked worker file outside authorized scope")
            if Path(name).name in {".env", "auth.json", "credentials.json"}:
                raise HarnessError("Secret artifact cannot be integrated")
            git(path, "add", "-N", "--", name)
        changed = git(path, "diff", "HEAD", "--name-only", "-z").split(b"\0")
        for raw in filter(None, changed):
            if not any(_within(raw.decode("utf-8"), scope) for scope in allowed_scopes):
                raise HarnessError("Worker patch exceeds authorized scope")
        return git(path, "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv")

    def integrate(self, staging, patch):
        target = self.validate(staging, expected_id="integration", expected_role="integrator", integrated=True)
        if not patch:
            return {"status": "unchanged", "patch_digest": digest("")}
        # Check before mutation. Conflict remains a task; no reset, force, or auto-overwrite.
        try:
            git(target, "apply", "--check", "--binary", "-", input_bytes=patch)
        except HarnessError:
            # A controller may have died after the exact local patch committed
            # to disk but before its journal checkpoint. Confirm the reverse
            # transformation without executing it; do not write the patch twice.
            git(target, "apply", "--reverse", "--check", "--binary", "-", input_bytes=patch)
            return {"status": "reconciled_already_applied", "patch_digest": __import__("hashlib").sha256(patch).hexdigest()}
        git(target, "apply", "--binary", "-", input_bytes=patch)
        return {"status": "integrated", "patch_digest": __import__("hashlib").sha256(patch).hexdigest()}

    def tree(self, staging, *, patch=None):
        """Compute exact current/expected Git tree using an isolated index.

        No mutation of the checkout/index or hooks is needed to prepare the
        postcondition. The temporary index is private and removed on failure.
        """
        import tempfile
        path = self.validate(staging, expected_id="integration", expected_role="integrator", integrated=True)
        with tempfile.TemporaryDirectory(prefix="integration-index-", dir=self.directory.parent) as temp:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / "index"))
            git(path, "read-tree", "HEAD", env=env)
            if patch is None:
                git(path, "add", "--all", env=env)
            elif patch:
                git(path, "apply", "--cached", "--binary", "-", input_bytes=patch, env=env)
            return git(path, "write-tree", env=env).decode().strip()


WORKTREE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "schema_version": {"const": 1}, "id": {"type": "string", "maxLength": 128},
    "path": {"type": "string", "maxLength": 16384}, "base_revision": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
    "branch": {"type": ["string", "null"]}, "role": {"enum": ["owner", "reviewer", "architect", "diagnostician", "integrator"]},
    "ownership_token": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    "created_at": {"type": "string", "format": "utc"}},
    "required": ["schema_version", "id", "path", "base_revision", "branch", "role", "ownership_token", "created_at"]}


class SupervisorWorktreeOwnership:
    def __init__(self, directory, *, create=True):
        from .supervisor_store import SupervisorStore
        self.store = SupervisorStore(directory, create=create)

    def write(self, record):
        self.store.write(record["ownership_token"] + ".yaml", record, WORKTREE_SCHEMA, exclusive=True)

    def read(self, token):
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise HarnessError("Invalid worktree ownership token")
        return self.store.read(token + ".yaml", WORKTREE_SCHEMA)
