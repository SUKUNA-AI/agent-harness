"""Content-addressed contracts, workspace snapshots and recoverable run state.

The locks coordinate cooperating harness processes. They are not an OS sandbox or
a protection against a malicious agent with the same user account.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import ctypes
import errno
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid

SCHEMA_VERSION = 1
STATES = {"planned", "running", "verifying", "ready_for_review", "accepted", "blocked", "failed", "cancelled", "integrated"}
WORKFLOWS = {"answer", "research", "plan", "review", "diagnostics", "change", "performance", "document", "learning", "operations", "monitor"}
KINDS = {"command", "source", "content", "visual", "runtime"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class HarnessError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def remaining_budget_seconds(contract):
    deadline = contract.get("budget", {}).get("deadline")
    if not deadline:
        return None
    try:
        value = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("timezone missing")
    except (ValueError, TypeError, AttributeError):
        raise HarnessError("budget.deadline must be an ISO timestamp with timezone")
    return (value - datetime.now(timezone.utc)).total_seconds()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    # A regular path may have become a FIFO since the caller inspected it.
    # Nonblocking open permits descriptor validation without waiting for a writer.
    with os.fdopen(os.open(path, flags), "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise HarnessError("Hash input must be a regular file: " + str(path))
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical(value) + b"\n"
    if exclusive:
        with path.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        return
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def process_identity(pid):
    """PID plus process creation identity, never PID alone. None means not alive."""
    try:
        if os.name == "nt":
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenProcess.restype = wintypes.HANDLE
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            handle = k.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            try:
                creation, exit_time, kernel, user = [wintypes.FILETIME() for _ in range(4)]
                if not k.GetProcessTimes(wintypes.HANDLE(handle), ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                exit_code = wintypes.DWORD()
                if not k.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(exit_code)) or exit_code.value != 259:
                    return None
                return {"pid": int(pid), "created": f"{creation.dwHighDateTime}:{creation.dwLowDateTime}"}
            finally:
                k.CloseHandle(wintypes.HANDLE(handle))
        pid = int(pid)
        if pid <= 0:
            return None
        # A bind-mounted /proc can describe a different PID namespace. ESRCH in
        # our namespace wins over a stale or unrelated /proc/<pid>/stat entry.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass  # EPERM proves existence; do not reclaim a live owner's lease.
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            try:
                tail = stat.read_text().rsplit(")", 1)[1].split()
            except PermissionError:
                return {"pid": pid, "created": "unknown"}
            if tail[0] == "Z":
                return None
            return {"pid": int(pid), "created": tail[19]}
        # Portable fallback is conservative: cannot distinguish reused PIDs.
        return {"pid": pid, "created": "unknown"}
    except (OSError, ValueError, IndexError):
        return None


def identity_alive(identity):
    current = process_identity(identity["pid"]) if identity else None
    return bool(current and (current == identity or current["created"] == "unknown" or identity.get("created") == "unknown"))


@contextmanager
def _os_lock(path):
    """Hold a stable kernel lock; never unlink its anchor or replace its inode."""
    # Locking the JSON lease itself would fail when stale recovery replaces it:
    # another process could then lock a different inode at the same path.
    with Path(path).open("a+b") as guard:
        try:
            if os.name == "nt":
                import msvcrt
                guard.seek(0)
                # Byte-range locks may extend past EOF; no racy initialization write.
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            elif os.name == "posix":
                import fcntl
                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise HarnessError(f"OS file locking is unsupported: {os.name}")
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise HarnessError(f"Resource busy: {path}") from exc
            raise HarnessError(f"Cannot acquire OS lock at {path}: {exc}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                guard.seek(0)
                msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


@contextmanager
def lock(path, *, purpose="state"):
    """Serialize recovery and ownership; keep legacy leases and stale receipts."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The anchor survives normal release and crashes. Its OS lock is released
    # on descriptor close/process exit, without a second stale-recovery race.
    with _os_lock(path.with_name(path.name + ".guard")):
        token = uuid.uuid4().hex
        record = {"identity": process_identity(os.getpid()), "token": token, "purpose": purpose, "created_at": now()}
        for _ in range(3):
            try:
                atomic_json(path, record, exclusive=True)
                break
            except FileExistsError:
                try:
                    old = read_json(path)
                    if not isinstance(old, dict):
                        raise ValueError("Lease must be an object")
                    identity = old.get("identity")
                    if identity and (not isinstance(identity, dict) or type(identity.get("pid")) is not int or identity["pid"] <= 0 or not isinstance(identity.get("created"), str) or not identity["created"]):
                        raise ValueError("Invalid lease identity")
                except (ValueError, OSError):
                    raise HarnessError(f"Unreadable lock; inspect it rather than removing blindly: {path}")
                if not identity or identity_alive(identity):
                    raise HarnessError(f"Resource busy ({old.get('purpose', 'unknown')}): {path}")
                # Check and replacement are serialized, including the new owner's
                # entire critical section. Never archive a concurrent live lease.
                archive = path.with_name(path.name + ".stale-" + uuid.uuid4().hex + ".json")
                try:
                    os.replace(path, archive)
                except FileNotFoundError:
                    continue
        else:
            raise HarnessError(f"Could not acquire lock: {path}")
        try:
            yield record
        finally:
            try:
                if read_json(path).get("token") == token:
                    path.unlink()
            except (FileNotFoundError, ValueError):
                pass


