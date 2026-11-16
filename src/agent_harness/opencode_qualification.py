"""Native V2 qualification, owned private processes and exact session lookup.

Configuration is an input, never a permission attestation. Only this module's
live verifier may issue an in-memory proof; an operator JSON receipt is not an
input that can enable a backend. Evidence contains identities and observations,
not provider credentials, prompts or transcripts.
"""
from __future__ import annotations

import base64
import copy
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import threading
import time
from types import MappingProxyType
import urllib.error
import urllib.parse
import urllib.request

from .codex_execution import _binary_hash, _duplicate_free, _stop_owned_tree
from .core import HarnessError, digest, process_identity
from .file_io import canonical_directory, read_regular_file
from .runner import _start_contained
from .supervisor_store import atomic_write

ISSUER = object()
MAX_RESPONSE = 4 * 1024 * 1024
MAX_LINE = 1024 * 1024
PROOF_TTL = 60
DENIED_LEAF = ("subagent", "execute", "external_directory")
DENIED_REVIEWER = ("shell", "edit", "write", "patch")
GUARD_ID = "agent-harness.execution-guard"
GUARD_PACKAGE = {"name": "agent-harness-opencode-execution-guard", "version": "1.0.0",
                 "type": "module", "exports": "./index.js"}

# No prompts, credentials, headers or provider bodies are persisted. The native
# pre-turn hook sees the actual executable snapshot; the HTTP hook catches raw
# body overlays applied after protocol lowering. Unknown transports fail closed.
GUARD_SOURCE = r'''import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
const specPath = process.env.HARNESS_OPENCODE_GUARD_SPEC;
if (!specPath) throw new Error("HARNESS_GUARD_SPEC_MISSING");
const spec = JSON.parse(fs.readFileSync(specPath, "utf8"));
const canonical = value => JSON.stringify(value, (_, x) => x && typeof x === "object" && !Array.isArray(x)
  ? Object.fromEntries(Object.keys(x).sort().map(k => [k,x[k]])) : x);
const checksum = value => crypto.createHash("sha256").update(canonical(value)).digest("hex");
const catalog = tools => Object.entries(tools).map(([name,tool]) => ({name,
  input_schema:tool.input??{},description:tool.description??""})).sort((a,b)=>a.name<b.name?-1:a.name>b.name?1:0);
const write = (name,value) => {
  const raw = JSON.stringify(value)+"\n";
  if (Buffer.byteLength(raw)>4194304) throw new Error("HARNESS_GUARD_RECEIPT_TOO_LARGE");
  const fd = fs.openSync(name,fs.constants.O_WRONLY|fs.constants.O_APPEND|fs.constants.O_CREAT|fs.constants.O_NOFOLLOW,0o600);
  try { if (fs.fstatSync(fd).size+Buffer.byteLength(raw)>16777216) throw new Error("HARNESS_GUARD_LOG_TOO_LARGE");
    fs.writeSync(fd,raw);fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
};
export default {id:"agent-harness.execution-guard",async setup(ctx) {
  const root=fs.realpathSync(ctx.location.directory);
  if(root!==spec.root || process.env.PWD!==root) throw new Error("HARNESS_GUARD_WORKTREE_MISMATCH");
  const allowed=new Set(spec.allowed_tools), latest=new Map();let sequence=0;
  const sourceOK=()=>{
    const value=crypto.createHash("sha256").update(fs.readFileSync(spec.guard_source_path)).digest("hex");
    if(value!==spec.guard_source_digest)throw new Error("HARNESS_GUARD_SOURCE_DRIFT");
  };
  const connectedOK=async()=>{
    if(!(spec.connected_servers??[]).length)return;
    const result=await ctx.mcp.list();
    const live=new Set((result.data??result).filter(s=>s.status?.status==="connected").map(s=>s.name));
    if(!spec.connected_servers.every(name=>live.has(name)))throw new Error("HARNESS_GUARD_MCP_CONNECTION_LOST");
  };
  const runtime=process.env.HARNESS_OPENCODE_GUARD_RUNTIME;
  if(runtime && (path.dirname(path.resolve(runtime))!==spec.runtime_dir || fs.existsSync(runtime)))
    throw new Error("HARNESS_GUARD_RUNTIME_IDENTITY");
  const base={schema_version:1,proof_id:spec.proof_id,root,agent:spec.agent,role:spec.role};
  const inspect=(tools)=>{
    const result=catalog(tools);
    for(const item of result) if(!allowed.has(item.name)) throw new Error("HARNESS_GUARD_TOOL_DENIED:"+item.name);
    return result;
  };
  await ctx.session.hook("prompt",async()=>{
    const readiness=spec.phase==="qualification"?JSON.parse(fs.readFileSync(specPath,"utf8")):spec;
    const expected=readiness.expected_tools??[];
    const deadline=Date.now()+spec.readiness_timeout_ms;
    while(expected.length || (readiness.connected_servers??[]).length) {
      const tools=await ctx.tool.list();
      const names=new Set(tools.map(tool=>tool.id));
      const servers=await ctx.mcp.list();
      const ready=new Set((servers.data??servers).filter(s=>s.status?.status==="connected").map(s=>s.name));
      if(expected.every(name=>names.has(name)) && (readiness.connected_servers??[]).every(name=>ready.has(name)))return;
      if(Date.now()>=deadline)throw new Error("HARNESS_GUARD_LIVE_CATALOG_NOT_READY");
      await new Promise(resolve=>setTimeout(resolve,25));
    }
  });
  const context=kind=>async event=>{
    sourceOK();
    await connectedOK();
    if(event.agent!==spec.agent) throw new Error("HARNESS_GUARD_AGENT_MISMATCH");
    // V2 injects skill and Git worktree tools independently of ordinary tool
    // permissions. Project every assembled snapshot onto the qualified grants;
    // execute.before still rejects unsolicited calls to any removed operation.
    for(const name of Object.keys(event.tools))if(!allowed.has(name))delete event.tools[name];
    if(kind==="primary" && spec.expected_model && canonical(event.model)!==canonical(spec.expected_model))
      throw new Error("HARNESS_GUARD_MODEL_DRIFT");
    const actual=inspect(event.tools);
    const value={...base,stage:"context",kind,request_sequence:++sequence,session_id:event.sessionID,model:event.model,
      catalog:actual,catalog_digest:checksum(actual)};
    if(spec.phase==="qualification") {
      write(spec.probe_path,{...value,stage:"qualification_before_dispatch",
        history_contains_probe_marker:JSON.stringify(event.messages).includes(spec.marker)});
      throw new Error("HARNESS_QUALIFICATION_ABORT_BEFORE_MODEL");
    }
    if(kind==="primary" && value.catalog_digest!==spec.expected_catalog_digest)
      throw new Error("HARNESS_GUARD_CATALOG_DRIFT");
    latest.set(event.sessionID+":"+kind,value);
    if(runtime)write(runtime,value);
  };
  await ctx.session.hook("context",context("primary"));
  await ctx.session.hook("compaction",context("compaction"));
  await ctx.session.hook("generate",context("generate"));
  await ctx.tool.hook("execute.before",event=>{
    sourceOK();
    if(!allowed.has(event.tool))throw new Error("HARNESS_GUARD_EXECUTION_DENIED:"+event.tool);
  });
  await ctx.session.hook("experimental.ws.handshake",()=>{throw new Error("HARNESS_GUARD_UNQUALIFIED_WEBSOCKET")});
  await ctx.session.hook("http.request",async event=>{
    sourceOK();
    await connectedOK();
    if(event.kind==="title") throw new Error("HARNESS_GUARD_UNPLANNED_TITLE_REQUEST");
    const previous=latest.get(event.sessionID+":"+event.kind);
    if(!previous)throw new Error("HARNESS_GUARD_CONTEXT_MISSING");
    const clone=event.request.clone();const raw=await clone.text();
    if(Buffer.byteLength(raw)>16777216)throw new Error("HARNESS_GUARD_WIRE_BODY_TOO_LARGE");
    const body=JSON.parse(raw);let wire;
    if(Array.isArray(body.tools))wire=body.tools.map(t=>t.function
      ? {name:t.function.name,input_schema:t.function.parameters??{},description:t.function.description??""}
      : {name:t.name,input_schema:t.input_schema??t.parameters??{},description:t.description??""});
    else if(body.tools===undefined)wire=[];
    else throw new Error("HARNESS_GUARD_UNQUALIFIED_WIRE_TOOLS");
    wire.sort((a,b)=>a.name<b.name?-1:a.name>b.name?1:0);
    if(checksum(wire)!==previous.catalog_digest)throw new Error("HARNESS_GUARD_WIRE_CATALOG_MISMATCH");
    if(runtime)write(runtime,{...previous,stage:"before_dispatch"});
  });
  await ctx.session.hook("http.response",async event=>{
    if(!(spec.connected_servers??[]).length)return;
    const previous=latest.get(event.sessionID+":"+event.kind);
    if(!previous)throw new Error("HARNESS_GUARD_CONTEXT_MISSING");
    const finish=async()=>{
      sourceOK();await connectedOK();
      if(runtime)write(runtime,{...previous,stage:"after_response"});
    };
    if(!event.response.body){await finish();return;}
    const body=event.response.body.pipeThrough(new TransformStream({
      transform(chunk,controller){controller.enqueue(chunk)},flush:finish
    }));
    event.response=new Response(body,{status:event.response.status,statusText:event.response.statusText,headers:event.response.headers});
  });
}};
'''


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_duplicate_free,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise HarnessError("Invalid bounded native OpenCode JSON: " + type(error).__name__) from error


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HarnessError("OpenCode qualification deadline exhausted")
    return remaining


