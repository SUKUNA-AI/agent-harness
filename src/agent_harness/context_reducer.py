"""Bounded model-facing packets with retrievable provenance, never raw state."""
from __future__ import annotations

from collections import OrderedDict
import json

from .core import HarnessError, canonical, digest

DEFAULT_BUDGETS = {"file_snippet": 8192, "error_packet": 8192, "repo_map": 8192,
                   "memory": 8192, "handoff": 12288, "agent_context": 16384,
                   "tool_result": 8192}


def _size(value):
    return len(canonical(value)) + 2  # reserve Windows CRLF


def compact_provenance(value):
    if not isinstance(value, dict):
        return value
    return {**{key: value[key] for key in ("repository_identity", "revision", "snapshot_id", "toolchain_digest") if key in value},
            "raw_provenance_digest": digest(value), "file_details_omitted": len(value.get("files", {}))}


def reduce(records, *, category="tool_result", max_bytes=None, required=None):
    """Group equal observations and select complete records under a byte budget.

    Required user constraints are never truncated. Large observations are reduced
    to status/causality/provenance and an exact retrieval pointer. Omissions are
    explicit and do not count as successful observations.
    """
    if category not in DEFAULT_BUDGETS:
        raise HarnessError("Unknown context budget category")
    limit = DEFAULT_BUDGETS[category] if max_bytes is None else max_bytes
    if type(limit) is not int or not 1024 <= limit <= 1048576:
        raise HarnessError("Context budget must be 1024..1048576 UTF-8 bytes")
    if not isinstance(records, list) or len(records) > 100000:
        raise HarnessError("Context records must be a bounded list")
    base = {"schema_version": 1, "category": category, "trust": "untrusted_observations",
            "required": required or {}, "items": [], "omitted": 0, "omission_pointers": [],
            "duplicates_grouped": 0, "input_records": len(records), "next_offset": None,
            "max_bytes": limit}
    if _size(base) > limit:
        raise HarnessError("Required contract/constraints exceed context budget; explicit chunking required")
    groups = OrderedDict()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise HarnessError("Context observation must be an object")
        fingerprint = digest({key: value for key, value in record.items()
                              if key not in {"event_id", "timestamp", "pointer"}})
        if fingerprint in groups:
            item = groups[fingerprint]
            item["occurrences"] += 1
            item["last_occurrence"] = record.get("timestamp", record.get("event_id", index))
            base["duplicates_grouped"] += 1
        else:
            groups[fingerprint] = {"observation": record, "occurrences": 1,
                "first_occurrence": record.get("timestamp", record.get("event_id", index)),
                "last_occurrence": record.get("timestamp", record.get("event_id", index)),
                "source_offset": index}
    # Failures/constraints before successful and already examined observations.
    ordered = sorted(groups.values(), key=lambda item: (
        0 if item["observation"].get("kind") in {"failure", "repair_hypothesis", "decision"} else 1,
        item["source_offset"]))
    for group in ordered:
        record = group["observation"]
        if record.get("validity_status", "valid") != "valid":
            omitted = True
        else:
            omitted = False
            base["items"].append(group)
            if _size(base) > limit:
                base["items"].pop()
                payload = record.get("payload", {})
                summary = {"event_id": record.get("event_id"), "kind": record.get("kind"),
                    "status": payload.get("status", record.get("status", "unknown")),
                    "failure_signature": payload.get("signature"), "category": payload.get("category"),
                    "pointer": record.get("pointer"), "provenance": compact_provenance(record.get("provenance")),
                    "content_digest": digest(record), "excerpt_omitted": True,
                    "occurrences": group["occurrences"]}
                base["items"].append(summary)
                if not record.get("pointer") or _size(base) > limit:
                    base["items"].pop()
                    omitted = True
        if omitted:
            base["omitted"] += group["occurrences"]
            if base["next_offset"] is None:
                base["next_offset"] = group["source_offset"]
            pointer = {"offset": group["source_offset"], "event_id": record.get("event_id"),
                       "pointer": record.get("pointer"), "digest": digest(record),
                       "reason": "stale" if record.get("validity_status", "valid") != "valid" else "byte_budget"}
            base["omission_pointers"].append(pointer)
            if _size(base) > limit:
                base["omission_pointers"].pop()
    while _size(base) > limit and base["items"]:
        removed = base["items"].pop()
        base["omitted"] += removed.get("occurrences", 1)
    if _size(base) > limit:
        raise HarnessError("Context envelope exceeds budget; split required inputs")
    base["complete"] = base["omitted"] == 0 and not any(item.get("excerpt_omitted") for item in base["items"])
    # complete itself is inside the same output budget.
    if _size(base) > limit:
        base.pop("complete")
    return base


def successful_check(check_id, *, exit_code, fingerprint, evidence_id, raw_pointer):
    return {"kind": "check", "payload": {"check_id": check_id,
        "status": "passed" if exit_code == 0 else "failed", "exit_code": exit_code,
        "evidence_id": evidence_id}, "provenance": {"input_fingerprint": fingerprint},
        "pointer": raw_pointer}
