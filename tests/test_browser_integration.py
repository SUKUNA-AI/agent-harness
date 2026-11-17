"""Managed browser policy: unit checks plus opt-in real official MCP/Chromium.

Set HARNESS_PLAYWRIGHT_ENV to an isolated npm prefix containing @playwright/mcp
and browsers/. The live tests run the actual native-facing stdio protocol; no
fake browser, cached catalog or caller-supplied success flag qualifies them.
"""
from copy import deepcopy
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.browser_integration import (TOOLS, _PrivateProbe, browser_server,
    qualify_browser, run_managed_browser_job, validate_browser_policy)
from agent_harness.core import HarnessError, identity_alive
from agent_harness.optional_tool_jobs import BrowserJob
from agent_harness.tooling_probe import StdioProbe

LIVE_PREFIX = os.environ.get("HARNESS_PLAYWRIGHT_ENV")


class BrowserPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.private = self.base / "private"
        self.private.mkdir()
        self.policy = {"isolated": True, "allowed_origins": ["http://127.0.0.1:8123"],
            "mcp_package": str(self.base / "cli.js"), "browser_executable": str(self.base / "browser"),
            "browser_cache": str(self.base)}

    def test_policy_rejects_implicit_origins_and_unsupported_security_bypasses(self):
        for updates in ({"isolated": False}, {"allowed_origins": []}, {"allowed_origins": ["http://localhost:8123"]},
                {"allowed_origins": ["http://127.0.0.1:*"]}, {"allowed_origins": ["http://127.0.0.1:0"]},
                {"allowed_origins": ["http://127.0.0.1:8123/path"]}, {"allowed_origins": ["http://user@127.0.0.1"]},
                {"allowed_origins": ["file:///etc/passwd"]}, {"allowed_origins": ["http://192.168.1.1"]},
                {"allowed_origins": ["https://127.0.0.1"]}, {"allow_test_writes": True},
                {"no_sandbox": True}, {"artifacts": "../../source"}, {"mcp_package": "npx"}):
            with self.subTest(updates=updates), self.assertRaises(HarnessError):
                validate_browser_policy({**self.policy, **updates})

    def test_rendering_is_pure_private_and_does_not_offer_code_or_file_tools(self):
        server = browser_server(self.policy, root=self.source, private_root=self.private)
        second = self.base / "other"
        second.mkdir()
        other = browser_server(self.policy, root=second, private_root=self.private)
        self.assertNotEqual(server["command"][-1], other["command"][-1])
        self.assertEqual(list(self.private.iterdir()), [])
        self.assertEqual(set(server["allowed_tools"]), set(TOOLS))
        self.assertFalse(any("code" in tool or "evaluate" in tool or "upload" in tool for tool in TOOLS))
        self.assertEqual(server["env_vars"], [])

    def test_missing_browser_dependency_fails_closed_without_source_changes(self):
        result = qualify_browser(self.policy, root=self.source, private_root=self.private)
        self.assertEqual(result["status"], "BLOCKED_NOT_QUALIFIED")
        self.assertNotIn("browser_launch_observed", result)
        self.assertEqual(list(self.source.iterdir()), [])

    def test_explicit_child_environment_never_mutates_supervisor_environment(self):
        fixture = Path(__file__).parent / "tooling" / "fake_mcp.py"
        server = {"transport": "stdio", "command": [sys.executable, str(fixture)],
            "cwd": str(self.source), "environment": {"PATH": os.environ.get("PATH", "")},
            "startup_timeout": 5, "tool_timeout": 5, "allowed_tools": [], "forbidden_tools": [], "required_tools": []}
        with patch.dict(os.environ, {"BROWSER_FIXTURE_SECRET": "supervisor-value"}):
            with _PrivateProbe(server) as connection:
                connection.inventory()
                self.assertEqual(os.environ["BROWSER_FIXTURE_SECRET"], "supervisor-value")
                if sys.platform.startswith("linux"):
                    child = Path(f"/proc/{connection.process.pid}/environ").read_bytes()
                    self.assertNotIn(b"BROWSER_FIXTURE_SECRET", child)