def _capture_owned(argv, *, cwd, environment, deadline):
    """Bound both diagnostic pipes and confirm all descendants have stopped."""
    process, job = _start_contained(argv, cwd=cwd, env=environment)
    identity = process_identity(process.pid)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    errors = []
    threads = []
    mutex = threading.Lock()
    def capture(stream, name):
        try:
            while True:
                chunk = stream.read(16384)
                if not chunk:
                    return
                with mutex:
                    if sum(map(len, buffers.values())) + len(chunk) > MAX_RESPONSE:
                        errors.append("Native diagnostic capture exceeds bounds")
                        return
                    buffers[name].extend(chunk)
        except OSError:
            errors.append("Native diagnostic capture failed")
    try:
        for source, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            thread = threading.Thread(target=capture, args=(source, name), daemon=True)
            thread.start()
            threads.append(thread)
        while process.poll() is None:
            if errors:
                raise HarnessError(errors[0])
            time.sleep(min(.02, _remaining(deadline)))
    finally:
        try:
            _stop_owned_tree(process, job, identity)
        finally:
            job.close()
            for thread in threads:
                thread.join(timeout=1)
            # A retained pipe from an escaped/unknown process must not make
            # BufferedReader.close block while a reader owns its I/O lock.
            if not any(thread.is_alive() for thread in threads):
                process.stdout.close()
                process.stderr.close()
    if errors or any(thread.is_alive() for thread in threads):
        raise HarnessError(errors[0] if errors else "Native diagnostic capture remains uncertain")
    return subprocess.CompletedProcess(argv, process.returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"]))


def _file_identity(path):
    path = Path(path)
    info = path.stat()
    return {"path": str(path.resolve()), "device": info.st_dev, "inode": info.st_ino,
            "bytes": info.st_size, "mtime_ns": info.st_mtime_ns}


def input_identity(adapter, root):
    """Recheck launch/config/root inputs without recording environment values."""
    root = canonical_directory(root)
    info = root.stat()
    environment = dict(adapter.environment if adapter.environment is not None else os.environ)
    environment.pop("HARNESS_OPENCODE_GUARD_RUNTIME", None)
    sources = []
    home = Path(environment.get("HOME", Path.home()))
    config_home = Path(environment.get("XDG_CONFIG_HOME", home / ".config"))
    directories = {config_home / "opencode", home / ".opencode", root / ".opencode"}
    current = root
    while True:
        directories.add(current)
        if current.parent == current:
            break
        current = current.parent
    for name in ("OPENCODE_CONFIG",):
        if environment.get(name):
            path = Path(environment[name])
            if path.is_file():
                sources.append({**_file_identity(path), "sha256": hashlib.sha256(read_regular_file(path)).hexdigest()})
    if environment.get("OPENCODE_CONFIG_DIR"):
        directories.add(Path(environment["OPENCODE_CONFIG_DIR"]))
    for directory in sorted(directories, key=str):
        for name in ("config.json", "opencode.json", "opencode.jsonc"):
            path = directory / name
            if path.is_file():
                sources.append({**_file_identity(path), "sha256": hashlib.sha256(read_regular_file(path)).hexdigest()})
    launchers = []
    for value in adapter.executable[1:]:
        path = Path(value)
        if path.is_file():
            launchers.append({**_file_identity(path), "sha256": hashlib.sha256(read_regular_file(path)).hexdigest()})
    return {"binary": {**_file_identity(adapter.executable[0]), "sha256": _binary_hash(adapter.executable[0])},
        "launcher_files": launchers, "argv": list(adapter.executable),
        "root": {"path": str(root), "device": info.st_dev, "inode": info.st_ino},
        "tool_config_digest": digest(adapter.tool_config), "config_sources": sources,
        "environment_digest": digest(environment)}


@dataclass(frozen=True)
class Qualification:
    """Process-local issuer capability. Persisted receipts cannot construct one."""
    _issuer: object
    identity: dict
    role: str
    created_monotonic: float
    receipt: dict
    catalog: tuple
    flags: MappingProxyType

    def valid(self, adapter, root):
        return (self._issuer is ISSUER and time.monotonic() - self.created_monotonic < PROOF_TTL
                and self.identity == input_identity(adapter, root))


class NativeServer:
    """One authenticated private API server in the existing contained lifecycle."""
    def __init__(self, adapter, root, *, deadline, config=None, environment=None):
        self.adapter, self.root, self.deadline = adapter, str(canonical_directory(root)), deadline
        self.environment = dict(environment if environment is not None else
            adapter.environment if adapter.environment is not None else os.environ)
        self.environment["PWD"] = self.root
        self.nonce = secrets.token_urlsafe(32)
        self.environment["OPENCODE_PASSWORD"] = self.nonce
        if config is not None:
            self.environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, sort_keys=True,
                separators=(",", ":"), allow_nan=False)
        self.process = self.job = self.identity = None
        self.address = None
        self.threads = []
        self.inbox = queue.Queue(maxsize=64)
        self.capture_error = None
        self.capture_bytes = 0
        self.capture_lock = threading.Lock()

    def _read(self, stream, name):
        try:
            while True:
                raw = stream.readline(MAX_LINE + 1)
                if not raw:
                    break
                with self.capture_lock:
                    self.capture_bytes += len(raw)
                    if len(raw) > MAX_LINE or self.capture_bytes > MAX_RESPONSE:
                        self.capture_error = "Native private-server capture exceeds bounds"
                        break
                if name == "stdout":
                    try:
                        self.inbox.put_nowait(raw)
                    except queue.Full:
                        self.capture_error = "Native private-server readiness flood"
                        break
        except OSError:
            self.capture_error = "Native private-server capture failed"

    def __enter__(self):
        try:
            self.process, self.job = _start_contained([*self.adapter.executable,
                "serve", "--stdio", "--port", "0"], cwd=self.root,
                env=self.environment, stdin=subprocess.PIPE)
            self.identity = process_identity(self.process.pid)
            for source, name in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
                thread = threading.Thread(target=self._read, args=(source, name), daemon=True)
                thread.start()
                self.threads.append(thread)
            while True:
                if self.capture_error:
                    raise HarnessError(self.capture_error)
                if self.process.poll() is not None:
                    raise HarnessError("Native private server exited before readiness")
                try:
                    raw = self.inbox.get(timeout=min(.1, _remaining(self.deadline)))
                except queue.Empty:
                    continue
                value = _json(raw)
                address = value.get("url") if isinstance(value, dict) else None
                parsed = urllib.parse.urlsplit(address or "")
                if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                        or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment):
                    raise HarnessError("Unsafe native private-server readiness endpoint")
                self.address = address.rstrip("/")
                return self
        except BaseException:
            self.close()
            raise

    def request(self, method, path, body=None, *, allowed=(200,)):
        if not self.address or not path.startswith("/api/") or "\0" in path:
            raise HarnessError("Invalid native qualification API request")
        if self.capture_error or self.process.poll() is not None:
            raise HarnessError("Native private server lost readiness")
        auth = "Basic " + base64.b64encode(("opencode:" + self.nonce).encode()).decode()
        raw = json.dumps(body, allow_nan=False).encode() if body is not None else None
        if raw is not None and len(raw) > MAX_RESPONSE:
            raise HarnessError("Native API request exceeds bound")
        request = urllib.request.Request(self.address + path, data=raw, method=method,
            headers={"Authorization": auth, "Content-Type": "application/json"})
        # Local authenticated requests must not use the user's proxy or redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=min(10, _remaining(self.deadline))) as response:
                status, content = response.status, response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            try:
                status, content = error.code, error.read(MAX_RESPONSE + 1)
            finally:
                error.close()
        if len(content) > MAX_RESPONSE:
            raise HarnessError("Native API response exceeds bound")
        if status not in allowed:
            raise HarnessError("Native API " + method + " " + path.split("?")[0] + " returned " + str(status))
        return status, _json(content) if content else None

    def data(self, method, path, body=None):
        _, value = self.request(method, path, body)
        if not isinstance(value, dict) or "data" not in value:
            raise HarnessError("Native API data envelope is missing")
        return value["data"]

    def close(self):
        try:
            if self.process is not None and self.job is not None:
                _stop_owned_tree(self.process, self.job, self.identity)
        finally:
            if self.job is not None:
                self.job.close()
            for thread in self.threads:
                thread.join(timeout=1)
            if self.process is not None:
                sources = [self.process.stdin]
                if not any(thread.is_alive() for thread in self.threads):
                    sources.extend((self.process.stdout, self.process.stderr))
                for source in sources:
                    if source is not None:
                        try:
                            source.close()
                        except OSError:
                            pass
            self.address = None
        if any(thread.is_alive() for thread in self.threads):
            raise HarnessError("Native private-server pipes remain uncertain after cleanup")

    def __exit__(self, *args):
        self.close()


