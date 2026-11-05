"""Optional bundled Rust backend: one request per process, never a daemon/cache.

Only absence permits auto fallback. A present binary's validation, execution,
deadline or protocol failures are errors, so acceptance cannot hide them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import stat
import subprocess
import tempfile
import threading
import time

from . import __version__
from .core import HarnessError

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 100 * 1024 * 1024 + 2
_verified_bundles = {}
_routing_hints = {}
AUTO_NATIVE_FILE_THRESHOLD = 512


def _mode():
    value = os.environ.get("AGENT_HARNESS_BACKEND", "auto").lower()
    if value not in {"auto", "python", "native"}:
        raise HarnessError("AGENT_HARNESS_BACKEND must be auto, python or native")
    return value


def platform_key():
    system = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(platform.system())
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    return f"{system}-{architecture}" if system and architecture else None


def _stamp(path):
    value = path.stat()
    if not stat.S_ISREG(value.st_mode):
        raise HarnessError("Native executable/metadata must be regular files")
    return tuple(getattr(value, "st_" + key) for key in ("dev", "ino", "mode", "size", "mtime_ns", "ctime_ns"))


def _strict_json(raw):
    def object_value(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def constant(_value):
        raise ValueError("non-finite JSON number")

    return json.loads(raw, object_pairs_hook=object_value, parse_constant=constant)


def _verify_bundle(path):
    metadata_path = path.parent / "build-info.json"
    try:
        identity = (_stamp(path), _stamp(metadata_path))
        cached = _verified_bundles.get(path)
        if cached is not None and cached[0] == identity:
            return cached[1]
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(metadata_path, flags), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise HarnessError("Native build metadata must be a regular file")
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise HarnessError("Native build metadata exceeds 1 MiB")
        info = _strict_json(raw)
        if not isinstance(info, dict) or info.get("schema_version") != 1 or info.get("protocol_version") != PROTOCOL_VERSION or info.get("version") != __version__:
            raise HarnessError("Bundled native build version/protocol does not match this package")
        checksum = hashlib.sha256()
        with os.fdopen(os.open(path, flags), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise HarnessError("Native executable must be a regular file")
            for block in iter(lambda: source.read(1024 * 1024), b""):
                checksum.update(block)
        if checksum.hexdigest() != info.get("binary_sha256"):
            raise HarnessError("Bundled native executable integrity check failed")
        if identity != (_stamp(path), _stamp(metadata_path)):
            raise HarnessError("Native executable/metadata changed during validation")
        # This memo only avoids rereading an unchanged installed executable.
        # Source files are always rehashed by the snapshot operation.
        _verified_bundles[path] = (identity, info)
        return info
    except (OSError, ValueError, TypeError) as error:
        raise HarnessError(f"Cannot verify bundled native executable: {error}") from error


def find_binary(required=False):
    """Discover/verify native executable. Explicit overrides are developer-owned."""
    override = os.environ.get("AGENT_HARNESS_NATIVE")
    if override is not None:
        path = Path(override)
        if not override or not path.is_absolute() or not path.is_file():
            raise HarnessError("AGENT_HARNESS_NATIVE must name an existing absolute executable")
        path = path.resolve()
        _stamp(path)
        if os.name != "nt" and not os.access(path, os.X_OK):
            raise HarnessError("AGENT_HARNESS_NATIVE is not executable")
        return path
    key = platform_key()
    if key:
        package = Path(__file__).resolve().parent
        executable = "harness-native.exe" if os.name == "nt" else "harness-native"
        for directory in (package / "bin" / key, package.parent.parent / "bin" / key):
            path = directory / executable
            if os.path.lexists(path) or os.path.lexists(directory / "build-info.json"):
                _verify_bundle(path)
                if os.name != "nt" and not os.access(path, os.X_OK):
                    raise HarnessError("Bundled native executable is not executable")
                return path
    if required:
        raise HarnessError("Native backend unavailable: install/build the platform binary or set AGENT_HARNESS_NATIVE")
    return None


def backend_info():
    requested = _mode()
    path = find_binary(required=requested == "native")
    selected = "adaptive" if path and requested == "auto" else "native" if path and requested == "native" else "python"
    info = {"requested": requested, "selected": selected,
            "available": path is not None, "platform": platform_key(), "path": str(path) if path else None,
            "protocol_version": PROTOCOL_VERSION, "build_info": None}
    if selected == "adaptive":
        info["snapshot_policy"] = {"python_below_observed_files": AUTO_NATIVE_FILE_THRESHOLD,
                                   "native_for_large_or_unknown_tree": True,
                                   "hint_cache": "process-local routing only; never file hashes"}
    if path is not None and os.environ.get("AGENT_HARNESS_NATIVE") is None:
        metadata = _verify_bundle(path)
        info["build_info"] = {key: metadata[key] for key in ("version", "protocol_version", "binary_sha256", "source_digest", "target", "rustc") if key in metadata}
    return info


def _prefer_python(contract):
    """A cheap performance hint, never an input inventory or acceptance cache.

    Counting stops early. Ignored Git files may overestimate a tree, which merely
    selects native. A changed tree can keep its hint until this CLI process exits;
    both implementations still hash and validate the actual inputs each time.
    """
    cwd = contract.get("cwd")
    if not cwd:
        return True
    from .core import matches
    root = Path(cwd).resolve()
    excludes = (".git", ".git/**", *contract.get("scope", {}).get("exclude", []))
    key = (str(root), excludes)
    if key in _routing_hints:
        return _routing_hints[key]
    deadline = time.monotonic() + 0.02
    pending, files, directories = [root], 0, 0
    try:
        while pending and files < AUTO_NATIVE_FILE_THRESHOLD and directories < 64 and time.monotonic() < deadline:
            directory = pending.pop()
            directories += 1
            with os.scandir(directory) as entries:
                for entry in entries:
                    name = Path(entry.path).relative_to(root).as_posix()
                    if matches(name, excludes):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    else:
                        files += 1
                    if files >= AUTO_NATIVE_FILE_THRESHOLD or time.monotonic() >= deadline:
                        _routing_hints[key] = False
                        return False
        small = not pending and files < AUTO_NATIVE_FILE_THRESHOLD
    except OSError:
        small = False
    if len(_routing_hints) >= 128:
        _routing_hints.clear()
    _routing_hints[key] = small
    return small


def invoke(request, timeout=120.0):
    """Run a required native operation; all failures propagate as HarnessError."""
    path = find_binary(required=True)
    if not isinstance(request, dict) or type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise HarnessError("Native request must be an object with a finite positive timeout")
    request = {"protocol_version": PROTOCOL_VERSION, **request}
    try:
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise HarnessError(f"Cannot encode native request: {error}") from error
    if len(payload) > MAX_REQUEST_BYTES:
        raise HarnessError("Native request exceeds 16 MiB")
    process = job = waiter = None
    with tempfile.TemporaryFile() as input_file, tempfile.TemporaryFile() as output_file, tempfile.TemporaryFile() as error_file:
        input_file.write(payload)
        input_file.seek(0)
        try:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004 if os.name == "nt" else 0
            process = subprocess.Popen([str(path)], stdin=input_file, stdout=output_file, stderr=error_file,
                                       shell=False, start_new_session=os.name == "posix", creationflags=flags)
            if os.name == "nt":
                from .runner import WindowsJob
                job = WindowsJob(process)
                job.resume_suspended_process(process)
            if os.name == "posix":
                # Popen.wait(timeout) polls exponentially on POSIX; short native
                # calls otherwise wait for an unnecessary 16/32 ms polling tick.
                completed = threading.Event()
                wait_errors = []
                def wait_for_exit():
                    try:
                        process.wait()
                    except OSError as error:
                        wait_errors.append(error)
                    finally:
                        completed.set()
                waiter = threading.Thread(target=wait_for_exit, daemon=True)
                waiter.start()
                if not completed.wait(timeout):
                    raise subprocess.TimeoutExpired([str(path)], timeout)
                if wait_errors:
                    raise wait_errors[0]
            else:
                process.wait(timeout=timeout)
            output_file.seek(0)
            raw = output_file.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise HarnessError("Native response exceeds 100 MiB")
            try:
                value = _strict_json(raw)
            except (ValueError, UnicodeError) as error:
                raise HarnessError("Native backend returned invalid JSON") from error
            if not isinstance(value, dict):
                raise HarnessError("Native backend response must be an object")
            if process.returncode or value.get("error"):
                detail = str(value.get("error", "no structured error"))[:4000]
                raise HarnessError(f"Native {request.get('operation', 'operation')} failed (exit {process.returncode}): {detail}")
            return value
        except subprocess.TimeoutExpired as error:
            raise HarnessError(f"Native operation exceeded {timeout:g} seconds") from error
        except OSError as error:
            raise HarnessError(f"Cannot execute native backend: {error}") from error
        finally:
            if job:
                job.close()
            if process is not None:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if waiter is not None:
                    waiter.join(timeout=5)


def snapshot(contract):
    """None has one meaning: the caller should use the Python implementation."""
    mode = _mode()
    if mode == "python":
        return None
    if find_binary(required=mode == "native") is None:
        return None
    if mode == "auto" and _prefer_python(contract):
        return None
    value = invoke({"operation": "snapshot", "contract": contract})
    if value.get("schema_version") != 1 or value.get("kind") not in {"non_file", "directory", "git"} or not isinstance(value.get("files"), dict) or not isinstance(value.get("external_inputs"), dict):
        raise HarnessError("Native backend returned an invalid snapshot manifest")
    if (value.get("snapshot_scheme_version") != 2
            or value.get("topology_policy") != "nofollow-ancestors-literal-leaf-v1"
            or not isinstance(value.get("content_digest"), str)
            or contract.get("cwd") and not isinstance(value.get("root_binding"), dict)):
        raise HarnessError("Native snapshot capability is stale; rebuild the backend for physical snapshot scheme v2")
    return value
