"""Native-client fixture: actual MCP child, config filtering, two event protocols."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
args = sys.argv[1:]
if '--version' in args:
    print('opencode v2.2.5'); raise SystemExit
if '--help' in args:
    print('exec resume SESSION_ID --json --output-schema; run --format json --session SESSION --standalone; session list export'); raise SystemExit
prompt=args[-1]
bound = Path(__file__).with_suffix('.bind-' + str(os.getpid()))
end = time.monotonic() + 30
while not bound.exists():
    if time.monotonic() > end: raise RuntimeError('fixture binding timeout')
    time.sleep(.01)
bound.unlink()
open_backend = '--format' in args
import uuid
resume_flag = '--session' if open_backend else 'resume'
session = args[args.index(resume_flag)+1] if resume_flag in args else 'session_' + uuid.uuid4().hex
session_file=Path(__file__).with_suffix('.session-' + session + '.json')
if resume_flag in args:
    assert session_file.exists() and json.loads(session_file.read_text())=={'session':session,'backend':'opencode' if open_backend else 'codex','cwd':str(Path.cwd())}
else:
    assert not session_file.exists()
    session_file.write_text(json.dumps({'session':session,'backend':'opencode' if open_backend else 'codex','cwd':str(Path.cwd())}))
with Path(__file__).with_suffix('.calls').open('a') as calls:
    calls.write(json.dumps({'session':session,'resume':resume_flag in args,'cwd':str(Path.cwd()),'backend':'opencode' if open_backend else 'codex'})+'\n')

def emit(kind, **data):
    if not open_backend:
        print(json.dumps(dict(type=kind, **data)), flush=True); return
    part = {'sessionID': session, 'messageID': 'message_1'}
    if kind == 'thread.started': return
    if kind == 'turn.started': native='step_start'
    elif kind == 'turn.completed': native='step_finish'; part['reason']='stop'
    elif kind == 'turn.failed': native='error'
    elif data.get('item', {}).get('type') == 'agent_message': native='text'; part['text']=data['item']['text']
    else:
        native='tool_use'; part.update(tool='fixture_search_symbol',state={'status':'completed','output':data['item']['result']})
    print(json.dumps({'type':native,'sessionID':session,'part':part}), flush=True)

emit('thread.started',thread_id=session); emit('turn.started')
if 'HARNESS_FINAL_REVIEW' in prompt:
    emit('item.completed',item={'type':'agent_message','text':json.dumps({'schema_version':1,'verdict':'approved','findings':[]})})
    emit('turn.completed',usage={'input_tokens':10,'output_tokens':3}); raise SystemExit
payload=json.loads(prompt.rsplit('\n',1)[-1])
goal=payload.get('goal',payload.get('objective','managed'))
if goal=='pause' and resume_flag not in args:
    time.sleep(30)
if open_backend:
    specs=json.loads(os.environ['OPENCODE_CONFIG_CONTENT']).get('mcp',{}).get('servers',{})
else:
    specs={}
    for i,arg in enumerate(args[:-1]):
        if arg=='-c' and args[i+1].startswith('mcp_servers.'):
            key,value=args[i+1].split('=',1);specs[json.loads(key.split('.',1)[1])]=tomllib.loads('value='+value)['value']
for name,spec in specs.items():
    if spec.get('enabled') is False: continue
    command=spec['command'] if isinstance(spec['command'],list) else [spec['command'],*spec.get('args',[])]
    with subprocess.Popen(command,cwd=spec.get('cwd'),env=dict(os.environ,**spec.get('env',spec.get('environment',{}))),stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True) as server:
        def rpc(i,method,params):
            server.stdin.write(json.dumps({'jsonrpc':'2.0','id':i,'method':method,'params':params})+'\n');server.stdin.flush()
            response=json.loads(server.stdout.readline());assert response['id']==i;return response['result']
        rpc(1,'initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}})
        catalog=rpc(2,'tools/list',{})['tools']
        allowed=set(spec.get('enabled_tools',['search_symbol','read_file']))-set(spec.get('disabled_tools',[]))
        assert 'write_file' not in allowed and 'execute_command' not in allowed
        assert 'search_symbol' in allowed and any(t['name']=='search_symbol' for t in catalog)
        result=rpc(3,'tools/call',{'name':'search_symbol','arguments':{'symbol':'shared_symbol'}})
        if not result.get('isError'):
            body=json.loads(result['content'][0]['text']);assert body['cwd']==str(Path.cwd()) and body['project']==str(Path.cwd())
        if not os.environ.get('HARNESS_FIXTURE_OMIT_MCP'):
            emit('item.completed',item={'type':'mcp_tool_call','server':name,'tool':'search_symbol','status':'completed','result':result})
        server.stdin.close();server.wait(timeout=3)
if goal in ('left','right','pause'):
    target=('left' if goal=='pause' else goal)+'.py'
    Path(target).write_text('VALUE = 42\n')
emit('turn.completed',usage={'input_tokens':10,'output_tokens':3})