def check_session(server, session_id, root):
    """Missing IDs fail before CLI invocation; never manufacture a continuation."""
    from .codex_execution import SESSION
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise HarnessError("An exact safe OpenCode session ID is required")
    escaped = urllib.parse.quote(session_id, safe="")
    status, value = server.request("GET", "/api/session/" + escaped, allowed=(200, 404))
    if status == 404:
        raise HarnessError("EXACT_RESUME_MISSING_SESSION: no replacement session is permitted")
    info = value.get("data") if isinstance(value, dict) else None
    if (not isinstance(info, dict) or info.get("id") != session_id
            or not isinstance(info.get("location"), dict)
            or canonical_directory(info["location"].get("directory", "")) != canonical_directory(root)):
        raise HarnessError("EXACT_RESUME_DRIFT: native session/worktree identity changed")
    exported = server.data("GET", "/api/experimental/session/" + escaped + "/export?sanitize=true")
    if (not isinstance(exported, dict) or exported.get("info", {}).get("id") != session_id
            or not isinstance(exported.get("messages"), list)):
        raise HarnessError("EXACT_RESUME_DRIFT: native session export is not exact")
    return {"session_id": session_id, "project_id": info.get("projectID"), "model": info.get("model"),
            "message_count": len(exported["messages"]), "export_digest": digest(exported)}


