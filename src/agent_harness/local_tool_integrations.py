"""Optional native tool launchers: private state, pinned interfaces, no model API.

These are launch/configuration helpers, not an MCP proxy. The MCP client still
connects directly to the original server. No user config or auth is copied.
"""
from __future__ import annotations

# An absolute script path is intentionally usable from a frozen source copy;
# graph provenance can then pin these implementation bytes as a builder input.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "agent_harness"

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid

from .core import HarnessError
from .file_io import canonical_directory, read_regular_file
from .runner import _contained_capture
from .supervisor_store import _mkdir, atomic_write

PINNED = {"serena-agent": "2.1.0", "graphifyy": "0.9.111",
          "@upstash/context7-mcp": "4.6.0", "@ast-grep/cli": "0.51.3"}
SERENA_READ_TOOLS = ("find_symbol", "find_referencing_symbols", "get_symbols_overview",
                     "search_for_pattern", "read_file")
CONTEXT7_READ_TOOLS = ("resolve-library-id", "query-docs")


def installed_tool_digest(executable):
    """Fingerprint the launcher and installed code, excluding mutable bytecode.

    Isolated venv packages hold the implementation and native parser libraries;
    hashing only a console-script launcher would miss their in-place changes.
    File contents are never persisted, only a bounded aggregate identity.
    """
    binary = Path(shutil.which(executable) or executable)
    binary = binary.resolve(strict=True)
    prefix = binary.parent.parent if binary.parent.name in {"bin", "Scripts"} else binary.parent
    roots = [*prefix.glob("lib/python*/site-packages"), prefix / "Lib/site-packages"]
    paths = [binary]
    first = read_regular_file(binary, max_bytes=128 * 1024 * 1024).split(b"\n", 1)[0]
    if first.startswith(b"#!/"):
        interpreter = Path(os.fsdecode(first[2:].split()[0]))
        if interpreter.is_file():
            paths.append(interpreter.resolve(strict=True))
    suffixes = {".py", ".pyi", ".so", ".pyd", ".dll", ".dylib", ".js", ".mjs", ".cjs", ".json", ".scm", ".pth", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".wasm"}
    for root in roots:
        if root.is_dir():
            for path in root.rglob("*"):
                if path.suffix in suffixes and (path.is_file() or path.is_symlink()):
                    paths.append(path)
                    if len(paths) > 50000:
                        raise HarnessError("Installed tool implementation exceeds file-count bound")
    if len(paths) > 50000:
        raise HarnessError("Installed tool implementation exceeds file-count bound")
    checksum = hashlib.sha256();total = 0
    for path in sorted(set(paths), key=str):
        raw = read_regular_file(path, max_bytes=128 * 1024 * 1024)
        total += len(raw)
        if total > 1024 * 1024 * 1024:
            raise HarnessError("Installed tool implementation exceeds byte bound")
        checksum.update(str(path).encode() + b"\0" + hashlib.sha256(raw).digest())
    return checksum.hexdigest()


def _outside(root, cache):
    root = canonical_directory(root)
    cache = Path(cache)
    if not cache.is_absolute() or ".." in cache.parts:
        raise HarnessError("Tool cache must be an absolute ordinary private path")
    cache = canonical_directory(cache)
    if cache == root or root in cache.parents:
        raise HarnessError("Tool mutable state must be outside the source worktree")
    _mkdir(cache)
    return root, cache


def _private_env(directory):
    # Local-only builders do not inherit provider keys, user config or plugins.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "LANG", "LC_ALL"}}
    for key, name in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data"), ("TMPDIR", "tmp")):
        target = directory / name
        _mkdir(target)
        env[key] = str(target)
    return env


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
        raise HarnessError("Optional tool timeout must be finite and within300seconds")
    return value


def _published_policy(path, policy, root):
    # Serena legitimately persists discovered projects/defaults in this owned
    # file. Preserve those values while refusing drift of authoritative grants.
    if path.exists() or path.is_symlink():
        from .strict_yaml import load
        existing = load(path)
        if (not isinstance(existing, dict)
                or existing.get("projects") not in ([], [str(root)])
                or any(existing.get(k) != v for k, v in policy.items() if k != "projects")):
            raise HarnessError("Owned tool policy changed; use a fresh per-worktree cache")
    else:
        atomic_write(path, (json.dumps(policy, sort_keys=True) + "\n").encode(), exclusive=True)


