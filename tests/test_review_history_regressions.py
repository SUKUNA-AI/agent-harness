"""R01 receipt/retry isolation and durable review publication controls.

All model responses come from the contained fake Codex process, through the
real adapter, ownership callback, journal, validators and supervisor recovery.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, digest
from agent_harness.execution_receipts import ExecutionReceipts, validate_record
from agent_harness import managed_review
from agent_harness.supervisor import Supervisor
import test_verified_flow as verified_fixture


class Crash(BaseException):
    pass


def review_failures(messages):
    """Persist the synthetic review sequence outside the task's checkout."""
    def edit(script):
        return script.replace("if 'HARNESS_FINAL_REVIEW' in prompt:\n",
            "if 'HARNESS_FINAL_REVIEW' in prompt:\n"
            " attempts=pathlib.Path(__file__).with_suffix('.reviews')\n"
            " count=int(attempts.read_text()) if attempts.exists() else 0\n"
            " attempts.write_text(str(count+1))\n"
            " failures=" + repr(messages) + "\n"
            " if count<len(failures):event('turn.failed',error={'message':failures[count]});raise SystemExit(1)\n")
    return edit


class ReviewHistoryRegressions(unittest.TestCase):
    fixture = verified_fixture.VerifiedFlowTests.fixture

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)

    def controls(self, failures, *, wall_seconds=verified_fixture.VERIFIED_FLOW_WALL_SECONDS):
        supervisor, fake = self.fixture(script_edit=review_failures(failures), wall_seconds=wall_seconds)
        deadline = supervisor.state["deadline"]
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", supervisor.adapter.failure_summary(result))
        self.assertEqual(supervisor.state["deadline"], deadline)
        records = supervisor.receipts().records()
        for record in records:
            validate_record(record)
        completions = [r for r in records if r["record_type"] == "completion" and r["node_id"] == "@review"]
        self.assertEqual([r["payload"]["outcome"] for r in completions], ["failed"] * len(failures) + ["completed"])
        self.assertEqual(len(fake.with_suffix(".calls").read_text().splitlines()), 5 + len(failures))
        self.assertEqual(result["metrics"]["repair_attempts"], len(failures))
        self.assertEqual(sum(r["record_type"] == "coverage" for r in records), 1)
        self.assertEqual(sum(r["record_type"] == "review" for r in records), 1)
        self.assertTrue(supervisor.status(verify=True)["evidence_current"])

    def test_zero_failures_then_approval(self):
        self.controls([])

    def test_one_repairable_failure_then_approval(self):
        self.controls(["compiler alpha"])

    def test_two_distinct_repairable_failures_then_approval(self):
        self.controls(["compiler alpha", "test beta"], wall_seconds=verified_fixture.TWO_PHASE_WALL_SECONDS)

    def test_repeated_failure_remains_bounded_no_progress(self):
        supervisor, fake = self.fixture(script_edit=review_failures(["test alpha", "test alpha"]))
        self.assertEqual(supervisor.start()["status"], "NO_PROGRESS")
        self.assertEqual(fake.with_suffix(".reviews").read_text(), "2")
        self.assertFalse(any(r["record_type"] in {"review", "coverage"} for r in supervisor.receipts().records()))
        self.assertIsNone(supervisor._core().state()["accepted"])

    def crash_at(self, boundary, *, failures=(), wall_seconds=verified_fixture.VERIFIED_FLOW_WALL_SECONDS):
        supervisor, fake = self.fixture(script_edit=review_failures(list(failures)), wall_seconds=wall_seconds)
        publish = ExecutionReceipts.publish
        def crash(instance, record):
            value = publish(instance, record)
            if record["record_type"] == boundary and (boundary != "completion" or record["payload"]["outcome"] == "completed") and record["node_id"] in {"@review", "@coverage"}:
                raise Crash()
            return value
        with patch.object(ExecutionReceipts, "publish", crash), self.assertRaises(Crash):
            supervisor.start()
        return supervisor, fake

    def recover(self, supervisor, fake):
        calls = fake.with_suffix(".calls").read_bytes()
        completions = [r for r in supervisor.receipts().records() if r["record_type"] == "completion"]
        deadline = supervisor.state["deadline"]
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        result = recovered.resume()
        self.assertEqual(result["status"], "ACCEPTED", recovered.adapter.failure_summary(result))
        self.assertEqual(recovered.state["deadline"], deadline)
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
        records = recovered.receipts().records()
        self.assertEqual([r for r in records if r["record_type"] == "completion"], completions)
        for kind in ("coverage", "review"):
            self.assertEqual(sum(r["record_type"] == kind for r in records), 1)
        self.assertTrue(recovered.status(verify=True)["evidence_current"])
        return recovered

    def test_crash_after_completion_recovers_exact_raw_without_model(self):
        self.recover(*self.crash_at("completion", failures=("compiler alpha", "test beta"),
                                    wall_seconds=verified_fixture.TWO_PHASE_WALL_SECONDS))

    def test_crash_between_coverage_and_review_recovers_without_model(self):
        self.recover(*self.crash_at("coverage", wall_seconds=verified_fixture.TWO_PHASE_WALL_SECONDS))

    def test_crash_after_review_before_state_recovers_without_model(self):
        self.recover(*self.crash_at("review", wall_seconds=verified_fixture.TWO_PHASE_WALL_SECONDS))

    def assert_raw_required(self, boundary, *, wall_seconds=verified_fixture.VERIFIED_FLOW_WALL_SECONDS):
        supervisor, fake = self.crash_at(boundary, wall_seconds=wall_seconds)
        records = supervisor.receipts().records()
        completion = next(r for r in records if r["record_type"] == "completion" and r["node_id"] == "@review")
        raw = Path(completion["payload"]["logs"]["stdout"])
        original = raw.read_bytes()
        calls = fake.with_suffix(".calls").read_bytes()
        for mutation in ("missing", "corrupt"):
            with self.subTest(mutation=mutation):
                if mutation == "missing":
                    raw.unlink()
                else:
                    raw.write_bytes(b'corrupt raw verdict\n')
                with patch.object(managed_review, "invoke", side_effect=AssertionError("must not call model")):
                    with self.assertRaises((OSError, ValueError, HarnessError)):
                        managed_review.review_candidate(supervisor, supervisor._core(), {"digest": supervisor.state["candidate_digest"]})
                self.assertEqual(supervisor.receipts().records(), records)
                self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
                self.assertIsNone(supervisor._core().state()["accepted"])
                raw.write_bytes(original)
        self.recover(supervisor, fake)

    def test_missing_or_corrupt_raw_before_publication_cannot_approve(self):
        self.assert_raw_required("completion")

    def test_missing_or_corrupt_raw_after_publication_cannot_approve(self):
        self.assert_raw_required("review", wall_seconds=verified_fixture.TWO_PHASE_WALL_SECONDS)

    def test_deterministic_publication_replay_and_same_id_conflict(self):
        supervisor, fake = self.crash_at("completion")
        candidate = supervisor.state["candidate_digest"]
        run = supervisor._core()
        calls = fake.with_suffix(".calls").read_bytes()
        managed_review.review_candidate(supervisor, run, {"digest": candidate})
        receipts = supervisor.receipts()
        records = receipts.records()
        completion = next(r for r in records if r["record_type"] == "completion" and r["node_id"] == "@review")
        intent = next(r for r in records if r["record_id"] == completion["payload"]["intent_ref"])
        index = {r["record_id"]: r for r in records}
        before = len(supervisor.store.events())
        for kind in ("coverage", "review"):
            record = next(r for r in records if r["record_type"] == kind)
            expected_id = kind + "-" + digest([intent["invocation_id"], candidate, kind])
            self.assertEqual(record["record_id"], expected_id)
            self.assertNotEqual(record["record_id"], kind + "-" + digest([intent["invocation_id"], "a" * 64, kind]))
            self.assertEqual(managed_review._publish_verdict_record(receipts, index, intent, completion,
                candidate, kind, record["payload"]), record)
            changed = deepcopy(record)
            changed["payload"]["candidate_digest"] = "a" * 64
            with self.assertRaisesRegex(HarnessError, "integrity conflict"):
                receipts.publish(changed)
            payload = deepcopy(record["payload"])
            payload["candidate_digest"] = "b" * 64
            with self.assertRaisesRegex(HarnessError, "replay integrity conflict"):
                managed_review._publish_verdict_record(receipts, index, intent, completion, candidate, kind, payload)
        self.assertEqual(len(supervisor.store.events()), before)
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)

    def conflicting_record(self, kind, *, blocked=False):
        edit = lambda script: script.replace("'verdict':'approved'", "'verdict':'blocked'") if blocked else script
        supervisor, fake = self.fixture(script_edit=edit)
        publish = ExecutionReceipts.publish
        def tamper(instance, record):
            if record["record_type"] == kind:
                record = deepcopy(record)
                if kind == "coverage":
                    record["payload"]["coverage"][0]["status"] = "blocked"
                else:
                    record["payload"]["verdict"] = "approved"
                publish(instance, record)  # Real validator and content-bound journal.
                raise Crash()
            return publish(instance, record)
        with patch.object(ExecutionReceipts, "publish", tamper), self.assertRaises(Crash):
            supervisor.start()
        return supervisor, fake

    def test_conflicting_coverage_is_rejected_without_model_replay(self):
        supervisor, fake = self.conflicting_record("coverage")
        calls = fake.with_suffix(".calls").read_bytes()
        with patch.object(managed_review, "invoke", side_effect=AssertionError("must not call model")):
            with self.assertRaisesRegex(HarnessError, "coverage replay integrity conflict"):
                managed_review.review_candidate(supervisor, supervisor._core(), {"digest": supervisor.state["candidate_digest"]})
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
        self.assertIsNone(supervisor._core().state()["accepted"])

    def test_cached_approval_cannot_override_exact_raw_blocked_verdict(self):
        supervisor, fake = self.conflicting_record("review", blocked=True)
        calls = fake.with_suffix(".calls").read_bytes()
        with patch.object(managed_review, "invoke", side_effect=AssertionError("must not call model")):
            with self.assertRaisesRegex(HarnessError, "raw reviewer verdict"):
                managed_review.review_candidate(supervisor, supervisor._core(), {"digest": supervisor.state["candidate_digest"]})
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
        self.assertIsNone(supervisor._core().state()["accepted"])

    def legacy_coverage(self, *, conflict=False, wall_seconds=verified_fixture.VERIFIED_FLOW_WALL_SECONDS):
        supervisor, fake = self.fixture(wall_seconds=wall_seconds)
        publish = ExecutionReceipts.publish
        def legacy(instance, record):
            if record["record_type"] == "coverage":
                record = deepcopy(record)
                record["record_id"] = record["invocation_id"] + "-coverage"
                if conflict:
                    record["payload"]["candidate_digest"] = "a" * 64
                publish(instance, record)
                raise Crash()
            return publish(instance, record)
        with patch.object(ExecutionReceipts, "publish", legacy), self.assertRaises(Crash):
            supervisor.start()
        return supervisor, fake

    def test_legacy_coverage_only_recovery_keeps_original_id_and_bytes(self):
        supervisor, fake = self.legacy_coverage(wall_seconds=verified_fixture.TWO_PHASE_WALL_SECONDS)
        before = next(r for r in supervisor.receipts().records() if r["record_type"] == "coverage")
        projection = supervisor.store.path("receipts/" + before["record_id"] + ".yaml")
        original = projection.read_bytes()
        recovered = self.recover(supervisor, fake)
        after = next(r for r in recovered.receipts().records() if r["record_type"] == "coverage")
        self.assertEqual(before, after)
        self.assertEqual(original, projection.read_bytes())

    def test_conflicting_legacy_coverage_cannot_publish_duplicate_or_call_model(self):
        supervisor, fake = self.legacy_coverage(conflict=True)
        calls = fake.with_suffix(".calls").read_bytes()
        records = supervisor.receipts().records()
        with patch.object(managed_review, "invoke", side_effect=AssertionError("must not call model")):
            with self.assertRaisesRegex(HarnessError, "legacy replay integrity conflict"):
                managed_review.review_candidate(supervisor, supervisor._core(), {"digest": supervisor.state["candidate_digest"]})
        self.assertEqual(records, supervisor.receipts().records())
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
        self.assertIsNone(supervisor._core().state()["accepted"])

    def test_raw_event_types_and_bounds_fail_with_explicit_protocol_error(self):
        invalid = [b'[]\n', b'null\n', b'{"type":null}\n',
            b'{"type":"item.completed","item":null}\n',
            b'{"type":"item.completed","item":[]}\n',
            b'{"type":"item.completed","item":{"type":"agent_message"}}\n',
            b'{"type":"item.completed","item":{"type":"agent_message","text":42}}\n',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"[]"}}\n',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"null"}}\n',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"{}"}}\n',
            b'', b'not JSON\n', b'x' * (managed_review.MAX_EVENT_BYTES + 1),
            b'x' * (managed_review.MAX_LOG_BYTES + 1)]
        for number, raw in enumerate(invalid):
            with self.subTest(case=number), self.assertRaisesRegex(managed_review.InvalidRawVerdict,
                    "Invalid final-review raw verdict"):
                managed_review._raw_verdict(raw)

    def test_digest_valid_malformed_raw_refuses_fresh_cached_and_replay_without_model(self):
        variants = [b'[]\n', b'{"type":"item.completed","item":null}\n',
            b'{"type":"item.completed","item":{"type":"agent_message","text":42}}\n',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"[]"}}\n']
        original_root = self.root
        for number, raw in enumerate(variants):
            with self.subTest(case=number):
                self.root = original_root / str(number)
                self.root.mkdir()
                supervisor, fake = self.fixture()
                invoke = managed_review.invoke
                def corrupt_transport(*args, **options):
                    result = invoke(*args, **options)
                    self.assertEqual(result["status"], "completed")
                    # Fault injection before completion publication means the
                    # real receipt stores the malformed stream's actual hash.
                    Path(result["logs"]["stdout"]).write_bytes(raw)
                    return result
                with patch.object(managed_review, "invoke", corrupt_transport):
                    result = supervisor.start()
                self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
                self.assertIn("Invalid final-review raw verdict", result["reason"])
                receipts = supervisor.receipts()
                records = receipts.records()
                completion = next(r for r in records if r["record_type"] == "completion" and r["node_id"] == "@review")
                artifact = completion["payload"]["logs"]
                self.assertEqual(artifact["stdout_digest"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(Path(artifact["stdout"]).read_bytes(), raw)
                self.assertFalse(any(r["record_type"] in {"review", "coverage"} for r in records))
                calls = fake.with_suffix(".calls").read_bytes()
                candidate = {"digest": supervisor.state["candidate_digest"]}
                with patch.object(managed_review, "invoke", side_effect=AssertionError("no model replay")):
                    with self.assertRaises(managed_review.InvalidRawVerdict):
                        managed_review.review_candidate(supervisor, supervisor._core(), candidate)
                self.assertEqual(records, receipts.records())
                # A schema-valid forged summary must not turn malformed raw
                # evidence into a cached approval. Publish through the real
                # validator/journal; do not rewrite an immutable completion.
                review = receipts.envelope("review", "@review", completion["attempt_id"], completion["invocation_id"])
                review["payload"] = {"completion_ref": completion["record_id"],
                    "candidate_digest": candidate["digest"], "verdict": "approved", "findings": []}
                if review["schema_version"] == 2:
                    intent = next(r for r in records if r["record_id"] == completion["payload"]["intent_ref"])
                    review["payload"]["review_binding"] = intent["payload"]["review_binding"]
                    review["input_refs"] = list(intent["input_refs"])
                receipts.publish(review)
                with patch.object(managed_review, "invoke", side_effect=AssertionError("no model replay")):
                    with self.assertRaises(managed_review.InvalidRawVerdict):
                        managed_review.review_candidate(supervisor, supervisor._core(), candidate)
                    errors = managed_review.evidence_errors(supervisor, candidate["digest"])
                    self.assertTrue(any("Invalid final-review raw verdict" in error for error in errors), errors)
                    recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
                    result = recovered.resume()
                    self.assertEqual(result["status"], "BLOCKED_AMBIGUOUS", result)
                    self.assertIn("Invalid final-review raw verdict", result["reason"])
                self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
                self.assertIsNone(supervisor._core().state()["accepted"])
        self.root = original_root


if __name__ == "__main__":
    unittest.main()
