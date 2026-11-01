from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest

import test_core as fixtures
from agent_harness.cli import main
from agent_harness.runner import gate


class ContextCLITests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CoreIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_compact_status_and_handoff_keep_verification_exit_codes_and_state(self):
        run, evidence = self.fixture.passing()
        self.assertEqual("accepted", gate(run)["verdict"])
        run.checkpoint("Keep the confirmed parser decision", "Read the next file")
        before = run.state_path.read_bytes()
        code, output, err = self.invoke(["status", "--run-dir", str(run.path), "--summary",
                                        "--verify-evidence", "--max-output-bytes", "4096"])
        self.assertEqual(0, code, err)
        self.assertLessEqual(len(output.encode("utf-8")), 4096)
        self.assertTrue(json.loads(output)["evidence_current"])
        code, output, err = self.invoke(["handoff", "--run-dir", str(run.path), "--verify-evidence"])
        self.assertEqual(0, code, err)
        self.assertEqual("Read the next file", json.loads(output)["checkpoint"]["next_safe_step"]["text"])
        self.assertEqual(before, run.state_path.read_bytes())
        (run.path / evidence["artifacts"][0]["path"]).unlink()
        code, output, err = self.invoke(["status", "--run-dir", str(run.path), "--summary", "--verify-evidence"])
        self.assertEqual(1, code, err)
        self.assertFalse(json.loads(output)["evidence_current"])
        self.assertEqual("stale", json.loads(output)["status"])
        self.assertEqual(before, run.state_path.read_bytes())

    def test_full_status_default_remains_available(self):
        run, _ = self.fixture.passing()
        code, output, err = self.invoke(["status", "--run-dir", str(run.path)])
        self.assertEqual(0, code, err)
        result = json.loads(output)
        self.assertEqual(str(run.path), result["run_dir"])
        self.assertIn("processes", result)
        self.assertNotIn("view", result)
        self.assertIsNone(result["evidence_current"])


if __name__ == "__main__":
    unittest.main()
