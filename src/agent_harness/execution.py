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
import re

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
    user_home = Path(env.get("HOME", env.get("USERPROFILE", str(Path.home()))))
    codex_home = Path(env.get("CODEX_HOME", user_home / ".codex"))
    paths = [codex_home / "config.toml"]
    # Include the whole project chain, including untrusted config, so later
    # trust/config changes cannot silently change a resumed invocation.
    for parent in [root, *root.parents]:
        paths.extend([parent / ".codex" / "config.toml", parent / "opencode.json", parent / "opencode.jsonc",
                      parent / ".opencode" / "opencode.json", parent / ".opencode" / "opencode.jsonc"])
    xdg = Path(env.get("XDG_CONFIG_HOME", user_home / ".config")) / "opencode"
    paths.extend([xdg / "opencode.json", xdg / "opencode.jsonc"])
    # Native V2 also loads explicitly selected files/directories. These may
    # select a different model or provider without changing the tool fragment.
    for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"):
        if not env.get(key):
            continue
        raw = env[key]
        if raw == "~":
            path = user_home
        elif raw.startswith(("~/", "~\\")):
            path = user_home / raw[2:]
        else:
            path = Path(raw)
        path = path if path.is_absolute() else root / path
        paths.extend([path / "opencode.json", path / "opencode.jsonc"]
                     if key == "OPENCODE_CONFIG_DIR" else [path])
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


def _qualification_ref(supervisor, adapter, role):
    receipt = getattr(adapter, "qualification_receipt", None)
    if not callable(receipt):
        if callable(getattr(adapter, "qualify", None)):
            raise HarnessError("BLOCKED_NOT_QUALIFIED: backend has no immutable qualification receipt")
        return None
    ref = receipt()
    if ref is None:
        if callable(getattr(adapter, "qualify", None)):
            raise HarnessError("BLOCKED_NOT_QUALIFIED: backend qualification receipt missing")
        return None
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise HarnessError("Invalid native qualification reference")
    role = {"owner": "worker", "helper": "worker", "architect": "planner"}.get(role, role)
    validate({**ref, "role": role}, _schema("tool-execution.schema.json")["properties"]["client_qualification"])
    path = Path(ref["path"])
    if not path.is_absolute() or ".." in path.parts:
        raise HarnessError("Native qualification reference must be an absolute owned path")
    try:
        relative = path.relative_to(supervisor.path).as_posix()
    except ValueError as error:
        raise HarnessError("Native qualification reference escaped private run store") from error
    result = {"path": relative, "sha256": ref["sha256"], "role": role}
    validate(result, _schema("tool-execution.schema.json")["properties"]["client_qualification"])
    if hashlib.sha256(read_regular_file(supervisor.store.path(relative), max_bytes=4 * 1024 * 1024)).hexdigest() != ref["sha256"]:
        raise HarnessError("Native qualification receipt bytes changed")
    return result


def available_profile(profile, readiness):
    """Narrow native grants using diagnostics; this never attests client tools.

    An unavailable optional server is omitted, and an allowed-but-unadvertised
    optional tool is not a readiness requirement. Required tools still fail
    closed. The native verifier must separately audit its live runtime catalog.
    """
    result = deepcopy(profile)
    observed = {row["id"]: row for row in readiness["servers"]}
    result["servers"] = []
    for server in profile["servers"]:
        row = observed.get(server["id"], {})
        if row.get("status") != "READY":
            if server["required"]:
                raise HarnessError("Required MCP unavailable during native configuration: " + server["id"])
            continue
        names = {tool["name"] for tool in row["catalog"]}
        if not set(server["required_tools"]) <= names:
            raise HarnessError("Required MCP tools missing during native configuration: " + server["id"])
        narrowed = deepcopy(server)
        narrowed["allowed_tools"] = [name for name in server["allowed_tools"] if name in names]
        narrowed["read_only_tools"] = [name for name in server.get("read_only_tools", [])
                                       if name in narrowed["allowed_tools"]]
        result["servers"].append(narrowed)
    return result


def require_native_dependencies(profile, adapter, capabilities):
    """Required MCP readiness must belong to the actual native client."""
    required = [server for server in profile["servers"] if server["required"]]
    if not required:
        return
    connected = capabilities.get("connected_mcp_servers")
    if (not isinstance(connected, list) or any(not isinstance(item, str) for item in connected)
            or not {s["id"] for s in required} <= set(connected)):
        raise HarnessError("Required MCP is not connected in the native execution client")
    catalog = adapter.effective_tool_catalog()
    names = {tool["name"] for tool in catalog}
    for server in required:
        prefix = re.sub(r"[^A-Za-z0-9_]", "_", server["id"])
        wanted = {prefix + "_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
                  for name in server["required_tools"]}
        if not wanted <= names:
            raise HarnessError("Required MCP tools absent from native execution catalog: " + server["id"])


