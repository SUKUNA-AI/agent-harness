"""Core orchestration fixtures; no installed client or live qualification claim."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, digest
from agent_harness.execution import admit_reviewer, evidence_errors, prepare, revalidate
from agent_harness.strict_yaml import load, validate
import test_tooling_acceptance as tooling_fixtures
import test_verified_flow as verified_fixtures


class NativeVerifierFixture(tooling_fixtures.QualifiedFixtureOpenCode):
    """A typed fixture verifier, deliberately not a production opt-in flag."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = []
        self.qualified = False
        self.proof = None

    def capabilities(self, **kwargs):
        self.observations.append("fresh-caps" if self.qualified else "base-caps")
        value = super().capabilities(**kwargs)
        value.update(managed_leaf_qualified=self.qualified, effective_tool_catalog=self.qualified,
            reviewer_isolation_qualified=self.qualified, exact_resume_qualified=self.qualified)
        if self.qualified:
            value["effective_catalog_digest"] = digest(["fixture-client-read"])
        return value

    def with_tool_config(self, config, **kwargs):
        self.observations.append("configure")
        return super().with_tool_config(config, **kwargs)

    def qualify(self, *, root, role, evidence_dir, timeout):
        self.observations.append("qualify")
        assert self.tool_config is not None and 0 < timeout <= 120
        clone = copy.copy(self)
        clone.qualified = True
        clone.tool_config = copy.deepcopy(self.tool_config)
        clone.tool_config["policies"].append({"action": "fixture.guard", "resource": "*", "effect": "deny"})
        evidence_dir.mkdir(parents=True, exist_ok=True)
        proof = evidence_dir / "fixture-native-proof.json"
        # The fixture contents do not qualify any real backend.
        data = json.dumps({"kind": "fixture-only", "root": str(root), "role": role}).encode()
        proof.write_bytes(data)
        clone.proof = {"path": str(proof), "sha256": hashlib.sha256(data).hexdigest()}
        return clone

    def qualification_receipt(self):
        return self.proof

    def verify_qualification(self, *, root=None, role=None):
        self.observations.append("verify")
        if not self.proof or hashlib.sha256(Path(self.proof["path"]).read_bytes()).hexdigest() != self.proof["sha256"]:
            raise HarnessError("fixture proof changed")
        value = json.loads(Path(self.proof["path"]).read_bytes())
        if value["root"] != str(root) or value["role"] != role:
            raise HarnessError("fixture qualification subject drift")


