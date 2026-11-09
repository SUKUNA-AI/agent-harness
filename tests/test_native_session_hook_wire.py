"""Real local bootstrap against Codex 0.167.0 embedded command-hook schemas.

The two fixtures were extracted from that client, not inferred from our API.
No Codex process, model, real session log or transcript is opened by these tests.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from agent_harness import native_session as session, native_observability
from agent_harness.core import canonical
from agent_harness.strict_yaml import load, validate
import test_native_session as fixtures


def client_schema(kind):
    path = fixtures.ROOT / 'tests/fixtures' / ('codex-0.167.0-session-start.'+kind+'.schema.json')
    raw = path.read_bytes()
    expected = {'input': 'f7299ffcc96c8ad0ab51df77743632f8374390329b14bef87c4e0d00b2caa4a2',
                'output': '72c8cded2efc7024abfd62692b84934b1b1999cb595665f4a520dc8ebb5eb690'}
    if hashlib.sha256(raw).hexdigest() != expected[kind]:
        raise AssertionError('Extracted client schema fixture changed')
    # Equivalent local reference spelling for the strict validator vocabulary.
    return json.loads(raw.decode().replace('"definitions"', '"$defs"').replace('#/definitions/', '#/$defs/'))


class NativeHookWireTests(unittest.TestCase):
    setUp = fixtures.NativeSessionTests.setUp
    event = fixtures.NativeSessionTests.event
    start = fixtures.NativeSessionTests.start

    def context(self, raw, budget=4096):
        self.assertLessEqual(len(raw)+1, budget)  # emitted LF plus CRLF reserve
        outer = json.loads(raw)
        validate(outer, client_schema('output'))
        specific = outer['hookSpecificOutput']
        self.assertEqual(specific['hookEventName'], 'SessionStart')
        text = specific['additionalContext']
        self.assertTrue(text.startswith(session.HOOK_HEADER.decode()))
        return json.loads(text[len(session.HOOK_HEADER.decode()):])

    def cli(self, event, budget=4096):
        return subprocess.run([sys.executable, str(fixtures.ROOT/'tools/session.py'),
            '--state', str(self.state), '--max-bytes', str(budget), 'hook'],
            input=canonical(event), capture_output=True, timeout=30,
            env={**os.environ, 'PYTHONIOENCODING': 'ascii'})

    def test_shipped_input_bootstraps_without_fabricating_observed_identity(self):
        event = self.event()
        validate(event, client_schema('input'))
        original = deepcopy(event)
        with patch.object(native_observability, 'observe', wraps=native_observability.observe) as observe:
            packet = session.hook_packet(self.state, event)
        self.assertEqual(event, original)
        self.assertIs(observe.call_args.args[1], event)
        self.assertEqual(observe.call_count, 1)
        observed = load(next((self.state/'native-observations').rglob('*.yaml')))
        self.assertIsNone(observed['agent_id'])
        self.assertEqual(observed['agent_type'], 'unknown')
        self.assertEqual(observed['resolution_status'], 'ambiguous')
        self.assertEqual(observed['namespace'], 'unbound')
        required = packet['required']
        self.assertTrue(required['memory'])
        self.assertEqual(required['ledger'], 'inform')
        self.assertEqual(required['goal'], session.HOOK_GOAL)
        self.assertEqual(required['constraints'], session.SESSION_CONSTRAINTS)
        self.assertTrue(Path(required['packet_pointer']).is_file())
        self.assertTrue(Path(required['raw_pointer']).is_file())

    def test_real_cli_success_is_closed_client_json_with_complete_byte_budget(self):
        self.root.rename(self.base/'Проект中文😀')
        self.root = self.base/'Проект中文😀'
        result = self.cli(self.event())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        view = self.context(result.stdout)
        self.assertEqual(view['root'], str(self.root))
        self.assertEqual(view['session'], view['packet']['required']['session'])
        self.assertEqual(view['evidence'], view['packet']['required']['packet_pointer'])
        saved = json.loads(Path(view['evidence']).read_bytes())
        self.assertEqual(view['packet']['required'], saved['required'])

    def test_guarded_cli_degraded_is_valid_json_and_private_payload_is_not_echoed(self):
        for extra in ({'agent_type': 'unknown'}, {'agent_type': 'child'}, {'agent_type': None},
                      {'parent_session_id': 'parent'}, {'agent_type': 'root', 'parent_session_id': 'parent'}):
            with self.subTest(extra=extra):
                result = self.cli(self.event(**extra, prompt='private-marker-never-store', transcript_path='private-marker-never-store'))
                self.assertEqual(result.returncode, 0, result.stderr)
                view = self.context(result.stdout)
                self.assertTrue(view['degraded'])
                self.assertEqual(view['reason_code'], 'NATIVE_BOOTSTRAP_UNAVAILABLE')
                self.assertEqual(view['required_context'], 'not_delivered')
                self.assertFalse((self.state/'native-sessions').exists())
        for path in self.state.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'private-marker-never-store', path.read_bytes())
        rejected = self.cli(self.event(hook_event_name='SubagentStart'))
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(rejected.stdout, b'')

    def test_optional_escaping_overflow_keeps_every_required_field_and_saved_pointer(self):
        packet = self.start()
        packet['items'] = [{'optional': '\\"\n中文😀'*400}]
        saved = self.base/'full-packet.json'
        packet['required']['packet_pointer'] = str(saved)
        session.persist(saved, packet)
        value = {'session': packet['required']['session'], 'root': packet['required']['root'],
                 'evidence': str(saved), 'packet': packet}
        with patch.object(session, 'session_packet', side_effect=AssertionError('Must not repeat bootstrap')):
            raw = session.hook_output(value, max_bytes=4096)
        view = self.context(raw)
        self.assertEqual(view['packet']['required'], packet['required'])
        self.assertEqual({key: view[key] for key in ('session', 'root', 'evidence')},
                         {key: value[key] for key in ('session', 'root', 'evidence')})
        omission = view['packet']['optional_context']
        self.assertTrue(omission['omitted'])
        self.assertEqual(omission['reason'], 'hook_wire_budget')
        self.assertFalse(view['packet']['complete'])
        self.assertEqual(json.loads(Path(omission['pointer']).read_bytes()), packet)

    def test_exact_wire_bound_includes_escaping_and_CRLF_reserve(self):
        packet = self.start()
        required = packet['required']
        value = {'session': required['session'], 'root': required['root'],
                 'evidence': required['packet_pointer'], 'packet': packet}
        raw = session.hook_output(value, max_bytes=65536)
        bound = len(raw)+1
        self.assertEqual(session.hook_output(value, max_bytes=bound), raw)
        narrowed = session.hook_output(value, max_bytes=bound-1)
        self.assertNotEqual(narrowed, raw)
        self.context(narrowed, budget=bound-1)

    def test_required_overflow_and_corrupt_own_checkpoint_are_valid_degraded_context(self):
        packet = self.start()
        required = packet['required']
        value = {'session': required['session'], 'root': required['root'],
                 'evidence': required['packet_pointer'], 'packet': packet}
        required['goal'] = '\\"中文😀'*2000
        before = deepcopy(value)
        view = self.context(session.hook_output(value, max_bytes=2048), budget=2048)
        self.assertEqual(value, before)
        self.assertEqual(view['reason_code'], 'BUDGET_TOO_SMALL')
        self.assertEqual(view['required_context'], 'not_delivered')
        self.assertNotIn('packet', view)
        hook = session.hook_packet(self.state, self.event())
        own = next((self.state/'memory').rglob(hook['required']['session']+'.yaml'))
        own.write_text('broken: [\n')
        result = self.cli(self.event())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.context(result.stdout)['reason_code'], 'NATIVE_BOOTSTRAP_UNAVAILABLE')

    def test_real_minimum_budget_bootstraps_once_or_explicitly_degrades(self):
        # Literal backslashes/quotes cause escaping costs twice in the wire JSON.
        if os.name != 'nt':
            self.root.rename(self.base/'project\\"中文😀')
            self.root = self.base/'project\\"中文😀'
        result = self.cli(self.event(), budget=2048)
        self.assertEqual(result.returncode, 0, result.stderr)
        view = self.context(result.stdout, budget=2048)
        if view.get('degraded'):
            self.assertEqual(view['reason_code'], 'BUDGET_TOO_SMALL')
            self.assertEqual(view['required_context'], 'not_delivered')
        else:
            self.assertTrue(session.packet_path(view['packet'], 'root').samefile(self.root))
            self.assertEqual(view['packet']['required']['constraints'], session.SESSION_CONSTRAINTS)
        metadata = list((self.state/'native-sessions').glob('*/session.json'))
        self.assertLessEqual(len(metadata), 1)
        if metadata:
            self.assertEqual(json.loads(metadata[0].read_bytes())['receipt_generation'], 1)


if __name__ == '__main__':
    unittest.main()