def _write_owned(path, value, *, raw=False):
    path = Path(path)
    data = value if raw else (json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False) + "\n").encode()
    if path.exists():
        if read_regular_file(path) != data:
            raise HarnessError("Immutable native qualification artifact changed")
    else:
        atomic_write(path, data, exclusive=True)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}


def _allowed_tools(config, role):
    agent = config.get("default_agent")
    agents = config.get("agents", {})
    if not isinstance(agent, str) or agent not in agents or agents[agent].get("mode") != "primary":
        raise HarnessError("OpenCode qualification requires a selected primary agent")
    rules = agents[agent].get("permissions")
    if (not isinstance(rules, list) or not rules
            or rules[0] != {"action": "*", "resource": "*", "effect": "deny"}):
        raise HarnessError("Qualified OpenCode leaves require default-deny tool permissions")
    allowed = set()
    servers = config.get("mcp", {}).get("servers", {})
    prefixes = tuple(re.sub(r"[^A-Za-z0-9_]", "_", name) + "_" for name in servers)
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"action", "resource", "effect"}:
            raise HarnessError("Unqualified OpenCode permission shape")
        action = rule["action"]
        if rule["effect"] == "allow":
            if (not isinstance(action, str) or any(c in action for c in "*?[")
                    or rule["resource"] != "*"):
                raise HarnessError("Qualified OpenCode tool grants must name one exact tool")
            if action not in {"read", "glob", "grep", "edit"} and not action.startswith(prefixes):
                raise HarnessError("Unqualified native tool grant outside the declared MCP servers")
            if action not in {"read", "glob", "grep", "edit"} and any(
                    word in action.casefold() for word in ("exec", "command", "shell", "terminal", "spawn", "evaluate", "python", "javascript")):
                raise HarnessError("MCP command/code execution cannot qualify a managed leaf")
            allowed.add(action)
    forbidden = set(DENIED_LEAF) | {"shell", "skill"}
    if role != "worker":
        forbidden.update(DENIED_REVIEWER)
    if allowed & forbidden:
        raise HarnessError("OpenCode role grants a forbidden tool")
    for name, server in servers.items():
        if server.get("codemode") is not False or server.get("pool") is not False:
            raise HarnessError("Managed OpenCode MCP requires direct tools and unshared connections")
    return agent, sorted(allowed)


