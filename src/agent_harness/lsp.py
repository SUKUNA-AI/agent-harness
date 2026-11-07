"""On-demand local LSP navigation and reviewed rename plans; no daemon or index."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

from .context import _path, _root
from .core import HarnessError, atomic_json
from .rename import (MAX_FILES, MAX_TOTAL_BYTES, Positions, apply_plan, capture,
                     bound_apply_result, make_plan, read_plan)

MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_STDERR_BYTES = 65536
MAX_ITEMS = 10000
LANGUAGES = {
    ".py": "python", ".pyi": "python", ".rs": "rust", ".go": "go",
    ".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript",
    ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
}
SERVERS = {
    "python": [("pyright-langserver", "--stdio"), ("pylsp",)],
    "rust": [("rust-analyzer",)], "go": [("gopls",)],
    "typescript": [("typescript-language-server", "--stdio")],
    "typescriptreact": [("typescript-language-server", "--stdio")],
    "javascript": [("typescript-language-server", "--stdio")],
    "javascriptreact": [("typescript-language-server", "--stdio")],
    "c": [("clangd",)], "cpp": [("clangd",)],
}
METHODS = {"symbols": ("textDocument/documentSymbol", "documentSymbolProvider"),
           "definition": ("textDocument/definition", "definitionProvider"),
           "references": ("textDocument/references", "referencesProvider"),
           "rename": ("textDocument/rename", "renameProvider")}


def _executable(found):
    """Resolve only known npm wrappers through package metadata, never a shell."""
    path = Path(found).absolute()
    if path.suffix.lower() not in {".cmd", ".bat"}:
        return [str(path)]
    packages = {"pyright-langserver": "pyright", "typescript-language-server": "typescript-language-server"}
    name = path.stem.lower()
    package = packages.get(name)
    node = shutil.which("node")
    if package is None or not node or Path(node).suffix.lower() in {".cmd", ".bat"}:
        return None
    candidates = [path.parent / "node_modules" / package]
    if path.parent.name == ".bin" and path.parent.parent.name == "node_modules":
        candidates.append(path.parent.parent / package)
    for candidate in candidates:
        try:
            package_root = candidate.resolve(strict=True)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
            import stat
            with os.fdopen(os.open(package_root / "package.json", flags), "rb") as manifest:
                if not stat.S_ISREG(os.fstat(manifest.fileno()).st_mode):
                    continue
                raw = manifest.read(65537)
            if len(raw) > 65536:
                continue
            metadata = json.loads(raw.decode("utf-8"))
            bins = metadata.get("bin") if isinstance(metadata, dict) else None
            target = bins.get(name) if isinstance(bins, dict) else bins
            if (metadata.get("name") != package or not isinstance(target, str) or not target
                    or "\x00" in target or Path(target).is_absolute() or ":" in target):
                continue
            script = (package_root / target).resolve(strict=True)
            if not script.is_relative_to(package_root) or not script.is_file():
                continue
            return [str(Path(node).absolute()), str(script)]
        except (OSError, ValueError, TypeError):
            continue
    return None


def _language(name, explicit=None):
    value = explicit or LANGUAGES.get(Path(name).suffix.lower())
    if not isinstance(value, str) or not value or len(value) > 100:
        raise HarnessError("Unknown language; provide --language and --server JSON argv")
    return value


def _argv(value, language):
    if value is None:
        for candidate in SERVERS.get(language, []):
            found = shutil.which(candidate[0])
            resolved = _executable(found) if found else None
            if resolved:
                return [*resolved, *candidate[1:]]
        choices = ", ".join(row[0] for row in SERVERS.get(language, [])) or "a compatible language server"
        raise HarnessError("No local LSP server available. Install " + choices
                           + " explicitly or pass --server JSON argv; no server is auto-installed")
    if isinstance(value, str):
        value = json.loads(value)
    if (not isinstance(value, list) or not value or len(value) > 64
            or any(not isinstance(item, str) or not item or "\x00" in item or len(item) > 8192 for item in value)):
        raise HarnessError("--server must be a nonempty JSON argv array; shell commands are unsupported")
    found = shutil.which(value[0])
    if not found:
        raise HarnessError("LSP executable is unavailable; inspect --server or PATH")
    resolved = _executable(found)
    if resolved is None:
        raise HarnessError("Shell wrappers are unsupported; pass the executable and script separately, e.g. node + server.js")
    return [*resolved, *value[1:]]


def doctor(root=".", *, file=None, server=None, language=None, max_bytes=8192):
    _root(root)
    selected = _language(file, language) if file is not None else language
    names = list(dict.fromkeys(row[0] for rows in SERVERS.values() for row in rows))
    items = []
    for name in names:
        found = shutil.which(name)
        wrapper = bool(found and Path(found).suffix.lower() in {".cmd", ".bat"})
        resolved = _executable(found) if found else None
        items.append({"server": name, "executable": found, "available": resolved is not None,
                      "launch_prefix": resolved, "requires_explicit_executable_argv": wrapper and resolved is None,
                      "languages": [key for key, rows in SERVERS.items() if any(row[0] == name for row in rows)]})
    result = {"status": "inventory", "operation": "doctor", "items": items,
              "selected_language": selected, "servers_started": 0,
              "runtime_capabilities_verified": False,
              "hint": "Use a locally installed server or explicit --server JSON argv; never auto-installed"}
    if server is not None:
        command = _argv(server, selected)
        result["explicit_server"] = {"executable": command[0], "argument_count": len(command) - 1}
    return bounded(result, max_bytes)


class Session:
    """A bounded stdio JSON-RPC session with tree cleanup on every exit path."""
    def __init__(self, root, argv, *, timeout=20, settings=None, backend=None):
        if (type(timeout) not in (int, float) or not 0.1 <= timeout <= 120
                or (settings is not None and not isinstance(settings, dict))):
            raise HarnessError("LSP timeout must be 0.1..120 seconds and settings must be an object")
        self.root, self.argv, self.settings = root, argv, settings or {}
        self.deadline = time.monotonic() + timeout
        self.incoming, self.outgoing = queue.Queue(maxsize=32), queue.Queue(maxsize=4)
        self.stop = threading.Event()
        self.threads = []
        self.process = self.job = None
        self.counter = 0
        self.progress = set()
        self.server_busy = False
        self.registrations = set()
        self.stderr_tail = bytearray()
        self.stderr_bytes = 0
        self.capabilities = {}
        self.server_info = {"name": Path(argv[0]).name}
        self.initialized = False
        self.backend = backend if backend is not None else os.environ.get("AGENT_HARNESS_LSP_BACKEND", "python")
        if not isinstance(self.backend,str) or self.backend not in {"python", "native"}:
            raise HarnessError("AGENT_HARNESS_LSP_BACKEND must be python or native")
        self._native = None
        self._native_shutdown_ack = False
        self._native_exit_admitted = False

    def _post(self, value):
        while not self.stop.is_set():
            try:
                self.incoming.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            total = messages = 0
            source = self.process.stdout
            while not self.stop.is_set():
                headers, used = {}, 0
                while True:
                    line = source.readline(8193)
                    if not line:
                        raise HarnessError("LSP server closed stdout before completing the response")
                    used += len(line)
                    if used > 8192 or not line.endswith(b"\r\n"):
                        raise HarnessError("Invalid or oversized LSP framing header")
                    if line == b"\r\n":
                        break
                    name, separator, value = line[:-2].partition(b":")
                    key = name.lower().strip()
                    if not separator or key in headers:
                        raise HarnessError("Invalid or duplicate LSP framing header")
                    headers[key] = value.strip()
                length = headers.get(b"content-length", b"")
                if not length.isdigit() or not 0 < int(length) <= MAX_FRAME_BYTES:
                    raise HarnessError("Invalid or oversized LSP Content-Length")
                total += used + int(length)
                messages += 1
                if total > MAX_STREAM_BYTES or messages > 20000:
                    raise HarnessError("LSP response stream exceeded bounded-session limits")
                remaining, parts = int(length), []
                while remaining:
                    data = source.read(remaining)
                    if not data:
                        raise HarnessError("Truncated LSP JSON-RPC frame")
                    parts.append(data)
                    remaining -= len(data)
                message = json.loads(b"".join(parts).decode("utf-8"))
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise HarnessError("Invalid LSP JSON-RPC response")
                self._post(message)
        except (OSError, ValueError, HarnessError, RecursionError) as error:
            self._post(error)

    def _write(self):
        try:
            while not self.stop.is_set():
                try:
                    frame = self.outgoing.get(timeout=0.1)
                except queue.Empty:
                    continue
                view = memoryview(frame)
                while view and not self.stop.is_set():
                    written = self.process.stdin.write(view)
                    if not written:
                        raise HarnessError("LSP server stopped reading stdin")
                    view = view[written:]
        except (OSError, ValueError, HarnessError) as error:
            self._post(error)

    def _stderr(self):
        try:
            while True:
                data = self.process.stderr.read(4096)
                if not data:
                    return
                self.stderr_bytes += len(data)
                self.stderr_tail.extend(data)
                del self.stderr_tail[:-MAX_STDERR_BYTES]
        except (OSError, ValueError):
            pass

    def _send(self, message, deadline=None):
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_FRAME_BYTES:
            raise HarnessError("LSP request exceeds the frame limit")
        remaining = (self.deadline if deadline is None else deadline) - time.monotonic()
        if remaining <= 0:
            raise HarnessError("LSP session timed out")
        if self._native is not None:
            self._native.send(body, self.deadline if deadline is None else deadline)
            return
        try:
            self.outgoing.put(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body,
                              timeout=remaining)
        except queue.Full:
            raise HarnessError("LSP server did not consume requests before the deadline") from None

    def notify(self, method, params, *, deadline=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params}, deadline)

    def _server_message(self, message, deadline):
        method, params = message.get("method"), message.get("params") or {}
        if not isinstance(method, str):
            return False
        if "id" in message:
            result, error = None, None
            if method == "workspace/configuration":
                items = params.get("items", []) if isinstance(params, dict) else []
                if not isinstance(items, list) or len(items) > 1000:
                    error = {"code": -32602, "message": "Invalid configuration request"}
                else:
                    result = []
                    for item in items:
                        value = self.settings
                        section = item.get("section", "") if isinstance(item, dict) else ""
                        for key in section.split(".") if isinstance(section, str) and section else []:
                            value = value.get(key) if isinstance(value, dict) else None
                        result.append(value)
            elif method == "workspace/workspaceFolders":
                result = [{"uri": self.root.as_uri(), "name": self.root.name}]
            elif method in {"client/registerCapability", "client/unregisterCapability"}:
                if isinstance(params, dict):
                    for entry in params.get("registrations", []):
                        if isinstance(entry, dict) and isinstance(entry.get("method"), str):
                            self.registrations.add(entry["method"])
            elif method in {"window/workDoneProgress/create", "window/showMessageRequest"}:
                pass
            elif method == "workspace/applyEdit":
                result = {"applied": False, "failureReason": "Only an explicitly applied reviewed rename plan may write files"}
            else:
                error = {"code": -32601, "message": "Client method not supported"}
            response = {"jsonrpc": "2.0", "id": message["id"]}
            response["error" if error else "result"] = error if error else result
            self._send(response, deadline)
        elif method == "$/progress" and isinstance(params, dict):
            token, value = params.get("token"), params.get("value")
            if type(token) in (str, int) and isinstance(value, dict):
                if value.get("kind") == "begin":
                    self.progress.add(token)
                elif value.get("kind") == "end":
                    self.progress.discard(token)
        elif method == "experimental/serverStatus" and isinstance(params, dict):
            self.server_busy = params.get("quiescent") is False
        return True

    def _receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessError("LSP session timed out")
        try:
            message = self.incoming.get(timeout=remaining)
        except queue.Empty:
            raise HarnessError("LSP session timed out") from None
        if isinstance(message, BaseException):
            raise HarnessError("LSP transport failed: " + str(message)[:300]) from message
        return message

    def request(self, method, params, *, deadline=None):
        deadline = self.deadline if deadline is None else deadline
        self.counter += 1
        request_id = self.counter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, deadline)
        while True:
            message = self._receive(deadline)
            if self._server_message(message, deadline):
                continue
            if message.get("id") != request_id:
                raise HarnessError("Unexpected LSP response ID")
            if ("result" in message) == ("error" in message):
                raise HarnessError("LSP response must contain exactly one result or error")
            if "error" in message:
                value = message["error"]
                detail = value.get("message", "request failed") if isinstance(value, dict) else "invalid error"
                raise HarnessError("LSP server rejected " + method + ": " + str(detail)[:300])
            return message["result"]

    def supports(self, method, capability):
        value = self.capabilities.get(capability)
        return value is True or isinstance(value, dict) or method in self.registrations

    def __enter__(self):
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004 if os.name == "nt" else 0
        try:
            if self.backend == "native":
                from .lsp_native import NativeTransport
                self._native_shutdown_ack = self._native_exit_admitted = False
                self._native = NativeTransport(self.root,self.argv,self.deadline,self._post)
                self._native.start()
            else:
                self.process = subprocess.Popen(self.argv, cwd=self.root, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                            shell=False, start_new_session=os.name != "nt", creationflags=flags)
                if os.name == "nt":
                    from .runner import WindowsJob
                    self.job = WindowsJob(self.process)
                    self.job.resume_suspended_process(self.process)
                for target in (self._read, self._write, self._stderr):
                    thread = threading.Thread(target=target, daemon=True)
                    thread.start()
                    self.threads.append(thread)
            initialized = self.request("initialize", {
                "processId": os.getpid(), "clientInfo": {"name": "agent-harness"},
                "rootUri": self.root.as_uri(), "rootPath": str(self.root),
                "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                "capabilities": {
                    "general": {"positionEncodings": ["utf-16"]}, "offsetEncoding": ["utf-16"],
                    "window": {"workDoneProgress": True},
                    "workspace": {"configuration": True, "workspaceFolders": True, "applyEdit": False,
                                  "workspaceEdit": {"documentChanges": True, "resourceOperations": []}},
                    "textDocument": {"documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                                     "definition": {"linkSupport": True}, "references": {},
                                     "rename": {"prepareSupport": False}},
                }})
            if not isinstance(initialized, dict) or not isinstance(initialized.get("capabilities"), dict):
                raise HarnessError("LSP initialize returned invalid capabilities")
            self.capabilities = initialized["capabilities"]
            if self.capabilities.get("positionEncoding", "utf-16") != "utf-16":
                raise HarnessError("Server did not negotiate the required UTF-16 position encoding")
            offset_encoding = initialized.get("offsetEncoding", "utf-16")
            if offset_encoding not in ("utf-16", ["utf-16"]):
                raise HarnessError("Server selected an unsupported legacy position encoding")
            info = initialized.get("serverInfo")
            if isinstance(info, dict):
                self.server_info = {key: value[:100] for key, value in info.items()
                                    if key in {"name", "version"} and isinstance(value, str)} or self.server_info
            self.initialized = True
            self.notify("initialized", {})
            self.notify("workspace/didChangeConfiguration", {"settings": self.settings})
            return self
        except BaseException as error:
            if self._native is not None:
                try:
                    self.close()
                except (HarnessError,OSError) as close_error:
                    error.add_note("Native LSP closure also failed: "+str(close_error)[:300])
            else:
                self.close()
            raise

    def open(self, source, language):
        self.notify("textDocument/didOpen", {"textDocument": {"uri": (self.root / source["path"]).as_uri(),
                    "languageId": language, "version": 1, "text": source["text"]}})

    def ready(self, uri):
        # Requests form a server-side barrier; then drain startup progress rather
        # than treating initialize as proof that workspace indexing finished.
        barrier = self.supports(*METHODS["symbols"])
        if barrier:
            self.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        quiet_until = time.monotonic() + 0.15
        while True:
            now = time.monotonic()
            if now >= self.deadline:
                raise HarnessError("LSP workspace readiness timed out")
            if not self.progress and not self.server_busy and now >= quiet_until:
                break
            wait_until = min(self.deadline, now + 0.1)
            try:
                message = self.incoming.get(timeout=max(0, wait_until - now))
            except queue.Empty:
                continue
            if isinstance(message, BaseException):
                raise HarnessError("LSP transport failed during workspace readiness") from message
            if not self._server_message(message, self.deadline):
                raise HarnessError("Unexpected LSP response during workspace readiness")
            quiet_until = time.monotonic() + 0.15
        return {"request_barrier": barrier, "progress_quiet": True, "complete_index_verified": False}

    def close(self):
        if self._native is not None:
            transport = self._native
            try:
                if self.initialized and not transport._cancelled:
                    transport.closing = True
                    deadline = time.monotonic()+0.4
                    try:
                        if transport.error or transport._closed.is_set():
                            raise HarnessError("Native LSP ended before shutdown acknowledgement")
                        if self.request("shutdown",None,deadline=deadline) is not None:
                            raise HarnessError("LSP shutdown acknowledgement must have a null result")
                        self._native_shutdown_ack = True
                        self.notify("exit",None,deadline=deadline)
                        self._native_exit_admitted = True
                    except (HarnessError,OSError) as error:
                        if transport.error is None:
                            close_error = HarnessError("Native LSP shutdown/exit handshake failed")
                            close_error.__cause__ = error
                            transport.error = close_error
            finally:
                self.stop.set()
                try:
                    transport.close(require_terminal=self.initialized and not transport._cancelled)
                finally:
                    if not (self._native_shutdown_ack and self._native_exit_admitted) or transport._cancelled:
                        transport.session_complete = False
                    self.stderr_bytes = transport.stderr_bytes
                    self.stderr_tail = transport.stderr_tail
                    if transport.cleanup_complete:
                        self._native = None
            return
        if self.process is None:
            return
        process = self.process
        try:
            if self.initialized and process.poll() is None:
                deadline = time.monotonic() + 0.4
                try:
                    self.request("shutdown", None, deadline=deadline)
                    self.notify("exit", None, deadline=deadline)
                    process.wait(timeout=0.3)
                except (HarnessError, OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            self.stop.set()
            if self.job is not None:
                self.job.close()
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for thread in self.threads:
                thread.join(timeout=0.5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            self.process = None

    def __exit__(self, exc_type, exc, traceback):
        if self._native is not None and exc is not None:
            try:
                self.close()
            except (HarnessError,OSError) as close_error:
                exc.add_note("Native LSP closure also failed: "+str(close_error)[:300])
            return False
        self.close()


def _file_uri_path(uri, *, windows):
    if not isinstance(uri, str):
        raise HarnessError("LSP location URI must be a string")
    # urlsplit silently removes some raw controls; reject rather than interpret
    # a different file name. Percent-encoded controls are checked after decode.
    if any(ord(char) < 32 or ord(char) == 127 for char in uri):
        raise HarnessError("LSP file URI must not contain control characters")
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
        return None
    if re.search(r"%(?![0-9a-fA-F]{2})", parsed.path):
        raise HarnessError("Malformed percent escape in LSP file URI")
    try:
        decoded = unquote(parsed.path, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise HarnessError("LSP file URI requires valid UTF-8 percent encoding") from None
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded) or "\\" in decoded:
        raise HarnessError("LSP file URI contains unsupported control characters or backslashes")
    if decoded.startswith("//"):
        return None  # UNC/remote paths also have a no-authority URI spelling.
    if windows:
        # Python 3.11-3.13 url2pathname inspects literal ':' before unquoting.
        # Pyright emits /c%3A/...; decode exactly once, then remove the URI slash
        # before its drive. A second unquote would corrupt literal '%2F' names.
        if re.match(r"^/[a-zA-Z]:/", decoded):
            decoded = decoded[1:]
        path = PureWindowsPath(decoded)
    else:
        path = PurePosixPath(decoded)
    if not path.is_absolute():
        raise HarnessError("LSP file URI must be absolute")
    return path


def _uri_name(root, uri):
    path = _file_uri_path(uri, windows=os.name == "nt")
    if path is None:
        return None
    try:
        name = path.relative_to(root).as_posix()
    except ValueError:
        return None
    _path(root, name)
    return name


class Sources:
    def __init__(self, root, initial):
        self.root, self.values = root, {initial["path"]: initial}
        self.bytes = len(initial["data"])
        self.deadline = None

    def check_deadline(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise HarnessError("LSP request deadline exceeded while capturing targets")

    def get(self, name):
        self.check_deadline()
        if name not in self.values:
            if len(self.values) >= MAX_FILES:
                raise HarnessError("LSP result exceeds captured file/byte limits")
            value = capture(self.root, name)
            self.check_deadline()
            self.bytes += len(value["data"])
            if self.bytes > MAX_TOTAL_BYTES:
                raise HarnessError("LSP result exceeds captured file/byte limits")
            self.values[name] = value
        return self.values[name]

    def verify(self):
        for name, expected in self.values.items():
            self.check_deadline()
            current = capture(self.root, name)
            self.check_deadline()
            if current["identity"] != expected["identity"] or current["sha256"] != expected["sha256"]:
                raise HarnessError("LSP source changed; retry the request from current files")


def _location(root, value, sources):
    if not isinstance(value, dict):
        raise HarnessError("Invalid LSP location")
    name = _uri_name(root, value.get("uri", value.get("targetUri")))
    if name is None:
        return None
    selected = value.get("range", value.get("targetSelectionRange"))
    if not isinstance(selected, dict):
        raise HarnessError("Invalid LSP location range")
    source = sources.get(name)
    positions = Positions(source["text"])
    if positions.offset(selected.get("start")) > positions.offset(selected.get("end")):
        raise HarnessError("Inverted LSP location range")
    return {"path": name, "sha256": source["sha256"],
            "range": {"start": positions.human(selected.get("start")), "end": positions.human(selected.get("end"))}}


def _navigation_rows(root, name, operation, response):
    if response is None:
        response = []
    if isinstance(response, dict) and operation == "definition":
        response = [response]
    if not isinstance(response, list):
        raise HarnessError("Invalid LSP navigation result")
    pending = [(value, None) for value in reversed(response)]
    seen = 0
    while pending:
        value, container = pending.pop()
        seen += 1
        if seen > MAX_ITEMS or not isinstance(value, dict):
            raise HarnessError("Invalid or excessive LSP result items")
        if operation == "symbols":
            if not isinstance(value.get("name"), str) or type(value.get("kind")) is not int:
                raise HarnessError("Invalid LSP symbol")
            location = value.get("location") or {"uri": (root / name).as_uri(),
                                                   "range": value.get("selectionRange", value.get("range"))}
            metadata = {"name": value["name"], "kind": value["kind"]}
            if container or value.get("containerName"):
                metadata["container"] = container or value["containerName"]
            children = value.get("children", [])
            if not isinstance(children, list):
                raise HarnessError("Invalid LSP symbol children")
            pending.extend((child, value["name"]) for child in reversed(children))
            yield location, metadata
        else:
            yield value, {}


def _navigation_targets(root, name, operation, response):
    # Discover documents without interpreting potentially stale closed-file
    # ranges. Those ranges are used only after a request on opened snapshots.
    names = set()
    for location, _ in _navigation_rows(root, name, operation, response):
        if not isinstance(location, dict):
            raise HarnessError("Invalid LSP location")
        target = _uri_name(root, location.get("uri", location.get("targetUri")))
        if target is not None:
            names.add(target)
            if len(names) > MAX_FILES:
                raise HarnessError("LSP result exceeds captured file/byte limits")
    return names


def _navigation(root, name, operation, response, sources):
    items, omitted = [], 0
    for location, metadata in _navigation_rows(root, name, operation, response):
        item = _location(root, location, sources)
        if item is None:
            omitted += 1
        else:
            item.update(metadata)
            items.append(item)
    return items, omitted


def _changes(root, value):
    if value is None:
        return {}
    if not isinstance(value, dict) or value.get("changeAnnotations"):
        raise HarnessError("Only unannotated LSP workspace text edits are supported")
    if "changes" in value and "documentChanges" in value:
        raise HarnessError("Ambiguous LSP workspace edit: both change forms supplied")
    rows = []
    if "documentChanges" in value:
        if not isinstance(value["documentChanges"], list):
            raise HarnessError("Invalid LSP documentChanges")
        for item in value["documentChanges"]:
            if not isinstance(item, dict) or "kind" in item or not isinstance(item.get("textDocument"), dict):
                raise HarnessError("LSP resource create/rename/delete operations are unsupported")
            document = item["textDocument"]
            if document.get("version") is not None and document.get("version") != 1:
                raise HarnessError("LSP edit targets an unexpected document version")
            rows.append((document.get("uri"), item.get("edits")))
    elif "changes" in value:
        if not isinstance(value["changes"], dict):
            raise HarnessError("Invalid LSP changes object")
        rows = list(value["changes"].items())
    elif value:
        raise HarnessError("Unsupported LSP workspace edit")
    changes = {}
    for uri, edits in rows:
        name = _uri_name(root, uri)
        if name is None:
            raise HarnessError("Rename may only edit files inside the selected workspace")
        if name in changes or not isinstance(edits, list):
            raise HarnessError("Duplicate or invalid LSP document edits")
        changes[name] = edits
        if len(changes) > MAX_FILES:
            raise HarnessError("Rename exceeds the workspace file limit")
    return changes


def bounded(result, max_bytes):
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 262144:
        raise HarnessError("LSP output budget must be 1024..262144 UTF-8 bytes")
    if result.get("operation") == "apply":
        return bound_apply_result(result, max_bytes)
    result = dict(result)
    def fits(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 2 <= max_bytes
    key = "conflicts" if "conflicts" in result and "items" not in result else "items"
    items = result.get(key, [])
    result.update(total_items=len(items), returned_items=len(items), truncated=False, max_bytes=max_bytes)
    if fits(result):
        return result
    if "plan" in result:
        result.pop("plan")
        result["plan_included"] = False
        result["hint"] = "Full plan omitted from stdout; use --output outside the workspace to save a complete applyable plan"
    result["truncated"] = True
    low, high = 0, len(items)
    while low < high:
        middle = (low + high + 1) // 2
        result.update({key: items[:middle], "returned_items": middle})
        if fits(result):
            low = middle
        else:
            high = middle - 1
    result.update({key: items[:low], "returned_items": low})
    if not fits(result):
        raise HarnessError("LSP result metadata exceeds the output budget; increase --max-bytes")
    return result


def query(root, file, *, operation="symbols", line=None, column=None, new_name=None,
          server=None, language=None, timeout=20, settings=None, include_declaration=True,
          max_bytes=8192, output=None):
    if operation not in METHODS:
        raise HarnessError("Unknown LSP operation")
    bounded({}, max_bytes)
    if output is not None and operation != "rename":
        raise HarnessError("--output is supported for complete rename plans only")
    root = _root(root)
    output_path = Path(output).absolute() if output is not None else None
    if output_path is not None and output_path.resolve().is_relative_to(root):
        raise HarnessError("Save rename plans outside the source workspace")
    source = capture(root, file)
    language = _language(file, language)
    command = _argv(server, language)
    method, capability = METHODS[operation]
    uri = (root / file).as_uri()
    params = {"textDocument": {"uri": uri}}
    if operation != "symbols":
        params["position"] = Positions(source["text"]).from_human(line, column)
    if operation == "references":
        params["context"] = {"includeDeclaration": bool(include_declaration)}
    if operation == "rename":
        if (not isinstance(new_name, str) or not 1 <= len(new_name) <= 256
                or any(ord(char) < 32 for char in new_name)):
            raise HarnessError("Rename requires a nonempty --new-name without control characters (max 256)")
        params["newName"] = new_name
    sources = Sources(root, source)
    with Session(root, command, timeout=timeout, settings=settings) as client:
        sources.deadline = client.deadline
        client.open(source, language)
        readiness = client.ready(uri)
        if not client.supports(method, capability):
            raise HarnessError("The selected LSP server does not advertise " + operation)
        response = client.request(method, params)
        result = {"status": "preview" if operation == "rename" else "ok", "operation": operation,
                  "source": {"path": file, "sha256": source["sha256"]}, "server": client.server_info,
                  "positions": "1-based Unicode code points", "readiness": readiness}
        if operation == "rename":
            changes = _changes(root, response)
            opened = {file}
            for _ in range(3):
                unopened = set(changes) - opened
                if not unopened:
                    break
                for name in sorted(unopened):
                    client.open(sources.get(name), LANGUAGES.get(Path(name).suffix.lower(), language))
                    opened.add(name)
                # Bind closed-document edits to the exact text supplied above.
                response = client.request(method, params)
                changes = _changes(root, response)
            if set(changes) - opened:
                raise HarnessError("Rename target set did not stabilize; retry with a narrower workspace")
            sources.verify()
            plan = make_plan(root, sources.values, changes,
                             request={"file": file, "line": line, "column": column, "new_name": new_name},
                             server=client.server_info)
            result.update(plan=plan, plan_digest=plan["digest"], plan_included=True,
                          file_count=plan["file_count"], edit_count=plan["edit_count"],
                          items=[{"path": item["path"], "edits": len(item["edits"]),
                                  "before_sha256": item["before_sha256"], "after_sha256": item["after_sha256"]}
                                 for item in plan["files"]])
        else:
            opened = {file}
            targets = _navigation_targets(root, file, operation, response)
            for _ in range(3):
                unopened = targets - opened
                if not unopened:
                    break
                for name in sorted(unopened):
                    client.open(sources.get(name), LANGUAGES.get(Path(name).suffix.lower(), language))
                    opened.add(name)
                response = client.request(method, params)
                targets = _navigation_targets(root, file, operation, response)
            if targets - opened:
                raise HarnessError("Navigation target set did not stabilize; retry with a narrower workspace")
            result["items"], result["outside_workspace_omitted"] = _navigation(root, file, operation, response, sources)
            sources.verify()
    result["stderr_bytes"] = client.stderr_bytes
    result["stderr_truncated"] = client.stderr_bytes > MAX_STDERR_BYTES
    if output_path is not None:
        result["plan_path"] = str(output_path)
    result = bounded(result, max_bytes)
    if output_path is not None:
        atomic_json(output_path, plan, exclusive=True)
    return result


def dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for operation in METHODS:
        command = commands.add_parser(operation)
        command.add_argument("--root", required=True)
        command.add_argument("--file", required=True)
        command.add_argument("--server", help="JSON argv for a trusted local executable, without a shell")
        command.add_argument("--language")
        command.add_argument("--settings", type=json.loads)
        command.add_argument("--timeout", type=float, default=20)
        command.add_argument("--max-bytes", type=int, default=8192)
        if operation != "symbols":
            command.add_argument("--line", type=int, required=True)
            command.add_argument("--column", type=int, required=True)
        if operation == "references":
            command.add_argument("--exclude-declaration", action="store_true")
        if operation == "rename":
            command.add_argument("--new-name", required=True)
            command.add_argument("--output")
    apply = commands.add_parser("apply")
    apply.add_argument("--root", required=True)
    apply.add_argument("--plan", required=True)
    apply.add_argument("--max-bytes", type=int, default=8192)
    inventory = commands.add_parser("doctor")
    inventory.add_argument("--root", default=".")
    inventory.add_argument("--file")
    inventory.add_argument("--server")
    inventory.add_argument("--language")
    inventory.add_argument("--max-bytes", type=int, default=8192)
    args = parser.parse_args(argv)
    try:
        bounded({}, args.max_bytes)
        if args.command == "doctor":
            result = doctor(args.root, file=args.file, server=args.server, language=args.language, max_bytes=args.max_bytes)
        elif args.command == "apply":
            result = bounded(apply_plan(args.root, read_plan(args.plan), result_budget=args.max_bytes), args.max_bytes)
        else:
            result = query(args.root, args.file, operation=args.command,
                           line=getattr(args, "line", None), column=getattr(args, "column", None),
                           new_name=getattr(args, "new_name", None), server=args.server, language=args.language,
                           timeout=args.timeout, settings=args.settings, max_bytes=args.max_bytes,
                           include_declaration=not getattr(args, "exclude_declaration", False),
                           output=getattr(args, "output", None))
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 2 if result.get("status") in {"failed_compensated", "recovery_required"} else 0
    except (HarnessError, ValueError, OSError, TypeError, subprocess.SubprocessError) as error:
        result = {"status": "unavailable" if "No local LSP" in str(error) else "error",
                  "error": str(error)[:400], "type": type(error).__name__}
        budget = args.max_bytes if type(args.max_bytes) is int and args.max_bytes >= 1024 else 1024
        while len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 2 > budget:
            result["error"] = result["error"][:len(result["error"]) // 2]
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(dispatch(sys.argv[1:]))