def _verify_qualification_ref(supervisor, binding, *, require_execution=False, session_id=None, catalog_artifacts=None):
    """Persisted evidence verifies closure; it never grants native capabilities."""
    ref = binding.get("client_qualification")
    if ref is None:
        return
    path = supervisor.store.path(ref["path"])
    if hashlib.sha256(read_regular_file(path, max_bytes=4 * 1024 * 1024)).hexdigest() != ref["sha256"]:
        raise HarnessError("Native qualification evidence changed")
    if binding["identity"]["backend"] != "opencode":
        raise HarnessError("Unsupported native qualification evidence backend")
    try:
        from .opencode_qualification import verify_qualification_evidence
    except ImportError as error:
        raise HarnessError("Native qualification evidence verifier unavailable") from error
    verify_qualification_evidence({"path": str(path), "sha256": ref["sha256"]},
        root=Path(binding["identity"]["root"]), role=ref["role"],
        require_execution=require_execution, session_id=session_id, catalog_artifacts=catalog_artifacts)


def _configured(supervisor, selected, *, root, role, node_id, attempt_id):
    """Configure first, then ask the native verifier for fresh strong claims.

    This helper does not reserve a model/planning slot or mutate run state. Its
    bounded qualification artifacts belong to the existing private run store.
    """
    from .tool_profiles import load_profile, preflight_profile, render_codex, render_opencode
    from .strict_yaml import load
    validate_selection(selected)
    profile_role = {"owner": "worker", "helper": "worker", "architect": "planner"}.get(role, role)
    backend = selected.get("backend", "codex")
    adapter = supervisor._adapter(backend)
    capabilities = adapter.capabilities(deadline=supervisor.state["deadline"])
    if capabilities.get("status") == "unsupported":
        raise HarnessError("UNSUPPORTED_CAPABILITY: " + backend + " version " + str(capabilities.get("version", "unknown")))
    if capabilities.get("status") not in {"observed", "qualified"}:
        raise HarnessError("Backend capability preflight unavailable")
    profile = load_profile(selected.get("tool_profile"))
    if supervisor.remaining() <= 0:
        raise HarnessError("Absolute task deadline expired before tool readiness")
    root = canonical_directory(root)
    cache = supervisor.path / "tool-cache" / node_id.replace("@", "_")
    graph = load(selected["graph_index"]["path"]) if selected.get("graph_index") else None
    readiness = preflight_profile(profile, backend, root, profile_role, cache_root=cache, graph_index=graph)
    if readiness["status"] != "READY":
        raise HarnessError("TOOL_PROFILE_NOT_READY: " + json.dumps(readiness, ensure_ascii=False))
    if supervisor.remaining() <= 0:
        raise HarnessError("Absolute task deadline expired during tool readiness")
    runtime_profile = available_profile(profile, readiness) if backend == "opencode" else profile
    native = (render_codex if backend == "codex" else render_opencode)(runtime_profile, root, role=profile_role, cache_root=cache)
    configure = getattr(adapter, "with_tool_config", None)
    if native and callable(configure):
        adapter = configure(native, root=root)
    elif profile["id"] != "baseline" and not callable(configure):
        raise HarnessError("BLOCKED_NOT_QUALIFIED: backend lacks tool configuration support")
    qualify = getattr(adapter, "qualify", None)
    if callable(qualify):
        remaining = supervisor.remaining()
        if remaining <= 0:
            raise HarnessError("Absolute task deadline expired before backend qualification")
        adapter = qualify(root=root, role=profile_role,
            evidence_dir=cache / "qualification" / attempt_id, timeout=min(remaining, 120))
        verify = getattr(adapter, "verify_qualification", None)
        if not callable(verify):
            raise HarnessError("BLOCKED_NOT_QUALIFIED: backend qualification has no verifier")
        verify(root=root, role=profile_role)
        # Qualification may inject a core-owned pre-turn catalog guard. Hash
        # the final native configuration, not the earlier renderer fragment.
        native = deepcopy(getattr(adapter, "tool_config", getattr(adapter, "_tool_config", native)))
    capabilities = adapter.capabilities(deadline=supervisor.state["deadline"])
    if capabilities.get("status") not in {"observed", "qualified"}:
        raise HarnessError("BLOCKED_NOT_QUALIFIED: configured backend capability preflight unavailable")
    if backend == "opencode" and capabilities.get("managed_leaf_qualified") is not True:
        raise HarnessError("BLOCKED_NOT_QUALIFIED: OpenCode managed leaf isolation is not live qualified")
    for name in selected.get("required_capabilities", []):
        if capabilities.get(name) is not True:
            raise HarnessError("BLOCKED_NOT_QUALIFIED: required backend capability " + name)
    if profile["servers"] and capabilities.get("effective_tool_catalog") is not True:
        raise HarnessError("BLOCKED_NOT_QUALIFIED: execution-bound effective MCP catalog is unqualified; diagnostic catalog is insufficient")
    if callable(qualify):
        if capabilities.get("effective_tool_catalog") is not True:
            raise HarnessError("BLOCKED_NOT_QUALIFIED: native verifier lacks effective catalog evidence")
        validate(capabilities.get("effective_catalog_digest"),
            _schema("tool-execution.schema.json")["properties"]["identity"]["properties"]["catalog_digest"])
        _qualification_ref(supervisor, adapter, profile_role)
        if backend == "opencode":
            require_native_dependencies(profile, adapter, capabilities)
    if role == "reviewer" and capabilities.get("reviewer_isolation_qualified") is not True:
        raise HarnessError("BLOCKED_NOT_QUALIFIED: effective reviewer catalog, shell, plugins and MCP isolation need live enforcement")
    if supervisor.remaining() <= 0:
        raise HarnessError("Absolute task deadline expired during backend qualification")
    return adapter, profile, readiness, native, capabilities, root, cache, graph


