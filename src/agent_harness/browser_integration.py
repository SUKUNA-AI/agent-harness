"""Optional, locally installed Playwright MCP behind a closed browser policy.

The native client connects to this stdio server, never to the unrestricted
upstream catalog. Each connection owns its upstream process, HTTP proxy, fresh
browser context and private artifacts. No package installation happens here.
Only explicitly declared numeric loopback HTTP test origins are supported.
This is browser request containment, not an OS sandbox for hostile Chromium.
"""
from __future__ import annotations

from copy import deepcopy
import base64
import http.client
import http.server
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import uuid

from .core import HarnessError, digest, file_hash, now, process_identity
from .file_io import read_regular_file
from .optional_tool_jobs import _directory, _environment, _inventory, _output_root
from .supervisor_store import atomic_write
from .tooling_probe import MAX_MESSAGE_BYTES, ProbeFailure, StdioProbe

TOOLS = ("browser_navigate", "browser_snapshot", "browser_click", "browser_type",
    "browser_press_key", "browser_wait_for", "browser_close", "browser_take_screenshot")
_FIELDS = {
    "browser_navigate": {"url"}, "browser_snapshot": {"target", "depth", "boxes"},
    "browser_click": {"element", "target", "button", "doubleClick", "modifiers"},
    "browser_type": {"element", "target", "text", "submit", "slowly"},
    "browser_press_key": {"key"}, "browser_wait_for": {"text", "textGone", "time"},
    "browser_close": set(), "browser_take_screenshot": {"type", "scale"}}
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_BOOTSTRAP = "import sys;sys.path.insert(0,sys.argv.pop(1));from agent_harness.browser_integration import main;main()"


def _origin(url, *, origin_only=False):
    if not isinstance(url, str) or not 0 < len(url) <= 2048 or any(ord(c) < 33 for c in url) or "\\" in url:
        raise HarnessError("Browser URL must be a bounded loopback HTTP URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = 80 if parsed.port is None else parsed.port
    except ValueError as error:
        raise HarnessError("Browser origin requires a numeric loopback address and valid port") from error
    if (parsed.scheme != "http" or not address.is_loopback or parsed.username is not None
            or parsed.password is not None or not 1 <= port <= 65535
            or origin_only and (parsed.path not in ("", "/") or parsed.query or parsed.fragment)):
        raise HarnessError("Browser policy supports exact numeric loopback HTTP origins only")
    host = "[" + address.compressed + "]" if address.version == 6 else address.compressed
    return "http://" + host + (":" + str(port) if port != 80 else "")


def validate_browser_policy(policy):
    """Closed semantic validation also used without the profile JSON schema."""
    if not isinstance(policy, dict) or set(policy) - {"isolated", "artifacts", "allow_test_writes",
            "allowed_origins", "mcp_package", "browser_executable", "browser_cache"}:
        raise HarnessError("Unsupported managed browser policy fields")
    if policy.get("isolated") is not True or policy.get("allow_test_writes", False) is not False:
        raise HarnessError("Managed browser requires an isolated context and readonly source")
    origins = policy.get("allowed_origins")
    if not isinstance(origins, list) or not 1 <= len(origins) <= 16:
        raise HarnessError("Declare 1..16 exact loopback allowed_origins")
    canonical = [_origin(value, origin_only=True) for value in origins]
    if len(set(canonical)) != len(canonical):
        raise HarnessError("Duplicate browser test origins")
    for key in ("mcp_package", "browser_executable", "browser_cache"):
        value = policy.get(key)
        if not isinstance(value, str) or not value or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise HarnessError("Browser dependency requires an explicit absolute " + key)
    artifact = policy.get("artifacts", "browser-artifacts")
    if not isinstance(artifact, str) or not artifact or len(artifact) > 64 or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in artifact):
        raise HarnessError("Browser artifacts must be a contained directory name")
    return {**policy, "allowed_origins": canonical, "artifacts": artifact, "allow_test_writes": False}


