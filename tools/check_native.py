"""Required compiled-helper smoke: parity, real gate, and task-relevant RepoMap.

Missing native code is a failure. This is a small integration gate, not a second
copy of the Python test suite. All fixtures and evidence are temporary.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_harness.core import HarnessError, Run, canonical, snapshot, snapshot_python
from agent_harness.native import backend_info, invoke
from agent_harness.repo_map import make_map
from agent_harness.runner import gate, run_check


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()


def parity(contract):
    expected = snapshot_python(contract)
    observed = invoke({"operation": "snapshot", "contract": contract})
    assert canonical(observed) == canonical(expected), "Rust/Python manifest or digest differs"
    return observed


def main():
    # This command explicitly qualifies compiled execution, irrespective of the
    # size-based auto selector used by ordinary small-workspace operations.
    os.environ["AGENT_HARNESS_BACKEND"] = "native"
    info = backend_info()
    assert info["selected"] == "native" and info["available"], "Compiled native backend is mandatory"
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="harness-native-check-") as temp:
        base = Path(temp).resolve()
        source = base / "Проект with spaces"
        source.mkdir()
        (source / "main.py").write_text("def find_user():\n    return 7\n", encoding="utf-8")
        (source / "use.py").write_text("from main import find_user\n\ndef serve():\n    return find_user()\n", encoding="utf-8")
        (source / "ignored").mkdir()
        (source / "ignored" / "payload.txt").write_text("ignored", encoding="utf-8")
        (source / "[literal].txt").write_text("λ\r\n", encoding="utf-8", newline="")
        external = base / "input.txt"
        external.write_text("frozen input", encoding="utf-8")
        contract = {"schema_version": 1, "run_id": "native-smoke", "task_id": "native-smoke",
                    "attempt_id": "a1", "workflow": "change", "process_level": "standard",
                    "goal": "Verify compiled backend with real command and gate", "cwd": str(source),
                    "permissions": ["read", "write", "test"],
                    "scope": {"write_paths": ["main.py"], "exclude": ["ignored/**", "__pycache__/**"]},
                    "inputs": [{"path": str(external)}],
                    "checks": [{"id": "unit", "kind": "command", "required": True,
                                "argv": [sys.executable, "-I", "-B", "-c", "print('native fixture passed')"],
                                "timeout": 10, "expected_exit": 0,
                                "assertions": [{"stream": "stdout", "regex": "native fixture passed"}]}],
                    "budget": {"max_attempts": 2, "native_cap": 1, "root_counts": False,
                               "native_cap_source": "isolated integration fixture"}}
        parity({})
        parity(contract)
        outcomes.append("non_git_unicode_external_exact_manifest")
        git(source, "init", "-q")
        git(source, "config", "user.email", "native-fixture@example.invalid")
        git(source, "config", "user.name", "Native Fixture")
        git(source, "config", "core.autocrlf", "false")
        git(source, "config", "core.filemode", "false")
        (source / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (source / "deleted.txt").write_text("tracked deletion", encoding="utf-8")
        git(source, "add", ".")
        git(source, "commit", "-qm", "fixture")
        (source / "deleted.txt").unlink()
        (source / "dirty.txt").write_text("new bytes", encoding="utf-8")
        observed = parity(contract)
        assert observed["files"]["deleted.txt"] == {"kind": "missing"}
        outcomes.append("git_dirty_untracked_deleted_exact_manifest")
        submodule = source / "submodule"
        submodule.mkdir()
        (submodule / "child.py").write_text("def child():\n    return 1\n", encoding="utf-8")
        (submodule / ".git").write_text("ignored metadata", encoding="utf-8")
        git(source, "update-index", "--add", "--cacheinfo", f"160000,{git(source, 'rev-parse', 'HEAD')},submodule")
        assert "submodule/child.py" in parity(contract)["files"]
        outcomes.append("gitlink_actual_tree_exact_manifest")
        try:
            (source / "link").symlink_to("main.py")
        except OSError:
            outcomes.append("symlink_creation_unavailable")
        else:
            assert parity(contract)["files"]["link"]["kind"] == "symlink"
            outcomes.append("symlink_exact_manifest")
        if os.name == "posix":
            normal = source / "collision" / "unit.py"
            normal.parent.mkdir()
            literal_name = "collision" + chr(92) + "unit.py"
            literal = source / literal_name
            normal.write_text("def normal(): return 1\n", encoding="utf-8")
            literal.write_text("def literal(): return 2\n", encoding="utf-8")
            initial = parity(contract)
            assert initial["files"]["collision/unit.py"]["sha256"] != initial["files"][literal_name]["sha256"]
            literal.write_text("def literal(): return 3\n", encoding="utf-8")
            changed = parity(contract)
            assert initial["digest"] != changed["digest"]
            assert initial["files"]["collision/unit.py"] == changed["files"]["collision/unit.py"]
            normal.write_text("def normal(): return 4\n", encoding="utf-8")
            assert changed["digest"] != parity(contract)["digest"]
            outcomes.append("git_literal_backslash_never_aliases_directory_separator")
        run = Run.create(base / "run", contract)
        run.freeze()
        assert run_check(run, "unit")["status"] == "passed"
        assert gate(run)["verdict"] == "accepted"
        assert run.status(verify_evidence=True)["evidence_current"] is True
        (source / "main.py").write_text("def find_user():\n    return 8\n", encoding="utf-8")
        try:
            gate(run)
        except HarnessError as error:
            assert "Stale candidate" in str(error)
        else:
            raise AssertionError("Changed source reused prior acceptance")
        outcomes.append("real_command_accept_and_reject_stale_candidate")

        mapped = make_map(source, query="find_user", max_bytes=8192)
        if os.name == "posix":
            assert mapped["scan"]["partial"] and mapped["scan"]["skipped"]["denied_path"] >= 1
        assert any(row["name"] == "find_user" and row["path"] == "main.py" for row in mapped["symbols"]), mapped
        assert any(row["path"] == "use.py" for row in mapped["neighbors"] + mapped["symbols"]), mapped
        before = mapped["revision"]["content_digest"]
        (source / "main.py").write_text("def find_user():\n    return 9\n", encoding="utf-8")
        after = make_map(source, query="find_user", max_bytes=8192)
        assert before != after["revision"]["content_digest"], "Map binding ignored uncommitted changes"
        assert mapped["revision"]["git_head"] == after["revision"]["git_head"]
        outcomes.append("task_symbols_neighbors_and_working_tree_binding")
        (source / "zz_focus.py").write_text("def priority_symbol():\n    return 1\n", encoding="utf-8")
        bounded = make_map(source, query="priority_symbol", focus_files=["zz_focus.py"], max_files=1, max_bytes=2048)
        assert any(row["name"] == "priority_symbol" for row in bounded["symbols"]), bounded
        assert bounded["scan"]["partial"] is True
        stream = io.BytesIO()
        writer = io.TextIOWrapper(stream, encoding="utf-8", newline="\r\n")
        print(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")), file=writer)
        writer.flush()
        assert len(stream.getvalue()) <= 2048, "Map exceeds actual Windows output budget"
        outcomes.append("focus_priority_partial_scope_and_crlf_byte_budget")

        glob_root = base / "glob-parity"
        glob_root.mkdir()
        (glob_root / "a").write_bytes(b"before")
        (glob_root / "x").write_bytes(b"other")
        glob_contract = {"cwd": str(glob_root), "scope": {"exclude": ["[a--!a]"]}}
        first = parity(glob_contract)
        assert set(first["files"]) == {"a"}
        (glob_root / "a").write_bytes(b"after")
        assert parity(glob_contract)["digest"] != first["digest"]
        outcomes.append("normalized_glob_scope_detects_changed_included_file")

        if os.name == "posix":
            whitespace_root = base / "git-root \t\n"
            whitespace_root.mkdir()
            git(whitespace_root, "init", "-q")
            (whitespace_root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (whitespace_root / "tracked.txt").write_text("tracked", encoding="utf-8")
            (whitespace_root / "ignored.txt").write_text("ignored", encoding="utf-8")
            git(whitespace_root, "add", ".gitignore", "tracked.txt")
            (whitespace_root / "tracked.txt").unlink()
            whitespace = parity({"cwd": str(whitespace_root)})
            assert whitespace["kind"] == "git"
            assert whitespace["files"]["tracked.txt"] == {"kind": "missing"}
            assert "ignored.txt" not in whitespace["files"]
            outcomes.append("git_root_trailing_whitespace_keeps_inventory")
    print(json.dumps({"status": "passed", "backend": info["selected"], "platform": info["platform"],
                      "checks": outcomes}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
