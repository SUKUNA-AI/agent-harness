"""Narrow verified claims, bounded checkpoint projections and native identity."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import __version__
from agent_harness.core import HarnessError, digest, now
from agent_harness.checkpoint_index import CheckpointIndex, configure, settings
from agent_harness.execution_ledger import repository_identity
from agent_harness.knowledge_claims import build_fact, verify_fact, FACT_SCHEMA
from agent_harness.memory import Memory
from agent_harness.native_session import current_provenance, session_packet, OPERATION_SCHEMA, OPERATION_V1_SCHEMA, OPERATION_READ_SCHEMA
from agent_harness.native_observability import observe, status, OBSERVER_SCHEMA, OBSERVER_V1_SCHEMA, OBSERVER_READ_SCHEMA
from agent_harness.receipt_identity import build_identity, package_build_digest
from agent_harness.strict_yaml import dump, load, validate


class NarrowContextFollowup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "module.py").write_text("class App:\n    def method(self):\n        return 42\n", encoding="utf-8")
        (self.root / "style.css").write_text("body { color: black; }\n")
        self.state = self.base / "state"
        self.project = repository_identity(self.root)
        self.memory = Memory(self.state / "memory", self.project, enabled=True)

    def current(self):
        return current_provenance(self.root)

    def short(self, identifier):
        return {"schema_version": 1, "id": identifier, "repository_identity": self.project,
            "goal": "Inspect module", "constraints": [], "checkpoint": "Observed " + identifier,
            "active_task": "Inspect module", "failures": [], "important_files": [], "next_step": "Review",
            "evidence_ids": [], "session_id": identifier, "attempts_used": 0, "tokens_reported": None,
            "updated_at": now(), "provenance": self.current()}

    def test_exact_file_fact_survives_unrelated_changes_and_new_discovery_file(self):
        row = self.memory.record_file_fact(self.root, "module.py")
        before = self.current()
        (self.root / "style.css").write_text("body { color: blue; }\n")
        (self.root / "plugin.py").write_text("raise RuntimeError('discovery')\n")
        after = self.current()
        self.assertNotEqual(before["snapshot_id"], after["snapshot_id"])
        recalled = self.memory.recall("module", after)
        self.assertEqual(recalled["verified_facts"][0]["id"], row["id"])
        self.assertEqual(recalled["verified_facts"][0]["verification_level"], "self_checked")
        self.assertEqual(row["closure"]["positive_dependencies"][0]["path"], "module.py")
        self.assertEqual(row["closure"]["negative_dependencies"][0]["kind"], "no_link_ancestors")
        (self.root / "module.py").write_text("changed\n")
        self.assertEqual(self.memory.recall("module", self.current())["verified_facts"], [])

    def test_qualified_symbol_location_and_negative_uniqueness(self):
        (self.root / "module.py").write_text("s='😀\u2028literal'\nclass App:\n    def method(self):\n        return 42\n", encoding="utf-8")
        fact = self.memory.record_symbol_fact(self.root, "module.py", "App.method")
        self.assertEqual(fact["location"]["start_line"], 3)
        self.assertEqual(fact["location"]["start_column"], 4)
        self.assertEqual(fact["location"]["column_encoding"], "utf8_bytes_zero_based")
        negative = fact["closure"]["negative_dependencies"][1]
        self.assertEqual(negative["kind"], "symbol_uniqueness")
        self.assertEqual(negative["matching_declarations"], 1)
        self.assertTrue(fact["closure"]["parser_version"])
        self.assertEqual(verify_fact(fact, self.current())["source_validity"], "current")
        (self.root / "module.py").write_text("class App:\n    def method(self): pass\n    def method(self): pass\n")
        with self.assertRaisesRegex(HarnessError, "AMBIGUOUS"):
            self.memory.record_symbol_fact(self.root, "module.py", "App.method")
        self.assertNotEqual(verify_fact(fact, self.current())["source_validity"], "current")

    def test_authored_closure_and_wrong_claim_kind_cannot_promote(self):
        fact = build_fact(self.root, "module.py", self.current())
        forged = deepcopy(fact)
        forged["closure"]["positive_dependencies"].append({"path": "style.css", "sha256": digest("fake"), "size_bytes": 1})
        forged["closure_digest"] = digest(forged["closure"])
        self.assertNotEqual(verify_fact(forged, self.current())["source_validity"], "current")
        forged = deepcopy(fact)
        forged["claim_kind"] = "exact_symbol_location"
        self.assertEqual(verify_fact(forged, self.current())["source_validity"], "changed")
        forged = deepcopy(fact)
        forged["closure"]["config_refs"]["reader_policy"] = digest("foreign-reader")
        forged["closure_digest"] = digest(forged["closure"])
        self.assertEqual(verify_fact(forged, self.current())["source_validity"], "changed")
        forged = deepcopy(fact)
        forged["assertion"] = "The entire API is correct"
        with self.assertRaises(ValueError):
            validate(forged, FACT_SCHEMA)

    def test_project_checkout_and_parser_bindings_are_required(self):
        fact = self.memory.record_symbol_fact(self.root, "module.py", "App.method")
        current = self.current()
        for key in ("repository_identity", "checkout_ref"):
            self.assertEqual(verify_fact(fact, {**current, key: digest("foreign")})["source_validity"], "changed")
        self.assertNotEqual(verify_fact(fact, {**current, "checkout_root": str(self.base)})["source_validity"], "current")
        from agent_harness import knowledge_claims
        real = knowledge_claims._policies
        def different(symbol):
            reader, parser, version, builder = real(symbol)
            return reader, digest("different-parser"), version, builder
        with patch.object(knowledge_claims, "_policies", side_effect=different):
            self.assertEqual(verify_fact(fact, current)["source_validity"], "changed")

    def test_narrow_fact_rejects_link_and_unsupported_language(self):
        target = self.root / "alias.py"
        try:
            target.symlink_to(self.root / "module.py")
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(HarnessError):
            self.memory.record_file_fact(self.root, "alias.py")
        with self.assertRaises(HarnessError):
            self.memory.record_symbol_fact(self.root, "style.css", "App")

    def test_configuration_is_explicit_bounded_and_generation_checked(self):
        changed = configure(self.state, checkpoint_scan={"max_records": 2}, memory_scan={"max_records": 1}, expected_generation=1)
        self.assertEqual(changed["checkpoint_scan"]["max_records"], 2)
        self.assertEqual(settings(self.state)["memory_scan"]["max_records"], 1)
        with self.assertRaisesRegex(HarnessError, "GENERATION_CONFLICT"):
            configure(self.state, checkpoint_scan={"max_records": 3}, expected_generation=1)
        with self.assertRaises(ValueError):
            configure(self.state, checkpoint_scan={"max_records": 0})
        with self.assertRaises(ValueError):
            configure(self.state, checkpoint_scan={"deadline_ms": True})

    def test_warm_index_revalidates_selected_bytes_and_updates_under_writer_lock(self):
        for identifier in ("one", "two", "three"):
            self.memory.update_short_term(self.short(identifier))
        index = CheckpointIndex(self.memory.root, self.project)
        first, diag, cold = index.select()
        self.assertEqual(len(first), 3)
        self.assertFalse(diag)
        self.assertTrue(cold["scan_complete"])
        second, diag, warm = index.select()
        self.assertTrue(warm["index_hit"])
        self.assertEqual(len(second), 3)
        self.assertGreater(warm["source_bytes_read"], 0)
        self.assertLessEqual(warm["records_examined"], cold["records_examined"])
        self.memory.update_short_term(self.short("four"))
        latest, _, updated = index.select()
        self.assertEqual(latest[0][0]["id"], "four")
        self.assertTrue(updated["index_hit"])
        self.assertGreater(updated["index_generation"], warm["index_generation"])

    def test_corrupt_index_rebuild_and_partial_cursor_do_not_claim_global_latest(self):
        for i in range(4):
            self.memory.update_short_term(self.short("entry-" + str(i)))
        index = CheckpointIndex(self.memory.root, self.project, limits={"max_records": 1})
        rows, diag, metadata = index.select()
        self.assertEqual(len(rows), 1)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["selection_scope"], "bounded_scanned_records")
        self.assertIsNotNone(metadata["next_cursor"])
        for _ in range(5):
            if metadata["next_cursor"] is None:
                break
            _, _, metadata = index.select(cursor=metadata["next_cursor"])
        self.assertTrue(metadata["scan_complete"])
        index.path.write_text("broken: [\n")
        _, _, rebuilt = index.select()
        self.assertFalse(rebuilt["index_hit"])
        self.assertFalse(rebuilt["scan_complete"])
        (self.memory.root / "short_term" / "foreign.yaml").write_text("invalid: [\n")
        with self.assertRaisesRegex(HarnessError, "STALE_CURSOR"):
            index.select(cursor=rebuilt["next_cursor"])

    def test_corrupt_optional_record_and_capped_inventory_are_truthful(self):
        for i in range(3):
            self.memory.update_short_term(self.short("entry-" + str(i)))
        directory = self.memory.root / "short_term"
        (directory / "broken.yaml").write_text("broken: [\n")
        rows, diag, metrics = CheckpointIndex(self.memory.root, self.project).select()
        self.assertEqual(len(rows), 3)
        self.assertTrue(any(row["reason"] == "invalid_foreign_checkpoint" for row in diag))
        self.assertEqual(metrics["invalid_records"], 1)
        _, _, capped = CheckpointIndex(self.memory.root, self.project, limits={"max_inventory_entries": 1}).select()
        self.assertFalse(capped["inventory_complete"])
        self.assertIsNone(capped["next_cursor"])
        self.assertEqual(capped["terminal_diagnostic"], "checkpoint_scope_must_be_narrowed")

    def test_concurrent_checkpoint_updates_do_not_lose_index_entries(self):
        self.memory.update_short_term(self.short("initial"))
        index = CheckpointIndex(self.memory.root, self.project)
        index.select()
        rows = [self.short("concurrent-" + str(i)) for i in range(6)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.memory.update_short_term, rows))
        _, _, metrics = index.select()
        self.assertTrue(metrics["scan_complete"])
        index_row = load(index.path)
        self.assertEqual({row["id"] for row in index_row["entries"]}, {"initial", *(row["id"] for row in rows)})

    def test_checkpoint_write_uses_writer_admission_not_optional_scan_deadline(self):
        import threading
        import time
        from agent_harness.core import lock
        memory = Memory(self.state / 'memory', self.project, enabled=True,
                        checkpoint_limits={'deadline_ms': 1})
        entered, release, writing = threading.Event(), threading.Event(), threading.Event()
        def hold():
            with lock(memory.root / '.lock', purpose='concurrent native writer'):
                entered.set()
                if not release.wait(5): raise AssertionError('writer was not released')
        def write():
            writing.set()
            return memory.update_short_term(self.short('queued'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(hold)
            try:
                self.assertTrue(entered.wait(5))
                writer = pool.submit(write)
                self.assertTrue(writing.wait(5))
                time.sleep(.05)
                self.assertFalse(writer.done(), 'scan budget prematurely refused authoritative write')
            finally:
                release.set()
            holder.result(timeout=5)
            self.assertEqual(writer.result(timeout=5)['id'], 'queued')
        self.assertEqual(load(memory.root / 'short_term' / 'queued.yaml')['id'], 'queued')

    def event(self, **kwargs):
        return {"hook_event_name": "PostModelRequest", "session_id": "runtime", "cwd": str(self.root),
                "agent_type": "root", "event_id": "event-one", "request_id": "request-one", "attempt_id": "attempt-one", **kwargs}

    def test_native_observation_identity_is_actual_bound_and_future_versions_fail_closed(self):
        result = observe(self.state, self.event(client_version="invented", prompt="private-canary"))
        record = load(result["path"], OBSERVER_SCHEMA)
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["harness_version"], __version__)
        self.assertIsNone(record["client_version"])
        self.assertIsNone(record["capability_receipt_ref"])
        self.assertIsNone(record["invocation_id"])
        self.assertEqual(record["attempt_id"], "attempt-one")
        self.assertEqual(record["build_provenance"]["source"], "on_disk_package_and_loaded_producer")
        self.assertGreater(record["build_provenance"]["package_files_examined"], 1)
        self.assertEqual(record["input_refs"][0]["content_digest"], record["payload_digest"])
        with self.assertRaises(ValueError):
            validate({**record, "schema_version": 3}, OBSERVER_READ_SCHEMA)
        with self.assertRaises(ValueError):
            validate(record, OBSERVER_V1_SCHEMA)

    def test_new_checkpoint_receipt_identity_and_version_barrier(self):
        started = session_packet(self.state, root=self.root, goal="Inspect")
        identifier = started["required"]["session"]
        session_packet(self.state, session=identifier, note="Checked", next_step="Review", operation_id="step-one")
        path = self.state / "native-sessions" / identifier / "operation-step-one.yaml"
        operation = load(path, OPERATION_SCHEMA)
        self.assertEqual(operation["schema_version"], 2)
        self.assertEqual(operation["project_id"], self.project)
        self.assertIsNone(operation["run_id"])
        self.assertIsNone(operation["task_id"])
        self.assertEqual(operation["input_refs"][0]["content_digest"], operation["request_digest"])
        self.assertEqual({ref["kind"] for ref in operation["input_refs"]},
                         {"checkpoint_request", "source", "goal", "context_replay"})
        self.assertEqual(len(operation["input_refs"]), 4)
        with self.assertRaises(ValueError):
            validate(operation, OPERATION_V1_SCHEMA)
        dump(path, {**operation, "schema_version": 99})
        with self.assertRaises(ValueError):
            session_packet(self.state, session=identifier, note="Checked", next_step="Review", operation_id="step-one")

    def test_shared_build_digest_covers_schema_vendor_and_installed_layout(self):
        source = self.base / "build-source"
        package = source / "src" / "agent_harness"
        (package / "_vendor").mkdir(parents=True)
        (package / "__init__.py").write_text("version='fixture'\n")
        (package / "_vendor" / "parser.py").write_text("rule=1\n")
        schemas = source / "schemas"
        schemas.mkdir()
        (schemas / "record.schema.json").write_text('{"type":"object"}')
        (package / "_vendor" / "manifest.json").write_text('{"parser":"v1"}')
        before = package_build_digest(package)
        (package / "_vendor" / "manifest.json").write_text('{"parser":"v2"}')
        self.assertNotEqual(package_build_digest(package), before)
        (package / "_vendor" / "manifest.json").write_text('{"parser":"v1"}')
        (schemas / "record.schema.json").write_text('{"type":"string"}')
        self.assertNotEqual(package_build_digest(package), before)
        (schemas / "record.schema.json").write_text('{"type":"object"}')
        (package / "_vendor" / "parser.py").write_text("rule=2\n")
        self.assertNotEqual(package_build_digest(package), before)
        (package / "_vendor" / "parser.py").write_text("rule=1\n")
        import shutil
        installed = self.base / "installed" / "agent_harness"
        shutil.copytree(package, installed)
        shutil.copytree(schemas, installed / "schemas")
        self.assertEqual(package_build_digest(installed), before)
        (installed / "foreign-native-artifact.bin").write_bytes(b"foreign")
        self.assertEqual(package_build_digest(installed), before)

    def test_build_identity_observes_package_bytes_and_loaded_producer(self):
        from agent_harness import receipt_identity
        before = build_identity("agent_harness.native_session")
        with patch.object(receipt_identity, "_package_manifest", return_value={"actual.py": digest("changed bytes")}):
            after = build_identity("agent_harness.native_session")
        self.assertNotEqual(before["harness_build_digest"], after["harness_build_digest"])
        self.assertEqual(before["build_provenance"]["producer_code_digest"], after["build_provenance"]["producer_code_digest"])


if __name__ == "__main__":
    unittest.main()