def _git_metadata(marker):
    """Recognize a checkout/worktree, including gitdir pointers and reftables."""
    try:
        if marker.is_dir():
            git_dir = marker
        elif marker.is_file():
            pointer = marker.read_text(encoding="utf-8", errors="surrogateescape").strip()
            if not pointer.startswith("gitdir: ") or not pointer[8:].strip():
                return False
            git_dir = (marker.parent / pointer[8:]).resolve()
        else:
            return False
        head = git_dir / "HEAD"
        if head.is_symlink():
            valid_head = bool(re.fullmatch(r"refs/[^\s]+", head.readlink().as_posix()))
        elif head.is_file():
            value = head.read_text(encoding="ascii", errors="surrogateescape").strip()
            valid_head = bool(re.fullmatch(r"ref: refs/[^\s]+|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value))
        else:
            return False
        common = git_dir
        common_pointer = git_dir / "commondir"
        if common_pointer.is_file():
            relative = common_pointer.read_text(encoding="utf-8", errors="surrogateescape").strip()
            if not relative:
                return False
            common = (git_dir / relative).resolve()
        return valid_head and (common / "objects").is_dir() and ((common / "refs").is_dir() or (common / "reftable").is_dir())
    except OSError as exc:
        raise HarnessError(f"Cannot inspect Git checkout metadata at {marker}: {exc}") from exc


def checkout_identity(value):
    """Physical checkout root, not its shared Git common directory; no Git process."""
    if not isinstance(value, (str, os.PathLike)) or not Path(value).is_absolute() or not Path(value).is_dir():
        raise HarnessError("Workspace must be an existing absolute directory")
    path = Path(value).resolve()
    for parent in (path, *path.parents):
        if _git_metadata(parent / ".git"):
            return parent
    return path


def checkout_overlap(left, right):
    """One writer per checkout, also excluding physically nested workspaces."""
    first, second = checkout_identity(left), checkout_identity(right)
    left, right = Path(left).resolve(), Path(right).resolve()
    return (left == right or left in right.parents or right in left.parents
            or first == second or first in second.parents or second in first.parents)


def _identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise HarnessError(f"Invalid {name}: use a simple stable ID")


def _relative(value):
    p = str(value).replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        raise HarnessError(f"Scope path must remain inside workspace: {value}")
    return p


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) or (p.endswith("/**") and path == p[:-3]) for p in patterns)


def _tool_version_probes(check):
    probes = check.get("tool_versions", {})
    if not isinstance(probes, dict):
        raise HarnessError("tool_versions must be an object")
    if "executable" in probes:
        raise HarnessError("tool_versions name 'executable' is reserved for the command fingerprint")
    return probes


