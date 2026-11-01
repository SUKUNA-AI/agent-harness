"""Conservative check reuse. Fresh acceptance remains the runner's authority."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import shutil

from .core import HarnessError, digest, now, snapshot
from .execution_ledger import HASH, SHORT, object_schema, safe_private, sanitized
from .file_io import read_regular_file

PROOF_SCHEMA = object_schema({"schema_version": {"const": 1}, "snapshot_id": HASH,
    "repository_identity": HASH, "toolchain_digest": HASH, "binary_digest": HASH,
    "argv": {"type": "array", "items": SHORT}, "cwd": SHORT, "env_digest": HASH,
    "config_digest": HASH, "platform": SHORT, "input_closure_complete": {"const": True},
    "isolated_environment": {"const": True}, "no_external_dependencies": {"const": True}},
    ("schema_version", "snapshot_id", "repository_identity", "toolchain_digest", "binary_digest",
     "argv", "cwd", "env_digest", "config_digest", "platform", "input_closure_complete",
     "isolated_environment", "no_external_dependencies"))
CACHE_SCHEMA = object_schema({"schema_version": {"const": 1}, "key": HASH,
    "first_run_id": SHORT, "original_timestamp": SHORT, "proof": PROOF_SCHEMA,
    "evidence_id": SHORT, "evidence_path": SHORT, "evidence_digest": HASH,
    "status": {"const": "passed"}}, ("schema_version", "key", "first_run_id",
    "original_timestamp", "proof", "evidence_id", "evidence_path", "evidence_digest", "status"))


def make_proof(root, check, *, toolchain_digest, environment, config, isolated_environment=False,
               no_external_dependencies=False):
    """Compute actual full-tree and binary identities; no caller-supplied hash shortcut."""
    from .execution_ledger import repository_identity
    if not isolated_environment or not no_external_dependencies:
        raise HarnessError("Cache proof requires isolated environment and explicit complete dependency closure")
    if not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()):
        raise HarnessError("Cache environment must be explicit string mapping")
    sanitized(environment)
    argv = check.get("argv", [])
    executable = shutil.which(argv[0], path=environment.get("PATH", os.defpath)) if argv else None
    if not executable:
        raise HarnessError("Cache executable identity unavailable")
    from .runner import _executable_hash
    binary_digest = _executable_hash(executable, {})
    if not binary_digest:
        raise HarnessError("Cache executable fingerprint unavailable")
    tree = snapshot({"cwd": str(Path(root).resolve()), "scope": {"exclude": []}})
    return {"schema_version": 1, "snapshot_id": tree["digest"],
        "repository_identity": repository_identity(root), "toolchain_digest": toolchain_digest,
        "binary_digest": binary_digest, "argv": argv, "cwd": str(Path(root).resolve()),
        "env_digest": digest(environment), "config_digest": digest(config),
        "platform": platform.platform() + "/" + platform.machine(), "input_closure_complete": True,
        "isolated_environment": True, "no_external_dependencies": True}


class CheckCache:
    def __init__(self, directory, *, allowlist=()):
        self.root = safe_private(directory, create=True)
        self.allowlist = frozenset(allowlist)

    def eligible(self, check, proof):
        from .strict_yaml import validate
        if (check.get("id") not in self.allowlist
                or check.get("cache_policy", "never") != "verified_inputs_only"
                or check.get("deterministic") is not True
                or check.get("effect") != "pure"
                or check.get("flaky", False) or check.get("integration", False)
                or check.get("argv") != proof.get("argv")):
            return False
        try:
            validate(proof, PROOF_SCHEMA)
        except (ValueError, HarnessError):
            return False
        return True

    def put(self, check, proof, evidence, *, run_id):
        from .strict_yaml import dump
        if not self.eligible(check, proof) or evidence.get("status") != "passed":
            return {"stored": False, "reason": "not proven pure/reproducible passed check"}
        source = safe_private(evidence["path"])
        data = read_regular_file(source, max_bytes=16 * 1024 * 1024)
        key = digest({"check": check, "proof": proof})
        row = sanitized({"schema_version": 1, "key": key, "first_run_id": run_id,
            "original_timestamp": now(), "proof": proof, "evidence_id": evidence["id"],
            "evidence_path": str(source), "evidence_digest": hashlib.sha256(data).hexdigest(), "status": "passed"})
        path = self.root / (key + ".yaml")
        if path.exists():
            return {"stored": False, "reason": "immutable existing cache record", "key": key}
        dump(path, row, CACHE_SCHEMA, exclusive=True)
        return {"stored": True, "key": key}

    def get(self, check, proof, *, fresh=False):
        from .strict_yaml import load
        if fresh or not self.eligible(check, proof):
            return {"hit": False, "reason": "fresh execution required" if fresh else "cache policy defaults to never"}
        key = digest({"check": check, "proof": proof})
        path = safe_private(self.root / (key + ".yaml"))
        if not path.exists():
            return {"hit": False, "reason": "no matching complete input proof"}
        row = load(path, CACHE_SCHEMA)
        if row["key"] != key or row["proof"] != proof:
            raise HarnessError("Cache record integrity mismatch")
        try:
            data = read_regular_file(safe_private(row["evidence_path"]), max_bytes=16 * 1024 * 1024)
        except (OSError, ValueError):
            return {"hit": False, "reason": "original evidence unavailable"}
        if hashlib.sha256(data).hexdigest() != row["evidence_digest"]:
            return {"hit": False, "reason": "original evidence changed"}
        return {"hit": True, "reused": True, "read_only": True, "record": row,
                "warning": "informational reuse does not replace a required fresh acceptance run"}
