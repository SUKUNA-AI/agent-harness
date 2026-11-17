"""Zero-inference native policy qualification against a private scripted provider.

No provider credential is read/copied. This is a bounded local protocol test,
not an inference backend, proxy, cloud availability test or operator attestation.
"""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from .codex_execution import CodexAdapter,sanitized
from .codex_tool_catalog import _toml
from .core import HarnessError,digest
from .file_io import read_regular_file

MCP_FIXTURE=r'''import json,pathlib,sys
root=pathlib.Path(sys.argv[1]);counter=root/'calls.jsonl'
for line in sys.stdin:
 if len(line)>1048576:raise SystemExit(2)
 q=json.loads(line)
 if 'id' not in q:continue
 method=q.get('method');value={}
 if method=='initialize':value={'protocolVersion':q['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'harness-native-policy-fixture','version':'1'}}
 elif method=='tools/list':value={'tools':[{'name':name,'inputSchema':{'type':'object','properties':{}},'annotations':{'readOnlyHint':name in ['read_file','search_symbol']}} for name in ['read_file','search_symbol','write_file','execute_command']]}
 elif method=='tools/call':
  name=q['params']['name']
  with counter.open('a') as output:output.write(json.dumps(name)+'\n')
  if name in ['write_file','execute_command']:(root/'sentinel.txt').write_text('FORBIDDEN')
  value={'content':[{'type':'text','text':'harness-native-isolated-read'}]}
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':value}),flush=True)
'''


