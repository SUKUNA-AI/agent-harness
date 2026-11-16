"""Tooling contract, real subprocess integration and security negative tests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, digest, identity_alive
from agent_harness.strict_yaml import StrictYAMLError, dumps, loads
from agent_harness.tool_profiles import (builtin_profiles, load_profile, preflight_profile,
    profile_digest, render_codex, render_opencode, resolved_servers)
from agent_harness.tooling_probe import ProbeFailure, StdioProbe, probe_server
from agent_harness.tooling_provenance import ast_grep_migrate, build_graph_index, source_snapshot, verify_graph_index

FIXTURES = Path(__file__).resolve().parent / "tooling"


class ToolProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "symbol.py").write_text("def shared_symbol(): return 'A'\n")

    def profile(self, mode="ready", *, reviewer=False):
        tools = ["search_symbol", "read_file"]
        return load_profile({"schema_version": 1, "id": "fixture-tools", "version": 1,
            "servers": [{"id": "fixture", "transport": "stdio", "required": True,
                "command": [sys.executable, str(FIXTURES / "fake_mcp.py"), mode, "{worktree}"],
                "allowed_tools": tools, "read_only_tools": tools, "required_tools": ["search_symbol"],
                "forbidden_tools": ["write_file", "execute_command"],
                "startup_timeout": 2, "tool_timeout": 2}]})

    def test_closed_schema_reference_and_defaults(self):
        for name in builtin_profiles():
            profile = load_profile(name)
            self.assertEqual(profile_digest(profile), profile_digest(loads(dumps(profile))))
        baseline = load_profile({"id": "baseline", "version": 1})
        self.assertEqual(baseline["servers"], [])
        with self.assertRaises(HarnessError):
            load_profile({"id": "baseline", "version": 2})
        with self.assertRaises(StrictYAMLError):
            load_profile({"schema_version": 1, "id": "bad", "version": 1, "arbitrary": True})
        with self.assertRaises(HarnessError):
            load_profile({"id": "baseline", "version": True})

    def test_mandatory_policy_conflict_and_secret_literals_rejected(self):
        profile = self.profile()
        profile["servers"][0]["required_tools"].append("write_file")
        with self.assertRaises(HarnessError):
            load_profile(profile)
        profile = self.profile()
        profile["servers"][0]["environment"] = {"API_KEY": "fixture-secret-value"}
        with self.assertRaises(HarnessError):
            load_profile(profile)
        profile = self.profile()
        profile["servers"].append(deepcopy(profile["servers"][0]))
        with self.assertRaises((HarnessError, StrictYAMLError)):
            load_profile(profile)

    def test_credential_literal_names_and_flags_are_rejected_without_echo(self):
        for name in ("SERVICE_TOKEN", "client_secret", "DB_PASSWORD", "Service_Credential",
                "vendor_api_key", "AUTHORIZATION", "api-key", "PASSWD"):
            with self.subTest(environment=name):
                profile = self.profile()
                profile["servers"][0]["environment"] = {name: "fixture-private-value"}
                with self.assertRaises(HarnessError) as error:
                    load_profile(profile)
                self.assertNotIn("fixture-private-value", str(error.exception))
        for flag in ("--password", "--token=fixture-private-value", "--api-key",
                "--client_secret=fixture-private-value", "--github-token", "--Authorization"):
            for field in ("command", "help_argv"):
                with self.subTest(flag=flag, field=field):
                    profile = self.profile()
                    profile["servers"][0][field] = [sys.executable, flag, "fixture-private-value"]
                    with self.assertRaises(HarnessError) as error:
                        load_profile(profile)
                    self.assertNotIn("fixture-private-value", str(error.exception))
        allowed = self.profile()
        allowed["servers"][0]["env_vars"] = ["SERVICE_TOKEN", "API_KEY"]
        self.assertEqual(load_profile(allowed)["servers"][0]["env_vars"], ["SERVICE_TOKEN", "API_KEY"])

    def test_native_renderers_bind_root_and_live_readiness_without_global_writes(self):
        before = sorted(self.root.rglob("*"))
        codex = render_codex(self.profile(), self.source, cache_root=self.root / "cache")
        self.assertEqual(codex["mcp_servers"]["fixture"]["cwd"], str(self.source))
        self.assertEqual(codex["mcp_servers"]["fixture"]["startup_readiness"], "connection")
        opencode = render_opencode(self.profile(), self.source, role="reviewer")
        server = opencode["mcp"]["servers"]["fixture"]
        self.assertFalse(server["pool"])
        self.assertFalse(server["codemode"])
        self.assertEqual(server["cwd"], str(self.source))
        rules = opencode["agents"]["harness-leaf"]["permissions"]
        self.assertIn({"action": "subagent", "resource": "*", "effect": "deny"}, rules)
        self.assertIn({"action": "shell", "resource": "*", "effect": "deny"}, rules)
        self.assertEqual(before, sorted(self.root.rglob("*")))
        self.assertNotIn("model", opencode)
        self.assertNotIn("approval_policy", codex)

    def test_render_nested_uncreated_cache_is_pure_and_root_isolated(self):
        cache = self.root / "private" / "tools" / "node"
        first = resolved_servers(self.profile(), self.source, cache_root=cache)
        self.assertEqual(first[0]["cwd"], str(self.source))
        self.assertFalse(cache.exists())
        serena = load_profile("coding")
        original = resolved_servers(serena, self.source, cache_root=cache)[0]["environment"]["SERENA_HOME"]
        other = self.root / "other"
        other.mkdir()
        distinct = resolved_servers(serena, other, cache_root=cache)[0]["environment"]["SERENA_HOME"]
        self.assertNotEqual(original, distinct)
        self.assertFalse(cache.exists())

    def test_baseline_without_optional_executables_is_ready(self):
        for backend in ("codex", "opencode"):
            result = preflight_profile("baseline", backend, self.source)
            self.assertEqual(result["status"], "READY", result)
            self.assertEqual(result["servers"], [])
            self.assertFalse(result["client_catalog_verified"])
            self.assertEqual(result["acceptance"], "not_performed")

    def test_A1_parallel_live_stdio_servers_have_separate_worktree_results(self):
        other = self.root / "other"
        other.mkdir()
        (other / "symbol.py").write_text("def shared_symbol(): return 'B'\n")
        profile = self.profile()
        def worker(root):
            spec = resolved_servers(profile, root)[0]
            with StdioProbe(spec) as client:
                client.inventory()
                result = client.call_readonly("search_symbol", {"symbol": "shared_symbol"})
                identity = client.identity
            self.assertTrue(client.stopped)
            self.assertFalse(identity_alive(identity))
            return json.loads(result["content"][0]["text"]), client.events
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(worker, (self.source, other)))
        self.assertNotEqual(first[0]["pid"], second[0]["pid"])
        for result, source, value in ((first, self.source, "A"), (second, other, "B")):
            self.assertEqual(result[0]["cwd"], str(source))
            self.assertEqual(result[0]["project"], str(source))
            self.assertIn("return '" + value + "'", result[0]["content"])
            self.assertEqual(result[1][-1]["event"], "process.stopped")
        self.assertNotIn("return 'B'", first[0]["content"])
        self.assertNotIn("return 'A'", second[0]["content"])
        self.assertEqual(sorted(p.name for p in self.source.iterdir()), ["symbol.py"])

    def test_A3_diagnostic_catalog_projection_and_negative_calls_are_enforced(self):
        spec = resolved_servers(self.profile(), self.source)[0]
        with StdioProbe(spec, role="reviewer") as client:
            catalog = client.inventory()
            self.assertEqual({tool["name"] for tool in catalog}, {"search_symbol", "read_file"})
            for tool in ("write_file", "execute_command"):
                with self.assertRaisesRegex(ProbeFailure, "TOOL_FORBIDDEN_BY_POLICY"):
                    client.call_readonly(tool, {"path": "forbidden.txt"})
        self.assertFalse((self.source / "forbidden.txt").exists())
        self.assertFalse((self.source / "shell-forbidden.txt").exists())
        # This proves the diagnostic boundary only; no backend claim is inferred.
        receipt = preflight_profile(self.profile(), "codex", self.source, "reviewer")
        self.assertEqual(receipt["status"], "READY")
        self.assertFalse(receipt["client_catalog_verified"])

    def test_large_diagnostic_request_is_rejected_before_blocking_pipe_write(self):
        spec = resolved_servers(self.profile(), self.source)[0]
        with StdioProbe(spec) as client:
            client.inventory()
            with self.assertRaisesRegex(ProbeFailure, "REQUEST_LIMIT"):
                client.call_readonly("search_symbol", {"symbol": "x" * 8192})
        self.assertTrue(client.stopped)

    def test_A5_required_mcp_failures_never_become_ready(self):
        for mode, reason in (("startup-failure", "STARTUP_FAILED"), ("handshake-failure", "HANDSHAKE_FAILED"),
                ("missing-tool", "MISSING_TOOL"), ("timeout", "TIMEOUT"), ("disconnect", "DISCONNECTED"),
                ("ping-timeout", "TIMEOUT")):
            with self.subTest(mode=mode):
                profile = self.profile(mode)
                profile["servers"][0]["startup_timeout"] = 0.25
                result = preflight_profile(profile, "codex", self.source)
                self.assertEqual(result["status"], "BLOCKED", result)
                self.assertEqual(result["servers"][0]["reason"], reason, result)
                self.assertTrue(result["servers"][0]["process_tree_stopped"])
                self.assertNotEqual(result.get("acceptance"), "accepted")

    def test_delayed_live_connection_is_observed_and_closed(self):
        result = preflight_profile(self.profile("delayed"), "codex", self.source)
        self.assertEqual(result["status"], "READY", result)
        server = result["servers"][0]
        self.assertTrue(server["live_connection"])
        self.assertTrue(server["process_tree_stopped"])
        self.assertFalse(identity_alive(server["process"]))

    def test_optional_failure_is_explicit_and_missing_executable_classified(self):
        profile = self.profile()
        profile["servers"][0]["command"][0] = "harness-tool-that-does-not-exist"
        result = preflight_profile(profile, "codex", self.source)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["servers"][0]["reason"], "SERVER_MISSING")
        profile["servers"][0]["required"] = False
        result = preflight_profile(profile, "codex", self.source)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["servers"][0]["status"], "BLOCKED")
        profile["availability"] = "optional"
        profile["servers"][0]["required"] = True
        self.assertEqual(preflight_profile(profile, "codex", self.source)["status"], "NOT_AVAILABLE")

    def test_reviewer_rejects_unclassified_tools_even_if_catalog_annotation_lies(self):
        profile = self.profile()
        profile["servers"][0]["allowed_tools"].append("write_file")
        profile["servers"][0]["forbidden_tools"].remove("write_file")
        result = preflight_profile(profile, "opencode", self.source, "reviewer")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["servers"], [])
        with self.assertRaises(HarnessError):
            render_codex(profile, self.source, role="reviewer")

    def test_A2_graph_index_is_bound_to_frozen_dirty_source_and_invalidates(self):
        frozen = source_snapshot(self.source)
        fixture = FIXTURES / "fake_graphify.py"
        receipt = build_graph_index(self.source, frozen, self.root / "artifacts",
            [sys.executable, str(fixture), "{source_root}", "{index_output}"], tool_version="fixture-graphify 1",
            version_argv=[sys.executable, str(fixture), "--version"], settings={"language": "python"})
        self.assertEqual(receipt["source_snapshot_digest"], frozen["digest"])
        self.assertEqual(verify_graph_index(receipt, frozen)["status"], "CURRENT")
        # Independent check reads actual source and Python AST, not the graph.
        import ast
        module = ast.parse((self.source / "symbol.py").read_text())
        self.assertEqual(module.body[0].name, "shared_symbol")
        (self.source / "symbol.py").write_text("def shared_symbol(): return 'modified'\n")
        current = source_snapshot(self.source)
        self.assertNotEqual(frozen["digest"], current["digest"])
        with self.assertRaisesRegex(HarnessError, "STALE_GRAPH_INDEX"):
            verify_graph_index(receipt, current)
        historical = verify_graph_index(receipt, current, required=False)
        self.assertEqual(historical["status"], "HISTORICAL_ONLY")
        with self.assertRaisesRegex(HarnessError, "STALE_GRAPH_SOURCE"):
            build_graph_index(self.source, frozen, self.root / "other-artifacts",
                [sys.executable, str(fixture), "{source_root}", "{index_output}"], tool_version="fixture-graphify 1")
        Path(receipt["index_path"]).write_text("tampered")
        with self.assertRaisesRegex(HarnessError, "index_bytes_drift"):
            verify_graph_index(receipt, current)

    def test_ast_grep_migration_requires_scans_scope_and_real_regression(self):
        (self.source / "calc.py").write_text("result = old_api(41)\n")
        result = ast_grep_migrate(self.source, ["calc.py"],
            pattern="old_api($A)", rewrite="new_api($A)", language="python",
            executable=[sys.executable, str(FIXTURES / "fake_ast_grep.py")],
            checks=[[sys.executable, "-c", "from pathlib import Path; scope={'new_api':lambda value:value+1}; exec(Path('calc.py').read_text(),scope); assert scope['result']==42"]])
        self.assertEqual(result["status"], "verified_migration")
        self.assertEqual(result["initial_scan"]["count"], 1)
        self.assertEqual(result["old_pattern_scan"]["count"], 0)
        self.assertEqual(result["new_pattern_scan"]["count"], 1)
        self.assertEqual(result["changed_paths"], ["calc.py"])
        self.assertEqual(result["tests"][0]["exit_code"], 0)
        self.assertIn("fixture", result["tool"]["version"])
        self.assertNotIn("accepted", result["acceptance"])

    def test_ast_grep_success_cannot_hide_failed_independent_test(self):
        (self.source / "calc.py").write_text("result = old_api(41)\n")
        with self.assertRaisesRegex(HarnessError, "regression test failed"):
            ast_grep_migrate(self.source, ["calc.py"], pattern="old_api($A)",
                rewrite="new_api($A)", language="python",
                executable=[sys.executable, str(FIXTURES / "fake_ast_grep.py")],
                checks=[[sys.executable, "-c", "raise SystemExit(3)"]])
        self.assertIn("new_api", (self.source / "calc.py").read_text())

    def test_graph_old_manifest_does_not_bypass_current_source_verification(self):
        frozen = source_snapshot(self.source)
        fixture = FIXTURES / "fake_graphify.py"
        receipt = build_graph_index(self.source, frozen, self.root / "artifacts",
            [sys.executable, str(fixture), "{source_root}", "{index_output}"],
            tool_version="fixture-graphify 1", version_argv=[sys.executable, str(fixture), "--version"])
        self.assertTrue(Path(receipt["frozen_source_path"]).is_dir())
        (self.source / "symbol.py").write_text("CHANGED = True\n")
        with self.assertRaisesRegex(HarnessError, "current_source_drift"):
            verify_graph_index(receipt, frozen)
        self.assertEqual(verify_graph_index(receipt, frozen, required=False)["status"], "HISTORICAL_ONLY")

    def test_documents_modes_cannot_bypass_unimplemented_controlled_runner(self):
        profile = load_profile("documents")
        profile["cli"] = []
        for mode in ("local-only", "remote"):
            with self.subTest(mode=mode):
                profile["documents"]["mode"] = mode
                result = preflight_profile(profile, "codex", self.source)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["requested_conversion_mode"], mode)
                self.assertEqual(result["actual_conversion_mode"], "not_run")
                self.assertIn("controlled conversion runner", result["reason"])

    def test_required_graph_readiness_blocks_before_process_without_current_binding(self):
        profile = self.profile()
        profile["index"] = {"required": True, "policy": "frozen_source", "provider": "graphify"}
        missing = preflight_profile(profile, "codex", self.source)
        self.assertEqual(missing["status"], "BLOCKED")
        self.assertIn("REQUIRED_GRAPH_INDEX_MISSING", missing["reason"])
        self.assertEqual(missing["servers"], [])
        frozen = source_snapshot(self.source)
        fixture = FIXTURES / "fake_graphify.py"
        graph = build_graph_index(self.source, frozen, self.root / "artifacts",
            [sys.executable, str(fixture), "{source_root}", "{index_output}"],
            tool_version="fixture-graphify 1", version_argv=[sys.executable, str(fixture), "--version"])
        current = preflight_profile(profile, "codex", self.source, graph_index=graph)
        self.assertEqual(current["status"], "READY", current)
        self.assertEqual(current["graph_index"]["identity"], graph["identity"])
        (self.source / "symbol.py").write_text("CHANGED = True\n")
        stale = preflight_profile(profile, "codex", self.source, graph_index=graph)
        self.assertEqual(stale["status"], "BLOCKED")
        self.assertEqual(stale["servers"], [])
        self.assertIn("STALE_GRAPH_INDEX", stale["reason"])

    def test_graph_output_traversal_is_blocked_before_any_source_mutation(self):
        outside = self.root / "outside"
        outside.mkdir()
        destination = outside / ".." / "source" / "forbidden-index"
        fixture = FIXTURES / "fake_graphify.py"
        frozen = source_snapshot(self.source)
        with self.assertRaises(HarnessError):
            build_graph_index(self.source, frozen, destination,
                [sys.executable, str(fixture), "{source_root}", "{index_output}"],
                tool_version="fixture-graphify 1", version_argv=[sys.executable, str(fixture), "--version"])
        self.assertFalse((self.source / "forbidden-index").exists())
        self.assertEqual(source_snapshot(self.source)["digest"], frozen["digest"])

    @unittest.skipIf(os.name == "nt", "POSIX symlink creation; Windows reparse checks share _ordinary")
    def test_graph_output_linked_ancestor_is_blocked_before_source_mutation(self):
        alias = self.root / "linked-artifacts"
        alias.symlink_to(self.source, target_is_directory=True)
        frozen = source_snapshot(self.source)
        with self.assertRaises(HarnessError):
            build_graph_index(self.source, frozen, alias / "forbidden-index",
                [sys.executable, "{source_root}", "{index_output}"], tool_version="fixture")
        self.assertFalse((self.source / "forbidden-index").exists())
        self.assertEqual(source_snapshot(self.source)["digest"], frozen["digest"])

    def test_graph_artifacts_cannot_pollute_source(self):
        with self.assertRaises(HarnessError):
            build_graph_index(self.source, source_snapshot(self.source), self.source / "index",
                [sys.executable, "{source_root}", "{index_output}"], tool_version="fixture")


if __name__ == "__main__":
    unittest.main()
