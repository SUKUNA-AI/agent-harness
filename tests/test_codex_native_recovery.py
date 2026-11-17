"""Contained fake-RPC recovery regressions; synthetic proof is NOT qualification."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agent_harness.codex_app_server import StrictCodexAdapter
from agent_harness.core import digest, process_identity
from agent_harness import codex_qualification as qualification

FAKE = Path(__file__).resolve().parent / 'tooling' / 'fake_codex_app_server.py'


@contextmanager
def fixture(root, mode):
    """Pin a clearly synthetic proof; keep the actual final closure verifier."""
    home, runtime = root / 'home', root / 'runtime'
    home.mkdir(exist_ok=True)
    runtime.mkdir(exist_ok=True)

    def reference(name, value):
        raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
        path = root / name
        path.write_bytes(raw)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    original = {'slug': 'fixture'}
    original_ref = reference('original.json', original)
    narrowed = dict(original, shell_type='disabled', tool_mode='direct',
                    supports_search_tool=False, experimental_supported_tools=[])
    metadata = reference('metadata.json', {'models': [narrowed]})
    scenarios = ['mcp__fixture__read_file', 'exec_command', 'spawn_agent',
                 'mcp__fixture__write_file', 'mcp__fixture__execute_command']
    # Synthetic policy input needed by the real proof-closure validator. These
    # are fixture fields, not claims that a provider or native policy was tested.
    observations = [{'scenario': name, 'provider_requests': 2,
                     'sentinel_unchanged': True, 'process_tree_stopped': True,
                     'mcp_calls': ['read_file'] if name == scenarios[0] else [],
                     'logs': {}, 'log_sha256': {}} for name in scenarios]
    probe = reference('probe.json', {'kind': 'codex-native-policy-probe',
                      'metadata_digest': digest(narrowed), 'role': 'worker',
                      'builtins': [], 'observations': observations})
    catalog = qualification.complete_catalog([], [])
    document = {'schema_version': 1, 'kind': 'codex-native-core-qualification',
                'proof_id': 'synthetic-recovery-test', 'root': str(root), 'role': 'worker',
                'model': 'fixture', 'model_provider': 'fixture', 'identity': {},
                'metadata': metadata, 'metadata_source': dict(original_ref, model_digest=digest(original)),
                'native_policy_probe': probe, 'hooks': [], 'runtime_dir': str(runtime),
                'catalog': catalog, 'catalog_digest': digest(catalog), 'mcp_catalog': [],
                'builtins': [], 'connected_mcp_servers': [], 'read_context': [],
                'created_at': 'synthetic-test-fixture'}
    receipt = reference('proof.json', document)
    adapter = object.__new__(StrictCodexAdapter)
    adapter.executable = (str(Path(sys.executable).resolve()),)
    adapter.environment = {'HOME': str(home), 'CODEX_HOME': str(home), 'PATH': os.defpath}
    if os.name == 'nt' and 'SystemRoot' in os.environ:
        adapter.environment['SystemRoot'] = os.environ['SystemRoot']
    adapter.tool_root = str(root)
    adapter.tool_config = {'mcp_servers': {}}
    adapter.read_context = ()
    adapter._native_qualification = SimpleNamespace(
        receipt=receipt, role='worker', catalog=tuple(catalog), connected=())
    # Bypass only discovery/preflight. The contained RPC process, identity
    # checks, lifecycle, log parsing and final closure validator remain real.
    adapter.preflight = lambda *args, **kwargs: []
    with patch.object(qualification, 'args', return_value=[
            sys.executable, '-B', str(FAKE), str(root), mode]):
        yield adapter


def calls(root):
    return [json.loads(row) for row in (root / 'calls.jsonl').read_text().splitlines()]


def wait_file(path, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(.01)
    raise AssertionError('Fixture milestone unavailable: ' + str(path))


def signal_runner(root):
    with fixture(root, 'turn_stall') as adapter:
        result = adapter.run('fixture', cwd=root, timeout=5, log_dir=root / 'logs',
            on_process=lambda value: (root / 'callback.json').write_text(json.dumps(value)))
    (root / 'result.json').write_text(json.dumps(result))


class CodexNativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='harness-codex-recovery-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def run_mode(self, mode, **options):
        with fixture(self.root, mode) as adapter:
            return adapter.run('fixture', cwd=self.root, timeout=1,
                               log_dir=self.root / 'logs', **options)

    def assert_unknown(self, result):
        self.assertEqual(result['status'], 'unknown_outcome', result)
        self.assertFalse(result['outcome_known'])
        self.assertFalse(result['turn_completed'])
        self.assertTrue(result['process_tree_stopped'])

    def test_process_restart_resumes_exact_thread_without_fresh_fallback(self):
        first = self.run_mode('complete')
        self.assertEqual(first['status'], 'completed', first)
        first_pid = int((self.root / 'fake.pid').read_text())
        self.assertIsNone(process_identity(first_pid))
        second = self.run_mode('complete', session_id=first['thread_id'])
        self.assertEqual(second['status'], 'completed', second)
        self.assertEqual(second['thread_id'], first['thread_id'])
        starts = [json.loads(row) for row in (self.root / 'starts.jsonl').read_text().splitlines()]
        self.assertEqual(len(starts), 2)
        self.assertNotEqual(starts[0]['pid'], starts[1]['pid'])
        methods = [row['method'] for row in calls(self.root)]
        self.assertEqual(methods.count('thread/start'), 1)
        self.assertEqual(methods.count('thread/resume'), 1)
        resumed = next(row for row in calls(self.root) if row['method'] == 'thread/resume')
        self.assertEqual(resumed['params']['threadId'], first['thread_id'])

    def test_missing_exact_session_never_falls_back_to_fresh_thread(self):
        result = self.run_mode('complete', session_id='thread-missing')
        self.assert_unknown(result)
        self.assertEqual(result['thread_id'], 'thread-missing')
        methods = [row['method'] for row in calls(self.root)]
        self.assertIn('thread/resume', methods)
        self.assertNotIn('thread/start', methods)
        self.assertNotIn('turn/start', methods)

    def test_changed_resume_thread_identity_is_rejected_without_turn(self):
        (self.root / 'thread.json').write_text('{"id":"thread-fixture"}')
        result = self.run_mode('wrong_resume', session_id='thread-fixture')
        self.assert_unknown(result)
        methods = [row['method'] for row in calls(self.root)]
        self.assertNotIn('thread/start', methods)
        self.assertNotIn('turn/start', methods)

    def test_turn_response_identity_must_match_notifications(self):
        result = self.run_mode('wrong_turn_response')
        self.assert_unknown(result)
        self.assertIn('turn', result['error'])

    def test_terminal_turn_identity_must_match_active_turn(self):
        result = self.run_mode('wrong_turn_terminal')
        self.assert_unknown(result)
        self.assertIn('terminal', result['error'])

    def test_effective_temporary_write_grants_refuse_before_turn(self):
        result = self.run_mode('unsafe_tmp')
        self.assert_unknown(result)
        self.assertIn('temporary write grants', result['error'])
        self.assertNotIn('turn/start', [row['method'] for row in calls(self.root)])

    def test_partial_jsonl_after_terminal_makes_outcome_unknown(self):
        result = self.run_mode('trailing_partial')
        self.assert_unknown(result)
        log = Path(result['logs']['stdout']).read_text()
        self.assertNotIn('"type":"turn.completed"', log)
        self.assertIn('MCP_CLIENT', result['error'])

    def test_process_callback_is_published_before_initialize_stall(self):
        seen = []
        def callback(value):
            seen.append(value)
            (self.root / 'callback.json').write_text(json.dumps(value))
        with fixture(self.root, 'init_stall') as adapter:
            result = adapter.run('fixture', cwd=self.root, timeout=.5,
                                 log_dir=self.root / 'logs', on_process=callback)
        self.assert_unknown(result)
        self.assertEqual(len(seen), 1)
        observation = json.loads((self.root / 'initialize-observation.json').read_text())
        self.assertTrue(observation['callback_seen'])
        self.assertIsNone(process_identity(int((self.root / 'fake.pid').read_text())))
        self.assertNotIn('thread/start', [row['method'] for row in calls(self.root)])

    @unittest.skipUnless(os.name == 'posix' and hasattr(signal, 'SIGTERM'),
                         'POSIX SIGTERM/process-group lifecycle unsupported on this OS')
    def test_sigterm_runner_reaps_app_server_and_descendant(self):
        environment = {'PATH': os.defpath, 'HARNESS_TEST_SOURCE_ROOT': str(ROOT)}
        wrapper = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
            '--fixture-runner', str(self.root)], env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        fake_pid = child_pid = None
        try:
            wait_file(self.root / 'callback.json')
            wait_file(self.root / 'descendant.pid')
            wait_file(self.root / 'descendant-ready')
            fake_pid = int((self.root / 'fake.pid').read_text())
            child_pid = int((self.root / 'descendant.pid').read_text())
            self.assertIsNotNone(process_identity(fake_pid))
            self.assertIsNotNone(process_identity(child_pid))
            wrapper.send_signal(signal.SIGTERM)
            stdout, stderr = wrapper.communicate(timeout=3)
            self.assertEqual(wrapper.returncode, 0, (stdout, stderr))
            result = json.loads((self.root / 'result.json').read_text())
            self.assertEqual(result['status'], 'cancelled', result)
            self.assertTrue(result['process_tree_stopped'])
            self.assertFalse(result['outcome_known'])
            self.assertIsNone(process_identity(fake_pid))
            self.assertIsNone(process_identity(child_pid))
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.communicate(timeout=2)
            # A failed lifecycle assertion must not leave fixture children alive.
            if fake_pid is not None:
                try:
                    os.killpg(fake_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--fixture-runner':
        signal_runner(Path(sys.argv[2]).resolve())
    else:
        unittest.main()
