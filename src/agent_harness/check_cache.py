"""Conservative check reuse. Fresh acceptance remains the runner's authority."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import platform
import shutil
import sys

from .core import HarnessError, digest, now, snapshot
from .execution_ledger import HASH, SHORT, object_schema, safe_private, sanitized
from .file_io import read_regular_file

from .input_closure import CLOSURE_SCHEMA, bounded_closure

PROOF_SCHEMA = object_schema({"schema_version": {"const": 2}, "snapshot_id": HASH,
    "repository_identity": HASH, "toolchain_digest": HASH, "binary_digest": HASH,
    "binary_path": SHORT, "input_closure": CLOSURE_SCHEMA, "live_environment_digest": HASH,
    "check_definition_digest": HASH,
    "probe_policy": {"const": "isolated_literal_python_probe_v1"},
    "argv": {"type": "array", "items": SHORT}, "cwd": SHORT, "env_digest": HASH,
    "config_digest": HASH, "platform": SHORT, "input_closure_complete": {"const": True},
    "isolated_environment": {"const": True}, "no_external_dependencies": {"const": True}},
    ("schema_version", "snapshot_id", "repository_identity", "toolchain_digest", "binary_digest", "binary_path",
     "input_closure", "live_environment_digest", "check_definition_digest", "probe_policy",
     "argv", "cwd", "env_digest", "config_digest", "platform", "input_closure_complete",
     "isolated_environment", "no_external_dependencies"))
CACHE_SCHEMA = object_schema({"schema_version": {"const": 2}, "key": HASH,
    "first_run_id": SHORT, "original_timestamp": SHORT, "proof": PROOF_SCHEMA,
    "evidence_id": SHORT, "evidence_path": SHORT, "evidence_digest": HASH,
    "status": {"const": "passed"}}, ("schema_version", "key", "first_run_id",
    "original_timestamp", "proof", "evidence_id", "evidence_path", "evidence_digest", "status"))


def _literal_probe(root, check):
    """Recognize only constant print and UTF-8 literal relative read assertions.

    This is an AST admission check, not a Python interpreter. Opaque tests and
    commands are unsupported even if a caller labels them pure/closed.
    """
    from .context import _path
    argv = check.get("argv", [])
    if not isinstance(argv, list) or len(argv) < 5 or any(not isinstance(a, str) for a in argv):
        raise HarnessError("Optional cache supports isolated literal Python probes only")
    if Path(argv[0]).resolve() != Path(sys.executable).resolve() or argv.count("-c") != 1:
        raise HarnessError("Opaque command/tool inputs cannot be proven by a purity declaration")
    index = argv.index("-c")
    flags = argv[1:index]
    if index != len(argv) - 2 or not {"-I", "-S"} <= set(flags) or any(flag not in {"-I", "-S", "-B"} for flag in flags):
        raise HarnessError("Cache probe requires isolated startup -I -S, without import hooks")
    code = argv[-1]
    if len(code.encode("utf-8")) > 4096:
        raise HarnessError("Literal cache probe code exceeds admission limit")
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as error:
        raise HarnessError("Invalid literal cache probe") from error
    if not 1 <= len(tree.body) <= 16 or sum(1 for _ in ast.walk(tree)) > 256:
        raise HarnessError("Literal cache probe is outside bounded grammar")

    def literal(node):
        return isinstance(node, ast.Constant) and type(node.value) in {str, int, float, bool, type(None)} and len(repr(node.value)) <= 4096

    def read(node):
        if not (isinstance(node, ast.Call) and not node.args and not node.keywords and isinstance(node.func, ast.Attribute)
                and node.func.attr == "read" and isinstance(node.func.value, ast.Call)):
            return False
        opened = node.func.value
        if not (isinstance(opened.func, ast.Name) and opened.func.id == "open" and len(opened.args) == 1
                and isinstance(opened.args[0], ast.Constant) and isinstance(opened.args[0].value, str)
                and len(opened.keywords) == 1 and opened.keywords[0].arg == "encoding"
                and isinstance(opened.keywords[0].value, ast.Constant) and opened.keywords[0].value.value == "utf-8"):
            return False
        if not _path(Path(root).resolve(strict=True), opened.args[0].value).is_file():
            raise HarnessError("Literal cache probe needs an existing regular input file")
        return True

    for statement in tree.body:
        if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name) and statement.value.func.id == "print"
                and len(statement.value.args) == 1 and not statement.value.keywords
                and (literal(statement.value.args[0]) or read(statement.value.args[0]))):
            continue
        if (isinstance(statement, ast.Assert) and statement.msg is None and isinstance(statement.test, ast.Compare)
                and len(statement.test.ops) == 1 and isinstance(statement.test.ops[0], ast.Eq)
                and len(statement.test.comparators) == 1 and literal(statement.test.comparators[0])
                and (literal(statement.test.left) or read(statement.test.left))):
            continue
        raise HarnessError("Opaque Python code cannot declare its way into the optional cache")
    return "isolated_literal_python_probe_v1"


def make_proof(root, check, *, toolchain_digest, environment, config, isolated_environment=False,
               no_external_dependencies=False):
    """Compute actual full-tree and binary identities; no caller-supplied hash shortcut."""
    from .execution_ledger import repository_identity
    if not isolated_environment or not no_external_dependencies:
        raise HarnessError("Cache proof requires isolated environment and explicit complete dependency closure")
    probe_policy = _literal_probe(root, check)
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
    closure = bounded_closure(root)
    return {"schema_version": 2, "snapshot_id": tree["digest"],
        "repository_identity": repository_identity(root), "toolchain_digest": toolchain_digest,
        "binary_digest": binary_digest, "binary_path": str(Path(executable).absolute()),
        "input_closure": closure, "live_environment_digest": digest(dict(os.environ)), "probe_policy": probe_policy,
        "check_definition_digest": digest(check), "argv": argv, "cwd": str(Path(root).resolve()),
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
                or check.get("argv") != proof.get("argv") or digest(check) != proof.get("check_definition_digest")):
            return False
        try:
            validate(proof, PROOF_SCHEMA)
            _literal_probe(proof["cwd"], check)
        except (ValueError, HarnessError):
            return False
        return True

    @staticmethod
    def _current(proof):
        """Never trust a caller's old proof as evidence of current source bytes."""
        from .execution_ledger import repository_identity
        from .runner import _executable_hash
        try:
            if not Path(proof["binary_path"]).is_absolute() or Path(proof["binary_path"]).resolve() != Path(proof["argv"][0]).resolve():
                return False
            if proof["platform"] != platform.platform() + "/" + platform.machine():
                return False
            if proof["live_environment_digest"] != digest(dict(os.environ)):
                return False
            if proof["repository_identity"] != repository_identity(proof["cwd"]):
                return False
            closure = bounded_closure(proof["cwd"])
            if closure != proof["input_closure"]:
                return False
            if snapshot({"cwd": proof["cwd"], "scope": {"exclude": []}})["digest"] != proof["snapshot_id"]:
                return False
            if _executable_hash(proof["binary_path"], {}) != proof["binary_digest"]:
                return False
            return True
        except (OSError, ValueError, HarnessError, KeyError):
            return False

    def put(self, check, proof, evidence, *, run_id):
        from .strict_yaml import dump
        if not self.eligible(check, proof) or not self._current(proof) or evidence.get("status") != "passed":
            return {"stored": False, "reason": "not proven pure/reproducible passed check"}
        source = safe_private(evidence["path"])
        data = read_regular_file(source, max_bytes=16 * 1024 * 1024)
        key = digest({"check": check, "proof": proof})
        row = sanitized({"schema_version": 2, "key": key, "first_run_id": run_id,
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
        if not self._current(proof):
            return {"hit": False, "reason": "actual source/binary/environment closure changed or cannot be proven"}
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