def _dependencies(policy):
    from .supervisor_store import _ordinary, _parents
    package, executable = Path(policy["mcp_package"]), Path(policy["browser_executable"])
    for path in (package, executable):
        _parents(path)
        _ordinary(path)
    cache = _directory(policy["browser_cache"])
    if cache not in executable.parents:
        raise HarnessError("Browser executable must belong to the declared isolated browser cache")
    metadata = json.loads(read_regular_file(package.parent / "package.json", max_bytes=65536))
    if package.name != "cli.js" or metadata.get("name") != "@playwright/mcp":
        raise HarnessError("Managed browser requires the installed official @playwright/mcp CLI")
    node = shutil.which("node")
    if node is None:
        raise HarnessError("Node runtime is unavailable")
    npm_root = package.parent.parent.parent
    runtime_roots = [package.parent, npm_root / "playwright", npm_root / "playwright-core", executable.parent]
    inventory, pending, total, visited = [], [(path, str(index)) for index, path in enumerate(runtime_roots)], 0, 0
    while pending:
        directory, prefix = pending.pop()
        _ordinary(directory, directory=True)
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > 5000:
                    raise HarnessError("Browser dependency fingerprint entry bound exceeded")
                path = Path(entry.path)
                key = prefix + "/" + entry.name
                if entry.is_dir(follow_symlinks=False):
                    pending.append((path, key))
                else:
                    info = _ordinary(path)
                    total += info.st_size
                    if total > 512 * 1024 * 1024:
                        raise HarnessError("Browser dependency fingerprint byte bound exceeded")
                    inventory.append({"path": key, "sha256": file_hash(path)})
    return {"mcp_version": metadata["version"], "mcp_cli_sha256": file_hash(package),
        "mcp_metadata_sha256": file_hash(package.parent / "package.json"),
        "browser_sha256": file_hash(executable), "node": str(Path(node).resolve()),
        "node_sha256": file_hash(Path(node).resolve()), "adapter_sha256": file_hash(Path(__file__)),
        "runtime_digest": digest(sorted(inventory, key=lambda item: item["path"])),
        "runtime_files": len(inventory), "runtime_bytes": total,
        "limitation": "host system shared libraries are outside this package/resource fingerprint"}


_FLOW_WORKER = r"""
const fs = require('node:fs');
const job = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const {chromium} = require(job.playwright);
const result = {status:'FAILED',context_closed:false,browser_closed:false,actions:[],external_requests_blocked:0,websockets_blocked:0,console_messages:0};
(async () => {
  let browser, context;
  try {
    browser = await chromium.launch({headless:true,chromiumSandbox:true,executablePath:job.executable,
      proxy:{server:job.proxy,bypass:'<-loopback>'},
      args:['--disable-quic','--force-webrtc-ip-handling-policy=disable_non_proxied_udp']});
    context = await browser.newContext({acceptDownloads:false,serviceWorkers:'block',viewport:{width:1280,height:720}});
    const origins = new Set(job.origins);
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if(url.protocol === 'http:' && origins.has(url.origin)) await route.continue();
      else {result.external_requests_blocked++;await route.abort('blockedbyclient');}
    });
    if(typeof context.routeWebSocket !== 'function') throw new Error('Required WebSocket interception unavailable');
    await context.routeWebSocket('**/*', socket => {result.websockets_blocked++;socket.close({code:1008,reason:'Browser test policy'});});
    await context.addInitScript(() => {
      for(const name of ['RTCPeerConnection','webkitRTCPeerConnection','WebTransport'])
        Object.defineProperty(globalThis,name,{value:undefined,writable:false,configurable:false});
    });
    const page = await context.newPage();
    page.on('download',download=>download.cancel());
    page.on('filechooser',chooser=>chooser.setFiles([]));
    page.on('console',()=>{result.console_messages=Math.min(10000,result.console_messages+1);});
    page.setDefaultTimeout(job.action_timeout_ms);
    await page.goto(job.url,{waitUntil:'domcontentloaded',timeout:job.action_timeout_ms});
    let failed=false;
    for(const [index,action] of job.actions.entries()) {
      const target=page.locator(action.selector);
      if(action.kind==='click') await target.click();
      else if(action.kind==='fill') await target.fill(action.value);
      else if(action.kind==='expect_text') failed=(await target.innerText())!==action.value;
      else if(action.kind==='expect_visible') failed=(await target.isVisible())!==action.visible;
      else throw new Error('Finite browser action denied');
      result.actions.push({index,kind:action.kind,passed:!failed});
      if(failed) break;
    }
    await page.screenshot({path:job.output+'/page.png',fullPage:false});
    result.status=failed?'ASSERTION_FAILED':'PASSED';
  } catch(error) {
    result.status='FAILED';
    result.reason='Finite browser operation failed';
    result.error_type=error.name;
    if(String(error.message).includes("Executable doesn't exist")) {
      result.status='BLOCKED_NOT_QUALIFIED';result.reason='Browser executable unavailable';
    }
  } finally {
    if(context) {await context.close();result.context_closed=true;}
    if(browser) {await browser.close();result.browser_closed=true;}
  }
})().catch(error=>{result.status='FAILED';result.reason='Browser cleanup failed';result.error_type=error.name;}).finally(()=>{
  fs.writeFileSync(job.output+'/browser-result.json',JSON.stringify(result));
  process.exitCode=['PASSED','ASSERTION_FAILED'].includes(result.status)?0:2;
});
"""


