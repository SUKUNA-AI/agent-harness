"""Real argv-based checks and evidence-bound acceptance gates."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import ctypes
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid

from .validation import json_equal
from .core import AcceptanceDeadlineExhausted, HarnessError, Run, _tool_version_probes, atomic_json, canonical, changed_paths, digest, file_hash, identity_alive, lock, now, process_identity, read_json, remaining_budget_seconds, scope_errors, snapshot


class WindowsJob:
    """A private kill-on-close job for this check's process tree."""
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(x, ctypes.c_uint64) for x in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = self.kernel.CreateJobObjectW(None, None)
        if not handle:
            raise HarnessError("Could not create process-tree job")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not self.kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
            self.kernel.CloseHandle(handle)
            raise HarnessError("Could not contain process tree in a private job; refusing unmanaged execution")
        self.handle = handle

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def resume_suspended_process(self, process):
        """Use documented Toolhelp/OpenThread/ResumeThread after job assignment.

        CREATE_SUSPENDED ensures no user-mode child code runs before containment.
        subprocess closes the primary thread handle, so retrieve that handle by
        its process ID using the documented thread snapshot API.
        """
        if os.name != "nt":
            return
        from ctypes import wintypes

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        self.kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        self.kernel.OpenThread.restype = wintypes.HANDLE
        self.kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        self.kernel.ResumeThread.restype = wintypes.DWORD
        snapshot_handle = self.kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot_handle == ctypes.c_void_p(-1).value:
            raise HarnessError("Could not inspect suspended primary thread; refusing to resume")
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            found = []
            has_entry = self.kernel.Thread32First(snapshot_handle, ctypes.byref(entry))
            while has_entry:
                if entry.th32OwnerProcessID == process.pid:
                    found.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                has_entry = self.kernel.Thread32Next(snapshot_handle, ctypes.byref(entry))
            if len(found) != 1:
                raise HarnessError("Expected exactly one suspended primary thread before execution")
            thread = self.kernel.OpenThread(0x0002, False, found[0])  # THREAD_SUSPEND_RESUME
            if not thread:
                raise HarnessError("Could not open suspended primary thread")
            try:
                previous_count = self.kernel.ResumeThread(thread)
                if previous_count != 1:
                    raise HarnessError("Unexpected primary thread suspend count; containment cannot be established")
            finally:
                self.kernel.CloseHandle(thread)
        finally:
            self.kernel.CloseHandle(snapshot_handle)


def redact(value):
    # Never dump environment values. Only inspect candidate secret values for redaction.
    for key, secret in os.environ.items():
        if len(secret) >= 6 and re.search(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|AUTHORIZATION", key, re.I):
            value = value.replace(secret, "<redacted>")
    value = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s\"']+", r"\1<redacted>", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "<redacted>", value)
    return value


def _drain(source, path, errors):
    try:
        with Path(path).open("x", encoding="utf-8", newline="\n") as output:
            for line in iter(source.readline, b""):
                output.write(redact(line.decode("utf-8", errors="replace")))
                output.flush()
    except Exception as error:
        errors.append(redact(f"Log capture failed: {type(error).__name__}: {error}"))
    finally:
        source.close()


def _terminate(process, job):
    if os.name == "nt":
        job.close()
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise HarnessError("Owned process did not terminate; inspect retained process identity")


def _start_contained(argv, cwd=None, *, env=None):
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004 if os.name == "nt" else 0
    process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, shell=False, creationflags=flags, start_new_session=(os.name != "nt"), env=env)
    job = None
    try:
        job = WindowsJob(process)
        job.resume_suspended_process(process)
        return process, job
    except BaseException:
        if job:
            job.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        raise


def _contained_capture(argv, *, cwd=None, timeout=10, env=None):
    """Use the same tree containment for version probes and scope adapters."""
    process, job = _start_contained(argv, cwd, **({"env": env} if env is not None else {}))
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate(process, job)
            process.communicate(timeout=5)
            raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        job.close()
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _check(contract, check_id):
    for check in contract["checks"]:
        if check["id"] == check_id:
            return check
    raise HarnessError(f"Unknown check ID: {check_id}")


def _result_path(run, state, check_id):
    return run.attempt_dir(state) / "checks" / check_id / "result.json"


def _proof(path, base):
    path = Path(path).resolve()
    if not path.is_file():
        raise HarnessError(f"Evidence artifact missing: {path}")
    return {"path": path.relative_to(base).as_posix(), "sha256": file_hash(path), "size": path.stat().st_size}


def validate_artifacts(run, result):
    errors = []
    for item in result.get("artifacts", []):
        path = (run.path / item["path"]).resolve()
        if run.path not in path.parents:
            errors.append("artifact escaped run store")
        elif not path.is_file():
            errors.append(f"artifact missing: {item['path']}")
        elif file_hash(path) != item["sha256"] or path.stat().st_size != item["size"]:
            errors.append(f"artifact integrity failed: {item['path']}")
    if not result.get("artifacts"):
        errors.append("no evidence artifacts")
    return errors


