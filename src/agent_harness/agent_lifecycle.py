"""On-demand model leases and isolated Git worktrees, without a daemon."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import subprocess
import time
import uuid

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
                    "child_identity": {**IDENTITY_SCHEMA, "type": ["object", "null"]}},
                "required": ["schema_version", "run_id", "task_id", "role", "identity", "created_at", "lease_id", "status", "child_identity"]}


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
        self.value.update(child_identity=identity, status="active")
        self.store.write(self.name, self.value, LEASE_SCHEMA)

    def complete(self):
        if identity_alive(self.value["child_identity"]):
            raise HarnessError("Cannot release model slot while the exact child is alive")
        self.completed = True

    def close(self):
        if self.completed or self.value["child_identity"] and not identity_alive(self.value["child_identity"]):
            self.value["status"] = "released"
            self.store.write(self.name, self.value, LEASE_SCHEMA)


class GlobalSlots:
    """All managed planners/workers/reviewers share five OS-backed leases."""
    def __init__(self, state_dir):
        from .supervisor_store import SupervisorStore
        self.store = SupervisorStore(Path(state_dir) / "global-model-slots")

    def inventory(self, run_id=None):
        rows = []
        for slot in range(5):
            name = "slot-" + str(slot) + ".yaml"
            if not self.store.path(name).exists():
                continue
            value = self.store.read(name, LEASE_SCHEMA)
            if run_id is not None and value["run_id"] != run_id:
                continue
            child = value["child_identity"]
            rows.append({"slot": slot, **value, "child_alive": identity_alive(child),
                         "unresolved": value["status"] != "released" and (child is None or identity_alive(child))})
        return rows

    @contextmanager
    def acquire(self, run_id, task_id, role, *, cancel_file=None, timeout=60):
        from .strict_yaml import dump
        start = time.monotonic()
        while True:
            if cancel_file and Path(cancel_file).exists():
                raise HarnessError("Cancelled while waiting for a global model slot")
            for slot in range(5):
                # Exclusive OS leases, no PID-only slot release or mutable active=false.
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
                            child = previous["child_identity"]
                            # A dead controller does not prove its model died.
                            # Missing child identity after a crash is uncertain.
                            if child is None or identity_alive(child):
                                context.__exit__(None, None, None)
                                continue
                except BaseException:
                    context.__exit__(None, None, None)
                    raise
                lease = {"schema_version": 1, "run_id": run_id, "task_id": task_id,
                         "role": role, "identity": process_identity(os.getpid()),
                         "created_at": now(), "lease_id": uuid.uuid4().hex,
                         "status": "reserved", "child_identity": None}
                ownership = ModelLease(self.store, name, lease)
                try:
                    self.store.write(name, lease, LEASE_SCHEMA)
                    yield ownership
                    return
                finally:
                    # Keep a historical ownership receipt. The guard, not stale YAML, is the lease.
                    try:
                        ownership.close()
                    finally:
                        context.__exit__(None, None, None)
            if time.monotonic() - start >= timeout:
                raise HarnessError("All five global model slots are busy")
            time.sleep(0.1)


def git(cwd, *args, input_bytes=None, timeout=30):
    result = subprocess.run(["git", *args], cwd=cwd, input=input_bytes,
                            capture_output=True, timeout=timeout, shell=False)
    if result.returncode:
        from .runner import redact
        raise HarnessError("Git operation failed: " + redact(result.stderr.decode("utf-8", errors="replace"))[:2048])
    return result.stdout


class Worktrees:
    def __init__(self, repository, directory):
        self.repository = Path(repository).resolve(strict=True)
        self.directory = Path(directory).resolve()
        if self.directory == self.repository or self.repository in self.directory.parents:
            raise HarnessError("Managed worktrees must be outside the source checkout")
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise HarnessError("Unsafe managed worktree directory")

    def create(self, task_id, base_revision, *, role="owner"):
        from .supervisor_store import SupervisorStore
        from .strict_yaml import dump
        if not task_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in task_id):
            raise HarnessError("Invalid worktree task ID")
        base = git(self.repository, "rev-parse", "--verify", base_revision + "^{commit}").decode().strip()
        path = self.directory / (task_id + "-" + uuid.uuid4().hex[:8])
        git(self.repository, "worktree", "add", "--detach", str(path), base)
        return {"schema_version": 1, "id": task_id, "path": str(path), "base_revision": base,
                "branch": None, "role": role, "ownership_token": uuid.uuid4().hex,
                "created_at": now()}

    def patch(self, record, allowed_scopes):
        path = Path(record["path"]).resolve(strict=True)
        if path.parent != self.directory or path.is_symlink():
            raise HarnessError("Worktree ownership boundary mismatch")
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
        if not patch:
            return {"status": "unchanged", "patch_digest": digest("")}
        target = Path(staging["path"]).resolve(strict=True)
        if target.parent != self.directory:
            raise HarnessError("Integration worktree ownership mismatch")
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
