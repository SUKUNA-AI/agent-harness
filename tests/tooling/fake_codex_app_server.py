"""Bounded synthetic JSONL RPC peer; no provider, auth, network, or native proof."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def send(value):
    print(json.dumps(value), flush=True)


def main():
    root, mode = Path(sys.argv[1]), sys.argv[2]
    (root / 'fake.pid').write_text(str(os.getpid()))
    with (root / 'starts.jsonl').open('a') as output:
        output.write(json.dumps({'pid': os.getpid(), 'mode': mode}) + '\n')
    done = False
    thread = 'thread-fixture'
    for raw in sys.stdin:
        request = json.loads(raw)
        method, ident = request.get('method'), request.get('id')
        with (root / 'calls.jsonl').open('a') as output:
            output.write(json.dumps({'pid': os.getpid(), 'method': method,
                                     'params': request.get('params', {})}) + '\n')
        if ident is None:
            continue
        if method == 'initialize':
            if mode == 'init_stall':
                (root / 'initialize-observation.json').write_text(json.dumps({
                    'callback_seen': (root / 'callback.json').exists()}))
                time.sleep(8)
            result = {}
        elif method == 'hooks/list':
            result = {'data': []}
        elif method in ('thread/start', 'thread/resume'):
            state = root / 'thread.json'
            if method == 'thread/start':
                if state.exists():
                    send({'id': ident, 'error': {'message': 'Fresh fallback forbidden'}})
                    continue
                state.write_text(json.dumps({'id': thread}))
            else:
                requested = request['params']['threadId']
                if not state.exists() or json.loads(state.read_text())['id'] != requested:
                    send({'id': ident, 'error': {'message': 'Unknown exact session'}})
                    continue
                thread = requested
                if mode == 'wrong_resume':
                    thread = 'thread-wrong'
            result = {'thread': {'id': thread, 'cwd': str(root)}, 'model': 'fixture',
                      'modelProvider': 'fixture',
                      'sandbox': {'type': 'workspaceWrite', 'writableRoots': [str(root)]}}
        elif method == 'mcpServerStatus/list':
            send({'id': ident, 'result': {'data': [], 'nextCursor': None}})
            if done and mode == 'trailing_partial':
                sys.stdout.write('{"method":')
                sys.stdout.flush()
                time.sleep(8)
            continue
        elif method == 'turn/start':
            response_turn = 'turn-wrong' if mode == 'wrong_turn_response' else 'turn-fixture'
            send({'id': ident, 'result': {'turn': {'id': response_turn, 'status': 'inProgress'}}})
            send({'method': 'turn/started', 'params': {
                'threadId': thread, 'turn': {'id': 'turn-fixture'}}})
            if mode == 'turn_stall':
                child_code = ('import pathlib,signal,sys,time;'
                              'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                              'pathlib.Path(sys.argv[1]).write_text("ready");'
                              'time.sleep(8)')
                child = subprocess.Popen([sys.executable, '-B', '-c', child_code,
                                          str(root / 'descendant-ready')],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                (root / 'descendant.pid').write_text(str(child.pid))
                time.sleep(8)
                continue
            terminal_turn = 'turn-wrong' if mode == 'wrong_turn_terminal' else 'turn-fixture'
            send({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {
                'id': terminal_turn, 'status': 'completed'}}})
            done = True
            continue
        else:
            result = {}
        send({'id': ident, 'result': result})


if __name__ == '__main__':
    main()