def _unknown_artifacts(run, state, check_id, result):
    folder = _result_path(run, state, check_id).parent
    declared = {(run.path / proof["path"]).resolve() for proof in result.get("artifacts", [])}
    declared.add(_result_path(run, state, check_id).resolve())
    return [f"unknown artifact: {path.relative_to(run.path).as_posix()}" for path in folder.rglob("*") if path.is_file() and path.resolve() not in declared]


def _report_stamp(path):
    """Observe production of a report, including an identical-byte rewrite.

    Access time is deliberately excluded: reading an old report is not a new
    test run. If the filesystem cannot distinguish writes, use a new output path
    for each check rather than treating an unchanged file as fresh evidence.
    """
    try:
        observed = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(observed.st_mode):
        return None
    return {key: getattr(observed, "st_" + key) for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")}


def _validate_evidence(check, stdout_path, stderr_path, report_paths, *, timeout, on_start=None, cancel_file=None):
    """Bound parsing separately from command execution, with no pipe buffering.

    The standalone worker can only parse data. Literal-only output assertions
    avoid process startup, but use the same input/output caps and semantics.
    """
    from . import validation

    started = time.monotonic()
    deadline = started + timeout
    mode = "literal" if not check.get("reports") and validation.literal_assertions(check.get("assertions", [])) else "worker"
    info = {"mode": mode, "timeout": timeout}

    def failure(status, message):
        return {"schema_version": 1, "status": status, "errors": [message], "reports": []}

    def cancelled():
        return bool(cancel_file and Path(cancel_file).exists())

    response = failure("failed", "evidence validation did not complete")
    try:
        request = {"schema_version": 1, "assertions": check.get("assertions", []),
                   "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
                   "reports": [{"definition": definition, "path": str(path)} for definition, path in report_paths]}
        payload = validation.encode_request(request)
        if cancelled():
            response = failure("cancelled", "evidence validation cancelled")
        elif time.monotonic() >= deadline:
            response = failure("validation_timeout", "evidence validation timed out")
        elif mode == "literal":
            response = json.loads(validation.encode_response(validation.validate(request, literals=True, timeout=deadline - time.monotonic())))
            if cancelled():
                response = failure("cancelled", "evidence validation cancelled")
        else:
            # Real file handles avoid PIPE deadlocks and communicate() retaining
            # an unbounded worker response in the runner's memory.
            with tempfile.TemporaryFile(dir=stdout_path.parent) as input_file, tempfile.TemporaryFile(dir=stdout_path.parent) as output_file, tempfile.TemporaryFile(dir=stdout_path.parent) as error_file:
                input_file.write(payload)
                input_file.seek(0)
                process = None
                try:
                    process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).with_name("validation.py").resolve())],
                                               stdin=input_file, stdout=output_file, stderr=error_file, shell=False)
                    identity = process_identity(process.pid)
                    info["process"] = {"identity": identity}
                    if on_start is not None:
                        on_start(identity)
                    while True:
                        if cancelled():
                            response = failure("cancelled", "evidence validation cancelled")
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            response = failure("validation_timeout", "evidence validation timed out")
                            break
                        try:
                            process.wait(timeout=min(0.05, remaining) if cancel_file else remaining)
                            break
                        except subprocess.TimeoutExpired:
                            if not cancel_file:
                                response = failure("validation_timeout", "evidence validation timed out")
                                break
                    if process.poll() is not None and response["status"] not in {"cancelled", "validation_timeout"}:
                        output_file.seek(0)
                        raw = output_file.read(validation.MAX_RESPONSE_BYTES + 1)
                        if len(raw) > validation.MAX_RESPONSE_BYTES:
                            response = failure("failed", "validation worker response exceeds byte limit")
                        elif process.returncode:
                            response = failure("failed", f"validation worker exited with code {process.returncode}")
                        else:
                            response = json.loads(raw)
                finally:
                    if process is not None:
                        if process.poll() is None:
                            process.kill()
                        process.wait()  # Reap even when interrupted during callback/wait/decoding.
        if not isinstance(response, dict) or response.get("schema_version") != 1 or response.get("status") not in {"passed", "failed", "cancelled", "validation_timeout"} or not isinstance(response.get("errors"), list) or any(not isinstance(error, str) for error in response["errors"]) or not isinstance(response.get("reports"), list):
            response = failure("failed", "invalid validation worker response")
        if response["status"] == "passed" and response["errors"]:
            response["status"] = "failed"
    except KeyboardInterrupt:
        response = failure("cancelled", "evidence validation cancelled")
    except Exception as error:
        detail = str(error) if isinstance(error, validation.InputLimit) else type(error).__name__
        response = failure("failed", "evidence validation failed: " + detail)
    info.update(status=response["status"], elapsed_seconds=round(time.monotonic() - started, 6))
    if "input_bytes" in response:
        info["input_bytes"] = response["input_bytes"]
    return info, response["errors"], response["reports"]


