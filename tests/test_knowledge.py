"""Behavioral scenarios for version-bound recall, reuse and model-facing queries."""
from __future__ import annotations

from copy import deepcopy
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

from agent_harness.core import HarnessError, digest, now, snapshot
from agent_harness.execution_ledger import Ledger, provenance_for, repository_identity, EVENT_SCHEMA
from agent_harness.memory import Memory
from agent_harness.context_reducer import reduce
from agent_harness.check_cache import CheckCache, make_proof
from agent_harness.repomap_queries import query_repository
from agent_harness.format_bench import benchmark, select_format, encode_toon, render
from agent_harness.strict_yaml import StrictYAMLError, dump, load, validate


class KnowledgeScenarios(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "auth.py").write_text("def validate_token(value):\n    return value == 'ok'\n", encoding="utf-8")
        self.store = self.base / "private"
        self.identity = repository_identity(self.repo)
        self.current = self.provenance()

    def tearDown(self):
        self.temp.cleanup()

    def provenance(self):
        return provenance_for(self.repo, snapshot({"cwd": str(self.repo)}), toolchain_digest=digest("tool-v1"))

    def ledger(self):
        return Ledger(self.store / "run-test", self.identity)

    def episode(self, **changes):
        evidence = self.base / "check-evidence.json"
        if not evidence.exists():
            evidence.write_text('{"status":"passed"}', encoding="utf-8")
        value = {"schema_version": 1, "id": "episode-auth", "repository_identity": self.identity,
            "problem_signature": "expired-auth-token", "category": "code_defect", "context": "expired token returned 500",
            "hypothesis": "handle expiration", "attempted_fix": "return 401", "root_cause": "exception path",
            "outcome": "verified", "evidence_ids": ["check-expired"], "affected_symbols": ["validate_token"],
            "evidence_refs": [{"id": "check-expired", "path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}],
            "tool_version": "test-tool-v1", "trust_level": "verified", "tags": ["auth"], "notes": "observed fixture",
            "actor": "fixture-evaluator", "created_at": now(), "expires_at": None,
            "validity_status": "valid", "supersedes": None, "provenance": deepcopy(self.current)}
        value.update(changes)
        return value

    def test_ledger_govern_reuses_empty_search_only_with_unchanged_complete_closure(self):
        ledger = self.ledger()
        output = self.base / "empty.txt"
        output.write_text("", encoding="utf-8")
        operation = {"argv": ["rg", "absent", "."], "cwd": str(self.repo), "purpose": "locate absent symbol",
            "effect": "read_only", "input_closure_complete": True, "toolchain_digest": digest("rg-v1"),
            "env_digest": digest({}), "config_digest": digest({})}
        decision = ledger.govern(operation, self.current)
        self.assertEqual(decision["decision"], "allow")
        record = ledger.append("command", {"argv": operation["argv"], "cwd": str(self.repo),
            "effect": "read_only", "status": "completed", "exit_code": 1,
            "operation_fingerprint": decision["operation_fingerprint"], "evidence_path": str(output),
            "evidence_digest": hashlib.sha256(b"").hexdigest()}, self.current)
        reused = ledger.govern(operation, self.current)
        self.assertEqual(reused["decision"], "suggest_reuse")
        self.assertEqual(reused["reused_event_id"], record["event_id"])
        (self.repo / "new.py").write_text("absent = 1\n", encoding="utf-8")
        self.assertEqual(ledger.govern(operation, self.provenance())["decision"], "allow")
        self.assertEqual(ledger.govern(operation, self.current, force=True)["decision"], "allow")

    def test_state_sensitive_write_unknown_and_unproven_reads_never_reuse(self):
        ledger = self.ledger()
        for argv, effect in ((["git", "status"], "read_only"), (["git", "commit"], "write"), (["curl", "example.invalid"], "external")):
            self.assertEqual(ledger.govern({"argv": argv, "effect": effect}, self.current)["decision"], "allow")
        self.assertEqual(ledger.govern({"argv": ["unknown"]}, self.current)["decision"], "unknown")
        self.assertEqual(ledger.govern({"argv": ["rg", "x"], "effect": "read_only"}, self.current)["decision"], "requires_revalidation")

    def test_ledger_typed_events_stale_inform_and_corrupt_index_rebuild(self):
        ledger = self.ledger()
        event = ledger.append("check", {"check_id": "auth", "status": "failed", "evidence_id": "check-1", "fact": "auth fixture fails"}, self.current)
        with self.assertRaises((HarnessError, StrictYAMLError)):
            ledger.append("check", {"fact": "fake pass"}, self.current)
        corrupt = deepcopy(event)
        del corrupt["payload"]["check_id"]
        with self.assertRaises(StrictYAMLError):
            validate(corrupt, EVENT_SCHEMA)
        self.assertEqual(ledger.inform(self.current)["input_records"], 1)
        (ledger.root / "index.yaml").write_text("broken: true\n", encoding="utf-8")
        self.assertIn("auth", ledger.show(event["event_id"])["content"])
        (self.repo / "auth.py").write_text("# changed\n", encoding="utf-8")
        inform = ledger.inform(self.provenance())
        self.assertEqual(inform["required"]["stale_items_count"], 1)
        self.assertEqual(inform["items"], [])

    def test_ledger_evidence_mutation_cannot_be_reused(self):
        ledger = self.ledger()
        evidence = self.base / "result.txt"
        evidence.write_text("passed")
        ledger.append("check", {"check_id": "auth", "status": "passed", "evidence_id": "check-1",
            "evidence_path": str(evidence), "evidence_digest": hashlib.sha256(b"passed").hexdigest()}, self.current)
        evidence.write_text("failed")
        self.assertEqual(ledger.inform(self.current)["items"], [])

    def test_memory_is_opt_in_and_scoped_to_repository(self):
        memory = Memory(self.store, self.identity)
        with self.assertRaises(HarnessError):
            memory.record_episode(self.episode())
        self.assertFalse(memory.root.exists())
        memory = Memory(self.store, self.identity, enabled=True)
        memory.record_episode(self.episode())
        self.assertEqual(len(memory.recall("expired-auth-token", self.current)["verified_facts"]), 1)
        other = self.base / "same-name" / "repo"
        other.mkdir(parents=True)
        (other / "auth.py").write_text((self.repo / "auth.py").read_text())
        other_identity = repository_identity(other)
        self.assertNotEqual(other_identity, self.identity)
        self.assertEqual(Memory(self.store, other_identity, enabled=True).recall("auth", {**self.current, "repository_identity": other_identity})["verified_facts"], [])
        with self.assertRaises(HarnessError):
            memory.recall("auth", {**self.current, "repository_identity": other_identity})

    def test_memory_stale_superseded_expired_and_hypotheses_are_not_verified(self):
        memory = Memory(self.store, self.identity, enabled=True)
        memory.record_episode(self.episode())
        memory.record_episode(self.episode(id="episode-fix2", supersedes="episode-auth"))
        memory.record_episode(self.episode(id="episode-expired", expires_at="2000-01-01T00:00:00Z"))
        memory.record_episode(self.episode(id="episode-hypothesis", outcome="unknown", trust_level="historical_hypothesis"))
        recall = memory.recall("auth", self.current)
        self.assertEqual([row["id"] for row in recall["verified_facts"]], ["episode-fix2"])
        self.assertEqual(recall["historical_hypothesis"][0]["id"], "episode-hypothesis")
        self.assertEqual({r["status"] for r in recall["stale_items"]}, {"superseded", "expired"})
        (self.repo / "auth.py").write_text("def validate_token(value):\n    return False\n")
        self.assertEqual(memory.recall("auth", self.provenance())["verified_facts"], [])

    def test_semantic_memory_rebuild_and_short_handoff(self):
        memory = Memory(self.store, self.identity, enabled=True)
        memory.record_semantic({"schema_version": 1, "id": "decision-local", "repository_identity": self.identity,
            "document": "architecture", "claim": "Use local file state", "source": "auth.py:1", "actor": "test-reviewer",
            "confidence": "verified", "last_checked": now(), "provenance": self.current,
            "validity_status": "valid", "relations": [{"from": "auth", "to": "validate_token", "kind": "defines"}], "supersedes": None})
        (memory.root / "index.yaml").write_text("corrupt!")
        memory.rebuild_index()
        self.assertIn("Use local file state", (memory.root / "knowledge" / "architecture.md").read_text())
        self.assertEqual(load(memory.root / "knowledge" / "relations.yaml")["records"][0]["kind"], "defines")
        record = {"schema_version": 1, "id": "current", "repository_identity": self.identity,
            "goal": "fix auth", "constraints": ["preserve scope"], "checkpoint": "checkpoint-1", "active_task": "auth",
            "failures": ["expired"], "important_files": ["auth.py"], "next_step": "verify", "evidence_ids": ["check-1"],
            "session_id": None, "attempts_used": 1, "tokens_reported": None, "updated_at": now(), "provenance": self.current}
        memory.update_short_term(record)
        self.assertIn("preserve scope", memory.show("current")["content"])

    def test_memory_unknown_fields_secrets_and_path_traversal_rejected(self):
        memory = Memory(self.store, self.identity, enabled=True)
        with self.assertRaises(StrictYAMLError):
            memory.record_episode(self.episode(unknown="unsafe"))
        bad = self.episode()
        bad["provenance"]["files"] = {"../private": digest("data")}
        with self.assertRaises((HarnessError, StrictYAMLError)):
            memory.record_episode(bad)
        with self.assertRaises(HarnessError):
            memory.record_episode(self.episode(notes="password=secret-value"))

    def test_reducer_groups_duplicates_preserves_failures_and_budgets(self):
        record = {"kind": "failure", "payload": {"status": "failed", "signature": "expected401-got500", "fact": "x" * 20000},
                  "pointer": "logs/check-1", "provenance": {"snapshot_id": digest("tree")}, "event_id": "failure-1"}
        packet = reduce([record, {**record, "event_id": "failure-2"}], max_bytes=2048,
                        required={"constraints": ["do not remove required tests"]})
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode()) + 2, 2048)
        self.assertEqual(packet["duplicates_grouped"], 1)
        self.assertEqual(packet["items"][0]["status"], "failed")
        self.assertEqual(packet["items"][0]["pointer"], "logs/check-1")
        with self.assertRaises(HarnessError):
            reduce([], max_bytes=1024, required={"contract": "x" * 10000})

    def test_cache_requires_pure_closure_and_invalidates_config_toolchain_evidence(self):
        check = {"id": "pure-one", "argv": [sys.executable, "-c", "print(1)"], "cache_policy": "verified_inputs_only", "deterministic": True, "effect": "pure"}
        proof = make_proof(self.repo, check, toolchain_digest=digest("python-v1"), environment={}, config={"strict": True},
                           isolated_environment=True, no_external_dependencies=True)
        evidence = self.base / "proof.txt"
        evidence.write_text("1\n")
        cache = CheckCache(self.store / "cache", allowlist=["pure-one"])
        self.assertTrue(cache.put(check, proof, {"id": "check-one", "path": str(evidence), "status": "passed"}, run_id="first-run")["stored"])
        hit = cache.get(check, proof)
        self.assertTrue(hit["hit"])
        self.assertEqual(hit["record"]["first_run_id"], "first-run")
        self.assertFalse(cache.get(check, proof, fresh=True)["hit"])
        for field in ("config_digest", "toolchain_digest", "snapshot_id"):
            self.assertFalse(cache.get(check, {**proof, field: digest("changed")})["hit"])
        self.assertFalse(cache.get({**check, "effect": "local_test"}, proof)["hit"])
        self.assertFalse(cache.get({**check, "cache_policy": "never"}, proof)["hit"])
        evidence.write_text("mutated")
        self.assertFalse(cache.get(check, proof)["hit"])

    def test_four_queries_return_actual_version_bound_links(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_auth.py").write_text("from auth import validate_token\ndef test_expired():\n    assert not validate_token('expired')\n")
        (self.repo / "client.py").write_text("from auth import validate_token\nvalidate_token('ok')\n")
        related = query_repository(self.repo, "related-tests", target="auth.py")
        self.assertEqual(related["results"][0]["file"], "tests/test_auth.py")
        impact = query_repository(self.repo, "change-impact", changed=["auth.py"])
        self.assertIn("client.py", {r["file"] for r in impact["results"]})
        context = query_repository(self.repo, "symbol-context", path="auth.py", symbol="validate_token")
        self.assertEqual(context["results"][0]["line"], 1)
        self.assertEqual(context["results"][0]["link"]["snapshot_id"], context["snapshot_id"])
        trace = self.base / "trace.txt"
        trace.write_text('File "auth.py", line 2, in validate_token\nRuntimeError: no exec\n')
        mapped = query_repository(self.repo, "trace-to-code", trace_file=trace)
        self.assertEqual(mapped["results"][0]["line"], 2)
        with self.assertRaises(HarnessError):
            query_repository(self.repo, "related-tests", target="../escape.py")

    def test_query_pagination_and_absent_rg_fallback(self):
        for i in range(20):
            (self.repo / ("test_auth_" + str(i) + ".py")).write_text("from auth import validate_token\n")
        page = query_repository(self.repo, "related-tests", target="auth.py", max_bytes=2048)
        self.assertIsNotNone(page["next_page"])
        next_page = query_repository(self.repo, "related-tests", target="auth.py", max_bytes=2048, offset=page["next_page"])
        self.assertNotEqual(page["results"], next_page["results"])
        with patch("agent_harness.repomap_queries.shutil.which", return_value=None), patch("agent_harness.native.backend_info", return_value={"available": False}):
            result = query_repository(self.repo, "symbol-context", path="auth.py", symbol="validate_token")
        self.assertIn("unavailable", result["method"])
        self.assertTrue(result["results"])

    def test_selective_toon_only_after_matching_positive_real_usage_shape(self):
        rows = [{"identifier_long": i, "description_long": "value " + str(i)} for i in range(20)]
        self.assertEqual(select_format(rows)["format"], "json")
        unmeasured = benchmark(rows)
        self.assertEqual(unmeasured["status"], "insufficient_data")
        # Controlled measured callback fixture verifies the policy, not LLM gain.
        measure = lambda name, payload: {"input_tokens": 2000 if name == "json" else 1200,
            "correct": True, "model": "fixture-model", "source": "controlled_test_fixture"}
        positive = benchmark(rows, measure, model="fixture-model")
        self.assertEqual(positive["status"], "measured_positive")
        selected = select_format(rows, evaluation=positive, model="fixture-model")
        self.assertEqual(selected["format"], "toon")
        self.assertTrue(selected["payload"].startswith("rows[20]{identifier_long,description_long}:"))
        self.assertEqual(select_format(rows + [{"identifier_long": 99, "description_long": "new"}], evaluation=positive, model="fixture-model")["format"], "json")
        self.assertEqual(select_format(rows, evaluation=positive, model="changed-model")["format"], "json")
        negative = benchmark(rows, lambda n, p: {"input_tokens": 100, "correct": True, "model": "installed"})
        self.assertFalse(negative["enabled"])
        self.assertEqual(negative["status"], "measured_negative")
        quality = benchmark(rows, lambda n, p: {"input_tokens": 200 if n == "json" else 100, "correct": n != "toon", "model": "installed"})
        self.assertEqual(quality["status"], "quality_regression")
        self.assertFalse(quality["enabled"])

    def test_toon_unsuitable_and_missing_usage_fall_back_without_cost_claim(self):
        nested = [{"id": 1, "details": {"x": 1}}]
        self.assertEqual(benchmark(nested)["status"], "unsuitable_data")
        self.assertEqual(select_format(nested)["format"], "json")
        result = benchmark([{"id": 1}], lambda n, p: {"input_tokens": None, "correct": True})
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["input_token_saving_fraction"])

    def test_optional_lsp_unavailable_and_stale_fall_back_without_stale_exact_link(self):
        with patch("agent_harness.lsp.query", side_effect=HarnessError("server absent")):
            result = query_repository(self.repo, "symbol-context", path="auth.py", symbol="validate_token", use_lsp=True)
        self.assertEqual(result["lsp_status"], "unavailable_or_stale_fallback")
        self.assertTrue(result["results"])
        with patch("agent_harness.lsp.query", return_value={"source": {"sha256": digest("old")}, "items": []}):
            result = query_repository(self.repo, "symbol-context", path="auth.py", symbol="validate_token", use_lsp=True)
        self.assertTrue(all(row["confidence"] != "semantic" for row in result["results"]))

    def test_changed_episode_evidence_blocks_verified_recall(self):
        memory = Memory(self.store, self.identity, enabled=True)
        record = self.episode()
        memory.record_episode(record)
        Path(record["evidence_refs"][0]["path"]).write_text('{"status":"failed"}')
        recalled = memory.recall("expired-auth-token", self.current)
        self.assertEqual(recalled["verified_facts"], [])
        self.assertEqual(recalled["stale_memory_prevented"], 1)

    def test_missing_check_proof_and_mismatched_file_digest_not_verified(self):
        ledger = self.ledger()
        ledger.append("check", {"check_id": "auth", "status": "passed", "evidence_id": "missing-receipt"}, self.current)
        self.assertEqual(ledger.inform(self.current)["items"], [])
        with self.assertRaises(HarnessError):
            ledger.append("file_read", {"path": "auth.py", "digest": digest("wrong"), "purpose": "review"}, self.current)

    def test_programmatic_recall_utf8_pages_obey_serialized_budget(self):
        from agent_harness.execution_ledger import paginate
        value = {"observation": "Ошибка строки: \"данные\"\n" * 1000}
        offset, chunks = 0, []
        while True:
            page = paginate(value, offset=offset, limit=512)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 2, 512)
            chunks.append(page["content"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        from agent_harness.core import canonical
        self.assertEqual("".join(chunks), canonical(value).decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