def validate_contract(contract):
    if not isinstance(contract, dict) or contract.get("schema_version") != SCHEMA_VERSION:
        raise HarnessError("Unsupported contract schema_version")
    for key in ("run_id", "task_id", "attempt_id"):
        _identifier(contract.get(key), key)
    if contract.get("lane_id") is not None:
        _identifier(contract["lane_id"], "lane_id")
    if contract.get("workflow") not in WORKFLOWS:
        raise HarnessError("Unknown workflow")
    if contract.get("process_level") not in {"light", "standard", "coordinated"}:
        raise HarnessError("Unknown process_level")
    if not isinstance(contract.get("goal"), str) or not contract["goal"].strip():
        raise HarnessError("A concrete goal is required")
    if contract.get("persistence", True) is not True:
        raise HarnessError("Persistence prohibited: do not create a harness run; use permitted transient context")
    permissions = contract.get("permissions", [])
    if not isinstance(permissions, list) or any(x not in {"read", "write", "test", "external"} for x in permissions):
        raise HarnessError("Invalid permissions")
    if contract["workflow"] in {"review", "diagnostics", "plan", "answer", "research"} and "write" in permissions:
        raise HarnessError("Read/review/plan/research contract must not authorize product edits; use a mixed change workflow explicitly")
    cwd = contract.get("cwd")
    if cwd is not None and (not Path(cwd).is_absolute() or not Path(cwd).is_dir()):
        raise HarnessError("cwd must be an existing absolute directory")
    resource_root = contract.get("resource_lock_root")
    if resource_root:
        path = Path(resource_root)
        if not path.is_absolute() or (cwd and (path.resolve() == Path(cwd).resolve() or Path(cwd).resolve() in path.resolve().parents)):
            raise HarnessError("resource_lock_root must be absolute and outside source workspace")
    scope = contract.get("scope", {})
    for key in ("write_paths", "protected_paths", "exclude"):
        values = scope.get(key, [])
        if not isinstance(values, list):
            raise HarnessError(f"scope.{key} must be a list")
        for value in values:
            _relative(value)
    if scope.get("write_paths") and "write" not in permissions:
        raise HarnessError("Writable paths require explicit write permission")
    checks = contract.get("checks")
    if not isinstance(checks, list) or not checks:
        raise HarnessError("A persisted contract requires applicable checks")
    ids = []
    for check in checks:
        _identifier(check.get("id"), "check ID")
        ids.append(check["id"])
        if check.get("kind") not in KINDS or not isinstance(check.get("required"), bool):
            raise HarnessError("Every check needs a kind and required boolean")
        if check["kind"] == "command":
            _tool_version_probes(check)
            if "test" not in permissions:
                raise HarnessError("Command checks require test permission")
            argv = check.get("argv")
            if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or "\x00" in a for a in argv):
                raise HarnessError("Command checks use a nonempty argv array, never a shell string")
            command_cwd = Path(check.get("cwd", cwd or ""))
            if not command_cwd.is_absolute() or not command_cwd.is_dir():
                raise HarnessError("Command cwd must exist and be absolute")
            if not isinstance(check.get("timeout"), (int, float)) or not 0 < check["timeout"] <= 86400:
                raise HarnessError("Command timeout must be >0 and <=86400 seconds")
            if not isinstance(check.get("expected_exit", 0), int):
                raise HarnessError("expected_exit must be an integer")
            if check.get("effect", "local_test") not in {"read_only", "local_test"}:
                raise HarnessError("Check runner cannot perform preparation or external operations")
            if not isinstance(check.get("resources", []), list):
                raise HarnessError("resources must be a list")
            for assertion in check.get("assertions", []):
                if not isinstance(assertion, dict) or assertion.get("stream", "combined") not in {"stdout", "stderr", "combined"}:
                    raise HarnessError("Invalid output assertion")
                try:
                    re.compile(assertion["regex"])
                except (KeyError, TypeError, re.error):
                    raise HarnessError("Output assertions require a valid regex")
            for report in check.get("reports", []):
                if not report.get("path") or report.get("format") not in {"junit", "json"}:
                    raise HarnessError("Reports require a path and supported format")
                if report["format"] == "junit" and (not isinstance(report.get("min_tests", 1), int) or report.get("min_tests", 1) < 1 or not isinstance(report.get("max_skipped", 0), int) or report.get("max_skipped", 0) < 0):
                    raise HarnessError("JUnit reports require at least one real case and nonnegative skip allowance")
        else:
            if not isinstance(check.get("criteria"), list) or not check["criteria"]:
                raise HarnessError("Non-command checks require explicit criterion IDs")
            for criterion in check["criteria"]:
                _identifier(criterion, "criterion ID")
    if len(ids) != len(set(ids)):
        raise HarnessError("Check IDs must be unique")
    budget = contract.get("budget", {})
    if not isinstance(budget, dict):
        raise HarnessError("budget must be an object")
    if type(budget.get("max_attempts", 3)) is not int or budget.get("max_attempts", 3) < 1:
        raise HarnessError("max_attempts must be positive")
    if "native_cap" in budget and (type(budget["native_cap"]) is not int or budget["native_cap"] < 1):
        raise HarnessError("native_cap must be a positive integer")
    if "root_counts" in budget and type(budget["root_counts"]) is not bool:
        raise HarnessError("root_counts must be an explicit boolean")
    remaining_budget_seconds(contract)
    for shared in scope.get("shared_files", []):
        _relative(shared.get("path", ""))
        if not shared.get("allowed_records"):
            raise HarnessError("Shared files require allowed_records")
        if shared.get("format") not in {"json-records", "validator"}:
            raise HarnessError("Shared files need json-records or a frozen adapter validator")
        if shared["format"] == "validator":
            if not shared.get("validator_argv") or not shared.get("validator_files"):
                raise HarnessError("Adapter validators need argv and content hashes of their implementation files")
            for source, expected in shared["validator_files"].items():
                if file_hash(source) != expected:
                    raise HarnessError(f"Adapter implementation changed: {source}")
    return contract


def _git(cwd, *args):
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=30)
        if p.returncode == 0:
            return p.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _entry(path):
    if not isinstance(path, Path):
        path = Path(path)
    try:
        before = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return {"kind": "missing"}
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(path)
        entry = {"kind": "symlink", "target": target, "sha256": hashlib.sha256(target.encode("utf-8")).hexdigest()}
    elif stat.S_ISREG(before.st_mode):
        entry = {"kind": "file", "sha256": file_hash(path), "size": before.st_size,
                 "executable": bool(before.st_mode & 0o111) if os.name != "nt" else None}
    else:
        return {"kind": "directory"}
    after = path.lstat()
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise HarnessError("Source changed during snapshot: " + str(path))
    return entry


