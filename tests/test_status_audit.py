import unittest
from unittest.mock import patch

import test_core as fixtures
from agent_harness.runner import gate


class StatusEvidenceTests(unittest.TestCase):
    def test_cheap_status_does_not_claim_artifact_validation(self):
        fixture = fixtures.CoreIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        run, result = fixture.passing()
        self.assertEqual("accepted", gate(run)["verdict"])
        before = run.state_path.read_bytes()
        with patch("agent_harness.runner.accepted_evidence_errors") as validate:
            observed = run.status()
            validate.assert_not_called()
        self.assertTrue(observed["source_current"])
        self.assertIsNone(observed["evidence_current"])
        self.assertEqual("not_checked", observed["evidence_validation"]["status"])
        self.assertEqual("accepted", observed["recorded_status"])
        self.assertTrue(run.status(verify_evidence=True)["evidence_current"])
        (run.path / result["artifacts"][0]["path"]).unlink()
        self.assertIsNone(run.status()["evidence_current"])
        observed = run.status(verify_evidence=True)
        self.assertFalse(observed["evidence_current"])
        self.assertEqual("stale", observed["status"])
        self.assertTrue(any("artifact missing" in error for error in observed["evidence_validation"]["errors"]))
        self.assertEqual(before, run.state_path.read_bytes(), "Read-only status mutated persistent state")


if __name__ == "__main__":
    unittest.main()
