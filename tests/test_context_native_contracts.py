"""Adversarial and positive controls for native/context source-bound contracts."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core import HarnessError, Run, canonical, digest, now, snapshot
from agent_harness.runner import run_check, gate
from agent_harness.context_reducer import reduce, retrieve_recordset
from agent_harness.execution_ledger import provenance_for, validity
from agent_harness.memory import Memory, typed_reference
from agent_harness.native_session import session_packet, hook_packet, current_provenance
from agent_harness.native_observability import observe, status, verification_bridge
from agent_harness.repomap_queries import query_repository
from agent_harness.strict_yaml import load

REPO = Path(__file__).resolve().parents[1]


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "one.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        (self.root / "two.py").write_text("x = 2\n", encoding="utf-8")
        self.state = self.base / "state"
        self.current = current_provenance(self.root)
        self.memory = Memory(self.state / "memory", self.current["repository_identity"], enabled=True)

    def start(self, **kwargs):
        return session_packet(self.state, root=self.root, goal="Inspect alpha", **kwargs)

    def semantic(self, identifier="same", **kwargs):
        return {"schema_version": 1, "id": identifier, "repository_identity": self.memory.repository_identity,
                "document": "decisions", "claim": "alpha uses local state", "source": "author", "actor": "test",
                "confidence": "verified", "last_checked": now(), "provenance": self.current,
                "validity_status": "valid", "relations": [], "supersedes": None, **kwargs}

    def episode(self, identifier="episode", refs=None):
        return {"schema_version": 1, "id": identifier, "repository_identity": self.memory.repository_identity,
            "problem_signature": "alpha", "category": "check", "context": "alpha", "hypothesis": "h",
            "attempted_fix": "fix", "root_cause": "author assertion must not become a fact", "outcome": "verified",
            "evidence_ids": [r["id"] for r in refs or []], "evidence_refs": refs or [], "affected_symbols": [],
            "tool_version": "fixture", "trust_level": "verified", "tags": [], "notes": "local check",
            "actor": "test", "created_at": now(), "expires_at": None, "validity_status": "valid",
            "supersedes": None, "provenance": self.current, "claim_kind": "check_result",
            "verification_rule": "check_completion_v1", "check_id": "check-alpha"}

    def evidence(self):
        run = Run.create(self.base / "check-run", {"schema_version": 1, "run_id": "check-run", "task_id": "alpha",
            "attempt_id": "a1", "workflow": "change", "process_level": "standard", "goal": "Check alpha",
            "cwd": str(self.root), "permissions": ["read", "test"], "scope": {"write_paths": []},
            "checks": [{"id": "check-alpha", "kind": "command", "required": True,
                        "argv": [sys.executable, "-I", "-B", "-c", "print('alpha checked')"], "timeout": 3}]})
        run.freeze()
        result = run_check(run, "check-alpha")
        self.assertEqual(result["status"], "passed")
        ref = self.memory.register_core_evidence(run.path, "check-alpha", self.current)
        return Path(ref["path"]), ref

    def test_full_snapshot_not_partial_detail_defines_source(self):
        tree = snapshot({"cwd": str(self.root)})
        full = provenance_for(self.root, tree, file_limit=2)
        compact = provenance_for(self.root, tree, file_limit=1)
        self.assertEqual(validity(full, compact), "valid")
        self.assertEqual(validity(compact, full), "valid")
        self.assertFalse(compact["details_complete"])
        (self.root / "two.py").write_text("x=3\n")
        changed = provenance_for(self.root, snapshot({"cwd": str(self.root)}), file_limit=1)
        self.assertEqual(validity(full, changed), "needs_revalidation")
        for key in ("repository_identity", "inventory_policy_digest", "scope_digest", "checkout_ref", "snapshot_algorithm"):
            self.assertEqual(validity(full, {**compact, key: "different"}), "needs_revalidation")

    def test_full_provenance_fact_survives_native_one_file_projection(self):
        _, ref = self.evidence()
        row = self.episode(refs=[ref])
        row["provenance"] = provenance_for(self.root, snapshot({"cwd": str(self.root)}), file_limit=2)
        self.memory.record_episode(row)
        self.assertEqual(len(self.memory.recall("alpha", self.current)["verified_facts"]), 1)
        (self.root / "two.py").write_text("x = 9\n")
        changed = current_provenance(self.root)
        self.assertEqual(self.memory.recall("alpha", changed)["verified_facts"], [])

    def test_external_receipt_fields_cannot_fabricate_verified_fact(self):
        receipt = self.base / "assertion.json"
        receipt.write_text(json.dumps({"schema_version": 1, "record_type": "check_completion", "id": "asserted",
            "check_id": "check-alpha", "status": "passed", "candidate_digest": self.current["snapshot_id"],
            "producer": "claimed-checker"}))
        ref = self.memory.register_evidence(receipt, self.current)
        self.memory.record_episode(self.episode(refs=[ref]))
        result = self.memory.recall("alpha", self.current)
        self.assertEqual(result["verified_facts"], [])
        self.assertEqual(len(result["current_observations"]), 1)

    def test_typed_show_and_legacy_collision_do_not_guess(self):
        _, ref = self.evidence()
        episode = self.episode("same", [ref])
        semantic = self.semantic()
        self.memory.record_episode(episode)
        self.memory.record_semantic(semantic)
        with self.assertRaisesRegex(HarnessError, "AMBIGUOUS_RECORD_ID"):
            self.memory.show("same")
        self.assertIn("problem_signature", self.memory.show("same", kind="episode")["content"])
        typed = typed_reference(self.memory.repository_identity, "semantic", semantic)
        self.assertIn("alpha uses", self.memory.show("same", reference=typed)["content"])
        with self.assertRaisesRegex(HarnessError, "STALE_REFERENCE"):
            self.memory.show("same", reference={**typed, "content_digest": digest("bad")})
        newer = self.semantic("new", supersedes="same", relations=[{"from": "new", "to": "same", "kind": "supersedes"}])
        self.memory.record_semantic(newer)
        recalled = self.memory.recall("alpha", self.current)
        self.assertEqual(len(recalled["verified_facts"]), 1)
        self.assertIn("same", [r["id"] for r in recalled["current_observations"]])
        self.assertEqual(load(self.memory.root / "knowledge" / "relations.yaml")["records"], [])

    def test_evidence_current_revoked_unknown_and_mutated(self):
        path, ref = self.evidence()
        row = self.episode(refs=[ref])
        self.memory.record_episode(row)
        recall = self.memory.recall("alpha", self.current)
        self.assertEqual(len(recall["verified_facts"]), 1)
        self.assertNotIn("author assertion", json.dumps(recall["verified_facts"]))
        self.memory.invalidate_evidence(ref["id"])
        self.assertEqual(self.memory.evidence_status(row), "revoked")
        self.assertEqual(self.memory.recall("alpha", self.current)["verified_facts"], [])
        (self.memory.root / "evidence_status" / ref["id"] / "00000002.yaml").unlink()
        self.assertEqual(self.memory.evidence_status(row), "unknown")
        path.write_text("changed")
        self.assertEqual(self.memory.evidence_status(row), "stale")

    def test_legacy_author_confidence_does_not_become_verified(self):
        self.memory.record_semantic(self.semantic())
        result = self.memory.recall("alpha", self.current)
        self.assertEqual(result["verified_facts"], [])
        self.assertEqual(result["current_observations"][0]["asserted_confidence"], "legacy_verified")

    def test_checkpoint_keeps_evidence_and_rechecks_revocation(self):
        packet = self.start()
        identifier = packet["required"]["session"]
        receipt, ref = self.evidence()
        session_packet(self.state, session=identifier, note="Check passed", next_step="Review", evidence=receipt)
        own = self.memory.root / "short_term" / (identifier + ".yaml")
        row = load(own)
        self.assertEqual(row["evidence_refs"][0]["reference"]["kind"], "evidence")
        resumed = session_packet(self.state, session=identifier)
        self.assertIn("Check passed", json.dumps(resumed))
        self.memory.invalidate_evidence(ref["id"])
        revoked = session_packet(self.state, session=identifier)
        raw = json.loads(Path(revoked["required"]["raw_pointer"]).read_text())
        own_view = next(r for r in raw["previous_short_term"] if r["pointer"] == str(own))
        self.assertEqual(own_view["payload"]["evidence_validity"], "revoked")
        self.assertEqual(own_view["validity_status"], "needs_revalidation")
        self.assertNotIn("Check passed", json.dumps(revoked["items"]))

    def test_optional_corruption_isolated_but_own_blocks(self):
        packet = self.start()
        identifier = packet["required"]["session"]
        directory = self.memory.root / "short_term"
        (directory / "foreign.yaml").write_text("broken: [\n")
        second = self.start()
        self.assertGreater(second["required"]["degraded_records"], 0)
        own = directory / (identifier + ".yaml")
        own.write_text("broken: [\n")
        with self.assertRaisesRegex(HarnessError, "OWN_CHECKPOINT_CORRUPT"):
            session_packet(self.state, session=identifier)
        self.assertTrue(own.exists())
        self.assertTrue((directory / "foreign.yaml").exists())

    def test_goal_generation_lifecycle_preserves_task_and_conflicts(self):
        event = {"hook_event_name": "SessionStart", "session_id": "root-runtime",
                 "source": "startup", "cwd": str(self.root)}
        first = hook_packet(self.state, event)
        identifier = first["required"]["session"]
        session_packet(self.state, session=identifier, goal="Fix alpha regression", expected_generation=1)
        for source in ("resume", "compact", "clear", "fork"):
            packet = hook_packet(self.state, {**event, "source": source})
            self.assertEqual(packet["required"]["goal_generation"], 2)
        metadata = json.loads((self.state / "native-sessions" / identifier / "session.json").read_text())
        self.assertEqual(metadata["active_goal"], "Fix alpha regression")
        own = load(self.memory.root / "short_term" / (identifier + ".yaml"))
        self.assertEqual(own["goal"], metadata["active_goal"])
        with self.assertRaisesRegex(HarnessError, "GOAL_GENERATION_CONFLICT"):
            session_packet(self.state, session=identifier, goal="Other task", expected_generation=1)

    def test_operation_replay_is_idempotent_and_conflict_detected(self):
        identifier = self.start()["required"]["session"]
        first = session_packet(self.state, session=identifier, note="Completed check", next_step="Review", operation_id="checkpoint-one")
        directory = self.state / "native-sessions" / identifier
        events = list((directory / "ledger" / "events").glob("*.yaml"))
        second = session_packet(self.state, session=identifier, note="Completed check", next_step="Review", operation_id="checkpoint-one")
        self.assertEqual(first, second)
        self.assertEqual(len(events), len(list((directory / "ledger" / "events").glob("*.yaml"))))
        with self.assertRaisesRegex(HarnessError, "OPERATION_ID_CONFLICT"):
            session_packet(self.state, session=identifier, note="Different", next_step="Review", operation_id="checkpoint-one")

    def test_utf8_ascii_pipe_and_bound(self):
        unicode_root = self.base / "Проект中文😀"
        unicode_root.mkdir()
        result = subprocess.run([sys.executable, str(REPO / "tools/session.py"), "--state", str(self.state),
            "--max-bytes", "2048", "start", "--root", str(unicode_root), "--goal", "Проверка中文😀"],
            env={**os.environ, "PYTHONIOENCODING": "ascii"}, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout) + 1, 2048)
        self.assertEqual(json.loads(result.stdout)["root"], str(unicode_root))
        with self.assertRaises(UnicodeEncodeError):
            session_packet(self.base / "bad-state", root=self.root, goal="bad\ud800")
        self.assertFalse((self.base / "bad-state").exists())

    def test_reducer_late_eviction_earliest_pointer_and_sweep(self):
        records = [{"kind": "failure", "payload": {"status": "failed", "signature": "sig"}, "pointer": "raw/zero"}]
        records += [{"kind": "observation", "payload": {"index": i}, "validity_status": "expired", "pointer": "raw/" + str(i)} for i in range(10)]
        for budget in range(1024, 1300, 17):
            packet = reduce(records, max_bytes=budget)
            self.assertLessEqual(len(canonical(packet)) + 2, budget)
            if not packet["items"]:
                self.assertEqual(packet["next_offset"], 0)
                self.assertEqual(packet["omission_pointers"][0]["pointer"], "raw/zero")
            self.assertEqual(packet["omitted"] + sum(r.get("occurrences", 1) for r in packet["items"]), 11)
        with self.assertRaisesRegex(HarnessError, "BUDGET_TOO_SMALL"):
            reduce(records, max_bytes=1023)

    def test_retained_recordset_recovers_evicted_zero_with_typed_digest(self):
        records = [{"kind": "observation", "payload": {"large": "x" * 5000}}]
        store = self.base / "recordsets"
        packet = reduce(records, max_bytes=1024, recordset_store=store)
        ref = packet["recordset_ref"]
        parts, offset = [], 0
        for _ in range(100):
            page = retrieve_recordset(store, ref, offset=offset, limit=1024)
            parts.append(page["content"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(parts))["records"], records)
        with self.assertRaisesRegex(HarnessError, "STALE_REFERENCE"):
            retrieve_recordset(store, {**ref, "content_digest": digest("changed")})
        rejected_store = self.base / "rejected-recordsets"
        with self.assertRaisesRegex(HarnessError, "BUDGET_TOO_SMALL"):
            reduce([], max_bytes=1024, required={"big": "x" * 4000}, recordset_store=rejected_store)
        self.assertFalse(rejected_store.exists())

    def test_broken_pipe_after_commit_remains_replayable(self):
        identifier = self.start()["required"]["session"]
        argv = [sys.executable, str(REPO / "tools/session.py"), "--state", str(self.state), "checkpoint",
                "--session", identifier, "--note", "Pipe checkpoint", "--next", "Review", "--operation-id", "pipe-op"]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        process.wait(timeout=20)
        directory = self.state / "native-sessions" / identifier
        receipt = load(directory / "operation-pipe-op.yaml")
        self.assertTrue(receipt["committed"])
        events = len(list((directory / "ledger" / "events").glob("*.yaml")))
        replay = subprocess.run(argv, capture_output=True, timeout=20)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(len(list((directory / "ledger" / "events").glob("*.yaml"))), events)
        process.stderr.close()

    def test_bridge_revalidates_exact_isolated_transfer(self):
        identifier = self.start()["required"]["session"]
        isolated = self.base / "isolated"
        shutil.copytree(self.root, isolated)
        run = Run.create(self.base / "isolated-run", {"schema_version": 1, "run_id": "isolated", "task_id": "alpha",
            "attempt_id": "a1", "workflow": "change", "process_level": "standard", "goal": "Verify alpha",
            "cwd": str(isolated), "permissions": ["read", "test"], "scope": {"write_paths": []},
            "checks": [{"id": "alpha", "kind": "command", "required": True,
                        "argv": [sys.executable, "-I", "-B", "-c", "print('alpha checked')"], "timeout": 3}]})
        candidate = run.freeze()
        run_check(run, "alpha")
        self.assertEqual(gate(run)["verdict"], "accepted")
        receipt = verification_bridge(self.state, identifier, run.path, expected_candidate=candidate["digest"])
        self.assertEqual(receipt["execution_owner"], "codex_native")
        self.assertEqual(receipt["verification"], "core_gate")
        self.assertEqual(status(self.state, self.root, session=identifier)["acceptance"], "unknown")
        self.assertEqual(status(self.state, self.root, session=identifier, verify_evidence=True)["acceptance"], "accepted")
        saved_state = run.state_path.read_bytes()
        run.state_path.write_text("{invalid")
        unavailable = status(self.state, self.root, session=identifier, verify_evidence=True)
        self.assertEqual(unavailable["acceptance"], "unknown")
        self.assertEqual(unavailable["recorded_acceptance"], "accepted")
        self.assertEqual(unavailable["evidence_validation"], "unavailable")
        self.assertIn("CORE_VERIFICATION_RUN_UNAVAILABLE", unavailable["reason_codes"])
        self.assertEqual(unavailable["verification_receipt"]["candidate_digest"], candidate["digest"])
        run.state_path.write_bytes(saved_state)
        backup = run.path.with_name("temporarily-unavailable-core-run")
        run.path.rename(backup)
        try:
            missing = status(self.state, self.root, session=identifier, verify_evidence=True)
            self.assertEqual(missing["acceptance"], "unknown")
            self.assertEqual(missing["registered_events"], unavailable["registered_events"])
            self.assertIn("CORE_VERIFICATION_RUN_MISSING", missing["reason_codes"])
            self.assertEqual(missing["verification_receipt"]["candidate_digest"], candidate["digest"])
        finally:
            backup.rename(run.path)
        with self.assertRaisesRegex(HarnessError, "CANDIDATE_BINDING_MISMATCH"):
            verification_bridge(self.state, identifier, run.path, expected_candidate=digest("wrong"))
        (self.root / "one.py").write_text("changed\n")
        with self.assertRaisesRegex(HarnessError, "NATIVE_TRANSFER_SOURCE_MISMATCH"):
            verification_bridge(self.state, identifier, run.path, expected_candidate=candidate["digest"])

    def test_reducer_pages_and_changed_recordset_cursor(self):
        rows = [{"kind": "observation", "payload": {"index": i, "text": "a" * 80}, "pointer": "raw/" + str(i)} for i in range(10)]
        cursor, seen = None, []
        for _ in range(20):
            packet = reduce(rows, max_bytes=1600, cursor=cursor)
            seen.extend(item["observation"]["payload"]["index"] for item in packet["items"] if "observation" in item)
            cursor = packet["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, list(range(10)))
        first = reduce(rows, max_bytes=1600)
        with self.assertRaisesRegex(HarnessError, "STALE_CURSOR"):
            reduce(rows[::-1], max_bytes=1600, cursor=first["next_cursor"])

    def event(self, **kwargs):
        return {"schema_version": 1, "hook_event_name": "PostModelRequest", "cwd": str(self.root),
                "session_id": "runtime", "agent_type": "root", "event_id": "event-one", "request_id": "request-one",
                "attempt_id": "attempt-one", "outcome": "completed", "usage": {"input_tokens": 4}, **kwargs}

    def test_observer_privacy_dedup_ambiguous_and_concurrency(self):
        event = self.event(prompt="canary-private-text", transcript_path="canary-private-path", unknown={"secret": "never-persist"})
        receipt = observe(self.state, event)
        self.assertFalse(receipt["idempotent"])
        self.assertTrue(observe(self.state, event)["idempotent"])
        with self.assertRaisesRegex(HarnessError, "NATIVE_EVENT_ID_CONFLICT"):
            observe(self.state, self.event(usage={"input_tokens": 9}))
        observe(self.state, self.event(event_id=None, request_id=None, attempt_id=None, agent_type="unknown"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: observe(self.state, self.event(event_id="event-" + str(i), attempt_id="attempt-" + str(i))), range(8)))
        report = status(self.state, self.root)
        self.assertEqual(report["registered_events"], 10)
        self.assertIsNone(report["authoritative_usage"])
        self.assertEqual(report["capability"], "not_tested")
        self.assertEqual(report["observed_unreconciled"], 1)
        for path in self.state.rglob("*.yaml"):
            self.assertNotIn("canary-private", path.read_text())
            self.assertNotIn("never-persist", path.read_text())

    def test_child_namespace_and_unknown_identity_never_mutate_root(self):
        event = {"hook_event_name": "SessionStart", "session_id": "same-runtime", "source": "startup", "cwd": str(self.root)}
        for extra in ({"agent_type": "unknown"}, {"agent_type": "child"}, {"agent_type": None},
                      {"parent_session_id": "parent"}, {"agent_type": "root", "parent_session_id": "parent"}):
            with self.subTest(extra=extra), self.assertRaisesRegex(HarnessError, "AMBIGUOUS_AGENT_IDENTITY"):
                hook_packet(self.state, {**event, **extra})
        self.assertFalse((self.state / "native-sessions").exists())
        root = hook_packet(self.state, event)
        one = hook_packet(self.state, {**event, "agent_type": "child", "agent_id": "child-one"})
        two = hook_packet(self.state, {**event, "agent_type": "child", "agent_id": "child-two"})
        self.assertNotEqual(one["required"]["session"], two["required"]["session"])
        self.assertNotIn(root["required"]["session"], (one["required"]["session"], two["required"]["session"]))

    def test_migration_dry_run_and_gc_preserve_originals(self):
        self.memory.record_semantic(self.semantic())
        original = (self.memory.root / "semantic" / "same.yaml").read_bytes()
        migration = self.memory.maintenance("migrate", scope=["semantic"])
        self.assertTrue(migration["dry_run"])
        self.assertFalse((self.memory.root / "migrations").exists())
        applied = self.memory.maintenance("migrate", scope=["semantic"], apply=True)
        self.assertTrue(Path(applied["manifest_path"]).exists())
        gc = self.memory.maintenance("gc", scope=["semantic"], apply=True)
        self.assertEqual(gc["deletions"], [])
        self.assertEqual(original, (self.memory.root / "semantic" / "same.yaml").read_bytes())

    def test_query_all_matches_physical_lines_and_cursor(self):
        (self.root / "many.py").write_text("alpha()\n" * 80)
        first = query_repository(self.root, "symbol-context", symbol="alpha", path="one.py", max_bytes=3000)
        self.assertEqual(first["total_results"], 82)  # syntax + occurrence on definition + 80 callers
        seen = set()
        packet = first
        for _ in range(100):
            seen.update((r["file"], r["line"], r["reason"]) for r in packet["results"])
            if not packet["next_cursor"]:
                break
            packet = query_repository(self.root, "symbol-context", symbol="alpha", path="one.py", max_bytes=3000, cursor=packet["next_cursor"])
        self.assertEqual(len(seen), 82)
        (self.root / "one.py").write_text("s='a\u2028b\u2029c\u0085d'\ndef alpha():\n    return 1\n", encoding="utf-8")
        for rg in (True, False):
            with patch("agent_harness.repomap_queries.shutil.which", wraps=shutil.which if rg else lambda _: None):
                result = query_repository(self.root, "symbol-context", symbol="alpha", path="one.py")
                definition = next(r for r in result["results"] if r["reason"] == "direct definition syntax")
                self.assertEqual(definition["line"], 2)
                self.assertIn("def alpha", definition["snippet"])
        with self.assertRaisesRegex(HarnessError, "STALE_CURSOR"):
            query_repository(self.root, "symbol-context", symbol="alpha", path="one.py", cursor=first["next_cursor"])

    @unittest.skipIf(os.name == "nt", "POSIX filename controls")
    def test_query_literal_newline_path(self):
        name = "odd\nname.py"
        (self.root / name).write_text("alpha()\n")
        result = query_repository(self.root, "symbol-context", symbol="alpha", path="one.py")
        self.assertIn(name, {r["file"] for r in result["results"]})
        self.assertTrue(result["inventory_complete"])

    @unittest.skipUnless(shutil.which("git"), "git unavailable")
    def test_query_includes_tracked_ignored_but_not_untracked_ignored(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "test_one.py").write_text("from one import alpha\n")
        subprocess.run(["git", "-C", str(self.root), "add", "test_one.py", "one.py"], check=True)
        (self.root / ".gitignore").write_text("test_one.py\nprivate.py\n")
        (self.root / "private.py").write_text("alpha()\n")
        result = query_repository(self.root, "related-tests", target="one.py")
        self.assertIn("test_one.py", {r["file"] for r in result["results"]})
        self.assertNotIn("private.py", {r["file"] for r in result["results"]})


if __name__ == "__main__":
    unittest.main()
