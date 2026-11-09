"""Проверки bounded native bootstrap настоящими API в изолированном состоянии."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import native_session as session
from agent_harness.core import HarnessError, canonical, now
from agent_harness.memory import Memory
from agent_harness.context_reducer import retrieve_recordset

ROOT = Path(__file__).resolve().parents[1]


class NativeSessionTests(unittest.TestCase):
    """Изолирует source/state; модели и клиент Codex в этих тестах не запускаются."""

    def setUp(self):
        """Создаёт независимый проект и private store для каждого поведения."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Ожидания используют canonical root, включая Windows 8.3 alias каталога tempfile.
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "input.txt").write_text("first\n")
        self.state = self.base / "state"

    def start(self, **kwargs):
        """Запускает реальный adapter с общими тестовыми входами."""
        return session.session_packet(self.state, root=self.root, goal="Inspect fixture", **kwargs)

    def event(self, **kwargs):
        """Возвращает минимальное поддержанное событие, не читая transcript."""
        return {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(self.root),
                "session_id": "019-session-test", "model": "fixture", "permission_mode": "default",
                "transcript_path": None, **kwargs}

    def test_compact_packet_and_cross_process_checkpoint(self):
        """Пакет укладывается в бюджет; новый процесс обновляет настоящую short-term память."""
        packet = self.start(max_bytes=2048)
        self.assertLessEqual(len(canonical(packet)) + 2, 2048)
        required = packet["required"]
        self.assertTrue(required["memory"])
        self.assertEqual(required["ledger"], "inform")
        self.assertTrue(required["no_model_calls"])
        self.assertEqual(required["acceptance"], "not_performed")
        self.assertIsInstance(json.loads(Path(required["raw_pointer"]).read_text())["memory"], dict)
        result = subprocess.run([sys.executable, str(ROOT / "tools/session.py"), "--state", str(self.state),
            "checkpoint", "--session", required["session"], "--note", "Verified fixture", "--next", "Review"],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["session"], required["session"])
        self.assertEqual(output["root"], str(self.root))
        self.assertTrue(Path(output["evidence"]).is_file())
        self.assertLessEqual(len(result.stdout.encode()) + 1, 4096)
        second = output["packet"]
        memory = Memory(self.state / "memory", required["repository_identity"], enabled=True)
        row = memory.show(required["session"])
        self.assertIn("Verified fixture", json.dumps(row))
        self.assertEqual(second["required"]["snapshot_id"], required["snapshot_id"])

    def test_hook_ignores_private_payload_and_resume_keeps_goal(self):
        """Hook не сохраняет prompt/transcript; resume использует существующий checkpoint."""
        marker = "private-payload-marker"
        event = self.event(prompt=marker, transcript_path=marker)
        packet = session.hook_packet(self.state, event)
        identifier = packet["required"]["session"]
        session.session_packet(self.state, session=identifier, note="Task checkpoint", next_step="Continue")
        second = session.hook_packet(self.state, self.event(source="resume"))
        self.assertEqual(second["required"]["session"], identifier)
        self.assertIn("Task checkpoint", json.dumps(second))
        for path in self.state.rglob("*"):
            if path.is_file():
                self.assertNotIn(marker, path.read_text())
        other = self.base / "other"
        other.mkdir()
        separate = session.hook_packet(self.state, self.event(cwd=str(other)))
        self.assertNotEqual(separate["required"]["repository_identity"], packet["required"]["repository_identity"])

    def test_context_change_and_bounded_memory_never_prove_source_facts(self):
        """Ограниченный context не подтверждает даже неизменённый semantic fact."""
        first = self.start()
        required = first["required"]
        raw = json.loads(Path(required["raw_pointer"]).read_text())
        memory = Memory(self.state / "memory", required["repository_identity"], enabled=True)
        memory.record_semantic({"schema_version": 1, "id": "known-fact",
            "repository_identity": required["repository_identity"], "document": "decisions",
            "claim": "Known fixture decision", "source": "fixture", "actor": "test",
            "confidence": "verified", "last_checked": now(), "provenance": raw["provenance"],
            "validity_status": "valid", "relations": [], "supersedes": None})
        same = self.start()
        self.assertNotIn("Known fixture decision", json.dumps(same))
        self.assertEqual(same["required"]["stale_memory_count"], 1)
        (self.root / "AGENTS.md").write_text("Updated project instructions.\n")
        changed = self.start()
        self.assertNotEqual(changed["required"]["snapshot_id"], required["snapshot_id"])
        self.assertEqual(changed["required"]["stale_memory_count"], 1)
        self.assertNotIn("Known fixture decision", json.dumps(changed))
        self.assertTrue(changed["omitted"])

    def test_invalid_inputs_do_not_create_state(self):
        """Ошибки формы, bounds и credential guard отклоняются до записи metadata."""
        secret = "sk-" + "a" * 40
        cases = [lambda: self.start(max_bytes=1024),
                 lambda: session.session_packet(self.state, root=self.root, goal=secret),
                 lambda: session.session_packet(self.state, root=self.root, goal="g" * 4097),
                 lambda: session.session_packet(self.state, root=self.root, goal="x", session="../../escape"),
                 lambda: session.hook_packet(self.state, self.event(hook_event_name="Stop")),
                 lambda: session.hook_packet(self.state, self.event(source="unknown")),
                 lambda: session.hook_packet(self.state, self.event(session_id="../../escape")),
                 lambda: session.hook_packet(self.state, self.event(cwd=""))]
        for run in cases:
            with self.subTest(run=run), self.assertRaises((HarnessError, ValueError)):
                run()
            self.assertFalse(self.state.exists())

    def test_bootstrap_checkpoint_and_govern_do_not_enumerate_source(self):
        """Даже home-подобный root не требует source inventory для native API."""
        unrelated = self.root / "unrelated-cache"
        unrelated.mkdir()
        # A sparse giant input must never be read by bootstrap.
        with (unrelated / "large.bin").open("wb") as stream:
            stream.truncate(2**34)
        scandir = os.scandir
        def guarded(path):
            if not isinstance(path, int) and Path(path) in {self.root, unrelated}:
                raise AssertionError("Source enumeration is forbidden during native context")
            return scandir(path)
        with patch("agent_harness.core.snapshot", side_effect=HarnessError("Native snapshot failed (exit 1): inventory limit")), \
                patch("os.scandir", side_effect=guarded), patch.object(session, "write_json") as output:
            started = session.hook_packet(self.state, self.event())
            identifier = started["required"]["session"]
            resumed = session.session_packet(self.state, session=identifier, note="Observed fixture", next_step="Review")
            self.assertEqual(session.main(["--state", str(self.state), "govern", "--session", identifier,
                "--argv-json", '["rg", "fixture", "input.txt"]', "--effect", "read_only"]), 0)
        self.assertEqual(resumed["required"]["binding_kind"], "bounded_context")
        self.assertFalse(resumed["required"]["inventory_complete"])
        self.assertEqual(output.call_args.args[0]["packet"]["decision"], "requires_revalidation")
        raw = json.loads(Path(resumed["required"]["raw_pointer"]).read_text())
        self.assertFalse(raw["provenance"]["file_inventory_complete"])
        self.assertIn("source_inventory_not_collected", raw["provenance"]["context_omissions"])

    def test_context_paths_are_bounded_and_instruction_creation_invalidates(self):
        first = session.current_provenance(self.root)
        self.assertLessEqual(len(first["context_paths"]), 64)
        (self.base / "AGENTS.md").write_text("Ancestor instructions.\n")
        ancestor = session.current_provenance(self.root)
        self.assertNotEqual(first["snapshot_id"], ancestor["snapshot_id"])
        (self.root / "input.txt").write_text("Unrelated task input.\n")
        self.assertEqual(ancestor["snapshot_id"], session.current_provenance(self.root)["snapshot_id"])
        (self.root / "AGENTS.md").write_bytes(b"x" * 65537)
        incomplete = session.current_provenance(self.root)
        self.assertFalse(incomplete["context_complete"])
        self.assertIn("unavailable:AGENTS.md", incomplete["context_omissions"])
        started = self.start()
        arguments = {"session": started["required"]["session"], "note": "Observe", "next_step": "Review", "operation_id": "incomplete"}
        session.session_packet(self.state, **arguments)
        with self.assertRaisesRegex(HarnessError, "STALE_OPERATION_REPLAY: context changed or unavailable"):
            session.session_packet(self.state, **arguments)

    def test_deep_unicode_context_metadata_and_checkpoint_replay_are_bounded(self):
        for index in range(24):
            self.root = self.root / ("路径😀" * 4 + str(index).zfill(2))
            self.root.mkdir()
        current = session.current_provenance(self.root)
        self.assertLess(len(canonical(current)), 16384)
        self.assertTrue(all(path.isascii() and len(path) <= 128 for path in current["context_paths"]))
        self.assertEqual(current["context_path_encoding"], "ancestor_relative_v1")
        started = self.start()
        arguments = {"session": started["required"]["session"], "note": "Observe", "next_step": "Review", "operation_id": "deep"}
        packet = session.session_packet(self.state, **arguments)
        self.assertEqual(session.session_packet(self.state, **arguments), packet)

    def test_bounded_context_cannot_enable_ledger_reuse_or_full_acceptance(self):
        from agent_harness.core import snapshot
        from agent_harness.execution_ledger import Ledger, provenance_for, validity
        full = provenance_for(self.root, snapshot({"cwd": str(self.root)}))
        bounded = session.current_provenance(self.root)
        self.assertEqual(validity(full, full), "valid")
        self.assertEqual(validity(full, bounded), "needs_revalidation")
        self.assertEqual(validity(bounded, bounded), "needs_revalidation")
        legacy = {key: full[key] for key in ("repository_identity", "revision", "snapshot_id", "files")}
        self.assertEqual(validity(legacy, {**bounded, "snapshot_id": legacy["snapshot_id"]}), "needs_revalidation")
        memory = Memory(self.state / "memory", bounded["repository_identity"], enabled=True)
        old_checkpoint = {"provenance": full, "evidence_ids": [], "checkpoint": "Legacy checkpoint", "next_step": "Revalidate"}
        observed = session.checkpoint_observation(memory, old_checkpoint, self.state / "legacy.yaml", bounded)
        self.assertEqual(observed["validity_status"], "needs_revalidation")
        self.assertEqual(observed["payload"]["source_validity"], "unverified")
        self.assertEqual(old_checkpoint["provenance"], full)
        ledger = Ledger(self.state, bounded["repository_identity"])
        with patch("agent_harness.execution_ledger._rg_reuse_context", side_effect=AssertionError("Bounded proof cannot reuse")):
            advisory = ledger.govern({"argv": ["rg", "fixture", "input.txt"], "effect": "read_only",
                "input_closure_complete": True, "toolchain_digest": "a" * 64,
                "env_digest": "b" * 64, "config_digest": "c" * 64}, bounded)
        self.assertEqual(advisory["decision"], "requires_revalidation")
        with patch("agent_harness.native.snapshot", side_effect=HarnessError("Native snapshot failed (exit 1): inventory limit")):
            with self.assertRaisesRegex(HarnessError, "Native snapshot failed"):
                snapshot({"cwd": str(self.root)})

    def test_symlink_state_and_evidence_are_rejected(self):
        """Private store и evidence не могут перенаправляться символической ссылкой."""
        target = self.base / "target"
        target.mkdir()
        try:
            self.state.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink unavailable")
        with self.assertRaises(HarnessError):
            self.start()
        self.state.unlink()
        link = self.base / "receipt"
        link.symlink_to(self.root / "input.txt")
        with self.assertRaises(HarnessError):
            self.start(evidence=link)
        self.assertEqual(list(target.iterdir()), [])

    def test_evidence_hash_and_budget_omissions_are_retrievable(self):
        """Полный receipt digest остаётся в raw, а пропуски имеют pointers."""
        receipt = self.base / "receipt.json"
        receipt.write_text('{"status":"observed"}\n')
        for _ in range(3):
            self.start()
        packet = self.start(evidence=receipt, max_bytes=2048)
        raw = json.loads(session.packet_path(packet, "raw_pointer").read_text())
        self.assertEqual(raw["evidence"]["sha256"], hashlib.sha256(receipt.read_bytes()).hexdigest())
        self.assertLessEqual(len(canonical(packet)) + 2, 2048)
        self.assertTrue(packet["required"]["raw_pointer"])

    def test_hook_cli_rejects_malformed_and_oversized_without_echo(self):
        """Ошибочный hook stdin не копирует потенциальный секрет в stdout/stderr."""
        for raw in (b'{"private":"do-not-echo"', b"x" * 65537):
            result = subprocess.run([sys.executable, str(ROOT / "tools/session.py"), "--state", str(self.state), "hook"],
                input=raw, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(b"do-not-echo", result.stderr)
            self.assertEqual(result.stdout, b"")


class EscapedPathNativeBudgetTests(unittest.TestCase):
    """Exercise real native APIs with Windows-like serialized path costs on POSIX."""

    start = NativeSessionTests.start
    test_context_change_and_bounded_memory_never_prove_source_facts = NativeSessionTests.test_context_change_and_bounded_memory_never_prove_source_facts

    def setUp(self):
        NativeSessionTests.setUp(self)
        # Windows already uses the long, escaped runner temp profile. On POSIX
        # a literal backslash directory gives the same JSON escaping cost while
        # remaining a real local path for immutable-recordset retrieval.
        if os.name != "nt":
            self.base = self.base / r"Users\runneradmin\AppData\Local\Temp\tmp01234567"
            self.base.mkdir()
            self.root = self.base / "project"
            self.root.mkdir()
            (self.root / "input.txt").write_text("first\n")
            self.state = self.base / "state"

    def test_small_cli_packet_keeps_outer_utf8_byte_budget(self):
        for _ in range(3):
            self.start()
        result = subprocess.run([sys.executable, str(ROOT / "tools/session.py"), "--state", str(self.state),
            "--max-bytes", "2048", "start", "--root", str(self.root), "--goal", "Inspect fixture"],
            capture_output=True, env={**os.environ, "PYTHONIOENCODING": "ascii"}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout) + 1, 2048)
        small = json.loads(result.stdout)["packet"]
        self.assertEqual(session.packet_path(small, "root"), self.root)
        self.assertTrue(session.packet_path(small, "packet_pointer").is_file())
        result = subprocess.run([sys.executable, str(ROOT / "tools/session.py"), "--state", str(self.state),
            "--max-bytes", "4096", "start", "--root", str(self.root), "--goal", "Inspect fixture"],
            capture_output=True, env={**os.environ, "PYTHONIOENCODING": "ascii"}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout) + 1, 4096)
        output = json.loads(result.stdout)
        self.assertEqual(output["root"], str(self.root))
        self.assertTrue(output["packet"]["recordset_ref"])

    def test_small_packet_preserves_all_omitted_inputs_via_recordset(self):
        receipt = self.base / "receipt.json"
        receipt.write_text('{"status":"observed"}\n')
        for _ in range(3):
            self.start()
        packet = self.start(evidence=receipt, max_bytes=2048)
        required = packet["required"]
        self.assertLessEqual(len(canonical(session.packet_envelope(packet))) + 2, 2048)
        raw = json.loads(session.packet_path(packet, "raw_pointer").read_text())
        self.assertEqual(raw["evidence"]["sha256"], hashlib.sha256(receipt.read_bytes()).hexdigest())
        store = session.packet_path(packet, "packet_pointer").parent / "recordsets"
        parts, offset = [], 0
        for _ in range(100):
            page = retrieve_recordset(store, packet["recordset_ref"], offset=offset, limit=1024)
            parts.append(page["content"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        retained = json.loads("".join(parts))
        self.assertTrue(retained["records"])
        from agent_harness.core import digest
        self.assertEqual(digest(retained["records"]), packet["recordset_digest"])
        for omission in packet["omission_pointers"]:
            index = omission["recordset_index"] if "recordset_index" in omission else omission["offset"]
            self.assertLess(index, len(retained["records"]))


if __name__ == "__main__":
    unittest.main()
