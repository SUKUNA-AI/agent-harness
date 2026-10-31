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
import subprocess
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

from .core import HarnessError, Run, atomic_json, canonical, changed_paths, digest, file_hash, identity_alive, lock, now, process_identity, read_json, remaining_budget_seconds, scope_errors, snapshot


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


def _drain(source, path):
    with Path(path).open("x", encoding="utf-8", newline="\n") as output:
        for line in iter(source.readline, b""):
            output.write(redact(line.decode("utf-8", errors="replace")))
            output.flush()
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


def _start_contained(argv, cwd=None):
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004 if os.name == "nt" else 0
    process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, shell=False, creationflags=flags, start_new_session=(os.name != "nt"))
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


def _contained_capture(argv, *, cwd=None, timeout=10):
    """Use the same tree containment for version probes and scope adapters."""
    process, job = _start_contained(argv, cwd)
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


def _verify_assertions(check, stdout, stderr, report_paths):
    errors = []
    for assertion in check.get("assertions", []):
        stream = {"stdout": stdout, "stderr": stderr, "combined": stdout + "\n" + stderr}.get(assertion.get("stream", "combined"))
        if stream is None:
            errors.append("unknown assertion stream")
            continue
        count = len(re.findall(assertion["regex"], stream, re.M))
        if assertion.get("forbidden", False) and count:
            errors.append(f"forbidden output pattern found: {assertion['regex']}")
        elif not assertion.get("forbidden", False) and count < assertion.get("min_matches", 1):
            errors.append(f"required output pattern missing: {assertion['regex']}")
    reports = []
    for definition, path in report_paths:
        try:
            if definition["format"] == "junit":
                root = ET.parse(path).getroot()
                cases = list(root.iter("testcase"))
                names = [f"{case.get('classname', '')}::{case.get('name', '')}" for case in cases]
                skipped = sum(case.find("skipped") is not None for case in cases)
                failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
                report = {"format": "junit", "cases": names, "tests": len(cases), "skipped": skipped, "failed": failed}
                if len(cases) < definition.get("min_tests", 1):
                    errors.append("test report collected too few cases")
                if skipped > definition.get("max_skipped", 0):
                    errors.append("test report skipped mandatory cases")
                if failed:
                    errors.append("test report contains failures")
                for expected in definition.get("required_cases", []):
                    if not any(re.search(expected, name) for name in names):
                        errors.append(f"mandatory case missing: {expected}")
                reports.append(report)
            elif definition["format"] == "json":
                value = read_json(path)
                for key, expected in definition.get("equals", {}).items():
                    cursor = value
                    for segment in key.split("."):
                        cursor = cursor[segment]
                    if cursor != expected:
                        errors.append(f"report field mismatch: {key}")
                reports.append({"format": "json", "verified_equals": definition.get("equals", {})})
            else:
                errors.append("unsupported report format")
        except (OSError, ValueError, KeyError, ET.ParseError) as error:
            errors.append(f"invalid test report: {type(error).__name__}")
    return errors, reports


def _versions(check):
    result = {}
    executable = shutil.which(check["argv"][0])
    result["executable"] = {"requested": check["argv"][0], "resolved": executable, "version": "unknown", "sha256": file_hash(executable) if executable and Path(executable).is_file() else None}
    for name, argv in check.get("tool_versions", {}).items():
        if not isinstance(argv, list) or not argv:
            raise HarnessError("tool_versions entries must be argv arrays")
        try:
            proc = _contained_capture(argv, timeout=10)
            result[name] = {"argv": argv, "exit_code": proc.returncode, "version": redact((proc.stdout + proc.stderr).decode("utf-8", errors="replace"))[:2000].strip()}
        except (OSError, subprocess.TimeoutExpired) as error:
            result[name] = {"argv": argv, "status": type(error).__name__}
    return result


