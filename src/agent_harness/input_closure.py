"""Bounded physical input closure for optional reuse, independent of Git ignores.

The acceptance scanner keeps its existing semantics. This deliberately small
proof reader fails closed instead of attempting a second unbounded indexer.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import time

from .core import HarnessError, digest
from .file_io import read_regular_file
from .execution_ledger import HASH, SHORT, object_schema, safe_private

MAX_FILES = 256
MAX_DIRECTORIES = 128
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 2.0

ENTRY_SCHEMA = object_schema({"kind": {"enum": ["file", "directory"]},
    "sha256": HASH, "bytes": {"type": "integer", "minimum": 0, "maximum": MAX_FILE_BYTES},
    "mode": {"type": "integer", "minimum": 0, "maximum": 4095}}, ("kind", "mode"))
ENTRY_SCHEMA["allOf"] = [{"if": {"properties": {"kind": {"const": "file"}}, "additionalProperties": True},
    "then": {"required": ["sha256", "bytes"]}, "else": {"not": {"anyOf": [{"required": ["sha256"]}, {"required": ["bytes"]}]}}}]
CLOSURE_SCHEMA = object_schema({"schema_version": {"const": 1}, "root": SHORT,
    "policy": {"const": "physical_regular_tree_including_ignored_v1"},
    "entries": {"type": "object", "maxProperties": MAX_FILES + MAX_DIRECTORIES,
        "propertyNames": {"type": "string", "pattern": r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*\\)(?!.*:).+$"},
        "additionalProperties": ENTRY_SCHEMA}, "file_count": {"type": "integer", "minimum": 0, "maximum": MAX_FILES},
    "directory_count": {"type": "integer", "minimum": 1, "maximum": MAX_DIRECTORIES},
    "total_bytes": {"type": "integer", "minimum": 0, "maximum": MAX_TOTAL_BYTES}, "digest": HASH},
    ("schema_version", "root", "policy", "entries", "file_count", "directory_count", "total_bytes", "digest"))


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def bounded_closure(root):
    """Hash every physical file, including ignored/hidden data; no partial proof.

    Links, special files, credential paths, external Git administrative pointers
    and exceeded budgets are unsupported. Callers must re-execute on failure.
    """
    root = safe_private(root)
    if not root.is_dir():
        raise HarnessError("Input closure root must be an existing ordinary directory")
    root = root.resolve(strict=True)
    started = time.monotonic()
    entries, observed, directories = {}, {}, {root: _stamp(root.lstat())}
    files, total = 0, 0
    pending = [root]
    while pending:
        directory = pending.pop()
        if time.monotonic() - started > MAX_SECONDS:
            raise HarnessError("Complete reuse closure exceeds time budget; re-execute")
        for child in sorted(directory.iterdir()):
            info = child.lstat()
            name = child.relative_to(root).as_posix()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise HarnessError("Complete reuse closure cannot follow links or special files")
            if ":" in name or "\\" in name:
                raise HarnessError("Reuse closure path is outside the portable supported subset")
            if stat.S_ISDIR(info.st_mode):
                if len(directories) >= MAX_DIRECTORIES:
                    raise HarnessError("Complete reuse closure exceeds directory budget")
                directories[child] = _stamp(info)
                entries[name] = {"kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
                pending.append(child)
                continue
            if (child.name.casefold().startswith(".env") or child.name.casefold() in {
                    "auth.json", "credentials", "credentials.json", "cookies.json", "id_rsa", "id_ed25519"}
                    or child.suffix.casefold() in {".pem", ".key", ".p12", ".pfx"}):
                raise HarnessError("Reuse closure refuses credential inputs")
            if name == ".git" or name.endswith("/objects/info/alternates") or name.endswith("/commondir"):
                raise HarnessError("External Git administrative dependencies are not a closed tree")
            files += 1
            if files > MAX_FILES or info.st_size > MAX_FILE_BYTES or total + info.st_size > MAX_TOTAL_BYTES:
                raise HarnessError("Complete reuse closure exceeds file/byte budget; re-execute")
            data = read_regular_file(child, max_bytes=MAX_FILE_BYTES)
            after = child.lstat()
            if _stamp(info) != _stamp(after):
                raise HarnessError("Input changed during closure proof")
            total += len(data)
            observed[child] = _stamp(after)
            entries[name] = {"kind": "file", "mode": stat.S_IMODE(info.st_mode),
                             "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if time.monotonic() - started > MAX_SECONDS:
        raise HarnessError("Complete reuse closure exceeds time budget; re-execute")
    for path, stamp in {**observed, **directories}.items():
        if _stamp(path.lstat()) != stamp:
            raise HarnessError("Input tree changed during closure proof")
    value = {"schema_version": 1, "root": str(root), "policy": "physical_regular_tree_including_ignored_v1",
             "entries": entries, "file_count": files, "directory_count": len(directories), "total_bytes": total}
    value["digest"] = digest(value)
    return value
