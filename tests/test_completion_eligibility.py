"""R02 original-mission optional work and frozen review version controls."""
from copy import deepcopy
import os
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.supervisor import Supervisor
from agent_harness.core import HarnessError, digest
from agent_harness import completion_eligibility as eligibility, managed_review
from agent_harness.execution_receipts import ExecutionReceipts, validate_record
from agent_harness.strict_yaml import dump, validate, _schema
from agent_harness.file_io import read_regular_file
from agent_harness.task_packets import schema_file
from agent_harness.scheduling import validate_plan
import test_verified_flow as fixtures


class CompletionEligibilityTests(unittest.TestCase):
    fixture = fixtures.VerifiedFlowTests.fixture

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def optional(contract):
        row = deepcopy(contract["mission"]["requirements"][0])
        row.update(id="R-later", required=False, text="Optional future work")
        contract["mission"]["requirements"].append(row)

    def test_optional_never_planned_is_deferred_and_does_not_block(self):
        supervisor, fake = self.fixture(contract_edit=self.optional)
        # A historical active schema may exist already. A new policy must use
        # its own addressed schema without changing these old bytes.
        legacy = supervisor.store.path("plans/final-review-schema.json")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        old_bytes = read_regular_file(schema_file(supervisor, "managed-review.schema.json", 2))
        legacy.write_bytes(old_bytes)
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        rows = {row["id"]: row for row in result["progress"]["requirements"]}
        self.assertEqual(rows["R-later"]["status"], "deferred")
        self.assertTrue(rows["R-later"]["reason"])
        self.assertEqual(rows["R-later"]["evidence_ids"], [])
        self.assertTrue(supervisor.status(verify=True)["evidence_current"])
        self.assertEqual(legacy.read_bytes(), old_bytes)
        records = supervisor.receipts().records()
        for record in records:
            validate_record(record)
            self.assertEqual(record["schema_version"], 2)
        review = next(r for r in records if r["record_type"] == "review")
        binding = review["payload"]["review_binding"]
        self.assertEqual(binding["review_version"], 3)
        self.assertIn(binding["schema_digest"], binding["schema_path"])
        self.assertEqual(hashlib.sha256(read_regular_file(binding["schema_path"])).hexdigest(), binding["schema_digest"])
        self.assertTrue({binding["policy_digest"], binding["schema_digest"], digest(binding)} <= set(review["input_refs"]))
        proof = next(r for r in records if r["record_type"] == "coverage")
        self.assertEqual(next(r for r in proof["payload"]["coverage"] if r["id"] == "R-later")["status"], "deferred")
        calls = fake.with_suffix(".calls").read_bytes()
        self.assertEqual(Supervisor(supervisor.path, adapter=supervisor.adapter).resume()["status"], "ACCEPTED")
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
        # Readers may not rebuild missing addressed schemas while claiming
        # current acceptance, or treat altered policy/schema bytes as current.
        schema_path = Path(binding["schema_path"])
        schema_bytes = schema_path.read_bytes()
        for value in (None, schema_bytes + b" "):
            if value is None:
                schema_path.unlink()
            else:
                schema_path.write_bytes(value)
            self.assertEqual(supervisor.status(verify=True)["status"], "STALE")
            self.assertEqual(schema_path.exists(), value is not None)
            with self.assertRaises((HarnessError, OSError)):
                managed_review.review_candidate(supervisor, supervisor._core(), {"digest": supervisor.state["candidate_digest"]})
            self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
            schema_path.write_bytes(schema_bytes)
        policy_path = supervisor.store.path(eligibility.POLICY_PATH)
        policy_bytes = policy_path.read_bytes()
        policy_path.write_bytes(policy_bytes + b" ")
        with self.assertRaisesRegex(HarnessError, "policy integrity"):
            eligibility.policy(supervisor)
        policy_path.write_bytes(policy_bytes)
        self.assertTrue(supervisor.status(verify=True)["evidence_current"])

        # Every persisted subject uses one explicit receipt version and one
        # policy/schema binding; a structurally valid mixed chain is refused.
        for target in ("review", "completion", "invocation_intent"):
            mutated = deepcopy(records)
            review_copy = next(r for r in mutated if r["record_id"] == review["record_id"])
            completion = next(r for r in mutated if r["record_id"] == review_copy["payload"]["completion_ref"])
            intent = next(r for r in mutated if r["record_id"] == completion["payload"]["intent_ref"])
            subject = {"review": review_copy, "completion": completion, "invocation_intent": intent}[target]
            subject["schema_version"] = 1
            subject["payload"].pop("review_binding", None)
            validate_record(subject)
            with self.subTest(target=target):
                self.assertTrue(managed_review._record_errors(mutated, review_copy))
        mutated = deepcopy(review)
        mutated["payload"]["review_binding"]["policy_digest"] = "0" * 64
        mutated["input_refs"] = [supervisor.state["contract_digest"], "0" * 64,
            binding["schema_digest"], digest(mutated["payload"]["review_binding"])]
        validate_record(mutated)
        self.assertTrue(managed_review._record_errors(records, mutated))

    def test_planned_optional_prerequisite_is_promised_and_stays_freshness_critical(self):
        def optional_left(contract):
            contract["mission"]["requirements"][0]["required"] = False
            self.optional(contract)
        supervisor, fake = self.fixture(contract_edit=optional_left)
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        row = result["progress"]["requirements"][0]
        self.assertEqual((row["required"], row["planned"], row["status"]), (False, True, "accepted"))
        self.assertTrue(row["evidence_ids"])
        records = supervisor.receipts().records()
        right = next(i for i, r in enumerate(records) if r["record_type"] == "invocation_intent" and r["node_id"] == "right")
        self.assertTrue(any(r["record_type"] == "node_acceptance" and r["node_id"] == "left" for r in records[:right]))
        accepted = next(r for r in records if r["record_id"] == row["evidence_ids"][0])
        check = next(r for r in records if r["record_id"] == accepted["payload"]["integrated_ref"])
        raw_check = Path(check["payload"]["verification_run"]) / "state.json"
        raw_check.write_text("{}")
        calls = fake.with_suffix(".calls").read_bytes()
        self.assertEqual(supervisor.status(verify=True)["status"], "STALE")
        with self.assertRaises(HarnessError):
            managed_review.review_candidate(supervisor, supervisor._core(), {"digest": supervisor.state["candidate_digest"]})
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)

    def test_failed_optional_planned_prerequisite_cannot_be_deferred(self):
        supervisor, fake = self.fixture(attempts=1,
            contract_edit=lambda c: c["mission"]["requirements"][0].update(required=False),
            script_edit=lambda s: s.replace("if mode in ('left','right'):target=mode+'.py'",
                "if mode in ('left','right'):target=mode+'.py'\nif mode=='left':value=0"))
        result = supervisor.start()
        self.assertEqual(result["status"], "FAILED_TERMINAL", result)
        self.assertFalse(result["progress"]["execution_complete"])
        optional = result["progress"]["requirements"][0]
        self.assertEqual((optional["required"], optional["planned"], optional["status"]), (False, True, "pending"))
        self.assertFalse(any(r["node_id"] == "right" and r["record_type"] == "invocation_intent"
            for r in supervisor.receipts().records()))
        self.assertEqual(fake.with_suffix(".calls").read_text().splitlines(), ["exec", "exec"])

    def test_refined_optional_deferral_survives_fresh_cached_and_strict_status(self):
        detail = "; no additional output was promised."
        supervisor, fake = self.fixture(contract_edit=self.optional,
            script_edit=lambda s: s.replace("'reason':r['reason']",
                "'reason':r['reason']+(" + repr(detail) + " if r['status']=='deferred' else '')"))
        result = supervisor.start()
        self.assertEqual(result["status"], "ACCEPTED", result)
        records = supervisor.receipts().records()
        proof = next(r for r in records if r["record_type"] == "coverage")
        validate_record(proof)
        deferred = next(r for r in proof["payload"]["coverage"] if r["id"] == "R-later")
        self.assertEqual(deferred["reason"], eligibility.DEFER_REASON + detail)
        self.assertEqual((deferred["status"], deferred["evidence_ids"]), ("deferred", []))
        current = next(r for r in result["progress"]["requirements"] if r["id"] == "R-later")
        self.assertEqual(current["reason"], eligibility.DEFER_REASON)
        calls = fake.with_suffix(".calls").read_bytes()
        review = managed_review.review_candidate(supervisor, supervisor._core(), {"digest": supervisor.state["candidate_digest"]})
        self.assertEqual(review["verdict"], "approved")
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        self.assertEqual(recovered.resume()["status"], "ACCEPTED")
        self.assertTrue(recovered.status(verify=True)["evidence_current"])
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
        self.assertEqual(recovered.receipts().records(), records)

    def test_refined_reason_cannot_defer_required_work_in_fresh_review_or_replay(self):
        reason = eligibility.DEFER_REASON + "; no additional output was promised."
        def edit(script):
            return script.replace("'status':'deferred' if r['status']=='deferred' else 'accepted'", "'status':'deferred'").replace(
                "'evidence_ids':r['evidence_ids']", "'evidence_ids':[]").replace("'reason':r['reason']", "'reason':" + repr(reason))
        supervisor, fake = self.fixture(script_edit=edit)
        self.assertNotEqual(supervisor.start()["status"], "ACCEPTED")
        self.assertFalse(any(r["record_type"] == "review" for r in supervisor.receipts().records()))
        calls = fake.with_suffix(".calls").read_bytes()
        with self.assertRaisesRegex(HarnessError, "required or promised"):
            managed_review.review_candidate(supervisor, supervisor._core(), {"digest": supervisor.state["candidate_digest"]})
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)

    def test_original_omission_blocks_fresh_review_and_completion_replay(self):
        supervisor, fake = self.fixture(contract_edit=self.optional,
            script_edit=lambda s: s.replace("'unrepresented_requirements':[]", "'unrepresented_requirements':['Real requested outcome omitted']"))
        result = supervisor.start()
        self.assertNotEqual(result["status"], "ACCEPTED", result)
        records = supervisor.receipts().records()
        self.assertFalse(any(r["record_type"] == "review" for r in records))
        calls = fake.with_suffix(".calls").read_bytes()
        with self.assertRaisesRegex(HarnessError, "Original mission coverage"):
            managed_review.review_candidate(supervisor, supervisor._core(), {"digest": supervisor.state["candidate_digest"]})
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)

    def test_legacy_coverage_crash_resumes_old_versions_and_schema_bytes(self):
        with patch.object(eligibility, "pin", return_value=None):
            supervisor, fake = self.fixture(legacy=True)
        publish = ExecutionReceipts.publish
        def crash(receipts, record):
            value = publish(receipts, record)
            if record["record_type"] == "coverage":
                raise fixtures.Crash()
            return value
        with patch.object(ExecutionReceipts, "publish", crash), self.assertRaises(fixtures.Crash):
            supervisor.start()
        schema = supervisor.store.path("plans/final-review-schema.json")
        before = schema.read_bytes()
        self.assertEqual(json.loads(before)["properties"]["schema_version"], {"const": 2})
        original_deadline = supervisor.state["deadline"]
        calls = fake.with_suffix(".calls").read_bytes()
        recovered = Supervisor(supervisor.path, adapter=supervisor.adapter)
        self.assertEqual(recovered.resume()["status"], "ACCEPTED")
        self.assertEqual(recovered.state["deadline"], original_deadline)
        self.assertEqual(schema.read_bytes(), before)
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
        self.assertTrue(all(r["schema_version"] == 1 for r in recovered.receipts().records()))
        self.assertIsNone(eligibility.policy(recovered))

    def test_requiredness_successor_invalidates_old_completeness_without_budget_reset(self):
        supervisor, fake = self.fixture(contract_edit=self.optional)
        self.assertEqual(supervisor.start()["status"], "ACCEPTED")
        deadline = supervisor.state["deadline"]
        attempts = supervisor.state["attempts_used"]
        metrics = deepcopy(supervisor.state["metrics"])
        old_plan = supervisor.store.read(supervisor.state["plan_path"], "agent-plan.schema.json")
        revised = deepcopy(supervisor.contract)
        revised["mission"].update(generation=2, previous_digest=digest(supervisor.contract["mission"]))
        revised["mission"]["requirements"][-1]["required"] = True
        path = self.root / "successor.yaml"
        dump(path, revised)
        calls = fake.with_suffix(".calls").read_bytes()
        result = supervisor.update_mission(path, reason="User now requires previously optional output")
        self.assertEqual(result["status"], "NEW")
        self.assertFalse(result["progress"]["complete"])
        self.assertIsNone(supervisor.state["plan_path"])
        self.assertEqual(supervisor.state["deadline"], deadline)
        self.assertEqual(supervisor.state["attempts_used"], attempts)
        self.assertEqual(supervisor.state["metrics"]["model_invocations"], metrics["model_invocations"])
        self.assertEqual(fake.with_suffix(".calls").read_bytes(), calls)
        with self.assertRaisesRegex(HarnessError, "different mission generation"):
            validate_plan(old_plan, supervisor.contract)
        revised_plan = deepcopy(old_plan)
        revised_plan.update(generation=2, mission_digest=digest(supervisor.contract["mission"]))
        with self.assertRaisesRegex(HarnessError, "MISSION_COVERAGE_INCOMPLETE"):
            validate_plan(revised_plan, supervisor.contract)

    def test_shared_policy_rejects_promised_deferral_incomplete_nodes_and_id_omissions(self):
        contract = {"mission": {"requirements": [{"id": "required", "required": True},
            {"id": "promised", "required": False}, {"id": "later", "required": False}]}}
        plan = {"tasks": [{"id": "a", "requirement_ids": ["required"]}, {"id": "b", "requirement_ids": ["promised"]}]}
        current = eligibility.rows(contract, plan, {"a": {"record_id": "a-proof"}})
        self.assertFalse(eligibility.complete(current))
        current = eligibility.rows(contract, plan, {"a": {"record_id": "a-proof"}, "b": {"record_id": "b-proof"}})
        self.assertTrue(eligibility.complete(current))
        verdict = {"schema_version": 3, "verdict": "approved", "findings": [], "unrepresented_requirements": [],
            "requirements": [{key: row[key] for key in ("id", "status", "reason", "evidence_ids")} for row in current]}
        validate(verdict, "managed-review.v3.schema.json")
        eligibility.validate_review(current, verdict)
        refined = deepcopy(verdict)
        refined["requirements"][-1]["reason"] = eligibility.DEFER_REASON + "; no additional output was promised."
        validate(refined, "managed-review.v3.schema.json")
        eligibility.validate_review(current, refined)
        for reason in (eligibility.DEFER_REASON + ";", eligibility.DEFER_REASON + "; ",
                eligibility.DEFER_REASON + "; \n\t", eligibility.DEFER_REASON + " no separator"):
            broken = deepcopy(verdict)
            broken["requirements"][-1]["reason"] = reason
            with self.subTest(reason=reason), self.assertRaises(HarnessError):
                eligibility.validate_review(current, broken)
        for variant in ("required-deferred", "promised-deferred", "required-refined-deferred", "promised-refined-deferred",
                "required-empty-base-deferred", "promised-empty-base-deferred",
                "required-blocked-deferred", "promised-blocked-deferred",
                "missing", "duplicate", "unknown", "original-omission", "fake-reason"):
            broken = deepcopy(verdict)
            if variant.endswith("deferred"):
                row = broken["requirements"][0 if variant.startswith("required") else 1]
                row.update(status="deferred", reason=eligibility.DEFER_REASON + ("; reviewer detail" if "refined" in variant else ""), evidence_ids=[])
                if "empty-base" in variant or "blocked" in variant:
                    # Accepted rows have an empty base reason. This would pass
                    # refinement syntax if the authorization guard disappeared;
                    # a well-formed explanation must never grant deferral.
                    row["reason"] = "; reviewer detail"
                if "blocked" in variant:
                    # The unconditional rule also applies before an approved
                    # verdict. Exercise it independently of approval's second
                    # required/planned coverage guard.
                    broken["verdict"] = "blocked"
                    validate(broken, "managed-review.v3.schema.json")
            elif variant == "missing":
                broken["requirements"].pop()
            elif variant == "duplicate":
                broken["requirements"][1] = deepcopy(broken["requirements"][0])
            elif variant == "unknown":
                broken["requirements"][0]["id"] = "unknown"
            elif variant == "original-omission":
                broken["unrepresented_requirements"] = ["Real original deliverable omitted"]
            else:
                broken["requirements"][-1]["reason"] = "model chose to skip"
            with self.subTest(variant=variant), self.assertRaises(HarnessError):
                eligibility.validate_review(current, broken)

    def test_accepted_rows_require_nonempty_evidence_in_both_wire_schemas(self):
        row = {"id": "R-left", "status": "accepted", "reason": "", "evidence_ids": ["proof"]}
        verdict = {"schema_version": 3, "verdict": "approved", "findings": [], "requirements": [deepcopy(row)], "unrepresented_requirements": []}
        validate(verdict, "managed-review.v3.schema.json")
        verdict["requirements"][0]["evidence_ids"] = []
        with self.assertRaises(ValueError):
            validate(verdict, "managed-review.v3.schema.json")
        supervisor, _ = self.fixture()
        binding = managed_review._review_binding(supervisor)
        proof = supervisor.receipts().envelope("coverage", "@coverage", "format-control", "reviewer-invocation")
        proof["payload"] = {"candidate_digest": "a" * 64, "mission_digest": digest(supervisor.contract["mission"]),
            "review_binding": binding, "coverage": [row]}
        proof["input_refs"].extend([binding["policy_digest"], binding["schema_digest"], digest(binding)])
        proof["payload_digest"] = digest(proof)
        validate_record(proof)
        row["evidence_ids"] = []
        with self.assertRaises(ValueError):
            validate(proof, "execution-receipt.v2.schema.json")
        with self.assertRaises(ValueError):
            validate_record(proof)

    def test_frozen_schema_version_matrix_and_reserved_plan_quality_payload(self):
        for version in (1, 2, 99):
            verdict = {"schema_version": version, "verdict": "approved", "findings": []}
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate(verdict, "managed-review.v3.schema.json")
        row = {"id": "later", "status": "deferred", "reason": "", "evidence_ids": []}
        verdict = {"schema_version": 3, "verdict": "approved", "findings": [], "requirements": [row], "unrepresented_requirements": []}
        with self.assertRaises(ValueError):
            validate(verdict, "managed-review.v3.schema.json")
        self.assertEqual(_schema("execution-receipt.schema.json")["properties"]["schema_version"], {"const": 1})
        self.assertEqual(_schema("execution-receipt.v2.schema.json")["properties"]["schema_version"], {"const": 2})
        supervisor, _ = self.fixture()
        receipt = supervisor.receipts().envelope("plan_quality_assessment", "@plan-quality", "round-1", "reviewer-invocation")
        receipt["payload"] = {key: "a" * 64 for key in ("assessment_digest", "mission_digest", "contract_digest", "plan_digest", "policy_digest", "input_digest", "output_schema_digest")}
        receipt["payload"].update(completion_ref="completion", assessment_ref="assessment.json", round=1, verdict="requires_revision")
        receipt["payload_digest"] = digest(receipt)
        validate_record(receipt)
        for change in ({"schema_version": 99}, {"schema_version": 1}, {"payload": dict(receipt["payload"], future_unknown=True)},
                {"payload": dict(receipt["payload"], verdict="changes_requested")}):
            with self.subTest(change=change), self.assertRaises((HarnessError, ValueError)):
                validate_record(dict(receipt, **change))


if __name__ == "__main__":
    unittest.main()
