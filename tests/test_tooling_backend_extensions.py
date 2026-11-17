"""Backend-neutral required results preserve legacy acceptance and MCP identity."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError
from agent_harness.execution import ProfileExecution, require_native_dependencies
from agent_harness.tool_profiles import load_profile


class EventClient:
    def __init__(self, events=(), catalog=()):
        self.events, self.catalog = events, catalog

    def run(self, prompt, *, on_event, **options):
        for event in self.events:
            on_event(event)
        return {"status": "completed", "outcome_known": True}

    def effective_tool_catalog(self):
        return self.catalog


class ToolingBackendExtensionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = load_profile({"schema_version": 1, "id": "runtime-policy", "version": 1,
            "servers": [{"id": "fixture", "transport": "stdio", "command": [sys.executable],
                "required": True, "allowed_tools": ["snapshot", "close"],
                "required_tools": ["snapshot", "close"]}]})
        self.binding = {"identity": {"root": str(self.root), "cache_root": str(self.root / "cache")}}

    def event(self, tool, *, error=False):
        return {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "fixture",
            "tool": tool, "result": {"isError": error, "content": []}}}

    def test_explicit_result_subset_and_legacy_all_required_results(self):
        client = EventClient([self.event("snapshot")])
        legacy = ProfileExecution(client, self.profile, self.binding).run("test")
        self.assertEqual(legacy["status"], "failed")
        self.assertEqual(legacy["error"], "MANDATORY_MCP_RESULT_UNCONFIRMED")
        selected = deepcopy(self.profile)
        selected["servers"][0]["required_results"] = ["snapshot"]
        self.assertEqual(ProfileExecution(client, selected, self.binding).run("test")["status"], "completed")

    def test_nonrequired_result_tool_failure_still_rejects_mandatory_server(self):
        self.profile["servers"][0]["required_results"] = ["snapshot"]
        client = EventClient([self.event("snapshot"), self.event("close", error=True)])
        self.assertEqual(ProfileExecution(client, self.profile, self.binding).run("test")["status"], "failed")

    def test_native_required_tools_bind_server_and_tool_not_only_local_name(self):
        catalog = [{"server": "fixture", "name": name} for name in ("snapshot", "close")]
        require_native_dependencies(self.profile, EventClient(catalog=catalog),
            {"connected_mcp_servers": ["fixture"]})
        catalog[0]["server"] = "other"
        with self.assertRaisesRegex(HarnessError, "absent"):
            require_native_dependencies(self.profile, EventClient(catalog=catalog),
                {"connected_mcp_servers": ["fixture", "other"]})


if __name__ == "__main__":
    unittest.main()
