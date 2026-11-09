"""Version 1 project-owned custom tasks, answers and observable fault schedule.

These six semantic tasks are neither an official benchmark split nor coverage of
all F-regressions. Real Codex interruption/compact/resume is a separate run.
"""
import json
from pathlib import Path

from . import strict_yaml
from .benchmark_bundles import BUNDLE, ORACLE, _hash
from .core import HarnessError, digest
from .file_io import read_regular_file
from .supervisor_store import SupervisorStore, atomic_write

REVISION = "harness-custom-corpus-v1"
CODE = {"src/service.py": "def total(values):\n    return sum(values)\n\ndef checkout(values):\n    return total(values)\n",
        "tests/test_service.py": "from src.service import total\n\ndef test_total():\n    assert total([2, 3]) == 5\n",
        "src/ui.py": "from .service import checkout\n\ndef submit(values):\n    return checkout(values)\n",
        "src/unrelated.py": "def unused():\n    return 99\n"}
CORPUS = (
    {"suite": "harness-recovery", "id": "pending-wave", "question": "From state.json compute the remaining topological order, completed nodes, and known input/output token totals. Never rerun a completed node. Write answer.json with keys remaining, completed, input_tokens, output_tokens.",
     "files": {"state.json": json.dumps({"nodes": [{"id": "left", "depends_on": [], "status": "completed"}, {"id": "right", "depends_on": [], "status": "pending"}, {"id": "join", "depends_on": ["left", "right"], "status": "pending"}], "receipts": [{"invocation_id": "left-1", "status": "completed", "input_tokens": 5, "output_tokens": 2}]}) + "\n"},
     "answer": {"remaining": ["right", "join"], "completed": ["left"], "input_tokens": 5, "output_tokens": 2}},
    {"suite": "harness-recovery", "id": "unknown-outcome", "question": "Inspect invocations.json. A dispatched invocation without completion must be reconciled, never blindly retried. Write answer.json keys may_retry, must_reconcile, coverage, known_tokens. Count each invocation once.",
     "files": {"invocations.json": json.dumps([{"id": "a", "intent": True, "completion": {"input_tokens": 5, "output_tokens": 2}}, {"id": "b", "intent": True, "completion": None}]) + "\n"},
     "answer": {"may_retry": [], "must_reconcile": ["b"], "coverage": "partial", "known_tokens": 7}},
    {"suite": "harness-retrieval-long-task", "id": "related-tests", "question": "Find the test for src/service.py::total. Write answer.json with related_tests as a sorted array of physical path, start_line, end_line objects; include the actual test function only, not imports.",
     "files": CODE, "answer": {"related_tests": [{"path": "tests/test_service.py", "start_line": 3, "end_line": 4}]}},
    {"suite": "harness-retrieval-long-task", "id": "trace-to-code", "question": "Trace: src/ui.py:4 in submit -> src/service.py:5 in checkout -> src/service.py:2 in total. Write answer.json key frames as ordered physical path/line/function objects starting at submit.",
     "files": CODE, "answer": {"frames": [{"path": "src/ui.py", "line": 4, "function": "submit"}, {"path": "src/service.py", "line": 5, "function": "checkout"}, {"path": "src/service.py", "line": 2, "function": "total"}]}},
    {"suite": "harness-retrieval-long-task", "id": "change-impact", "question": "If total changes return behavior, find its direct production caller, transitive production caller, and direct test. Write answer.json keys direct, transitive, tests, each sorted strings path::symbol. Ignore unused functions.",
     "files": CODE, "answer": {"direct": ["src/service.py::checkout"], "transitive": ["src/ui.py::submit"], "tests": ["tests/test_service.py::test_total"]}},
    {"suite": "harness-retrieval-long-task", "id": "symbol-context", "question": "Return minimal context for src/service.py::checkout: definition span and resolved callee span. Write answer.json keys definition and callees as physical path/start_line/end_line/symbol objects, with callees sorted by path then line.",
     "files": CODE, "answer": {"definition": {"path": "src/service.py", "start_line": 4, "end_line": 5, "symbol": "checkout"}, "callees": [{"path": "src/service.py", "start_line": 1, "end_line": 2, "symbol": "total"}]}})