class _Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.hits.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.forbidden + "/redirect-leak")
            self.end_headers()
            return
        if self.path == "/app":
            body = (self.server.source / "app.html").read_bytes()
        elif self.path == "/attacks":
            body = ("<h1>network-policy-ready</h1><img src='" + self.server.forbidden + "/image-leak'>"
                "<iframe src='" + self.server.forbidden + "/frame-leak'></iframe>"
                "<script>fetch('" + self.server.forbidden + "/fetch-leak').catch(()=>{});"
                "new WebSocket('" + self.server.forbidden.replace('http:', 'ws:') + "/ws-leak');"
                "new WebSocket('ws://127.0.0.1:" + str(self.server.server_port) + "/local-ws');"
                "document.body.dataset.rtc=typeof RTCPeerConnection;</script>").encode()
        else:
            body = b"<h1>forbidden-origin-reached</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(LIVE_PREFIX, "real official MCP/browser: set HARNESS_PLAYWRIGHT_ENV; not qualified by fixture tests")
class LiveManagedBrowserTests(unittest.TestCase):
    def setUp(self):
        BrowserPolicyTests.setUp(self)
        prefix = Path(LIVE_PREFIX)
        matches = list((prefix / "browsers").glob("chromium*/chrome-headless-shell-linux64/chrome-headless-shell"))
        if not matches:
            self.fail("Required actual Chromium browser is missing from the declared test prefix")
        self.policy.update(mcp_package=str(prefix / "node_modules/@playwright/mcp/cli.js"),
            browser_executable=str(matches[0]), browser_cache=str(prefix / "browsers"))
        self.sites = []
        for _ in range(2):
            site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
            site.daemon_threads = True
            site.hits = []
            site.source = self.source
            thread = threading.Thread(target=site.serve_forever, daemon=True)
            thread.start()
            self.sites.append(site)
            self.addCleanup(site.server_close)
            self.addCleanup(site.shutdown)
            self.addCleanup(lambda thread=thread: thread.join(timeout=2))
        self.allowed = "http://127.0.0.1:" + str(self.sites[0].server_port)
        self.forbidden = "http://127.0.0.1:" + str(self.sites[1].server_port)
        for site in self.sites:
            site.forbidden = self.forbidden
        self.policy["allowed_origins"] = [self.allowed]
        self.addCleanup(self._export_evidence)

    def _export_evidence(self):
        destination = os.environ.get("HARNESS_BROWSER_EVIDENCE")
        if destination:
            destination = Path(destination) / self._testMethodName
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copytree(self.private, destination / self.base.name)

    def _connection(self):
        return StdioProbe(browser_server(self.policy, root=self.source, private_root=self.private))

    def _call(self, connection, name, arguments=None):
        return connection.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=30)

    def test_real_qualification_launches_and_closes_browser_no_self_report_override(self):
        result = qualify_browser(self.policy, root=self.source, private_root=self.private)
        self.assertEqual(result["status"], "QUALIFIED", result)
        self.assertTrue(result["browser_launch_observed"])
        self.assertTrue(result["browser_close_observed"])
        self.assertTrue(result["process_tree_stopped"])
        self.assertFalse(identity_alive(result["process_identity"]))
        bad = {**self.policy, "browser_executable": str(self.base / "missing-browser")}
        self.assertEqual(qualify_browser(bad, root=self.source, private_root=self.private)["status"], "BLOCKED_NOT_QUALIFIED")

    def test_real_stdio_client_denies_code_upload_path_escape_origins_and_redirects(self):
        marker = self.base / "escape.txt"
        with self._connection() as connection:
            catalog = connection.inventory()
            self.assertEqual({tool["name"] for tool in catalog}, set(TOOLS))
            for name, arguments in (("browser_evaluate", {"function": "()=>1"}),
                    ("browser_run_code", {"code": "process.exit()"}),
                    ("browser_run_code_unsafe", {"code": "process.exit()"}),
                    ("browser_file_upload", {"paths": [str(marker)]}),
                    ("browser_install", {}), ("browser_snapshot", {"filename": str(marker)}),
                    ("browser_take_screenshot", {"filename": "../../escape.txt", "scale": "css"}),
                    ("browser_navigate", {"url": "file:///etc/passwd"}),
                    ("browser_navigate", {"url": "javascript:alert(1)"}),
                    ("browser_navigate", {"url": self.forbidden + "/direct-leak"}),
                    ("browser_navigate", {"url": "https://example.com/"})):
                with self.subTest(name=name, arguments=arguments):
                    self.assertTrue(self._call(connection, name, arguments).get("isError"))
            attacked = self._call(connection, "browser_navigate", {"url": self.allowed + "/attacks"})
            self.assertFalse(attacked.get("isError"), attacked)
            self.assertIn("network-policy-ready", json.dumps(attacked))
            visible = self._call(connection, "browser_wait_for", {"text": "network-policy-ready"})
            self.assertFalse(visible.get("isError"), visible)
            # Snapshot after page load is a protocol round trip after attempted
            # subresources; external sockets are checked by actual server hits.
            self.assertFalse(self._call(connection, "browser_snapshot").get("isError"))
            self._call(connection, "browser_navigate", {"url": self.allowed + "/redirect"})
            self.assertEqual(self.sites[1].hits, [])
            self.assertNotIn("/local-ws", self.sites[0].hits)
            self.assertFalse(marker.exists())
            shot = self._call(connection, "browser_take_screenshot", {"scale": "css"})
            self.assertFalse(shot.get("isError"), shot)
            self.assertFalse(self._call(connection, "browser_close").get("isError"))
        self.assertTrue(connection.stopped)
        self.assertFalse(identity_alive(connection.identity))
        receipts = list(self.private.rglob("receipt.json"))
        self.assertTrue(receipts)
        actual = json.loads(receipts[-1].read_text())
        self.assertTrue(any(item["path"].endswith(".png") for item in actual["artifacts"]))
        self.assertTrue(actual["process_tree_stopped"])
        self.assertEqual(self.sites[1].hits, [])

    def test_real_regression_reproduces_defect_then_verifies_scoped_fix(self):
        app = self.source / "app.html"
        template = "<button id='add' onclick='out.textContent=Number(out.textContent)+STEP'>Add</button><output id='out'>0</output>"
        app.write_text(template.replace("STEP", "2"))
        with self._connection() as connection:
            connection.inventory()
            self.assertFalse(self._call(connection, "browser_navigate", {"url": self.allowed + "/app"}).get("isError"))
            self.assertFalse(self._call(connection, "browser_click", {"target": "#add"}).get("isError"))
            reproduced = self._call(connection, "browser_snapshot", {"target": "#out"})
            self.assertIn('"2"', json.dumps(reproduced).replace('\\"', '"'))
        # An independent automated browser regression observes the same defect.
        regression = self.base / "regression.cjs"
        regression.write_text("const {chromium}=require(process.argv[2]);const assert=require('node:assert/strict');"
            "(async()=>{const b=await chromium.launch({headless:true,chromiumSandbox:true,executablePath:process.argv[3]});"
            "try{const p=await b.newPage();await p.goto(process.argv[4]);await p.locator('#add').click();"
            "assert.equal(await p.locator('#out').innerText(),'1')}finally{await b.close()}})().catch(e=>{console.error(e.message);process.exitCode=1});")
        command = ["node", str(regression), str(Path(LIVE_PREFIX) / "node_modules/playwright"),
            self.policy["browser_executable"], self.allowed + "/app"]
        job = BrowserJob(root=self.source, artifact_root=self.private, url=self.allowed + "/app",
            actions=({"kind": "click", "selector": "#add"}, {"kind": "expect_text", "selector": "#out", "value": "1"}),
            regression_argv=tuple(command), expectation="reproduces")
        before = run_managed_browser_job(self.policy, job)
        self.assertEqual(before["status"], "REPRODUCED", before)
        self.assertEqual(before["regression"]["exit_code"], 1)
        app.write_text(template.replace("STEP", "1"))
        from dataclasses import replace
        after = run_managed_browser_job(self.policy, replace(job, expectation="passes"))
        self.assertEqual(after["status"], "COMPLETED", after)
        self.assertEqual(after["regression"]["exit_code"], 0)
        self.assertTrue(after["browser"]["context_closed"])
        self.assertTrue(after["browser"]["browser_closed"])
        self.assertFalse(identity_alive(after["execution"]["process_identity"]))
        with self._connection() as connection:
            connection.inventory()
            self.assertFalse(self._call(connection, "browser_navigate", {"url": self.allowed + "/app"}).get("isError"))
            self.assertFalse(self._call(connection, "browser_click", {"target": "#add"}).get("isError"))
            fixed = self._call(connection, "browser_wait_for", {"text": "1"})
            self.assertFalse(fixed.get("isError"), fixed)
            self.assertIn("1", json.dumps(fixed))
        (self.private / "regression-scenario.json").write_text(json.dumps({
            "kind": "actual_browser_regression_fixture", "production_application_acceptance": False,
            "reproduced_snapshot": reproduced, "fixed_observation": fixed,
            "independent_regression_before_exit": before["regression"]["exit_code"],
            "independent_regression_after_exit": after["regression"]["exit_code"],
            "managed_before_receipt": before["receipt_path"], "managed_after_receipt": after["receipt_path"],
            "scope": "single button counter test application; increment 2 corrected to increment 1",
            "regression_command": command, "app_before": template.replace("STEP", "2"),
            "app_after": app.read_text()}, indent=2))


if __name__ == "__main__":
    unittest.main()
