"""Real contained fake V2 processes; no inference, tokens, DB or daemon needed."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, identity_alive
from agent_harness.opencode_execution import OpenCodeAdapter, OpenCodeEventNormalizer, _environment
from agent_harness.task_packets import NotDispatched


FAKE = r'''
import hashlib,json,os,pathlib,signal,subprocess,sys,time
args=sys.argv[1:]
if '--version' in args:
    print(os.environ.get('FAKE_OPENCODE_VERSION','opencode v2.2.5'));sys.exit(0)
if '--help' in args:
    print('run --format json --session SESSION --standalone; session list export');sys.exit(0)
assert args[0]=='run' and '--standalone' in args and '--format' in args
assert '--continue' not in args and '--server' not in args and '--auto' not in args
mode=args[-1]
session='ses_'+hashlib.sha256(str(pathlib.Path.cwd()).encode()).hexdigest()[:16]
session_file=pathlib.Path('fixture-session.json')
if '--session' in args:
    requested=args[args.index('--session')+1]
    assert session_file.exists() and json.loads(session_file.read_text())['id']==requested
    assert requested==session
else:
    session_file.write_text(json.dumps({'id':session,'cwd':str(pathlib.Path.cwd())}))
def event(t,**fields):
    print(json.dumps(dict(type=t,timestamp=1,sessionID=session,**fields)),flush=True)
def part(t,**fields):
    return dict(type=t,sessionID=session,messageID='msg_a',**fields)
if mode=='partial':
    sys.stdout.write('{"type":"step_start","sessionID":"'+session+'"}');sys.stdout.flush();sys.exit(0)
if mode=='corrupt':print('{not-json}',flush=True);sys.exit(0)
if mode=='duplicate-key':print('{"type":"error","type":"text"}',flush=True);sys.exit(0)
event('step_start',part=part('step-start'))
if mode=='wait':
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    pathlib.Path('descendant.pid').write_text(str(child.pid))
    time.sleep(60)
if mode=='edit':pathlib.Path('result.txt').write_text('initial')
if mode=='resume':
    assert '--session' in args
    pathlib.Path('result.txt').write_text('resumed')
if mode=='catalog':
    config=json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
    rules=config['agents'][config['default_agent']]['permissions']
    def allow(name):
        import fnmatch
        effect='ask'
        for rule in rules:
            if fnmatch.fnmatchcase(name,rule['action']) and rule['resource']=='*':effect=rule['effect']
        return effect=='allow'
    tools=['fake_search_symbol','fake_read_file','fake_write_file','fake_execute_command','shell','edit','execute','subagent']
    allowed=[x for x in tools if allow(x)]
    assert allowed==['fake_search_symbol','fake_read_file'],allowed
    # Exercise the fixture's effective dispatcher for every prohibited action.
    for tool in tools[2:]:
        assert not allow(tool)
    assert not pathlib.Path('forbidden-write.txt').exists()
    event('text',part=part('text',text=json.dumps({'tools':allowed,'denied':tools[2:]})))
elif mode=='scope':
    event('text',part=part('text',text=pathlib.Path('symbol.py').read_text()))
elif mode=='secret':
    event('text',part=part('text',text='api_key=sk-'+'q'*40))
else:event('text',part=part('text',text='ok'))
if mode=='conflicting':
    print(json.dumps({'type':'text','sessionID':'ses_other','part':part('text',text='wrong')}),flush=True);sys.exit(0)
if mode=='nested-conflict':
    event('text',part={'sessionID':'ses_other','messageID':'msg_a','text':'wrong'});sys.exit(0)
if mode in ['subagent','execute']:
    event('tool_use',part=part('tool',tool=mode,state={'status':'completed'}));sys.exit(0)
if mode=='crash':sys.exit(7)
if mode=='failed':event('error',error={'message':'permission denied'});sys.exit(1)
if mode=='no-finish':sys.exit(0)
event('step_finish',part=part('step-finish',reason='tool-calls' if mode=='intermediate' else 'stop'))
if mode=='late':event('text',part=part('text',text='late'))
'''


class FixtureOpenCodeAdapter(OpenCodeAdapter):
    """Fixture-only verifier; deliberately unavailable from the production CLI."""
    qualify = None
    def _qualification(self):
        return {"managed_leaf_qualified": True, "exact_resume_qualified": True,
                "reviewer_isolation_qualified": True}

    def with_environment(self, environment):
        clone = copy.copy(self)
        clone.environment = _environment(environment)
        return clone


class OpenCodeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cwd = self.root / 'source'
        self.cwd.mkdir()
        self.fake = self.root / 'fake_opencode.py'
        self.fake.write_text(FAKE, encoding='utf-8')
        self.executable = (sys.executable, str(self.fake))
        self.adapter = FixtureOpenCodeAdapter(self.executable)
        self.logs = self.root / 'private' / 'logs'

    def tearDown(self):
        self.temp.cleanup()

    def run_fake(self, mode, **options):
        return self.adapter.run(mode, cwd=self.cwd, timeout=options.pop('timeout', 5), log_dir=self.logs, **options)

    def test_v2_capabilities_are_separate_from_real_qualification(self):
        real = OpenCodeAdapter(self.executable)
        caps = real.capabilities()
        self.assertEqual(caps['version'], 'opencode v2.2.5')
        self.assertTrue(caps['json'] and caps['resume'] and caps['standalone'])
        self.assertFalse(caps['output_schema'])
        self.assertFalse(caps['native_children_disabled'])
        self.assertFalse(caps['managed_leaf_qualified'])
        self.assertEqual(caps['qualification_status'], 'BLOCKED_NOT_QUALIFIED')
        with self.assertRaisesRegex(NotDispatched, 'BLOCKED_NOT_QUALIFIED'):
            real.run('edit', cwd=self.cwd)
        self.assertFalse((self.cwd / 'result.txt').exists())

    def test_standalone_exact_session_roundtrip_after_adapter_restart(self):
        observations = []
        initial = self.run_fake('edit', on_event=observations.append)
        self.assertEqual(initial['status'], 'completed', initial)
        self.adapter = FixtureOpenCodeAdapter(self.executable)
        resumed = self.run_fake('resume', session_id=initial['thread_id'])
        self.assertEqual(resumed['status'], 'completed', resumed)
        self.assertEqual(initial['thread_id'], resumed['thread_id'])
        self.assertEqual((self.cwd / 'result.txt').read_text(), 'resumed')
        self.assertNotEqual(initial['process']['identity'], resumed['process']['identity'])
        self.assertTrue(initial['process_tree_stopped'] and resumed['process_tree_stopped'])
        self.assertTrue(Path(initial['logs']['stdout']).exists())
        self.assertTrue(initial['log_sha256']['stdout'])
        self.assertEqual(observations[0]['type'], 'thread.started')

    def test_two_parallel_workers_have_distinct_sessions_cwd_and_receipts(self):
        a, b = self.root / 'a', self.root / 'b'
        a.mkdir(); b.mkdir()
        (a / 'symbol.py').write_text('def target(): return "A"')
        (b / 'symbol.py').write_text('def target(): return "B"')
        def execute(path):
            return self.adapter.run('scope', cwd=path, timeout=5, log_dir=self.root / ('logs-' + path.name))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(execute, (a, b)))
        for result, expected, other in zip(results, ('A', 'B'), ('B', 'A')):
            self.assertEqual(result['status'], 'completed', result)
            text = Path(result['logs']['stdout']).read_text()
            self.assertIn(expected, text)
            self.assertNotIn('return \\"' + other, text)
            self.assertEqual(result['process']['cwd'], str(a if expected == 'A' else b))
        self.assertNotEqual(results[0]['thread_id'], results[1]['thread_id'])
        self.assertNotEqual(results[0]['process']['identity'], results[1]['process']['identity'])

    def test_partial_corrupt_conflicting_or_incomplete_streams_are_unknown(self):
        for mode in ('partial', 'corrupt', 'duplicate-key', 'conflicting', 'nested-conflict', 'crash', 'no-finish', 'intermediate', 'late'):
            with self.subTest(mode=mode):
                result = self.run_fake(mode)
                self.assertEqual(result['status'], 'unknown_outcome', result)
                self.assertFalse(result['outcome_known'])
                self.assertTrue(result['process_tree_stopped'])

    def test_exact_resume_rejects_changed_session_id(self):
        result = self.run_fake('edit')
        resumed = self.run_fake('resume', session_id='ses_different')
        self.assertEqual(resumed['status'], 'unknown_outcome', resumed)
        self.assertNotEqual(resumed['status'], 'completed')
        self.assertEqual((self.cwd / 'result.txt').read_text(), 'initial')

    def test_observed_native_subagent_or_codemode_tool_is_uncertain(self):
        for mode in ('subagent', 'execute'):
            with self.subTest(mode=mode):
                result = self.run_fake(mode)
                self.assertEqual(result['status'], 'unknown_outcome', result)
                self.assertIn('Forbidden', result['error'])

    def test_interrupt_restart_resume_preserves_session_and_history(self):
        cancel = self.root / 'cancel'
        def event(value):
            if value['type'] == 'thread.started':
                cancel.touch()
        interrupted = self.run_fake('wait', cancel_file=cancel, on_event=event)
        self.assertEqual(interrupted['status'], 'cancelled', interrupted)
        self.assertFalse(interrupted['outcome_known'])
        self.assertFalse(identity_alive(interrupted['process']['identity']))
        self.assertTrue(interrupted['process_tree_stopped'])
        cancel.unlink()
        self.adapter = FixtureOpenCodeAdapter(self.executable)
        resumed = self.run_fake('resume', session_id=interrupted['thread_id'])
        self.assertEqual(resumed['status'], 'completed', resumed)
        self.assertEqual(resumed['thread_id'], interrupted['thread_id'])
        self.assertTrue(Path(interrupted['logs']['stdout']).exists())
        self.assertNotEqual(interrupted['logs']['stdout'], resumed['logs']['stdout'])

    def test_v2_readonly_effective_dispatcher_denies_write_shell_execute_subagent(self):
        config = {'default_agent': 'harness-leaf', 'agents': {'harness-leaf': {'mode': 'primary', 'permissions': [
            {'action': '*', 'resource': '*', 'effect': 'deny'},
            {'action': 'fake_search_symbol', 'resource': '*', 'effect': 'allow'},
            {'action': 'fake_read_file', 'resource': '*', 'effect': 'allow'}]}}}
        self.adapter = self.adapter.with_tool_config(config, root=self.cwd)
        result = self.run_fake('catalog', read_only=True)
        self.assertEqual(result['status'], 'completed', result)
        events = [json.loads(x) for x in Path(result['logs']['stdout']).read_text().splitlines()]
        catalog = json.loads(next(x['item']['text'] for x in events if x.get('item', {}).get('type') == 'agent_message'))
        self.assertEqual(catalog['tools'], ['fake_search_symbol', 'fake_read_file'])
        self.assertIn('fake_write_file', catalog['denied'])
        self.assertFalse((self.cwd / 'forbidden-write.txt').exists())

    def test_profile_config_and_worktree_drift_block_before_spawn(self):
        config = {'mcp': {'servers': {}}}
        artifact = self.root / 'owned.json'
        artifact.write_text(json.dumps(config))
        self.adapter = self.adapter.with_tool_config(config, config_path=artifact, root=self.cwd)
        other = self.root / 'other'; other.mkdir()
        with self.assertRaisesRegex(NotDispatched, 'worktree'):
            self.adapter.run('edit', cwd=other)
        artifact.write_text('{"mcp":{"servers":{"unknown":{}}}}')
        with self.assertRaisesRegex(NotDispatched, 'drift'):
            self.run_fake('edit')
        self.assertFalse((self.cwd / 'result.txt').exists())

    def test_no_model_or_user_config_override_and_no_continue_or_shared_server(self):
        argv = self.adapter.argv('task', session_id='ses_exact')
        self.assertEqual(argv[argv.index('--session') + 1], 'ses_exact')
        self.assertIn('--standalone', argv)
        for forbidden in ('--continue', '--fork', '--auto', '--model', '--server', '--worktree'):
            self.assertNotIn(forbidden, argv)
        for config in ({'model': 'different'}, {'providers': {}}, {'auth': {}}):
            with self.assertRaises(HarnessError):
                self.adapter.with_tool_config(config)
        with self.assertRaisesRegex(HarnessError, 'reasoning-effort'):
            self.adapter.argv('task', reasoning_effort='high')
        with self.assertRaisesRegex(HarnessError, 'native subagents'):
            self.adapter.argv('task', native_children=True)

    def test_environment_clone_is_bounded_and_does_not_require_codex_home(self):
        source = OpenCodeAdapter(self.executable)
        clone = source.with_environment({'PATH': os.defpath, 'HOME': str(self.root)})
        self.assertIsNone(source.environment)
        self.assertNotIn('CODEX_HOME', clone.environment)
        self.assertEqual(source.executable, clone.executable)
        with self.assertRaises(HarnessError):
            clone.with_environment({'PATH': 'one', 'path': 'two'})
        configured = source.with_tool_config({'mcp': {'servers': {}}})
        copied = configured.with_environment({'PATH': os.defpath})
        self.assertEqual(copied.tool_config_digest, configured.tool_config_digest)
        self.assertEqual(copied.environment['OPENCODE_CONFIG_CONTENT'], configured.environment['OPENCODE_CONFIG_CONTENT'])
        with self.assertRaises(HarnessError):
            configured.with_environment({'OPENCODE_CONFIG_CONTENT': '{}'})

    def test_no_model_smoke_filters_credentials_and_cannot_grant_qualification(self):
        fake = self.root / 'smoke.py'
        fake.write_text('''import json,os,pathlib,sys
assert "PRIVATE_FIXTURE_CREDENTIAL" not in os.environ
assert sys.argv[1:]==["--standalone","--print-logs","debug","config"]
assert os.environ["OPENCODE_DISABLE_PROJECT_CONFIG"]=="1"
assert pathlib.Path.cwd().name=="project"
assert all(x["effect"]=="deny" for x in json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])["policies"])
print("{}")
''')
        adapter = OpenCodeAdapter((sys.executable, str(fake)), environment=dict(os.environ, PRIVATE_FIXTURE_CREDENTIAL='never-log'))
        result = adapter.live_smoke(root=self.cwd)
        self.assertTrue(result['standalone_ready'], result)
        self.assertEqual(result['status'], 'BLOCKED_NOT_QUALIFIED')
        self.assertTrue(result['no_model_calls'])
        self.assertFalse(result['project_profile_qualified'])

    def test_unrecognized_version_is_fail_closed_and_absence_is_optional(self):
        adapter = self.adapter.with_environment(dict(os.environ, FAKE_OPENCODE_VERSION='opencode v1.99.0'))
        self.assertEqual(adapter.capabilities()['status'], 'unsupported')
        with self.assertRaisesRegex(HarnessError, 'unavailable'):
            OpenCodeAdapter(('opencode-does-not-exist-fixture',))

    def test_known_failure_and_redacted_event_receipts(self):
        failed = self.run_fake('failed')
        self.assertEqual(failed['status'], 'failed', failed)
        self.assertTrue(failed['outcome_known'])
        secret = self.run_fake('secret')
        self.assertEqual(secret['status'], 'completed', secret)
        self.assertNotIn('q' * 40, Path(secret['logs']['stdout']).read_text())
        self.assertEqual(secret['log_sha256']['stdout'], hashlib.sha256(Path(secret['logs']['stdout']).read_bytes()).hexdigest())

    def test_multi_step_normalization_requires_final_stop(self):
        normalizer = OpenCodeEventNormalizer()
        def value(kind, message, **fields):
            return {'type': kind, 'sessionID': 'ses_a', 'part': {'messageID': message, 'sessionID': 'ses_a', **fields}}
        normalizer.feed(value('step_start', 'a'))
        normalizer.feed(value('step_finish', 'a', reason='tool-calls'))
        with self.assertRaises(ValueError):
            normalizer.finish()
        normalizer.feed(value('step_start', 'b'))
        normalizer.feed(value('step_finish', 'b', reason='stop'))
        self.assertEqual(normalizer.finish()[0]['type'], 'turn.completed')

    def test_native_usage_includes_cached_input_writes_and_reasoning(self):
        normalizer = OpenCodeEventNormalizer()
        for index, tokens in enumerate((
                {'input': 1357, 'output': 13, 'reasoning': 51, 'cache': {'read': 0, 'write': 0}},
                {'input': 81, 'output': 111, 'reasoning': 0, 'cache': {'read': 2560, 'write': 0}},
                {'input': 4, 'output': 4, 'reasoning': 0, 'cache': {'read': 0, 'write': 1302}})):
            message = 'msg_' + str(index)
            normalizer.feed({'type': 'step_start', 'sessionID': 'ses_usage', 'part': {'messageID': message}})
            normalizer.feed({'type': 'step_finish', 'sessionID': 'ses_usage', 'part': {
                'messageID': message, 'reason': 'stop' if index == 2 else 'tool-calls', 'tokens': tokens}})
        usage = normalizer.finish()[0]['usage']
        self.assertEqual(usage, {'input_tokens': 1357 + 2641 + 1306, 'output_tokens': 64 + 111 + 4,
            'cached_input_tokens': 2560, 'reasoning_tokens': 51})

    def test_native_missing_or_zero_fallback_usage_is_unknown(self):
        for tokens in (None, {'input': 0, 'output': 0, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}},
                {'input': 7, 'output': 2}):
            normalizer = OpenCodeEventNormalizer()
            normalizer.feed({'type': 'step_start', 'sessionID': 'ses_usage', 'part': {'messageID': 'msg_usage'}})
            part = {'messageID': 'msg_usage', 'reason': 'stop'}
            if tokens is not None:
                part['tokens'] = tokens
            normalizer.feed({'type': 'step_finish', 'sessionID': 'ses_usage', 'part': part})
            usage = normalizer.finish()[0].get('usage')
            self.assertTrue(usage is None or usage['input_tokens'] is None and usage['output_tokens'] is None)

    @unittest.skipUnless(os.environ.get('HARNESS_LIVE_OPENCODE') == '1' and shutil.which('opencode'),
                         'BLOCKED_NOT_QUALIFIED: opt-in real OpenCode standalone/config/permission/resume qualification required')
    def test_live_help_does_not_qualify_permissions_or_resume(self):
        caps = OpenCodeAdapter().capabilities()
        self.assertEqual(caps['status'], 'observed')
        self.assertFalse(caps['managed_leaf_qualified'])


if __name__ == '__main__':
    unittest.main()
