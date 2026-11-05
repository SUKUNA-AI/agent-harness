"""Actual local producer/build identity for new native receipts.

Package bytes and the loaded producer's Python code are distinct observations.
Neither Git HEAD nor requested client/model settings stand in for runtime proof.
"""
from __future__ import annotations

import hashlib
import importlib
import marshal
from pathlib import Path
import sys
import types

from . import __version__
from .core import HarnessError, digest, now
from .execution_ledger import HASH, IDENTIFIER, SHORT, object_schema
from .file_io import read_regular_file

NULL_ID = {"type": ["string", "null"], "maxLength": 128}
NULL_HASH = {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"}
INPUT_REF_SCHEMA = object_schema({"kind": SHORT, "id": SHORT, "content_digest": HASH, "scope": SHORT},
                                 ("kind", "id", "content_digest", "scope"))
IDENTITY_PROPERTIES = {
    "identity_contract": {"const": "native-receipt-identity-v1"},
    "record_id": IDENTIFIER, "project_id": HASH, "payload_digest": HASH, "record_digest": HASH,
    "run_id": NULL_ID, "task_id": NULL_ID, "plan_digest": NULL_HASH, "node_id": NULL_ID,
    "attempt_id": NULL_ID, "invocation_id": NULL_ID, "producer_id": IDENTIFIER, "producer_kind": SHORT,
    "generation": {"type": "integer", "minimum": 1}, "created_at": SHORT,
    "harness_version": SHORT, "harness_build_digest": HASH,
    "build_provenance": object_schema({"package_digest": HASH, "producer_code_digest": HASH,
        "package_files_examined": {"type": "integer", "minimum": 1},
        "source": {"const": "on_disk_package_and_loaded_producer"}},
        ("package_digest", "producer_code_digest", "package_files_examined", "source")),
    "client_version": {"type": ["string", "null"], "maxLength": 128},
    "capability_receipt_ref": {"type": ["string", "null"], "maxLength": 512},
    "input_refs": {"type": "array", "maxItems": 16, "items": INPUT_REF_SCHEMA},
}
IDENTITY_REQUIRED = tuple(IDENTITY_PROPERTIES)


def _package_manifest(package_dir=None):
    root = Path(package_dir).absolute() if package_dir is not None else Path(__file__).parent
    names = set(root.rglob("*.py"))
    vendor = root / "_vendor"
    if vendor.is_dir():
        names.update(path for path in vendor.rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix != ".pyc")
    schema_root = root.parent.parent / "schemas"
    installed_schemas = root / "schemas"
    schema_paths = list(schema_root.glob("*.json")) if schema_root.is_dir() else list(installed_schemas.glob("*.json"))
    names.update(schema_paths)
    if not names or len(names) > 512:
        raise HarnessError("HARNESS_BUILD_INVENTORY_BOUND")
    result, total = {}, 0
    for path in sorted(names):
        if any(part == "__pycache__" for part in path.parts):
            continue
        data = read_regular_file(path, max_bytes=2 * 1024 * 1024)
        total += len(data)
        if total > 16 * 1024 * 1024:
            raise HarnessError("HARNESS_BUILD_BYTE_BOUND")
        name = path.relative_to(root).as_posix() if path.is_relative_to(root) else "schemas/" + path.name
        result[name] = hashlib.sha256(data).hexdigest()
    return result


def package_build_digest(package_dir=None):
    """Path-independent digest of actual Python, vendored assets and JSON schemas.

    Optional package_dir is the agent_harness directory (not the repository
    root). Source sibling schemas and installed package/schemas map to the same
    manifest names. External interpreters/native artifacts are not silently
    included; their runtime identity belongs in separate execution input refs.
    """
    return digest(_package_manifest(package_dir))


def _loaded_code(module):
    rows = {}
    for name, value in vars(module).items():
        if isinstance(value, types.FunctionType) and value.__module__ == module.__name__:
            rows[name] = hashlib.sha256(marshal.dumps(value.__code__)).hexdigest()
        elif isinstance(value, type) and value.__module__ == module.__name__:
            for member, method in vars(value).items():
                method = method.__func__ if isinstance(method, (staticmethod, classmethod)) else method
                if isinstance(method, types.FunctionType):
                    rows[name + "." + member] = hashlib.sha256(marshal.dumps(method.__code__)).hexdigest()
    if not rows:
        raise HarnessError("HARNESS_PRODUCER_CODE_UNAVAILABLE")
    return rows


def build_identity(producer_module):
    module = importlib.import_module(producer_module)
    manifest = _package_manifest()
    observed = {"package_digest": digest(manifest), "producer_code_digest": digest(_loaded_code(module)),
                "package_files_examined": len(manifest), "source": "on_disk_package_and_loaded_producer"}
    return {"harness_version": __version__, "harness_build_digest": observed["package_digest"],
            "build_provenance": observed}


def envelope(*, record_id, project_id, producer_id, producer_kind, producer_module, generation,
             input_refs, run_id=None, task_id=None, plan_digest=None, node_id=None,
             attempt_id=None, invocation_id=None):
    """Only supported runtime proof may fill nullable client/capability identity."""
    return {"identity_contract": "native-receipt-identity-v1", "record_id": record_id, "project_id": project_id,
        "run_id": run_id, "task_id": task_id, "plan_digest": plan_digest, "node_id": node_id,
        "attempt_id": attempt_id, "invocation_id": invocation_id, "producer_id": producer_id, "producer_kind": producer_kind,
        "generation": generation, "created_at": now(), **build_identity(producer_module),
        "client_version": None, "capability_receipt_ref": None, "input_refs": input_refs}


def input_ref(kind, identifier, value, scope):
    return {"kind": kind, "id": identifier, "content_digest": digest(value), "scope": scope}


def seal(record):
    """Bind identity metadata and payload together without self-reference."""
    record["record_digest"] = digest({key: value for key, value in record.items() if key != "record_digest"})
    return record


def verify_record(record):
    expected = record.get("record_digest")
    if not expected or digest({key: value for key, value in record.items() if key != "record_digest"}) != expected:
        raise HarnessError("NATIVE_RECEIPT_INTEGRITY_MISMATCH")
