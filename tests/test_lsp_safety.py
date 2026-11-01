from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_harness import lsp, rename
from agent_harness.core import HarnessError


RACE_SERVER = r'''
import json, re, sys
from pathlib import Path
root, mode, operation, traffic = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
opened, requests, symbols = {}, 0, 0
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line: raise SystemExit()
        if line == b"\r\n": break
        key, value = line.split(b":", 1); headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers[b"content-length"])))
def send(message, value):
    body = json.dumps({"jsonrpc":"2.0", "id":message["id"], "result":value}).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()
while True:
    message = read(); method = message.get("method"); params = message.get("params") or {}
    if method == "initialize":
        send(message, {"capabilities":{"documentSymbolProvider":True, "definitionProvider":True, "referencesProvider":True}})
    elif method == "textDocument/didOpen":
        opened[params["textDocument"]["uri"]] = params["textDocument"]["text"]
    elif method in ("textDocument/documentSymbol", "textDocument/definition", "textDocument/references"):
        if method == "textDocument/documentSymbol":
            symbols += 1
            if operation != "symbols" or symbols == 1:
                send(message, []); continue
        requests += 1
        target = root / ("target-%s.py" % requests if mode == "unstable" else "b.py")
        if mode == "unstable": target.write_bytes(b"target = 1\n")
        text = opened.get(target.as_uri(), target.read_bytes().decode())
        index = next(index for index, line in enumerate(text.splitlines()) if "target" in line)
        content = text.splitlines()[index]
        start = len(content[:content.index("target")].encode("utf-16-le")) // 2
        selected = {"start":{"line":index,"character":start}, "end":{"line":index,"character":start+6}}
        if requests == 1 and mode in ("race", "shorter"):
            target.write_bytes(b"target = 1\n" if mode == "shorter" else 'prefix = "😀"\r\ntarget = 1\r\n'.encode())
        if requests > 1 and mode == "late_edit": target.write_bytes(b"# editor\ntarget = 2\n")
        location = {"uri":target.as_uri(), "range":selected}
        if operation == "symbols": result = [{"name":"target", "kind":13, "location":location}]
        elif operation == "definition": result = [{"targetUri":target.as_uri(), "targetSelectionRange":selected}]
        else: result = [location]
        traffic.write_text(json.dumps({"requests":requests, "opened":list(opened)}))
        send(message, result)
    elif method == "shutdown": send(message, None)
    elif method == "exit": break
'''


class LSPSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.before = b"target = 1\n"
        for name in ("a.py", "b.py"):
            (self.root / name).write_bytes(self.before)
        self.script = self.base / "server.py"
        self.script.write_text(RACE_SERVER, encoding="utf-8")
        self.traffic = self.base / "traffic.json"

    def query(self, operation, mode="race"):
        return lsp.query(self.root, "a.py", operation=operation,
                         **({} if operation == "symbols" else {"line":1, "column":1}),
                         timeout=5, server=[sys.executable, "-I", "-B", str(self.script),
                                            str(self.root), mode, operation, str(self.traffic)])

    def plan(self):
        sources = {name: rename.capture(self.root, name) for name in ("a.py", "b.py")}
        edit = {"range":{"start":{"line":0,"character":0},"end":{"line":0,"character":6}}, "newText":"renamed"}
        return rename.make_plan(self.root, sources, {name:[edit] for name in sources}, request={}, server={})

    def cleanup_recovery(self, result):
        if result.get("recovery_receipt"):
            receipt = Path(result["recovery_receipt"])
            journal = json.loads(receipt.read_text())
            for row in journal["files"]:
                for key in ("backup", "staged"):
                    path = Path(row[key])
                    self.addCleanup(path.unlink, missing_ok=True)
            self.addCleanup(shutil.rmtree, receipt.parent, True)

    def test_closed_navigation_targets_are_opened_and_requeried_before_ranges_are_bound(self):
        for operation in ("definition", "references", "symbols"):
            for mode in ("race", "shorter"):
                with self.subTest(operation=operation, mode=mode):
                    (self.root / "b.py").write_bytes(b"\n\n\ntarget = 1\n" if mode == "shorter" else self.before)
                    result = self.query(operation, mode)
                    item = result["items"][0]
                    text = (self.root / item["path"]).read_bytes().decode()
                    start, end = item["range"]["start"], item["range"]["end"]
                    selected = text.splitlines()[start["line"]-1][start["column"]-1:end["column"]-1]
                    self.assertEqual("target", selected)
                    self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), item["sha256"])
                    traffic = json.loads(self.traffic.read_text())
                    self.assertEqual(2, traffic["requests"])
                    self.assertIn((self.root / "b.py").as_uri(), traffic["opened"])

    def test_later_navigation_edit_and_unstable_target_set_fail_closed(self):
        for operation in ("definition", "references", "symbols"):
            with self.subTest(operation=operation):
                (self.root / "b.py").write_bytes(self.before)
                with self.assertRaisesRegex(HarnessError, "source changed"):
                    self.query(operation, "late_edit")
                with self.assertRaisesRegex(HarnessError, "did not stabilize"):
                    self.query(operation, "unstable")
                self.assertEqual(4, json.loads(self.traffic.read_text())["requests"])

    def test_navigation_snapshot_file_byte_and_deadline_limits_remain_enforced(self):
        with patch.object(lsp, "MAX_FILES", 1), self.assertRaisesRegex(HarnessError, "file/byte"):
            self.query("definition")
        with patch.object(lsp, "MAX_TOTAL_BYTES", len(self.before)), self.assertRaisesRegex(HarnessError, "file/byte"):
            self.query("references")
        sources = lsp.Sources(self.root, rename.capture(self.root, "a.py"))
        sources.deadline = time.monotonic() - 1
        with patch.object(lsp, "capture") as capture, self.assertRaisesRegex(HarnessError, "deadline"):
            sources.get("b.py")
        capture.assert_not_called()

    def test_corrupt_or_replaced_backup_never_claims_compensation(self):
        for mutation in ("bytes", "inode", "symlink", "fifo"):
            if mutation == "fifo" and not hasattr(os, "mkfifo"):
                continue
            with self.subTest(mutation=mutation):
                for name in ("a.py", "b.py"):
                    (self.root / name).write_bytes(self.before)
                plan, stage, replace, backups = self.plan(), rename.Parent.stage, rename.Parent.replace, {}
                def remember(parent, *args, **options):
                    value = stage(parent, *args, **options)
                    backups.setdefault(parent.name, parent.path.parent / value[0])
                    return value
                def fail_second(parent, staged, **options):
                    if parent.name == "b.py":
                        backup = backups["a.py"]
                        if mutation == "bytes":
                            backup.write_bytes(b"CORRUPTED BACKUP\n")
                        elif mutation == "inode":
                            replacement = self.root / "replacement"
                            replacement.write_bytes(self.before)
                            os.replace(replacement, backup)
                        else:
                            backup.unlink()
                            if mutation == "fifo":
                                os.mkfifo(backup)
                            else:
                                try:
                                    backup.symlink_to(self.root / "b.py")
                                except OSError:
                                    self.skipTest("Symlink privilege unavailable")
                        raise OSError("injected second-file write failure")
                    return replace(parent, staged, **options)
                with patch.object(rename.Parent, "stage", remember), patch.object(rename.Parent, "replace", fail_second):
                    result = rename.apply_plan(self.root, plan)
                self.cleanup_recovery(result)
                self.assertEqual("recovery_required", result["status"])
                self.assertEqual(b"renamed = 1\n", (self.root / "a.py").read_bytes())
                receipt = json.loads(Path(result["recovery_receipt"]).read_text())
                self.assertEqual("recovery_required", receipt["files"][0]["state"])

    def test_restoration_is_read_back_before_journal_claims_restored(self):
        plan, replace = self.plan(), rename.Parent.replace
        def fail_and_change_restored(parent, staged, **options):
            if parent.name == "b.py":
                raise OSError("second-file failure")
            value = replace(parent, staged, **options)
            if options.get("staged_source") is not None:
                parent.path.write_bytes(b"# concurrent editor after restoration\n")
            return value
        with patch.object(rename.Parent, "replace", fail_and_change_restored):
            result = rename.apply_plan(self.root, plan)
        self.cleanup_recovery(result)
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(b"# concurrent editor after restoration\n", (self.root / "a.py").read_bytes())
        receipt = json.loads(Path(result["recovery_receipt"]).read_text())
        self.assertEqual("recovery_required", receipt["files"][0]["state"])

    def test_small_cli_budget_preserves_real_outcome_receipt_digest_and_counts(self):
        plan, replace = self.plan(), rename.Parent.replace
        plan_path = self.base / "plan.json"
        plan_path.write_text(json.dumps(plan))
        def fail_second_and_restore(parent, staged, **options):
            if parent.name == "b.py" or options.get("staged_source") is not None:
                raise OSError("无法写入文件：" * 60)
            return replace(parent, staged, **options)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(rename.Parent, "replace", fail_second_and_restore), redirect_stdout(stdout), redirect_stderr(stderr):
            code = lsp.dispatch(["apply", "--root", str(self.root), "--plan", str(plan_path), "--max-bytes", "1024"])
        result = json.loads(stdout.getvalue())
        self.cleanup_recovery(result)
        self.assertEqual(2, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(plan["digest"], result["plan_digest"])
        self.assertEqual((2, 2, 1), (result["files"], result["edits"], result["conflict_count"]))
        self.assertTrue(Path(result["recovery_receipt"]).is_file())
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(stdout.getvalue().encode()) + 1, 1024)

    def test_impossible_receipt_budget_is_rejected_before_staging_or_source_writes(self):
        plan, mkdtemp, long_dir = self.plan(), tempfile.mkdtemp, self.base
        for _ in range(6):
            long_dir /= "long-" + "x" * 120
        try:
            long_dir.mkdir(parents=True)
        except OSError:
            if os.name == "nt":
                self.skipTest("Long filesystem paths unavailable")
            raise
        created = []
        def make_receipt_dir(**options):
            path = mkdtemp(dir=long_dir, **options)
            created.append(Path(path))
            return path
        with patch.object(rename.tempfile, "mkdtemp", make_receipt_dir), patch.object(rename.Parent, "stage") as stage:
            with self.assertRaisesRegex(HarnessError, "recovery receipt exceed"):
                rename.apply_plan(self.root, plan, result_budget=1024)
        stage.assert_not_called()
        self.assertEqual(self.before, (self.root / "a.py").read_bytes())
        self.assertEqual(self.before, (self.root / "b.py").read_bytes())
        self.assertFalse(created[0].exists())

    def test_apply_budget_positive_boundary_keeps_complete_minimum(self):
        result = {"status":"recovery_required", "operation":"apply", "plan_digest":"a" * 64,
                  "recovery_receipt":"/tmp/" + "路径" * 160, "files":256, "edits":10000,
                  "conflicts":[{"error":"\x01" * 300}], "error":"无法写入" * 300}
        output = lsp.bounded(result, 2048)
        for key in ("status", "plan_digest", "recovery_receipt", "files", "edits"):
            self.assertEqual(result[key], output[key])
        self.assertLessEqual(len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode()) + 2, 2048)

    def test_unencodable_optional_error_does_not_hide_apply_outcome(self):
        result = {"status":"recovery_required", "operation":"apply", "plan_digest":"a" * 64,
                  "recovery_receipt":"/tmp/receipt.json", "files":1, "edits":1,
                  "conflicts":[{"error":"\udcff"}], "error":"\udcff"}
        output = lsp.bounded(result, 1024)
        self.assertEqual("recovery_required", output["status"])
        self.assertEqual(result["recovery_receipt"], output["recovery_receipt"])
        self.assertEqual(1, output["conflict_count"])
        self.assertLessEqual(len(json.dumps(output, ensure_ascii=False).encode()), 1024)


if __name__ == "__main__":
    unittest.main()
