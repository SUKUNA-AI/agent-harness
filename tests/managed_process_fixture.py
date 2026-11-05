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
        self.transport_results = []

    def run(self, prompt, *, on_process=None, **options):
        trace = {}
        def bind_then_release(process):
            identity = process.get('identity', process)
            try:
                if on_process is None:
                    raise AssertionError('Managed fixture requires the real ownership callback')
                on_process(process)
                self.bound_children.append(identity)
            except BaseException as error:
                trace['bind_error'] = (type(error).__name__ + ': ' + str(error))[:1024]
                raise
            finally:
                # The synthetic Python script is the second executable argv.
                # This marker is outside the task worktree and cannot affect
                # its candidate snapshot or ownership/scope rejection checks.
                Path(self.executable[1]).with_suffix('.bind-'+str(identity['pid'])).write_text('bound')
        result = super().run(prompt, on_process=bind_then_release, **options)
        trace.update({key: result.get(key) for key in ('status', 'error', 'usage', 'turn_started',
            'turn_completed', 'outcome_known', 'process_tree_stopped', 'exit_code', 'logs',
            'process', 'events', 'elapsed_seconds')})
        self.transport_results.append(trace)
        return result

    def failure_summary(self, result):
        """Bounded synthetic diagnostics; preserve the original bind exception."""
        traces = []
        for saved in self.transport_results[-8:]:
            trace = {key: value for key, value in saved.items() if key != 'logs'}
            trace['tails'] = {}
            for name, path in (saved.get('logs') or {}).items():
                try:
                    with Path(path).open('rb') as stream:
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 1024))
                        trace['tails'][name] = stream.read(1024).decode('utf-8', errors='replace')
                except OSError as error:
                    trace['tails'][name] = type(error).__name__
            traces.append(trace)
        return {'status': result.get('status'), 'reason': str(result.get('reason', ''))[:1024],
            'unknown_invocation_ids': result.get('metrics', {}).get('unknown_invocation_ids'),
            'transports': traces}
