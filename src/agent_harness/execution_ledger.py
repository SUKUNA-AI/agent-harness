"""Version-bound execution facts; govern is advisory for native Codex tools.

Records contain observations, never instructions or authority. The immutable YAML
events are authoritative; the index can be rebuilt without model calls.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from .core import HarnessError, canonical, digest, lock, now
from .context import SECRET, _path, _read, _root
from .file_io import read_regular_file

ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
SHA = r"^[a-f0-9]{64}$"
TEXT = {"type": "string", "maxLength": 16384}
SHORT = {"type": "string", "maxLength": 512}
IDENTIFIER = {"type": "string", "pattern": ID}
HASH = {"type": "string", "pattern": SHA}
VALIDITY = ["valid", "needs_revalidation", "superseded", "rejected", "expired"]


def object_schema(properties, required=()):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(required)}


PROVENANCE_SCHEMA = object_schema({
    "repository_identity": HASH, "revision": SHORT, "snapshot_id": HASH,
    "files": {"type": "object", "maxProperties": 4096,
        "propertyNames": {"type": "string", "pattern": r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*\\)(?!.*:).+$"},
        "additionalProperties": HASH},
    "toolchain_digest": HASH, "branch": SHORT,
    "file_inventory_complete": {"type": "boolean"},
    "omitted_file_count": {"type": "integer", "minimum": 0},
}, ("repository_identity", "revision", "snapshot_id", "files"))

PAYLOAD_PROPERTIES = {
    "path": SHORT, "digest": HASH, "start_line": {"type": "integer", "minimum": 1},
    "end_line": {"type": "integer", "minimum": 1}, "purpose": SHORT,
    "symbol": SHORT, "language": SHORT, "definition_ids": {"type": "array", "items": SHORT},
    "reference_ids": {"type": "array", "items": SHORT}, "confidence": {"enum": ["exact", "syntactic", "heuristic", "unknown"]},
    "argv": {"type": "array", "maxItems": 256, "items": SHORT}, "cwd": SHORT,
    "toolchain_digest": HASH, "env_digest": HASH, "config_digest": HASH,
    "effect": {"enum": ["read_only", "pure", "local_test", "write", "external", "unknown"]},
    "input_closure_complete": {"type": "boolean"}, "exit_code": {"type": ["integer", "null"]},
    "timed_out": {"type": "boolean"}, "stdout_digest": HASH,
    "evidence_id": SHORT, "evidence_path": SHORT, "evidence_digest": HASH,
    "check_id": SHORT, "status": SHORT, "fact": TEXT, "category": SHORT,
    "signature": SHORT, "first_seen": SHORT, "last_seen": SHORT, "hypothesis": TEXT,
    "affected_files": {"type": "array", "items": SHORT}, "outcome": TEXT,
    "context": TEXT, "alternative": TEXT, "reason": TEXT, "constraints": TEXT,
    "authorization": TEXT, "from_status": SHORT, "to_status": SHORT,
    "operation_fingerprint": HASH, "result_pointer": SHORT, "decision": SHORT,
    "supersedes": SHORT,
}
KINDS = {"file_read": ("path", "digest", "purpose"),
         "symbol_examined": ("path", "symbol", "digest", "confidence"),
         "command": ("argv", "cwd", "effect", "status"),
         "check": ("check_id", "status", "evidence_id"),
         "failure": ("category", "signature", "status"),
         "repair_hypothesis": ("hypothesis", "outcome"),
         "decision": ("reason",), "state_transition": ("from_status", "to_status", "reason")}
EVENT_SCHEMA = object_schema({
    "schema_version": {"const": 1}, "event_id": IDENTIFIER, "run_id": IDENTIFIER,
    "task_id": IDENTIFIER, "kind": {"enum": list(KINDS)}, "actor": SHORT,
    "session_id": {"type": ["string", "null"], "maxLength": 128},
    "causal_parent": {"type": ["string", "null"], "maxLength": 128},
    "timestamp": {"type": "string", "format": "date-time"},
    "validity_status": {"enum": VALIDITY}, "provenance": PROVENANCE_SCHEMA,
    "payload": object_schema(PAYLOAD_PROPERTIES),
}, ("schema_version", "event_id", "run_id", "task_id", "kind", "actor", "session_id",
    "causal_parent", "timestamp", "validity_status", "provenance", "payload"))
EVENT_SCHEMA["allOf"] = [{"if": {"properties": {"kind": {"const": kind}}, "additionalProperties": True},
    "then": {"properties": {"payload": {"required": list(required)}}, "additionalProperties": True}}
    for kind, required in KINDS.items()]
INDEX_SCHEMA = object_schema({"schema_version": {"const": 1},
    "events": {"type": "array", "maxItems": 100000, "items": IDENTIFIER}}, ("schema_version", "events"))


def safe_private(path, *, create=False):
    """Refuse links/reparse parents before private state access."""
    path = Path(path).absolute()
    for part in reversed((path, *path.parents)):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise HarnessError("Private state cannot use symlink/reparse paths")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def sanitized(value):
    """Reject secret-looking data rather than turning secrets into facts."""
    data = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(data.encode("utf-8")) > 262144:
        raise HarnessError("Knowledge record exceeds 262144 bytes")
    if SECRET.search(data):
        raise HarnessError("Possible credential in knowledge record")
    for key, secret in os.environ.items():
        if len(secret) >= 6 and re.search(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|AUTHORIZATION", key, re.I) and secret in data:
            raise HarnessError("Environment credential in knowledge record")
    if re.search(r'(?i)(?:authorization|api[_-]?key|password|access[_-]?token)\s*[=:]\s*["\w-]', data):
        raise HarnessError("Credential-shaped data in knowledge record")
    for token in re.findall(r"[A-Za-z0-9_+/=-]{48,}", data):
        if re.fullmatch(r"[a-fA-F0-9]{48,128}", token):
            continue  # expected content digests
        entropy = -sum((token.count(c) / len(token)) * math.log2(token.count(c) / len(token)) for c in set(token))
        if entropy > 4.5 and any(c.isdigit() for c in token) and any(c.isupper() for c in token):
            raise HarnessError("High-entropy credential-shaped value in knowledge record")
    return value


def repository_identity(root):
    """Local identity includes canonical repository path; no cross-repo leakage."""
    from .core import _git
    root = _root(root)
    common = _git(root, "rev-parse", "--git-common-dir")
    if common:
        location = Path(os.fsdecode(common).strip())
        root = (root / location).resolve() if not location.is_absolute() else location.resolve()
    return digest({"local_repository": os.path.normcase(str(root))})


def provenance_for(root, snapshot, *, toolchain_digest=None, file_limit=256):
    if type(file_limit) is not int or not 1 <= file_limit <= 4096:
        raise HarnessError("Provenance file limit must be 1..4096")
    entries = [(name, entry["sha256"]) for name, entry in sorted(snapshot["files"].items()) if entry.get("kind") == "file"]
    value = {"repository_identity": repository_identity(root),
             "revision": snapshot.get("git", {}).get("head") or "directory",
             "snapshot_id": snapshot["digest"],
             "files": dict(entries[:file_limit]), "file_inventory_complete": len(entries) <= file_limit,
             "omitted_file_count": max(0, len(entries) - file_limit)}
    if snapshot.get("git", {}).get("branch"):
        value["branch"] = snapshot["git"]["branch"]
    if toolchain_digest:
        value["toolchain_digest"] = toolchain_digest
    return value


def validity(provenance, current, *, status="valid", evidence=None):
    if status != "valid":
        return status
    for field in ("repository_identity", "revision", "branch", "toolchain_digest"):
        if field in provenance and provenance.get(field) != current.get(field):
            return "needs_revalidation"
    # Snapshot binding is intentionally conservative, even with a file subset.
    if provenance.get("snapshot_id") != current.get("snapshot_id"):
        return "needs_revalidation"
    if any(current.get("files", {}).get(name) != sha for name, sha in provenance.get("files", {}).items()):
        return "needs_revalidation"
    if evidence:
        try:
            if hashlib.sha256(read_regular_file(evidence[0], max_bytes=16 * 1024 * 1024)).hexdigest() != evidence[1]:
                return "needs_revalidation"
        except (OSError, ValueError):
            return "needs_revalidation"
    return "valid"


def paginate(value, *, offset=0, limit=8192):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 256 <= limit <= 65536:
        raise HarnessError("Pagination needs offset>=0 and limit=256..65536")
    data = canonical(value)
    fragment = data[offset:offset + limit].decode("utf-8", errors="replace")
    return {"content": fragment, "offset": offset, "total_bytes": len(data),
            "next_offset": offset + limit if offset + limit < len(data) else None,
            "sha256": hashlib.sha256(data).hexdigest(), "trust": "untrusted_observation"}


class Ledger:
    def __init__(self, run_dir, repository_identity=None, *, run_id=None, task_id=None):
        self.root = safe_private(Path(run_dir) / "ledger", create=True)
        self.events = safe_private(self.root / "events", create=True)
        self.repository_identity = repository_identity
        self.run_id = run_id or Path(run_dir).name
        self.task_id = task_id or self.run_id

    def append(self, kind, payload, provenance, *, actor="supervisor", session_id=None, causal_parent=None):
        from .strict_yaml import dump, validate
        if kind not in KINDS or any(key not in payload for key in KINDS.get(kind, ())):
            raise HarnessError("Invalid ledger event kind or missing typed payload fields")
        if self.repository_identity and provenance.get("repository_identity") != self.repository_identity:
            raise HarnessError("Ledger repository identity mismatch")
        event = sanitized({"schema_version": 1, "event_id": "event-" + uuid.uuid4().hex,
            "run_id": self.run_id, "task_id": self.task_id, "kind": kind, "actor": actor,
            "session_id": session_id, "causal_parent": causal_parent, "timestamp": now(),
            "validity_status": "valid", "provenance": provenance, "payload": payload})
        validate(event, EVENT_SCHEMA)
        with lock(self.root / ".lock", purpose="execution ledger append"):
            dump(self.events / (event["event_id"] + ".yaml"), event, EVENT_SCHEMA, exclusive=True)
            self.rebuild_index(locked=True)
        return event

    def rebuild_index(self, *, locked=False):
        from .strict_yaml import dump, load
        def rebuild():
            rows = [load(safe_private(path), EVENT_SCHEMA) for path in self.events.glob("*.yaml")]
            rows.sort(key=lambda row: (row["timestamp"], row["event_id"]))
            dump(self.root / "index.yaml", {"schema_version": 1, "events": [r["event_id"] for r in rows]}, INDEX_SCHEMA)
            return rows
        if locked:
            return rebuild()
        with lock(self.root / ".lock", purpose="ledger index rebuild"):
            return rebuild()

    def _records(self):
        from .strict_yaml import load
        try:
            index = load(safe_private(self.root / "index.yaml"), INDEX_SCHEMA)
            records = [load(safe_private(self.events / (event_id + ".yaml")), EVENT_SCHEMA) for event_id in index["events"]]
            if len(records) != len(list(self.events.glob("*.yaml"))):
                return self.rebuild_index()
            return records
        except (OSError, ValueError, HarnessError):
            return self.rebuild_index()

    def show(self, event_id, *, offset=0, limit=8192):
        from .strict_yaml import load
        if not re.fullmatch(ID, event_id):
            raise HarnessError("Invalid ledger event identifier")
        value = load(safe_private(self.events / (event_id + ".yaml")), EVENT_SCHEMA)
        return paginate(value, offset=offset, limit=limit)

    def inform(self, current, *, max_bytes=8192):
        from .context_reducer import reduce
        rows = self._records()
        superseded = {row["payload"].get("supersedes") for row in rows}
        facts, failures, examined, stale = [], [], [], []
        for row in rows:
            payload = row["payload"]
            evidence = (payload["evidence_path"], payload["evidence_digest"]) if payload.get("evidence_path") and payload.get("evidence_digest") else None
            state = validity(row["provenance"], current, status="superseded" if row["event_id"] in superseded else row["validity_status"], evidence=evidence)
            item = {"event_id": row["event_id"], "kind": row["kind"], "payload": payload,
                    "provenance": row["provenance"], "validity_status": state,
                    "pointer": "ledger/events/" + row["event_id"] + ".yaml"}
            if state != "valid":
                stale.append({"event_id": row["event_id"], "status": state})
            elif row["kind"] == "failure" and payload["status"] not in {"resolved", "superseded"}:
                failures.append(item)
            elif row["kind"] in {"file_read", "symbol_examined"}:
                examined.append(item)
            else:
                facts.append(item)
        return reduce(failures + facts + examined, category="memory", max_bytes=max_bytes,
                      required={"repository_revision": current.get("revision"), "stale_items": stale[:10], "stale_items_count": len(stale),
                                "govern_scope": "advisory_for_native_codex_tools"})

    def govern(self, operation, current, *, force=False):
        """Never suppress writes or unknown tools. Identical rg requires full tree proof."""
        sanitized(operation)
        argv = operation.get("argv", [])
        program = Path(argv[0]).name.lower().removesuffix(".exe") if argv else ""
        allowed = program == "rg" and operation.get("effect") == "read_only"
        proof = (operation.get("input_closure_complete") is True and operation.get("toolchain_digest")
                 and operation.get("env_digest") and operation.get("config_digest") and current.get("snapshot_id"))
        fingerprint = digest({"operation": operation, "current": current})
        decision, reason, reused = "allow", "fresh execution requested or no proven equivalent read", None
        if operation.get("policy_denied") is True:
            decision, reason = "deny_unsafe", "independent contract policy denies this operation"
        elif not argv or operation.get("effect", "unknown") == "unknown":
            decision, reason = "unknown", "operation effects or command identity are not proven"
        elif allowed and not force:
            if not proof:
                decision, reason = "requires_revalidation", "complete tree and toolchain proof required"
            else:
                for row in reversed(self._records()):
                    payload = row["payload"]
                    if (row["kind"] == "command" and payload.get("operation_fingerprint") == fingerprint
                            and payload.get("status") == "completed" and validity(row["provenance"], current) == "valid"):
                        if payload.get("evidence_path") and payload.get("evidence_digest") and validity(row["provenance"], current, evidence=(payload["evidence_path"], payload["evidence_digest"])) == "valid":
                            decision, reason, reused = "suggest_reuse", "same full input closure and immutable result", row["event_id"]
                            break
        result = {"decision": decision, "reason": reason, "reused_event_id": reused,
                  "operation_fingerprint": fingerprint, "scope": "advisory_for_native_codex_tools"}
        self.append("decision", {"decision": decision, "reason": reason, "operation_fingerprint": fingerprint}, current)
        return result


def ledger_dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show")
    show.add_argument("event_id")
    show.add_argument("--run-dir", required=True)
    show.add_argument("--offset", type=int, default=0)
    show.add_argument("--limit", type=int, default=8192)
    rebuild = commands.add_parser("rebuild")
    rebuild.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    ledger = Ledger(args.run_dir)
    value = ledger.show(args.event_id, offset=args.offset, limit=args.limit) if args.command == "show" else {"events": len(ledger.rebuild_index())}
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0
