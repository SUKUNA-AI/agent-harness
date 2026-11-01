"""Paired Python/Rust snapshot and whole-check-cycle measurements, no model calls."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_harness.core import Run, canonical, snapshot
from agent_harness.native import backend_info
from agent_harness.runner import gate, run_check


@contextmanager
def backend(name):
    previous = os.environ.get("AGENT_HARNESS_BACKEND")
    os.environ["AGENT_HARNESS_BACKEND"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENT_HARNESS_BACKEND", None)
        else:
            os.environ["AGENT_HARNESS_BACKEND"] = previous


def sample(name, contract, run_dir):
    with backend(name):
        started = time.perf_counter()
        observed = snapshot(contract)
        snapshot_seconds = time.perf_counter() - started
        started = time.perf_counter()
        run = Run.create(run_dir, contract)
        frozen = run.freeze()
        statuses = [run_check(run, check["id"])["status"] for check in contract["checks"]]
        verdict = gate(run)["verdict"]
        cycle_seconds = time.perf_counter() - started
        assert statuses == ["passed"] * len(statuses) and verdict == "accepted"
        assert observed["digest"] == frozen["digest"]
        return {"backend": name, "snapshot_seconds": snapshot_seconds, "cycle_seconds": cycle_seconds,
                "snapshot_digest": observed["digest"], "verdict": verdict}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=3000)
    parser.add_argument("--bytes-per-file", type=int, default=16384)
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--candidate-backend", choices=("native", "auto"), default="native")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.files <= 50000 or not 64 <= args.bytes_per_file <= 1048576 or not 1 <= args.pairs <= 20:
        parser.error("Use 1..50000 files, 64..1048576 bytes/file, 1..20 pairs")
    if args.files * args.bytes_per_file > 1024 * 1024 * 1024:
        parser.error("Limit the generated fixture to 1 GiB")
    with backend("native"):
        info = backend_info()
        assert info["available"], "Native binary is mandatory for a comparison"
    with tempfile.TemporaryDirectory(prefix="harness-native-profile-") as directory:
        base = Path(directory).resolve()
        source = base / "source"
        source.mkdir()
        seed = b"native snapshot measurement\n"
        data = (seed * (args.bytes_per_file // len(seed) + 1))[:args.bytes_per_file]
        for index in range(args.files):
            folder = source / f"part-{index // 100:04}"
            folder.mkdir(exist_ok=True)
            (folder / f"source-{index:06}.txt").write_bytes(data)
        for argv in (["init", "-q"], ["config", "core.autocrlf", "false"], ["add", "."],
                     ["-c", "user.name=Native Fixture", "-c", "user.email=native-fixture@example.invalid", "commit", "-qm", "fixture"]):
            subprocess.run(["git", "-C", str(source), *argv], capture_output=True, check=True)
        contract = {"schema_version": 1, "run_id": "profile", "task_id": "profile", "attempt_id": "a1",
                    "workflow": "change", "process_level": "standard", "goal": "Compare native snapshot implementations",
                    "cwd": str(source), "permissions": ["read", "test"], "scope": {"write_paths": [], "exclude": []},
                    "checks": [{"id": f"case-{i}", "kind": "command", "required": True,
                                "argv": [sys.executable, "-I", "-B", "-c", "print('fixture passed')"],
                                "timeout": 15, "expected_exit": 0,
                                "assertions": [{"stream": "stdout", "regex": "fixture passed"}]} for i in range(3)],
                    "budget": {"max_attempts": 1, "native_cap": 1, "root_counts": False,
                               "native_cap_source": "isolated performance fixture"}}
        candidate = args.candidate_backend
        warmups = [sample(name, contract, base / ("warmup-" + name)) for name in ("python", candidate)]
        pairs = []
        for index in range(args.pairs):
            order = ("python", candidate) if index % 2 == 0 else (candidate, "python")
            row = {name: sample(name, contract, base / f"pair-{index}-{name}") for name in order}
            assert row["python"]["snapshot_digest"] == row[candidate]["snapshot_digest"], "Snapshot parity failed"
            pairs.append(row)
        assert len({r["snapshot_digest"] for r in warmups} | {v["snapshot_digest"] for row in pairs for v in row.values()}) == 1
        summary = {}
        for metric in ("snapshot_seconds", "cycle_seconds"):
            changes = [100 * (row[candidate][metric] / row["python"][metric] - 1) for row in pairs]
            summary[metric] = {"python_median": statistics.median(row["python"][metric] for row in pairs),
                               "candidate_median": statistics.median(row[candidate][metric] for row in pairs),
                               "paired_change_percent_median": statistics.median(changes),
                               "paired_change_percent_range": [min(changes), max(changes)]}
        result = {"schema_version": 1, "platform": platform.platform(), "python": platform.python_version(),
                  "measured_sources": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in [*sorted((ROOT / "src" / "agent_harness").glob("*.py")), Path(__file__).resolve()]},
                  "files": args.files, "bytes_per_file": len(data), "source_bytes": args.files * len(data),
                  "fixture_payload_sha256": hashlib.sha256(data).hexdigest(),
                  "contract_sha256": hashlib.sha256(canonical(contract)).hexdigest(),
                  "native": info, "candidate_backend": candidate, "warmups": warmups, "pairs": pairs, "summary": summary,
                  "limitations": ["Warm OS cache; imports excluded; repeated reads and full native process startup included",
                                  "Whole cycle is init/freeze/three real commands/gate, not a Codex/model session",
                                  "No model-token, billing, or model-quality claims; source identity identical within each pair"]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write("\n")
        print(json.dumps({"output": str(args.output), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