def _validate_serena_project(project):
    # Native project.local.yml overrides project.yml. Validate both before
    # invoking native code; a fixed MCP tool list does not suppress startup hooks.
    from .strict_yaml import load
    empty_keys = ("activation_command", "ls_specific_settings", "included_optional_tools",
        "excluded_tools", "included_apis", "excluded_apis", "added_modes", "default_modes",
        "ls_additional_workspace_folders", "fixed_tools")
    for name in ("project.yml", "project.local.yml"):
        path = project / name
        if not path.exists() and not path.is_symlink():
            continue
        value = load(path)
        if name == "project.local.yml" and value is None:
            continue  # Serena creates a comments-only local overrides template.
        if not isinstance(value, dict):
            raise HarnessError("Invalid private Serena project policy")
        for key in empty_keys:
            item = value.get(key)
            if item not in (None, "", [], {}):
                raise HarnessError("SERENA_PROJECT_POLICY_FORBIDDEN: " + key)
        for key, expected in (("agent_interface", "tools"), ("language_backend", "LSP")):
            if value.get(key) not in (None, "", expected):
                raise HarnessError("SERENA_PROJECT_POLICY_FORBIDDEN: " + key)
        if value.get("ls_workspace_folders") not in (None, "", ["."]):
            raise HarnessError("SERENA_PROJECT_POLICY_FORBIDDEN: workspace folders")


def prepare_serena(root, cache, *, executable="serena", timeout=15, expected_implementation=None):
    """Return original Serena argv/env with classic tools and private indexes.

    Version2 defaults to a Python REPL; managed profiles must explicitly request
    the separately allowlisted tool interface. Dashboard/GUI never start here.
    """
    timeout = _timeout(timeout)
    root, cache = _outside(root, cache)
    binary = shutil.which(executable)
    if binary is None:
        raise HarnessError("SERENA_NOT_AVAILABLE")
    implementation = installed_tool_digest(binary)
    if expected_implementation is not None and implementation != expected_implementation:
        raise HarnessError("SERENA_IMPLEMENTATION_DRIFT: installed tool code changed")
    env = _private_env(cache)
    env["SERENA_HOME"] = str(cache / "serena-home")
    env["SERENA_USAGE_REPORTING"] = "false"
    root_info = root.stat()
    binding = {"schema_version": 1, "root": str(root), "device": root_info.st_dev, "inode": root_info.st_ino}
    binding_path = cache / "harness-project.json"
    if binding_path.exists() or binding_path.is_symlink():
        if json.loads(read_regular_file(binding_path)) != binding:
            raise HarnessError("SERENA_WORKTREE_DRIFT: private cache belongs to another worktree")
    else:
        atomic_write(binding_path, (json.dumps(binding, sort_keys=True) + "\n").encode(), exclusive=True)
    _mkdir(Path(env["SERENA_HOME"]))
    project = cache / "project-data"
    _mkdir(project)  # Existing private location wins over project-local fallback.
    _validate_serena_project(project)
    policy = {"agent_interface": "tools", "fixed_tools": list(SERENA_READ_TOOLS),
              "project_serena_folder_location": str(project), "projects": [],
              "trusted_project_path_patterns": [], "language_backend": "LSP", "ls_specific_settings": {}}
    # Use the installed pinned Python language server, rather than a per-worker
    # uvx download. Other languages require their own separately qualified LSP.
    python_lsp = Path(binary).parent / "pyright-langserver"
    if python_lsp.is_file():
        policy["ls_specific_settings"] = {"python": {"ls_base_cmd": [str(python_lsp)]}}
    raw = (json.dumps(policy, sort_keys=True) + "\n").encode()
    _published_policy(Path(env["SERENA_HOME"]) / "serena_config.yml", policy, root)
    version = _contained_capture([binary, "--version"], env=env, timeout=timeout)
    text = version.stdout.decode("utf-8", errors="replace").strip()
    if version.returncode or re.search(r"\b2\.1\.0\b", text) is None:
        raise HarnessError("UNSUPPORTED_SERENA_VERSION: requires pinned2.1.0")
    help_result = _contained_capture([binary, "start-mcp-server", "--help"], env=env, timeout=timeout)
    help_text = help_result.stdout.decode("utf-8", errors="replace")
    options = ("--project", "--agent-interface", "--transport", "--enable-web-dashboard",
               "--enable-gui-log-window", "--open-web-dashboard")
    if help_result.returncode or any(option not in help_text for option in options):
        raise HarnessError("UNSUPPORTED_SERENA_CLI")
    argv = [binary, "start-mcp-server", "--project", str(root), "--agent-interface", "tools",
            "--transport", "stdio", "--enable-web-dashboard", "false",
            "--enable-gui-log-window", "false", "--open-web-dashboard", "false"]
    return {"argv": argv, "environment": env, "cwd": str(root), "version": text,
            "policy_digest": hashlib.sha256(raw).hexdigest(), "project_state": str(project),
            "implementation_digest": implementation}


