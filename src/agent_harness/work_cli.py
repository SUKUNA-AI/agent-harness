"""Local foreground work commands. Status/watch never invoke a model."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid
import signal
import threading

from .core import HarnessError


def default_state_dir():
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    pointer = codex_home / "harness-machine.json"
    if pointer.exists():
        from .file_io import read_regular_file
        value = json.loads(read_regular_file(pointer, max_bytes=65536))
        state = value.get("installation_state")
        if state and Path(state).is_absolute():
            return Path(state)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "agent-harness"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "agent-harness"


def _adapter(value):
    if value is None:
        return None
    from .codex_execution import CodexAdapter
    path = Path(value).absolute()
    if not path.is_file() or path.is_symlink():
        raise HarnessError("Explicit Codex executable must be an ordinary file")
    return CodexAdapter((sys.executable, str(path)) if path.suffix == ".py" else (str(path),))


def _print(value, style):
    from .output import write_json, write_utf8
    if style == "yaml":
        from .strict_yaml import dumps
        write_utf8(dumps(value))
    elif style == "json":
        write_json(value)
    else:
        if "required_total" not in value:
            write_json(value)
            return
        print(f"RUN {value['run_id']} | {value['status']} | attempt {value['attempt']}/{value['max_attempts']}")
        print(f"Acceptance: {value['required_passed']}/{value['required_total']} required criteria passed")
        print("Reason: " + value["reason"])
        print("Candidate: " + value["cwd"])
        print("Evidence current: " + str(value["evidence_current"]))


def _foreground(supervisor, callback):
    if threading.current_thread() is not threading.main_thread():
        return callback()
    previous = {}
    def request_cancel(signum, frame):
        from .strict_yaml import dump
        from .core import now
        dump(supervisor.path / "cancel.yaml", {"schema_version": 1, "reason": "foreground signal " + str(signum), "timestamp": now()},
             {"type": "object", "properties": {"schema_version": {"const": 1}, "reason": {"type": "string"},
                 "timestamp": {"type": "string", "format": "utc"}}, "required": ["schema_version", "reason", "timestamp"]})
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, request_cancel)
        return callback()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def dispatch(argv):
    if argv and argv[0] in {"ledger", "memory", "format-bench", "benchmark", "migrate"}:
        if argv[0] == "migrate":
            from .state_migration import dispatch as migration
            return migration(argv[1:])
        if argv[0] == "benchmark":
            from .evaluation import dispatch as evaluation
            return evaluation(argv[1:])
        if argv[0] == "ledger":
            from .execution_ledger import ledger_dispatch
            return ledger_dispatch(argv[1:])
        if argv[0] == "memory":
            from .memory import memory_dispatch
            return memory_dispatch(argv[1:])
        from .format_bench import dispatch as benchmark
        return benchmark(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "resume", "status", "watch", "stop", "report", "mission-update"):
        p = commands.add_parser(action)
        p.add_argument("--run-dir", required=action != "start")
        p.add_argument("--state-dir", help="Global local state directory; shared model slot registry")
        p.add_argument("--format", choices=("text", "yaml", "json"), default="text")
        if action in {"start", "resume"}:
            if action == "resume":
                p.add_argument("--plan", help="Validated plan for a newly authorized mission generation")
            p.add_argument("--codex", help="Explicit executable or test adapter script; no shell interpolation")
        if action == "resume":
            p.add_argument("--repair-journal", action="store_true", help="Preserve and repair only an incomplete final journal append, then reconcile")
            p.add_argument("--new-attempt", metavar="REASON", help="Explicit retry of a terminal non-accepted task; original limits remain")
            p.add_argument("--native-disabled-proof", help="Fresh version-bound proof if the persisted managed suppression observation expired")
        if action == "start":
            p.add_argument("--contract", required=True)
            p.add_argument("--cwd", default=os.getcwd())
            p.add_argument("--plan", help="Explicit versioned task plan; semantic validation is mandatory")
            p.add_argument("--native-disabled-proof", help="Version/binary-bound native tools inventory proof for managed concurrency")
        if action == "mission-update":
            p.add_argument("--contract", required=True)
            p.add_argument("--reason", required=True)
        if action == "status":
            p.add_argument("--summary", action="store_true")
        if action in {"status", "report"}:
            p.add_argument("--verify-evidence", action="store_true")
        if action == "watch":
            p.add_argument("--interval", type=float, default=2)
            p.add_argument("--timeout", type=float, default=3600)
        if action == "stop":
            p.add_argument("--reason", default="user requested stop")
    commands.add_parser("ledger", help="Version-bound execution facts; ledger --help")
    commands.add_parser("memory", help="Opt-in repository memory; memory --help")
    commands.add_parser("format-bench", help="Measured JSON/Markdown/TOON selection; format-bench --help")
    commands.add_parser("benchmark", help="Offline frozen portfolio validation and paired accounting; benchmark --help")
    commands.add_parser("migrate", help="Explicit versioned state migration; migrate --help")
    args = parser.parse_args(argv)
    try:
        from .supervisor import Supervisor, TERMINAL
        state_dir = Path(args.state_dir).absolute() if args.state_dir else default_state_dir()
        if args.action == "start":
            run_dir = Path(args.run_dir).absolute() if args.run_dir else state_dir / "runs" / ("work-" + uuid.uuid4().hex)
            supervisor = Supervisor.create(args.contract, run_dir=run_dir, cwd=args.cwd,
                                           state_dir=state_dir, adapter=_adapter(args.codex))
            supervisor.plan_input = args.plan
            supervisor.native_disabled_proof = args.native_disabled_proof
            result = _foreground(supervisor, supervisor.start)
        else:
            run_dir = Path(args.run_dir).absolute()
            if not (run_dir / "state.yaml").is_file():
                raise HarnessError("No supervisor state at requested run directory")
            supervisor = Supervisor(run_dir, state_dir=state_dir if args.state_dir else None,
                                    adapter=_adapter(args.codex) if hasattr(args, "codex") else None)
            if args.action == "resume":
                supervisor.plan_input = args.plan
                result = _foreground(supervisor, lambda: supervisor.resume(repair_journal=args.repair_journal, new_attempt=args.new_attempt,
                                           native_disabled_proof=args.native_disabled_proof))
            elif args.action == "mission-update":
                result = supervisor.update_mission(args.contract, reason=args.reason)
            elif args.action == "stop":
                result = supervisor.stop(args.reason)
            elif args.action == "watch":
                if not 0.1 <= args.interval <= 60 or not 0 < args.timeout <= 604800:
                    raise HarnessError("watch interval/timeout outside bounded limits")
                start, generation = time.monotonic(), None
                while time.monotonic() - start < args.timeout:
                    supervisor.load()
                    result = supervisor.status()
                    if result["generation"] != generation:
                        _print(result, args.format)
                        generation = result["generation"]
                    if result["status"] in TERMINAL or result["status"].startswith("BLOCKED_"):
                        return int(result["status"] != "ACCEPTED")
                    time.sleep(args.interval)
                return 1
            else:
                supervisor.load()
                result = supervisor.status(verify=args.verify_evidence or args.action == "report")
        _print(result, args.format)
        if args.action in {"start", "resume"}:
            return int(result["status"] != "ACCEPTED" or result.get("evidence_current") is False)
        return 0
    except KeyboardInterrupt:
        if "supervisor" in locals() and args.action in {"start", "resume"}:
            supervisor.stop("foreground interrupt")
        print("Interrupted; inspect exact process and use work resume for reconciliation", file=sys.stderr)
        return 130
    except (HarnessError, OSError, ValueError, TypeError) as error:
        from .runner import redact
        if "supervisor" in locals() and supervisor.state and supervisor.state["status"] not in {"ACCEPTED", "FAILED_TERMINAL"}:
            # Do not try to acquire a second writer after a lease/busy rejection.
            pass
        print(json.dumps({"error": redact(str(error)), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
