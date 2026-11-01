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