def export_own_corpus(destination):
    """Create one pinned asset bundle; no model, network, evaluator or downloads."""
    root = Path(destination).absolute()
    store = SupervisorStore(root)
    with store.lease("export versioned own benchmark corpus"):
        if any(path.name != "locks" for path in root.iterdir()):
            raise HarnessError("BENCH_CORPUS_DESTINATION_EXISTS")
        def put(name, raw):
            if isinstance(raw, str):
                raw = raw.encode()
            atomic_write(store.path(name), raw, exclusive=True)
            return {"path": name, "sha256": _hash(raw)}
        license_ref = put("provenance.txt", "Project-owned custom evaluation data authored in this repository. No third-party task assets imported. Repository distribution terms apply; no official dataset license or leaderboard equivalence asserted.\n")
        evaluator = put("evaluator.py", read_regular_file(Path(__file__).with_name("benchmark_bundle_evaluator.py")))
        adapter = put("adapter.py", read_regular_file(Path(__file__).with_name("benchmark_bundles.py")))
        toolchain = put("toolchain.json", json.dumps({"profile": "artifact-json-v1", "python": ">=3.11", "network": "requires separately qualified deny boundary", "runtime": "existing CodexAdapter/Supervisor", "observed_qualification": "unavailable"}, sort_keys=True))
        tasks = []
        for item in CORPUS:
            prefix = "tasks/" + item["id"] + "/"
            checkpoint = {"stage": "inputs_verified", "task": item["id"]}
            objective = item["question"] + " First read README.md and inputs, then write checkpoint.json exactly " + json.dumps(checkpoint, sort_keys=True) + ". The external controller may interrupt at this artifact milestone. On continuation preserve checkpoint.json and produce answer.json. These are custom semantic tasks; do not claim an actual harness recovery test."
            files = [{"path": name, "source": put(prefix + "source/" + name, raw)} for name, raw in sorted({**item["files"], "README.md": objective + "\n"}.items())]
            answer = put(prefix + "gold/answer.json", json.dumps(item["answer"], sort_keys=True) + "\n")
            marker = put(prefix + "gold/checkpoint.json", json.dumps(checkpoint, sort_keys=True) + "\n")
            oracle = {"schema_version": 1, "record_type": "artifact_json_oracle", "checks": [{"path": "answer.json", "expected": answer}, {"path": "checkpoint.json", "expected": marker}]}
            oracle_ref = put(prefix + "gold/oracle.yaml", strict_yaml.dumps(oracle, ORACLE))
            bundle = {"schema_version": 1, "record_type": "benchmark_task_bundle", "suite": item["suite"], "task_id": item["id"], "revision": REVISION,
                "provenance": "project_owned", "api_profile": "artifact-json-v1", "license": license_ref, "objective": objective,
                "files": files, "writable_paths": ["checkpoint.json", "answer.json"], "oracle": oracle_ref,
                "milestones": [{"id": "inputs-verified", "artifact": "checkpoint.json", "expected": marker, "fault": "after_durable_checkpoint" if item["suite"] == "harness-recovery" else "controller_restart"}, {"id": "answer-published", "artifact": "answer.json", "expected": answer, "fault": "observe"}],
                "max_attempts": 2, "max_wall_seconds": 120, "turn_timeout_seconds": 30}
            ref = put(prefix + "bundle.yaml", strict_yaml.dumps(bundle, BUNDLE))
            tasks.append({"suite": item["suite"], "id": item["id"], "source": ref})
        index = put("corpus-index.json", json.dumps({"schema_version": 1, "revision": REVISION, "corpus_digest": digest(CORPUS), "scope": "six custom semantic tasks; not all F-regressions or live recovery", "tasks": tasks}, sort_keys=True))
    return {"revision": REVISION, "corpus_digest": digest(CORPUS), "index": index, "evaluator": evaluator,
        "adapter": adapter, "toolchain": toolchain, "suites": [{"id": suite, "revision": REVISION, "assets": index, "evaluator": evaluator, "adapter": adapter, "toolchain": toolchain,
            "tasks": [{"id": task["id"], "source": task["source"]} for task in tasks if task["suite"] == suite]} for suite in ("harness-recovery", "harness-retrieval-long-task")]}
