from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.context import dispatch, make_packet, read_packet, verify_packet
from agent_harness.core import HarnessError, digest


class SourcePacketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        (self.root / "main.py").write_text("value = 'тест'\n", encoding="utf-8")

    def test_source_changes_and_missing_files_are_stale(self):
        packet = make_packet(self.root, ["main.py"])
        self.assertEqual("current", verify_packet(self.root, packet)["status"])
        (self.root / "main.py").write_text("value = 'edit'\n", encoding="utf-8")
        self.assertEqual("stale", verify_packet(self.root, packet)["status"])
        (self.root / "main.py").unlink()
        self.assertEqual("source_unavailable", verify_packet(self.root, packet)["errors"][0]["reason"])

    def test_explicit_limits_never_claim_complete_for_omitted_files(self):
        (self.root / "large.txt").write_text("X" * 100, encoding="utf-8")
        packet = make_packet(self.root, ["main.py", "large.txt"], max_file_bytes=50)
        self.assertFalse(packet["complete"])
        self.assertEqual(["main.py"], [item["path"] for item in packet["files"]])
        self.assertEqual("byte_limit", packet["omitted"][0]["reason"])
        self.assertFalse(verify_packet(self.root, packet)["complete"])
        packet = make_packet(self.root, ["main.py"], max_bytes=1)
        self.assertEqual([], packet["files"])
        self.assertFalse(packet["complete"])

    def test_traversal_credentials_and_generated_paths_rejected(self):
        for name in ("../outside", "sub/../../outside", "/outside", "sub\\file", ".env", ".env.production", "auth.json", ".git/config", "node_modules/package/file", "private.key"):
            with self.subTest(name=name), self.assertRaises(HarnessError):
                make_packet(self.root, [name])

    def test_symlink_to_outside_is_not_followed(self):
        (self.base / "outside.txt").write_text("private data", encoding="utf-8")
        try:
            (self.root / "link.txt").symlink_to(self.base / "outside.txt")
        except OSError:
            self.skipTest("Symlink creation unavailable")
        with self.assertRaisesRegex(HarnessError, "symlink"):
            make_packet(self.root, ["link.txt"])

    def test_binary_and_invalid_utf8_are_omitted(self):
        (self.root / "binary").write_bytes(b"abc\x00def")
        (self.root / "encoded").write_bytes(b"\xff\xfe")
        packet = make_packet(self.root, ["binary", "encoded"])
        self.assertEqual(["binary", "not_utf8"], [item["reason"] for item in packet["omitted"]])
        self.assertEqual([], packet["files"])

    def test_embedded_credential_fails_before_output(self):
        (self.root / "main.py").write_text("key = '" + "ghp_" + "A" * 40 + "'", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "Possible credential"):
            make_packet(self.root, ["main.py"])

    def test_tampered_packet_and_duplicate_records_rejected(self):
        packet = make_packet(self.root, ["main.py"])
        bad = deepcopy(packet)
        bad["files"][0]["content"] = "changed"
        with self.assertRaisesRegex(HarnessError, "integrity"):
            verify_packet(self.root, bad)
        bad = deepcopy(packet)
        bad["files"].append(deepcopy(bad["files"][0]))
        bad["digest"] = digest({k: v for k, v in bad.items() if k != "digest"})
        with self.assertRaisesRegex(HarnessError, "Duplicate"):
            verify_packet(self.root, bad)

    def test_root_binding_prevents_accidental_cross_project_reuse(self):
        packet = make_packet(self.root, ["main.py"])
        other = self.base / "another-project"
        other.mkdir()
        with self.assertRaisesRegex(HarnessError, "another source root"):
            verify_packet(other, packet)

    def test_cli_roundtrip_and_existing_output_preserved(self):
        output = self.base / "context.json"
        args = ["pack", "--root", str(self.root), "--include", "main.py", "--output", str(output)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(0, dispatch(args))
            before = output.read_bytes()
            self.assertEqual(2, dispatch(args))
            self.assertEqual(before, output.read_bytes())
            self.assertEqual(0, dispatch(["verify", "--root", str(self.root), "--packet", str(output)]))
        self.assertTrue(read_packet(output)["complete"])
        self.assertEqual(["main.py"], [p.name for p in self.root.iterdir()])

    def test_source_tree_cannot_receive_private_packet(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(2, dispatch(["pack", "--root", str(self.root), "--include", "main.py", "--output", str(self.root / "context.json")]))
        self.assertFalse((self.root / "context.json").exists())

    def test_json_escaping_cannot_create_an_unreadable_packet(self):
        names = [f"source-{index}.py" for index in range(4)]
        for name in names:
            (self.root / name).write_text("#" + "\x01" * 262142 + "\n", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "Serialized packet"):
            make_packet(self.root, names, max_file_bytes=262144, max_bytes=1048576)

    def test_parent_swap_before_read_cannot_capture_outside_file(self):
        import agent_harness.context as context
        parent = self.root / "sub"
        parent.mkdir()
        (parent / "module.py").write_text("inside", encoding="utf-8")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "module.py").write_text("outside-private-fixture", encoding="utf-8")
        probe = self.base / "probe-link"
        try:
            probe.symlink_to(outside, target_is_directory=True)
            probe.unlink()
        except OSError:
            self.skipTest("Directory symlink creation unavailable")
        original = context._read

        def swap(path, limit, **kwargs):
            parent.rename(self.root / "original-sub")
            parent.symlink_to(outside, target_is_directory=True)
            return original(path, limit, **kwargs)

        with patch.object(context, "_read", side_effect=swap), self.assertRaises(HarnessError):
            make_packet(self.root, ["sub/module.py"])


if __name__ == "__main__":
    unittest.main()
