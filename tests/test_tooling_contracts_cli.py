"""Strict extension compatibility and operator output containment negatives."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.codex_tool_catalog import _toml
from agent_harness.core import HarnessError, digest
from agent_harness.scheduling import Scheduler
from agent_harness.strict_yaml import StrictYAMLError, validate
from agent_harness.tooling_cli import dispatch
from agent_harness.work_contract import normalize
from test_managed_scheduling import node


class ToolingContractCLITests(unittest.TestCase):
    def contract(self):
        return normalize({"schema_version": 1, "task": {"id": "managed", "objective": "fixture", "allowed_write_scopes": ["**"]},
            "mode": "managed", "budgets": {"max_concurrent_managed_agents": 12},
            "acceptance": [{"id": "combined", "kind": "command", "required": True, "argv": [sys.executable, "-c", "pass"]}]})

    def test_old_normalized_digest_has_no_new_defaults_and_extensions_are_closed(self):
        value = self.contract()
        self.assertEqual(digest(value), digest(normalize(value)))
        self.assertNotIn("execution", value)
        for change in ({"backend": "unknown"}, {"tool_profile": {"id": "baseline", "version": True}},
                       {"unexpected": True}, {"required_capabilities": ["invented"]},
                       {"tool_profile": {"id": "baseline", "version": 1, "path": "relative.yaml"}}):
            trial = deepcopy(value)
            trial["execution"] = change
            with self.subTest(change=change), self.assertRaises((HarnessError, StrictYAMLError)):
                normalize(trial)
        value["mode"] = "native"
        value["budgets"]["max_concurrent_managed_agents"] = 1
        value["execution"] = {"backend": "opencode"}
        with self.assertRaisesRegex(HarnessError, "native subagents"):
            normalize(value)

    def test_mixed_backend_dag_still_has_twelve_total_and_three_reviewer_slots(self):
        plan = {"schema_version": 1, "task_id": "managed", "tasks": []}
        for index in range(13):
            task = node("item-" + str(index))
            task["execution"] = {"backend": "codex" if index % 2 else "opencode", "tool_profile": {"id": "baseline", "version": 1}}
            if index < 4:
                task.update(role="reviewer", read_only=True, write_scope=[], outputs=[])
            plan["tasks"].append(task)
        scheduler = Scheduler(plan, self.contract())
        selected = scheduler.next()["selected"]
        self.assertEqual(len(selected), 12)
        self.assertEqual(sum(scheduler.nodes[n]["role"] == "reviewer" for n in selected), 3)
        self.assertEqual(scheduler.next(active=selected)["selected"], [])

    def test_valid_fractional_native_timeouts_and_nonfinite_rejection(self):
        self.assertEqual(tomllib.loads("a=" + _toml({"startup_timeout_sec": .3}))["a"]["startup_timeout_sec"], .3)
        for value in (float("nan"), float("inf")):
            with self.assertRaises(HarnessError):
                _toml(value)

    def test_dry_run_is_pure_and_apply_and_snapshot_reject_traversal_or_user_config(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            (source / "file.py").write_text("VALUE = 1\n")
            private = base / "private"
            private.mkdir()
            home = base / "codex-home"
            home.mkdir()
            before = set(base.rglob("*"))
            with patch.dict(os.environ, {"CODEX_HOME": str(home)}), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(dispatch(["dry-run", "--root", str(source)]), 0)
                self.assertEqual(set(base.rglob("*")), before)
                for command, args in (("apply", ["--apply"]), ("snapshot", [])):
                    target = str(private / ".." / "source" / "forbidden")
                    self.assertEqual(dispatch([command, "--root", str(source), "--output", target, *args]), 2)
                self.assertEqual(dispatch(["apply", "--root", str(source), "--output", str(home / "config.toml"), "--apply"]), 2)
                target = private / "tools.toml"
                self.assertEqual(dispatch(["apply", "--root", str(source), "--output", str(target), "--apply"]), 0)
                self.assertEqual(tomllib.loads(target.read_text()), {"mcp_servers": {}})
                self.assertEqual(dispatch(["apply", "--root", str(source), "--output", str(target), "--apply"]), 2)
            self.assertFalse((source / "forbidden").exists())
            self.assertFalse((home / "config.toml").exists())

    def test_native_qualification_rejects_source_config_paths_without_starting_client(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            private = base / "private"
            private.mkdir()
            codex = base / "codex"
            codex.mkdir()
            with patch.dict(os.environ, {"CODEX_HOME": str(codex)}), \
                    patch("agent_harness.tooling_cli._opencode_catalog", side_effect=AssertionError("must not start client")), \
                    patch("agent_harness.tooling_cli._codex_catalog", return_value={"status": "READY", "client_catalog_verified": True}) as codex_catalog, \
                    redirect_stdout(io.StringIO()):
                for target in (source, codex, base / "absent", private / ".." / "source"):
                    with self.subTest(path=target):
                        self.assertEqual(dispatch(["qualify", "--backend", "opencode", "--root", str(source),
                            "--evidence-dir", str(target)]), 2)
                self.assertEqual(dispatch(["qualify", "--backend", "codex", "--root", str(source),
                    "--evidence-dir", str(private)]), 0)
                codex_catalog.assert_called_once()
            self.assertEqual(list(source.iterdir()), [])
            self.assertEqual(list(private.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