def _reject_unqualified_plugins(identity, environment, root):
    # Reject pre-existing plugin sources before startup as an additional
    # conservative preflight. The authoritative native policy
    # checks the exact source target before module evaluation, not its export ID.
    for source in identity["config_sources"]:
        text = read_regular_file(source["path"]).decode("utf-8-sig")
        for match in re.finditer(r'("(?:[^"\\]|\\.)*")\s*:', text):
            if json.loads(match[1]) in {"plugin", "plugins"}:
                raise HarnessError("BLOCKED_NOT_QUALIFIED: user/project plugin configuration")
    home = Path(environment.get("HOME", Path.home()))
    directories = {Path(environment.get("XDG_CONFIG_HOME", home / ".config")) / "opencode" / name
                   for name in ("plugin", "plugins")}
    directories.update(home / ".opencode" / name for name in ("plugin", "plugins"))
    current = Path(root)
    while True:
        directories.update(current / ".opencode" / name for name in ("plugin", "plugins"))
        if current.parent == current:
            break
        current = current.parent
    if environment.get("OPENCODE_CONFIG_DIR"):
        directories.update(Path(environment["OPENCODE_CONFIG_DIR"]) / name for name in ("plugin", "plugins"))
    for directory in directories:
        if directory.is_dir() and any(directory.iterdir()):
            raise HarnessError("BLOCKED_NOT_QUALIFIED: discovered user/project plugin source")


def _rows(path):
    if not Path(path).exists():
        return []
    raw = read_regular_file(path, max_bytes=16 * 1024 * 1024)
    if raw and not raw.endswith(b"\n"):
        raise HarnessError("Partial native guard receipt")
    result = []
    for line in raw.splitlines():
        if not line or len(line) > MAX_RESPONSE:
            raise HarnessError("Invalid native guard receipt bounds")
        result.append(_json(line))
    return result


def _wait_probe(path, session_id, deadline, *, after=0):
    while True:
        rows = _rows(path)
        if len(rows) > after:
            value = rows[-1]
            if value.get("session_id") != session_id or value.get("stage") != "qualification_before_dispatch":
                raise HarnessError("Native qualification context subject changed")
            return value, len(rows)
        time.sleep(min(.05, _remaining(deadline)))


