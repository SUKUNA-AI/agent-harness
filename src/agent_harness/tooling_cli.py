"""Tool profiles in the existing CLI; diagnostics never install or authenticate."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .core import HarnessError, digest
from .strict_yaml import dumps
from .supervisor_store import atomic_write


def _output_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path).startswith("//"):
        raise HarnessError("Output requires an unambiguous absolute path without parent traversal")
    from .supervisor_store import _parents
    _parents(path)
    return path


def _opencode_catalog(config, root, role, evidence_dir, profile):
    from .execution import require_native_dependencies
    from .opencode_execution import OpenCodeAdapter
    adapter = OpenCodeAdapter().with_tool_config(config, root=root)
    adapter = adapter.qualify(root=root, role=role, evidence_dir=evidence_dir, timeout=60)
    adapter.verify_qualification(root=root, role=role)
    require_native_dependencies(profile, adapter, adapter.capabilities())
    return {"status": "READY", "client_catalog_verified": True,
        "catalog": adapter.effective_tool_catalog(), "capabilities": adapter.capabilities(),
        "qualification_ref": adapter.qualification_receipt(), "model_calls": 0,
        "qualified_for_exec": False, "reusable_as_authorization": False,
        "scope": "native rehearsal; supervisor requalifies and guards each real invocation"}


def _codex_catalog(config, root, role, evidence_dir, profile):
    from .codex_execution import CodexAdapter
    from .execution import require_native_dependencies
    adapter = CodexAdapter().with_tool_config(config, root=root)
    managed = getattr(adapter, "with_managed_tools", None)
    if not callable(managed):
        raise HarnessError("BLOCKED_NOT_QUALIFIED: installed Codex adapter has no managed native factory")
    adapter = managed(root=root).qualify(root=root, role=role, evidence_dir=evidence_dir, timeout=60)
    adapter.verify_qualification(root=root, role=role)
    capabilities = adapter.capabilities()
    require_native_dependencies(profile, adapter, capabilities)
    return {"status": "READY", "client_catalog_verified": True,
        "catalog": adapter.effective_tool_catalog(), "capabilities": capabilities,
        "qualification_ref": adapter.qualification_receipt(), "model_calls": 0,
        "qualified_for_exec": False, "reusable_as_authorization": False,
        "scope": "native rehearsal; supervisor requalifies and guards each real invocation"}


def dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Built-in optional profile identifiers")
    command = sub.add_parser("browser-job", help="Run finite managed Node browser actions and independent regression")
    command.add_argument("--profile", default="browser-testing", help="Configured browser profile ID or absolute Strict YAML path")
    command.add_argument("--job", required=True, help="BrowserJob JSON object or absolute ordinary JSON file")
    command = sub.add_parser("graphify-build", help="Build a local-only Graphify index with pinned native implementation")
    command.add_argument("--root", required=True)
    command.add_argument("--artifact-dir", required=True)
    command.add_argument("--executable", default="graphify")
    command.add_argument("--snapshot", help="Optional absolute frozen source manifest")
    command.add_argument("--timeout", type=float, default=120)
    command = sub.add_parser("snapshot", help="Freeze actual source manifest for a graph build")
    command.add_argument("--root", required=True)
    command.add_argument("--output", required=True)
    command = sub.add_parser("index-build", help="Build content-addressed Graphify artifact from frozen bytes")
    for option in ("root", "snapshot", "artifact-dir", "command-json", "tool-version"):
        command.add_argument("--" + option, required=True)
    command.add_argument("--version-argv-json")
    command.add_argument("--settings-json", default="{}")
    command.add_argument("--timeout", type=float, default=120)
    command = sub.add_parser("index-verify", help="Verify graph/source/tool identity without rebuilding")
    command.add_argument("--root", required=True)
    command.add_argument("--receipt", required=True)
    command.add_argument("--historical", action="store_true")
    for name in ("show", "dry-run", "check", "catalog", "smoke", "qualify", "apply"):
        command = sub.add_parser(name)
        command.add_argument("--profile", default="baseline", help="Built-in ID or absolute Strict YAML path")
        command.add_argument("--backend", choices=("codex", "opencode"), default="codex")
        command.add_argument("--root", required=True)
        command.add_argument("--role", default="owner", choices=("owner", "architect", "reviewer", "helper"))
        command.add_argument("--graph-index", help="Ordinary YAML graph receipt, verified against current root")
        if name == "qualify":
            command.add_argument("--evidence-dir", required=True, help="Existing private directory outside source/user config")
        if name == "apply":
            command.add_argument("--output", required=True, help="New private per-run config file; never a user/project config")
            command.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args(argv)
    from .tool_profiles import builtin_profiles, execution_profile, load_profile, profile_digest, preflight_profile, render_codex, render_opencode
    try:
        if args.command == "browser-job":
            from dataclasses import fields
            from .browser_integration import run_managed_browser_job
            from .codex_execution import _duplicate_free
            from .file_io import read_regular_file
            from .optional_tool_jobs import BrowserJob
            from .tool_profiles import _browser_policy
            profile = load_profile(Path(args.profile) if Path(args.profile).is_absolute() else args.profile)
            if not profile.get("browser"):
                raise HarnessError("Configured browser policy is required for browser-job")
            if args.job.lstrip().startswith("{"):
                if len(args.job.encode()) > 65536:
                    raise HarnessError("Browser job JSON exceeds byte bound")
                raw = args.job
            else:
                raw = read_regular_file(_output_path(args.job), max_bytes=65536)
            value = json.loads(raw, object_pairs_hook=_duplicate_free)
            if not isinstance(value, dict) or set(value) - {field.name for field in fields(BrowserJob)}:
                raise HarnessError("Browser job must be a closed BrowserJob JSON object")
            try:
                job = BrowserJob(**value)
            except TypeError as error:
                raise HarnessError("Browser job is missing required fields") from error
            result = run_managed_browser_job(_browser_policy(profile), job)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["status"] in {"COMPLETED", "REPRODUCED"} else 2
        if args.command == "graphify-build":
            from .local_tool_integrations import build_local_graph_index
            from .strict_yaml import load
            result = build_local_graph_index(_output_path(args.root), _output_path(args.artifact_dir),
                executable=args.executable, frozen=load(_output_path(args.snapshot)) if args.snapshot else None,
                timeout=args.timeout)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command in {"snapshot", "index-build", "index-verify"}:
            from .tooling_provenance import source_snapshot, build_graph_index, verify_graph_index
            from .strict_yaml import load
            root = Path(args.root)
            if not root.is_absolute() or not root.is_dir():
                raise HarnessError("Graph source root must be absolute")
            if args.command == "snapshot":
                manifest = source_snapshot(root)
                target = _output_path(args.output)
                if target == root or root in target.parents:
                    raise HarnessError("Frozen manifest must live outside the source")
                atomic_write(target, dumps(manifest).encode("utf-8"), exclusive=True)
                result = {"status": "FROZEN", "source_snapshot_digest": manifest["digest"], "path": str(target)}
            elif args.command == "index-build":
                result = build_graph_index(root, load(args.snapshot), Path(args.artifact_dir),
                    json.loads(args.command_json), tool_version=args.tool_version, settings=json.loads(args.settings_json),
                    version_argv=json.loads(args.version_argv_json) if args.version_argv_json else None, timeout=args.timeout)
            else:
                result = verify_graph_index(load(args.receipt), source_snapshot(root), required=not args.historical)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "list":
            result = {"profiles": list(builtin_profiles()),
                      "default_backend": "codex", "default_profile": "baseline"}
        else:
            root = Path(args.root)
            if not root.is_absolute() or not root.is_dir():
                raise HarnessError("--root must be an existing absolute project/worktree directory")
            root = root.resolve()
            profile = load_profile(Path(args.profile) if Path(args.profile).is_absolute() else args.profile)
            role = {"owner": "worker", "helper": "worker", "architect": "planner"}.get(args.role, args.role)
            native = (render_codex if args.backend == "codex" else render_opencode)(profile, root, role=role)
            result = {"profile": profile, "profile_digest": profile_digest(profile), "backend": args.backend,
                "native_config": native, "effective_config_digest": digest(native), "applied": False,
                "client_catalog_verified": False}
            if args.command in {"check", "catalog", "smoke", "qualify"}:
                from .strict_yaml import load
                graph = load(args.graph_index) if args.graph_index else None
                result["readiness"] = preflight_profile(profile, args.backend, root, role, graph_index=graph)
                runtime_profile = profile
                if result["readiness"]["status"] == "READY":
                    from .execution import available_profile
                    runtime_profile = available_profile(execution_profile(profile, root), result["readiness"])
                    native = (render_codex if args.backend == "codex" else render_opencode)(runtime_profile, root, role=role)
                    result.update(native_config=native, effective_config_digest=digest(native))
            if args.command == "catalog" and args.backend == "codex":
                if result["readiness"]["status"] != "READY":
                    result["client_catalog"] = {"status": "BLOCKED", "client_catalog_verified": False,
                        "reason": "Tool dependencies are not ready for native catalog diagnostics"}
                else:
                    from .codex_execution import CodexAdapter
                    from .codex_tool_catalog import audit_catalog
                    result["client_catalog"] = audit_catalog(CodexAdapter(), native, root)
                result["client_catalog_verified"] = result["client_catalog"]["client_catalog_verified"]
            elif args.command in {"catalog", "qualify"}:
                if result["readiness"]["status"] != "READY":
                    result["client_catalog"] = {"status": "BLOCKED", "client_catalog_verified": False,
                        "reason": "Tool dependencies are not ready for native qualification"}
                elif args.command == "qualify":
                    evidence = _output_path(args.evidence_dir)
                    codex = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
                    opencode = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).resolve() / "opencode"
                    if (not evidence.is_dir() or evidence.is_symlink()
                            or any(evidence == p or p in evidence.parents for p in (root, codex, opencode))):
                        raise HarnessError("Qualification requires an existing private directory outside source/user config")
                    qualifier = _codex_catalog if args.backend == "codex" else _opencode_catalog
                    result["client_catalog"] = qualifier(native, root, role, evidence, runtime_profile)
                else:
                    import tempfile
                    with tempfile.TemporaryDirectory(prefix="harness-opencode-catalog-") as directory:
                        result["client_catalog"] = _opencode_catalog(native, root, role, Path(directory), runtime_profile)
                    result["client_catalog"]["artifact_lifetime"] = "temporary_diagnostic_only"
                result["client_catalog_verified"] = result["client_catalog"]["client_catalog_verified"]
            if args.command == "smoke":
                try:
                    if args.backend == "codex":
                        from .codex_execution import CodexAdapter
                        adapter = CodexAdapter()
                    else:
                        from .opencode_execution import OpenCodeAdapter
                        adapter = OpenCodeAdapter()
                    result["backend_capabilities"] = adapter.capabilities()
                    if args.backend == "opencode":
                        result["standalone_probe"] = adapter.live_smoke(root=root, config=native)
                except HarnessError as error:
                    result["backend_capabilities"] = {"status": "unavailable", "reason": str(error)}
                result["live_execution"] = "BLOCKED_NOT_QUALIFIED"
                result["reason"] = "Help/config/dependency probes are not live model/session/permissions qualification"
            if args.command == "apply":
                target = _output_path(args.output)
                codex = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
                opencode = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).resolve() / "opencode"
                if (target.exists() or target.is_symlink() or root == target or root in target.parents
                        or codex == target or codex in target.parents or opencode == target or opencode in target.parents):
                    raise HarnessError("Apply writes only a NEW private per-run file outside source/user config roots")
                if not target.parent.is_dir():
                    raise HarnessError("Create the private output directory explicitly before apply")
                if args.backend == "codex":
                    from .codex_execution import _toml_literal
                    content = "\n".join(key + "=" + _toml_literal(value) for key, value in native.items()) + "\n"
                else:
                    content = json.dumps(native, ensure_ascii=False, indent=2) + "\n"
                atomic_write(target, content.encode("utf-8"), exclusive=True)
                result.update(applied=True, output=str(target))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.command == "smoke":
            return 2
        return 2 if (result.get("readiness", {}).get("status") == "BLOCKED"
                     or result.get("client_catalog", {}).get("status") == "BLOCKED") else 0
    except (HarnessError, OSError, ValueError) as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}, ensure_ascii=False))
        return 2
