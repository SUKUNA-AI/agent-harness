"""Closed, reproducible tool policies; configuration is never client attestation.

Profiles render native client configuration. The diagnostic MCP probe neither
proxies model calls nor proves effective client permissions. Dispatch is allowed
only after the execution backend independently establishes the required policy.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .core import HarnessError, digest, file_hash
from .file_io import read_regular_file
from .runner import _contained_capture
from .strict_yaml import load, validate

PROFILE_SCHEMA = "tool-profile.schema.json"
ROLES = ("worker", "reviewer", "planner", "root")
_READ_TOOLS = ["find_symbol", "find_referencing_symbols", "get_symbols_overview", "search_for_pattern", "read_file"]


_CREDENTIAL_ENV = re.compile(r"TOKEN|SECRET|PASS(?:WORD|WD)|CREDENTIAL|API[_-]?KEY|AUTHORIZATION", re.I)
_CREDENTIAL_OPTIONS = {"password", "passwd", "token", "api-key", "apikey", "secret",
    "client-secret", "credential", "credentials", "authorization", "auth", "bearer"}


def _check_credential_argv(argv):
    for item in argv:
        if not isinstance(item, str) or not item.startswith("-"):
            continue
        option = item.split("=", 1)[0].lstrip("-").lower().replace("_", "-")
        if (option in _CREDENTIAL_OPTIONS or option.endswith(("-token", "-secret", "-password",
                "-passwd", "-credential", "-credentials", "-api-key", "-authorization"))):
            raise HarnessError("Credential-bearing command flags are forbidden in Tool Profiles; use env_vars names or native client auth")


def _serena():
    # The local CLI must confirm these options before this command may launch.
    return {"id": "serena", "transport": "stdio", "required": True,
        "command": ["serena", "start-mcp-server", "--project", "{worktree}"],
        "help_argv": ["serena", "start-mcp-server", "--help"],
        "required_help": ["--project"], "allowed_tools": _READ_TOOLS,
        "read_only_tools": _READ_TOOLS, "required_tools": ["find_symbol"],
        "binding": "worktree", "cache": "private",
        "environment": {"SERENA_HOME": "{cache_root}/serena"}}


def builtin_profiles():
    """Return independent policy templates. No dependency is installed or fetched."""
    baseline = {"schema_version": 1, "id": "baseline", "version": 1,
        "backends": ["codex", "opencode"], "roles": list(ROLES), "servers": [], "cli": []}
    values = {"baseline": baseline}
    values["coding"] = {**baseline, "id": "coding", "roles": ["worker", "root"],
        "servers": [_serena()], "cli": [{"id": "ast-grep", "commands": ["ast-grep", "sg"],
        "required": False, "version_pattern": r"ast-grep\s+\d"}]}
    values["architecture"] = {**baseline, "id": "architecture", "roles": ["worker", "planner", "root"],
        "servers": [_serena()], "cli": [{"id": "graphify", "commands": ["graphify"], "required": True}],
        "index": {"required": True, "policy": "frozen_source", "provider": "graphify"}}
    values["reviewer-readonly"] = {**baseline, "id": "reviewer-readonly", "roles": ["reviewer"],
        "cache": "readonly"}
    # No unverified CLI command recipes: the operator supplies an installed
    # server's actual invocation after version/help verification.
    values["browser-testing"] = {**baseline, "id": "browser-testing", "roles": ["worker", "root"],
        "browser": {"isolated": True, "artifacts": "browser-artifacts", "allow_test_writes": False}}
    from .local_tool_integrations import context7_server
    for base in ("coding", "architecture"):
        values[base + "-context7"] = {**deepcopy(values[base]), "id": base + "-context7",
            "servers": [*deepcopy(values[base]["servers"]), context7_server()]}
    values["documents"] = {**baseline, "id": "documents", "roles": ["worker", "root"],
        "cli": [{"id": "docling", "commands": ["docling"], "required": True}],
        "documents": {"mode": "local-only", "max_input_bytes": 8388608, "timeout": 120,
            "input_root": "{worktree}/documents", "output_root": "{cache_root}/documents"}}
    return deepcopy(values)


def load_profile(reference=None):
    """Load a builtin ID, path, closed reference {id,version,path?}, or value."""
    if reference is None:
        reference = "baseline"
    expected = None
    if isinstance(reference, dict) and "schema_version" not in reference:
        if set(reference) - {"id", "version", "path"} or "id" not in reference or "version" not in reference:
            raise HarnessError("Tool profile reference must contain id/version and optional path only")
        if not isinstance(reference["id"], str) or type(reference["version"]) is not int:
            raise HarnessError("Tool profile reference has invalid types")
        expected = (reference["id"], reference["version"])
        reference = reference.get("path", reference["id"])
    if isinstance(reference, dict):
        value = deepcopy(reference)
    elif isinstance(reference, (str, Path)):
        builtins = builtin_profiles()
        value = builtins[str(reference)] if str(reference) in builtins else load(Path(reference))
    else:
        raise HarnessError("Unsupported tool profile reference")
    validate(value, PROFILE_SCHEMA)
    # JSON inputs carry no aliases; detach any shared lists in Python callers too.
    value = json.loads(json.dumps(value))
    value.setdefault("backends", ["codex", "opencode"])
    value.setdefault("roles", list(ROLES))
    value.setdefault("availability", "required")
    value.setdefault("servers", [])
    value.setdefault("cli", [])
    value.setdefault("binding", "worktree")
    value.setdefault("cache", "private")
    value.setdefault("timeouts", {"startup": 15, "tool": 60})
    seen = set()
    for server in value["servers"]:
        if server["id"] in seen:
            raise HarnessError("Duplicate MCP server ID")
        seen.add(server["id"])
        for key, default in (("required", True), ("allowed_tools", []), ("forbidden_tools", []),
                ("required_tools", []), ("read_only_tools", []), ("environment", {}), ("env_vars", []),
                ("readiness", "live"), ("binding", "worktree"), ("cache", "private"),
                ("startup_timeout", value["timeouts"]["startup"]), ("tool_timeout", value["timeouts"]["tool"])):
            server.setdefault(key, default)
        allowed, denied = set(server["allowed_tools"]), set(server["forbidden_tools"])
        if allowed & denied or not set(server["required_tools"]) <= allowed:
            raise HarnessError("Required/allowed/forbidden MCP tools conflict")
        if not set(server["read_only_tools"]) <= allowed:
            raise HarnessError("Read-only tools must be explicitly allowed")
        if not set(server.get("required_results", server["required_tools"])) <= set(server["required_tools"]):
            raise HarnessError("Required execution results must be a subset of required catalog tools")
        if server["transport"] == "stdio":
            if not server.get("command") or "url" in server:
                raise HarnessError("stdio MCP requires command and forbids URL")
        elif not server.get("url") or "command" in server:
            raise HarnessError("HTTP MCP requires URL and forbids command")
        if server["binding"] == "shared_readonly" and (server["cache"] != "readonly" or set(server["read_only_tools"]) != allowed):
            raise HarnessError("Shared MCP requires audited read-only tools and index/cache")
        # Profiles are shareable policy. Secret values belong to inherited env
        # names or client auth stores, never rendered files/checkpoints.
        from .codex_execution import sanitized
        if any(_CREDENTIAL_ENV.search(key) for key in server["environment"]):
            raise HarnessError("Credential-bearing environment literals are forbidden; declare env_vars names only")
        _check_credential_argv(server.get("command", []))
        _check_credential_argv(server.get("help_argv", []))
        if sanitized(server) != server:
            raise HarnessError("Tool profile contains credential-like material")
    for dep in value["cli"]:
        if not dep["commands"]:
            raise HarnessError("CLI dependency needs at least one command")
        dep.setdefault("required", True)
        dep.setdefault("version_args", ["--version"])
        dep.setdefault("capabilities", [])
        _check_credential_argv(dep["version_args"])
    validate(value, PROFILE_SCHEMA)
    if expected and expected != (value["id"], value["version"]):
        raise HarnessError("Tool profile reference identity/version mismatch")
    return value


def profile_digest(profile):
    return digest(load_profile(profile))


def _roots(root, cache_root):
    root = Path(root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise HarnessError("Profile worktree must be an ordinary absolute directory")
    from .supervisor_store import _parents, _ordinary
    _parents(root)
    _ordinary(root, directory=True)
    canonical = str(root.resolve())
    if cache_root is None:
        # Pure rendering never creates directories; dispatch chooses a private
        # per-attempt cache outside the candidate and persists its identity.
        cache_root = Path(root).parent / (".harness-tools-" + digest(canonical)[:16])
    cache_root = Path(cache_root)
    if not cache_root.is_absolute():
        raise HarnessError("Tool cache requires an absolute path")
    if ".." in cache_root.parts:
        raise HarnessError("Tool cache path must not contain parent traversal")
    # Pure render tolerates not-yet-created private cache parents, but inspects
    # every existing ancestor without following links/reparse points.
    for candidate in [*reversed(cache_root.parents), cache_root]:
        if candidate.exists() or candidate.is_symlink():
            _ordinary(candidate, directory=True)
    # Even a shared operator cache base gets a separate mutable worktree namespace.
    return canonical, str(cache_root / digest(canonical)[:16])


def _expand(value, root, cache):
    if isinstance(value, str):
        result = value.replace("{worktree}", root).replace("{cache_root}", cache)
        if "{" in result or "}" in result or "\x00" in result:
            raise HarnessError("Unsupported tool profile placeholder")
        return result
    if isinstance(value, list):
        return [_expand(child, root, cache) for child in value]
    if isinstance(value, dict):
        return {key: _expand(child, root, cache) for key, child in value.items()}
    return value


def resolved_servers(profile, root, *, cache_root=None):
    profile = load_profile(profile)
    root, cache = _roots(root, cache_root)
    result = []
    for spec in profile["servers"]:
        server = _expand(spec, root, cache)
        server["cwd"] = root
        if server["id"] == "playwright-managed":
            if profile.get("browser"):
                raise HarnessError("Managed browser server ID is reserved for the browser policy")
            _managed_browser_policy(server, root, cache)
        if server["id"] in {"serena", "context7"}:
            from . import local_tool_integrations as local
            if server["transport"] != "stdio":
                raise HarnessError("Managed local tool requires a worktree-bound stdio process")
            command = server["command"]
            helper = str(Path(local.__file__).resolve())
            prepared = len(command) > 2 and command[1] == helper
            if server["id"] == "serena":
                if prepared:
                    if "--executable" not in command or command[-1] == "--executable":
                        raise HarnessError("Managed Serena launcher has no executable binding")
                    executable = command[command.index("--executable") + 1]
                elif len(command) == 4 and command[1:] == ["start-mcp-server", "--project", root]:
                    executable = command[0]
                else:
                    raise HarnessError("Serena requires the managed worktree-bound launcher")
                expected = local.serena_stdio_argv(root, Path(cache) / "serena", executable=executable)
                server["environment"]["SERENA_HOME"] = str(Path(cache) / "serena" / "serena-home")
            else:
                if prepared:
                    if "--executable" not in command or command[-1] == "--executable":
                        raise HarnessError("Managed Context7 launcher has no executable binding")
                    executable = command[command.index("--executable") + 1]
                elif len(command) == 3 and command[1:] == ["--transport", "stdio"]:
                    executable = command[0]
                else:
                    raise HarnessError("Context7 requires the managed public-read launcher")
                expected = local.context7_stdio_argv(root, Path(cache) / "context7", executable=executable)
            if prepared and command != expected:
                raise HarnessError("Managed tool launcher binding drifted")
            server["command"] = expected
            server.pop("help_argv", None)
            server.pop("required_help", None)
        result.append(server)
    if profile.get("browser"):
        from .browser_integration import browser_server
        if any(server["id"] == "playwright-managed" for server in result):
            raise HarnessError("Managed browser server ID is reserved for the browser policy")
        result.append(browser_server(_browser_policy(profile), root=Path(root), private_root=Path(cache).parent))
    return result


def _browser_policy(profile):
    policy = profile["browser"]
    required = ("allowed_origins", "mcp_package", "browser_executable", "browser_cache")
    missing = [key for key in required if not policy.get(key)]
    if missing:
        raise HarnessError("CONFIG_REQUIRED: browser-testing requires explicit " + ", ".join(missing))
    from .browser_integration import validate_browser_policy
    return validate_browser_policy(policy)


def _managed_browser_policy(server, root, cache):
    """Runtime logical specs retain the exact core launcher and closed grants."""
    import base64
    from .browser_integration import browser_server
    command = server.get("command", [])
    if len(command) != 8:
        raise HarnessError("Reserved browser server requires the core managed launcher")
    try:
        policy = json.loads(base64.b64decode(command[5], altchars=b"-_", validate=True))
        expected = browser_server(policy, root=Path(root), private_root=Path(cache).parent)
    except (ValueError, TypeError) as error:
        raise HarnessError("Invalid managed browser policy binding") from error
    if any(server.get(key) != value for key, value in expected.items() if key != "cwd"):
        raise HarnessError("Reserved browser server launcher or grants drifted")
    return policy


def execution_profile(profile, root, *, cache_root=None):
    """Resolve dynamic dependencies into a closed runtime-only logical profile.

    Callers retain the original profile for its source identity. Removing cwd
    and the expanded browser policy keeps rendering idempotent and schema-valid.
    """
    result = load_profile(profile)
    result["servers"] = [{key: value for key, value in spec.items() if key != "cwd"}
        for spec in resolved_servers(result, root, cache_root=cache_root)]
    result.pop("browser", None)
    return load_profile(result)


def _role(profile, backend, role):
    if backend not in profile["backends"] or role not in profile["roles"]:
        raise HarnessError("Tool profile incompatible with backend/role")
    if role == "reviewer":
        for server in profile["servers"]:
            if set(server["allowed_tools"]) != set(server["read_only_tools"]):
                raise HarnessError("Reviewer profile includes tools without explicit read-only audit")
        if profile.get("browser") or profile.get("documents"):
            raise HarnessError("Reviewer cannot perform browser/document conversion writes")


def render_codex(profile, root, *, cache_root=None, role="worker"):
    """Native configuration projection; caller controls CLI overrides/precedence."""
    profile = load_profile(profile)
    _role(profile, "codex", role)
    servers = {}
    for spec in resolved_servers(profile, root, cache_root=cache_root):
        item = {"enabled": True, "required": spec["required"],
            "enabled_tools": spec["allowed_tools"], "disabled_tools": spec["forbidden_tools"],
            "startup_timeout_sec": spec["startup_timeout"], "tool_timeout_sec": spec["tool_timeout"],
            "startup_readiness": "connection"}
        if spec["transport"] == "stdio":
            item.update(command=spec["command"][0], args=spec["command"][1:], cwd=spec["cwd"],
                env=spec["environment"], env_vars=spec["env_vars"])
        else:
            item["url"] = spec["url"]
        servers[spec["id"]] = item
    return {"mcp_servers": servers}


def render_opencode(profile, root, *, cache_root=None, role="worker"):
    """OpenCode V2 only: no V1 permission/task/bash configuration."""
    profile = load_profile(profile)
    _role(profile, "opencode", role)
    if role == "planner" and (any(set(server["allowed_tools"]) != set(server["read_only_tools"])
            for server in profile["servers"]) or profile.get("browser") or profile.get("documents")):
        raise HarnessError("OpenCode planner requires audited read-only tools")
    servers = {}
    # A native subagent denial does not stop an arbitrary shell from launching
    # another model CLI. Qualified leaves use native file tools; the supervisor
    # owns compilers/tests and other argv-based checks.
    rules = [{"action": "*", "resource": "*", "effect": "deny"}]
    rules += [{"action": action, "resource": "*", "effect": "allow"} for action in ("read", "glob", "grep")]
    if role == "worker":
        rules.append({"action": "edit", "resource": "*", "effect": "allow"})
    for spec in resolved_servers(profile, root, cache_root=cache_root):
        item = {"type": "local" if spec["transport"] == "stdio" else "remote",
            "codemode": False, "pool": False,
            "timeout": {"startup": int(spec["startup_timeout"] * 1000),
                        "catalog": int(spec["startup_timeout"] * 1000),
                        "execution": int(spec["tool_timeout"] * 1000)}}
        if spec["transport"] == "stdio":
            item.update(command=spec["command"], cwd=spec["cwd"], environment={**spec["environment"],
                **{key: "{env:" + key + "}" for key in spec["env_vars"]}})
        else:
            item["url"] = spec["url"]
        servers[spec["id"]] = item
        prefix = re.sub(r"[^A-Za-z0-9_]", "_", spec["id"])
        rules.append({"action": prefix + "_*", "resource": "*", "effect": "deny"})
        rules.extend({"action": prefix + "_" + re.sub(r"[^A-Za-z0-9_]", "_", tool),
            "resource": "*", "effect": "allow"} for tool in spec["allowed_tools"])
    rules.extend({"action": action, "resource": "*", "effect": "deny"}
        for action in ("subagent", "execute", "external_directory", "shell", "skill"))
    if role in {"reviewer", "planner"}:
        rules.extend({"action": action, "resource": "*", "effect": "deny"} for action in ("shell", "edit"))
    return {"default_agent": "harness-leaf", "mcp": {"servers": servers}, "agents": {"harness-leaf": {"mode": "primary", "permissions": rules}},
        "policies": [{"action": "tool.use", "resource": "subagent:*", "effect": "deny"},
                     {"action": "plugin.use", "resource": "*", "effect": "deny"},
                     {"action": "mcp.use", "resource": "*", "effect": "deny"},
                     *({"action": "mcp.use", "resource": name, "effect": "allow"} for name in sorted(servers))]}


def inspect_cli(dependency, *, timeout=10):
    """Check command identity; Linux's unrelated sg never qualifies as ast-grep."""
    for command in dependency["commands"]:
        executable = shutil.which(command)
        if executable is None:
            continue
        try:
            result = _contained_capture([executable, *dependency.get("version_args", ["--version"])], timeout=timeout)
            output = result.stdout.decode("utf-8", errors="replace").strip()
            if len(output) > 65536 or result.returncode:
                continue
            if dependency.get("version_pattern") and not re.search(dependency["version_pattern"], output):
                continue
            if dependency["id"] == "ast-grep" and not re.search(r"ast-grep\s+\d", output):
                continue
            capabilities = dependency.get("capabilities", [])
            if capabilities:
                help_result = _contained_capture([executable, "--help"], timeout=timeout)
                help_text = help_result.stdout.decode("utf-8", errors="replace")
                if help_result.returncode or len(help_text) > 65536 or any(capability not in help_text for capability in capabilities):
                    continue
            from .codex_execution import sanitized
            output = sanitized(output)
            return {"id": dependency["id"], "status": "READY", "capabilities": list(capabilities), "executable": str(Path(executable).resolve()),
                "binary_sha256": file_hash(Path(executable).resolve()), "version": output[:4096]}
        except (OSError, HarnessError, subprocess.TimeoutExpired):
            continue
    return {"id": dependency["id"], "status": "NOT_AVAILABLE", "reason": "executable/version unavailable or unsupported"}