def run_managed_browser_job(policy, job):
    """Finite Node browser actions plus an independent operator regression argv.

    This uses the same BrowserJob contract as the optional Python helper. It
    never exposes JavaScript, process creation or file paths as model MCP tools.
    A produced receipt is evidence for an independent gate, not acceptance.
    """
    from .optional_tool_jobs import _browser_validation, _execute, _publish
    from .tooling_provenance import source_snapshot
    policy = validate_browser_policy(policy)
    root, artifact_root, regression = _browser_validation(job)
    if _origin(job.url) not in policy["allowed_origins"]:
        raise HarnessError("Browser job target is outside declared test origins")
    receipt = {"schema_version": 1, "kind": "managed_browser_job", "status": "BLOCKED_NOT_QUALIFIED",
        "expectation": job.expectation, "created_at": now(), "policy_digest": digest(policy),
        "acceptance": "independent_final_gate_required", "context_policy": "isolated_exact_loopback_origins_proxy_no_websockets_downloads_serviceworkers"}
    try:
        inputs = _dependencies(policy)
    except (HarnessError, OSError, ValueError) as error:
        receipt["reason"] = "Browser dependency unavailable: " + type(error).__name__
        return receipt
    before = source_snapshot(root)
    receipt.update(source_snapshot_digest=before["digest"], inputs=inputs)
    folder = artifact_root / ("managed-browser-" + uuid.uuid4().hex)
    folder.mkdir(mode=0o700)
    output = folder / "output"
    output.mkdir(mode=0o700)
    environment = _environment(folder)
    executions = []
    proxy = _OriginProxy(policy["allowed_origins"])
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="hpw-", dir="/tmp" if os.name != "nt" else None) as temporary:
            for name in ("TMPDIR", "TEMP", "TMP", "PWTEST_SOCKETS_DIR"):
                environment[name] = temporary
            environment["PLAYWRIGHT_BROWSERS_PATH"] = policy["browser_cache"]
            request = {"url": job.url, "origins": policy["allowed_origins"], "actions": list(job.actions),
                "playwright": str(Path(policy["mcp_package"]).parent.parent.parent / "playwright"),
                "executable": policy["browser_executable"], "proxy": "http://127.0.0.1:" + str(proxy.server_port),
                "output": str(output), "action_timeout_ms": max(1, min(int(job.timeout * 1000 / (len(job.actions) + 1)), 10000))}
            request_path = folder / "request.json"
            atomic_write(request_path, json.dumps(request).encode(), exclusive=True)
            execution = _execute([inputs["node"], "-e", _FLOW_WORKER, str(request_path)], cwd=folder,
                environment=environment, timeout=job.timeout, output_dir=output, max_output_bytes=job.max_artifact_bytes)
            executions.append(execution)
            receipt["execution"] = execution
            result_path = output / "browser-result.json"
            if execution["status"] == "UNKNOWN_OUTCOME":
                receipt.update(status="UNKNOWN_OUTCOME", reason=execution.get("reason"))
            elif result_path.is_file():
                result = json.loads(read_regular_file(result_path, max_bytes=32768))
                receipt["browser"] = result
                if result.get("status") == "BLOCKED_NOT_QUALIFIED":
                    receipt.update(status="BLOCKED_NOT_QUALIFIED", reason=result.get("reason"))
                elif execution["status"] != "COMPLETED" or not result.get("context_closed") or not result.get("browser_closed"):
                    receipt.update(status="FAILED", reason="Browser execution/context cleanup failed")
                elif result.get("status") in {"PASSED", "ASSERTION_FAILED"}:
                    independent = _execute(regression, cwd=root, environment=environment, timeout=job.timeout)
                    executions.append(independent)
                    receipt["regression"] = independent
                    if independent["status"] == "UNKNOWN_OUTCOME":
                        receipt.update(status="UNKNOWN_OUTCOME", reason="Regression cleanup unconfirmed")
                    elif job.expectation == "passes" and result["status"] == "PASSED" and independent["status"] == "COMPLETED":
                        receipt["status"] = "COMPLETED"
                    elif (job.expectation == "reproduces" and result["status"] == "ASSERTION_FAILED"
                            and type(independent.get("exit_code")) is int and independent["exit_code"] > 0
                            and not independent.get("reason")):
                        receipt["status"] = "REPRODUCED"
                    else:
                        receipt.update(status="FAILED", reason="Browser and independent regression expectations disagree")
                else:
                    receipt.update(status="FAILED", reason="No qualified browser assertion")
            else:
                receipt.update(status="FAILED", reason=execution.get("reason", "No browser result"))
            receipt["artifacts"] = _inventory(output, job.max_artifact_bytes, hashes=True)
            receipt["output_root"] = str(output)
            if source_snapshot(root)["digest"] != before["digest"]:
                raise HarnessError("Readonly browser verification changed source")
    except (HarnessError, OSError, ValueError) as error:
        receipt.update(status="FAILED", reason=str(error)[:512])
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=2)
        receipt["proxy_requests"] = {"allowed": proxy.allowed, "denied": proxy.denied}
    return _publish(folder, receipt, executions)


