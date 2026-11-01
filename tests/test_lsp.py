from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_harness import lsp, rename
from agent_harness.core import HarnessError, digest, identity_alive, process_identity


FAKE_SERVER = r'''
import json, os, re, subprocess, sys, time
from pathlib import Path
root, mode, receipt = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
receipt.write_text(json.dumps({"pid": os.getpid()}))
opened = {}
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line: raise SystemExit()
        if line == b"\r\n": break
        k,v = line.split(b":",1); headers[k.lower()] = v.strip()
    size = int(headers[b"content-length"])
    parts = bytearray()
    while len(parts)<size:
        chunk=sys.stdin.buffer.read(size-len(parts))
        if not chunk: raise SystemExit()
        parts.extend(chunk)
    return json.loads(parts)
def send(value):
    body=json.dumps(value,ensure_ascii=False,separators=(",",":")).encode()
    frame=b"Content-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body
    for start in range(0,len(frame),17):
        sys.stdout.buffer.write(frame[start:start+17]); sys.stdout.buffer.flush()
def respond(message,value): send({"jsonrpc":"2.0","id":message["id"],"result":value})
def ranges(name):
    uri=(root/name).as_uri()
    text=opened.get(uri)
    if text is None:
        text=(root/name).read_bytes().decode()
    answer=[]
    for index,line in enumerate(re.split(r"\r\n|\r|\n",text)):
        for found in re.finditer(r"target",line):
            start=len(line[:found.start()].encode("utf-16-le"))//2
            answer.append({"start":{"line":index,"character":start},"end":{"line":index,"character":start+6}})
    return answer
while True:
    message=read(); method=message.get("method"); params=message.get("params") or {}
    if method=="initialize":
        if mode in ("timeout","child_timeout"):
            if mode=="child_timeout":
                child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])
                receipt.write_text(json.dumps({"pid":os.getpid(),"child":child.pid}))
            time.sleep(30)
        if mode=="bad_frame":
            sys.stdout.buffer.write(b"Content-Length: 999999999\r\n\r\n");sys.stdout.buffer.flush();time.sleep(30)
        if mode=="stderr":
            sys.stderr.buffer.write(b"x"*262144);sys.stderr.buffer.flush()
        send({"jsonrpc":"2.0","id":"configuration","method":"workspace/configuration","params":{"items":[{"section":"test.flag"}]}})
        config=read()
        if config.get("result")!=[True]: raise RuntimeError("configuration handshake failed")
        respond(message,{"capabilities":{"positionEncoding":"utf-8" if mode=="encoding" else "utf-16", "documentSymbolProvider":True,"definitionProvider":True,"referencesProvider":True,"renameProvider":True},"serverInfo":{"name":"fixture","version":"1"}})
        if mode=="block_stdin": time.sleep(30)
    elif method=="textDocument/didOpen": opened[params["textDocument"]["uri"]]=params["textDocument"]["text"]
    elif method=="textDocument/documentSymbol":
        send({"jsonrpc":"2.0","method":"$/progress","params":{"token":"index","value":{"kind":"begin","title":"Index"}}})
        selected=ranges("a.py")[0]
        items=[{"name":"target","kind":13,"range":selected,"selectionRange":selected}]
        if mode=="many": items*=100
        respond(message,items)
        send({"jsonrpc":"2.0","method":"$/progress","params":{"token":"index","value":{"kind":"end"}}})
    elif method=="textDocument/definition":
        saved=json.loads(receipt.read_text()); saved["position"]=params["position"];receipt.write_text(json.dumps(saved))
        selected=ranges("a.py")[0]
        respond(message,[{"targetUri":(root/"a.py").as_uri(),"targetRange":selected,"targetSelectionRange":selected}])
    elif method=="textDocument/references":
        send({"jsonrpc":"2.0","id":"write-refused","method":"workspace/applyEdit","params":{"edit":{}}})
        reply=read()
        if reply.get("result",{}).get("applied") is not False: raise RuntimeError("unreviewed write permitted")
        items=[{"uri":(root/name).as_uri(),"range":selected} for name in ("a.py","b.py") for selected in ranges(name)]
        items.append({"uri":(root.parent/"outside.py").as_uri(),"range":ranges("a.py")[0]})
        respond(message,items)
    elif method=="textDocument/rename":
        if mode=="resources": respond(message,{"documentChanges":[{"kind":"delete","uri":(root/"a.py").as_uri()}]})
        else:
            changes={ (root/name).as_uri():[{"range":r,"newText":params["newName"]} for r in ranges(name)] for name in ("a.py","b.py") }
            respond(message,{"changes":changes})
    elif method=="shutdown": respond(message,None)
    elif method=="exit": raise SystemExit()
'''


class LSPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.a = 'emoji = "😀"; target = 1\r\nvalue = target\r\n'.encode()
        self.b = b"from a import target\r\nother = target\r\n"
        (self.root / "a.py").write_bytes(self.a)
        (self.root / "b.py").write_bytes(self.b)
        self.script = self.base / "server.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.receipt = self.base / "server.json"

    def server(self, mode="normal"):
        return [sys.executable, "-I", "-B", str(self.script), str(self.root), mode, str(self.receipt)]

    def query(self, operation="symbols", **options):
        if operation != "symbols":
            options.setdefault("line", 1)
            options.setdefault("column", len(self.a.decode().split("target")[0]) + 1)
        return lsp.query(self.root, "a.py", operation=operation,
                         server=self.server(options.pop("mode", "normal")),
                         settings={"test": {"flag": True}}, **options)

    def plan(self):
        sources = {name: rename.capture(self.root, name) for name in ("a.py", "b.py")}
        changes = {}
        for name, source in sources.items():
            edits = []
            for line, content in enumerate(source["text"].splitlines()):
                start = content.find("target")
                if start >= 0:
                    units = len(content[:start].encode("utf-16-le")) // 2
                    edits.append({"range": {"start": {"line": line, "character": units},
                                             "end": {"line": line, "character": units + 6}}, "newText": "renamed"})
            changes[name] = edits
        return rename.make_plan(self.root, sources, changes, request={}, server={"name": "fixture"})

    def cleanup_receipt(self, result):
        if result.get("recovery_receipt"):
            self.addCleanup(shutil.rmtree, Path(result["recovery_receipt"]).parent, True)

    def test_utf16_positions_preserve_crlf_bom_and_reject_split_surrogates(self):
        positions = rename.Positions("\ufeffa😀b\r\nx\r\n")
        self.assertEqual({"line": 0, "character": 4}, positions.from_human(1, 4))
        self.assertEqual(8, positions.offset({"line": 0, "character": 4}))
        self.assertEqual({"line": 1, "column": 4}, positions.human({"line": 0, "character": 4}))
        self.assertEqual(14, positions.offset({"line": 2, "character": 0}))
        with self.assertRaisesRegex(HarnessError, "surrogate"):
            positions.offset({"line": 0, "character": 3})
        with self.assertRaises(HarnessError):
            positions.offset({"line": 1, "character": 2})

    def test_navigation_real_protocol_configuration_links_and_unicode(self):
        symbols = self.query()
        self.assertEqual("target", symbols["items"][0]["name"])
        result = self.query("definition")
        observed = json.loads(self.receipt.read_text())
        prefix = self.a.decode().split("target")[0]
        self.assertEqual(len(prefix.encode("utf-16-le")) // 2, observed["position"]["character"])
        self.assertEqual(len(prefix) + 1, result["items"][0]["range"]["start"]["column"])
        references = self.query("references")
        self.assertEqual(4, references["total_items"])
        self.assertEqual(1, references["outside_workspace_omitted"])
        self.assertEqual({"a.py", "b.py"}, {item["path"] for item in references["items"]})
        self.assertFalse(identity_alive(process_identity(observed["pid"])))

    def test_complete_rename_preview_saved_outside_then_applied(self):
        output = self.base / "plan.json"
        result = self.query("rename", new_name="renamed", output=output, max_bytes=1024)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["plan_included"])
        self.assertNotIn("plan", result)
        self.assertEqual(self.a, (self.root / "a.py").read_bytes())
        plan = rename.read_plan(output)
        self.assertEqual((2, 4), (plan["file_count"], plan["edit_count"]))
        applied = rename.apply_plan(self.root, plan)
        self.assertEqual("applied", applied["status"])
        self.assertEqual(self.a.replace(b"target", b"renamed"), (self.root / "a.py").read_bytes())
        self.assertEqual(self.b.replace(b"target", b"renamed"), (self.root / "b.py").read_bytes())
        self.assertFalse(any(self.root.glob(".agent-harness-rename-*")))

    def test_preview_does_not_overwrite_existing_plan_or_write_inside_workspace(self):
        output = self.base / "plan.json"
        output.write_text("keep")
        with self.assertRaises(FileExistsError):
            self.query("rename", new_name="renamed", output=output)
        self.assertEqual("keep", output.read_text())
        with self.assertRaisesRegex(HarnessError, "outside"):
            self.query("rename", new_name="renamed", output=self.root / "plan.json")

    def test_unsupported_resource_operation_and_encoding_fail_closed(self):
        for mode, operation in (("resources", "rename"), ("encoding", "symbols")):
            with self.subTest(mode=mode), self.assertRaises(HarnessError):
                self.query(operation, mode=mode, **({"new_name": "renamed"} if operation == "rename" else {}))
        self.assertEqual(self.a, (self.root / "a.py").read_bytes())

    def test_timeout_and_malformed_frame_reap_processes(self):
        for mode in ("timeout", "bad_frame", "block_stdin"):
            started = time.monotonic()
            with self.subTest(mode=mode), self.assertRaises(HarnessError):
                self.query(mode=mode, timeout=0.3)
            self.assertLess(time.monotonic() - started, 3)
            pid = json.loads(self.receipt.read_text())["pid"]
            self.assertIsNone(process_identity(pid))

    def test_timeout_closes_owned_child_tree(self):
        with self.assertRaises(HarnessError):
            self.query(mode="child_timeout", timeout=0.3)
        observed = json.loads(self.receipt.read_text())
        self.assertIsNone(process_identity(observed["pid"]))
        deadline = time.monotonic() + 2
        while process_identity(observed["child"]) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(process_identity(observed["child"]))

    def test_stderr_drained_without_unbounded_output(self):
        result = self.query(mode="stderr")
        self.assertEqual(262144, result["stderr_bytes"])
        self.assertTrue(result["stderr_truncated"])
        self.assertNotIn("x" * 100, json.dumps(result))

    def test_timeout_cleans_up_when_server_does_not_read_large_stdin_frame(self):
        (self.root / "a.py").write_bytes(self.a + b"#" * 300000)
        started = time.monotonic()
        with self.assertRaises(HarnessError):
            self.query(mode="block_stdin", timeout=0.75)
        self.assertLess(time.monotonic() - started, 4)
        self.assertIsNone(process_identity(json.loads(self.receipt.read_text())["pid"]))

    def test_output_budget_counts_json_escaping_and_crlf(self):
        result = self.query(mode="many", max_bytes=1024)
        self.assertEqual(100, result["total_items"])
        self.assertLess(result["returned_items"], result["total_items"])
        output = io.BytesIO()
        wrapper = io.TextIOWrapper(output, encoding="utf-8", newline="\r\n")
        with redirect_stdout(wrapper):
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        wrapper.flush()
        self.assertLessEqual(len(output.getvalue()), 1024)
        self.assertTrue(output.getvalue().endswith(b"\r\n"))
        wrapper.detach()

    def test_unavailable_is_actionable_without_fake_semantic_fallback(self):
        with patch("agent_harness.lsp.shutil.which", return_value=None):
            with self.assertRaisesRegex(HarnessError, "No local LSP.*auto-installed"):
                lsp.query(self.root, "a.py")
            inventory = lsp.doctor(self.root)
        self.assertEqual(0, inventory["servers_started"])
        self.assertFalse(any(item["available"] for item in inventory["items"]))

    def test_stale_content_and_same_content_replaced_inode_are_rejected(self):
        plan = self.plan()
        (self.root / "b.py").write_bytes(self.b + b"# user\r\n")
        with self.assertRaisesRegex(HarnessError, "changed"):
            rename.apply_plan(self.root, plan)
        self.assertEqual(self.a, (self.root / "a.py").read_bytes())
        (self.root / "b.py").write_bytes(self.b)
        plan = self.plan()
        temp = self.root / "replacement"
        temp.write_bytes(self.a)
        os.replace(temp, self.root / "a.py")
        with self.assertRaisesRegex(HarnessError, "changed"):
            rename.apply_plan(self.root, plan)

    def test_digest_workspace_overlap_and_unsupported_byte_ranges_rejected(self):
        plan = self.plan()
        plan["files"][0]["edits"][0]["text"] = "tampered"
        with self.assertRaisesRegex(HarnessError, "integrity"):
            rename.apply_plan(self.root, plan)
        plan = self.plan()
        with self.assertRaisesRegex(HarnessError, "workspace"):
            rename.apply_plan(self.base, plan)
        with self.assertRaisesRegex(HarnessError, "Overlapping"):
            rename.edited_bytes(b"abc", [{"start": 0, "end": 2, "text": "x"}, {"start": 1, "end": 3, "text": "y"}])
        with self.assertRaisesRegex(HarnessError, "UTF-8"):
            rename.edited_bytes("😀".encode(), [{"start": 1, "end": 2, "text": "x"}])

    def test_modes_ownership_and_bom_survive_apply(self):
        content = b"\xef\xbb\xbf" + self.a
        (self.root / "a.py").write_bytes(content)
        if os.name == "posix":
            (self.root / "a.py").chmod(0o751)
        before = (self.root / "a.py").stat()
        result = rename.apply_plan(self.root, self.plan())
        self.assertEqual("applied", result["status"])
        after = (self.root / "a.py").stat()
        self.assertEqual(stat.S_IMODE(before.st_mode), stat.S_IMODE(after.st_mode))
        self.assertEqual((before.st_uid, before.st_gid), (after.st_uid, after.st_gid))
        self.assertEqual(content.replace(b"target", b"renamed"), (self.root / "a.py").read_bytes())

    def test_observed_symlink_and_hardlink_are_never_replaced(self):
        plan = self.plan()
        target = self.root / "a.py"
        outside = self.base / "outside.py"
        outside.write_bytes(self.a)
        target.unlink()
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink privilege unavailable")
        with self.assertRaises(HarnessError):
            rename.apply_plan(self.root, plan)
        self.assertEqual(self.a, outside.read_bytes())
        target.unlink()
        os.link(outside, target)
        with self.assertRaisesRegex(HarnessError, "hard-linked"):
            self.plan()

    @unittest.skipUnless(os.name == "posix" and os.rename in os.supports_dir_fd, "POSIX anchored directory race")
    def test_swapped_parent_symlink_does_not_redirect_replacement_or_cleanup(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "a.py").write_bytes(self.a)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "a.py").write_bytes(b"outside must stay intact")
        source = rename.capture(self.root, "nested/a.py")
        selected = {"start": {"line": 1, "character": 8}, "end": {"line": 1, "character": 14}}
        plan = rename.make_plan(self.root, {"nested/a.py": source},
                                {"nested/a.py": [{"range": selected, "newText": "renamed"}]}, request={}, server={})
        original, staged = rename.Parent.stage, 0
        def swap_after_staging(parent, data, metadata):
            nonlocal staged
            result = original(parent, data, metadata)
            staged += 1
            if staged == 2:
                nested.rename(self.root / "old-parent")
                nested.symlink_to(outside, target_is_directory=True)
            return result
        with patch.object(rename.Parent, "stage", swap_after_staging):
            result = rename.apply_plan(self.root, plan)
        self.cleanup_receipt(result)
        self.assertEqual("failed_compensated", result["status"])
        self.assertEqual(b"outside must stay intact", (outside / "a.py").read_bytes())
        self.assertEqual(self.a, (self.root / "old-parent" / "a.py").read_bytes())
        self.assertFalse(any((self.root / "old-parent").glob(".agent-harness-rename-*")))

    def test_partial_io_failure_compensates_first_file(self):
        plan, original = self.plan(), rename.Parent.replace
        def fail_second(parent, staged, **options):
            if parent.name == "b.py":
                raise OSError("injected replace failure")
            return original(parent, staged, **options)
        with patch.object(rename.Parent, "replace", fail_second):
            result = rename.apply_plan(self.root, plan)
        self.cleanup_receipt(result)
        self.assertEqual("failed_compensated", result["status"])
        self.assertEqual(self.a, (self.root / "a.py").read_bytes())
        self.assertEqual(self.b, (self.root / "b.py").read_bytes())

    def test_error_after_successful_syscall_is_compensated(self):
        plan, original = self.plan(), rename.Parent.replace
        injected = False
        def fail_after(parent, staged, **options):
            nonlocal injected
            result = original(parent, staged, **options)
            if not injected:
                injected = True
                raise OSError("injected after replacement")
            return result
        with patch.object(rename.Parent, "replace", fail_after):
            result = rename.apply_plan(self.root, plan)
        self.cleanup_receipt(result)
        self.assertEqual("failed_compensated", result["status"])
        self.assertEqual(self.a, (self.root / "a.py").read_bytes())

    def test_concurrent_edit_before_second_file_preserved_and_first_restored(self):
        plan, original = self.plan(), rename.Parent.replace
        changed = False
        user = self.b + b"# concurrent user edit\r\n"
        def edit_next(parent, staged, **options):
            nonlocal changed
            result = original(parent, staged, **options)
            if not changed:
                changed = True
                (self.root / "b.py").write_bytes(user)
            return result
        with patch.object(rename.Parent, "replace", edit_next):
            result = rename.apply_plan(self.root, plan)
        self.cleanup_receipt(result)
        self.assertEqual("failed_compensated", result["status"])
        self.assertEqual(self.a, (self.root / "a.py").read_bytes())
        self.assertEqual(user, (self.root / "b.py").read_bytes())

    def test_concurrent_edit_at_final_replace_check_is_preserved(self):
        plan, original = self.plan(), rename.Parent.replace
        user = self.a + b"# user\r\n"
        def edit_before(parent, staged, **options):
            (self.root / "a.py").write_bytes(user)
            return original(parent, staged, **options)
        with patch.object(rename.Parent, "replace", edit_before):
            result = rename.apply_plan(self.root, plan)
        self.cleanup_receipt(result)
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(user, (self.root / "a.py").read_bytes())

    def test_compensation_preserves_edit_to_already_applied_file_and_retains_backup(self):
        plan, original = self.plan(), rename.Parent.replace
        user = b"# user rewrote applied file\r\n"
        def change_applied(parent, staged, **options):
            if parent.name == "b.py":
                (self.root / "a.py").write_bytes(user)
                raise OSError("injected second failure")
            return original(parent, staged, **options)
        with patch.object(rename.Parent, "replace", change_applied):
            result = rename.apply_plan(self.root, plan)
        self.cleanup_receipt(result)
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(user, (self.root / "a.py").read_bytes())
        receipt = json.loads(Path(result["recovery_receipt"]).read_text())
        self.assertEqual(self.a, Path(receipt["files"][0]["backup"]).read_bytes())
        self.assertEqual(self.b, (self.root / "b.py").read_bytes())

    @unittest.skipUnless(os.name == "posix", "POSIX mode/xattr metadata race")
    def test_post_replace_metadata_edit_is_not_adopted_or_erased_by_compensation(self):
        original = rename.Parent.replace
        for raises_after_syscall in (False, True):
            for metadata in ("mode", "xattr"):
                with self.subTest(raises_after_syscall=raises_after_syscall, metadata=metadata):
                    target = self.root / "a.py"
                    target.write_bytes(self.a)
                    target.chmod(0o644)
                    if metadata == "xattr":
                        if not hasattr(os, "setxattr"):
                            continue
                        try:
                            os.setxattr(target, "user.harness_test", b"probe")
                            os.removexattr(target, "user.harness_test")
                        except OSError:
                            continue
                    plan = self.plan()
                    changed = False
                    def change_metadata(parent, staged, **options):
                        nonlocal changed
                        if parent.name == "b.py":
                            raise OSError("injected second failure")
                        result = original(parent, staged, **options)
                        if not changed:
                            changed = True
                            if metadata == "mode":
                                target.chmod(0o600)
                            else:
                                os.setxattr(target, "user.harness_test", b"concurrent user metadata")
                            if raises_after_syscall:
                                raise OSError("injected error after successful syscall")
                        return result
                    with patch.object(rename.Parent, "replace", change_metadata):
                        result = rename.apply_plan(self.root, plan)
                    self.cleanup_receipt(result)
                    self.assertEqual("recovery_required", result["status"])
                    self.assertEqual(self.a.replace(b"target", b"renamed"), target.read_bytes())
                    self.assertEqual(self.b, (self.root / "b.py").read_bytes())
                    if metadata == "mode":
                        self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))
                    else:
                        self.assertEqual(b"concurrent user metadata", os.getxattr(target, "user.harness_test"))
                        os.removexattr(target, "user.harness_test")

    def test_final_journal_failure_compensates_all_files(self):
        plan, original = self.plan(), rename.atomic_json
        injected = False
        def fail_publication(path, value, **options):
            nonlocal injected
            if value.get("status") == "applied" and not injected:
                injected = True
                raise OSError("injected journal publication failure")
            return original(path, value, **options)
        with patch.object(rename, "atomic_json", fail_publication):
            result = rename.apply_plan(self.root, plan)
        self.cleanup_receipt(result)
        self.assertEqual("failed_compensated", result["status"])
        self.assertEqual(self.a, (self.root / "a.py").read_bytes())
        self.assertEqual(self.b, (self.root / "b.py").read_bytes())

    def test_workspace_lock_and_xattr_metadata_fail_before_writes(self):
        plan = self.plan()
        with rename.workspace_lock(self.root), self.assertRaisesRegex(HarnessError, "lock"):
            rename.apply_plan(self.root, plan)
        if not hasattr(os, "setxattr"):
            return
        try:
            os.setxattr(self.root / "a.py", "user.harness_test", b"preserve")
        except OSError:
            return
        with self.assertRaisesRegex(HarnessError, "attributes/ACLs"):
            self.plan()
        self.assertEqual(self.a, (self.root / "a.py").read_bytes())
        self.assertEqual(b"preserve", os.getxattr(self.root / "a.py", "user.harness_test"))

    def test_known_npm_wrappers_resolve_local_and_global_bin_without_shell(self):
        for layout, name, package in (("local", "pyright-langserver", "pyright"),
                                      ("global", "typescript-language-server", "typescript-language-server")):
            prefix = self.base / layout
            node_modules = prefix / "node_modules"
            package_root = node_modules / package
            package_root.mkdir(parents=True)
            script = package_root / "server.js"
            script.write_text("// fixture")
            (package_root / "package.json").write_text(json.dumps({"name": package, "bin": {name: "server.js"}}))
            wrapper = (node_modules / ".bin" if layout == "local" else prefix) / (name + ".cmd")
            wrapper.parent.mkdir(exist_ok=True)
            wrapper.write_text("must never execute a shell")
            def which(command):
                return sys.executable if command == "node" else str(wrapper) if command == name else None
            with patch("agent_harness.lsp.shutil.which", side_effect=which):
                resolved = lsp._argv([name, "--stdio"], "python")
                self.assertEqual([str(Path(sys.executable).absolute()), str(script.resolve()), "--stdio"], resolved)
                self.assertTrue(next(item for item in lsp.doctor(self.root)["items"] if item["server"] == name)["available"])
                (package_root / "package.json").write_text(json.dumps({"name": package, "bin": {name: "../outside.js"}}))
                (node_modules / "outside.js").write_text("unsafe")
                with self.assertRaisesRegex(HarnessError, "Shell wrappers"):
                    lsp._argv([name, "--stdio"], "python")

    @unittest.skipUnless(os.name == "nt", "Windows native DACL and ReplaceFileW contract")
    def test_windows_dacl_preserved_on_apply_and_compensated_failure(self):
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        api.GetFileSecurityW.restype = wintypes.BOOL
        api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        api.SetFileSecurityW.restype = wintypes.BOOL
        api.SetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, wintypes.WORD, wintypes.WORD]
        api.SetSecurityDescriptorControl.restype = wintypes.BOOL
        api.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        api.GetSecurityDescriptorControl.restype = wintypes.BOOL
        api.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        api.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        api.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        api.SetNamedSecurityInfoW.restype = wintypes.DWORD
        target = self.root / "a.py"
        def descriptor(path=target):
            needed = wintypes.DWORD()
            api.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(needed))
            self.assertGreater(needed.value, 0)
            value = ctypes.create_string_buffer(needed.value)
            self.assertTrue(api.GetFileSecurityW(str(path), 4, value, needed.value, ctypes.byref(needed)))
            return value.raw

        def set_model(legacy, protected):
            current = ctypes.create_string_buffer(descriptor())
            if legacy:
                self.assertTrue(api.SetSecurityDescriptorControl(current, 0x1000, 0x1000 if protected else 0))
                self.assertTrue(api.SetFileSecurityW(str(target), 4, current))
            else:
                present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
                self.assertTrue(api.GetSecurityDescriptorDacl(current, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
                flags = 4 | (0x80000000 if protected else 0x20000000)
                self.assertEqual(0, api.SetNamedSecurityInfoW(str(target), 1, flags, None, None, dacl, None))
            control, revision = wintypes.WORD(), wintypes.DWORD()
            self.assertTrue(api.GetSecurityDescriptorControl(ctypes.create_string_buffer(descriptor()), ctypes.byref(control), ctypes.byref(revision)))
            self.assertEqual(protected, bool(control.value & 0x1000))
            self.assertEqual(not legacy, bool(control.value & 0x0400))

        original = rename.Parent.replace
        stage_original = rename.Parent.stage
        for legacy, protected in ((True, False), (True, True), (False, False), (False, True)):
            for fail in (False, True):
                with self.subTest(legacy=legacy, protected=protected, compensation=fail):
                    target.unlink()
                    target.write_bytes(self.a)
                    (self.root / "b.py").write_bytes(self.b)
                    set_model(legacy, protected)
                    before = descriptor()
                    def replace(parent, staged, **options):
                        if fail and parent.name == "b.py":
                            raise OSError("injected Windows second-file failure")
                        return original(parent, staged, **options)
                    def stage(parent, data, metadata):
                        name, observed = stage_original(parent, data, metadata)
                        self.assertEqual(descriptor(parent.path), descriptor(parent.path.parent / name))
                        return name, observed
                    with patch.object(rename.Parent, "replace", replace), patch.object(rename.Parent, "stage", stage):
                        result = rename.apply_plan(self.root, self.plan())
                    self.cleanup_receipt(result)
                    self.assertEqual("failed_compensated" if fail else "applied", result["status"])
                    self.assertEqual(before, descriptor())
                    self.assertEqual(self.a if fail else self.a.replace(b"target", b"renamed"), target.read_bytes())

        # An intervening DACL edit observed before restoration is preserved.
        stale = descriptor()
        set_model(True, False)
        changed = descriptor()
        self.assertNotEqual(stale, changed)
        with self.assertRaisesRegex(HarnessError, "DACL changed"):
            rename._windows_set_dacl(target, stale, expected=stale)
        self.assertEqual(changed, descriptor())

        # An unknown post-replacement security outcome must keep recovery data.
        target.write_bytes(self.a)
        (self.root / "b.py").write_bytes(self.b)
        replace_windows = rename._windows_replace
        def fail_verification(path, staged):
            replace_windows(path, staged)
            raise OSError("injected post-replace DACL verification failure")
        with patch.object(rename, "_windows_replace", fail_verification):
            result = rename.apply_plan(self.root, self.plan())
        self.cleanup_receipt(result)
        self.assertEqual("recovery_required", result["status"])
        receipt = json.loads(Path(result["recovery_receipt"]).read_text())
        self.assertEqual("recovery_required", receipt["files"][0]["state"])
        self.assertTrue(Path(receipt["files"][0]["backup"]).is_file())

    def test_cli_budget_validated_before_apply_and_errors_are_bounded(self):
        plan_path = self.base / "plan.json"
        plan_path.write_text(json.dumps(self.plan()))
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(2, lsp.dispatch(["apply", "--root", str(self.root), "--plan", str(plan_path), "--max-bytes", "1"]))
        self.assertEqual(self.a, (self.root / "a.py").read_bytes())
        with patch.object(lsp, "query", side_effect=HarnessError("\x01" * 400)), redirect_stderr(output):
            self.assertEqual(2, lsp.dispatch(["symbols", "--root", str(self.root), "--file", "a.py", "--max-bytes", "1024"]))
        self.assertLessEqual(len(output.getvalue().splitlines()[-1].encode()) + 2, 1024)


class RealLSPTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("HARNESS_TEST_LSP_SERVER"), "Set HARNESS_TEST_LSP_SERVER to explicit installed server JSON argv")
    def test_pyright_cross_file_references_preview_apply_and_resolve_afterward(self):
        command = json.loads(os.environ["HARNESS_TEST_LSP_SERVER"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "lib.py").write_bytes(b"def greet(name: str):\r\n    return name\r\n")
            (root / "main.py").write_bytes(b'from lib import greet\r\n\r\nvalue = greet("hi")\r\n')
            kwargs = {"server": command, "timeout": 30, "line": 1, "column": 5}
            references = lsp.query(root, "lib.py", operation="references", **kwargs)
            self.assertEqual(3, references["total_items"])
            self.assertEqual({"lib.py", "main.py"}, {item["path"] for item in references["items"]})
            preview = lsp.query(root, "lib.py", operation="rename", new_name="welcome", **kwargs)
            self.assertEqual((2, 3), (preview["file_count"], preview["edit_count"]))
            applied = rename.apply_plan(root, preview["plan"])
            self.assertEqual("applied", applied["status"])
            definition = lsp.query(root, "main.py", operation="definition", line=3, column=9, server=command, timeout=30)
            self.assertEqual("lib.py", definition["items"][0]["path"])
            self.assertEqual(b'def welcome(name: str):\r\n    return name\r\n', (root / "lib.py").read_bytes())
            self.assertEqual(b'from lib import welcome\r\n\r\nvalue = welcome("hi")\r\n', (root / "main.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
