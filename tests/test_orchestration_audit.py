"""An unavailable upstream must not stop independent authorized work."""
import unittest

import test_orchestration as fixtures
from agent_harness.orchestration import next_wave


class MissingDependencyTests(unittest.TestCase):
    def test_unavailable_dependency_blocks_only_its_dependants(self):
        for damage in ("missing_state", "malformed_state", "missing_contract"):
            with self.subTest(damage=damage):
                fixture = fixtures.OrchestrationTests()
                fixture.setUp()
                try:
                    run, _ = fixture.accepted_upstream()
                    fixture.plan["nodes"].append(fixture.node("c"))
                    if damage == "missing_state":
                        run.state_path.unlink()
                    elif damage == "malformed_state":
                        run.state_path.write_text("{", encoding="utf-8")
                    else:
                        (run.path / "contracts" / (run.state()["contract_digest"] + ".json")).unlink()
                    result = next_wave(fixture.plan, fixture.contract, fixture.observation)
                    self.assertEqual(["c"], [item["node_id"] for item in result["selected"]])
                    self.assertEqual("dependency_not_accepted_current", result["blocked"]["b"])
                finally:
                    fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
