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
import threading
import uuid

from .core import HarnessError, _os_lock, identity_alive, now, process_identity
from .file_io import read_regular_file, ParentDirectory
from . import strict_yaml

MAX_JOURNAL_BYTES = 64 * 1024 * 1024
JOURNAL_READ_SCHEMA = "supervisor-journal-event.v2.schema.json"


def _event_version(payload, version):
    kind = payload.get("kind", "")
    root = isinstance(kind, str) and kind.startswith(("root_core_preparation_", "root_model_reservation", "root_preparation_failure"))
    if type(version) is not int or version not in (1, 2) or root != (version == 2):
        raise HarnessError("Unsupported journal/root protocol version; compatible writer required")
    if root:
        from .root_preparation import EVENT_SCHEMAS
        if kind not in EVENT_SCHEMAS:
            raise HarnessError("Unsupported root journal operation")
        strict_yaml.validate(payload, EVENT_SCHEMAS[kind])


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
            with ParentDirectory(item) as parent:
                try:
                    if parent.fd is not None:
                        os.mkdir(item.name, mode=0o700, dir_fd=parent.fd)
                    else:
                        item.mkdir(mode=0o700)
                except FileExistsError:
                    # Another owner may have created this ordinary parent.
                    pass
                parent.sync()
                parent.check()
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
    with ParentDirectory(path) as parent:
        before = _ordinary(path, missing_ok=True)
        if exclusive and before is not None:
            raise FileExistsError(path)
        temp = ".yaml-stage-" + uuid.uuid4().hex
        fd = parent.open(os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), name=temp)
        try:
            with os.fdopen(fd, "wb") as output:
                if output.write(data) != len(data):
                    raise OSError("Short private record write")
                output.flush()
                os.fsync(output.fileno())
            parent.check()
            current = _ordinary(path, missing_ok=True)
            if before is None and current is not None or before is not None and (current is None or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)):
                raise HarnessError("Private target changed before atomic replacement")
            if exclusive:
                # Publish once in the pinned directory, never a retargeted path.
                if parent.fd is not None:
                    os.link(temp, path.name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd, follow_symlinks=False)
                else:
                    os.link(path.parent / temp, path)
                parent.sync()
                parent.check()
            else:
                parent.replace(temp)
        finally:
            parent.unlink(temp, missing_ok=True)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


