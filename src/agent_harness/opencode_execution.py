"""Optional OpenCode V2 leaf transport using the existing contained lifecycle.

Help output is evidence of syntax, never permission or exact-resume proof. The
real client remains blocked until its effective permissions, tools and session
storage have been qualified. Deterministic fixture subclasses exercise the
transport without granting that qualification to an installed client.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import MappingProxyType

from .codex_execution import (CodexAdapter, MAX_EVENT_BYTES, MAX_LOG_BYTES,
    MAX_PROMPT_BYTES, SESSION, _binary_hash, _duplicate_free, _read_stream, sanitized)
from .core import HarnessError, digest
from .file_io import read_regular_file
from .runner import _contained_capture
from .task_packets import NotDispatched

OBSERVED_VERSIONS = frozenset({"2.2.5"})
JSON_TYPES = frozenset({"step_start", "text", "reasoning", "tool_use", "step_finish", "error"})
FORBIDDEN_LEAF_TOOLS = frozenset({"subagent", "task", "spawn_agent", "spawn_subagent", "execute"})


def _environment(value):
    if value is None:
        return None
    if not isinstance(value, Mapping) or len(value) > 512:
        raise HarnessError("OpenCode environment must be a bounded string mapping")
    copied = dict(value)
    if any(not isinstance(k, str) or not k or "=" in k or "\0" in k
           or not isinstance(v, str) or "\0" in v for k, v in copied.items()):
        raise HarnessError("Invalid OpenCode environment entry")
    if len({k.casefold() for k in copied}) != len(copied):
        raise HarnessError("Case-ambiguous OpenCode environment")
    if sum(len(k.encode()) + len(v.encode()) for k, v in copied.items()) > 2 * 1024 * 1024:
        raise HarnessError("OpenCode environment byte budget exceeded")
    return MappingProxyType(copied)


class OpenCodeEventNormalizer:
    """V2 run JSONL to the supervisor's existing observable event vocabulary.

    A step is not an execution: intermediate tool-call steps cannot complete a
    turn. Completion is emitted only at a clean EOF after a stop finish; the
    existing runner additionally requires exit code zero and a stopped tree.
    """
    def __init__(self):
        self.session_id = None
        self.step = None
        self.closed_steps = set()
        self.final_reason = None
        self.failed = False

    def feed(self, value):
        if not isinstance(value, dict) or value.get("type") not in JSON_TYPES:
            raise ValueError("Unknown OpenCode JSONL event type")
        session_id = value.get("sessionID")
        if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
            raise ValueError("Missing or unsafe OpenCode session ID")
        if self.session_id is not None and self.session_id != session_id:
            raise ValueError("Conflicting OpenCode session IDs or uncontrolled subagent")
        part = value.get("part")
        if part is not None and not isinstance(part, dict):
            raise ValueError("Invalid OpenCode part")
        if part and part.get("sessionID", session_id) != session_id:
            raise ValueError("OpenCode nested session identity changed")
        event_type = value["type"]
        if self.failed:
            raise ValueError("Event after failed OpenCode execution")
        events = []
        if self.session_id is None:
            self.session_id = session_id
            events.extend([{"type": "thread.started", "thread_id": session_id, "backend": "opencode"},
                           {"type": "turn.started", "backend": "opencode"}])
        if event_type == "error":
            self.failed = True
            events.append({"type": "turn.failed", "error": value.get("error") or "OpenCode failed",
                           "backend_event": value})
            return events
        if not part or not isinstance(part.get("messageID"), str) or not part["messageID"]:
            raise ValueError("Missing OpenCode step identity")
        message_id = part["messageID"]
        if event_type == "step_start":
            if self.step is not None or message_id in self.closed_steps or self.final_reason == "stop":
                raise ValueError("Duplicate or out-of-order OpenCode step")
            self.step, self.final_reason = message_id, None
        elif self.step != message_id:
            raise ValueError("OpenCode event outside its active step")
        if event_type == "step_finish":
            self.step = None
            self.closed_steps.add(message_id)
            self.final_reason = part.get("reason")
            if self.final_reason not in {"stop", "tool-calls", "length", "content-filter", "error", "other", "unknown"}:
                raise ValueError("Unknown OpenCode finish reason")
        if event_type == "tool_use":
            tool = part.get("tool")
            if not isinstance(tool, str) or not tool or tool in FORBIDDEN_LEAF_TOOLS:
                raise ValueError("Forbidden or unknown managed leaf tool")
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") not in {"completed", "error"}:
                raise ValueError("Incomplete OpenCode tool result")
            item_type = "command_execution" if tool == "shell" else "file_change" if tool in {"edit", "write", "patch"} else "mcp_tool_call"
            events.append({"type": "item.completed", "item": {"type": item_type, "tool": tool},
                           "backend_event": value})
        else:
            item_type = "agent_message" if event_type == "text" else "reasoning" if event_type == "reasoning" else "backend_step"
            item = {"type": item_type}
            if isinstance(part.get("text"), str):
                item["text"] = part["text"]
            events.append({"type": "item.completed", "item": item, "backend_event": value})
        return events

    def finish(self):
        if self.failed:
            return []
        if not self.session_id or self.step is not None or self.final_reason != "stop":
            raise ValueError("OpenCode EOF does not prove a complete execution")
        return [{"type": "turn.completed", "backend": "opencode"}]


def _read_opencode_stream(source, name, target, inbox):
    if name != "stdout":
        return _read_stream(source, name, target, inbox)
    total = 0
    published = 0
    normalizer = OpenCodeEventNormalizer()

    def publish(event):
        nonlocal published
        clean = sanitized(event)
        raw = json.dumps(clean, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        published += len(raw)
        if published > MAX_LOG_BYTES:
            raise ValueError("Normalized OpenCode log byte budget exceeded")
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
        inbox.put(("event", clean))

    try:
        while True:
            line = source.readline(MAX_EVENT_BYTES + 1)
            if not line:
                break
            total += len(line)
            if total > MAX_LOG_BYTES or len(line) > MAX_EVENT_BYTES:
                raise ValueError("OpenCode event/log byte budget exceeded")
            if not line.endswith(b"\n"):
                raise ValueError("Partial OpenCode JSONL at EOF")
            value = json.loads(line.decode("utf-8"), object_pairs_hook=_duplicate_free,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
            for event in normalizer.feed(value):
                publish(event)
        for event in normalizer.finish():
            publish(event)
    except (ValueError, UnicodeError, RecursionError, OSError) as error:
        inbox.put(("capture_error", "Invalid OpenCode JSONL: " + str(error)))
    finally:
        source.close()
        inbox.put(("eof", name))


class OpenCodeAdapter(CodexAdapter):
    """Optional V2 backend; no global config/auth writes or shared service."""
    backend = "opencode"
    _stream_reader = staticmethod(_read_opencode_stream)

    def __init__(self, executable=("opencode",), *, environment=None):
        self.environment = _environment(environment)
        if isinstance(executable, (str, Path)):
            executable = (str(executable),)
        if (not isinstance(executable, (tuple, list)) or not executable
                or any(not isinstance(x, str) or not x or "\0" in x for x in executable)):
            raise HarnessError("OpenCode executable must be a nonempty argv")
        resolved = shutil.which(executable[0], path=self.environment.get("PATH", os.defpath) if self.environment is not None else None)
        if resolved is None:
            raise HarnessError("OpenCode executable unavailable; Codex remains available")
        self.executable = (str(Path(resolved).resolve()), *executable[1:])
        self.tool_config = None
        self.tool_config_digest = None
        self.tool_config_path = None
        self.tool_root = None

    def with_environment(self, environment):
        if type(self) is not OpenCodeAdapter:
            raise HarnessError("ENVIRONMENT_CLONE_CONTRACT_REQUIRED: subclass must implement its own clone")
        if any(name in self.__dict__ for name in ("run", "capabilities", "argv")):
            raise HarnessError("ENVIRONMENT_CLONE_CONTRACT_REQUIRED: instance transport override")
        clone = copy.copy(self)
        clone.environment = _environment(environment)
        if self.tool_config is not None:
            if clone.environment is None:
                raise HarnessError("OpenCode profile requires its explicit invocation environment")
            raw = json.dumps(self.tool_config, sort_keys=True, separators=(",", ":"), allow_nan=False)
            env = dict(clone.environment)
            if env.get("OPENCODE_CONFIG_CONTENT") not in (None, raw):
                raise HarnessError("OpenCode environment clone conflicts with its tool profile")
            env["OPENCODE_CONFIG_CONTENT"] = raw
            clone.environment = _environment(env)
        return clone

    def with_tool_config(self, config, *, config_path=None, root=None):
        if not isinstance(config, dict) or set(config) - {"$schema", "mcp", "agents", "permissions", "policies", "default_agent"}:
            raise HarnessError("Tool config may contain only V2 tool/agent policy fields")
        raw = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > 1024 * 1024:
            raise HarnessError("OpenCode tool config byte budget exceeded")
        if config_path is not None:
            actual = json.loads(read_regular_file(config_path, max_bytes=1024 * 1024), object_pairs_hook=_duplicate_free)
            if actual != config:
                raise HarnessError("OpenCode owned config artifact differs from the rendered profile")
        clone = copy.copy(self)
        clone.tool_config = json.loads(raw)
        clone.tool_config_digest = digest(clone.tool_config)
        clone.tool_config_path = str(Path(config_path).resolve()) if config_path else None
        clone.tool_root = str(Path(root).resolve()) if root is not None else None
        env = dict(self.environment if self.environment is not None else os.environ)
        if env.get("OPENCODE_CONFIG_CONTENT") and env["OPENCODE_CONFIG_CONTENT"] != raw:
            raise HarnessError("Existing OPENCODE_CONFIG_CONTENT conflicts; no implicit user override")
        env["OPENCODE_CONFIG_CONTENT"] = raw
        clone.environment = _environment(env)
        return clone

    def _qualification(self):
        # No operator flag, CLI self-report or fixture receipt can turn this
        # into a live qualification. Add a tested core verifier before enabling.
        return {"managed_leaf_qualified": False, "exact_resume_qualified": False,
                "reviewer_isolation_qualified": False}

    def capabilities(self, *, deadline=None):
        values = {}
        try:
            for name, options in (("version", ["--version"]), ("run", ["run", "--help"]),
                                  ("session", ["session", "--help"]), ("export", ["session", "export", "--help"])):
                timeout = 15
                if deadline is not None:
                    remaining = (datetime.fromisoformat(deadline.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
                    if remaining <= 0:
                        raise HarnessError("deadline_exhausted")
                    timeout = min(timeout, remaining)
                result = _contained_capture([*self.executable, *options], timeout=timeout,
                    **({"env": self.environment} if self.environment is not None else {}))
                if result.returncode or len(result.stdout) > 262144:
                    raise HarnessError("OpenCode capability probe failed")
                values[name] = result.stdout.decode("utf-8", errors="strict")
            match = re.fullmatch(r"opencode v?(\d+\.\d+\.\d+)\s*", values["version"])
            supported = match is not None and match[1] in OBSERVED_VERSIONS
            qualification = self._qualification()
            return {"status": "observed" if supported else "unsupported", "backend": self.backend,
                "version": values["version"].strip(), "binary_sha256": _binary_hash(self.executable[0]),
                "json": "--format" in values["run"] and "json" in values["run"],
                "resume": "--session" in values["run"], "standalone": "--standalone" in values["run"],
                "session_export": "export" in values["session"], "output_schema": False,
                "resume_output_schema": False, "configured_settings": {},
                "native_children_disabled": qualification["managed_leaf_qualified"] and supported,
                "qualification_status": "BLOCKED_NOT_QUALIFIED",
                "suppression_proof": "Live effective permissions, catalog, negative tests and exact resume are unqualified",
                "tool_config_digest": self.tool_config_digest, **qualification}
        except (OSError, ValueError, HarnessError, subprocess.TimeoutExpired) as error:
            return {"status": "unavailable", "backend": self.backend, "reason": type(error).__name__,
                    "native_children_disabled": False, **self._qualification()}

    def live_smoke(self, *, root=None, config=None, timeout=15):
        """No-model private-server probe in a disposable HOME/XDG/project.

        This deliberately does not load the requested project/profile: doing so
        could execute unqualified plugins or MCP commands. It diagnoses basic
        standalone readiness only and never upgrades execution qualification.
        """
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise HarnessError("OpenCode smoke timeout must be between 0 and 60")
        if root is not None and not Path(root).is_dir():
            raise HarnessError("OpenCode smoke source root must exist")
        base = {k: v for k, v in (self.environment if self.environment is not None else os.environ).items()
                if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "TEMP", "TMP", "TMPDIR"}}
        with tempfile.TemporaryDirectory(prefix="harness-opencode-smoke-") as folder:
            private = Path(folder)
            for key, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                   ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
                target = private / directory
                target.mkdir()
                base[key] = str(target)
            project = private / "project"
            project.mkdir()
            base.update(OPENCODE_DISABLE_PROJECT_CONFIG="1", OPENCODE_DISABLE_FREE_MODELS="1",
                        OPENCODE_CONFIG_CONTENT=json.dumps({"policies": [
                            {"action": "plugin.use", "resource": "*", "effect": "deny"},
                            {"action": "mcp.use", "resource": "*", "effect": "deny"},
                            {"action": "provider.use", "resource": "*", "effect": "deny"}]}))
            try:
                result = _contained_capture([*self.executable, "--standalone", "--print-logs", "debug", "config"],
                                            cwd=str(project), env=base, timeout=timeout)
                errors = result.stderr.decode("utf-8", errors="replace")
                reason = ("private_server_listen_denied" if "EPERM" in errors and "listen" in errors else
                          "private_server_not_ready" if result.returncode else "standalone_ready_only")
                return {"status": "BLOCKED_NOT_QUALIFIED", "backend": self.backend, "reason": reason,
                    "exit_code": result.returncode, "standalone_ready": result.returncode == 0,
                    "no_model_calls": True, "project_profile_qualified": False,
                    "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                    "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
                    "diagnostic": sanitized(errors[-2048:])}
            except (OSError, HarnessError, subprocess.TimeoutExpired) as error:
                return {"status": "BLOCKED_NOT_QUALIFIED", "backend": self.backend,
                        "reason": type(error).__name__, "standalone_ready": False,
                        "no_model_calls": True, "project_profile_qualified": False}

    def argv(self, prompt, *, session_id=None, output_schema=None, native_children=False, read_only=False,
             write_authorized=False, allow_non_git=False, reasoning_effort=None):
        if not isinstance(prompt, str) or not prompt or "\0" in prompt or len(prompt.encode()) > MAX_PROMPT_BYTES:
            raise HarnessError("OpenCode prompt must be nonempty and within the byte budget")
        if any(type(x) is not bool for x in (native_children, read_only, write_authorized, allow_non_git)):
            raise HarnessError("OpenCode mode flags must be booleans")
        if native_children:
            raise HarnessError("BLOCKED_NOT_QUALIFIED: OpenCode native subagents are unsupported")
        if session_id is not None and (not isinstance(session_id, str) or not SESSION.fullmatch(session_id)):
            raise HarnessError("An exact safe OpenCode session ID is required; --continue is forbidden")
        if reasoning_effort is not None:
            raise HarnessError("UNSUPPORTED_CAPABILITY: OpenCode has no model-independent reasoning-effort flag")
        if output_schema is not None:
            read_regular_file(output_schema, max_bytes=2 * 1024 * 1024)
            # OpenCode has no --output-schema. The supervisor still validates
            # final JSON against its original schema; a prompt is no proof.
        argv = [*self.executable, "run", "--standalone", "--format", "json"]
        if session_id is not None:
            argv += ["--session", session_id]
        if self.tool_config is not None and self.tool_config.get("default_agent"):
            argv += ["--agent", self.tool_config["default_agent"]]
        argv.append(prompt)
        if os.name == "nt" and len(subprocess.list2cmdline(argv)) > 30000:
            raise HarnessError("OpenCode Windows argv byte budget exceeded")
        return argv

    def preflight(self, prompt, **options):
        argv = self.argv(prompt, **options)
        if self.tool_config is not None:
            if digest(self.tool_config) != self.tool_config_digest:
                raise HarnessError("OpenCode rendered tool configuration drift")
            raw = json.dumps(self.tool_config, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if self.environment is None or self.environment.get("OPENCODE_CONFIG_CONTENT") != raw:
                raise HarnessError("OpenCode effective invocation fragment drift")
        proof = self._qualification()
        if not proof["managed_leaf_qualified"]:
            raise HarnessError("BLOCKED_NOT_QUALIFIED: effective OpenCode leaf permissions/tool catalog not qualified")
        if options.get("session_id") is not None and not proof["exact_resume_qualified"]:
            raise HarnessError("BLOCKED_NOT_QUALIFIED: OpenCode exact session resume not qualified")
        if options.get("read_only") and not proof["reviewer_isolation_qualified"]:
            raise HarnessError("BLOCKED_NOT_QUALIFIED: effective OpenCode reviewer isolation not qualified")
        return argv

    def run(self, prompt, *, cwd, **options):
        try:
            self.preflight(prompt, **{k: v for k, v in options.items() if k in {
                "session_id", "output_schema", "native_children", "read_only", "write_authorized", "allow_non_git", "reasoning_effort"}})
            if self.tool_root is not None and str(Path(cwd).resolve()) != self.tool_root:
                raise HarnessError("OpenCode worktree differs from the profile binding")
            if self.tool_config_path is not None:
                actual = json.loads(read_regular_file(self.tool_config_path, max_bytes=1024 * 1024), object_pairs_hook=_duplicate_free)
                if digest(actual) != self.tool_config_digest:
                    raise HarnessError("OpenCode tool profile artifact drift")
        except (HarnessError, OSError, ValueError) as error:
            raise NotDispatched(str(error)) from error
        result = super().run(prompt, cwd=cwd, **options)
        result["backend"] = self.backend
        result["tool_config_digest"] = self.tool_config_digest
        return result
