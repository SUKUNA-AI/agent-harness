"""Opt-in, stdlib-only local overhead benchmark. No LLM, network or user checkout.

Measured regions overlap: snapshot_seconds includes git_seconds. Never add them.
Fixture construction and Git initialization are outside the timed region.
"""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_harness import __version__, core, runner


def sample(files, checks):
    with tempfile.TemporaryDirectory(prefix="harness-profile-") as temporary:
        base = Path(temporary).resolve()
        source = base / "source"
        source.mkdir()
        for index in range(files):
            (source / f"file-{index:05d}.py").write_text("value = 42\n" * 100, encoding="utf-8")
        subprocess.run(["git", "-c", "init.templateDir=", "-c", "core.hooksPath=" + str(base / "no-hooks"), "init", "-q", str(source)], check=True, capture_output=True)
        check = {"kind": "command", "required": True, "argv": [sys.executable, "-I", "-B", "-c", "print('case passed')"], "timeout": 10, "expected_exit": 0, "assertions": [{"stream": "stdout", "regex": "case passed"}], "tool_versions": {"python": [sys.executable, "--version"]}}
        contract = {"schema_version": 1, "run_id": "profile", "task_id": "profile", "attempt_id": "a1", "workflow": "change", "process_level": "standard", "goal": "Measure isolated local overhead", "cwd": str(source), "permissions": ["read", "test"], "checks": [{**deepcopy(check), "id": f"check-{index}"} for index in range(checks)]}
        counts = {"snapshots": 0, "git_queries": 0, "version_probes": 0, "source_hash_bytes": 0, "tool_hash_calls": 0, "tool_hash_bytes": 0}
        elapsed = {"snapshot_seconds": 0.0, "git_seconds": 0.0, "version_seconds": 0.0}
        real_snapshot, real_git = core.snapshot, core._git
        real_probe, real_hash = runner._contained_capture, core.file_hash

        def timed_snapshot(value):
            counts["snapshots"] += 1
            start = time.perf_counter()
            try:
                return real_snapshot(value)
            finally:
                elapsed["snapshot_seconds"] += time.perf_counter() - start

        def timed_git(*args):
            counts["git_queries"] += 1
            start = time.perf_counter()
            try:
                return real_git(*args)
            finally:
                elapsed["git_seconds"] += time.perf_counter() - start

        def timed_probe(*args, **kwargs):
            counts["version_probes"] += 1
            start = time.perf_counter()
            try:
                return real_probe(*args, **kwargs)
            finally:
                elapsed["version_seconds"] += time.perf_counter() - start

        def counted_hash(path):
            resolved = Path(path).resolve()
            if source in resolved.parents:
                counts["source_hash_bytes"] += Path(path).stat().st_size
            elif base not in resolved.parents:
                # The isolated fixture has no external inputs besides tools.
                counts["tool_hash_calls"] += 1
                counts["tool_hash_bytes"] += Path(path).stat().st_size
            return real_hash(path)

        with patch.object(core, "snapshot", timed_snapshot), patch.object(runner, "snapshot", timed_snapshot), patch.object(core, "_git", timed_git), patch.object(runner, "_contained_capture", timed_probe), patch.object(core, "file_hash", counted_hash), patch.object(runner, "file_hash", counted_hash):
            wall, cpu = time.perf_counter(), time.process_time()
            run = core.Run.create(base / "run", contract)
            run.freeze()
            for item in contract["checks"]:
                result = runner.run_check(run, item["id"])
                if result["status"] != "passed":
                    raise RuntimeError("Fixture failed: " + json.dumps(result))
            verdict = runner.gate(run)
            wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
        if verdict["verdict"] != "accepted":
            raise RuntimeError("Fixture gate rejected: " + json.dumps(verdict))
        return {"wall_seconds": wall, "parent_cpu_seconds": cpu, **counts, **elapsed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=100)
    parser.add_argument("--checks", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--output")
    args = parser.parse_args()
    if not (1 <= args.files <= 10000 and 1 <= args.checks <= 20 and 1 <= args.repeats <= 50 and 0 <= args.warmup <= 5):
        parser.error("Use 1..10000 files, 1..20 checks, 1..50 repeats, 0..5 warmup")
    for _ in range(args.warmup):
        sample(args.files, args.checks)
    samples = [sample(args.files, args.checks) for _ in range(args.repeats)]
    result = {"schema_version": 1, "harness_version": __version__, "python": platform.python_version(), "platform": platform.system(), "machine": platform.machine(), "cpu_count": os.cpu_count(), "files": args.files, "bytes_per_file": 1100, "checks": args.checks, "warmup": args.warmup, "samples": samples, "median": {key: statistics.median(s[key] for s in samples) for key in samples[0]}, "limitations": ["No LLM/browser/network; this does not measure native Codex end-to-end latency", "snapshot_seconds includes git_seconds; measured regions overlap", "Parent CPU excludes child processes", "Tiny isolated Git fixture; source creation is outside timed region"]}
    data = json.dumps(result, indent=2) + "\n"
    if args.output:
        with Path(args.output).open("x", encoding="utf-8") as output:
            output.write(data)
    print(data)


if __name__ == "__main__":
    main()