def serena_stdio_argv(root, cache, *, executable="serena"):
    binary = shutil.which(executable)
    command = [sys.executable, str(Path(__file__).resolve()), "serena-stdio", "--executable", str(Path(binary).resolve()) if binary else executable,
               "--project", str(root), "--cache-root", str(cache)]
    if binary is not None:
        command.extend(["--implementation-digest", installed_tool_digest(binary)])
    return command


def context7_server(executable="context7-mcp", *, timeout=30):
    """Only public documentation reads; no setup command, credentials or plugins."""
    return {"id": "context7", "transport": "stdio", "required": False,
        "command": [executable, "--transport", "stdio"], "allowed_tools": list(CONTEXT7_READ_TOOLS),
        "read_only_tools": list(CONTEXT7_READ_TOOLS), "forbidden_tools": [],
        "required_tools": list(CONTEXT7_READ_TOOLS), "startup_timeout": timeout, "tool_timeout": timeout}


def context7_smoke(executable, root, *, timeout=30):
    """Public documentation reads without auth mutation; never model inference."""
    from .tooling_probe import StdioProbe, ProbeFailure
    from .core import digest
    spec = context7_server(executable, timeout=_timeout(timeout))
    spec["cwd"] = str(canonical_directory(root))
    receipt = {"status": "BLOCKED", "mode": "public-anonymous", "model_calls": False,
               "configured_credentials": False, "client_catalog_verified": False}
    temporary = tempfile.TemporaryDirectory(prefix="context7-public-")
    client = StdioProbe(spec, role="reviewer", environment=_private_env(Path(temporary.name)))
    try:
        with client:
            catalog = client.inventory()
            receipt.update(catalog_digest=digest(catalog), tools=[t["name"] for t in catalog], server_info=client.server_info)
            for name, arguments in (("resolve-library-id", {"libraryName": "pytest", "query": "pytest equality assertion"}),
                                    ("query-docs", {"libraryId": "/pytest-dev/pytest", "query": "How to assert that two integers are equal in a pytest test?"})):
                result = client.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
                if not isinstance(result, dict):
                    raise ProbeFailure("INVALID_MESSAGE")
                if result.get("isError"):
                    content = json.dumps(result).casefold()
                    reason = ("AUTH_REQUIRED" if any(v in content for v in ("unauthorized", "api key", "401")) else
                              "RATE_LIMITED" if any(v in content for v in ("rate limit", "429")) else
                              "NETWORK_UNAVAILABLE" if any(v in content for v in ("fetch failed", "network", "enotfound", "eai_again")) else "PUBLIC_READ_FAILED")
                    receipt["reason"] = reason
                    break
                receipt.setdefault("reads", []).append({"tool": name, "result_digest": digest(result)})
            else:
                receipt["status"] = "OBSERVED_READ"
    except (OSError, ValueError, ProbeFailure) as error:
        receipt["reason"] = error.code if isinstance(error, ProbeFailure) else type(error).__name__
    finally:
        temporary.cleanup()
    receipt["process_tree_stopped"] = client.process is None or client.stopped
    if not receipt["process_tree_stopped"]:
        receipt.update(status="BLOCKED", reason="UNKNOWN_PROCESS_OUTCOME")
    return receipt


def _graphify_version(executable, *, timeout=15):
    timeout = _timeout(timeout)
    binary = shutil.which(executable)
    if binary is None:
        raise HarnessError("GRAPHIFY_NOT_AVAILABLE")
    with tempfile.TemporaryDirectory(prefix="graphify-version-") as tmp:
        result = _contained_capture([binary, "--version"], env=_private_env(Path(tmp)), timeout=timeout)
    version = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode or re.search(r"\b0\.9\.111\b", version) is None:
        raise HarnessError("UNSUPPORTED_GRAPHIFY_VERSION: requires pinned0.9.111")
    return str(Path(binary).resolve()), version + " (implementation_sha256=" + installed_tool_digest(binary) + ")"