def qualify(adapter, *, root, role, evidence_dir, session_id=None, timeout=60):
    """Observe the real native pre-turn snapshot, deliberately aborting inference.

    A separate native CLI restart reopens the same diagnostic session and must
    observe its unpredictable previous marker. The execution plugin then checks
    every real request again, including the final lowered HTTP tool catalog.
    """
    if role not in {"worker", "reviewer", "planner"}:
        raise HarnessError("Unknown native qualification role")
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise HarnessError("Invalid native qualification timeout")
    if adapter.tool_config is None or adapter.tool_root != str(canonical_directory(root)):
        raise HarnessError("Native qualification requires an exact configured worktree")
    deadline = time.monotonic() + timeout
    from .opencode_execution import OBSERVED_VERSIONS
    version = _capture_owned([*adapter.executable, "--version"], cwd=root,
        environment=adapter.environment, deadline=min(deadline, time.monotonic() + 10))
    match = re.fullmatch(rb"opencode v?(\d+\.\d+\.\d+)\s*", version.stdout)
    if version.returncode or match is None or match[1].decode() not in OBSERVED_VERSIONS:
        raise HarnessError("UNSUPPORTED_CAPABILITY: native OpenCode version is not qualified")
    root = canonical_directory(root)
    config = deepcopy(adapter.tool_config)
    agent, allowed = _allowed_tools(config, role)
    environment = dict(adapter.environment if adapter.environment is not None else os.environ)
    environment["PWD"] = str(root)
    initial = input_identity(adapter, root)
    _reject_unqualified_plugins(initial, environment, root)
    evidence_dir = canonical_directory(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if evidence_dir == root or root in evidence_dir.parents:
        raise HarnessError("Native qualification evidence must be outside the worker worktree")
    stable = digest({"root": str(root), "role": role, "config": config,
        "binary": initial["binary"]["sha256"], "guard": hashlib.sha256(GUARD_SOURCE.encode()).hexdigest()})
    guard_dir = evidence_dir.parent / ("guard-" + stable[:32])
    guard_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = [_write_owned(guard_dir / "index.js", GUARD_SOURCE.encode(), raw=True),
             _write_owned(guard_dir / "package.json", GUARD_PACKAGE)]
    folder = evidence_dir / ("qualification-" + secrets.token_hex(12))
    folder.mkdir(mode=0o700)
    runtime_dir = folder / "runtime"
    runtime_dir.mkdir(mode=0o700)
    spec_path, probe_path = folder / "guard-spec.json", folder / "probe.jsonl"
    spec = {"schema_version": 1, "proof_id": secrets.token_hex(24), "root": str(root), "role": role,
        "agent": agent, "allowed_tools": allowed, "runtime_dir": str(runtime_dir),
        "phase": "qualification", "probe_path": str(probe_path), "marker": secrets.token_hex(24),
        "readiness_timeout_ms": min(30000, int(timeout * 1000)),
        "guard_source_path": str(guard_dir / "index.js"), "guard_source_digest": files[0]["sha256"]}
    _write_owned(spec_path, spec)
    config["plugins"] = [str(guard_dir)]
    config["policies"] = [*config.get("policies", []),
        {"action": "plugin.use", "resource": "*", "effect": "deny"},
        {"action": "plugin.use", "resource": str(guard_dir), "effect": "allow"}]
    config_path = folder / "effective-config.json"
    files.append(_write_owned(config_path, config))
    clone = copy.copy(adapter)
    clone.tool_config, clone.tool_config_digest = config, digest(config)
    clone.tool_config_path = str(config_path)
    environment.update(OPENCODE_CONFIG_CONTENT=json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False),
        HARNESS_OPENCODE_GUARD_SPEC=str(spec_path))
    from .opencode_execution import _environment
    clone.environment = _environment(environment)
    permissions = []
    mcp_status = []
    probe_session = None
    first = None
    try:
        with NativeServer(clone, root, deadline=deadline) as server:
            _, location = server.request("GET", "/api/location")
            if not isinstance(location, dict) or location.get("directory") != str(root):
                raise HarnessError("EXACT_WORKTREE_MISMATCH: native location differs from process cwd")
            previous_session = check_session(server, session_id, root) if session_id is not None else None
            agents = server.data("GET", "/api/agent")
            if not isinstance(agents, list) or not any(a.get("id") == agent and a.get("mode") == "primary" for a in agents):
                raise HarnessError("Native controlled primary agent is unavailable")
            plugins = server.data("GET", "/api/plugin")
            if not isinstance(plugins, list) or not any(p.get("id") == GUARD_ID for p in plugins):
                observed = [{"id": p.get("id"), "source": p.get("source"), "state": p.get("state")}
                    for p in plugins if p.get("source", {}).get("type") != "builtin"] if isinstance(plugins, list) else []
                _write_owned(folder / "plugin-status.json", observed)
                raise HarnessError("Native core guard was not loaded; see owned plugin-status.json")
            if any(p.get("source", {}).get("type") != "builtin" and p.get("id") != GUARD_ID
                   and p.get("state", {}).get("status") == "active" for p in plugins):
                raise HarnessError("Unqualified native plugin is active")
            declared_servers = set(config.get("mcp", {}).get("servers", {}))
            if declared_servers:
                while True:
                    observed = server.data("GET", "/api/mcp")
                    if not isinstance(observed, list):
                        raise HarnessError("Invalid native MCP status catalog")
                    mcp_status = [{"name": item.get("name"), "status": item.get("status", {}).get("status")}
                        for item in observed if item.get("name") in declared_servers]
                    if (set(item["name"] for item in mcp_status) == declared_servers
                            and all(item["status"] != "pending" for item in mcp_status)):
                        break
                    time.sleep(min(.05, _remaining(deadline)))
            connected = [item["name"] for item in mcp_status if item["status"] == "connected"]
            prefixes = tuple(re.sub(r"[^A-Za-z0-9_]", "_", name) + "_" for name in connected)
            spec.update(expected_tools=[name for name in allowed
                if name in {"read", "glob", "grep", "edit", "skill"} or name.startswith(prefixes)],
                connected_servers=connected)
            atomic_write(spec_path, (json.dumps(spec, sort_keys=True, separators=(",", ":")) + "\n").encode())
            session_options = {"title": "Harness native qualification", "agent": agent}
            if previous_session and previous_session["model"] is not None:
                session_options["model"] = previous_session["model"]
            info = server.data("POST", "/api/session", session_options)
            probe_session = info["id"]
            checks = set(DENIED_LEAF) | {"shell", "skill"}
            if role != "worker":
                checks.update(DENIED_REVIEWER)
                for name in declared_servers:
                    prefix = re.sub(r"[^A-Za-z0-9_]", "_", name)
                    checks.update((prefix + "_write_file", prefix + "_execute_command"))
            for action in sorted(checks):
                decision = server.data("POST", "/api/session/" + probe_session + "/permission",
                    {"action": action, "resources": ["*"], "agent": agent})
                if decision.get("effect") != "deny":
                    raise HarnessError("Native role does not deny " + action)
                permissions.append({"action": action, "effect": "deny"})
            server.data("POST", "/api/session/" + probe_session + "/prompt",
                {"text": "Harness qualification marker " + spec["marker"], "delivery": "queue"})
            first, count = _wait_probe(probe_path, probe_session, deadline)
            before = check_session(server, probe_session, root)
        # The next private server must wait for live MCP registration before it
        # assembles its snapshot. Connected sessions can precede tool refresh.
        spec.update(expected_tools=[tool["name"] for tool in first["catalog"]],
            connected_servers=[item["name"] for item in mcp_status if item["status"] == "connected"],
            expected_model=first["model"])
        atomic_write(spec_path, (json.dumps(spec, sort_keys=True, separators=(",", ":")) + "\n").encode())
        # A new CLI/private-server process must reopen the existing session. Its
        # prompt deliberately omits the unpredictable first-turn marker.
        result = _capture_owned([*clone.executable, "run", "--standalone", "--format", "json",
            "--agent", agent, "--session", probe_session, "Continue the harness qualification without inference."],
            cwd=root, environment=environment, deadline=deadline)
        second, _ = _wait_probe(probe_path, probe_session, deadline, after=count)
        _write_owned(folder / "resume-observations.json", {"first": first, "second": second,
            "native_mcp_status": mcp_status, "before": before, "cli_exit_code": result.returncode,
            "cli_stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "cli_stderr_sha256": hashlib.sha256(result.stderr).hexdigest()})
        if second.get("history_contains_probe_marker") is not True or second.get("catalog_digest") != first.get("catalog_digest"):
            raise HarnessError("EXACT_RESUME_UNQUALIFIED: CLI restart lost native session history/catalog")
        with NativeServer(clone, root, deadline=deadline) as server:
            after = check_session(server, probe_session, root)
            if before["project_id"] != after["project_id"] or after["message_count"] < before["message_count"]:
                raise HarnessError("EXACT_RESUME_UNQUALIFIED: native session store identity changed")
            missing = "ses_harness_missing_" + secrets.token_hex(12)
            status, _ = server.request("GET", "/api/session/" + missing, allowed=(200, 404))
            if status != 404:
                raise HarnessError("Native missing-session lookup did not fail closed")
            server.request("DELETE", "/api/session/" + probe_session, allowed=(204,))
        catalog = first.get("catalog")
        if (not isinstance(catalog, list) or any(not isinstance(t, dict) or t.get("name") not in allowed for t in catalog)
                or digest(catalog) != first.get("catalog_digest")):
            raise HarnessError("Native qualification catalog integrity mismatch")
        spec.update(phase="execution", expected_catalog_digest=digest(catalog))
        # Diagnostic servers have stopped. Publish the final immutable spec for
        # all subsequent execution processes, then close its proof by hash.
        atomic_write(spec_path, (json.dumps(spec, sort_keys=True, separators=(",", ":")) + "\n").encode())
        files.append({"path": str(spec_path), "sha256": hashlib.sha256(read_regular_file(spec_path)).hexdigest()})
        identity = input_identity(clone, root)
        proof = {"schema_version": 1, "kind": "opencode-native-core-qualification", "proof_id": spec["proof_id"],
            "role": role, "root": str(root), "agent": agent, "identity": identity,
            "guard_source_digest": hashlib.sha256(GUARD_SOURCE.encode()).hexdigest(), "artifacts": files,
            "spec_path": str(spec_path), "runtime_dir": str(runtime_dir), "catalog": catalog,
            "catalog_digest": digest(catalog), "permissions": permissions,
            "mcp_status": mcp_status,
            "exact_resume": {"same_session_after_cli_restart": True, "prior_marker_observed": True,
                "missing_session_refused": True, "provider_inference": False},
            "created_at": datetime.now(timezone.utc).isoformat()}
        ref = _write_owned(folder / "proof.json", proof)
        flags = MappingProxyType({"managed_leaf_qualified": True, "reviewer_isolation_qualified": role != "worker",
            "exact_resume_qualified": True, "effective_tool_catalog": True,
            "connected_mcp_servers": tuple(item["name"] for item in mcp_status if item["status"] == "connected")})
        clone._native_qualification = Qualification(ISSUER, identity, role, time.monotonic(), ref,
            tuple(deepcopy(catalog)), flags)
        return clone
    except BaseException as error:
        _write_owned(folder / "failure.json", {"schema_version": 1,
            "status": "BLOCKED_NOT_QUALIFIED", "failure_type": type(error).__name__,
            "root": str(root), "role": role, "probe_session": probe_session})
        raise


def verify_qualification_evidence(ref, *, root, role, require_execution=False,
                                session_id=None, catalog_artifacts=None, execution_log=None):
    """Read-only closure verification; never issues a dispatch capability."""
    if (not isinstance(ref, dict) or set(ref) != {"path", "sha256"}
            or not isinstance(ref["sha256"], str) or len(ref["sha256"]) != 64):
        raise HarnessError("Malformed native qualification reference")
    path = Path(ref["path"])
    if not path.is_absolute():
        raise HarnessError("Native qualification reference must be absolute")
    raw = read_regular_file(path, max_bytes=MAX_RESPONSE)
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise HarnessError("Native qualification receipt changed")
    proof = _json(raw)
    fields = {"schema_version", "kind", "proof_id", "role", "root", "agent", "identity", "guard_source_digest",
              "artifacts", "spec_path", "runtime_dir", "catalog", "catalog_digest", "permissions", "mcp_status", "exact_resume", "created_at"}
    if (not isinstance(proof, dict) or set(proof) != fields or proof["schema_version"] != 1
            or proof["kind"] != "opencode-native-core-qualification"
            or proof["root"] != str(canonical_directory(root)) or proof["role"] != role
            or proof["guard_source_digest"] != hashlib.sha256(GUARD_SOURCE.encode()).hexdigest()
            or digest(proof["catalog"]) != proof["catalog_digest"]):
        raise HarnessError("Native qualification source/subject changed")
    artifacts = proof["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 4:
        raise HarnessError("Native qualification artifact closure incomplete")
    seen = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"} or item["path"] in seen:
            raise HarnessError("Invalid native qualification artifact reference")
        seen.add(item["path"])
        if hashlib.sha256(read_regular_file(item["path"], max_bytes=MAX_RESPONSE)).hexdigest() != item["sha256"]:
            raise HarnessError("Native qualification artifact changed")
    spec = _json(read_regular_file(proof["spec_path"], max_bytes=MAX_RESPONSE))
    if (proof["spec_path"] not in seen or spec.get("phase") != "execution"
            or any(spec.get(key) != proof[key] for key in ("proof_id", "root", "role", "agent", "runtime_dir"))
            or spec.get("expected_catalog_digest") != proof["catalog_digest"]
            or spec.get("guard_source_digest") != proof["guard_source_digest"]
            or spec.get("guard_source_path") not in seen):
        raise HarnessError("Native guard specification subject changed")
    if not require_execution:
        return proof
    if not session_id or not isinstance(catalog_artifacts, list) or not 1 <= len(catalog_artifacts) <= 64:
        raise HarnessError("Completed native execution lacks pinned catalog artifacts/session")
    contexts, dispatched, responses = set(), set(), set()
    for item in catalog_artifacts:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or Path(item["path"]).parent != Path(proof["runtime_dir"])):
            raise HarnessError("Native runtime catalog is outside the owned proof directory")
        raw = read_regular_file(item["path"], max_bytes=16 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise HarnessError("Pinned native runtime catalog changed")
        rows = _rows(item["path"])
        for row in rows:
            expected = {"schema_version", "proof_id", "root", "agent", "role", "stage", "kind",
                        "request_sequence", "session_id", "model", "catalog", "catalog_digest"}
            if (not isinstance(row, dict) or set(row) != expected or row["schema_version"] != 1
                    or any(row.get(key) != proof[key] for key in ("proof_id", "root", "agent", "role"))
                    or row["session_id"] != session_id or row["stage"] not in {"context", "before_dispatch", "after_response"}
                    or type(row["request_sequence"]) is not int or row["request_sequence"] <= 0
                    or row["kind"] not in {"primary", "compaction", "generate"}
                    or digest(row["catalog"]) != row["catalog_digest"]
                    or any(t.get("name") not in spec["allowed_tools"] for t in row["catalog"])):
                raise HarnessError("Native runtime catalog subject/integrity changed")
            if row["kind"] == "primary":
                if row["catalog_digest"] != proof["catalog_digest"]:
                    raise HarnessError("Native execution catalog differs from qualified snapshot")
                key = (item["path"], row["request_sequence"], digest(row["model"]))
                target = contexts if row["stage"] == "context" else dispatched if row["stage"] == "before_dispatch" else responses
                target.add(key)
    if (not contexts or contexts != dispatched
            or any(item["status"] == "connected" for item in proof["mcp_status"]) and contexts != responses):
        raise HarnessError("Native context lacks matching dispatch/response catalog audit")
    if not isinstance(execution_log, dict) or set(execution_log) != {"path", "sha256"}:
        raise HarnessError("Native runtime catalog requires its pinned normalized event stream")
    raw = read_regular_file(execution_log["path"], max_bytes=16 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != execution_log["sha256"] or not raw.endswith(b"\n"):
        raise HarnessError("Native event stream changed or is partial")
    steps, completed = 0, False
    for line in raw.splitlines():
        if len(line) > MAX_LINE:
            raise HarnessError("Native event stream exceeds event bound")
        value = _json(line)
        if not isinstance(value, dict):
            raise HarnessError("Native normalized event is not an object")
        native = value.get("backend_event", {})
        if isinstance(native, dict) and native.get("type") == "step_start":
            if native.get("sessionID") != session_id:
                raise HarnessError("Native event stream session changed")
            steps += 1
        completed |= value.get("type") == "turn.completed"
    if not completed or steps == 0 or len(dispatched) < steps:
        raise HarnessError("Native model step lacks an execution-bound pre-dispatch catalog")
    return proof