def _executable(command, cwd):
    # POSIX exec searches relative paths after chdir. Windows Popen uses the
    # parent's search context even when the child has a different cwd.
    resolution_cwd = Path.cwd() if os.name == "nt" else Path(cwd)
    if os.path.dirname(command):
        executable = shutil.which(str(resolution_cwd / command))
    elif os.name == "nt":
        executable = shutil.which(command)
    else:
        search = os.pathsep.join(str(resolution_cwd / entry) for entry in os.get_exec_path())
        executable = shutil.which(command, path=search)
    # Keep the invoked symlink path: resolving it can change virtualenv semantics.
    return os.path.abspath(executable) if executable else None


def _executable_hash(executable, cache):
    if not executable:
        return None
    stamp = _report_stamp(Path(executable))
    if stamp is None:
        return None
    # Hash a physical file once per phase while its identity/metadata is stable.
    # Symlink spelling stays in the evidence's resolved executable field.
    key = (os.path.normcase(str(Path(executable).resolve())), tuple(stamp.items()))
    if key not in cache:
        cache[key] = file_hash(Path(executable).resolve())
    return cache[key]


def _versions(check, *, cwd=None, probe_cache=None, fingerprint_cache=None):
    probes = _tool_version_probes(check)
    cwd = str(Path(check.get("cwd") or cwd or Path.cwd()).resolve())
    result = {}
    fingerprints = {} if fingerprint_cache is None else fingerprint_cache
    executable = _executable(check["argv"][0], cwd)
    result["executable"] = {"requested": check["argv"][0], "resolved": executable, "version": "unknown", "sha256": _executable_hash(executable, fingerprints)}
    cache = {} if probe_cache is None else probe_cache
    for name, argv in probes.items():
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise HarnessError("tool_versions entries must be argv arrays")
        probe_executable = _executable(argv[0], cwd)
        probe_hash = _executable_hash(probe_executable, fingerprints)
        # Environment values never enter persisted evidence. This digest only
        # separates inherited environments in this short-lived phase cache.
        environment = digest(json.dumps(dict(os.environ), ensure_ascii=True, sort_keys=True))
        key = (cwd, environment, tuple(argv), probe_executable, probe_hash)
        if key not in cache:
            try:
                proc = _contained_capture(argv, cwd=cwd, timeout=10)
                cache[key] = {"exit_code": proc.returncode, "version": redact((proc.stdout + proc.stderr).decode("utf-8", errors="replace"))[:2000].strip()}
            except (OSError, subprocess.TimeoutExpired) as error:
                cache[key] = {"status": type(error).__name__}
        result[name] = {"argv": argv, **cache[key]}
    return result


def _toolchains(contract):
    """One freeze phase; no probe result survives this call for reuse later."""
    probes, fingerprints = {}, {}
    return {check["id"]: _versions(check, cwd=contract.get("cwd"), probe_cache=probes, fingerprint_cache=fingerprints) for check in contract["checks"] if check["kind"] == "command"}


