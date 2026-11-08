"""A published bind marker must not still have an open writer on Windows."""
from pathlib import Path
from contextlib import ExitStack
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src")]
from agent_harness.agent_lifecycle import GlobalSlots, SlotCapacityUnavailable, MAX_MANAGED_MODEL_SLOTS
from managed_process_fixture import BoundFixtureCodexAdapter, bound_fake_script


class ManagedFixturePublicationTests(unittest.TestCase):
    def test_twelve_global_model_slots_but_no_more_than_three_reviewers(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = GlobalSlots(Path(temp) / "state")
            with ExitStack() as stack:
                reviewers = [stack.enter_context(registry.acquire("run", f"review-{i}", "reviewer", timeout=1))
                             for i in range(3)]
                with self.assertRaises(SlotCapacityUnavailable):
                    with registry.acquire("another-run", "review-4", "reviewer", timeout=0.15):
                        self.fail("Fourth reviewer must not acquire a model slot")
                workers = [stack.enter_context(registry.acquire("run", f"worker-{i}", "owner", timeout=1))
                           for i in range(MAX_MANAGED_MODEL_SLOTS - len(reviewers))]
                self.assertEqual(len(registry.inventory()), MAX_MANAGED_MODEL_SLOTS)
                with self.assertRaises(SlotCapacityUnavailable):
                    with registry.acquire("other", "overflow", "owner", timeout=0.15):
                        self.fail("Thirteenth model invocation must not acquire a slot")
                for lease in reviewers + workers:
                    lease.complete()

    def test_real_child_consumes_only_a_closed_binding_marker(self):
        script = '''import sys,json,pathlib,time,uuid,os
args=sys.argv[1:]
prompt=args[-1]
def event(t,**v):print(json.dumps(dict(type=t,**v)),flush=True)
event('thread.started',thread_id=str(uuid.uuid4()));event('turn.started')
event('turn.completed',usage={'input_tokens':3,'output_tokens':1})
'''
        script = bound_fake_script(script)
        # POSIX models Windows delete-sharing with an actual file lock. On
        # Windows unlink itself refuses a marker whose writer is still open.
        script = script.replace('bound.unlink()', '''if os.name != 'nt':
 import fcntl
 with bound.open('rb') as proof:
  fcntl.flock(proof.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
bound.unlink()''')
        original_open = Path.open
        def hold_visible_writer(path, mode='r', *args, **options):
            stream = original_open(path, mode, *args, **options)
            if '.bind-' in path.name and 'w' in mode:
                if os.name != 'nt':
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                # A direct write publishes the path before the file closes.
                # Give the real child time to exercise that unsafe interval.
                time.sleep(.3)
            return stream
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / 'fake.py'
            fake.write_text(script, encoding='utf-8')
            adapter = BoundFixtureCodexAdapter((sys.executable, str(fake)))
            with GlobalSlots(root / 'state').acquire('fixture', 'task', 'owner') as lease:
                with patch.object(Path, 'open', hold_visible_writer):
                    result = adapter.run('synthetic fixture', cwd=str(root), timeout=5,
                        allow_non_git=True, on_process=lease.bind)
                lease.complete(result)
            self.assertEqual(result['status'], 'completed', adapter.failure_summary(result))
            self.assertEqual(result['usage'], {'input_tokens':3, 'output_tokens':1})
            self.assertEqual(len(adapter.bound_children), 1)


if __name__ == '__main__':
    unittest.main()
