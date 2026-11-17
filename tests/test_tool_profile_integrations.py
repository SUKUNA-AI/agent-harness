"""Wiring for optional browser/native tool profiles without package installation."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.browser_integration import TOOLS
from agent_harness.core import HarnessError
from agent_harness.strict_yaml import dumps
from agent_harness.tool_profiles import (builtin_profiles, execution_profile, load_profile,
    preflight_profile, profile_digest, render_codex, render_opencode, resolved_servers)
from agent_harness.tooling_cli import dispatch


class ToolProfileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.private = self.base / "private"
        self.private.mkdir()

    def profile(self):
        result = builtin_profiles()["browser-testing"]
        result["browser"].update(allowed_origins=["http://127.0.0.1:8123"],
            mcp_package=str(self.private / "cli.js"), browser_executable=str(self.private / "browser"),
            browser_cache=str(self.private))
        return result

    def ready(self, server, **_):
        return {"id": server["id"], "status": "READY", "catalog": [
            {"name": name, "inputSchema": {"type": "object"}} for name in server["allowed_tools"]]}

    def test_baseline_and_profile_inventory_never_resolve_optional_executables(self):
        with patch("agent_harness.local_tool_integrations.serena_stdio_argv", side_effect=AssertionError("no Serena")), \
                patch("agent_harness.local_tool_integrations.context7_stdio_argv", side_effect=AssertionError("no Context7")), \
                patch("agent_harness.tool_profiles.inspect_cli", side_effect=AssertionError("no CLI")):
            self.assertEqual(preflight_profile("baseline", "codex", self.source)["status"], "READY")
            self.assertEqual(render_codex("baseline", self.source), {"mcp_servers": {}})
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(dispatch(["list"]), 0)
            self.assertEqual(set(json.loads(output.getvalue())["profiles"]), set(builtin_profiles()))

    def test_minimal_browser_requires_configuration_without_playwright_cli_probe(self):
        with patch("agent_harness.tool_profiles.inspect_cli", side_effect=AssertionError("not CLI presence")):
            result = preflight_profile("browser-testing", "codex", self.source)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("CONFIG_REQUIRED", result["reason"])
        self.assertEqual(load_profile("browser-testing")["cli"], [])

    def test_configured_browser_has_actual_lifecycle_and_dynamic_required_dependency(self):
        profile = self.profile()
        with patch("agent_harness.browser_integration.qualify_browser", return_value={"status": "QUALIFIED"}) as actual, \
                patch("agent_harness.tooling_probe.probe_server", side_effect=self.ready):
            result = preflight_profile(profile, "codex", self.source, cache_root=self.private)
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual(result["required_dependencies"], ["playwright-managed"])
        self.assertEqual({tool["name"] for tool in result["servers"][0]["catalog"]}, set(TOOLS))
        actual.assert_called_once()
        self.assertEqual(actual.call_args.kwargs["root"], self.source)
        with patch("agent_harness.browser_integration.qualify_browser", return_value={"status": "BLOCKED_NOT_QUALIFIED", "reason": "browser unavailable"}), \
                patch("agent_harness.tooling_probe.probe_server", side_effect=self.ready):
            result = preflight_profile(profile, "codex", self.source, cache_root=self.private)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("browser unavailable", result["reason"])

    def test_execution_profile_is_schema_valid_idempotent_and_keeps_source_digest(self):
        source = self.profile()
        identity = profile_digest(source)
        runtime = execution_profile(source, self.source, cache_root=self.private)
        self.assertNotIn("browser", runtime)
        self.assertNotIn("cwd", runtime["servers"][0])
        server = runtime["servers"][0]
        self.assertEqual(set(server["required_tools"]), set(TOOLS))
        self.assertEqual(server["required_results"], ["browser_snapshot"])
        self.assertEqual(render_codex(runtime, self.source, cache_root=self.private),
            render_codex(source, self.source, cache_root=self.private))
        self.assertEqual(render_opencode(runtime, self.source, cache_root=self.private),
            render_opencode(source, self.source, cache_root=self.private))
        self.assertEqual(profile_digest(source), identity)
        self.assertEqual(source, self.profile())
        forged = deepcopy(runtime)
        forged["servers"][0]["command"] = [sys.executable, "-c", "print('forged')"]
        with self.assertRaisesRegex(HarnessError, "Reserved browser"):
            resolved_servers(forged, self.source, cache_root=self.private)

    def test_required_results_is_closed_subset_without_new_legacy_default(self):
        old = builtin_profiles()["coding"]
        self.assertNotIn("required_results", load_profile(old)["servers"][0])
        bad = deepcopy(old)
        bad["servers"][0]["required_results"] = ["write_file"]
        with self.assertRaisesRegex(HarnessError, "subset"):
            load_profile(bad)
        bad["servers"][0]["required_results"] = []
        self.assertEqual(load_profile(bad)["servers"][0]["required_results"], [])

    def test_serena_launch_is_lazy_worktree_bound_and_repeatable(self):
        from agent_harness import local_tool_integrations as local
        def launcher(root, cache, executable="serena"):
            return [sys.executable, str(Path(local.__file__).resolve()), "serena-stdio", "--executable", "/fixture/serena",
                "--project", str(root), "--cache-root", str(cache)]
        with patch.object(local, "serena_stdio_argv", side_effect=launcher) as selected:
            profile = execution_profile("coding", self.source, cache_root=self.private)
            first = resolved_servers(profile, self.source, cache_root=self.private)[0]
            self.assertEqual(first["command"], profile["servers"][0]["command"])
            self.assertIn(str(self.source), first["command"])
            self.assertNotIn("help_argv", first)
            self.assertTrue(selected.called)
        malformed = builtin_profiles()["coding"]
        malformed["servers"][0]["command"] = [sys.executable, str(Path(local.__file__).resolve()), "serena-stdio", "--executable"]
        with self.assertRaisesRegex(HarnessError, "executable binding"):
            resolved_servers(malformed, self.source, cache_root=self.private)

    def test_context7_requires_explicit_named_profile_and_uses_clean_launcher(self):
        builtins = builtin_profiles()
        for name in ("coding", "architecture"):
            self.assertNotIn("context7", [server["id"] for server in builtins[name]["servers"]])
            self.assertIn("context7", [server["id"] for server in builtins[name + "-context7"]["servers"]])
        with patch("agent_harness.local_tool_integrations.context7_stdio_argv", return_value=[sys.executable, "private-context7"]):
            profile = deepcopy(builtins["coding-context7"])
            profile["servers"] = [server for server in profile["servers"] if server["id"] == "context7"]
            server = resolved_servers(profile, self.source, cache_root=self.private)[0]
        self.assertEqual(server["command"], [sys.executable, "private-context7"])
        self.assertFalse(server["required"])

    def test_browser_job_cli_passes_closed_dataclass_to_managed_node_driver(self):
        profile = self.private / "browser.yaml"
        profile.write_text(dumps(self.profile()))
        value = {"root": str(self.source), "artifact_root": str(self.private), "url": "http://127.0.0.1:8123/",
            "actions": [{"kind": "expect_text", "selector": "#result", "value": "fixed"}],
            "regression_argv": [sys.executable, "-c", "pass"]}
        job = self.private / "job.json"
        job.write_text(json.dumps(value))
        with patch("agent_harness.browser_integration.run_managed_browser_job", return_value={"status": "COMPLETED"}) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch(["browser-job", "--profile", str(profile), "--job", str(job)]), 0)
        self.assertEqual(run.call_args.args[1].root, value["root"])
        value["evaluate"] = "forbidden"
        with patch("agent_harness.browser_integration.run_managed_browser_job", side_effect=AssertionError("no process")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch(["browser-job", "--profile", str(profile), "--job", json.dumps(value)]), 2)

    def test_graphify_cli_calls_local_only_provenance_builder(self):
        with patch("agent_harness.local_tool_integrations.build_local_graph_index", return_value={"status": "BUILT"}) as build, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch(["graphify-build", "--root", str(self.source), "--artifact-dir", str(self.private),
                "--executable", "/fixture/graphify", "--timeout", "42"]), 0)
        self.assertEqual(build.call_args.args, (self.source, self.private))
        self.assertEqual(build.call_args.kwargs, {"executable": "/fixture/graphify", "frozen": None, "timeout": 42})

    def test_native_qualify_cli_retains_dynamic_browser_dependencies_for_both_backends(self):
        profile = self.private / "browser.yaml"
        profile.write_text(dumps(self.profile()))
        def qualify(config, root, role, evidence, runtime):
            self.assertNotIn("browser", runtime)
            self.assertEqual(len(runtime["servers"]), 1)
            self.assertEqual(runtime["servers"][0]["id"], "playwright-managed")
            self.assertEqual(set(runtime["servers"][0]["required_tools"]), set(TOOLS))
            self.assertEqual(runtime["servers"][0]["required_results"], ["browser_snapshot"])
            return {"status": "READY", "client_catalog_verified": True}
        with patch("agent_harness.browser_integration.qualify_browser", return_value={"status": "QUALIFIED"}), \
                patch("agent_harness.tooling_probe.probe_server", side_effect=self.ready), \
                patch("agent_harness.tooling_cli._codex_catalog", side_effect=qualify) as codex, \
                patch("agent_harness.tooling_cli._opencode_catalog", side_effect=qualify) as opencode, \
                redirect_stdout(io.StringIO()):
            for backend in ("codex", "opencode"):
                self.assertEqual(dispatch(["qualify", "--profile", str(profile), "--root", str(self.source),
                    "--backend", backend, "--evidence-dir", str(self.private)]), 0)
        codex.assert_called_once()
        opencode.assert_called_once()

    def test_codex_qualification_factory_is_required_and_strong_api_is_used(self):
        from agent_harness.tooling_cli import _codex_catalog
        class Legacy:
            def with_tool_config(self, *args, **kwargs):
                return self
        with patch("agent_harness.codex_execution.CodexAdapter", return_value=Legacy()):
            with self.assertRaisesRegex(HarnessError, "no managed native factory"):
                _codex_catalog({"mcp_servers": {}}, self.source, "worker", self.private, load_profile("baseline"))
        calls = []
        class Managed(Legacy):
            def with_managed_tools(self, **kwargs):
                calls.append(("factory", kwargs));return self
            def qualify(self, **kwargs):
                calls.append(("qualify", kwargs));return self
            def verify_qualification(self, **kwargs):
                calls.append(("verify", kwargs))
            def capabilities(self):
                return {"status": "qualified", "effective_tool_catalog": True}
            def effective_tool_catalog(self):
                return []
            def qualification_receipt(self):
                return {"path": "/fixture/core-receipt.json", "sha256": "1" * 64}
        with patch("agent_harness.codex_execution.CodexAdapter", return_value=Managed()):
            receipt = _codex_catalog({"mcp_servers": {}}, self.source, "worker", self.private, load_profile("baseline"))
        self.assertEqual(receipt["status"], "READY")
        self.assertFalse(receipt["qualified_for_exec"])
        self.assertFalse(receipt["reusable_as_authorization"])
        self.assertEqual([name for name, _ in calls], ["factory", "qualify", "verify"])
        self.assertEqual(calls[1][1]["evidence_dir"], self.private)


if __name__ == "__main__":
    unittest.main()