def run_check(run: Run, check_id, *, cancel_file=None):
    state = run.state()
    contract = run.contract(state)
    check = _check(contract, check_id)
    if check["kind"] != "command":
        raise HarnessError("Use evidence for non-command checks")
    remaining = remaining_budget_seconds(contract)
    if remaining is not None and remaining <= 0:
        raise AcceptanceDeadlineExhausted("Run deadline exhausted: check not_run, no process started")
    with ExitStack() as stack:
        stack.enter_context(run.workspace_lock(contract))
        state = run.state()
        if state["contract_digest"] != digest(contract):
            raise HarnessError("Attempt changed before command execution")
        folder = _result_path(run, state, check_id).parent
        folder.mkdir(parents=True, exist_ok=True)
        for resource in sorted(set(check.get("resources", []))):
            stack.enter_context(lock(run.resource_path("resource:" + resource, contract), purpose="check resource: " + resource))
        check_lease = run.path / ".check-locks" / (digest({"attempt": state["current_attempt"], "check": check_id}) + ".lock")
        stack.enter_context(lock(check_lease, purpose="command check " + check_id))
        if _result_path(run, state, check_id).exists():
            raise HarnessError("Check already has immutable evidence; use a new attempt to repeat it")
        before = run.assert_current(state, contract)
        frozen_toolchains = read_json(run.attempt_dir(state) / "toolchains.json")
        if digest(frozen_toolchains) != state.get("toolchain_digest"):
            raise HarnessError("Frozen toolchain manifest integrity failed")
        actual_tools = _versions(check, cwd=contract.get("cwd"))
        if frozen_toolchains.get(check_id) != actual_tools:
            raise HarnessError("Toolchain changed after candidate freeze; create a new attempt")
        run_dir = folder / uuid.uuid4().hex
        run_dir.mkdir()
        stdout_path, stderr_path = run_dir / "stdout.log", run_dir / "stderr.log"
        record = {"schema_version": 1, "run_id": contract["run_id"], "task_id": contract["task_id"], "lane_id": contract.get("lane_id"), "attempt_id": contract["attempt_id"], "check_id": check_id, "kind": "command", "contract_digest": state["contract_digest"], "candidate_digest": before["digest"], "input_before": before["digest"], "argv": check["argv"], "cwd": check.get("cwd", contract.get("cwd")), "timeout": check["timeout"], "expected_exit": check.get("expected_exit", 0), "started_at": now(), "status": "not_run", "tools": actual_tools, "toolchain_digest": state["toolchain_digest"], "artifacts": [], "reports": []}
        remaining = remaining_budget_seconds(contract)
        record["effective_timeout"] = min(check["timeout"], max(0, remaining)) if remaining is not None else check["timeout"]
        process = None
        job = None
        readers = []
        reader_errors = []
        report_before = {}
        captured_paths = [stdout_path, stderr_path]
        started = time.monotonic()
        try:
            if record["effective_timeout"] <= 0:
                record["status"] = "budget_exhausted"
                stdout_path.write_text("", encoding="utf-8")
                stderr_path.write_text("Run deadline exhausted before execution\n", encoding="utf-8")
            elif cancel_file and Path(cancel_file).exists():
                record["status"] = "cancelled"
                stdout_path.write_text("", encoding="utf-8")
                stderr_path.write_text("Cancelled before execution\n", encoding="utf-8")
            else:
                for index, report in enumerate(check.get("reports", [])):
                    source = Path(report["path"])
                    if not source.is_absolute():
                        source = Path(record["cwd"]) / source
                    report_before[index] = _report_stamp(source)
                with run.edit() as current:
                    if current["contract_digest"] != state["contract_digest"] or current["current_attempt"] != state["current_attempt"]:
                        raise HarnessError("Attempt changed before process start")
                    process, job = _start_contained(check["argv"], record["cwd"])
                    identity = process_identity(process.pid)
                    record["process"] = {"identity": identity, "tree_control": "windows_suspended_job_kill_on_close" if os.name == "nt" else "private_process_group"}
                    current.setdefault("processes", {})[check_id] = {"identity": identity, "status": "running", "started_at": now(), "attempt_id": contract["attempt_id"], "argv_digest": digest(check["argv"])}
                    current["status"] = "verifying"
                for source, path in ((process.stdout, stdout_path), (process.stderr, stderr_path)):
                    thread = threading.Thread(target=_drain, args=(source, path, reader_errors), daemon=True)
                    thread.start()
                    readers.append(thread)
                while process.poll() is None:
                    if cancel_file and Path(cancel_file).exists():
                        record["status"] = "cancelled"
                        _terminate(process, job)
                        break
                    if time.monotonic() - started >= record["effective_timeout"]:
                        record["status"] = "timeout" if record["effective_timeout"] == check["timeout"] else "budget_exhausted"
                        _terminate(process, job)
                        break
                    time.sleep(0.025)
                if record["status"] == "not_run":
                    record["status"] = "passed" if process.returncode == record["expected_exit"] else "failed"
                record["exit_code"] = process.returncode
        except FileNotFoundError as error:
            record.update({"status": "missing_dependency", "error": redact(str(error)), "exit_code": None})
        except KeyboardInterrupt:
            record["status"] = "cancelled"
            if process and job:
                _terminate(process, job)
        except BaseException as error:
            record.update({"status": "failed", "error": redact(f"{type(error).__name__}: {error}"), "exit_code": process.poll() if process else None})
            if process and process.poll() is None:
                if job:
                    _terminate(process, job)
                else:
                    process.kill()
                    process.wait()
        finally:
            if job:
                job.close()  # Also terminate descendants left behind by an exited parent.
            if process and os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for reader in readers:
                reader.join(timeout=10)
            if any(reader.is_alive() for reader in readers):
                record.update({"status": "failed", "error": "Log reader did not terminate; evidence incomplete"})
            if reader_errors:
                record.update({"status": "failed", "error": "; ".join(reader_errors)})
            record["finished_at"] = now()
            record["elapsed_seconds"] = round(time.monotonic() - started, 6)
            record["validation"] = {"status": "not_run", "mode": "none"}
            if process:
                record.setdefault("exit_code", process.poll())
            try:
                for path in (stdout_path, stderr_path):
                    if not path.exists():
                        path.write_text("", encoding="utf-8")
                after = snapshot(contract)
                record["input_after"] = after["digest"]
                record["tools_after"] = _versions(check, cwd=contract.get("cwd"))
                if record["tools_after"] != record["tools"]:
                    record["status"] = "toolchain_changed"
                if before["digest"] != after["digest"]:
                    record["status"] = "inputs_changed"
                    record["changed_paths"] = changed_paths(before, after)
                report_paths = []
                record["report_observations"] = []
                for index, report in enumerate(check.get("reports", [])):
                    source = Path(report["path"])
                    if not source.is_absolute():
                        source = Path(record["cwd"]) / source
                    try:
                        observed = _report_stamp(source)
                        fresh = process is not None and observed is not None and observed != report_before.get(index)
                        record["report_observations"].append({"path": str(source), "before": report_before.get(index), "after": observed, "fresh": fresh})
                        if observed is None:
                            record.setdefault("assertion_errors", []).append(f"required report missing: {report['path']}")
                            continue
                        if not fresh:
                            record.setdefault("assertion_errors", []).append(f"report not produced by this check: {report['path']}")
                        target = run_dir / (f"report-{index}" + source.suffix)
                        # Retain even a stale or malformed report for diagnosis.
                        captured_paths.append(target)
                        shutil.copyfile(source, target)
                        report_paths.append((report, target))
                        if _report_stamp(source) != observed:
                            record.setdefault("assertion_errors", []).append(f"report changed during capture: {report['path']}")
                    except (OSError, ValueError, TypeError) as error:
                        record.setdefault("assertion_errors", []).append(redact(f"report capture failed: {report['path']}: {type(error).__name__}: {error}"))
                record.setdefault("assertion_errors", [])
                if not check.get("assertions") and not check.get("reports"):
                    record["validation"] = {"status": "not_required", "mode": "none"}
                elif record["status"] != "cancelled":
                    def validation_started(identity):
                        with run.edit() as current:
                            if current["current_attempt"] != contract["attempt_id"]:
                                raise HarnessError("Attempt changed before evidence validation")
                            current.setdefault("processes", {})[check_id] = {"identity": identity, "status": "running", "phase": "validation", "started_at": now(), "attempt_id": contract["attempt_id"]}

                    timeout = min(5.0, check["timeout"])
                    remaining = remaining_budget_seconds(contract)
                    if remaining is not None:
                        timeout = min(timeout, max(0, remaining))
                    validation_info, errors, reports = _validate_evidence(check, stdout_path, stderr_path, report_paths, timeout=timeout, on_start=validation_started, cancel_file=cancel_file)
                    record["validation"] = validation_info
                    record.setdefault("assertion_errors", []).extend(errors)
                    record["reports"] = reports
                    if validation_info["status"] in {"cancelled", "validation_timeout"} and record["status"] == "passed":
                        record["status"] = validation_info["status"]
                    elif validation_info["status"] != "passed" and record["status"] == "passed":
                        record["status"] = "failed"
                if record.get("assertion_errors") and record["validation"]["status"] == "passed":
                    record["validation"]["status"] = "failed"
                if record.get("assertion_errors") and record["status"] == "passed":
                    record["status"] = "failed"
            except Exception as error:
                record.update({"status": "failed", "error": redact(f"Evidence finalization failed: {type(error).__name__}: {error}")})
            # Partial evidence is useful on a capture failure. A missing artifact
            # still fails the check; it must not prevent its result/ledger closure.
            for path in captured_paths:
                try:
                    record["artifacts"].append(_proof(path, run.path))
                except (OSError, ValueError, HarnessError) as error:
                    record.update({"status": "failed", "error": redact(f"Evidence artifact capture failed: {type(error).__name__}: {error}")})
            record["digest"] = digest(record)
            try:
                atomic_json(_result_path(run, state, check_id), record, exclusive=True)
            except BaseException:
                record["status"] = "failed"
                raise
            finally:
                with run.edit() as current:
                    if current["current_attempt"] == contract["attempt_id"]:
                        if check_id in current.get("processes", {}):
                            current["processes"][check_id]["status"] = record["status"]
                        if record["status"] != "passed":
                            current["status"] = "failed" if record["status"] != "cancelled" else "cancelled"
        return record


