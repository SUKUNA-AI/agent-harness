"""Small optional document/browser jobs; no installation or model invocation.

These utilities publish ordinary artifacts/receipts below an explicitly owned
private output root. They do not admit managed workers or qualify client tools.
Configured converter commands are trusted operator input, like graph builders.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

from .codex_execution import _stop_owned_tree, sanitized
from .core import HarnessError, digest, file_hash, now, process_identity
from .file_io import read_regular_file
from .runner import _start_contained
from .supervisor_store import _mkdir, _ordinary, _parents, atomic_write
from .tool_profiles import _check_credential_argv

MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_FILES = 128


@dataclass(frozen=True)
class DocumentJob:
    input_root: Path
    input_path: Path
    output_root: Path
    command: tuple[str, ...]
    tool_version: str
    version_argv: tuple[str, ...] = ()
    mode: str = "local-only"
    expected_output: str = "converted.md"
    max_input_bytes: int = 8 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    timeout: float = 120


@dataclass(frozen=True)
class BrowserJob:
    root: Path
    url: str
    artifact_root: Path
    actions: tuple[dict, ...]
    regression_argv: tuple[str, ...]
    expectation: str = "passes"
    timeout: float = 30
    max_artifact_bytes: int = 8 * 1024 * 1024


def _positive(value, name, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise HarnessError(name + " is outside the bounded job policy")


def _directory(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or os.name != "nt" and str(path).startswith("//"):
        raise HarnessError("Job directories must be unambiguous absolute paths")
    _parents(path)
    _ordinary(path, directory=True)
    return path.resolve()


def _output_root(value, source):
    path = _directory(value)
    if path == source or source in path.parents:
        raise HarnessError("Private job outputs must live outside source/input root")
    return path


def _relative(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise HarnessError("Job output name must be a portable contained relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or ":" in value or len(value) > 256:
        raise HarnessError("Job output name escaped its owned directory")
    return path


def _argv(value):
    if not isinstance(value, (list, tuple)) or not value or len(value) > 64 or any(
            not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 16384 for arg in value):
        raise HarnessError("Job commands must be bounded explicit argv, never a shell string")
    if sanitized(list(value)) != list(value):
        raise HarnessError("Credential-like material is forbidden in job argv")
    _check_credential_argv(value)
    executable = shutil.which(value[0])
    if executable is None:
        raise HarnessError("Optional tool executable is unavailable")
    return [str(Path(executable).resolve()), *value[1:]]


def _environment(folder):
    # Do not forward worker credentials, PYTHONPATH, plugins or package hooks.
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "LD_LIBRARY_PATH"}}
    for name, child in (("HOME", "home"), ("USERPROFILE", "home"), ("TMPDIR", "tmp"),
            ("TEMP", "tmp"), ("TMP", "tmp"), ("XDG_CACHE_HOME", "cache"),
            ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state")):
        directory = folder / child
        directory.mkdir(exist_ok=True)
        env[name] = str(directory)
    env.update(HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1", DO_NOT_TRACK="1", NO_COLOR="1")
    return env


def _inventory(folder, maximum, *, hashes=False):
    """Bound both traversal and bytes; reject links/special files before reading."""
    _parents(folder)
    _ordinary(folder, directory=True)
    pending, visited, total, result = [folder], 0, 0, []
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_FILES:
                    raise HarnessError("Optional tool artifact count limit exceeded")
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    _ordinary(path, directory=True)
                    pending.append(path)
                else:
                    info = _ordinary(path)
                    total += info.st_size
                    if total > maximum:
                        raise HarnessError("Optional tool artifact byte limit exceeded")
                    result.append({"path": path.relative_to(folder).as_posix(), "bytes": info.st_size,
                        **({"sha256": file_hash(path)} if hashes else {})})
    return sorted(result, key=lambda item: item["path"])


def _execute(argv, *, cwd, environment, timeout, output_dir=None, max_output_bytes=0, capture=None, capture_stderr=None, max_log_bytes=MAX_LOG_BYTES):
    """Bound stdout/stderr and output growth; stop descendants on every exit."""
    started = time.monotonic()
    process, job = _start_contained(argv, str(cwd), env=environment)
    identity = process_identity(process.pid)
    result = {"status": "UNKNOWN_OUTCOME", "exit_code": None, "process_identity": identity,
        "process_tree_stopped": False, "argv_digest": digest(argv), "cwd": str(cwd),
        "events": [{"type": "process.started", "identity": identity, "at": now()}]}
    streams = {}
    inbox = queue.Queue()
    readers = []
    def drain(stream, name):
        count, checksum = 0, hashlib.sha256()
        try:
            while True:
                block = stream.read(65536)
                if not block:
                    break
                count += len(block)
                if count > max_log_bytes:
                    inbox.put("OUTPUT_LOG_LIMIT")
                    return
                checksum.update(block)
                if name == "stdout" and capture is not None:
                    capture.append(block)
                if name == "stderr" and capture_stderr is not None:
                    capture_stderr.append(block)
        except (OSError, ValueError):
            inbox.put("OUTPUT_CAPTURE_FAILED")
        finally:
            streams[name] = {"bytes": count, "sha256": checksum.hexdigest()}
            stream.close()
    try:
        if not identity:
            raise HarnessError("Optional tool process identity unavailable")
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            reader = threading.Thread(target=drain, args=(stream, name), daemon=True)
            reader.start()
            readers.append(reader)
        while process.poll() is None:
            try:
                failure = inbox.get(timeout=0.05)
            except queue.Empty:
                failure = None
            if failure:
                result.update(status="FAILED", reason=failure)
                break
            if time.monotonic() - started >= timeout:
                result.update(status="FAILED", reason="TOOL_TIMEOUT")
                break
            if output_dir is not None:
                try:
                    _inventory(output_dir, max_output_bytes)
                except (OSError, HarnessError) as error:
                    result.update(status="FAILED", reason="ARTIFACT_LIMIT_OR_UNSAFE_PATH")
                    break
        else:
            result.update(status="COMPLETED" if process.returncode == 0 else "FAILED", exit_code=process.returncode)
    except (OSError, HarnessError) as error:
        result.update(status="UNKNOWN_OUTCOME", reason=type(error).__name__)
    finally:
        try:
            _stop_owned_tree(process, job, identity)
            result["process_tree_stopped"] = True
        except (OSError, HarnessError, subprocess.TimeoutExpired):
            result.update(status="UNKNOWN_OUTCOME", reason="PROCESS_TREE_CLEANUP_UNCONFIRMED")
        job.close()
        for reader in readers:
            reader.join(timeout=2)
        if any(reader.is_alive() for reader in readers):
            result.update(status="UNKNOWN_OUTCOME", reason="OUTPUT_CAPTURE_CLEANUP_UNCONFIRMED")
        if not inbox.empty() and result["status"] == "COMPLETED":
            result.update(status="FAILED", reason=inbox.get_nowait())
        result["streams"] = streams
        result["exit_code"] = process.returncode
        result["events"].append({"type": "process.stopped", "confirmed": result["process_tree_stopped"], "at": now()})
    return result


def _capture_small(argv, *, cwd, environment, timeout):
    """Bounded probe bytes in memory; content is never copied into receipts."""
    chunks, errors = [], []
    execution = _execute(argv, cwd=cwd, environment=environment, timeout=timeout,
        capture=chunks, capture_stderr=errors, max_log_bytes=65536)
    diagnostic = b"".join(errors).lower()
    if b"operation not permitted" in diagnostic or b"permission denied" in diagnostic:
        execution["diagnostic_code"] = "OS_PERMISSION_DENIED"
    return (b"".join(chunks) if execution["status"] == "COMPLETED" else None), execution


_NET_PROBE = "import json,os,socket; print(json.dumps({'namespace':os.readlink('/proc/self/ns/net'),'interfaces':[name for _,name in socket.if_nameindex()]}))"


def _bwrap_command(binary):
    return [binary, "--unshare-user", "--unshare-net", "--unshare-pid", "--die-with-parent",
        "--new-session", "--cap-drop", "ALL", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev"]


def probe_network_isolation(*, timeout=5):
    """Qualify a real fresh net namespace without any external network call."""
    _positive(timeout, "network probe timeout", 15)
    blocked = {"status": "BLOCKED_NOT_QUALIFIED", "scheme": "linux_bwrap_net_namespace",
        "reason": "network namespace support unavailable", "external_network_calls": False}
    binary = shutil.which("bwrap") if sys.platform.startswith("linux") else None
    if binary is None or not Path("/proc/self/ns/net").exists():
        return blocked
    host_namespace = os.readlink("/proc/self/ns/net")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="harness-net-probe-") as folder:
        folder = Path(folder)
        environment = _environment(folder)
        raw, execution = _capture_small([*_bwrap_command(binary), "--", sys.executable, "-I", "-c", _NET_PROBE],
            cwd=folder, environment=environment, timeout=timeout)
        blocked["probe"] = execution
        if raw is None:
            blocked["reason"] = "bwrap network namespace probe refused or failed" + (" (OS_PERMISSION_DENIED)" if execution.get("diagnostic_code") == "OS_PERMISSION_DENIED" else "")
            return blocked
        try:
            observed = json.loads(raw)
            if (not isinstance(observed, dict) or observed.get("namespace") == host_namespace
                    or not isinstance(observed.get("namespace"), str)
                    or not isinstance(observed.get("interfaces"), list)
                    or set(observed["interfaces"]) - {"lo"}):
                raise ValueError("namespace not isolated")
        except (ValueError, TypeError):
            blocked["reason"] = "different loopback-only network namespace was not observed"
            return blocked
        return {"status": "QUALIFIED", "scheme": "linux_bwrap_net_namespace", "binary": str(Path(binary).resolve()),
            "binary_sha256": file_hash(Path(binary).resolve()), "host_namespace": host_namespace,
            "observed_namespace": observed["namespace"], "interfaces": observed["interfaces"],
            "probe": execution, "external_network_calls": False}


def _publish(folder, receipt, executions):
    events = [event for execution in executions for event in execution.get("events", [])]
    raw = b"".join(json.dumps(sanitized(event), sort_keys=True, separators=(",", ":")).encode() + b"\n" for event in events)
    path = folder / "execution-events.jsonl"
    atomic_write(path, raw, exclusive=True)
    receipt["events_receipt"] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    receipt["receipt_path"] = str(folder / "receipt.json")
    atomic_write(folder / "receipt.json", json.dumps(receipt, sort_keys=True, indent=2).encode() + b"\n", exclusive=True)
    return receipt


def run_document_job(job: DocumentJob):
    """Run an explicitly configured converter; Docling flags are never guessed."""
    if not isinstance(job, DocumentJob) or job.mode not in {"local-only", "remote"}:
        raise HarnessError("Document job requires explicit local-only or remote mode")
    _positive(job.timeout, "document timeout", 3600)
    for value, label in ((job.max_input_bytes, "input byte limit"), (job.max_output_bytes, "output byte limit")):
        if type(value) is not int:
            raise HarnessError(label + " must be an integer")
        _positive(value, label, 64 * 1024 * 1024)
    root = _directory(job.input_root)
    output_root = _output_root(job.output_root, root)
    source = Path(job.input_path)
    if not source.is_absolute() or ".." in source.parts or root not in source.parents:
        raise HarnessError("Document input must be declared below its input root")
    _parents(source)
    _ordinary(source)
    raw_input = read_regular_file(source, max_bytes=job.max_input_bytes)
    input_digest = hashlib.sha256(raw_input).hexdigest()
    destination = _relative(job.expected_output)
    argv = _argv(job.command)
    version_argv = _argv(job.version_argv) if job.version_argv else [argv[0], "--version"]
    if (not isinstance(job.tool_version, str) or not job.tool_version or len(job.tool_version) > 4096
            or sanitized(job.tool_version) != job.tool_version):
        raise HarnessError("Expected converter version must be a bounded non-secret string")
    if not any("{input}" in arg for arg in argv) or not any("{output}" in arg or "{output_dir}" in arg for arg in argv):
        raise HarnessError("Converter argv requires explicit {input} and {output}/{output_dir}")
    for arg in argv:
        remainder = arg.replace("{input}", "").replace("{output}", "").replace("{output_dir}", "")
        if "{" in remainder or "}" in remainder:
            raise HarnessError("Unsupported converter placeholder")
    receipt = {"schema_version": 1, "kind": "document_job", "status": "BLOCKED_NOT_QUALIFIED",
        "requested_conversion_mode": job.mode, "actual_conversion_mode": "not_run",
        "input": {"path": str(source), "sha256": input_digest, "bytes": len(raw_input)},
        "acceptance": "independent_final_gate_required", "created_at": now()}
    network = probe_network_isolation() if job.mode == "local-only" else {
        "status": "NOT_REQUESTED", "scheme": "explicit_remote_mode", "external_network_calls": "permitted_by_operator"}
    receipt["network_isolation"] = network
    if job.mode == "local-only" and network["status"] != "QUALIFIED":
        receipt["reason"] = "Local-only conversion requires an actual isolated network namespace"
        return receipt
    folder = output_root / ("document-" + uuid.uuid4().hex)
    folder.mkdir(mode=0o700)
    environment = _environment(folder)
    output = folder / "output"
    output.mkdir()
    staged_input = folder / ("input" + source.suffix[:32])
    atomic_write(staged_input, raw_input, exclusive=True)
    expected = output / destination
    expected.parent.mkdir(parents=True, exist_ok=True)
    executions = []
    try:
        if job.mode == "local-only":
            if file_hash(network["binary"]) != network["binary_sha256"]:
                raise HarnessError("Network isolation executable drifted after qualification")
            version_argv = [*_bwrap_command(network["binary"]), "--bind", str(folder), str(folder),
                "--chdir", str(folder), "--", *version_argv]
        version_raw, version_execution = _capture_small(version_argv, cwd=folder, environment=environment, timeout=min(job.timeout, 15))
        executions.append(version_execution)
        if version_raw is None or version_raw.decode("utf-8", errors="replace").strip() != job.tool_version:
            raise HarnessError("Converter version differs from the qualified operator command")
        command = [arg.replace("{input}", str(staged_input)).replace("{output_dir}", str(output)).replace("{output}", str(expected)) for arg in argv]
        receipt["tool"] = {"executable": argv[0], "binary_sha256": file_hash(argv[0]), "version": job.tool_version,
            "command_digest": digest(list(job.command)), "wrapper_inputs": {str(Path(arg).resolve()): file_hash(Path(arg).resolve())
                for arg in [*argv, *version_argv] if Path(arg).is_absolute() and Path(arg).is_file()}}
        if job.mode == "local-only":
            # Recheck inside the ACTUAL conversion namespace before exec. The
            # earlier qualification receipt alone never authorizes a host run.
            guard = "import json,os,socket,sys; assert os.readlink('/proc/self/ns/net')!=sys.argv[1]; assert set(n for _,n in socket.if_nameindex())<={'lo'}; os.execv(sys.argv[2],sys.argv[2:])"
            command = [*_bwrap_command(network["binary"]), "--bind", str(folder), str(folder),
                "--chdir", str(folder), "--", sys.executable, "-I", "-c", guard, network["host_namespace"], *command]
            if file_hash(network["binary"]) != network["binary_sha256"]:
                raise HarnessError("Network isolation executable drifted after qualification")
        execution = _execute(command, cwd=folder, environment=environment, timeout=job.timeout,
            output_dir=output, max_output_bytes=job.max_output_bytes)
        executions.append(execution)
        receipt["actual_conversion_mode"] = job.mode
        receipt["execution"] = execution
        receipt["status"] = execution["status"]
        if execution["status"] == "COMPLETED":
            artifacts = _inventory(output, job.max_output_bytes, hashes=True)
            if destination.as_posix() not in {item["path"] for item in artifacts}:
                raise HarnessError("Converter produced no expected result artifact")
            if file_hash(staged_input) != input_digest or file_hash(source) != input_digest:
                raise HarnessError("Document input changed during conversion")
            receipt["artifacts"] = artifacts
            receipt["output_root"] = str(output)
    except (OSError, ValueError, HarnessError) as error:
        receipt.update(status="FAILED", reason=str(error) if isinstance(error, HarnessError) else type(error).__name__)
    return _publish(folder, receipt, executions)


# The child owns its browser/context through finally; parent containment also
# kills every descendant after timeout, crash and a prematurely exited leader.
_PLAYWRIGHT_WORKER = r'''
import json,pathlib,sys,urllib.parse
job=json.loads(pathlib.Path(sys.argv[1]).read_text())
sys.path.insert(0,job['package_root'])
output=pathlib.Path(job['output'])
result={'status':'FAILED','context_closed':False,'browser_closed':False,'actions':[],'external_requests_blocked':0,'console_messages':0}
browser=context=None
try:
 from playwright.sync_api import sync_playwright
 with sync_playwright() as runtime:
  try:
   browser=runtime.chromium.launch(headless=True,chromium_sandbox=True,args=['--disable-background-networking','--disable-component-update','--disable-sync','--metrics-recording-only','--no-first-run'])
   context=browser.new_context(viewport={'width':1280,'height':720},accept_downloads=False,service_workers='block')
   expected=urllib.parse.urlsplit(job['url'])
   origin=(expected.scheme,expected.hostname,expected.port or (443 if expected.scheme=='https' else 80))
   def route(request):
    target=urllib.parse.urlsplit(request.request.url)
    actual=(target.scheme,target.hostname,target.port or (443 if target.scheme=='https' else 80))
    if actual==origin:
     request.continue_()
    else:
     result['external_requests_blocked']+=1
     request.abort()
   context.route('**/*',route)
   page=context.new_page()
   page.set_default_timeout(job['action_timeout_ms'])
   page.on('console',lambda event: result.update(console_messages=min(result['console_messages']+1,10000)))
   page.goto(job['url'],wait_until='domcontentloaded')
   assertion_failed=False
   for index,action in enumerate(job['actions']):
    kind=action['kind']; locator=page.locator(action['selector'])
    if kind=='click':
     locator.click()
    elif kind=='fill':
     locator.fill(action['value'])
    elif kind=='expect_text':
     if locator.inner_text()!=action['value']:
      assertion_failed=True
    elif kind=='expect_visible':
     if locator.is_visible()!=action['visible']:
      assertion_failed=True
    result['actions'].append({'index':index,'kind':kind,'passed':not assertion_failed})
    if assertion_failed:
     break
   page.screenshot(path=str(output/'page.png'),full_page=False)
   result['status']='ASSERTION_FAILED' if assertion_failed else 'PASSED'
  except Exception as error:
   message=str(error)
   if 'Executable doesn' in message or 'playwright install' in message:
    result.update(status='BLOCKED_NOT_QUALIFIED',reason='Playwright browser executable unavailable')
   else:
    result.update(status='FAILED',reason='Browser operation failed',error_type=type(error).__name__)
  finally:
   if context is not None:
    context.close(); result['context_closed']=True
   if browser is not None:
    browser.close(); result['browser_closed']=True
except ImportError:
 result.update(status='BLOCKED_NOT_QUALIFIED',reason='Playwright Python package unavailable')
finally:
 (output/'browser-result.json').write_text(json.dumps(result,sort_keys=True))
raise SystemExit(0 if result['status'] in ('PASSED','ASSERTION_FAILED') else 2)
'''


def _playwright_package():
    try:
        spec = importlib.util.find_spec("playwright")
        if spec is None or spec.origin is None:
            return None
        path = Path(spec.origin).resolve()
        return {"package_root": str(path.parent.parent), "origin": str(path), "origin_sha256": file_hash(path),
            "version": importlib.metadata.version("playwright")}
    except (ImportError, OSError, ValueError, importlib.metadata.PackageNotFoundError):
        return None


def _browser_validation(job):
    if not isinstance(job, BrowserJob) or job.expectation not in {"passes", "reproduces"}:
        raise HarnessError("Browser job requires passes or reproduces expectation")
    _positive(job.timeout, "browser timeout", 300)
    if type(job.max_artifact_bytes) is not int:
        raise HarnessError("Browser artifact byte limit must be an integer")
    _positive(job.max_artifact_bytes, "browser artifact byte limit", 32 * 1024 * 1024)
    root = _directory(job.root)
    artifacts = _output_root(job.artifact_root, root)
    if not isinstance(job.url, str) or len(job.url) > 2048 or sanitized(job.url) != job.url:
        raise HarnessError("Browser URL must be a bounded non-secret local test URL")
    parsed = urllib.parse.urlsplit(job.url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.fragment):
        raise HarnessError("Browser flow is restricted to an explicit localhost test origin")
    try:
        parsed.port
    except ValueError as error:
        raise HarnessError("Browser URL has an invalid port") from error
    if not isinstance(job.actions, (list, tuple)) or not 1 <= len(job.actions) <= 32:
        raise HarnessError("Browser flow requires 1..32 bounded actions")
    has_assertion = False
    for action in job.actions:
        if not isinstance(action, dict) or action.get("kind") not in {"click", "fill", "expect_text", "expect_visible"}:
            raise HarnessError("Unsupported browser action")
        kind = action["kind"]
        fields = {"kind", "selector", "visible" if kind == "expect_visible" else "value"} if kind != "click" else {"kind", "selector"}
        if set(action) != fields or not isinstance(action["selector"], str) or not 0 < len(action["selector"]) <= 512:
            raise HarnessError("Browser action has invalid fields or selector")
        if kind == "expect_visible":
            if type(action["visible"]) is not bool:
                raise HarnessError("Visibility expectation must be boolean")
        elif kind != "click" and (not isinstance(action["value"], str) or len(action["value"]) > 4096):
            raise HarnessError("Browser action value exceeds its string bound")
        if sanitized(action) != action:
            raise HarnessError("Credential-like browser action values cannot be persisted")
        has_assertion |= kind.startswith("expect_")
    if not has_assertion:
        raise HarnessError("Browser flow requires an explicit reproducible assertion")
    regression = _argv(job.regression_argv)
    return root, artifacts, regression


def run_browser_job(job: BrowserJob):
    """Run an isolated Playwright context AND an independent regression command.

    A reproduced assertion plus a failing regression test is evidence of the
    original defect. A fixed flow needs passing assertions and regression. No
    screenshot or MCP receipt changes supervisor acceptance semantics.
    """
    root, artifact_root, regression = _browser_validation(job)
    receipt = {"schema_version": 1, "kind": "browser_job", "status": "BLOCKED_NOT_QUALIFIED",
        "expectation": job.expectation, "origin": urllib.parse.urlsplit(job.url).netloc,
        "context_policy": "new_context_same_origin_no_downloads_no_service_workers",
        "acceptance": "independent_final_gate_required", "created_at": now()}
    package = _playwright_package()
    if package is None:
        receipt["reason"] = "Playwright Python package unavailable; no browser/model process started"
        return receipt
    from .tooling_provenance import source_snapshot
    before = source_snapshot(root)
    receipt["source_snapshot_digest"] = before["digest"]
    folder = artifact_root / ("browser-" + uuid.uuid4().hex)
    folder.mkdir(mode=0o700)
    environment = _environment(folder)
    output = folder / "output"
    output.mkdir()
    request = {"url": job.url, "actions": list(job.actions), "output": str(output),
        "action_timeout_ms": max(1, min(int(job.timeout * 1000 / (len(job.actions) + 1)), 10000)),
        "package_root": package["package_root"]}
    request_path = folder / "browser-job.json"
    atomic_write(request_path, json.dumps(request, sort_keys=True).encode(), exclusive=True)
    executions = []
    try:
        execution = _execute([sys.executable, "-I", "-c", _PLAYWRIGHT_WORKER, str(request_path)],
            cwd=folder, environment=environment, timeout=job.timeout,
            output_dir=output, max_output_bytes=job.max_artifact_bytes)
        executions.append(execution)
        receipt["execution"] = execution
        receipt["playwright"] = package
        browser_result_path = output / "browser-result.json"
        if execution["status"] == "UNKNOWN_OUTCOME":
            receipt.update(status="UNKNOWN_OUTCOME", reason=execution.get("reason"))
        elif browser_result_path.is_file():
            result = json.loads(read_regular_file(browser_result_path, max_bytes=32768))
            if not isinstance(result, dict):
                raise HarnessError("Browser result receipt is not an object")
            receipt["browser"] = result
            if result.get("status") == "BLOCKED_NOT_QUALIFIED":
                receipt.update(status="BLOCKED_NOT_QUALIFIED", reason=result.get("reason"))
            elif execution["status"] != "COMPLETED" or not result.get("context_closed") or not result.get("browser_closed"):
                receipt.update(status="FAILED", reason="Browser execution or context cleanup failed")
            elif result.get("status") in {"PASSED", "ASSERTION_FAILED"}:
                regression_execution = _execute(regression, cwd=root, environment=environment, timeout=job.timeout)
                executions.append(regression_execution)
                receipt["regression"] = regression_execution
                if regression_execution["status"] == "UNKNOWN_OUTCOME":
                    receipt.update(status="UNKNOWN_OUTCOME", reason="Regression process cleanup unconfirmed")
                elif (job.expectation == "passes" and result["status"] == "PASSED"
                        and regression_execution["status"] == "COMPLETED"):
                    receipt["status"] = "COMPLETED"
                elif (job.expectation == "reproduces" and result["status"] == "ASSERTION_FAILED"
                        and type(regression_execution.get("exit_code")) is int
                        and regression_execution["exit_code"] > 0 and not regression_execution.get("reason")):
                    receipt["status"] = "REPRODUCED"
                else:
                    receipt.update(status="FAILED", reason="Browser expectation and independent regression disagree")
            else:
                receipt.update(status="FAILED", reason="Browser did not produce a qualified assertion result")
        else:
            receipt.update(status="FAILED", reason=execution.get("reason", "Browser produced no result receipt"))
        receipt["artifacts"] = _inventory(output, job.max_artifact_bytes, hashes=True)
        receipt["output_root"] = str(output)
        after = source_snapshot(root)
        if after["digest"] != before["digest"]:
            raise HarnessError("Browser/regression changed source; readonly verification cannot accept edits")
    except (OSError, ValueError, HarnessError) as error:
        receipt.update(status="FAILED", reason=str(error) if isinstance(error, HarnessError) else type(error).__name__)
    return _publish(folder, receipt, executions)
