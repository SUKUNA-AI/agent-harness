"""Public CLI contracts for bounded retrieval configuration and narrow facts."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.execution_ledger import repository_identity
from agent_harness.execution_receipts import build_digest
from agent_harness.receipt_identity import package_build_digest

ROOT = Path(__file__).resolve().parents[1]


class StabilizationCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def call(self, script, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools" / script), *map(str,args)],
                              capture_output=True, timeout=30)

    def test_context_config_cas_and_budget_refusal_before_mutation(self):
        state = self.root / "state"
        common = ("--state", state)
        initial = self.call("session.py", *common, "context-config")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertEqual(json.loads(initial.stdout)["generation"], 1)
        changed = self.call("session.py", *common, "context-config", "--checkpoint-scan", '{"max_records":64}', "--expected-generation", 1)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertEqual(json.loads(changed.stdout)["generation"], 2)
        path = state / "native-context-config.yaml"
        before = path.read_bytes()
        stale = self.call("session.py", *common, "context-config", "--checkpoint-scan", '{"max_records":32}', "--expected-generation", 1)
        self.assertEqual(stale.returncode, 2)
        bounded = self.call("session.py", *common, "--max-bytes", 64, "context-config", "--checkpoint-scan", '{"max_records":32}', "--expected-generation", 2)
        self.assertEqual(bounded.returncode, 2)
        self.assertEqual(path.read_bytes(), before)

    def test_file_symbol_fact_registration_and_typed_show_use_actual_files(self):
        repository = self.root / "repository"
        repository.mkdir()
        (repository / "module.py").write_text('def greeting():\n    return "привет"\n', encoding="utf-8")
        identity = repository_identity(repository)
        store = self.root / "memory"
        common = ("--store", store, "--repository-identity", identity)
        for command, identifier, extra, kind in (
            ("file-fact", "fixture-file", (), "exact_file_content"),
            ("symbol-fact", "fixture-symbol", ("--symbol", "greeting"), "exact_symbol_location")):
            result = self.call("harness.py", "work", "memory", command, identifier, *common,
                               "--root", repository, "--path", "module.py", *extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            row = json.loads(result.stdout)
            self.assertEqual(row["claim_kind"], kind)
            self.assertTrue(row["closure"]["closure_complete"])
            shown = self.call("harness.py", "work", "memory", "show", identifier, *common, "--kind", "fact")
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn(kind, shown.stdout.decode("utf-8"))

    def test_execution_uses_shared_schema_and_vendor_bound_build_identity(self):
        self.assertEqual(build_digest(), package_build_digest())


if __name__ == "__main__":
    unittest.main()