def record_evidence(run: Run, check_id, supplied):
    state = run.state()
    contract = run.contract(state)
    check = _check(contract, check_id)
    if check["kind"] == "command":
        raise HarnessError("Command evidence may only come from check runner")
    with run.workspace_lock(contract):
        candidate = run.assert_current(state, contract)
        toolchains_path = run.attempt_dir(state) / "toolchains.json"
        toolchains = read_json(toolchains_path)
        if digest(toolchains) != state.get("toolchain_digest"):
            raise HarnessError("Frozen toolchain manifest integrity failed")
        if supplied.get("kind") != check["kind"]:
            raise HarnessError("Evidence kind mismatch")
        if supplied.get("status") not in {"satisfied", "failed", "not_run"}:
            raise HarnessError("Typed evidence status must be satisfied, failed or not_run")
        methods = {"source": {"source_review"}, "content": {"agent_assessment", "human_assessment"}, "visual": {"agent_visual_review", "human_visual_review"}, "runtime": {"observed_runtime"}}
        if supplied.get("method") not in methods[check["kind"]] or not supplied.get("assessor") or not supplied.get("assessment"):
            raise HarnessError("Typed evidence needs the actual assessment method, assessor and explanation")
        criteria = supplied.get("criteria", {})
        if set(criteria) != set(check["criteria"]) or any(not isinstance(v, str) or not v.strip() for v in criteria.values()):
            raise HarnessError("Evidence must explain every frozen criterion exactly")
        if check["kind"] == "source":
            sources = supplied.get("sources", [])
            if not sources:
                raise HarnessError("Source evidence needs source/claim/support records")
            for source in sources:
                if urllib.parse.urlparse(source.get("url", "")).scheme not in {"http", "https"} or not all(source.get(k) for k in ("retrieved_at", "claim", "support", "artifact")):
                    raise HarnessError("Every source needs URL, retrieval time, claim, supporting passage and captured artifact")
        paths = supplied.get("artifact_paths", [])
        if not paths:
            raise HarnessError("Typed evidence requires captured artifacts; a self-report alone is insufficient")
        if check["kind"] == "visual" and not any(Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".pdf"} for p in paths):
            raise HarnessError("Visual evidence needs the viewed image or document")
        folder = _result_path(run, state, check_id).parent
        folder.mkdir(parents=True, exist_ok=True)
        if _result_path(run, state, check_id).exists():
            raise HarnessError("Evidence immutable; new assessment requires a new attempt")
        record = {k: v for k, v in supplied.items() if k != "artifact_paths"}
        record.update({"schema_version": 1, "run_id": contract["run_id"], "task_id": contract["task_id"], "lane_id": contract.get("lane_id"), "attempt_id": contract["attempt_id"], "check_id": check_id, "kind": check["kind"], "contract_digest": state["contract_digest"], "candidate_digest": candidate["digest"], "recorded_at": now(), "artifacts": []})
        copied = {}
        for index, source_value in enumerate(paths):
            source = Path(source_value).resolve()
            if not source.is_file():
                raise HarnessError(f"Evidence artifact missing: {source}")
            target = folder / "artifacts" / (str(index) + "-" + source.name)
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            proof = _proof(target, run.path)
            record["artifacts"].append(proof)
            copied[str(source)] = proof["path"]
        if check["kind"] == "source":
            for source in record["sources"]:
                source_path = str(Path(source["artifact"]).resolve())
                if source_path not in copied:
                    raise HarnessError("Source artifact must be among copied evidence artifacts")
                source["artifact"] = copied[source_path]
        record["digest"] = digest(record)
        atomic_json(_result_path(run, state, check_id), record, exclusive=True)
        return record


