"""Meaningful strict-format, durability, migration and ownership scenarios."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness import strict_yaml as sy
from agent_harness.core import HarnessError
from agent_harness.supervisor_store import SupervisorStore

SCHEMA = {"type": "object", "required": ["schema_version", "status"], "properties": {"schema_version": {"const": 1}, "status": {"enum": ["pending", "accepted"]}, "count": {"type": "integer", "minimum": 0}, "timestamp": {"type": "string", "format": "utc"}}}


class StrictYAMLTests(unittest.TestCase):
    def test_numeric_looking_windows_file_ids_roundtrip_as_strings(self):
        from agent_harness.strict_yaml import dumps, loads
        for value in ('12e999', '960e0000000000000000000000000000', '1e12', '-2E3',
                      'true', 'null', '2026-11-08', '0000123'):
            with self.subTest(value=value):
                document = {value: {'inode': value}}
                self.assertEqual(loads(dumps(document)), document)

    def test_block_yaml_roundtrip_json_scalar_policy(self):
        value = sy.loads("schema_version: 1\nstatus: pending\ncount: 2\n", SCHEMA)
        self.assertEqual(value["count"], 2)
        self.assertEqual(sy.loads(sy.dumps(value, SCHEMA), SCHEMA), value)
        self.assertEqual(sy.loads("a: yes\nb: on\nc: 2026-01-01\nd: 012\n"), {"a": "yes", "b": "on", "c": "2026-01-01", "d": "012"})

    def test_rejects_duplicate_tag_anchor_merge_multidoc_and_unknown(self):
        invalid = ["a: 1\na: 2", "a: !!str 1", "a: &x 1\nb: *x", "<<: {}", "a: 1\n---\nb: 2", "schema_version: 1\nstatus: pending\nother: true"]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(sy.StrictYAMLError):
                sy.loads(text, SCHEMA if "schema_version" in text else None)

    def test_future_version_bool_number_and_invalid_timestamp(self):
        for value in [{"schema_version": 2, "status": "pending"}, {"schema_version": True, "status": "pending"}, {"schema_version": 1, "status": "pending", "timestamp": "2026-01-01T00:00:00+03:00"}]:
            with self.assertRaises(sy.StrictYAMLError):
                sy.validate(value, SCHEMA)
        with self.assertRaises(sy.StrictYAMLError):
            sy.validate({"a": True}, {"const": {"a": 1}})

    def test_conditional_subschema_does_not_invent_unknown_fields(self):
        schema = {"type": "object", "properties": {"kind": {"enum": ["command", "content"]}, "argv": {"type": "array"}, "criteria": {"type": "array"}}, "required": ["kind"], "allOf": [{"if": {"properties": {"kind": {"const": "command"}}}, "then": {"required": ["argv"]}, "else": {"required": ["criteria"]}}]}
        sy.validate({"kind": "command", "argv": ["python"]}, schema)
        sy.validate({"kind": "content", "criteria": ["complete"]}, schema)
        for invalid in [{"kind": "command", "criteria": []}, {"kind": "content", "argv": []}, {"kind": "command", "argv": [], "unexpected": True}]:
            with self.assertRaises(sy.StrictYAMLError):
                sy.validate(invalid, schema)

    def test_depth_size_nonfinite_and_unknown_schema_rejected(self):
        for text in ["[" * 51 + "0" + "]" * 51, "x" * (sy.MAX_BYTES + 1)]:
            with self.assertRaises(sy.StrictYAMLError):
                sy.loads(text)
        with self.assertRaises(sy.StrictYAMLError):
            sy.dumps({"a": float("nan")})
        with self.assertRaises(sy.StrictYAMLError):
            sy.validate({}, {"type": "object", "unsupported": True})

    def test_json_migration_preserves_original_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "old.json", Path(folder) / "new.yaml"
            source.write_text('{"schema_version":1,"status":"pending"}', encoding="utf-8")
            before = source.read_bytes()
            receipt = sy.migrate_json(source, destination, SCHEMA)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(receipt["source_sha256"], hashlib.sha256(before).hexdigest())
            self.assertEqual(sy.load(destination, SCHEMA)["status"], "pending")
            with self.assertRaises(FileExistsError):
                sy.migrate_json(source, destination, SCHEMA)

    def test_offline_vendored_parser_without_site_packages(self):
        code = "import sys;sys.path.insert(0,sys.argv[1]);from agent_harness.strict_yaml import loads;assert loads('x: 1')['x']==1"
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(Path(__file__).resolve().parents[1] / "src")], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_vendored_version_hashes_license_and_no_optional_c_extension(self):
        base = Path(sy.__file__).parent / "_vendor"
        manifest = json.loads((base / "pyyaml-manifest.json").read_text())
        self.assertEqual(manifest["version"], "6.0.3")
        self.assertEqual(manifest["license"], "MIT")
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((base / "yaml" / name).read_bytes()).hexdigest(), digest)
        self.assertFalse((base / "yaml/cyaml.py").exists())
        self.assertFalse(sy.yaml.__with_libyaml__)
        self.assertEqual(hashlib.sha256((base / "yaml/LICENSE").read_bytes()).hexdigest(), manifest["license_sha256"])


class SupervisorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "private"
        self.store = SupervisorStore(self.root, SCHEMA)

    def tearDown(self):
        self.temp.cleanup()

    def test_checkpoint_chain_and_recover_stale_state(self):
        self.store.commit({"schema_version": 1, "status": "pending"}, "start")
        result = self.store.commit({"schema_version": 1, "status": "accepted"}, "checks passed")
        self.assertEqual(result["generation"], 2)
        self.store.write("state.yaml", {"schema_version": 1, "status": "pending"}, SCHEMA)
        restored = self.store.recover()
        self.assertTrue(restored["recovered"])
        self.assertEqual(restored["state"]["status"], "accepted")
        self.assertFalse(self.store.recover()["recovered"])

    def test_readonly_recovery_preserves_legacy_and_current_store_contents(self):
        self.store.commit({"schema_version": 1, "status": "pending"}, "start")
        guard = self.root / "locks/journal-snapshot.guard"
        self.assertTrue(guard.is_file())
        for legacy in (False, True):
            if legacy:
                guard.unlink()
            before = {str(path.relative_to(self.root)): path.read_bytes()
                      for path in self.root.rglob("*") if path.is_file()}
            reader = SupervisorStore(self.root, SCHEMA, create=False)
            self.assertEqual(reader.recover(read_only=True)["state"]["status"], "pending")
            after = {str(path.relative_to(self.root)): path.read_bytes()
                     for path in self.root.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_legacy_read_rejects_new_snapshot_anchor_without_adopting_publication(self):
        self.store.commit({"schema_version": 1, "status": "pending"}, "start")
        guard = self.root / "locks/journal-snapshot.guard"
        guard.unlink()
        reader = SupervisorStore(self.root, SCHEMA, create=False)
        from agent_harness.file_io import ParentDirectory
        opening = ParentDirectory.open
        def publication(parent, *args, **kwargs):
            if parent.path.name == "state.yaml":
                guard.write_bytes(b"")
            return opening(parent, *args, **kwargs)
        with patch.object(ParentDirectory, "open", new=publication):
            with self.assertRaisesRegex(HarnessError, "anchor appeared during legacy read"):
                reader.recover(read_only=True)

    def test_read_snapshot_cannot_upgrade_to_lifetime_writer_lease(self):
        self.store.commit({"schema_version": 1, "status": "pending"}, "start")
        with self.store._snapshot():
            with self.assertRaisesRegex(HarnessError, "Cannot upgrade"):
                self.store.append_event({"must": "not publish"})
            with self.assertRaisesRegex(HarnessError, "Cannot upgrade"):
                with self.store.lease():
                    self.fail("reader acquired a lifetime lease")
        self.assertEqual(len(self.store.events()), 1)

    def test_prepared_publication_rejects_mutated_value_without_writing(self):
        value = {"schema_version": 1, "status": "pending"}
        writes = [("state.yaml", value, SCHEMA, False)]
        self.store._prepared_writes = self.store._publish_writes(writes)
        value["status"] = "accepted"
        try:
            with self.assertRaisesRegex(HarnessError, "Prepared publication"):
                self.store.write("state.yaml", value, SCHEMA)
        finally:
            self.store._prepared_writes = {}
        self.assertFalse((self.root / "state.yaml").exists())

    def test_snapshot_rejects_linked_guard_or_lease_before_external_open(self):
        self.store.commit({"schema_version": 1, "status": "pending"}, "start")
        external = Path(self.temp.name) / "external"
        external.write_bytes(b"external bytes must remain unchanged")
        opening = Path.open
        def checked(path, *args, **kwargs):
            if path in (self.root / "locks/journal-snapshot.guard", self.root / "locks/journal-snapshot"):
                raise AssertionError("linked coordination path was opened")
            return opening(path, *args, **kwargs)
        for name in ("journal-snapshot.guard", "journal-snapshot"):
            path = self.root / "locks" / name
            original = path.read_bytes() if path.exists() else None
            if path.exists():
                path.unlink()
            for target in (external, external.with_name("missing-external")):
                try:
                    path.symlink_to(target)
                except OSError:
                    if original is not None:
                        path.write_bytes(original)
                    self.skipTest("Symlink privilege unavailable")
                try:
                    with patch.object(Path, "open", new=checked):
                        with self.assertRaisesRegex(HarnessError, "Symlink/reparse"):
                            self.store.recover(read_only=True)
                finally:
                    path.unlink()
            if original is not None:
                path.write_bytes(original)
        self.assertEqual(external.read_bytes(), b"external bytes must remain unchanged")
        self.assertFalse(external.with_name("missing-external").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction substitution")
    def test_legacy_snapshot_rejects_junction_created_in_missing_lock_parent(self):
        empty = SupervisorStore(Path(self.temp.name) / "empty")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        locks = empty.root / "locks"
        try:
            with self.assertRaisesRegex(HarnessError, "Symlink/reparse|Unsupported"):
                with empty._snapshot():
                    linked = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(locks), str(outside)], capture_output=True)
                    self.assertEqual(linked.returncode, 0, linked.stderr)
        finally:
            if locks.exists():
                locks.rmdir()
        self.assertEqual(list(outside.iterdir()), [])

    def test_crash_after_durable_journal_rebuilds_checkpoint(self):
        original = self.store.write
        def fail_checkpoint(name, *args, **kwargs):
            if str(name).startswith("checkpoints/"):
                raise OSError("simulated power loss")
            return original(name, *args, **kwargs)
        with patch.object(self.store, "write", side_effect=fail_checkpoint):
            with self.assertRaises(OSError):
                self.store.commit({"schema_version": 1, "status": "pending"}, "preflight")
        recovered = SupervisorStore(self.root, SCHEMA).recover()
        self.assertEqual(recovered["state"]["status"], "pending")
        self.assertEqual(recovered["generation"], 1)

    def test_torn_or_corrupt_journal_is_never_success(self):
        self.store.commit({"schema_version": 1, "status": "pending"}, "start")
        path = self.root / "journal/events.jsonl"
        data = path.read_bytes()
        path.write_bytes(data + b'{"partial":')
        with self.assertRaisesRegex(HarnessError, "Torn"):
            self.store.recover()
        path.write_bytes(data.replace(b'"reason":"start"', b'"reason":"other"'))
        with self.assertRaisesRegex(HarnessError, "hash"):
            self.store.recover()

    def test_explicit_tail_repair_preserves_original_and_requires_reconcile(self):
        self.store.commit({"schema_version": 1, "status": "pending"}, "start")
        path = self.root / "journal/events.jsonl"
        complete = path.read_bytes()
        torn = complete + b'{"schema_version":1,"partial":'
        path.write_bytes(torn)
        receipt = self.store.repair_torn_tail()
        self.assertTrue(receipt["recovery_required"])
        self.assertEqual((self.root / receipt["archive"]).read_bytes(), torn)
        self.assertEqual(receipt["original_sha256"], hashlib.sha256(torn).hexdigest())
        self.assertTrue(path.read_bytes().startswith(complete))
        resumed = self.store.recover()
        self.assertTrue(resumed["recovery_required"])
        self.assertEqual(resumed["state"]["status"], "pending")
        self.assertEqual(self.store.repair_torn_tail()["status"], "no_repair_needed")

    def test_tail_repair_refuses_complete_corruption_and_missing_newline(self):
        self.store.commit({"schema_version": 1, "status": "pending"}, "start")
        path = self.root / "journal/events.jsonl"
        complete = path.read_bytes()
        for tail in [b'{"valid":"object"}', b'{"invalid": garbage}', b'garbage', b'{"invalid": garbage}\n']:
            path.write_bytes(complete + tail)
            with self.subTest(tail=tail), self.assertRaises(HarnessError):
                self.store.repair_torn_tail()
            self.assertEqual(path.read_bytes(), complete + tail)

    def test_atomic_replace_failure_keeps_old_file(self):
        self.store.write("plain.yaml", {"schema_version": 1, "status": "pending"}, SCHEMA)
        before = (self.root / "plain.yaml").read_bytes()
        with patch("agent_harness.supervisor_store.os.replace", side_effect=OSError("power loss")):
            with self.assertRaises(OSError):
                self.store.write("plain.yaml", {"schema_version": 1, "status": "accepted"}, SCHEMA)
        self.assertEqual((self.root / "plain.yaml").read_bytes(), before)
        self.assertEqual(list(self.root.glob(".yaml-stage-*")), [])

    def test_two_processes_cannot_own_one_lease(self):
        code = "import sys;sys.path.insert(0,sys.argv[1]);from agent_harness.supervisor_store import SupervisorStore;s=SupervisorStore(sys.argv[2]);\nwith s.lease(): print('WRITER')"
        with self.store.lease():
            result = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1] / "src"), str(self.root)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"WRITER", result.stdout)
        self.assertIn(b"busy", result.stderr)

    def test_traversal_symlink_fifo_and_no_outside_creation(self):
        for path in ["../outside.yaml", "/outside.yaml", "x:bad"]:
            with self.assertRaises(HarnessError):
                self.store.write(path, {})
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(HarnessError):
            self.store.write("linked/new/sub/file.yaml", {})
        self.assertEqual(list(outside.iterdir()), [])
        if hasattr(os, "mkfifo"):
            fifo = self.root / "fifo.yaml"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                self.store.read("fifo.yaml")


if __name__ == "__main__":
    unittest.main()
