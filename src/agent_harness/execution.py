"""Optional backend/profile binding inside the existing supervisor store.

Old contracts are deliberately not normalized to add these fields: their saved
hashes and Codex flow remain unchanged. Tool observations are never acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from .core import HarnessError, digest, now
from .file_io import read_regular_file, canonical_directory
from .strict_yaml import validate, _schema
from .task_packets import NotDispatched


def provenance_ref(intent):
    checksum = intent.get("capability_receipt_ref")
    if not checksum:
        return None
    return {"path": "tooling/" + intent["node_id"].replace("@", "_") + "/" + intent["attempt_id"] + "-" + checksum + ".yaml",
            "digest": checksum}


def validate_selection(value):
    validate(value, _schema("work-contract.tooling.v1.schema.json")["properties"]["execution"])
    ref = value.get("tool_profile", {})
    if ref.get("path") and not Path(ref["path"]).is_absolute():
        raise HarnessError("Tool profile path must be absolute; relative resume identity is ambiguous")
    if value.get("graph_index") and not Path(value["graph_index"]["path"]).is_absolute():
        raise HarnessError("Graph index receipt path must be absolute")
    return value


def selection(contract, node=None, *, review=False):
    # An explicit node selection replaces individual declared values, while a
    # missing node selection inherits the authorized contract's defaults.
    result = deepcopy(contract.get("review_execution" if review else "execution", {}))
    if node is not None:
        result.update(deepcopy(node.get("execution", {})))
    validate_selection(result)
    return result


def _configuration_identity(adapter, root):
    """Hash config bytes only; never persist auth or configuration values."""
    env = getattr(adapter, "environment", None) or os.environ
    home = Path(env.get("CODEX_HOME", Path.home() / ".codex"))
    paths = [home / "config.toml"]
    # Include the whole project chain, including untrusted config, so later
    # trust/config changes cannot silently change a resumed invocation.
    for parent in [root, *root.parents]:
        paths.extend([parent / ".codex" / "config.toml", parent / "opencode.json", parent / "opencode.jsonc",
                      parent / ".opencode" / "opencode.json", parent / ".opencode" / "opencode.jsonc"])
    xdg = Path(env.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opencode"
    paths.extend([xdg / "opencode.json", xdg / "opencode.jsonc"])
    result = {}
    for path in dict.fromkeys(paths):
        if path.exists() or path.is_symlink():
            result[str(path)] = hashlib.sha256(read_regular_file(path, max_bytes=2 * 1024 * 1024)).hexdigest()
    return digest(result)


def _launch_identity(adapter, profile, root, cache):
    from .tool_profiles import resolved_servers
    files = {}
    args = list(getattr(adapter, "executable", ()))
    for server in resolved_servers(profile, root, cache_root=cache):
        args.extend(server.get("command", []))
    for arg in args:
        path = Path(arg)
        if path.is_absolute() and path.is_file():
            from .codex_execution import _binary_hash
            files[str(path.resolve())] = _binary_hash(path.resolve())
    return digest(files)


class ProfileExecution:
    """Policy observer around a native client; never an MCP tool proxy."""
    def __init__(self, adapter, profile, binding):
        self.adapter, self.profile, self.binding = adapter, profile, binding

    def capabilities(self, **kwargs):
        return self.adapter.capabilities(**kwargs)

    def preflight(self, prompt, **options):
        return self.adapter.preflight(prompt, **options)

    def run(self, prompt, **options):
        previous = options.get("on_event")
        failed = []
        mandatory = {s["id"] for s in self.profile["servers"] if s.get("required")}
        required = {(s["id"], tool) for s in self.profile["servers"] if s.get("required") for tool in s["required_tools"]}
        confirmed = set()
        def observe(event):
            item = event.get("item", {})
            if item.get("type") == "mcp_tool_call":
                pairs = [(item.get("server"), item.get("tool"))]
                if not item.get("server"):
                    pairs = [(s, t) for s, t in required if item.get("tool") in {s + "_" + t, "mcp__" + s + "__" + t}]
                state = event.get("backend_event", {}).get("part", {}).get("state", {})
                if len(pairs) == 1 and not item.get("error") and item.get("status") != "failed" and state.get("status") != "error":
                    if item.get("result") is not None or state.get("status") == "completed":
                        confirmed.add(pairs[0])
            if mandatory and item.get("type") == "mcp_tool_call" and (item.get("server") in mandatory or not item.get("server")):
                if (item.get("error") or item.get("status") == "failed"
                        or isinstance(item.get("result"), dict) and item["result"].get("isError")
                        or event.get("backend_event", {}).get("part", {}).get("state", {}).get("status") == "error"):
                    failed.append("MANDATORY_MCP_RUNTIME_FAILURE")
            if previous:
                previous(event)
        options["on_event"] = observe
        result = self.adapter.run(prompt, **options)
        if failed and result.get("status") == "completed":
            result.update(status="failed", error=failed[0], outcome_known=True)
        elif required - confirmed and result.get("status") == "completed":
            result.update(status="failed", error="MANDATORY_MCP_RESULT_UNCONFIRMED", outcome_known=True)
        return result


def prepare(supervisor, selected, *, root, role, node_id, attempt_id, worktree=None, prior=None):
    """Fresh readiness before model admission; immutable source-bound receipt.

    `prior` is required on exact resume and checked before any model slot. The
    existing process/ownership recovery remains authoritative and precedes this.
    """
    from .tool_profiles import load_profile, profile_digest, preflight_profile, render_codex, render_opencode
    validate_selection(selected)
    profile_role = {"owner": "worker", "helper": "worker", "architect": "planner"}.get(role, role)
    backend = selected.get("backend", "codex")
    try:
        adapter = supervisor._adapter(backend)
        capabilities = adapter.capabilities(deadline=supervisor.state["deadline"])
        if capabilities.get("status") == "unsupported":
            raise HarnessError("UNSUPPORTED_CAPABILITY: " + backend + " version " + str(capabilities.get("version", "unknown")))
        if capabilities.get("status") not in {"observed", "qualified"}:
            raise HarnessError("Backend capability preflight unavailable")
        if backend == "opencode" and capabilities.get("managed_leaf_qualified") is not True:
            raise HarnessError("BLOCKED_NOT_QUALIFIED: OpenCode managed leaf isolation is not live qualified")
        for name in selected.get("required_capabilities", []):
            if capabilities.get(name) is not True:
                raise HarnessError("BLOCKED_NOT_QUALIFIED: required backend capability " + name)
        profile = load_profile(selected.get("tool_profile"))
        if supervisor.remaining() <= 0:
            raise HarnessError("Absolute task deadline expired before tool readiness")
        root = canonical_directory(root)
        info = root.stat()
        cache = supervisor.path / "tool-cache" / node_id.replace("@", "_")
        from .strict_yaml import load
        graph = load(selected["graph_index"]["path"]) if selected.get("graph_index") else None
        readiness = preflight_profile(profile, backend, root, profile_role, cache_root=cache, graph_index=graph)
        if readiness["status"] != "READY":
            raise HarnessError("TOOL_PROFILE_NOT_READY: " + json.dumps(readiness, ensure_ascii=False))
        if supervisor.remaining() <= 0:
            raise HarnessError("Absolute task deadline expired during tool readiness")
        if profile["servers"] and capabilities.get("effective_tool_catalog") is not True:
            raise HarnessError("BLOCKED_NOT_QUALIFIED: execution-bound effective MCP catalog is unqualified; diagnostic catalog is insufficient")
        native = (render_codex if backend == "codex" else render_opencode)(profile, root, role=profile_role, cache_root=cache)
        if role == "reviewer" and capabilities.get("reviewer_isolation_qualified") is not True:
            raise HarnessError("BLOCKED_NOT_QUALIFIED: effective reviewer catalog, shell, plugins and MCP isolation need live enforcement")
        configure = getattr(adapter, "with_tool_config", None)
        if native and callable(configure):
            adapter = configure(native, root=root)
        elif profile["id"] != "baseline" and not callable(configure):
            raise HarnessError("BLOCKED_NOT_QUALIFIED: backend lacks tool configuration support")
        identity = {"backend": backend, "executable": list(getattr(adapter, "executable", ())),
            "binary_sha256": capabilities.get("binary_sha256", "unobserved"), "version": capabilities.get("version", "unobserved"),
            "profile_id": profile["id"], "profile_version": profile["version"], "profile_digest": profile_digest(profile),
            "effective_config_digest": digest(native), "configuration_inputs_digest": _configuration_identity(adapter, root),
            "catalog_digest": readiness["catalog_digest"], "required_dependencies": readiness["required_dependencies"],
            "root": str(root), "directory_identity": [info.st_dev, info.st_ino],
            "worktree_digest": digest(worktree) if worktree else None,
            "selection_digest": digest(selected), "graph_index_digest": digest(graph) if graph else None, "cache_root": str(cache),
            "launch_files_digest": _launch_identity(adapter, profile, root, cache),
            "dependency_fingerprint": digest({"servers": [{k: row[k] for k in ("id", "executable", "binary_sha256", "server_info", "catalog") if k in row}
                for row in readiness["servers"]], "cli": readiness["cli"]})}
        if prior:
            saved = supervisor.store.read(prior["path"], "tool-execution.schema.json")
            if digest(saved) != prior["digest"] or saved["identity"] != identity:
                raise HarnessError("EXACT_RESUME_DRIFT: backend/profile/catalog/config/worktree identity changed")
            if backend == "opencode" and capabilities.get("exact_resume_qualified") is not True:
                raise HarnessError("BLOCKED_NOT_QUALIFIED: OpenCode exact resume")
        # Snapshot includes dirty and untracked source bytes, unlike HEAD.
        from .work_contract import legacy_contract
        from .core import snapshot
        source = snapshot(legacy_contract(supervisor.contract, cwd=str(root), source_cwd=supervisor.state["source_cwd"],
            run_id=supervisor.state["run_id"], attempt=1, deadline=supervisor.state["deadline"]))["digest"]
        receipt = {"schema_version": 1, "run_id": supervisor.state["run_id"], "node_id": node_id,
            "attempt_id": attempt_id, "repository_identity": supervisor.repository_identity(),
            "base_revision": worktree["base_revision"] if worktree else supervisor.state["revision"],
            "source_snapshot_digest": source, "identity": identity, "readiness": "READY", "created_at": now()}
        path = "tooling/" + node_id.replace("@", "_") + "/" + attempt_id + "-" + digest(receipt) + ".yaml"
        supervisor.store.write(path, receipt, "tool-execution.schema.json", exclusive=True)
        ref = {"path": path, "digest": digest(receipt)}
        return ProfileExecution(adapter, profile, receipt), ref
    except (HarnessError, OSError, ValueError) as error:
        raise NotDispatched(str(error)) from error


def evidence_errors(supervisor):
    """Verify every new tooling receipt already admitted to the execution chain."""
    if not supervisor.store.path("tooling").exists():
        # Never erase a required binding simply by deleting its directory.
        if "execution" not in supervisor.contract and "review_execution" not in supervisor.contract:
            path = supervisor.state.get("plan_path")
            if not path or not any("execution" in n for n in supervisor.store.read(path, "agent-plan.tooling.v1.schema.json")["tasks"]):
                return []
    errors = []
    records = supervisor.receipts().records()
    refs = {r for row in records for r in row.get("input_refs", [])}
    required = {row["capability_receipt_ref"] for row in records if row.get("capability_receipt_ref")}
    directory = supervisor.store.path("tooling")
    seen = set()
    bindings = {}
    if directory.exists():
        for path in directory.glob("*/*.yaml"):
            try:
                value = supervisor.store.read(path.relative_to(supervisor.path).as_posix(), "tool-execution.schema.json")
                checksum = digest(value)
                if checksum in refs:
                    if value["run_id"] != supervisor.state["run_id"] or value["readiness"] != "READY":
                        errors.append("Tooling receipt subject/readiness mismatch")
                    seen.add(checksum)
                    bindings[checksum] = value
            except (OSError, ValueError, HarnessError):
                errors.append("Tooling receipt missing or corrupt")
    # Known binding projections provide a required, closed set even if a file
    # is deleted. Old agents have no binding and follow their legacy rules.
    for name in supervisor.state["agents"]:
        row = supervisor.store.read(supervisor.agent_path(name), "agent-state.v3.schema.json")
        ref = row.get("tool_provenance")
        if ref and ref["digest"] not in seen:
            errors.append("Tooling provenance is no longer bound to immutable invocation evidence: " + name)
    ref = supervisor.state.get("tool_provenance")
    if ref and ref["digest"] not in seen:
        errors.append("Root tooling provenance lost immutable invocation binding")
    if required - seen:
        errors.append("Required execution tool receipts missing or modified")
    intents = {row["invocation_id"]: row for row in records if row["record_type"] == "invocation_intent"}
    for row in records:
        ref = row.get("capability_receipt_ref")
        if not ref or ref not in bindings:
            continue
        binding = bindings[ref]
        intent = intents.get(row["invocation_id"])
        if (intent is None or ref not in row["input_refs"] or ref != intent.get("capability_receipt_ref")
                or binding["run_id"] != row["run_id"] or binding["repository_identity"] != row["project_id"]
                or binding["node_id"] != row["node_id"] or binding["attempt_id"] != row["attempt_id"]
                or canonical_directory(binding["identity"]["root"]) != canonical_directory(intent["payload"]["checkout"])):
            errors.append("Execution tool receipt subject differs from invocation")
    return errors


def revalidate(adapter, binding, selected):
    """Cheap immutable-input check immediately before invocation intent."""
    from .tool_profiles import load_profile, profile_digest
    from .codex_execution import _binary_hash
    value = binding["identity"]
    root = canonical_directory(value["root"])
    info = root.stat()
    if selected.get("graph_index"):
        from .strict_yaml import load
        from .tooling_provenance import source_snapshot, verify_graph_index
        graph = load(selected["graph_index"]["path"])
        if digest(graph) != value["graph_index_digest"]:
            raise NotDispatched("EXACT_RESUME_DRIFT: graph index receipt changed")
        verify_graph_index(graph, source_snapshot(root))
    transport = adapter.adapter if isinstance(adapter, ProfileExecution) else adapter
    if (profile_digest(load_profile(selected.get("tool_profile"))) != value["profile_digest"]
            or [info.st_dev, info.st_ino] != value["directory_identity"]
            or _configuration_identity(transport, root) != value["configuration_inputs_digest"]
            or _binary_hash(transport.executable[0]) != value["binary_sha256"]):
        raise NotDispatched("EXACT_RESUME_DRIFT: execution inputs changed before dispatch")
    cache = value["cache_root"]
    if _launch_identity(transport, load_profile(selected.get("tool_profile")), root, cache) != value["launch_files_digest"]:
        raise NotDispatched("EXACT_RESUME_DRIFT: executable or required dependency files changed")
