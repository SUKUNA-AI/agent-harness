"""Runtime regressions for C01-C06 and N01 from the audited source tree."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_harness import native_session
from agent_harness.core import HarnessError, Run, digest, snapshot, snapshot_python
from agent_harness.execution_ledger import Ledger, INDEX_SCHEMA
from agent_harness.knowledge_claims import verify_fact
from agent_harness.memory import Memory, SHORT_SCHEMA
from agent_harness.repomap_queries import query_repository
from agent_harness.runner import run_check
from agent_harness.strict_yaml import dump, load
from agent_harness.output import json_bytes


class ContextIntegrity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        (self.root / 'one.py').write_text('def alpha():\n    return 1\n')
        self.state = self.base / 'state'
        self.current = native_session.current_provenance(self.root)
        self.memory = Memory(self.state / 'memory', self.current['repository_identity'], enabled=True)

    def start(self):
        return native_session.session_packet(self.state, root=self.root, goal='Repair alpha')

    def own(self, identifier):
        return self.memory.root / 'short_term' / (identifier + '.yaml')

    def test_killed_goal_publication_requires_recovery_and_preserves_old_head(self):
        identifier = self.start()['required']['session']
        directory = self.state / 'native-sessions' / identifier
        before = (directory / 'session.json').read_bytes()
        checkpoint = self.own(identifier).read_bytes()
        # A real child process exits at the old crash window, before publishing
        # the short-term mirror. The durable pending barrier must survive it.
        script = '''
import os,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[3])
from agent_harness.native_session import session_packet
with patch('agent_harness.native_session.Memory.update_short_term', side_effect=lambda _: os._exit(73)):
    session_packet(Path(sys.argv[1]), session=sys.argv[2], goal='Different mission', expected_generation=1)
'''
        # tools/run_tests.py sets only the parent's sys.path, not PYTHONPATH.
        process = subprocess.run([sys.executable, '-c', script, str(self.state), identifier,
            str(Path(__file__).resolve().parents[1] / 'src')], capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 73, process.stderr)
        self.assertEqual((directory / 'session.json').read_bytes(), before)
        self.assertEqual(self.own(identifier).read_bytes(), checkpoint)
        pending = json.loads((directory / 'pending-generation.json').read_text())
        generation = json.loads(Path(pending['generation_path']).read_text())
        self.assertEqual(digest(generation), pending['generation_digest'])
        self.assertEqual(generation['checkpoint']['goal_generation'], 2)
        with self.assertRaisesRegex(HarnessError, 'SESSION_RECOVERY_REQUIRED'):
            native_session.session_packet(self.state, session=identifier)

    def test_crash_after_head_publication_never_returns_partial_success(self):
        identifier = self.start()['required']['session']
        real_persist = native_session.persist
        def fail_after_head(path, value, **kwargs):
            real_persist(path, value, **kwargs)
            if path.name == 'session.json':
                raise OSError('simulated process failure after head')
        with patch.object(native_session, 'persist', side_effect=fail_after_head):
            with self.assertRaises(OSError):
                native_session.session_packet(self.state, session=identifier, goal='Mission two', expected_generation=1)
        with self.assertRaisesRegex(HarnessError, 'SESSION_RECOVERY_REQUIRED'):
            native_session.session_packet(self.state, session=identifier)

    def test_crash_after_checkpoint_before_head_is_explicitly_blocked(self):
        identifier = self.start()['required']['session']
        directory = self.state / 'native-sessions' / identifier
        before = (directory / 'session.json').read_bytes()
        real_persist = native_session.persist
        def fail_before_head(path, value, **kwargs):
            if path.name == 'session.json':
                raise OSError('simulated crash after checkpoint mirror')
            real_persist(path, value, **kwargs)
        with patch.object(native_session, 'persist', side_effect=fail_before_head):
            with self.assertRaises(OSError):
                native_session.session_packet(self.state, session=identifier, goal='Mission two', expected_generation=1)
        self.assertEqual((directory / 'session.json').read_bytes(), before)
        self.assertEqual(load(self.own(identifier), SHORT_SCHEMA)['goal'], 'Mission two')
        with self.assertRaisesRegex(HarnessError, 'SESSION_RECOVERY_REQUIRED'):
            native_session.session_packet(self.state, session=identifier)

    def test_committed_goal_cas_replay_and_own_generation_binding(self):
        identifier = self.start()['required']['session']
        packet = native_session.session_packet(self.state, session=identifier, goal='Mission two', expected_generation=1, operation_id='goal-two')
        replay = native_session.session_packet(self.state, session=identifier, goal='Mission two', expected_generation=1, operation_id='goal-two')
        self.assertEqual(replay, packet)
        with self.assertRaisesRegex(HarnessError, 'GOAL_GENERATION_CONFLICT'):
            native_session.session_packet(self.state, session=identifier, goal='Mission three', expected_generation=1)
        row = load(self.own(identifier), SHORT_SCHEMA)
        row['goal_generation'] = 1
        dump(self.own(identifier), row, SHORT_SCHEMA)
        with self.assertRaisesRegex(HarnessError, 'OWN_CHECKPOINT_CORRUPT'):
            native_session.session_packet(self.state, session=identifier, goal='Mission two', expected_generation=1, operation_id='goal-two')

    def test_valid_schema_checkpoint_tampering_is_blocked(self):
        identifier = self.start()['required']['session']
        row = load(self.own(identifier), SHORT_SCHEMA)
        row['checkpoint'] = 'Invented checkpoint'
        dump(self.own(identifier), row, SHORT_SCHEMA)
        with self.assertRaisesRegex(HarnessError, 'OWN_CHECKPOINT_CORRUPT'):
            native_session.session_packet(self.state, session=identifier)

    def test_old_operation_cannot_replace_the_current_required_mission(self):
        identifier = self.start()['required']['session']
        native_session.session_packet(self.state, session=identifier, goal='Mission two', expected_generation=1, operation_id='goal-two')
        native_session.session_packet(self.state, session=identifier, goal='Mission three', expected_generation=2)
        with self.assertRaisesRegex(HarnessError, 'GOAL_GENERATION_CONFLICT'):
            native_session.session_packet(self.state, session=identifier, goal='Mission two', expected_generation=1, operation_id='goal-two')
        self.assertEqual(native_session.session_packet(self.state, session=identifier)['required']['goal'], 'Mission three')

    def test_replay_rejects_changed_source_and_preserves_immutable_operation(self):
        identifier = self.start()['required']['session']
        arguments = dict(session=identifier, note='Observed source', next_step='Review', operation_id='observed')
        original = native_session.session_packet(self.state, **arguments)
        directory = self.state / 'native-sessions' / identifier
        operation = directory / 'operation-observed.yaml'
        saved = operation.read_bytes()
        (self.root / 'one.py').write_text('def alpha():\n    return 2\n')
        with self.assertRaisesRegex(HarnessError, 'STALE_OPERATION_REPLAY: source changed'):
            native_session.session_packet(self.state, **arguments)
        self.assertEqual(operation.read_bytes(), saved)
        current = native_session.session_packet(self.state, session=identifier)
        self.assertNotEqual(current['required']['snapshot_id'], original['required']['snapshot_id'])
        self.assertEqual(current['required']['goal'], original['required']['goal'])

    def test_replay_rechecks_the_complete_requested_transport_budget(self):
        identifier = self.start()['required']['session']
        arguments = dict(session=identifier, note='Observed source', next_step='Review', operation_id='budget')
        packet = native_session.session_packet(self.state, max_bytes=65536, **arguments)
        required = packet['required']
        outer = {'session': identifier, 'root': str(self.root), 'evidence': required['packet_pointer'], 'packet': packet}
        exact = len(json_bytes(outer)) + 1
        self.assertGreater(exact, 2048)
        directory = self.state / 'native-sessions' / identifier
        before = {p.name: p.read_bytes() for p in directory.glob('generation-*.json')}
        saved_operation = (directory / 'operation-budget.yaml').read_bytes()
        with self.assertRaisesRegex(HarnessError, 'BUDGET_TOO_SMALL'):
            native_session.session_packet(self.state, max_bytes=2048, **arguments)
        self.assertEqual(native_session.session_packet(self.state, max_bytes=exact, **arguments), packet)
        self.assertEqual(native_session.session_packet(self.state, max_bytes=65536, **arguments), packet)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.glob('generation-*.json')})
        self.assertEqual(saved_operation, (directory / 'operation-budget.yaml').read_bytes())

    def test_replay_requires_current_memory_and_ledger_observations(self):
        identifier = self.start()['required']['session']
        arguments = dict(session=identifier, note='Observed context', next_step='Review', operation_id='context')
        native_session.session_packet(self.state, **arguments)
        self.memory.record_file_fact(self.root, 'one.py')
        with self.assertRaisesRegex(HarnessError, 'STALE_OPERATION_REPLAY: context proof'):
            native_session.session_packet(self.state, **arguments)
        arguments['operation_id'] = 'context-two'
        native_session.session_packet(self.state, **arguments)
        ledger = Ledger(self.state / 'native-sessions' / identifier, self.current['repository_identity'])
        ledger.append('decision', {'reason': 'invalidate cached context'}, self.current)
        with self.assertRaisesRegex(HarnessError, 'STALE_OPERATION_REPLAY: context proof'):
            native_session.session_packet(self.state, **arguments)

    def test_replay_requires_all_unique_typed_memory_scan_observers(self):
        identifier = self.start()['required']['session']
        arguments = dict(session=identifier, note='Observed context', next_step='Review', operation_id='observer')
        packet = native_session.session_packet(self.state, **arguments)
        operation = self.state / 'native-sessions' / identifier / 'operation-observer.yaml'
        saved = operation.read_bytes()
        real_recall = native_session.Memory.recall
        for mutation in ('missing', 'empty', 'missing_group', 'duplicate_group', 'unknown_group',
                         'not_list', 'malformed_row', 'unhashable_kind', 'partial', 'truthy_complete'):
            def degraded(memory, *args, **kwargs):
                result = real_recall(memory, *args, **kwargs)
                scans = result['scan_metrics']
                if mutation == 'missing':
                    result.pop('scan_metrics')
                elif mutation == 'empty':
                    result['scan_metrics'] = []
                elif mutation == 'missing_group':
                    scans.pop()
                elif mutation == 'duplicate_group':
                    scans[2] = dict(scans[0])
                elif mutation == 'unknown_group':
                    scans[2]['kind'] = 'unobserved'
                elif mutation == 'not_list':
                    result['scan_metrics'] = {'complete': True}
                elif mutation == 'malformed_row':
                    scans[2] = None
                elif mutation == 'unhashable_kind':
                    scans[2]['kind'] = ['facts']
                elif mutation == 'partial':
                    scans[2]['complete'] = False
                else:
                    scans[2]['complete'] = 1
                return result
            with self.subTest(mutation=mutation), patch.object(native_session.Memory, 'recall', new=degraded):
                with self.assertRaisesRegex(HarnessError, 'STALE_OPERATION_REPLAY: context proof'):
                    native_session.session_packet(self.state, **arguments)
            self.assertEqual(operation.read_bytes(), saved)
        self.assertEqual(native_session.session_packet(self.state, **arguments), packet)

    def test_required_mission_survives_changed_source_and_lifecycle(self):
        identifier = self.start()['required']['session']
        for source in ('resume', 'compact', 'clear', 'fork'):
            (self.root / 'one.py').write_text('# ' + source + '\n')
            packet = native_session.session_packet(self.state, session=identifier, lifecycle_source=source)
            required = packet['required']
            self.assertEqual(required['goal'], 'Repair alpha')
            self.assertEqual(required['goal_generation'], 1)
            self.assertEqual(required['goal_digest'], hashlib.sha256(b'Repair alpha').hexdigest())
            self.assertEqual(required['constraints'], native_session.SESSION_CONSTRAINTS)
            self.assertEqual(required['acceptance'], 'not_performed')

    def test_duplicate_equal_cardinality_index_cannot_hide_invalidation(self):
        ledger = Ledger(self.base / 'run', self.current['repository_identity'])
        first = ledger.append('decision', {'reason': 'initial observation'}, self.current)
        second = ledger.append('decision', {'reason': 'revoke', 'supersedes': first['event_id']}, self.current)
        dump(ledger.root / 'index.yaml', {'schema_version': 1, 'events': [first['event_id'], first['event_id']]}, INDEX_SCHEMA)
        rows = ledger._records()
        self.assertEqual({r['event_id'] for r in rows}, {first['event_id'], second['event_id']})
        packet = ledger.inform(self.current)
        self.assertEqual(packet['required']['stale_items_count'], 1)
        self.assertNotIn(first['event_id'], [item['observation']['event_id'] for item in packet['items']])

    def test_ledger_authoritative_filename_binding_and_scan_bound(self):
        ledger = Ledger(self.base / 'run', self.current['repository_identity'])
        first = ledger.append('decision', {'reason': 'observation'}, self.current)
        (ledger.events / (first['event_id'] + '.yaml')).rename(ledger.events / 'event-other.yaml')
        with self.assertRaisesRegex(HarnessError, 'LEDGER_EVENT_CORRUPT'):
            ledger._records()
        with patch('agent_harness.execution_ledger.time.monotonic', side_effect=[0, 6]):
            with self.assertRaisesRegex(HarnessError, 'LEDGER_SCAN_BOUND'):
                ledger.rebuild_index()

    def test_deep_valid_python_symbol_fact_and_native_recall(self):
        (self.root / 'one.py').write_text('def alpha():\n    return ' + '+'.join(['1'] * 1500) + '\n')
        current = native_session.current_provenance(self.root)
        fact = self.memory.record_symbol_fact(self.root, 'one.py', 'alpha')
        self.assertEqual(verify_fact(fact, current)['source_validity'], 'current')
        packet = self.start()
        self.assertTrue(packet['required']['memory'])
        with patch('agent_harness.knowledge_claims._build', side_effect=RecursionError('optional verifier exhausted')):
            recalled = self.memory.recall('alpha', current, optional=True)
        self.assertFalse(recalled['verified_facts'])
        self.assertEqual(recalled['stale_memory_prevented'], 1)
        with patch('agent_harness.knowledge_claims._build', side_effect=RuntimeError('verifier failure')):
            self.assertEqual(verify_fact(fact, current)['source_validity'], 'unknown')

    def test_no_rg_source_caps_and_invalid_utf8_are_honestly_incomplete(self):
        (self.root / 'large.py').write_text('# ' + 'x' * 262145 + '\nalpha()\n')
        (self.root / 'invalid.py').write_bytes(b'\xff\nalpha()\n')
        with patch('agent_harness.repomap_queries.shutil.which', return_value=None):
            result = query_repository(self.root, 'symbol-context', path='one.py', symbol='alpha')
        self.assertFalse(result['results_complete'])
        self.assertIsNone(result['total_results'])
        self.assertIn('source_byte_bound', result['truncation_reasons'])
        self.assertIn('source_encoding_unavailable', result['truncation_reasons'])
        self.assertEqual(result['skipped_source_count'], 2)

    @unittest.skipIf(os.name == 'nt', 'POSIX padded executable control')
    def test_large_real_executable_core_evidence_streams_and_rechecks_bytes(self):
        original = shutil.which('true')
        if not original:
            self.skipTest('true unavailable')
        executable = self.base / 'large-tool'
        shutil.copyfile(Path(original).resolve(), executable)
        with executable.open('ab') as output:
            output.write(b'\0' * (17 * 1024 * 1024))
        executable.chmod(0o755)
        run = Run.create(self.base / 'run', {'schema_version': 1, 'run_id': 'large-tool', 'task_id': 'alpha',
            'attempt_id': 'a1', 'workflow': 'change', 'process_level': 'standard', 'goal': 'Check alpha',
            'cwd': str(self.root), 'permissions': ['read', 'test'], 'scope': {'write_paths': []},
            'checks': [{'id': 'alpha', 'kind': 'command', 'required': True, 'argv': [str(executable)], 'timeout': 5}]})
        run.freeze()
        self.assertEqual(run_check(run, 'alpha')['status'], 'passed')
        ref = self.memory.register_core_evidence(run.path, 'alpha', self.current)
        self.assertTrue(Path(ref['path']).is_file())
        with executable.open('ab') as output:
            output.write(b'changed')
        with self.assertRaisesRegex(HarnessError, 'executable identity changed'):
            self.memory.register_core_evidence(run.path, 'alpha', self.current)

    @unittest.skipIf(os.name == 'nt', 'POSIX spelling; Windows UNC remains supported')
    def test_double_slash_root_is_refused_before_backend_or_inventory(self):
        subprocess.run(['git', '-C', str(self.root), 'init', '-q'], check=True)
        (self.root / '.gitignore').write_text('ignored.py\n')
        (self.root / 'ignored.py').write_text('ignored = True\n')
        ordinary = snapshot_python({'cwd': str(self.root)})
        self.assertEqual(ordinary['kind'], 'git')
        self.assertNotIn('ignored.py', ordinary['files'])
        for backend in (snapshot, snapshot_python):
            with self.assertRaisesRegex(HarnessError, 'UNSUPPORTED_ROOT_SPELLING'):
                backend({'cwd': '/' + str(self.root)})
        target = self.base / 'alias'
        target.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(HarnessError):
            snapshot_python({'cwd': str(target)})


if __name__ == '__main__':
    unittest.main()
