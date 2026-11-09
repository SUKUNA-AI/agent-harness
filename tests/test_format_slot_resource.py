"""Contained format evaluator must persist usage and close real model leases."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.agent_lifecycle import GlobalSlots
from agent_harness.codex_execution import CodexAdapter
from agent_harness.core import HarnessError
from agent_harness.format_bench import codex_measure, render, FORMAT_SAMPLE_SCHEMA
from agent_harness.strict_yaml import load


class FormatSlotResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "state"
        self.evidence = self.root / "evidence"
        self.fake = self.root / "fake.py"
        self.fake.write_text("""import json
print(json.dumps({'type':'thread.started','thread_id':'contained-format'}))
print(json.dumps({'type':'turn.started'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'answer':'value 10'})}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':3}}))
""", encoding="utf-8")
        self.adapter = CodexAdapter((sys.executable, str(self.fake)))
        self.rows = [{"identifier_long": i, "description_long": "value " + str(i)} for i in range(20)]

    def measure(self):
        with patch("agent_harness.codex_execution.CodexAdapter", return_value=self.adapter), \
                patch("agent_harness.codex_execution.configured_settings", return_value={}), \
                patch("agent_harness.work_cli.default_state_dir", return_value=self.state):
            return codex_measure(self.rows, evidence_dir=self.evidence)("json", render(self.rows, "json"))

    def test_contained_completed_sample_persists_usage_and_releases_real_lease(self):
        observation = self.measure()
        self.assertTrue(observation["correct"])
        self.assertEqual(observation["input_tokens"], 100)
        sample = load(self.evidence / observation["evidence"], FORMAT_SAMPLE_SCHEMA)
        self.assertEqual(sample["input_tokens"], 100)
        self.assertEqual(sum(row["unresolved"] for row in GlobalSlots(self.state).inventory()), 0)

    def test_unverified_cleanup_keeps_usage_receipt_and_uncertain_slot(self):
        original = self.adapter.run
        def unverified(*args, **kwargs):
            result = original(*args, **kwargs)
            return {**result, "process_tree_stopped": False}
        with patch.object(self.adapter, "run", side_effect=unverified):
            with self.assertRaisesRegex(HarnessError, "process-tree cleanup"):
                self.measure()
        samples = list(self.evidence.glob("*.yaml"))
        self.assertEqual(len(samples), 1)
        self.assertEqual(load(samples[0], FORMAT_SAMPLE_SCHEMA)["input_tokens"], 100)
        self.assertEqual(load(samples[0], FORMAT_SAMPLE_SCHEMA)["execution_status"], "cleanup_unverified")
        self.assertEqual(sum(row["unresolved"] for row in GlobalSlots(self.state).inventory()), 1)


if __name__ == "__main__":
    unittest.main()