def browser_server(policy, *, root, private_root):
    """Pure native config projection; startup checks actual dependencies again."""
    policy = validate_browser_policy(policy)
    root = _directory(root)
    # Rendering may precede creation of the private per-worktree cache.
    private = Path(private_root)
    from .tool_profiles import _roots
    _, namespaced = _roots(root, private)
    private = Path(namespaced) / policy["artifacts"]
    return {"id": "playwright-managed", "transport": "stdio", "required": True,
        "command": [sys.executable, "-I", "-c", _BOOTSTRAP, str(Path(__file__).resolve().parents[1]),
            base64.urlsafe_b64encode(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).decode(), str(root), str(private)],
        "cwd": str(root), "environment": {}, "env_vars": [], "allowed_tools": list(TOOLS),
        "forbidden_tools": [], "required_tools": list(TOOLS), "required_results": ["browser_snapshot"], "read_only_tools": [],
        "binding": "worktree", "cache": "private", "startup_timeout": 30, "tool_timeout": 30}


class _OriginProxy(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, origins):
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.origins = set(origins)
        self.allowed = self.denied = 0


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """No tunnels, DNS, upgrades, direct-origin fallback or redirect following."""
    timeout = 10

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        self.server.denied += 1
        self.send_error(403, "Browser proxy forbids tunnels and WebSockets")

    def _forward(self):
        connection = None
        try:
            origin = _origin(self.path)
            if origin not in self.server.origins or self.headers.get("Upgrade") or self.headers.get("Transfer-Encoding"):
                raise HarnessError("Origin or protocol denied")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 1024 * 1024:
                raise HarnessError("Request body limit")
            target = urllib.parse.urlsplit(self.path)
            body = self.rfile.read(length) if length else None
            connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=10)
            headers = {key: value for key, value in self.headers.items() if key.lower() not in
                {"connection", "proxy-connection", "proxy-authorization", "host"}}
            connection.request(self.command, urllib.parse.urlunsplit(("", "", target.path or "/", target.query, "")), body, headers)
            response = connection.getresponse()
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise HarnessError("Response body limit")
            self.server.allowed += 1
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (HarnessError, ValueError, OSError, http.client.HTTPException):
            self.server.denied += 1
            try:
                self.send_error(403, "Browser request denied or unavailable")
            except OSError:
                pass
        finally:
            if connection:
                connection.close()
            self.close_connection = True

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _forward


