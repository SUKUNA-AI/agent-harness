"""Windows JSON path costs exercised as data, with real immutable source reads."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.context_reducer import reduce, retrieve_recordset
from agent_harness.core import HarnessError, canonical, digest


class NativeWindowsBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "recordsets"
        self.project = "a" * 64
        self.session = "native-" + "b" * 32
        profile = r"T:\Profiles\testuser\AppData\Local\Temp\tmp_fixture"
        self.root = profile + r"\project"
        directory = profile + "\\state\\native-sessions\\" + self.session
        self.packet_path = directory + "\\packet-" + "c" * 32 + ".json"
        self.required = {"session": self.session, "root": self.root, "repository_identity": self.project,
            "snapshot_id": "d" * 64, "memory": True, "ledger": "inform", "no_model_calls": True,
            "acceptance": "not_performed", "raw_pointer": directory + "\\retrieval-" + "e" * 32 + ".json",
            "packet_pointer": self.packet_path, "stale_memory_count": 0, "stale_ledger_count": 0,
            "memory_omitted_records": 0, "ledger_omitted_records": 0, "goal_generation": 1, "degraded_records": 0}
        self.records = [{"kind": "checkpoint", "payload": {"checkpoint": "started",
            "next_step": "Work within current task authorization", "goal_generation": 1,
            "source_validity": "current", "evidence_validity": "unknown", "verification_level": "observation"},
            "validity_status": "valid", "pointer": profile + "\\state\\memory\\" + self.project +
                "\\short_term\\native-" + str(i) * 32 + ".yaml"} for i in range(3)]

    def outer(self, packet):
        return {"session": self.session, "root": self.root, "evidence": self.packet_path, "packet": packet}

    def packet(self, records=None, *, budget=2048, cursor=None, required=None):
        overhead = len(canonical(self.outer(None))) - len(b"null")
        return reduce(self.records if records is None else records, category="handoff", max_bytes=budget - overhead,
                      required=self.required if required is None else required, recordset_store=self.store, cursor=cursor)

    def retained(self, reference):
        parts, offset = [], 0
        for _ in range(100):
            page = retrieve_recordset(self.store, reference, offset=offset, limit=1024)
            self.assertLessEqual(len(canonical(page)) + 2, 1024)
            parts.append(page["content"])
            if page["next_offset"] is None:
                return json.loads("".join(parts))
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.fail("Immutable source pagination did not terminate")

    def test_small_outer_budget_explicitly_reads_source_without_repeating_empty_cursor(self):
        packet = self.packet()
        self.assertLessEqual(len(canonical(self.outer(packet))) + 2, 2048)
        self.assertEqual(packet["required"], self.required)
        self.assertIsNone(packet["next_cursor"])
        self.assertTrue(packet["continuation_requires_source_read"])
        self.assertTrue(packet["omission_pointers"])
        self.assertTrue(all("recordset_index" in pointer for pointer in packet["omission_pointers"]))
        source = self.retained(packet["recordset_ref"])
        self.assertEqual(source["records"], self.records)
        self.assertEqual(digest(source["records"]), packet["recordset_digest"])
        for pointer in packet["omission_pointers"]:
            self.assertIn(source["records"][pointer["recordset_index"]], self.records)

    def test_source_read_marker_is_not_dropped_when_root_grows(self):
        original_root = self.root
        for extra in (16, 24):
            self.root = original_root + "x" * extra
            self.required["root"] = self.root
            narrowed = self.packet()
            self.assertIsNone(narrowed["next_cursor"])
            self.assertTrue(narrowed["continuation_requires_source_read"])
            self.assertLessEqual(len(canonical(self.outer(narrowed))) + 2, 2048)
            self.assertEqual(self.retained(narrowed["recordset_ref"])["records"], self.records)

    def test_nonempty_compact_pages_progress_and_reject_changed_inputs(self):
        records = [{"kind": "observation", "payload": {"index": i}, "pointer": "source-row"} for i in range(20)]
        packet = self.packet(records, budget=2200)
        first_cursor = packet["next_cursor"]
        self.assertEqual(set(first_cursor), {"binding", "position"})
        seen = []
        for _ in range(len(records)):
            self.assertTrue(packet["items"])
            self.assertLessEqual(len(canonical(self.outer(packet))) + 2, 2200)
            seen.extend(item["observation"]["payload"]["index"] for item in packet["items"])
            cursor = packet["next_cursor"]
            if cursor is None:
                break
            previous_position = cursor["position"]
            packet = self.packet(records, budget=2200, cursor=cursor)
            if packet["next_cursor"] is not None:
                self.assertGreater(packet["next_cursor"]["position"], previous_position)
        self.assertEqual(seen, list(range(20)))
        changed = deepcopy(records)
        changed[0]["payload"]["index"] = 99
        with self.assertRaisesRegex(HarnessError, "STALE_CURSOR"):
            self.packet(changed, budget=2200, cursor=first_cursor)
        with self.assertRaisesRegex(HarnessError, "STALE_CURSOR"):
            self.packet(records, budget=2200, cursor=first_cursor, required={**self.required, "goal_generation": 2})

    def test_budget_sweep_never_changes_required_or_retained_inputs(self):
        reference = None
        for budget in (2048, 2049, 2112, 2304, 3072, 4096):
            packet = self.packet(budget=budget)
            self.assertLessEqual(len(canonical(self.outer(packet))) + 2, budget)
            self.assertEqual(packet["required"], self.required)
            reference = reference or packet["recordset_ref"]
            self.assertEqual(packet["recordset_ref"], reference)
        self.assertEqual(self.retained(reference)["records"], self.records)
        self.assertEqual(len(list(self.store.glob("*.yaml"))), 1)

    def test_small_memory_claim_survives_summary_large_claim_stays_retrievable(self):
        for claim in ("Known fixture decision", "界😀\\" * 512):
            memory = {"kind": "memory", "payload": {"id": "known-fact", "record": {"claim": claim,
                "provenance_details": "source binding " * 300}}, "pointer": self.required["raw_pointer"]}
            packet = self.packet([self.records[0], memory], budget=4096)
            self.assertLessEqual(len(canonical(self.outer(packet))) + 2, 4096)
            if claim == "Known fixture decision":
                self.assertIn(claim, json.dumps(packet))
            else:
                self.assertNotIn(claim[:20], json.dumps(packet, ensure_ascii=False))
            self.assertEqual(self.retained(packet["recordset_ref"])["records"][1]["payload"]["record"]["claim"], claim)
