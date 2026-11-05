import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import context, file_io
from agent_harness.core import HarnessError


class PacketReaderSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.packet = self.base / "packet.json"

    def test_regular_packet_exact_byte_boundary_and_oversized_input(self):
        self.packet.write_bytes(b'"' + b"x" * 62 + b'"')
        with patch.object(context, "MAX_PACKET_BYTES", 64):
            self.assertEqual("x" * 62, context.read_packet(self.packet))
            self.packet.write_bytes(b'"' + b"x" * 63 + b'"')
            with self.assertRaisesRegex(HarnessError, "byte limit"):
                context.read_packet(self.packet)

    def test_packet_symlink_directory_missing_and_invalid_json_fail_closed(self):
        with self.assertRaises(HarnessError):
            context.read_packet(self.base)
        with self.assertRaises(FileNotFoundError):
            context.read_packet(self.packet)
        self.packet.write_bytes(b"not json")
        with self.assertRaises(json.JSONDecodeError):
            context.read_packet(self.packet)
        link = self.base / "linked.json"
        try:
            link.symlink_to(self.packet)
        except OSError:
            self.skipTest("Symlink privilege unavailable")
        with self.assertRaisesRegex(HarnessError, "symlink"):
            context.read_packet(link)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support")
    def test_packet_fifo_and_regular_to_fifo_open_race_return_without_writer(self):
        # Use a process deadline so a regression cannot hang the test runner.
        script = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[3])
from agent_harness import context, file_io
from agent_harness.core import HarnessError
path, raced = Path(sys.argv[1]), sys.argv[2] == "race"
if raced: path.write_bytes(b"{}")
else: os.mkfifo(path)
original = file_io.os.open
def opening(value, flags, *args, **kwargs):
    if Path(value).name == path.name and not flags & getattr(os, "O_DIRECTORY", 0) and raced:
        path.unlink(); os.mkfifo(path)
    return original(value, flags, *args, **kwargs)
try:
    with patch.object(file_io.os, "open", opening): context.read_packet(path)
except HarnessError as error:
    print(str(error)); raise SystemExit(0)
raise SystemExit(3)
'''
        for mode in ("fifo", "race"):
            path = self.base / mode
            result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(path), mode,
                                     str(Path(context.__file__).resolve().parents[1])],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("regular file", result.stdout)

    def test_packet_reader_rejects_source_mutation_during_bounded_read(self):
        self.packet.write_bytes(b'{"a":1}')
        read = file_io.os.read
        changed = False
        def editing(fd, size):
            nonlocal changed
            value = read(fd, size)
            if not changed:
                changed = True
                self.packet.write_bytes(b'{"a":2}')
            return value
        with patch.object(file_io.os, "read", editing), self.assertRaisesRegex(HarnessError, "changed"):
            context.read_packet(self.packet)


if __name__ == "__main__":
    unittest.main()