def probe(adapter,metadata,role,folder,*,timeout=60):
    """Drive the installed native dispatcher, including valid namespaced calls."""
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    deadline=time.monotonic()+timeout
    state={'requests':[],'transports':[],'mode':None,'calls':0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            length=int(self.headers.get('Content-Length',0))
            if not 0<length<=8*1024*1024:self.send_error(413);return
            body=json.loads(self.rfile.read(length));state['calls']+=1
            additions=[item.get('tools') for item in body.get('input',[]) if isinstance(item,dict) and item.get('type')=='additional_tools']
            tools=additions[-1] if additions else body.get('tools',[])
            state['requests'].append(tools)
            state['transports'].append('additional_tools' if additions else 'top_level')
            name=state['mode']
            message={'id':'msg_fixture','type':'message','role':'assistant','status':'completed',
                'content':[{'type':'output_text','text':'harness-native-qualified','annotations':[]}]}
            if state['calls']==1 and name:
                message={'id':'call_fixture','type':'function_call','call_id':'call_fixture','name':name,
                    'arguments':json.dumps({'cmd':'printf FORBIDDEN > forbidden.txt','command':'printf FORBIDDEN > forbidden.txt',
                        'path':'sentinel.txt','content':'FORBIDDEN','name':'shared_symbol'})}
                if name.startswith('mcp__fixture__'):
                    message.update(namespace='mcp__fixture',name=name[len('mcp__fixture__'):])
                elif metadata.get('use_responses_lite'):
                    message['namespace']='functions'
            response={'id':'resp_fixture','status':'completed','output':[message],
                'usage':{'input_tokens':10,'output_tokens':3,'total_tokens':13}}
            events=[{'type':'response.created','response':dict(response,status='in_progress',output=[])},
                {'type':'response.output_item.done','output_index':0,'item':message},
                {'type':'response.completed','response':response}]
            data=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    observations=[];builtin=None
    try:
        with tempfile.TemporaryDirectory(prefix='harness-codex-native-policy-') as td:
            private=Path(td);home=private/'home';home.mkdir();project=private/'project';project.mkdir()
            sentinel=project/'sentinel.txt';sentinel.write_text('BEFORE')
            mcp=private/'mcp.py';mcp.write_text(MCP_FIXTURE)
            # Preserve the selected model's full/Lite path as well as its tool
            # selection fields. Lite carries definitions in additional_tools.
            model=copy.deepcopy(metadata)
            catalog=private/'models.json';catalog.write_text(json.dumps({'models':[model]}))
            from .codex_qualification import DISABLED
            config={'model':model['slug'],'model_provider':'harness-qualification',
                'model_providers':{'harness-qualification':{'name':'Harness local policy qualification','wire_api':'responses',
                    'base_url':'http://127.0.0.1:'+str(server.server_port)+'/v1','requires_openai_auth':False,'supports_websockets':False}},
                'model_catalog_json':str(catalog),'sandbox_mode':'read-only','approval_policy':'never','web_search':'disabled',
                'features':{name:False for name in (*DISABLED,'enable_request_compression')},
                'tools':{'update_plan':{'enabled':False},'experimental_request_user_input':{'enabled':False}},
                'mcp_servers':{'fixture':{'command':sys.executable,'args':[str(mcp),str(project)],
                    'enabled_tools':['read_file','search_symbol'],'disabled_tools':['write_file','execute_command'],
                    'required':True,'startup_readiness':'connection'}}}
            arguments=[]
            for key,value in config.items():arguments+=['-c',key+'='+_toml(value)]
            environment={key:value for key,value in os.environ.items()
                if key in {'PATH','SYSTEMROOT','WINDIR','PATHEXT','COMSPEC','TMP','TEMP','TMPDIR'}}
            environment.update(HOME=str(private),CODEX_HOME=str(home))
            native=CodexAdapter((*adapter.executable,*arguments),environment=environment)
            modes=['mcp__fixture__read_file','exec_command','spawn_agent','mcp__fixture__write_file','mcp__fixture__execute_command']
            if role!='worker':modes.insert(2,'apply_patch')
            for index,mode in enumerate(modes):
                remaining=deadline-time.monotonic()
                if remaining<=0:raise HarnessError('Native policy qualification deadline exhausted')
                state.update(requests=[],transports=[],mode=mode,calls=0)
                counter=project/'calls.jsonl'
                if counter.exists():counter.unlink()
                result=native.run('Use the supplied scripted response, then finish.',cwd=project,
                    allow_non_git=True,read_only=True,timeout=min(20,remaining),log_dir=folder/str(index))
                if not state['requests'] or not result.get('process_tree_stopped'):
                    raise HarnessError('Native policy probe did not reach/reap the installed client')
                tools=state['requests'][0]
                (folder/('catalog-'+str(index)+'.json')).write_text(json.dumps({'schema_version':1,'role':role,
                    'scenario':mode,'catalog_transport':state['transports'][0],'tools':sanitized(tools)},
                    ensure_ascii=False,sort_keys=True,indent=2)+'\n')
                if metadata.get('use_responses_lite') and state['transports'][0]!='additional_tools':
                    raise HarnessError('Selected native Lite catalog path was not observed')
                namespace=[tool for tool in tools if tool.get('type')=='namespace' and tool.get('name')=='mcp__fixture']
                if len(namespace)!=1 or {tool['name']for tool in namespace[0].get('tools',[])}!={'read_file','search_symbol'}:
                    raise HarnessError('Native MCP model-visible allowlist failed')
                allowed={'list_mcp_resources','list_mcp_resource_templates','read_mcp_resource'}
                if role=='worker' and metadata.get('apply_patch_tool_type') is not None:allowed.add('apply_patch')
                actual_builtin=[]
                for tool in tools:
                    if tool.get('name')=='mcp__fixture':continue
                    if tool.get('type')=='namespace':
                        if tool.get('name')!='functions' or not isinstance(tool.get('tools'),list):
                            raise HarnessError('Unqualified native tool namespace')
                        for leaf in tool['tools']:
                            if leaf.get('type') not in {'function','custom'}:
                                raise HarnessError('Unqualified nested native tool type')
                            actual_builtin.append({**leaf,'namespace':'functions',
                                'namespace_description':tool.get('description','')})
                    else:
                        if tool.get('type') not in {'function','custom'}:
                            raise HarnessError('Unqualified native tool type')
                        actual_builtin.append(tool)
                if any(tool.get('name') not in allowed for tool in actual_builtin):
                    raise HarnessError('Native privileged builtin escaped its policy ceiling')
                if builtin is None:builtin=actual_builtin
                elif builtin!=actual_builtin:raise HarnessError('Native policy catalog changed during qualification')
                calls=[json.loads(line) for line in counter.read_text().splitlines()] if counter.exists() else []
                if (mode=='mcp__fixture__read_file' and calls!=['read_file'] or mode!='mcp__fixture__read_file' and calls
                        or sentinel.read_text()!='BEFORE' or (project/'forbidden.txt').exists()):
                    raise HarnessError('Native unsolicited tool call bypassed its policy')
                observations.append({'scenario':mode,'provider_requests':len(state['requests']),'mcp_calls':calls,
                    'catalog_transport':state['transports'][0],
                    'catalog_digest':digest(tools),'sentinel_unchanged':True,'process_tree_stopped':True,
                    'logs':result['logs'],'log_sha256':result['log_sha256']})
        result={'schema_version':1,'kind':'codex-native-policy-probe','provider':'local_scripted_fixture',
            'cloud_model_qualified':False,'metadata_digest':digest(metadata),'role':role,
            'builtins':builtin,'observations':observations}
        raw=(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n').encode()
        path=folder/'policy-probe.json';path.write_bytes(raw)
        return {'path':str(path.resolve()),'sha256':__import__('hashlib').sha256(raw).hexdigest()},builtin
    finally:
        server.shutdown();server.server_close();worker.join(timeout=2)
