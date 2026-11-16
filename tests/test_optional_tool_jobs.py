"""Optional utility integration without model/API calls or tool installation."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, identity_alive, process_identity
from agent_harness.optional_tool_jobs import (BrowserJob, DocumentJob, probe_network_isolation,
    run_browser_job, run_document_job)
from agent_harness.optional_tool_jobs import _playwright_package

FIXTURE = Path(__file__).resolve().parent / "tooling" / "fake_document_converter.py"

FAKE_PLAYWRIGHT = r'''
from pathlib import Path
class Route:
 def __init__(self,url): self.request=type('Request',(),{'url':url})();self.effect=None
 def continue_(self): self.effect='continue'
 def abort(self): self.effect='abort'
class WebSocket:
 def __init__(self,url): self.url=url;self.closed=False
 def close(self,code,reason): assert code==1008 and reason;self.closed=True
class Locator:
 def click(self): pass
 def fill(self,value): pass
 def inner_text(self): return Path(__file__).with_name('content.txt').read_text()
 def is_visible(self): return True
class Page:
 def __init__(self,context): self.context=context
 def set_default_timeout(self,value): assert 0<value<=10000
 def on(self,*args): pass
 def goto(self,url,wait_until):
  assert wait_until=='domcontentloaded'
  local=Route(url);self.context.handler(local);assert local.effect=='continue'
  external=Route('https://outside.invalid/');self.context.handler(external);assert external.effect=='abort'
  for address in ['ws://outside.invalid/socket','wss://outside.invalid/socket','ws://127.0.0.1:8123/socket']:
   websocket=WebSocket(address);self.context.websocket_handler(websocket);assert websocket.closed
 def locator(self,selector): assert isinstance(selector,str);return Locator()
 def screenshot(self,path,full_page):
  assert full_page is False
  Path(path).write_bytes(b'PNG-fixture-only')
class Context:
 def route(self,pattern,handler): assert pattern=='**/*';self.handler=handler
 def route_web_socket(self,pattern,handler): assert pattern=='**/*';self.websocket_handler=handler
 def new_page(self): assert hasattr(self,'websocket_handler');return Page(self)
 def close(self): pass
class Browser:
 def new_context(self,**options):
  assert options['accept_downloads'] is False and options['service_workers']=='block'
  return Context()
 def close(self): pass
class Chromium:
 def launch(self,**options):
  assert options['headless'] is True and options['chromium_sandbox'] is True
  return Browser()
class Runtime:
 chromium=Chromium()
 def __enter__(self): return self
 def __exit__(self,*args): pass
def sync_playwright(): return Runtime()
'''


class OptionalToolJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.document = self.source / "small.txt"
        self.document.write_text("маленький документ\n", encoding="utf-8")
        self.private = self.root / "private"
        self.private.mkdir()

    def document_job(self, mode="normal", **kwargs):
        return DocumentJob(input_root=self.source, input_path=self.document, output_root=self.private,
            command=(sys.executable, str(FIXTURE), mode, "{input}", "{output}"),
            tool_version="fixture-document-converter 1", version_argv=(sys.executable, str(FIXTURE), "--version"),
            mode="remote", **kwargs)

    def browser_job(self, **kwargs):
        return BrowserJob(root=self.source, artifact_root=self.private, url="http://127.0.0.1:8123/example",
            actions=({"kind": "expect_text", "selector": "#result", "value": "fixed"},),
            regression_argv=(sys.executable, "-c", "from pathlib import Path; assert Path('small.txt').is_file()"), **kwargs)

    def fake_playwright(self, content="fixed"):
        package = self.root / "fixture-runtime" / "playwright"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "sync_api.py").write_text(FAKE_PLAYWRIGHT)
        (package / "content.txt").write_text(content)
        return {"package_root": str(package.parent), "origin": str(package / "__init__.py"),
            "origin_sha256": hashlib.sha256(b"").hexdigest(), "version": "fixture-playwright-only"}

    def test_remote_conversion_real_subprocess_has_bounded_outputs_mode_and_receipts(self):
        receipt = run_document_job(self.document_job())
        self.assertEqual(receipt["status"], "COMPLETED", receipt)
        self.assertEqual(receipt["requested_conversion_mode"], "remote")
        self.assertEqual(receipt["actual_conversion_mode"], "remote")
        self.assertEqual((Path(receipt["output_root"]) / "converted.md").read_text(), "МАЛЕНЬКИЙ ДОКУМЕНТ\n")
        self.assertEqual(receipt["input"]["sha256"], hashlib.sha256(self.document.read_bytes()).hexdigest())
        self.assertTrue(receipt["execution"]["process_tree_stopped"])
        self.assertFalse(identity_alive(receipt["execution"]["process_identity"]))
        events = [json.loads(line) for line in Path(receipt["events_receipt"]["path"]).read_text().splitlines()]
        self.assertEqual(events[0]["type"], "process.started")
        self.assertEqual(events[-1]["type"], "process.stopped")
        self.assertIn("fixture", receipt["tool"]["version"])
        self.assertEqual(receipt["acceptance"], "independent_final_gate_required")
        self.assertNotIn("маленький документ", Path(receipt["receipt_path"]).read_text())

    def test_local_only_never_falls_back_to_host_network(self):
        denied = {"status": "BLOCKED_NOT_QUALIFIED", "reason": "fixture namespace denial"}
        with patch("agent_harness.optional_tool_jobs.probe_network_isolation", return_value=denied):
            receipt = run_document_job(replace(self.document_job(), mode="local-only"))
        self.assertEqual(receipt["status"], "BLOCKED_NOT_QUALIFIED")
        self.assertEqual(receipt["actual_conversion_mode"], "not_run")
        self.assertEqual(list(self.private.iterdir()), [])

    def test_invalid_document_bounds_paths_and_argv_fail_before_job_creation(self):
        bad = [replace(self.document_job(), mode="automatic"), replace(self.document_job(), expected_output="../../escape"),
            replace(self.document_job(), timeout=float("nan")), replace(self.document_job(), max_input_bytes=True),
            replace(self.document_job(), command=(sys.executable, "--password", "fixture-private-value", "{input}", "{output}")),
            replace(self.document_job(), output_root=self.source),
            replace(self.document_job(), input_path=self.root / "outside.txt")]
        for job in bad:
            with self.subTest(job=job):
                with self.assertRaises((HarnessError, ValueError)):
                    run_document_job(job)
                self.assertEqual(list(self.private.iterdir()), [])

    def test_document_input_size_checked_before_converter_spawn(self):
        with self.assertRaises(ValueError):
            run_document_job(self.document_job(max_input_bytes=1))
        self.assertEqual(list(self.private.iterdir()), [])

    def test_converter_version_mismatch_is_not_a_conversion(self):
        receipt = run_document_job(replace(self.document_job(), tool_version="wrong version"))
        self.assertEqual(receipt["status"], "FAILED", receipt)
        self.assertEqual(receipt["actual_conversion_mode"], "not_run")
        self.assertNotIn("artifacts", receipt)

    def test_converter_timeout_crash_and_output_limit_fail_with_tree_cleanup(self):
        for mode in ("slow", "crash", "oversize"):
            with self.subTest(mode=mode):
                receipt = run_document_job(self.document_job(mode, timeout=0.3, max_output_bytes=1024))
                self.assertEqual(receipt["status"], "FAILED", receipt)
                self.assertTrue(receipt["execution"]["process_tree_stopped"])
                self.assertFalse(identity_alive(receipt["execution"]["process_identity"]))
                self.assertNotIn("artifacts", receipt)

    @unittest.skipIf(os.name == "nt", "POSIX fixture symlink creation; ordinary/reparse checks are shared")
    def test_converter_symlink_output_cannot_become_verified_artifact(self):
        receipt = run_document_job(self.document_job("link"))
        self.assertEqual(receipt["status"], "FAILED", receipt)
        self.assertNotIn("artifacts", receipt)

    def test_leader_success_cleans_up_lingering_converter_descendant(self):
        receipt = run_document_job(self.document_job("child"))
        self.assertEqual(receipt["status"], "COMPLETED", receipt)
        pid = int((Path(receipt["output_root"]) / "child.pid").read_text())
        identity = process_identity(pid)
        if identity:
            self.assertFalse(identity_alive(identity))
        self.assertTrue(receipt["execution"]["process_tree_stopped"])

    def test_browser_dependency_absence_is_explicit_without_starting_regression(self):
        with patch("agent_harness.optional_tool_jobs._playwright_package", return_value=None):
            receipt = run_browser_job(self.browser_job())
        self.assertEqual(receipt["status"], "BLOCKED_NOT_QUALIFIED")
        self.assertEqual(list(self.private.iterdir()), [])

    def test_browser_rejects_external_urls_missing_regression_and_weak_assertions(self):
        for job in (replace(self.browser_job(), url="https://outside.invalid"),
                replace(self.browser_job(), regression_argv=()),
                replace(self.browser_job(), actions=({"kind": "click", "selector": "#result"},)),
                replace(self.browser_job(), actions=({"kind": "evaluate", "selector": "body", "value": "code"},))):
            with self.assertRaises(HarnessError):
                run_browser_job(job)
        self.assertEqual(list(self.private.iterdir()), [])

    def test_fixture_browser_context_and_independent_regression_must_both_pass(self):
        identity = self.fake_playwright()
        with patch("agent_harness.optional_tool_jobs._playwright_package", return_value=identity):
            receipt = run_browser_job(self.browser_job())
        self.assertEqual(receipt["status"], "COMPLETED", receipt)
        self.assertTrue(receipt["browser"]["context_closed"])
        self.assertTrue(receipt["browser"]["browser_closed"])
        self.assertEqual(receipt["browser"]["external_requests_blocked"], 1)
        self.assertEqual(receipt["regression"]["exit_code"], 0)
        self.assertEqual(receipt["playwright"]["version"], "fixture-playwright-only")
        self.assertTrue(Path(receipt["events_receipt"]["path"]).is_file())

    def test_browser_blocks_ws_and_wss_before_any_page_can_create_them(self):
        identity = self.fake_playwright()
        with patch("agent_harness.optional_tool_jobs._playwright_package", return_value=identity):
            receipt = run_browser_job(self.browser_job())
        self.assertEqual(receipt["status"], "COMPLETED", receipt)
        self.assertEqual(receipt["browser"]["websockets_blocked"], 3)
        self.assertIn("no_websockets", receipt["context_policy"])

    def test_browser_missing_websocket_interception_blocks_before_regression(self):
        identity = self.fake_playwright()
        module = Path(identity["package_root"]) / "playwright" / "sync_api.py"
        module.write_text(module.read_text().replace(" def route_web_socket(", " def unsupported_route_web_socket("))
        with patch("agent_harness.optional_tool_jobs._playwright_package", return_value=identity):
            receipt = run_browser_job(self.browser_job())
        self.assertEqual(receipt["status"], "BLOCKED_NOT_QUALIFIED", receipt)
        self.assertIn("WebSocket interception", receipt["reason"])
        self.assertNotIn("regression", receipt)
        self.assertTrue(receipt["browser"]["context_closed"])
        self.assertTrue(receipt["browser"]["browser_closed"])

    def test_fixture_browser_screenshot_cannot_hide_failed_regression(self):
        identity = self.fake_playwright()
        with patch("agent_harness.optional_tool_jobs._playwright_package", return_value=identity):
            receipt = run_browser_job(replace(self.browser_job(), regression_argv=(sys.executable, "-c", "raise SystemExit(1)")))
        self.assertEqual(receipt["browser"]["status"], "PASSED")
        self.assertEqual(receipt["status"], "FAILED")

    def test_fixture_browser_reproduction_needs_actual_assertion_and_regression_failure(self):
        identity = self.fake_playwright("broken")
        with patch("agent_harness.optional_tool_jobs._playwright_package", return_value=identity):
            receipt = run_browser_job(replace(self.browser_job(expectation="reproduces"),
                regression_argv=(sys.executable, "-c", "raise SystemExit(1)")))
        self.assertEqual(receipt["status"], "REPRODUCED", receipt)
        self.assertEqual(receipt["browser"]["status"], "ASSERTION_FAILED")
        self.assertNotEqual(receipt["regression"]["exit_code"], 0)
        self.assertTrue(receipt["execution"]["process_tree_stopped"])

    def test_live_local_network_namespace_or_explicit_unqualified_skip(self):
        network = probe_network_isolation(timeout=3)
        if network["status"] != "QUALIFIED":
            self.skipTest("BLOCKED_NOT_QUALIFIED: " + network["reason"])
        self.assertNotEqual(network["host_namespace"], network["observed_namespace"])
        self.assertFalse(set(network["interfaces"]) - {"lo"})
        receipt = run_document_job(replace(self.document_job("local"), mode="local-only"))
        self.assertEqual(receipt["status"], "COMPLETED", receipt)
        self.assertEqual(receipt["actual_conversion_mode"], "local-only")
        actual = (Path(receipt["output_root"]) / "converted.md").read_text().splitlines()[0]
        self.assertNotIn(network["host_namespace"], actual)

    def test_live_playwright_package_or_explicit_unqualified_skip(self):
        if _playwright_package() is None:
            self.skipTest("BLOCKED_NOT_QUALIFIED: actual Playwright Python package unavailable")
        # A real browser flow additionally needs the operator's actual local app.
        self.skipTest("BLOCKED_NOT_QUALIFIED: no operator local app/browser fixture supplied for live flow")


if __name__ == "__main__":
    unittest.main()
