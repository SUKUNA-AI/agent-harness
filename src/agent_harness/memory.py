"""Opt-in repository-scoped short, episodic and semantic file memory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from .core import HarnessError, digest, lock, now
from .execution_ledger import (HASH, ID, IDENTIFIER, PROVENANCE_SCHEMA, SHORT, TEXT,
    VALIDITY, object_schema, paginate, safe_private, sanitized, validity)
from .file_io import read_regular_file

STRING_LIST = {"type": "array", "maxItems": 128, "items": SHORT}
SHORT_SCHEMA = object_schema({"schema_version": {"const": 1}, "id": IDENTIFIER,
    "repository_identity": HASH, "goal": TEXT, "constraints": STRING_LIST,
    "checkpoint": SHORT, "active_task": SHORT, "failures": STRING_LIST,
    "important_files": STRING_LIST, "next_step": TEXT, "evidence_ids": STRING_LIST,
    "session_id": {"type": ["string", "null"], "maxLength": 128},
    "attempts_used": {"type": "integer", "minimum": 0},
    "tokens_reported": {"type": ["integer", "null"], "minimum": 0},
    "updated_at": {"type": "string", "format": "date-time"}, "provenance": PROVENANCE_SCHEMA,
}, ("schema_version", "id", "repository_identity", "goal", "constraints", "checkpoint",
    "active_task", "failures", "important_files", "next_step", "evidence_ids", "session_id",
    "attempts_used", "tokens_reported", "updated_at", "provenance"))
EPISODE_SCHEMA = object_schema({"schema_version": {"const": 1}, "id": IDENTIFIER,
    "repository_identity": HASH, "problem_signature": SHORT, "category": SHORT,
    "context": TEXT, "hypothesis": TEXT, "attempted_fix": TEXT, "root_cause": TEXT,
    "outcome": {"enum": ["verified", "failed", "unknown"]}, "evidence_ids": STRING_LIST,
    "evidence_refs": {"type": "array", "maxItems": 128, "items": object_schema({
        "id": SHORT, "path": SHORT, "sha256": HASH}, ("id", "path", "sha256"))},
    "affected_symbols": STRING_LIST, "tool_version": SHORT, "trust_level": {"enum": ["verified", "historical_hypothesis"]},
    "tags": STRING_LIST, "notes": TEXT, "actor": SHORT,
    "created_at": {"type": "string", "format": "date-time"},
    "expires_at": {"type": ["string", "null"], "maxLength": 64, "format": "date-time"},
    "validity_status": {"enum": VALIDITY}, "supersedes": {"type": ["string", "null"], "maxLength": 128},
    "provenance": PROVENANCE_SCHEMA,
}, ("schema_version", "id", "repository_identity", "problem_signature", "category", "context",
    "hypothesis", "attempted_fix", "root_cause", "outcome", "evidence_ids", "affected_symbols",
    "tool_version", "trust_level", "tags", "notes", "actor", "created_at", "expires_at",
    "validity_status", "supersedes", "provenance"))
SEMANTIC_SCHEMA = object_schema({"schema_version": {"const": 1}, "id": IDENTIFIER,
    "repository_identity": HASH, "document": {"enum": ["architecture", "decisions", "conventions"]},
    "claim": TEXT, "source": SHORT, "actor": SHORT,
    "confidence": {"enum": ["verified", "hypothesis"]},
    "last_checked": {"type": "string", "format": "date-time"}, "provenance": PROVENANCE_SCHEMA,
    "validity_status": {"enum": VALIDITY}, "relations": {"type": "array", "maxItems": 128,
        "items": object_schema({"from": SHORT, "to": SHORT, "kind": SHORT}, ("from", "to", "kind"))},
    "supersedes": {"type": ["string", "null"], "maxLength": 128},
}, ("schema_version", "id", "repository_identity", "document", "claim", "source", "actor",
    "confidence", "last_checked", "provenance", "validity_status", "relations", "supersedes"))
RELATIONS_SCHEMA = object_schema({"schema_version": {"const": 1}, "repository_identity": HASH,
    "records": {"type": "array", "items": object_schema({"record_id": IDENTIFIER,
        "from": SHORT, "to": SHORT, "kind": SHORT, "provenance": PROVENANCE_SCHEMA},
        ("record_id", "from", "to", "kind", "provenance"))}}, ("schema_version", "repository_identity", "records"))
MEMORY_INDEX_SCHEMA = object_schema({"schema_version": {"const": 1}, "repository_identity": HASH,
    "episodes": {"type": "array", "items": IDENTIFIER}, "semantic": {"type": "array", "items": IDENTIFIER}},
    ("schema_version", "repository_identity", "episodes", "semantic"))


class Memory:
    def __init__(self, store, repository_identity, *, enabled=False):
        if not re.fullmatch(r"[a-f0-9]{64}", repository_identity):
            raise HarnessError("Memory requires opaque repository identity SHA256")
        self.enabled, self.repository_identity = enabled, repository_identity
        self.root = safe_private(Path(store) / repository_identity, create=enabled)

    def _require(self):
        if not self.enabled:
            raise HarnessError("Repository memory is opt-in; enable it explicitly")

    def _record(self, record, schema, directory, *, mutable=False):
        from .strict_yaml import dump, validate
        self._require()
        value = sanitized(dict(record))
        if value.get("repository_identity") != self.repository_identity or value.get("provenance", {}).get("repository_identity") != self.repository_identity:
            raise HarnessError("Memory repository identity mismatch")
        validate(value, schema)
        for name in value["provenance"]["files"]:
            if not isinstance(name, str) or name.startswith("/") or "\\" in name or ":" in name or any(p in {"", ".", ".."} for p in name.split("/")):
                raise HarnessError("Memory provenance needs safe relative paths")
        target = safe_private(self.root / directory, create=True)
        with lock(self.root / ".lock", purpose="memory write"):
            dump(target / (value["id"] + ".yaml"), value, schema, exclusive=not mutable)
            if directory != "short_term":
                self.rebuild_index(locked=True)
        return value

    def update_short_term(self, record):
        return self._record(record, SHORT_SCHEMA, "short_term", mutable=True)

    def record_episode(self, record):
        self._require()
        if record.get("outcome") == "verified" and (record.get("trust_level") != "verified" or not record.get("evidence_ids") or not record.get("evidence_refs") or not self._evidence_current(record)):
            raise HarnessError("Verified episode requires unique complete current check evidence")
        return self._record(record, EPISODE_SCHEMA, "episodes")

    @staticmethod
    def _evidence_current(record):
        try:
            identifiers, refs = record.get("evidence_ids"), record.get("evidence_refs")
            if (not isinstance(identifiers, list) or not identifiers
                    or any(not isinstance(value, str) or not value for value in identifiers)
                    or not isinstance(refs, list) or len(refs) != len(identifiers)
                    or any(not isinstance(ref, dict) or not isinstance(ref.get("id"), str) or not ref["id"] for ref in refs)):
                return False
            referenced = [ref["id"] for ref in refs]
            if len(set(identifiers)) != len(identifiers) or len(set(referenced)) != len(referenced) or set(identifiers) != set(referenced):
                return False
            return all(hashlib.sha256(
                    read_regular_file(safe_private(ref["path"]), max_bytes=16 * 1024 * 1024)).hexdigest() == ref["sha256"]
                for ref in refs)
        except (KeyError, OSError, ValueError, TypeError, HarnessError):
            return False

    def record_semantic(self, record):
        return self._record(record, SEMANTIC_SCHEMA, "semantic")

    def _records(self, kind, schema):
        from .strict_yaml import load
        directory = safe_private(self.root / kind)
        return [load(safe_private(path), schema) for path in directory.glob("*.yaml")] if directory.exists() else []

    def rebuild_index(self, *, locked=False):
        from .strict_yaml import dump
        self._require()
        def rebuild():
            episodes = self._records("episodes", EPISODE_SCHEMA)
            semantics = self._records("semantic", SEMANTIC_SCHEMA)
            index = {"schema_version": 1, "repository_identity": self.repository_identity,
                     "episodes": sorted(r["id"] for r in episodes), "semantic": sorted(r["id"] for r in semantics)}
            dump(self.root / "index.yaml", index, MEMORY_INDEX_SCHEMA)
            knowledge = safe_private(self.root / "knowledge", create=True)
            relations = [{"record_id": r["id"], **relation, "provenance": r["provenance"]}
                         for r in semantics for relation in r["relations"]]
            dump(knowledge / "relations.yaml", {"schema_version": 1,
                "repository_identity": self.repository_identity, "records": relations}, RELATIONS_SCHEMA)
            # Derived Markdown documents are readable but never authoritative recall.
            for document in ("architecture", "decisions", "conventions"):
                selected = [r for r in semantics if r["document"] == document]
                text = "# " + document + "\n\nDerived observations; validate YAML provenance before reuse.\n\n"
                text += "\n\n".join("## " + r["id"] + "\n\n" + r["claim"] + "\n\nSource: " + r["source"] + "; revision: " + r["provenance"]["revision"] + "; actor: " + r["actor"] + "; confidence: " + r["confidence"] + "; checked: " + r["last_checked"] for r in selected)
                # Atomic output through safe same-directory temporary replacement.
                target = safe_private(knowledge / (document + ".md"))
                temp = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
                import os
                try:
                    with temp.open("x", encoding="utf-8", newline="\n") as stream:
                        stream.write(text + "\n"); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
            return index
        if locked:
            return rebuild()
        with lock(self.root / ".lock", purpose="memory index rebuild"):
            return rebuild()

    def recall(self, query, current, *, top_k=3, max_bytes=8192):
        self._require()
        if type(top_k) is not int or not 1 <= top_k <= 3 or not isinstance(query, str) or len(query) > 4096 or type(max_bytes) is not int or not 2048 <= max_bytes <= 65536:
            raise HarnessError("Memory recall supports query<=4096 and top_k=1..3")
        if current.get("repository_identity") != self.repository_identity:
            raise HarnessError("Memory recall repository mismatch")
        records = self._records("episodes", EPISODE_SCHEMA) + self._records("semantic", SEMANTIC_SCHEMA)
        superseded = {r.get("supersedes") for r in records if r.get("supersedes")}
        words = set(re.findall(r"\w+", query.casefold()))
        valid, historical, stale = [], [], []
        for row in records:
            status = "superseded" if row["id"] in superseded else row["validity_status"]
            expires = row.get("expires_at")
            if expires:
                try:
                    expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        raise ValueError("timezone missing")
                    if expiry <= datetime.now(timezone.utc):
                        status = "expired"
                except ValueError as exc:
                    raise HarnessError("Memory expiration requires timezone-aware ISO timestamp") from exc
            status = validity(row["provenance"], current, status=status)
            if "outcome" in row and row["outcome"] == "verified" and not self._evidence_current(row):
                status = "needs_revalidation"
            terms = set(re.findall(r"\w+", " ".join(str(row.get(k, "")) for k in ("problem_signature", "category", "tags", "claim", "context")).casefold()))
            score = len(words & terms) + (10 if query == row.get("problem_signature") else 0)
            item = {"id": row["id"], "score": score, "validity_status": status,
                    "record": row, "pointer": str((self.root / ("episodes" if "outcome" in row else "semantic") / (row["id"] + ".yaml")).relative_to(self.root)),
                    "trust": "untrusted_observation"}
            if status != "valid":
                stale.append({"id": row["id"], "status": status})
            elif row.get("trust_level") == "historical_hypothesis" or row.get("confidence") == "hypothesis" or row.get("outcome") in {"failed", "unknown"}:
                historical.append(item)
            else:
                valid.append(item)
        ranked = lambda rows: sorted(rows, key=lambda row: (-row["score"], row["id"]))[:top_k]
        result = {"schema_version": 1, "repository_identity": self.repository_identity,
                "verified_facts": ranked(valid), "historical_hypothesis": ranked(historical),
                "stale_items": stale[:10], "stale_memory_prevented": len(stale), "omitted_stale_items": max(0, len(stale) - 10), "top_k": top_k,
                "omitted_records": 0}
        from .context_reducer import compact_provenance
        from .core import canonical
        if len(canonical(result)) + 2 > max_bytes:
            for group in ("verified_facts", "historical_hypothesis"):
                for item in result[group]:
                    row = item["record"]
                    item["record_digest"] = digest(row)
                    item["record"] = {key: row[key] for key in ("id", "problem_signature", "category", "outcome", "evidence_ids", "claim", "source", "confidence") if key in row}
                    if "claim" in item["record"] and len(item["record"]["claim"]) > 512:
                        item["record"]["claim"] = item["record"]["claim"][:512]
                    item["provenance"] = compact_provenance(row["provenance"])
                    item["excerpt_omitted"] = True
        while len(canonical(result)) + 2 > max_bytes:
            group = "historical_hypothesis" if result["historical_hypothesis"] else "verified_facts"
            if not result[group]:
                raise HarnessError("Memory recall envelope exceeds budget")
            result[group].pop()
            result["omitted_records"] += 1
        return result

    def show(self, record_id, *, offset=0, limit=8192):
        self._require()
        if not re.fullmatch(ID, record_id):
            raise HarnessError("Invalid memory identifier")
        for directory, schema in (("episodes", EPISODE_SCHEMA), ("semantic", SEMANTIC_SCHEMA), ("short_term", SHORT_SCHEMA)):
            from .strict_yaml import load
            path = safe_private(self.root / directory / (record_id + ".yaml"))
            if path.exists():
                return paginate(load(path, schema), offset=offset, limit=limit)
        raise HarnessError("Memory record not found")


def memory_dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("show", "rebuild"))
    parser.add_argument("record_id", nargs="?")
    parser.add_argument("--store", required=True)
    parser.add_argument("--repository-identity", required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=8192)
    args = parser.parse_args(argv)
    memory = Memory(args.store, args.repository_identity, enabled=True)
    if args.command == "show" and not args.record_id:
        parser.error("show requires record_id")
    value = memory.show(args.record_id, offset=args.offset, limit=args.limit) if args.command == "show" else memory.rebuild_index()
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0
