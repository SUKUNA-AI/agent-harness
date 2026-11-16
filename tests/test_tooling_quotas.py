"""Mixed-backend scheduling/reservation limits, without client/model qualification."""
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.agent_lifecycle import GlobalSlots, SlotCapacityUnavailable
from agent_harness.scheduling import Scheduler, validate_plan
from agent_harness.work_contract import normalize


class MixedBackendQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.contract = normalize({"schema_version": 1, "mode": "managed",
            "task": {"id": "mixed-quota", "objective": "Exercise shared admission limits",
                "allowed_write_scopes": ["workers/**"]},
            "budgets": {"max_concurrent_managed_agents": 12, "max_wall_seconds": 120},
            "acceptance": [{"id": "independent-check", "kind": "command", "required": True,
                "argv": ["python", "-c", "pass"], "timeout": 5}]})
        self.plan = {"schema_version": 1, "task_id": "mixed-quota", "tasks": []}
        for role, count in (("owner", 14), ("reviewer", 4)):
            for index in range(count):
                backend = "codex" if index % 2 == 0 else "opencode"
                reviewer = role == "reviewer"
                name = ("reviewer-" if reviewer else "worker-") + str(index).zfill(2)
                self.plan["tasks"].append({"id": name, "goal": name, "role": role,
                    "read_only": reviewer, "read_scope": ["**"],
                    "write_scope": [] if reviewer else ["workers/" + name + ".py"],
                    "depends_on": [], "acceptance_ids": ["independent-check"], "outputs": [],
                    "estimated_seconds": 100 if reviewer else 60, "max_attempts": 1,
                    "max_wall_seconds": 120, "risk": "low", "impact": 5,
                    "verification": ["independent-check"], "execution": {"backend": backend,
                        "tool_profile": {"id": "reviewer-readonly" if reviewer else "baseline", "version": 1}}})
        validate_plan(self.plan, self.contract)
        self.scheduler = Scheduler(self.plan, self.contract)

    def test_scheduler_saturates_twelve_total_and_three_reviewers_across_both_backends(self):
        self.assertEqual(self.plan["tooling_schema_version"], 1)
        selected = self.scheduler.next()["selected"]
        self.assertEqual(len(selected), 12)
        reviews = [name for name in selected if self.scheduler.nodes[name]["role"] == "reviewer"]
        workers = [name for name in selected if self.scheduler.nodes[name]["role"] == "owner"]
        self.assertEqual(len(reviews), 3)
        self.assertEqual(len(workers), 9)
        for cohort in (reviews, workers):
            self.assertEqual({self.scheduler.nodes[name]["execution"]["backend"] for name in cohort}, {"codex", "opencode"})
        self.assertEqual(self.scheduler.next(active=selected)["selected"], [])
        # Releasing a worker creates one slot, but cannot admit a fourth reviewer.
        active = [name for name in selected if name != workers[0]]
        replacement = self.scheduler.next(active=active, completed=[workers[0]])["selected"]
        self.assertEqual(len(replacement), 1)
        self.assertEqual(self.scheduler.nodes[replacement[0]]["role"], "owner")
        # Releasing a reviewer permits the waiting reviewer of the other backend.
        active = [name for name in selected if name != reviews[0]]
        replacement = self.scheduler.next(active=active, completed=[reviews[0]])["selected"]
        self.assertEqual(replacement, ["reviewer-03"])
        self.assertEqual(self.scheduler.nodes[replacement[0]]["execution"]["backend"], "opencode")

    def test_global_twelve_reservations_are_shared_by_codex_and_opencode_runs(self):
        registries = {backend: GlobalSlots(self.state) for backend in ("codex", "opencode")}
        leases = []
        with ExitStack() as stack:
            try:
                for name in self.scheduler.next()["selected"]:
                    node = self.scheduler.nodes[name]
                    backend = node["execution"]["backend"]
                    leases.append(stack.enter_context(registries[backend].acquire(
                        "run-" + backend, name, node["role"], timeout=3)))
                inventory = registries["codex"].inventory()
                self.assertEqual(len([row for row in inventory if row["unresolved"]]), 12)
                self.assertEqual({row["run_id"] for row in inventory}, {"run-codex", "run-opencode"})
                self.assertEqual(sum(row["role"] == "reviewer" for row in inventory), 3)
                for backend, registry in registries.items():
                    with self.subTest(backend=backend), self.assertRaises(SlotCapacityUnavailable):
                        with registry.acquire("extra-" + backend, "thirteenth-worker", "owner", timeout=.15):
                            self.fail("A backend obtained a thirteenth global model reservation")
                self.assertEqual(sum(row["unresolved"] for row in registries["opencode"].inventory()), 12)
            finally:
                # Reservations have no child/model invocation; completion releases them normally.
                for lease in leases:
                    lease.complete()
        self.assertTrue(all(not row["unresolved"] for row in registries["codex"].inventory()))

    def test_global_reviewer_limit_is_shared_even_with_nine_free_model_slots(self):
        registries = {backend: GlobalSlots(self.state) for backend in ("codex", "opencode")}
        leases = []
        with ExitStack() as stack:
            try:
                for index in range(3):
                    node = self.scheduler.nodes["reviewer-" + str(index).zfill(2)]
                    backend = node["execution"]["backend"]
                    leases.append(stack.enter_context(registries[backend].acquire(
                        "review-run-" + backend, node["id"], "reviewer", timeout=3)))
                for backend, registry in registries.items():
                    with self.subTest(backend=backend), self.assertRaises(SlotCapacityUnavailable):
                        with registry.acquire("fourth-review-" + backend, "reviewer-extra", "reviewer", timeout=.15):
                            self.fail("A backend obtained a fourth global reviewer reservation")
                # A worker from either backend can still reserve remaining model capacity.
                for backend, registry in registries.items():
                    with registry.acquire("worker-run-" + backend, "worker-extra", "owner", timeout=3) as lease:
                        self.assertEqual(sum(row["unresolved"] for row in registry.inventory()), 4)
                        self.assertEqual(sum(row["unresolved"] and row["role"] == "reviewer" for row in registry.inventory()), 3)
                        lease.complete()
            finally:
                for lease in leases:
                    lease.complete()
        self.assertTrue(all(not row["unresolved"] for row in registries["codex"].inventory()))


if __name__ == "__main__":
    unittest.main()