def _git_settings(cwd):
    """Read all snapshot-relevant config in one process, preserving last-value semantics."""
    settings = dict.fromkeys(("core.autocrlf", "core.eol", "core.filemode"))
    raw = _git(cwd, "config", "--null", "--get-regexp", r"^(core\.autocrlf|core\.eol|core\.filemode)$") or b""
    for entry in raw.split(b"\0"):
        key, _, value = entry.partition(b"\n")
        name = os.fsdecode(key).lower()
        if name in settings:
            settings[name] = os.fsdecode(value).strip()
    return settings


def snapshot(contract):
    """Actual bytes, including tracked deletion and nonignored untracked files."""
    cwd_value = contract.get("cwd")
    result = {"schema_version": 1, "kind": "non_file", "files": {}, "external_inputs": {}}
    if cwd_value:
        cwd = Path(cwd_value).resolve()
        excludes = [".git", ".git/**", *contract.get("scope", {}).get("exclude", [])]
        head = _git(cwd, "rev-parse", "HEAD")
        root = _git(cwd, "rev-parse", "--show-toplevel")
        is_root = root is not None and Path(os.fsdecode(root).strip()).resolve() == cwd
        result.update({"kind": "git" if is_root else "directory", "cwd": str(cwd), "exclude": excludes})
        names = set()
        if is_root:
            raw = _git(cwd, "ls-files", "--cached", "--others", "--exclude-standard", "-z") or b""
            names.update(os.fsdecode(n).replace("\\", "/") for n in raw.split(b"\0") if n)
            settings = _git_settings(cwd)
            result["git"] = {"head": os.fsdecode(head).strip() if head else None, "branch": os.fsdecode(_git(cwd, "symbolic-ref", "--short", "HEAD") or b"").strip() or None, "settings": settings}
            # Gitlinks need the actual submodule tree as well as the recorded SHA.
            stages = _git(cwd, "ls-files", "--stage", "-z") or b""
            for line in stages.split(b"\0"):
                if line.startswith(b"160000 "):
                    name = os.fsdecode(line.split(b"\t", 1)[1]).replace("\\", "/")
                    sub = cwd / name
                    if sub.is_dir():
                        for directory, dirs, files in os.walk(sub, followlinks=False):
                            dirs[:] = [d for d in dirs if d != ".git"]
                            names.update((Path(directory) / f).relative_to(cwd).as_posix() for f in files if f != ".git")
        else:
            for directory, dirs, files in os.walk(cwd, followlinks=False):
                relative = Path(directory).relative_to(cwd)
                dirs[:] = [d for d in dirs if not matches((relative / d).as_posix(), excludes) and not (Path(directory) / d).is_symlink()]
                names.update((relative / f).as_posix() for f in files)
                names.update((relative / d).as_posix() for d in os.listdir(directory) if (Path(directory) / d).is_symlink())
        result["files"] = {n: _entry(cwd / n) for n in sorted(names) if not matches(n, excludes)}
    for item in contract.get("inputs", []):
        path = Path(item["path"]).resolve()
        if not path.is_file():
            raise HarnessError(f"Required input missing: {path}")
        entry = _entry(path)
        if item.get("sha256") and entry["sha256"] != item["sha256"]:
            raise HarnessError(f"Frozen input changed: {path}")
        result["external_inputs"][str(path)] = entry
    result["digest"] = digest(result)
    return result


def changed_paths(before, after):
    return sorted(p for p in set(before["files"]) | set(after["files"]) if before["files"].get(p) != after["files"].get(p))


def verify_snapshot(value, label="snapshot"):
    supplied = value.get("digest")
    body = {key: item for key, item in value.items() if key != "digest"}
    if not supplied or digest(body) != supplied:
        raise HarnessError(f"{label} manifest integrity failed")
    return value


def scope_errors(contract, baseline, candidate):
    verify_snapshot(baseline, "Baseline")
    verify_snapshot(candidate, "Candidate")
    scope = contract.get("scope", {})
    changes = changed_paths(baseline, candidate)
    errors = []
    for path in changes:
        if matches(path, scope.get("protected_paths", [])):
            errors.append(f"protected path changed: {path}")
        elif "write" not in contract.get("permissions", []) or not matches(path, scope.get("write_paths", [])):
            errors.append(f"out-of-scope path changed: {path}")
    if baseline.get("external_inputs") != candidate.get("external_inputs"):
        errors.append("versioned external inputs changed")
    return errors


