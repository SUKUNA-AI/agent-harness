"""Four bounded engineering queries, rg first with explicit version bindings."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .context import _path, _read, _root
from .core import HarnessError, canonical, snapshot, _git
from .execution_ledger import repository_identity, safe_private, sanitized
from .file_io import read_regular_file

SOURCE_EXTENSIONS = {".py", ".pyi", ".rs", ".js", ".ts", ".tsx", ".jsx", ".c", ".h", ".cpp", ".hpp", ".go", ".java", ".cs"}


def _source(root, name, *, max_bytes=262144):
    path = _path(root, name)
    data, _ = _read(path, max_bytes, root=root, name=name)
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    sanitized(text)
    return text, hashlib.sha256(data).hexdigest()


def _files(root):
    rg = shutil.which("rg")
    partial = False
    if rg:
        process = subprocess.run([rg, "--files", "--hidden", "-g", "!.git", "-g", "!node_modules", "-g", "!.venv"], cwd=root,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if process.returncode not in {0, 1}:
            raise HarnessError("rg source inventory failed")
        names = process.stdout.decode("utf-8").splitlines()
        method = "rg_first"
    else:
        from .native import backend_info
        if backend_info().get("available"):
            from .repo_map import make_map
            mapped = make_map(root, max_files=4096)
            names = [r["path"] for r in [*mapped.get("neighbors", []), *mapped.get("symbols", [])] if isinstance(r, dict) and "path" in r]
            partial = True  # bounded Rust ranking is deliberately not a full inventory
            method = "rust_map_fallback"
        else:
            names = []
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv", "__pycache__"} and not (Path(directory) / d).is_symlink()]
                names.extend((Path(directory) / f).relative_to(root).as_posix() for f in files)
                if len(names) > 4096:
                    break
            method = "bounded_file_lookup_rg_unavailable"
    safe = []
    for name in names:
        name = name.replace("\\", "/") if os.name == "nt" else name
        if Path(name).suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        try:
            _path(root, name)
        except HarnessError:
            continue
        safe.append(name)
    return sorted(set(safe))[:4096], partial or len(safe) > 4096, method


def _matches(root, names, token):
    """Narrow rg to known safe source names; never execute trace/model text."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", token):
        raise HarnessError("Symbol lookup requires a bounded identifier")
    rg = shutil.which("rg")
    result = []
    if rg and names:
        # Batch argv size remains safe on Windows and does not need a shell.
        for start in range(0, len(names), 64):
            process = subprocess.run([rg, "--json", "-n", "-F", "-w", "--max-count", "64", "--", token, *names[start:start + 64]],
                                     cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            if process.returncode not in {0, 1}:
                raise HarnessError("rg symbol query failed")
            if len(process.stdout) > 4 * 1024 * 1024:
                raise HarnessError("rg result exceeds bounded query budget; narrow scope")
            for raw in process.stdout.splitlines():
                row = json.loads(raw)
                if row.get("type") == "match":
                    data = row["data"]
                    name = data["path"]["text"].replace("\\", "/")
                    _path(root, name)
                    result.append((name, data["line_number"]))
    else:
        pattern = re.compile(r"\b" + re.escape(token) + r"\b")
        for name in names:
            source = _source(root, name)
            if source:
                result.extend((name, index) for index, line in enumerate(source[0].splitlines(), 1) if pattern.search(line))
    return result[:8192]


def query_repository(root, mode, *, revision=None, target=None, changed=(), symbol=None,
                     path=None, trace_file=None, max_bytes=8192, offset=0, use_lsp=False, lsp_server=None):
    if mode not in {"related-tests", "trace-to-code", "change-impact", "symbol-context"}:
        raise HarnessError("Unknown engineering repository query")
    if type(max_bytes) is not int or not 2048 <= max_bytes <= 65536 or type(offset) is not int or offset < 0:
        raise HarnessError("Repository query bounds require 2048..65536 bytes and nonnegative offset")
    root = _root(root)
    head = _git(root, "rev-parse", "HEAD")
    actual_revision = os.fsdecode(head).strip() if head else "directory"
    if revision:
        expected = _git(root, "rev-parse", "--verify", revision + "^{commit}") if head else None
        if not expected or os.fsdecode(expected).strip() != actual_revision:
            raise HarnessError("Requested revision is not the current checkout; use its isolated worktree")
    tree = snapshot({"cwd": str(root), "scope": {"exclude": []}})
    names, partial, method = _files(root)
    rows, consulted, lsp_status = [], {}, "not_requested"

    def add(name, line, rank, reason, *, selected_symbol=None, end_line=None, confidence="heuristic"):
        if len(rows) >= 8192:
            return
        source = consulted.get(name)
        if source is None:
            source = _source(root, name)
            consulted[name] = source
        if not source:
            return
        lines = source[0].splitlines()
        if not 1 <= line <= max(1, len(lines)):
            return
        rows.append({"file": name, "line": line, "end_line": end_line or line,
            "symbol": selected_symbol, "rank": rank, "reason": reason,
            "sha256": source[1], "confidence": confidence, "snippet": lines[line - 1][:512] if lines else "",
            "link": {"path": name, "line": line, "revision": actual_revision, "snapshot_id": tree["digest"]}})

    if mode == "trace-to-code":
        if not trace_file:
            raise HarnessError("trace-to-code requires trace_file")
        text = read_regular_file(safe_private(trace_file), max_bytes=1024 * 1024).decode("utf-8")
        sanitized(text)
        frames = re.findall(r'(?:File\s+["\']([^"\']+)["\'],\s*line\s+(\d+)|([\w./\\:-]+\.(?:py|rs|js|ts|tsx|cpp|c|go|java)):(\d+))', text)
        for frame in frames[:256]:
            raw_name, number = (frame[0], frame[1]) if frame[0] else (frame[2], frame[3])
            candidate = Path(raw_name)
            if candidate.is_absolute():
                try:
                    name = candidate.resolve().relative_to(root).as_posix()
                except ValueError:
                    continue
            else:
                name = raw_name.replace("\\", "/")
            try:
                _path(root, name)
                if (root / name).is_file():
                    add(name, int(number), 100, "stack frame path and line")
            except (HarnessError, OSError):
                continue
    elif mode == "symbol-context":
        if not symbol or not path:
            raise HarnessError("symbol-context requires symbol and path")
        selected = _source(root, path)
        if not selected:
            raise HarnessError("Selected symbol source is unavailable or too large")
        consulted[path] = selected
        if use_lsp:
            try:
                from .lsp import query
                symbols = query(root, path, operation="symbols", server=lsp_server, timeout=10, max_bytes=max_bytes)
                if symbols.get("source", {}).get("sha256") != selected[1]:
                    raise HarnessError("LSP source binding is stale")
                for item in symbols.get("items", []):
                    if item.get("name") == symbol:
                        name = item["path"]
                        source = _source(root, name)
                        if not source or source[1] != item["sha256"]:
                            raise HarnessError("LSP definition binding is stale")
                        selected_range = item["range"]
                        add(name, selected_range["start"]["line"], 120, "LSP symbol definition bound to source digest",
                            selected_symbol=symbol, end_line=selected_range["end"]["line"], confidence="semantic")
                lsp_status = "observed"
            except (HarnessError, OSError, ValueError, subprocess.TimeoutExpired):
                lsp_status = "unavailable_or_stale_fallback"
        pattern = re.compile(r"\b(?:def|class|fn|struct|enum|function|interface|trait)\s+" + re.escape(symbol) + r"\b")
        for line, text in enumerate(selected[0].splitlines(), 1):
            if pattern.search(text):
                add(path, line, 100, "direct definition syntax", selected_symbol=symbol)
        for name, line in _matches(root, names, symbol):
            add(name, line, 50, "identifier occurrence; semantic reference not guaranteed", selected_symbol=symbol)
    else:
        targets = [target] if mode == "related-tests" else list(changed)
        if not targets or any(not value for value in targets):
            raise HarnessError("Query requires target or changed paths")
        for target_name in targets:
            _path(root, target_name)
            stem = Path(target_name).stem
            tests = [n for n in names if re.search(r"(?:^|/)(?:tests?|__tests__)(?:/|_)|(?:_test|\.test|\.spec)\.", n) or Path(n).stem.startswith("test_")]
            for name in tests:
                if stem in Path(name).stem:
                    add(name, 1, 90, "test filename corresponds to changed module")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", stem):
                for name, line in _matches(root, tests if mode == "related-tests" else names, stem):
                    if name != target_name:
                        add(name, line, 80, "module/import identifier occurrence")
            if mode == "change-impact" and (root / target_name).exists():
                source = _source(root, target_name)
                if source:
                    tokens = re.findall(r"\b(?:def|class|fn|struct|function)\s+([A-Za-z_]\w*)", source[0])[:16]
                    for token in tokens:
                        for name, line in _matches(root, names, token):
                            if name != target_name:
                                add(name, line, 60, "changed module symbol occurrence", selected_symbol=token)
    # Re-read bindings after query to reject stale line references.
    for name, source in consulted.items():
        if source and _source(root, name) != source:
            raise HarnessError("Source changed during repository query; repeat on current snapshot")
    after = snapshot({"cwd": str(root), "scope": {"exclude": []}})
    if tree["digest"] != after["digest"]:
        raise HarnessError("Repository changed during query; links are stale")
    unique = {(r["file"], r["line"], r["reason"]): r for r in rows}
    ranked = sorted(unique.values(), key=lambda r: (-r["rank"], r["file"], r["line"]))
    result = {"schema_version": 1, "mode": mode, "query": {"target": target, "changed": list(changed), "symbol": symbol, "path": path},
        "repository_identity": repository_identity(root), "revision": actual_revision,
        "snapshot_id": tree["digest"], "method": method, "results": [], "total_results": len(ranked),
        "offset": offset, "next_page": None, "complete_inventory": not partial, "lsp_status": lsp_status,
        "limitations": ["heuristic ranking; dynamic imports and semantic references may be missing",
                        "LSP is on-demand; fallback results are heuristic" if use_lsp else "LSP optional; no language server was requested"]}
    for index in range(offset, len(ranked)):
        result["results"].append(ranked[index])
        if len(canonical(result)) + 2 > max_bytes:
            result["results"].pop()
            result["next_page"] = index
            break
    if len(canonical(result)) + 2 > max_bytes:
        raise HarnessError("Repository query envelope exceeds budget")
    return result


def dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("related-tests", "trace-to-code", "change-impact", "symbol-context"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--revision")
    parser.add_argument("--target")
    parser.add_argument("--changed", action="append", default=[])
    parser.add_argument("--symbol")
    parser.add_argument("--path")
    parser.add_argument("--trace-file")
    parser.add_argument("--max-bytes", type=int, default=8192)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--use-lsp", action="store_true")
    parser.add_argument("--lsp-server", nargs="+")
    args = vars(parser.parse_args(argv))
    value = query_repository(**args)
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0