def _shared_record_id(value):
    if type(value) not in (str, int) or value == "":
        raise ValueError("record IDs must be nonempty strings or integers (not booleans)")
    return value


def _shared_record_map(rows, id_key, label):
    if not isinstance(rows, list):
        raise ValueError(f"{label} records must be an array")
    result = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or id_key not in row:
            raise ValueError(f"{label} record {index} must be an object with ID field {id_key}")
        record_id = _shared_record_id(row[id_key])
        if record_id in result:
            raise ValueError(f"{label} contains duplicate record ID: {record_id}")
        result[record_id] = row
    return result


def _shared_scope_errors(run, state, contract):
    errors = []
    receipts = []
    for definition in contract.get("scope", {}).get("shared_files", []):
        baseline = run.path / "origin-baseline-files" / definition["path"]
        current = Path(contract["cwd"]) / definition["path"]
        if not baseline.is_file() or not current.is_file():
            errors.append(f"shared file removed or baseline missing: {definition['path']}")
            continue
        original = read_json(run.path / "origin-baseline.json")["files"].get(definition["path"], {})
        if original.get("sha256") != file_hash(baseline):
            errors.append(f"shared baseline content integrity failed: {definition['path']}")
            continue
        if definition["format"] == "json-records":
            try:
                full_a, full_b = read_json(baseline), read_json(current)
                left, right = full_a, full_b
                key = definition.get("records_key")
                if key is not None:
                    if not isinstance(key, str) or not key or any(not isinstance(value, dict) or key not in value for value in (left, right)):
                        raise ValueError("records_key must identify an array in both object roots")
                    left, right = left[key], right[key]
                id_key = definition.get("id_key", "id")
                if not isinstance(id_key, str) or not id_key:
                    raise ValueError("id_key must be a nonempty string")
                allowed = definition["allowed_records"]
                if not isinstance(allowed, list) or not allowed:
                    raise ValueError("allowed_records must be a nonempty array of IDs")
                allowed = {_shared_record_id(value) for value in allowed}
                a, b = _shared_record_map(left, id_key, "baseline"), _shared_record_map(right, id_key, "candidate")
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(f"invalid shared records {definition['path']}: {error}")
                continue
            if set(a) != set(b):
                errors.append(f"shared record IDs changed: {definition['path']}")
            for record_id in set(a) | set(b):
                if not json_equal(a.get(record_id), b.get(record_id)) and record_id not in allowed:
                    errors.append(f"foreign shared record changed: {record_id}")
            # Preserve all non-record sections too.
            if key:
                full_a.pop(key, None)
                full_b.pop(key, None)
                if not json_equal(full_a, full_b):
                    errors.append(f"shared metadata changed: {definition['path']}")
        else:
            for source, expected in definition["validator_files"].items():
                if not Path(source).is_file() or file_hash(source) != expected:
                    errors.append(f"scope adapter implementation changed: {source}")
            if errors:
                continue
            substitutions = {"{baseline_file}": str(baseline), "{candidate_file}": str(current), "{allowed_records_json}": json.dumps(definition["allowed_records"])}
            argv = [substitutions.get(arg, arg) for arg in definition["validator_argv"]]
            try:
                result = _contained_capture(argv, cwd=contract["cwd"], timeout=definition.get("timeout", 30))
                receipt = {"path": definition["path"], "argv": argv, "exit_code": result.returncode, "stdout": redact(result.stdout.decode("utf-8", errors="replace")), "stderr": redact(result.stderr.decode("utf-8", errors="replace"))}
                receipts.append(receipt)
                if result.returncode != 0:
                    errors.append(f"shared-record scope adapter rejected {definition['path']}")
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(f"shared-record scope adapter unavailable: {type(error).__name__}")
    return errors, receipts



