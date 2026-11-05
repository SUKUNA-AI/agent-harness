"""Deterministic narrow facts. No dependency graph or application behavior claims.

An ordinary-file fact depends on exactly its bytes and no-link read policy.
A Python symbol-location fact additionally depends on the parser and absence of
other declarations with the same qualified name in that exact file. Builders and
verifiers use the same closed deterministic procedure, never an authored file list.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import platform
import re
import sys

from .context import _path, _read
from .core import HarnessError, digest, now
from .execution_ledger import HASH, IDENTIFIER, PROVENANCE_SCHEMA, SHORT, object_schema, repository_identity, safe_private
from .file_io import read_regular_file

MAX_FACT_FILE_BYTES = 1024 * 1024
PATH = {"type": "string", "minLength": 1, "maxLength": 512}
NULL_TEXT = {"type": ["string", "null"], "maxLength": 512}
POSITIVE_SCHEMA = object_schema({"path": PATH, "sha256": HASH, "size_bytes": {"type": "integer", "minimum": 0}},
                                ("path", "sha256", "size_bytes"))
NEGATIVE_SCHEMA = object_schema({"kind": {"enum": ["no_link_ancestors", "symbol_uniqueness"]}, "path": PATH,
    "symbol": NULL_TEXT, "matching_declarations": {"type": ["integer", "null"], "minimum": 0}},
    ("kind", "path", "symbol", "matching_declarations"))
LOCATION_SCHEMA = object_schema({"start_line": {"type": "integer", "minimum": 1},
    "end_line": {"type": "integer", "minimum": 1}, "start_column": {"type": "integer", "minimum": 0},
    "end_column": {"type": "integer", "minimum": 0}, "column_encoding": {"const": "utf8_bytes_zero_based"}},
    ("start_line", "end_line", "start_column", "end_column", "column_encoding"))
CLOSURE_SCHEMA = object_schema({"schema_version": {"const": 1}, "binding_kind": {"const": "deterministic_claim"},
    "builder": {"enum": ["ordinary-file-bytes-v1", "python-symbol-location-v1"]}, "builder_digest": HASH,
    "policy_digest": HASH, "project_id": HASH, "checkout_ref": HASH, "physical_root": object_schema({
        "device": {"type": "integer", "minimum": 0}, "inode": {"type": "integer", "minimum": 0}}, ("device", "inode")),
    "subject": PATH, "symbol": NULL_TEXT, "parser_version": NULL_TEXT,
    "positive_dependencies": {"type": "array", "minItems": 1, "maxItems": 1, "items": POSITIVE_SCHEMA},
    "negative_dependencies": {"type": "array", "minItems": 1, "maxItems": 2, "items": NEGATIVE_SCHEMA},
    "config_refs": object_schema({"reader_policy": HASH, "parser_policy": {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"}},
                                  ("reader_policy", "parser_policy")),
    "closure_complete": {"const": True}},
    ("schema_version", "binding_kind", "builder", "builder_digest", "policy_digest", "project_id", "checkout_ref",
     "physical_root", "subject", "symbol", "parser_version", "positive_dependencies", "negative_dependencies", "config_refs", "closure_complete"))
FACT_SCHEMA = object_schema({"schema_version": {"const": 1}, "id": IDENTIFIER, "repository_identity": HASH,
    "claim_kind": {"enum": ["exact_file_content", "exact_symbol_location"]},
    "verification_rule": {"const": "deterministic-claim-v1"}, "created_at": SHORT,
    "validity_status": {"enum": ["valid", "needs_revalidation", "superseded", "rejected", "expired"]},
    "provenance": PROVENANCE_SCHEMA, "closure": CLOSURE_SCHEMA, "closure_digest": HASH,
    "location": {"anyOf": [{"type": "null"}, LOCATION_SCHEMA]}},
    ("schema_version", "id", "repository_identity", "claim_kind", "verification_rule", "created_at", "validity_status",
     "provenance", "closure", "closure_digest", "location"))


def _fingerprint_module(name):
    import importlib
    module = importlib.import_module(name)
    path = Path(module.__file__).resolve(strict=True)
    return hashlib.sha256(read_regular_file(path, max_bytes=2 * 1024 * 1024)).hexdigest()


def _policies(symbol):
    reader = digest({"context": _fingerprint_module("agent_harness.context"),
                     "file_io": _fingerprint_module("agent_harness.file_io"),
                     "policy": "ordinary-file-no-links-max1MiB-v1"})
    parser_version = None
    parser = None
    if symbol is not None:
        parser_version = platform.python_implementation() + " " + platform.python_version()
        parser = digest({"version": parser_version, "ast": _fingerprint_module("ast"),
                         "grammar": list(sys.version_info[:2]), "type_comments": True,
                         "policy": "qualified-declarations-unique-in-file-v1"})
    builder = _fingerprint_module(__name__)
    return reader, parser, parser_version, builder


def _declarations(tree, prefix=()):
    rows = []
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = (*prefix, child.name)
            rows.append((".".join(name), child))
            rows.extend(_declarations(child, name))
        else:
            rows.extend(_declarations(child, prefix))
    return rows


def _build(root, path, current, symbol):
    root = safe_private(Path(root).absolute()).resolve(strict=True)
    if current.get("checkout_root") != str(root) or repository_identity(root) != current.get("repository_identity"):
        raise HarnessError("CLAIM_PROJECT_BINDING_MISMATCH")
    if symbol is not None and (not isinstance(symbol, str) or len(symbol) > 256 or
                              not all(re.fullmatch(r"[A-Za-z_]\w*", part) for part in symbol.split("."))):
        raise HarnessError("Unsupported qualified symbol")
    physical = root.stat()
    source = _path(root, path)
    data, size = _read(source, MAX_FACT_FILE_BYTES, root=root, name=path)
    if data is None:
        raise HarnessError("CLAIM_SOURCE_BYTE_BOUND")
    reader, parser, parser_version, builder = _policies(symbol)
    closure = {"schema_version": 1, "binding_kind": "deterministic_claim",
        "builder": "ordinary-file-bytes-v1" if symbol is None else "python-symbol-location-v1",
        "builder_digest": builder, "policy_digest": digest({"reader": reader, "parser": parser}),
        "project_id": current["repository_identity"], "checkout_ref": current["checkout_ref"],
        "physical_root": {"device": physical.st_dev, "inode": physical.st_ino}, "subject": path, "symbol": symbol,
        "parser_version": parser_version,
        "positive_dependencies": [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": size}],
        "negative_dependencies": [{"kind": "no_link_ancestors", "path": path, "symbol": None, "matching_declarations": None}],
        "config_refs": {"reader_policy": reader, "parser_policy": parser}, "closure_complete": True}
    location = None
    if symbol is not None:
        if Path(path).suffix not in {".py", ".pyi"}:
            raise HarnessError("Unsupported parser language; use whole-snapshot observation")
        try:
            tree = ast.parse(data.decode("utf-8"), type_comments=True, feature_version=sys.version_info[:2])
        except (UnicodeDecodeError, SyntaxError, ValueError) as exc:
            raise HarnessError("CLAIM_PARSER_UNAVAILABLE") from exc
        matches = [node for name, node in _declarations(tree) if name == symbol]
        if len(matches) != 1:
            raise HarnessError("CLAIM_SYMBOL_MISSING_OR_AMBIGUOUS")
        node = matches[0]
        location = {"start_line": node.lineno, "end_line": node.end_lineno,
                    "start_column": node.col_offset, "end_column": node.end_col_offset,
                    "column_encoding": "utf8_bytes_zero_based"}
        closure["negative_dependencies"].append({"kind": "symbol_uniqueness", "path": path, "symbol": symbol,
                                                "matching_declarations": 1})
    # Recheck exact input after parsing; facts never attest to an intermediate read.
    again, _ = _read(source, MAX_FACT_FILE_BYTES, root=root, name=path)
    if data != again or root.stat().st_ino != physical.st_ino or root.stat().st_dev != physical.st_dev:
        raise HarnessError("CLAIM_SOURCE_CHANGED_DURING_VERIFICATION")
    return closure, location


def build_fact(root, path, current, *, symbol=None, record_id=None):
    from .strict_yaml import validate
    closure, location = _build(root, path, current, symbol)
    row = {"schema_version": 1, "id": record_id or "fact-" + digest({"closure": closure, "location": location})[:32],
        "repository_identity": current["repository_identity"],
        "claim_kind": "exact_file_content" if symbol is None else "exact_symbol_location",
        "verification_rule": "deterministic-claim-v1", "created_at": now(), "validity_status": "valid",
        "provenance": current, "closure": closure, "closure_digest": digest(closure), "location": location}
    validate(row, FACT_SCHEMA)
    return row


def verify_fact(record, current):
    """Recompute the full supported closure. An authored complete flag proves nothing."""
    from .strict_yaml import validate
    try:
        validate(record, FACT_SCHEMA)
        closure = record["closure"]
        if record["validity_status"] != "valid":
            return {"source_validity": "unknown", "reason_code": "CLAIM_LIFECYCLE_INACTIVE"}
        if (record["repository_identity"] != current.get("repository_identity") or
                closure["project_id"] != current.get("repository_identity") or
                closure["checkout_ref"] != current.get("checkout_ref") or digest(closure) != record["closure_digest"]):
            return {"source_validity": "changed", "reason_code": "CLAIM_BINDING_CHANGED"}
        root = current.get("checkout_root")
        if not isinstance(root, str):
            return {"source_validity": "unknown", "reason_code": "CLAIM_CURRENT_ROOT_UNAVAILABLE"}
        rebuilt, location = _build(root, closure["subject"], current, closure["symbol"])
        expected_kind = "exact_symbol_location" if closure["symbol"] is not None else "exact_file_content"
        if rebuilt != closure or location != record["location"] or expected_kind != record["claim_kind"]:
            return {"source_validity": "changed", "reason_code": "CLAIM_CLOSURE_CHANGED"}
        return {"source_validity": "current", "reason_code": "DETERMINISTIC_CLAIM_VERIFIED"}
    except (OSError, ValueError, KeyError, TypeError, HarnessError):
        return {"source_validity": "unknown", "reason_code": "CLAIM_REVALIDATION_UNAVAILABLE"}
