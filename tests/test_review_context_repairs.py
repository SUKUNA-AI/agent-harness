"""Expected-safe regressions for independent review of context/native delivery."""
import hashlib
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_harness.checkpoint_index import CheckpointIndex
from agent_harness.core import HarnessError, digest, now
from agent_harness.execution_ledger import repository_identity
from agent_harness.memory import Memory
from agent_harness.native_observability import OBSERVER_V1_SCHEMA, observe, status
from agent_harness.native_session import current_provenance
from agent_harness.strict_yaml import dump, load


def legacy_v1_record(root, hook_cwd):
    """Actual v1 wire shape/formula: no v2 namespace epoch or envelope fields."""
    project = repository_identity(root)
    checkout = digest({"root": str(root)})
    payload = {"event": "PostModelRequest", "event_id": "event-one", "session_id": "runtime",
        "parent_session_id": None, "agent_id": None, "agent_type": "root", "turn_id": None,
        "request_id": "request-one", "attempt_id": "attempt-one", "context_epoch": 0,
        "session_cwd": str(root), "hook_process_cwd": str(hook_cwd), "tool_requested_cwd": None,
        "observed_checkout_ref": checkout,
        "namespace": digest({"session": "runtime", "agent": None, "root_tag": "root", "checkout": checkout}),
        "resolution_status": "observed", "outcome": "completed", "usage": {"input_tokens": 4},
        "usage_binding": "stable_attempt", "acceptance": "not_performed"}
    record_id = "observed-" + digest({"project": project, "session": "runtime", "agent": None, "event_id": "event-one"})[:32]
    return {"schema_version": 1, "record_type": "native_observation", "record_id": record_id,
            "project_id": project, "producer_kind": "native_hook_observer", "generation": 1,
            "created_at": now(), "payload_digest": digest(payload), **payload}


class ContextReviewRepairs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        self.state = self.base / "state"
        self.project = repository_identity(self.root)
        self.memory = Memory(self.state / "memory", self.project, enabled=True)

    def test_compact_verified_facts_keep_authoritative_digest_for_typed_show(self):
        stored = {}
        for i in range(3):
            name = "file_" + str(i) + ".py"
            (self.root / name).write_text("value=" + str(i) + "\n")
            row = self.memory.record_file_fact(self.root, name)
            stored[row["id"]] = row
        recalled = self.memory.recall("file", current_provenance(self.root), max_bytes=2048)
        self.assertTrue(recalled["verified_facts"])
        self.assertTrue(all(row["excerpt_omitted"] for row in recalled["verified_facts"]))
        for row in recalled["verified_facts"]:
            authoritative = stored[row["id"]]
            self.assertEqual(row["record_digest"], digest(authoritative))
            self.assertEqual(row["projection_digest"], digest(row["record"]))
            reference = {"project_id": self.project, "kind": "fact", "id": row["id"],
                         "generation": 1, "content_digest": row["record_digest"]}
            shown = self.memory.show(row["id"], reference=reference)
            self.assertTrue(shown["content"])
            with self.assertRaisesRegex(HarnessError, "STALE_REFERENCE"):
                self.memory.show(row["id"], reference={**reference, "content_digest": digest("genuine mutation")})

    def test_ignored_inventory_entries_obey_deadline_and_entry_cap(self):
        self.memory.update_short_term({"schema_version": 1, "id": "own", "repository_identity": self.project,
            "goal": "Inspect", "constraints": [], "checkpoint": "Observe", "active_task": "Inspect", "failures": [],
            "important_files": [], "next_step": "Review", "evidence_ids": [], "session_id": None,
            "attempts_used": 0, "tokens_reported": None, "updated_at": now(), "provenance": current_provenance(self.root)})
        index = CheckpointIndex(self.memory.root, self.project, limits={"max_inventory_entries": 1})
        for expired in (True, False):
            consumed = []
            class Entries:
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    return False
                def __iter__(self):
                    for i in range(10000):
                        consumed.append(i)
                        yield SimpleNamespace(name="ignored-" + str(i) + ".txt")
            metrics = {"inventory_entries_examined": 0}
            with self.subTest(expired=expired), patch("agent_harness.checkpoint_index.os.scandir", return_value=Entries()):
                inventory, complete = index._inventory(time.monotonic() - 1 if expired else time.monotonic() + 60, metrics)
            self.assertFalse(complete)
            self.assertEqual(inventory, {})
            self.assertLessEqual(len(consumed), 1)
            self.assertEqual(metrics["inventory_entries_examined"], 0 if expired else 1)

    def test_real_v1_callback_replay_is_idempotent_without_rewriting(self):
        hook_cwd = self.base
        record = legacy_v1_record(self.root, hook_cwd)
        directory = self.state / "native-observations" / self.project
        directory.mkdir(parents=True)
        target = directory / (record["record_id"] + ".yaml")
        dump(target, record, OBSERVER_V1_SCHEMA)
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        event = {"hook_event_name": "PostModelRequest", "event_id": "event-one", "session_id": "runtime",
                 "agent_type": "root", "request_id": "request-one", "attempt_id": "attempt-one", "cwd": str(self.root),
                 "outcome": "completed", "usage": {"input_tokens": 4}}
        replay = observe(self.state, event, hook_process_cwd=hook_cwd)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)
        self.assertEqual(load(target)["schema_version"], 1)
        self.assertEqual(status(self.state, self.root)["receipt_identity_coverage"]["legacy_unknown"], 1)
        for changed in ({"usage": {"input_tokens": 5}}, {"context_epoch": 1}, {"outcome": "failed"}):
            with self.subTest(changed=changed), self.assertRaisesRegex(HarnessError, "NATIVE_EVENT_ID_CONFLICT"):
                observe(self.state, {**event, **changed}, hook_process_cwd=hook_cwd)