def _check_evidence_errors(run, state, contract, candidate, toolchains, *, details=None):
    """Shared read-only validation; never starts a command or changes run state."""
    errors, receipts = [], []
    for check in contract["checks"]:
        first_error = len(errors)
        path = _result_path(run, state, check["id"])
        if not path.is_file():
            if check["required"]:
                errors.append(f"required check missing: {check['id']}")
                if details is not None:
                    details.append({"check_id": check["id"], "errors": errors[first_error:]})
            continue
        result = read_json(path)
        sha = result.pop("digest", None)
        if digest(result) != sha:
            errors.append(f"result integrity failed: {check['id']}")
        result["digest"] = sha
        for key, expected in {"run_id": contract["run_id"], "task_id": contract["task_id"], "lane_id": contract.get("lane_id"), "attempt_id": contract["attempt_id"], "check_id": check["id"], "kind": check["kind"], "contract_digest": state["contract_digest"], "candidate_digest": candidate["digest"]}.items():
            if result.get(key) != expected:
                errors.append(f"wrong {key} in {check['id']}")
        artifacts_errors = validate_artifacts(run, result)
        artifacts_errors.extend(_unknown_artifacts(run, state, check["id"], result))
        errors.extend(f"{check['id']}: {message}" for message in artifacts_errors)
        passed_status = "passed" if check["kind"] == "command" else "satisfied"
        if check["required"] and result.get("status") != passed_status:
            errors.append(f"required check not satisfied: {check['id']} ({result.get('status')})")
        if check["kind"] == "command":
            if result.get("tools") != toolchains.get(check["id"]) or result.get("tools_after") != result.get("tools") or result.get("toolchain_digest") != state["toolchain_digest"]:
                errors.append(f"toolchain identity mismatch: {check['id']}")
            if result.get("argv") != check["argv"] or result.get("cwd") != check.get("cwd", contract.get("cwd")) or result.get("timeout") != check["timeout"]:
                errors.append(f"runner command contract mismatch: {check['id']}")
            if result.get("status") == "passed" and (result.get("exit_code") != check.get("expected_exit", 0) or result.get("input_before") != candidate["digest"] or result.get("input_after") != candidate["digest"]):
                errors.append(f"invalid command success: {check['id']}")
            artifact_names = [Path(p["path"]).name for p in result.get("artifacts", [])]
            if artifact_names.count("stdout.log") != 1 or artifact_names.count("stderr.log") != 1:
                errors.append(f"runner logs missing or duplicated: {check['id']}")
        elif set(result.get("criteria", {})) != set(check["criteria"]) or not result.get("assessor") or not result.get("method") or not result.get("assessment"):
            errors.append(f"typed assessment incomplete: {check['id']}")
        receipts.append({"check_id": check["id"], "result_digest": sha, "status": result.get("status")})
        if details is not None:
            details.append({"check_id": check["id"], "errors": errors[first_error:]})
    return errors, receipts

def accepted_evidence_errors(run: Run, verdict_digest, *, _ancestors=(), _evidence_validation=None):
    """Revalidate existing evidence without running checks, a new gate or run mutations."""
    if str(run.path) in _ancestors:
        return ["accepted dependency cycle"]
    try:
        state = run.state()
        contract = run.contract(state)
        candidate = run.assert_current(state, contract)
        accepted = state.get("accepted")
        if not accepted or accepted.get("verdict_digest") != verdict_digest:
            return ["accepted verdict identity changed"]
        path = (run.path / accepted["verdict_path"]).resolve()
        if run.path not in path.parents:
            return ["accepted verdict escaped run store"]
        verdict = read_json(path)
        supplied = verdict.pop("digest", None)
        errors = []
        if verdict.get("dependency_commit_protocol") == "closure_v1":
            from .dependency_commit import current_vector_errors
            errors.extend(current_vector_errors(verdict["dependency_vector"]))
        if supplied != verdict_digest or digest(verdict) != verdict_digest or verdict.get("verdict") != "accepted":
            errors.append("accepted verdict integrity failed")
        for key, expected in {"run_id": contract["run_id"], "task_id": contract["task_id"], "attempt_id": contract["attempt_id"], "contract_digest": state["contract_digest"], "candidate_digest": candidate["digest"]}.items():
            if verdict.get(key) != expected:
                errors.append("accepted verdict wrong " + key)
        toolchains = read_json(run.attempt_dir(state) / "toolchains.json")
        if digest(toolchains) != state.get("toolchain_digest"):
            errors.append("frozen toolchain integrity failed")
        check_errors, receipts = _check_evidence_errors(run, state, contract, candidate, toolchains, details=_evidence_validation)
        errors.extend(check_errors)
        # Bind the complete receipt set, including optional results present at
        # acceptance. A never-run optional check contributes no receipt.
        accepted_receipts = verdict.get("receipts", [])
        accepted_checks = [item for item in accepted_receipts if "check_id" in item]
        if not json_equal(receipts, accepted_checks):
            errors.append("accepted check receipts changed or missing")
        shared_paths = [item["path"] for item in contract.get("scope", {}).get("shared_files", []) if item["format"] != "json-records"]
        accepted_shared = [item for item in accepted_receipts if "check_id" not in item]
        if [item.get("path") for item in accepted_shared] != shared_paths:
            errors.append("accepted shared scope receipts changed or missing")
        for dependency in contract.get("dependencies", []):
            errors.extend(accepted_evidence_errors(Run(dependency["run_dir"]), dependency["verdict_digest"], _ancestors=(*_ancestors, str(run.path))))
        if any(o["status"] in {"planned", "unknown"} for o in state.get("operations", {}).values()):
            errors.append("unknown operation outcome")
        if snapshot(contract)["digest"] != candidate["digest"] or run.state() != state:
            errors.append("accepted inputs/state changed during read-only validation")
        return errors
    except (HarnessError, OSError, ValueError, KeyError, TypeError) as error:
        return ["accepted evidence unavailable: " + str(error)]


