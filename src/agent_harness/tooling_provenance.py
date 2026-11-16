"""Source-bound immutable tool artifacts and independently checked migrations.

Graph output remains advisory. A successful builder/MCP call is never acceptance.
The existing harness snapshot and atomic publication primitives are reused.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .core import HarnessError, changed_paths, digest, file_hash, lock, now, snapshot, verify_snapshot
from .execution_ledger import repository_identity
from .file_io import read_regular_file
from .runner import _contained_capture
from .strict_yaml import dumps
from .supervisor_store import _mkdir, _ordinary, _parents, atomic_write
from .tool_profiles import inspect_cli

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024


def source_snapshot(root):
    """Freeze actual source bytes including dirty and nonignored untracked files."""
    root = Path(root)
    if not root.is_absolute() or not root.is_dir():
        raise HarnessError("Source root must be an existing absolute directory")
    return snapshot({"cwd": str(root.resolve()), "scope": {"exclude": []}, "inputs": []})


def _source_files(manifest):
    verify_snapshot(manifest)
    if len(manifest["files"]) > 10000:
        raise HarnessError("Source inventory exceeds graph build limit")
    files = {}
    total = 0
    for name, entry in manifest["files"].items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise HarnessError("Invalid frozen source path")
        if entry["kind"] == "missing":
            continue
        if entry["kind"] != "file":
            raise HarnessError("Graph indexing rejects linked or special source inputs")
        total += entry["size"]
        if total > MAX_SOURCE_BYTES:
            raise HarnessError("Graph source exceeds byte limit")
        files[name] = entry
    return files


def _tree_bytes(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            _ordinary(Path(directory) / name, directory=True)
        for name in files:
            path = Path(directory) / name
            _ordinary(path)
            result[path.relative_to(root).as_posix()] = file_hash(path)
    return result


def build_graph_index(root, frozen, artifact_dir, command, *, tool_version, settings=None,
                      timeout=120, version_argv=None):
    """Run an operator-qualified builder on a frozen copy, publish by content.

    command must contain {source_root} and {index_output}; no shell is used.
    This accepts actual Graphify CLI arguments supplied after inspecting its
    installed help. It does not invent a Graphify command/version contract.
    """
    root = Path(root).resolve()
    verify_snapshot(frozen)
    if frozen.get("cwd") != str(root) or source_snapshot(root)["digest"] != frozen["digest"]:
        raise HarnessError("STALE_GRAPH_SOURCE: initial frozen snapshot differs from source")
    files = _source_files(frozen)
    artifact_dir = Path(artifact_dir)
    if not artifact_dir.is_absolute() or artifact_dir == root or root in artifact_dir.parents:
        raise HarnessError("Graph artifacts must live outside the indexed source")
    _mkdir(artifact_dir)
    _parents(artifact_dir)
    _ordinary(artifact_dir, directory=True)
    if (not isinstance(command, (list, tuple)) or not command or
            any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command) or
            not any("{source_root}" in arg for arg in command) or not any("{index_output}" in arg for arg in command)):
        raise HarnessError("Graph builder needs explicit source/output placeholders and argv")
    executable = shutil.which(command[0])
    if executable is None:
        raise HarnessError("GRAPHIFY_NOT_AVAILABLE: executable missing")
    probe = _contained_capture(version_argv or [executable, "--version"], timeout=min(timeout, 15))
    observed_version = probe.stdout.decode("utf-8", errors="replace").strip()
    if probe.returncode or observed_version != tool_version or not tool_version or len(tool_version) > 4096:
        raise HarnessError("Graph builder version differs from qualified tool identity")
    binary_digest = file_hash(Path(executable).resolve())
    builder_inputs = {str(Path(arg).resolve()): file_hash(Path(arg).resolve())
        for arg in [*command, *(version_argv or [])] if Path(arg).is_absolute() and Path(arg).is_file()}
    with tempfile.TemporaryDirectory(prefix="graph-build-", dir=artifact_dir) as staging:
        staging = Path(staging)
        expected_files = {name: entry["sha256"] for name, entry in files.items()}
        # Keep the frozen copy at a stable content-addressed path: an index may
        # contain absolute source locations, which must not point at deleted tmp.
        frozen_root = artifact_dir / ("source-" + digest(expected_files))
        with lock(artifact_dir / ("source-" + digest(expected_files) + ".lock"), purpose="graph snapshot"):
            if not frozen_root.exists():
                prepared = staging / "source"
                prepared.mkdir()
                for name, entry in files.items():
                    raw = read_regular_file(root / name, max_bytes=MAX_SOURCE_BYTES)
                    if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                        raise HarnessError("Source changed while materializing graph snapshot")
                    dest = prepared / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(raw)
                if _tree_bytes(prepared) != expected_files:
                    raise HarnessError("Frozen graph source copy is incomplete")
                os.rename(prepared, frozen_root)
            _parents(frozen_root)
            _ordinary(frozen_root, directory=True)
            if _tree_bytes(frozen_root) != expected_files:
                raise HarnessError("Immutable frozen graph source changed")
        output = staging / "index.bin"
        argv = [str(executable), *[arg.replace("{source_root}", str(frozen_root)).replace("{index_output}", str(output)) for arg in command[1:]]]
        execution = _contained_capture(argv, cwd=str(frozen_root), timeout=timeout)
        if execution.returncode:
            raise HarnessError("Graph builder failed; no index published")
        if _tree_bytes(frozen_root) != expected_files:
            raise HarnessError("Graph builder modified frozen source")
        raw_index = read_regular_file(output, max_bytes=MAX_INDEX_BYTES)
        if not raw_index:
            raise HarnessError("Graph builder produced no index bytes")
        if source_snapshot(root)["digest"] != frozen["digest"]:
            raise HarnessError("Source changed during graph build; index not current")
        index_digest = hashlib.sha256(raw_index).hexdigest()
        index_path = artifact_dir / (index_digest + ".graph-index")
        if index_path.exists():
            if read_regular_file(index_path, max_bytes=MAX_INDEX_BYTES) != raw_index:
                raise HarnessError("Immutable graph artifact collision")
        else:
            atomic_write(index_path, raw_index, exclusive=True)
        receipt = {"schema_version": 1, "kind": "graph_index", "provider": "graphify",
            "repository_identity": repository_identity(root),
            "base_commit": frozen.get("git", {}).get("head"), "source_root": str(root),
            "source_snapshot_digest": frozen["digest"],
            "source_content_digest": frozen.get("content_digest", frozen["digest"]),
            "source_files_digest": digest(expected_files), "frozen_source_path": str(frozen_root),
            "builder_inputs": builder_inputs, "tool_version": tool_version,
            "executable": str(Path(executable).resolve()), "binary_sha256": binary_digest,
            "settings_digest": digest(settings or {}), "builder_argv_digest": digest(list(command)),
            "index_digest": index_digest, "index_path": str(index_path), "index_bytes": len(raw_index),
            "built_at": now(), "protection": "content_addressed_verified_on_use",
            "acceptance": "not_performed", "independent_source_checks": "required"}
        receipt["identity"] = digest(receipt)
        receipt_path = artifact_dir / (receipt["identity"] + ".graph-receipt.yaml")
        if not receipt_path.exists():
            atomic_write(receipt_path, dumps(receipt).encode(), exclusive=True)
        return receipt


def verify_graph_index(receipt, expected_snapshot, *, required=True):
    """Fail closed for current evidence; explicit historical use stays labelled."""
    verify_snapshot(expected_snapshot)
    expected_identity = digest({key: value for key, value in receipt.items() if key != "identity"})
    reasons = []
    if expected_identity != receipt.get("identity"):
        reasons.append("receipt_integrity")
    if (receipt.get("source_root") != expected_snapshot.get("cwd") or
            receipt.get("source_snapshot_digest") != expected_snapshot["digest"]):
        reasons.append("source_snapshot_drift")
    try:
        root = Path(receipt["source_root"])
        if source_snapshot(root)["digest"] != expected_snapshot["digest"]:
            reasons.append("current_source_drift")
        if digest(_tree_bytes(Path(receipt["frozen_source_path"]))) != receipt["source_files_digest"]:
            reasons.append("frozen_source_drift")
        if any(file_hash(path) != expected for path, expected in receipt["builder_inputs"].items()):
            reasons.append("builder_input_drift")
        if repository_identity(root) != receipt["repository_identity"]:
            reasons.append("repository_identity_drift")
        if file_hash(receipt["index_path"]) != receipt["index_digest"]:
            reasons.append("index_bytes_drift")
        if file_hash(receipt["executable"]) != receipt["binary_sha256"]:
            reasons.append("builder_binary_drift")
    except (OSError, KeyError, HarnessError):
        reasons.append("artifact_unavailable")
    if reasons and required:
        raise HarnessError("STALE_GRAPH_INDEX: " + ",".join(reasons))
    return {"status": "CURRENT" if not reasons else "HISTORICAL_ONLY", "reasons": reasons,
        "identity": receipt.get("identity"), "index_path": receipt.get("index_path"), "acceptance": "not_performed"}


def ast_grep_migrate(root, paths, *, pattern, rewrite, language, checks, executable=None, timeout=60):
    """Bounded structural migration with before/after scans and real test checks."""
    root = Path(root).resolve()
    if not paths or not checks or len(paths) > 128:
        raise HarnessError("Migration requires bounded explicit files and independent tests")
    targets = []
    for path in paths:
        path = Path(path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise HarnessError("Migration target must be a contained relative file")
        _parents(root / path)
        _ordinary(root / path)
        targets.append(path.as_posix())
    identity = inspect_cli({"id": "ast-grep", "commands": [executable] if executable else ["ast-grep", "sg"], "version_args": ["--version"]})
    if identity["status"] != "READY":
        raise HarnessError("AST_GREP_NOT_AVAILABLE: installed command is missing or not ast-grep")
    command = identity["executable"]
    help_result = _contained_capture([command, "run", "--help"], timeout=min(timeout, 15))
    help_text = help_result.stdout.decode("utf-8", errors="replace")
    if help_result.returncode or any(flag not in help_text for flag in ("--pattern", "--rewrite", "--update-all", "--json")):
        raise HarnessError("Unsupported ast-grep structural rewrite CLI")

    def scan(value):
        result = _contained_capture([command, "run", "--pattern", value, "--lang", language, "--json=compact", *targets], cwd=str(root), timeout=timeout)
        if result.returncode not in (0, 1) or len(result.stdout) > MAX_SOURCE_BYTES:
            raise HarnessError("ast-grep scan failed")
        matches = json.loads(result.stdout)
        if not isinstance(matches, list):
            raise HarnessError("ast-grep scan did not return match array")
        return {"count": len(matches), "digest": digest(matches), "exit_code": result.returncode}

    before = source_snapshot(root)
    original = scan(pattern)
    if not original["count"]:
        raise HarnessError("No original structural matches; migration evidence is vacuous")
    changed = _contained_capture([command, "run", "--pattern", pattern, "--rewrite", rewrite,
        "--lang", language, "--update-all", *targets], cwd=str(root), timeout=timeout)
    after = source_snapshot(root)
    actual_changes = changed_paths(before, after)
    if not set(actual_changes) <= set(targets):
        raise HarnessError("Structural rewrite escaped declared scope")
    old, new = scan(pattern), scan(rewrite)
    if changed.returncode or old["count"] or new["count"] < original["count"] or not actual_changes:
        raise HarnessError("Structural postconditions failed; rewrite exit is insufficient")
    tests = []
    for argv in checks:
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or "\x00" in v for v in argv):
            raise HarnessError("Independent test must be an argv list")
        result = _contained_capture(argv, cwd=str(root), timeout=timeout)
        tests.append({"argv_digest": digest(argv), "exit_code": result.returncode,
            "stdout_digest": hashlib.sha256(result.stdout).hexdigest(), "stderr_digest": hashlib.sha256(result.stderr).hexdigest()})
        if result.returncode:
            raise HarnessError("Independent migration regression test failed")
    final = source_snapshot(root)
    if not set(changed_paths(before, final)) <= set(targets):
        raise HarnessError("Migration/test changed undeclared source paths")
    return {"schema_version": 1, "status": "verified_migration", "tool": identity,
        "source_before": before["digest"], "source_after": final["digest"], "changed_paths": actual_changes,
        "initial_scan": original, "old_pattern_scan": old, "new_pattern_scan": new, "tests": tests,
        "acceptance": "independent_final_gate_required"}