_IDENTITY = {"type": ["object", "null"], "properties": {"pid": {"type": "integer", "minimum": 1}, "created": {"type": "string", "minLength": 1}}, "required": ["pid", "created"]}
LEASE_SCHEMA = {"type": "object", "required": ["schema_version", "identity", "token", "purpose", "created_at"], "properties": {"schema_version": {"const": 1}, "identity": _IDENTITY, "token": {"type": "string"}, "purpose": {"type": "string"}, "created_at": {"type": "string", "format": "utc"}}}
EVENT_SCHEMA = {"type": "object", "required": ["schema_version", "sequence", "event_id", "timestamp", "previous_hash", "payload", "hash"], "properties": {"schema_version": {"const": 1}, "sequence": {"type": "integer", "minimum": 1}, "event_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}, "timestamp": {"type": "string", "format": "utc"}, "previous_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "payload": {"type": "object", "additionalProperties": True}, "hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}}
CHECKPOINT_SCHEMA = {"type": "object", "required": ["schema_version", "generation", "event_id", "journal_hash", "state_digest", "state", "reason", "owner", "attempt_id", "timestamp"], "properties": {"schema_version": {"const": 1}, "generation": {"type": "integer", "minimum": 1}, "event_id": {"type": "string"}, "journal_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "state_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "state": {"type": "object", "additionalProperties": True}, "reason": {"type": "string", "minLength": 1}, "owner": {"type": "string", "minLength": 1}, "attempt_id": {"type": ["string", "null"]}, "timestamp": {"type": "string", "format": "utc"}}}
RECOVERY_SCHEMA = {"type": "object", "required": ["schema_version", "status", "original_sha256", "prefix_sha256", "archive", "discarded_bytes", "complete_events", "timestamp"], "properties": {"schema_version": {"const": 1}, "status": {"const": "RECOVERY_REQUIRED"}, "original_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "prefix_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "archive": {"type": "string", "pattern": "^journal/interrupted-[a-f0-9]{32}\\.jsonl$"}, "discarded_bytes": {"type": "integer", "minimum": 1}, "complete_events": {"type": "integer", "minimum": 0}, "timestamp": {"type": "string", "format": "utc"}}}


class SupervisorStore:
    def __init__(self, root, state_schema=None, *, create=True):
        self.root = Path(os.path.abspath(root))
        if create:
            _mkdir(self.root)
        _parents(self.root)
        _ordinary(self.root, directory=True)
        self.state_schema = state_schema
        self._lease = None
        self._mutex = threading.RLock()
        # Only a bounded head summary is cached, never caller-owned records.
        # Every reuse checks the complete physical mutation stamp under lease.
        # Explicit replay/resume always validates the entire immutable prefix.
        self._journal_head = None
        # Windows st_ctime is creation time; POSIX timestamps can coalesce too.
        # Retain bounded SHA256 state, never journal rows, and prove the actual
        # bytes before reusing a parsed head, even when metadata is unchanged.
        self._journal_content_hash = None
        self._snapshot_depth = 0
        self._prepared_writes = {}

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
        prepared = self._prepared_writes.get(str(relative))
        if prepared is not None:
            if (prepared[0] is not value or prepared[1] is not schema
                    or _digest(value) != prepared[3] or _digest(schema) != prepared[4]):
                raise HarnessError("Prepared publication value/schema mismatch")
            atomic_write(path, prepared[2], exclusive=exclusive)
        else:
            strict_yaml.dump(path, value, schema, exclusive=exclusive)
        return path

    @contextmanager
    def lease(self, purpose="supervisor"):
        with self._mutex:
            if self._snapshot_depth and self._lease is None:
                raise HarnessError("Cannot upgrade a read snapshot to a supervisor writer lease")
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
        # Worker callbacks share the foreground owner's process lease, but must
        # still serialize journal sequence allocation inside that process.
        with self._mutex:
            if self._lease is not None:
                yield
            else:
                if self._snapshot_depth:
                    raise HarnessError("Cannot upgrade a read snapshot to a supervisor writer lease")
                with self.lease():
                    yield

    @contextmanager
    def _snapshot(self, *, writing=False):
        """Serialize short journal/projection publications across processes.

        Writers acquire their lifetime lease first. Readers never acquire it,
        so a live model cannot prevent status or cancellation. Legacy read-only
        stores lacking the new coordination anchor are inspected strictly with
        no filesystem writes; mixed-version live writers receive no guarantee.
        """
        with self._mutex:
            if self._snapshot_depth:
                yield
                return
            guard = self.path("locks/journal-snapshot.guard")
            if writing:
                _mkdir(guard.parent)
            from .native_observability import observer_lock
            pinned = guard
            while not pinned.parent.exists() and not pinned.parent.is_symlink():
                pinned = pinned.parent
            with ParentDirectory(pinned) as parent:
                before = _ordinary(self.path("locks/journal-snapshot.guard"), missing_ok=True)
                lease = self.path("locks/journal-snapshot")
                _ordinary(lease, missing_ok=True)
                if before is None and not writing:
                    self._snapshot_depth = 1
                    try:
                        yield
                    finally:
                        self._snapshot_depth = 0
                    parent.check()
                    if _ordinary(self.path("locks/journal-snapshot.guard"), missing_ok=True) is not None:
                        raise HarnessError("Journal snapshot anchor appeared during legacy read")
                    _ordinary(self.path("locks/journal-snapshot"), missing_ok=True)
                    return
                with observer_lock(lease, purpose="journal snapshot"):
                    opened = _ordinary(guard)
                    _ordinary(lease)
                    descriptor = parent.open(os.O_RDONLY | getattr(os, "O_BINARY", 0))
                    try:
                        pinned_info = os.fstat(descriptor)
                        if (not stat.S_ISREG(pinned_info.st_mode)
                                or (pinned_info.st_dev, pinned_info.st_ino) != (opened.st_dev, opened.st_ino)):
                            raise HarnessError("Journal snapshot anchor changed while opening")
                    finally:
                        os.close(descriptor)
                    read_regular_file(lease, max_bytes=strict_yaml.MAX_BYTES)
                    if before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise HarnessError("Journal snapshot anchor changed while locking")
                    self._snapshot_depth = 1
                    try:
                        yield
                    finally:
                        self._snapshot_depth = 0
                    parent.check()
                    after = _ordinary(guard)
                    if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
                        raise HarnessError("Journal snapshot anchor changed during publication")

    def _capture_journal(self):
        """Capture physical bytes only; the caller holds the snapshot guard."""
        path = self.path("journal/events.jsonl")
        if not path.exists():
            if self._journal_head is not None and self._journal_head[1]:
                raise HarnessError("Validated journal prefix disappeared")
            return None, b""
        _parents(path)
        before = self._journal_stamp(path)
        raw = read_regular_file(path, max_bytes=MAX_JOURNAL_BYTES)
        after = self._journal_stamp(path)
        if before != after:
            raise HarnessError("Journal changed during capture")
        return after, raw

    def _validate_journal(self, stamp, raw):
        """Validate the captured generation, never a later filesystem head."""
        if stamp is None:
            self._journal_head = self._journal_content_hash = None
            return []
        records = self._decode_events(raw)
        previous = self._journal_head
        if previous is not None and previous[1] and (len(records) < previous[1] or records[previous[1]-1]["hash"] != previous[2]):
            raise HarnessError("Validated journal prefix changed; recovery required")
        self._journal_head = ((stamp, len(records), records[-1]["hash"] if records else "0" * 64,
                              sum(r["payload"].get("kind") == "checkpoint" for r in records)) if stamp else None)
        self._journal_content_hash = hashlib.sha256(raw) if stamp else None
        return records

    def events(self):
        with self._mutex:
            with self._snapshot():
                stamp, raw = self._capture_journal()
            return self._validate_journal(stamp, raw)

    @staticmethod
    def _journal_stamp(path):
        info = _ordinary(path, missing_ok=True)
        if info is None:
            return None
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @staticmethod
    def _matches_open_stamp(stamp, info):
        # Windows stat/fstat synthesize executable bits and ctime differently.
        mask = ~0o111 if os.name == "nt" else -1
        expected = (info.st_dev, info.st_ino, info.st_mode & mask,
                    info.st_nlink, info.st_size, info.st_mtime_ns)
        observed = (*stamp[:2], stamp[2] & mask, *stamp[3:6]) if stamp else None
        return observed == expected and (os.name == "nt" or stamp[6] == info.st_ctime_ns)

    def _head(self):
        # Called only under _writing. Any replacement, append, truncate or
        # same-size edit invalidates the prefix; a fresh store has no trust.
        path = self.path("journal/events.jsonl")
        stamp = self._journal_stamp(path)
        if (self._journal_head is None or self._journal_head[0] != stamp
                or not self._journal_content_matches(path)):
            if self._snapshot_depth:
                raise HarnessError("Journal head changed before publication; refusing replay under snapshot")
            self.events()
        return self._journal_head or (None, 0, "0" * 64, 0)

    def _journal_content_matches(self, path, expected=None):
        """Prove cache freshness without replaying validated rows.

        Use the existing bounded, anchored, double-pass reader; a metadata
        cache hit alone cannot establish that the hash-chain bytes survived.
        The metadata-only _journal_stamp is also persisted in slot receipts,
        so its established representation must remain unchanged.
        """
        expected = self._journal_content_hash if expected is None else expected
        return (expected is not None and hashlib.sha256(
            read_regular_file(path, max_bytes=MAX_JOURNAL_BYTES)).digest() == expected.digest())

    @staticmethod
    def _decode_events(raw):
        if raw and not raw.endswith(b"\n"):
            raise HarnessError("Torn journal tail: recovery required; refusing blind replay")
        result, previous = [], "0" * 64
        for index, line in enumerate(raw.splitlines(), 1):
            if not line or len(line) > strict_yaml.MAX_BYTES:
                raise HarnessError("Invalid or oversized journal record")
            try:
                record = strict_yaml.loads(line, JOURNAL_READ_SCHEMA)
            except (ValueError, UnicodeError) as error:
                raise HarnessError("Corrupt journal record " + str(index)) from error
            expected = dict(record)
            _event_version(record["payload"], record["schema_version"])
            expected.pop("hash")
            if record["sequence"] != index or record["previous_hash"] != previous or _digest(expected) != record["hash"]:
                raise HarnessError("Journal sequence/hash chain mismatch")
            previous = record["hash"]
            result.append(record)
        return result

    def repair_torn_tail(self):
        """Archive a demonstrably truncated final record, requiring reconciliation.

        A complete corrupt row, even the final one, is never silently discarded.
        Missing final LF on otherwise valid JSON is ambiguous and also refused.
        The archive and recovery marker are durable before journal replacement.
        """
        with self._writing():
            path = self.path("journal/events.jsonl")
            _parents(path)
            with self._snapshot():
                captured_stamp, raw = self._capture_journal()
                marker_exists = self.path("recovery-required.yaml").exists()
            if not raw or raw.endswith(b"\n"):
                self._decode_events(raw)
                return {"status": "no_repair_needed", "recovery_required": self.path("recovery-required.yaml").exists()}
            offset = raw.rfind(b"\n") + 1
            prefix, tail = raw[:offset], raw[offset:]
            records = self._validate_journal(captured_stamp, prefix)
            try:
                text = tail.decode("utf-8")
            except UnicodeDecodeError as error:
                if error.end != len(tail) or error.reason != "unexpected end of data":
                    raise HarnessError("Corrupt final journal encoding; refusing tail repair") from error
                text = tail[:error.start].decode("utf-8")
            if not text.lstrip().startswith("{"):
                raise HarnessError("Final journal record is not a demonstrably truncated object")
            try:
                json.loads(text, object_pairs_hook=lambda pairs: dict(pairs))
            except json.JSONDecodeError as error:
                if error.pos < len(text.rstrip()) and not error.msg.startswith("Unterminated string"):
                    raise HarnessError("Complete or ambiguous journal corruption; refusing tail repair") from error
            else:
                raise HarnessError("Valid JSON without final newline is ambiguous; refusing tail repair")
            archive = "journal/interrupted-" + uuid.uuid4().hex + ".jsonl"
            receipt = {"schema_version": 1, "status": "RECOVERY_REQUIRED", "original_sha256": hashlib.sha256(raw).hexdigest(), "prefix_sha256": hashlib.sha256(prefix).hexdigest(), "archive": archive, "discarded_bytes": len(tail), "complete_events": len(records), "timestamp": now()}
            name = "recovery/" + uuid.uuid4().hex + ".yaml" if marker_exists else "recovery-required.yaml"
            writes = [(name, receipt, RECOVERY_SCHEMA, True)]
            prepared = self._publish_writes(writes)
            event = self._prepare_event({"kind": "tail_repair", "receipt": receipt}, head=self._journal_head)
            with self._snapshot(writing=True):
                if self._journal_stamp(path) != captured_stamp or read_regular_file(path, max_bytes=MAX_JOURNAL_BYTES) != raw:
                    raise HarnessError("Torn journal changed before repair publication")
                atomic_write(self.path(archive), raw, exclusive=True)
                prior, self._prepared_writes = self._prepared_writes, prepared
                try:
                    self.write(name, receipt, RECOVERY_SCHEMA, exclusive=True)
                finally:
                    self._prepared_writes = prior
                atomic_write(path, prefix)
                head = (self._journal_stamp(path), *event[0][1:])
                self._journal_head, self._journal_content_hash = head, hashlib.sha256(prefix)
                self._append_prepared(head, *event[1:])
            return {**receipt, "recovery_required": True}

    def _prepare_event(self, event, format_version=1, *, head=None, content=None):
        strict_yaml.validate(event, {"type": "object", "additionalProperties": True})
        _event_version(event, format_version)
        head = self._head() if head is None else head
        record = {"schema_version": format_version, "sequence": head[1] + 1, "event_id": uuid.uuid4().hex,
                  "timestamp": now(), "previous_hash": head[2], "payload": event}
        record["hash"] = _digest(record)
        strict_yaml.validate(record, JOURNAL_READ_SCHEMA)
        data = _canonical(record) + b"\n"
        content = self._journal_content_hash if content is None else content
        expected = content.copy() if content is not None else hashlib.sha256()
        expected.update(data)
        return head, record, data, expected

    def _append_prepared(self, head, record, data, expected_content):
        with self._snapshot(writing=True):
            path = self.path("journal/events.jsonl")
            _mkdir(path.parent)
            _parents(path)
            _ordinary(path, missing_ok=True)
            if len(data) > strict_yaml.MAX_BYTES or path.exists() and path.stat().st_size + len(data) > MAX_JOURNAL_BYTES:
                raise HarnessError("Journal storage budget exhausted")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with ParentDirectory(path) as parent:
                if (self._journal_stamp(path) != head[0]
                        or head[0] is not None and not self._journal_content_matches(path)):
                    raise HarnessError("Journal head changed before append")
                fd = parent.open(flags)
                with os.fdopen(fd, "ab") as output:
                    opened = os.fstat(output.fileno())
                    if not stat.S_ISREG(opened.st_mode):
                        raise HarnessError("Journal is not a regular file")
                    if (head[0] is None and opened.st_size != 0 or
                            head[0] is not None and not self._matches_open_stamp(head[0], opened)):
                        raise HarnessError("Journal head changed while opening append")
                    if output.write(data) != len(data):
                        raise OSError("Short journal append")
                    output.flush()
                    os.fsync(output.fileno())
                    written = os.fstat(output.fileno())
                parent.sync()
                parent.check()
                stamp = self._journal_stamp(path)
                if not self._matches_open_stamp(stamp, written):
                    raise HarnessError("Journal changed during append")
                if not self._journal_content_matches(path, expected_content):
                    raise HarnessError("Journal content changed during append")
                self._journal_head = (stamp, record["sequence"], record["hash"],
                                      head[3] + (record["payload"].get("kind") == "checkpoint"))
                self._journal_content_hash = expected_content
        return record

    def append_event(self, event, *, format_version=1):
        with self._writing():
            return self._append_prepared(*self._prepare_event(event, format_version))

    def _publish_writes(self, writes):
        """Encode/check schemas before taking the physical publication guard."""
        prepared = {name: (value, schema, strict_yaml.dumps(value, schema).encode("utf-8"), _digest(value), _digest(schema))
                    for name, value, schema, exclusive in writes}
        return prepared

    def commit(self, state, reason, owner="supervisor", attempt_id=None):
        with self._writing():
            strict_yaml.validate(state, self.state_schema or {"type": "object", "additionalProperties": True})
            if not isinstance(reason, str) or not reason:
                raise HarnessError("Checkpoint reason is required")
            generation = self._head()[3] + 1
            payload = {"kind": "checkpoint", "generation": generation, "state": state, "state_digest": _digest(state), "reason": reason, "owner": owner, "attempt_id": attempt_id}
            prepared_event = self._prepare_event(payload)
            event = prepared_event[1]
            checkpoint = {"schema_version": 1, "generation": generation, "event_id": event["event_id"], "journal_hash": event["hash"], "state_digest": payload["state_digest"], "state": state, "reason": reason, "owner": owner, "attempt_id": attempt_id, "timestamp": event["timestamp"]}
            writes = [(f"checkpoints/{generation:06d}.yaml", checkpoint, CHECKPOINT_SCHEMA, True),
                      ("state.yaml", state, self.state_schema, False)]
            prepared = self._publish_writes(writes)
            with self._snapshot(writing=True):
                self._append_prepared(*prepared_event)
                prior, self._prepared_writes = self._prepared_writes, prepared
                try:
                    for name, value, schema, exclusive in writes:
                        self.write(name, value, schema, exclusive=exclusive)
                finally:
                    self._prepared_writes = prior
            return checkpoint

    def recover(self, *, read_only=False):
        # Capture mutable projections with the authoritative journal. Validation
        # consumes only those bytes after releasing the short process guard.
        with self._mutex if read_only else self._writing():
            with self._snapshot():
                stamp, raw = self._capture_journal()
                marker_raw = read_regular_file(self.path("recovery-required.yaml"), max_bytes=strict_yaml.MAX_BYTES, missing_ok=True)
                state_raw = read_regular_file(self.path("state.yaml"), max_bytes=strict_yaml.MAX_BYTES, missing_ok=True)
            records = self._validate_journal(stamp, raw)
            marker = strict_yaml.loads(marker_raw, RECOVERY_SCHEMA) if marker_raw is not None else None
            checkpoints = [r for r in records if r["payload"].get("kind") == "checkpoint"]
            if not checkpoints:
                if state_raw is not None:
                    raise HarnessError("State lacks committed journal provenance")
                return {"state": None, "generation": 0, "recovered": False, "events": len(records), "recovery_required": marker is not None, "recovery_receipt": marker}
            writes = []
            for index, event in enumerate(checkpoints, 1):
                payload = event["payload"]
                if payload.get("generation") != index or _digest(payload.get("state")) != payload.get("state_digest"):
                    raise HarnessError("Checkpoint generation/state digest mismatch")
                strict_yaml.validate(payload["state"], self.state_schema or {"type": "object", "additionalProperties": True})
                name = f"checkpoints/{index:06d}.yaml"
                expected = {"schema_version": 1, "generation": index, "event_id": event["event_id"], "journal_hash": event["hash"], "state_digest": payload["state_digest"], "state": payload["state"], "reason": payload["reason"], "owner": payload["owner"], "attempt_id": payload["attempt_id"], "timestamp": event["timestamp"]}
                # Immutable projections can be captured later, but not during a
                # cooperating exclusive-link publication. Compare to the exact
                # expected old-generation content, never a newer state.yaml.
                with self._snapshot():
                    checkpoint_raw = read_regular_file(self.path(name), max_bytes=strict_yaml.MAX_BYTES, missing_ok=True)
                if checkpoint_raw is not None:
                    if strict_yaml.loads(checkpoint_raw, CHECKPOINT_SCHEMA) != expected:
                        raise HarnessError("Immutable checkpoint differs from journal")
                elif not read_only:
                    writes.append((name, expected, CHECKPOINT_SCHEMA, True))
            state = checkpoints[-1]["payload"]["state"]
            recovered = False
            if marker is None:
                current = strict_yaml.loads(state_raw, self.state_schema) if state_raw is not None else None
                recovered = current != state
            if recovered and not read_only and marker is None:
                writes.append(("state.yaml", state, self.state_schema, False))
            if writes:
                prepared = self._publish_writes(writes)
                prior, self._prepared_writes = self._prepared_writes, prepared
                try:
                    for name, value, schema, exclusive in writes:
                        with self._snapshot(writing=True):
                            if (self._journal_stamp(self.path("journal/events.jsonl")) != stamp
                                    or not self._journal_content_matches(self.path("journal/events.jsonl"))
                                    or read_regular_file(self.path("state.yaml"), max_bytes=strict_yaml.MAX_BYTES, missing_ok=True) != state_raw
                                    or read_regular_file(self.path("recovery-required.yaml"), max_bytes=strict_yaml.MAX_BYTES, missing_ok=True) != marker_raw):
                                raise HarnessError("Recovery publication input changed")
                            self.write(name, value, schema, exclusive=exclusive)
                finally:
                    self._prepared_writes = prior
            if marker is not None:
                return {"state": state, "generation": len(checkpoints), "recovered": False, "events": len(records), "recovery_required": True, "recovery_receipt": marker}
            return {"state": state, "generation": len(checkpoints), "recovered": recovered and not read_only, "projection_stale": recovered, "events": len(records), "recovery_required": False, "recovery_receipt": None}
