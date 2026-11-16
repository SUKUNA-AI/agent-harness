"""No-inference native Codex app-server MCP catalog audit.

This uses Codex's own MCP client, not a harness proxy. The ephemeral diagnostic
thread is deliberately distinct from a subsequent exec thread: neither catalog
success nor this connection is proof of reviewer isolation or model acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time

from .codex_execution import _binary_hash, _duplicate_free, _stop_owned_tree, sanitized
from .core import HarnessError, digest, now, process_identity
from .runner import _start_contained

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_SERVERS = 128
MAX_TOOLS = 4096
NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _toml(value, *, depth=0):
    if depth > 8:
        raise HarnessError("MCP native config nesting exceeds its bound")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_toml(x, depth=depth + 1) for x in value) + "]"
    if isinstance(value, dict) and all(isinstance(k, str) and NAME.fullmatch(k) for k in value):
        return "{" + ",".join(json.dumps(k) + "=" + _toml(v, depth=depth + 1) for k, v in sorted(value.items())) + "}"
    raise HarnessError("Unsupported MCP native config value")


def _config_args(config):
    if not isinstance(config, dict) or set(config) != {"mcp_servers"} or not isinstance(config["mcp_servers"], dict):
        raise HarnessError("Catalog audit accepts only a native mcp_servers fragment")
    if len(config["mcp_servers"]) > MAX_SERVERS:
        raise HarnessError("MCP server inventory exceeds bound")
    args = []
    for name, server in sorted(config["mcp_servers"].items()):
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(server, dict):
            raise HarnessError("Invalid native MCP server entry")
        args.extend(["-c", "mcp_servers." + name + "=" + _toml(server)])
    if sum(len(x.encode()) for x in args) > 128 * 1024:
        raise HarnessError("MCP config argv exceeds bound")
    if os.name == "nt" and len(subprocess.list2cmdline(args)) > 24000:
        raise HarnessError("MCP config exceeds Windows argv bound")
    return args


class _AppServer:
    """One contained stdio client, sequential RPCs, bounded strict JSONL."""
    def __init__(self, argv, root, environment, timeout):
        self.deadline = time.monotonic() + timeout
        self.inbox = queue.Queue()
        self.process, self.job = _start_contained(argv, str(root), env=environment, stdin=subprocess.PIPE)
        self.identity = process_identity(self.process.pid)
        self.sequence = 0
        self.responses = set()
        self.messages = 0
        self.events_digest = hashlib.sha256()
        self.errors_digest = hashlib.sha256()
        self.readers = []
        self.startup_notifications = []
        self.stopped = False
        for stream, name in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
            reader = threading.Thread(target=self._read, args=(stream, name), daemon=True)
            reader.start()
            self.readers.append(reader)
        if not self.identity:
            self.close()
            raise HarnessError("Catalog process identity unavailable")

    def _read(self, stream, name):
        total = 0
        try:
            while True:
                raw = stream.readline(MAX_MESSAGE_BYTES + 1)
                if not raw:
                    break
                total += len(raw)
                if len(raw) > MAX_MESSAGE_BYTES or total > MAX_STREAM_BYTES:
                    raise ValueError("Catalog stream bound exceeded")
                if name == "stderr":
                    self.errors_digest.update(sanitized(raw.decode("utf-8", errors="replace")).encode())
                    continue
                if not raw.endswith(b"\n"):
                    raise ValueError("Partial catalog JSONL event")
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_free,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                if not isinstance(value, dict):
                    raise ValueError("Catalog JSONL message is not an object")
                clean = sanitized(value)
                self.events_digest.update(json.dumps(clean, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n")
                self.messages += 1
                if self.messages > 8192:
                    raise ValueError("Catalog message count bound exceeded")
                self.inbox.put(("message", value))
        except (ValueError, UnicodeError, RecursionError, OSError) as error:
            self.inbox.put(("error", type(error).__name__))
        finally:
            stream.close()
            self.inbox.put(("eof", name))

    def _receive(self):
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError("MCP_CLIENT_TIMEOUT")
            try:
                kind, value = self.inbox.get(timeout=remaining)
            except queue.Empty as error:
                raise HarnessError("MCP_CLIENT_TIMEOUT") from error
            if kind == "eof" and value == "stderr":
                continue
            if kind != "message":
                raise HarnessError("MCP_CLIENT_PROTOCOL_" + kind.upper())
            return value

    def notify(self, method, params=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        ident = self.sequence
        message = {"id": ident, "method": method, "params": params}
        self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        self.process.stdin.flush()
        while True:
            value = self._receive()
            if "id" not in value:
                if not isinstance(value.get("method"), str):
                    raise HarnessError("MCP_CLIENT_INVALID_NOTIFICATION")
                if value["method"] == "mcpServer/startupStatus/updated":
                    self.startup_notifications.append(value.get("params", {}))
                continue
            if value["id"] != ident or value["id"] in self.responses or "method" in value:
                # Never answer permission/tool/approval requests in diagnostics.
                raise HarnessError("MCP_CLIENT_UNEXPECTED_RESPONSE_OR_REQUEST")
            self.responses.add(ident)
            if "error" in value:
                return {"rpc_error": sanitized(value["error"])}
            if "result" not in value or not isinstance(value["result"], dict):
                raise HarnessError("MCP_CLIENT_INVALID_RESPONSE")
            return value["result"]

    def close(self):
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            _stop_owned_tree(self.process, self.job, self.identity)
            self.stopped = True
        finally:
            self.job.close()
            for reader in self.readers:
                reader.join(timeout=3)
            if any(reader.is_alive() for reader in self.readers):
                self.stopped = False
                raise HarnessError("Catalog reader cleanup unconfirmed")


def audit_catalog(adapter, config, root, *, timeout=15):
    """Audit the isolated native client catalog, without starting an inference.

    Startup may preconnect network endpoints; no turn/start is ever sent. The
    disposable CODEX_HOME has no copied authentication or user configuration.
    Results never attest to another exec invocation's sandbox or tool set.
    """
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise HarnessError("Catalog timeout must be between 0 and 60 seconds")
    root = Path(root)
    if not root.is_absolute() or not root.is_dir() or str(root) != str(root.resolve()):
        raise HarnessError("Catalog root must be an existing canonical absolute directory")
    args = _config_args(config)
    result = {"status": "BLOCKED", "tools": [], "catalog_digest": None,
        "client_catalog_verified": False, "live_connection_verified": False,
        "qualified_for_exec": False, "reviewer_isolation_qualified": False,
        "probe_environment": "disposable_codex_home", "config_digest": digest(config),
        "root": str(root), "thread_id": None, "process_identity": None,
        "process_tree_stopped": False, "no_inference_requests": True,
        "observed_at": now(), "reason": "MCP_CLIENT_NOT_STARTED"}
    client = None
    private = None
    base_environment = adapter.environment if adapter.environment is not None else os.environ
    environment = {k: v for k, v in base_environment.items()
                   if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "TEMP", "TMP", "TMPDIR"}}
    try:
        result["binary_sha256"] = _binary_hash(adapter.executable[0])
        private = tempfile.TemporaryDirectory(prefix="harness-codex-catalog-")
        folder = private.name
        home = Path(folder) / "home"
        home.mkdir()
        environment.update(CODEX_HOME=str(home), HOME=str(home))
        argv = [*adapter.executable, *args, "app-server", "--stdio"]
        client = _AppServer(argv, root, environment, timeout)
        result["process_identity"] = client.identity
        initialized = client.request("initialize", {"clientInfo": {"name": "harness-tool-probe", "version": "1"},
            "capabilities": {"experimentalApi": True}})
        if "rpc_error" in initialized:
            raise HarnessError("MCP_CLIENT_INITIALIZE_FAILED")
        result["client_identity"] = sanitized(initialized.get("userAgent"))
        client.notify("initialized")
        started = client.request("thread/start", {"cwd": str(root), "ephemeral": True})
        thread = started.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or thread.get("ephemeral") is not True:
            raise HarnessError("MCP_CLIENT_THREAD_START_FAILED")
        if thread.get("cwd") != str(root):
            raise HarnessError("MCP_CLIENT_WORKTREE_MISMATCH")
        result["thread_id"] = thread["id"]
        servers, cursors = {}, set()
        cursor = None
        while True:
            params = {"limit": 100, "threadId": thread["id"], "detail": "toolsAndAuthOnly"}
            if cursor is not None:
                params["cursor"] = cursor
            page = client.request("mcpServerStatus/list", params)
            if not isinstance(page.get("data"), list):
                raise HarnessError("MCP_CLIENT_CATALOG_FAILED")
            for server in page["data"]:
                if not isinstance(server, dict) or not isinstance(server.get("name"), str) or server["name"] in servers:
                    raise HarnessError("MCP_CLIENT_DUPLICATE_OR_INVALID_SERVER")
                servers[server["name"]] = server
            if len(servers) > MAX_SERVERS:
                raise HarnessError("MCP_CLIENT_SERVER_BOUND")
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in cursors or len(cursors) >= 128:
                raise HarnessError("MCP_CLIENT_PAGINATION_INVALID")
            cursors.add(cursor)
        errors = []
        for name, expected in config["mcp_servers"].items():
            actual = servers.get(name)
            required = expected.get("required", False)
            if actual is None:
                if required:
                    errors.append(name + ":server_absent")
                continue
            tools = actual.get("tools")
            if not isinstance(tools, dict) or actual.get("toolsError") is not None:
                errors.append(name + ":catalog_failed")
                continue
            enabled, disabled = expected.get("enabled_tools"), expected.get("disabled_tools", [])
            if enabled is not None and (not isinstance(enabled, list) or any(x not in enabled for x in tools)):
                errors.append(name + ":tool_policy_violation")
            if any(x in disabled for x in tools):
                errors.append(name + ":disabled_tool_exposed")
            if required and enabled is not None and any(x not in tools for x in enabled if x not in disabled):
                errors.append(name + ":required_tool_missing")
            if required and actual.get("runtimeStatus") != "connected":
                errors.append(name + ":live_connection_unverified")
            for tool_name, definition in sorted(tools.items()):
                if not isinstance(definition, dict) or definition.get("name") != tool_name:
                    raise HarnessError("MCP_CLIENT_INVALID_TOOL")
                result["tools"].append({"server": name, "name": tool_name, "schema_digest": digest(definition)})
                if len(result["tools"]) > MAX_TOOLS:
                    raise HarnessError("MCP_CLIENT_TOOL_BOUND")
        unexpected = sorted(set(servers) - set(config["mcp_servers"]))
        if unexpected:
            errors.append("undeclared_servers_present")
        result.update(client_catalog_verified=not errors, catalog_digest=digest(result["tools"]),
            live_connection_verified=not errors and all(s.get("runtimeStatus") == "connected" for s in servers.values()),
            server_connections={name: value.get("runtimeStatus") for name, value in sorted(servers.items())},
            status="READY" if not errors else "BLOCKED", reason="native_catalog_observed" if not errors else ";".join(errors))
    except (HarnessError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        result["reason"] = str(error) if isinstance(error, HarnessError) else type(error).__name__
    finally:
        if client is not None:
            try:
                client.close()
                result["process_tree_stopped"] = client.stopped
            except (HarnessError, OSError, subprocess.TimeoutExpired):
                result.update(status="BLOCKED", client_catalog_verified=False, live_connection_verified=False,
                              reason="MCP_CLIENT_CLEANUP_UNCONFIRMED")
            result["event_stream_digest"] = client.events_digest.hexdigest()
            result["stderr_digest"] = client.errors_digest.hexdigest()
            result["event_count"] = client.messages
        if private is not None:
            try:
                private.cleanup()
            except OSError:
                result.update(status="BLOCKED", client_catalog_verified=False, live_connection_verified=False,
                              reason="MCP_CLIENT_PRIVATE_CLEANUP_FAILED")
    result["finished_at"] = now()
    return result
