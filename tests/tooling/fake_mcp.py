"""Deterministic real stdio MCP process. No model/API credentials required."""
import json
import os
from pathlib import Path
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ready"
if mode == "startup-failure":
    raise SystemExit(23)
if mode == "timeout":
    time.sleep(30)
    raise SystemExit

def emit(ident, result):
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    ident = message.get("id")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        if mode == "handshake-failure":
            emit(ident, {"protocolVersion": "unsupported"})
        else:
            if mode == "delayed":
                time.sleep(0.1)
            emit(ident, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "harness-fixture-only", "version": "fixture-1"}})
    elif method == "tools/list":
        names = ["read_file", "write_file", "execute_command"]
        if mode != "missing-tool":
            names.insert(0, "search_symbol")
        emit(ident, {"tools": [{"name": name, "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": name in ("search_symbol", "read_file")}} for name in names]})
        if mode == "disconnect":
            raise SystemExit
    elif method == "ping":
        if mode == "ping-timeout":
            time.sleep(30)
        emit(ident, {})
    elif method == "tools/call":
        tool = message["params"]["name"]
        if tool == "write_file":
            Path("forbidden.txt").write_text("write unexpectedly dispatched")
        if tool == "execute_command":
            Path("shell-forbidden.txt").write_text("shell unexpectedly dispatched")
        content = Path("symbol.py").read_text() if tool == "search_symbol" else "fixture"
        emit(ident, {"content": [{"type": "text", "text": json.dumps({"pid": os.getpid(),
            "cwd": str(Path.cwd()), "project": sys.argv[2] if len(sys.argv) > 2 else str(Path.cwd()),
            "content": content})}]})
