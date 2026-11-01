from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_harness.context import dispatch, excerpt_source, MAX_EXCERPT_FILE_BYTES
from agent_harness.core import HarnessError


class ContextReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_located_lines_preserve_crlf_and_bind_to_full_file(self):
        path = self.root / "log.txt"
        path.write_bytes(b"first\r\nERROR: target\r\nlast\r\n")
        before = path.read_bytes()
        result = excerpt_source(self.root, "log.txt", start_line=2, lines=1)
        self.assertEqual("ERROR: target\r\n", result["content"])
        self.assertEqual(3, result["total_lines"])
        self.assertTrue(result["selection_complete"])
        self.assertFalse(result["file_complete"])
        self.assertEqual(before, path.read_bytes())
        path.write_bytes(b"changed outside view\r\nERROR: target\r\nlast\r\n")
        with self.assertRaisesRegex(HarnessError, "hash changed"):
            excerpt_source(self.root, "log.txt", start_line=2, lines=1,
                           expect_sha256=result["sha256"])

    def test_long_unicode_line_pages_without_loss_and_honors_encoded_bound(self):
        content = ('\"\\\tтест😀\x01' * 1500) + "\nend\n"
        (self.root / "long.txt").write_bytes(content.encode("utf-8"))
        reconstructed = []
        offset, expected_hash = 0, None
        while offset is not None:
            result = excerpt_source(self.root, "long.txt", offset=offset,
                                    max_bytes=1024, expect_sha256=expected_hash)
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            self.assertLessEqual(len(encoded), 1024)
            self.assertGreater(result["end_offset"], offset)
            self.assertTrue(result["content"])
            if not reconstructed:
                self.assertTrue(result["truncated"])
                self.assertFalse(result["selection_complete"])
            reconstructed.append(result["content"])
            expected_hash, offset = result["sha256"], result["next_offset"]
        self.assertEqual(content, "".join(reconstructed))

    def test_offset_inside_unicode_character_is_rejected(self):
        (self.root / "unicode.txt").write_bytes("A😀B".encode("utf-8"))
        with self.assertRaisesRegex(HarnessError, "UTF-8 boundary"):
            excerpt_source(self.root, "unicode.txt", offset=2)
        self.assertEqual("😀B", excerpt_source(self.root, "unicode.txt", offset=1)["content"])

    def test_empty_and_eof_are_explicit(self):
        (self.root / "empty.txt").write_bytes(b"")
        result = excerpt_source(self.root, "empty.txt")
        self.assertEqual(0, result["total_lines"])
        self.assertTrue(result["file_complete"])
        self.assertIsNone(result["next_offset"])
        (self.root / "one.txt").write_bytes(b"one")
        self.assertEqual("", excerpt_source(self.root, "one.txt", start_line=2)["content"])
        with self.assertRaisesRegex(HarnessError, "beyond"):
            excerpt_source(self.root, "one.txt", start_line=3)

    def test_binary_credentials_symlink_and_traversal_are_rejected(self):
        for name, data in [("binary.txt", b"a\x00b"), ("bad.txt", b"\xff"),
                           ("secret.txt", ("ghp_" + "A" * 40).encode("ascii"))]:
            (self.root / name).write_bytes(data)
            with self.subTest(name=name), self.assertRaises(HarnessError):
                excerpt_source(self.root, name)
        for name in ("../outside", ".env", "node_modules/file"):
            with self.subTest(name=name), self.assertRaises(HarnessError):
                excerpt_source(self.root, name)
        try:
            (self.root / "link.txt").symlink_to(self.root / "binary.txt")
        except OSError:
            return  # Windows without symlink privilege; packet suite covers it.
        with self.assertRaisesRegex(HarnessError, "symlink"):
            excerpt_source(self.root, "link.txt")

    def test_limits_are_validated_and_large_input_is_not_loaded(self):
        path = self.root / "large.txt"
        with path.open("wb") as target:
            target.truncate(MAX_EXCERPT_FILE_BYTES + 1)
        with self.assertRaisesRegex(HarnessError, "16 MiB"):
            excerpt_source(self.root, "large.txt")
        for kwargs in ({"lines": 0}, {"lines": True}, {"offset": True},
                       {"max_bytes": 100}, {"start_line": 0},
                       {"start_line": 2, "offset": 0}, {"expect_sha256": "invalid"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(HarnessError):
                excerpt_source(self.root, "large.txt", **kwargs)

    def test_cli_budget_and_hash_mismatch_never_return_partial_source(self):
        (self.root / "long.txt").write_bytes(b"line\n" * 1000)
        args = ["read", "--root", str(self.root), "--file", "long.txt",
                "--lines", "1000", "--max-bytes", "1024"]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(0, dispatch(args))
        self.assertLessEqual(len(out.getvalue().encode("utf-8")), 1024)
        self.assertTrue(json.loads(out.getvalue())["truncated"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(2, dispatch(args + ["--expect-sha256", "0" * 64]))
        self.assertEqual("", out.getvalue())
        self.assertIn("hash changed", err.getvalue())

    def test_cli_budget_includes_windows_crlf_translation(self):
        (self.root / "long.txt").write_bytes(b"X" * 5000)
        binary = io.BytesIO()
        stream = io.TextIOWrapper(binary, encoding="utf-8", newline="\r\n")
        with redirect_stdout(stream), redirect_stderr(io.StringIO()):
            self.assertEqual(0, dispatch(["read", "--root", str(self.root), "--file", "long.txt", "--max-bytes", "1024"]))
        stream.flush()
        raw = binary.getvalue()
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertLessEqual(len(raw), 1024)
        self.assertTrue(json.loads(raw)["truncated"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO only")
    def test_regular_source_replaced_with_fifo_fails_without_waiting(self):
        import subprocess
        import sys
        script = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from agent_harness import context
from agent_harness.core import HarnessError
root = Path(sys.argv[2])
p = root / "fifo"
p.write_bytes(b"regular")
original = context._open_source
def swapped(root, name):
    p.unlink()
    os.mkfifo(p)
    return original(root, name)
with patch.object(context, "_open_source", side_effect=swapped):
    try:
        context.excerpt_source(root, "fifo")
    except HarnessError:
        pass
    else:
        raise SystemExit("FIFO accepted")
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                                 str(Path(__file__).resolve().parents[1] / "src"), str(self.root)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
