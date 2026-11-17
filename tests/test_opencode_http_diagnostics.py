"""Bounded native HTTP diagnostics disclose routes and timing, never contents."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_harness.opencode_execution import OpenCodeAdapter
from agent_harness.opencode_qualification import NativeServer, qualify
from agent_harness.tool_profiles import render_opencode


class NativeHttpDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'
        self.root.mkdir()
        self.server = NativeServer(SimpleNamespace(environment={}), self.root, deadline=time.monotonic()+30)

    def test_route_and_stderr_diagnostics_do_not_disclose_payloads(self):
        self.server._read(io.BytesIO(b'Error MCP fixture-secret-token prompt text\n'), 'stderr')
        self.server._request = lambda *args, **kwargs: (200, {'private': 'response-secret'})
        self.server.request('POST', '/api/session/ses_fixture_secret/prompt?key=query-secret', {'text': 'body-secret'})
        view = self.server.diagnostics()
        self.assertEqual(view['requests'][0]['route'], '/api/session/{session}/prompt')
        self.assertEqual(view['stderr']['lines'], 1)
        self.assertEqual(view['stderr']['categories']['mcp'], 1)
        self.assertEqual(len(view['stderr']['sha256']), 64)
        for value in ('fixture-secret-token', 'prompt text', 'ses_fixture_secret', 'query-secret', 'body-secret', 'response-secret'):
            self.assertNotIn(value, json.dumps(view))
        for _ in range(300):
            self.server.request('GET', '/api/mcp')
        self.assertEqual(len(self.server.requests), 256)
        self.assertEqual(self.server.dropped_requests, 45)

    def test_http_timeout_records_operation_without_exception_text(self):
        self.server.address = 'http://127.0.0.1:12345'
        self.server.process = SimpleNamespace(poll=lambda: None)
        with patch('agent_harness.opencode_qualification.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = TimeoutError('fixture-secret-error-text')
            with self.assertRaises(TimeoutError):
                self.server.request('POST', '/api/session/private-session/permission', {'action': 'edit'})
        view = self.server.diagnostics()
        self.assertEqual(view['requests'][0]['error_type'], 'TimeoutError')
        self.assertEqual(view['requests'][0]['route'], '/api/session/{session}/permission')
        self.assertNotIn('fixture-secret-error-text', json.dumps(view))
        self.assertNotIn('private-session', json.dumps(view))

    def test_qualification_failure_persists_exact_stage_and_safe_server_summary(self):
        class FailureServer:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def request(self, method, path, body=None, **kwargs):
                if path == '/api/location': return 200, {'directory': str(self_root)}
                raise AssertionError('unexpected route')
            def data(self, method, path, body=None):
                if path == '/api/agent': return [{'id': 'harness-leaf', 'mode': 'primary'}]
                if path == '/api/plugin': return [{'id': 'agent-harness.execution-guard', 'source': {'type': 'local'}, 'state': {'status': 'active'}}]
                if path == '/api/session': return {'id': 'ses_fixture'}
                if path.endswith('/permission'): raise TimeoutError('must-not-persist-this-text')
                raise AssertionError('unexpected route')
            def diagnostics(self): return {'requests': [{'route': '/api/session/{session}/permission', 'error_type': 'TimeoutError'}]}
        self_root = self.root
        adapter = OpenCodeAdapter(sys.executable, environment={}).with_tool_config(render_opencode('baseline', self.root), root=self.root)
        identity = {'binary': {'sha256': 'a'*64}, 'config_sources': []}
        with patch('agent_harness.opencode_qualification._capture_owned', return_value=subprocess.CompletedProcess([], 0, b'opencode 2.2.5\n', b'')), patch('agent_harness.opencode_qualification.input_identity', return_value=identity), patch('agent_harness.opencode_qualification.NativeServer', FailureServer):
            with self.assertRaises(TimeoutError):
                qualify(adapter, root=self.root, role='worker', evidence_dir=Path(self.tmp.name)/'proof', timeout=30)
        failure = next((Path(self.tmp.name)/'proof').glob('qualification-*/failure.json'))
        value = json.loads(failure.read_text())
        self.assertEqual(value['stage'], 'permission_checks')
        self.assertEqual(value['status'], 'BLOCKED_NOT_QUALIFIED')
        self.assertEqual(value['failure_type'], 'TimeoutError')
        self.assertNotIn('must-not-persist', failure.read_text())
        self.assertTrue(value['native_servers'])


if __name__ == '__main__': unittest.main()
