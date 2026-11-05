"""A published bind marker must not still have an open writer on Windows."""
from pathlib import Path
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src")]
from agent_harness.agent_lifecycle import GlobalSlots
from managed_process_fixture import BoundFixtureCodexAdapter, bound_fake_script


class ManagedFixturePublicationTests(unittest.TestCase):
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
