"""Opt-in repository-scoped short, episodic and semantic file memory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from .core import HarnessError, digest, lock, now
from .execution_ledger import (HASH, ID, IDENTIFIER, PROVENANCE_SCHEMA, SHORT, TEXT,
    VALIDITY, object_schema, paginate, safe_private, sanitized, validity)
from .file_io import read_regular_file
from .knowledge_claims import FACT_SCHEMA, build_fact, verify_fact
from .checkpoint_index import CheckpointIndex, checked_limits

STRING_LIST = {"type": "array", "maxItems": 128, "items": SHORT}
TYPED_REF_SCHEMA = object_schema({"project_id": HASH,
    "kind": {"enum": ["semantic", "episode", "short_term", "ledger_event", "evidence", "fact"]},
    "id": IDENTIFIER, "generation": {"type": "integer", "minimum": 1}, "content_digest": HASH},
    ("project_id", "kind", "id", "generation", "content_digest"))
EVIDENCE_REF_SCHEMA = object_schema({"id": SHORT, "path": SHORT, "sha256": HASH,
    "reference": TYPED_REF_SCHEMA}, ("id", "path", "sha256"))


def typed_reference(project, kind, record, *, generation=1):
    return {"project_id": project, "kind": kind, "id": record["id"],
            "generation": generation, "content_digest": digest(record)}


EVIDENCE_RECORD_SCHEMA = object_schema({"schema_version": {"const": 1}, "id": IDENTIFIER,
    "project_id": HASH, "check_id": SHORT, "candidate_digest": HASH, "producer": IDENTIFIER,
    "path": SHORT, "sha256": HASH,
    "verification_method": {"enum": ["external_observation", "core_command_check_v1"]},
    "run_dir": SHORT}, ("schema_version", "id", "project_id", "check_id", "candidate_digest", "producer", "path", "sha256", "verification_method"))
EVIDENCE_STATUS_SCHEMA = object_schema({"schema_version": {"const": 1}, "id": IDENTIFIER,
    "generation": {"type": "integer", "minimum": 1}, "status": {"enum": ["current", "revoked", "superseded"]},
    "previous_digest": {"type": ["string", "null"], "pattern": r"^[a-f0-9]{64}$"}},
    ("schema_version", "id", "generation", "status", "previous_digest"))
EVIDENCE_HEAD_SCHEMA = object_schema({"generation": {"type": "integer", "minimum": 1}, "digest": HASH}, ("generation", "digest"))


SHORT_SCHEMA = object_schema({"schema_version": {"const": 1}, "id": IDENTIFIER,
    "repository_identity": HASH, "goal": TEXT, "constraints": STRING_LIST,
    "checkpoint": SHORT, "active_task": SHORT, "failures": STRING_LIST,
    "important_files": STRING_LIST, "next_step": TEXT, "evidence_ids": STRING_LIST,
    "session_id": {"type": ["string", "null"], "maxLength": 128},
    "attempts_used": {"type": "integer", "minimum": 0},
    "tokens_reported": {"type": ["integer", "null"], "minimum": 0},
    "updated_at": {"type": "string", "format": "date-time"}, "provenance": PROVENANCE_SCHEMA,
    "evidence_refs": {"type": "array", "maxItems": 128, "items": EVIDENCE_REF_SCHEMA},
    "goal_generation": {"type": "integer", "minimum": 1}, "goal_origin": SHORT,
    "active_task_ref": {"type": ["string", "null"], "maxLength": 128},
    "bootstrap_purpose": SHORT, "context_epoch": {"type": "integer", "minimum": 0},
}, ("schema_version", "id", "repository_identity", "goal", "constraints", "checkpoint",
    "active_task", "failures", "important_files", "next_step", "evidence_ids", "session_id",
    "attempts_used", "tokens_reported", "updated_at", "provenance"))
EPISODE_SCHEMA = object_schema({"schema_version": {"const": 1}, "id": IDENTIFIER,
    "repository_identity": HASH, "problem_signature": SHORT, "category": SHORT,
    "context": TEXT, "hypothesis": TEXT, "attempted_fix": TEXT, "root_cause": TEXT,
    "outcome": {"enum": ["verified", "failed", "unknown"]}, "evidence_ids": STRING_LIST,
    "evidence_refs": {"type": "array", "maxItems": 128, "items": object_schema({
        "id": SHORT, "path": SHORT, "sha256": HASH, "reference": TYPED_REF_SCHEMA}, ("id", "path", "sha256"))},
    "affected_symbols": STRING_LIST, "tool_version": SHORT, "trust_level": {"enum": ["verified", "historical_hypothesis"]},
    "tags": STRING_LIST, "notes": TEXT, "actor": SHORT,
    "created_at": {"type": "string", "format": "date-time"},
    "expires_at": {"type": ["string", "null"], "maxLength": 64, "format": "date-time"},
    "validity_status": {"enum": VALIDITY}, "supersedes": {"anyOf": [{"type": ["string", "null"], "maxLength": 128}, TYPED_REF_SCHEMA]},
    "provenance": PROVENANCE_SCHEMA,
    "claim_kind": {"enum": ["check_result"]}, "verification_rule": {"enum": ["check_completion_v1"]},
    "check_id": SHORT,
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
        "items": object_schema({"from": {"anyOf": [SHORT, TYPED_REF_SCHEMA]}, "to": {"anyOf": [SHORT, TYPED_REF_SCHEMA]}, "kind": SHORT}, ("from", "to", "kind"))},
    "supersedes": {"anyOf": [{"type": ["string", "null"], "maxLength": 128}, TYPED_REF_SCHEMA]},
}, ("schema_version", "id", "repository_identity", "document", "claim", "source", "actor",
    "confidence", "last_checked", "provenance", "validity_status", "relations", "supersedes"))
RELATIONS_SCHEMA = object_schema({"schema_version": {"const": 1}, "repository_identity": HASH,
    "records": {"type": "array", "items": object_schema({"record_id": IDENTIFIER,
        "from": {"anyOf": [SHORT, TYPED_REF_SCHEMA]}, "to": {"anyOf": [SHORT, TYPED_REF_SCHEMA]}, "kind": SHORT, "provenance": PROVENANCE_SCHEMA},
        ("record_id", "from", "to", "kind", "provenance"))}}, ("schema_version", "repository_identity", "records"))
MEMORY_INDEX_SCHEMA = object_schema({"schema_version": {"const": 1}, "repository_identity": HASH,
    "episodes": {"type": "array", "items": IDENTIFIER}, "semantic": {"type": "array", "items": IDENTIFIER},
    "facts": {"type": "array", "items": IDENTIFIER}},
    ("schema_version", "repository_identity", "episodes", "semantic"))


class Memory:
    def __init__(self, store, repository_identity, *, enabled=False, scan_limits=None, checkpoint_limits=None):
        if not re.fullmatch(r"[a-f0-9]{64}", repository_identity):
            raise HarnessError("Memory requires opaque repository identity SHA256")
        self.enabled, self.repository_identity = enabled, repository_identity
        self.scan_limits = checked_limits(scan_limits, memory=True)
        self.checkpoint_limits = checked_limits(checkpoint_limits)
        self.index_diagnostics = []
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
        from .native_observability import observer_lock
        with observer_lock(self.root / ".lock", purpose="memory write", timeout=self.checkpoint_limits["deadline_ms"] / 1000):
            dump(target / (value["id"] + ".yaml"), value, schema, exclusive=not mutable)
            if directory == "short_term":
                try:
                    CheckpointIndex(self.root, self.repository_identity, limits=self.checkpoint_limits).update(value)
                except (OSError, ValueError, HarnessError):
                    self.index_diagnostics.append({"reason": "checkpoint_index_update_unavailable"})
            else:
                self.rebuild_index(locked=True)
        return value

    def update_short_term(self, record):
        return self._record(record, SHORT_SCHEMA, "short_term", mutable=True)

    def record_file_fact(self, root, path, *, record_id=None):
        return self._record_fact(root, path, record_id=record_id)

    def record_symbol_fact(self, root, path, symbol, *, record_id=None):
        return self._record_fact(root, path, symbol=symbol, record_id=record_id)

    def _record_fact(self, root, path, *, symbol=None, record_id=None):
        from .core import snapshot
        from .execution_ledger import provenance_for
        self._require()
        current = provenance_for(root, snapshot({"cwd": str(root)}), file_limit=1)
        row = build_fact(root, path, current, symbol=symbol, record_id=record_id)
        return self._record(row, FACT_SCHEMA, "facts")

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

    def _records(self, kind, schema, *, optional=False):
        from .strict_yaml import loads
        directory = safe_private(self.root / kind)
        rows, scanned_bytes, started = [], 0, time.monotonic()
        limits = self.scan_limits
        budget = getattr(self, "_optional_scan_budget", {"started": started, "records": 0, "bytes": 0}) if optional else {"started": started, "records": 0, "bytes": 0}
        examined = 0
        complete = True
        for index, path in enumerate(directory.glob("*.yaml") if directory.exists() else ()):
            if budget["records"] >= limits["max_records"] or index >= limits["max_inventory_entries"] or budget["bytes"] >= limits["max_bytes"] or (time.monotonic() - budget["started"]) * 1000 > limits["deadline_ms"]:
                if not optional:
                    raise HarnessError("Memory scan bound exceeded; rebuild requires narrowing")
                complete = False
                self.diagnostics.append({"kind": kind, "reason": "scan_bound", "complete": False})
                break
            budget["records"] += 1
            try:
                data = read_regular_file(safe_private(path), max_bytes=min(limits["max_record_bytes"], limits["max_bytes"] - budget["bytes"]))
                scanned_bytes += len(data)
                budget["bytes"] += len(data)
                examined += 1
                row = loads(data, schema)
                if row["repository_identity"] != self.repository_identity or row["id"] != path.stem:
                    raise HarnessError("Memory record binding mismatch")
                rows.append(row)
            except (OSError, ValueError, HarnessError):
                if not optional:
                    raise
                self.diagnostics.append({"kind": kind, "reason": "invalid_optional_record"})
        if optional:
            self.scan_metrics.append({"kind": kind, "records_examined": examined, "bytes_read": scanned_bytes,
                                      "complete": complete, "wall_ms": (time.monotonic() - started) * 1000})
        return rows

    def rebuild_index(self, *, locked=False):
        from .strict_yaml import dump
        self._require()
        def rebuild():
            episodes = self._records("episodes", EPISODE_SCHEMA)
            semantics = self._records("semantic", SEMANTIC_SCHEMA)
            facts = self._records("facts", FACT_SCHEMA)
            index = {"schema_version": 1, "repository_identity": self.repository_identity,
                     "episodes": sorted(r["id"] for r in episodes), "semantic": sorted(r["id"] for r in semantics), "facts": sorted(r["id"] for r in facts)}
            dump(self.root / "index.yaml", index, MEMORY_INDEX_SCHEMA)
            knowledge = safe_private(self.root / "knowledge", create=True)
            ids = [r["id"] for r in episodes + semantics + facts]
            def unambiguous(relation):
                return all(not isinstance(relation.get(key), str) or ids.count(relation[key]) < 2 for key in ("from", "to"))
            relations = [{"record_id": r["id"], **relation, "provenance": r["provenance"]}
                         for r in semantics for relation in r["relations"] if unambiguous(relation)]
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

    def recall(self, query, current, *, top_k=3, max_bytes=8192, optional=False):
        self._require()
        if type(top_k) is not int or not 1 <= top_k <= 3 or not isinstance(query, str) or len(query) > 4096 or type(max_bytes) is not int or not 2048 <= max_bytes <= 65536:
            raise HarnessError("Memory recall supports query<=4096 and top_k=1..3")
        if current.get("repository_identity") != self.repository_identity:
            raise HarnessError("Memory recall repository mismatch")
        self.diagnostics = []
        self.scan_metrics = []
        self._optional_scan_budget = {"started": time.monotonic(), "records": 0, "bytes": 0}
        records = (self._records("episodes", EPISODE_SCHEMA, optional=optional) + self._records("semantic", SEMANTIC_SCHEMA, optional=optional)
                   + self._records("facts", FACT_SCHEMA, optional=optional))
        counts = {}
        for row in records:
            counts[row["id"]] = counts.get(row["id"], 0) + 1
        superseded = set()
        for row in records:
            ref = row.get("supersedes")
            if isinstance(ref, str) and counts.get(ref) == 1:
                target = next(r for r in records if r["id"] == ref)
                superseded.add(("fact" if "closure" in target else "episode" if "outcome" in target else "semantic", ref))
            elif isinstance(ref, dict) and ref.get("project_id") == self.repository_identity:
                for target in records:
                    target_kind = "fact" if "closure" in target else "episode" if "outcome" in target else "semantic"
                    if (target["id"] == ref.get("id") and target_kind == ref.get("kind")
                            and ref.get("generation") == 1 and digest(target) == ref.get("content_digest")):
                        superseded.add((target_kind, target["id"]))
        words = set(re.findall(r"\w+", query.casefold()))
        valid, observations, historical, stale = [], [], [], []
        for row in records:
            status = "superseded" if ("fact" if "closure" in row else "episode" if "outcome" in row else "semantic", row["id"]) in superseded else row["validity_status"]
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
            narrow = verify_fact(row, current) if row.get("verification_rule") == "deterministic-claim-v1" else None
            status = (status if status != "valid" else "valid" if narrow["source_validity"] == "current" else "needs_revalidation") if narrow else validity(row["provenance"], current, status=status)
            if "outcome" in row and row["outcome"] == "verified" and not self._evidence_current(row):
                status = "needs_revalidation"
            evidence_validity = self.evidence_status(row) if row.get("evidence_refs") else "unknown"
            if row.get("claim_kind") == "check_result" and evidence_validity != "current":
                status = "needs_revalidation"
            terms = set(re.findall(r"\w+", " ".join(str(row.get(k, "")) for k in ("problem_signature", "category", "tags", "claim", "context", "closure")).casefold()))
            score = len(words & terms) + (10 if query == row.get("problem_signature") else 0)
            item = {"id": row["id"], "score": score, "validity_status": status,
                    "record": row, "pointer": str((self.root / ("facts" if "closure" in row else "episodes" if "outcome" in row else "semantic") / (row["id"] + ".yaml")).relative_to(self.root)),
                    "trust": "untrusted_observation", "evidence_validity": evidence_validity}
            if status != "valid":
                stale.append({"id": row["id"], "status": status})
            elif row.get("trust_level") == "historical_hypothesis" or row.get("confidence") == "hypothesis" or row.get("outcome") in {"failed", "unknown"}:
                historical.append(item)
            elif narrow and narrow["source_validity"] == "current":
                item.update(verification_level="self_checked", source_validity="current", evidence_validity="current", record_digest=digest(row))
                subject = row["closure"]["positive_dependencies"][0]
                item["record"] = {"id": row["id"], "claim_kind": row["claim_kind"], "verification_rule": row["verification_rule"],
                    "subject": subject, "symbol": row["closure"]["symbol"], "location": row["location"], "closure_digest": row["closure_digest"]}
                valid.append(item)
            elif (row.get("claim_kind") == "check_result" and row.get("verification_rule") == "check_completion_v1"
                  and self.evidence_status(row) == "current" and self._verified_check_claim(row)):
                # Extraction is intentionally limited to the observed check and
                # candidate, never neighboring author prose/root-cause assertions.
                item["record_digest"] = digest(row)
                item["record"] = {"id": row["id"], "check_id": row["check_id"], "outcome": "verified",
                                  "candidate_digest": row["provenance"]["snapshot_id"],
                                  "claim_kind": "check_result", "verification_rule": "check_completion_v1"}
                valid.append(item)
            else:
                item["verification_level"] = "observation"
                if row.get("confidence") == "verified" or row.get("trust_level") == "verified":
                    item["asserted_confidence"] = "legacy_verified"
                observations.append(item)
        ranked = lambda rows: sorted(rows, key=lambda row: (-row["score"], row["id"]))[:min(top_k, self.scan_limits["returned_count"])]
        result = {"schema_version": 2, "trust_policy": "check-completion-v1", "repository_identity": self.repository_identity,
                "verified_facts": ranked(valid), "current_observations": ranked(observations),
                "historical_hypothesis": ranked(historical), "historical_hypotheses": [],
                "diagnostics": self.diagnostics[:8], "diagnostics_omitted": max(0, len(self.diagnostics) - 8),
                "stale_items": stale[:10], "stale_memory_prevented": len(stale), "omitted_stale_items": max(0, len(stale) - 10), "top_k": top_k,
                "omitted_records": 0, "scan_metrics": self.scan_metrics}
        from .context_reducer import compact_provenance
        from .core import canonical
        if len(canonical(result)) + 2 > max_bytes:
            for group in ("verified_facts", "current_observations", "historical_hypothesis"):
                for item in result[group]:
                    row = item["record"]
                    item.setdefault("record_digest", digest(row))
                    item["record"] = {key: row[key] for key in ("id", "problem_signature", "category", "outcome", "evidence_ids", "claim", "source", "confidence") if key in row}
                    if "claim" in item["record"] and len(item["record"]["claim"]) > 512:
                        item["record"]["claim"] = item["record"]["claim"][:512]
                    item["projection_digest"] = digest(item["record"])
                    if "provenance" in row:
                        item["provenance"] = compact_provenance(row["provenance"])
                    item["excerpt_omitted"] = True
        while len(canonical(result)) + 2 > max_bytes:
            group = next((key for key in ("historical_hypothesis", "current_observations", "verified_facts") if result[key]), "verified_facts")
            if not result[group]:
                raise HarnessError("Memory recall envelope exceeds budget")
            result[group].pop()
            result["omitted_records"] += 1
        return result

    def show(self, record_id, *, kind=None, reference=None, offset=0, limit=8192):
        self._require()
        if not isinstance(record_id, str) or not re.fullmatch(ID, record_id):
            raise HarnessError("Invalid memory identifier")
        namespaces = {"episode": ("episodes", EPISODE_SCHEMA), "semantic": ("semantic", SEMANTIC_SCHEMA),
                      "short_term": ("short_term", SHORT_SCHEMA), "fact": ("facts", FACT_SCHEMA)}
        if reference is not None:
            from .strict_yaml import validate
            validate(reference, TYPED_REF_SCHEMA)
            if reference["project_id"] != self.repository_identity or reference["id"] != record_id:
                raise HarnessError("Typed memory reference project/ID mismatch")
            kind = reference["kind"]
        if kind is not None and kind not in namespaces:
            raise HarnessError("Unsupported memory kind")
        choices = []
        for name, (directory, schema) in namespaces.items():
            path = safe_private(self.root / directory / (record_id + ".yaml"))
            if path.exists() and (kind is None or name == kind):
                choices.append((name, path, schema))
        if len(choices) > 1:
            raise HarnessError("AMBIGUOUS_RECORD_ID: specify kind " + ",".join(row[0] for row in choices))
        if not choices:
            raise HarnessError("Memory record not found")
        from .strict_yaml import load
        _, path, schema = choices[0]
        row = load(path, schema)
        if row["repository_identity"] != self.repository_identity or row["id"] != record_id:
            raise HarnessError("Memory record identity mismatch")
        if reference and (digest(row) != reference["content_digest"] or reference["generation"] != row.get("goal_generation", 1)):
            raise HarnessError("STALE_REFERENCE: memory generation/content changed")
        return paginate(row, offset=offset, limit=limit)

    def register_evidence(self, receipt_path, current):
        """Explicitly import a completed check receipt; no trust from prose."""
        from .strict_yaml import dump
        path = safe_private(receipt_path)
        raw = read_regular_file(path, max_bytes=131072)
        receipt = json.loads(raw)
        required = {"schema_version", "record_type", "id", "check_id", "status", "candidate_digest", "producer"}
        if (not isinstance(receipt, dict) or set(receipt) != required or receipt["schema_version"] != 1
                or receipt["record_type"] != "check_completion" or receipt["status"] != "passed"
                or receipt["candidate_digest"] != current["snapshot_id"]
                or not all(isinstance(receipt[k], str) and re.fullmatch(ID, receipt[k]) for k in ("id", "check_id", "producer"))):
            raise HarnessError("Unsupported check completion receipt or candidate mismatch")
        record = {"schema_version": 1, "id": receipt["id"], "project_id": self.repository_identity,
                  "check_id": receipt["check_id"], "candidate_digest": receipt["candidate_digest"],
                  "producer": receipt["producer"], "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                  "verification_method": "external_observation"}
        sanitized(record)
        directory = safe_private(self.root / "evidence", create=True)
        target = directory / (record["id"] + ".yaml")
        with lock(self.root / ".lock", purpose="evidence registration"):
            if target.exists():
                from .strict_yaml import load
                if load(target, EVIDENCE_RECORD_SCHEMA) != record:
                    raise HarnessError("Evidence ID integrity conflict")
            else:
                dump(target, record, EVIDENCE_RECORD_SCHEMA, exclusive=True)
                self._evidence_transition(record["id"], "current", locked=True)
        return {"id": record["id"], "path": str(path), "sha256": record["sha256"],
                "reference": typed_reference(self.repository_identity, "evidence", record)}

    def _verified_check_claim(self, record):
        from .strict_yaml import load
        try:
            return bool(record.get("evidence_refs")) and all(
                load(safe_private(self.root / "evidence" / (ref["id"] + ".yaml")), EVIDENCE_RECORD_SCHEMA)
                .get("verification_method") == "core_command_check_v1" for ref in record["evidence_refs"])
        except (OSError, ValueError, KeyError, HarnessError):
            return False

    def _current_core_check(self, run_dir, check_id, current):
        from .core import Run, read_json, file_hash
        from .execution_ledger import repository_identity
        from .runner import _check_evidence_errors, _executable
        from .core import _tool_version_probes
        run = Run(run_dir)
        state = run.state()
        contract = run.contract(state)
        candidate = run.assert_current(state, contract)
        if (candidate["digest"] != current["snapshot_id"] or
                repository_identity(contract["cwd"]) != self.repository_identity):
            raise HarnessError("Core check source/project binding mismatch")
        check = next((check for check in contract["checks"] if check["id"] == check_id), None)
        if not check or check["kind"] != "command":
            raise HarnessError("Only completed core command checks support fact extraction")
        # Recall is read-only: never rerun arbitrary tool-version commands from
        # a saved contract as a side effect of bootstrap. Unsupported closures
        # remain observations until an explicit supported verifier is available.
        if _tool_version_probes(check):
            raise HarnessError("Core check tool-probe closure requires explicit revalidation")
        frozen = read_json(run.attempt_dir(state) / "toolchains.json")
        if digest(frozen) != state.get("toolchain_digest"):
            raise HarnessError("Core frozen toolchain manifest mismatch")
        executable = frozen.get(check_id, {}).get("executable", {})
        actual = _executable(check["argv"][0], check.get("cwd", contract["cwd"]))
        if (not actual or actual != executable.get("resolved") or
                file_hash(Path(actual).resolve(strict=True)) != executable.get("sha256")):
            raise HarnessError("Core check executable identity changed")
        details = []
        _check_evidence_errors(run, state, contract, candidate, frozen, details=details)
        selected = next((row for row in details if row["check_id"] == check_id), None)
        path = safe_private(run.attempt_dir(state) / "checks" / check_id / "result.json")
        result = read_json(path)
        if selected is None or selected["errors"] or result.get("status") != "passed":
            raise HarnessError("Core check evidence is not current/passed")
        return run, path, result

    def register_core_evidence(self, run_dir, check_id, current):
        """Import real runner artifacts, contract, candidate and toolchain proof."""
        self._require()
        from .strict_yaml import dump, load
        if not re.fullmatch(ID, check_id):
            raise HarnessError("Invalid check ID")
        run, path, result = self._current_core_check(run_dir, check_id, current)
        raw = read_regular_file(path, max_bytes=131072)
        identifier = "evidence-" + digest({"run": str(run.path), "result": result["digest"]})[:32]
        record = {"schema_version": 1, "id": identifier, "project_id": self.repository_identity,
                  "check_id": check_id, "candidate_digest": current["snapshot_id"], "producer": "core-runner",
                  "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                  "verification_method": "core_command_check_v1", "run_dir": str(run.path)}
        sanitized(record)
        directory = safe_private(self.root / "evidence", create=True)
        target = directory / (identifier + ".yaml")
        with lock(self.root / ".lock", purpose="core check evidence registration"):
            if target.exists():
                if load(target, EVIDENCE_RECORD_SCHEMA) != record:
                    raise HarnessError("Evidence ID integrity conflict")
            else:
                dump(target, record, EVIDENCE_RECORD_SCHEMA, exclusive=True)
                self._evidence_transition(identifier, "current", locked=True)
        return {"id": identifier, "path": str(path), "sha256": record["sha256"],
                "reference": typed_reference(self.repository_identity, "evidence", record)}

    def reference_for_evidence(self, path):
        """Resolve an explicitly registered path without trusting a model ID."""
        from .strict_yaml import load
        matches = []
        for index, target in enumerate(safe_private(self.root / "evidence").glob("*.yaml")):
            if index >= 512:
                raise HarnessError("Evidence lookup bound exceeded")
            try:
                row = load(safe_private(target), EVIDENCE_RECORD_SCHEMA)
            except (OSError, ValueError, HarnessError):
                continue
            if row["path"] == str(path):
                matches.append(row)
        if len(matches) > 1:
            raise HarnessError("AMBIGUOUS_EVIDENCE_PATH")
        if not matches:
            return None
        row = matches[0]
        return {"id": row["id"], "path": row["path"], "sha256": row["sha256"],
                "reference": typed_reference(self.repository_identity, "evidence", row)}

    def _evidence_transition(self, identifier, status, *, locked=False):
        from .strict_yaml import dump, load
        if not re.fullmatch(ID, identifier) or status not in {"current", "revoked", "superseded"}:
            raise HarnessError("Invalid evidence transition")
        def append():
            directory = safe_private(self.root / "evidence_status" / identifier, create=True)
            records = sorted(directory.glob("*.yaml"))
            if len(records) > 4096:
                raise HarnessError("Evidence status chain bound exceeded")
            prior = load(records[-1], EVIDENCE_STATUS_SCHEMA) if records else None
            if prior and prior["status"] in {"revoked", "superseded"} and status == "current":
                raise HarnessError("Evidence revocation cannot be reversed; register new evidence")
            row = {"schema_version": 1, "id": identifier, "generation": len(records) + 1,
                   "status": status, "previous_digest": digest(prior) if prior else None}
            dump(directory / (str(row["generation"]).zfill(8) + ".yaml"), row, EVIDENCE_STATUS_SCHEMA, exclusive=True)
            heads = safe_private(self.root / "evidence_heads", create=True)
            dump(heads / (identifier + ".yaml"), {"generation": row["generation"], "digest": digest(row)}, EVIDENCE_HEAD_SCHEMA)
            return row
        if locked:
            return append()
        with lock(self.root / ".lock", purpose="evidence invalidation"):
            return append()

    def invalidate_evidence(self, identifier, *, status="revoked"):
        self._require()
        return self._evidence_transition(identifier, status)

    def evidence_status(self, record):
        """Integrity and current lifecycle are independent; unavailable is unknown."""
        from .strict_yaml import load, validate
        if not self._evidence_current(record):
            return "stale"
        for ref in record.get("evidence_refs", []):
            typed = ref.get("reference")
            if not typed:
                return "unknown"
            try:
                validate(typed, TYPED_REF_SCHEMA)
                if (typed["project_id"] != self.repository_identity or typed["kind"] != "evidence"
                        or typed["id"] != ref["id"] or typed["generation"] != 1):
                    return "unknown"
                authoritative = load(safe_private(self.root / "evidence" / (typed["id"] + ".yaml")), EVIDENCE_RECORD_SCHEMA)
                if (digest(authoritative) != typed["content_digest"] or authoritative["sha256"] != ref["sha256"]
                        or authoritative["path"] != ref["path"] or authoritative["project_id"] != self.repository_identity
                        or authoritative["candidate_digest"] != record["provenance"]["snapshot_id"]
                        or (record.get("claim_kind") == "check_result" and authoritative["check_id"] != record.get("check_id"))):
                    return "stale"
                if authoritative.get("verification_method") == "core_command_check_v1":
                    self._current_core_check(authoritative["run_dir"], authoritative["check_id"], record["provenance"])
                files = sorted(safe_private(self.root / "evidence_status" / typed["id"]).glob("*.yaml"))
                head = load(safe_private(self.root / "evidence_heads" / (typed["id"] + ".yaml")), EVIDENCE_HEAD_SCHEMA)
                if (set(head) != {"generation", "digest"} or not files or len(files) > 4096
                        or head["generation"] != len(files)):
                    return "unknown"
                previous = None
                for generation, path in enumerate(files, 1):
                    row = load(safe_private(path), EVIDENCE_STATUS_SCHEMA)
                    if (set(row) != {"schema_version", "id", "generation", "status", "previous_digest"}
                            or row["schema_version"] != 1 or row["id"] != typed["id"] or row["generation"] != generation
                            or path.stem != str(generation).zfill(8)
                            or row["previous_digest"] != (digest(previous) if previous else None)):
                        return "unknown"
                    previous = row
                if digest(previous) != head["digest"]:
                    return "unknown"
                if previous["status"] != "current":
                    return previous["status"] if previous["status"] in {"revoked", "superseded"} else "unknown"
            except (OSError, ValueError, KeyError, TypeError, HarnessError):
                return "unknown"
        return "current" if record.get("evidence_refs") else "unknown"

    def maintenance(self, operation, *, scope, apply=False):
        """Explicit scoped dry-run/migration and conservative retention planning.

        Records are never rewritten. GC retains authoritative data until an
        authoritative complete external reachability inventory is available;
        memory alone cannot prove a run/packet no longer references a record.
        """
        self._require()
        if operation not in {"migrate", "gc"} or not isinstance(scope, list) or not scope:
            raise HarnessError("Maintenance requires an explicit operation and nonempty kind scope")
        choices = {"episode": ("episodes", EPISODE_SCHEMA), "semantic": ("semantic", SEMANTIC_SCHEMA),
                   "short_term": ("short_term", SHORT_SCHEMA), "fact": ("facts", FACT_SCHEMA)}
        if any(kind not in choices for kind in scope) or len(set(scope)) != len(scope):
            raise HarnessError("Unsupported or duplicate maintenance scope")
        self.diagnostics = []
        self.scan_metrics = []
        self._optional_scan_budget = {"started": time.monotonic(), "records": 0, "bytes": 0}
        refs = []
        for kind in scope:
            directory, schema = choices[kind]
            for row in self._records(directory, schema, optional=True):
                refs.append(typed_reference(self.repository_identity, kind, row, generation=row.get("goal_generation", 1)))
        refs.sort(key=lambda ref: (ref["kind"], ref["id"]))
        result = {"schema_version": 1, "operation": operation, "project_id": self.repository_identity,
                  "scope": scope, "dry_run": not apply, "recordset_digest": digest(refs), "records": refs,
                  "diagnostics": self.diagnostics, "complete": not self.diagnostics,
                  "deletions": [], "trust_policy": "legacy_assertions_remain_observations"}
        if operation == "gc":
            result["reason_code"] = "UNRESOLVED_EXTERNAL_REACHABILITY"
            result["retained_records"] = len(refs)
            # No model assertion or incomplete local scan authorizes deletion.
            return result
        if apply:
            from .strict_yaml import dump
            manifest = {**result, "created_at": now(), "migration_id": "migration-" + uuid.uuid4().hex}
            target = safe_private(self.root / "migrations", create=True) / (manifest["migration_id"] + ".yaml")
            with lock(self.root / ".lock", purpose="memory migration manifest"):
                # Revalidate every input immediately before publication.
                for ref in refs:
                    self.show(ref["id"], reference=ref, limit=256)
                dump(target, manifest, exclusive=True)
            result["manifest_path"] = str(target)
        return result


def memory_dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("show", "rebuild", "migrate", "gc", "register-check", "revoke-evidence", "file-fact", "symbol-fact"))
    parser.add_argument("record_id", nargs="?")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--check-id")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--path")
    parser.add_argument("--symbol")
    parser.add_argument("--scope", action="append", choices=("episode", "semantic", "short_term"), default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--store", required=True)
    parser.add_argument("--repository-identity", required=True)
    parser.add_argument("--kind", choices=("episode", "semantic", "short_term", "fact"))
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=8192)
    args = parser.parse_args(argv)
    memory = Memory(args.store, args.repository_identity, enabled=True)
    if args.command == "show" and not args.record_id:
        parser.error("show requires record_id")
    if args.command in {"file-fact", "symbol-fact"}:
        if not args.root or not args.path or args.command == "symbol-fact" and not args.symbol:
            parser.error("file-fact/symbol-fact require --root, --path and symbol-fact --symbol")
        value = (memory.record_file_fact(args.root, args.path, record_id=args.record_id)
                 if args.command == "file-fact" else
                 memory.record_symbol_fact(args.root, args.path, args.symbol, record_id=args.record_id))
    elif args.command == "register-check":
        if not args.run_dir or not args.check_id:
            parser.error("register-check requires --run-dir and --check-id")
        from .core import Run
        from .execution_ledger import provenance_for
        run = Run(args.run_dir)
        current = provenance_for(run.contract()["cwd"], run.assert_current(run.state(), run.contract()))
        value = memory.register_core_evidence(args.run_dir, args.check_id, current)
    elif args.command == "revoke-evidence":
        if not args.record_id:
            parser.error("revoke-evidence requires an evidence ID")
        value = memory.invalidate_evidence(args.record_id)
    elif args.command in {"migrate", "gc"}:
        value = memory.maintenance(args.command, scope=args.scope, apply=args.apply)
    else:
        value = memory.show(args.record_id, kind=args.kind, offset=args.offset, limit=args.limit) if args.command == "show" else memory.rebuild_index()
    from .output import write_json
    write_json(value)
    return 0
