"""Explicitly scoped, read-only run metadata. No harness runner is imported.

Only the optional ``create_server`` path imports the official MCP v2 SDK.
Recorded evidence is never promoted to a fresh workspace verification.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys


MAX_FILE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_DIRECTORY_ENTRIES = 2048
MAX_CHECKS = 100
MAX_OWNERS = 100
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT[0-9:.+Z-]{8,28}\Z")
STATUSES = frozenset({"planned", "running", "verifying", "ready_for_review", "accepted",
                      "rejected", "blocked", "paused", "stale", "complete", "completed",
                      "open", "idle", "done", "stopped", "closed", "unavailable", "unknown",
                      "passed", "failed", "satisfied", "not_run", "skipped", "timeout",
                      "cancelled", "interrupted", "missing_dependency", "error",
                      "budget_exhausted", "toolchain_changed", "inputs_changed", "validation_timeout"})
WORKFLOWS = frozenset({"answer", "research", "plan", "review", "diagnostics", "change",
                       "performance", "document", "learning", "operations", "mixed"})
KINDS = frozenset({"command", "source", "content", "visual", "runtime"})


class MetadataError(ValueError):
    """An intentionally generic error: never echo paths or raw input to MCP."""


def _id(value):
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def _sha(value):
    return value if isinstance(value, str) and SHA.fullmatch(value) else None


def _timestamp(value):
    return value if isinstance(value, str) and TIMESTAMP.fullmatch(value) else None


def _status(value):
    return value if isinstance(value, str) and value in STATUSES else "unknown"


def _choice(value, choices):
    return value if isinstance(value, str) and value in choices else "unknown"


def _integer(value, low=0, high=1_000_000):
    return value if type(value) is int and low <= value <= high else None


def _bounded(value):
    if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise MetadataError("Metadata output exceeds the configured bound")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MetadataError("Duplicate JSON keys are not supported")
        result[key] = value
    return result


def _check_tree(value):
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 8192:
            raise MetadataError("JSON structure exceeds the configured bound")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)


def _opened_path(fd):
    """Resolve the opened handle itself, not the path that may have been replaced."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        get_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
        get_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
        get_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_path(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
        if not 0 < length < len(buffer):
            raise MetadataError("Cannot verify the opened metadata handle")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    if sys.platform.startswith("linux"):
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    if sys.platform == "darwin":
        import fcntl
        return Path(os.fsdecode(fcntl.fcntl(fd, 50, bytes(1024))).split("\0", 1)[0])  # F_GETPATH
    raise MetadataError("Opened-path validation is unavailable on this platform")


class RunMetadataStore:
    """One existing, absolute local store; tools accept IDs, never paths."""

    def __init__(self, store):
        self.root = Path(store)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise MetadataError("An explicit absolute store directory is required")
        if os.name == "nt" and str(self.root).startswith("\\\\"):
            raise MetadataError("A local store is required; UNC paths are not supported")
        self._no_links(self.root)
        # Expand Windows 8.3 aliases only AFTER rejecting linked ancestors.
        self.root = self.root.resolve(strict=True)
        self._validate(self.root, directory=True)

    @staticmethod
    def _no_links(path):
        # Reject links/reparse points in the root AND every ancestor. In particular,
        # a Windows junction must not turn an in-store path into an outside read.
        for part in reversed((path, *path.parents)):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise MetadataError("Linked metadata paths are not supported")

    def _validate(self, path, *, directory=False):
        if not path.is_relative_to(self.root):
            raise MetadataError("Path is outside the configured store")
        self._no_links(path)
        if not path.resolve(strict=True).is_relative_to(self.root):
            raise MetadataError("Path escapes the configured store")
        info = path.lstat()
        if directory and not stat.S_ISDIR(info.st_mode):
            raise MetadataError("Expected a metadata directory")
        return info

    def _directory(self, *parts):
        path = self.root.joinpath(*parts)
        self._validate(path, directory=True)
        return path

    def _entries(self, path):
        self._validate(path, directory=True)
        names = []
        with os.scandir(path) as entries:
            for entry in entries:
                if len(names) >= MAX_DIRECTORY_ENTRIES:
                    raise MetadataError("Directory exceeds the configured entry bound")
                names.append(entry.name)
        self._validate(path, directory=True)
        return sorted(names)

    def _read(self, *parts, missing_ok=False):
        path = self.root.joinpath(*parts)
        try:
            before = self._validate(path)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise MetadataError("Expected an unlinked regular metadata file")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > MAX_FILE_BYTES:
                    raise MetadataError("Metadata file exceeds the bound or is not regular")
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise MetadataError("Metadata changed while opening")
                actual = _opened_path(source.fileno())
                if actual != path or not actual.is_relative_to(self.root):
                    raise MetadataError("Opened metadata handle escapes its expected path")
                self._validate(path)
                data = source.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise MetadataError("Metadata file exceeds the configured byte bound")
            value = json.loads(data.decode("utf-8"), object_pairs_hook=_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(MetadataError("Non-finite JSON number")))
            if not isinstance(value, dict):
                raise MetadataError("Metadata JSON must be an object")
            _check_tree(value)
            return value
        except MetadataError:
            raise
        except FileNotFoundError:
            if missing_ok:
                return None
            raise MetadataError("Metadata is missing, inaccessible or invalid") from None
        except (OSError, ValueError, RecursionError):
            raise MetadataError("Metadata is missing, inaccessible or invalid") from None

    def _state(self, run_id):
        if _id(run_id) is None:
            raise MetadataError("Invalid run identifier")
        value = self._read(run_id, "state.json")
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise MetadataError("Unsupported run state schema")
        if value.get("run_id") != run_id or _id(value.get("current_attempt")) is None:
            raise MetadataError("Invalid run state identity")
        return value

    @staticmethod
    def _summary(state):
        return {"run_id": _id(state.get("run_id")), "task_id": _id(state.get("task_id")),
                "workflow": _choice(state.get("workflow"), WORKFLOWS),
                "recorded_status": _status(state.get("status")), "current_attempt": _id(state.get("current_attempt")),
                "created_at": _timestamp(state.get("created_at")), "updated_at": _timestamp(state.get("updated_at")),
                "checkpoints": _integer(state.get("checkpoints"))}

    def list_runs(self, limit=20, offset=0):
        if _integer(limit, 1, 50) is None or _integer(offset, 0, MAX_DIRECTORY_ENTRIES) is None:
            raise MetadataError("limit must be 1..50 and offset within the scan bound")
        summaries = []
        skipped = 0
        try:
            names = self._entries(self.root)
        except (OSError, ValueError):
            raise MetadataError("Store cannot be listed within its configured bounds") from None
        for name in names:
            if not _id(name):
                continue
            try:
                self._directory(name)
                summaries.append(self._summary(self._state(name)))
            except (OSError, ValueError, TypeError):
                skipped += 1
        page = summaries[offset:offset + limit]
        return _bounded({"runs": page, "next_offset": offset + limit if offset + limit < len(summaries) else None,
                         "skipped_entries": skipped, "evidence_freshness": "not_checked"})

    def run_status(self, run_id):
        state = self._state(run_id)
        owners = state.get("owners", {})
        if not isinstance(owners, dict) or len(owners) > MAX_OWNERS:
            raise MetadataError("Owner metadata exceeds the configured bound")
        result = self._summary(state)
        result.update({"contract_digest": _sha(state.get("contract_digest")),
                       "candidate_digest": _sha(state.get("candidate_digest")),
                       "evidence_freshness": "not_checked", "owners": [], "checks": []})
        for owner_id, owner in sorted(owners.items()):
            if not _id(owner_id) or not isinstance(owner, dict):
                raise MetadataError("Invalid owner metadata")
            result["owners"].append({"owner_id": owner_id, "lane_id": _id(owner.get("lane_id")),
                                     "recorded_status": _status(owner.get("status")),
                                     "observed_at": _timestamp(owner.get("observed_at")),
                                     "needs_native_reconciliation": owner.get("needs_native_reconciliation") is True})
        attempt = state["current_attempt"]
        try:
            check_dir = self._directory(run_id, "attempts", attempt, "checks")
        except FileNotFoundError:
            return _bounded(result)
        except (OSError, ValueError):
            raise MetadataError("Check metadata directory is not accessible") from None
        try:
            names = self._entries(check_dir)
            if len(names) > MAX_CHECKS:
                raise MetadataError("Check metadata exceeds the configured bound")
            for check_id in names:
                if not _id(check_id):
                    raise MetadataError("Invalid check identifier")
                record = self._read(run_id, "attempts", attempt, "checks", check_id, "result.json", missing_ok=True)
                if record is None:
                    # run_check creates its folder before publishing a final
                    # result. Re-read state, then the result: finalization may
                    # have completed between the first missing read and now.
                    latest = self._state(run_id)
                    for key in ("task_id", "current_attempt", "contract_digest", "candidate_digest"):
                        if latest.get(key) != state.get(key):
                            raise MetadataError("Run identity changed while reading checks")
                    record = self._read(run_id, "attempts", attempt, "checks", check_id, "result.json", missing_ok=True)
                    if record is None:
                        processes = latest.get("processes", {})
                        if not isinstance(processes, dict):
                            raise MetadataError("Invalid process metadata")
                        process = processes.get(check_id)
                        running = (isinstance(process, dict) and process.get("status") == "running"
                                   and process.get("attempt_id") == attempt)
                        starting = process is None and latest.get("status") == "verifying"
                        if not (running or starting):
                            raise MetadataError("Completed check has no result metadata")
                        result["checks"].append({"check_id": check_id,
                                                 "kind": "command" if running else "unknown",
                                                 "recorded_status": "running" if running else "unknown",
                                                 "attempt_id": attempt, "result_available": False,
                                                 "matches_recorded_candidate": None})
                        continue
                if record.get("check_id") != check_id:
                    raise MetadataError("Invalid check metadata identity")
                digest = _sha(record.get("candidate_digest"))
                result["checks"].append({"check_id": check_id, "kind": _choice(record.get("kind"), KINDS),
                                          "recorded_status": _status(record.get("status")),
                                          "result_available": True,
                                          "exit_code": _integer(record.get("exit_code"), -2147483648, 4294967295),
                                          "attempt_id": _id(record.get("attempt_id")),
                                          "candidate_digest": digest,
                                          "matches_recorded_candidate": digest == result["candidate_digest"] if digest else None})
        except (OSError, ValueError, TypeError):
            raise MetadataError("Check metadata is invalid or exceeds its bounds") from None
        return _bounded(result)


def create_server(store):
    """Build an SDK v2 server with exactly two read-only tools."""
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
    except ImportError:
        raise MetadataError("Install the optional native-agent-harness[mcp] extra (MCP SDK 2.3.0)") from None
    metadata = RunMetadataStore(store)
    server = MCPServer("harness-run-metadata", instructions="Read-only recorded run metadata; freshness and native lifecycle are not verified.")
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

    @server.tool(annotations=annotations, structured_output=True)
    def list_runs(limit: int = 20, offset: int = 0) -> dict[str, object]:
        """List bounded recorded run summaries; no prompts, paths, logs or fresh verification."""
        try:
            return metadata.list_runs(limit, offset)
        except (MetadataError, OSError, ValueError, TypeError):
            raise ToolError("Run summaries unavailable: invalid scope, metadata or bounds") from None

    @server.tool(annotations=annotations, structured_output=True)
    def run_status(run_id: str) -> dict[str, object]:
        """Read recorded status/checks/owners for a run ID; does not resume or accept work."""
        try:
            return metadata.run_status(run_id)
        except (MetadataError, OSError, ValueError, TypeError):
            raise ToolError("Run metadata unavailable: invalid ID, scope, metadata or bounds") from None

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only private run metadata over MCP stdio")
    parser.add_argument("--store", required=True, help="Existing absolute private run-store directory")
    args = parser.parse_args(argv)
    try:
        create_server(args.store).run(transport="stdio")
    except (MetadataError, OSError, ValueError):
        print("Cannot start metadata MCP: check the explicit store and optional MCP extra", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