def preflight_profile(profile, backend, root, role="worker", *, cache_root=None, graph_index=None):
    """Live dependency diagnostics, explicitly separate from client attestation."""
    from .tooling_probe import probe_server
    profile = load_profile(profile)
    receipt = {"schema_version": 1, "status": "READY", "profile_id": profile["id"],
        "profile_version": profile["version"], "profile_digest": profile_digest(profile),
        "backend": backend, "role": role, "root": str(Path(root).resolve()), "servers": [], "cli": [],
        "required_dependencies": [],
        "client_catalog_verified": False, "acceptance": "not_performed"}
    try:
        _role(profile, backend, role)
        servers = resolved_servers(profile, root, cache_root=cache_root)
        receipt["required_dependencies"] = [s["id"] for s in servers if s["required"]]
        browser_policy = (_browser_policy(profile) if profile.get("browser") else
            next((_managed_browser_policy(server, str(Path(root).resolve()), _roots(root, cache_root)[1])
                for server in servers if server["id"] == "playwright-managed"), None))
        if browser_policy is not None:
            from .browser_integration import qualify_browser
            _, cache = _roots(root, cache_root)
            receipt["browser"] = qualify_browser(browser_policy, root=Path(root), private_root=Path(cache).parent,
                timeout=min(profile["timeouts"]["startup"], 30))
            if receipt["browser"]["status"] != "QUALIFIED":
                receipt["status"] = "BLOCKED"
                receipt["reason"] = "BLOCKED_NOT_QUALIFIED: " + receipt["browser"].get("reason", "actual browser lifecycle failed")
        index_policy = profile.get("index", {})
        if index_policy.get("required") and graph_index is None:
            raise HarnessError("REQUIRED_GRAPH_INDEX_MISSING: bind a verified frozen-source index receipt before dispatch")
        if graph_index is not None:
            if not isinstance(graph_index, dict):
                raise HarnessError("Graph index binding must be a full provenance receipt")
            from .tooling_provenance import source_snapshot, verify_graph_index
            verify_graph_index(graph_index, source_snapshot(root))
            if index_policy and graph_index.get("provider") != index_policy.get("provider"):
                raise HarnessError("Graph index provider differs from profile")
            receipt["graph_index"] = {key: graph_index[key] for key in
                ("identity", "index_digest", "source_snapshot_digest", "index_path")}
        for dep in profile["cli"]:
            observed = inspect_cli(dep, timeout=profile["timeouts"]["startup"])
            receipt["cli"].append(observed)
            if observed["status"] != "READY" and dep["required"]:
                receipt["status"] = "BLOCKED"
        for server in servers:
            observed = probe_server(server, role=role)
            receipt["servers"].append(observed)
            if observed["status"] != "READY" and server["required"]:
                receipt["status"] = "BLOCKED"
        if profile.get("documents"):
            receipt["status"] = "BLOCKED"
            receipt["requested_conversion_mode"] = profile["documents"]["mode"]
            receipt["actual_conversion_mode"] = "not_run"
            receipt["reason"] = "BLOCKED_NOT_QUALIFIED: controlled conversion runner required for " + profile["documents"]["mode"] + "; CLI presence does not enforce input/output/size/time/network policy"
    except (HarnessError, OSError, ValueError) as error:
        receipt["status"], receipt["reason"] = "BLOCKED", str(error)
    receipt["catalog_digest"] = digest([{k: row[k] for k in ("id", "catalog") if k in row} for row in receipt["servers"]])
    if receipt["status"] == "BLOCKED" and profile["availability"] == "optional":
        receipt["status"] = "NOT_AVAILABLE"
    return receipt
