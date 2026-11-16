"""Concurrent backend admission must check the adapters selected by the plan."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.managed_execution import _requires_codex_suppression


class BackendSuppressionAdmissionTests(unittest.TestCase):
    def test_old_and_unknown_plans_keep_codex_suppression_requirement(self):
        self.assertTrue(_requires_codex_suppression({}, None))
        self.assertTrue(_requires_codex_suppression({}, {"tasks": [{"id": "legacy"}]}))

    def test_only_explicit_opencode_nodes_do_not_require_unselected_codex(self):
        plan = {"tasks": [{"execution": {"backend": "opencode"}},
                          {"execution": {"backend": "opencode"}}]}
        self.assertFalse(_requires_codex_suppression({}, plan))
        self.assertFalse(_requires_codex_suppression({"execution": {"backend": "opencode"}},
            {"tasks": [{"id": "inherited"}]}))

    def test_mixed_and_implicit_codex_nodes_still_require_proof(self):
        self.assertTrue(_requires_codex_suppression({}, {"tasks": [
            {"execution": {"backend": "opencode"}}, {"id": "implicit-codex"}]}))
        self.assertTrue(_requires_codex_suppression({"execution": {"backend": "opencode"}},
            {"tasks": [{"execution": {"backend": "codex"}}]}))


if __name__ == "__main__":
    unittest.main()