def gate(run: Run, *, actor="orchestrator"):
    """Retry preparation only when a mutable dependency generation changed."""
    if actor != "orchestrator":
        raise HarnessError("Only the orchestrator accepts a result; owners submit ready_for_review")
    from .dependency_commit import DependencyChanged
    remaining = remaining_budget_seconds(run.contract())
    deadline = time.monotonic() + (max(0, remaining) if remaining is not None else 30)
    for attempt in range(3):
        if time.monotonic() >= deadline:
            raise AcceptanceDeadlineExhausted("GATE_DEADLINE_EXHAUSTED: original acceptance deadline reached")
        try:
            return _gate_once(run, actor=actor, commit_deadline=deadline)
        except DependencyChanged as error:
            # _gate_once has unwound every closure lock before this handler.
            # Re-read the full closure and repeat expensive preparation on the
            # new inputs. Integrity, source and IO failures are never retried.
            if attempt == 2:
                raise HarnessError("DEPENDENCY_RETRY_EXHAUSTED: dependency generations changed in all three preparations") from error
    raise AssertionError("unreachable bounded gate loop")


def _gate_once(run: Run, *, actor, commit_deadline):
    from .dependency_commit import closure, commit_guard, vector
    observed = closure(run)
    state = run.state()
    contract = run.contract(state)
    candidate = run.assert_current(state, contract)
    toolchains = read_json(run.attempt_dir(state) / "toolchains.json")
    if digest(toolchains) != state.get("toolchain_digest"):
        raise HarnessError("Frozen toolchain manifest integrity failed")
    baseline = read_json(run.path / "origin-baseline.json")
    errors = scope_errors(contract, baseline, candidate)
    receipts = []
    shared_errors, shared_receipts = _shared_scope_errors(run, state, contract)
    errors.extend(shared_errors)
    receipts.extend(shared_receipts)
    check_errors, check_receipts = _check_evidence_errors(run, state, contract, candidate, toolchains)
    errors.extend(check_errors)
    receipts.extend(check_receipts)
    for dependency in contract.get("dependencies", []):
        dependency_errors = accepted_evidence_errors(Run(dependency["run_dir"]), dependency["verdict_digest"])
        errors.extend(f"dependency not accepted/current: {dependency['run_dir']}: {message}" for message in dependency_errors)
    unresolved = [key for key, value in state.get("operations", {}).items() if value["status"] in {"planned", "unknown"}]
    if unresolved:
        errors.append("unknown operation outcomes: " + ", ".join(unresolved))
    if snapshot(contract)["digest"] != candidate["digest"]:
        errors.append("candidate changed during gate")
    remaining = commit_deadline - time.monotonic()
    if remaining <= 0:
        raise AcceptanceDeadlineExhausted("GATE_DEADLINE_EXHAUSTED: original deadline reached during acceptance preparation")
    with commit_guard(run, observed, timeout=min(30, remaining), allow_root_change=True) as committed:
        current = run.state()
        if not json_equal(current, state):
            errors.append("run state changed during gate; repeat acceptance against current state")
        verdict = {"schema_version": 1, "run_id": contract["run_id"], "task_id": contract["task_id"], "attempt_id": contract["attempt_id"], "contract_digest": state["contract_digest"], "candidate_digest": candidate["digest"], "verdict": "accepted" if not errors else "rejected", "errors": errors, "receipts": receipts, "assessor": actor, "created_at": now(), "limitations": "Cooperating-process locks and hashes are process controls, not a sandbox or protection from an actor with the same OS rights. Semantic and visual assessments remain identified as assessments."}
        verdict.update(dependency_commit_protocol="closure_v1", accepted_as_of_generation=digest(state),
            dependency_vector=vector(committed, exclude=run))
        verdict["digest"] = digest(verdict)
        target = run.attempt_dir(state) / "gates" / (uuid.uuid4().hex + ".json")
        atomic_json(target, verdict, exclusive=True)
        current["status"] = "accepted" if not errors else "failed"
        current["accepted"] = {"verdict_digest": verdict["digest"], "candidate_digest": candidate["digest"], "verdict_path": target.relative_to(run.path).as_posix()} if not errors else None
        current["updated_at"] = now()
        atomic_json(run.state_path, current)
    return verdict
