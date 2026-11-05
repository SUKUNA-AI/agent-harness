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
from .core import HarnessError, canonical, digest, snapshot, _git
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


def _files(root, inventory=None):
    """Use the same snapshot inventory, including tracked files now ignored."""
    tree = inventory if inventory is not None else snapshot({"cwd": str(root), "scope": {"exclude": []}})
    names = []
    for name, entry in tree["files"].items():
        name.encode("utf-8", errors="strict")
        if entry.get("kind") == "file" and Path(name).suffix.lower() in SOURCE_EXTENSIONS:
            _path(root, name)
            names.append(name)
    return sorted(names)[:4096], len(names) > 4096, "snapshot_inventory"


class MatchRows(list):
    def __init__(self, rows=(), reasons=()):
        super().__init__(rows)
        self.truncation_reasons = list(reasons)


def _matches(root, names, token):
    """Narrow rg to known safe source names; never execute trace/model text."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", token):
        raise HarnessError("Symbol lookup requires a bounded identifier")
    rg = shutil.which("rg")
    result = []
    reasons = []
    if rg and names:
        # Batch argv size remains safe on Windows and does not need a shell.
        for start in range(0, len(names), 64):
            process = subprocess.run([rg, "--no-config", "--json", "-n", "-F", "-w", "--", token, *names[start:start + 64]],
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
                result.extend((name, index) for index, line in enumerate(source[0].split("\n"), 1) if pattern.search(line))
    return MatchRows(result[:8192], [*reasons, *(["global_match_cap"] if len(result) > 8192 else [])])


def query_repository(root, mode, *, revision=None, target=None, changed=(), symbol=None,
                     path=None, trace_file=None, max_bytes=8192, offset=0, use_lsp=False, lsp_server=None, cursor=None):
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
    names, partial, method = _files(root, tree)
    rows, consulted, lsp_status = [], {}, "not_requested"
    truncations = ["source_inventory_cap"] if partial else []
    inventory_digest = digest({name: tree["files"][name] for name in names})

    def matches(selected_names, token):
        found = _matches(root, selected_names, token)
        truncations.extend(found.truncation_reasons)
        return found

    def add(name, line, rank, reason, *, selected_symbol=None, end_line=None, confidence="heuristic"):
        if len(rows) >= 8192:
            truncations.append("global_row_cap")
            return
        source = consulted.get(name)
        if source is None:
            source = _source(root, name)
            consulted[name] = source
        if not source:
            truncations.append("source_unavailable_or_size_cap")
            return
        lines = source[0].split("\n")
        if not 1 <= line <= max(1, len(lines)):
            return
        rows.append({"file": name, "line": line, "end_line": end_line or line,
            "symbol": selected_symbol, "rank": rank, "reason": reason,
            "sha256": source[1], "confidence": confidence, "column_encoding": "unicode_code_point", "snippet": lines[line - 1][:512] if lines else "",
            "link": {"path": name, "line": line, "revision": actual_revision, "snapshot_id": tree["digest"]}})

    if mode == "trace-to-code":
        if not trace_file:
            raise HarnessError("trace-to-code requires trace_file")
        text = read_regular_file(safe_private(trace_file), max_bytes=1024 * 1024).decode("utf-8")
        sanitized(text)
        frames = re.findall(r'(?:File\s+["\']([^"\']+)["\'],\s*line\s+(\d+)|([\w./\\:-]+\.(?:py|rs|js|ts|tsx|cpp|c|go|java)):(\d+))', text)
        if len(frames) > 256:
            truncations.append("trace_frame_cap")
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
        for line, text in enumerate(selected[0].split("\n"), 1):
            if pattern.search(text):
                add(path, line, 100, "direct definition syntax", selected_symbol=symbol)
        for name, line in matches(names, symbol):
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
                for name, line in matches(tests if mode == "related-tests" else names, stem):
                    if name != target_name:
                        add(name, line, 80, "module/import identifier occurrence")
            if mode == "change-impact" and (root / target_name).exists():
                source = _source(root, target_name)
                if source:
                    all_tokens = re.findall(r"\b(?:def|class|fn|struct|function)\s+([A-Za-z_]\w*)", source[0])
                    if len(all_tokens) > 16:
                        truncations.append("changed_symbol_cap")
                    tokens = all_tokens[:16]
                    for token in tokens:
                        for name, line in matches(names, token):
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
    query = {"target": target, "changed": list(changed), "symbol": symbol, "path": path,
             "use_lsp": use_lsp, "trace_digest": hashlib.sha256(text.encode()).hexdigest() if mode == "trace-to-code" else None}
    binding = digest({"snapshot": tree["digest"], "inventory": inventory_digest,
                      "query": query, "engine": "repomap-query-v2", "ordered_rows": ranked})
    if cursor is not None:
        if (not isinstance(cursor, dict) or set(cursor) != {"binding", "position"}
                or cursor["binding"] != binding or type(cursor["position"]) is not int
                or not 0 <= cursor["position"] <= len(ranked)):
            raise HarnessError("STALE_CURSOR: query, inventory or ordered results changed")
        offset = cursor["position"]
    if offset > len(ranked):
        raise HarnessError("Invalid query offset")
    reasons = sorted(set(truncations))
    result = {"schema_version": 2, "mode": mode, "query": query,
        "repository_identity": repository_identity(root), "revision": actual_revision,
        "snapshot_id": tree["digest"], "method": method, "results": [],
        "total_results": None if reasons else len(ranked), "observed_results": len(ranked),
        "offset": offset, "next_page": None, "next_cursor": None,
        "inventory_scope": "snapshot_source_extensions", "inventory_digest": inventory_digest,
        "inventory_complete": not partial, "complete_inventory": not partial,
        "results_complete": not reasons, "truncated": bool(reasons), "truncation_reasons": reasons,
        "resume_supported": True, "terminal_diagnostic": "query_must_be_narrowed" if reasons else None,
        "analysis_kind": "heuristic", "lsp_status": lsp_status,
        "limitations": ["lexical and syntactic ranking; dynamic imports and semantic references may be missing"]}
    # Cursor fields are present during sizing; changing next_page to an integer
    # after selection cannot silently exceed the accepted transport bound.
    for index in range(offset, len(ranked)):
        result["results"].append(ranked[index])
        result["next_page"] = index + 1 if index + 1 < len(ranked) else None
        result["next_cursor"] = {"binding": binding, "position": index + 1} if index + 1 < len(ranked) else None
        if len(canonical(result)) + 2 > max_bytes:
            result["results"].pop()
            result["next_page"] = index
            result["next_cursor"] = {"binding": binding, "position": index}
            break
    if not result["results"] and offset < len(ranked):
        raise HarnessError("BUDGET_TOO_SMALL: a query row and continuation exceed budget")
    if len(canonical(result)) + 2 > max_bytes:
        raise HarnessError("BUDGET_TOO_SMALL: repository query envelope exceeds budget")
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
    parser.add_argument("--cursor", type=json.loads)
    parser.add_argument("--use-lsp", action="store_true")
    parser.add_argument("--lsp-server", nargs="+")
    args = vars(parser.parse_args(argv))
    value = query_repository(**args)
    from .output import write_json
    write_json(value, max_bytes=args["max_bytes"])
    return 0