def admit_reviewer(supervisor):
    """Read-only final-review admission before planning/worker reservations."""
    return _configured(supervisor, selection(supervisor.contract, review=True),
        root=supervisor.state["cwd"], role="reviewer", node_id="@review-admission", attempt_id="admission")[0]


def prepare(supervisor, selected, *, root, role, node_id, attempt_id, worktree=None, prior=None):
    """Fresh native qualification before model admission; source-bound receipt.

    `prior` is required on exact resume and checked before any model slot. The
    existing process/ownership recovery remains authoritative and precedes this.
    """
    from .tool_profiles import profile_digest
    backend = selected.get("backend", "codex")
    try:
        if prior:
            saved = supervisor.store.read(prior["path"], "tool-execution.schema.json")
            if digest(saved) != prior["digest"]:
                raise HarnessError("EXACT_RESUME_DRIFT: saved tool receipt changed")
            _verify_qualification_ref(supervisor, saved)
        adapter, profile, readiness, native, capabilities, root, cache, graph = _configured(
            supervisor, selected, root=root, role=role, node_id=node_id, attempt_id=attempt_id)
        info = root.stat()
        identity = {"backend": backend, "executable": list(getattr(adapter, "executable", ())),
            "binary_sha256": capabilities.get("binary_sha256", "unobserved"), "version": capabilities.get("version", "unobserved"),
            "profile_id": profile["id"], "profile_version": profile["version"], "profile_digest": profile_digest(profile),
            "effective_config_digest": digest(native), "configuration_inputs_digest": _configuration_identity(adapter, root),
            "catalog_digest": capabilities.get("effective_catalog_digest", readiness["catalog_digest"]),
            "required_dependencies": readiness["required_dependencies"],
            "root": str(root), "directory_identity": [info.st_dev, info.st_ino],
            "worktree_digest": digest(worktree) if worktree else None,
            "selection_digest": digest(selected), "graph_index_digest": digest(graph) if graph else None, "cache_root": str(cache),
            "launch_files_digest": _launch_identity(adapter, profile, root, cache),
            "dependency_fingerprint": digest({"servers": [{k: row[k] for k in ("id", "executable", "binary_sha256", "server_info", "catalog") if k in row}
                for row in readiness["servers"]], "cli": readiness["cli"]})}
        if capabilities.get("effective_catalog_digest"):
            identity["diagnostic_catalog_digest"] = readiness["catalog_digest"]
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
        qualification = _qualification_ref(supervisor, adapter, role)
        if qualification is not None:
            receipt["client_qualification"] = qualification
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
    for checksum, binding in bindings.items():
        try:
            completions = [row for row in records if row["record_type"] == "completion"
                and row.get("capability_receipt_ref") == checksum and row["payload"]["outcome"] == "completed"]
            if completions:
                for completion in completions:
                    session_id = completion["payload"].get("thread_id")
                    if binding.get("client_qualification") and not session_id:
                        raise HarnessError("Native qualification execution session missing")
                    catalog_artifacts = None
                    if binding.get("client_qualification"):
                        from .execution_receipts import verify_tool_artifacts
                        catalog_artifacts = verify_tool_artifacts(supervisor, completion)
                        if not catalog_artifacts:
                            raise HarnessError("Native execution catalog has no immutable completion artifact")
                    _verify_qualification_ref(supervisor, binding, require_execution=True,
                        session_id=session_id, catalog_artifacts=catalog_artifacts)
            else:
                _verify_qualification_ref(supervisor, binding)
        except (HarnessError, OSError, ValueError, ImportError):
            errors.append("Native client qualification evidence missing, changed or execution unconfirmed")
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
    verify = getattr(transport, "verify_qualification", None)
    if binding.get("client_qualification") and not callable(verify):
        raise NotDispatched("Native qualification verifier missing before dispatch")
    if callable(verify):
        ref = binding.get("client_qualification")
        if ref is None:
            raise NotDispatched("Native qualification binding missing before dispatch")
        verify(root=root, role=ref["role"])
        actual = getattr(transport, "qualification_receipt", lambda: None)()
        if actual != {"path": str(Path(value["cache_root"]).parents[1] / ref["path"]), "sha256": ref["sha256"]}:
            raise NotDispatched("Native qualification reference changed before dispatch")
    if _launch_identity(transport, load_profile(selected.get("tool_profile")), root, cache) != value["launch_files_digest"]:
        raise NotDispatched("EXACT_RESUME_DRIFT: executable or required dependency files changed")
