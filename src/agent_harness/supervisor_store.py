"""Private atomic YAML files and a durable, hash-chained recovery journal.

Cooperating processes are serialized by the existing kernel lock machinery.
This is not an OS sandbox against another process running as the same user.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .core import HarnessError, _os_lock, identity_alive, now, process_identity
from .file_io import read_regular_file
from . import strict_yaml

MAX_JOURNAL_BYTES = 64 * 1024 * 1024


def _ordinary(path, directory=False, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise HarnessError("Symlink/reparse private path is forbidden: " + str(path))
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise HarnessError("Unsupported private path type: " + str(path))
    return info


def _parents(path):
    # Inspect before resolve: resolving first would hide a junction/symlink.
    path = Path(os.path.abspath(path))
    for parent in reversed(path.parents):
        _ordinary(parent, directory=True)
    return path


def _mkdir(path):
    path = Path(os.path.abspath(path))
    for item in [*reversed(path.parents), path]:
        try:
            _ordinary(item, directory=True)
        except FileNotFoundError:
            item.mkdir(mode=0o700)
            _ordinary(item, directory=True)


def _fsync_dir(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path, data, *, exclusive=False):
    """Same-directory staging, fsync, replace; refuse links and special files."""
    path = Path(os.path.abspath(path))
    _mkdir(path.parent)
    _parents(path)
    before = _ordinary(path, missing_ok=True)
    if exclusive and before is not None:
        raise FileExistsError(path)
    temp = path.with_name(".yaml-stage-" + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        _parents(path)
        current = _ordinary(path, missing_ok=True)
        if before is None and current is not None or before is not None and (current is None or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)):
            raise HarnessError("Private target changed before atomic replacement")
        if exclusive:
            # Hard-link creation is atomic and refuses concurrent creation.
            os.link(temp, path)
            temp.unlink()
        else:
            os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


_IDENTITY = {"type": ["object", "null"], "properties": {"pid": {"type": "integer", "minimum": 1}, "created": {"type": "string", "minLength": 1}}, "required": ["pid", "created"]}
LEASE_SCHEMA = {"type": "object", "required": ["schema_version", "identity", "token", "purpose", "created_at"], "properties": {"schema_version": {"const": 1}, "identity": _IDENTITY, "token": {"type": "string"}, "purpose": {"type": "string"}, "created_at": {"type": "string", "format": "utc"}}}
EVENT_SCHEMA = {"type": "object", "required": ["schema_version", "sequence", "event_id", "timestamp", "previous_hash", "payload", "hash"], "properties": {"schema_version": {"const": 1}, "sequence": {"type": "integer", "minimum": 1}, "event_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}, "timestamp": {"type": "string", "format": "utc"}, "previous_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "payload": {"type": "object", "additionalProperties": True}, "hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}}
CHECKPOINT_SCHEMA = {"type": "object", "required": ["schema_version", "generation", "event_id", "journal_hash", "state_digest", "state", "reason", "owner", "attempt_id", "timestamp"], "properties": {"schema_version": {"const": 1}, "generation": {"type": "integer", "minimum": 1}, "event_id": {"type": "string"}, "journal_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "state_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "state": {"type": "object", "additionalProperties": True}, "reason": {"type": "string", "minLength": 1}, "owner": {"type": "string", "minLength": 1}, "attempt_id": {"type": ["string", "null"]}, "timestamp": {"type": "string", "format": "utc"}}}


class SupervisorStore:
    def __init__(self, root, state_schema=None):
        self.root = Path(os.path.abspath(root))
        _mkdir(self.root)
        _parents(self.root)
        _ordinary(self.root, directory=True)
        self.state_schema = state_schema
        self._lease = None

    def path(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or relative.anchor or ".." in relative.parts or not relative.parts or any(":" in p for p in relative.parts):
            raise HarnessError("Private path must be a contained relative path")
        result = self.root / relative
        # Existing ancestors must already be ordinary; missing parents are made
        # only inside this private root by the corresponding writer.
        for parent in result.parents:
            candidate = parent
            if self.root not in (candidate, *candidate.parents):
                break
            if candidate.exists() or candidate.is_symlink():
                _ordinary(candidate, directory=True)
        return result

    def read(self, relative, schema=None):
        path = self.path(relative)
        _parents(path)
        return strict_yaml.load(path, schema)

    def write(self, relative, value, schema=None, exclusive=False):
        path = self.path(relative)
        strict_yaml.dump(path, value, schema, exclusive=exclusive)
        return path

    @contextmanager
    def lease(self, purpose="supervisor"):
        if self._lease is not None:
            raise HarnessError("Supervisor lease is not reentrant")
        guard = self.path("locks/supervisor.guard")
        _mkdir(guard.parent)
        _parents(guard)
        _ordinary(guard, missing_ok=True)
        with _os_lock(guard):
            lease = self.path("locks/supervisor.yaml")
            if lease.exists() or lease.is_symlink():
                old = self.read("locks/supervisor.yaml", LEASE_SCHEMA)
                if not old["identity"] or identity_alive(old["identity"]):
                    raise HarnessError("Supervisor busy: active or uncertain process identity")
                os.replace(lease, lease.with_name("supervisor.stale-" + old["token"] + ".yaml"))
            record = {"schema_version": 1, "identity": process_identity(os.getpid()), "token": uuid.uuid4().hex, "purpose": purpose, "created_at": now()}
            self.write("locks/supervisor.yaml", record, LEASE_SCHEMA, exclusive=True)
            self._lease = record
            try:
                yield record
            finally:
                self._lease = None
                try:
                    if self.read("locks/supervisor.yaml", LEASE_SCHEMA)["token"] == record["token"]:
                        lease.unlink()
                        _fsync_dir(lease.parent)
                except FileNotFoundError:
                    pass

    @contextmanager
    def _writing(self):
        if self._lease is not None:
            yield
        else:
            with self.lease():
                yield

    def events(self):
        path = self.path("journal/events.jsonl")
        if not path.exists():
            return []
        _parents(path)
        raw = read_regular_file(path, max_bytes=MAX_JOURNAL_BYTES)
        if raw and not raw.endswith(b"\n"):
            raise HarnessError("Torn journal tail: recovery required; refusing blind replay")
        result, previous = [], "0" * 64
        for index, line in enumerate(raw.splitlines(), 1):
            if not line or len(line) > strict_yaml.MAX_BYTES:
                raise HarnessError("Invalid or oversized journal record")
            try:
                record = strict_yaml.loads(line, EVENT_SCHEMA)
            except (ValueError, UnicodeError) as error:
                raise HarnessError("Corrupt journal record " + str(index)) from error
            expected = dict(record)
            expected.pop("hash")
            if record["sequence"] != index or record["previous_hash"] != previous or _digest(expected) != record["hash"]:
                raise HarnessError("Journal sequence/hash chain mismatch")
            previous = record["hash"]
            result.append(record)
        return result

    def append_event(self, event):
        with self._writing():
            strict_yaml.validate(event, {"type": "object", "additionalProperties": True})
            records = self.events()
            record = {"schema_version": 1, "sequence": len(records) + 1, "event_id": uuid.uuid4().hex, "timestamp": now(), "previous_hash": records[-1]["hash"] if records else "0" * 64, "payload": event}
            record["hash"] = _digest(record)
            strict_yaml.validate(record, EVENT_SCHEMA)
            path = self.path("journal/events.jsonl")
            _mkdir(path.parent)
            _parents(path)
            _ordinary(path, missing_ok=True)
            data = _canonical(record) + b"\n"
            if len(data) > strict_yaml.MAX_BYTES or path.exists() and path.stat().st_size + len(data) > MAX_JOURNAL_BYTES:
                raise HarnessError("Journal storage budget exhausted")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            with os.fdopen(fd, "ab") as output:
                if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                    raise HarnessError("Journal is not a regular file")
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            _fsync_dir(path.parent)
            return record

    def commit(self, state, reason, owner="supervisor", attempt_id=None):
        with self._writing():
            strict_yaml.validate(state, self.state_schema or {"type": "object", "additionalProperties": True})
            if not isinstance(reason, str) or not reason:
                raise HarnessError("Checkpoint reason is required")
            previous = [r for r in self.events() if r["payload"].get("kind") == "checkpoint"]
            generation = len(previous) + 1
            payload = {"kind": "checkpoint", "generation": generation, "state": state, "state_digest": _digest(state), "reason": reason, "owner": owner, "attempt_id": attempt_id}
            event = self.append_event(payload)
            checkpoint = {"schema_version": 1, "generation": generation, "event_id": event["event_id"], "journal_hash": event["hash"], "state_digest": payload["state_digest"], "state": state, "reason": reason, "owner": owner, "attempt_id": attempt_id, "timestamp": event["timestamp"]}
            self.write(f"checkpoints/{generation:06d}.yaml", checkpoint, CHECKPOINT_SCHEMA, exclusive=True)
            self.write("state.yaml", state, self.state_schema)
            return checkpoint

    def recover(self):
        with self._writing():
            records = self.events()
            checkpoints = [r for r in records if r["payload"].get("kind") == "checkpoint"]
            if not checkpoints:
                if self.path("state.yaml").exists():
                    raise HarnessError("State lacks committed journal provenance")
                return {"state": None, "generation": 0, "recovered": False, "events": len(records)}
            for index, event in enumerate(checkpoints, 1):
                payload = event["payload"]
                if payload.get("generation") != index or _digest(payload.get("state")) != payload.get("state_digest"):
                    raise HarnessError("Checkpoint generation/state digest mismatch")
                strict_yaml.validate(payload["state"], self.state_schema or {"type": "object", "additionalProperties": True})
                name = f"checkpoints/{index:06d}.yaml"
                expected = {"schema_version": 1, "generation": index, "event_id": event["event_id"], "journal_hash": event["hash"], "state_digest": payload["state_digest"], "state": payload["state"], "reason": payload["reason"], "owner": payload["owner"], "attempt_id": payload["attempt_id"], "timestamp": event["timestamp"]}
                if self.path(name).exists():
                    if self.read(name, CHECKPOINT_SCHEMA) != expected:
                        raise HarnessError("Immutable checkpoint differs from journal")
                else:
                    self.write(name, expected, CHECKPOINT_SCHEMA, exclusive=True)
            state = checkpoints[-1]["payload"]["state"]
            try:
                current = self.read("state.yaml", self.state_schema)
            except FileNotFoundError:
                current = None
            # A corrupt state is not silently discarded; the intact journal can
            # be inspected and an explicit recovery action taken by the caller.
            recovered = current != state
            if recovered:
                self.write("state.yaml", state, self.state_schema)
            return {"state": state, "generation": len(checkpoints), "recovered": recovered, "events": len(records)}
