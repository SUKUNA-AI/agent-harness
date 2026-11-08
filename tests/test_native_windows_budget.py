"""Windows JSON path costs exercised as data, with real immutable source reads."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_harness import native_session as session
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


class LongNativePathTests(unittest.TestCase):
    """Real long private paths, transport, checkpoint and content-addressed reads."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        while len(str(self.base)) < 190:
            self.base /= "long-native-profile"
        self.base.mkdir(parents=True)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "input.txt").write_text("fixture\n", encoding="utf-8")
        self.state = self.base / "state"

    def start(self, **kwargs):
        return session.session_packet(self.state, root=self.root, goal="Inspect fixture", max_bytes=2048, **kwargs)

    def test_long_temp_cli_checkpoint_and_legacy_receipt_roundtrip(self):
        self.assertGreaterEqual(len(str(self.base)), 190)
        tool = Path(__file__).resolve().parents[1] / "tools/session.py"
        started = subprocess.run([sys.executable, str(tool), "--state", str(self.state), "--max-bytes", "2048",
            "start", "--root", str(self.root), "--goal", "Inspect fixture"], capture_output=True, timeout=30)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertLessEqual(len(started.stdout) + 1, 2048)
        outer = json.loads(started.stdout)
        self.assertEqual(set(outer), {"packet"})
        packet = outer["packet"]
        required = packet["required"]
        self.assertEqual(required["path_encoding"], "base_relative_v1")
        self.assertEqual(session.packet_path(packet, "root"), self.root)
        self.assertEqual(json.loads(session.packet_path(packet, "packet_pointer").read_bytes()), packet)
        raw = json.loads(session.packet_path(packet, "raw_pointer").read_bytes())
        self.assertEqual(raw["provenance"]["snapshot_id"], required["snapshot_id"])
        self.assertEqual(required["goal"], "Inspect fixture")
        self.assertEqual(required["goal_digest"], session.hashlib.sha256(b"Inspect fixture").hexdigest())
        self.assertEqual(required["constraints"], session.SESSION_CONSTRAINTS)
        self.assertTrue(required["memory"])
        self.assertEqual(required["ledger"], "inform")
        self.assertTrue(required["no_model_calls"])
        self.assertEqual(required["acceptance"], "not_performed")
        identifier = required["session"]
        options = dict(session=identifier, note="Verified fixture", next_step="Review", operation_id="long-path-op", max_bytes=2048)
        checkpoint = session.session_packet(self.state, **options)
        self.assertEqual(session.session_packet(self.state, **options), checkpoint)
        # The persisted operation still has its original absolute packet_path
        # and digest schema; only model-facing packet addresses are compact.
        from agent_harness.strict_yaml import load
        operation = load(self.state / "native-sessions" / identifier / "operation-long-path-op.yaml")
        self.assertEqual(Path(operation["packet_path"]), session.packet_path(checkpoint, "packet_pointer"))
        self.assertEqual(operation["packet_digest"], digest(checkpoint))

    def test_long_temp_omissions_and_legacy_reference_keep_hash_binding(self):
        for _ in range(3):
            self.start()
        packet = self.start()
        self.assertLessEqual(len(canonical(session.packet_envelope(packet))) + 2, 2048)
        self.assertTrue(packet["omitted"])
        store = session.packet_path(packet, "packet_pointer").parent / "recordsets"
        reference = packet["recordset_ref"]
        self.assertNotIn("content_digest", reference)
        parts, offset = [], 0
        while True:
            page = retrieve_recordset(store, reference, offset=offset, limit=1024)
            parts.append(page["content"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        retained = json.loads("".join(parts))
        self.assertEqual(digest(retained["records"]), packet["recordset_digest"])
        legacy = {**reference, "content_digest": reference["id"][len("recordset-"):]}
        self.assertEqual(retrieve_recordset(store, legacy), retrieve_recordset(store, reference))
        with self.assertRaisesRegex(HarnessError, "STALE_REFERENCE"):
            retrieve_recordset(store, {**legacy, "content_digest": "0" * 64})
        path = store / (reference["id"] + ".yaml")
        path.write_text(path.read_text(encoding="utf-8").replace("started", "changed"), encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "STALE_REFERENCE"):
            retrieve_recordset(store, reference)

    def test_compact_resolver_rejects_traversal_and_real_required_overflow(self):
        packet = self.start()
        for path in ("../outside", "part/../../outside", r"..\outside", "/outside", r"Q:\outside", "Q:outside", r"\\server\outside"):
            changed = deepcopy(packet)
            changed["required"]["raw_pointer"] = path
            with self.subTest(path=path), self.assertRaises(HarnessError):
                session.packet_path(changed, "raw_pointer")
        identifier = "native-" + "a" * 32
        with self.assertRaisesRegex(HarnessError, "BUDGET_TOO_SMALL"):
            session.session_packet(self.state, root=self.root, session=identifier, goal="g" * 4096, max_bytes=2048)
        self.assertFalse((self.state / "native-sessions" / identifier / "session.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 addressing")
    def test_long_temp_83_alias_resolves_same_compact_addresses(self):
        import ctypes
        from ctypes import wintypes
        short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        short_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        count = short_path(str(self.base), buffer, len(buffer))
        if not count or buffer.value == str(self.base):
            self.skipTest("8.3 aliases unavailable on fixture volume")
        alias = Path(buffer.value)
        packet = session.session_packet(alias / "state", root=alias / "project", goal="Inspect fixture", max_bytes=2048)
        self.assertEqual(session.packet_path(packet, "root"), self.root)
        self.assertTrue(session.packet_path(packet, "raw_pointer").is_file())
        resumed = session.session_packet(self.state, session=packet["required"]["session"], max_bytes=2048)
        self.assertEqual(resumed["required"]["snapshot_id"], packet["required"]["snapshot_id"])