class Run:
    def __init__(self, directory):
        self.path = Path(directory).resolve()

    @property
    def state_path(self):
        return self.path / "state.json"

    def state(self):
        if not self.state_path.is_file():
            raise HarnessError(f"No run at {self.path}")
        return read_json(self.state_path)

    @contextmanager
    def edit(self):
        with lock(self.path / ".state.lock", purpose="authoritative run state"):
            state = self.state()
            yield state
            state["updated_at"] = now()
            atomic_json(self.state_path, state)

    def attempt_dir(self, state=None):
        state = state or self.state()
        return self.path / "attempts" / state["current_attempt"]

    def contract(self, state=None):
        state = state or self.state()
        result = read_json(self.path / "contracts" / (state["contract_digest"] + ".json"))
        if digest(result) != state["contract_digest"]:
            raise HarnessError("Frozen contract integrity failed")
        validate_contract(result)
        return result

    def resource_path(self, name):
        contract = self.contract()
        root = Path(contract["resource_lock_root"]) if contract.get("resource_lock_root") else self.path.parent / ".harness-resource-locks"
        return root / (hashlib.sha256(name.encode("utf-8")).hexdigest() + ".lock")

    @contextmanager
    def workspace_lock(self, contract=None):
        contract = contract or self.contract()
        # Subdirectories of one Git checkout share a lock; linked worktrees do
        # not. Non-Git callers must agree on the same workspace root/namespace.
        name = "workspace:" + os.path.normcase(str(checkout_identity(contract["cwd"]))) if contract.get("cwd") else "run:" + str(self.path)
        with lock(self.resource_path(name), purpose=name):
            yield

    @classmethod
    def create(cls, directory, contract):
        validate_contract(contract)  # Must happen before making any persisted files.
        initial_snapshot = snapshot(contract)
        cls._check_base(contract, initial_snapshot)
        run = cls(directory)
        if contract.get("cwd"):
            cwd = Path(contract["cwd"]).resolve()
            if run.path == cwd or cwd in run.path.parents:
                raise HarnessError("Run store must be outside source workspace to avoid snapshot contamination")
        run.path.mkdir(parents=True, exist_ok=True)
        with lock(run.path / ".state.lock", purpose="initialize run"):
            if run.state_path.exists():
                raise HarnessError("Run already exists; use status/resume or a new attempt")
            sha = digest(contract)
            atomic_json(run.path / "contracts" / (sha + ".json"), contract, exclusive=True)
            state = {"schema_version": 1, "run_id": contract["run_id"], "task_id": contract["task_id"], "workflow": contract["workflow"], "goal": contract["goal"], "current_attempt": contract["attempt_id"], "contract_digest": sha, "attempts": [contract["attempt_id"]], "status": "planned", "created_at": now(), "updated_at": now(), "checkpoints": 0, "owners": {}, "operations": {}, "accepted": None, "unknown_telemetry": ["account_cost", "tokens"]}
            atomic_json(run.state_path, state, exclusive=True)
            run._initialize_attempt(state, contract, initial_snapshot)
            shutil.copyfile(run.attempt_dir(state) / "baseline.json", run.path / "origin-baseline.json")
            if (run.attempt_dir(state) / "baseline-files").exists():
                shutil.copytree(run.attempt_dir(state) / "baseline-files", run.path / "origin-baseline-files")
        return run

    @staticmethod
    def _check_base(contract, baseline):
        if contract.get("base_revision"):
            if baseline.get("kind") != "git" or baseline.get("git", {}).get("head") != contract["base_revision"]:
                raise HarnessError("Contract base_revision does not match the actual initial Git checkout")

    def _initialize_attempt(self, state, contract, baseline=None):
        folder = self.attempt_dir(state)
        folder.mkdir(parents=True, exist_ok=False)
        baseline = baseline or snapshot(contract)
        atomic_json(folder / "baseline.json", baseline, exclusive=True)
        for shared in contract.get("scope", {}).get("shared_files", []):
            source = Path(contract["cwd"]) / shared["path"]
            target = folder / "baseline-files" / shared["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                shutil.copyfile(source, target)

    def new_attempt(self, contract, *, reason):
        validate_contract(contract)
        if not isinstance(reason, str) or not reason.strip():
            raise HarnessError("New attempt requires the new hypothesis or changed requirement")
        with self.edit() as state:
            old = self.contract(state)
            for key in ("run_id", "task_id"):
                if old[key] != contract[key]:
                    raise HarnessError("Attempt must belong to the same run and task")
            if contract["attempt_id"] in state["attempts"]:
                raise HarnessError("Attempt ID already used")
            old_budget, new_budget = old.get("budget", {}), contract.get("budget", {})
            old_limit, new_limit = old_budget.get("max_attempts", 3), new_budget.get("max_attempts", 3)
            if new_limit > old_limit:
                raise HarnessError("An ordinary attempt cannot increase the run attempt budget")
            old_deadline, new_deadline = old_budget.get("deadline"), new_budget.get("deadline")
            if old_deadline and (not new_deadline or datetime.fromisoformat(new_deadline.replace("Z", "+00:00")) > datetime.fromisoformat(old_deadline.replace("Z", "+00:00"))):
                raise HarnessError("An ordinary attempt cannot extend or remove the run deadline")
            if len(state["attempts"]) >= new_limit:
                raise HarnessError("Attempt budget exhausted; do not reset it with a new agent")
            if len(state["attempts"]) >= 2 and reason == state.get("attempt_reason"):
                signatures = []
                for attempt_id in state["attempts"][-2:]:
                    records = [read_json(p) for p in sorted((self.path / "attempts" / attempt_id / "checks").glob("*/result.json"))]
                    failed = [{key: item.get(key) for key in ("check_id", "status", "exit_code", "error", "assertion_errors")} for item in records if item.get("status") not in {"passed", "satisfied"}]
                    signatures.append(failed)
                if signatures[0] and signatures[0] == signatures[1]:
                    raise HarnessError("Two identical failed attempts: supply a new diagnostic hypothesis instead of repeating")
            self._require_reconciled(state)
            baseline = snapshot(contract)
            self._check_base(contract, baseline)
            sha = digest(contract)
            atomic_json(self.path / "contracts" / (sha + ".json"), contract, exclusive=True)
            state.update({"contract_digest": sha, "current_attempt": contract["attempt_id"], "status": "planned", "accepted": None, "attempt_reason": reason})
            state["attempts"].append(contract["attempt_id"])
            self._initialize_attempt(state, contract, baseline)
        return self.status()

    def _require_reconciled(self, state):
        unknown = [name for name, value in state.get("operations", {}).items() if value["status"] in {"planned", "unknown"}]
        live = [name for name, value in state.get("processes", {}).items() if identity_alive(value.get("identity"))]
        open_owners = [name for name, value in state.get("owners", {}).items() if value.get("needs_native_reconciliation")]
        if unknown or live or open_owners:
            raise HarnessError(f"Reconcile before changing attempt: operations={unknown}, processes={live}, open native owners={open_owners}")

    def freeze(self):
        with self.workspace_lock():
            with self.edit() as state:
                contract = self.contract(state)
                remaining = remaining_budget_seconds(contract)
                if remaining is not None and remaining <= 0:
                    raise HarnessError("Run deadline exhausted; inspect/checkpoint instead of starting checks")
                candidate = snapshot(contract)
                baseline = read_json(self.path / "origin-baseline.json")
                errors = scope_errors(contract, baseline, candidate)
                if errors:
                    raise HarnessError("; ".join(errors))
                from .runner import _toolchains
                toolchains = _toolchains(contract)
                if snapshot(contract)["digest"] != candidate["digest"]:
                    raise HarnessError("Source changed while freezing toolchain")
                toolchain_path = self.attempt_dir(state) / "toolchains.json"
                if toolchain_path.exists():
                    if read_json(toolchain_path) != toolchains:
                        raise HarnessError("Toolchain already frozen; changed tools require a new attempt")
                else:
                    atomic_json(toolchain_path, toolchains, exclusive=True)
                target = self.attempt_dir(state) / "candidate.json"
                if target.exists():
                    if read_json(target)["digest"] != candidate["digest"]:
                        raise HarnessError("Candidate already frozen; new source needs a new attempt")
                else:
                    atomic_json(target, candidate, exclusive=True)
                state.update({"status": "verifying", "candidate_digest": candidate["digest"], "toolchain_digest": digest(toolchains), "accepted": None})
        return candidate

    def candidate(self, state=None):
        state = state or self.state()
        target = self.attempt_dir(state) / "candidate.json"
        if not target.exists():
            raise HarnessError("Freeze the final candidate before checks")
        result = read_json(target)
        verify_snapshot(result, "Candidate")
        supplied = result["digest"]
        if state.get("candidate_digest") != supplied:
            raise HarnessError("Candidate belongs to a different state")
        return result

    def assert_current(self, state, contract):
        expected = self.candidate(state)
        actual = snapshot(contract)
        if expected["digest"] != actual["digest"]:
            raise HarnessError("Stale candidate: source bytes or input versions changed after freeze")
        return actual

    def checkpoint(self, note, next_step):
        if not note.strip() or not next_step.strip():
            raise HarnessError("Checkpoint requires decisions/context and the next safe step")
        with self.workspace_lock():
            with self.edit() as state:
                contract = self.contract(state)
                state["checkpoints"] += 1
                record = {"schema_version": 1, "version": state["checkpoints"], "created_at": now(), "state": state.copy(), "contract": contract, "actual_snapshot": snapshot(contract), "note": note, "next_safe_step": next_step, "native_resume_claim": False}
                record["digest"] = digest(record)
                atomic_json(self.path / "checkpoints" / f"{state['checkpoints']:06d}.json", record, exclusive=True)
        return record

    def resume(self):
        with self.edit() as state:
            contract = self.contract(state)
            actual = snapshot(contract)
            problems = []
            if state.get("candidate_digest") and actual["digest"] != state["candidate_digest"]:
                problems.append("source/input snapshot changed; previous checks are stale")
            for key, process in state.get("processes", {}).items():
                live = identity_alive(process.get("identity"))
                process["observed_alive"] = live
                if live:
                    problems.append(f"owned process still alive: {key}; do not launch another")
                elif process.get("status") == "running":
                    process["status"] = "interrupted"
                    problems.append(f"check process disappeared without complete evidence: {key}")
            for key, owner in state.get("owners", {}).items():
                if owner.get("status") != "closed":
                    owner["needs_native_reconciliation"] = True
                    problems.append(f"native owner needs backend observation: {key}")
            for key, operation in state.get("operations", {}).items():
                if operation["status"] in {"planned", "unknown"}:
                    problems.append(f"operation outcome requires observation before retry: {key}")
            from .runner import accepted_evidence_errors, validate_artifacts
            evidence_validation = []
            if state.get("accepted"):
                # Read-only validation takes no state locks and sees persisted
                # state until this edit commits. Hash artifacts only once while
                # checking receipts and the full accepted dependency chain.
                errors = accepted_evidence_errors(self, state["accepted"]["verdict_digest"], _evidence_validation=evidence_validation)
                problems.extend("accepted result: " + error for error in errors)
            else:
                for path in sorted((self.attempt_dir(state) / "checks").glob("*/result.json")):
                    try:
                        record = read_json(path)
                        sha = record.pop("digest", None)
                        errors = []
                        if digest(record) != sha:
                            errors.append("result content hash failed")
                        record["digest"] = sha
                        errors.extend(validate_artifacts(self, record))
                        for key, expected in {"run_id": contract["run_id"], "task_id": contract["task_id"], "attempt_id": contract["attempt_id"], "contract_digest": state["contract_digest"], "candidate_digest": state.get("candidate_digest")}.items():
                            if record.get(key) != expected:
                                errors.append(f"wrong/stale {key}")
                        evidence_validation.append({"check_id": record.get("check_id"), "errors": errors})
                        problems.extend(f"evidence {record.get('check_id')}: {error}" for error in errors)
                    except (OSError, ValueError, KeyError) as error:
                        problems.append(f"unreadable evidence {path.name}: {type(error).__name__}")
            if state.get("checkpoints"):
                checkpoint = self.path / "checkpoints" / f"{state['checkpoints']:06d}.json"
                try:
                    record = read_json(checkpoint)
                    sha = record.pop("digest", None)
                    if digest(record) != sha:
                        problems.append("latest checkpoint integrity failed")
                except (OSError, ValueError):
                    problems.append("latest checkpoint missing or unreadable")
            state["resume"] = {"observed_at": now(), "snapshot": actual["digest"], "problems": problems, "evidence_validation": evidence_validation, "mode": "checkpoint_context_recovery", "native_resume": "not_claimed"}
            if problems:
                state["status"] = "blocked"
                state["accepted"] = None
        return {"run_id": state["run_id"], "resume": state["resume"], "safe_to_start_new_owner": not problems}

    def status(self, *, verify_evidence=False):
        state = self.state()
        contract = self.contract(state)
        actual = snapshot(contract)
        state["source_current"] = state.get("candidate_digest") == actual["digest"]
        # A cheap status observation cannot assert that logs/reports still exist.
        # Full read-only validation is explicit, avoiding heavy repeated polling.
        state["evidence_current"] = None if state["source_current"] else False
        state["evidence_validation"] = {"status": "not_checked", "errors": []}
        state["recorded_status"] = state.get("status")
        if verify_evidence and state.get("accepted"):
            from .runner import accepted_evidence_errors
            errors = accepted_evidence_errors(self, state["accepted"]["verdict_digest"])
            state["evidence_current"] = not errors
            state["evidence_validation"] = {"status": "failed" if errors else "passed", "errors": errors}
        if state["evidence_current"] is False and state.get("status") == "accepted":
            state["status"] = "stale"
        state["actual_snapshot"] = actual["digest"]
        state["run_dir"] = str(self.path)
        return state

    def record_operation(self, operation_id, action, status, proof):
        _identifier(operation_id, "operation ID")
        if status not in {"planned", "unknown", "confirmed", "failed"}:
            raise HarnessError("Unknown operation status")
        if status in {"confirmed", "failed"} and not proof.strip():
            raise HarnessError("Observed proof required to resolve operation outcome")
        with self.edit() as state:
            old = state["operations"].get(operation_id)
            if old and old["status"] in {"planned", "unknown"} and status == "planned":
                raise HarnessError("Operation outcome unknown: observe it before retry; do not repeat blindly")
            if old and old["status"] == "confirmed" and status == "planned":
                raise HarnessError("Operation already completed; repeating it requires a different explicit operation")
            if old and old["action"] != action:
                raise HarnessError("An operation ID cannot identify another action")
            state["operations"][operation_id] = {"action": action, "status": status, "proof": proof, "updated_at": now()}
        return state["operations"][operation_id]

    def record_owner(self, record):
        """Ledger of observations, not a native spawn API and not an account quota."""
        for key in ("owner_id", "lane_id", "native_ref"):
            if not record.get(key):
                raise HarnessError(f"Owner record needs {key}")
        if record.get("status") not in {"open", "running", "idle", "done", "stopped", "closed", "unavailable"}:
            raise HarnessError("Invalid native lifecycle status")
        if not record.get("observation"):
            raise HarnessError("Lifecycle updates require a backend observation reference")
        if record.get("status") == "closed" and (record.get("closed_confirmed") is not True or not record.get("closure_evidence")):
            raise HarnessError("Closing a ledger slot requires confirmed backend closure evidence")
        with self.edit() as state:
            contract = self.contract(state)
            budget = contract.get("budget", {})
            cap = budget.get("native_cap")
            if type(cap) is not int or cap < 1 or not budget.get("native_cap_source"):
                raise HarnessError("No observed/documented native cap and source in contract")
            root_counts = budget.get("root_counts")
            if type(root_counts) is not bool:
                raise HarnessError("Native capacity requires explicit boolean budget.root_counts")
            previous = state["owners"].get(record["owner_id"])
            remaining = remaining_budget_seconds(contract)
            if previous is None and remaining is not None and remaining <= 0:
                raise HarnessError("Run deadline exhausted; a new owner cannot reset the budget")
            if previous and previous["native_ref"] != record["native_ref"] and previous["status"] != "closed":
                raise HarnessError("Original owner lifecycle unresolved; cannot replace native reference")
            owners = {**state["owners"], record["owner_id"]: {**record, "observed_at": now()}}
            open_owners = [v for v in owners.values() if v["status"] != "closed"]
            count = len(open_owners) + int(root_counts)
            if count > cap:
                raise HarnessError(f"Shared native cap exceeded: {count}>{cap}; idle/done/stopped still count until closed")
            # A helper and its owner share this exact same ledger and cap.
            writers = [v for v in open_owners if v.get("write_cwd")]
            for i, left in enumerate(writers):
                for right in writers[i + 1:]:
                    if checkout_overlap(left["write_cwd"], right["write_cwd"]):
                        # One mutable checkout gets one writer, independent of claimed paths.
                        raise HarnessError("Concurrent writers must not share a mutable checkout")
            state["owners"] = owners
            state["max_observed_counted_threads"] = max(count, state.get("max_observed_counted_threads", 0))
        return state["owners"][record["owner_id"]]

    def capacity(self):
        state = self.state()
        budget = self.contract(state).get("budget", {})
        cap = budget.get("native_cap")
        if type(cap) is not int or cap < 1 or not budget.get("native_cap_source"):
            raise HarnessError("Native capacity is unknown; observe the client before spawning")
        root_counts = budget.get("root_counts")
        if type(root_counts) is not bool:
            raise HarnessError("Native capacity requires explicit boolean budget.root_counts")
        owners = state.get("owners", {})
        counted = [key for key, value in owners.items() if value.get("status") != "closed"]
        count = len(counted) + int(root_counts)
        pending = [key for key, value in owners.items() if value.get("needs_native_reconciliation") or value.get("status") == "unavailable"]
        return {"cap": cap, "root_counts": root_counts, "cap_source": budget["native_cap_source"], "counted": count, "open_owner_ids": counted, "remaining": max(0, cap - count), "must_reconcile": pending, "spawn_allowed": count < cap and not pending, "limitation": "A preflight for the sole native orchestrator, not a spawn API or account-wide quota"}

    def submit_result(self, record):
        """An owner can request review; only gate can accept the candidate."""
        state = self.state()
        contract = self.contract(state)
        candidate = self.assert_current(state, contract)
        for key, expected in {"run_id": contract["run_id"], "task_id": contract["task_id"], "attempt_id": contract["attempt_id"], "contract_digest": state["contract_digest"], "candidate_digest": candidate["digest"]}.items():
            if record.get(key) != expected:
                raise HarnessError(f"Late or mismatched owner result: {key}")
        for key in ("summary", "limitations", "next_safe_step"):
            if not isinstance(record.get(key), str) or not record[key].strip():
                raise HarnessError(f"Owner result requires {key}")
        if record.get("status") != "ready_for_review":
            raise HarnessError("Owner result may only request ready_for_review")
        if not isinstance(record.get("evidence_ids"), list):
            raise HarnessError("Owner result requires evidence IDs")
        for check_id in record["evidence_ids"]:
            if not (self.attempt_dir(state) / "checks" / check_id / "result.json").is_file():
                raise HarnessError(f"Owner result references missing evidence: {check_id}")
        sha = digest(record)
        atomic_json(self.attempt_dir(state) / "results" / (sha + ".json"), record, exclusive=True)
        with self.edit() as current:
            if current["current_attempt"] != state["current_attempt"]:
                raise HarnessError("Attempt changed while owner submitted result")
            current.update({"status": "ready_for_review", "owner_result_digest": sha})
        return {"result_digest": sha, "status": "ready_for_review"}
