"""Explicit migration CAS, preservation, old-writer barrier and usage trust."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import io
import json
import subprocess
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, digest
from agent_harness.state_migration import migrate
from agent_harness.strict_yaml import dump, load, validate
from agent_harness.supervisor import Supervisor
from agent_harness.supervisor_store import SupervisorStore


class StateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        source = self.root / "source"
        source.mkdir()
        (source / "calc.py").write_text("VALUE=0\n")
        contract = {"schema_version": 1, "task": {"id": "fixture", "objective": "migration fixture",
                     "allowed_write_scopes": ["calc.py"]},
                    "acceptance": [{"id": "check", "kind": "command", "required": True,
                                    "argv": [sys.executable, "-c", "print('fixture')"], "timeout": 5}],
                    "budgets": {"max_attempts": 1, "max_wall_seconds": 60}}
        path = self.root / "contract.yaml"
        dump(path, contract)
        supervisor = Supervisor.create(path, run_dir=self.root / "unused", cwd=source,
                                       state_dir=self.root / "state")
        state = deepcopy(supervisor.state)
        state["schema_version"] = 1
        state["generation"] = 1
        state["metrics"]["usage_accounting_version"] = 2
        state["metrics"].pop("legacy_history_unverified", None)
        self.run = self.root / "legacy"
        self.store = SupervisorStore(self.run, "work-state.schema.json")
        self.store.write("contract.yaml", supervisor.contract, "work-contract.schema.json")
        self.store.commit(state, "synthetic legacy fixture")
        self.generation = 1

    def test_dry_run_does_not_mutate_any_existing_bytes(self):
        before = {str(p.relative_to(self.run)): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        preview = migrate(self.run, expected_generation=self.generation)
        self.assertFalse(preview["mutated"])
        after = {str(p.relative_to(self.run)): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_apply_preserves_history_and_never_upgrades_usage_trust(self):
        preview = migrate(self.run, expected_generation=self.generation)
        old_checkpoint = (self.run / "checkpoints/000001.yaml").read_bytes()
        receipt = migrate(self.run, expected_generation=self.generation, apply=True,
                          expected_plan_digest=preview["plan_digest"])
        state = load(self.run / "state.yaml", "work-state.schema.json")
        self.assertEqual(state["schema_version"], 2)
        self.assertTrue(state["metrics"]["legacy_history_unverified"])
        self.assertEqual(state["metrics"]["usage_accounting_version"], 2)
        self.assertEqual((self.run / "checkpoints/000001.yaml").read_bytes(), old_checkpoint)
        self.assertEqual(receipt["status"], "committed")
        with self.assertRaises(ValueError):
            validate(state, {"type": "object", "properties": {"schema_version": {"const": 1}}, "additionalProperties": True})
        backup = self.run / "migrations" / receipt["operation_id"] / "backup/checkpoints/000001.yaml"
        self.assertEqual(backup.read_bytes(), old_checkpoint)

    def test_plan_and_generation_conflicts_preserve_source(self):
        preview = migrate(self.run, expected_generation=self.generation)
        for kwargs in ({"expected_generation": 0},
                       {"expected_generation": self.generation, "apply": True, "expected_plan_digest": "0" * 64}):
            with self.assertRaises(HarnessError):
                migrate(self.run, **kwargs)
        self.assertEqual(load(self.run / "state.yaml")["schema_version"], 1)
        self.assertFalse((self.run / "migrations").exists())

    def test_corrupt_required_checkpoint_blocks_without_backups_or_conversion(self):
        (self.run / "checkpoints/000001.yaml").write_text("invalid: [")
        with self.assertRaises((ValueError, HarnessError)):
            migrate(self.run, expected_generation=self.generation)
        self.assertEqual(load(self.run / "state.yaml")["schema_version"], 1)

    def test_interruption_before_barrier_preserves_usable_original(self):
        preview = migrate(self.run, expected_generation=1)
        with patch.object(SupervisorStore, "commit", side_effect=OSError("injected before barrier")):
            with self.assertRaises(OSError):
                migrate(self.run, expected_generation=1, apply=True, expected_plan_digest=preview["plan_digest"])
        state = load(self.run / "state.yaml", "work-state.schema.json")
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["generation"], 1)
        self.assertTrue(list((self.run / "migrations").glob("*/intent.yaml")))

    def test_pending_barrier_blocks_dispatch_and_explicit_recovery_is_idempotent(self):
        preview = migrate(self.run, expected_generation=1)
        commit = SupervisorStore.commit
        def interrupt(store, state, *args, **kwargs):
            if state.get("migration", {}).get("status") == "committed":
                raise OSError("injected after pending head")
            return commit(store, state, *args, **kwargs)
        with patch.object(SupervisorStore, "commit", interrupt):
            with self.assertRaises(OSError):
                migrate(self.run, expected_generation=1, apply=True, expected_plan_digest=preview["plan_digest"])
        state = load(self.run / "state.yaml", "work-state.schema.json")
        self.assertEqual(state["migration"]["status"], "pending")
        with self.assertRaisesRegex(HarnessError, "MIGRATION_RECOVERY_REQUIRED"):
            Supervisor(self.run).load()
        result = migrate(self.run, expected_generation=2, recover=state["migration"]["operation_id"])
        events = self.store.events()
        repeated = migrate(self.run, expected_generation=3, recover=state["migration"]["operation_id"])
        self.assertEqual(result, repeated)
        self.assertEqual(events, self.store.events())

    def test_committed_head_can_recover_receipt_after_delivery_failure(self):
        preview = migrate(self.run, expected_generation=1)
        write = SupervisorStore.write
        def interrupt(store, name, *args, **kwargs):
            if name.endswith("/receipt.yaml"):
                raise OSError("injected receipt publication failure")
            return write(store, name, *args, **kwargs)
        with patch.object(SupervisorStore, "write", interrupt):
            with self.assertRaises(OSError):
                migrate(self.run, expected_generation=1, apply=True, expected_plan_digest=preview["plan_digest"])
        state = load(self.run / "state.yaml", "work-state.schema.json")
        self.assertEqual(state["migration"]["status"], "committed")
        result = migrate(self.run, expected_generation=3, recover=state["migration"]["operation_id"])
        self.assertEqual(result["destination_state_digest"], digest(state))

    def test_changed_bound_contract_blocks_plan(self):
        contract = load(self.run / "contract.yaml")
        contract["task"]["objective"] = "changed contract"
        dump(self.run / "contract.yaml", contract)
        with self.assertRaisesRegex(HarnessError, "MIGRATION_CONTRACT_BINDING_MISMATCH"):
            migrate(self.run, expected_generation=1)

    def test_actual_baseline_writer_refuses_migrated_fixture_before_authoritative_write(self):
        baseline = "353e82fc4a1e35a13b644304aa996f79dfbbeaee"
        source = Path(__file__).resolve().parents[1]
        archived = subprocess.run(["git", "-C", str(source), "archive", baseline, "src", "schemas"],
                                  capture_output=True, timeout=15)
        if archived.returncode:
            self.skipTest("Actual baseline Git object unavailable; CI fetch-depth=0 is required")
        destination = self.root / "baseline"
        destination.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
            for item in archive.getmembers():
                target = destination / item.name
                if not target.resolve().is_relative_to(destination) or not (item.isdir() or item.isfile()):
                    self.fail("Unsafe baseline archive")
                if item.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(item).read())
        preview = migrate(self.run, expected_generation=1)
        migrate(self.run, expected_generation=1, apply=True, expected_plan_digest=preview["plan_digest"])
        def authoritative():
            return {str(path.relative_to(self.run)): path.read_bytes()
                    for path in self.run.rglob("*") if path.is_file() and "locks" not in path.relative_to(self.run).parts}
        before = authoritative()
        code = """import json,sys
sys.path.insert(0,sys.argv[1])
from agent_harness.supervisor import Supervisor
try:
    Supervisor(sys.argv[2]).resume()
except (ValueError,RuntimeError) as error:
    markers=('schema_version','legacy_history_unverified','unknown field migration')
    print(json.dumps({'blocked':True,'schema_refusal':type(error).__name__=='StrictYAMLError' and any(marker in str(error) for marker in markers),'type':type(error).__name__}))
else:
    raise SystemExit('baseline writer accepted new state')
"""
        result = subprocess.run([sys.executable, "-I", "-c", code, str(destination / "src"), str(self.run)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["schema_refusal"])
        self.assertEqual(before, authoritative())


if __name__ == "__main__":
    unittest.main()
