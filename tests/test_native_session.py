"""Проверки bounded native bootstrap настоящими API в изолированном состоянии."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_harness import native_session as session
from agent_harness.core import HarnessError, canonical, now
from agent_harness.memory import Memory

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
                "session_id": "019-session-test", "agent_type": "root", **kwargs}

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

    def test_source_change_prevents_stale_recall(self):
        """Изменение source bytes исключает старый verified fact и старый checkpoint."""
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
        self.assertIn("Known fixture decision", json.dumps(same))
        (self.root / "input.txt").write_text("second\n")
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
        raw = json.loads(Path(packet["required"]["raw_pointer"]).read_text())
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


if __name__ == "__main__":
    unittest.main()
