"""A published bind marker must not still have an open writer on Windows."""
from pathlib import Path
from contextlib import ExitStack, contextmanager
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src")]
from agent_harness import agent_lifecycle
from agent_harness.agent_lifecycle import GlobalSlots, SlotCapacityUnavailable, SlotWaitCancelled, MAX_MANAGED_MODEL_SLOTS
from managed_process_fixture import BoundFixtureCodexAdapter, bound_fake_script


class ManagedFixturePublicationTests(unittest.TestCase):
    def test_admission_rechecks_cancellation_and_budget_after_slow_boundaries(self):
        for role, boundary in (('reviewer', 'inventory'), ('owner', 'guard'),
                               ('reviewer', 'guard'), ('owner', 'write'), ('reviewer', 'write')):
            for reason in ('cancel', 'timeout'):
                with self.subTest(role=role, boundary=boundary, reason=reason), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    registry = GlobalSlots(root / 'state')
                    cancel = root / 'cancel'
                    clock = [0.0]
                    def elapsed():
                        if reason == 'cancel':
                            cancel.touch()
                        else:
                            clock[0] = 2.0
                    inventory, write, lock = registry.inventory, registry.store.write, agent_lifecycle._os_lock
                    def slow_inventory(*args, **options):
                        value = inventory(*args, **options)
                        elapsed()
                        return value
                    def slow_write(*args, **options):
                        value = write(*args, **options)
                        elapsed()
                        return value
                    @contextmanager
                    def slow_guard(path):
                        with lock(path):
                            if Path(path).name.startswith('slot-'):
                                elapsed()
                            yield
                    target, name, replacement = {
                        'inventory': (registry, 'inventory', slow_inventory),
                        'guard': (agent_lifecycle, '_os_lock', slow_guard),
                        'write': (registry.store, 'write', slow_write)}[boundary]
                    expected = SlotWaitCancelled if reason == 'cancel' else SlotCapacityUnavailable
                    with patch.object(agent_lifecycle.time, 'monotonic', side_effect=lambda: clock[0]), \
                            patch.object(target, name, replacement):
                        with self.assertRaises(expected):
                            with registry.acquire('run', 'task', role, cancel_file=cancel, timeout=1) as lease:
                                lease.complete()
                    self.assertFalse(any(row['unresolved'] for row in registry.inventory()))

    def test_cross_run_reviewer_admission_and_crashed_reservation(self):
        script = '''import pathlib,sys,time
sys.path.insert(0,sys.argv[1])
from agent_harness.agent_lifecycle import GlobalSlots,SlotCapacityUnavailable
root=pathlib.Path(sys.argv[2]);name=sys.argv[3]
registry=GlobalSlots(root/'state')
(root/(name+'.ready')).touch()
while not (root/'start').exists():time.sleep(.01)
try:
 with registry.acquire(name,'review','reviewer',timeout=1) as lease:
  (root/(name+'.acquired')).touch()
  end=time.monotonic()+20
  while not (root/'release').exists():
   if time.monotonic()>end:raise RuntimeError('release barrier timed out')
   time.sleep(.01)
  lease.complete()
except SlotCapacityUnavailable:(root/(name+'.blocked')).touch()
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            children = []
            def wait_for(predicate):
                deadline = time.monotonic() + 15
                while not predicate():
                    if time.monotonic() >= deadline:
                        self.fail("Reviewer subprocess barrier timed out")
                    time.sleep(.02)
            try:
                for index in range(4):
                    children.append(subprocess.Popen([sys.executable, '-c', script,
                        str(Path(__file__).resolve().parents[1] / 'src'), str(root), str(index)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                wait_for(lambda: len(list(root.glob('*.ready'))) == 4)
                (root / 'start').touch()
                wait_for(lambda: len(list(root.glob('*.acquired'))) + len(list(root.glob('*.blocked'))) == 4)
                acquired = list(root.glob('*.acquired'))
                self.assertEqual(len(acquired), 3)
                victim = children[int(acquired[0].stem)]
                victim.kill(); victim.wait(timeout=5)
                registry = GlobalSlots(root / 'state')
                self.assertEqual(sum(row['unresolved'] and row['role'] == 'reviewer'
                    for row in registry.inventory()), 3)
                with self.assertRaises(SlotCapacityUnavailable):
                    with registry.acquire('later', 'review', 'reviewer', timeout=.15):
                        self.fail('A crashed unresolved reservation still consumes reviewer capacity')
                with registry.acquire('later', 'implementation', 'owner', timeout=1) as lease:
                    lease.complete()
                (root / 'release').touch()
                for child in children:
                    if child is victim:
                        continue
                    stdout, stderr = child.communicate(timeout=10)
                    self.assertEqual(child.returncode, 0, (stdout, stderr))
                self.assertEqual(sum(row['unresolved'] for row in registry.inventory()), 1)
                with ExitStack() as stack:
                    leases = [stack.enter_context(registry.acquire('later', str(i), 'reviewer', timeout=1)) for i in range(2)]
                    with self.assertRaises(SlotCapacityUnavailable):
                        with registry.acquire('later', 'overflow', 'reviewer', timeout=.15):
                            self.fail('An unresolved reviewer must remain counted after other runs finish')
                    for lease in leases:
                        lease.complete()
            finally:
                (root / 'release').touch()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)

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
