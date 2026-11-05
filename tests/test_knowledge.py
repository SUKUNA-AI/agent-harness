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
import encodings
import zipfile
from unittest.mock import patch

from agent_harness.core import HarnessError, digest, now, snapshot
from agent_harness.execution_ledger import Ledger, provenance_for, repository_identity, EVENT_SCHEMA
from agent_harness.memory import Memory
from agent_harness.context_reducer import reduce
from agent_harness.check_cache import CheckCache, make_proof
from agent_harness.repomap_queries import query_repository
from agent_harness.format_bench import benchmark, select_format, encode_toon, render
from agent_harness.strict_yaml import StrictYAMLError, dump, load, validate


def closed_python_fixture(base):
    """An actual, private bounded runtime; ordinary external Python is unsupported."""
    if os.name != "nt":
        raise unittest.SkipTest("Optional Python cache has no proven self-contained runtime on this platform")
    runtime = Path(base) / "closed-python-runtime"
    runtime.mkdir(exist_ok=True)
    source = Path(sys.executable).resolve().parent
    stem = "python" + str(sys.version_info.major) + str(sys.version_info.minor)
    for name in ("python.exe", stem + ".dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        candidate = Path(sys.executable).resolve() if name == "python.exe" else source / name
        if not candidate.is_file():
            raise unittest.SkipTest("Private runtime dependency unavailable: " + name)
        shutil.copyfile(candidate, runtime / name)
    reference = Path(encodings.__file__).parent
    with zipfile.ZipFile(runtime / (stem + ".zip"), "w", compression=zipfile.ZIP_STORED) as archive:
        for path in reference.glob("*.py"):
            archive.write(path, "encodings/" + path.name)
        archive.write(reference.parent / "linecache.py", "linecache.py")
    (runtime / (stem + "._pth")).write_text(stem + ".zip\n", encoding="utf-8")
    return runtime / "python.exe"


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
        operation = {"argv": ["rg", "--no-config", "--no-ignore-parent", "--no-ignore-global", "--no-ignore-vcs", "absent", "."], "cwd": str(self.repo), "purpose": "locate absent symbol",
            "effect": "read_only", "input_closure_complete": True, "toolchain_digest": digest("rg-v1"),
            "env_digest": digest({}), "config_digest": digest({})}
        decision = ledger.govern(operation, self.current)
        if shutil.which("rg") is None:
            self.assertEqual(decision["decision"], "requires_revalidation", decision)
            self.assertIn("executable identity", decision["reason"])
            self.assertIsNone(decision["reused_event_id"])
            return
        self.assertEqual(decision["decision"], "allow", decision)
        record = ledger.append("command", {"argv": operation["argv"], "cwd": str(self.repo),
            "effect": "read_only", "status": "completed", "exit_code": 1,
            "operation_fingerprint": decision["operation_fingerprint"], "evidence_path": str(output),
            "evidence_digest": hashlib.sha256(b"").hexdigest()}, self.current)
        reused = ledger.govern(operation, self.current)
        self.assertEqual(reused["decision"], "suggest_reuse")
        self.assertEqual(reused["reused_event_id"], record["event_id"])
        (self.repo / "new.py").write_text("absent = 1\n", encoding="utf-8")
        self.assertEqual(ledger.govern(operation, self.provenance())["decision"], "allow")
        self.assertNotEqual(ledger.govern(operation, self.current)["decision"], "suggest_reuse")
        self.assertEqual(ledger.govern(operation, self.current, force=True)["decision"], "allow")

    def test_missing_rg_requires_revalidation_without_inventing_a_tool_identity(self):
        operation = {"argv": ["rg", "--no-config", "--no-ignore-parent", "--no-ignore-global",
            "--no-ignore-vcs", "absent", "."], "cwd": str(self.repo), "effect": "read_only",
            "purpose": "missing tool fixture", "input_closure_complete": True,
            "toolchain_digest": digest("rg-v1"), "env_digest": digest({}), "config_digest": digest({})}
        with patch("shutil.which", return_value=None):
            decision = self.ledger().govern(operation, self.current)
        self.assertEqual(decision["decision"], "requires_revalidation", decision)
        self.assertIn("executable identity", decision["reason"])
        self.assertIsNone(decision["reused_event_id"])

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
        self.assertEqual(len(memory.recall("expired-auth-token", self.current)["current_observations"]), 1)
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
        self.assertEqual([row["id"] for row in recall["current_observations"]], ["episode-fix2"])
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
        executable = closed_python_fixture(self.base)
        check = {"id": "pure-one", "argv": [str(executable), "-I", "-S", "-c", "print(1)"], "cache_policy": "verified_inputs_only", "deterministic": True, "effect": "pure"}
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
        self.assertEqual("snapshot_inventory", result["method"])
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
        self.assertEqual(selected["format"], "json")
        self.assertEqual(positive["measurement_kind"], "synthetic")
        self.assertFalse(positive["enabled"])
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


class ReuseSafetyRegressions(unittest.TestCase):
    """Actual reproductions: fresh final gates are independent of these read caches."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "tracked.txt").write_text("one")
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        for args in (["init"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "-c", "commit.gpgsign=false", "commit", "-m", "fixture"]):
            subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=True)

    def tearDown(self):
        self.temp.cleanup()

    def cache_case(self, filename):
        executable = closed_python_fixture(self.base)
        check = {"id": "closed-read", "argv": [str(executable), "-I", "-S", "-c", "assert open(" + repr(filename) + ", encoding='utf-8').read() == 'one'"],
                 "cache_policy": "verified_inputs_only", "deterministic": True, "effect": "pure"}
        proof = make_proof(self.repo, check, toolchain_digest=digest("python"), environment={}, config={},
                           isolated_environment=True, no_external_dependencies=True)
        self.assertEqual(subprocess.run(check["argv"], cwd=self.repo, env={}, capture_output=True).returncode, 0)
        evidence = self.base / "check.json"
        evidence.write_text('{"status":"passed"}')
        cache = CheckCache(self.base / "cache", allowlist=[check["id"]])
        cache.put(check, proof, {"id": "check-closed-read", "path": str(evidence), "status": "passed"}, run_id="first")
        return check, proof, cache

    def test_old_proof_never_hits_after_tracked_source_edit(self):
        check, old, cache = self.cache_case("tracked.txt")
        (self.repo / "tracked.txt").write_text("two")
        self.assertEqual(subprocess.run(check["argv"], cwd=self.repo, env={}, capture_output=True).returncode, 1)
        self.assertFalse(cache.get(check, old)["hit"])

    def test_fresh_proof_covers_git_ignored_actual_check_input(self):
        (self.repo / "ignored.txt").write_text("one")
        check, old, cache = self.cache_case("ignored.txt")
        (self.repo / "ignored.txt").write_text("two")
        fresh = make_proof(self.repo, check, toolchain_digest=digest("python"), environment={}, config={},
                           isolated_environment=True, no_external_dependencies=True)
        self.assertEqual(subprocess.run(check["argv"], cwd=self.repo, env={}, capture_output=True).returncode, 1)
        self.assertNotEqual(old, fresh)
        self.assertFalse(cache.get(check, fresh)["hit"])

    def unsafe_rg_reuse(self, *, outside=False):
        target = self.base / "external.txt" if outside else self.repo / "ignored.txt"
        target.write_text("nothing\n")
        argv = [shutil.which("rg"), *([] if outside else ["--no-ignore", "--hidden"]), "CLOSURE_SENTINEL", str(target) if outside else "."]
        if not argv[0]:
            self.skipTest("rg unavailable")
        before = subprocess.run(argv, cwd=self.repo, capture_output=True)
        self.assertEqual(before.returncode, 1)
        current = provenance_for(self.repo, snapshot({"cwd": str(self.repo)}), toolchain_digest=digest("rg"))
        operation = {"argv": argv, "cwd": str(self.repo), "purpose": "search sentinel", "effect": "read_only",
            "input_closure_complete": True, "toolchain_digest": digest("rg-tool"), "env_digest": digest({}), "config_digest": digest({})}
        ledger = Ledger(self.base / "run", repository_identity(self.repo))
        decision = ledger.govern(operation, current)
        raw = self.base / "empty-rg.txt"
        raw.write_bytes(before.stdout)
        ledger.append("command", {"argv": argv, "cwd": str(self.repo), "effect": "read_only", "status": "completed", "exit_code": 1,
            "operation_fingerprint": decision["operation_fingerprint"], "evidence_path": str(raw), "evidence_digest": hashlib.sha256(raw.read_bytes()).hexdigest()}, current)
        target.write_text("CLOSURE_SENTINEL\n")
        fresh = provenance_for(self.repo, snapshot({"cwd": str(self.repo)}), toolchain_digest=digest("rg"))
        self.assertEqual(subprocess.run(argv, cwd=self.repo, capture_output=True).returncode, 0)
        self.assertNotEqual(ledger.govern(operation, fresh)["decision"], "suggest_reuse")

    def test_no_ignore_hidden_rg_cannot_reuse_uncovered_ignored_input(self):
        self.unsafe_rg_reuse()

    def test_rg_external_absolute_scope_never_reuses_repository_proof(self):
        self.unsafe_rg_reuse(outside=True)

    def test_physical_closure_schema_and_file_directory_budgets_fail_closed(self):
        from agent_harness.input_closure import bounded_closure, CLOSURE_SCHEMA
        tree = bounded_closure(self.repo)
        validate(tree, CLOSURE_SCHEMA)
        self.assertIn(".git", tree["entries"])
        self.assertEqual(tree["entries"][".git"]["kind"], "directory")
        broken = deepcopy(tree)
        broken["entries"][".git"]["bytes"] = 0
        with self.assertRaises(StrictYAMLError):
            validate(broken, CLOSURE_SCHEMA)
        with patch("agent_harness.input_closure.MAX_FILES", 1):
            with self.assertRaises(HarnessError):
                bounded_closure(self.repo)
        with patch("agent_harness.input_closure.MAX_DIRECTORIES", 1):
            with self.assertRaises(HarnessError):
                bounded_closure(self.repo)
        with patch("agent_harness.input_closure.MAX_TOTAL_BYTES", 1):
            with self.assertRaises(HarnessError):
                bounded_closure(self.repo)

    def test_cache_revalidates_live_environment_and_never_migrates_legacy_proof_in_place(self):
        check, proof, cache = self.cache_case("tracked.txt")
        self.assertTrue(cache.get(check, proof)["hit"])
        with patch.dict(os.environ, {"REUSE_AUDIT_OPTION": "changed"}):
            self.assertFalse(cache.get(check, proof)["hit"])
        legacy = deepcopy(proof)
        legacy["schema_version"] = 1
        self.assertFalse(cache.get(check, legacy)["hit"])
        # Original immutable record remains exactly available after misses.
        saved = list(cache.root.glob("*.yaml"))
        self.assertEqual(len(saved), 1)
        original = saved[0].read_bytes()
        self.assertFalse(cache.get(check, legacy)["hit"])
        self.assertEqual(saved[0].read_bytes(), original)

    def test_follow_config_stdin_and_outside_cwd_require_actual_revalidation(self):
        ledger = Ledger(self.base / "run", repository_identity(self.repo))
        current = provenance_for(self.repo, snapshot({"cwd": str(self.repo)}), toolchain_digest=digest("rg"))
        controls = ["--no-config", "--no-ignore-parent", "--no-ignore-global", "--no-ignore-vcs"]
        for extra, paths, cwd in ((["--follow"], ["."], self.repo), (["--hidden"], ["."], self.repo),
                                  ([], ["-"], self.repo), ([], ["../"], self.repo), ([], ["."], self.base)):
            operation = {"argv": ["rg", *controls, *extra, "pattern", *paths], "cwd": str(cwd), "purpose": "audit",
                "effect": "read_only", "input_closure_complete": True, "toolchain_digest": digest("rg"),
                "env_digest": digest({}), "config_digest": digest({})}
            self.assertEqual(ledger.govern(operation, current)["decision"], "requires_revalidation")

    def test_purity_booleans_never_admit_opaque_external_or_importing_code(self):
        for code in ("import os; print(os.environ)", "print(eval('1'))", "print(__import__('time').time())",
                     "assert open('../outside.txt', encoding='utf-8').read() == 'one'", "print(open('tracked.txt', 'w').write('bad'))"):
            check = {"id": "claimed-pure", "argv": [sys.executable, "-I", "-S", "-c", code],
                "cache_policy": "verified_inputs_only", "deterministic": True, "effect": "pure"}
            with self.assertRaises(HarnessError):
                make_proof(self.repo, check, toolchain_digest=digest("python"), environment={}, config={},
                           isolated_environment=True, no_external_dependencies=True)

    def test_controlled_rg_reuses_actual_empty_read_but_not_ignored_file_edit(self):
        rg = shutil.which("rg")
        if not rg:
            self.skipTest("rg unavailable")
        argv = [rg, "--no-config", "--no-ignore-parent", "--no-ignore-global", "--no-ignore-vcs", "CLOSURE_SENTINEL", "."]
        result = subprocess.run(argv, cwd=self.repo, capture_output=True)
        self.assertEqual(result.returncode, 1)
        current = provenance_for(self.repo, snapshot({"cwd": str(self.repo)}), toolchain_digest=digest("rg"))
        operation = {"argv": argv, "cwd": str(self.repo), "purpose": "closed search", "effect": "read_only",
            "input_closure_complete": True, "toolchain_digest": digest("rg"), "env_digest": digest({}), "config_digest": digest({})}
        ledger = Ledger(self.base / "run", repository_identity(self.repo))
        first = ledger.govern(operation, current)
        self.assertEqual(first["decision"], "allow")
        raw = self.base / "rg-result.txt"
        raw.write_bytes(result.stdout)
        ledger.append("command", {"argv": argv, "cwd": str(self.repo), "effect": "read_only", "status": "completed", "exit_code": 1,
            "operation_fingerprint": first["operation_fingerprint"], "evidence_path": str(raw), "evidence_digest": hashlib.sha256(raw.read_bytes()).hexdigest()}, current)
        self.assertEqual(ledger.govern(operation, current)["decision"], "suggest_reuse")
        (self.repo / "ignored.txt").write_text("CLOSURE_SENTINEL\n")
        fresh = provenance_for(self.repo, snapshot({"cwd": str(self.repo)}), toolchain_digest=digest("rg"))
        self.assertEqual(fresh["snapshot_id"], current["snapshot_id"])
        self.assertEqual(subprocess.run(argv, cwd=self.repo, capture_output=True).returncode, 0)
        self.assertNotEqual(ledger.govern(operation, current)["decision"], "suggest_reuse")

    def test_external_python_runtime_does_not_gain_closure_from_booleans(self):
        check = {"id": "literal", "argv": [sys.executable, "-I", "-S", "-c", "print(1)"],
                 "cache_policy": "verified_inputs_only", "deterministic": True, "effect": "pure"}
        with self.assertRaises(HarnessError):
            make_proof(self.repo, check, toolchain_digest=digest("python"), environment={}, config={},
                       isolated_environment=True, no_external_dependencies=True)

    def test_actual_closed_runtime_startup_present_absent_and_stdlib_revalidation(self):
        executable = closed_python_fixture(self.base)
        check = {"id": "literal", "argv": [str(executable), "-I", "-S", "-c", "print(1)"],
                 "cache_policy": "verified_inputs_only", "deterministic": True, "effect": "pure"}
        proof = make_proof(self.repo, check, toolchain_digest=digest("python"), environment={}, config={},
                           isolated_environment=True, no_external_dependencies=True)
        before = subprocess.run(check["argv"], cwd=self.repo, env={}, capture_output=True)
        self.assertEqual((before.returncode, before.stdout.strip()), (0, b"1"))
        evidence = self.base / "literal.txt"
        evidence.write_bytes(before.stdout)
        cache = CheckCache(self.base / "cache", allowlist=["literal"])
        self.assertTrue(cache.put(check, proof, {"id": "literal-receipt", "path": str(evidence), "status": "passed"}, run_id="first")["stored"])
        self.assertTrue(cache.get(check, proof)["hit"])
        runtime = executable.parent
        stem = "python" + str(sys.version_info.major) + str(sys.version_info.minor)
        startup = runtime / (stem + "._pth")
        original = startup.read_bytes()
        archive_path = runtime / (stem + ".zip")
        original_archive = archive_path.read_bytes()
        startup.write_text("missing_stdlib\n")
        self.assertNotEqual(subprocess.run(check["argv"], cwd=self.repo, env={}, capture_output=True).returncode, 0)
        self.assertFalse(cache.get(check, proof)["hit"])
        with self.assertRaises(HarnessError):
            make_proof(self.repo, check, toolchain_digest=digest("python"), environment={}, config={},
                       isolated_environment=True, no_external_dependencies=True)
        startup.write_bytes(original)
        self.assertTrue(cache.get(check, proof)["hit"])
        startup.unlink()
        self.assertFalse(cache.get(check, proof)["hit"])
        startup.write_bytes(original)
        alternate = runtime / "python._pth"
        alternate.write_text(stem + ".zip\n")
        self.assertFalse(cache.get(check, proof)["hit"])
        alternate.unlink()
        parent_venv = self.base / "pyvenv.cfg"
        parent_venv.write_text("home = outside\n")
        self.assertFalse(cache.get(check, proof)["hit"])
        parent_venv.unlink()
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("unrelated.py", "# no codec startup")
        self.assertNotEqual(subprocess.run(check["argv"], cwd=self.repo, env={}, capture_output=True).returncode, 0)
        self.assertFalse(cache.get(check, proof)["hit"])
        archive_path.write_bytes(original_archive)
        self.assertTrue(cache.get(check, proof)["hit"])
        (runtime / (stem + ".dll")).write_bytes(b"invalid private DLL fixture")
        self.assertFalse(cache.get(check, proof)["hit"])


if __name__ == "__main__":
    unittest.main()
