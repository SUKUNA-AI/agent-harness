"""Bounded derived checkpoint index and explicit configurable native scan budgets.

Records remain authoritative. Every selected record is re-read and content-bound;
the index only ranks candidates. Incomplete inventory/scans never claim latest
across the store. Writers serialize record/index updates under Memory's lease.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
import time

from .core import HarnessError, digest
from .execution_ledger import HASH, ID, IDENTIFIER, SHORT, object_schema, safe_private, sanitized
from .file_io import read_regular_file
from .strict_yaml import dump, load, loads, validate

DEFAULT_LIMITS = {"max_records": 128, "max_bytes": 2 * 1024 * 1024, "max_record_bytes": 65536,
                  "deadline_ms": 500, "returned_count": 3, "max_inventory_entries": 512}
MEMORY_DEFAULT_LIMITS = {**DEFAULT_LIMITS, "max_records": 512, "max_bytes": 4 * 1024 * 1024,
                         "max_record_bytes": 131072, "deadline_ms": 1000}
LIMITS_SCHEMA = object_schema({"max_records": {"type": "integer", "minimum": 1, "maximum": 1024},
    "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 16 * 1024 * 1024},
    "max_record_bytes": {"type": "integer", "minimum": 512, "maximum": 2 * 1024 * 1024},
    "deadline_ms": {"type": "integer", "minimum": 1, "maximum": 10000},
    "returned_count": {"type": "integer", "minimum": 1, "maximum": 16},
    "max_inventory_entries": {"type": "integer", "minimum": 1, "maximum": 1024}}, tuple(DEFAULT_LIMITS))
CONFIG_SCHEMA = object_schema({"schema_version": {"const": 1}, "generation": {"type": "integer", "minimum": 1},
    "checkpoint_scan": LIMITS_SCHEMA, "memory_scan": LIMITS_SCHEMA},
    ("schema_version", "generation", "checkpoint_scan", "memory_scan"))
STAMP_SCHEMA = object_schema({key: {"type": "integer"} for key in ("device", "inode", "size", "mtime_ns", "ctime_ns")},
                             ("device", "inode", "size", "mtime_ns", "ctime_ns"))
REFERENCE_SCHEMA = object_schema({"project_id": HASH, "kind": {"const": "short_term"}, "id": IDENTIFIER,
    "generation": {"type": "integer", "minimum": 1}, "content_digest": HASH},
    ("project_id", "kind", "id", "generation", "content_digest"))
ENTRY_SCHEMA = object_schema({"id": IDENTIFIER, "reference": REFERENCE_SCHEMA, "updated_at": SHORT, "content_digest": HASH,
    "file_sha256": HASH, "goal_generation": {"type": "integer", "minimum": 1}},
    ("id", "reference", "updated_at", "content_digest", "file_sha256", "goal_generation"))
INDEX_SCHEMA = object_schema({"schema_version": {"const": 1}, "project_id": HASH,
    "generation": {"type": "integer", "minimum": 1}, "index_digest": HASH, "limits_digest": HASH, "inventory_digest": HASH,
    "inventory": {"type": "object", "maxProperties": 1024, "additionalProperties": STAMP_SCHEMA},
    "inventory_complete": {"type": "boolean"}, "scan_complete": {"type": "boolean"},
    "next_position": {"type": "integer", "minimum": 0},
    "entries": {"type": "array", "maxItems": 1024, "items": ENTRY_SCHEMA},
    "invalid_records": {"type": "integer", "minimum": 0}},
    ("schema_version", "project_id", "generation", "index_digest", "limits_digest", "inventory_digest", "inventory", "inventory_complete",
     "scan_complete", "next_position", "entries", "invalid_records"))


def checked_limits(value=None, *, memory=False):
    result = {**(MEMORY_DEFAULT_LIMITS if memory else DEFAULT_LIMITS), **(value or {})}
    validate(result, LIMITS_SCHEMA)
    if result["max_record_bytes"] > result["max_bytes"]:
        raise HarnessError("Record byte cap cannot exceed the total byte cap")
    return result


def settings(state):
    path = safe_private(Path(state) / "native-context-config.yaml")
    if path.exists():
        value = load(path, CONFIG_SCHEMA)
        checked_limits(value["checkpoint_scan"])
        checked_limits(value["memory_scan"], memory=True)
        return value
    return {"schema_version": 1, "generation": 1, "checkpoint_scan": dict(DEFAULT_LIMITS),
            "memory_scan": dict(MEMORY_DEFAULT_LIMITS)}


def configure(state, *, checkpoint_scan=None, memory_scan=None, expected_generation=None):
    """Explicit private configuration; no Codex model/security/profile settings."""
    from .native_observability import observer_lock
    state = safe_private(state, create=True)
    with observer_lock(state / "native-context-config.lock", purpose="native context limits"):
        prior = settings(state)
        if expected_generation is not None and expected_generation != prior["generation"]:
            raise HarnessError("CONTEXT_CONFIG_GENERATION_CONFLICT")
        row = {"schema_version": 1, "generation": prior["generation"] + 1,
               "checkpoint_scan": checked_limits({**prior["checkpoint_scan"], **(checkpoint_scan or {})}),
               "memory_scan": checked_limits({**prior["memory_scan"], **(memory_scan or {})}, memory=True)}
        dump(state / "native-context-config.yaml", row, CONFIG_SCHEMA)
    return row


def _stamp(info):
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


class CheckpointIndex:
    def __init__(self, memory_root, project_id, *, limits=None):
        self.root, self.project = safe_private(memory_root), project_id
        self.directory = safe_private(self.root / "short_term")
        self.path = safe_private(self.root / "checkpoint-index.yaml")
        self.limits = checked_limits(limits)

    def _inventory(self, deadline, metrics):
        inventory, complete = {}, True
        if not self.directory.exists():
            return inventory, complete
        with os.scandir(self.directory) as entries:
            iterator = iter(entries)
            examined = 0
            while True:
                # Every directory entry consumes budget, including ignored
                # names, directories and malformed optional records. Do not
                # advance the iterator after its deadline or entry cap.
                if examined >= self.limits["max_inventory_entries"] or time.monotonic() >= deadline:
                    complete = False
                    break
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                examined += 1
                metrics["inventory_entries_examined"] += 1
                if not entry.name.endswith(".yaml"):
                    continue
                try:
                    if not re.fullmatch(ID, entry.name[:-5]):
                        raise HarnessError("Invalid optional checkpoint ID")
                    sanitized(entry.name)
                except HarnessError:
                    complete = False
                    continue
                # lstat never follows optional unsafe records. Opening/validation
                # later decides whether a particular candidate is usable.
                try:
                    inventory[entry.name] = _stamp(entry.stat(follow_symlinks=False))
                except OSError:
                    complete = False
        return dict(sorted(inventory.items())), complete

    def _read_record(self, name, metrics, schema):
        path = safe_private(self.directory / name)
        raw = read_regular_file(path, max_bytes=self.limits["max_record_bytes"])
        metrics["source_bytes_read"] += len(raw)
        row = loads(raw, schema)
        if row["id"] + ".yaml" != name or row["repository_identity"] != self.project:
            raise HarnessError("CHECKPOINT_RECORD_BINDING_MISMATCH")
        return row, {"id": row["id"], "updated_at": row["updated_at"], "content_digest": digest(row),
                     "file_sha256": hashlib.sha256(raw).hexdigest(), "goal_generation": row.get("goal_generation", 1),
                     "reference": {"project_id": self.project, "kind": "short_term", "id": row["id"],
                                   "generation": row.get("goal_generation", 1), "content_digest": digest(row)}}

    def _load_index(self, metrics):
        try:
            raw = read_regular_file(self.path, max_bytes=2 * 1024 * 1024)
            metrics["index_bytes_read"] += len(raw)
            index = loads(raw, INDEX_SCHEMA)
            if index["project_id"] != self.project or index["inventory_digest"] != digest(index["inventory"]):
                raise HarnessError("CHECKPOINT_INDEX_BINDING_MISMATCH")
            if digest({key: value for key, value in index.items() if key != "index_digest"}) != index["index_digest"]:
                raise HarnessError("CHECKPOINT_INDEX_INTEGRITY_MISMATCH")
            ids = [entry["id"] for entry in index["entries"]]
            prefix = list(index["inventory"])[:index["next_position"]]
            if (len(set(ids)) != len(ids) or len(ids) + index["invalid_records"] != index["next_position"]
                    or index["next_position"] > len(index["inventory"])
                    or any(identifier + ".yaml" not in prefix for identifier in ids)
                    or index["scan_complete"] and (not index["inventory_complete"] or index["next_position"] != len(index["inventory"]))):
                raise HarnessError("CHECKPOINT_INDEX_COMPLETENESS_MISMATCH")
            return index
        except (OSError, ValueError, HarnessError):
            return None

    def _rebuild(self, prior, inventory, complete, deadline, metrics, schema, cursor=None, cache=None):
        position, entries, invalid = 0, [], 0
        generation = (prior["generation"] if prior else 0) + 1
        if cursor is not None:
            if (not prior or not isinstance(cursor, dict) or set(cursor) != {"generation", "inventory", "position", "limits"}
                    or cursor["generation"] != prior["generation"] or cursor["inventory"] != digest(inventory)
                    or cursor["limits"] != digest(self.limits) or cursor["position"] != prior["next_position"]
                    or not prior["inventory_complete"] or not complete):
                raise HarnessError("STALE_CURSOR: checkpoint index inputs changed")
            position, entries, invalid = prior["next_position"], list(prior["entries"]), prior["invalid_records"]
        names = list(inventory)
        while position < len(names):
            size = inventory[names[position]]["size"]
            if size > self.limits["max_record_bytes"] and metrics["records_examined"] < self.limits["max_records"]:
                metrics["records_examined"] += 1
                invalid += 1
                position += 1
                continue
            if (metrics["records_examined"] >= self.limits["max_records"] or time.monotonic() >= deadline
                    or metrics["source_bytes_read"] + min(size, self.limits["max_record_bytes"] + 1) > self.limits["max_bytes"]):
                break
            name = names[position]
            metrics["records_examined"] += 1
            position += 1
            try:
                source_row, summary = self._read_record(name, metrics, schema)
                if cache is not None:
                    cache[source_row["id"]] = (source_row, summary)
                entries.append(summary)
            except (OSError, ValueError, HarnessError):
                invalid += 1
        after, after_complete = self._inventory(deadline, metrics)
        stable = complete and after_complete and after == inventory
        row = {"schema_version": 1, "project_id": self.project, "generation": generation,
               "limits_digest": digest(self.limits), "inventory_digest": digest(inventory), "inventory": inventory,
               "inventory_complete": stable, "scan_complete": stable and position == len(names),
               "next_position": position, "entries": entries, "invalid_records": invalid}
        row["index_digest"] = digest(row)
        dump(self.path, row, INDEX_SCHEMA)
        return row

    def select(self, *, exclude_session=None, cursor=None, rebuild=False):
        from .memory import SHORT_SCHEMA
        from .native_observability import observer_lock
        start, cpu = time.monotonic(), time.process_time()
        deadline = start + self.limits["deadline_ms"] / 1000
        metrics = {"records_examined": 0, "source_bytes_read": 0, "index_bytes_read": 0,
                   "inventory_entries_examined": 0, "index_hit": False}
        diagnostics, selected, cache = [], [], {}
        with observer_lock(self.root / ".lock", purpose="checkpoint index selection", timeout=self.limits["deadline_ms"] / 1000):
            inventory, complete = self._inventory(deadline, metrics)
            # A complete inventory containing only the excluded checkpoint
            # establishes that there is no foreign context to rank or read.
            if (complete and exclude_session is not None and cursor is None and not rebuild
                    and inventory.keys() == {exclude_session + ".yaml"}):
                metrics.update(inventory_complete=True, scan_complete=True, selection_scope="all_foreign_records",
                               index_generation=None, invalid_records=0, next_cursor=None, terminal_diagnostic=None,
                               wall_ms=(time.monotonic() - start) * 1000, cpu_ms=(time.process_time() - cpu) * 1000)
                return selected, diagnostics, metrics
            prior = self._load_index(metrics)
            current = (prior is not None and prior["limits_digest"] == digest(self.limits)
                       and prior["inventory"] == inventory and prior["inventory_complete"] and complete)
            if cursor is not None or rebuild or not current:
                index = self._rebuild(prior, inventory, complete, deadline, metrics, SHORT_SCHEMA, cursor, cache)
            else:
                index = prior
                metrics["index_hit"] = True
            candidates = sorted(index["entries"], key=lambda row: (row["updated_at"], row["id"]), reverse=True)
            for candidate in candidates:
                if candidate["id"] == exclude_session:
                    continue
                if len(selected) >= self.limits["returned_count"]:
                    break
                cached = cache.get(candidate["id"])
                if cached is None and (time.monotonic() >= deadline or metrics["records_examined"] >= self.limits["max_records"]
                        or metrics["source_bytes_read"] + index["inventory"][candidate["id"] + ".yaml"]["size"] > self.limits["max_bytes"]):
                    diagnostics.append({"reason": "checkpoint_selection_bound"})
                    break
                try:
                    if cached is not None:
                        row, checked = cached
                    else:
                        metrics["records_examined"] += 1
                        row, checked = self._read_record(candidate["id"] + ".yaml", metrics, SHORT_SCHEMA)
                    if checked != candidate:
                        raise HarnessError("CHECKPOINT_INDEX_CONTENT_CHANGED")
                    selected.append((row, self.directory / (candidate["id"] + ".yaml")))
                except (OSError, ValueError, HarnessError):
                    diagnostics.append({"reason": "invalid_foreign_checkpoint"})
            if index["invalid_records"]:
                diagnostics.append({"reason": "invalid_foreign_checkpoint", "count": index["invalid_records"]})
            if not index["scan_complete"]:
                diagnostics.append({"reason": "foreign_scan_bound", "complete": False})
            metrics.update(inventory_complete=index["inventory_complete"], scan_complete=index["scan_complete"],
                           selection_scope="all_indexed_records" if index["scan_complete"] else "bounded_scanned_records",
                           index_generation=index["generation"], invalid_records=index["invalid_records"],
                           next_cursor=({"generation": index["generation"], "inventory": index["inventory_digest"],
                                         "position": index["next_position"], "limits": digest(self.limits)}
                                        if index["inventory_complete"] and not index["scan_complete"] else None),
                           terminal_diagnostic="checkpoint_scope_must_be_narrowed" if not index["inventory_complete"] else None)
        metrics.update(wall_ms=(time.monotonic() - start) * 1000, cpu_ms=(time.process_time() - cpu) * 1000)
        return selected, diagnostics, metrics

    def update(self, row):
        """Called with Memory's writer lease held, after publishing a checkpoint."""
        metrics = {"inventory_entries_examined": 0, "index_bytes_read": 0}
        prior = self._load_index(metrics)
        if prior is None:
            return  # First reader performs a bounded rebuild.
        deadline = time.monotonic() + self.limits["deadline_ms"] / 1000
        inventory, complete = self._inventory(deadline, metrics)
        others_old = {name: stamp for name, stamp in prior["inventory"].items() if name != row["id"] + ".yaml"}
        others_new = {name: stamp for name, stamp in inventory.items() if name != row["id"] + ".yaml"}
        path = safe_private(self.directory / (row["id"] + ".yaml"))
        raw = read_regular_file(path, max_bytes=self.limits["max_record_bytes"])
        summary = {"id": row["id"], "updated_at": row["updated_at"], "content_digest": digest(row),
                   "file_sha256": hashlib.sha256(raw).hexdigest(), "goal_generation": row.get("goal_generation", 1),
                     "reference": {"project_id": self.project, "kind": "short_term", "id": row["id"],
                                   "generation": row.get("goal_generation", 1), "content_digest": digest(row)}}
        valid = complete and prior["scan_complete"] and others_new == others_old and prior["limits_digest"] == digest(self.limits)
        entries = [entry for entry in prior["entries"] if entry["id"] != row["id"]]
        if len(entries) < 1024:
            entries.append(summary)
        else:
            valid = False
        updated = {**prior, "generation": prior["generation"] + 1, "inventory": inventory,
                   "inventory_digest": digest(inventory), "inventory_complete": complete,
                   "scan_complete": valid, "entries": entries, "next_position": len(inventory) if valid else 0}
        updated["index_digest"] = digest({key: value for key, value in updated.items() if key != "index_digest"})
        dump(self.path, updated, INDEX_SCHEMA)
