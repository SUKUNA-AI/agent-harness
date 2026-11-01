"""Real-process lock ownership and recovery; pipe handshakes define the schedule."""
from __future__ import annotations

from pathlib import Path
import errno
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))
from agent_harness.core import HarnessError, atomic_json, lock, process_identity, read_json


# Separate interpreters avoid inherited locks and work with the same protocol on
# Windows and POSIX. Timeouts only bound a broken test; no sleeps select a winner.
WORKER = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_harness import core
path, mode = Path(sys.argv[2]), sys.argv[3]
def report(phase, **fields):
    print(json.dumps({"phase": phase, **fields}), flush=True)
if mode == "paused-recovery":
    original_read = core.read_json
    paused = False
    def read_lease(value):
        global paused
        result = original_read(value)
        if Path(value) == path and not paused:
            paused = True
            report("inspected", record=result)
            assert sys.stdin.readline().strip() == "continue"
        return result
    core.read_json = read_lease
try:
    with core.lock(path, purpose="fixture " + mode) as record:
        report("entered", record=record)
        command = sys.stdin.readline().strip()
        if command == "crash":
            os._exit(23)
        assert command == "release", command
    report("released")
except core.HarnessError as exc:
    report("busy", error=str(exc))
"""


class LockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-lock-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "resource.lock"

    def start(self, mode="hold"):
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", WORKER,
                                    str(SOURCE), str(self.path), mode],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8")
        messages = queue.Queue()

        def receive():
            for line in process.stdout:
                messages.put(line)
            messages.put(None)

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        self.addCleanup(self.stop, process, reader)
        return process, messages

    @staticmethod
    def stop(process, reader):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        reader.join(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def message(self, child):
        process, messages = child
        try:
            line = messages.get(timeout=15)
        except queue.Empty:
            self.fail("Worker did not reach the next pipe handshake")
        if line is None:
            self.fail("Worker exited without a receipt: " + process.stderr.read())
        return json.loads(line)

    @staticmethod
    def send(child, command):
        child[0].stdin.write(command + "\n")
        child[0].stdin.flush()

    def finish(self, child, expected=0):
        self.assertEqual(expected, child[0].wait(timeout=15), child[0].stderr.read())

    def crashed_lease(self):
        owner = self.start()
        receipt = self.message(owner)
        self.assertEqual("entered", receipt["phase"])
        self.send(owner, "crash")
        self.finish(owner, expected=23)
        self.assertEqual(receipt["record"], read_json(self.path))
        self.assertIsNone(process_identity(receipt["record"]["identity"]["pid"]))
        return receipt["record"]

    def test_live_owner_excludes_another_process_and_releases_normally(self):
        owner = self.start()
        entered = self.message(owner)
        self.assertEqual("entered", entered["phase"])
        contender = self.start()
        denied = self.message(contender)
        self.assertEqual("busy", denied["phase"])
        self.assertIn("Resource busy", denied["error"])
        self.finish(contender)
        self.assertEqual(entered["record"], read_json(self.path))
        self.send(owner, "release")
        self.assertEqual("released", self.message(owner)["phase"])
        self.finish(owner)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.path.with_name(self.path.name + ".guard").is_file())
        with lock(self.path) as fresh:
            self.assertNotEqual(entered["record"]["token"], fresh["token"])

    def test_crashed_owner_releases_os_guard_and_preserves_stale_receipt(self):
        old = self.crashed_lease()
        with lock(self.path) as fresh:
            self.assertNotEqual(old["token"], fresh["token"])
            self.assertEqual(fresh, read_json(self.path))
        archived = list(self.path.parent.glob("resource.lock.stale-*.json"))
        self.assertEqual(1, len(archived))
        self.assertEqual(old, read_json(archived[0]))
        self.assertFalse(self.path.exists())

    def test_two_stale_recoverers_cannot_both_enter(self):
        old = self.crashed_lease()
        slow = self.start("paused-recovery")
        observed = self.message(slow)
        self.assertEqual(("inspected", old), (observed["phase"], observed["record"]))
        fast = self.start()
        fast_receipt = self.message(fast)
        # Keep any successful fast owner inside its section until the slow
        # recoverer resumes. The old replace-after-read race admitted both.
        self.send(slow, "continue")
        slow_receipt = self.message(slow)
        self.assertEqual("entered", slow_receipt["phase"])
        self.assertEqual(1, sum(r["phase"] == "entered" for r in (fast_receipt, slow_receipt)),
                         "Two recoverers acquired one critical section")
        self.assertEqual("busy", fast_receipt["phase"])
        self.finish(fast)
        self.assertEqual(slow_receipt["record"], read_json(self.path))
        self.send(slow, "release")
        self.assertEqual("released", self.message(slow)["phase"])
        self.finish(slow)
        archived = list(self.path.parent.glob("resource.lock.stale-*.json"))
        self.assertEqual([old], [read_json(p) for p in archived])

    def test_live_legacy_json_lease_is_not_reclaimed(self):
        old = {"identity": process_identity(os.getpid()), "token": "legacy", "purpose": "legacy owner"}
        self.assertIsNotNone(old["identity"])
        atomic_json(self.path, old, exclusive=True)
        with self.assertRaisesRegex(HarnessError, "Resource busy"):
            with lock(self.path):
                self.fail("Live legacy owner was replaced")
        self.assertEqual(old, read_json(self.path))
        self.assertEqual([], list(self.path.parent.glob("*.stale-*.json")))

    def test_unknown_or_unreadable_lease_fails_closed_and_releases_guard(self):
        for contents in ('{', '[]', '{}', '{"identity":{"pid":"wrong","created":"1"}}'):
            with self.subTest(contents=contents):
                self.path.write_text(contents, encoding="utf-8")
                with self.assertRaises(HarnessError):
                    with lock(self.path):
                        self.fail("Unproven stale owner was reclaimed")
                self.assertEqual(contents, self.path.read_text(encoding="utf-8"))
                self.path.unlink()  # Only the test's own malformed fixture.
                with lock(self.path):
                    pass
        self.assertEqual([], list(self.path.parent.glob("*.stale-*.json")))

    def test_exception_releases_lease_and_guard(self):
        with self.assertRaisesRegex(RuntimeError, "fixture error"):
            with lock(self.path):
                raise RuntimeError("fixture error")
        self.assertFalse(self.path.exists())
        with lock(self.path):
            pass

    def test_os_lock_failure_never_falls_back_to_unprotected_lease(self):
        target = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        with patch(target, side_effect=OSError(errno.ENOSYS, "fixture unsupported locking")):
            with self.assertRaisesRegex(HarnessError, "Cannot acquire OS lock"):
                with lock(self.path):
                    self.fail("OS locking failure was ignored")
        self.assertFalse(self.path.exists())
        with lock(self.path):
            pass


if __name__ == "__main__":
    unittest.main()
