"""Bounded model-facing packets over an immutable ordered projection."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from .core import HarnessError, canonical, digest

DEFAULT_BUDGETS = {"file_snippet": 8192, "error_packet": 8192, "repo_map": 8192,
                   "memory": 8192, "handoff": 12288, "agent_context": 16384,
                   "tool_result": 8192}


def _size(value):
    return len(canonical(value)) + 2


def compact_provenance(value):
    if not isinstance(value, dict):
        return value
    return {**{key: value[key] for key in ("repository_identity", "revision", "snapshot_id", "toolchain_digest") if key in value},
            "raw_provenance_digest": digest(value), "file_details_omitted": len(value.get("files", {}))}


def reduce(records, *, category="tool_result", max_bytes=None, required=None, cursor=None, view="current", recordset_store=None,
           compact_reference=False):
    """Reduce without losing late evictions, with version-bound source continuation.

    The cursor addresses the ranked projection, never a raw input offset. Full
    input remains owned by the caller; pointers are explicitly advisory unless a
    caller provides a persisted recordset reference. Required data is indivisible.
    """
    if category not in DEFAULT_BUDGETS or view not in {"current", "diagnostic"}:
        raise HarnessError("Unknown context budget category or view")
    limit = DEFAULT_BUDGETS[category] if max_bytes is None else max_bytes
    if type(limit) is not int or not 1024 <= limit <= 1048576:
        raise HarnessError("BUDGET_TOO_SMALL: context budget must be 1024..1048576 UTF-8 bytes")
    if not isinstance(records, list) or len(records) > 100000:
        raise HarnessError("Context records must be a bounded list")
    # Strict UTF-8 encoding happens before any caller can publish state.
    recordset = digest(records)
    selection = digest({"category": category, "required": required or {}, "view": view, "ordering": 1})
    cursor_binding = digest({"recordset": recordset, "selection": selection})
    start = 0
    if cursor is not None:
        legacy_matches = (isinstance(cursor, dict) and set(cursor) == {"recordset", "selection", "position"}
                          and cursor["recordset"] == recordset and cursor["selection"] == selection)
        compact_matches = (isinstance(cursor, dict) and set(cursor) == {"binding", "position"}
                           and cursor["binding"] == cursor_binding)
        if (not (legacy_matches or compact_matches)
                or type(cursor["position"]) is not int or cursor["position"] < 0):
            raise HarnessError("STALE_CURSOR: immutable recordset or selection changed")
        start = cursor["position"]
    reference = None
    if recordset_store is not None:
        project = (required or {}).get("repository_identity") or digest({"store": str(Path(recordset_store).absolute())})
        value = {"schema_version": 1, "project_id": project, "records": records}
        reference = {"project_id": project, "kind": "context_recordset", "id": "recordset-" + digest(value),
                     "generation": 1, "content_digest": digest(value)}
        if compact_reference:
            # The content-addressed ID already carries the complete digest.
            reference.pop("content_digest")
    base = {"schema_version": 2, "category": category, "trust": "untrusted_observations",
            "recordset_digest": recordset, "recordset_ref": reference, "required": required or {}, "items": [], "omitted": 0,
            "omission_pointers": [], "duplicates_grouped": 0, "input_records": len(records),
            "next_offset": None, "next_cursor": None, "max_bytes": limit, "complete": False}
    if _size(base) > limit:
        raise HarnessError("BUDGET_TOO_SMALL: required constraints exceed context budget; explicit chunking required")
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
                "last_occurrence": record.get("timestamp", record.get("event_id", index)), "source_offset": index}
    ordered = sorted(groups.values(), key=lambda item: (
        0 if item["observation"].get("kind") in {"failure", "repair_hypothesis", "decision"} else 1,
        item["source_offset"]))
    if start > len(ordered):
        raise HarnessError("STALE_CURSOR: invalid projection position")
    omitted, selected = {}, []

    def omit(position, reason):
        # All paths, including late envelope eviction, use exactly this ledger.
        omitted[position] = reason

    def reconcile():
        base["omitted"] = sum(ordered[p]["occurrences"] for p in omitted)
        base["next_offset"] = min((ordered[p]["source_offset"] for p in omitted), default=None)
        pending = [p for p, reason in omitted.items() if reason == "byte_budget"]
        position = min(pending) if pending else None
        base["next_cursor"] = ({"recordset": recordset, "selection": selection, "position": position}
                               if position is not None else None)
        base["complete"] = not omitted and not any(i.get("excerpt_omitted") for i in base["items"])
        base["omission_pointers"] = []
        for p in sorted(omitted, key=lambda p: ordered[p]["source_offset"]):
            group, reason = ordered[p], omitted[p]
            row = group["observation"]
            base["omission_pointers"].append({"offset": group["source_offset"], "pointer": row.get("pointer"),
                                            "digest": digest(row), "reason": reason})

    for position in range(start, len(ordered)):
        group, record = ordered[position], ordered[position]["observation"]
        if view == "current" and record.get("validity_status", "valid") != "valid":
            omit(position, "stale")
            continue
        if any(reason == "byte_budget" for reason in omitted.values()):
            omit(position, "byte_budget")
            continue
        base["items"].append(group)
        selected.append(position)
        if _size(base) > limit:
            base["items"].pop(); selected.pop()
            payload = record.get("payload", {})
            summary = {"event_id": record.get("event_id"), "kind": record.get("kind"),
                "status": payload.get("status", record.get("status", "unknown")),
                "failure_signature": payload.get("signature"), "pointer": record.get("pointer"),
                "content_digest": digest(record), "excerpt_omitted": True, "occurrences": group["occurrences"]}
            # Preserve the useful bounded claim when reducing a Memory row,
            # rather than spending its entire summary on generic metadata.
            # Full input bytes remain in the immutable retained recordset.
            memory_record = payload.get("record") if record.get("kind") == "memory" else None
            if isinstance(memory_record, dict):
                claim = memory_record.get("claim")
                if isinstance(claim, str) and _size(claim) <= 512:
                    summary["claim"] = claim
            base["items"].append(summary); selected.append(position)
            if _size(base) > limit:
                summary.pop("claim", None)
            if (not record.get("pointer") and reference is None) or _size(base) > limit:
                base["items"].pop(); selected.pop(); omit(position, "byte_budget")
    reconcile()
    # Manifest is itself bounded. Keep earliest omission before dropping later
    # details. A caller-owned immutable recordset plus next_cursor addresses them.
    while True:
        while _size(base) > limit and len(base["omission_pointers"]) > 1:
            base["omission_pointers"].pop()
        if _size(base) > limit and reference is not None:
            # The immutable top-level reference already binds every original
            # record. Avoid duplicating long escaped absolute paths/digests in
            # the omission manifest; this index addresses its raw records list.
            base["omission_pointers"] = [
                {"recordset_index": item.get("recordset_index", item.get("offset")), "reason": item["reason"]}
                for item in base["omission_pointers"]]
            if base["next_cursor"] is not None:
                # One hash binds the same recordset/selection as the legacy
                # cursor, without duplicating both digests in a small packet.
                base["next_cursor"] = {"binding": cursor_binding, "position": base["next_cursor"]["position"]}
        if _size(base) <= limit:
            break
        if not selected:
            if reference is not None and base["next_cursor"] is not None:
                base["next_cursor"] = None
                base["continuation_requires_source_read"] = True
                if _size(base) <= limit:
                    break
            raise HarnessError("BUDGET_TOO_SMALL: envelope and continuation need explicit chunking")
        position = selected.pop(); base["items"].pop(); omit(position, "byte_budget"); reconcile()
    # A source-only response has an explicit read path, not a cursor that can
    # repeat the same empty page forever at the same accepted byte budget.
    if not base["items"] and base["next_cursor"] and base["next_cursor"]["position"] == start:
        base["next_cursor"] = None
        base["continuation_requires_source_read"] = True
        if _size(base) > limit:
            raise HarnessError("BUDGET_TOO_SMALL: source-read continuation exceeds budget")
    if recordset_store is not None:
        retain_recordset(recordset_store, records, project_id=reference["project_id"])
    return base


def retain_recordset(store, records, *, project_id=None):
    """Publish immutable complete inputs in an explicitly authorized private store."""
    from .execution_ledger import HASH, object_schema, safe_private
    from .strict_yaml import dump, load
    project_id = project_id or digest({"store": str(Path(store).absolute())})
    recordset = {"schema_version": 1, "project_id": project_id, "records": records}
    identifier = "recordset-" + digest(recordset)
    schema = object_schema({"schema_version": {"const": 1}, "project_id": HASH,
                            "records": {"type": "array", "maxItems": 100000,
                                        "items": {"type": "object", "additionalProperties": True}}},
                           ("schema_version", "project_id", "records"))
    # Validate bytes/schema/size before store creation or publication.
    from .strict_yaml import dumps
    dumps(recordset, schema)
    directory = safe_private(store, create=True)
    path = safe_private(directory / (identifier + ".yaml"))
    try:
        dump(path, recordset, schema, exclusive=True)
    except FileExistsError:
        if load(path, schema) != recordset:
            raise HarnessError("RECORDSET_ID_CONFLICT")
    return {"project_id": project_id, "kind": "context_recordset", "id": identifier,
            "generation": 1, "content_digest": digest(recordset)}


def retrieve_recordset(store, reference, *, offset=0, limit=8192):
    """Safe store mapping: typed ref, not an arbitrary path from model text."""
    from .execution_ledger import safe_private, paginate
    from .strict_yaml import load
    import re
    keys = {"project_id", "kind", "id", "generation"}
    if (not isinstance(reference, dict) or set(reference) not in (keys, keys | {"content_digest"})
            or reference["kind"] != "context_recordset" or reference["generation"] != 1
            or not isinstance(reference["id"], str) or not re.fullmatch(r"recordset-[a-f0-9]{64}", reference["id"])):
        raise HarnessError("INVALID_RECORDSET_REFERENCE")
    path = safe_private(Path(store) / (reference["id"] + ".yaml"))
    value = load(path)
    expected = reference["id"][len("recordset-"):]
    if (value.get("project_id") != reference["project_id"] or digest(value) != expected
            or reference.get("content_digest", expected) != expected):
        raise HarnessError("STALE_REFERENCE: context recordset identity changed")
    return paginate(value, offset=offset, limit=limit)


def successful_check(check_id, *, exit_code, fingerprint, evidence_id, raw_pointer):
    return {"kind": "check", "payload": {"check_id": check_id,
        "status": "passed" if exit_code == 0 else "failed", "exit_code": exit_code,
        "evidence_id": evidence_id}, "provenance": {"input_fingerprint": fingerprint}, "pointer": raw_pointer}