def graphify_build(source, output, *, executable="graphify", timeout=120, failure_receipt=None):
    """Materialize original Graphify AST output as one provenance-bound artifact."""
    timeout = _timeout(timeout)
    source, parent = _outside(source, Path(output).parent)
    output = parent / Path(output).name
    binary, version = _graphify_version(executable)
    with tempfile.TemporaryDirectory(prefix="graphify-local-", dir=parent) as tmp:
        stage = Path(tmp)
        env = _private_env(stage)
        target = stage / "output"
        result = _contained_capture([binary, "extract", str(source), "--code-only", "--no-cluster",
            "--max-workers", "1", "--out", str(target)], cwd=str(source), env=env, timeout=timeout)
        if result.returncode:
            if failure_receipt is not None:
                from .core import digest
                destination = Path(failure_receipt)
                _outside(source, destination.parent)
                failure = {"schema_version": 1, "operation": "graphify-code-only", "exit_code": result.returncode,
                    "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                    "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(), "tool_identity": version,
                    "source_root_digest": digest(str(source)), "model_calls": False}
                atomic_write(destination, (json.dumps(failure, sort_keys=True) + "\n").encode(), exclusive=True)
            raise HarnessError("GRAPHIFY_AST_BUILD_FAILED")
        candidates = [target / "graphify-out" / name for name in ("graph.json", "extraction.json")]
        found = next((path for path in candidates if path.is_file()), None)
        if found is None:
            raise HarnessError("Graphify did not produce its qualified local extraction artifact")
        raw = read_regular_file(found, max_bytes=16 * 1024 * 1024)
        graph = json.loads(raw)
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges", graph.get("links")), list):
            raise HarnessError("Invalid Graphify extraction structure")
        # Preserve actual upstream graph; no semantic/LLM edges are synthesized.
        artifact = {"schema_version": 1, "provider": "Graphify-Labs/graphify", "distribution": "graphifyy",
            "version": version, "mode": "local-code-only", "graph": graph}
        atomic_write(output, (json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n").encode(), exclusive=True)
    return artifact


def build_local_graph_index(root, artifact_dir, *, executable="graphify", frozen=None, timeout=120):
    from .tooling_provenance import build_graph_index, source_snapshot
    from .core import digest
    from .strict_yaml import validate
    binary, version = _graphify_version(executable)
    implementation_digest = installed_tool_digest(binary)
    implementation = str(Path(__file__).resolve())
    root, artifact_dir = _outside(root, artifact_dir)
    failure_path = artifact_dir / "failures" / ("graphify-" + uuid.uuid4().hex + ".json")
    command = [sys.executable, implementation, "graphify-build", "--executable", binary,
               "--source", "{source_root}", "--output", "{index_output}", "--timeout", str(timeout),
               "--failure-receipt", str(failure_path)]
    receipt = build_graph_index(root, frozen or source_snapshot(root), artifact_dir, command,
        tool_version=version, settings={"mode": "local-code-only", "semantic_inference": False},
        timeout=timeout, version_argv=[sys.executable, implementation, "graphify-version", "--executable", binary])
    if _graphify_version(binary)[1] != version or installed_tool_digest(binary) != implementation_digest:
        raise HarnessError("GRAPHIFY_IMPLEMENTATION_DRIFT: installed tool changed during build")
    receipt["provider_toolchain"] = {"kind": "isolated-python-code-v1", "executable": binary,
        "implementation_digest": implementation_digest}
    receipt["identity"] = digest({key: value for key, value in receipt.items() if key != "identity"})
    validate(receipt, "graph-index.schema.json")
    from .strict_yaml import dumps
    atomic_write(artifact_dir / (receipt["identity"] + ".graph-receipt.yaml"),
                 dumps(receipt).encode(), exclusive=True)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    serena = commands.add_parser("serena-stdio")
    serena.add_argument("--executable", default="serena")
    serena.add_argument("--project", required=True)
    serena.add_argument("--cache-root", required=True)
    serena.add_argument("--implementation-digest")
    for name in ("graphify-build", "graphify-version"):
        command = commands.add_parser(name)
        command.add_argument("--executable", default="graphify")
        if name == "graphify-build":
            command.add_argument("--source", required=True)
            command.add_argument("--output", required=True)
            command.add_argument("--timeout", type=float, default=120)
            command.add_argument("--failure-receipt")
    args = parser.parse_args(argv)
    if args.operation == "serena-stdio":
        invocation = prepare_serena(args.project, args.cache_root, executable=args.executable,
                                    expected_implementation=args.implementation_digest)
        os.chdir(invocation["cwd"])
        os.execve(invocation["argv"][0], invocation["argv"], invocation["environment"])
    elif args.operation == "graphify-build":
        graphify_build(args.source, args.output, executable=args.executable, timeout=args.timeout,
                       failure_receipt=args.failure_receipt)
    else:
        print(_graphify_version(args.executable)[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
