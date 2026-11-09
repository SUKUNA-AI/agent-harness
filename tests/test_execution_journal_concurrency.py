"""A legitimate in-process append must not race a strict receipt/journal read."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.file_io import ParentDirectory
from agent_harness.supervisor_store import SupervisorStore


class JournalConcurrencyTests(unittest.TestCase):
    def barrier_read(self, read, append, mutex):
        """Pause after leaf stat; append before open iff the reader is unlocked.

        The writer's nonblocking lock result determines the schedule. No sleep
        or probabilistic timing decides whether the unsafe append occurs.
        """
        before_open, release_reader, writer_probed = (threading.Event() for _ in range(3))
        errors, results, writer_lock, opens = [], [], [], []
        original = ParentDirectory.open
        def guarded(parent, *args, **kwargs):
            if parent.path.name == "events.jsonl" and threading.current_thread().name == "journal-reader":
                opens.append(True)
                before_open.set()
                if not release_reader.wait(10):
                    raise AssertionError("reader barrier not released")
            return original(parent, *args, **kwargs)
        def reader():
            try:
                results.append(read())
            except BaseException as error:
                errors.append(error)
        def writer():
            acquired = mutex.acquire(blocking=False)
            writer_lock.append(acquired)
            try:
                if acquired:
                    # Baseline: complete the actual append while leaf stat is
                    # old, then let the strict reader open the changed file.
                    append()
                    writer_probed.set()
                else:
                    writer_probed.set()
                    append()  # Must wait until the intact read finishes.
            except BaseException as error:
                errors.append(error)
                writer_probed.set()
            finally:
                if acquired:
                    mutex.release()
        reading = threading.Thread(target=reader, name="journal-reader")
        writing = threading.Thread(target=writer, name="journal-writer")
        with patch.object(ParentDirectory, "open", guarded):
            try:
                reading.start()
                self.assertTrue(before_open.wait(10), "reader never reached strict leaf open")
                writing.start()
                self.assertTrue(writer_probed.wait(10), "writer never reached lock probe")
            finally:
                release_reader.set()
                reading.join(10)
                if writing.ident is not None:
                    writing.join(10)
        self.assertFalse(reading.is_alive())
        self.assertFalse(writing.is_alive())
        self.assertEqual(writer_lock, [False], "append interleaved with an authoritative read")
        self.assertEqual(errors, [])
        self.assertEqual(opens, [True], "strict freshness must not be weakened by read retries")
        return results[0]

    def test_store_events_serializes_fresh_read_and_append(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SupervisorStore(Path(temp) / "store")
            store.append_event({"kind": "fixture", "value": 1})
            first = self.barrier_read(store.events,
                lambda: store.append_event({"kind": "fixture", "value": 2}), store._mutex)
            self.assertEqual(len(first), 1)
            self.assertEqual([r["payload"]["value"] for r in store.events()], [1, 2])

    def test_receipt_snapshot_serializes_completion_publication(self):
        from test_execution_durability import ExecutionDurabilityTests
        with tempfile.TemporaryDirectory() as temp:
            supervisor, _ = ExecutionDurabilityTests().fixture(Path(temp))
            receipts = supervisor.receipts()
            intent = receipts.intent("left", "attempt-1", checkout=supervisor.state["cwd"], owner="fixture")
            first = self.barrier_read(receipts.records,
                lambda: receipts.completion(intent, {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 3}}),
                supervisor.mutex)
            self.assertEqual(len(first), 1)
            receipts.reconcile_usage()
            self.assertEqual(len(receipts.records()), 2)
            self.assertEqual((supervisor.state["metrics"]["input_tokens"], supervisor.state["metrics"]["output_tokens"]), (10, 3))
