"""Bounded live MCP stdio diagnostics, not a model execution client or proxy.

All children use private process containment and are stopped on every path.
The published catalog is a policy projection of a live advertised catalog;
it is explicitly NOT an attestation of a Codex/OpenCode effective catalog.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from .codex_execution import _duplicate_free, _stop_owned_tree, sanitized
from .core import HarnessError, digest, file_hash, now, process_identity
from .runner import WindowsJob, _contained_capture

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 4096


class ProbeFailure(HarnessError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class StdioProbe(AbstractContextManager):
    """One short-lived diagnostic connection, never shared between worktrees."""
    def __init__(self, server, *, role="worker"):
        self.server = server
        self.role = role
        self.process = self.job = None
        self.identity = None
        self.inbox = queue.Queue(maxsize=256)
        self.readers = []
        self.next_id = 0
        self.events = []
        self.catalog = None
        self.server_info = None
        self.executable = None
        self.stopped = False

    def _reader(self, source, stderr=False):
        total = 0
        try:
            while True:
                raw = source.readline(MAX_MESSAGE_BYTES + 1)
                if not raw:
                    if not stderr:
                        self.inbox.put(("error", "DISCONNECTED"), timeout=1)
                    return
                total += len(raw)
                if len(raw) > MAX_MESSAGE_BYTES or total > MAX_TOTAL_BYTES:
                    self.inbox.put(("error", "PROTOCOL_LIMIT"), timeout=1)
                    return
                if stderr:
                    # Diagnostics retain counts only; untrusted server logs can
                    # contain credentials/documents and are not checkpoint input.
                    continue
                if not raw.endswith(b"\n"):
                    self.inbox.put(("error", "PARTIAL_MESSAGE"), timeout=1)
                    return
                try:
                    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_free)
                    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                        raise ValueError("Invalid MCP message")
                except (ValueError, UnicodeError, RecursionError):
                    self.inbox.put(("error", "INVALID_MESSAGE"), timeout=1)
                    return
                self.inbox.put(("message", value), timeout=1)
        except (OSError, ValueError, queue.Full):
            try:
                self.inbox.put(("error", "CAPTURE_FAILED"), timeout=0.1)
            except queue.Full:
                pass
        finally:
            source.close()

    def __enter__(self):
        spec = self.server
        if spec["transport"] != "stdio":
            raise ProbeFailure("UNSUPPORTED_TRANSPORT")
        self.executable = shutil.which(spec["command"][0])
        if self.executable is None:
            raise ProbeFailure("SERVER_MISSING")
        if spec.get("help_argv"):
            try:
                result = _contained_capture(spec["help_argv"], cwd=spec["cwd"], timeout=spec["startup_timeout"])
                text = result.stdout.decode("utf-8", errors="replace")
                if result.returncode or len(text) > MAX_MESSAGE_BYTES or any(flag not in text for flag in spec.get("required_help", [])):
                    raise ProbeFailure("UNSUPPORTED_CLI")
            except subprocess.TimeoutExpired as error:
                raise ProbeFailure("STARTUP_TIMEOUT") from error
        env = dict(os.environ)
        env.update(spec.get("environment", {}))
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004 if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen([self.executable, *spec["command"][1:]], cwd=spec["cwd"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                start_new_session=os.name != "nt", creationflags=flags, env=env)
            self.job = WindowsJob(self.process)
            self.job.resume_suspended_process(self.process)
            self.identity = process_identity(self.process.pid)
            if not self.identity:
                raise ProbeFailure("PROCESS_IDENTITY_UNKNOWN")
            for stream, stderr in ((self.process.stdout, False), (self.process.stderr, True)):
                thread = threading.Thread(target=self._reader, args=(stream, stderr), daemon=True)
                thread.start()
                self.readers.append(thread)
            self.events.append({"event": "process.started", "identity": self.identity, "cwd": spec["cwd"], "at": now()})
            return self
        except BaseException:
            self.close()
            raise

    def _send(self, message):
        data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        # A request fits one ordinary pipe buffer. A server that stops reading
        # cannot make a large blocking stdin write bypass the response timeout.
        if len(data) > MAX_REQUEST_BYTES:
            raise ProbeFailure("REQUEST_LIMIT")
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            raise ProbeFailure("DISCONNECTED") from error

    def request(self, method, params=None, *, timeout=None):
        self.next_id += 1
        ident = self.next_id
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        deadline = time.monotonic() + (timeout or self.server["startup_timeout"])
        for _ in range(256):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeFailure("TIMEOUT")
            try:
                kind, value = self.inbox.get(timeout=remaining)
            except queue.Empty as error:
                raise ProbeFailure("TIMEOUT") from error
            if kind == "error":
                raise ProbeFailure(value)
            if "id" not in value:
                # Server notifications are not evidence of a successful request.
                continue
            if type(value["id"]) is not int or value["id"] != ident or "method" in value:
                raise ProbeFailure("CONFLICTING_RESPONSE")
            if "error" in value:
                raise ProbeFailure("REQUEST_REJECTED")
            if "result" not in value:
                raise ProbeFailure("INVALID_MESSAGE")
            self.events.append({"event": "response", "method": method, "response_digest": digest(sanitized(value)), "at": now()})
            return value["result"]
        raise ProbeFailure("PROTOCOL_LIMIT")

    def inventory(self):
        try:
            initialized = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "agent-harness-diagnostic", "version": "1"}})
            if (not isinstance(initialized, dict) or initialized.get("protocolVersion") not in
                    {"2024-11-05", "2025-03-26", "2025-06-18"} or not isinstance(initialized.get("serverInfo"), dict)
                    or not isinstance(initialized.get("capabilities"), dict)):
                raise ProbeFailure("HANDSHAKE_FAILED")
            self.server_info = sanitized(initialized["serverInfo"])
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except ProbeFailure as error:
            if error.code in {"DISCONNECTED", "REQUEST_REJECTED", "INVALID_MESSAGE", "CONFLICTING_RESPONSE"}:
                raise ProbeFailure("HANDSHAKE_FAILED") from error
            raise
        tools, cursor, cursors = [], None, set()
        for _ in range(16):
            page = self.request("tools/list", {"cursor": cursor} if cursor is not None else {})
            if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
                raise ProbeFailure("INVALID_CATALOG")
            tools.extend(page["tools"])
            if len(tools) > 512:
                raise ProbeFailure("CATALOG_LIMIT")
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or len(cursor) > 1024 or cursor in cursors:
                raise ProbeFailure("INVALID_CATALOG")
            cursors.add(cursor)
        else:
            raise ProbeFailure("CATALOG_LIMIT")
        names = []
        for tool in tools:
            if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                    or not isinstance(tool.get("inputSchema"), dict) or len(tool["name"]) > 128):
                raise ProbeFailure("INVALID_CATALOG")
            names.append(tool["name"])
        if len(names) != len(set(names)):
            raise ProbeFailure("INVALID_CATALOG")
        if not set(self.server.get("required_tools", [])) <= set(names):
            raise ProbeFailure("MISSING_TOOL")
        allowed = set(self.server["allowed_tools"]) - set(self.server["forbidden_tools"])
        if self.role == "reviewer" and not allowed <= set(self.server["read_only_tools"]):
            raise ProbeFailure("TOOL_FORBIDDEN_BY_POLICY")
        self.catalog = [sanitized(tool) for tool in tools if tool["name"] in allowed]
        self.catalog.sort(key=lambda item: item["name"])
        self.advertised_catalog_digest = digest(sorted((sanitized(tool) for tool in tools), key=lambda item: item["name"]))
        # A post-catalog round trip rules out cached-only discovery and detects
        # a connection that is lost directly after tools/list.
        self.request("ping")
        return self.catalog

    def call_readonly(self, name, arguments=None):
        """Explicit diagnostic smoke only. Denial happens before any request."""
        allowed = set(self.server["allowed_tools"]) - set(self.server["forbidden_tools"])
        if name not in allowed or name not in self.server["read_only_tools"]:
            raise ProbeFailure("TOOL_FORBIDDEN_BY_POLICY")
        if self.catalog is None or name not in {tool["name"] for tool in self.catalog}:
            raise ProbeFailure("MISSING_TOOL")
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=self.server["tool_timeout"])
        if not isinstance(result, dict) or result.get("isError"):
            raise ProbeFailure("TOOL_FAILED")
        return sanitized(result)

    def close(self):
        if self.process is None:
            return
        if self.process.stdin:
            self.process.stdin.close()
        try:
            if self.job is None and os.name == "nt":
                # A job-construction failure leaves the child suspended.
                self.process.kill()
                self.process.wait(timeout=5)
            else:
                _stop_owned_tree(self.process, self.job, self.identity)
            self.stopped = True
        finally:
            if self.job:
                self.job.close()
            for reader in self.readers:
                reader.join(timeout=2)
        self.events.append({"event": "process.stopped", "confirmed": self.stopped, "at": now()})

    def __exit__(self, kind, value, traceback):
        self.close()
        return False


def probe_server(server, *, role="worker"):
    receipt = {"id": server["id"], "status": "BLOCKED", "reason": None, "catalog": [],
        "required": server["required"], "process": None, "process_tree_stopped": True,
        "live_connection": False, "client_catalog_verified": False, "at": now()}
    connection = None
    if server["transport"] != "stdio":
        receipt["reason"] = "BLOCKED_NOT_QUALIFIED: HTTP diagnostic transport not implemented"
        return receipt
    try:
        connection = StdioProbe(server, role=role)
        with connection:
            receipt["catalog"] = connection.inventory()
            receipt.update(status="READY", live_connection=True,
                process=connection.identity, cwd=server["cwd"], server_info=connection.server_info,
                executable=str(Path(connection.executable).resolve()),
                binary_sha256=file_hash(Path(connection.executable).resolve()),
                advertised_catalog_digest=connection.advertised_catalog_digest,
                catalog_digest=digest(receipt["catalog"]))
    except ProbeFailure as error:
        receipt["reason"] = error.code
    except (OSError, ValueError, HarnessError, subprocess.TimeoutExpired) as error:
        receipt["reason"] = "STARTUP_FAILED" if connection is None or connection.process is None else "PROBE_FAILED"
        receipt["error_type"] = type(error).__name__
    if connection is not None:
        receipt["process"] = connection.identity
        receipt["process_tree_stopped"] = connection.process is None or connection.stopped
        receipt["events"] = connection.events
        if not receipt["process_tree_stopped"]:
            receipt.update(status="BLOCKED", reason="UNKNOWN_PROCESS_OUTCOME")
    return receipt