def run_check(run: Run, check_id, *, cancel_file=None):
    state = run.state()
    contract = run.contract(state)
    check = _check(contract, check_id)
    if check["kind"] != "command":
        raise HarnessError("Use evidence for non-command checks")
    remaining = remaining_budget_seconds(contract)
    if remaining is not None and remaining <= 0:
        raise HarnessError("Run deadline exhausted: check not_run, no process started")
    folder = _result_path(run, state, check_id).parent
    folder.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(run.workspace_lock(contract))
        for resource in sorted(set(check.get("resources", []))):
            stack.enter_context(lock(run.resource_path("resource:" + resource), purpose="check resource: " + resource))
        stack.enter_context(lock(folder / ".check.lock", purpose="command check " + check_id))
        if _result_path(run, state, check_id).exists():
            raise HarnessError("Check already has immutable evidence; use a new attempt to repeat it")
        before = run.assert_current(state, contract)
        frozen_toolchains = read_json(run.attempt_dir(state) / "toolchains.json")
        if digest(frozen_toolchains) != state.get("toolchain_digest"):
            raise HarnessError("Frozen toolchain manifest integrity failed")
        actual_tools = _versions(check)
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
                process, job = _start_contained(check["argv"], record["cwd"])
                identity = process_identity(process.pid)
                record["process"] = {"identity": identity, "tree_control": "windows_suspended_job_kill_on_close" if os.name == "nt" else "private_process_group"}
                with run.edit() as current:
                    current.setdefault("processes", {})[check_id] = {"identity": identity, "status": "running", "started_at": now(), "attempt_id": contract["attempt_id"], "argv_digest": digest(check["argv"])}
                    current["status"] = "verifying"
                for source, path in ((process.stdout, stdout_path), (process.stderr, stderr_path)):
                    thread = threading.Thread(target=_drain, args=(source, path), daemon=True)
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
            for path in (stdout_path, stderr_path):
                if not path.exists():
                    path.write_text("", encoding="utf-8")
            record["finished_at"] = now()
            record["elapsed_seconds"] = round(time.monotonic() - started, 6)
            after = snapshot(contract)
            record["input_after"] = after["digest"]
            record["tools_after"] = _versions(check)
            if record["tools_after"] != record["tools"]:
                record["status"] = "toolchain_changed"
            if before["digest"] != after["digest"]:
                record["status"] = "inputs_changed"
                record["changed_paths"] = changed_paths(before, after)
            report_paths = []
            for index, report in enumerate(check.get("reports", [])):
                source = Path(report["path"])
                if not source.is_absolute():
                    source = Path(record["cwd"]) / source
                if source.is_file():
                    target = run_dir / (f"report-{index}" + source.suffix)
                    shutil.copyfile(source, target)
                    report_paths.append((report, target))
                else:
                    record.setdefault("assertion_errors", []).append(f"required report missing: {report['path']}")
            errors, reports = _verify_assertions(check, stdout_path.read_text(encoding="utf-8"), stderr_path.read_text(encoding="utf-8"), report_paths)
            record.setdefault("assertion_errors", []).extend(errors)
            record["reports"] = reports
            if record["assertion_errors"] and record["status"] == "passed":
                record["status"] = "failed"
            record["artifacts"] = [_proof(p, run.path) for p in (stdout_path, stderr_path, *(p for _, p in report_paths))]
            record["digest"] = digest(record)
            atomic_json(_result_path(run, state, check_id), record, exclusive=True)
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
            left, right = read_json(baseline), read_json(current)
            key = definition.get("records_key")
            if key:
                left, right = left[key], right[key]
            id_key = definition.get("id_key", "id")
            a, b = ({row[id_key]: row for row in rows} for rows in (left, right))
            if set(a) != set(b):
                errors.append(f"shared record IDs changed: {definition['path']}")
            for record_id in set(a) | set(b):
                if a.get(record_id) != b.get(record_id) and record_id not in definition["allowed_records"]:
                    errors.append(f"foreign shared record changed: {record_id}")
            # Preserve all non-record sections too.
            if key:
                full_a, full_b = read_json(baseline), read_json(current)
                full_a.pop(key, None)
                full_b.pop(key, None)
                if full_a != full_b:
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


def gate(run: Run, *, actor="orchestrator"):
    if actor != "orchestrator":
        raise HarnessError("Only the orchestrator accepts a result; owners submit ready_for_review")
    with run.workspace_lock():
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
        for check in contract["checks"]:
            path = _result_path(run, state, check["id"])
            if not path.is_file():
                if check["required"]:
                    errors.append(f"required check missing: {check['id']}")
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
        for dependency in contract.get("dependencies", []):
            dependency_run = Run(dependency["run_dir"])
            observed = dependency_run.status()
            accepted = observed.get("accepted")
            if not observed.get("evidence_current") or not accepted or accepted.get("verdict_digest") != dependency["verdict_digest"]:
                errors.append(f"dependency not accepted/current: {dependency['run_dir']}")
        unresolved = [key for key, value in state.get("operations", {}).items() if value["status"] in {"planned", "unknown"}]
        if unresolved:
            errors.append("unknown operation outcomes: " + ", ".join(unresolved))
        if snapshot(contract)["digest"] != candidate["digest"]:
            errors.append("candidate changed during gate")
        verdict = {"schema_version": 1, "run_id": contract["run_id"], "task_id": contract["task_id"], "attempt_id": contract["attempt_id"], "contract_digest": state["contract_digest"], "candidate_digest": candidate["digest"], "verdict": "accepted" if not errors else "rejected", "errors": errors, "receipts": receipts, "assessor": actor, "created_at": now(), "limitations": "Cooperating-process locks and hashes are process controls, not a sandbox or protection from an actor with the same OS rights. Semantic and visual assessments remain identified as assessments."}
        verdict["digest"] = digest(verdict)
        target = run.attempt_dir(state) / "gates" / (uuid.uuid4().hex + ".json")
        atomic_json(target, verdict, exclusive=True)
        with run.edit() as current:
            if current["current_attempt"] != state["current_attempt"] or current["contract_digest"] != state["contract_digest"]:
                raise HarnessError("Attempt changed during gate")
            current["status"] = "accepted" if not errors else "failed"
            current["accepted"] = {"verdict_digest": verdict["digest"], "candidate_digest": candidate["digest"], "verdict_path": target.relative_to(run.path).as_posix()} if not errors else None
        return verdict
