"""Test-only process handshake for fast managed recovery fixtures.

A fixture must not exit before the real ModelLease.bind persists its live exact
identity. Production deliberately treats that race as unknown. This helper
keeps the child alive through on_process, including arbitrarily delayed bind,
and releases it after the real callback returns (also on error for cleanup).
"""
from pathlib import Path

from agent_harness.codex_execution import CodexAdapter


def bound_fake_script(script):
    marker = 'prompt=args[-1]'
    if script.count(marker) != 1:
        raise ValueError('Managed fake must have exactly one prompt entry point')
    return script.replace(marker, '''prompt=args[-1]
import os
bound=pathlib.Path(__file__).with_suffix('.bind-'+str(os.getpid()))
end=time.monotonic()+30
while not bound.exists():
 if time.monotonic()>=end: raise RuntimeError('fixture bind handshake timed out')
 time.sleep(0.01)
bound.unlink()
''')


class BoundFixtureCodexAdapter(CodexAdapter):
    def __init__(self, executable=('codex',)):
        super().__init__(executable)
        self.bound_children = []

    def run(self, prompt, *, on_process=None, **options):
        def bind_then_release(process):
            identity = process.get('identity', process)
            try:
                if on_process is None:
                    raise AssertionError('Managed fixture requires the real ownership callback')
                on_process(process)
                self.bound_children.append(identity)
            finally:
                # The synthetic Python script is the second executable argv.
                # This marker is outside the task worktree and cannot affect
                # its candidate snapshot or ownership/scope rejection checks.
                Path(self.executable[1]).with_suffix('.bind-'+str(identity['pid'])).write_text('bound')
        return super().run(prompt, on_process=bind_then_release, **options)
