"""Small deterministic CLI. Native agent controls remain in the client."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__

INSTALL_COMMANDS = {"doctor", "install", "verify", "rollback", "uninstall"}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in INSTALL_COMMANDS:
        from .installation import dispatch
        return dispatch(argv[0], argv[1:])
    if argv and argv[0] == "plan":
        from .orchestration import dispatch
        return dispatch(argv[1:])
    if argv and argv[0] == "context":
        from .context import dispatch
        return dispatch(argv[1:])
    if argv and argv[0] == "code":
        from .lsp import dispatch
        return dispatch(argv[1:])
    if argv and argv[0] == "native":
        from .native_cli import dispatch
        return dispatch(argv[1:])
    parser = argparse.ArgumentParser(description="Native Codex harness: state and verification, never a second agent engine")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Read-only orchestration helpers; use plan --help")
    commands.add_parser("context", help="Bounded local source packets; use context --help")
    commands.add_parser("code", help="On-demand local symbols, references and rename; use code --help")
    commands.add_parser("native", help="Rust backend status and local build; use native --help")
    for name in ("init", "attempt"):
        p = commands.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--contract", required=True)
        if name == "attempt":
            p.add_argument("--reason", required=True)
    for name in ("freeze", "gate", "status", "resume", "capacity"):
        p = commands.add_parser(name)
        p.add_argument("--run-dir", required=True)
        if name == "status":
            p.add_argument("--verify-evidence", action="store_true", help="Read-only source, verdict and artifact validation")
            p.add_argument("--summary", action="store_true", help="Bounded context view; full state remains the default")
            p.add_argument("--max-output-bytes", type=int, default=8192)
    p = commands.add_parser("handoff", help="Read-only checkpoint decisions and current run summary")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--verify-evidence", action="store_true")
    p.add_argument("--max-output-bytes", type=int, default=8192)
    p = commands.add_parser("check")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--check", required=True)
    p.add_argument("--cancel-file")
    for name in ("evidence", "result", "ledger"):
        p = commands.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--record", required=True)
        if name == "evidence":
            p.add_argument("--check", required=True)
    p = commands.add_parser("checkpoint")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--next", required=True, dest="next_step")
    p = commands.add_parser("operation")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--status", choices=("planned", "unknown", "confirmed", "failed"), required=True)
    p.add_argument("--proof", default="")
    p = commands.add_parser("runs")
    p.add_argument("--store", required=True)
    args = parser.parse_args(argv)
    if args.command == "status" and not args.summary and args.max_output_bytes != 8192:
        parser.error("--max-output-bytes requires --summary")
    # Version/help and unrelated dispatchers do not need process containment,
    # XML report parsing or runner imports. Preserve the same command behavior.
    from .core import HarnessError, Run, read_json
    try:
        if args.command == "runs":
            result = []
            for path in sorted(Path(args.store).resolve().glob("*/state.json")):
                state = read_json(path)
                result.append({"run_id": state.get("run_id"), "task_id": state.get("task_id"), "goal": state.get("goal"), "status": state.get("status"), "updated_at": state.get("updated_at"), "run_dir": str(path.parent)})
        elif args.command == "init":
            result = Run.create(args.run_dir, read_json(args.contract)).status()
        else:
            run = Run(args.run_dir)
            if args.command == "attempt":
                result = run.new_attempt(read_json(args.contract), reason=args.reason)
            elif args.command == "freeze":
                result = run.freeze()
            elif args.command == "check":
                from .runner import run_check
                result = run_check(run, args.check, cancel_file=args.cancel_file)
            elif args.command == "evidence":
                from .runner import record_evidence
                result = record_evidence(run, args.check, read_json(args.record))
            elif args.command == "result":
                result = run.submit_result(read_json(args.record))
            elif args.command == "ledger":
                result = run.record_owner(read_json(args.record))
            elif args.command == "checkpoint":
                result = run.checkpoint(args.note, args.next_step)
            elif args.command == "resume":
                result = run.resume()
            elif args.command == "status":
                result = run.status(verify_evidence=args.verify_evidence)
            elif args.command == "handoff":
                from .presentation import handoff
                result = handoff(run, verify=args.verify_evidence, max_bytes=args.max_output_bytes)
            elif args.command == "capacity":
                result = run.capacity()
            elif args.command == "operation":
                result = run.record_operation(args.id, args.action, args.status, args.proof)
            else:
                from .runner import gate
                result = gate(run)
        # Do not dump complete candidate manifests or transcript-sized logs into chat.
        if args.command == "freeze":
            result = {"candidate_digest": result["digest"], "files": len(result["files"]), "kind": result["kind"]}
        elif args.command == "checkpoint":
            result = {"checkpoint_version": result["version"], "digest": result["digest"], "next_safe_step": result["next_safe_step"]}
        elif args.command == "check":
            result = {k: result.get(k) for k in ("check_id", "status", "exit_code", "elapsed_seconds", "validation", "candidate_digest", "digest", "artifacts", "assertion_errors", "error")}
        elif args.command == "status" and args.summary:
            from .presentation import status_summary
            result = status_summary(run, result, max_bytes=args.max_output_bytes)
        if args.command == "handoff" or (args.command == "status" and args.summary):
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "check":
            return 0 if result.get("status") == "passed" else 1
        if args.command == "gate":
            return 0 if result.get("verdict") == "accepted" else 1
        if args.command == "resume":
            return 0 if result.get("safe_to_start_new_owner") else 1
        if args.command in {"status", "handoff"} and args.verify_evidence:
            return 0 if result.get("evidence_current") is True else 1
        return 0
    except (HarnessError, ValueError, OSError, KeyError) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
