"""Observable, contained Codex exec/resume; model completion is not acceptance."""
from __future__ import annotations

import hashlib
import copy
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from types import MappingProxyType

from .core import HarnessError, now, process_identity, digest
from .file_io import read_regular_file
from .runner import _contained_capture, _start_contained, _terminate, redact
from .supervisor_store import _mkdir, _ordinary, _parents

MAX_EVENT_BYTES = 1024 * 1024
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_PROMPT_BYTES = 24000
EVENT_TYPES = {"thread.started", "turn.started", "turn.completed", "turn.failed", "item.started", "item.updated", "item.completed", "error"}
SECRET_KEY = re.compile(r"^(?:authorization|password|passwd|secret|client_secret|api_key|apikey|access_token|refresh_token|id_token|credential|credentials|auth_token)$", re.I)
SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _private_group_stopped(pgid):
    """No live members, not merely a dead group leader (POSIX only).

    Linux zombies retain a process group but cannot write. Only use /proc when
    it describes our PID namespace; otherwise keep the outcome uncertain.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        if int(Path("/proc/self/stat").read_text().split(" ", 1)[0]) != os.getpid():
            return False
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    tail = (Path(entry.path) / "stat").read_text().rsplit(")", 1)[1].split()
                except FileNotFoundError:
                    continue
                if int(tail[2]) == pgid and tail[0] not in {"Z", "X"}:
                    return False
        return True
    except (OSError, ValueError, IndexError):
        return False


def _stop_owned_tree(process, job, identity):
    """Bounded cleanup on *every* exit, including after the leader has exited."""
    if os.name == "nt":
        _terminate(process, job)
        return
    current = process_identity(process.pid)
    if current and identity and current != identity:
        raise HarnessError("Owned process identity changed; refusing to signal a reused group")
    # SIGKILL is mandatory even if wait()/poll() already reaped the leader:
    # a descendant can ignore SIGTERM and keep writing with closed log pipes.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)
    deadline = time.monotonic() + 3
    while not _private_group_stopped(process.pid):
        if time.monotonic() >= deadline:
            raise HarnessError("Owned process group termination remains uncertain; retain model slot")
        time.sleep(0.02)


def _binary_hash(path):
    """Bound executable identity without Windows filename-derived execute bits."""
    path = Path(path)
    _parents(path)
    before = _ordinary(path)
    if before.st_size > 512 * 1024 * 1024:
        raise HarnessError("Executable exceeds identity hashing byte budget")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)
    h = hashlib.sha256()
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or stamp(opened) != stamp(before):
            raise HarnessError("Executable changed during identity inspection")
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
        if stamp(os.fstat(source.fileno())) != stamp(before) or stamp(_ordinary(path)) != stamp(before):
            raise HarnessError("Executable changed during identity hashing")
    return h.hexdigest()


def sanitized(value):
    """Bounded projection; untrusted outputs never become policy or permissions."""
    if isinstance(value, dict):
        return {key: "<redacted>" if SECRET_KEY.fullmatch(str(key)) else sanitized(child) for key, child in value.items()}
    if isinstance(value, list):
        return [sanitized(child) for child in value]
    if isinstance(value, str):
        value = redact(value)
        value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "<redacted>", value)
        value = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1<redacted>@", value)
        value = re.sub(r"(?i)((?:api[_-]?key|password|passwd|access[_-]?token|client[_-]?secret)\s*[:=]\s*)[^\s,;]+", r"\1<redacted>", value)
        return value
    return value


def _duplicate_free(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("Duplicate JSON event key")
        value[key] = child
    return value


def _read_stream(source, name, target, inbox):
    """Drain concurrently to prevent stdout/stderr pipe deadlocks."""
    total = 0
    try:
        while True:
            line = source.readline(MAX_EVENT_BYTES + 1)
            if not line:
                break
            total += len(line)
            if len(line) > MAX_EVENT_BYTES or total > MAX_LOG_BYTES:
                inbox.put(("capture_error", name + " exceeds log/event byte budget"))
                return
            if name == "stdout":
                if not line.endswith(b"\n"):
                    inbox.put(("capture_error", "Partial JSONL event at EOF"))
                    return
                try:
                    event = json.loads(line.decode("utf-8"), object_pairs_hook=_duplicate_free)
                    if not isinstance(event, dict) or event.get("type") not in EVENT_TYPES:
                        raise ValueError("Unknown or missing event type")
                    clean = sanitized(event)
                    serialized = json.dumps(clean, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
                except (ValueError, UnicodeError, RecursionError) as error:
                    inbox.put(("capture_error", "Invalid Codex JSONL: " + type(error).__name__))
                    return
                target.write(serialized)
                target.flush()
                os.fsync(target.fileno())
                inbox.put(("event", clean))
            else:
                clean = sanitized(line.decode("utf-8", errors="replace")).encode("utf-8")
                target.write(clean)
                target.flush()
                inbox.put(("stderr", len(clean)))
    except BaseException as error:
        inbox.put(("capture_error", "Stream capture failed: " + type(error).__name__))
    finally:
        source.close()
        inbox.put(("eof", name))


def _explicit_environment(environment):
    if environment is None:
        return None
    if not isinstance(environment, Mapping) or len(environment) > 512:
        raise HarnessError("Explicit Codex environment must be a bounded string mapping")
    copied = dict(environment)
    if any(not isinstance(key, str) or not key or "=" in key or "\x00" in key
           or not isinstance(value, str) or "\x00" in value for key, value in copied.items()):
        raise HarnessError("Explicit Codex environment contains an invalid entry")
    if len({key.casefold() for key in copied}) != len(copied):
        raise HarnessError("Explicit Codex environment contains case-ambiguous keys")
    if sum(len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in copied.items()) > 1024 * 1024:
        raise HarnessError("Explicit Codex environment exceeds its byte bound")
    if not copied.get("CODEX_HOME") or not Path(copied["CODEX_HOME"]).is_absolute():
        raise HarnessError("Explicit Codex environment requires an absolute CODEX_HOME")
    return MappingProxyType(copied)


def _codex_home(environment):
    if environment is None:
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    if not environment.get("CODEX_HOME") or not Path(environment["CODEX_HOME"]).is_absolute():
        raise HarnessError("Explicit Codex environment requires an absolute CODEX_HOME")
    return Path(environment["CODEX_HOME"])


def configured_settings(*, environment=None):
    """Safe file projection, explicitly distinct from effective session config."""
    import tomllib
    path = _codex_home(environment) / "config.toml"
    if not path.exists():
        return {}
    value = tomllib.loads(read_regular_file(path, max_bytes=2 * 1024 * 1024).decode("utf-8-sig"))
    keys = ("model", "model_provider", "model_reasoning_effort", "service_tier", "sandbox_mode", "approval_policy")
    return {key: value[key] for key in keys if key in value and isinstance(value[key], (str, bool, int))}


class CodexAdapter:
    def __init__(self, executable=("codex",), *, environment=None):
        self.environment = _explicit_environment(environment)
        if isinstance(executable, (str, Path)):
            executable = (str(executable),)
        if not isinstance(executable, (tuple, list)) or not executable or any(not isinstance(x, str) or not x or "\x00" in x for x in executable):
            raise HarnessError("Codex executable must be a nonempty argv sequence")
        resolved = shutil.which(executable[0], **({"path": self.environment.get("PATH", os.defpath)} if self.environment is not None else {}))
        if resolved is None:
            raise HarnessError("Codex executable unavailable")
        self.executable = (str(Path(resolved).resolve()), *executable[1:])

    def with_environment(self, environment):
        """Copy this transport with a separate environment and the same binary.

        This inherited implementation supports the exact concrete transport.
        Subclasses must implement their own clone contract; that implementation
        may itself be inherited normally. No delegation introspection is proof.
        The original adapter, global environment and resolved binary are unchanged.
        """
        if type(self) is not CodexAdapter:
            raise HarnessError("ENVIRONMENT_CLONE_CONTRACT_REQUIRED: subclass must implement its own clone")
        if any(name in self.__dict__ for name in ("run", "capabilities", "argv")):
            raise HarnessError("ENVIRONMENT_CLONE_CONTRACT_REQUIRED: instance transport override")
        clone = copy.copy(self)
        clone.environment = _explicit_environment(environment)
        return clone

    def capabilities(self, *, deadline=None):
        """Probe flags only. Tool-inventory suppression needs a separate receipt."""
        values = {}
        for name, args in (("version", ["--version"]), ("exec", ["exec", "--help"]), ("resume", ["exec", "resume", "--help"])):
            try:
                timeout = 15
                if deadline is not None:
                    from datetime import datetime, timezone
                    end = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                    if end.tzinfo is None:
                        raise HarnessError("Capability deadline requires an explicit timezone")
                    remaining = (end - datetime.now(timezone.utc)).total_seconds()
                    if remaining <= 0:
                        return {"status": "unavailable", "reason": "deadline_exhausted", "native_children_disabled": False}
                    timeout = min(timeout, remaining)
                result = _contained_capture([*self.executable, *args], timeout=timeout,
                    **({"env": self.environment} if self.environment is not None else {}))
                output = result.stdout.decode("utf-8", errors="replace")
                if result.returncode or len(output.encode("utf-8")) > 262144:
                    raise HarnessError("Capability probe failed")
                values[name] = output
            except (OSError, ValueError, subprocess.TimeoutExpired, HarnessError) as error:
                return {"status": "unavailable", "reason": type(error).__name__, "native_children_disabled": False}
        return {"status": "observed", "version": values["version"].strip(), "binary_sha256": _binary_hash(self.executable[0]), "json": "--json" in values["exec"], "resume": "SESSION_ID" in values["resume"], "output_schema": "--output-schema" in values["exec"], "resume_output_schema": "--output-schema" in values["resume"], "configured_settings": configured_settings(environment=self.environment), "native_children_disabled": False, "suppression_proof": "Separate observed tool inventory bound to this binary/version is required"}

    def argv(self, prompt, *, session_id=None, output_schema=None, native_children=False, read_only=False, write_authorized=False, allow_non_git=False, reasoning_effort=None):
        if not isinstance(prompt, str) or not prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES or "\x00" in prompt:
            raise HarnessError("Prompt must be nonempty and within the explicit byte budget; chunk it without discarding requirements")
        if any(type(value) is not bool for value in (native_children, read_only, write_authorized, allow_non_git)):
            raise HarnessError("Agent mode flags must be booleans")
        if session_id is not None and (not isinstance(session_id, str) or not SESSION.fullmatch(session_id)):
            raise HarnessError("An exact safe session ID is required; --last is forbidden")
        if reasoning_effort is not None and (not isinstance(reasoning_effort, str) or reasoning_effort not in {"medium", "high", "ultra"}):
            raise HarnessError("Unsupported explicit managed worker reasoning effort")
        argv = list(self.executable)
        if reasoning_effort is not None:
            # Per-invocation only: never change the coordinator's installed model
            # or reasoning defaults. The selected model must support this effort.
            argv += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        if not native_children:
            argv += ["-c", "agents.enabled=false", "-c", "features.multi_agent_v2=false"]
        if read_only:
            argv += ["-c", 'sandbox_mode="read-only"']
        elif write_authorized:
            import tomllib
            config_path = _codex_home(self.environment) / "config.toml"
            configured = None
            if config_path.exists():
                configured = tomllib.loads(read_regular_file(config_path, max_bytes=2 * 1024 * 1024).decode("utf-8-sig")).get("sandbox_mode")
            if configured not in {None, "workspace-write", "danger-full-access"}:
                raise HarnessError("Permission denied: explicit installed sandbox setting prohibits task writes; no automatic override")
            # Authorized contract edits get a bounded workspace. This changes no
            # persistent setting or approvals, and never overrides explicit read-only.
            argv += ["-c", 'sandbox_mode="workspace-write"']
        argv += ["exec"]
        if session_id is not None:
            argv += ["resume", session_id]
        argv += ["--json"]
        if allow_non_git:
            argv += ["--skip-git-repo-check"]
        if output_schema is not None:
            path = Path(output_schema)
            read_regular_file(path, max_bytes=2 * 1024 * 1024)
            argv += ["--output-schema", str(path.resolve())]
        argv += [prompt]
        if os.name == "nt" and len(subprocess.list2cmdline(argv)) > 30000:
            raise HarnessError("Windows argv budget exceeded; use a smaller referenced context packet")
        return argv

    def preflight(self, prompt, **options):
        """Validate this transport without starting a process; decorators delegate it."""
        return self.argv(prompt, **options)

    def run(self, prompt, *, cwd, session_id=None, timeout=600, cancel_file=None, on_process=None, on_event=None, output_schema=None, native_children=False, read_only=False, log_dir=None, deadline=None, write_authorized=False, allow_non_git=False, reasoning_effort=None):
        from .task_packets import NotDispatched
        if type(timeout) not in (int, float) or not 0 < timeout <= 86400:
            raise NotDispatched("Codex turn timeout must be finite and between 0 and 86400 seconds")
        cwd = Path(cwd)
        if not cwd.is_absolute() or not cwd.is_dir():
            raise NotDispatched("Codex cwd must be an existing absolute directory")
        try:
            argv = self.argv(prompt, session_id=session_id, output_schema=output_schema, native_children=native_children, read_only=read_only,
                             write_authorized=write_authorized, allow_non_git=allow_non_git, reasoning_effort=reasoning_effort)
        except (HarnessError, OSError, ValueError) as error:
            raise NotDispatched(str(error)) from error
        log_dir = Path(log_dir) if log_dir else Path(tempfile.mkdtemp(prefix="harness-codex-"))
        _mkdir(log_dir)
        _parents(log_dir)
        _ordinary(log_dir, directory=True)
        execution_id = uuid.uuid4().hex
        stdout_path, stderr_path = log_dir / (execution_id + ".jsonl"), log_dir / (execution_id + ".stderr.log")
        for path in (stdout_path, stderr_path):
            _ordinary(path, missing_ok=True)
        inbox = queue.Queue(maxsize=0)
        process = job = None
        readers = []
        started = time.monotonic()
        result = {"status": "unknown_outcome", "thread_id": session_id, "process": None, "usage": None, "events": 0, "observable_tool_calls": 0, "logs": {"stdout": str(stdout_path), "stderr": str(stderr_path)}, "error": None, "exit_code": None, "started_at": now(), "outcome_known": False, "process_tree_stopped": False}
        if self.environment is not None:
            result["environment_digest"] = digest(dict(self.environment))
        terminal = None
        seen_thread = False
        turn_started = False
        completed = False
        fatal = False
        eof = set()
        signal_cancel = threading.Event()
        handlers = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda signum, frame: signal_cancel.set())

        def consume(kind, payload):
            nonlocal terminal, seen_thread, turn_started, completed, fatal
            if kind == "capture_error":
                result["error"] = payload
                fatal = True
            elif kind == "eof":
                eof.add(payload)
            elif kind == "event":
                result["events"] += 1
                event_type = payload["type"]
                if completed or terminal == "failed":
                    result["error"], fatal = "Event after terminal Codex turn", True
                elif event_type == "thread.started":
                    thread_id = payload.get("thread_id")
                    if seen_thread or not isinstance(thread_id, str) or not SESSION.fullmatch(thread_id) or session_id is not None and session_id != thread_id:
                        result["error"], fatal = "Missing, changed, or duplicate exact thread ID", True
                    else:
                        result["thread_id"], seen_thread = thread_id, True
                elif event_type == "turn.started":
                    if turn_started or not seen_thread:
                        result["error"], fatal = "Out-of-order or duplicate turn.started", True
                    turn_started = True
                elif event_type == "turn.completed":
                    if not seen_thread or not turn_started:
                        result["error"], fatal = "Incomplete Codex event sequence", True
                    usage = payload.get("usage")
                    if usage is not None:
                        if not isinstance(usage, dict) or any(type(v) is not int or v < 0 for v in usage.values()):
                            result["error"], fatal = "Invalid token usage", True
                        else:
                            result["usage"] = usage
                    completed, terminal = True, "completed"
                elif event_type in {"error", "turn.failed"}:
                    result["error"] = sanitized(payload.get("message") or payload.get("error") or "Codex turn failed")
                    fatal, terminal = True, "failed"
                elif not turn_started and not (event_type == "item.completed" and isinstance(payload.get("item"), dict) and payload["item"].get("type") == "error"):
                    result["error"], fatal = "Item event before turn.started", True
                if event_type == "item.completed" and isinstance(payload.get("item"), dict) and payload["item"].get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
                    result["observable_tool_calls"] += 1
                if on_event is not None:
                    on_event(payload)

        try:
            with stdout_path.open("xb") as stdout_log, stderr_path.open("xb") as stderr_log:
                if cancel_file is not None and Path(cancel_file).exists():
                    result["status"], result["error"] = "cancelled", "Cancelled before process start"
                    result["outcome_known"] = True
                    result["dispatch_state"] = "not_dispatched_proven"
                    return result
                if deadline is not None:
                    from datetime import datetime, timezone
                    remaining = (datetime.fromisoformat(deadline.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
                    if remaining <= 0:
                        result.update(status="timed_out", error="Absolute task deadline expired before process start", outcome_known=True, dispatch_state="not_dispatched_proven")
                        return result
                    timeout = min(timeout, remaining)
                spawn_started = time.monotonic()
                process, job = _start_contained(argv, str(cwd),
                    **({"env": self.environment} if self.environment is not None else {}))
                result["spawn_seconds"] = time.monotonic() - spawn_started
                identity = process_identity(process.pid)
                result["process"] = {"identity": identity, "tree_control": "windows_suspended_job_kill_on_close" if os.name == "nt" else "private_process_group", "started_at": now(), "cwd": str(cwd), "argv_digest": hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode("utf-8")).hexdigest()}
                if not identity:
                    raise HarnessError("Cannot persist spawned process identity")
                if on_process is not None:
                    on_process(result["process"])
                for source, name, output in ((process.stdout, "stdout", stdout_log), (process.stderr, "stderr", stderr_log)):
                    reader = threading.Thread(target=_read_stream, args=(source, name, output, inbox), daemon=True)
                    reader.start()
                    readers.append(reader)
                while process.poll() is None or len(eof) < 2:
                    try:
                        consume(*inbox.get(timeout=0.05))
                    except queue.Empty:
                        pass
                    if fatal:
                        break
                    if signal_cancel.is_set() or cancel_file is not None and Path(cancel_file).exists():
                        result["status"], result["error"] = "cancelled", "Cancellation requested; source reconciliation required"
                        break
                    if time.monotonic() - started >= timeout:
                        result["status"], result["error"] = "timed_out", "Codex turn timed out; source reconciliation required"
                        break
                if process.poll() is None:
                    # First request graceful cancellation of only our process group.
                    try:
                        process.send_signal(signal.CTRL_BREAK_EVENT) if os.name == "nt" else os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                _stop_owned_tree(process, job, identity)
                result["process_tree_stopped"] = True
                for reader in readers:
                    reader.join(timeout=3)
                while not inbox.empty():
                    consume(*inbox.get_nowait())
                if any(reader.is_alive() for reader in readers):
                    result["error"], fatal = "Log reader did not terminate", True
                result["exit_code"] = process.returncode
                if result["status"] not in {"timed_out", "cancelled"}:
                    if not fatal and completed and seen_thread and process.returncode == 0:
                        result["status"], result["outcome_known"] = "completed", True
                    elif terminal == "failed":
                        result["status"], result["outcome_known"] = "failed", True
                    else:
                        result["status"] = "unknown_outcome"
                        result["error"] = result["error"] or "Missing completion/session or unexpected process exit"
                stdout_log.flush()
                stderr_log.flush()
                os.fsync(stdout_log.fileno())
                os.fsync(stderr_log.fileno())
        except BaseException as error:
            # Persistence failures and interruptions cannot turn possible writes
            # into a successful result. The containing supervisor reconciles.
            result["status"], result["error"] = "unknown_outcome", "Execution/persistence interrupted: " + type(error).__name__
            if isinstance(error, KeyboardInterrupt):
                result["status"] = "cancelled"
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            try:
                if process is not None and not result["process_tree_stopped"]:
                    _stop_owned_tree(process, job, (result.get("process") or {}).get("identity"))
                    result["process_tree_stopped"] = True
                elif process is None:
                    result["process_tree_stopped"] = True
            except (OSError, HarnessError, subprocess.TimeoutExpired) as error:
                result.update(status="unknown_outcome", outcome_known=False,
                              error="Owned process cleanup unconfirmed: " + type(error).__name__)
            finally:
                if job is not None:
                    job.close()
            for reader in readers:
                reader.join(timeout=3)
            if process is not None:
                result["exit_code"] = process.poll()
                for stream in (process.stdout, process.stderr):
                    if stream and not stream.closed:
                        stream.close()
            result["elapsed_seconds"] = time.monotonic() - started
            result["turn_started"] = turn_started
            result["turn_completed"] = completed and not fatal
            result["finished_at"] = now()
            result["log_sha256"] = {name: hashlib.sha256(read_regular_file(path, max_bytes=MAX_LOG_BYTES * 2)).hexdigest() for name, path in (("stdout", stdout_path), ("stderr", stderr_path)) if path.exists()}
        return result
