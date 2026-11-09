"""Canonical spelling must not weaken per-trial disjoint state guards."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.benchmark_runner import _trial_descendant


class BenchmarkPathTests(unittest.TestCase):
    def test_both_operands_use_same_canonical_spelling(self):
        # Reproduce Windows ordinary vs extended canonical spelling without
        # claiming Windows runtime qualification from this Linux control.
        root = Path.cwd() / "trial"
        canonical = Path.cwd() / "canonical" / "trial"
        def resolve(path, *, strict=False):
            self.assertTrue(strict)
            return canonical.parent / path.relative_to(root.parent)
        with patch.object(Path, "resolve", resolve):
            self.assertTrue(_trial_descendant(root, root / "source"))
            self.assertFalse(_trial_descendant(root, root))
            self.assertFalse(_trial_descendant(root, root.parent / "peer" / "source"))
            self.assertFalse(_trial_descendant(root, Path("source")))

    def test_actual_separate_trial_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("trial/source", "peer/source"):
                (root / name).mkdir(parents=True)
            self.assertTrue(_trial_descendant(root / "trial", root / "trial/source"))
            self.assertFalse(_trial_descendant(root / "trial", root / "peer/source"))