_INIT_PAGE = r"""
export default async ({ page }) => {
  const context = page.context();
  if (!context.__harnessGuarded) {
    context.__harnessGuarded = true;
    const allowed = new Set(ORIGINS);
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (allowed.has(url.origin) && url.protocol === 'http:') await route.continue();
      else await route.abort('blockedbyclient');
    });
    if (typeof context.routeWebSocket !== 'function') throw new Error('Required WebSocket interception unavailable');
    await context.routeWebSocket('**/*', socket => socket.close({code:1008,reason:'Browser test policy'}));
    await context.addInitScript(() => {
      for (const name of ['RTCPeerConnection','webkitRTCPeerConnection','WebTransport'])
        Object.defineProperty(globalThis, name, {value:undefined,writable:false,configurable:false});
    });
    context.on('page', child => {
      child.on('download', download => download.cancel());
      child.on('filechooser', chooser => chooser.setFiles([]));
    });
  }
  page.on('download', download => download.cancel());
  page.on('filechooser', chooser => chooser.setFiles([]));
};
"""


class ManagedBrowser:
    """Actual native-facing MCP policy, including defense against direct calls."""
    def __init__(self, policy, root, private_root, *, timeout=20):
        self.policy = validate_browser_policy(policy)
        self.root = _directory(root)
        self.private_root = _output_root(private_root, self.root)
        self.folder = self.private_root / ("connection-" + uuid.uuid4().hex)
        self.connection = self.proxy = self.proxy_thread = None
        self.catalog = None
        self.inputs = None
        self.calls = []
        self.short_temp = None
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise HarnessError("Browser request timeout must be in (0, 30]")
        self.timeout = timeout
        self.closed = False

    def __enter__(self):
        if self.closed:
            self.folder = self.private_root / ("connection-" + uuid.uuid4().hex)
            self.connection = self.proxy = self.proxy_thread = self.short_temp = None
            self.calls = []
            self.closed = False
        self.inputs = _dependencies(self.policy)
        self.folder.mkdir(mode=0o700)
        environment = _environment(self.folder)
        environment["PLAYWRIGHT_BROWSERS_PATH"] = self.policy["browser_cache"]
        # Chromium's Unix socket names cannot fit long worktree/run paths.
        # This directory is still unique and exclusively owned per connection.
        self.short_temp = tempfile.TemporaryDirectory(prefix="hpw-", dir="/tmp" if os.name != "nt" else None)
        for name in ("TMPDIR", "TMP", "TEMP", "PWTEST_SOCKETS_DIR"):
            environment[name] = self.short_temp.name
        self.proxy = _OriginProxy(self.policy["allowed_origins"])
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        try:
            init = self.folder / "guard.ts"
            atomic_write(init, _INIT_PAGE.replace("ORIGINS", json.dumps(self.policy["allowed_origins"])).encode(), exclusive=True)
            output = self.folder / "output"
            output.mkdir(mode=0o700)
            config = {"browser": {"browserName": "chromium", "isolated": True,
                "launchOptions": {"headless": True, "executablePath": self.policy["browser_executable"],
                    "chromiumSandbox": True, "proxy": {"server": "http://127.0.0.1:" + str(self.proxy.server_port), "bypass": "<-loopback>"},
                    "args": ["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]},
                "contextOptions": {"acceptDownloads": False, "serviceWorkers": "block", "viewport": {"width": 1280, "height": 720}},
                "initPage": [str(init)]}, "network": {"allowedOrigins": self.policy["allowed_origins"]},
                "outputDir": str(output), "outputMaxSize": MAX_ARTIFACT_BYTES,
                "webmcp": False, "saveSession": False, "ignoreRoots": True, "codegen": "none",
                "timeouts": {"action": 5000, "navigation": 15000}, "imageResponses": "omit"}
            config_path = self.folder / "config.json"
            atomic_write(config_path, json.dumps(config).encode(), exclusive=True)
            spec = {"transport": "stdio", "command": [self.inputs["node"], self.policy["mcp_package"], "--config", str(config_path)],
                "cwd": str(self.folder), "environment": environment,
                "startup_timeout": self.timeout, "tool_timeout": self.timeout, "allowed_tools": list(TOOLS),
                "forbidden_tools": [], "required_tools": list(TOOLS), "read_only_tools": []}
            # StdioProbe otherwise inherits os.environ. The wrapper process has
            # already cleared it; direct library use gets the same isolation.
            self.connection = _PrivateProbe(spec)
            self.connection.__enter__()
            catalog = self.connection.inventory()
            self.catalog = []
            for tool in catalog:
                tool = deepcopy(tool)
                schema = tool["inputSchema"]
                properties = schema.get("properties", {})
                schema["properties"] = {key: value for key, value in properties.items() if key in _FIELDS[tool["name"]]}
                schema["additionalProperties"] = False
                if set(schema.get("required", [])) - set(schema["properties"]):
                    raise HarnessError("Official Playwright tool contract changed")
                self.catalog.append(tool)
            return self
        except BaseException:
            self.close()
            raise

    def call(self, name, arguments):
        if name not in TOOLS or not isinstance(arguments, dict) or set(arguments) - _FIELDS.get(name, set()):
            raise HarnessError("BROWSER_TOOL_DENIED")
        if len(json.dumps(arguments)) > 8192:
            raise HarnessError("Browser arguments exceed bound")
        if self.closed:
            self.__enter__()
        # Structural schemas are closed; validate types and required fields
        # against the actual installed upstream contract as well.
        from .strict_yaml import validate
        schema = next(tool["inputSchema"] for tool in self.catalog if tool["name"] == name)
        validate(arguments, schema)
        from .codex_execution import sanitized
        if sanitized(arguments) != arguments:
            raise HarnessError("Credential-like browser arguments are forbidden")
        if name == "browser_navigate" and _origin(arguments["url"]) not in self.policy["allowed_origins"]:
            raise HarnessError("BROWSER_ORIGIN_DENIED")
        if "time" in arguments and (type(arguments["time"]) not in (int, float) or not 0 < arguments["time"] <= 15):
            raise HarnessError("Browser wait exceeds bound")
        if name == "browser_wait_for" and not (arguments.get("text") or arguments.get("textGone")):
            raise HarnessError("Browser waits require a concrete assertion")
        if "depth" in arguments and (type(arguments["depth"]) is not int or not 1 <= arguments["depth"] <= 32):
            raise HarnessError("Snapshot depth exceeds bound")
        forwarded = dict(arguments)
        if name == "browser_take_screenshot":
            # User-controlled filenames never reach upstream. Constant viewport
            # screenshot cannot write outside the generated private output root.
            forwarded["filename"] = str(self.folder / "output" / ("page-" + uuid.uuid4().hex + "." + arguments.get("type", "png")))
        result = self.connection.request("tools/call", {"name": name, "arguments": forwarded}, timeout=self.timeout)
        # Recent official servers return a file link for snapshots. Deliver the
        # bounded accessibility data through MCP so the native leaf does not
        # need filesystem permission outside its source worktree.
        for block in result.get("content", []):
            if block.get("type") != "text":
                continue
            for link in re.findall(r"\[Snapshot\]\(([^)]+)\)", block.get("text", ""))[:1]:
                candidate = Path(link)
                candidate = candidate if candidate.is_absolute() else self.folder / candidate
                if candidate.parent != self.folder / "output" or not re.fullmatch(r"[A-Za-z0-9_.-]+\.yml", candidate.name):
                    raise HarnessError("Upstream snapshot path escaped private output")
                text = read_regular_file(candidate, max_bytes=128 * 1024).decode("utf-8")
                block["text"] += "\n### Accessibility snapshot\n```yaml\n" + text + "\n```"
        _inventory(self.folder / "output", MAX_ARTIFACT_BYTES)
        self.calls.append({"name": name, "is_error": bool(result.get("isError")), "response_digest": digest(sanitized(result))})
        if name == "browser_close" and not result.get("isError"):
            self.close()
        else:
            self._write_receipt()
        return result

    def close(self):
        if self.closed:
            return
        if self.connection:
            try:
                if self.connection.catalog is not None and (not self.calls or self.calls[-1]["name"] != "browser_close"):
                    self.connection.request("tools/call", {"name": "browser_close", "arguments": {}}, timeout=5)
            except (HarnessError, OSError):
                pass
            finally:
                self.connection.close()
        if self.proxy:
            self.proxy.shutdown()
            self.proxy.server_close()
        if self.proxy_thread:
            self.proxy_thread.join(timeout=2)
        if self.short_temp:
            self.short_temp.cleanup()
        self.closed = True
        self._write_receipt()

    def _write_receipt(self):
        if self.folder.exists():
            receipt = {"kind": "managed_browser_connection", "at": now(), "worktree": str(self.root),
                "policy_digest": digest(self.policy), "inputs": self.inputs, "calls": self.calls,
                "process_identity": self.connection.identity if self.connection else None,
                "process_tree_stopped": bool(self.connection and self.connection.stopped),
                "proxy_requests": {"allowed": self.proxy.allowed, "denied": self.proxy.denied} if self.proxy else {},
                "artifacts": _inventory(self.folder / "output", MAX_ARTIFACT_BYTES, hashes=True) if (self.folder / "output").exists() else [],
                "acceptance": "independent_final_gate_required"}
            atomic_write(self.folder / "receipt.json", json.dumps(receipt, sort_keys=True).encode())

    def __exit__(self, *_):
        self.close()


class _PrivateProbe(StdioProbe):
    def __enter__(self):
        # The adapter's child environment is private even when used as a Python
        # library inside a credential-bearing supervisor process.
        from .runner import _start_contained
        self.executable = self.server["command"][0]
        try:
            self.process, self.job = _start_contained(self.server["command"], self.server["cwd"],
                env=self.server["environment"], stdin=subprocess.PIPE)
            self.identity = process_identity(self.process.pid)
            if not self.identity:
                raise ProbeFailure("PROCESS_IDENTITY_UNKNOWN")
            for stream, stderr in ((self.process.stdout, False), (self.process.stderr, True)):
                thread = threading.Thread(target=self._reader, args=(stream, stderr), daemon=True)
                thread.start()
                self.readers.append(thread)
            self.events.append({"event": "process.started", "identity": self.identity, "cwd": self.server["cwd"], "at": now()})
            return self
        except BaseException:
            self.close()
            raise


def qualify_browser(policy, *, root, private_root, timeout=30):
    """CORE invokes a real browser launch; a supplied receipt cannot self-attest."""
    receipt = {"kind": "browser_qualification", "status": "BLOCKED_NOT_QUALIFIED",
        "created_at": now(), "acceptance": "not_performed", "client_catalog_verified": False}
    connection = None
    try:
        policy = validate_browser_policy(policy)
        receipt["policy_digest"] = digest(policy)
        server = browser_server(policy, root=root, private_root=private_root)
        private = Path(server["command"][-1])
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = ManagedBrowser(policy, root, private, timeout=timeout)
        with connection:
            snapshot = connection.call("browser_snapshot", {})
            if not isinstance(snapshot, dict) or snapshot.get("isError") or not snapshot.get("content"):
                raise HarnessError("Actual browser snapshot failed; browser dependency is not qualified")
            closed = connection.call("browser_close", {})
            if closed.get("isError"):
                raise HarnessError("Browser context cleanup failed")
            receipt.update(status="QUALIFIED", inputs=connection.inputs,
                catalog_digest=digest(connection.catalog), policy_digest=digest(policy),
                browser_launch_observed=True, browser_close_observed=True)
        if not connection.connection.stopped:
            raise HarnessError("Browser process cleanup unconfirmed")
        receipt["process_identity"] = connection.connection.identity
        receipt["process_tree_stopped"] = True
        path = connection.folder / "receipt.json"
        receipt["connection_receipt"] = {"path": str(path), "sha256": file_hash(path)}
    except (HarnessError, OSError, ValueError, StopIteration) as error:
        receipt.update(status="BLOCKED_NOT_QUALIFIED", reason=str(error)[:512])
    return receipt


def _tool_call_params(params):
    """Accept only bounded native request metadata, with no tool authority.

    MCP progress tokens and OpenCode's session correlation identifier are
    transport metadata. Neither is forwarded to the browser or used as a path,
    session selector, permission, invocation argument or persisted value.
    """
    if not isinstance(params, dict) or set(params) - {"name", "arguments", "_meta"}:
        raise HarnessError("Invalid browser tool request")
    meta = params.get("_meta", {})
    if not isinstance(meta, dict) or set(meta) - {"progressToken", "ai.opencode/sessionID"}:
        raise HarnessError("Unsupported browser request metadata")
    if "progressToken" in meta:
        token = meta["progressToken"]
        if not ((type(token) is str and len(token) <= 256)
                or type(token) is int or type(token) is float and math.isfinite(token)):
            raise HarnessError("Invalid browser progress token")
    if "ai.opencode/sessionID" in meta:
        session = meta["ai.opencode/sessionID"]
        if type(session) is not str or not 0 < len(session) <= 128 or any(ord(char) < 32 for char in session):
            raise HarnessError("Invalid native browser request correlation")
    return {"name": params.get("name"), "arguments": params.get("arguments", {})}


def main():
    """Closed stdio MCP facade. Unsupported methods/tools never reach upstream."""
    policy, root, private = json.loads(base64.b64decode(sys.argv[1], altchars=b"-_", validate=True)), Path(sys.argv[2]), Path(sys.argv[3])
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never inherit credentials, browser auth state, Node hooks or proxy env.
    safe = _environment(private)
    os.environ.clear()
    os.environ.update(safe)
    with ManagedBrowser(policy, root, private) as browser:
        total = 0
        for raw in iter(lambda: sys.stdin.buffer.readline(16385), b""):
            total += len(raw)
            if len(raw) > 16384 or total > 8 * 1024 * 1024 or not raw.endswith(b"\n"):
                raise HarnessError("Browser MCP protocol bound exceeded")
            from .codex_execution import _duplicate_free
            message = json.loads(raw, object_pairs_hook=_duplicate_free)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise HarnessError("Invalid browser MCP request")
            ident, method = message.get("id"), message.get("method")
            if ident is None:
                continue
            response = {"jsonrpc": "2.0", "id": ident}
            try:
                if method == "initialize":
                    result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "agent-harness-playwright-managed", "version": "1"}}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": browser.catalog}
                elif method == "tools/call":
                    params = _tool_call_params(message.get("params", {}))
                    try:
                        result = browser.call(params.get("name"), params.get("arguments", {}))
                    except (HarnessError, ValueError, TypeError) as error:
                        result = {"isError": True, "content": [{"type": "text", "text": str(error)[:256]}]}
                else:
                    raise HarnessError("Browser MCP method denied")
                response["result"] = result
            except HarnessError:
                response["error"] = {"code": -32601, "message": "Browser MCP method denied"}
            data = json.dumps(response, separators=(",", ":"), allow_nan=False).encode()
            if len(data) > MAX_MESSAGE_BYTES:
                data = json.dumps({"jsonrpc": "2.0", "id": ident, "error": {"code": -32000, "message": "Browser response bound exceeded"}}).encode()
            sys.stdout.buffer.write(data + b"\n")
            sys.stdout.buffer.flush()