class ToolingQualificationWiringTests(unittest.TestCase):
    setUp = tooling_fixtures.ToolingAcceptanceTests.setUp
    create = tooling_fixtures.ToolingAcceptanceTests.create
    profile = tooling_fixtures.ToolingAcceptanceTests.profile

    def prepared(self, role="owner"):
        supervisor = self.create(backends=("opencode", "codex"), profile=False)
        supervisor.load()
        fixture = NativeVerifierFixture(self.opencode.executable)
        supervisor.adapter["opencode"] = fixture
        configured, ref = prepare(supervisor, {"backend": "opencode"}, root=self.source,
            role=role, node_id="@review" if role == "reviewer" else "left", attempt_id="fixture-1")
        return supervisor, fixture, configured, ref

    def test_configure_precedes_qualification_and_fresh_guards_and_final_config_hash(self):
        supervisor, fixture, configured, ref = self.prepared()
        self.assertEqual(fixture.observations[:5], ["base-caps", "configure", "qualify", "verify", "fresh-caps"])
        binding = supervisor.store.read(ref["path"], "tool-execution.schema.json")
        self.assertEqual(binding["identity"]["effective_config_digest"], digest(configured.adapter.tool_config))
        self.assertEqual(binding["identity"]["catalog_digest"], digest(["fixture-client-read"]))
        self.assertEqual(binding["identity"]["diagnostic_catalog_digest"], digest([]))
        self.assertEqual(binding["client_qualification"]["role"], "worker")
        self.assertFalse(Path(binding["client_qualification"]["path"]).is_absolute())
        revalidate(configured, binding, {"backend": "opencode"})

    def test_required_capability_is_checked_after_configured_native_verifier(self):
        supervisor = self.create(profile=False)
        supervisor.load()
        fixture = NativeVerifierFixture(self.opencode.executable)
        supervisor.adapter["opencode"] = fixture
        with self.assertRaisesRegex(HarnessError, "required backend capability output_schema"):
            prepare(supervisor, {"backend": "opencode", "required_capabilities": ["output_schema"]},
                root=self.source, role="owner", node_id="left", attempt_id="left-1")
        self.assertIn("qualify", fixture.observations)
        self.assertFalse(self.fake.with_suffix(".calls").exists())

    def test_exact_read_context_is_configured_before_native_qualification_without_a_model_slot(self):
        supervisor = self.create(profile=False)
        supervisor.load()
        fixture = NativeVerifierFixture(self.opencode.executable)
        supervisor.adapter["opencode"] = fixture
        paths = [self.source / "left.py"]
        def configure_reads(adapter, values):
            adapter.observations.append("read-context")
            clone = copy.copy(adapter)
            clone.read_context = list(values)
            return clone
        with patch.object(NativeVerifierFixture, "with_read_context", configure_reads, create=True):
            configured, _ = prepare(supervisor, {"backend": "opencode"}, root=self.source,
                role="reviewer", node_id="@review", attempt_id="context-1", read_context=paths)
            self.assertEqual(fixture.observations[:6], ["base-caps", "configure", "read-context", "qualify", "verify", "fresh-caps"])
            self.assertEqual(configured.adapter.read_context, paths)
            with self.assertRaisesRegex(HarnessError, "read-only context grants unavailable"):
                prepare(supervisor, {"backend": "opencode"}, root=self.source,
                    role="owner", node_id="left", attempt_id="context-worker", read_context=paths)
        self.assertFalse(self.fake.with_suffix(".calls").exists())

    def test_readonly_reviewer_admission_does_not_upgrade_state_or_reserve_round(self):
        # Use the real verified-flow state so an accidental round reservation
        # or tooling-state migration is observable.
        fixture_test = verified_fixtures.VerifiedFlowTests()
        fixture_test.setUp()
        self.addCleanup(fixture_test.doCleanups)
        supervisor, fake = fixture_test.fixture()
        supervisor.load()
        supervisor.contract["review_execution"] = {"backend": "opencode"}
        fixture = NativeVerifierFixture(self.opencode.executable)
        supervisor.adapter = {"opencode": fixture}
        before = copy.deepcopy(supervisor.state)
        before_journal = (supervisor.path / "journal/events.jsonl").read_bytes()
        admit_reviewer(supervisor)
        self.assertEqual(supervisor.state, before)
        self.assertEqual((supervisor.path / "journal/events.jsonl").read_bytes(), before_journal)
        self.assertEqual(supervisor.state["plan_quality"]["rounds_used"], 0)
        self.assertFalse(fake.with_suffix(".calls").exists())

    def test_prior_missing_proof_blocks_before_requalification_or_new_session(self):
        supervisor, fixture, configured, ref = self.prepared()
        Path(configured.adapter.proof["path"]).unlink()
        count = fixture.observations.count("qualify")
        with self.assertRaises(HarnessError):
            prepare(supervisor, {"backend": "opencode"}, root=self.source, role="owner",
                node_id="left", attempt_id="fixture-1", prior=ref)
        self.assertEqual(fixture.observations.count("qualify"), count)
        self.assertFalse(self.fake.with_suffix(".calls").exists())

    def test_reviewer_proof_loss_or_drift_invalidates_bound_gate_evidence(self):
        supervisor, fixture, configured, ref = self.prepared("reviewer")
        binding = supervisor.store.read(ref["path"], "tool-execution.schema.json")
        base = {"invocation_id": "fixture-invocation", "run_id": binding["run_id"],
            "project_id": binding["repository_identity"], "node_id": binding["node_id"],
            "attempt_id": binding["attempt_id"], "capability_receipt_ref": ref["digest"], "input_refs": [ref["digest"]]}
        rows = [{**base, "record_type": "invocation_intent", "payload": {"checkout": str(self.source)}},
                {**base, "record_type": "completion", "payload": {"outcome": "completed", "thread_id": "ses_fixture"}}]
        calls = []
        verifier = types.ModuleType("agent_harness.opencode_qualification")
        verifier.verify_qualification_evidence = lambda *args, **kwargs: calls.append(kwargs)
        proof = Path(configured.adapter.proof["path"])
        original = proof.read_bytes()
        with patch.object(supervisor, "receipts", return_value=types.SimpleNamespace(records=lambda: rows)), patch.dict(sys.modules,
                {"agent_harness.opencode_qualification": verifier}), patch(
                    "agent_harness.execution_receipts.verify_tool_artifacts", create=True,
                    return_value=[{"path": str(proof), "sha256": configured.adapter.proof["sha256"]}]):
            self.assertEqual(evidence_errors(supervisor), [])
            self.assertEqual(calls[0]["session_id"], "ses_fixture")
            self.assertTrue(calls[0]["require_execution"])
            self.assertEqual(calls[0]["execution_log"], {"path": None, "sha256": None})
            self.assertEqual(calls[0]["catalog_artifacts"][0]["sha256"], configured.adapter.proof["sha256"])
            with patch("agent_harness.execution_receipts.verify_tool_artifacts", create=True, return_value=[]):
                self.assertTrue(any("qualification" in item for item in evidence_errors(supervisor)))
            rows[1]["payload"]["thread_id"] = None
            self.assertTrue(any("qualification" in item for item in evidence_errors(supervisor)))
            rows[1]["payload"]["thread_id"] = "ses_fixture"
            proof.unlink()
            self.assertTrue(any("qualification" in item for item in evidence_errors(supervisor)))
            proof.write_bytes(original + b" ")
            self.assertTrue(any("qualification" in item for item in evidence_errors(supervisor)))
            proof.write_bytes(original)
            self.assertEqual(evidence_errors(supervisor), [])

    def test_strict_proof_reference_forbids_unknown_fields_and_nonhash_digest(self):
        supervisor, _, _, ref = self.prepared()
        binding = supervisor.store.read(ref["path"], "tool-execution.schema.json")
        for change in ({"sha256": "trusted"}, {"trust": True}, {"role": "admin"}):
            value = copy.deepcopy(binding)
            value["client_qualification"].update(change)
            with self.assertRaises(ValueError):
                validate(value, "tool-execution.schema.json")


if __name__ == "__main__":
    unittest.main()
