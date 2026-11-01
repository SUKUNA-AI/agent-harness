"""Bounded task-relevant repository maps, computed locally by the Rust binary."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .core import HarnessError


def make_map(root, *, query="", focus_files=(), excludes=(), max_bytes=8192,
             max_files=4096, max_file_bytes=262144, max_total_bytes=67108864,
             timeout=10.0):
    from .native import invoke

    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise HarnessError("Map root must be an existing directory")
    if not isinstance(query, str) or len(query.encode("utf-8")) > 4096:
        raise HarnessError("Map query must be at most 4096 UTF-8 bytes")
    bounds = ((max_bytes, 2048, 65536), (max_files, 1, 50000),
              (max_file_bytes, 1, 1048576), (max_total_bytes, 1, 134217728))
    if any(type(value) is not int or not low <= value <= high for value, low, high in bounds):
        raise HarnessError("Map resource limits are outside supported bounds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0.1 <= timeout <= 30:
        raise HarnessError("Map timeout must be between 0.1 and 30 seconds")
    # The native implementation independently checks paths and all limits.
    result = invoke({"operation": "repomap", "root": str(root), "query": query,
                     "focus_files": list(focus_files), "excludes": list(excludes),
                     "max_bytes": max_bytes, "max_files": max_files,
                     "max_file_bytes": max_file_bytes, "max_total_bytes": max_total_bytes,
                     "timeout_ms": round(timeout * 1000)}, timeout=timeout + 5)
    if not isinstance(result, dict) or result.get("purpose") != "syntactic_repo_map":
        raise HarnessError("Native map returned an invalid response")
    # Account for the largest newline emitted by Python's Windows text stream.
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) + 2 > max_bytes:
        raise HarnessError("Native map exceeded the requested output budget")
    return result


def dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--query", default="")
    parser.add_argument("--focus-file", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--max-bytes", type=int, default=8192)
    parser.add_argument("--max-files", type=int, default=4096)
    parser.add_argument("--max-file-bytes", type=int, default=262144)
    parser.add_argument("--max-total-bytes", type=int, default=67108864)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        result = make_map(args.root, query=args.query, focus_files=args.focus_file,
                          excludes=args.exclude, max_bytes=args.max_bytes,
                          max_files=args.max_files, max_file_bytes=args.max_file_bytes,
                          max_total_bytes=args.max_total_bytes, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        return 0
    except (HarnessError, OSError, ValueError, TypeError) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
