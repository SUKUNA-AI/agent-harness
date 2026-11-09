"""Legacy Windows references address the same contained artifact on every OS."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from agent_harness import plan_quality
from agent_harness.supervisor_store import SupervisorStore


class PlanQualityPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.supervisor = SimpleNamespace(path=root, store=SupervisorStore(root))

    def test_legacy_separators_read_the_addressed_artifact_without_rewriting_reference(self):
        value = {'goal': 'preserve the original input'}
        reference = plan_quality._publish(self.supervisor, value)
        self.assertTrue(reference['path'].startswith('context/required-'))
        legacy = {**reference, 'path': reference['path'].replace('/', '\\')}
        original = dict(legacy)
        self.assertEqual(plan_quality._read(self.supervisor, legacy), value)
        self.assertEqual(legacy, original)

    @unittest.skipIf(os.name == 'nt', 'POSIX literal backslash leaf')
    def test_legacy_reference_cannot_select_a_literal_backslash_file_outside_context(self):
        reference = plan_quality._publish(self.supervisor, {'goal': 'owned context only'})
        addressed = self.supervisor.store.path(reference['path'])
        legacy = {**reference, 'path': reference['path'].replace('/', '\\')}
        (self.supervisor.path / legacy['path']).write_bytes(addressed.read_bytes())
        addressed.unlink()
        with self.assertRaises(FileNotFoundError):
            plan_quality._read(self.supervisor, legacy)
