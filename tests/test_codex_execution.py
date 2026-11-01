"""Real contained fake processes exercise edit, exact resume and broken streams."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.codex_execution import CodexAdapter, MAX_PROMPT_BYTES
from agent_harness.core import HarnessError, identity_alive, process_identity

FAKE = r'''
import json,os,pathlib,signal,subprocess,sys,time
args=sys.argv[1:]
if '--version' in args:
    print('codex-fake 1.0');sys.exit(0)
if '--help' in args:
    print('exec resume SESSION_ID --json --output-schema');sys.exit(0)
mode=args[-1]
session='11111111-2222-3333-4444-555555555555'
def event(t,**fields):
    print(json.dumps(dict(type=t,**fields)),flush=True)
if mode=='nonjson':
    print('not a json event',flush=True);sys.exit(0)
if mode=='partial':
    sys.stdout.write('{"type":"thread.started","thread_id":"'+session+'"}');sys.stdout.flush();sys.exit(0)
event('thread.started',thread_id=session)
if mode=='no-turn':sys.exit(0)
if mode=='early-warning':event('item.completed',item={'type':'error','message':'Under-development features enabled: local_thread_store_compression'})
event('turn.started')
if mode=='duplicate':event('thread.started',thread_id=session)
if mode=='timeout':
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    pathlib.Path('descendant.pid').write_text(str(child.pid))
    time.sleep(60)
if mode=='edit':
    pathlib.Path('result.txt').write_text('initial',encoding='utf-8')
if mode=='repair':
    assert args[args.index('resume')+1]==session
    assert pathlib.Path('result.txt').read_text()=='initial'
    pathlib.Path('result.txt').write_text('repaired',encoding='utf-8')
if mode=='mode-check':
    assert 'agents.enabled=false' in args and 'features.multi_agent_v2=false' in args
    assert 'sandbox_mode="read-only"' in args
if mode=='secret':
    event('item.completed',item={'type':'agent_message','text':'api_key='+('sk-'+'q'*40),'access_token':'private-value'})
    print('password=should-not-appear',file=sys.stderr,flush=True)
if mode=='failed':
    event('turn.failed',error={'message':'permission denied by sandbox'});sys.exit(1)
if mode=='abrupt':sys.exit(7)
if mode=='wrong-id':
    event('thread.started',thread_id='different');sys.exit(0)
if mode=='bad-usage':event('turn.completed',usage={'input_tokens':True})
elif mode=='no-usage':event('turn.completed')
else:event('turn.completed',usage={'input_tokens':10,'cached_input_tokens':2,'output_tokens':3})
if mode=='after-terminal':event('item.completed',item={'type':'agent_message','text':'late'})
'''


class CodexExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.cwd = self.folder / "source"
        self.cwd.mkdir()
        self.fake = self.folder / "fake_codex.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.adapter = CodexAdapter((sys.executable, str(self.fake)))
        self.logs = self.folder / "private" / "logs"

    def tearDown(self):
        self.temp.cleanup()

    def run_fake(self, mode, **kwargs):
        return self.adapter.run(mode, cwd=self.cwd, log_dir=self.logs, timeout=kwargs.pop("timeout", 5), **kwargs)

    def test_edits_and_exact_resume_with_durable_callbacks(self):
        observations, identities = [], []
        def process_callback(value):
            self.assertTrue(identity_alive(value["identity"]))
            identities.append(value)
        def event_callback(value):
            # Event is already durably retrievable when it reaches supervisor.
            self.assertTrue(any(value["type"] in p.read_text() for p in self.logs.glob("*.jsonl")))
            observations.append(value)
        initial = self.run_fake("edit", on_process=process_callback, on_event=event_callback)
        self.assertEqual(initial["status"], "completed", initial)
        self.assertEqual((self.cwd / "result.txt").read_text(), "initial")
        repaired = self.run_fake("repair", session_id=initial["thread_id"])
        self.assertEqual(repaired["status"], "completed", repaired)
        self.assertEqual(repaired["thread_id"], initial["thread_id"])
        self.assertEqual((self.cwd / "result.txt").read_text(), "repaired")
        self.assertEqual(initial["usage"]["input_tokens"], 10)
        self.assertEqual(len(identities), 1)
        self.assertEqual(observations[0]["type"], "thread.started")
        self.assertTrue(initial["log_sha256"]["stdout"])

    def test_help_capability_probe_does_not_claim_tool_suppression(self):
        capabilities = self.adapter.capabilities()
        self.assertEqual(capabilities["status"], "observed")
        self.assertTrue(capabilities["json"])
        self.assertTrue(capabilities["resume_output_schema"])
        self.assertFalse(capabilities["native_children_disabled"])
        self.assertEqual(len(capabilities["binary_sha256"]), 64)
        result = self.run_fake("mode-check", read_only=True)
        self.assertEqual(result["status"], "completed", result)

    def test_missing_usage_unavailable_and_model_failure_not_accepted(self):
        result = self.run_fake("no-usage")
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["usage"])
        failed = self.run_fake("failed")
        self.assertEqual(failed["status"], "failed")
        self.assertNotEqual(failed["exit_code"], 0)
        self.assertFalse(failed["outcome_known"])

    def test_partial_missing_out_of_order_or_abrupt_stream_never_completed(self):
        for mode in ["partial", "nonjson", "no-turn", "duplicate", "abrupt", "bad-usage", "after-terminal"]:
            with self.subTest(mode=mode):
                result = self.run_fake(mode)
                self.assertNotEqual(result["status"], "completed", result)
                self.assertFalse(result["outcome_known"])

    def test_timeout_contains_descendant_and_retains_unknown_writes(self):
        observed = []
        result = self.run_fake("timeout", timeout=0.5, on_process=observed.append)
        self.assertEqual(result["status"], "timed_out", result)
        self.assertFalse(result["outcome_known"])
        self.assertFalse(identity_alive(observed[0]["identity"]))
        pid_file = self.cwd / "descendant.pid"
        self.assertTrue(pid_file.exists())
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 3
        while process_identity(child) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(process_identity(child))

    def test_cancel_before_start_does_not_spawn_and_cancel_running_stops_owned_tree(self):
        cancel = self.folder / "cancel"
        cancel.touch()
        called = []
        result = self.run_fake("edit", cancel_file=cancel, on_process=called.append)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["outcome_known"])
        self.assertEqual(called, [])
        self.assertFalse((self.cwd / "result.txt").exists())
        cancel.unlink()
        result = self.run_fake("timeout", cancel_file=cancel, on_process=lambda _: cancel.touch())
        self.assertEqual(result["status"], "cancelled", result)
        self.assertFalse(result["outcome_known"])

    def test_callbacks_failure_cannot_leave_live_process_or_success(self):
        observed = []
        def fail(value):
            observed.append(value)
            raise OSError("checkpoint failed")
        result = self.run_fake("timeout", on_process=fail)
        self.assertEqual(result["status"], "unknown_outcome")
        self.assertFalse(identity_alive(observed[0]["identity"]))

    def test_redacts_secret_keys_and_stdout_stderr_values(self):
        result = self.run_fake("secret")
        self.assertEqual(result["status"], "completed", result)
        output = Path(result["logs"]["stdout"]).read_text()
        errors = Path(result["logs"]["stderr"]).read_text()
        self.assertNotIn("q" * 40, output)
        self.assertNotIn("private-value", output)
        self.assertNotIn("should-not-appear", errors)
        self.assertIn("<redacted>", output)

    def test_rejects_unsafe_id_oversized_prompt_and_no_bypass_flags(self):
        for session in ["--last", "bad id", "../other"]:
            with self.assertRaises(HarnessError):
                self.adapter.argv("fix", session_id=session)
        with self.assertRaises(HarnessError):
            self.adapter.argv("x" * (MAX_PROMPT_BYTES + 1))
        argv = self.adapter.argv("fix", session_id="11111111-2222-3333-4444-555555555555")
        self.assertNotIn("--worktree", argv)
        self.assertNotIn("--last", argv)
        self.assertFalse(any("danger" in x or x == "--yolo" for x in argv))

    def test_observed_pre_turn_diagnostic_is_preserved_without_weakening_terminal_checks(self):
        result = self.run_fake("early-warning")
        self.assertEqual(result["status"], "completed", result)
        self.assertIn("Under-development features enabled", Path(result["logs"]["stdout"]).read_text())

    def test_expired_absolute_deadline_prevents_process_creation(self):
        created = []
        result = self.run_fake("edit", deadline="2000-01-01T00:00:00+00:00", on_process=created.append)
        self.assertEqual(result["status"], "timed_out", result)
        self.assertEqual(created, [])
        self.assertFalse((self.cwd / "result.txt").exists())


if __name__ == "__main__":
    unittest.main()
