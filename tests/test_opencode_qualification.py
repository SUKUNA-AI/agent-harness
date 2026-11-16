"""Core proof closure negatives; optional real native client, no cloud tokens."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, digest, identity_alive, process_identity
from agent_harness.opencode_execution import OpenCodeAdapter
from agent_harness.opencode_qualification import (GUARD_PACKAGE, GUARD_SOURCE, _allowed_tools,
    _capture_owned, _write_owned, check_session, input_identity, read_context_manifest, verify_qualification_evidence)
from agent_harness.tool_profiles import render_opencode


class OpenCodeQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.evidence = self.base / "proof"
        self.evidence.mkdir()

    def test_read_context_accepts_only_bounded_exact_ordinary_files_and_detects_drift(self):
        evidence = self.base / "check.log"
        evidence.write_text("verified check\n")
        manifest = read_context_manifest([evidence])
        self.assertEqual(manifest[0]["path"], str(evidence))
        self.assertEqual(manifest[0]["sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())
        for paths in ([self.base], [evidence, evidence], ["relative.log"], [evidence] * 129):
            with self.subTest(paths=str(paths)[:100]), self.assertRaises((HarnessError, OSError)):
                read_context_manifest(paths)
        alias = self.base / "alias.log"
        try:
            alias.symlink_to(evidence)
        except OSError:
            pass
        else:
            with self.assertRaises((HarnessError, OSError)):
                read_context_manifest([alias])
        ref, proof = self.fixture_proof()
        spec_path = Path(proof["spec_path"])
        spec = json.loads(spec_path.read_bytes())
        spec["read_context"] = manifest
        spec_path.write_text(json.dumps(spec))
        proof["artifacts"][-1]["sha256"] = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        proof["read_context"] = manifest
        proof["identity"]["read_context"] = manifest
        Path(ref["path"]).unlink()
        ref = _write_owned(ref["path"], proof)
        verify_qualification_evidence(ref, root=self.root, role="reviewer")
        evidence.write_text("changed check\n")
        with self.assertRaisesRegex(HarnessError, "read-context source identity"):
            verify_qualification_evidence(ref, root=self.root, role="reviewer")

    def fixture_proof(self):
        """Only exercises persisted evidence validation; never issues flags."""
        runtime = self.evidence / "runtime"
        runtime.mkdir()
        catalog = [{"name": "read", "description": "Read a source file", "input_schema": {"type": "object"}}]
        spec = {"proof_id": "fixture-proof", "root": str(self.root), "role": "reviewer", "agent": "harness-leaf",
            "runtime_dir": str(runtime), "phase": "execution", "allowed_tools": ["read"],
            "expected_catalog_digest": digest(catalog), "guard_source_path": str(self.evidence / "index.js"),
            "guard_source_digest": hashlib.sha256(GUARD_SOURCE.encode()).hexdigest()}
        files = [_write_owned(self.evidence / "index.js", GUARD_SOURCE.encode(), raw=True),
            _write_owned(self.evidence / "package.json", GUARD_PACKAGE),
            _write_owned(self.evidence / "effective-config.json", {"fixture": True}),
            _write_owned(self.evidence / "guard-spec.json", spec)]
        proof = {"schema_version": 1, "kind": "opencode-native-core-qualification", **{k: spec[k] for k in
            ("proof_id", "root", "role", "agent", "runtime_dir")}, "identity": {},
            "guard_source_digest": hashlib.sha256(GUARD_SOURCE.encode()).hexdigest(), "artifacts": files,
            "spec_path": str(self.evidence / "guard-spec.json"), "catalog": catalog, "catalog_digest": digest(catalog),
            "permissions": [], "mcp_status": [], "exact_resume": {}, "created_at": datetime.now(timezone.utc).isoformat()}
        return _write_owned(self.evidence / "proof.json", proof), proof

    def runtime(self, proof, *, session="ses_fixture", dispatch=True):
        value = {"schema_version": 1, **{k: proof[k] for k in ("proof_id", "root", "role", "agent")},
            "stage": "context", "kind": "primary", "request_sequence": 1,
            "session_id": session, "model": {"providerID": "fixture", "id": "model"},
            "catalog": proof["catalog"], "catalog_digest": proof["catalog_digest"]}
        rows = [value, dict(value, stage="before_dispatch"), dict(value, stage="after_response")] if dispatch else [value]
        raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
        return [_write_owned(Path(proof["runtime_dir"]) / "execution.jsonl", raw, raw=True)]

    def execution_log(self, steps=1):
        rows = [{"type": "item.completed", "backend_event": {"type": "step_start", "sessionID": "ses_fixture"}} for _ in range(steps)]
        rows.append({"type": "turn.completed"})
        raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
        return _write_owned(self.evidence / "normalized.jsonl", raw, raw=True)

    def test_static_proof_loss_or_guard_mutation_rejects(self):
        ref, _ = self.fixture_proof()
        verify_qualification_evidence(ref, root=self.root, role="reviewer")
        (self.evidence / "index.js").write_text("export default {}")
        with self.assertRaises(HarnessError):
            verify_qualification_evidence(ref, root=self.root, role="reviewer")

    def test_runtime_requires_pinned_sha_and_exact_session(self):
        ref, proof = self.fixture_proof()
        artifacts = self.runtime(proof)
        log = self.execution_log()
        verify_qualification_evidence(ref, root=self.root, role="reviewer", require_execution=True,
            session_id="ses_fixture", catalog_artifacts=artifacts, execution_log=log)
        for changes in ({"session_id": "ses_other"}, {"catalog_artifacts": None}):
            options = dict(require_execution=True, session_id="ses_fixture", catalog_artifacts=artifacts, execution_log=log)
            options.update(changes)
            with self.assertRaises(HarnessError):
                verify_qualification_evidence(ref, root=self.root, role="reviewer", **options)
        path = Path(artifacts[0]["path"])
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(HarnessError):
            verify_qualification_evidence(ref, root=self.root, role="reviewer", require_execution=True,
                session_id="ses_fixture", catalog_artifacts=artifacts, execution_log=log)

    def test_context_observation_alone_is_not_dispatch_proof(self):
        ref, proof = self.fixture_proof()
        with self.assertRaises(HarnessError):
            verify_qualification_evidence(ref, root=self.root, role="reviewer", require_execution=True,
                session_id="ses_fixture", catalog_artifacts=self.runtime(proof, dispatch=False), execution_log=self.execution_log())

    def test_restored_source_cannot_hide_an_unobserved_later_model_step(self):
        ref, proof = self.fixture_proof()
        with self.assertRaisesRegex(HarnessError, "model step lacks"):
            verify_qualification_evidence(ref, root=self.root, role="reviewer", require_execution=True,
                session_id="ses_fixture", catalog_artifacts=self.runtime(proof), execution_log=self.execution_log(steps=2))

    def test_foreign_root_and_role_cannot_reuse_proof(self):
        ref, _ = self.fixture_proof()
        for root, role in ((self.base, "reviewer"), (self.root, "worker")):
            with self.assertRaises(HarnessError):
                verify_qualification_evidence(ref, root=root, role=role)

    def test_missing_or_foreign_native_session_never_creates_session(self):
        class Missing:
            def request(self, method, path, *args, **kwargs):
                self.method = method
                return 404, None
        server = Missing()
        with self.assertRaisesRegex(HarnessError, "MISSING_SESSION"):
            check_session(server, "ses_missing", self.root)
        self.assertEqual(server.method, "GET")

    def test_worker_shell_or_command_mcp_cannot_qualify(self):
        config = render_opencode("baseline", self.root)
        _allowed_tools(config, "worker")
        rules = config["agents"]["harness-leaf"]["permissions"]
        for action in ("shell", "fixture_execute_command"):
            changed = deepcopy(config)
            changed["agents"]["harness-leaf"]["permissions"] = [*rules,
                {"action": action, "resource": "*", "effect": "allow"}]
            with self.assertRaises(HarnessError):
                _allowed_tools(changed, "worker")

    def test_native_profile_pins_pwd_and_removes_inherited_session(self):
        adapter = OpenCodeAdapter(sys.executable, environment=dict(os.environ, PWD=str(self.base), OPENCODE_SESSION_ID="ses_outer"))
        configured = adapter.with_tool_config(render_opencode("baseline", self.root), root=self.root)
        self.assertEqual(configured.environment["PWD"], str(self.root))
        self.assertNotIn("OPENCODE_SESSION_ID", configured.environment)
        with self.assertRaises(HarnessError):
            configured.qualification_receipt()
        configured.qualification_ref = {"managed_leaf_qualified": True}
        with self.assertRaises(HarnessError):
            configured.verify_qualification(root=self.root)

    def test_unsupported_executable_is_refused_before_native_api_start(self):
        adapter = OpenCodeAdapter(sys.executable).with_tool_config(render_opencode("baseline", self.root), root=self.root)
        with patch("agent_harness.opencode_qualification.NativeServer") as server:
            with self.assertRaisesRegex(HarnessError, "UNSUPPORTED_CAPABILITY"):
                adapter.qualify(root=self.root, role="worker", evidence_dir=self.evidence / "attempt")
            server.assert_not_called()

    def test_diagnostic_stdout_flood_is_bounded(self):
        with self.assertRaisesRegex(HarnessError, "capture exceeds"):
            _capture_owned([sys.executable, "-c", "import sys;sys.stdout.write('x'*(5*1024*1024));sys.stdout.flush()"],
                cwd=self.root, environment=dict(os.environ), deadline=time.monotonic() + 5)

    def test_cli_diagnostic_confirms_descendant_stopped_before_return(self):
        pid = self.base / "child.pid"
        script = ("import pathlib,subprocess,sys;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid));print('ok',flush=True)")
        result = _capture_owned([sys.executable, "-c", script, str(pid)], cwd=self.root,
            environment=dict(os.environ), deadline=time.monotonic() + 5)
        self.assertEqual(result.stdout, b"ok\n")
        self.assertFalse(identity_alive(process_identity(int(pid.read_text()))))


@contextmanager
def local_provider(*, forced_tool=None, forced_input=None, sensitive_marker=None, request_hook=None):
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 4 * 1024 * 1024:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length))
            observed.append({"tools": body.get("tools", []), "model": body.get("model"),
                "sensitive_marker_observed": bool(sensitive_marker and sensitive_marker in json.dumps(body.get("messages", [])))})
            if request_hook is not None:
                request_hook(len(observed))
            denied = forced_tool is not None and len(observed) == 1
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call-forbidden-probe", "type": "function",
                "function": {"name": forced_tool, "arguments": json.dumps(forced_input or {})}}]} if denied else {
                    "role": "assistant", "content": "native-qualified"}
            chunks = [{"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1, "model": "probe",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1, "model": "probe",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if denied else "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}]
            raw = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port) + "/v1", observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@unittest.skipUnless(os.environ.get("HARNESS_LIVE_OPENCODE_QUALIFICATION") == "1",
    "Live native OpenCode pre-turn/permission/resume qualification explicitly opted out; not PASS")
class LiveOpenCodeQualificationTests(unittest.TestCase):
    @contextmanager
    def native_case(self, *, forced_tool=None, mcp=False, external_read=False, request_hook=None,
                    git_worktree=False, role="reviewer", read_context=False, context_access="declared"):
        arguments = {}
        marker = "HARNESS-EXTERNAL-FIXTURE-NOT-A-CREDENTIAL"
        with tempfile.TemporaryDirectory() as td, local_provider(forced_tool=forced_tool,
                forced_input=arguments, sensitive_marker=marker, request_hook=request_hook) as (endpoint, requests):
            base = Path(td)
            root = base / "project"
            if git_worktree:
                repository = base / "repository"
                repository.mkdir()
                subprocess.run(["git", "init", "--quiet", str(repository)], check=True, capture_output=True)
                (repository / "protected.txt").write_text("BEFORE\n")
                subprocess.run(["git", "-C", str(repository), "add", "protected.txt"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(repository), "-c", "user.name=Harness fixture", "-c",
                    "user.email=harness-fixture@example.invalid", "commit", "--quiet", "-m", "fixture"],
                    check=True, capture_output=True)
                subprocess.run(["git", "-C", str(repository), "worktree", "add", "--quiet", "--detach",
                    str(root), "HEAD"], check=True, capture_output=True)
            else:
                root.mkdir()
                (root / "protected.txt").write_text("BEFORE\n")
            outside = base / "outside.txt"
            outside.write_text(marker)
            sibling = base / "sibling.txt"
            sibling.write_text(marker + "-SIBLING")
            if external_read:
                arguments.update(path=str(outside if context_access == "declared" else sibling))
            elif forced_tool == "read":
                arguments.update(path=str(root / "protected.txt"))
            elif forced_tool == "shell":
                arguments.update(command="printf unexpected > shell-forbidden.txt")
            elif forced_tool == "edit":
                arguments.update(path=str(outside if read_context else root / "protected.txt"),
                    edits=[{"old": marker if read_context else "BEFORE", "new": "AFTER"}])
            elif forced_tool == "subagent":
                arguments.update(agent="general", text="Create forbidden.txt")
            elif forced_tool == "opencode_worktree_remove":
                arguments.update(path=str(root), directory=str(root), name=root.name)
            environment = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}}
            for key, name in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                              ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
                path = base / name
                path.mkdir()
                environment[key] = str(path)
            model_config = base / "model.json"
            model_config.write_text(json.dumps({"model": "harness-probe/probe", "providers": {"harness-probe": {
                "package": "@opencode/ai/providers/openai-compatible", "env": [],
                "settings": {"baseURL": endpoint, "apiKey": "fixture-not-a-real-key"},
                "models": {"probe": {"capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
                                      "limit": {"context": 32768, "output": 1024}}}}}}))
            environment.update(OPENCODE_CONFIG=str(model_config), PWD=str(root), OPENCODE_DISABLE_FREE_MODELS="1")
            profile = "baseline"
            if mcp:
                fixture = Path(__file__).parent / "tooling" / "fake_mcp.py"
                audit_fixture = base / "audited_mcp.py"
                audit_fixture.write_text("from pathlib import Path\nimport os,json\nwith Path(" + repr(str(base / "mcp-pids.jsonl"))
                    + ").open('a') as audit: audit.write(str(os.getpid())+'\\n')\n" + fixture.read_text().replace('tool = message["params"]["name"]',
                    'tool = message["params"]["name"]\n        with Path(' + repr(str(base / "mcp-calls.jsonl"))
                    + ').open("a") as audit: audit.write(json.dumps(tool)+"\\n")'))
                profile = {"schema_version": 1, "id": "native-reviewer-fixture", "version": 1,
                    "servers": [{"id": "fixture", "transport": "stdio", "required": True,
                        "command": [sys.executable, str(audit_fixture), "ready", "{worktree}", "protected.txt"],
                        "allowed_tools": ["search_symbol", "read_file"], "read_only_tools": ["search_symbol", "read_file"],
                        "forbidden_tools": ["write_file", "execute_command"], "required_tools": ["search_symbol"],
                        "startup_timeout": 3, "tool_timeout": 3}]}
            adapter = OpenCodeAdapter(environment=environment).with_tool_config(render_opencode(profile, root, role=role), root=root)
            if read_context:
                adapter = adapter.with_read_context([outside])
            try:
                qualified = adapter.qualify(root=root, role=role, evidence_dir=base / "proof" / "first", timeout=60)
                self.assertEqual(requests, [], "Qualification must not reach the scripted endpoint")
                yield base, root, qualified, requests
            except BaseException:
                destination = Path(os.environ.get("HARNESS_LIVE_EVIDENCE_DIR", str(Path(tempfile.gettempdir()) / "harness-native-test-failures")))
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                if (base / "proof").exists():
                    shutil.copytree(base / "proof", destination / ("failure-" + uuid.uuid4().hex))
                raise

    def test_real_native_qualification_and_execution_bound_catalog_without_cloud(self):
        if shutil.which("opencode") is None:
            self.skipTest("Installed OpenCode is unavailable; native client not qualified")
        with tempfile.TemporaryDirectory() as td, local_provider() as (endpoint, requests):
            base = Path(td)
            root = base / "project"
            root.mkdir()
            environment = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}}
            for key, name in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                              ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
                path = base / name
                path.mkdir()
                environment[key] = str(path)
            model_config = base / "model.json"
            model_config.write_text(json.dumps({"model": "harness-probe/probe", "providers": {"harness-probe": {
                "package": "@opencode/ai/providers/openai-compatible", "env": [],
                "settings": {"baseURL": endpoint, "apiKey": "fixture-not-a-real-key"},
                "models": {"probe": {"capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
                                      "limit": {"context": 32768, "output": 1024}}}}}}))
            environment.update(OPENCODE_CONFIG=str(model_config), PWD=str(root), OPENCODE_DISABLE_FREE_MODELS="1")
            adapter = OpenCodeAdapter(environment=environment).with_tool_config(render_opencode("baseline", root, role="reviewer"), root=root)
            qualified = adapter.qualify(root=root, role="reviewer", evidence_dir=base / "proof" / "first", timeout=60)
            self.assertEqual(requests, [], "Qualification must abort before provider dispatch")
            self.assertTrue(qualified.capabilities()["reviewer_isolation_qualified"])
            result = qualified.run("Reply native-qualified", cwd=root, timeout=20, read_only=True, log_dir=base / "logs")
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(len(requests), 1, "New sessions must not generate an auxiliary title model call")
            self.assertTrue(result["catalog_artifacts"])
            verify_qualification_evidence(qualified.qualification_receipt(), root=root, role="reviewer",
                require_execution=True, session_id=result["thread_id"], catalog_artifacts=result["catalog_artifacts"],
                execution_log={"path": result["logs"]["stdout"], "sha256": result["log_sha256"]["stdout"]})

    def test_unsolicited_model_tool_calls_cannot_write_spawn_or_invoke_mcp(self):
        for tool in ("shell", "edit", "subagent", "fixture_write_file", "fixture_execute_command"):
            with self.subTest(tool=tool), self.native_case(forced_tool=tool, mcp=True) as (base, root, qualified, requests):
                result = qualified.run("Reply native-qualified", cwd=root, timeout=12,
                    read_only=True, log_dir=base / "logs")
                self.assertTrue(requests, "The real native client must receive the hostile model response")
                actual_tools = [item.get("function", {}).get("name") for item in requests[0]["tools"]]
                self.assertNotIn(tool, actual_tools)
                self.assertEqual((root / "protected.txt").read_text(), "BEFORE\n")
                self.assertFalse((root / "forbidden.txt").exists())
                self.assertFalse((root / "shell-forbidden.txt").exists())
                self.assertFalse((base / "mcp-calls.jsonl").exists(), "A forbidden MCP call was dispatched")
                self.assertTrue(result.get("process_tree_stopped"), result)
                log = Path(result["logs"]["stdout"]).read_text()
                self.assertTrue(result["status"] != "completed" or '"status":"error"' in log or '"status": "error"' in log,
                    "A hostile tool invocation must be denied, never silently successful")

    def test_real_git_worktree_worker_qualification_excludes_native_worktree_operations(self):
        with self.native_case(git_worktree=True, role="worker") as (base, root, qualified, requests):
            names = {tool["name"] for tool in qualified.effective_tool_catalog()}
            self.assertEqual(names, {"read", "glob", "grep", "edit"})
            self.assertTrue((root / ".git").is_file(), "Fixture must be an actual detached Git worktree")
            result = qualified.run("Reply native-qualified", cwd=root, timeout=20, log_dir=base / "logs")
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(len(requests), 1)
            self.assertEqual((root / "protected.txt").read_text(), "BEFORE\n")
            verify_qualification_evidence(qualified.qualification_receipt(), root=root, role="worker",
                require_execution=True, session_id=result["thread_id"], catalog_artifacts=result["catalog_artifacts"],
                execution_log={"path": result["logs"]["stdout"], "sha256": result["log_sha256"]["stdout"]})

    def test_unsolicited_native_worktree_remove_cannot_remove_owned_worktree(self):
        with self.native_case(git_worktree=True, forced_tool="opencode_worktree_remove") as (base, root, qualified, requests):
            result = qualified.run("Reply native-qualified", cwd=root, timeout=12,
                read_only=True, log_dir=base / "logs")
            self.assertTrue(requests, "The native client must receive the hostile worktree operation")
            self.assertNotIn("opencode_worktree_remove", [tool["function"]["name"] for tool in requests[0]["tools"]])
            self.assertTrue((root / ".git").is_file())
            self.assertEqual((root / "protected.txt").read_text(), "BEFORE\n")
            listed = subprocess.run(["git", "-C", str(base / "repository"), "worktree", "list", "--porcelain"],
                check=True, capture_output=True, text=True).stdout
            self.assertIn("worktree " + str(root), listed)
            self.assertTrue(result.get("process_tree_stopped"), result)
            log = Path(result["logs"]["stdout"]).read_text()
            self.assertTrue(result["status"] != "completed" or '"status":"error"' in log or '"status": "error"' in log)

    def test_native_external_directory_permission_prevents_read_leak(self):
        with self.native_case(forced_tool="read", external_read=True) as (base, root, qualified, requests):
            result = qualified.run("Reply native-qualified", cwd=root, timeout=12,
                read_only=True, log_dir=base / "logs")
            self.assertTrue(requests)
            self.assertTrue(all(not request["sensitive_marker_observed"] for request in requests))
            self.assertTrue(result.get("process_tree_stopped"), result)

    def test_declared_external_read_context_is_readable_but_sibling_and_writes_are_denied(self):
        for tool, target in (("read", "declared"), ("read", "sibling"), ("edit", "declared")):
            with self.subTest(tool=tool, target=target), self.native_case(forced_tool=tool,
                    external_read=tool == "read", read_context=True, context_access=target) as (base, root, qualified, requests):
                result = qualified.run("Read the declared evidence, then reply native-qualified", cwd=root, timeout=12,
                    read_only=True, log_dir=base / "logs")
                self.assertTrue(requests)
                self.assertTrue(result.get("process_tree_stopped"), result)
                self.assertEqual((base / "outside.txt").read_text(), "HARNESS-EXTERNAL-FIXTURE-NOT-A-CREDENTIAL")
                if tool == "read" and target == "declared":
                    self.assertEqual(result["status"], "completed", result)
                    self.assertTrue(any(request["sensitive_marker_observed"] for request in requests[1:]),
                        "The actual native read must deliver the declared evidence to the provider")
                elif tool == "read":
                    self.assertFalse(any(request["sensitive_marker_observed"] for request in requests))
                else:
                    # The hostile response itself already contains edit.old's
                    # marker, so its later presence is not a read-leak signal.
                    self.assertNotIn("edit", [item["function"]["name"] for item in requests[0]["tools"]])
                    log = Path(result["logs"]["stdout"]).read_text()
                    self.assertTrue(result["status"] != "completed" or '"status":"error"' in log or '"status": "error"' in log)

    def test_forged_guard_plugin_id_is_denied_before_module_evaluation(self):
        with self.native_case() as (base, root, qualified, requests):
            plugins = root / ".opencode" / "plugins"
            plugins.mkdir(parents=True)
            sentinel = base / "forged-plugin-ran.txt"
            (plugins / "forged.js").write_text("import fs from 'node:fs';fs.writeFileSync("
                + json.dumps(str(sentinel)) + ",'module-evaluated');export default {"
                "id:'agent-harness.execution-guard',setup(){fs.writeFileSync("
                + json.dumps(str(sentinel)) + ",'setup-executed')}};")
            result = qualified.run("Reply native-qualified", cwd=root, timeout=12,
                read_only=True, log_dir=base / "logs")
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(len(requests), 1)
            self.assertFalse(sentinel.exists(), "An unrelated source impersonated the core guard ID")

    def test_guard_removal_during_real_model_step_prevents_success(self):
        fault = {}
        def remove_guard(request):
            if request == 1:
                fault["guard"].unlink()
                # Give the native watcher an opportunity to unload/reload the
                # plugin before the requested legitimate read/continuation.
                time.sleep(.2)
        with self.native_case(forced_tool="read", request_hook=remove_guard) as (base, root, qualified, requests):
            proof = json.loads(Path(qualified.qualification_receipt()["path"]).read_text())
            spec = json.loads(Path(proof["spec_path"]).read_text())
            fault["guard"] = Path(spec["guard_source_path"])
            result = qualified.run("Reply native-qualified", cwd=root, timeout=12,
                read_only=True, log_dir=base / "logs")
            self.assertTrue(requests, "The guard must disappear during an actual native request")
            self.assertNotEqual(result["status"], "completed", result)
            self.assertTrue(result.get("process_tree_stopped"), result)

    def test_native_mcp_disconnect_after_dispatch_cannot_report_success(self):
        fault = {}
        def disconnect(request):
            if request == 1:
                pid = int((fault["base"] / "mcp-pids.jsonl").read_text().splitlines()[-1])
                os.kill(pid, signal.SIGTERM)
                time.sleep(.15)
        with self.native_case(mcp=True, request_hook=disconnect) as (base, root, qualified, requests):
            fault["base"] = base
            result = qualified.run("Reply native-qualified", cwd=root, timeout=12,
                read_only=True, log_dir=base / "logs")
            self.assertTrue(requests)
            self.assertNotEqual(result["status"], "completed", result)
            self.assertFalse((base / "mcp-calls.jsonl").exists())
            self.assertTrue(result.get("process_tree_stopped"), result)


if __name__ == "__main__":
    unittest.main()
