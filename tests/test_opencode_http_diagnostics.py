"""Bounded native HTTP diagnostics disclose routes and timing, never contents."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_harness.opencode_execution import OpenCodeAdapter
from agent_harness.opencode_qualification import GUARD_SOURCE, NativeServer, _allowed_tools, qualify
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

    def test_mcp_role_validation_preserves_native_hyphenated_server_and_tool_names(self):
        config = render_opencode('baseline', self.root)
        config['mcp']['servers']['public-docs'] = {'codemode': False, 'pool': False}
        config['agents']['harness-leaf']['permissions'].append(
            {'action': 'public-docs_query-docs', 'resource': '*', 'effect': 'allow'})
        _, allowed = _allowed_tools(config, 'worker')
        self.assertIn('public-docs_query-docs', allowed)
        self.assertNotIn('public_docs_query_docs', allowed)

    @unittest.skipUnless(shutil.which('node'), 'Guard hook behavior needs a Node runtime; not native OpenCode qualification')
    def test_prompt_guard_records_actual_hyphenated_names_before_readiness_wait(self):
        script_path = Path(self.tmp.name)/'guard.mjs';script_path.write_text(GUARD_SOURCE)
        output = Path(self.tmp.name)/'readiness.jsonl'
        spec_path = Path(self.tmp.name)/'spec.json'
        spec_path.write_text(json.dumps({'root': str(self.root), 'allowed_tools': ['public-docs_query-docs'],
            'agent': 'harness-leaf', 'role': 'worker', 'phase': 'qualification', 'expected_tools': ['public-docs_query-docs'],
            'connected_servers': ['public-docs'], 'readiness_timeout_ms': 100, 'readiness_path': str(output)}))
        script = ('const plugin=await import('+json.dumps(script_path.as_uri())+');const hooks={};'
            'await plugin.default.setup({location:{directory:'+json.dumps(str(self.root))+'},'
            'session:{hook:async(name,callback)=>{hooks[name]=callback}},'
            'tool:{hook:async()=>{},list:async()=>[{id:"public-docs_query-docs"}]},'
            'permission:{hook:async()=>{}},mcp:{list:async()=>({data:[{name:"public-docs",status:{status:"connected"}}]})}});'
            'await hooks.prompt();')
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script], cwd=self.root,
            env={'PATH': os.environ.get('PATH', ''), 'PWD': str(self.root), 'HARNESS_OPENCODE_GUARD_SPEC': str(spec_path)},
            capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        value = json.loads(output.read_text())
        self.assertEqual(value['observed_tools'], ['public-docs_query-docs'])
        self.assertEqual(value['expected_tools'], value['observed_tools'])
        self.assertEqual(set(value), {'schema_version', 'stage', 'observed_tools', 'expected_tools', 'connected_servers'})

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
