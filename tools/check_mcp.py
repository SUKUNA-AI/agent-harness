"""Required optional-extra smoke: real SDK v2 client/server over stdio.

Uses only an isolated dummy store. Missing SDK is failure, never a skipped pass.
"""
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback


async def roundtrip(store):
    from mcp import Client, StdioServerParameters

    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable,
                                   args=["-I", "-B", str(root / "tools/harness_mcp.py"), "--store", str(store)],
                                   env={"PYTHONDONTWRITEBYTECODE": "1"})
    async with Client(params) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {"list_runs", "run_status"}
        assert all(tool.annotations.read_only_hint and not tool.annotations.open_world_hint for tool in tools.tools)
        listed = await client.call_tool("list_runs", {"limit": 1})
        status = await client.call_tool("run_status", {"run_id": "dummy"})
        assert not listed.is_error and not status.is_error
        assert listed.structured_content["runs"][0]["run_id"] == "dummy"
        assert status.structured_content["checks"][0]["recorded_status"] == "passed"
        assert status.structured_content["evidence_freshness"] == "not_checked"
        assert "PRIVATE_" not in json.dumps([listed.structured_content, status.structured_content])
        bad = await client.call_tool("run_status", {"run_id": "../outside"})
        assert bad.is_error
        assert "PRIVATE_" not in str(bad)
        assert "outside" not in str(bad)
        print(json.dumps({"result": "passed", "transport": "stdio", "sdk_version": version("mcp"),
                          "protocol_version": client.protocol_version, "tools": sorted(tool.name for tool in tools.tools),
                          "valid_calls": 2, "negative_calls": 1, "fixture_only": True}))


def main():
    try:
        if version("mcp") != "2.3.0":
            raise RuntimeError("This smoke requires the pinned mcp==2.3.0 optional extra")
        temp_root = os.environ.get("HARNESS_TEST_TMP")
        if temp_root:
            Path(temp_root).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="harness-mcp-smoke-", dir=temp_root) as directory:
            store = Path(directory) / "store"
            check_dir = store / "dummy" / "attempts" / "a1" / "checks" / "unit"
            check_dir.mkdir(parents=True)
            (store / "dummy" / "state.json").write_text(json.dumps({
                "schema_version": 1, "run_id": "dummy", "task_id": "fixture", "status": "ready_for_review",
                "current_attempt": "a1", "owners": {"owner": {"lane_id": "lane", "status": "done", "observation": "PRIVATE_OWNER"}},
                "candidate_digest": "a" * 64, "goal": "PRIVATE_PROMPT", "credentials": "PRIVATE_TOKEN"}), encoding="utf-8")
            (check_dir / "result.json").write_text(json.dumps({
                "check_id": "unit", "attempt_id": "a1", "status": "passed", "candidate_digest": "a" * 64,
                "stdout": "PRIVATE_LOG", "argv": ["PRIVATE_COMMAND"]}), encoding="utf-8")
            before = {str(p.relative_to(store)): p.read_bytes() for p in store.rglob("*") if p.is_file()}
            asyncio.run(asyncio.wait_for(roundtrip(store), timeout=30))
            after = {str(p.relative_to(store)): p.read_bytes() for p in store.rglob("*") if p.is_file()}
            assert before == after, "Server changed the dummy run store"
    except Exception as error:
        print(f"MCP smoke failed: {type(error).__name__}: {error}", file=sys.stderr)
        traceback.print_exception(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
