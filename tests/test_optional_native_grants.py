"""Diagnostic fallback can restrict permissions, never grant client readiness."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError
from agent_harness.execution import available_profile, require_native_dependencies


class OptionalNativeGrantsTests(unittest.TestCase):
    def server(self, required=False):
        return {"id": "source", "required": required, "allowed_tools": ["read", "optional_search"],
            "required_tools": ["read"], "read_only_tools": ["read", "optional_search"]}

    def test_unavailable_optional_server_is_omitted_without_changing_policy(self):
        profile = {"servers": [self.server()]}
        before = deepcopy(profile)
        result = available_profile(profile, {"servers": [{"id": "source", "status": "BLOCKED", "catalog": []}],
            "client_catalog_verified": False})
        self.assertEqual(result["servers"], [])
        self.assertEqual(profile, before)
        self.assertNotIn("client_catalog_verified", result)

    def test_allowed_optional_tool_missing_from_catalog_does_not_expand_grants(self):
        server = self.server(True)
        result = available_profile({"servers": [server]}, {"servers": [
            {"id": "source", "status": "READY", "catalog": [{"name": "read"}, {"name": "write"}]}]})
        self.assertEqual(result["servers"][0]["allowed_tools"], ["read"])
        self.assertEqual(result["servers"][0]["read_only_tools"], ["read"])
        self.assertEqual(server["allowed_tools"], ["read", "optional_search"])

    def test_required_failure_and_required_missing_tool_remain_blocked(self):
        for required, status, tools in ((True, "BLOCKED", []), (True, "READY", []), (False, "READY", [])):
            with self.subTest(required=required, status=status), self.assertRaises(HarnessError):
                available_profile({"servers": [self.server(required)]}, {"servers": [
                    {"id": "source", "status": status, "catalog": tools}]})

    def test_separate_diagnostic_ready_cannot_replace_native_required_connection(self):
        profile = {"servers": [self.server(True)]}
        adapter = SimpleNamespace(effective_tool_catalog=lambda: [{"name": "source_read"}])
        for capabilities in ({}, {"connected_mcp_servers": []}, {"connected_mcp_servers": [{}]}):
            with self.subTest(capabilities=capabilities), self.assertRaises(HarnessError):
                require_native_dependencies(profile, adapter, capabilities)
        require_native_dependencies(profile, adapter, {"connected_mcp_servers": ["source"]})
        adapter.effective_tool_catalog = lambda: []
        with self.assertRaises(HarnessError):
            require_native_dependencies(profile, adapter, {"connected_mcp_servers": ["source"]})


if __name__ == "__main__":
    unittest.main()
